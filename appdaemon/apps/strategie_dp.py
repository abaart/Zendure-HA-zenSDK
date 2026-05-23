"""
Dynamisch Handelen - Batterij Optimalisatie via Dynamic Programming
===================================================================

Pure Python module, geen Home Assistant afhankelijkheden.
Volledig testbaar met pytest zonder HA te starten.

KERNPRINCIPE
------------
Gegeven alle bekende toekomstige elektriciteitsprijzen en de huidige
accustatus berekent dit algoritme de winstmaximaliserende laad/ontlaad
strategie via Dynamic Programming (DP).

WAAROM DYNAMIC PROGRAMMING?
----------------------------
We weten alle toekomstige prijzen, maar de waarde van een beslissing
nu (bijv. laden) hangt af van toekomstige kansen (bijv. een piek over
3 uur). Door achterwaarts te werken — van het laatste slot terug naar
nu — berekenen we voor elke combinatie van (tijdstip, SoC) wat de
maximale toekomstige winst is. Zo weten we bij elk tijdstip precies
wat de beste actie is, rekening houdend met de volledige horizon.

WAAROM WERKT HET MET VASTE SOC-STAPPEN (DISCRETISATIE)?
---------------------------------------------------------
Het DP-algoritme bouwt een opzoektabel: voor elk (tijdstip, SoC)-punt
de beste actie. Die tabel werkt alleen als het aantal SoC-niveaus
eindig en vooraf bekend is.

Met exacte drijvende-kommaberekeningen groeit de tabel onbeheersbaar:
elke laad- of ontlaadactie levert een uniek SoC-getal op dat nooit
exact overeenkomt met een eerder berekend punt. Na 10 slots heb je al
duizenden unieke SoC-waarden — de tabel groeit exponentieel.

De oplossing: SoC wordt opgedeeld in vaste stappen van SOC_STAP_KWH
(standaard 50 Wh). Een accu van 2,4 kWh heeft dan altijd precies 49
niveaus, ongeacht het aantal tijdslots. Dit maakt de tabel eindig en
de berekening O(T × S) in plaats van exponentieel.

De afrondingsfout per stap is maximaal SOC_STAP_KWH/2 = 25 Wh — ruim
binnen de meetonnauwkeurigheid van de SoC-sensoren zelf.

ALTERNATIEF: LINEAR PROGRAMMING (LP)
-------------------------------------
LP kan het probleem exact oplossen zonder discretisatie, door direct te
vragen: "welke laad/ontlaad-vermogens over alle slots maximaliseren de
winst?" Nadelen voor deze toepassing:

- Vereist scipy.optimize (externe dependency)
- Derating maakt de vermogensgrens SoC-afhankelijk → niet-lineair,
  buiten bereik van standaard LP-solvers
- Minder transparant en lastiger te debuggen

DP is hier de betere keuze: eenvoudig, transparant, geen dependencies,
en de discretisatiefout is in de praktijk verwaarloosbaar.

CONSISTENTIE VAN DISCRETISATIE
--------------------------------
Alle berekeningen in dit module — zowel de backwards pass als de
forwards extractie — gebruiken uitsluitend gekwantiseerde SoC-waarden
(veelvouden van SOC_STAP_KWH). Dit is essentieel om twee subtiele fouten
te vermijden:

1. Nep-winst door asymmetrische afronding
   Als kosten worden berekend op het exacte getal maar opbrengst op het
   afgeronde getal (of vice versa), kan een verlieslatende trade er
   winstgevend uitzien. Voorbeeld: je slaat 2,277 kWh op (afgerond naar
   2,30 kWh) maar betaalt alleen voor 2,277 kWh. Dat 'extra' kwartier
   kWh is gratis — wat de trade kunstmatig winstgevend maakt.
   Fix: bereken zowel kosten als opbrengst op de feitelijke SoC-sprong
   tussen twee gridpunten.

2. Springende SoC in de output
   soc_na van slot t en soc_voor van slot t+1 zijn hetzelfde fysieke
   moment. Als soc_na het exacte getal is (2,278) maar soc_voor het
   afgeronde gridpunt (2,30), kloppen de getallen in de output niet met
   elkaar terwijl ze hetzelfde bedoelen.
   Fix: gebruik in de forwards extractie altijd de gekwantiseerde waarde,
   zodat soc_na[t] == soc_voor[t+1] gegarandeerd.

SOC-EENHEID IN DIT MODULE
--------------------------
Intern werkt het DP-algoritme in "opgeslagen kWh" (de energie die
werkelijk in de batterij zit). De HA-sensoren rapporteren in neteenheden
(gecorrigeerd voor η). De conversie vindt plaats in haal_accustatus()
in het pyscript-bestand.

  stored_kwh    = beschikbare_kwh / η      (batterij → net verlies omgekeerd)
  stored_ruimte = benodigde_kwh × η        (net → batterij verlies omgekeerd)
  totale_stored = stored_kwh + stored_ruimte

Laadverlies:    energie_van_net  = Δstored / η   (wij betalen meer dan we opslaan)
Ontlaadverlies: energie_naar_net = Δstored × η   (wij ontvangen minder dan we onttrekken)
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any


# ── CONFIGURATIE ──────────────────────────────────────────────────────────────

# BMS-derating curve: bij welke SoC% verwachten we minder opgenomen laadvermogen?
# Formaat: lijst van (soc_procent, vermogensfactor), gesorteerd op soc_procent.
# Lineair geïnterpoleerd tussen opeenvolgende punten.
#
# Achtergrond: lithiumaccu's laden in twee fasen (CC/CV laadprofiel):
#   - Constant Current (CC): tot ±80% SoC laadt de accu op vol vermogen
#   - Constant Voltage (CV): daarboven daalt de stroom om cellen te sparen
#
# Pas deze waarden aan op basis van de werkelijke specs van jouw accu.
# los_dp_op() gebruikt SOC_DERATING alleen voor de verwachte SoC-verandering.
# De vermogensopdracht aan Zendure blijft max_laad_w zodra BMS-derating actief is.
SOC_DERATING: list[tuple[float, float]] = [
    (0.0,   1.00),  # 0–80 %: vol vermogen
    (80.0,  1.00),
    (90.0,  0.70),  # bij 90 % SoC: 70 % van max laadvermogen
    (95.0,  0.40),  # bij 95 % SoC: 40 % van max laadvermogen
    (100.0, 0.10),  # bij 100 % SoC: trickle charge (10 %)
]

# Stapgrootte voor SoC-discretisatie (kWh per DP-tabelrij).
# Kleiner = nauwkeuriger maar meer geheugen en rekentijd.
# Vuistregel: kies ≤ 5 % van de energie die per slot in/uit de accu gaat.
# Bij 2.4 kWh accu en 2400 W × 15 min = 0.6 kWh per slot → 0.05 kWh is ruim voldoende.
SOC_STAP_KWH: float = 0.05

# Zendure accepteert vermogensopdrachten in nette stappen. De DP rekent met
# SoC-gridpunten; rond alleen het gerapporteerde vermogen naar boven af.
VERMOGEN_STAP_W: int = 25

# Vermogens lager dan dit minimum worden niet als DP-keuze geëvalueerd. Rust
# blijft altijd mogelijk als aparte actie.
MINIMUM_VERMOGEN_W: int = 100

# Genormaliseerde kost voor warmteverlies: penalty = factor × basis × kWh × C².
# De factor komt uit Home Assistant; deze basis houdt factor=1 bewust mild.
WARMTE_PENALTY_EUR_PER_KWH_C2: float = 0.05

# Eenvoudig thermisch model:
# - temperatuur beweegt per slot richting de voorspelde buitentemperatuur;
# - laden/ontladen voegt warmte toe op basis van C² × duur;
# - de DP krijgt een extra euro-penalty boven de ingestelde packtemperatuurgrens.
TEMP_STAP_C: float = 3.0
WARMTE_STIJGING_C_PER_C2H: float = 3.0
TEMP_PENALTY_EUR_PER_C2H: float = 0.25

# ── DATATYPE ──────────────────────────────────────────────────────────────────

@dataclass
class Accustatus:
    """
    Snapshot van de batterijstatus op het moment van berekening.

    Alle energiewaarden zijn in kWh van opgeslagen energie (niet neteenheden).
    Zie de module-docstring voor de conversie vanuit HA-sensoren.
    """
    huidig_kwh: float     # Opgeslagen kWh boven min-SoC (batterij-intern)
    max_kwh: float        # Totale bruikbare capaciteit (batterij-intern, kWh)
    eta_laad: float       # Laadrendement (0–1): η_laad = √(RTE/100)
    eta_ontlaad: float    # Ontlaadrendement (0–1): η_ontlaad = √(RTE/100)
    max_laad_w: float     # Maximaal laadvermogen (W, van het net)
    max_ontlaad_w: float  # Maximaal ontlaadvermogen (W, naar het net)


# ── HULPFUNCTIES ──────────────────────────────────────────────────────────────

def bereken_derating(soc_kwh: float, max_kwh: float) -> float:
    """
    Geeft de vermogensfactor (0–1) voor het laadvermogen op basis van de huidige SoC.

    De CC/CV-curve verschilt per fabrikant; pas SOC_DERATING aan naar de specs.
    We gebruiken lineaire interpolatie tussen de geconfigureerde drempelwaarden.

    Args:
        soc_kwh: Huidige opgeslagen energie (kWh, boven min-SoC)
        max_kwh: Totale bruikbare capaciteit (kWh)

    Returns:
        Factor tussen 0 en 1; 1.0 = vol vermogen, 0.1 = trickle charge
    """
    if max_kwh <= 0:
        return 0.0

    soc_pct = (soc_kwh / max_kwh) * 100.0

    for i in range(len(SOC_DERATING) - 1):
        lo_pct, lo_factor = SOC_DERATING[i]
        hi_pct, hi_factor = SOC_DERATING[i + 1]
        if lo_pct <= soc_pct <= hi_pct:
            # Lineaire interpolatie
            t = (soc_pct - lo_pct) / (hi_pct - lo_pct)
            return lo_factor + t * (hi_factor - lo_factor)

    # Buiten het geconfigureerde bereik: gebruik de waarde van het laatste punt
    return SOC_DERATING[-1][1]


def rond_vermogen_omhoog(vermogen_w: float, maximum_w: float) -> int:
    """
    Rondt een vermogensopdracht naar boven af op VERMOGEN_STAP_W.

    De interne kWh-berekening blijft ongewijzigd. Deze functie maakt alleen de
    opdracht aan Zendure netter, bijvoorbeeld 2397 W → 2400 W.
    """
    if vermogen_w <= 0 or maximum_w <= 0:
        return 0

    stappen = math.ceil(vermogen_w / VERMOGEN_STAP_W)
    afgerond = stappen * VERMOGEN_STAP_W
    return int(min(maximum_w, afgerond))


def bereken_laadvermogen_voor_aansturing(
    verwacht_vermogen_w: float,
    max_laad_w: float,
    derating_factor: float,
) -> int:
    """
    Geeft de laadopdracht voor Zendure.

    bereken_derating() beschrijft wat het BMS naar verwachting opneemt. Zodra
    die factor lager is dan 1.0 sturen we max_laad_w aan en laten we het BMS
    de werkelijke laadstroom begrenzen.
    """
    if verwacht_vermogen_w <= 0 or max_laad_w <= 0:
        return 0

    if derating_factor < 1.0:
        return rond_vermogen_omhoog(max_laad_w, max_laad_w)

    return rond_vermogen_omhoog(verwacht_vermogen_w, max_laad_w)


def corrigeer_actief_slot_vermogen(
    schema: list[dict[str, Any]],
    accu: Accustatus,
    nu: datetime,
) -> list[dict[str, Any]]:
    """
    Bereken `vermogen_w` voor het lopende slot vanuit `accu.huidig_kwh`.

    Het DP-schema bewaart `soc_na_kwh` als einddoel van elk slot. Voor het
    lopende slot moet de Zendure daarom minimaal sturen op:

        actuele SoC -> soc_na_kwh binnen de resterende slottijd

    Als de actuele SoC al voorloopt en toekomstige laadslots niet goedkoper zijn,
    mag het lopende laadslot extra energie uit die latere laadslots naar voren
    halen. Als het lopende ontlaadslot duurder is dan latere ontlaadslots, mag
    het lopende ontlaadslot extra energie uit die latere ontlaadslots naar voren
    halen. De functie past alleen het actieve slot aan en laat toekomstige slots
    ongewijzigd.
    """

    def naar_datetime(waarde: Any) -> datetime:
        if isinstance(waarde, datetime):
            return waarde
        return datetime.fromisoformat(str(waarde))

    def prijs_ct(slot: dict[str, Any]) -> float | None:
        try:
            return float(slot["prijs_ct"])
        except (KeyError, TypeError, ValueError):
            return None

    def laadblok_doel_soc_kwh(actief_index: int, basis_doel_kwh: float) -> float:
        doel = basis_doel_kwh
        actieve_prijs = prijs_ct(schema[actief_index])
        if actieve_prijs is None:
            return doel

        for volgend in schema[actief_index + 1:]:
            if volgend.get("actie") != "laden":
                break

            volgende_prijs = prijs_ct(volgend)
            if volgende_prijs is None or volgende_prijs < actieve_prijs:
                break

            try:
                doel = max(doel, float(volgend["soc_na_kwh"]))
            except (KeyError, TypeError, ValueError):
                break

        return doel

    def ontlaadblok_doel_soc_kwh(actief_index: int, basis_doel_kwh: float) -> float:
        doel = basis_doel_kwh
        actieve_prijs = prijs_ct(schema[actief_index])
        if actieve_prijs is None:
            return doel

        for volgend in schema[actief_index + 1:]:
            if volgend.get("actie") != "ontladen":
                break

            volgende_prijs = prijs_ct(volgend)
            if volgende_prijs is None or volgende_prijs > actieve_prijs:
                break

            try:
                doel = min(doel, float(volgend["soc_na_kwh"]))
            except (KeyError, TypeError, ValueError):
                break

        return doel

    for actief_index, slot in enumerate(schema):
        try:
            start = naar_datetime(slot["start"])
            end = naar_datetime(slot["end"])
        except (KeyError, TypeError, ValueError):
            continue

        nu_slot = nu
        if start.tzinfo is not None and nu.tzinfo is not None:
            nu_slot = nu.astimezone(start.tzinfo)

        if not (start <= nu_slot < end):
            continue

        actie = slot.get("actie")
        if actie not in ("laden", "ontladen"):
            return schema

        try:
            doel_soc_kwh = float(slot["soc_na_kwh"])
        except (KeyError, TypeError, ValueError):
            return schema

        resterende_uren = (end - nu_slot).total_seconds() / 3600.0
        slot["geplande_actie"] = actie
        slot["verwacht_vermogen_w"] = slot.get("vermogen_w", 0)
        slot["actuele_soc_kwh"] = round(accu.huidig_kwh, 3)
        slot["doel_soc_kwh"] = round(doel_soc_kwh, 3)

        if resterende_uren <= 0:
            slot["actie"] = "rust"
            slot["vermogen_w"] = 0
            return schema

        if actie == "laden":
            laadblok_doel_kwh = laadblok_doel_soc_kwh(actief_index, doel_soc_kwh)
            maximaal_haalbaar_kwh = (
                accu.huidig_kwh
                + accu.max_laad_w / 1000.0 * resterende_uren * accu.eta_laad
            )
            doel_soc_kwh = min(
                laadblok_doel_kwh,
                max(doel_soc_kwh, maximaal_haalbaar_kwh),
            )
            slot["doel_soc_kwh"] = round(doel_soc_kwh, 3)

            delta_kwh = doel_soc_kwh - accu.huidig_kwh
            if delta_kwh <= 0 or accu.eta_laad <= 0:
                slot["actie"] = "rust"
                slot["vermogen_w"] = 0
                return schema
            gevraagd_w = delta_kwh / accu.eta_laad / resterende_uren * 1000.0
            slot["vermogen_w"] = round(min(accu.max_laad_w, max(0.0, gevraagd_w)))
            return schema

        if accu.eta_ontlaad <= 0:
            slot["actie"] = "rust"
            slot["vermogen_w"] = 0
            return schema

        ontlaadblok_doel_kwh = ontlaadblok_doel_soc_kwh(actief_index, doel_soc_kwh)
        maximaal_haalbaar_kwh = (
            accu.huidig_kwh
            - accu.max_ontlaad_w / 1000.0 * resterende_uren / accu.eta_ontlaad
        )
        doel_soc_kwh = max(ontlaadblok_doel_kwh, maximaal_haalbaar_kwh)
        slot["doel_soc_kwh"] = round(doel_soc_kwh, 3)

        delta_kwh = accu.huidig_kwh - doel_soc_kwh
        if delta_kwh <= 0:
            slot["actie"] = "rust"
            slot["vermogen_w"] = 0
            return schema
        gevraagd_w = delta_kwh * accu.eta_ontlaad / resterende_uren * 1000.0
        slot["vermogen_w"] = round(min(accu.max_ontlaad_w, max(0.0, gevraagd_w)))
        return schema

    return schema


# ── DYNAMIC PROGRAMMING ───────────────────────────────────────────────────────

def los_dp_op(
    slots: list[dict[str, Any]],
    accu: Accustatus,
    min_spread_ct_per_kwh: float = 0.0,
    plateau_drempel_ct: float = 2.0,
    max_plateau_uren: int = 5,
    plateau_spreiding: bool = True,
    warmte_penalty_laden_factor: float = 1.0,
    warmte_penalty_ontladen_factor: float = 1.0,
    minimum_vermogen_w: int = MINIMUM_VERMOGEN_W,
    batterij_temp_start_c: float | None = None,
    warmte_afkoeling_halveringstijd_h: float = 2.0,
    temp_limiet_c: float = 35.0,
    temp_penalty_factor: float = 1.0,
    temp_soc_drempel_pct: float = 80.0,
) -> list[dict[str, Any]]:
    """
    Berekent de winstmaximaliserende laad/ontlaad strategie via DP.

    ALGORITME — BACKWARDS PASS
    --------------------------
    We vullen een tabel V[t][s] met de maximale toekomstige winst vanuit
    tijdslot t bij SoC-niveau s. We werken van achter naar voor:

        V[T][s] = 0  voor alle s  (na de horizon: geen verdere winst)

        V[t][s] = max(
            rust:      V[t+1][s],
            laden:    -kosten_van_net(t,s)  + V[t+1][s_na_laden],
            ontladen: +opbrengst_naar_net(t,s) + V[t+1][s_na_ontladen],
        )

    Tegelijk slaan we in P[t][s] de optimale actie, vermogensopdracht en
    volgende SoC-index op.

    ALGORITME — FORWARDS EXTRACTIE
    --------------------------------
    Na de backwards pass volgen we P voorwaarts vanaf de huidige SoC.
    Dit geeft het concrete uur-voor-uur schema.

    MINIMALE SPREAD
    ---------------
    Om onnodige cycli bij kleine prijsverschillen te voorkomen, voegen we
    een "transactiekosten" toe van min_spread/2 per kWh bij zowel laden als
    ontladen. Een cyclus wordt daardoor alleen gepland als het brutoprijsverschil
    de efficiëntieverliezen én de minimale spread ruimschoots dekt.

    DISCRETISATIE
    -------------
    SoC wordt opgedeeld in stappen van SOC_STAP_KWH. Afrondingsfouten per
    stap zijn ≤ SOC_STAP_KWH/2 kWh. Ze accumuleren niet: elke stap rondt
    onafhankelijk af, zonder systematische drift over de horizon.

    VERMOGENSSTAPPEN EN WARMTE-PENALTY
    -----------------------------------
    Per slot evalueert los_dp_op() alle vermogensopdrachten vanaf
    minimum_vermogen_w in stappen van VERMOGEN_STAP_W. De DP-keuze bepaalt
    dus zowel actie als vermogen_w.

    Voor laden en ontladen telt los_dp_op() aparte C-waarde penalties mee:
    factor × WARMTE_PENALTY_EUR_PER_KWH_C2 × kWh × C².
    warmte_penalty_laden_factor weegt snel laden. warmte_penalty_ontladen_factor
    weegt snel ontladen. Met factor 0 is de penalty voor die richting uit.

    THERMISCH MODEL
    ---------------
    Als batterij_temp_start_c bekend is, breidt los_dp_op() de DP-state uit van
    (tijd, SoC) naar (tijd, SoC, packtemperatuur). De buitentemperatuur per slot
    komt uit slot["buiten_temp_c"]. Die forecasttemperatuur is de vloer/omgeving
    waar de packtemperatuur naartoe koelt. Acties voegen warmte toe via C² × duur.

    Boven temp_soc_drempel_pct en temp_limiet_c telt temp_penalty_factor mee als
    extra euro-penalty. Zo kan de DP vermogen verlagen als een eerder hard slot
    de voorspelde packtemperatuur in latere slots verhoogt.

    PLATEAU SPREIDING
    -----------------
    Als plateau_spreiding=True, worden opeenvolgende slots met dezelfde actie en een
    onderlinge prijsverschil ≤ plateau_drempel_ct herverdeeld via water-filling:
    elke slot krijgt een gelijk deel van de totale energie, tenzij het slot zijn
    vermogenslimiet bereikt. Het overschot gaat naar de overige slots.

    Dit verlaagt het piekvermogen en vermindert thermische belasting van de batterij
    zonder de winstgevendheid noemenswaardig te beïnvloeden.

    Args:
        slots:                   Tijdslots met 'start', 'end', 'price' (€/kWh), 'duration_h'
        accu:                    Accustatus snapshot
        min_spread_ct_per_kwh:   Minimale brutosspread (ct/kWh) om te handelen
        plateau_drempel_ct:      Max prijsverschil (ct/kWh) binnen een plateau (standaard 2 ct)
        max_plateau_uren:        Max duur van één plateau in uren. Bij kwartierprijzen
                                 telt elk kwartierslot als 0,25 uur.
        plateau_spreiding:       Schakelt de plateau-nabewerking aan of uit.
        warmte_penalty_laden_factor: Gewicht van de C-waarde penalty bij laden.
        warmte_penalty_ontladen_factor: Gewicht van de C-waarde penalty bij ontladen.
        minimum_vermogen_w:      Laagste vermogensopdracht die DP evalueert; rust blijft apart.
        batterij_temp_start_c:   Warmste batterij-packtemperatuur bij start van de planning.
        warmte_afkoeling_halveringstijd_h: Uren waarin het temperatuurverschil met buiten halveert.
        temp_limiet_c:           Packtemperatuurgrens voor hoge SoC.
        temp_penalty_factor:     Gewicht voor overschrijding van temp_limiet_c.
        temp_soc_drempel_pct:    SoC-percentage waarboven temp_limiet_c actief is.

    Returns:
        Lijst van slot-dicts met toegevoegde velden:
        actie, vermogen_w, verwacht_vermogen_w, soc_voor_kwh, soc_na_kwh,
        soc_voor_pct, soc_na_pct, winst_eur, c_waarde, warmte_penalty_eur.
        Als het thermisch model actief is, bevat elk slot ook batterij_temp_voor_c,
        batterij_temp_na_c, buiten_temp_c, temp_penalty_eur, temp_limiet_c en
        temp_limiet_actief.
    """
    n = len(slots)
    if n == 0:
        return []

    max_kwh     = accu.max_kwh
    eta_laad    = accu.eta_laad
    eta_ontlaad = accu.eta_ontlaad
    max_laad_w  = accu.max_laad_w
    max_ontlaad_w = accu.max_ontlaad_w
    warmte_penalty_laden_factor = max(0.0, float(warmte_penalty_laden_factor))
    warmte_penalty_ontladen_factor = max(0.0, float(warmte_penalty_ontladen_factor))
    minimum_vermogen_w = max(0, int(minimum_vermogen_w))
    warmte_afkoeling_halveringstijd_h = max(0.05, float(warmte_afkoeling_halveringstijd_h))
    temp_limiet_c = float(temp_limiet_c)
    temp_penalty_factor = max(0.0, float(temp_penalty_factor))
    temp_soc_drempel_pct = min(100.0, max(0.0, float(temp_soc_drempel_pct)))
    thermisch_actief = batterij_temp_start_c is not None
    batterij_temp_start = float(batterij_temp_start_c) if thermisch_actief else None

    # Transactiekosten in €/kWh (gesplitst over laden en ontladen).
    # Dit zorgt ervoor dat een cyclus alleen plaatsvindt als de brutospread
    # minstens min_spread_ct_per_kwh overstijgt boven de break-even grens.
    spread_eur_per_kwh = min_spread_ct_per_kwh / 100.0
    spread_helft = spread_eur_per_kwh / 2.0

    # SoC-grid: index 0 = leeg (= min-SoC), index n_soc = vol (= max-SoC)
    n_soc = max(1, round(max_kwh / SOC_STAP_KWH))

    def kwh_naar_idx(kwh: float) -> int:
        return min(n_soc, max(0, round(kwh / SOC_STAP_KWH)))

    def idx_naar_kwh(idx: int) -> float:
        return idx * SOC_STAP_KWH

    def kwh_naar_idx_omlaag(kwh: float) -> int:
        return min(n_soc, max(0, math.floor(kwh / SOC_STAP_KWH + 1e-9)))

    def kwh_naar_idx_omhoog(kwh: float) -> int:
        return min(n_soc, max(0, math.ceil(kwh / SOC_STAP_KWH - 1e-9)))

    def vermogensstappen(maximum_w: float) -> list[int]:
        if maximum_w < minimum_vermogen_w or VERMOGEN_STAP_W <= 0:
            return []

        start_w = int(math.ceil(minimum_vermogen_w / VERMOGEN_STAP_W) * VERMOGEN_STAP_W)
        eind_w = int(math.floor(maximum_w / VERMOGEN_STAP_W) * VERMOGEN_STAP_W)
        if eind_w < start_w:
            return []

        stappen = list(range(start_w, eind_w + 1, VERMOGEN_STAP_W))
        afgerond_max = int(round(maximum_w))
        if afgerond_max > eind_w and afgerond_max >= start_w:
            stappen.append(afgerond_max)
        return stappen

    def warmte_penalty_eur(energie_accu_kwh: float, duur_h: float, factor: float) -> float:
        if factor <= 0 or energie_accu_kwh <= 0 or duur_h <= 0 or max_kwh <= 0:
            return 0.0

        c_waarde = (energie_accu_kwh / duur_h) / max_kwh
        return factor * WARMTE_PENALTY_EUR_PER_KWH_C2 * energie_accu_kwh * c_waarde * c_waarde

    def c_waarde(energie_accu_kwh: float, duur_h: float) -> float:
        if energie_accu_kwh <= 0 or duur_h <= 0 or max_kwh <= 0:
            return 0.0
        return (energie_accu_kwh / duur_h) / max_kwh

    def slot_buiten_temp_c(t: int) -> float | None:
        if not thermisch_actief:
            return None
        try:
            return float(slots[t].get("buiten_temp_c", batterij_temp_start))
        except (TypeError, ValueError):
            return batterij_temp_start

    def voorspel_temp_na_c(
        temp_voor_c: float | None,
        buiten_temp_c: float | None,
        energie_accu_kwh: float,
        duur_h: float,
    ) -> float | None:
        if temp_voor_c is None or buiten_temp_c is None or duur_h <= 0:
            return temp_voor_c

        afkoel_factor = 0.5 ** (duur_h / warmte_afkoeling_halveringstijd_h)
        temp_na_koeling = buiten_temp_c + (temp_voor_c - buiten_temp_c) * afkoel_factor
        actie_c = c_waarde(energie_accu_kwh, duur_h)
        return temp_na_koeling + WARMTE_STIJGING_C_PER_C2H * actie_c * actie_c * duur_h

    def temperatuur_penalty_eur(temp_na_c: float | None, soc_na_kwh: float, duur_h: float) -> float:
        if (
            temp_na_c is None
            or temp_penalty_factor <= 0
            or duur_h <= 0
            or max_kwh <= 0
            or soc_na_kwh / max_kwh * 100.0 < temp_soc_drempel_pct
            or temp_na_c <= temp_limiet_c
        ):
            return 0.0
        overschrijding_c = temp_na_c - temp_limiet_c
        return temp_penalty_factor * TEMP_PENALTY_EUR_PER_C2H * overschrijding_c * overschrijding_c * duur_h

    if thermisch_actief:
        buiten_temperaturen = [
            waarde
            for waarde in (slot_buiten_temp_c(t) for t in range(n))
            if waarde is not None
        ]
        temp_min = math.floor((min([batterij_temp_start, temp_limiet_c] + buiten_temperaturen) - 5.0) / TEMP_STAP_C) * TEMP_STAP_C
        temp_max = math.ceil((max([batterij_temp_start, temp_limiet_c] + buiten_temperaturen) + 15.0) / TEMP_STAP_C) * TEMP_STAP_C
        n_temp = max(1, int(round((temp_max - temp_min) / TEMP_STAP_C)) + 1)
    else:
        temp_min = 0.0
        n_temp = 1

    def temp_naar_idx(temp_c: float | None) -> int:
        if not thermisch_actief or temp_c is None:
            return 0
        return min(n_temp - 1, max(0, round((temp_c - temp_min) / TEMP_STAP_C)))

    def idx_naar_temp(idx: int) -> float | None:
        if not thermisch_actief:
            return None
        return temp_min + idx * TEMP_STAP_C

    def volgende_temp_idx(q: int, t: int, energie_accu_kwh: float) -> int:
        if not thermisch_actief:
            return 0
        temp_na_c = voorspel_temp_na_c(
            idx_naar_temp(q),
            slot_buiten_temp_c(t),
            energie_accu_kwh,
            slots[t]["duration_h"],
        )
        return temp_naar_idx(temp_na_c)

    NEG_INF = float("-inf")

    # V[t][s][q]: maximale toekomstige winst (€) vanuit slot t, SoC-index s
    # en temperatuur-index q. Zonder thermisch model heeft q precies één waarde.
    # Terminale waarde (na de horizon) = 0.
    # Resterende energie heeft een onbekende toekomstige waarde. Door 0 te gebruiken
    # zijn we conservatief: we plannen alleen op wat we zeker kunnen verdienen.
    V = [[[0.0] * n_temp for _ in range(n_soc + 1)] for _ in range(n + 1)]
    # P[t][s][q]: optimale actie, vermogensopdracht, volgende SoC-index en volgende temp-index.
    P = [[[(0, 0, s, q) for q in range(n_temp)] for s in range(n_soc + 1)] for _ in range(n + 1)]

    # ── BACKWARDS PASS ────────────────────────────────────────────────────────
    for t in range(n - 1, -1, -1):
        prijs  = slots[t]["price"]       # €/kWh
        duur_h = slots[t]["duration_h"]  # uren

        for s in range(n_soc + 1):
            soc_kwh = idx_naar_kwh(s)  # altijd een gridpunt (veelvoud van SOC_STAP_KWH)

            for q in range(n_temp):
                # ── Optie: RUST ───────────────────────────────────────────────
                # Geen actie, SoC ongewijzigd, temperatuur koelt wel richting buiten.
                q_rust = volgende_temp_idx(q, t, 0.0)
                beste = V[t + 1][s][q_rust]
                beste_keuze = (0, 0, s, q_rust)

                # ── Optie: LADEN ──────────────────────────────────────────────
                # Evalueer alle vermogensopdrachten vanaf minimum_vermogen_w.
                derating = bereken_derating(soc_kwh, max_kwh)
                eff_laad_w = max_laad_w * derating
                ruimte_kwh = max_kwh - soc_kwh

                for vermogen_w in vermogensstappen(max_laad_w):
                    werkelijk_w = min(float(vermogen_w), eff_laad_w)
                    energie_ideaal = min(werkelijk_w / 1000.0 * duur_h * eta_laad, ruimte_kwh)
                    s_laden = kwh_naar_idx_omlaag(soc_kwh + energie_ideaal)
                    if s_laden <= s:
                        continue

                    energie_naar_accu_q = idx_naar_kwh(s_laden) - soc_kwh
                    energie_van_net = energie_naar_accu_q / eta_laad if eta_laad > 0 else 0.0
                    kosten_laden = energie_van_net * (prijs + spread_helft)
                    kosten_warmte = warmte_penalty_eur(
                        energie_naar_accu_q,
                        duur_h,
                        warmte_penalty_laden_factor,
                    )
                    q_laden = volgende_temp_idx(q, t, energie_naar_accu_q)
                    kosten_temp = temperatuur_penalty_eur(
                        idx_naar_temp(q_laden),
                        idx_naar_kwh(s_laden),
                        duur_h,
                    )
                    waarde = -kosten_laden - kosten_warmte - kosten_temp + V[t + 1][s_laden][q_laden]

                    if waarde > beste + 1e-12:
                        beste = waarde
                        beste_keuze = (1, int(vermogen_w), s_laden, q_laden)

                # ── Optie: ONTLADEN ───────────────────────────────────────────
                # Evalueer alle outputLimit-opdrachten vanaf minimum_vermogen_w.
                for vermogen_w in vermogensstappen(max_ontlaad_w):
                    max_naar_net_kwh = float(vermogen_w) / 1000.0 * duur_h
                    max_uit_accu_kwh = max_naar_net_kwh / eta_ontlaad if eta_ontlaad > 0 else 0.0
                    energie_ideaal_uit = min(max_uit_accu_kwh, soc_kwh)
                    s_ontladen = kwh_naar_idx_omhoog(soc_kwh - energie_ideaal_uit)
                    if s_ontladen >= s:
                        continue

                    energie_uit_accu_q = soc_kwh - idx_naar_kwh(s_ontladen)
                    energie_naar_net = energie_uit_accu_q * eta_ontlaad
                    opbrengst_ontladen = energie_naar_net * (prijs - spread_helft)
                    kosten_warmte = warmte_penalty_eur(
                        energie_uit_accu_q,
                        duur_h,
                        warmte_penalty_ontladen_factor,
                    )
                    q_ontladen = volgende_temp_idx(q, t, energie_uit_accu_q)
                    kosten_temp = temperatuur_penalty_eur(
                        idx_naar_temp(q_ontladen),
                        idx_naar_kwh(s_ontladen),
                        duur_h,
                    )
                    waarde = (
                        opbrengst_ontladen
                        - kosten_warmte
                        - kosten_temp
                        + V[t + 1][s_ontladen][q_ontladen]
                    )

                    if waarde > beste + 1e-12:
                        beste = waarde
                        beste_keuze = (-1, int(vermogen_w), s_ontladen, q_ontladen)

                V[t][s][q] = beste
                P[t][s][q] = beste_keuze

    # ── VOORWAARTSE EXTRACTIE ─────────────────────────────────────────────────
    # Volg de optimale acties voorwaarts vanaf de huidige SoC.
    # Alle berekeningen hier spiegelen de backwards pass exact:
    # - SoC-overgangen lopen via kwh_naar_idx / idx_naar_kwh (gekwantiseerd)
    # - Kosten en opbrengst zijn gebaseerd op de feitelijke gekwantiseerde SoC-sprong
    # Hierdoor is soc_na[t] == soc_voor[t+1] exact, en is de gerapporteerde winst
    # consistent met wat het DP-algoritme heeft geoptimaliseerd.
    resultaat: list[dict[str, Any]] = []
    huidig_s = kwh_naar_idx(accu.huidig_kwh)
    huidig_q = temp_naar_idx(batterij_temp_start)
    huidig_temp_c = batterij_temp_start

    for t in range(n):
        slot       = slots[t]
        soc_kwh    = idx_naar_kwh(huidig_s)   # gekwantiseerde SoC aan begin van slot
        actie_code, gekozen_vermogen_w, nieuwe_s, nieuwe_q = P[t][huidig_s][huidig_q]
        prijs      = slot["price"]
        duur_h     = slot["duration_h"]

        if actie_code == 1:  # Laden
            derating          = bereken_derating(soc_kwh, max_kwh)
            eff_laad_w        = max_laad_w * derating
            # Gekwantiseerde SoC-sprong → consistente kosten (zie backwards pass)
            energie_naar_accu = idx_naar_kwh(nieuwe_s) - soc_kwh
            energie_van_net   = energie_naar_accu / eta_laad if eta_laad > 0 else 0.0
            winst             = -energie_van_net * prijs
            verwacht_vermogen_w = energie_van_net / duur_h * 1000.0 if duur_h > 0 else 0.0
            vermogen_w = gekozen_vermogen_w
            warmte_penalty = warmte_penalty_eur(
                energie_naar_accu,
                duur_h,
                warmte_penalty_laden_factor,
            )
            energie_accu_voor_model = energie_naar_accu
            actie             = "laden"

        elif actie_code == -1:  # Ontladen
            # Gekwantiseerde SoC-daling → consistente opbrengst
            energie_uit_accu  = soc_kwh - idx_naar_kwh(nieuwe_s)
            energie_naar_net  = energie_uit_accu * eta_ontlaad
            winst             = energie_naar_net * prijs
            verwacht_vermogen_w = energie_naar_net / duur_h * 1000.0 if duur_h > 0 else 0.0
            vermogen_w        = gekozen_vermogen_w
            warmte_penalty    = warmte_penalty_eur(
                energie_uit_accu,
                duur_h,
                warmte_penalty_ontladen_factor,
            )
            energie_accu_voor_model = energie_uit_accu
            actie             = "ontladen"

        else:  # Rust
            winst      = 0.0
            verwacht_vermogen_w = 0.0
            vermogen_w = 0.0
            warmte_penalty = 0.0
            energie_accu_voor_model = 0.0
            actie      = "rust"

        soc_na_kwh = idx_naar_kwh(nieuwe_s)   # altijd een gridpunt → consistent met soc_voor[t+1]
        buiten_temp_c = slot_buiten_temp_c(t)
        temp_voor_c = huidig_temp_c
        temp_na_c = voorspel_temp_na_c(
            temp_voor_c,
            buiten_temp_c,
            energie_accu_voor_model,
            duur_h,
        )
        if thermisch_actief:
            huidig_temp_c = temp_na_c
        start = slot["start"]
        end   = slot["end"]

        slot_resultaat = {
            "start":        start.isoformat() if hasattr(start, "isoformat") else start,
            "end":          end.isoformat()   if hasattr(end,   "isoformat") else end,
            "prijs_ct":     round(prijs * 100, 3),
            "actie":        actie,
            "vermogen_w":   vermogen_w,
            "verwacht_vermogen_w": rond_vermogen_omhoog(
                verwacht_vermogen_w,
                eff_laad_w if actie == "laden" else max_ontlaad_w if actie == "ontladen" else 0.0,
            ),
            "soc_voor_kwh": round(soc_kwh,    3),
            "soc_na_kwh":   round(soc_na_kwh, 3),
            "soc_voor_pct": round(soc_kwh    / max_kwh * 100, 1) if max_kwh > 0 else 0.0,
            "soc_na_pct":   round(soc_na_kwh / max_kwh * 100, 1) if max_kwh > 0 else 0.0,
            "winst_eur":    round(winst, 4),
            "warmte_penalty_eur": round(warmte_penalty, 4),
            "c_waarde": round(c_waarde(energie_accu_voor_model, duur_h), 3),
        }
        if thermisch_actief:
            temp_limiet_actief = soc_na_kwh / max_kwh * 100.0 >= temp_soc_drempel_pct if max_kwh > 0 else False
            slot_resultaat.update({
                "batterij_temp_voor_c": round(temp_voor_c, 2) if temp_voor_c is not None else None,
                "batterij_temp_na_c": round(temp_na_c, 2) if temp_na_c is not None else None,
                "buiten_temp_c": round(buiten_temp_c, 2) if buiten_temp_c is not None else None,
                "temp_penalty_eur": round(temperatuur_penalty_eur(temp_na_c, soc_na_kwh, duur_h), 4),
                "temp_limiet_c": round(temp_limiet_c, 2),
                "temp_limiet_actief": bool(temp_limiet_actief),
            })

        resultaat.append(slot_resultaat)

        huidig_s = nieuwe_s
        huidig_q = nieuwe_q

    def herbereken_modelvelden(schema: list[dict[str, Any]]) -> None:
        temp_c = batterij_temp_start
        for k, s_r in enumerate(schema):
            duur_h = slots[k]["duration_h"]
            actie = s_r.get("actie")
            try:
                soc_voor = float(s_r["soc_voor_kwh"])
                soc_na = float(s_r["soc_na_kwh"])
            except (KeyError, TypeError, ValueError):
                soc_voor = soc_na = 0.0

            if actie == "laden":
                energie_accu = max(0.0, soc_na - soc_voor)
                factor = warmte_penalty_laden_factor
            elif actie == "ontladen":
                energie_accu = max(0.0, soc_voor - soc_na)
                factor = warmte_penalty_ontladen_factor
            else:
                energie_accu = 0.0
                factor = 0.0

            s_r["warmte_penalty_eur"] = round(warmte_penalty_eur(energie_accu, duur_h, factor), 4)
            s_r["c_waarde"] = round(c_waarde(energie_accu, duur_h), 3)

            if not thermisch_actief:
                continue

            buiten_c = slot_buiten_temp_c(k)
            temp_voor_c = temp_c
            temp_na_c = voorspel_temp_na_c(temp_voor_c, buiten_c, energie_accu, duur_h)
            if temp_na_c is not None:
                temp_c = temp_na_c

            temp_limiet_actief = soc_na / max_kwh * 100.0 >= temp_soc_drempel_pct if max_kwh > 0 else False
            s_r["batterij_temp_voor_c"] = round(temp_voor_c, 2) if temp_voor_c is not None else None
            s_r["batterij_temp_na_c"] = round(temp_c, 2) if temp_c is not None else None
            s_r["buiten_temp_c"] = round(buiten_c, 2) if buiten_c is not None else None
            s_r["temp_penalty_eur"] = round(temperatuur_penalty_eur(temp_c, soc_na, duur_h), 4)
            s_r["temp_limiet_c"] = round(temp_limiet_c, 2)
            s_r["temp_limiet_actief"] = bool(temp_limiet_actief)

    herbereken_modelvelden(resultaat)

    if not plateau_spreiding:
        return resultaat

    # ── PLATEAU SPREIDING ─────────────────────────────────────────────────────
    # Herverdeel energie gelijkmatig over aaneengesloten slots met dezelfde actie
    # en een onderlinge prijsverschil ≤ plateau_drempel_ct. Water-filling zorgt
    # ervoor dat slots die hun vermogenslimiet bereiken het overschot doorgeven.

    def water_filling(totaal: float, maxima: list[float]) -> list[float]:
        verdeling = [0.0] * len(maxima)
        resterend = totaal
        actief = set(range(len(maxima)))
        while resterend > 1e-9 and actief:
            gelijk = resterend / len(actief)
            gecapped: set[int] = set()
            for k in actief:
                if maxima[k] < gelijk - 1e-9:
                    verdeling[k] = maxima[k]
                    resterend -= maxima[k]
                    gecapped.add(k)
            if not gecapped:
                for k in actief:
                    verdeling[k] = gelijk
                break
            actief -= gecapped
        return verdeling

    def reset_rust(k: int, soc: float) -> None:
        """Zet slot k terug naar rust met ongewijzigde SoC."""
        q = idx_naar_kwh(kwh_naar_idx(soc))
        s = resultaat[k]
        s["actie"] = "rust"; s["vermogen_w"] = 0; s["winst_eur"] = 0.0
        s["verwacht_vermogen_w"] = 0
        s["soc_voor_kwh"] = round(q, 3); s["soc_na_kwh"] = round(q, 3)
        s["soc_voor_pct"] = round(q / max_kwh * 100, 1) if max_kwh > 0 else 0.0
        s["soc_na_pct"]   = s["soc_voor_pct"]

    def naar_datetime(waarde: Any) -> datetime:
        if isinstance(waarde, datetime):
            return waarde
        return datetime.fromisoformat(str(waarde))

    slot_starts = [naar_datetime(slot["start"]) for slot in slots]
    lokaal_venster = timedelta(hours=3)

    def is_lokaal_extremum(k: int, actie: str) -> bool:
        """Controleert of slot k binnen ±3 uur een lokaal minimum/maximum is."""
        prijs = resultaat[k]["prijs_ct"]
        start = slot_starts[k]

        for m, ander_start in enumerate(slot_starts):
            if m == k or abs(ander_start - start) > lokaal_venster:
                continue

            andere_prijs = resultaat[m]["prijs_ct"]
            if actie == "laden" and andere_prijs < prijs:
                return False
            if actie == "ontladen" and andere_prijs > prijs:
                return False

        return True

    def plateau_duur_h(start: int, end: int) -> float:
        """Geeft de totale duur van een halfopen slotvenster terug."""
        return sum(slots[k]["duration_h"] for k in range(start, end))

    def kan_plateau_slot_worden(k: int, actie: str, basis_prijs: float) -> bool:
        """Controleert of slot k aan het plateau rond de basisprijs mag meedoen."""
        if k < 0 or k >= n:
            return False
        if resultaat[k]["actie"] not in (actie, "rust"):
            return False
        if abs(resultaat[k]["prijs_ct"] - basis_prijs) > plateau_drempel_ct:
            return False
        return True

    def vind_plateau_rond_basis(basis: int, actie: str) -> tuple[int, int]:
        """
        Groeit een plateau per aangrenzend slot vanuit een lokaal extremum.

        Bij twee geldige buren kiest de functie het slot waarvan de prijs het
        dichtst bij de basisprijs ligt. De maximale plateau-lengte blijft in
        uren gedefinieerd, zodat uurprijzen en kwartierprijzen hetzelfde werken.
        """
        basis_prijs = resultaat[basis]["prijs_ct"]
        start = basis
        end = basis + 1

        while plateau_duur_h(start, end) < max_plateau_uren:
            kandidaten: list[tuple[float, int, int]] = []
            links = start - 1
            rechts = end

            if kan_plateau_slot_worden(links, actie, basis_prijs):
                nieuwe_duur = plateau_duur_h(links, end)
                if nieuwe_duur <= max_plateau_uren:
                    kandidaten.append((abs(resultaat[links]["prijs_ct"] - basis_prijs), 0, links))

            if kan_plateau_slot_worden(rechts, actie, basis_prijs):
                nieuwe_duur = plateau_duur_h(start, rechts + 1)
                if nieuwe_duur <= max_plateau_uren:
                    kandidaten.append((abs(resultaat[rechts]["prijs_ct"] - basis_prijs), 1, rechts))

            if not kandidaten:
                break

            _, _, gekozen = min(kandidaten)
            if gekozen < start:
                start = gekozen
            else:
                end = gekozen + 1

        return start, end

    def kies_plateau(i: int, j: int, actie: str) -> tuple[int, int] | None:
        """Kiest een plateau binnen de actie-groep i..j."""
        kandidaten: list[tuple[float, float, int, int]] = []
        for basis in range(i, j):
            if not is_lokaal_extremum(basis, actie):
                continue

            start, end = vind_plateau_rond_basis(basis, actie)
            if end - start < 2:
                continue

            duur = plateau_duur_h(start, end)
            prijs_score = resultaat[basis]["prijs_ct"] if actie == "laden" else -resultaat[basis]["prijs_ct"]
            kandidaten.append((-duur, prijs_score, start, end))

        if not kandidaten:
            return None

        _, _, start, end = min(kandidaten)
        return start, end

    i = 0
    while i < n:
        actie_i = resultaat[i]["actie"]
        if actie_i not in ("laden", "ontladen"):
            i += 1
            continue

        # Stap 1: vind aaneengesloten actie-slots.
        j = i + 1
        while j < n and resultaat[j]["actie"] == actie_i:
            j += 1

        plateau = kies_plateau(i, j, actie_i)
        if plateau is None:
            i = j
            continue
        gs, ge = plateau

        cand_start = gs
        cand_end = ge
        soc_start = resultaat[cand_start]["soc_voor_kwh"]
        soc_eind  = resultaat[cand_end - 1]["soc_na_kwh"]

        # Reset slots vóór het geselecteerde venster naar rust (ongewijzigde SoC).
        for k in range(cand_start, gs):
            reset_rust(k, soc_start)

        if actie_i == "ontladen":
            totaal_delta = soc_start - soc_eind
            maxima_groep = [
                (max_ontlaad_w / 1000.0 * slots[k]["duration_h"]) / eta_ontlaad
                if eta_ontlaad > 0 else 0.0
                for k in range(gs, ge)
            ]
            verdeling    = water_filling(totaal_delta, maxima_groep)

            huidig_soc = soc_start
            for k, delta in zip(range(gs, ge), verdeling):
                s_r    = resultaat[k]
                prijs  = s_r["prijs_ct"] / 100.0
                duur_h = slots[k]["duration_h"]
                s_voor = kwh_naar_idx(huidig_soc)
                s_na   = kwh_naar_idx(huidig_soc - delta)
                q_voor = idx_naar_kwh(s_voor)
                q_na   = idx_naar_kwh(s_na)
                e_uit  = q_voor - q_na
                e_net  = e_uit * eta_ontlaad
                s_r["soc_voor_kwh"] = round(q_voor, 3)
                s_r["soc_na_kwh"]   = round(q_na, 3)
                s_r["soc_voor_pct"] = round(q_voor / max_kwh * 100, 1) if max_kwh > 0 else 0.0
                s_r["soc_na_pct"]   = round(q_na   / max_kwh * 100, 1) if max_kwh > 0 else 0.0
                verwacht_vermogen = e_net / duur_h * 1000.0 if duur_h > 0 else 0.0
                s_r["vermogen_w"]   = rond_vermogen_omhoog(verwacht_vermogen, max_ontlaad_w)
                s_r["verwacht_vermogen_w"] = s_r["vermogen_w"]
                s_r["winst_eur"]    = round(e_net * prijs, 4)
                huidig_soc = q_na

        else:  # laden
            totaal_delta = soc_eind - soc_start
            n_groep      = ge - gs
            # Maxima op basis van gelijke verdeling: derating evalueren op de verwachte
            # SoC bij gelijke verdeling, zodat latere slots niet kunstmatig vol lijken.
            soc_ideaal_ps = totaal_delta / n_groep if n_groep > 0 else 0.0
            maxima_groep  = []
            for k_rel, k in enumerate(range(gs, ge)):
                duur_h = slots[k]["duration_h"]
                soc_bi = soc_start + k_rel * soc_ideaal_ps
                derat  = bereken_derating(soc_bi, max_kwh)
                cap    = min(max_laad_w * derat / 1000.0 * duur_h * eta_laad, max_kwh - soc_bi)
                maxima_groep.append(max(0.0, cap))
            verdeling = water_filling(totaal_delta, maxima_groep)

            huidig_soc = soc_start
            for k, delta in zip(range(gs, ge), verdeling):
                s_r    = resultaat[k]
                prijs  = s_r["prijs_ct"] / 100.0
                duur_h = slots[k]["duration_h"]
                s_voor = kwh_naar_idx(huidig_soc)
                s_na   = kwh_naar_idx(huidig_soc + delta)
                q_voor = idx_naar_kwh(s_voor)
                q_na   = idx_naar_kwh(s_na)
                e_naar = q_na - q_voor
                e_net  = e_naar / eta_laad if eta_laad > 0 else 0.0
                s_r["actie"]        = "laden" if e_naar > 0 else "rust"
                s_r["soc_voor_kwh"] = round(q_voor, 3)
                s_r["soc_na_kwh"]   = round(q_na, 3)
                s_r["soc_voor_pct"] = round(q_voor / max_kwh * 100, 1) if max_kwh > 0 else 0.0
                s_r["soc_na_pct"]   = round(q_na   / max_kwh * 100, 1) if max_kwh > 0 else 0.0
                verwacht_vermogen = e_net / duur_h * 1000.0 if duur_h > 0 else 0.0
                derat = bereken_derating(q_voor, max_kwh)
                s_r["vermogen_w"] = bereken_laadvermogen_voor_aansturing(
                    verwacht_vermogen,
                    max_laad_w,
                    derat,
                )
                s_r["verwacht_vermogen_w"] = rond_vermogen_omhoog(
                    verwacht_vermogen,
                    max_laad_w * derat,
                )
                s_r["winst_eur"]    = round(-e_net * prijs, 4)
                huidig_soc = q_na

        # Reset slots ná het geselecteerde venster naar rust (SoC na redistributie).
        for k in range(ge, cand_end):
            reset_rust(k, huidig_soc)

        i = cand_end

    herbereken_modelvelden(resultaat)
    return resultaat

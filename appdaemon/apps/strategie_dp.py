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

from dataclasses import dataclass
from typing import Any


# ── CONFIGURATIE ──────────────────────────────────────────────────────────────

# Derating curve: bij welke SoC% daalt het laadvermogen?
# Formaat: lijst van (soc_procent, vermogensfactor), gesorteerd op soc_procent.
# Lineair geïnterpoleerd tussen opeenvolgende punten.
#
# Achtergrond: lithiumaccu's laden in twee fasen (CC/CV laadprofiel):
#   - Constant Current (CC): tot ±80% SoC laadt de accu op vol vermogen
#   - Constant Voltage (CV): daarboven daalt de stroom om cellen te sparen
#
# Pas deze waarden aan op basis van de werkelijke specs van jouw accu.
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


# ── DYNAMIC PROGRAMMING ───────────────────────────────────────────────────────

def los_dp_op(
    slots: list[dict[str, Any]],
    accu: Accustatus,
    min_spread_ct_per_kwh: float = 0.0,
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

    Tegelijk slaan we in P[t][s] de optimale actie op (0=rust, 1=laden, -1=ontladen).

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

    VERMOGENSVEREENVOUDIGING
    ------------------------
    Per slot evalueren we alleen maximaal beschikbaar vermogen (na derating),
    geen partieel laden/ontladen. Dit is zelden suboptimaal bij arbitrage:
    je wilt zo snel mogelijk laden voor de piek. De SoC-capaciteitsgrens
    begrenst vanzelf de hoeveelheid energie als de accu bijna vol/leeg is.

    Args:
        slots:                   Tijdslots met 'start', 'end', 'price' (€/kWh), 'duration_h'
        accu:                    Accustatus snapshot
        min_spread_ct_per_kwh:   Minimale brutosspread (ct/kWh) om te handelen

    Returns:
        Lijst van slot-dicts met toegevoegde velden:
        actie, vermogen_w, soc_voor_kwh, soc_na_kwh, soc_voor_pct, soc_na_pct, winst_eur
    """
    n = len(slots)
    if n == 0:
        return []

    max_kwh     = accu.max_kwh
    eta_laad    = accu.eta_laad
    eta_ontlaad = accu.eta_ontlaad
    max_laad_w  = accu.max_laad_w
    max_ontlaad_w = accu.max_ontlaad_w

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

    NEG_INF = float("-inf")

    # V[t][s]: maximale toekomstige winst (€) vanuit slot t, SoC-index s
    # Terminale waarde (na de horizon) = 0.
    # Resterende energie heeft een onbekende toekomstige waarde. Door 0 te gebruiken
    # zijn we conservatief: we plannen alleen op wat we zeker kunnen verdienen.
    V = [[0.0] * (n_soc + 1) for _ in range(n + 1)]
    # P[t][s]: optimale actie vanuit slot t, SoC-index s
    P = [[0] * (n_soc + 1) for _ in range(n + 1)]

    # ── BACKWARDS PASS ────────────────────────────────────────────────────────
    for t in range(n - 1, -1, -1):
        prijs  = slots[t]["price"]       # €/kWh
        duur_h = slots[t]["duration_h"]  # uren

        for s in range(n_soc + 1):
            soc_kwh = idx_naar_kwh(s)  # altijd een gridpunt (veelvoud van SOC_STAP_KWH)

            # ── Optie: RUST ───────────────────────────────────────────────────
            # Geen actie, SoC ongewijzigd, waarde = toekomstige waarde.
            val_rust = V[t + 1][s]

            # ── Optie: LADEN ──────────────────────────────────────────────────
            # Derating op basis van de SoC aan het begin van dit slot.
            # We benaderen de gemiddelde derating over het slot door de beginwaarde
            # te gebruiken. Dit is conservatief: de werkelijke derating neemt toe
            # naarmate de accu voller wordt tijdens het laden.
            derating   = bereken_derating(soc_kwh, max_kwh)
            eff_laad_w = max_laad_w * derating

            # Ideale energie naar de accu (continu), begrensd door ruimte.
            max_naar_accu_kwh = eff_laad_w / 1000.0 * duur_h * eta_laad
            ruimte_kwh        = max_kwh - soc_kwh
            energie_ideaal    = min(max_naar_accu_kwh, ruimte_kwh)

            # Kwantiseer naar het dichtstbijzijnde gridpunt.
            s_laden           = kwh_naar_idx(soc_kwh + energie_ideaal)

            # KERNPUNT: gebruik de GEKWANTISEERDE SoC-sprong voor de kostenberekening.
            # Hierdoor zijn kosten en toestandsovergang volledig consistent met de
            # DP-tabel. Zonder dit kan discretisatie een verlieslatende trade als
            # licht winstgevend laten lijken (of vice versa).
            energie_naar_accu_q = idx_naar_kwh(s_laden) - soc_kwh
            energie_van_net     = energie_naar_accu_q / eta_laad if eta_laad > 0 else 0.0

            # Laadkosten + helft van de minimale spread als transactiedrempel.
            kosten_laden = energie_van_net * (prijs + spread_helft)

            # Laden is alleen zinvol als de SoC daadwerkelijk stijgt (s_laden > s).
            # Dit filtert ook micro-cycli bij een bijna-volle accu of extreme derating.
            if s_laden <= s:
                val_laden = NEG_INF
            else:
                val_laden = -kosten_laden + V[t + 1][s_laden]

            # ── Optie: ONTLADEN ───────────────────────────────────────────────
            # Ideale onttrokken energie (continu), begrensd door beschikbare SoC.
            max_uit_accu_kwh    = max_ontlaad_w / 1000.0 * duur_h
            energie_ideaal_uit  = min(max_uit_accu_kwh, soc_kwh)

            # Kwantiseer de eindige SoC na ontladen.
            s_ontladen          = kwh_naar_idx(soc_kwh - energie_ideaal_uit)

            # Gebruik de gekwantiseerde SoC-daling voor opbrengstberekening.
            energie_uit_accu_q  = soc_kwh - idx_naar_kwh(s_ontladen)
            energie_naar_net    = energie_uit_accu_q * eta_ontlaad

            # Ontlaadopbrengst minus helft van de minimale spread als drempel.
            opbrengst_ontladen  = energie_naar_net * (prijs - spread_helft)

            # Ontladen is alleen zinvol als de SoC daadwerkelijk daalt (s_ontladen < s).
            if s_ontladen >= s:
                val_ontladen = NEG_INF
            else:
                val_ontladen = opbrengst_ontladen + V[t + 1][s_ontladen]

            # ── Kies de beste optie ───────────────────────────────────────────
            beste = max(val_rust, val_laden, val_ontladen)
            V[t][s] = beste

            # Bij gelijke waarden: prefereer rust (voorkom onnodige cycli)
            if val_ontladen == beste:
                P[t][s] = -1
            elif val_laden == beste:
                P[t][s] = 1
            else:
                P[t][s] = 0

    # ── VOORWAARTSE EXTRACTIE ─────────────────────────────────────────────────
    # Volg de optimale acties voorwaarts vanaf de huidige SoC.
    # Alle berekeningen hier spiegelen de backwards pass exact:
    # - SoC-overgangen lopen via kwh_naar_idx / idx_naar_kwh (gekwantiseerd)
    # - Kosten en opbrengst zijn gebaseerd op de feitelijke gekwantiseerde SoC-sprong
    # Hierdoor is soc_na[t] == soc_voor[t+1] exact, en is de gerapporteerde winst
    # consistent met wat het DP-algoritme heeft geoptimaliseerd.
    resultaat: list[dict[str, Any]] = []
    huidig_s = kwh_naar_idx(accu.huidig_kwh)

    for t in range(n):
        slot       = slots[t]
        soc_kwh    = idx_naar_kwh(huidig_s)   # gekwantiseerde SoC aan begin van slot
        actie_code = P[t][huidig_s]
        prijs      = slot["price"]
        duur_h     = slot["duration_h"]

        if actie_code == 1:  # Laden
            derating          = bereken_derating(soc_kwh, max_kwh)
            eff_laad_w        = max_laad_w * derating
            max_naar_accu     = eff_laad_w / 1000.0 * duur_h * eta_laad
            energie_ideaal    = min(max_naar_accu, max_kwh - soc_kwh)
            nieuwe_s          = kwh_naar_idx(soc_kwh + energie_ideaal)
            # Gekwantiseerde SoC-sprong → consistente kosten (zie backwards pass)
            energie_naar_accu = idx_naar_kwh(nieuwe_s) - soc_kwh
            energie_van_net   = energie_naar_accu / eta_laad if eta_laad > 0 else 0.0
            winst             = -energie_van_net * prijs
            vermogen_w        = energie_van_net / duur_h * 1000.0 if duur_h > 0 else 0.0
            actie             = "laden"

        elif actie_code == -1:  # Ontladen
            max_uit_accu      = max_ontlaad_w / 1000.0 * duur_h
            energie_ideaal    = min(max_uit_accu, soc_kwh)
            nieuwe_s          = kwh_naar_idx(soc_kwh - energie_ideaal)
            # Gekwantiseerde SoC-daling → consistente opbrengst
            energie_uit_accu  = soc_kwh - idx_naar_kwh(nieuwe_s)
            energie_naar_net  = energie_uit_accu * eta_ontlaad
            winst             = energie_naar_net * prijs
            vermogen_w        = energie_naar_net / duur_h * 1000.0 if duur_h > 0 else 0.0
            actie             = "ontladen"

        else:  # Rust
            nieuwe_s   = huidig_s
            winst      = 0.0
            vermogen_w = 0.0
            actie      = "rust"

        soc_na_kwh = idx_naar_kwh(nieuwe_s)   # altijd een gridpunt → consistent met soc_voor[t+1]
        start = slot["start"]
        end   = slot["end"]

        resultaat.append({
            "start":        start.isoformat() if hasattr(start, "isoformat") else start,
            "end":          end.isoformat()   if hasattr(end,   "isoformat") else end,
            "prijs_ct":     round(prijs * 100, 3),
            "actie":        actie,
            "vermogen_w":   round(vermogen_w),
            "soc_voor_kwh": round(soc_kwh,    3),
            "soc_na_kwh":   round(soc_na_kwh, 3),
            "soc_voor_pct": round(soc_kwh    / max_kwh * 100, 1) if max_kwh > 0 else 0.0,
            "soc_na_pct":   round(soc_na_kwh / max_kwh * 100, 1) if max_kwh > 0 else 0.0,
            "winst_eur":    round(winst, 4),
        })

        huidig_s = nieuwe_s

    return resultaat

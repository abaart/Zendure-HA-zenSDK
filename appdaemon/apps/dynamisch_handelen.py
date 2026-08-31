"""
Dynamisch Handelen - Home Assistant integratie (AppDaemon)
==========================================================

AppDaemon app die ieder uur om :55 en op verzoek twee laad/ontlaadstrategieën
berekent: de gekozen strategie met penalties en de economisch optimale
vergelijkingsstrategie zonder penalties.

BESTANDSLOCATIES IN HA
-----------------------
  /config/appdaemon/apps/dynamisch_handelen.py   ← dit bestand
  /config/appdaemon/apps/strategie_dp.py         ← het algoritme
  /config/appdaemon/apps/wattwanneer_forecast.py ← HTTP- en SQLite-cachelogica

AppDaemon voegt de apps-map automatisch toe aan sys.path,
waardoor `from strategie_dp import ...` direct werkt.

GEBRUIKTE HA-ENTITEITEN
------------------------
Ingangen:
  input_text.dynamisch_nordpool_sensor                   Nordpool-bron met kwartierprijzen
  sensor.zendure_2400_ac_indicatie_beschikbare_energie   kWh leverbaar naar net
  sensor.zendure_2400_ac_indicatie_benodigde_energie     kWh nodig van net om vol te laden
  sensor.zendure_2400_ac_laadpercentage                  actuele SoC (%) voor actieve-slot-correctie
  sensor.zendure_2400_ac_rte_totaal                      round-trip efficiency (%)
  input_number.zendure_2400_ac_max_oplaadvermogen        max laadvermogen (W)
  input_number.zendure_2400_ac_max_ontlaadvermogen       max ontlaadvermogen (W)
  input_number.dynamisch_minimale_spread                 minimale spread (ct/kWh)
  input_number.dynamisch_warmte_penalty_laden_factor     gewicht voor warmteverlies bij laden
  input_number.dynamisch_warmte_penalty_ontladen_factor  gewicht voor warmteverlies bij ontladen
  input_number.dynamisch_warmte_afkoeling_halveringstijd_uren  afkoeling richting buitentemperatuur
  input_number.dynamisch_warmte_stijging_laden_c_per_c2h  accutemperatuurstijging door C² × uur laden
  input_number.dynamisch_warmte_stijging_ontladen_c_per_c2h  accutemperatuurstijging door C² × uur ontladen
  input_number.dynamisch_max_temp_boven_80_soc            accutemperatuurlimiet boven 80% SoC
  input_number.dynamisch_max_temp_onder_80_soc            accutemperatuurlimiet onder 80% SoC
  input_number.dynamisch_temp_penalty_factor              gewicht voor temperatuur-overschrijding
  input_number.dynamisch_temp_penalty_100_soc_factor      extra overtemp-gewicht bij 100% SoC
  input_number.dynamisch_hoge_soc_verblijf_penalty_factor verblijfskosten boven 90% SoC
  input_number.dynamisch_lage_soc_verblijf_penalty_factor verblijfskosten onder 10% SoC
  input_number.dynamisch_standby_verbruik_w               standbyverbruik bij niet-laden (W)
  input_number.dynamisch_minimum_vermogen_w               minimum laad/ontlaadvermogen voor DP (W)
  input_text.dynamisch_buitentemperatuur_sensor           optionele sensor met actuele en historische buitentemperatuur
  input_text.dynamisch_weather_entity                     optionele weather entity voor forecast
  input_button.dynamisch_handelsstrategie_herberekenen   knop voor handmatige herberekening
  input_button.dynamisch_strategie_advies_herberekenen   knop voor handmatige adviesanalyse
  input_number.dynamisch_advies_analyse_dagen            aantal dagen historie voor advies
  input_boolean.dynamisch_handelsstrategie_berekening_bezig  laadstatus voor dashboardknop

Uitgang:
  sensor.dynamisch_handelsstrategie   gekozen schema met penalties + prijs/RTE-winst (€)
  sensor.dynamisch_handelsstrategie_economisch  economisch schema + prijs/RTE-winst (€)
  sensor.dynamisch_strategie_advies   advies over DP- en thermische parameters
  sensor.dynamisch_handelsstrategie_berekening_duur  duur van de laatste bereken-run
  sensor.wattwanneer_forecast_status  ophaal-, cache- en kalibratiestatus
"""

import math
from datetime import datetime, time, timedelta, timezone
from time import monotonic
from zoneinfo import ZoneInfo

import appdaemon.plugins.hass.hassapi as hass

# strategie_dp.py staat in dezelfde apps-map; AppDaemon zet die map op sys.path.
from strategie_dp import (
    Accustatus,
    DP_VERMOGEN_STAP_W,
    HOGE_SOC_VERBLIJF_PENALTY_FACTOR,
    LAGE_SOC_VERBLIJF_PENALTY_FACTOR,
    MINIMUM_VERMOGEN_W,
    STANDBY_VERBRUIK_W,
    StrategieBerekeningGeannuleerd,
    WARMTE_STIJGING_LADEN_C_PER_C2H,
    WARMTE_STIJGING_ONTLADEN_C_PER_C2H,
    WARMTE_PENALTY_LADEN_FACTOR,
    WARMTE_PENALTY_ONTLADEN_FACTOR,
    TEMP_LIMIET_C,
    TEMP_LIMIET_LAGE_SOC_C,
    TEMP_PENALTY_100_SOC_FACTOR,
    TEMP_PENALTY_FACTOR,
    VERMOGEN_STAP_W,
    bereken_derating,
    bereken_laadvermogen_voor_aansturing,
    los_dp_op,
    rond_vermogen_omhoog,
)
from wattwanneer_forecast import (
    WATTWANNEER_URL,
    WattWanneerCacheResultaat,
    WattWanneerForecastCache,
)


GRAFIEK_HISTORIE_UREN = 6.0
KWARTIER_SLOT_MINUTEN = 15
DEFAULT_MINIMALE_SPREAD_CT_PER_KWH = 2.0
PLANNING_HORIZON_UREN = 72.0
FALLBACK_PRIJS_BASIS_UREN = 24
FALLBACK_SLOT_UREN = 1.0
PLANNING_TIJDZONE = ZoneInfo("Europe/Amsterdam")
ECONOMISCHE_STRATEGIE_ENTITY = "sensor.dynamisch_handelsstrategie_economisch"
DEFAULT_WATTWANNEER_CACHE_DB_PATH = "/share/zendure_kwartieren.sqlite"
MIN_WATTWANNEER_KALIBRATIE_UREN = 6
MAX_WATTWANNEER_KALIBRATIE_RESTFOUT_EUR_KWH = 0.0005
ADVIES_SAMPLE_STAP_MINUTEN = 5
ADVIES_ACTIEF_DREMPEL_W = 100.0
ADVIES_MIN_ACTIEF_BLOK_MINUTEN = 15
ADVIES_MIN_RUST_BLOK_MINUTEN = 30
ADVIES_MIN_C2H = 0.01
ADVIES_MIN_TEMP_STIJGING_C = 1.0
ADVIES_MIN_TEMP_DALING_C = 1.0
ADVIES_MIN_KOELVERSCHIL_C = 2.0
ADVIES_THERMISCHE_RUST_MAX_C = 0.10
ADVIES_MAX_KOELBLOK_WARMTE_C = 0.25


def bereken_prijs_rte_winst_eur(schema: list[dict]) -> float:
    """Telt alleen de prijs- en RTE-opbrengst van de gekozen slotacties op."""
    return sum(float(slot.get("winst_eur") or 0.0) for slot in schema)


def bereken_penalty_totalen_eur(schema: list[dict]) -> dict[str, float]:
    """Telt de niet-overlappende DP-penaltycategorieën over het schema op."""
    totalen = {
        "warmte_laden_eur": 0.0,
        "warmte_ontladen_eur": 0.0,
        "overtemp_eur": 0.0,
        "hoge_soc_verblijf_eur": 0.0,
        "lage_soc_verblijf_eur": 0.0,
    }

    for slot in schema:
        actie = slot.get("geplande_actie") or slot.get("actie")
        warmte_penalty = float(slot.get("warmte_penalty_eur") or 0.0)
        if actie == "laden":
            totalen["warmte_laden_eur"] += warmte_penalty
        elif actie == "ontladen":
            totalen["warmte_ontladen_eur"] += warmte_penalty

        overtemp_penalty = slot.get("overtemp_penalty_eur")
        if overtemp_penalty is None:
            overtemp_penalty = slot.get("temp_penalty_eur")
        totalen["overtemp_eur"] += float(overtemp_penalty or 0.0)
        totalen["hoge_soc_verblijf_eur"] += float(
            slot.get("hoge_soc_verblijf_penalty_eur") or 0.0
        )
        totalen["lage_soc_verblijf_eur"] += float(
            slot.get("lage_soc_verblijf_penalty_eur") or 0.0
        )

    afgerond = {naam: round(waarde, 6) for naam, waarde in totalen.items()}
    afgerond["totaal_eur"] = round(sum(totalen.values()), 6)
    return afgerond


def formatteer_penalty_attributen(totalen: dict[str, float]) -> dict[str, str]:
    """Publiceert ook nulwaarden; AppDaemon laat numerieke 0.0-attributen weg."""
    namen = {
        "penalty_totaal_eur": "totaal_eur",
        "warmte_penalty_laden_totaal_eur": "warmte_laden_eur",
        "warmte_penalty_ontladen_totaal_eur": "warmte_ontladen_eur",
        "overtemp_penalty_totaal_eur": "overtemp_eur",
        "hoge_soc_verblijf_penalty_totaal_eur": "hoge_soc_verblijf_eur",
        "lage_soc_verblijf_penalty_totaal_eur": "lage_soc_verblijf_eur",
    }
    return {
        attribuut: f"{float(totalen.get(categorie, 0.0)):.6f}"
        for attribuut, categorie in namen.items()
    }


def _lees_slot_datetimes(slot: dict) -> tuple[datetime, datetime] | None:
    """Leest start en end uit een strategieslot als timezone-aware datetimes."""
    try:
        start_raw = slot["start"]
        end_raw = slot["end"]
    except KeyError:
        return None

    def parse(value) -> datetime:
        if isinstance(value, datetime):
            dt = value
        else:
            dt = datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            return dt.astimezone()
        return dt.astimezone()

    try:
        return parse(start_raw), parse(end_raw)
    except (TypeError, ValueError):
        return None


def _lees_datetime_waarde(waarde) -> datetime | None:
    """Leest een datetime-waarde uit HA history of sensorattributen."""
    if isinstance(waarde, datetime):
        return waarde.astimezone()

    if waarde is None:
        return None

    tekst = str(waarde).strip()
    if not tekst:
        return None

    try:
        return datetime.fromisoformat(tekst.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None


def _percentiel(waarden: list[float], fractie: float) -> float | None:
    """Berekent een lineair geinterpoleerd percentiel voor een kleine meetreeks."""
    if not waarden:
        return None

    gesorteerd = sorted(waarden)
    positie = min(1.0, max(0.0, fractie)) * (len(gesorteerd) - 1)
    links = int(math.floor(positie))
    rechts = int(math.ceil(positie))
    if links == rechts:
        return gesorteerd[links]
    gewicht_rechts = positie - links
    return (
        gesorteerd[links] * (1.0 - gewicht_rechts)
        + gesorteerd[rechts] * gewicht_rechts
    )


def _betrouwbaarheid_voor_metingen(aantal: int) -> str:
    """Geeft een eenvoudige betrouwbaarheidsklasse op basis van onafhankelijke blokken."""
    if aantal < 3:
        return "laag"
    if aantal < 8:
        return "middel"
    return "hoog"


def bereken_thermische_meetstatistiek(
    vermogen_samples: list[tuple[datetime, float | None]],
    temperatuur_samples: list[tuple[datetime, float | None]],
    accu_max_kwh: float,
    *,
    buiten_samples: list[tuple[datetime, float | None]] | None = None,
    nu: datetime | None = None,
) -> dict:
    """
    Schat thermische modelwaarden rechtstreeks uit gemeten historie.

    Het vermogen wordt op een vijfminutenraster gezet. Aaneengesloten laad- en
    ontlaadblokken leveren C²×h en gemeten temperatuurstijging. De gewogen
    factor is som(stijging) / som(C²×h) over blokken met minimaal 1 °C
    stijging. De ingestelde opwarmingsfactor komt niet in de formule voor.

    Voor de omgevingstemperatuur gebruikt de analyse uitsluitend historische
    states van een temperatuursensor. Toekomstige forecastwaarden uit
    `slots[].buiten_temp_c` worden niet gebruikt. Een thermisch rustig blok mag
    vermogensbewegingen tot 0,10 C bevatten. De gemeten temperatuur moet in
    minimaal 30 minuten minstens 1 °C dalen; kleine berekende warmtebijdragen
    worden vóór de halveringstijdberekening afgetrokken.
    """
    buiten_samples = buiten_samples or []
    nu = (nu or datetime.now().astimezone()).astimezone()

    def normaliseer(
        samples: list[tuple[datetime, float | None]],
    ) -> list[tuple[float, float | None]]:
        resultaat: list[tuple[float, float | None]] = []
        for tijd, waarde in samples:
            if tijd.tzinfo is None:
                tijd = tijd.astimezone()
            if waarde is None:
                resultaat.append((tijd.timestamp(), None))
                continue
            try:
                getal = float(waarde)
            except (TypeError, ValueError):
                resultaat.append((tijd.timestamp(), None))
                continue
            if math.isfinite(getal):
                resultaat.append((tijd.timestamp(), getal))
            else:
                resultaat.append((tijd.timestamp(), None))
        return sorted(resultaat)

    vermogen = normaliseer(vermogen_samples)
    temperaturen = normaliseer(temperatuur_samples)
    buiten = normaliseer(buiten_samples)
    if accu_max_kwh <= 0 or not vermogen or not temperaturen:
        return {
            "status": "onvoldoende_data",
            "accu_max_kwh": round(max(0.0, accu_max_kwh), 3),
            "vermogen_samples": len(vermogen),
            "temperatuur_samples": len(temperaturen),
            "buiten_samples": len(buiten),
            "laden": {},
            "ontladen": {},
            "afkoeling": {"status": "onvoldoende_data", "metingen": 0},
        }

    stap_s = ADVIES_SAMPLE_STAP_MINUTEN * 60
    start_epoch = math.ceil(max(vermogen[0][0], temperaturen[0][0]) / stap_s) * stap_s
    eind_epoch = math.floor(nu.timestamp() / stap_s) * stap_s
    if eind_epoch - start_epoch < stap_s:
        return {
            "status": "onvoldoende_data",
            "accu_max_kwh": round(accu_max_kwh, 3),
            "vermogen_samples": len(vermogen),
            "temperatuur_samples": len(temperaturen),
            "buiten_samples": len(buiten),
            "laden": {},
            "ontladen": {},
            "afkoeling": {"status": "onvoldoende_data", "metingen": 0},
        }

    indices = {"vermogen": 0, "temperatuur": 0, "buiten": 0}

    def laatste_waarde(
        samples: list[tuple[float, float | None]],
        sleutel: str,
        epoch: float,
        max_ouderdom_s: float | None = None,
    ) -> float | None:
        if not samples or samples[0][0] > epoch:
            return None
        index = indices[sleutel]
        while index + 1 < len(samples) and samples[index + 1][0] <= epoch:
            index += 1
        indices[sleutel] = index
        sample_tijd, sample_waarde = samples[index]
        if max_ouderdom_s is not None and epoch - sample_tijd > max_ouderdom_s:
            return None
        return sample_waarde

    actieve_blokken: list[dict] = []
    koelblokken: list[dict] = []
    blok: dict | None = None
    koelblok: dict | None = None
    thermische_rust_grens_w = (
        ADVIES_THERMISCHE_RUST_MAX_C * accu_max_kwh * 1000.0
    )

    def sluit_blok() -> None:
        nonlocal blok
        if blok is None:
            return
        actieve_blokken.append(blok)
        blok = None

    def sluit_koelblok() -> None:
        nonlocal koelblok
        if koelblok is not None:
            koelblokken.append(koelblok)
        koelblok = None

    epoch = start_epoch
    while epoch + stap_s <= eind_epoch:
        volgend = epoch + stap_s
        # Een Recorder-state blijft geldig tot de volgende state. Een sensor die
        # urenlang exact 0 W blijft, krijgt dus terecht geen nieuwe records.
        # Expliciete unknown/unavailable-records staan als None in de reeks en
        # onderbreken het blok wel.
        vermogen_w = laatste_waarde(vermogen, "vermogen", epoch)
        temp_voor = laatste_waarde(temperaturen, "temperatuur", epoch)
        temp_na = laatste_waarde(temperaturen, "temperatuur", volgend)
        buiten_c = laatste_waarde(buiten, "buiten", epoch)

        if vermogen_w is None or temp_voor is None or temp_na is None:
            sluit_blok()
            sluit_koelblok()
            epoch = volgend
            continue

        if vermogen_w >= ADVIES_ACTIEF_DREMPEL_W:
            modus = "laden"
        elif vermogen_w <= -ADVIES_ACTIEF_DREMPEL_W:
            modus = "ontladen"
        else:
            modus = "rust"

        if modus == "rust":
            sluit_blok()
        else:
            if blok is None or blok["modus"] != modus:
                sluit_blok()
                blok = {
                    "modus": modus,
                    "start_epoch": epoch,
                    "end_epoch": volgend,
                    "temp_voor_c": temp_voor,
                    "temp_na_c": temp_na,
                    "duur_h": 0.0,
                    "c2h": 0.0,
                    "c_h": 0.0,
                }

        duur_h = stap_s / 3600.0
        if modus != "rust":
            blok["end_epoch"] = volgend
            blok["temp_na_c"] = temp_na
            blok["duur_h"] += duur_h
            c_waarde = abs(vermogen_w) / 1000.0 / accu_max_kwh
            blok["c2h"] += c_waarde * c_waarde * duur_h
            blok["c_h"] += c_waarde * duur_h

        if abs(vermogen_w) < thermische_rust_grens_w:
            if koelblok is None:
                koelblok = {
                    "start_epoch": epoch,
                    "end_epoch": volgend,
                    "temp_voor_c": temp_voor,
                    "temp_na_c": temp_na,
                    "stappen": 0,
                    "buiten_stappen": 0,
                    "buiten_c_som": 0.0,
                    "laden_c2h": 0.0,
                    "ontladen_c2h": 0.0,
                }
            koelblok["end_epoch"] = volgend
            koelblok["temp_na_c"] = temp_na
            koelblok["stappen"] += 1
            if buiten_c is not None:
                koelblok["buiten_stappen"] += 1
                koelblok["buiten_c_som"] += buiten_c
            c_waarde = abs(vermogen_w) / 1000.0 / accu_max_kwh
            if vermogen_w > 0:
                koelblok["laden_c2h"] += c_waarde * c_waarde * duur_h
            elif vermogen_w < 0:
                koelblok["ontladen_c2h"] += c_waarde * c_waarde * duur_h
        else:
            sluit_koelblok()

        epoch = volgend

    sluit_blok()
    sluit_koelblok()

    def vat_actieve_modus_samen(modus: str) -> dict:
        minimum_duur_s = ADVIES_MIN_ACTIEF_BLOK_MINUTEN * 60
        gekwalificeerd = [
            item
            for item in actieve_blokken
            if item["modus"] == modus
            and item["end_epoch"] - item["start_epoch"] >= minimum_duur_s
            and item["c2h"] >= ADVIES_MIN_C2H
        ]
        stijgend = []
        for item in gekwalificeerd:
            delta_c = item["temp_na_c"] - item["temp_voor_c"]
            if delta_c < ADVIES_MIN_TEMP_STIJGING_C:
                continue
            factor = delta_c / item["c2h"]
            stijgend.append({**item, "delta_c": delta_c, "factor": factor})

        factoren = [item["factor"] for item in stijgend]
        som_c2h = sum(item["c2h"] for item in stijgend)
        schatting = (
            sum(item["delta_c"] for item in stijgend) / som_c2h
            if som_c2h > 0
            else None
        )
        totale_duur_h = sum(item["duur_h"] for item in gekwalificeerd)
        gemiddelde_c = (
            sum(item["c_h"] for item in gekwalificeerd) / totale_duur_h
            if totale_duur_h > 0
            else None
        )
        return {
            "blokken": len(gekwalificeerd),
            "stijgende_blokken": len(stijgend),
            "schatting_c_per_c2h": round(schatting, 2) if schatting is not None else None,
            "mediaan_c_per_c2h": round(_percentiel(factoren, 0.5), 2) if factoren else None,
            "p25_c_per_c2h": round(_percentiel(factoren, 0.25), 2) if factoren else None,
            "p75_c_per_c2h": round(_percentiel(factoren, 0.75), 2) if factoren else None,
            "gemiddelde_c": round(gemiddelde_c, 3) if gemiddelde_c is not None else None,
            "betrouwbaarheid": _betrouwbaarheid_voor_metingen(len(stijgend)),
        }

    laden = vat_actieve_modus_samen("laden")
    ontladen = vat_actieve_modus_samen("ontladen")
    factor_laden = float(laden.get("schatting_c_per_c2h") or 0.0)
    factor_ontladen = float(ontladen.get("schatting_c_per_c2h") or 0.0)

    koeltijden: list[float] = []
    minimum_rust_s = ADVIES_MIN_RUST_BLOK_MINUTEN * 60
    afwijzingen = {
        "te_kort": 0,
        "onvoldoende_buitentemperatuur": 0,
        "te_weinig_temperatuurdaling": 0,
        "te_klein_startverschil": 0,
        "te_veel_warmtecorrectie": 0,
        "koelt_niet_naar_omgeving": 0,
        "halveertijd_buiten_bereik": 0,
    }
    blokken_voldoende_duur = 0
    if buiten:
        for item in koelblokken:
            duur_s = item["end_epoch"] - item["start_epoch"]
            if duur_s < minimum_rust_s:
                afwijzingen["te_kort"] += 1
                continue
            blokken_voldoende_duur += 1
            if item["buiten_stappen"] < item["stappen"] * 0.8:
                afwijzingen["onvoldoende_buitentemperatuur"] += 1
                continue
            if item["temp_voor_c"] - item["temp_na_c"] < ADVIES_MIN_TEMP_DALING_C:
                afwijzingen["te_weinig_temperatuurdaling"] += 1
                continue
            omgeving_c = item["buiten_c_som"] / item["buiten_stappen"]
            verschil_voor = item["temp_voor_c"] - omgeving_c
            if abs(verschil_voor) < ADVIES_MIN_KOELVERSCHIL_C:
                afwijzingen["te_klein_startverschil"] += 1
                continue
            warmtecorrectie_c = (
                factor_laden * item["laden_c2h"]
                + factor_ontladen * item["ontladen_c2h"]
            )
            if warmtecorrectie_c > ADVIES_MAX_KOELBLOK_WARMTE_C:
                afwijzingen["te_veel_warmtecorrectie"] += 1
                continue
            gecorrigeerde_temp_na_c = item["temp_na_c"] - warmtecorrectie_c
            verschil_na = gecorrigeerde_temp_na_c - omgeving_c
            ratio = verschil_na / verschil_voor
            if not 0.0 < ratio < 1.0:
                afwijzingen["koelt_niet_naar_omgeving"] += 1
                continue
            duur_h = duur_s / 3600.0
            halvering_h = duur_h * math.log(0.5) / math.log(ratio)
            if 0.25 <= halvering_h <= 72.0:
                koeltijden.append(halvering_h)
            else:
                afwijzingen["halveertijd_buiten_bereik"] += 1

    afkoeling_status = "ok" if koeltijden else (
        "geen_omgevingssensor" if not buiten else "onvoldoende_geldige_rustblokken"
    )
    heeft_schatting = any(
        deel.get("schatting_c_per_c2h") is not None
        for deel in (laden, ontladen)
    ) or bool(koeltijden)

    return {
        "status": "ok" if heeft_schatting else "onvoldoende_data",
        "analyse_vanaf": datetime.fromtimestamp(start_epoch, tz=nu.tzinfo).isoformat(),
        "analyse_tot": datetime.fromtimestamp(eind_epoch, tz=nu.tzinfo).isoformat(),
        "sample_stap_minuten": ADVIES_SAMPLE_STAP_MINUTEN,
        "accu_max_kwh": round(accu_max_kwh, 3),
        "vermogen_samples": len(vermogen),
        "temperatuur_samples": len(temperaturen),
        "buiten_samples": len(buiten),
        "laden": laden,
        "ontladen": ontladen,
        "afkoeling": {
            "status": afkoeling_status,
            "blokken": len(koelblokken),
            "blokken_voldoende_duur": blokken_voldoende_duur,
            "metingen": len(koeltijden),
            "schatting_h": round(_percentiel(koeltijden, 0.5), 2) if koeltijden else None,
            "p25_h": round(_percentiel(koeltijden, 0.25), 2) if koeltijden else None,
            "p75_h": round(_percentiel(koeltijden, 0.75), 2) if koeltijden else None,
            "betrouwbaarheid": _betrouwbaarheid_voor_metingen(len(koeltijden)),
            "thermische_rust_max_c": ADVIES_THERMISCHE_RUST_MAX_C,
            "thermische_rust_max_w": round(thermische_rust_grens_w),
            "max_warmtecorrectie_c": ADVIES_MAX_KOELBLOK_WARMTE_C,
            "afwijzingen": afwijzingen,
        },
    }


def bouw_wattwanneer_slots(records: list[dict]) -> list[dict]:
    """Zet gevalideerde WattWanneer-records om naar lokale slots van één uur."""
    slots: list[dict] = []
    for record in records:
        try:
            start = datetime.strptime(
                str(record["datetime"]),
                "%Y-%m-%d %H:%M",
            ).replace(tzinfo=PLANNING_TIJDZONE)
            end = (
                start.astimezone(timezone.utc) + timedelta(hours=1)
            ).astimezone(PLANNING_TIJDZONE)
            prijs = float(record["price_eur_kwh"])
            source = str(record["source"])
            generated_at = str(record["generated_at"])
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(prijs) or source not in {"entsoe_day_ahead", "model"}:
            continue
        slots.append(
            {
                "start": start,
                "end": end,
                "price": prijs,
                "ruwe_forecast_prijs": prijs,
                "duration_h": 1.0,
                "resolutie": "uurprijs_forecast",
                "prijs_bron": "WattWanneer",
                "prijs_is_forecast": True,
                "prijs_is_fallback": False,
                "forecast_source": source,
                "forecast_generated_at": generated_at,
            }
        )
    return sorted(slots, key=lambda slot: slot["start"])


def kalibreer_wattwanneer_prijzen(
    forecast_slots: list[dict],
    kwartier_slots: list[dict],
) -> dict:
    """Past WattWanneer-prijzen aan op de prijsbasis van de Nordpool-sensor."""
    meetpunten: list[tuple[float, float]] = []
    for forecast_slot in forecast_slots:
        if forecast_slot.get("forecast_source") != "entsoe_day_ahead":
            continue
        start = forecast_slot["start"]
        end = forecast_slot["end"]
        overlap = [
            slot
            for slot in kwartier_slots
            if slot["start"] >= start and slot["end"] <= end
        ]
        overlap.sort(key=lambda slot: slot["start"])
        if len(overlap) != 4:
            continue
        if overlap[0]["start"] != start or overlap[-1]["end"] != end:
            continue
        if any(overlap[index]["end"] != overlap[index + 1]["start"] for index in range(3)):
            continue
        duur = sum(float(slot["duration_h"]) for slot in overlap)
        if not math.isclose(duur, 1.0, abs_tol=1e-6):
            continue
        nordpool_prijs = sum(
            float(slot["price"]) * float(slot["duration_h"])
            for slot in overlap
        ) / duur
        meetpunten.append((float(forecast_slot["price"]), nordpool_prijs))

    if len(meetpunten) < MIN_WATTWANNEER_KALIBRATIE_UREN:
        raise ValueError(
            "onvoldoende prijsbasis-overlap: "
            f"{len(meetpunten)} volledige uren, minimaal {MIN_WATTWANNEER_KALIBRATIE_UREN} nodig"
        )

    x_gemiddeld = sum(x for x, _ in meetpunten) / len(meetpunten)
    y_gemiddeld = sum(y for _, y in meetpunten) / len(meetpunten)
    x_variantie = sum((x - x_gemiddeld) ** 2 for x, _ in meetpunten)
    if x_variantie <= 1e-10:
        raise ValueError("prijsbasis-overlap heeft te weinig prijsvariatie")
    factor = sum(
        (x - x_gemiddeld) * (y - y_gemiddeld)
        for x, y in meetpunten
    ) / x_variantie
    opslag = y_gemiddeld - factor * x_gemiddeld
    restfouten = [abs(y - (factor * x + opslag)) for x, y in meetpunten]
    max_restfout = max(restfouten)

    if not 0.5 <= factor <= 2.0:
        raise ValueError(f"prijsbasis-factor {factor:.6f} valt buiten 0,5-2,0")
    if not -0.5 <= opslag <= 0.5:
        raise ValueError(f"prijsbasis-opslag {opslag:.6f} EUR/kWh valt buiten bereik")
    if max_restfout > MAX_WATTWANNEER_KALIBRATIE_RESTFOUT_EUR_KWH:
        raise ValueError(
            "prijsbasis-restfout "
            f"{max_restfout:.6f} EUR/kWh is groter dan "
            f"{MAX_WATTWANNEER_KALIBRATIE_RESTFOUT_EUR_KWH:.6f}"
        )

    gekalibreerd: list[dict] = []
    for slot in forecast_slots:
        nieuw_slot = dict(slot)
        nieuw_slot["price"] = factor * float(slot["price"]) + opslag
        nieuw_slot["forecast_prijsfactor"] = factor
        nieuw_slot["forecast_prijsopslag_eur_kwh"] = opslag
        gekalibreerd.append(nieuw_slot)

    return {
        "slots": gekalibreerd,
        "meetpunten": len(meetpunten),
        "factor": factor,
        "opslag_eur_kwh": opslag,
        "max_restfout_eur_kwh": max_restfout,
    }


def haal_grafiek_slots_uit_history_items(
    strategie_items: list[dict],
    nu: datetime,
    historie_uren: float = GRAFIEK_HISTORIE_UREN,
) -> list[dict]:
    """
    Leest recente verlopen strategieslots uit recorder-history voor dashboardgrafieken.

    De functie gebruikt alleen `attributes.slots` uit oude sensorstates.
    `attributes.slots_grafiek` wordt genegeerd, zodat oude dashboard-history niet
    opnieuw in zichzelf wordt opgenomen.
    """
    grens = nu.astimezone() - timedelta(hours=historie_uren)
    gekozen: dict[tuple[str, str], tuple[datetime | None, dict]] = {}

    for item in strategie_items:
        attrs = item.get("attributes") or {}
        slots = attrs.get("slots") or []
        if not isinstance(slots, list):
            continue

        item_tijd = _lees_datetime_waarde(
            item.get("last_changed") or item.get("last_updated")
        )

        for slot in slots:
            if not isinstance(slot, dict):
                continue

            datetimes = _lees_slot_datetimes(slot)
            if datetimes is None:
                continue
            start, end = datetimes
            if end <= grens or start >= nu:
                continue
            if item_tijd is not None and item_tijd > end:
                continue

            sleutel = (start.isoformat(), end.isoformat())
            vorige = gekozen.get(sleutel)
            vorige_tijd = vorige[0] if vorige else None
            if vorige is not None:
                if item_tijd is None:
                    continue
                if vorige_tijd is not None and item_tijd <= vorige_tijd:
                    continue

            gekozen[sleutel] = (item_tijd, dict(slot))

    return [
        slot
        for _, (_, slot) in sorted(
            gekozen.items(),
            key=lambda item: item[0][0],
        )
    ]


def bouw_grafiek_slots(
    vorige_slots: list[dict],
    nieuwe_slots: list[dict],
    nu: datetime,
    historie_uren: float = GRAFIEK_HISTORIE_UREN,
) -> list[dict]:
    """
    Combineert recente verlopen slots met de nieuwe strategie voor dashboardgrafieken.

    `nieuwe_slots` blijft de actuele planning voor automatiseringen.
    `bouw_grafiek_slots` bewaart alleen oude slots waarvan `end` binnen het
    historievenster valt.
    """
    grens = nu.astimezone() - timedelta(hours=historie_uren)
    gecombineerd: dict[tuple[str, str], dict] = {}

    for slot in vorige_slots:
        datetimes = _lees_slot_datetimes(slot)
        if datetimes is None:
            continue
        start, end = datetimes
        if end <= grens or start >= nu:
            continue
        gecombineerd[(start.isoformat(), end.isoformat())] = dict(slot)

    for slot in nieuwe_slots:
        datetimes = _lees_slot_datetimes(slot)
        if datetimes is None:
            continue
        start, end = datetimes
        gecombineerd[(start.isoformat(), end.isoformat())] = dict(slot)

    return [
        slot
        for _, slot in sorted(
            gecombineerd.items(),
            key=lambda item: item[0][0],
        )
    ]


class DynamischHandelen(hass.Hass):

    def initialize(self):
        """
        AppDaemon roept initialize() aan bij opstarten en na een reload.
        We registreren hier de berekening op :55 en knop/config-triggers.
        """
        self.log("Dynamisch Handelen: gestart, schema wordt ieder uur om :55 herberekend")
        self._berekening_bezig = False
        self._herberekening_gepland = False
        self._laatste_herberekening_kwargs = None
        self._berekening_generatie = 0
        self._laatste_wattwanneer_metadata = {}
        self._zet_berekening_bezig(False)
        self._initialiseer_berekening_duur_sensor()
        self._initialiseer_advies_sensor()
        self._initialiseer_wattwanneer_status_sensor()
        self.run_hourly(self.bereken_strategie, time(0, 55, 0))
        self.listen_state(
            self._herbereken_op_knop,
            "input_button.dynamisch_handelsstrategie_herberekenen",
        )
        self.listen_state(
            self._herbereken_advies_op_knop,
            "input_button.dynamisch_strategie_advies_herberekenen",
        )
        self.listen_state(
            self._herbereken_advies_op_config,
            "input_number.dynamisch_advies_analyse_dagen",
        )
        self.listen_state(
            self._herbereken_op_config,
            "input_number.dynamisch_warmte_penalty_laden_factor",
        )
        self.listen_state(
            self._herbereken_op_config,
            "input_number.dynamisch_warmte_penalty_ontladen_factor",
        )
        for entity in (
            "input_number.dynamisch_warmte_afkoeling_halveringstijd_uren",
            "input_number.dynamisch_warmte_stijging_laden_c_per_c2h",
            "input_number.dynamisch_warmte_stijging_ontladen_c_per_c2h",
            # Oude helpernaam blijft een trigger voor installaties die de package nog niet hebben bijgewerkt.
            "input_number.dynamisch_warmte_stijging_c_per_c2h",
            "input_number.dynamisch_max_temp_boven_80_soc",
            "input_number.dynamisch_max_temp_onder_80_soc",
            "input_number.dynamisch_temp_penalty_factor",
            "input_number.dynamisch_temp_penalty_100_soc_factor",
            "input_number.dynamisch_hoge_soc_verblijf_penalty_factor",
            "input_number.dynamisch_lage_soc_verblijf_penalty_factor",
            "input_number.dynamisch_standby_verbruik_w",
            "input_number.dynamisch_minimum_vermogen_w",
            "input_number.dynamisch_minimale_spread",
            "input_number.zendure_2400_ac_max_oplaadvermogen",
            "input_number.zendure_2400_ac_max_ontlaadvermogen",
            "input_text.dynamisch_nordpool_sensor",
            "input_text.dynamisch_buitentemperatuur_sensor",
            "input_text.dynamisch_weather_entity",
        ):
            self.listen_state(self._herbereken_op_config, entity)

    # ── HOOFDFUNCTIE ─────────────────────────────────────────────────────────

    def _herbereken_op_knop(self, entity, attribute, old, new, kwargs):
        """Herberekent de strategie na een druk op de HA-knop."""
        self.log("Dynamisch Handelen: handmatige herberekening gestart via HA-knop")
        self.bereken_strategie({"trigger": entity})

    def _herbereken_op_config(self, entity, attribute, old, new, kwargs):
        """Herberekent de strategie wanneer een DP-configuratie wijzigt."""
        self.log(
            f"Dynamisch Handelen: herberekening gestart door wijziging van {entity}",
            level="INFO",
        )
        self.bereken_strategie({"trigger": entity})

    def _herbereken_advies_op_knop(self, entity, attribute, old, new, kwargs):
        """Herberekent het strategie-advies na een druk op de HA-knop."""
        self.log("Dynamisch Handelen: adviesanalyse handmatig gestart via HA-knop")
        self.bereken_strategie_advies({"trigger": entity})

    def _herbereken_advies_op_config(self, entity, attribute, old, new, kwargs):
        """Herberekent het strategie-advies wanneer de adviesconfiguratie wijzigt."""
        self.log(
            f"Dynamisch Handelen: adviesanalyse gestart door wijziging van {entity}",
            level="INFO",
        )
        self.bereken_strategie_advies({"trigger": entity})

    def bereken_strategie(self, kwargs):
        """
        Zet de dashboardknop op loading terwijl _bereken_strategie_impl() rekent.
        Extra triggers tijdens een lopende berekening annuleren die run en
        starten daarna één nieuwe run met de laatste trigger.
        """
        if self._berekening_bezig:
            self._berekening_generatie += 1
            self._herberekening_gepland = True
            self._laatste_herberekening_kwargs = kwargs or {}
            self._zet_berekening_bezig(False)
            self._zet_berekening_bezig(True)
            self.log(
                "Dynamisch Handelen: berekening loopt al; huidige run wordt geannuleerd en opnieuw gestart",
                level="INFO",
            )
            return

        self._berekening_bezig = True
        self._berekening_generatie += 1
        berekening_generatie = self._berekening_generatie
        berekening_start = monotonic()
        berekening_status = "voltooid"
        self._zet_berekening_bezig(True)
        try:
            self._bereken_strategie_impl(kwargs, berekening_generatie)
        except StrategieBerekeningGeannuleerd:
            berekening_status = "geannuleerd"
            self.log(
                "Dynamisch Handelen: strategie berekening geannuleerd door nieuwere input",
                level="INFO",
            )
        except Exception:
            berekening_status = "fout"
            raise
        finally:
            self._publiceer_berekening_duur(
                berekening_start,
                berekening_status,
                kwargs,
                berekening_generatie,
            )
            self._berekening_bezig = False
            self._zet_berekening_bezig(False)
            if self._herberekening_gepland:
                geplande_kwargs = self._laatste_herberekening_kwargs or {
                    "trigger": "geplande_herberekening",
                }
                self._herberekening_gepland = False
                self._laatste_herberekening_kwargs = None
                self.run_in(
                    self._voer_geplande_herberekening_uit,
                    1,
                    geplande_kwargs=geplande_kwargs,
                )

    def _initialiseer_berekening_duur_sensor(self) -> None:
        """Maakt de duur-sensor direct aan zodat het dashboard geen entity-melding toont."""
        if self.get_state("sensor.dynamisch_handelsstrategie_berekening_duur") is not None:
            return

        try:
            self.set_state(
                "sensor.dynamisch_handelsstrategie_berekening_duur",
                state="0.0",
                attributes={
                    "unit_of_measurement": "s",
                    "device_class": "duration",
                    "state_class": "measurement",
                    "friendly_name": "Dynamisch Strategie Berekening Duur",
                    "icon": "mdi:timer-outline",
                    "status": "nog_niet_berekend",
                    "trigger": None,
                    "generatie": self._berekening_generatie,
                    "laatst_bijgewerkt": datetime.now().astimezone().isoformat(),
                },
            )
        except Exception as exc:
            self.log(
                f"Dynamisch Handelen: kon berekening-duur sensor niet initialiseren: {exc}",
                level="DEBUG",
            )

    def _get_wattwanneer_cache(self) -> WattWanneerForecastCache:
        """Maakt de persistente forecastcache eenmalig per AppDaemon-instantie."""
        bestaande_cache = getattr(self, "_wattwanneer_cache", None)
        if bestaande_cache is not None:
            return bestaande_cache
        args = getattr(self, "args", {}) or {}
        self._wattwanneer_cache = WattWanneerForecastCache(
            db_path=str(
                args.get(
                    "wattwanneer_cache_db_path",
                    DEFAULT_WATTWANNEER_CACHE_DB_PATH,
                )
            ),
            url=str(args.get("wattwanneer_url", WATTWANNEER_URL)),
        )
        return self._wattwanneer_cache

    @staticmethod
    def _epoch_naar_iso(epoch: int | None) -> str | None:
        if epoch is None:
            return None
        return datetime.fromtimestamp(
            int(epoch),
            tz=timezone.utc,
        ).astimezone(PLANNING_TIJDZONE).isoformat()

    def _publiceer_wattwanneer_status(
        self,
        resultaat: WattWanneerCacheResultaat | None,
        *,
        extra_fout: str | None = None,
        kalibratie: dict | None = None,
        historie_fout: str | None = None,
        historie_metadata: dict | None = None,
    ) -> None:
        """Publiceert ophaal-, cache-, historie- en kalibratiestatus."""
        laatste_status = resultaat.laatste_status if resultaat else "failure"
        cache_beschikbaar = resultaat.cache_beschikbaar if resultaat else False
        fouten = [
            waarde
            for waarde in (
                extra_fout,
                historie_fout,
                resultaat.fout if resultaat else "cache niet beschikbaar",
            )
            if waarde
        ]
        fout = "; ".join(dict.fromkeys(fouten)) or None
        if extra_fout or historie_fout:
            state = "fout"
        elif laatste_status == "success" and cache_beschikbaar:
            state = "ok"
        elif laatste_status == "never":
            state = "nog_niet_opgehaald"
        else:
            state = "fout"
            if not fout and laatste_status == "in_progress":
                fout = "vorige ophaalpoging werd niet afgerond"

        attributes = {
            "friendly_name": "WattWanneer Forecast Status",
            "icon": "mdi:cloud-alert" if state != "ok" else "mdi:cloud-check",
            "status": state,
            "laatste_status": laatste_status,
            "laatste_poging": self._epoch_naar_iso(
                resultaat.laatste_poging_epoch if resultaat else None
            ),
            "laatste_succes": self._epoch_naar_iso(
                resultaat.laatste_succes_epoch if resultaat else None
            ),
            "volgende_poging": self._epoch_naar_iso(
                resultaat.volgende_poging_epoch if resultaat else None
            ),
            "poging_uitgevoerd": resultaat.poging_uitgevoerd if resultaat else False,
            "cache_beschikbaar": cache_beschikbaar,
            "cache_regels": len(resultaat.records) if resultaat else 0,
            "generated_at": resultaat.generated_at if resultaat else None,
            "forecast_fetch_id": (
                resultaat.payload_fetch_id if resultaat else None
            ),
            "laatste_poging_fetch_id": (
                resultaat.laatste_poging_fetch_id if resultaat else None
            ),
            "fout": fout,
            "historie_fout": historie_fout,
            "nordpool_historie_waarnemingen": (
                historie_metadata.get("waargenomen_slots")
                if historie_metadata
                else None
            ),
            "nordpool_nieuwe_prijsversies": (
                historie_metadata.get("nieuwe_prijsversies")
                if historie_metadata
                else None
            ),
            "kalibratie_history_id": (
                historie_metadata.get("kalibratie_history_id")
                if historie_metadata
                else None
            ),
            "url": str(
                (getattr(self, "args", {}) or {}).get(
                    "wattwanneer_url",
                    WATTWANNEER_URL,
                )
            ),
            "succes_interval_uren": 12,
            "fout_retry_interval_uren": 2,
            "planning_horizon_uren": PLANNING_HORIZON_UREN,
            "kalibratie_meetpunten": (
                kalibratie.get("meetpunten") if kalibratie else None
            ),
            "kalibratie_factor": (
                round(float(kalibratie["factor"]), 8) if kalibratie else None
            ),
            "kalibratie_opslag_eur_kwh": (
                round(float(kalibratie["opslag_eur_kwh"]), 8)
                if kalibratie
                else None
            ),
            "kalibratie_max_restfout_eur_kwh": (
                round(float(kalibratie["max_restfout_eur_kwh"]), 8)
                if kalibratie
                else None
            ),
        }
        self._laatste_wattwanneer_metadata = dict(attributes)
        try:
            self.set_state(
                "sensor.wattwanneer_forecast_status",
                state=state,
                attributes=attributes,
            )
        except Exception as exc:
            self.log(
                f"Dynamisch Handelen: kon WattWanneer-status niet publiceren: {exc}",
                level="DEBUG",
            )

    def _initialiseer_wattwanneer_status_sensor(self) -> None:
        """Herstelt bij AppDaemon-start de laatste persistente ophaalstatus."""
        try:
            resultaat = self._get_wattwanneer_cache().lees_status()
        except Exception as exc:
            self._publiceer_wattwanneer_status(
                None,
                extra_fout=f"SQLite-cache kon niet worden gelezen: {exc}",
            )
            return
        self._publiceer_wattwanneer_status(resultaat)

    def _publiceer_berekening_duur(
        self,
        start_monotonic: float,
        status: str,
        kwargs: dict | None,
        berekening_generatie: int,
    ) -> None:
        """Publiceert hoe lang de laatste strategie-run duurde."""
        duur_s = max(0.0, monotonic() - start_monotonic)
        trigger = kwargs.get("trigger") if isinstance(kwargs, dict) else None
        try:
            self.set_state(
                "sensor.dynamisch_handelsstrategie_berekening_duur",
                state=str(round(duur_s, 2)),
                attributes={
                    "unit_of_measurement": "s",
                    "device_class": "duration",
                    "state_class": "measurement",
                    "friendly_name": "Dynamisch Strategie Berekening Duur",
                    "icon": "mdi:timer-outline",
                    "status": status,
                    "trigger": trigger,
                    "generatie": berekening_generatie,
                    "laatst_bijgewerkt": datetime.now().astimezone().isoformat(),
                },
            )
        except Exception as exc:
            self.log(
                f"Dynamisch Handelen: kon berekening-duur sensor niet bijwerken: {exc}",
                level="DEBUG",
            )

    def _initialiseer_advies_sensor(self) -> None:
        """Maakt de advies-sensor direct aan zodat het dashboard geen entity-melding toont."""
        if self.get_state("sensor.dynamisch_strategie_advies") is not None:
            return

        self._publiceer_advies_sensor(
            "nog_niet_berekend",
            {
                "advies_tekst": "Adviesanalyse is nog niet uitgevoerd.",
                "adviesregels": [],
                "analyse_dagen": self._haal_advies_analyse_dagen(),
                "confidence": "onbekend",
                "history_beschikbaar": False,
                "laatst_bijgewerkt": datetime.now().astimezone().isoformat(),
            },
        )

    def bereken_strategie_advies(self, kwargs=None) -> None:
        """
        Analyseert recente strategie-runs en publiceert voorzichtig parameteradvies.

        De analyse gebruikt recorder-history als die via AppDaemon beschikbaar is.
        Zonder voldoende historische strategie-slots of temperatuurmetingen geeft
        de sensor expliciet lage betrouwbaarheid in plaats van schijnzekerheid.
        """
        dagen = self._haal_advies_analyse_dagen()
        trigger = kwargs.get("trigger") if isinstance(kwargs, dict) else None
        self.log(
            f"Dynamisch Handelen: adviesanalyse gestart over {dagen} dagen",
            level="INFO",
        )

        try:
            strategie_items = self._haal_history_items(
                "sensor.dynamisch_handelsstrategie",
                dagen,
                volledige_attributen=True,
            )
            temp_items = self._haal_history_items(
                "sensor.zendure_2400_ac_warmste_batterij_temperatuur",
                dagen,
            )
            vermogen_items = self._haal_history_items(
                "sensor.zendure_2400_ac_vermogen_aansturing",
                dagen,
            )
        except Exception as exc:
            self.log(
                f"Dynamisch Handelen: adviesanalyse kon history niet lezen: {exc}",
                level="WARNING",
            )
            self._publiceer_advies_sensor(
                "history_onbeschikbaar",
                {
                    "advies_tekst": "Recorder-history is niet beschikbaar voor de adviesanalyse.",
                    "adviesregels": ["Controleer of AppDaemon history mag lezen en of recorder actief is."],
                    "analyse_dagen": dagen,
                    "confidence": "laag",
                    "history_beschikbaar": False,
                    "trigger": trigger,
                    "laatst_bijgewerkt": datetime.now().astimezone().isoformat(),
                },
            )
            return

        buiten_entity = self._haal_buitentemperatuur_sensor_entity()
        buiten_items: list[dict] = []
        buiten_history_fout = None
        if buiten_entity:
            try:
                buiten_items = self._haal_history_items(buiten_entity, dagen)
            except Exception as exc:
                buiten_history_fout = str(exc)
                self.log(
                    "Dynamisch Handelen: adviesanalyse kon historie van "
                    f"{buiten_entity} niet lezen: {exc}",
                    level="WARNING",
                )

        slots_uit_history = self._haal_geanalyseerde_strategie_slots(strategie_items, dagen)
        slots_uit_huidige_sensor = self._haal_huidige_geanalyseerde_strategie_slots(dagen)
        slots = self._combineer_geanalyseerde_slots(
            slots_uit_history,
            slots_uit_huidige_sensor,
        )
        temp_samples = self._haal_temp_samples(temp_items, behoud_gaten=True)
        vermogen_samples = self._haal_numerieke_samples(
            vermogen_items,
            behoud_gaten=True,
        )
        buiten_samples = self._haal_numerieke_samples(
            buiten_items,
            behoud_gaten=True,
        )
        buiten_bron = f"{buiten_entity}.state_history" if buiten_samples else None
        accu_max_kwh = self._haal_advies_accu_max_kwh()
        meetstatistiek = bereken_thermische_meetstatistiek(
            vermogen_samples,
            temp_samples,
            accu_max_kwh,
            buiten_samples=buiten_samples,
        )
        advies = self._bouw_strategie_advies(
            slots,
            temp_samples,
            dagen,
            meetstatistiek=meetstatistiek,
            buiten_bron=buiten_bron,
        )
        advies["strategie_history_items"] = len(strategie_items)
        advies["strategie_history_items_met_slots"] = self._tel_history_items_met_slots(
            strategie_items
        )
        advies["strategie_slots_uit_history"] = len(slots_uit_history)
        advies["strategie_slots_uit_huidige_sensor"] = len(slots_uit_huidige_sensor)
        advies["temperatuur_samples"] = len(temp_samples)
        advies["vermogen_samples"] = len(vermogen_samples)
        advies["buitentemperatuur_samples"] = len(buiten_samples)
        advies["buitentemperatuur_sensor_samples"] = len(buiten_samples)
        advies["buitentemperatuur_entity"] = buiten_entity or "niet_ingesteld"
        advies["buitentemperatuur_bron"] = buiten_bron or "niet_beschikbaar"
        advies["buitentemperatuur_history_fout"] = buiten_history_fout
        advies["trigger"] = trigger
        self._publiceer_advies_sensor(advies.pop("state"), advies)

    def _publiceer_advies_sensor(self, state: str, attributes: dict) -> None:
        """Publiceert de strategie-advies sensor."""
        basis = {
            "friendly_name": "Dynamisch Strategie Advies",
            "icon": "mdi:lightbulb-on-outline",
        }
        basis.update(attributes)
        try:
            self.set_state(
                "sensor.dynamisch_strategie_advies",
                state=state,
                attributes=basis,
            )
        except Exception as exc:
            self.log(
                f"Dynamisch Handelen: kon advies-sensor niet bijwerken: {exc}",
                level="DEBUG",
            )

    def _haal_history_items(
        self,
        entity_id: str,
        dagen: int | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        volledige_attributen: bool = False,
    ) -> list[dict]:
        """Leest HA history en normaliseert AppDaemon's verschillende return-vormen."""
        kwargs = {
            "entity_id": entity_id,
            "significant_changes_only": False,
        }
        if volledige_attributen:
            kwargs["minimal_response"] = False
            kwargs["no_attributes"] = False
        if dagen is not None:
            kwargs["days"] = dagen
        if start_time is not None:
            kwargs["start_time"] = start_time
        if end_time is not None:
            kwargs["end_time"] = end_time

        try:
            history = self.get_history(**kwargs) or []
        except TypeError:
            compat_kwargs = {
                key: value
                for key, value in kwargs.items()
                if key not in (
                    "minimal_response",
                    "no_attributes",
                    "significant_changes_only",
                )
            }
            try:
                history = self.get_history(**compat_kwargs) or []
            except TypeError:
                if start_time is not None or end_time is not None:
                    try:
                        history = self.get_history(
                            entity_id,
                            start_time=start_time,
                            end_time=end_time,
                        ) or []
                    except TypeError:
                        history = self.get_history(entity_id, days=dagen or 1) or []
                else:
                    history = self.get_history(entity_id, days=dagen or 1) or []
        return self._flatten_history_items(history)

    def _haal_huidige_strategie_attributen(self) -> dict:
        """Leest de actuele strategie-attributen uit HA, met losse attribuut-fallbacks."""
        alle_state = (
            self.get_state("sensor.dynamisch_handelsstrategie", attribute="all") or {}
        )
        if isinstance(alle_state, dict):
            attributes = alle_state.get("attributes")
            if isinstance(attributes, dict):
                return attributes

        attributes = {}
        for attribuut in ("slots_grafiek", "slots"):
            waarde = self.get_state(
                "sensor.dynamisch_handelsstrategie",
                attribute=attribuut,
            )
            if waarde is not None:
                attributes[attribuut] = waarde
        return attributes

    def _haal_huidige_geanalyseerde_strategie_slots(self, dagen: int) -> list[dict]:
        """
        Leest verlopen slots uit de actuele strategie-sensor.

        HA recorder-history kan compacte states zonder `attributes.slots` teruggeven.
        De actuele sensor bewaart `slots_grafiek`, waarin recente verlopen slots staan.
        """
        attributes = self._haal_huidige_strategie_attributen()
        history_items = []
        for attribuut in ("slots_grafiek", "slots"):
            slots = attributes.get(attribuut)
            if isinstance(slots, list) and slots:
                history_items.append({"attributes": {"slots": slots}})

        if not history_items:
            return []

        return self._haal_geanalyseerde_strategie_slots(history_items, dagen)

    def _combineer_geanalyseerde_slots(self, *sloten_lijsten: list[dict]) -> list[dict]:
        """Combineert slotlijsten op start- en eindtijd zonder dubbele slots."""
        gekozen: dict[tuple[str, str], dict] = {}
        for slots in sloten_lijsten:
            for slot in slots:
                start = slot.get("_start_dt") or self._parse_datetime(slot.get("start"))
                end = slot.get("_end_dt") or self._parse_datetime(slot.get("end"))
                if start is None or end is None:
                    continue
                kopie = dict(slot)
                kopie["_start_dt"] = start
                kopie["_end_dt"] = end
                gekozen[(start.isoformat(), end.isoformat())] = kopie

        return sorted(gekozen.values(), key=lambda s: s["_start_dt"])

    def _tel_history_items_met_slots(self, items: list[dict]) -> int:
        """Telt history-items met een bruikbaar `attributes.slots` attribuut."""
        aantal = 0
        for item in items:
            attrs = item.get("attributes") or {}
            if isinstance(attrs.get("slots"), list):
                aantal += 1
        return aantal

    def _historische_float_state(
        self,
        entity_id: str,
        tijd: datetime | None,
        default: float | None = None,
    ) -> tuple[float | None, str]:
        """Leest de laatst bekende numerieke state op of voor `tijd`."""
        if tijd is None:
            waarde = self._float_state(entity_id)
            return (waarde if waarde is not None else default), "huidig"

        start_time = tijd - timedelta(hours=6)
        end_time = tijd + timedelta(minutes=1)
        items = self._haal_history_items(entity_id, start_time=start_time, end_time=end_time)

        beste_voor = None
        beste_voor_tijd = None
        eerste_na = None
        eerste_na_tijd = None

        for item in items:
            try:
                waarde = float(item.get("state"))
            except (TypeError, ValueError):
                continue

            item_tijd = (
                self._parse_datetime(item.get("last_changed"))
                or self._parse_datetime(item.get("last_updated"))
            )
            if item_tijd is None:
                continue

            if item_tijd <= tijd:
                if beste_voor_tijd is None or item_tijd > beste_voor_tijd:
                    beste_voor = waarde
                    beste_voor_tijd = item_tijd
                continue

            if eerste_na_tijd is None or item_tijd < eerste_na_tijd:
                eerste_na = waarde
                eerste_na_tijd = item_tijd

        if beste_voor is not None:
            return beste_voor, "history"

        if eerste_na is not None and eerste_na_tijd is not None:
            if (eerste_na_tijd - tijd).total_seconds() <= 120:
                return eerste_na, "history_na_start"

        waarde = self._float_state(entity_id)
        if waarde is not None:
            return waarde, "huidig_wegens_history_onbeschikbaar"
        return default, "default_wegens_history_onbeschikbaar"

    def _flatten_history_items(self, value) -> list[dict]:
        if isinstance(value, dict):
            if "state" in value or "attributes" in value:
                return [value]

            items: list[dict] = []
            for nested in value.values():
                items.extend(self._flatten_history_items(nested))
            return items

        if isinstance(value, list):
            items: list[dict] = []
            for nested in value:
                items.extend(self._flatten_history_items(nested))
            return items

        return []

    def _parse_datetime(self, waarde) -> datetime | None:
        return _lees_datetime_waarde(waarde)

    def _haal_geanalyseerde_strategie_slots(
        self,
        strategie_items: list[dict],
        dagen: int,
    ) -> list[dict]:
        """Pakt unieke, inmiddels verstreken strategieslots uit recente sensor-history."""
        nu = datetime.now().astimezone()
        sinds = nu - timedelta(days=dagen)
        gekozen: dict[tuple[str, str], dict] = {}

        for item in strategie_items:
            attrs = item.get("attributes") or {}
            item_tijd = self._parse_datetime(
                item.get("last_changed") or item.get("last_updated")
            )
            slots = attrs.get("slots") or []
            if not isinstance(slots, list):
                continue

            for slot in slots:
                if not isinstance(slot, dict):
                    continue

                start = self._parse_datetime(slot.get("start"))
                end = self._parse_datetime(slot.get("end"))
                if start is None or end is None or end < sinds or end > nu:
                    continue
                if item_tijd is not None and item_tijd > end:
                    continue

                sleutel = (start.isoformat(), end.isoformat())
                vorige = gekozen.get(sleutel)
                vorige_tijd = vorige.get("_item_tijd") if vorige else None
                if vorige is not None and item_tijd is not None and vorige_tijd is not None and item_tijd <= vorige_tijd:
                    continue

                kopie = dict(slot)
                kopie["_start_dt"] = start
                kopie["_end_dt"] = end
                kopie["_item_tijd"] = item_tijd
                gekozen[sleutel] = kopie

        return sorted(gekozen.values(), key=lambda s: s["_start_dt"])

    def _haal_numerieke_samples(
        self,
        items: list[dict],
        *,
        behoud_gaten: bool = False,
    ) -> list[tuple[datetime, float | None]]:
        """Zet HA-history om naar meetpunten en bewaart optioneel ongeldige states."""
        samples: list[tuple[datetime, float | None]] = []
        for item in items:
            tijd = self._parse_datetime(item.get("last_changed") or item.get("last_updated"))
            if tijd is None:
                continue
            try:
                waarde = float(item.get("state"))
            except (TypeError, ValueError):
                if behoud_gaten:
                    samples.append((tijd, None))
                continue
            if not math.isfinite(waarde):
                if behoud_gaten:
                    samples.append((tijd, None))
                continue
            samples.append((tijd, waarde))
        return sorted(samples, key=lambda item: item[0])

    def _haal_temp_samples(
        self,
        temp_items: list[dict],
        *,
        behoud_gaten: bool = False,
    ) -> list[tuple[datetime, float | None]]:
        """Backwards-compatible naam voor temperatuur-history."""
        return self._haal_numerieke_samples(
            temp_items,
            behoud_gaten=behoud_gaten,
        )

    def _haal_advies_accu_max_kwh(self) -> float:
        """Leest de DP-capaciteit waarmee gemeten vermogen naar C wordt omgerekend."""
        attributes = self._haal_huidige_strategie_attributen()
        try:
            waarde = float(attributes.get("accu_max_kwh"))
        except (TypeError, ValueError):
            return 0.0
        return waarde if math.isfinite(waarde) and waarde > 0 else 0.0

    def _temp_rond_tijd(
        self,
        samples: list[tuple[datetime, float | None]],
        tijd: datetime,
        marge_voor: timedelta = timedelta(minutes=45),
        marge_na: timedelta = timedelta(minutes=10),
    ) -> float | None:
        beste: tuple[datetime, float] | None = None
        for sample_tijd, waarde in samples:
            if sample_tijd > tijd + marge_na:
                break
            if waarde is not None and sample_tijd >= tijd - marge_voor:
                beste = (sample_tijd, waarde)
        return beste[1] if beste is not None else None

    def _mediaan(self, waarden: list[float]) -> float | None:
        if not waarden:
            return None

        gesorteerd = sorted(waarden)
        midden = len(gesorteerd) // 2
        if len(gesorteerd) % 2:
            return gesorteerd[midden]
        return (gesorteerd[midden - 1] + gesorteerd[midden]) / 2.0

    def _gemiddelde(self, waarden: list[float]) -> float:
        return sum(waarden) / len(waarden) if waarden else 0.0

    def _bouw_strategie_advies(
        self,
        slots: list[dict],
        temp_samples: list[tuple[datetime, float]],
        dagen: int,
        *,
        meetstatistiek: dict | None = None,
        buiten_bron: str | None = None,
    ) -> dict:
        """Combineert onafhankelijke meetstatistiek met expliciete DP-regeladviezen."""
        meetstatistiek = meetstatistiek or {}
        huidig_laden = self._haal_warmte_penalty_laden_factor()
        huidig_ontladen = self._haal_warmte_penalty_ontladen_factor()
        huidig_temp_penalty = self._haal_float_met_default(
            "input_number.dynamisch_temp_penalty_factor",
            TEMP_PENALTY_FACTOR,
            minimum=0.0,
        )
        huidig_stijging_laden = self._haal_warmte_stijging_factor(
            "input_number.dynamisch_warmte_stijging_laden_c_per_c2h",
            WARMTE_STIJGING_LADEN_C_PER_C2H,
        )
        huidig_stijging_ontladen = self._haal_warmte_stijging_factor(
            "input_number.dynamisch_warmte_stijging_ontladen_c_per_c2h",
            WARMTE_STIJGING_ONTLADEN_C_PER_C2H,
        )
        huidig_halvering = self._haal_float_met_default(
            "input_number.dynamisch_warmte_afkoeling_halveringstijd_uren",
            2.0,
            minimum=0.05,
        )

        laad_slots = [s for s in slots if s.get("actie") == "laden"]
        ontlaad_slots = [s for s in slots if s.get("actie") == "ontladen"]
        rust_slots = [s for s in slots if s.get("actie") == "rust"]
        actie_slots = laad_slots + ontlaad_slots
        overtemp_slots = [
            s for s in slots
            if float(s.get("overtemp_penalty_eur") or s.get("temp_penalty_eur") or 0.0) > 0
        ]

        c_laden = [float(s.get("c_waarde") or 0.0) for s in laad_slots]
        c_ontladen = [float(s.get("c_waarde") or 0.0) for s in ontlaad_slots]
        warmte_laden_ct = [
            float(s.get("warmte_penalty_eur") or 0.0) * 100.0 for s in laad_slots
        ]
        warmte_ontladen_ct = [
            float(s.get("warmte_penalty_eur") or 0.0) * 100.0 for s in ontlaad_slots
        ]
        overtemp_ct = [
            float(s.get("overtemp_penalty_eur") or s.get("temp_penalty_eur") or 0.0) * 100.0
            for s in slots
        ]

        fouten_laden: list[float] = []
        fouten_ontladen: list[float] = []
        fouten_rust: list[float] = []
        for slot in slots:
            voorspeld = slot.get("batterij_temp_na_c")
            if voorspeld is None:
                continue

            gemeten = self._temp_rond_tijd(temp_samples, slot["_end_dt"])
            if gemeten is None:
                continue

            fout = gemeten - float(voorspeld)
            if slot.get("actie") == "laden":
                fouten_laden.append(fout)
            elif slot.get("actie") == "ontladen":
                fouten_ontladen.append(fout)
            else:
                fouten_rust.append(fout)

        mediaan_fout_laden = self._mediaan(fouten_laden)
        mediaan_fout_ontladen = self._mediaan(fouten_ontladen)
        mediaan_fout_rust = self._mediaan(fouten_rust)

        laad_meting = meetstatistiek.get("laden") or {}
        ontlaad_meting = meetstatistiek.get("ontladen") or {}
        afkoel_meting = meetstatistiek.get("afkoeling") or {}
        statistisch_stijging_laden = laad_meting.get("schatting_c_per_c2h")
        statistisch_stijging_ontladen = ontlaad_meting.get("schatting_c_per_c2h")
        statistisch_halvering = afkoel_meting.get("schatting_h")

        # De fysieke adviezen zijn de gemeten schattingen zelf. De huidige
        # helperwaarden komen niet in die schattingen voor.
        aanbevolen_stijging_laden = statistisch_stijging_laden
        aanbevolen_stijging_ontladen = statistisch_stijging_ontladen
        aanbevolen_halvering = statistisch_halvering

        overtemp_ratio = len(overtemp_slots) / len(slots) if slots else 0.0
        aanbevolen_temp_penalty = huidig_temp_penalty
        if overtemp_ratio >= 0.08:
            aanbevolen_temp_penalty = max(0.05, huidig_temp_penalty * 1.25)
        elif len(slots) >= 20 and overtemp_ratio == 0.0 and self._gemiddelde(overtemp_ct) == 0.0:
            aanbevolen_temp_penalty = huidig_temp_penalty
        aanbevolen_temp_penalty = round(aanbevolen_temp_penalty, 3)

        aanbevolen_laden = round(huidig_laden, 2)
        if c_laden and self._gemiddelde(c_laden) >= 0.45 and self._gemiddelde(warmte_laden_ct) < 1.0:
            aanbevolen_laden = round(min(10.0, max(0.1, huidig_laden * 1.15)), 2)

        aanbevolen_ontladen = round(huidig_ontladen, 2)
        if c_ontladen and self._gemiddelde(c_ontladen) >= 0.45 and self._gemiddelde(warmte_ontladen_ct) < 1.0:
            aanbevolen_ontladen = round(min(10.0, max(0.1, huidig_ontladen * 1.15)), 2)

        regels: list[str] = []
        confidence_volgorde = {"laag": 0, "middel": 1, "hoog": 2}
        fysieke_confidences = [
            laad_meting.get("betrouwbaarheid", "laag"),
            ontlaad_meting.get("betrouwbaarheid", "laag"),
            afkoel_meting.get("betrouwbaarheid", "laag"),
        ]
        confidence = max(
            fysieke_confidences,
            key=lambda waarde: confidence_volgorde.get(waarde, 0),
        )
        state = "te_weinig_meetdata"
        temp_vergelijkingen = len(fouten_laden) + len(fouten_ontladen) + len(fouten_rust)
        instelling_wijkt_af = False

        def voeg_fysieke_schatting_toe(
            label: str,
            schatting: float | None,
            huidig: float,
            metingen: int,
            eenheid: str,
            betrouwbaarheid: str,
        ) -> None:
            nonlocal instelling_wijkt_af
            if schatting is None:
                regels.append(f"{label}: onvoldoende geldige meetblokken voor een schatting.")
                return
            regels.append(
                f"{label}: statistisch {schatting:.1f} {eenheid} uit {metingen} meetblokken; "
                f"ingesteld {huidig:.1f}."
            )
            relatieve_afwijking = abs(huidig - schatting) / max(abs(schatting), 0.01)
            if betrouwbaarheid != "laag" and relatieve_afwijking > 0.15:
                instelling_wijkt_af = True

        voeg_fysieke_schatting_toe(
            "Opwarming laden",
            statistisch_stijging_laden,
            huidig_stijging_laden,
            int(laad_meting.get("stijgende_blokken") or 0),
            "°C per C²h",
            laad_meting.get("betrouwbaarheid", "laag"),
        )
        voeg_fysieke_schatting_toe(
            "Opwarming ontladen",
            statistisch_stijging_ontladen,
            huidig_stijging_ontladen,
            int(ontlaad_meting.get("stijgende_blokken") or 0),
            "°C per C²h",
            ontlaad_meting.get("betrouwbaarheid", "laag"),
        )
        if statistisch_halvering is not None:
            voeg_fysieke_schatting_toe(
                "Afkoeling halveertijd",
                statistisch_halvering,
                huidig_halvering,
                int(afkoel_meting.get("metingen") or 0),
                "uur",
                afkoel_meting.get("betrouwbaarheid", "laag"),
            )
        elif not buiten_bron:
            regels.append(
                "Afkoeling halveertijd: niet statistisch berekenbaar zonder buitentemperatuurpunten."
            )
        else:
            regels.append(
                "Afkoeling halveertijd: de gebruikte buitentemperatuurreeks leverde "
                "onvoldoende geldige rustblokken."
            )

        if any(
            waarde is not None
            for waarde in (
                statistisch_stijging_laden,
                statistisch_stijging_ontladen,
                statistisch_halvering,
            )
        ):
            state = "instelling_wijkt_af" if instelling_wijkt_af else "statistiek_beschikbaar"

        if overtemp_ratio >= 0.08:
            state = "check_temperatuurlimieten"
            regels.append(
                f"Temp-straf kwam voor in {len(overtemp_slots)} van {len(slots)} slots; verhoog de straf of verlaag vermogen/temperatuurlimiet."
            )

        if actie_slots and self._gemiddelde([float(s.get("c_waarde") or 0.0) for s in actie_slots]) >= 0.45:
            regels.append("Gemiddelde C-waarde is hoog; warmtestraf voor laden en ontladen is zinvol.")

        def getal_tekst(waarde: float | None, decimalen: int, eenheid: str) -> str:
            if waarde is None:
                return "Onvoldoende data"
            return f"{float(waarde):.{decimalen}f} {eenheid}".strip()

        def spreiding_tekst(deel: dict, achtervoegsel: str) -> str:
            p25 = deel.get(f"p25_{achtervoegsel}")
            p75 = deel.get(f"p75_{achtervoegsel}")
            if p25 is None or p75 is None:
                return "Onvoldoende data"
            return f"{float(p25):.1f}–{float(p75):.1f}"

        def afkoelafwijzingen_tekst(deel: dict) -> str:
            labels = {
                "te_kort": "te kort",
                "onvoldoende_buitentemperatuur": "onvoldoende buitentemp",
                "te_weinig_temperatuurdaling": "minder dan 1 °C daling",
                "te_klein_startverschil": "startverschil onder 2 °C",
                "te_veel_warmtecorrectie": "warmtecorrectie boven 0,25 °C",
                "koelt_niet_naar_omgeving": "niet richting omgeving",
                "halveertijd_buiten_bereik": "halveertijd buiten bereik",
            }
            afwijzingen = deel.get("afwijzingen") or {}
            onderdelen = [
                f"{label}: {int(afwijzingen.get(sleutel) or 0)}"
                for sleutel, label in labels.items()
                if int(afwijzingen.get(sleutel) or 0) > 0
            ]
            return ", ".join(onderdelen) if onderdelen else "Geen afwijzingen"

        return {
            "state": state,
            "advies_tekst": " ".join(regels),
            "adviesregels": regels,
            "analyse_dagen": dagen,
            "geanalyseerde_slots": len(slots),
            "laad_slots": len(laad_slots),
            "ontlaad_slots": len(ontlaad_slots),
            "rust_slots": len(rust_slots),
            "temperatuur_vergelijkingen": temp_vergelijkingen,
            "confidence": confidence,
            "history_beschikbaar": True,
            "overtemp_slots": len(overtemp_slots),
            "overtemp_slots_ratio": round(overtemp_ratio, 3),
            "gemiddelde_overtemp_penalty_ct": round(self._gemiddelde(overtemp_ct), 3),
            "gemiddelde_warmte_penalty_laden_ct": round(self._gemiddelde(warmte_laden_ct), 3),
            "gemiddelde_warmte_penalty_ontladen_ct": round(self._gemiddelde(warmte_ontladen_ct), 3),
            "gemiddelde_c_laden": round(self._gemiddelde(c_laden), 3),
            "gemiddelde_c_ontladen": round(self._gemiddelde(c_ontladen), 3),
            "mediaan_temp_fout_laden_c": round(mediaan_fout_laden, 2) if mediaan_fout_laden is not None else None,
            "mediaan_temp_fout_ontladen_c": round(mediaan_fout_ontladen, 2) if mediaan_fout_ontladen is not None else None,
            "mediaan_temp_fout_rust_c": round(mediaan_fout_rust, 2) if mediaan_fout_rust is not None else None,
            "meetstatistiek_status": meetstatistiek.get("status", "onvoldoende_data"),
            "meetstatistiek_analyse_vanaf": meetstatistiek.get("analyse_vanaf"),
            "meetstatistiek_analyse_tot": meetstatistiek.get("analyse_tot"),
            "meetstatistiek_sample_stap_minuten": meetstatistiek.get("sample_stap_minuten"),
            "meetstatistiek_accu_max_kwh": meetstatistiek.get("accu_max_kwh"),
            "statistische_schatting_warmte_stijging_laden_c_per_c2h": statistisch_stijging_laden,
            "statistische_schatting_warmte_stijging_laden_tekst": getal_tekst(
                statistisch_stijging_laden, 1, "°C per C²h"
            ),
            "statistische_mediaan_warmte_stijging_laden_c_per_c2h": laad_meting.get("mediaan_c_per_c2h"),
            "statistische_p25_warmte_stijging_laden_c_per_c2h": laad_meting.get("p25_c_per_c2h"),
            "statistische_p75_warmte_stijging_laden_c_per_c2h": laad_meting.get("p75_c_per_c2h"),
            "statistische_spreiding_warmte_stijging_laden_tekst": spreiding_tekst(
                laad_meting, "c_per_c2h"
            ),
            "statistische_laadblokken": laad_meting.get("blokken", 0),
            "statistische_stijgende_laadblokken": laad_meting.get("stijgende_blokken", 0),
            "statistische_stijgende_laadblokken_tekst": str(
                int(laad_meting.get("stijgende_blokken") or 0)
            ),
            "statistische_betrouwbaarheid_laden": laad_meting.get("betrouwbaarheid", "laag"),
            "statistische_gemiddelde_c_laden": laad_meting.get("gemiddelde_c"),
            "statistische_schatting_warmte_stijging_ontladen_c_per_c2h": statistisch_stijging_ontladen,
            "statistische_schatting_warmte_stijging_ontladen_tekst": getal_tekst(
                statistisch_stijging_ontladen, 1, "°C per C²h"
            ),
            "statistische_mediaan_warmte_stijging_ontladen_c_per_c2h": ontlaad_meting.get("mediaan_c_per_c2h"),
            "statistische_p25_warmte_stijging_ontladen_c_per_c2h": ontlaad_meting.get("p25_c_per_c2h"),
            "statistische_p75_warmte_stijging_ontladen_c_per_c2h": ontlaad_meting.get("p75_c_per_c2h"),
            "statistische_spreiding_warmte_stijging_ontladen_tekst": spreiding_tekst(
                ontlaad_meting, "c_per_c2h"
            ),
            "statistische_ontlaadblokken": ontlaad_meting.get("blokken", 0),
            "statistische_stijgende_ontlaadblokken": ontlaad_meting.get("stijgende_blokken", 0),
            "statistische_stijgende_ontlaadblokken_tekst": str(
                int(ontlaad_meting.get("stijgende_blokken") or 0)
            ),
            "statistische_betrouwbaarheid_ontladen": ontlaad_meting.get("betrouwbaarheid", "laag"),
            "statistische_gemiddelde_c_ontladen": ontlaad_meting.get("gemiddelde_c"),
            "statistische_schatting_afkoeling_halveringstijd_h": statistisch_halvering,
            "statistische_schatting_afkoeling_tekst": getal_tekst(
                statistisch_halvering, 1, "uur"
            ),
            "statistische_spreiding_afkoeling_tekst": spreiding_tekst(
                afkoel_meting, "h"
            ),
            "statistische_afkoeling_metingen": afkoel_meting.get("metingen", 0),
            "statistische_afkoeling_metingen_tekst": str(
                int(afkoel_meting.get("metingen") or 0)
            ),
            "statistische_afkoeling_blokken": afkoel_meting.get("blokken", 0),
            "statistische_afkoeling_blokken_voldoende_duur": afkoel_meting.get(
                "blokken_voldoende_duur", 0
            ),
            "statistische_afkoeling_afwijzingen": afkoel_meting.get("afwijzingen", {}),
            "statistische_afkoeling_afwijzingen_tekst": afkoelafwijzingen_tekst(
                afkoel_meting
            ),
            "statistische_afkoeling_rustgrens_c": afkoel_meting.get(
                "thermische_rust_max_c"
            ),
            "statistische_afkoeling_rustgrens_w": afkoel_meting.get(
                "thermische_rust_max_w"
            ),
            "statistische_afkoeling_rustgrens_tekst": (
                f"{int(afkoel_meting.get('thermische_rust_max_w') or 0)} W "
                f"({float(afkoel_meting.get('thermische_rust_max_c') or 0.0):.2f} C)"
            ),
            "statistische_afkoeling_status": afkoel_meting.get("status", "onvoldoende_data"),
            "statistische_betrouwbaarheid_afkoeling": afkoel_meting.get("betrouwbaarheid", "laag"),
            "ingesteld_warmte_stijging_laden_c_per_c2h": huidig_stijging_laden,
            "ingesteld_warmte_stijging_ontladen_c_per_c2h": huidig_stijging_ontladen,
            "ingesteld_afkoeling_halveringstijd_h": huidig_halvering,
            "ingesteld_temp_penalty_factor": huidig_temp_penalty,
            "ingesteld_warmte_penalty_laden_factor": huidig_laden,
            "ingesteld_warmte_penalty_ontladen_factor": huidig_ontladen,
            "aanbevolen_warmte_stijging_laden_c_per_c2h": aanbevolen_stijging_laden,
            "aanbevolen_warmte_stijging_ontladen_c_per_c2h": aanbevolen_stijging_ontladen,
            "aanbevolen_afkoeling_halveringstijd_h": aanbevolen_halvering,
            "aanbevolen_temp_penalty_factor": aanbevolen_temp_penalty,
            "aanbevolen_warmte_penalty_laden_factor": aanbevolen_laden,
            "aanbevolen_warmte_penalty_ontladen_factor": aanbevolen_ontladen,
            "aanbevolen_temp_penalty_tekst": getal_tekst(aanbevolen_temp_penalty, 3, ""),
            "aanbevolen_warmte_penalty_laden_tekst": getal_tekst(aanbevolen_laden, 2, ""),
            "aanbevolen_warmte_penalty_ontladen_tekst": getal_tekst(aanbevolen_ontladen, 2, ""),
            "overtemp_slots_tekst": f"{len(overtemp_slots)} van {len(slots)} ({overtemp_ratio * 100:.1f}%)",
            "gemiddelde_c_laden_tekst": getal_tekst(self._gemiddelde(c_laden) if c_laden else None, 3, "C"),
            "gemiddelde_c_ontladen_tekst": getal_tekst(self._gemiddelde(c_ontladen) if c_ontladen else None, 3, "C"),
            "laatst_bijgewerkt": datetime.now().astimezone().isoformat(),
        }

    def _voer_geplande_herberekening_uit(self, kwargs):
        """Voert één extra herberekening uit na triggers tijdens een lopende run."""
        geplande_kwargs = kwargs.get("geplande_kwargs") if isinstance(kwargs, dict) else None
        self.bereken_strategie(geplande_kwargs or {"trigger": "geplande_herberekening"})

    def _zet_berekening_bezig(self, bezig: bool) -> None:
        """Stuurt de dashboard-helper voor de loading state van de refreshknop."""
        entity_id = "input_boolean.dynamisch_handelsstrategie_berekening_bezig"
        state = "on" if bezig else "off"
        service = "input_boolean/turn_on" if bezig else "input_boolean/turn_off"
        try:
            self.call_service(
                service,
                entity_id=entity_id,
            )
        except Exception as exc:
            self.log(
                f"Dynamisch Handelen: kon loading-helper niet bijwerken: {exc}",
                level="DEBUG",
            )
        try:
            self.set_state(
                entity_id,
                state=state,
                attributes={
                    "friendly_name": "Dynamisch Strategie Berekening Bezig",
                    "icon": "mdi:loading",
                },
            )
        except Exception as exc:
            self.log(
                f"Dynamisch Handelen: kon loading-helper state niet bijwerken: {exc}",
                level="DEBUG",
            )

    def _bereken_strategie_impl(self, kwargs, berekening_generatie: int):
        """
        Haalt data op, berekent de strategie en publiceert het resultaat.
        Fouten worden gelogd maar laten AppDaemon verder draaien.
        """
        self.log("Dynamisch Handelen: strategie berekening gestart", level="DEBUG")

        def is_geannuleerd() -> bool:
            return berekening_generatie != self._berekening_generatie

        def stop_als_geannuleerd() -> None:
            if is_geannuleerd():
                raise StrategieBerekeningGeannuleerd()

        try:
            slots = self._haal_prijsslots()
        except Exception as exc:
            self.log(f"Dynamisch Handelen: fout bij ophalen prijsslots: {exc}", level="ERROR")
            return
        stop_als_geannuleerd()

        if not slots:
            self.log("Dynamisch Handelen: geen prijsslots beschikbaar", level="WARNING")
            lege_penalty_attributen = formatteer_penalty_attributen({})
            self.set_state(
                "sensor.dynamisch_handelsstrategie",
                state="geen_data",
                attributes={
                    "slots": [],
                    "verwachte_winst_eur": 0,
                    **lege_penalty_attributen,
                },
            )
            self.set_state(
                ECONOMISCHE_STRATEGIE_ENTITY,
                state="geen_data",
                attributes={
                    "slots": [],
                    "verwachte_winst_eur": 0,
                    **lege_penalty_attributen,
                },
            )
            return

        try:
            dp_start_tijd = self._bepaal_dp_start_tijd(slots)
            accu, hw_min_pct, hw_max_pct, accu_bronnen = self._haal_accustatus(dp_start_tijd)
        except Exception as exc:
            self.log(f"Dynamisch Handelen: fout bij ophalen accustatus: {exc}", level="ERROR")
            return
        stop_als_geannuleerd()

        if accu.max_kwh <= 0:
            self.log(
                "Dynamisch Handelen: accucapaciteit onbekend (beschikbaar + benodigde = 0 kWh)",
                level="WARNING",
            )
            return

        min_spread = self._haal_minimale_spread()
        warmte_penalty_laden_factor = self._haal_warmte_penalty_laden_factor()
        warmte_penalty_ontladen_factor = self._haal_warmte_penalty_ontladen_factor()
        standby_verbruik_w = self._haal_standby_verbruik_w()
        minimum_vermogen_w = self._haal_minimum_vermogen_w()
        plateau_spreiding = self._haal_plateau_spreiding()
        thermisch = self._haal_thermische_config(slots, dp_start_tijd)
        stop_als_geannuleerd()
        kwartierprijs_slots = [
            s for s in slots
            if s.get("resolutie") == "kwartierprijs"
        ]
        forecast_prijs_slots = [
            s for s in slots
            if s.get("prijs_is_forecast") is True
        ]
        fallback_prijs_slots = [
            s for s in slots
            if s.get("prijs_is_fallback") is True
        ]
        kwartier_horizon_h = sum(
            float(s.get("duration_h", 0.0))
            for s in kwartierprijs_slots
        )
        planning_horizon_h = sum(
            float(s.get("duration_h", 0.0))
            for s in slots
        )
        fallback_prijs_ct = (
            round(float(fallback_prijs_slots[0]["price"]) * 100.0, 3)
            if fallback_prijs_slots
            else None
        )
        fallback_prijs_basis_slots = (
            int(fallback_prijs_slots[0].get("fallback_prijs_basis_slots", 0))
            if fallback_prijs_slots
            else 0
        )
        prijs_bron = slots[0].get("prijs_bron")

        self.log(
            f"Dynamisch Handelen: {len(kwartierprijs_slots)} kwartierprijzen "
            f"+ {len(forecast_prijs_slots)} WattWanneer-uurprijzen "
            f"+ {len(fallback_prijs_slots)} uurfallbackslots "
            f"over {planning_horizon_h:.2f} uur uit {prijs_bron} | "
            f"accu {accu.huidig_kwh:.2f}/{accu.max_kwh:.2f} kWh | "
            f"eta={accu.eta_laad:.3f} | "
            f"laad {accu.max_laad_w:.0f} W / ontlaad {accu.max_ontlaad_w:.0f} W | "
            f"min spread {min_spread:.1f} ct/kWh | "
            f"warmte laden {warmte_penalty_laden_factor:.2f} | "
            f"warmte ontladen {warmte_penalty_ontladen_factor:.2f} | "
            f"standby {standby_verbruik_w:.1f} W | "
            f"minimum vermogen {minimum_vermogen_w:d} W | "
            f"plateau {'aan' if plateau_spreiding else 'uit'} | "
            f"accutemp {thermisch['batterij_temp_start_c'] if thermisch['batterij_temp_start_c'] is not None else '-'} °C | "
            f"warmte stijging laden {thermisch['warmte_stijging_laden_c_per_c2h']:.2f} °C/C²h | "
            f"warmte stijging ontladen {thermisch['warmte_stijging_ontladen_c_per_c2h']:.2f} °C/C²h | "
            f"temp limiet hoog/laag {thermisch['temp_limiet_c']:.1f}/{thermisch['temp_limiet_lage_soc_c']:.1f} °C | "
            f"100% SoC temp factor {thermisch['temp_penalty_100_soc_factor']:.2f} | "
            f"SoC verblijf hoog/laag {thermisch['hoge_soc_verblijf_penalty_factor']:.2f}/{thermisch['lage_soc_verblijf_penalty_factor']:.2f} | "
            f"forecast {thermisch['forecast_bron']}",
            level="INFO",
        )

        schema = los_dp_op(
            slots,
            accu,
            min_spread_ct_per_kwh=min_spread,
            plateau_spreiding=plateau_spreiding,
            warmte_penalty_laden_factor=warmte_penalty_laden_factor,
            warmte_penalty_ontladen_factor=warmte_penalty_ontladen_factor,
            standby_verbruik_w=standby_verbruik_w,
            minimum_vermogen_w=minimum_vermogen_w,
            batterij_temp_start_c=thermisch["batterij_temp_start_c"],
            warmte_afkoeling_halveringstijd_h=thermisch["warmte_afkoeling_halveringstijd_h"],
            warmte_stijging_laden_c_per_c2h=thermisch["warmte_stijging_laden_c_per_c2h"],
            warmte_stijging_ontladen_c_per_c2h=thermisch["warmte_stijging_ontladen_c_per_c2h"],
            temp_limiet_c=thermisch["temp_limiet_c"],
            temp_limiet_lage_soc_c=thermisch["temp_limiet_lage_soc_c"],
            temp_penalty_factor=thermisch["temp_penalty_factor"],
            temp_penalty_100_soc_factor=thermisch["temp_penalty_100_soc_factor"],
            hoge_soc_verblijf_penalty_factor=thermisch["hoge_soc_verblijf_penalty_factor"],
            lage_soc_verblijf_penalty_factor=thermisch["lage_soc_verblijf_penalty_factor"],
            soc_min_pct=hw_min_pct,
            soc_max_pct=hw_max_pct,
            annuleer_check=is_geannuleerd,
        )
        stop_als_geannuleerd()
        economisch_schema = self._bereken_economisch_schema(
            slots,
            accu,
            standby_verbruik_w,
            minimum_vermogen_w,
            hw_min_pct,
            hw_max_pct,
            is_geannuleerd,
        )
        stop_als_geannuleerd()
        self._corrigeer_actief_slot_vermogen(schema, accu, hw_min_pct, hw_max_pct)
        self._corrigeer_actief_slot_vermogen(
            economisch_schema,
            accu,
            hw_min_pct,
            hw_max_pct,
            strategie_entity_id=ECONOMISCHE_STRATEGIE_ENTITY,
        )
        stop_als_geannuleerd()
        spread_blokkades = self._markeer_spread_blokkades(schema, accu.eta_laad, min_spread)
        stop_als_geannuleerd()

        # Vertaal DP-interne SoC% (0–100% van hw-venster) naar echte battery-%
        # zodat de grafiek overeenkomt met wat de Zendure rapporteert.
        hw_range = hw_max_pct - hw_min_pct
        for strategie_schema in (schema, economisch_schema):
            for s in strategie_schema:
                s["soc_voor_pct"] = round(
                    hw_min_pct + s["soc_voor_pct"] / 100.0 * hw_range,
                    1,
                )
                s["soc_na_pct"] = round(
                    hw_min_pct + s["soc_na_pct"] / 100.0 * hw_range,
                    1,
                )
                if s.get("stop_soc_pct") is not None:
                    try:
                        stop_soc_pct = float(s["stop_soc_pct"])
                    except (TypeError, ValueError):
                        s["stop_soc_pct"] = None
                    else:
                        s["stop_soc_pct"] = round(
                            hw_min_pct + stop_soc_pct / 100.0 * hw_range,
                            1,
                        )
        stop_als_geannuleerd()

        verwachte_winst = bereken_prijs_rte_winst_eur(schema)
        economische_verwachte_winst = bereken_prijs_rte_winst_eur(economisch_schema)
        winstverschil = economische_verwachte_winst - verwachte_winst
        penalty_totalen = bereken_penalty_totalen_eur(schema)
        economische_penalty_totalen = bereken_penalty_totalen_eur(economisch_schema)
        laad_slots      = [s for s in schema if s["actie"] == "laden"]
        ontlaad_slots   = [s for s in schema if s["actie"] == "ontladen"]
        economische_laad_slots = [
            s for s in economisch_schema if s["actie"] == "laden"
        ]
        economische_ontlaad_slots = [
            s for s in economisch_schema if s["actie"] == "ontladen"
        ]
        volgende        = next((s for s in schema if s["actie"] != "rust"), None)
        economische_volgende = next(
            (s for s in economisch_schema if s["actie"] != "rust"),
            None,
        )
        nu_publicatie   = datetime.now().astimezone()
        huidig = None
        for slot in schema:
            datetimes = _lees_slot_datetimes(slot)
            if datetimes is None:
                continue
            start, end = datetimes
            if start <= nu_publicatie < end:
                huidig = slot
                break
        economisch_huidig = None
        for slot in economisch_schema:
            datetimes = _lees_slot_datetimes(slot)
            if datetimes is None:
                continue
            start, end = datetimes
            if start <= nu_publicatie < end:
                economisch_huidig = slot
                break
        history_grafiek_slots: list[dict] = []
        try:
            strategie_history_items = self._haal_history_items(
                "sensor.dynamisch_handelsstrategie",
                1,
            )
            history_grafiek_slots = haal_grafiek_slots_uit_history_items(
                strategie_history_items,
                nu_publicatie,
                GRAFIEK_HISTORIE_UREN,
            )
        except Exception as exc:
            self.log(
                f"Dynamisch Handelen: kon grafiek-history niet lezen: {exc}",
                level="DEBUG",
            )
        vorige_grafiek_slots = (
            self.get_state("sensor.dynamisch_handelsstrategie", attribute="slots_grafiek")
            or self.get_state("sensor.dynamisch_handelsstrategie", attribute="slots")
            or []
        )
        if not isinstance(vorige_grafiek_slots, list):
            vorige_grafiek_slots = []
        grafiek_slots = bouw_grafiek_slots(
            history_grafiek_slots + vorige_grafiek_slots,
            schema,
            nu_publicatie,
            GRAFIEK_HISTORIE_UREN,
        )
        grafiek_start = grafiek_slots[0]["start"] if grafiek_slots else None
        strategie_einde = schema[-1]["end"] if schema else None

        self.log(
            f"Dynamisch Handelen: verwacht gekozen EUR {verwachte_winst:.3f} | "
            f"economisch EUR {economische_verwachte_winst:.3f} | "
            f"verschil EUR {winstverschil:.3f} | "
            f"{len(laad_slots)} laadslots / {len(ontlaad_slots)} ontlaadslots | "
            f"volgende: {volgende['actie'] if volgende else 'rust'} "
            f"om {volgende['start'] if volgende else '-'}",
            level="INFO",
        )

        self.set_state(
            ECONOMISCHE_STRATEGIE_ENTITY,
            state=str(round(economische_verwachte_winst, 3)),
            attributes={
                "unit_of_measurement": "EUR",
                "friendly_name": "Economisch Optimale Strategie Verwachte Winst",
                "icon": "mdi:cash-fast",
                "device_class": "monetary",
                "strategie_type": "economisch_optimaal",
                "optimalisatie": "prijs_en_rte",
                "penalties_actief": False,
                "verwachte_winst_berekening": "prijs_en_rte",
                "verwachte_winst_eur": round(economische_verwachte_winst, 4),
                "gekozen_verwachte_winst_eur": round(verwachte_winst, 4),
                "winstverschil_eur": round(winstverschil, 4),
                **formatteer_penalty_attributen(economische_penalty_totalen),
                "gekozen_strategie_entity": "sensor.dynamisch_handelsstrategie",
                "slots": economisch_schema,
                "strategie_einde": strategie_einde,
                "prijs_bron": prijs_bron,
                "planning_resolutie": "volledige horizon kwartierprijzen",
                "laad_slots": len(economische_laad_slots),
                "ontlaad_slots": len(economische_ontlaad_slots),
                "huidige_actie": (
                    economisch_huidig["actie"] if economisch_huidig else "rust"
                ),
                "volgende_actie": (
                    economische_volgende["actie"] if economische_volgende else "rust"
                ),
                "volgende_start": (
                    economische_volgende["start"] if economische_volgende else None
                ),
                "min_spread_ct": 0.0,
                "warmte_penalty_laden_factor": 0.0,
                "warmte_penalty_ontladen_factor": 0.0,
                "temp_penalty_factor": 0.0,
                "hoge_soc_verblijf_penalty_factor": 0.0,
                "lage_soc_verblijf_penalty_factor": 0.0,
                "plateau_spreiding": False,
                "standby_verbruik_w": standby_verbruik_w,
                "minimum_vermogen_w": minimum_vermogen_w,
                "dp_vermogen_stap_w": DP_VERMOGEN_STAP_W,
                "accu_huidig_kwh": round(accu.huidig_kwh, 3),
                "accu_max_kwh": round(accu.max_kwh, 3),
                "eta": round(accu.eta_laad, 3),
                "bijgewerkt": nu_publicatie.isoformat(),
            },
        )

        self.set_state(
            "sensor.dynamisch_handelsstrategie",
            state=str(round(verwachte_winst, 3)),
            attributes={
                "unit_of_measurement": "EUR",
                "friendly_name":       "Gekozen Strategie Verwachte Winst",
                "icon":                "mdi:cash-plus",
                "device_class":        "monetary",
                "strategie_type":       "gekozen_met_penalties",
                "optimalisatie":        "prijs_rte_en_penalties",
                "penalties_actief":     True,
                "verwachte_winst_berekening": "prijs_en_rte",
                "verwachte_winst_eur": round(verwachte_winst, 4),
                "economische_verwachte_winst_eur": round(
                    economische_verwachte_winst,
                    4,
                ),
                "winstverschil_eur": round(winstverschil, 4),
                **formatteer_penalty_attributen(penalty_totalen),
                "economische_strategie_entity": ECONOMISCHE_STRATEGIE_ENTITY,
                "slots":               schema,
                "slots_grafiek":       grafiek_slots,
                "grafiek_historie_uren": GRAFIEK_HISTORIE_UREN,
                "grafiek_start":       grafiek_start,
                "strategie_einde":     strategie_einde,
                "prijs_bron":           prijs_bron,
                "planning_resolutie":   "echte kwartierprijzen + gekalibreerde WattWanneer-uurprijzen + 24-uursgemiddelde fallback",
                "planning_horizon_h":   round(planning_horizon_h, 2),
                "kwartierprijs_slots":  len(kwartierprijs_slots),
                "uurprijs_slots":       len(forecast_prijs_slots) + len(fallback_prijs_slots),
                "forecast_prijsslots":  len(forecast_prijs_slots),
                "fallback_prijsslots":  len(fallback_prijs_slots),
                "fallback_prijs_ct":    fallback_prijs_ct,
                "fallback_prijs_basis_slots": fallback_prijs_basis_slots,
                "wattwanneer_status": getattr(
                    self, "_laatste_wattwanneer_metadata", {}
                ).get("status"),
                "wattwanneer_generated_at": getattr(
                    self, "_laatste_wattwanneer_metadata", {}
                ).get("generated_at"),
                "wattwanneer_laatste_poging": getattr(
                    self, "_laatste_wattwanneer_metadata", {}
                ).get("laatste_poging"),
                "wattwanneer_laatste_succes": getattr(
                    self, "_laatste_wattwanneer_metadata", {}
                ).get("laatste_succes"),
                "wattwanneer_volgende_poging": getattr(
                    self, "_laatste_wattwanneer_metadata", {}
                ).get("volgende_poging"),
                "wattwanneer_fout": getattr(
                    self, "_laatste_wattwanneer_metadata", {}
                ).get("fout"),
                "wattwanneer_forecast_fetch_id": getattr(
                    self, "_laatste_wattwanneer_metadata", {}
                ).get("forecast_fetch_id"),
                "wattwanneer_kalibratie_history_id": getattr(
                    self, "_laatste_wattwanneer_metadata", {}
                ).get("kalibratie_history_id"),
                "prijshistorie_fout": getattr(
                    self, "_laatste_wattwanneer_metadata", {}
                ).get("historie_fout"),
                "wattwanneer_kalibratie_factor": getattr(
                    self, "_laatste_wattwanneer_metadata", {}
                ).get("kalibratie_factor"),
                "wattwanneer_kalibratie_opslag_eur_kwh": getattr(
                    self, "_laatste_wattwanneer_metadata", {}
                ).get("kalibratie_opslag_eur_kwh"),
                # Bestaande attribuutnamen blijven beschikbaar voor dashboards en automations.
                "fijnmazige_horizon_h": round(kwartier_horizon_h, 2),
                "fijnmazige_slot_minuten": KWARTIER_SLOT_MINUTEN,
                "fijnmazige_slots":     len(kwartierprijs_slots),
                "bron_slots":           len(fallback_prijs_slots),
                "laad_slots":          len(laad_slots),
                "ontlaad_slots":       len(ontlaad_slots),
                "spread_blokkades":    spread_blokkades,
                "spread_blokkades_aantal": len(spread_blokkades),
                "huidige_actie":       huidig["actie"] if huidig else "rust",
                "huidige_stop_soc_pct": huidig.get("stop_soc_pct") if huidig else None,
                "huidige_stop_soc_richting": huidig.get("stop_soc_richting") if huidig else "geen",
                "volgende_actie":      volgende["actie"] if volgende else "rust",
                "volgende_start":      volgende["start"] if volgende else None,
                "accu_huidig_kwh":     round(accu.huidig_kwh, 3),
                "accu_max_kwh":        round(accu.max_kwh,    3),
                "max_laad_w":          round(accu.max_laad_w),
                "max_ontlaad_w":       round(accu.max_ontlaad_w),
                "dp_start":            dp_start_tijd.isoformat() if dp_start_tijd else None,
                "accu_bronnen":        accu_bronnen,
                "eta":                 round(accu.eta_laad,   3),
                "min_spread_ct":       min_spread,
                "warmte_penalty_laden_factor": warmte_penalty_laden_factor,
                "warmte_penalty_ontladen_factor": warmte_penalty_ontladen_factor,
                "standby_verbruik_w": standby_verbruik_w,
                "minimum_vermogen_w": minimum_vermogen_w,
                "dp_vermogen_stap_w": DP_VERMOGEN_STAP_W,
                "aansturing_vermogen_stap_w": VERMOGEN_STAP_W,
                "batterij_temp_start_c": thermisch["batterij_temp_start_c"],
                "batterij_temp_bron": thermisch["batterij_temp_bron"],
                "buiten_temp_huidig_c": thermisch["buiten_temp_huidig_c"],
                "buiten_temp_bron": thermisch["buiten_temp_bron"],
                "weather_entity": thermisch["weather_entity"],
                "forecast_bron": thermisch["forecast_bron"],
                "forecast_punten": thermisch["forecast_punten"],
                "warmte_afkoeling_halveringstijd_h": thermisch["warmte_afkoeling_halveringstijd_h"],
                "warmte_stijging_laden_c_per_c2h": thermisch["warmte_stijging_laden_c_per_c2h"],
                "warmte_stijging_ontladen_c_per_c2h": thermisch["warmte_stijging_ontladen_c_per_c2h"],
                # Backwards-compatible attribuut voor bestaande dashboards of automations.
                "warmte_stijging_c_per_c2h": thermisch["warmte_stijging_laden_c_per_c2h"],
                "temp_limiet_c": thermisch["temp_limiet_c"],
                "temp_limiet_lage_soc_c": thermisch["temp_limiet_lage_soc_c"],
                "temp_penalty_factor": thermisch["temp_penalty_factor"],
                "temp_penalty_100_soc_factor": thermisch["temp_penalty_100_soc_factor"],
                "hoge_soc_verblijf_penalty_factor": thermisch["hoge_soc_verblijf_penalty_factor"],
                "lage_soc_verblijf_penalty_factor": thermisch["lage_soc_verblijf_penalty_factor"],
                "temp_soc_drempel_pct": 80.0,
                "plateau_spreiding":   plateau_spreiding,
                "bijgewerkt":          nu_publicatie.isoformat(),
            },
        )

    def _bereken_economisch_schema(
        self,
        slots: list[dict],
        accu: "Accustatus",
        standby_verbruik_w: float,
        minimum_vermogen_w: int,
        hw_min_pct: float,
        hw_max_pct: float,
        annuleer_check,
    ) -> list[dict]:
        """
        Optimaliseert alleen op marktprijs en RTE binnen de fysieke accugrenzen.

        Minimale spread, thermische penalties, SoC-verblijfskosten en
        plateau-spreiding staan uit. Standbyverlies, vermogensgrenzen, derating
        en het beschikbare SoC-venster blijven fysieke modelvoorwaarden.
        """
        return los_dp_op(
            slots,
            accu,
            min_spread_ct_per_kwh=0.0,
            plateau_spreiding=False,
            warmte_penalty_laden_factor=0.0,
            warmte_penalty_ontladen_factor=0.0,
            standby_verbruik_w=standby_verbruik_w,
            minimum_vermogen_w=minimum_vermogen_w,
            batterij_temp_start_c=None,
            temp_penalty_factor=0.0,
            temp_penalty_100_soc_factor=1.0,
            hoge_soc_verblijf_penalty_factor=0.0,
            lage_soc_verblijf_penalty_factor=0.0,
            soc_min_pct=hw_min_pct,
            soc_max_pct=hw_max_pct,
            annuleer_check=annuleer_check,
        )

    # ── DATA OPHALEN ─────────────────────────────────────────────────────────

    def _huidige_planning_tijd(self) -> datetime:
        """Geeft de huidige tijd expliciet in Europe/Amsterdam terug."""
        return datetime.now().astimezone(PLANNING_TIJDZONE)

    def _haal_prijsslots(self) -> list[dict]:
        """
        Bouwt een prijsplanning van exact 72 verstreken uren.

        `input_text.dynamisch_nordpool_sensor` bevat de entity_id van de HACS
        Nordpool-sensor. We lezen `raw_today` en `raw_tomorrow` van die bron,
        zodat `input_boolean.dynamisch_15_minuten` geen kwartierprijzen meer tot
        uurprijzen kan middelen voordat de DP ze ontvangt.

        Alleen bron-slots van exact 15 minuten worden geaccepteerd. Ieder
        beschikbaar toekomstig kwartier uit `raw_today` en `raw_tomorrow` blijft
        een afzonderlijk DP-slot. Na het laatste geldige bronkwartier gebruikt de
        planning gekalibreerde WattWanneer-uurprijzen. Ontbrekende perioden worden
        aangevuld met slots van maximaal één uur. De fallbackprijs is het
        rekenkundig gemiddelde van maximaal de laatste 96 geldige bronkwartieren
        (24 uur prijsdata).

        WattWanneer wordt na succes maximaal eens per 12 uur opgehaald. Na een
        mislukte poging volgt maximaal eens per 2 uur een nieuwe poging. Beide
        tijdstippen staan in `/share/zendure_kwartieren.sqlite`, zodat Home
        Assistant- en AppDaemon-herstarts de limieten niet omzeilen.
        """
        bron_entity = str(
            self.get_state("input_text.dynamisch_nordpool_sensor") or ""
        ).strip()
        if not bron_entity.startswith("sensor."):
            self.log(
                "Dynamisch Handelen: input_text.dynamisch_nordpool_sensor bevat geen geldige sensor entity_id",
                level="ERROR",
            )
            return []

        attr         = self.get_state(bron_entity, attribute="all") or {}
        attributes   = attr.get("attributes", {})
        raw_today    = attributes.get("raw_today")    or []
        raw_tomorrow = attributes.get("raw_tomorrow") or []

        nu = self._huidige_planning_tijd()
        geldige_bron_slots: dict[tuple[datetime, datetime], dict] = {}

        for source_series, reeks in (
            ("raw_today", raw_today),
            ("raw_tomorrow", raw_tomorrow),
        ):
            for item in reeks:
                try:
                    start = datetime.fromisoformat(str(item["start"])).astimezone(
                        PLANNING_TIJDZONE
                    )
                    end = datetime.fromisoformat(str(item["end"])).astimezone(
                        PLANNING_TIJDZONE
                    )
                    price = float(item["value"])
                except (KeyError, ValueError, TypeError) as exc:
                    self.log(
                        f"Dynamisch Handelen: ongeldig prijsslot overgeslagen: {exc}",
                        level="WARNING",
                    )
                    continue

                duur_seconden = (end - start).total_seconds()
                if not math.isclose(
                    duur_seconden,
                    KWARTIER_SLOT_MINUTEN * 60,
                    abs_tol=1.0,
                ):
                    self.log(
                        f"Dynamisch Handelen: prijsslot van {duur_seconden / 60:.1f} minuten "
                        f"uit {bron_entity} overgeslagen; 15 minuten vereist",
                        level="WARNING",
                    )
                    continue

                geldige_bron_slots[(start, end)] = {
                    "start":      start,
                    "end":        end,
                    "price":      price,
                    "duration_h": duur_seconden / 3600.0,
                    "resolutie":  "kwartierprijs",
                    "prijs_bron": bron_entity,
                    "prijs_is_forecast": False,
                    "prijs_is_fallback": False,
                    "source_series": source_series,
                }

        bron_slots = sorted(
            geldige_bron_slots.values(),
            key=lambda slot: slot["start"],
        )

        historie_metadata: dict = {}
        historie_fouten: list[str] = []
        forecast_cache = self._get_wattwanneer_cache()
        try:
            historie_metadata.update(
                forecast_cache.bewaar_nordpool_prijzen(
                    price_entity=bron_entity,
                    slots=bron_slots,
                    observed_at_epoch=int(nu.timestamp()),
                )
            )
        except Exception as exc:
            historie_fouten.append(
                f"Nordpool-kwartierprijzen konden niet in SQLite worden opgeslagen: {exc}"
            )
            self.log(
                f"Dynamisch Handelen: {historie_fouten[-1]}",
                level="ERROR",
            )

        historie_fout = "; ".join(historie_fouten) or None

        forecast_infrastructuur_fout = None
        try:
            forecast_resultaat = forecast_cache.haal(
                now_epoch=int(nu.timestamp())
            )
        except Exception as exc:
            forecast_resultaat = None
            forecast_infrastructuur_fout = (
                f"SQLite-cache of downloader gaf een fout: {exc}"
            )
            self._publiceer_wattwanneer_status(
                None,
                extra_fout=forecast_infrastructuur_fout,
                historie_fout=historie_fout,
                historie_metadata=historie_metadata,
            )
        else:
            self._publiceer_wattwanneer_status(
                forecast_resultaat,
                historie_fout=historie_fout,
                historie_metadata=historie_metadata,
            )
            if forecast_resultaat.poging_uitgevoerd:
                if forecast_resultaat.laatste_status == "success":
                    self.log(
                        "Dynamisch Handelen: WattWanneer-forecast opgehaald en in SQLite opgeslagen",
                        level="INFO",
                    )
                else:
                    self.log(
                        "Dynamisch Handelen: WattWanneer ophalen mislukt; "
                        f"volgende poging pas na 2 uur: {forecast_resultaat.fout}",
                        level="ERROR",
                    )

        if not bron_slots:
            self._publiceer_wattwanneer_status(
                forecast_resultaat,
                extra_fout="geen geldige Nordpool-kwartieren voor prijsbasiskalibratie",
                historie_fout=historie_fout,
                historie_metadata=historie_metadata,
            )
            return []

        fallback_basis_aantal = int(
            FALLBACK_PRIJS_BASIS_UREN * 60 / KWARTIER_SLOT_MINUTEN
        )
        fallback_basis = bron_slots[-fallback_basis_aantal:]
        fallback_prijs = (
            sum(float(slot["price"]) for slot in fallback_basis)
            / len(fallback_basis)
        )

        horizon_start = nu.replace(
            minute=(nu.minute // KWARTIER_SLOT_MINUTEN) * KWARTIER_SLOT_MINUTEN,
            second=0,
            microsecond=0,
        )
        horizon_einde = (
            horizon_start.astimezone(timezone.utc)
            + timedelta(hours=PLANNING_HORIZON_UREN)
        ).astimezone(PLANNING_TIJDZONE)
        bekende_reeks_einde = max(slot["end"] for slot in bron_slots)

        forecast_slots: list[dict] = []
        kalibratie = None
        forecast_fout = forecast_infrastructuur_fout
        if forecast_resultaat and forecast_resultaat.records:
            ruwe_forecast_slots = bouw_wattwanneer_slots(forecast_resultaat.records)
            try:
                kalibratie = kalibreer_wattwanneer_prijzen(
                    ruwe_forecast_slots,
                    bron_slots,
                )
            except ValueError as exc:
                forecast_fout = f"WattWanneer-prijsbasiskalibratie mislukt: {exc}"
            else:
                forecast_slots = kalibratie["slots"]
                try:
                    historie_metadata["kalibratie_history_id"] = (
                        forecast_cache.bewaar_prijskalibratie(
                            calculated_at_epoch=int(nu.timestamp()),
                            forecast_fetch_id=(
                                forecast_resultaat.payload_fetch_id
                                if forecast_resultaat
                                else None
                            ),
                            price_entity=bron_entity,
                            overlap_hours=int(kalibratie["meetpunten"]),
                            factor=float(kalibratie["factor"]),
                            offset_eur_kwh=float(kalibratie["opslag_eur_kwh"]),
                            max_residual_eur_kwh=float(
                                kalibratie["max_restfout_eur_kwh"]
                            ),
                        )
                    )
                except Exception as exc:
                    historie_fouten.append(
                        "WattWanneer-prijskalibratie kon niet in SQLite worden "
                        f"opgeslagen: {exc}"
                    )
                    historie_fout = "; ".join(historie_fouten)
                    self.log(
                        f"Dynamisch Handelen: {historie_fouten[-1]}",
                        level="ERROR",
                    )
                for slot in forecast_slots:
                    slot["kalibratie_bron"] = bron_entity
                    slot["prijs_bron"] = f"WattWanneer gekalibreerd op {bron_entity}"
                if not forecast_slots or forecast_slots[-1]["end"] < horizon_einde:
                    forecast_fout = (
                        "WattWanneer-forecast dekt de 72-uursplanning niet volledig"
                    )

        self._publiceer_wattwanneer_status(
            forecast_resultaat,
            extra_fout=forecast_fout,
            kalibratie=kalibratie,
            historie_fout=historie_fout,
            historie_metadata=historie_metadata,
        )

        echte_slots_op_start = {
            slot["start"]: slot
            for slot in bron_slots
            if slot["end"] > horizon_start
            and slot["start"] >= horizon_start
            and slot["end"] <= horizon_einde
        }
        echte_starttijden = sorted(echte_slots_op_start)
        forecast_slots_op_start = {
            slot["start"]: slot
            for slot in forecast_slots
            if slot["start"] >= bekende_reeks_einde
            and slot["end"] > horizon_start
            and slot["start"] < horizon_einde
        }
        forecast_starttijden = sorted(forecast_slots_op_start)

        slots: list[dict] = []
        cursor = horizon_start
        while cursor < horizon_einde:
            echt_slot = echte_slots_op_start.get(cursor)
            if echt_slot is not None:
                slots.append(dict(echt_slot))
                cursor = echt_slot["end"]
                continue

            forecast_slot = forecast_slots_op_start.get(cursor)
            if forecast_slot is not None:
                forecast_einde = min(forecast_slot["end"], horizon_einde)
                nieuw_slot = dict(forecast_slot)
                nieuw_slot["end"] = forecast_einde
                nieuw_slot["duration_h"] = (
                    forecast_einde.astimezone(timezone.utc)
                    - cursor.astimezone(timezone.utc)
                ).total_seconds() / 3600.0
                slots.append(nieuw_slot)
                cursor = forecast_einde
                continue

            volgende_start = min(
                next(
                    (start for start in echte_starttijden if start > cursor),
                    horizon_einde,
                ),
                next(
                    (start for start in forecast_starttijden if start > cursor),
                    horizon_einde,
                ),
            )
            fallback_einde = min(
                (
                    cursor.astimezone(timezone.utc)
                    + timedelta(hours=FALLBACK_SLOT_UREN)
                ).astimezone(PLANNING_TIJDZONE),
                volgende_start,
                horizon_einde,
            )
            duur_h = (
                fallback_einde.astimezone(timezone.utc)
                - cursor.astimezone(timezone.utc)
            ).total_seconds() / 3600.0
            if duur_h <= 0:
                self.log(
                    "Dynamisch Handelen: prijsplanning kon niet voorbij "
                    f"{cursor.isoformat()} worden opgebouwd",
                    level="ERROR",
                )
                return []

            slots.append({
                "start": cursor,
                "end": fallback_einde,
                "price": fallback_prijs,
                "duration_h": duur_h,
                "resolutie": "uurprijs_fallback_24h_gemiddelde",
                "prijs_bron": bron_entity,
                "prijs_is_forecast": False,
                "prijs_is_fallback": True,
                "fallback_prijs_basis_slots": len(fallback_basis),
            })
            cursor = fallback_einde

        return slots

    def _bepaal_dp_start_tijd(self, slots: list[dict]) -> datetime | None:
        """Gebruikt het begin van het lopende prijsslot als starttijd voor de DP."""
        nu = datetime.now().astimezone()
        for slot in slots:
            try:
                start = datetime.fromisoformat(str(slot["start"])).astimezone()
                end = datetime.fromisoformat(str(slot["end"])).astimezone()
            except (KeyError, ValueError, TypeError):
                continue
            if start <= nu < end:
                return start
        return None

    # ── SPREAD-UITLEG ────────────────────────────────────────────────────────

    def _markeer_spread_blokkades(
        self,
        schema: list[dict],
        eta: float,
        min_spread_ct: float,
    ) -> list[dict]:
        """
        Zet uitleg op rust-slots waar de ruwe spread wel zichtbaar is maar
        min_spread_ct na rendement niet wordt gehaald.

        Voor elk rust-slot zoekt de functie de hoogste toekomstige prijs in het
        schema. Als beste_verkoop_ct hoger is dan prijs_ct maar lager dan de
        benodigde verkoopprijs, krijgt het slot velden voor dashboard en debug.
        """
        blokkades: list[dict] = []
        if eta <= 0:
            return blokkades

        spread_helft = min_spread_ct / 2.0
        eta_kwadraat = eta * eta

        for i, slot in enumerate(schema):
            prijs_ct = slot.get("prijs_ct")
            if slot.get("actie") != "rust" or prijs_ct is None:
                continue

            toekomstige_prijzen = [
                s.get("prijs_ct")
                for s in schema[i + 1:]
                if isinstance(s.get("prijs_ct"), (int, float))
            ]
            if not toekomstige_prijzen:
                continue

            beste_verkoop_ct = max(toekomstige_prijzen)
            bruto_spread_ct = beste_verkoop_ct - prijs_ct
            benodigde_verkoop_ct = ((prijs_ct + spread_helft) / eta_kwadraat) + spread_helft

            if bruto_spread_ct < min_spread_ct or beste_verkoop_ct >= benodigde_verkoop_ct:
                continue

            blokkade = {
                "start": slot.get("start"),
                "end": slot.get("end"),
                "prijs_ct": round(prijs_ct, 3),
                "beste_verkoop_ct": round(beste_verkoop_ct, 3),
                "benodigde_verkoop_ct": round(benodigde_verkoop_ct, 3),
                "bruto_spread_ct": round(bruto_spread_ct, 3),
                "spread_tekort_ct": round(benodigde_verkoop_ct - beste_verkoop_ct, 3),
                "min_spread_ct": round(min_spread_ct, 3),
                "reden": "spread_te_klein",
            }

            slot["geen_laden_reden"] = "spread_te_klein"
            slot["beste_verkoop_ct"] = blokkade["beste_verkoop_ct"]
            slot["benodigde_verkoop_ct"] = blokkade["benodigde_verkoop_ct"]
            slot["bruto_spread_ct"] = blokkade["bruto_spread_ct"]
            slot["spread_tekort_ct"] = blokkade["spread_tekort_ct"]
            slot["min_spread_ct"] = blokkade["min_spread_ct"]
            blokkades.append(blokkade)

        return blokkades

    # ── ACTIEVE-SLOT-CORRECTIE ───────────────────────────────────────────────

    def _corrigeer_actief_slot_vermogen(
        self,
        schema: list[dict],
        accu: "Accustatus",
        hw_min_pct: float,
        hw_max_pct: float,
        strategie_entity_id: str = "sensor.dynamisch_handelsstrategie",
    ) -> None:
        """
        Corrigeert alleen het lopende slot op basis van de actuele Zendure-SoC.

        los_dp_op() maakt een target voor het einde van elk prijsslot. Binnen het
        huidige slot gebruikt deze correctie alleen dat DP-target als opdracht.
        Deze functie haalt geen energie uit latere slots naar voren, omdat de DP
        dan thermische penalties en SoC-verblijfskosten niet opnieuw evalueert.
        """
        nu = datetime.now().astimezone()
        actief = None
        actief_index = None
        for index, slot in enumerate(schema):
            try:
                start = datetime.fromisoformat(str(slot["start"])).astimezone()
                end = datetime.fromisoformat(str(slot["end"])).astimezone()
            except (KeyError, ValueError, TypeError):
                continue
            if start <= nu < end:
                actief = slot
                actief_index = index
                break

        if actief is None or actief.get("actie") not in ("laden", "ontladen"):
            return

        raw_actuele_soc_kwh = self._haal_actuele_soc_kwh_via_laadpercentage(
            accu.max_kwh,
            hw_min_pct,
            hw_max_pct,
        )
        if raw_actuele_soc_kwh is None:
            return

        actie = actief["actie"]
        geplande_vermogen_w = self._begrens_gepland_vermogen(
            actief.get("vermogen_w"),
            accu.max_laad_w if actie == "laden" else accu.max_ontlaad_w,
        )
        volgende_actie = (
            schema[actief_index + 1].get("actie")
            if actief_index is not None and actief_index + 1 < len(schema)
            else "rust"
        )
        try:
            target_kwh = self._begrens_kwh_naar_accu(float(actief["soc_na_kwh"]), accu.max_kwh)
            begin_kwh = float(actief.get("soc_voor_kwh", raw_actuele_soc_kwh))
        except (KeyError, TypeError, ValueError):
            return

        try:
            eindtijd = datetime.fromisoformat(str(actief["end"])).astimezone()
        except (KeyError, ValueError, TypeError):
            return

        resterend_h = max(0.0, (eindtijd - nu).total_seconds() / 3600.0)
        if resterend_h <= 0.0:
            return

        actuele_soc_kwh, soc_bron = self._schat_actuele_soc_kwh(
            actief,
            actie,
            raw_actuele_soc_kwh,
            accu,
            nu,
            strategie_entity_id,
        )

        if actie == "laden":
            delta_kwh = max(0.0, target_kwh - actuele_soc_kwh)
            energie_net_kwh = delta_kwh / accu.eta_laad if accu.eta_laad > 0 else 0.0
            verwacht_vermogen_w = min(
                accu.max_laad_w,
                energie_net_kwh / resterend_h * 1000.0 if resterend_h > 0 else 0.0,
            )
            derating = bereken_derating(actuele_soc_kwh, accu.max_kwh)
            vermogen_w = bereken_laadvermogen_voor_aansturing(
                verwacht_vermogen_w,
                accu.max_laad_w,
                derating,
            )
        else:
            target_kwh = self._begrens_kwh_naar_accu(target_kwh, accu.max_kwh)
            delta_kwh = max(0.0, actuele_soc_kwh - target_kwh)
            energie_net_kwh = delta_kwh * accu.eta_ontlaad
            verwacht_vermogen_w = min(
                accu.max_ontlaad_w,
                energie_net_kwh / resterend_h * 1000.0 if resterend_h > 0 else 0.0,
            )
            vermogen_w = rond_vermogen_omhoog(verwacht_vermogen_w, accu.max_ontlaad_w)

        doel_bereikt = vermogen_w <= 0
        doorlopen_zelfde_actie = False
        if doel_bereikt and volgende_actie == actie and geplande_vermogen_w > 0:
            vermogen_w = geplande_vermogen_w
            verwacht_vermogen_w = geplande_vermogen_w
            doel_bereikt = False
            doorlopen_zelfde_actie = True

        actief["actie"] = "rust" if doel_bereikt else actie
        actief["geplande_actie"] = actie
        actief["vermogen_w"] = vermogen_w
        actief["verwacht_vermogen_w"] = rond_vermogen_omhoog(
            verwacht_vermogen_w,
            accu.max_laad_w * derating if actie == "laden" else accu.max_ontlaad_w,
        )
        actief["actief_slot_begin_kwh"] = round(begin_kwh, 3)
        actief["actief_slot_raw_soc_kwh"] = round(raw_actuele_soc_kwh, 3)
        actief["actuele_soc_kwh"] = round(actuele_soc_kwh, 3)
        actief["actuele_soc_bron"] = soc_bron
        actief["actief_slot_doel_kwh"] = round(target_kwh, 3)
        actief["doel_soc_kwh"] = round(target_kwh, 3)
        actief["soc_na_kwh"] = round(target_kwh, 3)
        actief["soc_na_pct"] = round(target_kwh / accu.max_kwh * 100, 1) if accu.max_kwh > 0 else 0.0
        actief["actief_slot_delta_kwh"] = round(
            target_kwh - actuele_soc_kwh if actie == "laden" else actuele_soc_kwh - target_kwh,
            3,
        )
        actief["actief_slot_resterend_h"] = round(resterend_h, 3)
        actief["doel_bereikt"] = doel_bereikt
        actief["actief_slot_doorlopen_zelfde_actie"] = doorlopen_zelfde_actie
        if doel_bereikt:
            actief["stop_soc_kwh"] = None
            actief["stop_soc_pct"] = None
            actief["stop_soc_richting"] = "geen"
        else:
            if volgende_actie == actie:
                actief["stop_soc_kwh"] = None
                actief["stop_soc_pct"] = None
                actief["stop_soc_richting"] = "geen"
            else:
                actief["stop_soc_kwh"] = round(target_kwh, 3)
                actief["stop_soc_pct"] = round(target_kwh / accu.max_kwh * 100, 1) if accu.max_kwh > 0 else 0.0
                actief["stop_soc_richting"] = (
                    "boven_of_gelijk" if actie == "laden" else "onder_of_gelijk"
                )
        if doel_bereikt:
            actief["actief_slot_stopreden"] = "doel_bereikt"

    def _prijs_ct(self, slot: dict) -> float | None:
        """Leest prijs_ct als getal uit een schema-slot."""
        try:
            return float(slot["prijs_ct"])
        except (KeyError, TypeError, ValueError):
            return None

    def _begrens_kwh_naar_accu(self, waarde_kwh: float, max_kwh: float) -> float:
        """Begrenst een DP-interne kWh-waarde op de huidige accugrootte."""
        return min(max(0.0, waarde_kwh), max(0.0, max_kwh))

    def _begrens_gepland_vermogen(self, waarde_w, maximum_w: float) -> int:
        """Leest een gepland vermogen en begrenst het op de huidige hardwarelimiet."""
        try:
            vermogen_w = float(waarde_w)
        except (TypeError, ValueError):
            return 0

        return round(min(max(0.0, vermogen_w), max(0.0, maximum_w)))

    def _schat_actuele_soc_kwh(
        self,
        actief: dict,
        actie: str,
        raw_soc_kwh: float,
        accu: "Accustatus",
        nu: datetime,
        strategie_entity_id: str = "sensor.dynamisch_handelsstrategie",
    ) -> tuple[float, str]:
        """
        Schat de actuele SoC binnen hetzelfde actieve slot wanneer electricLevel
        nog hetzelfde hele percentage meldt.
        """
        start = str(actief["start"])
        end = str(actief["end"])
        vorige_slots = self.get_state(strategie_entity_id, attribute="slots") or []
        vorige_slot = None
        for slot in vorige_slots:
            if str(slot.get("start")) == start and str(slot.get("end")) == end:
                vorige_slot = slot
                break

        if vorige_slot is None:
            return raw_soc_kwh, "laadpercentage"

        vorige_actie = vorige_slot.get("geplande_actie") or vorige_slot.get("actie")
        if vorige_actie != actie:
            return raw_soc_kwh, "laadpercentage"

        try:
            vorige_raw_soc_kwh = float(vorige_slot["actief_slot_raw_soc_kwh"])
            vorige_soc_kwh = float(vorige_slot["actuele_soc_kwh"])
        except (KeyError, TypeError, ValueError):
            return raw_soc_kwh, "laadpercentage"

        if abs(raw_soc_kwh - vorige_raw_soc_kwh) > 0.001:
            return raw_soc_kwh, "laadpercentage"

        bijgewerkt = self.get_state("sensor.dynamisch_handelsstrategie", attribute="bijgewerkt")
        try:
            vorige_tijd = datetime.fromisoformat(str(bijgewerkt)).astimezone()
        except (TypeError, ValueError):
            return raw_soc_kwh, "laadpercentage"

        verstreken_h = max(0.0, (nu - vorige_tijd).total_seconds() / 3600.0)
        if verstreken_h <= 0.0 or verstreken_h > 5.0 / 60.0:
            return raw_soc_kwh, "laadpercentage"

        try:
            vermogen_w = float(self.get_state("sensor.zendure_2400_ac_vermogen_aansturing") or 0)
        except (TypeError, ValueError):
            return raw_soc_kwh, "laadpercentage"

        geschat_soc_kwh = vorige_soc_kwh
        if actie == "laden" and vermogen_w > 0 and accu.eta_laad > 0:
            geschat_soc_kwh += vermogen_w / 1000.0 * verstreken_h * accu.eta_laad
        elif actie == "ontladen" and vermogen_w < 0 and accu.eta_ontlaad > 0:
            geschat_soc_kwh -= abs(vermogen_w) / 1000.0 * verstreken_h / accu.eta_ontlaad
        else:
            return raw_soc_kwh, "laadpercentage"

        return min(accu.max_kwh, max(0.0, geschat_soc_kwh)), "vermogen_aansturing"

    def _haal_actuele_soc_kwh_via_laadpercentage(
        self,
        max_kwh: float,
        hw_min_pct: float,
        hw_max_pct: float,
    ) -> float | None:
        """
        Converteert sensor.zendure_2400_ac_laadpercentage naar DP-interne kWh.

        De DP-interne schaal loopt van 0 kWh bij hw_min_pct naar max_kwh bij
        hw_max_pct. De echte Zendure-SoC wordt daarom eerst naar dat venster
        omgerekend en daarna begrensd op 0..max_kwh.
        """
        hw_range = hw_max_pct - hw_min_pct
        if max_kwh <= 0 or hw_range <= 0:
            return None

        try:
            soc_pct = float(self.get_state("sensor.zendure_2400_ac_laadpercentage"))
        except (TypeError, ValueError):
            return None

        venster_pct = (soc_pct - hw_min_pct) / hw_range
        return min(max_kwh, max(0.0, venster_pct * max_kwh))

    def _haal_accustatus(self, dp_start_tijd: datetime | None = None) -> tuple["Accustatus", float, float, dict]:
        """
        Leest de actuele batterijstatus uit HA en converteert naar interne eenheden.

        DE TWEE ENERGIE-SENSOREN
        ------------------------
        beschikbare_energie (kWh): energie leverbaar naar net vanuit huidige SoC.
            Berekend als: (soc - hw_min)% × totale_cap × η_ontlaad

        benodigde_energie (kWh): energie nodig van net om accu naar hw_max te laden.
            Berekend als: (hw_max - soc)% × totale_cap / η_laad

        Beide zijn neteenheden (al gecorrigeerd voor η). Voor het DP-algoritme
        converteren we naar opgeslagen kWh (batterij-intern):

            stored_current = beschikbare / η     (verwijder de ontlaadkorting)
            stored_ruimte  = benodigde × η       (verwijder de laadtoeslag)

        RTE: η_laad = η_ontlaad = √(RTE/100). Zie strategie_dp.py voor uitleg.

        Geeft ook hw_min_pct en hw_max_pct terug zodat de DP-uitvoer vertaald
        kan worden naar echte battery-SoC% (0–100 % van totale capaciteit).
        """
        beschikbaar_kwh, beschikbaar_bron = self._historische_float_state(
            "sensor.zendure_2400_ac_indicatie_beschikbare_energie",
            dp_start_tijd,
            0.0,
        )
        benodigde_kwh, benodigde_bron = self._historische_float_state(
            "sensor.zendure_2400_ac_indicatie_benodigde_energie",
            dp_start_tijd,
            0.0,
        )
        rte_pct, rte_bron = self._historische_float_state(
            "sensor.zendure_2400_ac_rte_totaal",
            dp_start_tijd,
            90.0,
        )
        max_laad_w, max_laad_bron = self._historische_float_state(
            "input_number.zendure_2400_ac_max_oplaadvermogen",
            dp_start_tijd,
            2400.0,
        )
        max_ontlaad_w, max_ontlaad_bron = self._historische_float_state(
            "input_number.zendure_2400_ac_max_ontlaadvermogen",
            dp_start_tijd,
            2400.0,
        )
        hw_min_pct, hw_min_bron = self._historische_float_state(
            "sensor.zendure_2400_ac_minimale_laadpercentage",
            dp_start_tijd,
            0.0,
        )
        hw_max_pct, hw_max_bron = self._historische_float_state(
            "sensor.zendure_2400_ac_maximale_laadpercentage",
            dp_start_tijd,
            100.0,
        )

        beschikbaar_kwh = float(beschikbaar_kwh or 0.0)
        benodigde_kwh = float(benodigde_kwh or 0.0)
        rte_pct = float(rte_pct or 90.0)
        max_laad_w = float(max_laad_w or 2400.0)
        max_ontlaad_w = float(max_ontlaad_w or 2400.0)
        hw_min_pct = float(hw_min_pct or 0.0)
        hw_max_pct = float(hw_max_pct or 100.0)

        if dp_start_tijd is not None and beschikbaar_kwh + benodigde_kwh <= 0.0:
            actuele_beschikbaar_kwh = self._float_state(
                "sensor.zendure_2400_ac_indicatie_beschikbare_energie"
            )
            actuele_benodigde_kwh = self._float_state(
                "sensor.zendure_2400_ac_indicatie_benodigde_energie"
            )
            actuele_waarden_geldig = (
                actuele_beschikbaar_kwh is not None
                and actuele_benodigde_kwh is not None
                and actuele_beschikbaar_kwh >= 0.0
                and actuele_benodigde_kwh >= 0.0
                and actuele_beschikbaar_kwh + actuele_benodigde_kwh > 0.0
            )
            if actuele_waarden_geldig:
                self.log(
                    "Dynamisch Handelen: historische energie-indicaties op "
                    f"{dp_start_tijd.isoformat()} zijn samen 0 kWh "
                    f"(beschikbaar={beschikbaar_kwh}, benodigd={benodigde_kwh}); "
                    "actuele energie-indicaties gebruikt "
                    f"(beschikbaar={actuele_beschikbaar_kwh}, "
                    f"benodigd={actuele_benodigde_kwh})",
                    level="WARNING",
                )
                beschikbaar_kwh = actuele_beschikbaar_kwh
                benodigde_kwh = actuele_benodigde_kwh
                beschikbaar_bron = "huidig_wegens_ongeldige_history"
                benodigde_bron = "huidig_wegens_ongeldige_history"

        rte_pct = max(50.0, min(100.0, rte_pct))
        eta     = math.sqrt(rte_pct / 100.0)

        stored_current = beschikbaar_kwh / eta
        stored_ruimte  = benodigde_kwh   * eta

        bronnen = {
            "tijd": dp_start_tijd.isoformat() if dp_start_tijd else None,
            "beschikbare_energie": beschikbaar_bron,
            "benodigde_energie": benodigde_bron,
            "rte_totaal": rte_bron,
            "max_laad_w": max_laad_bron,
            "max_ontlaad_w": max_ontlaad_bron,
            "hw_min_pct": hw_min_bron,
            "hw_max_pct": hw_max_bron,
        }

        return Accustatus(
            huidig_kwh    = stored_current,
            max_kwh       = stored_current + stored_ruimte,
            eta_laad      = eta,
            eta_ontlaad   = eta,
            max_laad_w    = max_laad_w,
            max_ontlaad_w = max_ontlaad_w,
        ), hw_min_pct, hw_max_pct, bronnen

    def _haal_minimale_spread(self) -> float:
        """
        Leest de gebruikersingestelde minimale spread (ct/kWh).
        Voorkomt handel bij kleine prijsverschillen die weliswaar theoretisch
        winstgevend zijn maar in de praktijk onzeker zijn.

        Home Assistant herstelt de laatst ingestelde helperwaarde. Alleen als
        die waarde tijdelijk ontbreekt of ongeldig is, gebruikt AppDaemon de
        aanbevolen buffer van 2 ct/kWh.
        """
        return self._haal_float_met_default(
            "input_number.dynamisch_minimale_spread",
            DEFAULT_MINIMALE_SPREAD_CT_PER_KWH,
            minimum=0.0,
        )

    def _haal_advies_analyse_dagen(self) -> int:
        """
        Leest hoeveel dagen recorder-history de adviesanalyse mag gebruiken.
        Korter dan 30 dagen is prima: de sensor publiceert dan lagere confidence
        als er nog weinig bruikbare slots zijn.
        """
        waarde = self._haal_float_met_default(
            "input_number.dynamisch_advies_analyse_dagen",
            14,
            minimum=1,
        )
        return min(30, max(1, int(round(waarde))))

    def _haal_warmte_penalty_laden_factor(self) -> float:
        """
        Leest hoeveel gewicht de DP aan C-waarde warmteverlies bij laden moet geven.
        """
        waarde = self.get_state("input_number.dynamisch_warmte_penalty_laden_factor")
        if waarde in (None, "unknown", "unavailable"):
            waarde = self.get_state("input_number.dynamisch_warmte_penalty_factor")
        try:
            return max(0.0, float(waarde))
        except (TypeError, ValueError):
            return WARMTE_PENALTY_LADEN_FACTOR

    def _haal_warmte_penalty_ontladen_factor(self) -> float:
        """
        Leest hoeveel gewicht de DP aan C-waarde warmteverlies bij ontladen moet geven.
        """
        waarde = self.get_state("input_number.dynamisch_warmte_penalty_ontladen_factor")
        try:
            return max(0.0, float(waarde))
        except (TypeError, ValueError):
            return WARMTE_PENALTY_ONTLADEN_FACTOR

    def _haal_standby_verbruik_w(self) -> float:
        """Leest het standbyverbruik voor rust- en ontlaadslots in W."""
        return self._haal_float_met_default(
            "input_number.dynamisch_standby_verbruik_w",
            STANDBY_VERBRUIK_W,
            minimum=0.0,
        )

    def _haal_minimum_vermogen_w(self) -> int:
        """Leest de minimumgrens voor DP-laad- en ontlaadvermogen in W."""
        waarde = self._haal_float_met_default(
            "input_number.dynamisch_minimum_vermogen_w",
            MINIMUM_VERMOGEN_W,
            minimum=50.0,
        )
        begrensd = min(3000.0, waarde)
        return int(math.ceil(begrensd / VERMOGEN_STAP_W) * VERMOGEN_STAP_W)

    def _haal_float_met_default(self, entity_id: str, default: float, minimum: float | None = None) -> float:
        """Leest een numerieke HA-helper en gebruikt default bij unknown/unavailable."""
        waarde = self.get_state(entity_id)
        try:
            getal = float(waarde)
        except (TypeError, ValueError):
            getal = default
        if minimum is not None:
            getal = max(minimum, getal)
        return getal

    def _haal_warmte_stijging_factor(self, entity_id: str, default: float) -> float:
        """
        Leest een richting-specifieke accutemperatuurfactor.

        input_number.dynamisch_warmte_stijging_c_per_c2h blijft de fallback voor
        installaties waar de twee nieuwe input_number helpers nog ontbreken.
        """
        waarde = self.get_state(entity_id)
        if waarde in (None, "unknown", "unavailable"):
            waarde = self.get_state("input_number.dynamisch_warmte_stijging_c_per_c2h")
        try:
            return max(0.0, float(waarde))
        except (TypeError, ValueError):
            return default

    def _haal_entity_id_uit_input_text(self, entity_id: str) -> str | None:
        """Leest een entity_id uit input_text en negeert lege helperwaarden."""
        waarde = self.get_state(entity_id)
        if waarde in (None, "unknown", "unavailable"):
            return None
        tekst = str(waarde).strip()
        return tekst or None

    def _float_state(self, entity_id: str) -> float | None:
        """Leest een state als float; unknown/unavailable geeft None."""
        try:
            return float(self.get_state(entity_id))
        except (TypeError, ValueError):
            return None

    def _haal_warmste_batterij_temp_c(self) -> float | None:
        """Neemt de hoogste accutemperatuur, niet de invertertemperatuur."""
        warmste = self._float_state("sensor.zendure_2400_ac_warmste_batterij_temperatuur")
        if warmste is not None:
            return warmste

        try:
            aantal = int(float(self.get_state("sensor.zendure_2400_ac_aantal_batterijen") or 0))
        except (TypeError, ValueError):
            aantal = 0
        max_index = aantal if aantal > 0 else 6

        waarden = []
        for index in range(1, max_index + 1):
            waarde = self._float_state(f"sensor.zendure_2400_ac_batterij_{index}_temperatuur")
            if waarde is not None:
                waarden.append(waarde)
        return max(waarden) if waarden else None

    def _haal_warmste_batterij_temp_c_op_tijd(self, tijd: datetime | None) -> tuple[float | None, str]:
        """Leest de warmste accutemperatuur op de DP-starttijd."""
        warmste, warmste_bron = self._historische_float_state(
            "sensor.zendure_2400_ac_warmste_batterij_temperatuur",
            tijd,
        )
        if warmste is not None:
            return warmste, warmste_bron

        try:
            aantal = int(float(self.get_state("sensor.zendure_2400_ac_aantal_batterijen") or 0))
        except (TypeError, ValueError):
            aantal = 0
        max_index = aantal if aantal > 0 else 6

        waarden = []
        bronnen = []
        for index in range(1, max_index + 1):
            waarde, bron = self._historische_float_state(
                f"sensor.zendure_2400_ac_batterij_{index}_temperatuur",
                tijd,
            )
            if waarde is not None:
                waarden.append(waarde)
                bronnen.append(bron)

        if waarden:
            return max(waarden), ",".join(sorted(set(bronnen)))
        return None, "onbekend"

    def _haal_buitentemperatuur_sensor_entity(self) -> str | None:
        """Kiest de ingestelde historische sensor of de aanwezige Buienradar-sensor."""
        ingesteld = self._haal_entity_id_uit_input_text(
            "input_text.dynamisch_buitentemperatuur_sensor"
        )
        if ingesteld:
            return ingesteld
        standaard = str(self.args.get("default_buitentemperatuur_sensor") or "").strip()
        if standaard and self.get_state(standaard) is not None:
            return standaard
        return None

    def _haal_buitentemperatuur_c(self, tijd: datetime | None = None) -> tuple[float | None, str]:
        """Leest buitentemperatuur uit de historische sensor of weather entity."""
        sensor_entity = self._haal_buitentemperatuur_sensor_entity()

        if sensor_entity and tijd is not None:
            sensor_temp, sensor_bron = self._historische_float_state(sensor_entity, tijd)
            if sensor_temp is not None:
                return sensor_temp, f"{sensor_entity}:{sensor_bron}"

        if sensor_entity:
            sensor_temp = self._float_state(sensor_entity)
            if sensor_temp is not None:
                return sensor_temp, sensor_entity

        weather_entity = self._haal_weather_entity()
        if weather_entity:
            try:
                weather_temp = float(self.get_state(weather_entity, attribute="temperature"))
                return weather_temp, f"{weather_entity}.temperature"
            except (TypeError, ValueError):
                pass

        return None, "onbekend"

    def _haal_weather_entity(self) -> str | None:
        """Leest de weather entity voor forecastdata."""
        ingesteld = self._haal_entity_id_uit_input_text("input_text.dynamisch_weather_entity")
        if ingesteld:
            return ingesteld

        standaard = "weather.openweathermap"
        return standaard if self.get_state(standaard) is not None else None

    def _normaliseer_forecast_punten(self, forecast: list[dict] | None) -> list[tuple[datetime, float]]:
        """Zet HA weather forecast-items om naar gesorteerde (tijd, temperatuur)-punten."""
        punten: list[tuple[datetime, float]] = []
        if not forecast:
            return punten

        for item in forecast:
            try:
                tijd = datetime.fromisoformat(str(item["datetime"])).astimezone()
                temp = float(item["temperature"])
            except (KeyError, TypeError, ValueError):
                continue
            punten.append((tijd, temp))
        punten.sort(key=lambda punt: punt[0])
        return punten

    def _haal_forecast_punten(self, weather_entity: str | None) -> tuple[list[tuple[datetime, float]], str]:
        """Haalt hourly forecast op via weather.get_forecasts of via oudere forecast-attributen."""
        if not weather_entity:
            return [], "geen_weather_entity"

        try:
            response = self.call_service(
                "weather/get_forecasts",
                entity_id=weather_entity,
                type="hourly",
                return_response=True,
            )
        except Exception as exc:
            self.log(
                f"Dynamisch Handelen: forecast service gaf geen resultaat voor {weather_entity}: {exc}",
                level="DEBUG",
            )
            response = None

        kandidaat_mappings = []
        if isinstance(response, dict):
            kandidaat_mappings.append(response)
            result = response.get("result")
            if isinstance(result, dict):
                kandidaat_mappings.append(result)
                response_data = result.get("response")
                if isinstance(response_data, dict):
                    kandidaat_mappings.insert(0, response_data)

        for mapping in kandidaat_mappings:
            payload = mapping.get(weather_entity)
            if payload is None:
                payload = next((waarde for waarde in mapping.values() if isinstance(waarde, dict)), None)
            if not isinstance(payload, dict):
                continue
            punten = self._normaliseer_forecast_punten(payload.get("forecast"))
            if punten:
                return punten, "weather.get_forecasts"

        forecast_attr = self.get_state(weather_entity, attribute="forecast")
        punten = self._normaliseer_forecast_punten(forecast_attr if isinstance(forecast_attr, list) else None)
        if punten:
            return punten, f"{weather_entity}.forecast"

        return [], "forecast_onbeschikbaar"

    def _forecast_temp_voor_slot(
        self,
        punten: list[tuple[datetime, float]],
        start: datetime,
    ) -> float | None:
        """Kiest de laatste forecasttemperatuur op of voor het slot, of anders de eerste erna."""
        if not punten:
            return None

        start = start.astimezone()
        gekozen = punten[0][1]
        for tijd, temp in punten:
            if tijd > start:
                break
            gekozen = temp
        return gekozen

    def _haal_thermische_config(self, slots: list[dict], dp_start_tijd: datetime | None = None) -> dict:
        """Leest thermische HA-config en zet buiten_temp_c op ieder prijsslot."""
        batterij_temp_start_c, batterij_temp_bron = self._haal_warmste_batterij_temp_c_op_tijd(dp_start_tijd)
        buiten_temp_huidig_c, buiten_temp_bron = self._haal_buitentemperatuur_c(dp_start_tijd)
        weather_entity = self._haal_weather_entity()
        forecast_punten, forecast_bron = self._haal_forecast_punten(weather_entity)

        for slot in slots:
            temp = self._forecast_temp_voor_slot(forecast_punten, slot["start"])
            if temp is None:
                temp = buiten_temp_huidig_c
            if temp is not None:
                slot["buiten_temp_c"] = temp

        return {
            "batterij_temp_start_c": batterij_temp_start_c,
            "batterij_temp_bron": batterij_temp_bron,
            "buiten_temp_huidig_c": buiten_temp_huidig_c,
            "buiten_temp_bron": buiten_temp_bron,
            "weather_entity": weather_entity,
            "forecast_bron": forecast_bron,
            "forecast_punten": len(forecast_punten),
            "warmte_afkoeling_halveringstijd_h": self._haal_float_met_default(
                "input_number.dynamisch_warmte_afkoeling_halveringstijd_uren",
                2.0,
                minimum=0.05,
            ),
            "warmte_stijging_laden_c_per_c2h": self._haal_warmte_stijging_factor(
                "input_number.dynamisch_warmte_stijging_laden_c_per_c2h",
                WARMTE_STIJGING_LADEN_C_PER_C2H,
            ),
            "warmte_stijging_ontladen_c_per_c2h": self._haal_warmte_stijging_factor(
                "input_number.dynamisch_warmte_stijging_ontladen_c_per_c2h",
                WARMTE_STIJGING_ONTLADEN_C_PER_C2H,
            ),
            "temp_limiet_c": self._haal_float_met_default(
                "input_number.dynamisch_max_temp_boven_80_soc",
                TEMP_LIMIET_C,
            ),
            "temp_limiet_lage_soc_c": self._haal_float_met_default(
                "input_number.dynamisch_max_temp_onder_80_soc",
                TEMP_LIMIET_LAGE_SOC_C,
            ),
            "temp_penalty_factor": self._haal_float_met_default(
                "input_number.dynamisch_temp_penalty_factor",
                TEMP_PENALTY_FACTOR,
                minimum=0.0,
            ),
            "temp_penalty_100_soc_factor": self._haal_float_met_default(
                "input_number.dynamisch_temp_penalty_100_soc_factor",
                TEMP_PENALTY_100_SOC_FACTOR,
                minimum=1.0,
            ),
            "hoge_soc_verblijf_penalty_factor": self._haal_float_met_default(
                "input_number.dynamisch_hoge_soc_verblijf_penalty_factor",
                HOGE_SOC_VERBLIJF_PENALTY_FACTOR,
                minimum=0.0,
            ),
            "lage_soc_verblijf_penalty_factor": self._haal_float_met_default(
                "input_number.dynamisch_lage_soc_verblijf_penalty_factor",
                LAGE_SOC_VERBLIJF_PENALTY_FACTOR,
                minimum=0.0,
            ),
        }

    def _haal_plateau_spreiding(self) -> bool:
        """
        Leest uit apps.yaml of de plateau-nabewerking actief mag zijn.
        """
        waarde = self.args.get("plateau_spreiding", True)
        if isinstance(waarde, bool):
            return waarde
        return str(waarde).strip().lower() in ("1", "true", "yes", "on", "aan")

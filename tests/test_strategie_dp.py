"""
Unit tests voor het DP-optimalisatie algoritme (strategie_dp.py).

Draait volledig zonder Home Assistant — geen HA-globals nodig.
Uitvoeren vanuit de repo-root:

    pip install pytest
    pytest tests/test_strategie_dp.py -v
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Voeg pyscript/modules toe aan het Python-pad zodat we de module direct importeren
sys.path.insert(0, str(Path(__file__).parent.parent / "appdaemon" / "apps"))

from strategie_dp import (
    Accustatus,
    DP_VERMOGEN_STAP_W,
    SOC_STAP_KWH,
    StrategieBerekeningGeannuleerd,
    bereken_derating,
    bereken_laadvermogen_voor_aansturing,
    corrigeer_actief_slot_vermogen,
    los_dp_op,
    rond_vermogen_omhoog,
)


# ── TESTHELPERS ───────────────────────────────────────────────────────────────

_T0 = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)


def maak_slots(prijzen: list[float], duur_h: float = 1.0) -> list[dict]:
    """Bouw een lijst van prijsslots vanuit een simpele prijslijst."""
    slots = []
    for i, prijs in enumerate(prijzen):
        start = _T0 + timedelta(hours=i * duur_h)
        slots.append({
            "start":      start,
            "end":        start + timedelta(hours=duur_h),
            "price":      prijs,
            "duration_h": duur_h,
        })
    return slots


def maak_accu(
    huidig_kwh: float  = 1.2,
    max_kwh: float     = 2.4,
    eta: float         = 0.949,    # ≈ √0.90, dus RTE ≈ 90 %
    max_laad_w: float  = 2400.0,
    max_ontlaad_w: float = 2400.0,
) -> Accustatus:
    """Standaard testaccu: 2.4 kWh bruikbaar, 2400 W, η ≈ 0.949."""
    return Accustatus(
        huidig_kwh    = huidig_kwh,
        max_kwh       = max_kwh,
        eta_laad      = eta,
        eta_ontlaad   = eta,
        max_laad_w    = max_laad_w,
        max_ontlaad_w = max_ontlaad_w,
    )


def totale_winst(schema: list[dict]) -> float:
    return sum(s["winst_eur"] for s in schema)


def acties(schema: list[dict]) -> list[str]:
    return [s["actie"] for s in schema]


def maak_slots_vanaf_iso(slot_prijzen_ct: list[tuple[str, str, float]]) -> list[dict]:
    """Bouw prijsslots vanuit ISO-start, ISO-einde, en prijs in ct/kWh."""
    slots = []
    for start_iso, end_iso, prijs_ct in slot_prijzen_ct:
        start = datetime.fromisoformat(start_iso)
        end = datetime.fromisoformat(end_iso)
        slots.append({
            "start":      start,
            "end":        end,
            "price":      prijs_ct / 100.0,
            "duration_h": (end - start).total_seconds() / 3600.0,
        })
    return slots


SCENARIO_18_MEI_SLOTS_CT = [
    ("2026-05-18T14:00:00+02:00", "2026-05-18T15:00:00+02:00", 24.663),
    ("2026-05-18T15:00:00+02:00", "2026-05-18T16:00:00+02:00", 25.496),
    ("2026-05-18T16:00:00+02:00", "2026-05-18T17:00:00+02:00", 26.300),
    ("2026-05-18T17:00:00+02:00", "2026-05-18T18:00:00+02:00", 28.720),
    ("2026-05-18T18:00:00+02:00", "2026-05-18T19:00:00+02:00", 30.868),
    ("2026-05-18T19:00:00+02:00", "2026-05-18T20:00:00+02:00", 35.321),
    ("2026-05-18T20:00:00+02:00", "2026-05-18T21:00:00+02:00", 41.322),
    ("2026-05-18T21:00:00+02:00", "2026-05-18T22:00:00+02:00", 37.963),
    ("2026-05-18T22:00:00+02:00", "2026-05-18T23:00:00+02:00", 32.392),
    ("2026-05-18T23:00:00+02:00", "2026-05-19T00:00:00+02:00", 30.867),
    ("2026-05-19T00:00:00+02:00", "2026-05-19T01:00:00+02:00", 30.652),
    ("2026-05-19T01:00:00+02:00", "2026-05-19T02:00:00+02:00", 29.691),
    ("2026-05-19T02:00:00+02:00", "2026-05-19T03:00:00+02:00", 29.269),
    ("2026-05-19T03:00:00+02:00", "2026-05-19T04:00:00+02:00", 29.106),
    ("2026-05-19T04:00:00+02:00", "2026-05-19T05:00:00+02:00", 29.247),
    ("2026-05-19T05:00:00+02:00", "2026-05-19T06:00:00+02:00", 29.951),
    ("2026-05-19T06:00:00+02:00", "2026-05-19T07:00:00+02:00", 31.775),
    ("2026-05-19T07:00:00+02:00", "2026-05-19T08:00:00+02:00", 30.869),
    ("2026-05-19T08:00:00+02:00", "2026-05-19T09:00:00+02:00", 28.636),
    ("2026-05-19T09:00:00+02:00", "2026-05-19T10:00:00+02:00", 26.353),
    ("2026-05-19T10:00:00+02:00", "2026-05-19T11:00:00+02:00", 24.150),
    ("2026-05-19T11:00:00+02:00", "2026-05-19T12:00:00+02:00", 22.600),
    ("2026-05-19T12:00:00+02:00", "2026-05-19T13:00:00+02:00", 21.499),
    ("2026-05-19T13:00:00+02:00", "2026-05-19T14:00:00+02:00", 21.662),
    ("2026-05-19T14:00:00+02:00", "2026-05-19T15:00:00+02:00", 21.873),
    ("2026-05-19T15:00:00+02:00", "2026-05-19T16:00:00+02:00", 23.042),
    ("2026-05-19T16:00:00+02:00", "2026-05-19T17:00:00+02:00", 24.421),
    ("2026-05-19T17:00:00+02:00", "2026-05-19T18:00:00+02:00", 27.272),
    ("2026-05-19T18:00:00+02:00", "2026-05-19T19:00:00+02:00", 29.216),
    ("2026-05-19T19:00:00+02:00", "2026-05-19T20:00:00+02:00", 32.471),
    ("2026-05-19T20:00:00+02:00", "2026-05-19T21:00:00+02:00", 33.808),
    ("2026-05-19T21:00:00+02:00", "2026-05-19T22:00:00+02:00", 31.744),
    ("2026-05-19T22:00:00+02:00", "2026-05-19T23:00:00+02:00", 30.268),
    ("2026-05-19T23:00:00+02:00", "2026-05-20T00:00:00+02:00", 28.736),
]


# ── DERATING ─────────────────────────────────────────────────────────────────

class TestDerating:
    def test_onder_tachtig_procent_vol_vermogen(self):
        """Tussen 0–80 % SoC is de derating-factor 1.0."""
        assert bereken_derating(0.0, 2.4)        == pytest.approx(1.0)
        assert bereken_derating(2.4 * 0.79, 2.4) == pytest.approx(1.0)

    def test_interpolatie_op_85_procent(self):
        """Op 85 % SoC: lineair tussen (80 %, 1.0) en (90 %, 0.7) → 0.85."""
        factor = bereken_derating(2.4 * 0.85, 2.4)
        assert factor == pytest.approx(0.85, abs=0.01)

    def test_exact_negentig_procent(self):
        assert bereken_derating(2.4 * 0.90, 2.4) == pytest.approx(0.70, abs=0.01)

    def test_exact_vijfennegentig_procent(self):
        assert bereken_derating(2.4 * 0.95, 2.4) == pytest.approx(0.40, abs=0.01)

    def test_volle_accu_trickle(self):
        assert bereken_derating(2.4, 2.4) == pytest.approx(0.10, abs=0.01)

    def test_nul_capaciteit_geeft_nul(self):
        """Bescherming tegen deling-door-nul."""
        assert bereken_derating(1.0, 0.0) == 0.0


class TestVermogenAfronding:
    def test_rondt_omhoog_op_25_watt_stappen(self):
        assert rond_vermogen_omhoog(1, 2400) == 25
        assert rond_vermogen_omhoog(2397, 2400) == 2400
        assert rond_vermogen_omhoog(2400, 2400) == 2400

    def test_rondt_nooit_boven_maximum(self):
        assert rond_vermogen_omhoog(2397, 2398) == 2398
        assert rond_vermogen_omhoog(0, 2400) == 0

    def test_bms_derating_verlaagt_alleen_verwacht_laadvermogen(self):
        """
        bereken_laadvermogen_voor_aansturing() stuurt max_laad_w naar Zendure
        zodra bereken_derating() een factor lager dan 1.0 geeft.
        """
        assert bereken_laadvermogen_voor_aansturing(950, 2400, 0.40) == 2400
        assert bereken_laadvermogen_voor_aansturing(950, 2400, 1.00) == 950


# ── DP BASISGEDRAG ───────────────────────────────────────────────────────────

class TestDPBasis:
    def test_lege_slotlijst(self):
        assert los_dp_op([], maak_accu()) == []

    def test_correct_aantal_slots_in_resultaat(self):
        schema = los_dp_op(maak_slots([0.10, 0.20, 0.30]), maak_accu())
        assert len(schema) == 3

    def test_soc_verloop_consistent(self):
        """soc_na[t] moet gelijk zijn aan soc_voor[t+1] voor elk opeenvolgend slot."""
        schema = los_dp_op(maak_slots([0.05, 0.10, 0.30, 0.08, 0.25]), maak_accu(huidig_kwh=0.5))
        for i in range(len(schema) - 1):
            assert schema[i]["soc_na_kwh"] == pytest.approx(schema[i + 1]["soc_voor_kwh"], abs=0.01)

    def test_soc_nooit_boven_max(self):
        """SoC mag de maximale capaciteit nooit overschrijden, ook niet bij agressief laden."""
        schema = los_dp_op(maak_slots([0.01] * 12), maak_accu(huidig_kwh=0.0))
        for s in schema:
            assert s["soc_na_kwh"] <= 2.4 + 0.01  # kleine afrondingsmarge

    def test_soc_nooit_onder_nul(self):
        """SoC mag nooit negatief worden, ook niet bij agressief ontladen."""
        schema = los_dp_op(maak_slots([0.50] * 12), maak_accu(huidig_kwh=1.0))
        for s in schema:
            assert s["soc_na_kwh"] >= -0.01  # kleine afrondingsmarge

    def test_ontlaadvermogen_is_ac_outputlimiet(self):
        """
        max_ontlaad_w is het Zendure outputLimit in W en wordt niet met eta verlaagd.

        Bij genoeg SoC moet het gerapporteerde vermogen dus 2400 W zijn, niet
        2400 * eta. De SoC-daling is wel groter dan 2.4 kWh door ontlaadverlies.
        """
        schema = los_dp_op(
            maak_slots([0.50]),
            maak_accu(huidig_kwh=4.0, max_kwh=5.0, eta=0.90, max_ontlaad_w=2400),
        )

        assert schema[0]["actie"] == "ontladen"
        assert schema[0]["vermogen_w"] == 2400
        assert schema[0]["soc_na_kwh"] == pytest.approx(4.0 - 2.4 / 0.90, abs=SOC_STAP_KWH)

    def test_eerste_soc_is_huidig(self):
        """Het eerste slot begint bij de opgegeven huidige SoC."""
        accu   = maak_accu(huidig_kwh=1.5)
        schema = los_dp_op(maak_slots([0.10]), accu)
        assert schema[0]["soc_voor_kwh"] == pytest.approx(accu.huidig_kwh, abs=SOC_STAP_KWH)

    def test_annuleer_check_stopt_dp_run(self):
        """AppDaemon kan een verouderde DP-run stoppen voordat die een schema teruggeeft."""
        with pytest.raises(StrategieBerekeningGeannuleerd):
            los_dp_op(
                maak_slots([0.05, 0.30, 0.04, 0.32]),
                maak_accu(huidig_kwh=0.0),
                annuleer_check=lambda: True,
            )


# ── ARBITRAGE LOGICA ──────────────────────────────────────────────────────────

class TestArbitrage:
    def test_simpele_arbitrage_laden_dan_ontladen(self):
        """
        Goedkoop slot → duur slot: algoritme kiest laden + ontladen.
        De netto winst is positief na aftrek van η-verliezen.
        """
        schema = los_dp_op(maak_slots([0.05, 0.30]), maak_accu(huidig_kwh=0.0))
        assert acties(schema) == ["laden", "ontladen"]
        assert totale_winst(schema) > 0

    def test_geen_arbitrage_bij_onvoldoende_spread(self):
        """
        Als de spread kleiner is dan de η²-break-even grens, is laden + ontladen
        verlieslatend. Het algoritme mag dan geen cyclus plannen.

        Break-even: p_verkoop / p_inkoop = 1/η²
        Bij η = 0.949: 1/η² ≈ 1.111 → 10 ct → 11 ct (ratio 1.10) is verlieslatend.
        """
        schema = los_dp_op(
            maak_slots([0.10, 0.11]),
            maak_accu(huidig_kwh=0.0, eta=0.949),
            hoge_soc_verblijf_penalty_factor=0.0,
            lage_soc_verblijf_penalty_factor=0.0,
        )
        assert all(s["actie"] == "rust" for s in schema)

    def test_meerdere_cycli_worden_benut(self):
        """
        Twee afzonderlijke goedkoop/duur-blokken → twee volledige cycli.
        Het algoritme mag er niet één missen.
        """
        schema = los_dp_op(maak_slots([0.05, 0.30, 0.04, 0.28]), maak_accu(huidig_kwh=0.0))
        assert acties(schema) == ["laden", "ontladen", "laden", "ontladen"]

    def test_geen_ontladen_zonder_lading(self):
        """Een lege accu kan niet ontladen, ook niet bij een hoge prijs."""
        schema = los_dp_op(maak_slots([0.50]), maak_accu(huidig_kwh=0.0))
        assert schema[0]["actie"] == "rust"

    def test_geen_laden_bij_volle_accu(self):
        """Een volledig geladen accu kan niet meer laden."""
        accu   = maak_accu(huidig_kwh=2.4, max_kwh=2.4)
        schema = los_dp_op(maak_slots([0.01, 0.30]), accu)
        assert schema[0]["actie"] != "laden"

    def test_duur_goedkoop_volgorde_geen_winst(self):
        """
        Duur slot → goedkoop slot: voor een lege accu is er geen winstgevende
        actie mogelijk. Laden bij hoge prijs en ontladen bij lage prijs maakt verlies.
        """
        schema = los_dp_op(maak_slots([0.30, 0.05]), maak_accu(huidig_kwh=0.0))
        assert totale_winst(schema) <= 0

    def test_winst_positief_bij_grote_spread(self):
        """Grote spread (5 ct → 30 ct) levert altijd een positieve netto winst."""
        schema = los_dp_op(maak_slots([0.05, 0.30]), maak_accu(huidig_kwh=0.0))
        assert totale_winst(schema) > 0.01  # ruim boven nul, niet slechts afronding


# ── MINIMALE SPREAD ───────────────────────────────────────────────────────────

class TestMinimaleSpread:
    def test_geen_handel_onder_minimale_spread(self):
        """
        Met een minimale spread van 30 ct/kWh mag er bij een verschil van
        slechts 20 ct (10 ct → 30 ct) geen cyclus plaatsvinden.

        De brutospread (20 ct/kWh) is hier kleiner dan de drempel (30 ct/kWh),
        ook al zou de trade zonder drempel theoretisch winstgevend zijn.
        """
        schema = los_dp_op(
            maak_slots([0.10, 0.30]),
            maak_accu(huidig_kwh=0.0),
            min_spread_ct_per_kwh=30.0,
            hoge_soc_verblijf_penalty_factor=0.0,
            lage_soc_verblijf_penalty_factor=0.0,
        )
        assert all(s["actie"] == "rust" for s in schema)

    def test_handel_boven_minimale_spread(self):
        """Met een kleine drempel (2 ct) en grote spread (25 ct) wordt er wel gehandeld."""
        schema = los_dp_op(
            maak_slots([0.05, 0.30]),
            maak_accu(huidig_kwh=0.0),
            min_spread_ct_per_kwh=2.0,
        )
        assert schema[0]["actie"] == "laden"
        assert schema[1]["actie"] == "ontladen"

    def test_18_mei_scenario_laadt_19_mei_niet_bij_spread_acht_ct(self):
        """
        Met min_spread_ct_per_kwh=8 blijft laden op 19 mei onder de drempel.

        De goedkoopste laadprijs op 19 mei is 21.499 ct/kWh en de hoogste
        ontlaadprijs op 19 mei is 33.808 ct/kWh. Met eta=0.922 blijft de marge
        lager dan 8 ct/kWh, dus los_dp_op() plant geen laadslot op 19 mei.
        """
        schema = los_dp_op(
            maak_slots_vanaf_iso(SCENARIO_18_MEI_SLOTS_CT),
            maak_accu(huidig_kwh=2.636, max_kwh=5.217, eta=0.922),
            min_spread_ct_per_kwh=8.0,
            hoge_soc_verblijf_penalty_factor=0.0,
            lage_soc_verblijf_penalty_factor=0.0,
        )

        laad_19_mei = [
            s for s in schema
            if s["start"].startswith("2026-05-19") and s["actie"] == "laden"
        ]

        assert laad_19_mei == []

    def test_18_mei_scenario_laadt_19_mei_wel_bij_spread_twee_ct(self):
        """
        Met min_spread_ct_per_kwh=2 plant los_dp_op() wel laadslots op 19 mei.

        Deze test gebruikt dezelfde prijzen, accu, eta, en start-SoC als
        test_18_mei_scenario_laadt_19_mei_niet_bij_spread_acht_ct.
        Alleen min_spread_ct_per_kwh verandert van 8 naar 2.
        """
        schema = los_dp_op(
            maak_slots_vanaf_iso(SCENARIO_18_MEI_SLOTS_CT),
            maak_accu(huidig_kwh=2.636, max_kwh=5.217, eta=0.922),
            min_spread_ct_per_kwh=2.0,
        )

        laad_19_mei = [
            s for s in schema
            if s["start"].startswith("2026-05-19") and s["actie"] == "laden"
        ]

        assert len(laad_19_mei) >= 1

    def test_18_mei_scenario_laadt_actief_gedeeltelijk_uur(self):
        """
        Bij een deels verstreken actief uur moet los_dp_op() nog laadruimte zien.

        Dit scenario gebruikt de sensorwaarden van 18 mei om 14:39. Het eerste
        slot heeft nog ongeveer 20,5 minuten. Als dat slot per ongeluk als
        volledig uur wordt ingevoerd, kiest de strategie rust; met de resterende
        duur kiest de strategie laden. De warmte-penalty kan de gekozen energie
        bewust lager maken dan het oude maximaal-vermogen schema.
        """
        slots = maak_slots_vanaf_iso(SCENARIO_18_MEI_SLOTS_CT)
        slots[0]["duration_h"] = 20.5 / 60.0

        schema = los_dp_op(
            slots,
            maak_accu(huidig_kwh=2.745, max_kwh=5.187, eta=0.922),
            min_spread_ct_per_kwh=8.0,
            plateau_spreiding=False,
        )

        assert schema[0]["actie"] == "laden"
        assert schema[0]["vermogen_w"] >= 100
        assert schema[0]["soc_na_kwh"] > schema[0]["soc_voor_kwh"]

    def test_warmte_penalty_factoren_nul_houden_oude_maximaal_vermogen_keuze(self):
        """
        Met beide warmtefactoren op 0 kiest los_dp_op() dezelfde agressieve
        laadstap als voor de C-waarde penalties.
        """
        slots = maak_slots_vanaf_iso(SCENARIO_18_MEI_SLOTS_CT)
        slots[0]["duration_h"] = 20.5 / 60.0

        schema = los_dp_op(
            slots,
            maak_accu(huidig_kwh=2.745, max_kwh=5.187, eta=0.922),
            min_spread_ct_per_kwh=8.0,
            plateau_spreiding=False,
            warmte_penalty_laden_factor=0.0,
            warmte_penalty_ontladen_factor=0.0,
            hoge_soc_verblijf_penalty_factor=0.0,
            lage_soc_verblijf_penalty_factor=0.0,
        )

        assert schema[0]["actie"] == "laden"
        assert schema[0]["vermogen_w"] == 2400
        assert schema[0]["soc_na_kwh"] == pytest.approx(3.5, abs=0.01)

    def test_dagplanning_gebruikt_laagste_laaduur_niet_langzamer(self):
        """
        Bij de prijzen van 24 mei krijgt het goedkoopste laadslot geen lager
        vermogen dan het duurdere laadslot erna.
        """
        start = datetime.fromisoformat("2026-05-23T13:00:00+02:00")
        prijzen_ct = [
            9.725, 11.342, 13.08, 13.529, 18.433, 26.997,
            30.363, 33.296, 34.44, 31.866, 30.285, 30.056,
            29.657, 29.074, 28.647, 28.416, 27.884, 27.689,
            25.45, 16.21, 13.561, 13.261, 11.893, 9.023,
            4.399, 6.111, 10.728, 13.327, 15.09, 24.384,
            29.684, 32.02, 31.956, 30.492, 29.348,
        ]
        slots = []
        for i, prijs_ct in enumerate(prijzen_ct):
            slot_start = start + timedelta(hours=i)
            slots.append({
                "start": slot_start,
                "end": slot_start + timedelta(hours=1),
                "price": prijs_ct / 100.0,
                "duration_h": 1.0,
            })

        schema = los_dp_op(
            slots,
            Accustatus(
                huidig_kwh=2.487,
                max_kwh=4.605,
                eta_laad=0.925,
                eta_ontlaad=0.925,
                max_laad_w=1800,
                max_ontlaad_w=2150,
            ),
            min_spread_ct_per_kwh=8.0,
            plateau_spreiding=False,
        )

        goedkoopste = schema[24]
        duurder_ernaast = schema[25]
        assert goedkoopste["prijs_ct"] == pytest.approx(4.399)
        assert duurder_ernaast["prijs_ct"] == pytest.approx(6.111)
        assert goedkoopste["actie"] == "laden"
        assert duurder_ernaast["actie"] == "laden"
        assert goedkoopste["vermogen_w"] >= duurder_ernaast["vermogen_w"]

    def test_dp_gebruikt_geen_vermogensstap_onder_minimum(self):
        schema = los_dp_op(
            maak_slots([0.10, 0.35, 0.09, 0.34]),
            maak_accu(huidig_kwh=0.0),
            plateau_spreiding=False,
            minimum_vermogen_w=100,
        )

        for slot in schema:
            assert slot["vermogen_w"] == 0 or slot["vermogen_w"] >= 100

    def test_dp_gebruikt_minimum_grove_stappen_en_exact_maximum(self):
        """
        De DP-kandidaten bevatten 100W, grove stappen en de exacte max-limiet.
        """
        accu = maak_accu(
            huidig_kwh=0.0,
            max_kwh=4.65,
            max_laad_w=1800,
            max_ontlaad_w=2150,
        )
        schema = los_dp_op(
            maak_slots([0.01, 1.00, 0.01, 1.00]),
            accu,
            plateau_spreiding=False,
            warmte_penalty_laden_factor=0.0,
            warmte_penalty_ontladen_factor=0.0,
        )

        geldige_laadwaarden = {100, 1800} | set(range(DP_VERMOGEN_STAP_W, 1801, DP_VERMOGEN_STAP_W))
        geldige_ontlaadwaarden = {100, 2150} | set(range(DP_VERMOGEN_STAP_W, 2151, DP_VERMOGEN_STAP_W))
        for slot in schema:
            if slot["actie"] == "laden":
                assert slot["vermogen_w"] in geldige_laadwaarden
            if slot["actie"] == "ontladen":
                assert slot["vermogen_w"] in geldige_ontlaadwaarden

    def test_warmte_penalty_ontladen_factor_nul_schakelt_ontladen_uit(self):
        accu = maak_accu(huidig_kwh=2.4, max_kwh=2.4)

        met_penalty = los_dp_op(
            maak_slots([1.00]),
            accu,
            plateau_spreiding=False,
            warmte_penalty_ontladen_factor=1.0,
        )
        zonder_penalty = los_dp_op(
            maak_slots([1.00]),
            accu,
            plateau_spreiding=False,
            warmte_penalty_ontladen_factor=0.0,
        )

        assert met_penalty[0]["actie"] == "ontladen"
        assert zonder_penalty[0]["actie"] == "ontladen"
        assert met_penalty[0]["warmte_penalty_eur"] > 0
        assert zonder_penalty[0]["warmte_penalty_eur"] == 0.0

    def test_thermisch_model_publiceert_temperatuurvelden(self):
        slots = maak_slots([0.01, 0.50])
        for slot in slots:
            slot["buiten_temp_c"] = 30.0

        schema = los_dp_op(
            slots,
            maak_accu(huidig_kwh=0.0),
            plateau_spreiding=False,
            batterij_temp_start_c=32.0,
            temp_limiet_c=35.0,
        )

        eerste = schema[0]
        assert "batterij_temp_voor_c" in eerste
        assert "batterij_temp_na_c" in eerste
        assert "buiten_temp_c" in eerste
        assert "c_waarde" in eerste
        assert "overtemp_penalty_eur" in eerste
        assert "temp_penalty_eur" in eerste
        assert "temp_limiet_actief" in eerste

    def test_thermisch_model_koelt_ook_tijdens_laden(self):
        slots = maak_slots([0.01, 1.00])
        for slot in slots:
            slot["buiten_temp_c"] = 0.0

        schema = los_dp_op(
            slots,
            maak_accu(huidig_kwh=0.0),
            plateau_spreiding=False,
            batterij_temp_start_c=30.0,
            warmte_afkoeling_halveringstijd_h=1.0,
            warmte_stijging_c_per_c2h=0.0,
            temp_penalty_factor=0.0,
        )

        assert schema[0]["actie"] == "laden"
        assert schema[0]["batterij_temp_na_c"] == pytest.approx(15.0)

    def test_thermisch_model_warmt_richting_warme_omgeving(self):
        slots = maak_slots([0.10])
        slots[0]["buiten_temp_c"] = 30.0

        schema = los_dp_op(
            slots,
            maak_accu(huidig_kwh=0.0),
            plateau_spreiding=False,
            batterij_temp_start_c=20.0,
            warmte_afkoeling_halveringstijd_h=1.0,
            temp_penalty_factor=0.0,
        )

        assert schema[0]["actie"] == "rust"
        assert schema[0]["batterij_temp_na_c"] == pytest.approx(25.0)

    def test_thermisch_model_rustslot_koelt_richting_buitenlucht(self):
        slots = maak_slots([0.10])
        slots[0]["buiten_temp_c"] = 10.0

        schema = los_dp_op(
            slots,
            maak_accu(huidig_kwh=0.0),
            plateau_spreiding=False,
            batterij_temp_start_c=30.0,
            warmte_afkoeling_halveringstijd_h=1.0,
            warmte_stijging_c_per_c2h=20.0,
        )

        assert schema[0]["actie"] == "rust"
        assert schema[0]["batterij_temp_na_c"] == pytest.approx(20.0)

    def test_thermisch_model_rustslot_kruist_buitenlucht_niet(self):
        slots = maak_slots([0.10], duur_h=10.0)
        slots[0]["buiten_temp_c"] = 20.0

        schema = los_dp_op(
            slots,
            maak_accu(huidig_kwh=0.0),
            plateau_spreiding=False,
            batterij_temp_start_c=30.0,
            warmte_afkoeling_halveringstijd_h=0.05,
        )

        assert schema[0]["actie"] == "rust"
        assert 20.0 <= schema[0]["batterij_temp_na_c"] <= 30.0

    def test_thermisch_model_warmte_stijging_factor_verhoogt_actietemperatuur(self):
        slots_zonder_stijging = maak_slots([0.01, 1.00])
        slots_met_stijging = maak_slots([0.01, 1.00])
        for slot in slots_zonder_stijging + slots_met_stijging:
            slot["buiten_temp_c"] = 0.0

        accu = maak_accu(huidig_kwh=0.0)
        zonder_stijging = los_dp_op(
            slots_zonder_stijging,
            accu,
            plateau_spreiding=False,
            batterij_temp_start_c=30.0,
            warmte_stijging_c_per_c2h=0.0,
            temp_penalty_factor=0.0,
        )
        met_stijging = los_dp_op(
            slots_met_stijging,
            accu,
            plateau_spreiding=False,
            batterij_temp_start_c=30.0,
            warmte_stijging_c_per_c2h=10.0,
            temp_penalty_factor=0.0,
        )

        assert met_stijging[0]["actie"] == "laden"
        assert met_stijging[0]["batterij_temp_na_c"] > zonder_stijging[0]["batterij_temp_na_c"]

    def test_thermisch_model_gebruikt_richting_specifieke_stijgingfactoren(self):
        laad_slots_zonder_stijging = maak_slots([0.01, 1.00])
        laad_slots_met_stijging = maak_slots([0.01, 1.00])
        ontlaad_slots_zonder_stijging = maak_slots([1.00])
        ontlaad_slots_met_stijging = maak_slots([1.00])
        for slot in (
            laad_slots_zonder_stijging
            + laad_slots_met_stijging
            + ontlaad_slots_zonder_stijging
            + ontlaad_slots_met_stijging
        ):
            slot["buiten_temp_c"] = 0.0

        laad_zonder_stijging = los_dp_op(
            laad_slots_zonder_stijging,
            maak_accu(huidig_kwh=0.0),
            plateau_spreiding=False,
            batterij_temp_start_c=30.0,
            warmte_stijging_laden_c_per_c2h=0.0,
            warmte_stijging_ontladen_c_per_c2h=40.0,
            temp_penalty_factor=0.0,
        )
        laad_met_stijging = los_dp_op(
            laad_slots_met_stijging,
            maak_accu(huidig_kwh=0.0),
            plateau_spreiding=False,
            batterij_temp_start_c=30.0,
            warmte_stijging_laden_c_per_c2h=40.0,
            warmte_stijging_ontladen_c_per_c2h=0.0,
            temp_penalty_factor=0.0,
        )
        ontlaad_zonder_stijging = los_dp_op(
            ontlaad_slots_zonder_stijging,
            maak_accu(huidig_kwh=2.4, max_laad_w=0.0),
            plateau_spreiding=False,
            batterij_temp_start_c=30.0,
            warmte_stijging_laden_c_per_c2h=40.0,
            warmte_stijging_ontladen_c_per_c2h=0.0,
            temp_penalty_factor=0.0,
        )
        ontlaad_met_stijging = los_dp_op(
            ontlaad_slots_met_stijging,
            maak_accu(huidig_kwh=2.4, max_laad_w=0.0),
            plateau_spreiding=False,
            batterij_temp_start_c=30.0,
            warmte_stijging_laden_c_per_c2h=0.0,
            warmte_stijging_ontladen_c_per_c2h=40.0,
            temp_penalty_factor=0.0,
        )

        assert laad_met_stijging[0]["actie"] == "laden"
        assert laad_met_stijging[0]["batterij_temp_na_c"] > laad_zonder_stijging[0]["batterij_temp_na_c"]
        assert ontlaad_met_stijging[0]["actie"] == "ontladen"
        assert ontlaad_met_stijging[0]["batterij_temp_na_c"] > ontlaad_zonder_stijging[0]["batterij_temp_na_c"]

    def test_thermisch_model_beperkt_laden_bij_warme_hoge_soc(self):
        slots_zonder_penalty = maak_slots([0.01, 1.00])
        slots_met_penalty = maak_slots([0.01, 1.00])
        for slot in slots_zonder_penalty + slots_met_penalty:
            slot["buiten_temp_c"] = 40.0

        accu = maak_accu(huidig_kwh=1.8, max_kwh=2.4)
        zonder_penalty = los_dp_op(
            slots_zonder_penalty,
            accu,
            plateau_spreiding=False,
            batterij_temp_start_c=40.0,
            temp_limiet_c=35.0,
            temp_penalty_factor=0.0,
        )
        met_penalty = los_dp_op(
            slots_met_penalty,
            accu,
            plateau_spreiding=False,
            batterij_temp_start_c=40.0,
            temp_limiet_c=35.0,
            temp_penalty_factor=1.0,
        )

        assert zonder_penalty[0]["soc_na_pct"] > 80.0
        assert met_penalty[0]["soc_na_pct"] <= 80.0
        assert met_penalty[0]["vermogen_w"] < zonder_penalty[0]["vermogen_w"]

    def test_overtemp_penalty_blijft_zichtbaar_bij_kleine_overschrijding(self):
        slots = maak_slots([0.10])
        slots[0]["buiten_temp_c"] = 35.01

        schema = los_dp_op(
            slots,
            maak_accu(huidig_kwh=2.0, max_kwh=2.4, max_ontlaad_w=0.0),
            plateau_spreiding=False,
            batterij_temp_start_c=35.01,
            warmte_afkoeling_halveringstijd_h=1.0,
            temp_limiet_c=35.0,
            temp_penalty_factor=1.0,
        )

        assert schema[0]["temp_limiet_actief"] is True
        assert schema[0]["overtemp_penalty_eur"] > 0.0
        assert schema[0]["temp_penalty_eur"] == schema[0]["overtemp_penalty_eur"]

    def test_overtemp_penalty_werkt_onder_soc_drempel_met_lage_soc_limiet(self):
        slots = maak_slots([0.10])
        slots[0]["buiten_temp_c"] = 46.0

        schema = los_dp_op(
            slots,
            maak_accu(huidig_kwh=0.2, max_kwh=2.4, max_laad_w=0.0, max_ontlaad_w=0.0),
            plateau_spreiding=False,
            batterij_temp_start_c=46.0,
            warmte_afkoeling_halveringstijd_h=1.0,
            temp_limiet_c=40.0,
            temp_limiet_lage_soc_c=45.0,
            temp_penalty_factor=1.0,
        )

        assert schema[0]["soc_na_pct"] < 80.0
        assert schema[0]["temp_limiet_c"] == 45.0
        assert schema[0]["temp_limiet_hoge_soc_c"] == 40.0
        assert schema[0]["temp_limiet_lage_soc_c"] == 45.0
        assert schema[0]["temp_limiet_actief"] is True
        assert schema[0]["overtemp_penalty_eur"] > 0.0

    def test_overtemp_penalty_weegt_100_soc_zwaarder_dan_90_soc(self):
        slots_90 = maak_slots([0.10])
        slots_100 = maak_slots([0.10])
        for slot in slots_90 + slots_100:
            slot["buiten_temp_c"] = 41.0

        schema_90 = los_dp_op(
            slots_90,
            maak_accu(huidig_kwh=2.4 * 0.90, max_kwh=2.4, max_laad_w=0.0, max_ontlaad_w=0.0),
            plateau_spreiding=False,
            batterij_temp_start_c=41.0,
            warmte_afkoeling_halveringstijd_h=1.0,
            temp_limiet_c=40.0,
            temp_penalty_factor=1.0,
            temp_penalty_100_soc_factor=2.0,
        )
        schema_100 = los_dp_op(
            slots_100,
            maak_accu(huidig_kwh=2.4, max_kwh=2.4, max_laad_w=0.0, max_ontlaad_w=0.0),
            plateau_spreiding=False,
            batterij_temp_start_c=41.0,
            warmte_afkoeling_halveringstijd_h=1.0,
            temp_limiet_c=40.0,
            temp_penalty_factor=1.0,
            temp_penalty_100_soc_factor=2.0,
        )

        assert schema_90[0]["temp_penalty_soc_factor"] == 1.0
        assert schema_100[0]["temp_penalty_soc_factor"] == 2.0
        assert schema_100[0]["overtemp_penalty_eur"] == pytest.approx(
            schema_90[0]["overtemp_penalty_eur"] * 2.0
        )

    def test_soc_multiplier_gebruikt_echte_soc_schaal(self):
        slots = maak_slots([0.10])
        slots[0]["buiten_temp_c"] = 41.0

        schema = los_dp_op(
            slots,
            maak_accu(huidig_kwh=2.4, max_kwh=2.4, max_laad_w=0.0, max_ontlaad_w=0.0),
            plateau_spreiding=False,
            batterij_temp_start_c=41.0,
            warmte_afkoeling_halveringstijd_h=1.0,
            temp_limiet_c=40.0,
            temp_penalty_factor=1.0,
            temp_penalty_100_soc_factor=2.0,
            soc_min_pct=0.0,
            soc_max_pct=90.0,
        )

        assert schema[0]["soc_na_pct"] == 100.0
        assert schema[0]["temp_penalty_soc_factor"] == 1.0

    def test_hoge_soc_verblijf_penalty_start_boven_90_soc(self):
        schema_90 = los_dp_op(
            maak_slots([0.10]),
            maak_accu(huidig_kwh=2.4 * 0.90, max_kwh=2.4, max_laad_w=0.0, max_ontlaad_w=0.0),
            plateau_spreiding=False,
            hoge_soc_verblijf_penalty_factor=1.0,
        )
        schema_100 = los_dp_op(
            maak_slots([0.10]),
            maak_accu(huidig_kwh=2.4, max_kwh=2.4, max_laad_w=0.0, max_ontlaad_w=0.0),
            plateau_spreiding=False,
            hoge_soc_verblijf_penalty_factor=1.0,
        )

        assert schema_90[0]["hoge_soc_verblijf_penalty_eur"] == 0.0
        assert schema_100[0]["hoge_soc_verblijf_penalty_eur"] > 0.0
        assert schema_100[0]["soc_verblijf_penalty_eur"] == schema_100[0]["hoge_soc_verblijf_penalty_eur"]

    def test_lage_soc_verblijf_penalty_start_onder_10_soc(self):
        schema_10 = los_dp_op(
            maak_slots([0.10]),
            maak_accu(huidig_kwh=2.4 * 0.10, max_kwh=2.4, max_laad_w=0.0, max_ontlaad_w=0.0),
            plateau_spreiding=False,
            lage_soc_verblijf_penalty_factor=1.0,
        )
        schema_5 = los_dp_op(
            maak_slots([0.10]),
            maak_accu(huidig_kwh=2.4 * 0.05, max_kwh=2.4, max_laad_w=0.0, max_ontlaad_w=0.0),
            plateau_spreiding=False,
            lage_soc_verblijf_penalty_factor=1.0,
        )

        assert schema_10[0]["lage_soc_verblijf_penalty_eur"] == 0.0
        assert schema_5[0]["lage_soc_verblijf_penalty_eur"] > 0.0
        assert schema_5[0]["soc_verblijf_penalty_eur"] == schema_5[0]["lage_soc_verblijf_penalty_eur"]

    def test_soc_verblijf_penalty_factor_nul_schakelt_uit(self):
        schema = los_dp_op(
            maak_slots([0.10]),
            maak_accu(huidig_kwh=2.4, max_kwh=2.4, max_laad_w=0.0, max_ontlaad_w=0.0),
            plateau_spreiding=False,
            hoge_soc_verblijf_penalty_factor=0.0,
            lage_soc_verblijf_penalty_factor=0.0,
        )

        assert schema[0]["soc_verblijf_penalty_eur"] == 0.0
        assert schema[0]["hoge_soc_verblijf_penalty_eur"] == 0.0
        assert schema[0]["lage_soc_verblijf_penalty_eur"] == 0.0


# ── DERATING EFFECT ───────────────────────────────────────────────────────────

class TestDeratingEffect:
    def test_derating_laat_dp_kleinere_vermogensstap_kiezen(self):
        """
        Bij bijna volle accu verlaagt bereken_derating() het verwachte
        laadvermogen. los_dp_op() mag nu een kleinere vermogensstap kiezen,
        omdat vermogen_w onderdeel van de DP-keuze is.
        """
        accu   = maak_accu(huidig_kwh=2.4 * 0.95, max_kwh=2.4)  # 95 % SoC
        schema = los_dp_op(maak_slots([0.01, 1.00]), accu, plateau_spreiding=False)

        assert schema[0]["actie"] == "laden"
        assert 100 <= schema[0]["vermogen_w"] < accu.max_laad_w
        assert schema[0]["verwacht_vermogen_w"] < accu.max_laad_w

    def test_derating_niet_actief_bij_lage_soc(self):
        """
        Bij lage SoC (< 80 %) is de derating-factor 1.0. We verifiëren dit
        direct via bereken_derating, los van het volledige DP-schema.
        """
        assert bereken_derating(2.4 * 0.50, 2.4) == pytest.approx(1.0)
        assert bereken_derating(2.4 * 0.75, 2.4) == pytest.approx(1.0)


# ── ACTIEF SLOT ───────────────────────────────────────────────────────────────

class TestActiefSlotVermogen:
    def test_lopend_laadslot_gebruikt_soc_na_als_doel(self):
        """
        Om 14:11 moet het 14:00-slot naar soc_na_kwh=4.7 sturen.
        Het lagere tussendoel 4.05 mag het laadvermogen niet beperken.
        """
        nu = datetime.fromisoformat("2026-05-21T14:11:42.812815+02:00")
        schema = [{
            "start": "2026-05-21T14:00:00+02:00",
            "end": "2026-05-21T15:00:00+02:00",
            "prijs_ct": 14.467,
            "actie": "laden",
            "vermogen_w": 1550,
            "soc_voor_kwh": 2.9,
            "soc_na_kwh": 4.7,
            "winst_eur": -0.2825,
        }]
        accu = maak_accu(huidig_kwh=2.916, max_kwh=5.146, eta=0.922)

        corrigeer_actief_slot_vermogen(schema, accu, nu)

        assert schema[0]["actie"] == "laden"
        assert schema[0]["vermogen_w"] == 2400
        assert schema[0]["doel_soc_kwh"] == 4.7
        assert schema[0]["actuele_soc_kwh"] == 2.916
        assert schema[0]["geplande_actie"] == "laden"

    def test_lopend_laadslot_beperkt_vermogen_als_doel_lager_is(self):
        """
        Als soc_na_kwh haalbaar is met minder dan max_laad_w, gebruikt het actieve
        slot het berekende vermogen in plaats van altijd max_laad_w.
        """
        nu = datetime.fromisoformat("2026-05-21T15:00:00+02:00")
        schema = [{
            "start": "2026-05-21T15:00:00+02:00",
            "end": "2026-05-21T16:00:00+02:00",
            "prijs_ct": 17.289,
            "actie": "laden",
            "vermogen_w": 2400,
            "soc_voor_kwh": 4.7,
            "soc_na_kwh": 5.15,
            "winst_eur": -0.0844,
        }]
        accu = maak_accu(huidig_kwh=4.7, max_kwh=5.146, eta=0.922)

        corrigeer_actief_slot_vermogen(schema, accu, nu)

        assert schema[0]["actie"] == "laden"
        assert schema[0]["vermogen_w"] == 488
        assert schema[0]["doel_soc_kwh"] == 5.15

    def test_lopend_laadslot_verhoogt_doel_bij_voorsprong_en_duurder_vervolg(self):
        """
        Als de actuele SoC voorloopt en het volgende laadslot duurder is, mag het
        actieve slot extra energie naar voren halen.
        """
        nu = datetime.fromisoformat("2026-05-21T14:30:00+02:00")
        schema = [
            {
                "start": "2026-05-21T14:00:00+02:00",
                "end": "2026-05-21T15:00:00+02:00",
                "prijs_ct": 14.467,
                "actie": "laden",
                "vermogen_w": 1550,
                "soc_voor_kwh": 2.9,
                "soc_na_kwh": 4.7,
                "winst_eur": -0.2825,
            },
            {
                "start": "2026-05-21T15:00:00+02:00",
                "end": "2026-05-21T16:00:00+02:00",
                "prijs_ct": 17.289,
                "actie": "laden",
                "vermogen_w": 488,
                "soc_voor_kwh": 4.7,
                "soc_na_kwh": 5.15,
                "winst_eur": -0.0844,
            },
        ]
        accu = maak_accu(huidig_kwh=4.0, max_kwh=5.146, eta=0.922)

        corrigeer_actief_slot_vermogen(schema, accu, nu)

        assert schema[0]["actie"] == "laden"
        assert schema[0]["vermogen_w"] == 2400
        assert schema[0]["doel_soc_kwh"] == pytest.approx(5.106, abs=0.001)

    def test_lopend_laadslot_verhoogt_doel_niet_als_vervolg_goedkoper_is(self):
        """
        Als het volgende laadslot goedkoper is, blijft het actieve slot bij het
        eigen soc_na_kwh-doel.
        """
        nu = datetime.fromisoformat("2026-05-21T14:30:00+02:00")
        schema = [
            {
                "start": "2026-05-21T14:00:00+02:00",
                "end": "2026-05-21T15:00:00+02:00",
                "prijs_ct": 15.0,
                "actie": "laden",
                "vermogen_w": 1550,
                "soc_voor_kwh": 2.9,
                "soc_na_kwh": 4.7,
                "winst_eur": -0.2825,
            },
            {
                "start": "2026-05-21T15:00:00+02:00",
                "end": "2026-05-21T16:00:00+02:00",
                "prijs_ct": 13.0,
                "actie": "laden",
                "vermogen_w": 488,
                "soc_voor_kwh": 4.7,
                "soc_na_kwh": 5.15,
                "winst_eur": -0.0844,
            },
        ]
        accu = maak_accu(huidig_kwh=4.0, max_kwh=5.146, eta=0.922)

        corrigeer_actief_slot_vermogen(schema, accu, nu)

        assert schema[0]["actie"] == "laden"
        assert schema[0]["vermogen_w"] == 1518
        assert schema[0]["doel_soc_kwh"] == 4.7

    def test_lopend_ontlaadslot_verlaagt_doel_bij_duurder_actief_slot(self):
        """
        Als het actieve ontlaadslot duurder is dan het volgende ontlaadslot, mag
        het actieve slot extra energie naar voren halen.
        """
        nu = datetime.fromisoformat("2026-05-21T20:06:28.030751+02:00")
        schema = [
            {
                "start": "2026-05-21T20:00:00+02:00",
                "end": "2026-05-21T21:00:00+02:00",
                "prijs_ct": 37.08,
                "actie": "ontladen",
                "vermogen_w": 1994,
                "soc_voor_kwh": 3.95,
                "soc_na_kwh": 2.0,
                "winst_eur": 0.6655,
            },
            {
                "start": "2026-05-21T21:00:00+02:00",
                "end": "2026-05-21T22:00:00+02:00",
                "prijs_ct": 35.607,
                "actie": "ontladen",
                "vermogen_w": 1841,
                "soc_voor_kwh": 2.0,
                "soc_na_kwh": 0.35,
                "winst_eur": 0.6554,
            },
        ]
        accu = maak_accu(huidig_kwh=3.933, max_kwh=5.13, eta=0.92)

        corrigeer_actief_slot_vermogen(schema, accu, nu)

        assert schema[0]["actie"] == "ontladen"
        assert schema[0]["vermogen_w"] == 2400
        assert schema[0]["doel_soc_kwh"] == pytest.approx(1.605, abs=0.001)

    def test_lopend_ontlaadslot_verlaagt_doel_niet_als_vervolg_duurder_is(self):
        """
        Als het volgende ontlaadslot duurder is, blijft het actieve slot bij het
        eigen soc_na_kwh-doel.
        """
        nu = datetime.fromisoformat("2026-05-21T20:00:00+02:00")
        schema = [
            {
                "start": "2026-05-21T20:00:00+02:00",
                "end": "2026-05-21T21:00:00+02:00",
                "prijs_ct": 35.0,
                "actie": "ontladen",
                "vermogen_w": 1840,
                "soc_voor_kwh": 4.0,
                "soc_na_kwh": 2.0,
                "winst_eur": 0.64,
            },
            {
                "start": "2026-05-21T21:00:00+02:00",
                "end": "2026-05-21T22:00:00+02:00",
                "prijs_ct": 37.0,
                "actie": "ontladen",
                "vermogen_w": 1840,
                "soc_voor_kwh": 2.0,
                "soc_na_kwh": 0.35,
                "winst_eur": 0.68,
            },
        ]
        accu = maak_accu(huidig_kwh=4.0, max_kwh=5.13, eta=0.92)

        corrigeer_actief_slot_vermogen(schema, accu, nu)

        assert schema[0]["actie"] == "ontladen"
        assert schema[0]["vermogen_w"] == 1840
        assert schema[0]["doel_soc_kwh"] == 2.0


# ── 15-MINUTEN SLOTS ─────────────────────────────────────────────────────────

class TestKwartierSlots:
    def test_laden_en_ontladen_in_kwartierslots(self):
        """
        4 goedkope kwartierslots → 4 dure kwartierslots:
        het algoritme laadt gedurende het goedkope blok en ontlaadt daarna.
        """
        prijzen = [0.05] * 4 + [0.30] * 4
        schema  = los_dp_op(maak_slots(prijzen, duur_h=0.25), maak_accu(huidig_kwh=0.0))
        for s in schema[:4]:
            assert s["actie"] == "laden"
        for s in schema[4:]:
            assert s["actie"] == "ontladen"

    def test_kwartierslots_minder_energie_per_slot(self):
        """
        Een kwartierslot verplaatst 1/4 van de energie van een uurslot (zelfde vermogen).
        De totale winst van twee kwartierslots is kleiner dan van twee uurslots
        bij dezelfde prijzen.
        """
        prijzen = [0.05, 0.30]
        accu    = maak_accu(huidig_kwh=0.0)

        winst_uur      = totale_winst(los_dp_op(maak_slots(prijzen, duur_h=1.00), accu))
        winst_kwartier = totale_winst(los_dp_op(maak_slots(prijzen, duur_h=0.25), accu))

        assert 0 < winst_kwartier < winst_uur

    def test_meer_cycli_mogelijk_bij_hogere_resolutie(self):
        """
        Met kwartierdata zijn er 4× zoveel slots, waardoor het algoritme
        fijnmaziger kan plannen en potentieel meer cycli kan benutten.
        """
        # Patroon: 4 goedkoop → 4 duur → 4 goedkoop → 4 duur (2 cycli van elk 1 uur)
        prijzen = ([0.04] * 4 + [0.30] * 4) * 2
        schema  = los_dp_op(maak_slots(prijzen, duur_h=0.25), maak_accu(huidig_kwh=0.0))
        n_ontlaad = sum(1 for s in schema if s["actie"] == "ontladen")
        # Minimaal één ontlaadblok; met genoeg capaciteit twee blokken
        assert n_ontlaad >= 4  # minstens 4 kwartierslots ontladen (= 1 volledige cyclus)

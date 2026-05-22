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
    SOC_STAP_KWH,
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


class TestVermogenAfronding:
    def test_rondt_omhoog_op_25_watt_stappen(self):
        assert rond_vermogen_omhoog(1, 2400) == 25
        assert rond_vermogen_omhoog(2397, 2400) == 2400
        assert rond_vermogen_omhoog(2400, 2400) == 2400

    def test_rondt_nooit_boven_maximum(self):
        assert rond_vermogen_omhoog(2397, 2398) == 2398
        assert rond_vermogen_omhoog(0, 2400) == 0


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

    def test_exact_gelijke_waarde_kiest_rust(self):
        """
        Bij exact gelijke waarde tussen rust en laden kiest los_dp_op() rust.

        Een prijs van 0 ct/kWh maakt laden financieel gelijk aan rust. De
        strategie mag dan geen onnodige laadactie plannen.
        """
        schema = los_dp_op(maak_slots([0.0]), maak_accu(huidig_kwh=0.0))
        assert schema[0]["actie"] == "rust"


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
        schema = los_dp_op(maak_slots([0.10, 0.11]), maak_accu(huidig_kwh=0.0, eta=0.949))
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

    def test_plateau_spreidt_over_bijna_gelijke_prijzen(self):
        """
        De standaard plateau_drempel_ct is 2 ct.

        Een laadslot van 10.0 ct/kWh mag daarom wel worden uitgesmeerd naar een
        naastliggend rustslot van 10.1 ct/kWh.
        """
        schema = los_dp_op(
            maak_slots([0.100, 0.101, 0.500]),
            maak_accu(huidig_kwh=0.0, max_kwh=2.4, eta=1.0),
        )

        assert acties(schema) == ["laden", "laden", "ontladen"]
        assert schema[0]["vermogen_w"] == 1200
        assert schema[1]["vermogen_w"] == 1200

    def test_plateau_spreidt_wel_over_exact_gelijke_prijzen(self):
        """
        Exact gelijke laadprijzen blijven een plateau.

        los_dp_op() mag één volle laadactie dan over twee gelijke goedkope uren
        verdelen.
        """
        schema = los_dp_op(
            maak_slots([0.100, 0.100, 0.500]),
            maak_accu(huidig_kwh=0.0, max_kwh=2.4, eta=1.0),
        )

        assert acties(schema) == ["laden", "laden", "ontladen"]
        assert schema[0]["vermogen_w"] == 1200
        assert schema[1]["vermogen_w"] == 1200

    def test_ontlaadplateau_spreidt_over_bijna_gelijke_rustslots(self):
        """
        Ontladen gebruikt ook bijna gelijke prijsuren als plateau.

        Een volle accu mag daarom niet alles op 50.0 ct/kWh ontladen als het
        naastliggende uur 49.9 ct/kWh is.
        """
        schema = los_dp_op(
            maak_slots([0.500, 0.499]),
            maak_accu(huidig_kwh=2.4, max_kwh=2.4, eta=1.0),
        )

        assert acties(schema) == ["ontladen", "ontladen"]
        assert schema[0]["vermogen_w"] == 1200
        assert schema[1]["vermogen_w"] == 1200
        assert schema[1]["soc_na_kwh"] == pytest.approx(0.0)


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
        slot heeft nog ongeveer 20,5 minuten. Met de resterende duur kiest
        los_dp_op() laden voor het actieve slot en spreidt los_dp_op() bijna
        gelijke latere laadprijzen weer als plateau.
        """
        slots = maak_slots_vanaf_iso(SCENARIO_18_MEI_SLOTS_CT)
        slots[0]["duration_h"] = 20.5 / 60.0

        schema = los_dp_op(
            slots,
            maak_accu(huidig_kwh=2.745, max_kwh=5.187, eta=0.922),
            min_spread_ct_per_kwh=8.0,
        )

        assert schema[0]["actie"] == "laden"
        assert schema[1]["actie"] == "laden"
        assert schema[2]["actie"] == "laden"
        assert schema[2]["soc_na_kwh"] == pytest.approx(3.5, abs=0.01)


# ── LADEN BIJ HOGE SOC ───────────────────────────────────────────────────────

class TestLadenBijHogeSoc:
    def test_bijna_volle_accu_wordt_alleen_door_resterende_ruimte_beperkt(self):
        """
        Het algoritme voorspelt geen BMS-begrenzing. Bij hoge SoC beperkt alleen
        de resterende accuruimte de laadopdracht.
        """
        accu   = maak_accu(huidig_kwh=2.4 * 0.95, max_kwh=2.4)  # 95 % SoC
        schema = los_dp_op(maak_slots([0.01, 1.00]), accu)

        assert schema[0]["actie"] == "laden"
        assert schema[0]["soc_na_kwh"] == pytest.approx(2.4, abs=0.01)
        assert schema[0]["vermogen_w"] == schema[0]["verwacht_vermogen_w"]


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

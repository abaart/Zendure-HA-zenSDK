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
    bereken_derating,
    corrigeer_actief_slot_vermogen,
    los_dp_op,
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

    def test_eerste_soc_is_huidig(self):
        """Het eerste slot begint bij de opgegeven huidige SoC."""
        accu   = maak_accu(huidig_kwh=1.5)
        schema = los_dp_op(maak_slots([0.10]), accu)
        assert schema[0]["soc_voor_kwh"] == pytest.approx(accu.huidig_kwh, abs=SOC_STAP_KWH)


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


# ── DERATING EFFECT ───────────────────────────────────────────────────────────

class TestDeratingEffect:
    def test_derating_verlaagt_effectief_vermogen(self):
        """
        Bij bijna volle accu (95 % SoC) is de derating-factor 0.40.
        Het gerapporteerde laadvermogen moet dus lager zijn dan het maximum.
        """
        accu   = maak_accu(huidig_kwh=2.4 * 0.95, max_kwh=2.4)  # 95 % SoC
        schema = los_dp_op(maak_slots([0.05, 0.30]), accu)
        if schema[0]["actie"] == "laden":
            assert schema[0]["vermogen_w"] < accu.max_laad_w

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

"""
Unit tests voor AppDaemon-specifieke strategie-logica.

De tests gebruiken een kleine fake voor appdaemon.plugins.hass.hassapi, zodat
we DynamischHandelen kunnen importeren zonder Home Assistant of AppDaemon.
"""

import math
import sys
import types
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "appdaemon" / "apps"))


def _installeer_fake_appdaemon() -> None:
    appdaemon = types.ModuleType("appdaemon")
    plugins = types.ModuleType("appdaemon.plugins")
    hass = types.ModuleType("appdaemon.plugins.hass")
    hassapi = types.ModuleType("appdaemon.plugins.hass.hassapi")

    class FakeHass:
        pass

    hassapi.Hass = FakeHass
    sys.modules.setdefault("appdaemon", appdaemon)
    sys.modules.setdefault("appdaemon.plugins", plugins)
    sys.modules.setdefault("appdaemon.plugins.hass", hass)
    sys.modules.setdefault("appdaemon.plugins.hass.hassapi", hassapi)


_installeer_fake_appdaemon()

from dynamisch_handelen import (  # noqa: E402
    DynamischHandelen,
    bouw_grafiek_slots,
    haal_grafiek_slots_uit_history_items,
)


def _history_item(state: str, tijd: str) -> dict:
    return {
        "state": state,
        "last_changed": tijd,
        "last_updated": tijd,
    }


def _maak_app(
    states: dict[str, object] | None = None,
    history: dict[str, list[dict]] | None = None,
    history_calls: list[dict] | None = None,
) -> DynamischHandelen:
    app = object.__new__(DynamischHandelen)
    states = states or {}
    history = history or {}

    def get_state(entity: str, attribute: str | None = None):
        waarde = states.get(entity)
        if attribute is None:
            if isinstance(waarde, dict) and "state" in waarde:
                return waarde["state"]
            return waarde

        if attribute == "all":
            if isinstance(waarde, dict) and "attributes" in waarde:
                return waarde
            return {"state": waarde, "attributes": {}}

        if isinstance(waarde, dict):
            attributes = waarde.get("attributes", waarde)
            if isinstance(attributes, dict):
                return attributes.get(attribute)
        return None

    def get_history(*args, **kwargs):
        if history_calls is not None:
            history_calls.append({"args": args, "kwargs": kwargs})
        entity = kwargs.get("entity_id") or (args[0] if args else None)
        return history.get(entity, [])

    app.get_state = get_state
    app.get_history = get_history
    app.log = lambda *args, **kwargs: None
    return app


def _slot(start: datetime, duur_uren: float, label: str) -> dict:
    return {
        "start": start.isoformat(),
        "end": (start + timedelta(hours=duur_uren)).isoformat(),
        "label": label,
    }


def _bron_prijs_slot(start: datetime, duur_minuten: int, prijs: float) -> dict:
    return {
        "start": start.isoformat(),
        "end": (start + timedelta(minutes=duur_minuten)).isoformat(),
        "value": prijs,
    }


def _advies_slot(
    start: datetime,
    duur_uren: float,
    actie: str = "rust",
    voorspelde_temp: float = 25.0,
) -> dict:
    return {
        "start": start.isoformat(),
        "end": (start + timedelta(hours=duur_uren)).isoformat(),
        "actie": actie,
        "c_waarde": 0.1 if actie != "rust" else 0.0,
        "batterij_temp_na_c": voorspelde_temp,
        "warmte_penalty_eur": 0.0,
        "overtemp_penalty_eur": 0.0,
    }


def test_initialize_plans_strategy_once_per_hour_at_minute_55():
    app = _maak_app()
    geplande_taken = []
    app.run_hourly = lambda callback, start: geplande_taken.append((callback, start))
    app.listen_state = lambda *args, **kwargs: None
    app._zet_berekening_bezig = lambda bezig: None
    app._initialiseer_berekening_duur_sensor = lambda: None
    app._initialiseer_advies_sensor = lambda: None

    app.initialize()

    assert geplande_taken == [(app.bereken_strategie, time(0, 55, 0))]


class TestKwartierPrijsslots:
    def test_bewaart_kwartierprijzen_over_volledige_bronhorizon(self):
        start = (
            datetime.now().astimezone().replace(second=0, microsecond=0)
            + timedelta(minutes=15)
        )
        bron_entity = "sensor.nordpool_kwartier"
        prijzen = [0.05 + index / 1000 for index in range(40)]
        raw_today = [
            _bron_prijs_slot(start + timedelta(minutes=15 * index), 15, prijs)
            for index, prijs in enumerate(prijzen[:24])
        ]
        raw_tomorrow = [
            _bron_prijs_slot(start + timedelta(minutes=15 * index), 15, prijs)
            for index, prijs in enumerate(prijzen[24:], start=24)
        ]
        app = _maak_app({
            "input_text.dynamisch_nordpool_sensor": bron_entity,
            bron_entity: {
                "state": str(prijzen[0]),
                "attributes": {
                    "raw_today": raw_today,
                    "raw_tomorrow": raw_tomorrow,
                },
            },
        })

        resultaat = app._haal_prijsslots()

        assert len(resultaat) == 40
        assert [slot["price"] for slot in resultaat] == prijzen
        assert [slot["duration_h"] for slot in resultaat] == [0.25] * 40
        assert {slot["resolutie"] for slot in resultaat} == {"kwartierprijs"}
        assert resultaat[-1]["end"] == start + timedelta(hours=10)

    def test_leest_verschillende_kwartierprijzen_rechtstreeks_uit_bron(self):
        start = (
            datetime.now().astimezone().replace(second=0, microsecond=0)
            + timedelta(minutes=15)
        )
        bron_entity = "sensor.nordpool_kwh_nl_eur_3_09_0"
        raw_today = [
            _bron_prijs_slot(start + timedelta(minutes=15 * index), 15, prijs)
            for index, prijs in enumerate((0.05, 0.08, 0.21, 0.13))
        ]
        app = _maak_app({
            "input_text.dynamisch_nordpool_sensor": bron_entity,
            bron_entity: {
                "state": "0.05",
                "attributes": {"raw_today": raw_today, "raw_tomorrow": []},
            },
            "sensor.dynamisch_nordpool": {
                "state": "0.1175",
                "attributes": {
                    "raw_today": [_bron_prijs_slot(start, 60, 0.1175)],
                },
            },
        })

        resultaat = app._haal_prijsslots()

        assert [slot["price"] for slot in resultaat] == [0.05, 0.08, 0.21, 0.13]
        assert [slot["duration_h"] for slot in resultaat] == [0.25] * 4
        assert {slot["resolutie"] for slot in resultaat} == {"kwartierprijs"}
        assert {slot["prijs_bron"] for slot in resultaat} == {bron_entity}

    def test_leest_raw_today_en_raw_tomorrow(self):
        start = (
            datetime.now().astimezone().replace(second=0, microsecond=0)
            + timedelta(minutes=15)
        )
        bron_entity = "sensor.nordpool_kwartier"
        app = _maak_app({
            "input_text.dynamisch_nordpool_sensor": bron_entity,
            bron_entity: {
                "state": "0.10",
                "attributes": {
                    "raw_today": [_bron_prijs_slot(start, 15, 0.10)],
                    "raw_tomorrow": [_bron_prijs_slot(start + timedelta(days=1), 15, 0.20)],
                },
            },
        })

        resultaat = app._haal_prijsslots()

        assert [slot["price"] for slot in resultaat] == [0.10, 0.20]

    def test_gebruikt_geen_uurprijs_als_kwartierprijs(self):
        start = (
            datetime.now().astimezone().replace(second=0, microsecond=0)
            + timedelta(minutes=15)
        )
        bron_entity = "sensor.nordpool_uur"
        app = _maak_app({
            "input_text.dynamisch_nordpool_sensor": bron_entity,
            bron_entity: {
                "state": "0.10",
                "attributes": {
                    "raw_today": [_bron_prijs_slot(start, 60, 0.10)],
                    "raw_tomorrow": [],
                },
            },
        })

        assert app._haal_prijsslots() == []


class TestHistorischeStates:
    def test_haal_history_items_vraagt_volledige_attributen(self):
        history_calls = []
        app = _maak_app(
            history={"sensor.test": [_history_item("1.0", "2026-05-24T14:00:00+02:00")]},
            history_calls=history_calls,
        )

        items = app._haal_history_items(
            "sensor.test",
            dagen=2,
            volledige_attributen=True,
        )

        assert len(items) == 1
        assert history_calls[0]["kwargs"]["entity_id"] == "sensor.test"
        assert history_calls[0]["kwargs"]["days"] == 2
        assert history_calls[0]["kwargs"]["minimal_response"] is False
        assert history_calls[0]["kwargs"]["no_attributes"] is False
        assert history_calls[0]["kwargs"]["significant_changes_only"] is False

    def test_haal_history_items_laat_attributen_weg_voor_numerieke_history(self):
        history_calls = []
        app = _maak_app(
            history={"sensor.test": [_history_item("1.0", "2026-05-24T14:00:00+02:00")]},
            history_calls=history_calls,
        )

        app._haal_history_items("sensor.test", dagen=2)

        assert "minimal_response" not in history_calls[0]["kwargs"]
        assert "no_attributes" not in history_calls[0]["kwargs"]
        assert history_calls[0]["kwargs"]["significant_changes_only"] is False

    def test_historische_float_state_gebruikt_laatst_bekende_waarde_op_starttijd(self):
        app = _maak_app(
            states={"sensor.test": "9.0"},
            history={
                "sensor.test": [
                    _history_item("1.0", "2026-05-24T13:50:00+02:00"),
                    _history_item("2.0", "2026-05-24T14:00:00+02:00"),
                    _history_item("3.0", "2026-05-24T14:01:00+02:00"),
                ]
            },
        )

        waarde, bron = app._historische_float_state(
            "sensor.test",
            datetime.fromisoformat("2026-05-24T14:00:00+02:00"),
        )

        assert waarde == 2.0
        assert bron == "history"

    def test_haal_accustatus_gebruikt_history_op_dp_start(self):
        tijd = "2026-05-24T14:00:00+02:00"
        app = _maak_app(
            history={
                "sensor.zendure_2400_ac_indicatie_beschikbare_energie": [
                    _history_item("2.33", tijd),
                ],
                "sensor.zendure_2400_ac_indicatie_benodigde_energie": [
                    _history_item("1.20", tijd),
                ],
                "sensor.zendure_2400_ac_rte_totaal": [
                    _history_item("85", tijd),
                ],
                "input_number.zendure_2400_ac_max_oplaadvermogen": [
                    _history_item("2100", tijd),
                ],
                "input_number.zendure_2400_ac_max_ontlaadvermogen": [
                    _history_item("1500", tijd),
                ],
                "sensor.zendure_2400_ac_minimale_laadpercentage": [
                    _history_item("5", tijd),
                ],
                "sensor.zendure_2400_ac_maximale_laadpercentage": [
                    _history_item("95", tijd),
                ],
            }
        )

        accu, hw_min_pct, hw_max_pct, bronnen = app._haal_accustatus(
            datetime.fromisoformat(tijd)
        )
        eta = math.sqrt(0.85)

        assert accu.huidig_kwh == pytest.approx(2.33 / eta)
        assert accu.max_kwh == pytest.approx(2.33 / eta + 1.20 * eta)
        assert accu.max_laad_w == 2100
        assert accu.max_ontlaad_w == 1500
        assert hw_min_pct == 5
        assert hw_max_pct == 95
        assert bronnen["beschikbare_energie"] == "history"
        assert bronnen["benodigde_energie"] == "history"

    def test_haal_accustatus_gebruikt_actueel_als_historische_som_nul_is(self):
        tijd = "2026-08-12T21:45:00+02:00"
        logregels = []
        app = _maak_app(
            states={
                "sensor.zendure_2400_ac_indicatie_beschikbare_energie": "0",
                "sensor.zendure_2400_ac_indicatie_benodigde_energie": "5.55",
            },
            history={
                "sensor.zendure_2400_ac_indicatie_beschikbare_energie": [
                    _history_item("0", tijd),
                ],
                "sensor.zendure_2400_ac_indicatie_benodigde_energie": [
                    _history_item("0.0", tijd),
                ],
            },
        )
        app.log = lambda bericht, **kwargs: logregels.append((bericht, kwargs))

        accu, _, _, bronnen = app._haal_accustatus(datetime.fromisoformat(tijd))
        eta = math.sqrt(0.90)

        assert accu.huidig_kwh == 0.0
        assert accu.max_kwh == pytest.approx(5.55 * eta)
        assert bronnen["beschikbare_energie"] == "huidig_wegens_ongeldige_history"
        assert bronnen["benodigde_energie"] == "huidig_wegens_ongeldige_history"
        assert len(logregels) == 1
        assert "actuele energie-indicaties gebruikt" in logregels[0][0]
        assert logregels[0][1]["level"] == "WARNING"

    def test_haal_accustatus_houdt_nul_als_actuele_combinatie_onvolledig_is(self):
        tijd = "2026-08-12T21:45:00+02:00"
        app = _maak_app(
            states={
                "sensor.zendure_2400_ac_indicatie_beschikbare_energie": "0",
                "sensor.zendure_2400_ac_indicatie_benodigde_energie": "unavailable",
            },
            history={
                "sensor.zendure_2400_ac_indicatie_beschikbare_energie": [
                    _history_item("0", tijd),
                ],
                "sensor.zendure_2400_ac_indicatie_benodigde_energie": [
                    _history_item("0.0", tijd),
                ],
            },
        )

        accu, _, _, bronnen = app._haal_accustatus(datetime.fromisoformat(tijd))

        assert accu.max_kwh == 0.0
        assert bronnen["beschikbare_energie"] == "history"
        assert bronnen["benodigde_energie"] == "history"


class TestStrategieAdvies:
    def test_advies_gebruikt_actuele_slots_grafiek_als_history_geen_slots_heeft(self):
        nu = datetime.now().astimezone()
        slots = [
            _advies_slot(nu - timedelta(hours=10 - index), 1.0, "laden", 25.0)
            for index in range(8)
        ]
        temp_history = [
            _history_item("25.0", slot["end"])
            for slot in slots
        ]
        gepubliceerde_states = []
        app = _maak_app(
            states={
                "input_number.dynamisch_advies_analyse_dagen": "14",
                "sensor.dynamisch_handelsstrategie": {
                    "state": "1.0",
                    "attributes": {"slots_grafiek": slots},
                },
            },
            history={
                "sensor.dynamisch_handelsstrategie": [
                    {
                        "state": "1.0",
                        "attributes": {},
                        "last_changed": (nu - timedelta(hours=1)).isoformat(),
                    }
                ],
                "sensor.zendure_2400_ac_warmste_batterij_temperatuur": temp_history,
            },
        )
        app.set_state = lambda entity, state, attributes: gepubliceerde_states.append(
            {"entity": entity, "state": state, "attributes": attributes}
        )

        app.bereken_strategie_advies({"trigger": "test"})

        advies = gepubliceerde_states[-1]
        attributes = advies["attributes"]
        assert advies["entity"] == "sensor.dynamisch_strategie_advies"
        assert advies["state"] == "stabiel"
        assert attributes["geanalyseerde_slots"] == 8
        assert attributes["temperatuur_vergelijkingen"] == 8
        assert attributes["strategie_slots_uit_history"] == 0
        assert attributes["strategie_slots_uit_huidige_sensor"] == 8
        assert attributes["strategie_history_items_met_slots"] == 0


class TestStrategieConfig:
    def test_haal_standby_verbruik_gebruikt_helperwaarde(self):
        app = _maak_app(
            states={"input_number.dynamisch_standby_verbruik_w": "7.5"}
        )

        assert app._haal_standby_verbruik_w() == 7.5

    def test_haal_standby_verbruik_gebruikt_default_bij_onbekende_helper(self):
        app = _maak_app(
            states={"input_number.dynamisch_standby_verbruik_w": "unknown"}
        )

        assert app._haal_standby_verbruik_w() == 5.0

    def test_haal_standby_verbruik_blijft_niet_negatief(self):
        app = _maak_app(
            states={"input_number.dynamisch_standby_verbruik_w": "-2"}
        )

        assert app._haal_standby_verbruik_w() == 0.0


def test_strategie_input_numbers_hebben_geen_initial_waarde():
    package = (
        Path(__file__).parent.parent
        / "Dutch (NL) Integration"
        / "packages"
        / "zendure_local_nl.yaml"
    )
    tekst = package.read_text(encoding="utf-8")
    input_number_blok = tekst.split("input_number:", 1)[1].split("template:", 1)[0]

    assert "initial:" not in input_number_blok


def test_strategie_helpernamen_zijn_kort():
    package = (
        Path(__file__).parent.parent
        / "Dutch (NL) Integration"
        / "packages"
        / "zendure_local_nl.yaml"
    )
    tekst = package.read_text(encoding="utf-8")

    oude_labels = (
        "Dynamisch Packtemp",
        "Dynamisch C-waarde Penalty",
        "Dynamisch Actuele Buitentemperatuur Sensor",
        "Dynamisch Forecast Weather Entity",
        "Dynamisch Handelsstrategie Herberekenen",
        "Dynamisch Strategie Advies Herberekenen",
    )
    for label in oude_labels:
        assert label not in tekst

    nieuwe_labels = (
        "name: Strategie verversen",
        "name: Advies verversen",
        "name: Warmtestraf laden",
        "name: Afkoeling halveertijd",
        "name: Opwarming laden",
        "name: Max temp bij SoC >80%",
        "name: Straf boven temp-limiet",
        "name: Buitentemp sensor",
        "name: Weer forecast",
    )
    for label in nieuwe_labels:
        assert label in tekst


def test_strategie_dashboard_groepeert_korte_instellingen():
    dashboard = (
        Path(__file__).parent.parent
        / "Dutch (NL) Integration"
        / "dashboard_strategie.yaml"
    )
    tekst = dashboard.read_text(encoding="utf-8")

    for label in ("Packtemp", "packtemp", "Pack temp"):
        assert label not in tekst

    for titel in (
        "title: Advies",
        "title: Warmtemodel",
        "title: Temperatuurgrenzen",
        "title: SoC-sturing",
        "title: Weerdata",
        "title: Straf per slot",
    ):
        assert titel in tekst


def test_bouw_grafiek_slots_bewaart_laatste_zes_uur():
    nu = datetime(2026, 5, 24, 14, 0, tzinfo=timezone.utc)
    oud = _slot(nu - timedelta(hours=8), 1, "te-oud")
    recent = _slot(nu - timedelta(hours=5), 1, "recent")
    toekomst = _slot(nu + timedelta(hours=1), 1, "toekomst")

    grafiek_slots = bouw_grafiek_slots([oud, recent], [toekomst], nu)

    assert [slot["label"] for slot in grafiek_slots] == ["recent", "toekomst"]


def test_bouw_grafiek_slots_gebruikt_nieuwe_slot_bij_dubbele_tijd():
    nu = datetime(2026, 5, 24, 14, 0, tzinfo=timezone.utc)
    start = nu + timedelta(hours=1)
    vorig = _slot(start, 1, "vorig")
    nieuw = _slot(start, 1, "nieuw")

    grafiek_slots = bouw_grafiek_slots([vorig], [nieuw], nu)

    assert [slot["label"] for slot in grafiek_slots] == ["nieuw"]


def test_haal_grafiek_slots_uit_history_items_leest_recente_slots():
    nu = datetime(2026, 5, 24, 14, 0, tzinfo=timezone.utc)
    oud = _slot(nu - timedelta(hours=8), 1, "te-oud")
    recent_vroeg = _slot(nu - timedelta(hours=2), 1, "recent-vroeg")
    recent_laat = _slot(nu - timedelta(hours=2), 1, "recent-laat")
    te_laat_gepubliceerd = _slot(nu - timedelta(hours=1), 1, "na-slot-einde")

    history_items = [
        {
            "last_changed": (nu - timedelta(hours=7)).isoformat(),
            "attributes": {"slots": [oud]},
        },
        {
            "last_changed": (nu - timedelta(hours=2, minutes=30)).isoformat(),
            "attributes": {"slots": [recent_vroeg]},
        },
        {
            "last_changed": (nu - timedelta(hours=1, minutes=15)).isoformat(),
            "attributes": {"slots": [recent_laat]},
        },
        {
            "last_changed": (nu + timedelta(minutes=5)).isoformat(),
            "attributes": {"slots": [te_laat_gepubliceerd]},
        },
        {
            "last_changed": (nu - timedelta(hours=1)).isoformat(),
            "attributes": {"slots_grafiek": [recent_laat]},
        },
    ]

    grafiek_slots = haal_grafiek_slots_uit_history_items(history_items, nu)

    assert [slot["label"] for slot in grafiek_slots] == ["recent-laat"]

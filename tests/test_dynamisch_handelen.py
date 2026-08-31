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
from zoneinfo import ZoneInfo

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

import dynamisch_handelen as dynamisch_handelen_module  # noqa: E402
from dynamisch_handelen import (  # noqa: E402
    DEFAULT_MINIMALE_SPREAD_CT_PER_KWH,
    ECONOMISCHE_STRATEGIE_ENTITY,
    PLANNING_HORIZON_UREN,
    DynamischHandelen,
    bereken_penalty_totalen_eur,
    bereken_prijs_rte_winst_eur,
    bereken_thermische_meetstatistiek,
    bouw_wattwanneer_slots,
    bouw_grafiek_slots,
    formatteer_penalty_attributen,
    haal_grafiek_slots_uit_history_items,
    kalibreer_wattwanneer_prijzen,
)
from wattwanneer_forecast import WattWanneerCacheResultaat  # noqa: E402


def _cache_resultaat(
    records: list[dict] | None = None,
    *,
    status: str = "failure",
    fout: str | None = "testcache heeft geen forecast",
) -> WattWanneerCacheResultaat:
    return WattWanneerCacheResultaat(
        records=records or [],
        laatste_status=status,
        poging_uitgevoerd=False,
        laatste_poging_epoch=None,
        laatste_succes_epoch=None,
        volgende_poging_epoch=None,
        generated_at=(records or [{}])[0].get("generated_at"),
        fout=fout,
    )


class _VasteForecastCache:
    def __init__(self, resultaat: WattWanneerCacheResultaat) -> None:
        self.resultaat = resultaat
        self.nordpool_calls = []
        self.kalibratie_calls = []

    def haal(self, *, now_epoch: int) -> WattWanneerCacheResultaat:
        return self.resultaat

    def lees_status(self) -> WattWanneerCacheResultaat:
        return self.resultaat

    def bewaar_nordpool_prijzen(self, **kwargs):
        self.nordpool_calls.append(kwargs)
        return {
            "waargenomen_slots": len(kwargs["slots"]),
            "nieuwe_prijsversies": len(kwargs["slots"]),
        }

    def bewaar_prijskalibratie(self, **kwargs):
        self.kalibratie_calls.append(kwargs)
        return len(self.kalibratie_calls)


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
    forecast_resultaat: WattWanneerCacheResultaat | None = None,
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
    app.set_state = lambda *args, **kwargs: None
    app.args = {}
    app._wattwanneer_cache = _VasteForecastCache(
        forecast_resultaat or _cache_resultaat()
    )
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
    state_listeners = []
    app.run_hourly = lambda callback, start: geplande_taken.append((callback, start))
    app.listen_state = lambda callback, entity, **kwargs: state_listeners.append(entity)
    app._zet_berekening_bezig = lambda bezig: None
    app._initialiseer_berekening_duur_sensor = lambda: None
    app._initialiseer_advies_sensor = lambda: None

    app.initialize()

    assert geplande_taken == [(app.bereken_strategie, time(0, 55, 0))]
    assert "input_number.dynamisch_minimum_vermogen_w" in state_listeners
    assert "input_number.zendure_2400_ac_max_oplaadvermogen" in state_listeners
    assert "input_number.zendure_2400_ac_max_ontlaadvermogen" in state_listeners


class TestMinimaleSpread:
    def test_leest_persistente_helperwaarde(self):
        app = _maak_app({"input_number.dynamisch_minimale_spread": "1.0"})

        assert app._haal_minimale_spread() == 1.0

    @pytest.mark.parametrize("waarde", [None, "unknown", "unavailable", "ongeldig"])
    def test_gebruikt_twee_cent_bij_ontbrekende_of_ongeldige_helper(self, waarde):
        app = _maak_app({"input_number.dynamisch_minimale_spread": waarde})

        assert app._haal_minimale_spread() == DEFAULT_MINIMALE_SPREAD_CT_PER_KWH

    def test_staat_nul_toe_als_bewuste_gebruikerskeuze(self):
        app = _maak_app({"input_number.dynamisch_minimale_spread": "0"})

        assert app._haal_minimale_spread() == 0.0


def test_prijs_rte_winst_telt_geen_penalties_mee():
    schema = [
        {
            "winst_eur": -0.2,
            "warmte_penalty_eur": 5.0,
            "overtemp_penalty_eur": 7.0,
            "soc_verblijf_penalty_eur": 11.0,
        },
        {
            "winst_eur": 0.5,
            "warmte_penalty_eur": 13.0,
            "overtemp_penalty_eur": 17.0,
            "soc_verblijf_penalty_eur": 19.0,
        },
    ]

    assert bereken_prijs_rte_winst_eur(schema) == pytest.approx(0.3)


def test_penalty_totalen_splitsen_categorieen_zonder_dubbeltelling():
    schema = [
        {
            "actie": "rust",
            "geplande_actie": "laden",
            "warmte_penalty_eur": 0.1,
            "overtemp_penalty_eur": 0.2,
            "temp_penalty_eur": 99.0,
            "soc_verblijf_penalty_eur": 0.7,
            "hoge_soc_verblijf_penalty_eur": 0.3,
            "lage_soc_verblijf_penalty_eur": 0.4,
        },
        {
            "actie": "ontladen",
            "warmte_penalty_eur": 0.5,
            "temp_penalty_eur": 0.6,
            "soc_verblijf_penalty_eur": 0.1,
            "hoge_soc_verblijf_penalty_eur": 0.0,
            "lage_soc_verblijf_penalty_eur": 0.1,
        },
    ]

    assert bereken_penalty_totalen_eur(schema) == {
        "warmte_laden_eur": 0.1,
        "warmte_ontladen_eur": 0.5,
        "overtemp_eur": 0.8,
        "hoge_soc_verblijf_eur": 0.3,
        "lage_soc_verblijf_eur": 0.5,
        "totaal_eur": 2.2,
    }


def test_penalty_attributen_behouden_nulwaarden_als_decimale_tekst():
    assert formatteer_penalty_attributen({"totaal_eur": 1.25}) == {
        "penalty_totaal_eur": "1.250000",
        "warmte_penalty_laden_totaal_eur": "0.000000",
        "warmte_penalty_ontladen_totaal_eur": "0.000000",
        "overtemp_penalty_totaal_eur": "0.000000",
        "hoge_soc_verblijf_penalty_totaal_eur": "0.000000",
        "lage_soc_verblijf_penalty_totaal_eur": "0.000000",
    }


def test_economische_strategie_zet_keuzepenalties_uit(monkeypatch):
    ontvangen = {}

    def fake_los_dp_op(slots, accu, **kwargs):
        ontvangen["slots"] = slots
        ontvangen["accu"] = accu
        ontvangen["kwargs"] = kwargs
        return [{"actie": "rust"}]

    monkeypatch.setattr(dynamisch_handelen_module, "los_dp_op", fake_los_dp_op)
    app = _maak_app()
    slots = [{"price": 0.1}]
    accu = object()

    def annuleer_check():
        return False

    resultaat = app._bereken_economisch_schema(
        slots,
        accu,
        standby_verbruik_w=8.0,
        minimum_vermogen_w=225,
        hw_min_pct=5.0,
        hw_max_pct=95.0,
        annuleer_check=annuleer_check,
    )

    assert resultaat == [{"actie": "rust"}]
    assert ontvangen["slots"] is slots
    assert ontvangen["accu"] is accu
    assert ontvangen["kwargs"] == {
        "min_spread_ct_per_kwh": 0.0,
        "plateau_spreiding": False,
        "warmte_penalty_laden_factor": 0.0,
        "warmte_penalty_ontladen_factor": 0.0,
        "standby_verbruik_w": 8.0,
        "minimum_vermogen_w": 225,
        "batterij_temp_start_c": None,
        "temp_penalty_factor": 0.0,
        "temp_penalty_100_soc_factor": 1.0,
        "hoge_soc_verblijf_penalty_factor": 0.0,
        "lage_soc_verblijf_penalty_factor": 0.0,
        "soc_min_pct": 5.0,
        "soc_max_pct": 95.0,
        "annuleer_check": annuleer_check,
    }


def test_geen_prijsdata_publiceert_beide_strategiesensoren():
    app = _maak_app()
    app._berekening_generatie = 4
    app._haal_prijsslots = lambda: []
    gepubliceerd = []
    app.set_state = lambda entity, state, attributes: gepubliceerd.append(
        (entity, state, attributes)
    )

    app._bereken_strategie_impl({"trigger": "test"}, 4)

    assert [item[0] for item in gepubliceerd] == [
        "sensor.dynamisch_handelsstrategie",
        ECONOMISCHE_STRATEGIE_ENTITY,
    ]
    assert all(item[1] == "geen_data" for item in gepubliceerd)
    assert all(item[2]["penalty_totaal_eur"] == "0.000000" for item in gepubliceerd)


class TestKwartierPrijsslots:
    def test_bewaart_kwartierprijzen_en_rekent_72_uur_vooruit(self):
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
        echte_slots = [slot for slot in resultaat if not slot["prijs_is_fallback"]]
        fallback_slots = [slot for slot in resultaat if slot["prijs_is_fallback"]]

        assert len(echte_slots) == 40
        assert [slot["price"] for slot in echte_slots] == prijzen
        assert [slot["duration_h"] for slot in echte_slots] == [0.25] * 40
        assert {slot["resolutie"] for slot in echte_slots} == {"kwartierprijs"}
        horizon_start = resultaat[0]["start"]
        verwacht_einde = horizon_start + timedelta(hours=PLANNING_HORIZON_UREN)
        assert resultaat[-1]["end"] == verwacht_einde
        assert sum(slot["duration_h"] for slot in resultaat) == pytest.approx(
            (verwacht_einde - resultaat[0]["start"]).total_seconds() / 3600.0
        )
        assert fallback_slots
        assert all(slot["duration_h"] <= 1.0 for slot in fallback_slots)
        assert all(
            slot["price"] == pytest.approx(sum(prijzen) / len(prijzen))
            for slot in fallback_slots
        )

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
        echte_slots = [slot for slot in resultaat if not slot["prijs_is_fallback"]]

        assert [slot["price"] for slot in echte_slots] == [0.05, 0.08, 0.21, 0.13]
        assert [slot["duration_h"] for slot in echte_slots] == [0.25] * 4
        assert {slot["resolutie"] for slot in echte_slots} == {"kwartierprijs"}
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
        echte_slots = [slot for slot in resultaat if not slot["prijs_is_fallback"]]

        assert [slot["price"] for slot in echte_slots] == [0.10, 0.20]

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

    def test_fallback_gebruikt_laatste_96_geldige_bronkwartieren(self):
        nu = datetime.now().astimezone()
        horizon_start = nu.replace(
            minute=(nu.minute // 15) * 15,
            second=0,
            microsecond=0,
        )
        bron_start = horizon_start - timedelta(hours=24)
        prijzen = [0.01 + index / 1000 for index in range(100)]
        bron_entity = "sensor.nordpool_kwartier"
        raw_today = [
            _bron_prijs_slot(
                bron_start + timedelta(minutes=15 * index),
                15,
                prijs,
            )
            for index, prijs in enumerate(prijzen)
        ]
        app = _maak_app({
            "input_text.dynamisch_nordpool_sensor": bron_entity,
            bron_entity: {
                "state": str(prijzen[-1]),
                "attributes": {"raw_today": raw_today, "raw_tomorrow": []},
            },
        })

        resultaat = app._haal_prijsslots()
        fallback_slots = [slot for slot in resultaat if slot["prijs_is_fallback"]]
        verwachte_prijs = sum(prijzen[-96:]) / 96

        assert fallback_slots
        assert all(
            slot["price"] == pytest.approx(verwachte_prijs)
            for slot in fallback_slots
        )
        assert {
            slot["fallback_prijs_basis_slots"] for slot in fallback_slots
        } == {96}

    def test_verstreken_prijzen_mogen_72u_fallback_bepalen(self):
        nu = datetime.now().astimezone()
        horizon_start = nu.replace(
            minute=(nu.minute // 15) * 15,
            second=0,
            microsecond=0,
        )
        prijzen = [0.10, 0.20, 0.30, 0.40]
        bron_entity = "sensor.nordpool_kwartier"
        raw_today = [
            _bron_prijs_slot(
                horizon_start - timedelta(hours=1) + timedelta(minutes=15 * index),
                15,
                prijs,
            )
            for index, prijs in enumerate(prijzen)
        ]
        app = _maak_app({
            "input_text.dynamisch_nordpool_sensor": bron_entity,
            bron_entity: {
                "state": str(prijzen[-1]),
                "attributes": {"raw_today": raw_today, "raw_tomorrow": []},
            },
        })

        resultaat = app._haal_prijsslots()

        assert len(resultaat) == 72
        assert all(slot["prijs_is_fallback"] for slot in resultaat)
        assert all(slot["duration_h"] == pytest.approx(1.0) for slot in resultaat)
        assert all(slot["price"] == pytest.approx(0.25) for slot in resultaat)
        assert resultaat[0]["start"] == horizon_start
        assert resultaat[-1]["end"] == horizon_start + timedelta(hours=72)

    def test_horizon_is_exact_72_verstreken_uren(self):
        nu = datetime.now().astimezone()
        bekende_reeks_einde = (
            nu.replace(hour=0, minute=0, second=0, microsecond=0)
            + timedelta(days=1)
        )
        bron_entity = "sensor.nordpool_kwartier"
        raw_today = [
            _bron_prijs_slot(
                bekende_reeks_einde - timedelta(minutes=15 * (4 - index)),
                15,
                0.20 + index / 100,
            )
            for index in range(4)
        ]
        app = _maak_app({
            "input_text.dynamisch_nordpool_sensor": bron_entity,
            bron_entity: {
                "state": "0.20",
                "attributes": {"raw_today": raw_today, "raw_tomorrow": []},
            },
        })

        resultaat = app._haal_prijsslots()

        verstreken_uren = (
            resultaat[-1]["end"].astimezone(timezone.utc)
            - resultaat[0]["start"].astimezone(timezone.utc)
        ).total_seconds() / 3600.0
        assert verstreken_uren == pytest.approx(72.0)

    def test_kalibreert_wattwanneer_op_nordpool_prijsbasis(self):
        start = datetime(2026, 8, 23, 0, 0, tzinfo=ZoneInfo("Europe/Amsterdam"))
        records = []
        kwartier_slots = []
        for uur in range(8):
            ruwe_prijs = 0.05 + uur * 0.01
            records.append({
                "datetime": (start + timedelta(hours=uur)).strftime("%Y-%m-%d %H:%M"),
                "price_eur_kwh": ruwe_prijs,
                "source": "entsoe_day_ahead",
                "generated_at": "20260822_1320",
            })
            nordpool_prijs = 1.21 * ruwe_prijs + 0.13564
            for kwartier in range(4):
                kwartier_start = start + timedelta(hours=uur, minutes=15 * kwartier)
                kwartier_slots.append({
                    "start": kwartier_start,
                    "end": kwartier_start + timedelta(minutes=15),
                    "price": nordpool_prijs,
                    "duration_h": 0.25,
                })

        kalibratie = kalibreer_wattwanneer_prijzen(
            bouw_wattwanneer_slots(records),
            kwartier_slots,
        )

        assert kalibratie["meetpunten"] == 8
        assert kalibratie["factor"] == pytest.approx(1.21)
        assert kalibratie["opslag_eur_kwh"] == pytest.approx(0.13564)
        assert kalibratie["max_restfout_eur_kwh"] < 1e-9

    def test_voegt_modeluren_na_kwartieren_toe_tot_72_uur(self):
        tijdzone = ZoneInfo("Europe/Amsterdam")
        nu = datetime(2026, 8, 22, 12, 0, tzinfo=tijdzone)
        forecast_start = datetime(2026, 8, 23, 0, 0, tzinfo=tijdzone)
        bekende_reeks_einde = datetime(2026, 8, 24, 0, 0, tzinfo=tijdzone)
        records = []
        for uur in range(168):
            records.append({
                "datetime": (forecast_start + timedelta(hours=uur)).strftime("%Y-%m-%d %H:%M"),
                "price_eur_kwh": 0.05 + (uur % 24) / 1000,
                "source": "entsoe_day_ahead" if uur < 24 else "model",
                "generated_at": "20260822_1320",
            })

        kwartieren = []
        cursor = nu
        while cursor < bekende_reeks_einde:
            if cursor >= forecast_start:
                ruwe_prijs = 0.05 + cursor.hour / 1000
                prijs = 1.21 * ruwe_prijs + 0.13564
            else:
                prijs = 0.25
            kwartieren.append(_bron_prijs_slot(cursor, 15, prijs))
            cursor += timedelta(minutes=15)

        bron_entity = "sensor.nordpool_kwartier"
        app = _maak_app(
            {
                "input_text.dynamisch_nordpool_sensor": bron_entity,
                bron_entity: {
                    "state": "0.25",
                    "attributes": {
                        "raw_today": kwartieren[:48],
                        "raw_tomorrow": kwartieren[48:],
                    },
                },
            },
            forecast_resultaat=_cache_resultaat(
                records,
                status="success",
                fout=None,
            ),
        )
        app._huidige_planning_tijd = lambda: nu

        resultaat = app._haal_prijsslots()
        forecast_slots = [
            slot for slot in resultaat if slot.get("prijs_is_forecast") is True
        ]
        fallback_slots = [
            slot for slot in resultaat if slot.get("prijs_is_fallback") is True
        ]

        assert resultaat[0]["start"] == nu
        assert resultaat[-1]["end"] == nu + timedelta(hours=72)
        assert len(forecast_slots) == 36
        assert fallback_slots == []
        assert forecast_slots[0]["start"] == bekende_reeks_einde
        assert forecast_slots[0]["price"] == pytest.approx(
            1.21 * float(forecast_slots[0]["ruwe_forecast_prijs"]) + 0.13564
        )
        assert app._laatste_wattwanneer_metadata["status"] == "ok"
        assert app._laatste_wattwanneer_metadata["kalibratie_factor"] == pytest.approx(1.21)
        assert app._wattwanneer_cache.nordpool_calls[0]["price_entity"] == bron_entity
        assert len(app._wattwanneer_cache.nordpool_calls[0]["slots"]) == len(kwartieren)
        assert app._wattwanneer_cache.kalibratie_calls[0]["overlap_hours"] == 24
        assert app._laatste_wattwanneer_metadata["kalibratie_history_id"] == 1

    def test_historieopslagfout_maakt_forecast_status_rood(self):
        tijdzone = ZoneInfo("Europe/Amsterdam")
        nu = datetime(2026, 8, 22, 12, 0, tzinfo=tijdzone)
        forecast_start = datetime(2026, 8, 22, 0, 0, tzinfo=tijdzone)
        records = [
            {
                "datetime": (forecast_start + timedelta(hours=uur)).strftime(
                    "%Y-%m-%d %H:%M"
                ),
                "price_eur_kwh": 0.05 + (uur % 24) / 1000,
                "source": "entsoe_day_ahead" if uur < 48 else "model",
                "generated_at": "20260822_1320",
            }
            for uur in range(168)
        ]
        kwartieren = []
        cursor = forecast_start
        for _ in range(48 * 4):
            ruwe_prijs = 0.05 + (cursor.hour % 24) / 1000
            kwartieren.append(
                _bron_prijs_slot(cursor, 15, 1.21 * ruwe_prijs + 0.13564)
            )
            cursor += timedelta(minutes=15)
        bron_entity = "sensor.nordpool_kwartier"
        app = _maak_app(
            {
                "input_text.dynamisch_nordpool_sensor": bron_entity,
                bron_entity: {
                    "state": "0.25",
                    "attributes": {
                        "raw_today": kwartieren[:96],
                        "raw_tomorrow": kwartieren[96:],
                    },
                },
            },
            forecast_resultaat=_cache_resultaat(
                records,
                status="success",
                fout=None,
            ),
        )
        app._huidige_planning_tijd = lambda: nu

        def opslagfout(**kwargs):
            raise OSError("database is read-only")

        app._wattwanneer_cache.bewaar_nordpool_prijzen = opslagfout

        assert app._haal_prijsslots()
        assert app._laatste_wattwanneer_metadata["status"] == "fout"
        assert "database is read-only" in app._laatste_wattwanneer_metadata["fout"]
        assert "Nordpool-kwartierprijzen" in app._laatste_wattwanneer_metadata[
            "historie_fout"
        ]


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
    def test_thermische_meetstatistiek_houdt_ongewijzigde_recorder_state_vast(self):
        start = datetime(2026, 8, 26, 20, 0, tzinfo=timezone.utc)
        temperatuur_samples = []
        buiten_samples = []
        for index in range(97):
            tijd = start + timedelta(minutes=5 * index)
            duur_h = index * 5 / 60.0
            temperatuur_samples.append((tijd, 20.0 + 10.0 * (0.5 ** (duur_h / 6.0))))
            buiten_samples.append((tijd, 20.0))

        statistiek = bereken_thermische_meetstatistiek(
            [(start, 0.0)],
            temperatuur_samples,
            5.0,
            buiten_samples=buiten_samples,
            nu=start + timedelta(hours=8),
        )

        assert statistiek["afkoeling"]["blokken"] == 1
        assert statistiek["afkoeling"]["blokken_voldoende_duur"] == 1
        assert statistiek["afkoeling"]["metingen"] == 1
        assert statistiek["afkoeling"]["schatting_h"] == pytest.approx(6.0)

    def test_numerieke_samples_bewaart_unknown_als_onderbreking(self):
        start = datetime(2026, 8, 26, 20, 0, tzinfo=timezone.utc)
        app = _maak_app()

        samples = app._haal_numerieke_samples(
            [
                _history_item("0", start.isoformat()),
                _history_item("unavailable", (start + timedelta(hours=1)).isoformat()),
                _history_item("100", (start + timedelta(hours=2)).isoformat()),
            ],
            behoud_gaten=True,
        )

        assert [waarde for _, waarde in samples] == [0.0, None, 100.0]

    def test_thermische_meetstatistiek_schat_factor_uit_vermogen_en_temperatuur(self):
        start = datetime(2026, 8, 26, 8, 0, tzinfo=timezone.utc)
        vermogen_samples = []
        temperatuur_samples = []
        for index in range(13):
            tijd = start + timedelta(minutes=5 * index)
            vermogen_samples.append((tijd, 2500.0 if index < 12 else 0.0))
            temperatuur_samples.append((tijd, 20.0 + 5.0 * index / 12.0))

        statistiek = bereken_thermische_meetstatistiek(
            vermogen_samples,
            temperatuur_samples,
            5.0,
            nu=start + timedelta(minutes=65),
        )

        assert statistiek["status"] == "ok"
        assert statistiek["laden"]["blokken"] == 1
        assert statistiek["laden"]["stijgende_blokken"] == 1
        assert statistiek["laden"]["schatting_c_per_c2h"] == pytest.approx(20.0)
        assert statistiek["laden"]["mediaan_c_per_c2h"] == pytest.approx(20.0)
        assert statistiek["afkoeling"]["status"] == "geen_omgevingssensor"

    def test_thermische_meetstatistiek_schat_halvering_uit_rust_en_omgeving(self):
        start = datetime(2026, 8, 26, 8, 0, tzinfo=timezone.utc)
        vermogen_samples = []
        temperatuur_samples = []
        buiten_samples = []
        for index in range(13):
            tijd = start + timedelta(minutes=5 * index)
            duur_h = index * 5 / 60.0
            vermogen_samples.append((tijd, 0.0))
            temperatuur_samples.append((tijd, 20.0 + 8.0 * (0.5**duur_h)))
            buiten_samples.append((tijd, 20.0))

        statistiek = bereken_thermische_meetstatistiek(
            vermogen_samples,
            temperatuur_samples,
            5.0,
            buiten_samples=buiten_samples,
            nu=start + timedelta(hours=1),
        )

        assert statistiek["status"] == "ok"
        assert statistiek["afkoeling"]["status"] == "ok"
        assert statistiek["afkoeling"]["metingen"] == 1
        assert statistiek["afkoeling"]["schatting_h"] == pytest.approx(1.0)

    def test_afkoeling_accepteert_exact_dertig_minuten_zonder_floatafrondingsfout(self):
        start = datetime(2026, 8, 26, 8, 0, tzinfo=timezone.utc)
        vermogen_samples = []
        temperatuur_samples = []
        buiten_samples = []
        for index in range(7):
            tijd = start + timedelta(minutes=5 * index)
            duur_h = index * 5 / 60.0
            vermogen_samples.append((tijd, 0.0))
            temperatuur_samples.append((tijd, 20.0 + 8.0 * (0.5**duur_h)))
            buiten_samples.append((tijd, 20.0))

        statistiek = bereken_thermische_meetstatistiek(
            vermogen_samples,
            temperatuur_samples,
            5.0,
            buiten_samples=buiten_samples,
            nu=start + timedelta(minutes=30),
        )

        assert statistiek["afkoeling"]["blokken_voldoende_duur"] == 1
        assert statistiek["afkoeling"]["metingen"] == 1
        assert statistiek["afkoeling"]["schatting_h"] == pytest.approx(1.0)

    def test_afkoeling_staat_thermisch_kleine_vermogensbewegingen_toe(self):
        start = datetime(2026, 8, 26, 8, 0, tzinfo=timezone.utc)
        vermogen_samples = []
        temperatuur_samples = []
        buiten_samples = []
        for index in range(7):
            tijd = start + timedelta(minutes=5 * index)
            duur_h = index * 5 / 60.0
            vermogen_samples.append((tijd, 400.0))
            temperatuur_samples.append((tijd, 20.0 + 8.0 * (0.5**duur_h)))
            buiten_samples.append((tijd, 20.0))

        statistiek = bereken_thermische_meetstatistiek(
            vermogen_samples,
            temperatuur_samples,
            5.0,
            buiten_samples=buiten_samples,
            nu=start + timedelta(minutes=30),
        )

        assert statistiek["afkoeling"]["thermische_rust_max_w"] == 500
        assert statistiek["afkoeling"]["metingen"] == 1

    def test_afkoeling_sluit_vermogen_boven_tien_procent_c_uit(self):
        start = datetime(2026, 8, 26, 8, 0, tzinfo=timezone.utc)
        vermogen_samples = []
        temperatuur_samples = []
        buiten_samples = []
        for index in range(7):
            tijd = start + timedelta(minutes=5 * index)
            duur_h = index * 5 / 60.0
            vermogen_samples.append((tijd, 600.0))
            temperatuur_samples.append((tijd, 20.0 + 8.0 * (0.5**duur_h)))
            buiten_samples.append((tijd, 20.0))

        statistiek = bereken_thermische_meetstatistiek(
            vermogen_samples,
            temperatuur_samples,
            5.0,
            buiten_samples=buiten_samples,
            nu=start + timedelta(minutes=30),
        )

        assert statistiek["afkoeling"]["blokken"] == 0
        assert statistiek["afkoeling"]["metingen"] == 0

    def test_advies_gebruikt_buienradar_history_zonder_ingestelde_sensor(self):
        nu = datetime.now().astimezone()
        slot = _advies_slot(nu - timedelta(minutes=30), 0.25)
        slot["buiten_temp_c"] = 99.0
        app = _maak_app(
            states={
                "sensor.buienradar_temperature": "18.5",
                "sensor.dynamisch_handelsstrategie": {
                    "state": "0.0",
                    "attributes": {
                        "slots_grafiek": [slot],
                        "accu_max_kwh": 5.0,
                    },
                },
            },
            history={
                "sensor.buienradar_temperature": [
                    _history_item("18.5", (nu - timedelta(minutes=30)).isoformat()),
                ],
            },
        )
        app.args["default_buitentemperatuur_sensor"] = "sensor.buienradar_temperature"
        gepubliceerd = {}

        def set_state(entity_id, *, state, attributes):
            gepubliceerd[entity_id] = {"state": state, "attributes": attributes}

        app.set_state = set_state

        app.bereken_strategie_advies({"trigger": "test"})

        attributen = gepubliceerd["sensor.dynamisch_strategie_advies"]["attributes"]
        assert attributen["buitentemperatuur_bron"] == (
            "sensor.buienradar_temperature.state_history"
        )
        assert attributen["buitentemperatuur_samples"] == 1
        assert attributen["buitentemperatuur_sensor_samples"] == 1

    def test_ingestelde_buitentemperatuur_sensor_heeft_voorrang_op_buienradar(self):
        app = _maak_app(
            states={
                "input_text.dynamisch_buitentemperatuur_sensor": "sensor.temperatuur_tuin",
                "sensor.buienradar_temperature": "18.5",
            }
        )
        app.args["default_buitentemperatuur_sensor"] = "sensor.buienradar_temperature"

        assert app._haal_buitentemperatuur_sensor_entity() == "sensor.temperatuur_tuin"

    def test_advies_gebruikt_geen_forecastslots_als_historische_buitentemperatuur(self):
        nu = datetime.now().astimezone()
        slot = _advies_slot(nu - timedelta(minutes=30), 0.25)
        slot["buiten_temp_c"] = 18.5
        app = _maak_app(
            states={
                "sensor.dynamisch_handelsstrategie": {
                    "state": "0.0",
                    "attributes": {
                        "slots_grafiek": [slot],
                        "accu_max_kwh": 5.0,
                    },
                },
            }
        )
        gepubliceerd = {}
        app.set_state = lambda entity_id, *, state, attributes: gepubliceerd.update(
            {entity_id: {"state": state, "attributes": attributes}}
        )

        app.bereken_strategie_advies({"trigger": "test"})

        attributen = gepubliceerd["sensor.dynamisch_strategie_advies"]["attributes"]
        assert attributen["buitentemperatuur_bron"] == "niet_beschikbaar"
        assert attributen["buitentemperatuur_samples"] == 0

    def test_statistische_opwarming_is_onafhankelijk_van_huidige_helperwaarde(self):
        meetstatistiek = {
            "status": "ok",
            "laden": {
                "schatting_c_per_c2h": 42.0,
                "mediaan_c_per_c2h": 41.0,
                "p25_c_per_c2h": 38.0,
                "p75_c_per_c2h": 45.0,
                "blokken": 5,
                "stijgende_blokken": 4,
                "gemiddelde_c": 0.4,
                "betrouwbaarheid": "middel",
            },
            "ontladen": {},
            "afkoeling": {
                "status": "geen_omgevingssensor",
                "metingen": 0,
                "betrouwbaarheid": "laag",
            },
        }

        adviezen = []
        for huidige_waarde in (96.0, 63.0):
            app = _maak_app(
                states={
                    "input_number.dynamisch_warmte_stijging_laden_c_per_c2h": str(
                        huidige_waarde
                    ),
                }
            )
            adviezen.append(
                app._bouw_strategie_advies(
                    [],
                    [],
                    14,
                    meetstatistiek=meetstatistiek,
                )
            )

        assert [
            advies["statistische_schatting_warmte_stijging_laden_c_per_c2h"]
            for advies in adviezen
        ] == [42.0, 42.0]
        assert [
            advies["aanbevolen_warmte_stijging_laden_c_per_c2h"]
            for advies in adviezen
        ] == [42.0, 42.0]
        assert [
            advies["ingesteld_warmte_stijging_laden_c_per_c2h"]
            for advies in adviezen
        ] == [96.0, 63.0]

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
        assert advies["state"] == "te_weinig_meetdata"
        assert attributes["geanalyseerde_slots"] == 8
        assert attributes["temperatuur_vergelijkingen"] == 8
        assert attributes["strategie_slots_uit_history"] == 0
        assert attributes["strategie_slots_uit_huidige_sensor"] == 8
        assert attributes["strategie_history_items_met_slots"] == 0
        assert attributes["vermogen_samples"] == 0
        assert (
            attributes["statistische_schatting_warmte_stijging_laden_tekst"]
            == "Onvoldoende data"
        )


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

    def test_haal_minimum_vermogen_gebruikt_helperwaarde(self):
        app = _maak_app(
            states={"input_number.dynamisch_minimum_vermogen_w": "225"}
        )

        assert app._haal_minimum_vermogen_w() == 225

    def test_haal_minimum_vermogen_rondt_omhoog_naar_aansturingstap(self):
        app = _maak_app(
            states={"input_number.dynamisch_minimum_vermogen_w": "226"}
        )

        assert app._haal_minimum_vermogen_w() == 250

    def test_haal_minimum_vermogen_gebruikt_default_bij_onbekende_helper(self):
        app = _maak_app(
            states={"input_number.dynamisch_minimum_vermogen_w": "unknown"}
        )

        assert app._haal_minimum_vermogen_w() == 100

    def test_haal_minimum_vermogen_begrenst_bereik(self):
        app_laag = _maak_app(
            states={"input_number.dynamisch_minimum_vermogen_w": "25"}
        )
        app_hoog = _maak_app(
            states={"input_number.dynamisch_minimum_vermogen_w": "3500"}
        )

        assert app_laag._haal_minimum_vermogen_w() == 50
        assert app_hoog._haal_minimum_vermogen_w() == 3000


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
        "name: Minimum vermogen",
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
        "title: Vermogensgrenzen",
        "title: SoC-sturing",
        "title: Weerdata",
        "title: Straf per slot",
    ):
        assert titel in tekst

    assert "input_number.dynamisch_minimum_vermogen_w" in tekst
    assert "attribute: dp_vermogen_stap_w" in tekst
    assert "sensor.dynamisch_handelsstrategie_economisch" in tekst
    assert "name: Gekozen met penalties" in tekst
    assert "name: Economisch optimaal" in tekst
    assert "title: Verwachte prijs+RTE-winst per slot" in tekst
    assert "title: Cumulatieve penalties — huidige planning" in tekst
    for attribuut in (
        "penalty_totaal_eur",
        "warmte_penalty_laden_totaal_eur",
        "warmte_penalty_ontladen_totaal_eur",
        "overtemp_penalty_totaal_eur",
        "hoge_soc_verblijf_penalty_totaal_eur",
        "lage_soc_verblijf_penalty_totaal_eur",
    ):
        assert f"attribute: {attribuut}" in tekst

    for attribuut in (
        "statistische_schatting_warmte_stijging_laden_tekst",
        "statistische_schatting_warmte_stijging_ontladen_tekst",
        "statistische_schatting_afkoeling_tekst",
        "statistische_spreiding_warmte_stijging_laden_tekst",
        "statistische_spreiding_warmte_stijging_ontladen_tekst",
        "aanbevolen_temp_penalty_tekst",
        "aanbevolen_warmte_penalty_laden_tekst",
        "aanbevolen_warmte_penalty_ontladen_tekst",
    ):
        assert f"attribute: {attribuut}" in tekst

    assert "title: Berekening per waarde" in tekst
    assert tekst.count("<details>") >= 6
    assert tekst.count("<summary>") >= 6
    assert "komt niet voor in de schattingsformule" in tekst
    assert "attribute: buitentemperatuur_bron" in tekst
    assert "attribute: buitentemperatuur_samples" in tekst
    assert "attribute: statistische_afkoeling_blokken_voldoende_duur" in tekst
    assert "attribute: statistische_afkoeling_afwijzingen_tekst" in tekst
    assert "sensor.dynamisch_handelsstrategie.attributes.slots[].buiten_temp_c" in tekst
    assert "wordt niet gebruikt" in tekst


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

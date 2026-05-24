"""
Unit tests voor AppDaemon-specifieke strategie-logica.

De tests gebruiken een kleine fake voor appdaemon.plugins.hass.hassapi, zodat
we DynamischHandelen kunnen importeren zonder Home Assistant of AppDaemon.
"""

import math
import sys
import types
from datetime import datetime
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

from dynamisch_handelen import DynamischHandelen  # noqa: E402


def _history_item(state: str, tijd: str) -> dict:
    return {
        "state": state,
        "last_changed": tijd,
        "last_updated": tijd,
    }


def _maak_app(states: dict[str, str] | None = None, history: dict[str, list[dict]] | None = None) -> DynamischHandelen:
    app = object.__new__(DynamischHandelen)
    states = states or {}
    history = history or {}

    def get_state(entity: str, attribute: str | None = None):
        if attribute is not None:
            return None
        return states.get(entity)

    def get_history(*args, **kwargs):
        entity = kwargs.get("entity_id") or (args[0] if args else None)
        return history.get(entity, [])

    app.get_state = get_state
    app.get_history = get_history
    return app


class TestHistorischeStates:
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

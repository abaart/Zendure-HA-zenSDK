"""
Unit tests voor de AppDaemon-laag van dynamisch_handelen.py.

De tests gebruiken een kleine AppDaemon-stub, zodat de actieve-slot-correctie
zonder Home Assistant kan draaien.
"""

import sys
import types
from datetime import datetime as real_datetime
from pathlib import Path


def installeer_appdaemon_stub() -> None:
    """Maakt genoeg appdaemon-modules aan om dynamisch_handelen.py te importeren."""
    hassapi = types.ModuleType("appdaemon.plugins.hass.hassapi")

    class Hass:
        pass

    hassapi.Hass = Hass
    sys.modules["appdaemon"] = types.ModuleType("appdaemon")
    sys.modules["appdaemon.plugins"] = types.ModuleType("appdaemon.plugins")
    sys.modules["appdaemon.plugins.hass"] = types.ModuleType("appdaemon.plugins.hass")
    sys.modules["appdaemon.plugins.hass.hassapi"] = hassapi


installeer_appdaemon_stub()
sys.path.insert(0, str(Path(__file__).parent.parent / "appdaemon" / "apps"))

import dynamisch_handelen  # noqa: E402
from strategie_dp import Accustatus  # noqa: E402


class FakeDateTime:
    current = real_datetime.fromisoformat("2026-05-22T12:00:00+02:00")

    @classmethod
    def now(cls):
        return cls.current

    @classmethod
    def fromisoformat(cls, value):
        return real_datetime.fromisoformat(value)


class StubApp(dynamisch_handelen.DynamischHandelen):
    def __init__(self, soc_pct: float, vorige_slots: list[dict] | None = None):
        self.soc_pct = soc_pct
        self.vorige_slots = vorige_slots or []

    def get_state(self, entity, attribute=None):
        if entity == "sensor.zendure_2400_ac_laadpercentage":
            return self.soc_pct
        if entity == "sensor.dynamisch_handelsstrategie" and attribute == "slots":
            return self.vorige_slots
        return None


def test_actief_laadslot_negeert_oud_sensordoel(monkeypatch):
    """
    _corrigeer_actief_slot_vermogen() gebruikt de nieuwe DP-uitkomst.

    Een oud doel_soc_kwh uit sensor.dynamisch_handelsstrategie mag het actieve
    laadvermogen niet naar beneden trekken.
    """
    monkeypatch.setattr(dynamisch_handelen, "datetime", FakeDateTime)
    FakeDateTime.current = real_datetime.fromisoformat("2026-05-22T12:30:00+02:00")

    app = StubApp(
        50.0,
        vorige_slots=[{
            "start": "2026-05-22T12:00:00+02:00",
            "end": "2026-05-22T13:00:00+02:00",
            "geplande_actie": "laden",
            "actief_slot_begin_kwh": 2.0,
            "doel_soc_kwh": 2.6,
            "soc_voor_kwh": 2.0,
            "soc_na_kwh": 2.6,
        }],
    )
    accu = Accustatus(2.5, 5.0, 0.9, 0.9, 2400, 2400)
    schema = [{
        "start": "2026-05-22T12:00:00+02:00",
        "end": "2026-05-22T13:00:00+02:00",
        "prijs_ct": 10.0,
        "actie": "laden",
        "vermogen_w": 2400,
        "verwacht_vermogen_w": 2400,
        "soc_voor_kwh": 2.5,
        "soc_na_kwh": 4.0,
        "soc_voor_pct": 50.0,
        "soc_na_pct": 80.0,
        "winst_eur": -0.16,
    }]

    app._corrigeer_actief_slot_vermogen(schema, accu, 0.0, 100.0)

    assert schema[0]["actie"] == "laden"
    assert schema[0]["vermogen_w"] == 2400
    assert schema[0]["doel_soc_kwh"] > 3.5
    assert schema[0]["doel_soc_kwh"] != 2.6


def test_actief_laadslot_werkt_later_soc_pad_en_winst_bij(monkeypatch):
    """
    Actief laden haalt energie naar voren en verlaagt latere laadvermogens.

    Het voorbeeld komt overeen met het 22-mei-patroon: 2400 W in het actieve
    slot, daarna langzamer laden omdat de SoC al dichter bij het einddoel zit.
    """
    monkeypatch.setattr(dynamisch_handelen, "datetime", FakeDateTime)
    FakeDateTime.current = real_datetime.fromisoformat("2026-05-22T12:45:33.967854+02:00")

    app = StubApp(64.0)
    accu = Accustatus(3.067, 5.162, 0.923, 0.923, 2400, 2400)
    schema = [
        {
            "start": "2026-05-22T12:00:00+02:00",
            "end": "2026-05-22T13:00:00+02:00",
            "prijs_ct": 13.564,
            "actie": "laden",
            "vermogen_w": 2400,
            "verwacht_vermogen_w": 2400,
            "soc_voor_kwh": 3.05,
            "soc_na_kwh": 3.6,
            "soc_voor_pct": 63.2,
            "soc_na_pct": 72.7,
            "winst_eur": -0.0809,
        },
        {
            "start": "2026-05-22T13:00:00+02:00",
            "end": "2026-05-22T14:00:00+02:00",
            "prijs_ct": 13.564,
            "actie": "laden",
            "vermogen_w": 925,
            "verwacht_vermogen_w": 925,
            "soc_voor_kwh": 3.6,
            "soc_na_kwh": 4.45,
            "soc_voor_pct": 72.7,
            "soc_na_pct": 87.6,
            "winst_eur": -0.125,
        },
        {
            "start": "2026-05-22T14:00:00+02:00",
            "end": "2026-05-22T15:00:00+02:00",
            "prijs_ct": 13.667,
            "actie": "laden",
            "vermogen_w": 2400,
            "verwacht_vermogen_w": 775,
            "soc_voor_kwh": 4.45,
            "soc_na_kwh": 5.15,
            "soc_voor_pct": 87.6,
            "soc_na_pct": 99.8,
            "winst_eur": -0.1037,
        },
    ]

    app._corrigeer_actief_slot_vermogen(schema, accu, 10.0, 100.0)

    assert [slot["vermogen_w"] for slot in schema] == [2400, 900, 775]
    assert [slot["verwacht_vermogen_w"] for slot in schema] == [2400, 900, 775]
    assert schema[0]["soc_na_kwh"] == schema[1]["soc_voor_kwh"]
    assert schema[1]["soc_na_kwh"] == schema[2]["soc_voor_kwh"]
    assert schema[0]["winst_eur"] != -0.0809


def test_actief_ontlaadslot_haalt_lagere_prijs_naar_voren(monkeypatch):
    """
    Actief ontladen gebruikt 2400 W als latere ontlaadslots niet duurder zijn.

    De resterende latere ontlaadenergie wordt opnieuw berekend vanaf de nieuwe
    SoC na het actieve slot.
    """
    monkeypatch.setattr(dynamisch_handelen, "datetime", FakeDateTime)
    FakeDateTime.current = real_datetime.fromisoformat("2026-05-21T20:06:28.030751+02:00")

    app = StubApp(79.0)
    accu = Accustatus(3.933, 5.13, 0.92, 0.92, 2400, 2400)
    schema = [
        {
            "start": "2026-05-21T20:00:00+02:00",
            "end": "2026-05-21T21:00:00+02:00",
            "prijs_ct": 37.08,
            "actie": "ontladen",
            "vermogen_w": 1994,
            "soc_voor_kwh": 3.95,
            "soc_na_kwh": 2.0,
            "soc_voor_pct": 79.3,
            "soc_na_pct": 45.1,
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
            "soc_voor_pct": 45.1,
            "soc_na_pct": 10,
            "winst_eur": 0.6554,
        },
    ]

    app._corrigeer_actief_slot_vermogen(schema, accu, 10.0, 100.0)

    assert schema[0]["actie"] == "ontladen"
    assert schema[0]["vermogen_w"] == 2400
    assert schema[0]["soc_na_kwh"] == schema[1]["soc_voor_kwh"]
    assert schema[1]["vermogen_w"] == 1175
    assert schema[0]["winst_eur"] > 0.79

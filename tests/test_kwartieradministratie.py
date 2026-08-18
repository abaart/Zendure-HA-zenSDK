"""Regressietests voor de idempotente Zendure-kwartieradministratie."""

from __future__ import annotations

import sqlite3
import sys
import types
from datetime import datetime, timezone
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

from kwartieradministratie import (  # noqa: E402
    KWARTIER_SECONDEN,
    KwartierLedger,
    normaliseer_forecast,
)


def _maak_recorder(path: Path) -> sqlite3.Connection:
    verbinding = sqlite3.connect(path)
    verbinding.executescript(
        """
        CREATE TABLE states_meta (
            metadata_id INTEGER PRIMARY KEY,
            entity_id TEXT NOT NULL UNIQUE
        );
        CREATE TABLE states (
            state_id INTEGER PRIMARY KEY AUTOINCREMENT,
            metadata_id INTEGER NOT NULL,
            state TEXT NOT NULL,
            last_changed_ts REAL,
            last_updated_ts REAL
        );
        CREATE TABLE statistics_meta (
            id INTEGER PRIMARY KEY,
            statistic_id TEXT NOT NULL UNIQUE
        );
        CREATE TABLE statistics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            metadata_id INTEGER NOT NULL,
            start_ts REAL NOT NULL,
            mean REAL,
            "sum" REAL
        );
        """
    )
    return verbinding


def _maak_ledger(tmp_path: Path) -> tuple[KwartierLedger, Path, Path]:
    recorder_path = tmp_path / "home-assistant_v2.db"
    ledger_path = tmp_path / "zendure_kwartieren.sqlite"
    recorder = _maak_recorder(recorder_path)
    recorder.executemany(
        "INSERT INTO states_meta(metadata_id, entity_id) VALUES (?, ?)",
        [
            (1, "sensor.import"),
            (2, "sensor.export"),
        ],
    )
    recorder.commit()
    recorder.close()
    ledger = KwartierLedger(
        ledger_db_path=str(ledger_path),
        recorder_db_path=str(recorder_path),
        import_entity="sensor.import",
        export_entity="sensor.export",
        legacy_price_entity="sensor.oude_prijs",
        timezone_name="Europe/Amsterdam",
    )
    ledger.initialiseer()
    return ledger, recorder_path, ledger_path


def test_normaliseert_zonneplan_forecast_en_micro_euro_prijs() -> None:
    forecast = [
        {
            "start_date": "2026-08-18T10:00:00+02:00",
            "end_date": "2026-08-18T10:15:00+02:00",
            "price_tax_included": {"amount": 3_734_772},
        },
        {
            "start_date": "ongeldig",
            "price_tax_included": {"amount": 1},
        },
    ]

    resultaat = normaliseer_forecast(forecast)

    assert resultaat == [(1787040000, 1787040900, pytest.approx(0.3734772))]


def test_settle_quarter_is_idempotent_en_vervangt_correctie(tmp_path: Path) -> None:
    ledger, recorder_path, ledger_path = _maak_ledger(tmp_path)
    start = 1_787_040_000
    einde = start + KWARTIER_SECONDEN
    with sqlite3.connect(recorder_path) as recorder:
        recorder.executemany(
            """
            INSERT INTO states(metadata_id, state, last_changed_ts, last_updated_ts)
            VALUES (?, ?, ?, ?)
            """,
            [
                (1, "100.00", start, start),
                (1, "100.05", einde, einde),
                (2, "50.00", start, start),
                (2, "50.08", einde, einde),
            ],
        )
    ledger.bewaar_prijzen([(start, einde, 0.25)], source="sensor.kwartierprijs")

    assert ledger.settle_quarter(start)
    assert ledger.settle_quarter(start)
    with sqlite3.connect(ledger_path) as database:
        count, import_kwh, export_kwh, netto = database.execute(
            "SELECT COUNT(*), import_kwh, export_kwh, net_result_eur FROM zendure_quarters"
        ).fetchone()
    assert count == 1
    assert import_kwh == pytest.approx(0.05)
    assert export_kwh == pytest.approx(0.08)
    assert netto == pytest.approx(0.0075)

    with sqlite3.connect(recorder_path) as recorder:
        recorder.execute(
            """
            INSERT INTO states(metadata_id, state, last_changed_ts, last_updated_ts)
            VALUES (1, '100.06', ?, ?)
            """,
            (einde, einde),
        )
    assert ledger.settle_quarter(start)
    with sqlite3.connect(ledger_path) as database:
        count, import_kwh, netto = database.execute(
            "SELECT COUNT(*), import_kwh, net_result_eur FROM zendure_quarters"
        ).fetchone()
    assert count == 1
    assert import_kwh == pytest.approx(0.06)
    assert netto == pytest.approx(0.005)


def test_backfill_en_timer_tellen_een_compleet_kwartier_niet_opnieuw(tmp_path: Path) -> None:
    ledger, recorder_path, ledger_path = _maak_ledger(tmp_path)
    start = 1_787_040_000
    with sqlite3.connect(recorder_path) as recorder:
        for metadata_id, beginwaarde, eindwaarde in (
            (1, 10.0, 10.1),
            (2, 20.0, 20.2),
        ):
            recorder.executemany(
                """
                INSERT INTO states(metadata_id, state, last_changed_ts, last_updated_ts)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (metadata_id, str(beginwaarde), start, start),
                    (
                        metadata_id,
                        str(eindwaarde),
                        start + KWARTIER_SECONDEN,
                        start + KWARTIER_SECONDEN,
                    ),
                ],
            )
    ledger.bewaar_prijzen(
        [(start, start + KWARTIER_SECONDEN, 0.30)], source="forecast"
    )

    eerste = ledger.settle_recente_kwartieren(now_ts=start + 1800, backfill_days=1)
    tweede = ledger.settle_recente_kwartieren(now_ts=start + 1800, backfill_days=1)

    assert eerste == (1, 0)
    assert tweede == (0, 0)
    with sqlite3.connect(ledger_path) as database:
        assert database.execute("SELECT COUNT(*) FROM zendure_quarters").fetchone()[0] == 1


def test_prijscorrectie_herberekent_compleet_kwartier_zonder_recorder(tmp_path: Path) -> None:
    ledger, recorder_path, ledger_path = _maak_ledger(tmp_path)
    start = 1_787_040_000
    einde = start + KWARTIER_SECONDEN
    with sqlite3.connect(recorder_path) as recorder:
        recorder.executemany(
            """
            INSERT INTO states(metadata_id, state, last_changed_ts, last_updated_ts)
            VALUES (?, ?, ?, ?)
            """,
            [
                (1, "10.00", start, start),
                (1, "10.10", einde, einde),
                (2, "20.00", start, start),
                (2, "20.20", einde, einde),
            ],
        )
    ledger.bewaar_prijzen([(start, einde, 0.20)], source="forecast")
    assert ledger.settle_quarter(start)

    ledger.bewaar_prijzen([(start, einde, 0.30)], source="gecorrigeerde_forecast")

    with sqlite3.connect(ledger_path) as database:
        prijs, importkosten, exportopbrengst, netto, status = database.execute(
            """
            SELECT price_eur_kwh, import_cost_eur, export_revenue_eur,
                   net_result_eur, status
            FROM zendure_quarters
            WHERE quarter_start = ?
            """,
            (start,),
        ).fetchone()
    assert prijs == pytest.approx(0.30)
    assert importkosten == pytest.approx(0.03)
    assert exportopbrengst == pytest.approx(0.06)
    assert netto == pytest.approx(0.03)
    assert status == "complete"


def test_legacy_openingssaldo_wordt_eenmalig_berekend(tmp_path: Path) -> None:
    ledger, recorder_path, ledger_path = _maak_ledger(tmp_path)
    cutoff = 1_787_054_400
    with sqlite3.connect(recorder_path) as recorder:
        recorder.executemany(
            "INSERT INTO statistics_meta(id, statistic_id) VALUES (?, ?)",
            [
                (1, "sensor.import"),
                (2, "sensor.export"),
                (3, "sensor.oude_prijs"),
            ],
        )
        for index, (import_sum, export_sum, prijs) in enumerate(
            ((0.0, 0.0, 0.10), (1.0, 0.5, 0.20), (2.0, 1.5, 0.30))
        ):
            timestamp = cutoff - 3 * 3600 + index * 3600
            recorder.executemany(
                "INSERT INTO statistics(metadata_id, start_ts, mean, \"sum\") VALUES (?, ?, ?, ?)",
                [
                    (1, timestamp, None, import_sum),
                    (2, timestamp, None, export_sum),
                    (3, timestamp, prijs, None),
                ],
            )
    with sqlite3.connect(ledger_path) as database:
        database.execute(
            """
            INSERT INTO zendure_quarters(
                quarter_start, quarter_end, price_eur_kwh, status, updated_at
            ) VALUES (?, ?, 0.25, 'complete', ?)
            """,
            (cutoff, cutoff + KWARTIER_SECONDEN, cutoff),
        )

    assert ledger.initialiseer_legacy_openingssaldo(now_ts=cutoff)
    assert ledger.initialiseer_legacy_openingssaldo(now_ts=cutoff + 1)
    with sqlite3.connect(ledger_path) as database:
        meta = dict(database.execute("SELECT key, value FROM ledger_meta"))
    assert float(meta["legacy_import_cost_eur"]) == pytest.approx(0.5)
    assert float(meta["legacy_export_revenue_eur"]) == pytest.approx(0.4)
    assert float(meta["legacy_net_result_eur"]) == pytest.approx(-0.1)
    assert int(meta["legacy_cutoff_utc"]) == cutoff


def test_overzicht_aggregeert_dagwinst_uit_tabel(tmp_path: Path) -> None:
    ledger, _, ledger_path = _maak_ledger(tmp_path)
    dag_start = int(datetime(2026, 8, 18, tzinfo=timezone.utc).timestamp())
    with sqlite3.connect(ledger_path) as database:
        database.execute(
            "INSERT INTO ledger_meta(key, value, updated_at) VALUES ('legacy_net_result_eur', '1.5', ?)",
            (dag_start,),
        )
        database.executemany(
            """
            INSERT INTO zendure_quarters(
                quarter_start, quarter_end, price_eur_kwh,
                import_cost_eur, export_revenue_eur, net_result_eur,
                status, updated_at
            ) VALUES (?, ?, 0.25, ?, ?, ?, 'complete', ?)
            """,
            [
                (dag_start, dag_start + 900, 0.10, 0.30, 0.20, dag_start),
                (dag_start + 900, dag_start + 1800, 0.20, 0.10, -0.10, dag_start),
            ],
        )

    overzicht = ledger.overzicht(now_ts=dag_start + 3600, days=2)

    assert overzicht["handelsresultaat_totaal_eur"] == pytest.approx(1.6)
    assert overzicht["complete_kwartieren"] == 2
    assert overzicht["dagresultaten"][0]["netto_eur"] == pytest.approx(0.1)
    assert overzicht["dagresultaten"][0]["cumulatief_eur"] == pytest.approx(1.6)

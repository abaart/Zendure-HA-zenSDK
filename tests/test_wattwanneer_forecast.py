"""Tests voor de persistente WattWanneer-forecastcache en requestbegrenzing."""

import gzip
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "appdaemon" / "apps"))

from wattwanneer_forecast import (  # noqa: E402
    FIREFOX_MAC_HEADERS,
    FOUT_RETRY_INTERVAL_SECONDEN,
    SUCCES_INTERVAL_SECONDEN,
    WattWanneerForecastCache,
    download_wattwanneer_forecast,
)


def _payload(aantal: int = 168) -> list[dict]:
    start = datetime(2026, 8, 23, 0, 0)
    return [
        {
            "datetime": (start + timedelta(hours=index)).strftime("%Y-%m-%d %H:%M"),
            "price_eur_kwh": 0.05 + index / 1000,
            "source": "entsoe_day_ahead" if index < 24 else "model",
            "generated_at": "20260822_1320",
            "predicted_price_p10": -99,
            "predicted_price_p90": 99,
        }
        for index in range(aantal)
    ]


def test_download_gebruikt_firefox_mac_headers_en_bewaart_geen_p10_p90():
    gezien = {}
    body = gzip.compress(json.dumps(_payload()).encode("utf-8"))

    class Response:
        status = 200
        headers = {"Content-Encoding": "gzip"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, maximum):
            return body

    def opener(request, *, timeout):
        gezien["headers"] = {key.lower(): value for key, value in request.header_items()}
        gezien["timeout"] = timeout
        return Response()

    records = download_wattwanneer_forecast(opener=opener)

    for naam, waarde in FIREFOX_MAC_HEADERS.items():
        assert gezien["headers"][naam.lower()] == waarde
    assert records[0] == {
        "datetime": "2026-08-23 00:00",
        "price_eur_kwh": 0.05,
        "source": "entsoe_day_ahead",
        "generated_at": "20260822_1320",
    }


def test_succes_wordt_over_herstart_12_uur_geblokkeerd(tmp_path):
    db_path = tmp_path / "zendure_kwartieren.sqlite"
    calls = []

    def downloader(url):
        calls.append(url)
        return _payload()

    eerste_cache = WattWanneerForecastCache(
        db_path=str(db_path),
        downloader=downloader,
    )
    start = 1_800_000_000

    eerste = eerste_cache.haal(now_epoch=start)
    assert eerste.laatste_status == "success"
    assert eerste.poging_uitgevoerd is True
    assert len(calls) == 1

    herstart_cache = WattWanneerForecastCache(
        db_path=str(db_path),
        downloader=downloader,
    )
    geblokkeerd = herstart_cache.haal(
        now_epoch=start + SUCCES_INTERVAL_SECONDEN - 1
    )
    assert geblokkeerd.poging_uitgevoerd is False
    assert len(calls) == 1

    toegestaan = herstart_cache.haal(
        now_epoch=start + SUCCES_INTERVAL_SECONDEN
    )
    assert toegestaan.poging_uitgevoerd is True
    assert len(calls) == 2


def test_fout_wordt_over_herstart_2_uur_geblokkeerd(tmp_path):
    db_path = tmp_path / "zendure_kwartieren.sqlite"
    calls = []

    def downloader(url):
        calls.append(url)
        raise RuntimeError("tijdelijke serverfout")

    eerste_cache = WattWanneerForecastCache(
        db_path=str(db_path),
        downloader=downloader,
    )
    start = 1_800_000_000

    eerste = eerste_cache.haal(now_epoch=start)
    assert eerste.laatste_status == "failure"
    assert eerste.poging_uitgevoerd is True
    assert len(calls) == 1

    herstart_cache = WattWanneerForecastCache(
        db_path=str(db_path),
        downloader=downloader,
    )
    geblokkeerd = herstart_cache.haal(
        now_epoch=start + FOUT_RETRY_INTERVAL_SECONDEN - 1
    )
    assert geblokkeerd.poging_uitgevoerd is False
    assert len(calls) == 1

    toegestaan = herstart_cache.haal(
        now_epoch=start + FOUT_RETRY_INTERVAL_SECONDEN
    )
    assert toegestaan.poging_uitgevoerd is True
    assert len(calls) == 2


def test_mislukte_verversing_bewaart_laatste_geldige_payload(tmp_path):
    db_path = tmp_path / "zendure_kwartieren.sqlite"
    start = 1_800_000_000
    cache = WattWanneerForecastCache(
        db_path=str(db_path),
        downloader=lambda url: _payload(),
    )
    assert cache.haal(now_epoch=start).laatste_status == "success"

    falende_cache = WattWanneerForecastCache(
        db_path=str(db_path),
        downloader=lambda url: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    resultaat = falende_cache.haal(
        now_epoch=start + SUCCES_INTERVAL_SECONDEN
    )

    assert resultaat.laatste_status == "failure"
    assert resultaat.cache_beschikbaar is True
    assert len(resultaat.records) == 168
    assert resultaat.fout == "offline"


def test_cachetabel_deelt_database_zonder_kwartiertabel_te_wijzigen(tmp_path):
    db_path = tmp_path / "zendure_kwartieren.sqlite"
    with sqlite3.connect(db_path) as database:
        database.execute(
            "CREATE TABLE zendure_quarters (quarter_start INTEGER PRIMARY KEY)"
        )

    cache = WattWanneerForecastCache(
        db_path=str(db_path),
        downloader=lambda url: _payload(),
    )
    cache.haal(now_epoch=1_800_000_000)

    with sqlite3.connect(db_path) as database:
        tabellen = {
            row[0]
            for row in database.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "zendure_quarters" in tabellen
    assert "wattwanneer_forecast_cache" in tabellen
    assert "wattwanneer_forecast_fetches" in tabellen
    assert "wattwanneer_forecast_history" in tabellen
    assert "nordpool_quarter_price_history" in tabellen
    assert "wattwanneer_price_calibration_history" in tabellen


def test_bestaande_cachetabel_krijgt_history_koppelingen_zonder_payloadverlies(
    tmp_path,
):
    db_path = tmp_path / "zendure_kwartieren.sqlite"
    payload_json = json.dumps(_payload())
    with sqlite3.connect(db_path) as database:
        database.executescript(
            """
            CREATE TABLE wattwanneer_forecast_cache (
                singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                last_attempt_at INTEGER,
                last_success_at INTEGER,
                last_status TEXT NOT NULL DEFAULT 'never',
                generated_at TEXT,
                payload_json TEXT,
                last_error TEXT,
                updated_at INTEGER NOT NULL
            );
            """
        )
        database.execute(
            """
            INSERT INTO wattwanneer_forecast_cache (
                singleton_id, last_attempt_at, last_success_at, last_status,
                generated_at, payload_json, updated_at
            ) VALUES (1, ?, ?, 'success', ?, ?, ?)
            """,
            (1_800_000_000, 1_800_000_000, "20260822_1320", payload_json, 1_800_000_000),
        )

    resultaat = WattWanneerForecastCache(db_path=str(db_path)).lees_status()

    assert len(resultaat.records) == 168
    assert resultaat.payload_fetch_id is None
    with sqlite3.connect(db_path) as database:
        kolommen = {
            row[1]
            for row in database.execute(
                "PRAGMA table_info(wattwanneer_forecast_cache)"
            )
        }
    assert {"payload_fetch_id", "last_attempt_fetch_id"} <= kolommen


def test_iedere_succesvolle_fetch_bewaart_een_volledige_forecastsnapshot(tmp_path):
    db_path = tmp_path / "zendure_kwartieren.sqlite"
    cache = WattWanneerForecastCache(
        db_path=str(db_path),
        downloader=lambda url: _payload(),
    )
    start = 1_800_000_000

    eerste = cache.haal(now_epoch=start)
    tweede = cache.haal(now_epoch=start + SUCCES_INTERVAL_SECONDEN)

    assert eerste.payload_fetch_id is not None
    assert tweede.payload_fetch_id is not None
    assert tweede.payload_fetch_id != eerste.payload_fetch_id
    with sqlite3.connect(db_path) as database:
        fetches = database.execute(
            """
            SELECT fetch_id, status, record_count, attempted_at_utc, completed_at_utc
            FROM wattwanneer_forecast_fetches
            ORDER BY fetch_id
            """
        ).fetchall()
        historie_aantal = database.execute(
            "SELECT COUNT(*) FROM wattwanneer_forecast_history"
        ).fetchone()[0]
        eerste_slot = database.execute(
            """
            SELECT fetched_at_utc, forecast_slot_start_iso,
                   forecast_slot_end_iso, forecast_datetime_local,
                   price_eur_kwh, source, generated_at
            FROM wattwanneer_forecast_history
            ORDER BY fetch_id, forecast_slot_start_epoch
            LIMIT 1
            """
        ).fetchone()

    assert [row[1] for row in fetches] == ["success", "success"]
    assert [row[2] for row in fetches] == [168, 168]
    assert all(row[3].endswith("+00:00") for row in fetches)
    assert all(row[4].endswith("+00:00") for row in fetches)
    assert historie_aantal == 336
    assert eerste_slot[0].endswith("+00:00")
    assert eerste_slot[1] == "2026-08-23T00:00:00+02:00"
    assert eerste_slot[2] == "2026-08-23T01:00:00+02:00"
    assert eerste_slot[3] == "2026-08-23 00:00"
    assert eerste_slot[4:] == (0.05, "entsoe_day_ahead", "20260822_1320")


def test_mislukte_fetch_bewaart_auditregel_zonder_forecastslots(tmp_path):
    db_path = tmp_path / "zendure_kwartieren.sqlite"
    cache = WattWanneerForecastCache(
        db_path=str(db_path),
        downloader=lambda url: (_ for _ in ()).throw(RuntimeError("offline")),
    )

    resultaat = cache.haal(now_epoch=1_800_000_000)

    assert resultaat.laatste_status == "failure"
    with sqlite3.connect(db_path) as database:
        fetch = database.execute(
            """
            SELECT status, record_count, error, completed_at_utc
            FROM wattwanneer_forecast_fetches
            """
        ).fetchone()
        historie_aantal = database.execute(
            "SELECT COUNT(*) FROM wattwanneer_forecast_history"
        ).fetchone()[0]
    assert fetch[:3] == ("failure", 0, "offline")
    assert fetch[3].endswith("+00:00")
    assert historie_aantal == 0


def test_nordpool_historie_bewaart_alleen_prijswijzigingen_als_nieuwe_versie(
    tmp_path,
):
    db_path = tmp_path / "zendure_kwartieren.sqlite"
    cache = WattWanneerForecastCache(db_path=str(db_path))
    tijdzone = ZoneInfo("Europe/Amsterdam")
    start = datetime(2026, 8, 23, 0, 0, tzinfo=tijdzone)
    slot = {
        "start": start,
        "end": start + timedelta(minutes=15),
        "price": 0.21,
        "source_series": "raw_tomorrow",
    }

    eerste = cache.bewaar_nordpool_prijzen(
        price_entity="sensor.nordpool_test",
        slots=[slot],
        observed_at_epoch=1_800_000_000,
    )
    tweede = cache.bewaar_nordpool_prijzen(
        price_entity="sensor.nordpool_test",
        slots=[{**slot, "source_series": "raw_today"}],
        observed_at_epoch=1_800_000_100,
    )
    derde = cache.bewaar_nordpool_prijzen(
        price_entity="sensor.nordpool_test",
        slots=[{**slot, "price": 0.22, "source_series": "raw_today"}],
        observed_at_epoch=1_800_000_200,
    )

    assert eerste == {"waargenomen_slots": 1, "nieuwe_prijsversies": 1}
    assert tweede == {"waargenomen_slots": 1, "nieuwe_prijsversies": 0}
    assert derde == {"waargenomen_slots": 1, "nieuwe_prijsversies": 1}
    with sqlite3.connect(db_path) as database:
        regels = database.execute(
            """
            SELECT price_eur_kwh, first_observed_at_epoch,
                   last_observed_at_epoch, observation_count,
                   first_series_name, last_series_name
            FROM nordpool_quarter_price_history
            ORDER BY price_version_id
            """
        ).fetchall()
    assert regels == [
        (0.21, 1_800_000_000, 1_800_000_100, 2, "raw_tomorrow", "raw_today"),
        (0.22, 1_800_000_200, 1_800_000_200, 1, "raw_today", "raw_today"),
    ]


def test_kalibratie_historie_verwijst_naar_de_gebruikte_forecastfetch(tmp_path):
    db_path = tmp_path / "zendure_kwartieren.sqlite"
    cache = WattWanneerForecastCache(
        db_path=str(db_path),
        downloader=lambda url: _payload(),
    )
    resultaat = cache.haal(now_epoch=1_800_000_000)

    calibration_id = cache.bewaar_prijskalibratie(
        calculated_at_epoch=1_800_000_100,
        forecast_fetch_id=resultaat.payload_fetch_id,
        price_entity="sensor.nordpool_test",
        overlap_hours=8,
        factor=1.21,
        offset_eur_kwh=0.13564,
        max_residual_eur_kwh=0.000003,
    )

    with sqlite3.connect(db_path) as database:
        regel = database.execute(
            """
            SELECT calibration_id, forecast_fetch_id, price_entity,
                   overlap_hours, factor, offset_eur_kwh,
                   max_residual_eur_kwh, calculated_at_utc
            FROM wattwanneer_price_calibration_history
            """
        ).fetchone()
    assert regel[:7] == (
        calibration_id,
        resultaat.payload_fetch_id,
        "sensor.nordpool_test",
        8,
        1.21,
        0.13564,
        0.000003,
    )
    assert regel[7].endswith("+00:00")


@pytest.mark.parametrize("aantal", [0, 71, 201])
def test_onvolledige_payload_wordt_als_fout_opgeslagen(tmp_path, aantal):
    cache = WattWanneerForecastCache(
        db_path=str(tmp_path / "cache.sqlite"),
        downloader=lambda url: _payload(aantal),
    )

    resultaat = cache.haal(now_epoch=1_800_000_000)

    assert resultaat.laatste_status == "failure"
    assert resultaat.cache_beschikbaar is False
    assert "forecast bevat" in resultaat.fout

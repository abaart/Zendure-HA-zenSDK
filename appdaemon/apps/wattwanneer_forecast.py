"""Persistente, terughoudende downloader voor de WattWanneer-weekforecast."""

from __future__ import annotations

import gzip
import json
import math
import sqlite3
import time
import zlib
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


WATTWANNEER_URL = "https://wattwanneer.nl/public/week_forecast.json"
SUCCES_INTERVAL_SECONDEN = 12 * 60 * 60
FOUT_RETRY_INTERVAL_SECONDEN = 2 * 60 * 60
REQUEST_TIMEOUT_SECONDEN = 30
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MIN_FORECAST_REGELS = 72
MAX_FORECAST_REGELS = 200
FORECAST_TIJDZONE = ZoneInfo("Europe/Amsterdam")

FIREFOX_MAC_HEADERS = {
    "Host": "wattwanneer.nl",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:154.0) "
        "Gecko/20100101 Firefox/154.0"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    # De AppDaemon-container heeft standaard geen brotli/zstandard. Gzip en
    # deflate voorkomen een tweede request wanneer de server anders br kiest.
    "Accept-Encoding": "gzip, deflate",
    "Referer": "https://wattwanneer.nl/index.html",
    "DNT": "1",
    "Sec-GPC": "1",
    "Connection": "keep-alive",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "Priority": "u=4",
    "Pragma": "no-cache",
    "Cache-Control": "no-cache",
    "TE": "trailers",
}


class WattWanneerFout(RuntimeError):
    """Geeft aan dat de HTTP-respons of forecastinhoud niet bruikbaar is."""


@dataclass(frozen=True)
class WattWanneerCacheResultaat:
    records: list[dict[str, Any]]
    laatste_status: str
    poging_uitgevoerd: bool
    laatste_poging_epoch: int | None
    laatste_succes_epoch: int | None
    volgende_poging_epoch: int | None
    generated_at: str | None
    fout: str | None
    payload_fetch_id: int | None = None
    laatste_poging_fetch_id: int | None = None

    @property
    def cache_beschikbaar(self) -> bool:
        return bool(self.records)


def _decodeer_response(raw: bytes, content_encoding: str | None) -> bytes:
    """Decodeert de compressies die de opgegeven Firefox-client aanbiedt."""
    encodings = [deel.strip().lower() for deel in (content_encoding or "").split(",")]
    for encoding in reversed([waarde for waarde in encodings if waarde and waarde != "identity"]):
        if encoding == "gzip":
            raw = gzip.decompress(raw)
        elif encoding == "deflate":
            try:
                raw = zlib.decompress(raw)
            except zlib.error:
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
        elif encoding == "br":
            try:
                import brotli
            except ImportError as exc:
                raise WattWanneerFout(
                    "server antwoordde met Brotli, maar Python-module brotli ontbreekt"
                ) from exc
            raw = brotli.decompress(raw)
        elif encoding == "zstd":
            try:
                import zstandard
            except ImportError as exc:
                raise WattWanneerFout(
                    "server antwoordde met Zstandard, maar Python-module zstandard ontbreekt"
                ) from exc
            raw = zstandard.ZstdDecompressor().decompress(
                raw,
                max_output_size=MAX_RESPONSE_BYTES,
            )
        else:
            raise WattWanneerFout(f"onbekende HTTP-compressie: {encoding}")

        if len(raw) > MAX_RESPONSE_BYTES:
            raise WattWanneerFout("gedecomprimeerde forecast is groter dan 2 MiB")
    return raw


def normaliseer_forecast_payload(payload: Any) -> list[dict[str, Any]]:
    """Valideert de uurreeks en bewaart alleen velden die de strategie gebruikt."""
    if not isinstance(payload, list):
        raise WattWanneerFout("forecast-hoofdwaarde is geen JSON-array")
    if not MIN_FORECAST_REGELS <= len(payload) <= MAX_FORECAST_REGELS:
        raise WattWanneerFout(
            f"forecast bevat {len(payload)} regels; verwacht "
            f"{MIN_FORECAST_REGELS}-{MAX_FORECAST_REGELS}"
        )

    resultaat: list[dict[str, Any]] = []
    vorige_tijd: datetime | None = None
    generated_at_values: set[str] = set()
    bronnen: set[str] = set()

    for index, regel in enumerate(payload):
        if not isinstance(regel, dict):
            raise WattWanneerFout(f"forecastregel {index} is geen object")

        datetime_raw = str(regel.get("datetime") or "").strip()
        generated_at = str(regel.get("generated_at") or "").strip()
        source = str(regel.get("source") or "").strip()
        try:
            moment = datetime.strptime(datetime_raw, "%Y-%m-%d %H:%M")
            prijs = float(regel["price_eur_kwh"])
        except (KeyError, TypeError, ValueError) as exc:
            raise WattWanneerFout(
                f"forecastregel {index} heeft ongeldige datetime of price_eur_kwh"
            ) from exc

        if not math.isfinite(prijs) or abs(prijs) > 5.0:
            raise WattWanneerFout(f"forecastregel {index} heeft ongeldige prijs {prijs!r}")
        if source not in {"entsoe_day_ahead", "model"}:
            raise WattWanneerFout(f"forecastregel {index} heeft onbekende source {source!r}")
        if not generated_at:
            raise WattWanneerFout(f"forecastregel {index} mist generated_at")
        if vorige_tijd is not None and moment != vorige_tijd + timedelta(hours=1):
            raise WattWanneerFout(
                f"forecasttijd {datetime_raw} sluit niet aan op {vorige_tijd:%Y-%m-%d %H:%M}"
            )

        resultaat.append(
            {
                "datetime": datetime_raw,
                "price_eur_kwh": prijs,
                "source": source,
                "generated_at": generated_at,
            }
        )
        vorige_tijd = moment
        generated_at_values.add(generated_at)
        bronnen.add(source)

    if len(generated_at_values) != 1:
        raise WattWanneerFout("forecastregels hebben verschillende generated_at-waarden")
    if "model" not in bronnen:
        raise WattWanneerFout("forecast bevat geen modeluren")
    return resultaat


def download_wattwanneer_forecast(
    url: str = WATTWANNEER_URL,
    *,
    timeout: int = REQUEST_TIMEOUT_SECONDEN,
    opener: Callable[..., Any] = urlopen,
) -> list[dict[str, Any]]:
    """Haalt de JSON op met de door Firefox op macOS verzonden requestheaders."""
    headers = dict(FIREFOX_MAC_HEADERS)
    headers["Host"] = urlsplit(url).netloc
    request = Request(url, headers=headers, method="GET")
    try:
        with opener(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 200))
            if status != 200:
                raise WattWanneerFout(f"HTTP-status {status}")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise WattWanneerFout("gecomprimeerde forecast is groter dan 2 MiB")
            raw = _decodeer_response(raw, response.headers.get("Content-Encoding"))
    except HTTPError as exc:
        raise WattWanneerFout(f"HTTP-status {exc.code}") from exc
    except (TimeoutError, URLError, OSError) as exc:
        raise WattWanneerFout(f"netwerkfout: {exc}") from exc

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WattWanneerFout("response bevat geen geldige UTF-8 JSON") from exc
    return normaliseer_forecast_payload(payload)


class WattWanneerForecastCache:
    """Bewaart forecast en ophaaltijden in de gedeelde Zendure SQLite-database."""

    def __init__(
        self,
        *,
        db_path: str,
        url: str = WATTWANNEER_URL,
        downloader: Callable[[str], list[dict[str, Any]]] | None = None,
    ) -> None:
        self.db_path = db_path
        self.url = url
        self.downloader = downloader

    def _open(self) -> sqlite3.Connection:
        parent = Path(self.db_path).expanduser().parent
        parent.mkdir(parents=True, exist_ok=True)
        verbinding = sqlite3.connect(self.db_path, timeout=10)
        verbinding.row_factory = sqlite3.Row
        verbinding.execute("PRAGMA busy_timeout = 10000")
        verbinding.execute("PRAGMA journal_mode = WAL")
        verbinding.execute("PRAGMA foreign_keys = ON")
        return verbinding

    def initialiseer(self) -> None:
        with closing(self._open()) as verbinding, verbinding:
            verbinding.executescript(
                """
                CREATE TABLE IF NOT EXISTS wattwanneer_forecast_cache (
                    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                    last_attempt_at INTEGER,
                    last_success_at INTEGER,
                    last_status TEXT NOT NULL DEFAULT 'never',
                    generated_at TEXT,
                    payload_json TEXT,
                    last_error TEXT,
                    payload_fetch_id INTEGER,
                    last_attempt_fetch_id INTEGER,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS wattwanneer_forecast_fetches (
                    fetch_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    attempted_at_epoch INTEGER NOT NULL,
                    attempted_at_utc TEXT NOT NULL,
                    completed_at_epoch INTEGER,
                    completed_at_utc TEXT,
                    status TEXT NOT NULL,
                    url TEXT NOT NULL,
                    generated_at TEXT,
                    record_count INTEGER NOT NULL DEFAULT 0,
                    error TEXT
                );

                CREATE INDEX IF NOT EXISTS ix_wattwanneer_fetches_attempted
                    ON wattwanneer_forecast_fetches(attempted_at_epoch);

                CREATE TABLE IF NOT EXISTS wattwanneer_forecast_history (
                    fetch_id INTEGER NOT NULL,
                    fetched_at_epoch INTEGER NOT NULL,
                    fetched_at_utc TEXT NOT NULL,
                    forecast_slot_start_epoch INTEGER NOT NULL,
                    forecast_slot_start_iso TEXT NOT NULL,
                    forecast_slot_end_epoch INTEGER NOT NULL,
                    forecast_slot_end_iso TEXT NOT NULL,
                    forecast_datetime_local TEXT NOT NULL,
                    price_eur_kwh REAL NOT NULL,
                    source TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    PRIMARY KEY (fetch_id, forecast_datetime_local),
                    FOREIGN KEY (fetch_id)
                        REFERENCES wattwanneer_forecast_fetches(fetch_id)
                );

                CREATE INDEX IF NOT EXISTS ix_wattwanneer_history_slot
                    ON wattwanneer_forecast_history(forecast_slot_start_epoch);
                CREATE INDEX IF NOT EXISTS ix_wattwanneer_history_fetched
                    ON wattwanneer_forecast_history(fetched_at_epoch);

                CREATE TABLE IF NOT EXISTS nordpool_quarter_price_history (
                    price_version_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    price_entity TEXT NOT NULL,
                    slot_start_epoch INTEGER NOT NULL,
                    slot_start_iso TEXT NOT NULL,
                    slot_end_epoch INTEGER NOT NULL,
                    slot_end_iso TEXT NOT NULL,
                    price_eur_kwh REAL NOT NULL,
                    first_observed_at_epoch INTEGER NOT NULL,
                    first_observed_at_utc TEXT NOT NULL,
                    last_observed_at_epoch INTEGER NOT NULL,
                    last_observed_at_utc TEXT NOT NULL,
                    observation_count INTEGER NOT NULL DEFAULT 1,
                    first_series_name TEXT NOT NULL,
                    last_series_name TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS ix_nordpool_history_entity_slot
                    ON nordpool_quarter_price_history(
                        price_entity, slot_start_epoch, price_version_id
                    );
                CREATE INDEX IF NOT EXISTS ix_nordpool_history_observed
                    ON nordpool_quarter_price_history(first_observed_at_epoch);

                CREATE TABLE IF NOT EXISTS wattwanneer_price_calibration_history (
                    calibration_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    calculated_at_epoch INTEGER NOT NULL,
                    calculated_at_utc TEXT NOT NULL,
                    forecast_fetch_id INTEGER,
                    price_entity TEXT NOT NULL,
                    overlap_hours INTEGER NOT NULL,
                    factor REAL NOT NULL,
                    offset_eur_kwh REAL NOT NULL,
                    max_residual_eur_kwh REAL NOT NULL,
                    FOREIGN KEY (forecast_fetch_id)
                        REFERENCES wattwanneer_forecast_fetches(fetch_id)
                );

                CREATE INDEX IF NOT EXISTS ix_wattwanneer_calibration_calculated
                    ON wattwanneer_price_calibration_history(calculated_at_epoch);

                INSERT OR IGNORE INTO wattwanneer_forecast_cache (
                    singleton_id, last_status, updated_at
                ) VALUES (1, 'never', 0);
                """
            )
            kolommen = {
                str(row["name"])
                for row in verbinding.execute(
                    "PRAGMA table_info(wattwanneer_forecast_cache)"
                )
            }
            if "payload_fetch_id" not in kolommen:
                verbinding.execute(
                    "ALTER TABLE wattwanneer_forecast_cache "
                    "ADD COLUMN payload_fetch_id INTEGER"
                )
            if "last_attempt_fetch_id" not in kolommen:
                verbinding.execute(
                    "ALTER TABLE wattwanneer_forecast_cache "
                    "ADD COLUMN last_attempt_fetch_id INTEGER"
                )

    @staticmethod
    def _utc_iso(epoch: int) -> str:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat()

    @staticmethod
    def _forecast_slot_tijden(datetime_local: str) -> tuple[int, str, int, str]:
        start = datetime.strptime(datetime_local, "%Y-%m-%d %H:%M").replace(
            tzinfo=FORECAST_TIJDZONE
        )
        end = (
            start.astimezone(timezone.utc) + timedelta(hours=1)
        ).astimezone(FORECAST_TIJDZONE)
        return (
            int(start.timestamp()),
            start.isoformat(),
            int(end.timestamp()),
            end.isoformat(),
        )

    def bewaar_nordpool_prijzen(
        self,
        *,
        price_entity: str,
        slots: list[dict[str, Any]],
        observed_at_epoch: int,
    ) -> dict[str, int]:
        """Bewaart nieuwe prijsversies en telt herhaalde waarnemingen."""
        self.initialiseer()
        waargenomen = 0
        nieuwe_versies = 0
        observed_at_epoch = int(observed_at_epoch)
        observed_at_utc = self._utc_iso(observed_at_epoch)

        with closing(self._open()) as verbinding:
            verbinding.execute("BEGIN IMMEDIATE")
            try:
                for slot in slots:
                    start = slot["start"]
                    end = slot["end"]
                    if not isinstance(start, datetime) or not isinstance(end, datetime):
                        raise ValueError("Nordpool-slot heeft geen datetime start/end")
                    if start.tzinfo is None or end.tzinfo is None:
                        raise ValueError("Nordpool-slot heeft geen tijdzone")
                    prijs = float(slot["price"])
                    if not math.isfinite(prijs):
                        raise ValueError("Nordpool-slot heeft geen eindige prijs")
                    series_name = str(slot.get("source_series") or "unknown")
                    start_epoch = int(start.timestamp())
                    end_epoch = int(end.timestamp())
                    if end_epoch <= start_epoch:
                        raise ValueError("Nordpool-slot eindigt niet na de start")

                    laatste = verbinding.execute(
                        """
                        SELECT price_version_id, slot_end_epoch, price_eur_kwh
                        FROM nordpool_quarter_price_history
                        WHERE price_entity = ? AND slot_start_epoch = ?
                        ORDER BY price_version_id DESC
                        LIMIT 1
                        """,
                        (price_entity, start_epoch),
                    ).fetchone()
                    waargenomen += 1
                    if (
                        laatste is not None
                        and int(laatste["slot_end_epoch"]) == end_epoch
                        and float(laatste["price_eur_kwh"]) == prijs
                    ):
                        verbinding.execute(
                            """
                            UPDATE nordpool_quarter_price_history
                            SET last_observed_at_epoch = ?,
                                last_observed_at_utc = ?,
                                observation_count = observation_count + 1,
                                last_series_name = ?
                            WHERE price_version_id = ?
                            """,
                            (
                                observed_at_epoch,
                                observed_at_utc,
                                series_name,
                                int(laatste["price_version_id"]),
                            ),
                        )
                        continue

                    verbinding.execute(
                        """
                        INSERT INTO nordpool_quarter_price_history (
                            price_entity,
                            slot_start_epoch,
                            slot_start_iso,
                            slot_end_epoch,
                            slot_end_iso,
                            price_eur_kwh,
                            first_observed_at_epoch,
                            first_observed_at_utc,
                            last_observed_at_epoch,
                            last_observed_at_utc,
                            observation_count,
                            first_series_name,
                            last_series_name
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                        """,
                        (
                            price_entity,
                            start_epoch,
                            start.isoformat(),
                            end_epoch,
                            end.isoformat(),
                            prijs,
                            observed_at_epoch,
                            observed_at_utc,
                            observed_at_epoch,
                            observed_at_utc,
                            series_name,
                            series_name,
                        ),
                    )
                    nieuwe_versies += 1
                verbinding.commit()
            except Exception:
                verbinding.rollback()
                raise
        return {
            "waargenomen_slots": waargenomen,
            "nieuwe_prijsversies": nieuwe_versies,
        }

    def bewaar_prijskalibratie(
        self,
        *,
        calculated_at_epoch: int,
        forecast_fetch_id: int | None,
        price_entity: str,
        overlap_hours: int,
        factor: float,
        offset_eur_kwh: float,
        max_residual_eur_kwh: float,
    ) -> int:
        """Bewaart de omzetting die een strategierun op de forecast toepaste."""
        self.initialiseer()
        calculated_at_epoch = int(calculated_at_epoch)
        waarden = (float(factor), float(offset_eur_kwh), float(max_residual_eur_kwh))
        if not all(math.isfinite(waarde) for waarde in waarden):
            raise ValueError("prijsbasiskalibratie bevat geen eindige waarden")
        with closing(self._open()) as verbinding, verbinding:
            cursor = verbinding.execute(
                """
                INSERT INTO wattwanneer_price_calibration_history (
                    calculated_at_epoch,
                    calculated_at_utc,
                    forecast_fetch_id,
                    price_entity,
                    overlap_hours,
                    factor,
                    offset_eur_kwh,
                    max_residual_eur_kwh
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    calculated_at_epoch,
                    self._utc_iso(calculated_at_epoch),
                    forecast_fetch_id,
                    price_entity,
                    int(overlap_hours),
                    *waarden,
                ),
            )
            return int(cursor.lastrowid)

    @staticmethod
    def _volgende_poging(row: sqlite3.Row) -> int | None:
        laatste_poging = row["last_attempt_at"]
        if laatste_poging is None:
            return None
        interval = (
            SUCCES_INTERVAL_SECONDEN
            if row["last_status"] == "success"
            else FOUT_RETRY_INTERVAL_SECONDEN
        )
        return int(laatste_poging) + interval

    @classmethod
    def _bouw_resultaat(
        cls,
        row: sqlite3.Row,
        *,
        poging_uitgevoerd: bool,
    ) -> WattWanneerCacheResultaat:
        records: list[dict[str, Any]] = []
        fout = str(row["last_error"]) if row["last_error"] else None
        payload_json = row["payload_json"]
        if payload_json:
            try:
                records = normaliseer_forecast_payload(json.loads(payload_json))
            except (json.JSONDecodeError, WattWanneerFout) as exc:
                fout = f"SQLite-cache bevat ongeldige forecast: {exc}"
        elif row["last_status"] == "success":
            fout = "SQLite-cache heeft successtatus zonder forecastpayload"

        effectieve_status = str(row["last_status"])
        if fout and not records and effectieve_status == "success":
            effectieve_status = "failure"
        return WattWanneerCacheResultaat(
            records=records,
            laatste_status=effectieve_status,
            poging_uitgevoerd=poging_uitgevoerd,
            laatste_poging_epoch=row["last_attempt_at"],
            laatste_succes_epoch=row["last_success_at"],
            volgende_poging_epoch=cls._volgende_poging(row),
            generated_at=row["generated_at"],
            fout=fout,
            payload_fetch_id=row["payload_fetch_id"],
            laatste_poging_fetch_id=row["last_attempt_fetch_id"],
        )

    def lees_status(self) -> WattWanneerCacheResultaat:
        self.initialiseer()
        with closing(self._open()) as verbinding:
            row = verbinding.execute(
                "SELECT * FROM wattwanneer_forecast_cache WHERE singleton_id = 1"
            ).fetchone()
        if row is None:
            raise WattWanneerFout("SQLite-cache kon singleton-rij niet initialiseren")
        return self._bouw_resultaat(row, poging_uitgevoerd=False)

    def _reserveer_poging(
        self,
        now_epoch: int,
    ) -> tuple[bool, sqlite3.Row, int | None]:
        with closing(self._open()) as verbinding:
            verbinding.execute("BEGIN IMMEDIATE")
            try:
                row = verbinding.execute(
                    "SELECT * FROM wattwanneer_forecast_cache WHERE singleton_id = 1"
                ).fetchone()
                if row is None:
                    raise WattWanneerFout("SQLite-cache mist singleton-rij")
                volgende_poging = self._volgende_poging(row)
                if volgende_poging is not None and now_epoch < volgende_poging:
                    verbinding.commit()
                    return False, row, None

                cursor = verbinding.execute(
                    """
                    INSERT INTO wattwanneer_forecast_fetches (
                        attempted_at_epoch,
                        attempted_at_utc,
                        status,
                        url
                    ) VALUES (?, ?, 'in_progress', ?)
                    """,
                    (now_epoch, self._utc_iso(now_epoch), self.url),
                )
                fetch_id = int(cursor.lastrowid)

                verbinding.execute(
                    """
                    UPDATE wattwanneer_forecast_cache
                    SET last_attempt_at = ?,
                        last_status = 'in_progress',
                        last_error = NULL,
                        last_attempt_fetch_id = ?,
                        updated_at = ?
                    WHERE singleton_id = 1
                    """,
                    (now_epoch, fetch_id, now_epoch),
                )
                row = verbinding.execute(
                    "SELECT * FROM wattwanneer_forecast_cache WHERE singleton_id = 1"
                ).fetchone()
                verbinding.commit()
                if row is None:
                    raise WattWanneerFout("SQLite-cache verloor singleton-rij")
                return True, row, fetch_id
            except Exception:
                verbinding.rollback()
                raise

    def _registreer_fetch_fout(
        self,
        *,
        fetch_id: int,
        now_epoch: int,
        fout: str,
    ) -> sqlite3.Row:
        completed_at = max(int(now_epoch), int(time.time()))
        with closing(self._open()) as verbinding, verbinding:
            verbinding.execute(
                """
                UPDATE wattwanneer_forecast_fetches
                SET completed_at_epoch = ?,
                    completed_at_utc = ?,
                    status = 'failure',
                    error = ?
                WHERE fetch_id = ?
                """,
                (
                    completed_at,
                    self._utc_iso(completed_at),
                    fout,
                    fetch_id,
                ),
            )
            verbinding.execute(
                """
                UPDATE wattwanneer_forecast_cache
                SET last_status = 'failure',
                    last_error = ?,
                    updated_at = ?
                WHERE singleton_id = 1
                """,
                (fout, int(now_epoch)),
            )
            row = verbinding.execute(
                "SELECT * FROM wattwanneer_forecast_cache WHERE singleton_id = 1"
            ).fetchone()
        if row is None:
            raise WattWanneerFout("SQLite-cache verloor singleton-rij na fout")
        return row

    def haal(self, *, now_epoch: int) -> WattWanneerCacheResultaat:
        """Haalt alleen op wanneer de persistente succes- of foutinterval verstreken is."""
        self.initialiseer()
        uitvoeren, row, fetch_id = self._reserveer_poging(int(now_epoch))
        if not uitvoeren:
            return self._bouw_resultaat(row, poging_uitgevoerd=False)
        if fetch_id is None:
            raise WattWanneerFout("SQLite-cache reserveerde geen fetch_id")

        try:
            downloader = self.downloader or download_wattwanneer_forecast
            records = normaliseer_forecast_payload(downloader(self.url))
            generated_at = str(records[0]["generated_at"])
            payload_json = json.dumps(records, ensure_ascii=False, separators=(",", ":"))
        except Exception as exc:
            fout = str(exc).strip() or exc.__class__.__name__
            fout = fout[:500]
            row = self._registreer_fetch_fout(
                fetch_id=fetch_id,
                now_epoch=int(now_epoch),
                fout=fout,
            )
            return self._bouw_resultaat(row, poging_uitgevoerd=True)

        completed_at = max(int(now_epoch), int(time.time()))
        historie_regels = []
        for record in records:
            (
                slot_start_epoch,
                slot_start_iso,
                slot_end_epoch,
                slot_end_iso,
            ) = self._forecast_slot_tijden(str(record["datetime"]))
            historie_regels.append(
                (
                    fetch_id,
                    completed_at,
                    self._utc_iso(completed_at),
                    slot_start_epoch,
                    slot_start_iso,
                    slot_end_epoch,
                    slot_end_iso,
                    str(record["datetime"]),
                    float(record["price_eur_kwh"]),
                    str(record["source"]),
                    str(record["generated_at"]),
                )
            )

        try:
            with closing(self._open()) as verbinding, verbinding:
                verbinding.executemany(
                    """
                    INSERT INTO wattwanneer_forecast_history (
                        fetch_id,
                        fetched_at_epoch,
                        fetched_at_utc,
                        forecast_slot_start_epoch,
                        forecast_slot_start_iso,
                        forecast_slot_end_epoch,
                        forecast_slot_end_iso,
                        forecast_datetime_local,
                        price_eur_kwh,
                        source,
                        generated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    historie_regels,
                )
                verbinding.execute(
                    """
                    UPDATE wattwanneer_forecast_fetches
                    SET completed_at_epoch = ?,
                        completed_at_utc = ?,
                        status = 'success',
                        generated_at = ?,
                        record_count = ?,
                        error = NULL
                    WHERE fetch_id = ?
                    """,
                    (
                        completed_at,
                        self._utc_iso(completed_at),
                        generated_at,
                        len(records),
                        fetch_id,
                    ),
                )
                verbinding.execute(
                    """
                    UPDATE wattwanneer_forecast_cache
                    SET last_success_at = ?,
                        last_status = 'success',
                        generated_at = ?,
                        payload_json = ?,
                        payload_fetch_id = ?,
                        last_error = NULL,
                        updated_at = ?
                    WHERE singleton_id = 1
                    """,
                    (
                        int(now_epoch),
                        generated_at,
                        payload_json,
                        fetch_id,
                        int(now_epoch),
                    ),
                )
                row = verbinding.execute(
                    "SELECT * FROM wattwanneer_forecast_cache WHERE singleton_id = 1"
                ).fetchone()
        except Exception as exc:
            fout = f"forecast-history kon niet in SQLite worden opgeslagen: {exc}"
            row = self._registreer_fetch_fout(
                fetch_id=fetch_id,
                now_epoch=int(now_epoch),
                fout=fout[:500],
            )
            return self._bouw_resultaat(row, poging_uitgevoerd=True)
        if row is None:
            raise WattWanneerFout("SQLite-cache verloor singleton-rij na succes")
        return self._bouw_resultaat(row, poging_uitgevoerd=True)

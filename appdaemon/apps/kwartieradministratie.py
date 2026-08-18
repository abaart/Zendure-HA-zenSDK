"""Persistente kwartieradministratie voor de Zendure-batterij.

De administratie gebruikt Home Assistant Recorder uitsluitend als read-only bron
voor historische energiestanden. Alle eigen gegevens staan in een afzonderlijke
SQLite-database. ``quarter_start`` is de primaire sleutel, zodat backfill,
retries en normale kwartierverwerking dezelfde rij bijwerken en nooit dubbel
tellen.
"""

from __future__ import annotations

import math
import os
import sqlite3
import threading
import time
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import appdaemon.plugins.hass.hassapi as hass


KWARTIER_SECONDEN = 15 * 60
PRIJS_SCHAAL = 10_000_000
ONGELDIGE_STATEN = {"", "none", "unknown", "unavailable"}


def kwartier_start_utc(timestamp: float | int) -> int:
    """Geeft de UTC Unix-timestamp van de kwartiergrens."""
    return int(timestamp) // KWARTIER_SECONDEN * KWARTIER_SECONDEN


def _parse_tijdstip(waarde: Any) -> int | None:
    if isinstance(waarde, (int, float)) and math.isfinite(float(waarde)):
        return int(waarde)
    if not isinstance(waarde, str) or not waarde.strip():
        return None
    tekst = waarde.strip().replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(tekst)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp())


def _parse_prijs(waarde: Any) -> float | None:
    if isinstance(waarde, dict):
        waarde = waarde.get("amount")
    try:
        prijs = float(waarde)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(prijs):
        return None
    if abs(prijs) > 1_000:
        prijs /= PRIJS_SCHAAL
    return prijs


def normaliseer_forecast(forecast: Any) -> list[tuple[int, int, float]]:
    """Normaliseert Zonneplan-forecastregels naar UTC kwartierprijzen."""
    if not isinstance(forecast, list):
        return []

    resultaat: dict[int, tuple[int, int, float]] = {}
    for regel in forecast:
        if not isinstance(regel, dict):
            continue
        start = _parse_tijdstip(
            regel.get("start_date") or regel.get("start") or regel.get("datetime")
        )
        einde = _parse_tijdstip(regel.get("end_date") or regel.get("end"))
        prijs = _parse_prijs(
            regel.get("price_tax_included")
            or regel.get("electricity_price")
            or regel.get("price")
            or regel.get("value")
        )
        if start is None or prijs is None:
            continue
        start = kwartier_start_utc(start)
        if einde is None or einde <= start:
            einde = start + KWARTIER_SECONDEN
        if einde - start != KWARTIER_SECONDEN:
            continue
        resultaat[start] = (start, einde, prijs)
    return [resultaat[start] for start in sorted(resultaat)]


@dataclass(frozen=True)
class KwartierResultaat:
    quarter_start: int
    quarter_end: int
    price_eur_kwh: float
    import_start_kwh: float
    import_end_kwh: float
    export_start_kwh: float
    export_end_kwh: float
    import_kwh: float
    export_kwh: float
    import_cost_eur: float
    export_revenue_eur: float
    net_result_eur: float


class KwartierLedger:
    """Leest Recorder read-only en beheert de afzonderlijke kwartierdatabase."""

    def __init__(
        self,
        *,
        ledger_db_path: str,
        recorder_db_path: str,
        import_entity: str,
        export_entity: str,
        legacy_price_entity: str,
        timezone_name: str = "Europe/Amsterdam",
    ) -> None:
        self.ledger_db_path = ledger_db_path
        self.recorder_db_path = recorder_db_path
        self.import_entity = import_entity
        self.export_entity = export_entity
        self.legacy_price_entity = legacy_price_entity
        self.timezone = ZoneInfo(timezone_name)

    def _open_ledger(self) -> sqlite3.Connection:
        parent = Path(self.ledger_db_path).expanduser().parent
        parent.mkdir(parents=True, exist_ok=True)
        verbinding = sqlite3.connect(self.ledger_db_path, timeout=10)
        verbinding.row_factory = sqlite3.Row
        verbinding.execute("PRAGMA busy_timeout = 10000")
        verbinding.execute("PRAGMA journal_mode = WAL")
        return verbinding

    def _open_recorder(self) -> sqlite3.Connection:
        absoluut = os.path.abspath(os.path.expanduser(self.recorder_db_path))
        uri = f"file:{absoluut}?mode=ro"
        verbinding = sqlite3.connect(uri, uri=True, timeout=10)
        verbinding.row_factory = sqlite3.Row
        verbinding.execute("PRAGMA busy_timeout = 10000")
        return verbinding

    def initialiseer(self) -> None:
        with closing(self._open_ledger()) as verbinding, verbinding:
            verbinding.executescript(
                """
                CREATE TABLE IF NOT EXISTS zendure_quarters (
                    quarter_start INTEGER PRIMARY KEY,
                    quarter_end INTEGER NOT NULL,
                    price_eur_kwh REAL,
                    import_start_kwh REAL,
                    import_end_kwh REAL,
                    export_start_kwh REAL,
                    export_end_kwh REAL,
                    import_kwh REAL,
                    export_kwh REAL,
                    import_cost_eur REAL,
                    export_revenue_eur REAL,
                    net_result_eur REAL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    price_source TEXT,
                    settlement_source TEXT,
                    updated_at INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS ix_zendure_quarters_status_start
                    ON zendure_quarters(status, quarter_start);

                CREATE TABLE IF NOT EXISTS ledger_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                """
            )

    def bewaar_prijzen(
        self,
        prijzen: Iterable[tuple[int, int, float]],
        *,
        source: str,
        updated_at: int | None = None,
    ) -> int:
        regels = list(prijzen)
        if not regels:
            return 0
        nu = int(updated_at if updated_at is not None else time.time())
        with closing(self._open_ledger()) as verbinding, verbinding:
            verbinding.executemany(
                """
                INSERT INTO zendure_quarters (
                    quarter_start, quarter_end, price_eur_kwh,
                    status, price_source, updated_at
                ) VALUES (?, ?, ?, 'pending', ?, ?)
                ON CONFLICT(quarter_start) DO UPDATE SET
                    quarter_end = excluded.quarter_end,
                    price_eur_kwh = excluded.price_eur_kwh,
                    import_cost_eur = CASE
                        WHEN zendure_quarters.status = 'complete'
                          AND zendure_quarters.import_kwh IS NOT NULL
                        THEN zendure_quarters.import_kwh * excluded.price_eur_kwh
                        ELSE zendure_quarters.import_cost_eur
                    END,
                    export_revenue_eur = CASE
                        WHEN zendure_quarters.status = 'complete'
                          AND zendure_quarters.export_kwh IS NOT NULL
                        THEN zendure_quarters.export_kwh * excluded.price_eur_kwh
                        ELSE zendure_quarters.export_revenue_eur
                    END,
                    net_result_eur = CASE
                        WHEN zendure_quarters.status = 'complete'
                          AND zendure_quarters.import_kwh IS NOT NULL
                          AND zendure_quarters.export_kwh IS NOT NULL
                        THEN (zendure_quarters.export_kwh - zendure_quarters.import_kwh)
                             * excluded.price_eur_kwh
                        ELSE zendure_quarters.net_result_eur
                    END,
                    price_source = excluded.price_source,
                    updated_at = excluded.updated_at
                """,
                [(start, einde, prijs, source, nu) for start, einde, prijs in regels],
            )
        return len(regels)

    @staticmethod
    def _metadata_id(verbinding: sqlite3.Connection, entity_id: str) -> int | None:
        rij = verbinding.execute(
            "SELECT metadata_id FROM states_meta WHERE entity_id = ?",
            (entity_id,),
        ).fetchone()
        return int(rij[0]) if rij else None

    @staticmethod
    def _laatste_numerieke_staat(
        verbinding: sqlite3.Connection,
        metadata_id: int,
        timestamp: int,
    ) -> float | None:
        rij = verbinding.execute(
            """
            SELECT state
            FROM states
            WHERE metadata_id = ?
              AND COALESCE(last_updated_ts, last_changed_ts) <= ?
              AND lower(state) NOT IN ('', 'none', 'unknown', 'unavailable')
            ORDER BY COALESCE(last_updated_ts, last_changed_ts) DESC, state_id DESC
            LIMIT 1
            """,
            (metadata_id, timestamp),
        ).fetchone()
        if not rij:
            return None
        try:
            waarde = float(rij[0])
        except (TypeError, ValueError):
            return None
        return waarde if math.isfinite(waarde) else None

    def _bouw_resultaat(
        self,
        recorder: sqlite3.Connection,
        quarter_start: int,
        quarter_end: int,
        prijs: float,
    ) -> KwartierResultaat | None:
        import_id = self._metadata_id(recorder, self.import_entity)
        export_id = self._metadata_id(recorder, self.export_entity)
        if import_id is None or export_id is None:
            return None

        import_start = self._laatste_numerieke_staat(recorder, import_id, quarter_start)
        import_end = self._laatste_numerieke_staat(recorder, import_id, quarter_end)
        export_start = self._laatste_numerieke_staat(recorder, export_id, quarter_start)
        export_end = self._laatste_numerieke_staat(recorder, export_id, quarter_end)
        if None in (import_start, import_end, export_start, export_end):
            return None

        assert import_start is not None and import_end is not None
        assert export_start is not None and export_end is not None
        import_delta = import_end - import_start
        export_delta = export_end - export_start
        if import_delta < -1e-9 or export_delta < -1e-9:
            return None
        import_delta = max(0.0, import_delta)
        export_delta = max(0.0, export_delta)
        kosten = import_delta * prijs
        opbrengst = export_delta * prijs
        return KwartierResultaat(
            quarter_start=quarter_start,
            quarter_end=quarter_end,
            price_eur_kwh=prijs,
            import_start_kwh=import_start,
            import_end_kwh=import_end,
            export_start_kwh=export_start,
            export_end_kwh=export_end,
            import_kwh=import_delta,
            export_kwh=export_delta,
            import_cost_eur=kosten,
            export_revenue_eur=opbrengst,
            net_result_eur=opbrengst - kosten,
        )

    def settle_quarter(self, quarter_start: int, *, updated_at: int | None = None) -> bool:
        """Berekent en upsert precies één afgesloten kwartier."""
        with closing(self._open_ledger()) as ledger:
            prijsrij = ledger.execute(
                """
                SELECT quarter_end, price_eur_kwh
                FROM zendure_quarters
                WHERE quarter_start = ? AND price_eur_kwh IS NOT NULL
                """,
                (quarter_start,),
            ).fetchone()
        if not prijsrij:
            return False

        quarter_end = int(prijsrij["quarter_end"])
        prijs = float(prijsrij["price_eur_kwh"])
        if quarter_end - quarter_start != KWARTIER_SECONDEN:
            return False

        try:
            with closing(self._open_recorder()) as recorder:
                resultaat = self._bouw_resultaat(
                    recorder, quarter_start, quarter_end, prijs
                )
        except sqlite3.Error:
            return False
        if resultaat is None:
            return False

        nu = int(updated_at if updated_at is not None else time.time())
        with closing(self._open_ledger()) as ledger, ledger:
            ledger.execute(
                """
                UPDATE zendure_quarters SET
                    price_eur_kwh = ?,
                    import_start_kwh = ?, import_end_kwh = ?,
                    export_start_kwh = ?, export_end_kwh = ?,
                    import_kwh = ?, export_kwh = ?,
                    import_cost_eur = ?, export_revenue_eur = ?,
                    net_result_eur = ?, status = 'complete',
                    settlement_source = 'recorder_states', updated_at = ?
                WHERE quarter_start = ?
                """,
                (
                    resultaat.price_eur_kwh,
                    resultaat.import_start_kwh,
                    resultaat.import_end_kwh,
                    resultaat.export_start_kwh,
                    resultaat.export_end_kwh,
                    resultaat.import_kwh,
                    resultaat.export_kwh,
                    resultaat.import_cost_eur,
                    resultaat.export_revenue_eur,
                    resultaat.net_result_eur,
                    nu,
                    quarter_start,
                ),
            )
        return True

    def settle_recente_kwartieren(
        self,
        *,
        now_ts: int | None = None,
        backfill_days: int = 10,
    ) -> tuple[int, int]:
        nu = int(now_ts if now_ts is not None else time.time())
        laatste_einde = kwartier_start_utc(nu)
        vanaf = laatste_einde - max(1, backfill_days) * 86400
        with closing(self._open_ledger()) as verbinding:
            starts = [
                int(rij[0])
                for rij in verbinding.execute(
                    """
                    SELECT quarter_start
                    FROM zendure_quarters
                    WHERE price_eur_kwh IS NOT NULL
                      AND status != 'complete'
                      AND quarter_end <= ?
                      AND quarter_start >= ?
                    ORDER BY quarter_start
                    """,
                    (laatste_einde, vanaf),
                )
            ]

        geslaagd = 0
        for start in starts:
            if self.settle_quarter(start, updated_at=nu):
                geslaagd += 1
        return geslaagd, len(starts) - geslaagd

    def _meta(self, sleutel: str) -> str | None:
        with closing(self._open_ledger()) as verbinding:
            rij = verbinding.execute(
                "SELECT value FROM ledger_meta WHERE key = ?", (sleutel,)
            ).fetchone()
        return str(rij[0]) if rij else None

    def _bewaar_meta_eenmalig(self, waarden: dict[str, Any], now_ts: int) -> None:
        with closing(self._open_ledger()) as verbinding, verbinding:
            verbinding.executemany(
                "INSERT OR IGNORE INTO ledger_meta(key, value, updated_at) VALUES (?, ?, ?)",
                [(sleutel, str(waarde), now_ts) for sleutel, waarde in waarden.items()],
            )

    def initialiseer_legacy_openingssaldo(self, *, now_ts: int | None = None) -> bool:
        """Slaat de oude uurgebaseerde uitkomst eenmalig als openingssaldo op."""
        if self._meta("legacy_cutoff_utc") is not None:
            return True
        with closing(self._open_ledger()) as ledger:
            rij = ledger.execute(
                """
                SELECT MIN(quarter_start)
                FROM zendure_quarters
                WHERE status = 'complete'
                """
            ).fetchone()
        if not rij or rij[0] is None:
            return False
        cutoff = int(rij[0])

        query = """
            WITH ids AS (
              SELECT
                MAX(CASE WHEN statistic_id = ? THEN id END) AS import_id,
                MAX(CASE WHEN statistic_id = ? THEN id END) AS export_id,
                MAX(CASE WHEN statistic_id = ? THEN id END) AS price_id
              FROM statistics_meta
              WHERE statistic_id IN (?, ?, ?)
            ), hourly AS (
              SELECT
                i.start_ts,
                i."sum" AS import_sum,
                e."sum" AS export_sum,
                p.mean AS price
              FROM ids
              JOIN statistics i ON i.metadata_id = ids.import_id
              JOIN statistics e
                ON e.metadata_id = ids.export_id AND e.start_ts = i.start_ts
              JOIN statistics p
                ON p.metadata_id = ids.price_id AND p.start_ts = i.start_ts
              WHERE i.start_ts < ?
                AND i."sum" IS NOT NULL
                AND e."sum" IS NOT NULL
                AND p.mean IS NOT NULL
            ), deltas AS (
              SELECT
                start_ts,
                start_ts - LAG(start_ts) OVER (ORDER BY start_ts) AS interval_seconds,
                import_sum - LAG(import_sum) OVER (ORDER BY start_ts) AS import_delta,
                export_sum - LAG(export_sum) OVER (ORDER BY start_ts) AS export_delta,
                price
              FROM hourly
            )
            SELECT
              COALESCE(SUM(CASE WHEN interval_seconds = 3600 AND import_delta > 0
                                THEN import_delta * price ELSE 0 END), 0) AS import_cost,
              COALESCE(SUM(CASE WHEN interval_seconds = 3600 AND export_delta > 0
                                THEN export_delta * price ELSE 0 END), 0) AS export_revenue
            FROM deltas
        """
        parameters = (
            self.import_entity,
            self.export_entity,
            self.legacy_price_entity,
            self.import_entity,
            self.export_entity,
            self.legacy_price_entity,
            cutoff,
        )
        try:
            with closing(self._open_recorder()) as recorder:
                resultaat = recorder.execute(query, parameters).fetchone()
        except sqlite3.Error:
            return False
        if not resultaat:
            return False
        importkosten = float(resultaat["import_cost"] or 0.0)
        exportopbrengst = float(resultaat["export_revenue"] or 0.0)
        nu = int(now_ts if now_ts is not None else time.time())
        self._bewaar_meta_eenmalig(
            {
                "legacy_cutoff_utc": cutoff,
                "legacy_import_cost_eur": importkosten,
                "legacy_export_revenue_eur": exportopbrengst,
                "legacy_net_result_eur": exportopbrengst - importkosten,
                "legacy_source": "hourly_long_term_statistics",
            },
            nu,
        )
        return True

    def overzicht(self, *, now_ts: int | None = None, days: int = 30) -> dict[str, Any]:
        nu = int(now_ts if now_ts is not None else time.time())
        vanaf = nu - max(1, days) * 86400
        with closing(self._open_ledger()) as verbinding:
            meta = {
                str(rij["key"]): str(rij["value"])
                for rij in verbinding.execute("SELECT key, value FROM ledger_meta")
            }
            totalen = verbinding.execute(
                """
                SELECT
                  COUNT(*) AS complete_quarters,
                  COALESCE(SUM(import_cost_eur), 0) AS import_cost,
                  COALESCE(SUM(export_revenue_eur), 0) AS export_revenue,
                  COALESCE(SUM(net_result_eur), 0) AS net_result,
                  MAX(quarter_start) AS last_quarter
                FROM zendure_quarters
                WHERE status = 'complete'
                """
            ).fetchone()
            pending = verbinding.execute(
                "SELECT COUNT(*) FROM zendure_quarters WHERE status != 'complete' AND quarter_end <= ?",
                (kwartier_start_utc(nu),),
            ).fetchone()[0]
            oudere_netto = verbinding.execute(
                """
                SELECT COALESCE(SUM(net_result_eur), 0)
                FROM zendure_quarters
                WHERE status = 'complete' AND quarter_start < ?
                """,
                (vanaf,),
            ).fetchone()[0]
            recente_rijen = list(
                verbinding.execute(
                    """
                    SELECT quarter_start, import_cost_eur, export_revenue_eur, net_result_eur
                    FROM zendure_quarters
                    WHERE status = 'complete' AND quarter_start >= ?
                    ORDER BY quarter_start
                    """,
                    (vanaf,),
                )
            )

        legacy_import = float(meta.get("legacy_import_cost_eur", 0.0))
        legacy_export = float(meta.get("legacy_export_revenue_eur", 0.0))
        legacy_net = float(meta.get("legacy_net_result_eur", 0.0))
        per_dag: dict[str, dict[str, float]] = defaultdict(
            lambda: {"importkosten_eur": 0.0, "exportopbrengst_eur": 0.0, "netto_eur": 0.0}
        )
        for rij in recente_rijen:
            lokale_datum = datetime.fromtimestamp(
                int(rij["quarter_start"]), timezone.utc
            ).astimezone(self.timezone).date()
            dag = per_dag[lokale_datum.isoformat()]
            dag["importkosten_eur"] += float(rij["import_cost_eur"] or 0.0)
            dag["exportopbrengst_eur"] += float(rij["export_revenue_eur"] or 0.0)
            dag["netto_eur"] += float(rij["net_result_eur"] or 0.0)

        cumulatief = legacy_net + float(oudere_netto or 0.0)
        dagresultaten = []
        for datum in sorted(per_dag):
            waarden = per_dag[datum]
            cumulatief += waarden["netto_eur"]
            lokale_start = datetime.fromisoformat(datum).replace(tzinfo=self.timezone)
            dagresultaten.append(
                {
                    "start": lokale_start.isoformat(),
                    "importkosten_eur": round(waarden["importkosten_eur"], 4),
                    "exportopbrengst_eur": round(waarden["exportopbrengst_eur"], 4),
                    "netto_eur": round(waarden["netto_eur"], 4),
                    "cumulatief_eur": round(cumulatief, 4),
                }
            )

        return {
            "importkosten_totaal_eur": round(
                legacy_import + float(totalen["import_cost"] or 0.0), 4
            ),
            "exportopbrengst_totaal_eur": round(
                legacy_export + float(totalen["export_revenue"] or 0.0), 4
            ),
            "handelsresultaat_totaal_eur": round(
                legacy_net + float(totalen["net_result"] or 0.0), 4
            ),
            "complete_kwartieren": int(totalen["complete_quarters"] or 0),
            "openstaande_kwartieren": int(pending or 0),
            "laatste_kwartier_utc": (
                datetime.fromtimestamp(int(totalen["last_quarter"]), timezone.utc).isoformat()
                if totalen["last_quarter"] is not None
                else None
            ),
            "legacy_cutoff_utc": (
                datetime.fromtimestamp(int(meta["legacy_cutoff_utc"]), timezone.utc).isoformat()
                if meta.get("legacy_cutoff_utc")
                else None
            ),
            "legacy_berekening": meta.get("legacy_source"),
            "dagresultaten": dagresultaten,
        }


class ZendureKwartieradministratie(hass.Hass):
    """AppDaemon-koppeling voor prijsinname, backfill en dashboardpublicatie."""

    def initialize(self) -> None:
        verplichte_args = (
            "price_entity",
            "import_entity",
            "export_entity",
            "ledger_db_path",
            "recorder_db_path",
        )
        ontbrekend = [naam for naam in verplichte_args if not self.args.get(naam)]
        if ontbrekend:
            self.log(
                f"Kwartieradministratie mist apps.yaml-instellingen: {', '.join(ontbrekend)}",
                level="ERROR",
            )
            return

        self._price_entity = self.args["price_entity"]
        self._backfill_days = int(self.args.get("backfill_days", 10))
        self._dashboard_days = int(self.args.get("dashboard_days", 30))
        self._settlement_delay = int(self.args.get("settlement_delay_seconds", 90))
        self._status_entity = self.args.get(
            "status_entity", "sensor.zendure_2400_ac_kwartieradministratie"
        )
        self._lock = threading.Lock()
        self._ledger = KwartierLedger(
            ledger_db_path=self.args["ledger_db_path"],
            recorder_db_path=self.args["recorder_db_path"],
            import_entity=self.args["import_entity"],
            export_entity=self.args["export_entity"],
            legacy_price_entity=self.args.get(
                "legacy_price_entity", "sensor.zonneplan_current_electricity_tariff"
            ),
            timezone_name=self.args.get("timezone", "Europe/Amsterdam"),
        )
        self._ledger.initialiseer()
        self.listen_state(self._prijs_gewijzigd, self._price_entity, attribute="all")
        self.run_in(self._start_backfill, 5)

        nu = datetime.now(timezone.utc)
        volgende_grens = datetime.fromtimestamp(
            kwartier_start_utc(nu.timestamp()) + KWARTIER_SECONDEN,
            timezone.utc,
        )
        eerste_run = volgende_grens + timedelta(seconds=self._settlement_delay)
        self.run_every(self._kwartier_timer, eerste_run, KWARTIER_SECONDEN)
        self._publiceer_status()

    def _lees_en_bewaar_prijzen(self) -> int:
        alle_data = self.get_state(self._price_entity, attribute="all")
        attributes = alle_data.get("attributes", {}) if isinstance(alle_data, dict) else {}
        prijzen = normaliseer_forecast(attributes.get("forecast"))

        huidige_prijs = _parse_prijs(
            alle_data.get("state") if isinstance(alle_data, dict) else self.get_state(self._price_entity)
        )
        if huidige_prijs is not None:
            start = kwartier_start_utc(time.time())
            prijzen.append((start, start + KWARTIER_SECONDEN, huidige_prijs))
        return self._ledger.bewaar_prijzen(
            prijzen, source=self._price_entity, updated_at=int(time.time())
        )

    def _verwerk(self) -> None:
        if not self._lock.acquire(blocking=False):
            self.log("Kwartieradministratie verwerkt al een andere callback", level="DEBUG")
            return
        try:
            aantal_prijzen = self._lees_en_bewaar_prijzen()
            geslaagd, openstaand = self._ledger.settle_recente_kwartieren(
                backfill_days=self._backfill_days
            )
            legacy_ok = self._ledger.initialiseer_legacy_openingssaldo()
            self._publiceer_status()
            self.log(
                "Zendure kwartieradministratie: "
                f"{aantal_prijzen} prijzen opgeslagen, {geslaagd} kwartieren verwerkt, "
                f"{openstaand} nog zonder complete meting, legacy {'gereed' if legacy_ok else 'nog niet beschikbaar'}"
            )
        except (OSError, sqlite3.Error, ValueError) as fout:
            self.log(f"Zendure kwartieradministratie mislukt: {fout}", level="ERROR")
        finally:
            self._lock.release()

    def _start_backfill(self, **kwargs: Any) -> None:
        self._verwerk()

    def _kwartier_timer(self, **kwargs: Any) -> None:
        self._verwerk()

    def _prijs_gewijzigd(
        self,
        entity: str,
        attribute: str,
        old: Any,
        new: Any,
        **kwargs: Any,
    ) -> None:
        self._verwerk()

    def _publiceer_status(self) -> None:
        overzicht = self._ledger.overzicht(days=self._dashboard_days)
        totaal = overzicht.pop("handelsresultaat_totaal_eur")
        self.set_state(
            self._status_entity,
            state=str(totaal),
            attributes={
                "friendly_name": "Zendure 2400 AC Kwartieradministratie",
                "icon": "mdi:chart-timeline-variant-shimmer",
                "unit_of_measurement": "EUR",
                "device_class": "monetary",
                "state_class": "total",
                **overzicht,
            },
        )

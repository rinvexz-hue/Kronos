"""`MarketStore` implementation backed by local SQLite.

Schema:

- `bars(symbol, timeframe, ts_utc, open, high, low, close, volume,
  is_closed)`, `UNIQUE(symbol, timeframe, ts_utc)`. Upserts use
  `INSERT ... ON CONFLICT DO UPDATE`, so a re-fetched bar (e.g. the
  still-forming current candle) simply overwrites its previous row.
- `source_health(source_name PRIMARY KEY, ok, last_success_utc,
  consecutive_failures, last_error)` — one row per upstream source, so
  `source_health()` survives process restarts instead of only reflecting
  in-memory breaker state (see `ingest.py`, which calls
  `record_source_health` after every fetch attempt).

Bars that fail `quality.check_quality` are never written; `upsert_bars`
always returns the gate's `QualityGateResult` regardless of whether the
batch was accepted, per `base.py`'s contract.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from kmd.data.base import Bar, QualityGateResult, SourceHealth, Timeframe
from kmd.data.markets_config import MarketsConfig, get_markets_config
from kmd.data.quality import check_quality

# Comfortably more than quality.py's own 50-bar gap-detection window, so a
# gap that started slightly more than 50 bars ago is still visible in the
# merged timeline check_quality performs.
_GATE_HISTORY_BARS = 200

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    ts_utc TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL NOT NULL,
    is_closed INTEGER NOT NULL,
    UNIQUE(symbol, timeframe, ts_utc)
);
CREATE INDEX IF NOT EXISTS idx_bars_symbol_tf_ts ON bars(symbol, timeframe, ts_utc);

CREATE TABLE IF NOT EXISTS source_health (
    source_name TEXT PRIMARY KEY,
    ok INTEGER NOT NULL,
    last_success_utc TEXT,
    consecutive_failures INTEGER NOT NULL,
    last_error TEXT
);
"""


class StoreBusyError(RuntimeError):
    """Raised when SQLite reports the database is locked even after
    `busy_timeout` has been exhausted (see `__init__`'s `busy_timeout_ms`).
    """


def _bar_ts_key(bar: Bar) -> str:
    # Fixed-width (always-microsecond-precision) ISO-8601 so lexical string
    # ordering in SQL (`ORDER BY ts_utc`) matches chronological ordering
    # regardless of whether any given bar happens to have zero microseconds
    # (isoformat() otherwise omits the fractional part when it's exactly
    # zero, which would otherwise make two same-length-looking timestamps
    # sort incorrectly against a third of different length).
    return bar.ts_utc.isoformat(timespec="microseconds")


class SqliteStore:
    """`MarketStore` implementation backed by a local SQLite file (or
    `":memory:"` for tests).
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        markets_config: MarketsConfig | None = None,
        busy_timeout_ms: int = 5000,
    ) -> None:
        self._db_path = str(db_path)
        if self._db_path != ":memory:":
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

        config = markets_config or get_markets_config()
        self._always_open_by_symbol: dict[str, bool] = {
            instrument.display_symbol: config.sessions[instrument.session_name].always_open
            for instrument in config.all_instruments()
        }

    def close(self) -> None:
        self._conn.close()

    def upsert_bars(self, bars: list[Bar]) -> QualityGateResult:
        if not bars:
            return QualityGateResult(passed=True, issues=[])

        symbol, timeframe = bars[0].symbol, bars[0].timeframe
        for bar in bars:
            if bar.symbol != symbol or bar.timeframe != timeframe:
                raise ValueError(
                    "upsert_bars requires every bar in one call to share a single "
                    f"(symbol, timeframe); got {bar.symbol}/{bar.timeframe} mixed with "
                    f"{symbol}/{timeframe}"
                )

        existing = self.get_latest_bars(symbol, timeframe, limit=_GATE_HISTORY_BARS)
        always_open = self._always_open_by_symbol.get(symbol, True)
        result = check_quality(bars, existing, always_open=always_open)
        if not result.passed:
            return result

        try:
            with self._conn:
                for bar in bars:
                    self._conn.execute(
                        """
                        INSERT INTO bars
                            (symbol, timeframe, ts_utc, open, high, low, close, volume, is_closed)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(symbol, timeframe, ts_utc) DO UPDATE SET
                            open = excluded.open,
                            high = excluded.high,
                            low = excluded.low,
                            close = excluded.close,
                            volume = excluded.volume,
                            is_closed = excluded.is_closed
                        """,
                        (
                            bar.symbol,
                            bar.timeframe.value,
                            _bar_ts_key(bar),
                            bar.open,
                            bar.high,
                            bar.low,
                            bar.close,
                            bar.volume,
                            int(bar.is_closed),
                        ),
                    )
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower():
                raise StoreBusyError(
                    f"database locked while upserting {symbol}/{timeframe.value}: {exc}"
                ) from exc
            raise
        return result

    def get_latest_bars(self, symbol: str, timeframe: Timeframe, limit: int) -> list[Bar]:
        rows = self._conn.execute(
            """
            SELECT symbol, timeframe, ts_utc, open, high, low, close, volume, is_closed
            FROM bars
            WHERE symbol = ? AND timeframe = ?
            ORDER BY ts_utc DESC
            LIMIT ?
            """,
            (symbol, timeframe.value, limit),
        ).fetchall()
        return [self._row_to_bar(row) for row in reversed(rows)]

    def get_last_closed_ts(self, symbol: str, timeframe: Timeframe) -> datetime | None:
        row = self._conn.execute(
            """
            SELECT ts_utc FROM bars
            WHERE symbol = ? AND timeframe = ? AND is_closed = 1
            ORDER BY ts_utc DESC
            LIMIT 1
            """,
            (symbol, timeframe.value),
        ).fetchone()
        return datetime.fromisoformat(row[0]) if row is not None else None

    def source_health(self) -> list[SourceHealth]:
        rows = self._conn.execute(
            "SELECT source_name, ok, last_success_utc, consecutive_failures, last_error "
            "FROM source_health"
        ).fetchall()
        return [
            SourceHealth(
                source_name=row[0],
                ok=bool(row[1]),
                last_success_utc=datetime.fromisoformat(row[2]) if row[2] else None,
                consecutive_failures=row[3],
                last_error=row[4],
            )
            for row in rows
        ]

    def record_source_health(self, health: SourceHealth) -> None:
        """Persists a source's current health snapshot. Not part of
        `MarketStore` (builder-core never needs to write health, only read
        it via `source_health()`) — called by `ingest.py` after every fetch
        attempt so state survives process restarts.
        """
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO source_health
                    (source_name, ok, last_success_utc, consecutive_failures, last_error)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source_name) DO UPDATE SET
                    ok = excluded.ok,
                    last_success_utc = excluded.last_success_utc,
                    consecutive_failures = excluded.consecutive_failures,
                    last_error = excluded.last_error
                """,
                (
                    health.source_name,
                    int(health.ok),
                    health.last_success_utc.isoformat(timespec="microseconds")
                    if health.last_success_utc
                    else None,
                    health.consecutive_failures,
                    health.last_error,
                ),
            )

    @staticmethod
    def _row_to_bar(row: tuple[str, str, str, float, float, float, float, float, int]) -> Bar:
        return Bar(
            symbol=row[0],
            timeframe=Timeframe(row[1]),
            ts_utc=datetime.fromisoformat(row[2]),
            open=row[3],
            high=row[4],
            low=row[5],
            close=row[6],
            volume=row[7],
            is_closed=bool(row[8]),
        )

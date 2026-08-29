"""Calibration forecast logging.

Every forecast that is actually served (a cache miss that triggered a
fresh Monte Carlo run) is persisted here, in full: symbol, timeframe, the
lookback window's anchor (`last_closed_ts`) and size, every model
parameter that shaped the run, `generated_at_utc`, and the complete
predicted distribution summary (`p_up_24h`, `q10/q50/q90`,
`p_vol_expansion`, `band_width_pct`). Once `horizon_ts` (the timestamp of
the final predicted bar) has fully elapsed, `calibration/score.py` joins
this against the realized outcome and fills in the score columns.

A row can also end up `unscorable` instead of scored: for a non-24/7
instrument, a forecast's nominal `horizon_ts` can land inside that
instrument's closed weekly session, where no bar can ever exist at that
exact timestamp (red-team Round 1, finding #3). `score.py` first tries to
resolve against the first real closed bar at-or-after `horizon_ts`; only
if none arrives within a bounded catch-up window is the row marked
`unscorable` (`unscorable_at_utc`/`unscorable_reason`) so it stops being
rescanned on every refresh cycle forever — see `score.py` for the exact
policy and `DECISIONS.md` for why this was chosen over teaching the
forecast engine session-awareness.

Stored in its own `forecast_log` table in the same dedicated SQLite file
as `forecast/cache.py`'s `forecast_cache` table — a file kept deliberately
separate from builder-data's `SqliteStore` (`Settings.db_path`, `bars` /
`source_health`), since that schema belongs to a different layer. This
data must survive restarts (the whole point of calibration is scoring
forecasts made in a previous process lifetime), so it cannot live in
memory.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from kmd.data.base import Timeframe

_SCHEMA = """
CREATE TABLE IF NOT EXISTS forecast_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    generated_at_utc TEXT NOT NULL,
    last_closed_ts TEXT NOT NULL,
    horizon_ts TEXT NOT NULL,
    lookback_bars INTEGER NOT NULL,
    pred_len INTEGER NOT NULL,
    model_name TEXT NOT NULL,
    temperature REAL NOT NULL,
    top_p REAL NOT NULL,
    top_k INTEGER NOT NULL,
    n_paths INTEGER NOT NULL,
    last_close REAL NOT NULL,
    p_up_24h REAL NOT NULL,
    q10 REAL NOT NULL,
    q50 REAL NOT NULL,
    q90 REAL NOT NULL,
    p_vol_expansion REAL NOT NULL,
    band_width_pct REAL NOT NULL,
    scored_at_utc TEXT,
    brier_score REAL,
    mae_q50 REAL,
    in_band INTEGER,
    unscorable_at_utc TEXT,
    unscorable_reason TEXT,
    UNIQUE (symbol, timeframe, last_closed_ts)
);
"""

# Additive migration for a `forecast_log` table created before the
# unscorable columns existed (`CREATE TABLE IF NOT EXISTS` above only
# applies to a brand-new file). Safe to re-run: `duplicate column name` is
# swallowed, any other `OperationalError` is re-raised.
_MIGRATIONS = [
    "ALTER TABLE forecast_log ADD COLUMN unscorable_at_utc TEXT",
    "ALTER TABLE forecast_log ADD COLUMN unscorable_reason TEXT",
]


def _apply_migrations(conn: sqlite3.Connection) -> None:
    for statement in _MIGRATIONS:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise
    conn.commit()


@dataclass(frozen=True)
class ForecastLogRecord:
    symbol: str
    timeframe: Timeframe
    generated_at_utc: datetime
    last_closed_ts: datetime
    horizon_ts: datetime
    lookback_bars: int
    pred_len: int
    model_name: str
    temperature: float
    top_p: float
    top_k: int
    n_paths: int
    last_close: float
    p_up_24h: float
    q10: float
    q50: float
    q90: float
    p_vol_expansion: float
    band_width_pct: float
    id: int | None = None
    scored_at_utc: datetime | None = None
    brier_score: float | None = None
    mae_q50: float | None = None
    in_band: bool | None = None
    unscorable_at_utc: datetime | None = None
    unscorable_reason: str | None = None


def _row_to_record(row: sqlite3.Row) -> ForecastLogRecord:
    return ForecastLogRecord(
        id=row["id"],
        symbol=row["symbol"],
        timeframe=Timeframe(row["timeframe"]),
        generated_at_utc=datetime.fromisoformat(row["generated_at_utc"]),
        last_closed_ts=datetime.fromisoformat(row["last_closed_ts"]),
        horizon_ts=datetime.fromisoformat(row["horizon_ts"]),
        lookback_bars=row["lookback_bars"],
        pred_len=row["pred_len"],
        model_name=row["model_name"],
        temperature=row["temperature"],
        top_p=row["top_p"],
        top_k=row["top_k"],
        n_paths=row["n_paths"],
        last_close=row["last_close"],
        p_up_24h=row["p_up_24h"],
        q10=row["q10"],
        q50=row["q50"],
        q90=row["q90"],
        p_vol_expansion=row["p_vol_expansion"],
        band_width_pct=row["band_width_pct"],
        scored_at_utc=(
            datetime.fromisoformat(row["scored_at_utc"]) if row["scored_at_utc"] else None
        ),
        brier_score=row["brier_score"],
        mae_q50=row["mae_q50"],
        in_band=bool(row["in_band"]) if row["in_band"] is not None else None,
        unscorable_at_utc=(
            datetime.fromisoformat(row["unscorable_at_utc"]) if row["unscorable_at_utc"] else None
        ),
        unscorable_reason=row["unscorable_reason"],
    )


class CalibrationLogger:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._conn.execute(_SCHEMA)
        self._conn.commit()
        _apply_migrations(self._conn)

    def close(self) -> None:
        self._conn.close()

    def log_forecast(self, record: ForecastLogRecord) -> None:
        """Insert a new forecast log row. A duplicate `(symbol, timeframe,
        last_closed_ts)` (i.e. a cache hit that re-ran build_snapshot
        without a new closed bar) is silently ignored — a forecast is
        logged exactly once per genuinely new closed bar.
        """
        self._conn.execute(
            "INSERT OR IGNORE INTO forecast_log ("
            "symbol, timeframe, generated_at_utc, last_closed_ts, horizon_ts, "
            "lookback_bars, pred_len, model_name, temperature, top_p, top_k, n_paths, "
            "last_close, p_up_24h, q10, q50, q90, p_vol_expansion, band_width_pct"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.symbol,
                record.timeframe.value,
                record.generated_at_utc.astimezone(UTC).isoformat(),
                record.last_closed_ts.astimezone(UTC).isoformat(),
                record.horizon_ts.astimezone(UTC).isoformat(),
                record.lookback_bars,
                record.pred_len,
                record.model_name,
                record.temperature,
                record.top_p,
                record.top_k,
                record.n_paths,
                record.last_close,
                record.p_up_24h,
                record.q10,
                record.q50,
                record.q90,
                record.p_vol_expansion,
                record.band_width_pct,
            ),
        )
        self._conn.commit()

    def get_unscored_matured(self, now: datetime) -> list[ForecastLogRecord]:
        """Forecasts whose horizon has fully elapsed as of `now` (caller-
        supplied, never read from wall-clock internally), have not yet
        been scored, AND have not been marked `unscorable` (see
        `mark_unscorable`) — this is what keeps the set this query scans
        bounded rather than growing forever with rows that structurally
        can never resolve (red-team Round 1, finding #3). Callers must
        still verify a realized closed bar actually exists before scoring
        — this only filters on the claimed horizon, not on whether data
        has arrived.
        """
        now_iso = now.astimezone(UTC).isoformat()
        rows = self._conn.execute(
            "SELECT * FROM forecast_log WHERE scored_at_utc IS NULL "
            "AND unscorable_at_utc IS NULL AND horizon_ts <= ?",
            (now_iso,),
        ).fetchall()
        return [_row_to_record(row) for row in rows]

    def get_unscorable(self, symbol: str, timeframe: Timeframe) -> list[ForecastLogRecord]:
        """Rows marked `unscorable` (for introspection/tests — the
        dashboard's `CalibrationStats` never counts these one way or the
        other, they are simply excluded from the pending-scan set).
        """
        rows = self._conn.execute(
            "SELECT * FROM forecast_log WHERE symbol = ? AND timeframe = ? "
            "AND unscorable_at_utc IS NOT NULL",
            (symbol, timeframe.value),
        ).fetchall()
        return [_row_to_record(row) for row in rows]

    def get_scored(self, symbol: str, timeframe: Timeframe) -> list[ForecastLogRecord]:
        rows = self._conn.execute(
            "SELECT * FROM forecast_log WHERE symbol = ? AND timeframe = ? "
            "AND scored_at_utc IS NOT NULL",
            (symbol, timeframe.value),
        ).fetchall()
        return [_row_to_record(row) for row in rows]

    def mark_scored(
        self,
        record_id: int,
        *,
        scored_at_utc: datetime,
        brier_score: float,
        mae_q50: float,
        in_band: bool,
    ) -> None:
        self._conn.execute(
            "UPDATE forecast_log SET scored_at_utc = ?, brier_score = ?, mae_q50 = ?, in_band = ? "
            "WHERE id = ?",
            (
                scored_at_utc.astimezone(UTC).isoformat(),
                brier_score,
                mae_q50,
                int(in_band),
                record_id,
            ),
        )
        self._conn.commit()

    def mark_unscorable(self, record_id: int, *, at_utc: datetime, reason: str) -> None:
        """Marks a row as permanently excluded from `get_unscored_matured`
        without ever having been scored — used when no real closed bar
        arrived within the catch-up window `score.py` allows (see its
        `MAX_HORIZON_CATCHUP`), e.g. an extended session closure or a real
        data outage. Never touches `scored_at_utc`/the score columns:
        "unscorable" and "scored" are mutually exclusive states.
        """
        self._conn.execute(
            "UPDATE forecast_log SET unscorable_at_utc = ?, unscorable_reason = ? WHERE id = ?",
            (at_utc.astimezone(UTC).isoformat(), reason, record_id),
        )
        self._conn.commit()

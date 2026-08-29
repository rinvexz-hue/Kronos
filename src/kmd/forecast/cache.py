"""On-disk forecast cache.

Cache key exactly per the brief: `(symbol, timeframe, last_closed_ts,
model_name, T, top_p, n_paths, lookback, pred_len)`. A cache hit means "the
last closed bar has not advanced and every parameter that could change the
distribution is unchanged" — recompute only happens when a genuinely new
closed candle lands, never on wall-clock polling.

Stored in a dedicated SQLite file (its own `forecast_cache` table),
conventionally a sibling of `Settings.db_path` rather than that path
itself — `Settings.db_path` is builder-data's `SqliteStore` schema
(`bars`, `source_health`), and this module never writes into another
layer's database file. `calibration/logger.py`'s `forecast_log` table
shares this same dedicated file (see its module docstring) since both are
small, local, single-writer, forecast-adjacent stores with no reason to
be split further (documented in DECISIONS.md). Storing on disk (not
in-process memory) is
required so a scheduler-only restart doesn't force the API to serve
nothing (or force an unnecessary Kronos re-run) before the next scheduled
refresh.

Only close-price paths are cached, not full OHLCV — every metric in
`forecast/metrics.py` operates on the close series only (see its module
docstring for why), so caching full OHLCV would be pure overhead. If a
future metric needs high/low/volume path detail, this cache's schema (and
`MonteCarloResult` upstream) will need to grow accordingly.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from kmd.data.base import Timeframe
from kmd.forecast.engine import MonteCarloResult

_SCHEMA = """
CREATE TABLE IF NOT EXISTS forecast_cache (
    cache_key TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    last_closed_ts TEXT NOT NULL,
    model_name TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class ForecastCacheKey:
    symbol: str
    timeframe: Timeframe
    last_closed_ts: datetime
    model_name: str
    temperature: float
    top_p: float
    n_paths: int
    lookback_bars: int
    pred_len: int

    def digest(self) -> str:
        payload = "|".join(
            [
                self.symbol,
                self.timeframe.value,
                self.last_closed_ts.astimezone(UTC).isoformat(),
                self.model_name,
                repr(self.temperature),
                repr(self.top_p),
                str(self.n_paths),
                str(self.lookback_bars),
                str(self.pred_len),
            ]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CachedForecast:
    close_paths: list[list[float]]  # n_paths x pred_len
    y_timestamps: list[datetime]
    last_close: float
    last_closed_ts: datetime
    generated_at_utc: datetime


class ForecastCache:
    """Thin SQLite-backed cache. One connection per instance; safe for the
    scheduler's single-process, single-writer usage pattern (not intended
    for concurrent multi-process writers).
    """

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def get(self, key: ForecastCacheKey) -> CachedForecast | None:
        row = self._conn.execute(
            "SELECT payload_json FROM forecast_cache WHERE cache_key = ?",
            (key.digest(),),
        ).fetchone()
        if row is None:
            return None
        payload = json.loads(row[0])
        return CachedForecast(
            close_paths=payload["close_paths"],
            y_timestamps=[datetime.fromisoformat(ts) for ts in payload["y_timestamps"]],
            last_close=payload["last_close"],
            last_closed_ts=datetime.fromisoformat(payload["last_closed_ts"]),
            generated_at_utc=datetime.fromisoformat(payload["generated_at_utc"]),
        )

    def put(self, key: ForecastCacheKey, forecast: CachedForecast) -> None:
        payload = {
            "close_paths": forecast.close_paths,
            "y_timestamps": [ts.astimezone(UTC).isoformat() for ts in forecast.y_timestamps],
            "last_close": forecast.last_close,
            "last_closed_ts": forecast.last_closed_ts.astimezone(UTC).isoformat(),
            "generated_at_utc": forecast.generated_at_utc.astimezone(UTC).isoformat(),
        }
        self._conn.execute(
            "INSERT OR REPLACE INTO forecast_cache "
            "(cache_key, symbol, timeframe, last_closed_ts, model_name, created_at_utc, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                key.digest(),
                key.symbol,
                key.timeframe.value,
                key.last_closed_ts.astimezone(UTC).isoformat(),
                key.model_name,
                datetime.now(UTC).isoformat(),
                json.dumps(payload),
            ),
        )
        self._conn.commit()


def result_to_cached(result: MonteCarloResult, generated_at_utc: datetime) -> CachedForecast:
    return CachedForecast(
        close_paths=[path["close"].tolist() for path in result.paths],
        y_timestamps=result.y_timestamps,
        last_close=result.last_close,
        last_closed_ts=result.last_closed_ts,
        generated_at_utc=generated_at_utc,
    )

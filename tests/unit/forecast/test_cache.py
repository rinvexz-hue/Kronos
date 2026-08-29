"""Tests for `kmd.forecast.cache`. Covers the exact cache-key shape from
the brief and that the cache survives a process restart (a fresh
`ForecastCache` pointed at the same file must still see prior entries).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

from kmd.data.base import Timeframe
from kmd.forecast.cache import ForecastCache, ForecastCacheKey, result_to_cached
from kmd.forecast.engine import MonteCarloResult

BASE_TS = datetime(2026, 1, 1, tzinfo=UTC)


def _key(**overrides: object) -> ForecastCacheKey:
    defaults: dict[str, object] = {
        "symbol": "BTC/USDT",
        "timeframe": Timeframe.H1,
        "last_closed_ts": BASE_TS,
        "model_name": "NeoQuasar/Kronos-small",
        "temperature": 1.0,
        "top_p": 0.9,
        "n_paths": 30,
        "lookback_bars": 400,
        "pred_len": 24,
    }
    defaults.update(overrides)
    return ForecastCacheKey(**defaults)  # type: ignore[arg-type]


def _make_result(n_paths: int = 3, pred_len: int = 4) -> MonteCarloResult:
    y_timestamps = [BASE_TS + timedelta(hours=i + 1) for i in range(pred_len)]
    paths = []
    for p in range(n_paths):
        closes = [100.0 + p + i for i in range(pred_len)]
        paths.append(
            pd.DataFrame(
                {
                    "open": closes,
                    "high": closes,
                    "low": closes,
                    "close": closes,
                    "volume": [0.0] * pred_len,
                    "amount": [0.0] * pred_len,
                },
                index=pd.Index(y_timestamps),
            )
        )
    return MonteCarloResult(
        symbol="BTC/USDT",
        timeframe=Timeframe.H1,
        paths=paths,
        last_close=99.0,
        last_closed_ts=BASE_TS,
        y_timestamps=y_timestamps,
        model_name="NeoQuasar/Kronos-small",
        n_paths=n_paths,
        temperature=1.0,
        top_p=0.9,
        lookback_bars=400,
        pred_len=pred_len,
    )


def test_cache_miss_on_empty_cache(tmp_path: Path) -> None:
    cache = ForecastCache(tmp_path / "cache.sqlite3")
    assert cache.get(_key()) is None


def test_put_then_get_roundtrips(tmp_path: Path) -> None:
    cache = ForecastCache(tmp_path / "cache.sqlite3")
    result = _make_result()
    generated_at = BASE_TS + timedelta(minutes=1)
    cache.put(_key(), result_to_cached(result, generated_at))

    cached = cache.get(_key())
    assert cached is not None
    assert cached.close_paths == [[float(v) for v in p["close"]] for p in result.paths]
    assert cached.last_close == result.last_close
    assert cached.last_closed_ts == result.last_closed_ts
    assert cached.generated_at_utc == generated_at


def test_cache_key_changes_on_advanced_last_closed_ts(tmp_path: Path) -> None:
    cache = ForecastCache(tmp_path / "cache.sqlite3")
    cache.put(_key(), result_to_cached(_make_result(), BASE_TS))

    advanced_key = _key(last_closed_ts=BASE_TS + timedelta(hours=1))
    assert cache.get(advanced_key) is None  # new closed bar -> must recompute


def test_cache_key_changes_on_any_model_parameter(tmp_path: Path) -> None:
    cache = ForecastCache(tmp_path / "cache.sqlite3")
    cache.put(_key(), result_to_cached(_make_result(), BASE_TS))

    for overrides in (
        {"model_name": "NeoQuasar/Kronos-base"},
        {"temperature": 1.1},
        {"top_p": 0.8},
        {"n_paths": 31},
        {"lookback_bars": 401},
        {"pred_len": 25},
        {"symbol": "XRP/USDT"},
        {"timeframe": Timeframe.H4},
    ):
        assert cache.get(_key(**overrides)) is None, f"unexpected cache hit for {overrides}"


def test_cache_survives_reopening_the_same_file(tmp_path: Path) -> None:
    db_path = tmp_path / "cache.sqlite3"
    cache_a = ForecastCache(db_path)
    cache_a.put(_key(), result_to_cached(_make_result(), BASE_TS))
    cache_a.close()

    # Simulates a scheduler-only process restart: a brand new ForecastCache
    # instance pointed at the same file must still see the prior entry.
    cache_b = ForecastCache(db_path)
    cached = cache_b.get(_key())
    assert cached is not None
    assert cached.last_close == 99.0

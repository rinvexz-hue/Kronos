"""Shared test doubles for builder-core's own test suite.

Deliberately separate from `tests/unit/data/fakes.py` (builder-data's own
doubles) — builder-core only depends on `kmd.data.base`'s Protocols, so
its tests build fakes directly against that Protocol instead of reaching
into the data layer's internals.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd
import torch

from kmd.data.base import Bar, QualityGateResult, SourceHealth, Timeframe


def make_bar(
    *,
    symbol: str = "BTC/USDT",
    timeframe: Timeframe = Timeframe.H1,
    ts_utc: datetime,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: float = 100.0,
    is_closed: bool = True,
) -> Bar:
    return Bar(
        symbol=symbol,
        timeframe=timeframe,
        ts_utc=ts_utc,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        is_closed=is_closed,
    )


class FakeMarketStore:
    """Minimal in-memory `MarketStore`. Structurally satisfies
    `kmd.data.base.MarketStore` (checked via `isinstance` against the
    `runtime_checkable` Protocol in tests that care).
    """

    def __init__(self) -> None:
        self._bars: dict[tuple[str, Timeframe], list[Bar]] = {}
        self._health: list[SourceHealth] = []

    def set_bars(self, symbol: str, timeframe: Timeframe, bars: list[Bar]) -> None:
        self._bars[(symbol, timeframe)] = sorted(bars, key=lambda b: b.ts_utc)

    def set_health(self, health: list[SourceHealth]) -> None:
        self._health = health

    def upsert_bars(self, bars: list[Bar]) -> QualityGateResult:
        for bar in bars:
            key = (bar.symbol, bar.timeframe)
            existing = self._bars.setdefault(key, [])
            existing.append(bar)
            existing.sort(key=lambda b: b.ts_utc)
        return QualityGateResult(passed=True, issues=[])

    def get_latest_bars(self, symbol: str, timeframe: Timeframe, limit: int) -> list[Bar]:
        bars = self._bars.get((symbol, timeframe), [])
        return bars[-limit:]

    def get_last_closed_ts(self, symbol: str, timeframe: Timeframe) -> datetime | None:
        closed = [b for b in self._bars.get((symbol, timeframe), []) if b.is_closed]
        return max((b.ts_utc for b in closed), default=None)

    def source_health(self) -> list[SourceHealth]:
        return self._health


class FakePredictor:
    """Stand-in for `model.kronos.KronosPredictor`, used by every test
    that must never load real Kronos weights.

    Reproduces the ONE property `forecast/engine.py` actually depends on
    from the real `predict_batch`: given a batch of N duplicated input
    series, it returns N paths that are reproducible for a fixed
    `torch.manual_seed` call made by the caller immediately before
    `predict_batch`, but genuinely distinct from each other within one
    batch (each row of the batch consumes fresh draws from the same
    seeded global RNG stream) - exactly like the real autoregressive
    sampler. This is what lets `test_engine.py` assert both
    reproducibility AND genuine per-path distinctness.
    """

    def __init__(self, step_scale: float = 1.0) -> None:
        self.step_scale = step_scale
        self.calls: list[dict[str, Any]] = []

    def predict_batch(
        self,
        df_list: list[pd.DataFrame],
        x_timestamp_list: list[pd.Series],
        y_timestamp_list: list[pd.Series],
        pred_len: int,
        T: float = 1.0,
        top_k: int = 0,
        top_p: float = 0.9,
        sample_count: int = 1,
        verbose: bool = True,
    ) -> list[pd.DataFrame]:
        self.calls.append(
            {
                "n_series": len(df_list),
                "pred_len": pred_len,
                "T": T,
                "top_k": top_k,
                "top_p": top_p,
                "sample_count": sample_count,
            }
        )
        results: list[pd.DataFrame] = []
        for df, y_timestamp in zip(df_list, y_timestamp_list, strict=True):
            last_close = float(df["close"].iloc[-1])
            steps = torch.randn(pred_len).numpy().astype(np.float64) * self.step_scale
            closes = last_close + np.cumsum(steps)
            out = pd.DataFrame(
                {
                    "open": closes,
                    "high": closes,
                    "low": closes,
                    "close": closes,
                    "volume": np.zeros(pred_len),
                    "amount": np.zeros(pred_len),
                },
                index=pd.Index(y_timestamp),
            )
            results.append(out)
        return results

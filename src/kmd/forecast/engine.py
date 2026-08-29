"""Kronos inference engine.

The single most important invariant enforced here: only `is_closed=True`
bars are ever fed to the model as lookback context (see `NOTES/kronos_api.md`
and the look-ahead-bias defense described in `kmd/data/base.py::Bar`). The
second most important invariant: `KronosPredictor.predict()` /
`predict_batch()` with `sample_count > 1` AVERAGES the internal rollouts
into a single mean path before returning — it never exposes the individual
draws. To get a genuine Monte Carlo distribution we drive `predict_batch`
with N duplicated copies of the same lookback window, each contributing
`sample_count=1`, and treat each of the N returned DataFrames as one path
ourselves. See DECISIONS.md for why `predict_batch` (one autoregressive
loop) is used instead of N sequential `predict()` calls.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

import pandas as pd
import torch

from kmd import vendor  # noqa: F401  (import side effect: puts vendored Kronos on sys.path)
from kmd.config import Settings
from kmd.data.base import Bar, Timeframe

TIMEFRAME_DELTAS: dict[Timeframe, timedelta] = {
    Timeframe.H1: timedelta(hours=1),
    Timeframe.H4: timedelta(hours=4),
    Timeframe.D1: timedelta(days=1),
}

PRICE_VOLUME_COLUMNS = ["open", "high", "low", "close", "volume"]


class InsufficientHistoryError(ValueError):
    """Raised when there are not enough CLOSED bars to satisfy the
    configured lookback window."""


class UnclosedBarError(ValueError):
    """Raised if a bar with `is_closed=False` would otherwise have been fed
    to the model. This should be unreachable in normal operation because
    `select_closed_lookback` filters unclosed bars out before this check
    ever runs — it exists as an explicit, redundant guard per the brief's
    instruction to verify the look-ahead defense explicitly, not just rely
    on the filter working silently.
    """


class PredictorProtocol(Protocol):
    """Shape of `model.kronos.KronosPredictor` that `engine.py` depends on.
    Tests inject a fake implementing this protocol instead of loading the
    real Kronos weights.
    """

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
    ) -> list[pd.DataFrame]: ...


@dataclass(frozen=True)
class MonteCarloResult:
    """N independently-sampled predicted paths for one symbol, plus the
    parameters that produced them (needed by cache.py / calibration/logger.py
    for provenance).
    """

    symbol: str
    timeframe: Timeframe
    paths: list[pd.DataFrame]  # each: index=y_timestamp, columns=PRICE_VOLUME_COLUMNS (+amount)
    last_close: float
    last_closed_ts: datetime
    y_timestamps: list[datetime]
    model_name: str
    n_paths: int
    temperature: float
    top_p: float
    lookback_bars: int
    pred_len: int


def select_closed_lookback(bars: list[Bar], lookback_bars: int) -> list[Bar]:
    """Return exactly the last `lookback_bars` CLOSED bars, ascending by
    `ts_utc`. This is the system's primary look-ahead-bias defense.

    Raises `InsufficientHistoryError` if fewer than `lookback_bars` closed
    bars are available. A naive implementation that simply slices
    `bars[-lookback_bars:]` without filtering `is_closed` would silently
    let a still-forming bar leak into the model's context whenever the
    caller passes the store's raw "latest N" result (which by construction
    ends with the currently-forming bar) — that is exactly the bug this
    function exists to prevent.
    """
    closed = [b for b in bars if b.is_closed]
    if len(closed) < lookback_bars:
        raise InsufficientHistoryError(
            f"need {lookback_bars} closed bars, only {len(closed)} available"
        )
    window = sorted(closed, key=lambda b: b.ts_utc)[-lookback_bars:]
    # Explicit, redundant verification (see UnclosedBarError docstring).
    for bar in window:
        if not bar.is_closed:
            raise UnclosedBarError(f"unclosed bar {bar.ts_utc} leaked into lookback window")
    return window


def _bars_to_frame(bars: list[Bar]) -> tuple[pd.DataFrame, pd.Series]:
    """Convert closed bars (ascending) into the DataFrame + timestamp Series
    shape `KronosPredictor` expects. Timestamps are passed through exactly
    as stored (UTC, tz-aware) — Kronos itself performs no timezone handling,
    so this boundary is where UTC consistency is guaranteed.
    """
    df = pd.DataFrame(
        {
            "open": [b.open for b in bars],
            "high": [b.high for b in bars],
            "low": [b.low for b in bars],
            "close": [b.close for b in bars],
            "volume": [b.volume for b in bars],
        }
    )
    x_timestamp = pd.Series([b.ts_utc for b in bars])
    return df, x_timestamp


def _future_timestamps(last_closed_ts: datetime, timeframe: Timeframe, pred_len: int) -> list[datetime]:
    step = TIMEFRAME_DELTAS[timeframe]
    return [last_closed_ts + step * (i + 1) for i in range(pred_len)]


def load_predictor(settings: Settings) -> PredictorProtocol:
    """Load the real Kronos tokenizer/model/predictor per `Settings`.
    Kept separate from `run_monte_carlo` so tests never call this (it
    downloads real HF weights) and can inject a fake predictor instead.
    """
    from model import Kronos, KronosPredictor, KronosTokenizer

    tokenizer = KronosTokenizer.from_pretrained(settings.tokenizer_name)
    model = Kronos.from_pretrained(settings.model_name)
    predictor: PredictorProtocol = KronosPredictor(
        model,
        tokenizer,
        device=settings.device,
        max_context=settings.model_max_context,
    )
    return predictor


def run_monte_carlo(
    predictor: PredictorProtocol,
    symbol: str,
    timeframe: Timeframe,
    bars: list[Bar],
    *,
    lookback_bars: int,
    pred_len: int,
    n_paths: int,
    temperature: float,
    top_p: float,
    top_k: int,
    seed: int,
    model_name: str,
) -> MonteCarloResult:
    """Run N genuinely independent Monte Carlo rollouts for one symbol.

    Implementation: N duplicated copies of the same (closed-bars-only)
    lookback window are batched into a single `predict_batch` call with
    `sample_count=1` per copy — one autoregressive loop instead of N
    sequential ones (see DECISIONS.md, performance budget). `torch.manual_seed`
    is set once, immediately before the call, for full reproducibility: the
    batch dimension draws independent `torch.multinomial` samples per
    duplicate from the single seeded RNG stream, so this still yields N
    distinct, reproducible paths, never a repeated single path.
    """
    if n_paths < 1:
        raise ValueError("n_paths must be >= 1")

    window = select_closed_lookback(bars, lookback_bars)
    last_bar = window[-1]
    df, x_timestamp = _bars_to_frame(window)
    y_timestamps = _future_timestamps(last_bar.ts_utc, timeframe, pred_len)
    y_timestamp = pd.Series(y_timestamps)

    df_list = [df] * n_paths
    x_timestamp_list = [x_timestamp] * n_paths
    y_timestamp_list = [y_timestamp] * n_paths

    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)

    paths = predictor.predict_batch(
        df_list,
        x_timestamp_list,
        y_timestamp_list,
        pred_len,
        T=temperature,
        top_k=top_k,
        top_p=top_p,
        sample_count=1,
        verbose=False,
    )

    return MonteCarloResult(
        symbol=symbol,
        timeframe=timeframe,
        paths=paths,
        last_close=last_bar.close,
        last_closed_ts=last_bar.ts_utc,
        y_timestamps=y_timestamps,
        model_name=model_name,
        n_paths=n_paths,
        temperature=temperature,
        top_p=top_p,
        lookback_bars=lookback_bars,
        pred_len=pred_len,
    )

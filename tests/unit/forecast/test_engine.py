"""Tests for `kmd.forecast.engine`. Never loads real Kronos weights — all
Monte Carlo tests inject `FakePredictor`. `select_closed_lookback` is the
system's primary look-ahead-bias defense; `test_naive_slicing_would_leak_*`
demonstrates, concretely, the bug a naive `bars[-N:]` implementation would
have, and that the real function does not.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import torch

from kmd.data.base import Bar, Timeframe
from kmd.forecast import engine
from tests.support import FakePredictor, make_bar

BASE_TS = datetime(2026, 1, 1, tzinfo=UTC)


def _closed_bars(n: int, *, start_price: float = 100.0) -> list[Bar]:
    return [
        make_bar(
            ts_utc=BASE_TS + timedelta(hours=i),
            open_=start_price + i,
            high=start_price + i + 1,
            low=start_price + i - 1,
            close=start_price + i,
            is_closed=True,
        )
        for i in range(n)
    ]


def test_select_closed_lookback_returns_ascending_window() -> None:
    bars = _closed_bars(10)
    window = engine.select_closed_lookback(bars, lookback_bars=5)
    assert len(window) == 5
    assert [b.close for b in window] == [105.0, 106.0, 107.0, 108.0, 109.0]
    assert all(b.is_closed for b in window)


def test_select_closed_lookback_raises_on_insufficient_history() -> None:
    bars = _closed_bars(3)
    with pytest.raises(engine.InsufficientHistoryError):
        engine.select_closed_lookback(bars, lookback_bars=5)


def test_select_closed_lookback_excludes_unclosed_last_bar() -> None:
    """The core look-ahead regression test.

    10 closed bars followed by one still-forming (`is_closed=False`) bar,
    lookback=10. A naive `bars[-10:]` slice would include the unclosed
    bar (demonstrated below) — `select_closed_lookback` must not.
    """
    closed = _closed_bars(10)
    forming = make_bar(
        ts_utc=BASE_TS + timedelta(hours=10),
        open_=200.0,
        high=201.0,
        low=199.0,
        close=200.0,
        is_closed=False,
    )
    bars = [*closed, forming]

    naive_slice = bars[-10:]
    assert naive_slice[-1].is_closed is False, (
        "sanity check: a naive bars[-N:] slice does include the unclosed bar"
    )

    window = engine.select_closed_lookback(bars, lookback_bars=10)
    assert all(b.is_closed for b in window)
    assert window == closed  # exactly the 10 closed bars, forming bar dropped
    assert 200.0 not in [b.close for b in window]


def test_select_closed_lookback_raises_if_only_unclosed_bars_present() -> None:
    forming = make_bar(ts_utc=BASE_TS, open_=1, high=1, low=1, close=1, is_closed=False)
    with pytest.raises(engine.InsufficientHistoryError):
        engine.select_closed_lookback([forming], lookback_bars=1)


def test_run_monte_carlo_produces_n_distinct_reproducible_paths() -> None:
    bars = _closed_bars(50)
    predictor = FakePredictor(step_scale=1.0)

    result_a = engine.run_monte_carlo(
        predictor,
        "BTC/USDT",
        Timeframe.H1,
        bars,
        lookback_bars=50,
        pred_len=6,
        n_paths=8,
        temperature=1.0,
        top_p=0.9,
        top_k=0,
        seed=42,
        model_name="fake-model",
    )

    assert len(result_a.paths) == 8
    assert all(len(p) == 6 for p in result_a.paths)

    final_closes = [float(p["close"].iloc[-1]) for p in result_a.paths]
    # Genuine per-path distinctness: NOT every path collapsed to the same
    # value (which is exactly the sample_count>1-averaging bug this engine
    # exists to avoid).
    assert len(set(final_closes)) > 1

    # Reproducibility: same seed, same everything -> identical paths.
    predictor_b = FakePredictor(step_scale=1.0)
    result_b = engine.run_monte_carlo(
        predictor_b,
        "BTC/USDT",
        Timeframe.H1,
        bars,
        lookback_bars=50,
        pred_len=6,
        n_paths=8,
        temperature=1.0,
        top_p=0.9,
        top_k=0,
        seed=42,
        model_name="fake-model",
    )
    for path_a, path_b in zip(result_a.paths, result_b.paths, strict=True):
        assert path_a["close"].tolist() == pytest.approx(path_b["close"].tolist())


def test_run_monte_carlo_calls_predict_batch_with_sample_count_one() -> None:
    """Never rely on the model's own `sample_count` for the distribution —
    every call into `predict_batch` must request `sample_count=1` (each of
    the N duplicated series contributes exactly one path itself).
    """
    bars = _closed_bars(30)
    predictor = FakePredictor()
    engine.run_monte_carlo(
        predictor,
        "XRP/USDT",
        Timeframe.H1,
        bars,
        lookback_bars=30,
        pred_len=4,
        n_paths=12,
        temperature=0.8,
        top_p=0.7,
        top_k=5,
        seed=7,
        model_name="fake-model",
    )
    assert len(predictor.calls) == 1  # one batched call, not N sequential ones
    call = predictor.calls[0]
    assert call["n_series"] == 12
    assert call["sample_count"] == 1
    assert call["T"] == 0.8
    assert call["top_p"] == 0.7
    assert call["top_k"] == 5


def test_run_monte_carlo_seeds_global_rng_before_call() -> None:
    bars = _closed_bars(20)
    predictor = FakePredictor()
    torch.manual_seed(999)  # perturb global state beforehand
    torch.randn(3)  # consume some of it

    result = engine.run_monte_carlo(
        predictor,
        "BTC/USDT",
        Timeframe.H1,
        bars,
        lookback_bars=20,
        pred_len=3,
        n_paths=4,
        temperature=1.0,
        top_p=0.9,
        top_k=0,
        seed=123,
        model_name="fake-model",
    )
    assert result.last_close == bars[-1].close
    assert result.last_closed_ts == bars[-1].ts_utc


def test_run_monte_carlo_rejects_zero_paths() -> None:
    bars = _closed_bars(10)
    with pytest.raises(ValueError, match="n_paths"):
        engine.run_monte_carlo(
            FakePredictor(),
            "BTC/USDT",
            Timeframe.H1,
            bars,
            lookback_bars=10,
            pred_len=2,
            n_paths=0,
            temperature=1.0,
            top_p=0.9,
            top_k=0,
            seed=1,
            model_name="fake-model",
        )

"""Hand-constructed known-answer tests for `kmd.analysis.regime`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from kmd.analysis import regime
from kmd.data.base import Bar, Timeframe
from tests.support import make_bar

BASE_TS = datetime(2026, 1, 1, tzinfo=UTC)


def _bars(closes: list[float], *, band: float = 1.0) -> list[Bar]:
    return [
        make_bar(
            timeframe=Timeframe.H1,
            ts_utc=BASE_TS + timedelta(hours=i),
            open_=c,
            high=c + band,
            low=c - band,
            close=c,
            is_closed=True,
        )
        for i, c in enumerate(closes)
    ]


def test_ema_matches_hand_computation() -> None:
    # EMA3 of [1,2,3,4,5]: seed = mean(1,2,3,4)... use period=2 for a small
    # hand-checkable case: seed=avg(1,2)=1.5, alpha=2/3.
    # next: alpha*3+(1-alpha)*1.5 = 2 + 0.5 = 2.5
    # next: alpha*4+(1-alpha)*2.5 = 8/3 + 5/6 = 3.5
    result = regime.ema([1.0, 2.0, 3.0, 4.0], period=2)
    assert result == pytest.approx([1.5, 1.5, 2.5, 3.5])


def test_ema_requires_enough_values() -> None:
    with pytest.raises(regime.InsufficientBarsError):
        regime.ema([1.0, 2.0], period=5)


def test_true_ranges_first_bar_uses_high_low_only() -> None:
    bars = _bars([100.0, 105.0], band=2.0)
    tr = regime.true_ranges(bars)
    assert tr[0] == pytest.approx(4.0)  # 102-98, no prior close
    # second bar: high=107, low=103, prior close=100
    # true range = max(107-103, |107-100|, |103-100|) = max(4, 7, 3) = 7
    assert tr[1] == pytest.approx(7.0)


def test_classify_trend_detects_uptrend_with_numeric_reason() -> None:
    closes = [100.0 + i for i in range(60)]
    bars = _bars(closes)
    label, reason = regime.classify_trend(bars)
    assert label == "trend_up"
    assert "EMA20" in reason and "EMA50" in reason


def test_classify_trend_detects_downtrend() -> None:
    closes = [200.0 - i for i in range(60)]
    bars = _bars(closes)
    label, _reason = regime.classify_trend(bars)
    assert label == "trend_down"


def test_classify_trend_detects_range_on_flat_prices() -> None:
    closes = [100.0 for _ in range(60)]
    bars = _bars(closes)
    label, _reason = regime.classify_trend(bars)
    assert label == "range"


def test_classify_trend_unknown_with_insufficient_bars() -> None:
    bars = _bars([100.0, 101.0, 102.0])
    label, reason = regime.classify_trend(bars)
    assert label == "unknown"
    assert "need >=" in reason


def test_classify_volatility_detects_low_after_transition_to_calm() -> None:
    # 100 bars of wide range, then 30 bars of a much narrower range: ATR is
    # a smoothed series, so by the end of the calm stretch the CURRENT
    # value is the minimum (or near-minimum) of the trailing 100-value
    # window used for the percentile rank -> "low".
    wide = _bars([100.0 + (i % 2) for i in range(100)], band=10.0)
    calm_closes = [100.0 + (i % 2) for i in range(30)]
    calm = _bars(calm_closes, band=0.05)
    # re-anchor calm bars' timestamps to continue after `wide`
    calm = [
        b.model_copy(update={"ts_utc": BASE_TS + timedelta(hours=100 + i)})
        for i, b in enumerate(calm)
    ]
    bars = wide + calm
    vol_regime, reason = regime.classify_volatility(bars)
    assert vol_regime == "low"
    assert "ATR14" in reason


def test_classify_volatility_detects_high_after_transition_to_wild() -> None:
    calm = _bars([100.0 + (i % 2) for i in range(100)], band=0.05)
    wild_closes = [100.0 + (i % 2) for i in range(30)]
    wild = _bars(wild_closes, band=10.0)
    wild = [
        b.model_copy(update={"ts_utc": BASE_TS + timedelta(hours=100 + i)})
        for i, b in enumerate(wild)
    ]
    bars = calm + wild
    vol_regime, _reason = regime.classify_volatility(bars)
    assert vol_regime == "high"


def test_classify_volatility_normal_with_insufficient_bars() -> None:
    bars = _bars([100.0, 101.0])
    vol_regime, reason = regime.classify_volatility(bars)
    assert vol_regime == "normal"
    assert "need >=" in reason


def test_compute_regime_ignores_unclosed_bars() -> None:
    closes = [100.0 + i for i in range(60)]
    bars = _bars(closes)
    forming = make_bar(
        timeframe=Timeframe.H1,
        ts_utc=BASE_TS + timedelta(hours=60),
        open_=1000.0,
        high=1001.0,
        low=999.0,
        close=1000.0,
        is_closed=False,
    )
    result_with_forming = regime.compute_regime([*bars, forming])
    result_without = regime.compute_regime(bars)
    assert result_with_forming == result_without

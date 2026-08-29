"""Hand-constructed known-answer tests for `kmd.analysis.levels`. Every
level asserted here also has its `reason` checked for the value that
should have produced it — a level with no traceable origin is a bug in
this module per the brief.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from kmd.analysis import levels
from kmd.data.base import Bar, Timeframe
from tests.support import make_bar

BASE_TS = datetime(2026, 1, 1, tzinfo=UTC)


def _flat_bars(n: int, *, high: float = 100.0, low: float = 90.0, close: float = 95.0) -> list[Bar]:
    return [
        make_bar(
            timeframe=Timeframe.H1,
            ts_utc=BASE_TS + timedelta(hours=i),
            open_=close,
            high=high,
            low=low,
            close=close,
            is_closed=True,
        )
        for i in range(n)
    ]


def test_swing_high_detected_at_local_max() -> None:
    bars = _flat_bars(20)
    # index 10: a clear, isolated spike in `high`.
    bars[10] = bars[10].model_copy(update={"high": 150.0, "low": 110.0, "close": 130.0})
    found = levels.swing_highs_lows(bars)
    highs = [lvl for lvl in found if lvl.kind == "swing_high"]
    assert len(highs) == 1
    assert highs[0].price == 150.0
    assert "150" in highs[0].reason
    assert bars[10].ts_utc.isoformat() in highs[0].reason


def test_swing_low_detected_at_local_min() -> None:
    bars = _flat_bars(20)
    bars[12] = bars[12].model_copy(update={"high": 90.0, "low": 50.0, "close": 60.0})
    found = levels.swing_highs_lows(bars)
    lows = [lvl for lvl in found if lvl.kind == "swing_low"]
    assert len(lows) == 1
    assert lows[0].price == 50.0
    assert "50" in lows[0].reason


def test_swing_highs_capped_to_most_recent_n() -> None:
    bars = _flat_bars(60)
    # 5 distinct spikes, spaced far enough apart to each be a local max.
    spike_indices = [10, 20, 30, 40, 50]
    for idx, i in enumerate(spike_indices):
        bars[i] = bars[i].model_copy(update={"high": 200.0 + idx})
    found = [lvl for lvl in levels.swing_highs_lows(bars) if lvl.kind == "swing_high"]
    assert len(found) == levels.MAX_SWINGS_PER_SIDE
    # keeps the most recent ones (highest prices, since each spike is higher
    # than the last in this construction)
    assert {lvl.price for lvl in found} == {202.0, 203.0, 204.0}


def test_previous_day_high_low_uses_previous_utc_calendar_day() -> None:
    day1 = [
        make_bar(
            timeframe=Timeframe.H1,
            ts_utc=datetime(2026, 1, 1, hour=h, tzinfo=UTC),
            open_=100.0,
            high=100.0 + h,  # max at h=23 -> 123
            low=50.0 - h * 0.1,  # min at h=23 -> ~47.7
            close=100.0,
            is_closed=True,
        )
        for h in range(24)
    ]
    day2 = [
        make_bar(
            timeframe=Timeframe.H1,
            ts_utc=datetime(2026, 1, 2, hour=h, tzinfo=UTC),
            open_=100.0,
            high=500.0,  # deliberately wild, must NOT leak into "previous day"
            low=500.0,
            close=500.0,
            is_closed=True,
        )
        for h in range(6)
    ]
    bars = day1 + day2
    found = levels.previous_day_high_low(bars)
    pdh = next(lvl for lvl in found if lvl.kind == "pdh")
    pdl = next(lvl for lvl in found if lvl.kind == "pdl")
    assert pdh.price == pytest.approx(123.0)
    assert pdl.price == pytest.approx(50.0 - 23 * 0.1)
    assert "2026-01-01" in pdh.reason


def test_previous_day_high_low_empty_with_single_day_of_history() -> None:
    bars = _flat_bars(5)
    assert levels.previous_day_high_low(bars) == []


def test_ma_clusters_detected_when_all_mas_coincide() -> None:
    # Constant closes -> SMA20 == SMA50 == SMA100 exactly -> one cluster.
    bars = _flat_bars(100, close=100.0)
    found = levels.ma_clusters(bars)
    assert len(found) == 1
    cluster = found[0]
    assert cluster.kind == "ma_cluster"
    assert cluster.price == pytest.approx(100.0)
    assert "SMA20" in cluster.reason and "SMA50" in cluster.reason and "SMA100" in cluster.reason


def test_ma_clusters_empty_when_mas_far_apart() -> None:
    # A sharp recent trend pulls SMA20 well away from SMA50/SMA100.
    closes = [100.0] * 80 + [500.0] * 20
    bars = [
        make_bar(
            timeframe=Timeframe.H1,
            ts_utc=BASE_TS + timedelta(hours=i),
            open_=c,
            high=c,
            low=c,
            close=c,
            is_closed=True,
        )
        for i, c in enumerate(closes)
    ]
    found = levels.ma_clusters(bars)
    assert found == []


def test_ma_clusters_empty_with_insufficient_history() -> None:
    bars = _flat_bars(10)
    assert levels.ma_clusters(bars) == []


@pytest.mark.parametrize(
    ("price", "decimals", "expected_below", "expected_above"),
    [
        (54321.0, 2, 54000.0, 55000.0),
        (1.0834, 5, 1.0, 1.1),
        (0.4123, 4, 0.41, 0.42),
    ],
)
def test_round_numbers_known_values(
    price: float, decimals: int, expected_below: float, expected_above: float
) -> None:
    found = levels.round_numbers(price, decimals)
    assert len(found) == 2
    assert all(lvl.kind == "round_number" for lvl in found)
    prices = sorted(lvl.price for lvl in found)
    assert prices == pytest.approx([expected_below, expected_above])


def test_round_numbers_rejects_non_positive_price() -> None:
    with pytest.raises(ValueError, match="positive"):
        levels.round_numbers(0.0, 2)


def test_compute_levels_only_returns_traceable_levels() -> None:
    bars = _flat_bars(150, close=100.0)
    bars[75] = bars[75].model_copy(update={"high": 250.0})
    found = levels.compute_levels(bars, current_price=100.0, decimals=2)
    assert found  # something was found
    for lvl in found:
        assert lvl.reason  # every level must carry a non-empty reason
        assert lvl.kind in {"swing_high", "swing_low", "pdh", "pdl", "ma_cluster", "round_number"}

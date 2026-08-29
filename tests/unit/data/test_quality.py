"""Tests for the pure quality-gate functions in `quality.py`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from kmd.data.base import Bar, Timeframe
from kmd.data.quality import check_quality

SYMBOL = "BTC/USDT"
TF = Timeframe.H1
T0 = datetime(2024, 1, 1, tzinfo=UTC)


def make_bar(
    hour_offset: int,
    *,
    close: float = 100.0,
    is_closed: bool = True,
    symbol: str = SYMBOL,
    timeframe: Timeframe = TF,
) -> Bar:
    ts = T0 + timedelta(hours=hour_offset)
    return Bar(
        symbol=symbol,
        timeframe=timeframe,
        ts_utc=ts,
        open=close,
        high=close + 1.0,
        low=close - 1.0,
        close=close,
        volume=10.0,
        is_closed=is_closed,
    )


def test_empty_incoming_batch_passes_trivially() -> None:
    result = check_quality([], [make_bar(0)])
    assert result.passed
    assert result.issues == []


def test_clean_batch_passes_with_no_issues() -> None:
    existing = [make_bar(i) for i in range(5)]
    incoming = [make_bar(i) for i in range(5, 8)]
    result = check_quality(incoming, existing)
    assert result.passed
    assert result.issues == []


def test_single_missing_bar_is_not_flagged_as_a_gap() -> None:
    # Exactly one bar missing (hour 5). Per the "gap > 1 bar" spec, this
    # must NOT trip the gate - only 2+ consecutive missing bars should.
    existing = [make_bar(i) for i in range(5)]
    incoming = [make_bar(6)]  # hour 5 skipped
    result = check_quality(incoming, existing)
    assert result.passed
    assert not any(issue.kind == "gap" for issue in result.issues)


def test_multi_bar_gap_is_flagged() -> None:
    existing = [make_bar(i) for i in range(5)]
    incoming = [make_bar(8)]  # hours 5, 6, 7 missing: a real gap
    result = check_quality(incoming, existing)
    assert not result.passed
    gap_issues = [issue for issue in result.issues if issue.kind == "gap"]
    assert len(gap_issues) == 1
    assert gap_issues[0].ts_utc == T0 + timedelta(hours=8)
    assert gap_issues[0].symbol == SYMBOL
    assert gap_issues[0].timeframe == TF


def test_duplicate_timestamp_within_incoming_batch_is_flagged() -> None:
    dup_bar = make_bar(10)
    other_value_same_ts = make_bar(10, close=101.0)
    result = check_quality([dup_bar, other_value_same_ts], [])
    assert not result.passed
    assert any(issue.kind == "duplicate" for issue in result.issues)


def test_reoccurring_ts_against_existing_is_not_a_duplicate() -> None:
    # Re-fetching a bar you already have (the normal incremental-update /
    # still-forming-bar pattern) must never be reported as "duplicate" -
    # only two rows sharing a ts_utc *within the incoming batch itself*
    # count.
    existing = [make_bar(3, is_closed=False)]
    incoming = [make_bar(3, is_closed=False)]
    result = check_quality(incoming, existing)
    assert result.passed
    assert not any(issue.kind == "duplicate" for issue in result.issues)


def test_out_of_order_incoming_batch_is_flagged() -> None:
    incoming = [make_bar(3), make_bar(2)]
    result = check_quality(incoming, [])
    assert not result.passed
    assert any(issue.kind == "out_of_order" for issue in result.issues)


def test_revised_closed_history_is_flagged() -> None:
    existing = [make_bar(1, close=100.0, is_closed=True)]
    incoming = [make_bar(1, close=999.0, is_closed=True)]
    result = check_quality(incoming, existing)
    assert not result.passed
    revised = [issue for issue in result.issues if issue.kind == "revised_history"]
    assert len(revised) == 1
    assert revised[0].ts_utc == T0 + timedelta(hours=1)


def test_unclosed_bar_updating_in_place_is_not_flagged_as_revised() -> None:
    # A still-forming bar is *expected* to change (higher high, new close,
    # more volume) on every fetch until it closes - this must never be
    # treated as suspicious "revised history".
    existing = [make_bar(5, close=100.0, is_closed=False)]
    incoming = [make_bar(5, close=101.5, is_closed=False)]
    result = check_quality(incoming, existing)
    assert result.passed
    assert result.issues == []


def _fx_bar(ts: datetime, close: float, *, is_closed: bool = True) -> Bar:
    return Bar(
        symbol="EUR/USD",
        timeframe=TF,
        ts_utc=ts,
        open=close,
        high=close + 0.001,
        low=close - 0.001,
        close=close,
        volume=1.0,
        is_closed=is_closed,
    )


def test_weekend_gap_tolerated_for_non_always_open_instrument() -> None:
    friday_close = datetime(2024, 1, 5, 22, 0, tzinfo=UTC)
    sunday_open = datetime(2024, 1, 7, 23, 0, tzinfo=UTC)
    existing = [_fx_bar(friday_close, 1.1000)]
    incoming = [_fx_bar(sunday_open, 1.1005)]
    result = check_quality(incoming, existing, always_open=False)
    assert result.passed


def test_same_weekend_gap_flagged_for_always_open_instrument() -> None:
    friday_close = datetime(2024, 1, 5, 22, 0, tzinfo=UTC)
    sunday_open = datetime(2024, 1, 7, 23, 0, tzinfo=UTC)
    existing = [
        Bar(
            symbol=SYMBOL,
            timeframe=TF,
            ts_utc=friday_close,
            open=100,
            high=101,
            low=99,
            close=100,
            volume=1.0,
            is_closed=True,
        )
    ]
    incoming = [
        Bar(
            symbol=SYMBOL,
            timeframe=TF,
            ts_utc=sunday_open,
            open=100,
            high=101,
            low=99,
            close=100.5,
            volume=1.0,
            is_closed=True,
        )
    ]
    result = check_quality(incoming, existing, always_open=True)
    assert not result.passed
    assert any(issue.kind == "gap" for issue in result.issues)


def test_mismatched_symbol_in_incoming_raises() -> None:
    a = make_bar(1, symbol="BTC/USDT")
    b = make_bar(2, symbol="XRP/USDT")
    with pytest.raises(ValueError, match="single \\(symbol, timeframe\\)"):
        check_quality([a, b], [])


def test_mismatched_symbol_in_existing_raises() -> None:
    incoming = [make_bar(1, symbol="BTC/USDT")]
    existing = [make_bar(0, symbol="XRP/USDT")]
    with pytest.raises(ValueError, match="single \\(symbol, timeframe\\)"):
        check_quality(incoming, existing)


# --- Property-based tests -----------------------------------------------
#
# Random hour-offsets naturally produce duplicates (two draws of the same
# offset), out-of-order sequences (hypothesis doesn't sort them), and gaps
# (arbitrary spacing) - exactly the fault classes this gate exists to
# catch. The property under test is simply: check_quality must never raise
# for any combination of these, and must always return a well-formed
# result whose `passed` flag agrees with whether any issues were found.


@given(
    incoming_offsets=st.lists(st.integers(min_value=0, max_value=500), max_size=60),
    existing_offsets=st.lists(st.integers(min_value=0, max_value=500), max_size=60, unique=True),
)
def test_check_quality_never_crashes_on_arbitrary_batches(
    incoming_offsets: list[int], existing_offsets: list[int]
) -> None:
    existing = [make_bar(h, close=50.0) for h in sorted(existing_offsets)]
    incoming = [make_bar(h, close=100.0) for h in incoming_offsets]

    result = check_quality(incoming, existing)

    assert isinstance(result.passed, bool)
    assert result.passed == (len(result.issues) == 0)
    for issue in result.issues:
        assert issue.kind in {"gap", "duplicate", "out_of_order", "revised_history"}
        assert issue.symbol == SYMBOL
        assert issue.timeframe == TF

"""Tests for `timeutil.compute_is_closed` - the single most important
invariant in the system (see its docstring and `base.py`'s `Bar` docstring).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from kmd.data.base import Timeframe
from kmd.data.timeutil import compute_is_closed


def test_mid_candle_bar_is_not_closed() -> None:
    """Regression test with teeth.

    This must FAIL against a broken `compute_is_closed` that considers a
    bar closed the moment it *opens*, e.g.:

        def compute_is_closed(ts_utc, timeframe, now_utc):
            return now_utc >= ts_utc          # BROKEN: missing "+ duration"

    That broken version returns True as soon as `now_utc` reaches the
    bar's open time - 47 minutes before a 1h candle has actually finished
    forming - which is exactly the look-ahead-bias bug `base.py`'s
    docstring warns about. The correct implementation requires
    `now_utc >= ts_utc + duration`, so with `now` at :47 past the hour this
    must be False.
    """
    bar_open = datetime(2024, 6, 3, 10, 0, tzinfo=UTC)
    now_mid_candle = datetime(2024, 6, 3, 10, 47, tzinfo=UTC)

    assert compute_is_closed(bar_open, Timeframe.H1, now_mid_candle) is False


def test_bar_closes_the_instant_its_duration_elapses() -> None:
    bar_open = datetime(2024, 6, 3, 10, 0, tzinfo=UTC)
    exactly_at_close = datetime(2024, 6, 3, 11, 0, tzinfo=UTC)
    one_second_before_close = exactly_at_close - timedelta(seconds=1)

    assert compute_is_closed(bar_open, Timeframe.H1, exactly_at_close) is True
    assert compute_is_closed(bar_open, Timeframe.H1, one_second_before_close) is False


def test_4h_and_1d_bars_use_their_own_duration() -> None:
    bar_open = datetime(2024, 6, 3, 8, 0, tzinfo=UTC)

    assert compute_is_closed(bar_open, Timeframe.H4, bar_open + timedelta(hours=3)) is False
    assert compute_is_closed(bar_open, Timeframe.H4, bar_open + timedelta(hours=4)) is True

    assert compute_is_closed(bar_open, Timeframe.D1, bar_open + timedelta(hours=23)) is False
    assert compute_is_closed(bar_open, Timeframe.D1, bar_open + timedelta(days=1)) is True


def test_future_bar_is_never_closed() -> None:
    now = datetime(2024, 6, 3, 10, 0, tzinfo=UTC)
    future_bar_open = now + timedelta(hours=1)

    assert compute_is_closed(future_bar_open, Timeframe.H1, now) is False


def test_naive_datetimes_are_rejected() -> None:
    naive = datetime(2024, 6, 3, 10, 0)
    aware = datetime(2024, 6, 3, 10, 0, tzinfo=UTC)

    with pytest.raises(ValueError, match="tz-aware"):
        compute_is_closed(naive, Timeframe.H1, aware)
    with pytest.raises(ValueError, match="tz-aware"):
        compute_is_closed(aware, Timeframe.H1, naive)

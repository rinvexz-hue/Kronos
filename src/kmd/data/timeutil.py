"""Shared timeframe/clock helper for source adapters.

Not part of the builder-core/builder-data contract in `base.py` — a small
internal helper reused by `ccxt_source.py` and `yfinance_source.py` so the
`is_closed` computation (the single most important invariant in this
system, per the project brief) lives in exactly one place instead of being
reimplemented per source.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from kmd.data.base import Timeframe

TIMEFRAME_DURATIONS: dict[Timeframe, timedelta] = {
    Timeframe.H1: timedelta(hours=1),
    Timeframe.H4: timedelta(hours=4),
    Timeframe.D1: timedelta(days=1),
}


def compute_is_closed(ts_utc: datetime, timeframe: Timeframe, now_utc: datetime) -> bool:
    """A bar is closed only once its full interval has elapsed relative to
    wall-clock `now_utc`. Conservative by construction: any ambiguity (a
    bar timestamped in the future, clock skew, an interval that has not
    fully elapsed) resolves to `False`, never `True` — downstream forecast
    code is only allowed to treat `is_closed=True` bars as context, so a
    false positive here is a look-ahead-bias bug.
    """
    if ts_utc.tzinfo is None or now_utc.tzinfo is None:
        raise ValueError("compute_is_closed requires tz-aware datetimes")
    close_time = ts_utc + TIMEFRAME_DURATIONS[timeframe]
    return now_utc >= close_time

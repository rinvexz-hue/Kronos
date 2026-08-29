"""Market-hours computation for non-24/7 instruments (metals futures, FX,
index/context), using real IANA timezone data via `zoneinfo` — never a
fixed UTC offset, since these open/close instants shift relative to UTC
across DST transitions.

Known scope limitation: `config/markets.yaml`'s `index` session (used by
the `context` group — DXY, S&P 500) is explicitly commented there as "a
placeholder single-session template; real sessions are looked up
per-weekday ... this is not a full schedule" — it encodes exactly one
weekly (weekday, open-time)/(weekday, close-time) pair, not a genuine
Mon-Fri exchange calendar. `is_market_open` below implements precisely
what a `SessionSpec` configures: one weekly open->close window (which
correctly handles the Sunday-evening-to-Friday-evening wraparound used by
`fx`/`metals_futures`). For `index` as currently configured, that means it
faithfully reports "open" only within Monday 13:30-20:00 America/New_York
and "closed" the rest of the week - a real Mon-Fri index calendar would
need a schema capable of a distinct window per weekday, which is a
`markets.yaml`/`SessionSpec` schema change beyond a data-layer-only fix
(see DECISIONS.md).
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from kmd.data.markets_config import SessionSpec

_WEEKDAY_ABBR: dict[str, int] = {
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}

_MINUTES_PER_DAY = 24 * 60
_MINUTES_PER_WEEK = 7 * _MINUTES_PER_DAY


class SessionConfigError(ValueError):
    """Raised when a `SessionSpec`'s weekday/time strings are malformed."""


def _parse_weekday_time(value: str) -> tuple[int, int]:
    """Parses `"sun 23:00"` into `(weekday, minutes_of_day)` with
    `weekday` 0=Monday..6=Sunday (Python's `date.weekday()` convention).
    """
    try:
        day_str, time_str = value.strip().lower().split()
        weekday = _WEEKDAY_ABBR[day_str[:3]]
        hour_str, minute_str = time_str.split(":")
        minutes_of_day = int(hour_str) * 60 + int(minute_str)
    except (KeyError, ValueError) as exc:
        raise SessionConfigError(f"malformed weekday/time spec: {value!r}") from exc
    if not (0 <= minutes_of_day < _MINUTES_PER_DAY):
        raise SessionConfigError(f"time out of range in {value!r}")
    return weekday, minutes_of_day


def _week_minutes(weekday: int, minutes_of_day: int) -> int:
    return weekday * _MINUTES_PER_DAY + minutes_of_day


def is_market_open(session: SessionSpec, now_utc: datetime) -> bool:
    """True iff the instrument's market is open at `now_utc` (must be
    tz-aware). Always-open (crypto) sessions always report `True`.

    DST is handled by converting `now_utc` into the session's home IANA
    timezone (`session.timezone`) *before* comparing it against the
    configured weekday/time window, so the UTC-relative open/close instant
    shifts automatically across DST transitions exactly as the real
    exchange's published local hours do — this is why the comparison must
    happen with `zoneinfo`, not a fixed UTC offset computed once.
    """
    if session.always_open:
        return True
    if now_utc.tzinfo is None or now_utc.utcoffset() is None:
        raise ValueError("is_market_open requires a tz-aware `now_utc`")
    assert session.weekday_open is not None
    assert session.weekday_close is not None
    assert session.timezone is not None

    local_now = now_utc.astimezone(ZoneInfo(session.timezone))
    now_week_min = _week_minutes(local_now.weekday(), local_now.hour * 60 + local_now.minute)

    open_weekday, open_min = _parse_weekday_time(session.weekday_open)
    close_weekday, close_min = _parse_weekday_time(session.weekday_close)
    open_week_min = _week_minutes(open_weekday, open_min)
    close_week_min = _week_minutes(close_weekday, close_min)

    if open_week_min == close_week_min:
        # Degenerate zero-width window - must not be reported as always
        # open just because the wraparound branch below would otherwise
        # treat "equal" ambiguously.
        return False

    if open_week_min < close_week_min:
        return open_week_min <= now_week_min < close_week_min
    # The window wraps across the week boundary (e.g. fx/metals_futures,
    # which open Sunday evening and close Friday evening).
    return now_week_min >= open_week_min or now_week_min < close_week_min

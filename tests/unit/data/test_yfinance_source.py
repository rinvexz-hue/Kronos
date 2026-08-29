"""Tests for `YfinanceSource`. Zero network access - `FakeYfTicker` (see
`fakes.py`) replays fixtures shaped like `yfinance.Ticker.history()`'s real
output (`tests/fixtures/yfinance_gc_f_1h.csv` / `_1d.csv`).
"""

from __future__ import annotations

import random
from datetime import UTC, timedelta

import pandas as pd
import pytest

from kmd.data.base import Timeframe
from kmd.data.resilience import BackoffPolicy
from kmd.data.yfinance_source import YfFetchError, YfinanceSource

from .fakes import FakeYfTicker, FrozenClock, RecordingSleep, load_yf_fixture

HOURLY_FRAME = load_yf_fixture("yfinance_gc_f_1h.csv")
DAILY_FRAME = load_yf_fixture("yfinance_gc_f_1d.csv")
LAST_HOURLY_OPEN_UTC = HOURLY_FRAME.index[-1].to_pydatetime().astimezone(UTC)


def _source(ticker: FakeYfTicker, *, clock: FrozenClock) -> YfinanceSource:
    return YfinanceSource(
        ticker_factory=lambda _symbol: ticker,
        backoff=BackoffPolicy(max_attempts=3, base_delay_s=0.0, jitter_s=0.0),
        min_interval_s=0.0,
        clock=clock,
        sleep=RecordingSleep(clock=clock),
        rng=random.Random(0),
    )


def test_h1_fetch_converts_frame_to_bars() -> None:
    ticker = FakeYfTicker(HOURLY_FRAME)
    clock = FrozenClock(LAST_HOURLY_OPEN_UTC + timedelta(hours=2))
    source = _source(ticker, clock=clock)

    bars = source.fetch_ohlcv("GC=F", Timeframe.H1, since=None, limit=1000)

    assert len(bars) == len(HOURLY_FRAME)
    assert all(bar.timeframe is Timeframe.H1 for bar in bars)
    assert all(bar.ts_utc.tzinfo is UTC for bar in bars)
    assert bars[0].open == pytest.approx(float(HOURLY_FRAME.iloc[0]["Open"]))
    assert ticker.calls[-1]["interval"] == "1h"
    assert ticker.calls[-1]["period"] == "730d"


def test_d1_fetch_uses_1d_interval_and_max_period() -> None:
    ticker = FakeYfTicker(DAILY_FRAME)
    clock = FrozenClock(DAILY_FRAME.index[-1].to_pydatetime().astimezone(UTC) + timedelta(days=2))
    source = _source(ticker, clock=clock)

    bars = source.fetch_ohlcv("GC=F", Timeframe.D1, since=None, limit=1000)

    assert len(bars) == len(DAILY_FRAME)
    assert all(bar.timeframe is Timeframe.D1 for bar in bars)
    assert ticker.calls[-1]["interval"] == "1d"
    assert ticker.calls[-1]["period"] == "max"


def test_since_is_passed_through_as_start() -> None:
    ticker = FakeYfTicker(HOURLY_FRAME)
    clock = FrozenClock(LAST_HOURLY_OPEN_UTC + timedelta(hours=1))
    source = _source(ticker, clock=clock)
    since = HOURLY_FRAME.index[5].to_pydatetime()

    bars = source.fetch_ohlcv("GC=F", Timeframe.H1, since=since, limit=1000)

    assert ticker.calls[-1]["start"] == since
    assert ticker.calls[-1]["period"] is None
    assert bars[0].ts_utc == since.astimezone(UTC)


def test_most_recent_h1_bar_marked_unclosed_when_interval_has_not_elapsed() -> None:
    ticker = FakeYfTicker(HOURLY_FRAME)
    clock = FrozenClock(LAST_HOURLY_OPEN_UTC + timedelta(minutes=5))
    source = _source(ticker, clock=clock)

    bars = source.fetch_ohlcv("GC=F", Timeframe.H1, since=None, limit=1000)

    assert bars[-1].ts_utc == LAST_HOURLY_OPEN_UTC
    assert bars[-1].is_closed is False
    assert all(bar.is_closed for bar in bars[:-1])


def test_h4_resamples_four_hourly_bars_into_one_closed_bucket() -> None:
    ticker = FakeYfTicker(HOURLY_FRAME)
    # Freeze well after the whole fixture so every bucket that can be full
    # is full and closed.
    clock = FrozenClock(LAST_HOURLY_OPEN_UTC + timedelta(hours=6))
    source = _source(ticker, clock=clock)

    h1_bars = source.fetch_ohlcv("GC=F", Timeframe.H1, since=None, limit=1000)
    h4_bars = source.fetch_ohlcv("GC=F", Timeframe.H4, since=None, limit=1000)

    assert all(bar.timeframe is Timeframe.H4 for bar in h4_bars)
    # Every 4h UTC-aligned bucket start must be a multiple of 4 hours.
    assert all(bar.ts_utc.hour % 4 == 0 for bar in h4_bars)

    # Spot-check one fully-covered bucket against its 4 constituent 1h bars.
    bucket_start = h4_bars[0].ts_utc
    constituents = [b for b in h1_bars if bucket_start <= b.ts_utc < bucket_start + timedelta(hours=4)]
    if len(constituents) == 4:
        assert h4_bars[0].open == constituents[0].open
        assert h4_bars[0].close == constituents[-1].close
        assert h4_bars[0].high == max(c.high for c in constituents)
        assert h4_bars[0].low == min(c.low for c in constituents)
        assert h4_bars[0].volume == pytest.approx(sum(c.volume for c in constituents))
        assert h4_bars[0].is_closed is True


def test_h4_trailing_partial_bucket_is_never_closed() -> None:
    # The fixture's own last hour happens to complete its 4h bucket
    # exactly, so drop the last 2 hourly bars to leave a genuinely partial
    # (2-of-4) trailing bucket, and freeze "now" shortly after that
    # truncated series' last bar.
    partial_frame = HOURLY_FRAME.iloc[:-2]
    last_open = partial_frame.index[-1].to_pydatetime().astimezone(UTC)
    clock = FrozenClock(last_open + timedelta(minutes=30))
    ticker = FakeYfTicker(partial_frame)
    source = _source(ticker, clock=clock)

    h4_bars = source.fetch_ohlcv("GC=F", Timeframe.H4, since=None, limit=1000)

    assert h4_bars[-1].is_closed is False


def test_h4_drops_incomplete_interior_bucket_rather_than_fabricating() -> None:
    # Remove one hourly bar from the middle of the fixture to create a
    # source-side gap inside an otherwise-complete 4h bucket.
    frame = HOURLY_FRAME.drop(HOURLY_FRAME.index[5])
    ticker = FakeYfTicker(frame)
    clock = FrozenClock(LAST_HOURLY_OPEN_UTC + timedelta(hours=6))
    source = _source(ticker, clock=clock)

    h4_bars = source.fetch_ohlcv("GC=F", Timeframe.H4, since=None, limit=1000)

    # Compute the affected bucket from the *UTC* hour of the removed bar -
    # not its original (America/New_York) local hour, which would floor to
    # the wrong bucket entirely.
    gap_ts_utc = HOURLY_FRAME.index[5].to_pydatetime().astimezone(UTC)
    gap_bucket_start = gap_ts_utc.replace(
        hour=(gap_ts_utc.hour // 4) * 4, minute=0, second=0, microsecond=0
    )
    assert gap_bucket_start not in {bar.ts_utc for bar in h4_bars}


def test_transient_failure_is_retried_and_recovers() -> None:
    ticker = FakeYfTicker(HOURLY_FRAME, fail_next=2)
    clock = FrozenClock(LAST_HOURLY_OPEN_UTC + timedelta(hours=1))
    source = _source(ticker, clock=clock)

    bars = source.fetch_ohlcv("GC=F", Timeframe.H1, since=None, limit=1000)

    assert len(bars) == len(HOURLY_FRAME)
    assert source.health().ok is True


def test_persistent_failure_raises_yf_fetch_error() -> None:
    ticker = FakeYfTicker(HOURLY_FRAME, fail_next=10)
    clock = FrozenClock(LAST_HOURLY_OPEN_UTC + timedelta(hours=1))
    source = _source(ticker, clock=clock)

    with pytest.raises(YfFetchError):
        source.fetch_ohlcv("GC=F", Timeframe.H1, since=None, limit=1000)
    assert source.health().ok is False


def test_nan_close_is_raised_as_yf_fetch_error_not_silently_returned() -> None:
    """Red-team Round 2 (fault injection): a real yfinance response for a
    genuinely illiquid period can carry a NaN close rather than raising -
    observed directly against the real `YfinanceSource` code path here, not
    just asserted. Before the `Bar` NaN/Inf guard + widened try/except,
    this silently returned a `Bar(close=nan, is_closed=True)` to the caller
    and recorded the fetch as a *success* (breaker never learned anything
    was wrong) - the exact "malformed response treated as valid data"
    failure mode the brief calls out.
    """
    nan_frame = HOURLY_FRAME.copy()
    nan_frame.iloc[-1, nan_frame.columns.get_loc("Close")] = float("nan")
    ticker = FakeYfTicker(nan_frame)
    clock = FrozenClock(LAST_HOURLY_OPEN_UTC + timedelta(hours=2))
    source = _source(ticker, clock=clock)

    with pytest.raises(YfFetchError, match="must be a finite number"):
        source.fetch_ohlcv("GC=F", Timeframe.H1, since=None, limit=1000)

    health = source.health()
    assert health.ok is False  # correctly recorded as a failure, not a success
    assert health.consecutive_failures == 1


def test_empty_response_is_not_an_error_but_yields_zero_bars() -> None:
    """An empty frame (e.g. a genuinely malformed/empty upstream response,
    or - legitimately - no new bars since the last incremental check) is
    NOT wrapped as a failure today: it returns `[]` and the breaker records
    a success. Documented here as the current (accepted for incremental,
    ambiguous for a first backfill) behavior - see REVIEW.md Round 2.
    """
    empty_frame = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    ticker = FakeYfTicker(empty_frame)
    clock = FrozenClock(LAST_HOURLY_OPEN_UTC)
    source = _source(ticker, clock=clock)

    bars = source.fetch_ohlcv("GC=F", Timeframe.H1, since=None, limit=1000)

    assert bars == []
    assert source.health().ok is True


def test_limit_truncates_from_the_most_recent_end() -> None:
    ticker = FakeYfTicker(HOURLY_FRAME)
    clock = FrozenClock(LAST_HOURLY_OPEN_UTC + timedelta(hours=1))
    source = _source(ticker, clock=clock)

    bars = source.fetch_ohlcv("GC=F", Timeframe.H1, since=None, limit=3)

    assert len(bars) == 3
    assert bars[-1].ts_utc == LAST_HOURLY_OPEN_UTC

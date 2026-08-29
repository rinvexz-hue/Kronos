"""Tests for `CcxtSource`. Zero network access - `FakeCcxtExchange` (see
`fakes.py`) replays a recorded fixture (`tests/fixtures/ccxt_btc_usdt_1h.json`,
shaped exactly like `ccxt.Exchange.fetch_ohlcv`'s real return value).
"""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta

import pytest

from kmd.data.base import Timeframe
from kmd.data.ccxt_source import CcxtFetchError, CcxtSource
from kmd.data.resilience import BackoffPolicy, CircuitOpenError

from .fakes import FIXTURES_DIR, FakeCcxtExchange, FrozenClock, RecordingSleep

with (FIXTURES_DIR / "ccxt_btc_usdt_1h.json").open() as _f:
    BTC_1H_ROWS: list[list[float]] = json.load(_f)

LAST_BAR_OPEN = datetime.fromtimestamp(BTC_1H_ROWS[-1][0] / 1000, tz=UTC)


def _source(exchange: FakeCcxtExchange, *, clock: FrozenClock) -> CcxtSource:
    return CcxtSource(
        exchange,
        backoff=BackoffPolicy(max_attempts=3, base_delay_s=0.0, jitter_s=0.0),
        clock=clock,
        sleep=RecordingSleep(clock=clock),
        rng=random.Random(0),
    )


def test_fetch_ohlcv_converts_rows_to_bars() -> None:
    exchange = FakeCcxtExchange(BTC_1H_ROWS)
    clock = FrozenClock(LAST_BAR_OPEN + timedelta(hours=2))
    source = _source(exchange, clock=clock)

    bars = source.fetch_ohlcv("BTC/USDT", Timeframe.H1, since=None, limit=60)

    assert len(bars) == len(BTC_1H_ROWS)
    first = bars[0]
    assert first.symbol == "BTC/USDT"
    assert first.timeframe is Timeframe.H1
    assert first.ts_utc == datetime.fromtimestamp(BTC_1H_ROWS[0][0] / 1000, tz=UTC)
    assert first.open == BTC_1H_ROWS[0][1]
    assert first.close == BTC_1H_ROWS[0][4]
    assert first.is_closed is True  # long closed relative to the frozen clock


def test_most_recent_bar_marked_unclosed_when_interval_has_not_elapsed() -> None:
    exchange = FakeCcxtExchange(BTC_1H_ROWS)
    # Freeze "now" 10 minutes after the last bar's open - well inside its
    # still-forming 1h window.
    clock = FrozenClock(LAST_BAR_OPEN + timedelta(minutes=10))
    source = _source(exchange, clock=clock)

    bars = source.fetch_ohlcv("BTC/USDT", Timeframe.H1, since=None, limit=60)

    assert bars[-1].ts_utc == LAST_BAR_OPEN
    assert bars[-1].is_closed is False
    assert all(bar.is_closed for bar in bars[:-1])


def test_since_is_converted_to_milliseconds_and_passed_through() -> None:
    exchange = FakeCcxtExchange(BTC_1H_ROWS)
    clock = FrozenClock(LAST_BAR_OPEN + timedelta(hours=1))
    source = _source(exchange, clock=clock)
    since = datetime.fromtimestamp(BTC_1H_ROWS[10][0] / 1000, tz=UTC)

    bars = source.fetch_ohlcv("BTC/USDT", Timeframe.H1, since=since, limit=100)

    assert exchange.calls[-1] == ("BTC/USDT", "1h", int(since.timestamp() * 1000), 100)
    assert bars[0].ts_utc == since


def test_transient_failure_is_retried_and_recovers() -> None:
    exchange = FakeCcxtExchange(BTC_1H_ROWS, fail_next=2)
    clock = FrozenClock(LAST_BAR_OPEN + timedelta(hours=1))
    source = _source(exchange, clock=clock)

    bars = source.fetch_ohlcv("BTC/USDT", Timeframe.H1, since=None, limit=60)

    assert len(bars) == len(BTC_1H_ROWS)
    health = source.health()
    assert health.ok is True
    assert health.consecutive_failures == 0


def test_persistent_failure_raises_ccxt_fetch_error_and_updates_health() -> None:
    exchange = FakeCcxtExchange(BTC_1H_ROWS, fail_next=10)
    clock = FrozenClock(LAST_BAR_OPEN + timedelta(hours=1))
    source = _source(exchange, clock=clock)

    with pytest.raises(CcxtFetchError):
        source.fetch_ohlcv("BTC/USDT", Timeframe.H1, since=None, limit=60)

    health = source.health()
    assert health.ok is False
    assert health.consecutive_failures == 1


def test_circuit_breaker_opens_after_repeated_failures_across_calls() -> None:
    exchange = FakeCcxtExchange(BTC_1H_ROWS, fail_next=100)
    clock = FrozenClock(LAST_BAR_OPEN + timedelta(hours=1))
    source = CcxtSource(
        exchange,
        backoff=BackoffPolicy(max_attempts=1, base_delay_s=0.0, jitter_s=0.0),
        failure_threshold=3,
        circuit_reset_after_s=60.0,
        clock=clock,
        sleep=RecordingSleep(clock=clock),
        rng=random.Random(0),
    )

    for _ in range(3):
        with pytest.raises(CcxtFetchError):
            source.fetch_ohlcv("BTC/USDT", Timeframe.H1, since=None, limit=60)

    # The 4th call should be short-circuited by the breaker itself, without
    # even reaching the (still-failing) exchange.
    calls_before = len(exchange.calls)
    with pytest.raises(CircuitOpenError):
        source.fetch_ohlcv("BTC/USDT", Timeframe.H1, since=None, limit=60)
    assert len(exchange.calls) == calls_before


def test_malformed_row_is_raised_as_ccxt_fetch_error_not_silently_returned() -> None:
    """Red-team Round 2 (fault injection): a truncated/garbage OHLCV row
    (fewer than 6 fields) used to raise a raw, unwrapped `IndexError`
    *after* `breaker.on_success()` had already been called - recorded as a
    healthy fetch despite never actually producing a usable bar, and not
    recognized by `_fetch_with_fallback` as one of `_FETCH_ERRORS`, so a
    configured fallback would never even be tried. Confirmed here against
    the real `CcxtSource` code path.
    """
    exchange = FakeCcxtExchange([[1_700_000_000_000, 100.0, 101.0]])  # missing low/close/volume
    clock = FrozenClock(datetime.fromtimestamp(1_700_000_000, tz=UTC) + timedelta(hours=1))
    source = _source(exchange, clock=clock)

    with pytest.raises(CcxtFetchError, match="list index out of range"):
        source.fetch_ohlcv("BTC/USDT", Timeframe.H1, since=None, limit=60)

    health = source.health()
    assert health.ok is False  # correctly recorded as a failure, not a success
    assert health.consecutive_failures == 1


def test_nan_close_is_raised_as_ccxt_fetch_error_not_silently_returned() -> None:
    """Same failure mode, this time from a well-formed-shape row whose
    close is NaN (e.g. an exchange-side data glitch) rather than a
    truncated one - the `Bar` NaN/Inf guard is what actually catches this;
    this test proves it's wired all the way through to a well-typed,
    breaker-recorded `CcxtFetchError` rather than a silently-accepted
    poisoned bar.
    """
    exchange = FakeCcxtExchange([[1_700_000_000_000, 100.0, 101.0, 99.0, float("nan"), 10.0]])
    clock = FrozenClock(datetime.fromtimestamp(1_700_000_000, tz=UTC) + timedelta(hours=1))
    source = _source(exchange, clock=clock)

    with pytest.raises(CcxtFetchError, match="must be a finite number"):
        source.fetch_ohlcv("BTC/USDT", Timeframe.H1, since=None, limit=60)
    assert source.health().ok is False


def test_source_name_defaults_to_ccxt_prefixed_exchange_id() -> None:
    exchange = FakeCcxtExchange(BTC_1H_ROWS, id="binance")
    source = CcxtSource(exchange)
    assert source.name == "ccxt:binance"

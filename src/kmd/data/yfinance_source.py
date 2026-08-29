"""`MarketSource` for metals/FX/index instruments, backed by yfinance.

## Interval mapping (see NOTES/data_sources.md for the empirical findings
## this is based on, and why live verification was not possible)

yfinance/Yahoo Finance's `interval` strings do not line up 1:1 with this
project's `Timeframe` enum:

- `Timeframe.H1` -> yfinance interval `"1h"`. Yahoo restricts intraday data
  at this granularity to roughly the trailing 730 days, so backfill uses
  `period="730d"` (the documented cap for 60m/1h — smaller periods like
  "60d" apply only to sub-hour intervals).
- `Timeframe.D1` -> yfinance interval `"1d"`, `period="max"` for backfill
  (whatever daily history Yahoo actually has for the ticker).
- `Timeframe.H4` -> **Yahoo has no native 4-hour interval.** This adapter
  fetches `"1h"` bars and resamples them into 4h buckets aligned to UTC
  00/04/08/12/16/20 (`_resample_h1_to_h4`), matching how ccxt's native "4h"
  candles are aligned on Binance. A historical (non-trailing) bucket is
  only emitted when all 4 constituent hourly bars are present — an
  incomplete interior bucket is *dropped*, never synthesized from partial
  data, per the "never return a fabricated/interpolated bar" rule in
  `base.py`. The trailing (most-recent) bucket is always emitted from
  however many hourly bars exist so far, but is unconditionally treated as
  not-yet-closed on top of the usual `compute_is_closed` check.
"""

from __future__ import annotations

import random
import time
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol

from kmd.data.base import Bar, SourceHealth, Timeframe
from kmd.data.resilience import (
    BackoffPolicy,
    CircuitBreaker,
    MinIntervalLimiter,
    with_retry,
)
from kmd.data.timeutil import compute_is_closed

if TYPE_CHECKING:
    import pandas as pd

_YF_INTERVAL_FOR_TIMEFRAME: dict[Timeframe, str] = {
    Timeframe.H1: "1h",
    Timeframe.H4: "1h",  # fetched as 1h, resampled below - see module docstring
    Timeframe.D1: "1d",
}

_YF_BACKFILL_PERIOD_FOR_TIMEFRAME: dict[Timeframe, str] = {
    Timeframe.H1: "730d",
    Timeframe.H4: "730d",
    Timeframe.D1: "max",
}

_DEFAULT_MIN_INTERVAL_S = 0.5  # yfinance/Yahoo has no official rate-limit API


class YfHistoryClient(Protocol):
    """The minimal surface of a `yfinance.Ticker` this adapter needs. Real
    code gets one from `build_yf_ticker`; tests inject a fake that returns
    a canned `pandas.DataFrame` shaped like `Ticker.history()`'s real
    output (`DatetimeIndex` + Open/High/Low/Close/Volume columns), so the
    whole test suite runs with zero network access.
    """

    def history(
        self,
        *,
        interval: str,
        period: str | None = None,
        start: datetime | None = None,
        auto_adjust: bool = False,
    ) -> pd.DataFrame: ...


def build_yf_ticker(source_symbol: str) -> YfHistoryClient:
    """Lazily imports `yfinance` and constructs a real ticker. Kept out of
    module import time so tests never need `yfinance` installed to exercise
    `YfinanceSource` with a fake client.
    """
    import yfinance as yf

    ticker: YfHistoryClient = yf.Ticker(source_symbol)
    return ticker


class YfFetchError(RuntimeError):
    """Wraps any underlying yfinance/network error into one well-typed
    exception at the `MarketSource` boundary, per `base.py`'s contract.
    """


def _floor_to_4h(ts_utc: datetime) -> datetime:
    return ts_utc.replace(hour=(ts_utc.hour // 4) * 4, minute=0, second=0, microsecond=0)


def _resample_h1_to_h4(hourly_bars: list[Bar], symbol: str, now_utc: datetime) -> list[Bar]:
    """Aggregates closed-hour `Bar`s into 4h buckets. See the module
    docstring for the rules on incomplete buckets.
    """
    buckets: dict[datetime, list[Bar]] = defaultdict(list)
    for bar in sorted(hourly_bars, key=lambda b: b.ts_utc):
        buckets[_floor_to_4h(bar.ts_utc)].append(bar)

    bucket_starts = sorted(buckets)
    result: list[Bar] = []
    for i, bucket_start in enumerate(bucket_starts):
        members = buckets[bucket_start]
        is_trailing = i == len(bucket_starts) - 1
        if len(members) < 4 and not is_trailing:
            # An interior bucket is missing hourly bars (a source-side gap)
            # - drop it rather than synthesize a 4h OHLC from partial data.
            # The quality gate will separately flag the resulting gap in
            # the closed-bar timeline.
            continue
        is_closed = (
            len(members) == 4
            and all(m.is_closed for m in members)
            and compute_is_closed(bucket_start, Timeframe.H4, now_utc)
        )
        result.append(
            Bar(
                symbol=symbol,
                timeframe=Timeframe.H4,
                ts_utc=bucket_start,
                open=members[0].open,
                high=max(m.high for m in members),
                low=min(m.low for m in members),
                close=members[-1].close,
                volume=sum(m.volume for m in members),
                is_closed=is_closed,
            )
        )
    return result


class YfinanceSource:
    """Yahoo Finance (via yfinance), as a `MarketSource`. One instance
    serves every yfinance-backed instrument — unlike crypto, a "fallback"
    here is just an alternate ticker string on the same provider, resolved
    by `ingest.py`'s `SourceRegistry`, not a second `YfinanceSource`.
    """

    name = "yfinance"

    def __init__(
        self,
        *,
        ticker_factory: Callable[[str], YfHistoryClient] = build_yf_ticker,
        backoff: BackoffPolicy | None = None,
        failure_threshold: int = 5,
        circuit_reset_after_s: float = 60.0,
        min_interval_s: float = _DEFAULT_MIN_INTERVAL_S,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self._ticker_factory = ticker_factory
        self._backoff = backoff or BackoffPolicy()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._breaker = CircuitBreaker(
            failure_threshold=failure_threshold,
            reset_timeout_s=circuit_reset_after_s,
            clock=self._clock,
        )
        self._limiter = MinIntervalLimiter(min_interval_s, clock=self._clock, sleep=self._sleep)

    def fetch_ohlcv(
        self,
        source_symbol: str,
        timeframe: Timeframe,
        since: datetime | None,
        limit: int,
    ) -> list[Bar]:
        self._breaker.before_call()
        self._limiter.wait()

        yf_interval = _YF_INTERVAL_FOR_TIMEFRAME[timeframe]
        native_timeframe = Timeframe.D1 if yf_interval == "1d" else Timeframe.H1

        def _do_fetch() -> pd.DataFrame:
            client = self._ticker_factory(source_symbol)
            if since is not None:
                return client.history(interval=yf_interval, start=since, auto_adjust=False)
            period = _YF_BACKFILL_PERIOD_FOR_TIMEFRAME[timeframe]
            return client.history(interval=yf_interval, period=period, auto_adjust=False)

        try:
            frame = with_retry(_do_fetch, policy=self._backoff, sleep=self._sleep, rng=self._rng)
        except Exception as exc:  # deliberately wraps every failure mode into YfFetchError
            self._breaker.on_failure(str(exc))
            raise YfFetchError(
                f"{self.name}: fetch_ohlcv({source_symbol}, {timeframe.value}) failed: {exc}"
            ) from exc

        self._breaker.on_success()
        now = self._clock()
        native_bars = self._frame_to_bars(frame, source_symbol, native_timeframe, now)

        bars = (
            _resample_h1_to_h4(native_bars, source_symbol, now)
            if timeframe is Timeframe.H4
            else native_bars
        )
        if limit <= 0:
            return []
        return bars[-limit:] if limit < len(bars) else bars

    def health(self) -> SourceHealth:
        return self._breaker.health(self.name)

    @staticmethod
    def _frame_to_bars(
        frame: pd.DataFrame,
        source_symbol: str,
        timeframe: Timeframe,
        now: datetime,
    ) -> list[Bar]:
        bars: list[Bar] = []
        for index_value, row in frame.iterrows():
            ts_utc = _to_utc(index_value)
            volume = row["Volume"]
            bars.append(
                Bar(
                    symbol=source_symbol,
                    timeframe=timeframe,
                    ts_utc=ts_utc,
                    open=float(row["Open"]),
                    high=float(row["High"]),
                    low=float(row["Low"]),
                    close=float(row["Close"]),
                    volume=0.0 if volume != volume else float(volume),  # NaN check w/o numpy import
                    is_closed=compute_is_closed(ts_utc, timeframe, now),
                )
            )
        return bars


def _to_utc(index_value: object) -> datetime:
    """`Ticker.history()` returns a tz-aware `pandas.Timestamp` index (in
    the exchange's local timezone); this normalizes it to a tz-aware, UTC
    `datetime`, satisfying `Bar.ts_utc`'s validator.
    """
    ts = (
        index_value.to_pydatetime()
        if hasattr(index_value, "to_pydatetime")
        else index_value
    )
    if not isinstance(ts, datetime):
        raise TypeError(f"expected a datetime-like index value, got {type(ts)!r}")
    if ts.tzinfo is None:
        raise ValueError(
            "yfinance returned a tz-naive timestamp; this adapter requires tz-aware history "
            "(it should never happen with a real yfinance.Ticker.history() response)"
        )
    return ts.astimezone(UTC) - timedelta(seconds=0)

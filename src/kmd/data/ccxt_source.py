"""`MarketSource` for crypto instruments, backed by ccxt.

One `CcxtSource` instance represents exactly one exchange (per `base.py`'s
own docstring: "a ccxt exchange, yfinance, ..."). `config/markets.yaml`'s
Binance-primary/Coinbase-fallback pairing for each crypto instrument is
resolved one level up, in `ingest.py`'s `SourceRegistry` — this module has
no notion of "fallback" itself, it just talks to whichever single exchange
it was constructed for.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Protocol

from kmd.data.base import Bar, SourceHealth, Timeframe
from kmd.data.resilience import (
    BackoffPolicy,
    CircuitBreaker,
    MinIntervalLimiter,
    with_retry,
)
from kmd.data.timeutil import compute_is_closed

_DEFAULT_TIMEOUT_MS = 10_000


class CcxtExchangeLike(Protocol):
    """The minimal surface of a `ccxt.Exchange` this adapter needs. Real
    code passes an actual `ccxt.Exchange` (constructed by
    `build_ccxt_exchange`, below); tests pass a small fake so the whole
    test suite runs with zero network access and without ccxt installed.
    """

    id: str

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = "1h",
        since: int | None = None,
        limit: int | None = None,
    ) -> list[list[float]]: ...


class CcxtFetchError(RuntimeError):
    """Wraps any underlying ccxt/network error into one well-typed
    exception at the `MarketSource` boundary, per `base.py`'s contract.
    """


def build_ccxt_exchange(
    exchange_id: str,
    *,
    api_key: str = "",
    api_secret: str = "",
    timeout_ms: int = _DEFAULT_TIMEOUT_MS,
) -> CcxtExchangeLike:
    """Lazily imports `ccxt` and constructs a real exchange instance. Kept
    out of module import time so tests never need `ccxt` installed to
    exercise `CcxtSource` with a fake exchange, and so importing this
    module never has a network side effect.
    """
    import ccxt

    exchange_class = getattr(ccxt, exchange_id)
    exchange: CcxtExchangeLike = exchange_class(
        {
            "apiKey": api_key or None,
            "secret": api_secret or None,
            "timeout": timeout_ms,
            "enableRateLimit": True,
        }
    )
    return exchange


class CcxtSource:
    """One ccxt exchange, as a `MarketSource`."""

    def __init__(
        self,
        exchange: CcxtExchangeLike,
        *,
        name: str | None = None,
        backoff: BackoffPolicy | None = None,
        failure_threshold: int = 5,
        circuit_reset_after_s: float = 60.0,
        min_interval_s: float = 0.0,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self._exchange = exchange
        self.name = name or f"ccxt:{exchange.id}"
        self._backoff = backoff or BackoffPolicy()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._breaker = CircuitBreaker(
            failure_threshold=failure_threshold,
            reset_timeout_s=circuit_reset_after_s,
            clock=self._clock,
        )
        # ccxt exchanges rate-limit themselves internally when constructed
        # with enableRateLimit=True (see build_ccxt_exchange), so the
        # default here is a no-op; it exists for a fake/injected exchange
        # in tests, or an exchange constructed without that flag.
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

        since_ms = int(since.timestamp() * 1000) if since is not None else None

        def _do_fetch() -> list[list[float]]:
            return self._exchange.fetch_ohlcv(source_symbol, timeframe.value, since_ms, limit)

        try:
            raw_rows = with_retry(
                _do_fetch,
                policy=self._backoff,
                sleep=self._sleep,
                rng=self._rng,
            )
            now = self._clock()
            # Bar construction (row_to_bar - pydantic validation included, e.g.
            # the NaN/Inf OHLCV guard) is inside this same try block on
            # purpose: a malformed/truncated row is a failure of this fetch
            # exactly as much as a network error is, and must be reported the
            # same well-typed way (`CcxtFetchError`, breaker.on_failure) -
            # NOT recorded as `on_success()` with a bar list that never gets
            # built, and NOT allowed to escape as a raw, unwrapped
            # IndexError/ValueError that `_fetch_with_fallback` doesn't
            # recognize as retryable/fallback-triggering (red-team Round 2).
            bars = [self._row_to_bar(source_symbol, timeframe, row, now) for row in raw_rows]
        except Exception as exc:  # deliberately wraps every failure mode into CcxtFetchError
            self._breaker.on_failure(str(exc))
            raise CcxtFetchError(
                f"{self.name}: fetch_ohlcv({source_symbol}, {timeframe.value}) failed: {exc}"
            ) from exc

        self._breaker.on_success()
        return bars

    def health(self) -> SourceHealth:
        return self._breaker.health(self.name)

    @staticmethod
    def _row_to_bar(
        source_symbol: str,
        timeframe: Timeframe,
        row: Sequence[float],
        now: datetime,
    ) -> Bar:
        ts_ms, open_, high, low, close, volume = row[0], row[1], row[2], row[3], row[4], row[5]
        ts_utc = datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
        return Bar(
            symbol=source_symbol,
            timeframe=timeframe,
            ts_utc=ts_utc,
            open=float(open_),
            high=float(high),
            low=float(low),
            close=float(close),
            volume=float(volume),
            is_closed=compute_is_closed(ts_utc, timeframe, now),
        )

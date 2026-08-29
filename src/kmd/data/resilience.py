"""Retry-with-backoff, a minimal rate limiter, and a circuit breaker shared
by every source adapter. Kept dependency-free (no ccxt/yfinance/httpx
imports) and fully injectable (clock, sleep, rng) so it is trivially unit
testable without wall-clock waits or real network calls.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TypeVar

from kmd.data.base import SourceHealth

T = TypeVar("T")


@dataclass(frozen=True)
class BackoffPolicy:
    """Exponential backoff with jitter. `delay_for(attempt, rng)` returns
    the delay (seconds) to sleep *before* retrying, for the given 1-indexed
    attempt number that just failed.
    """

    max_attempts: int = 5
    base_delay_s: float = 0.5
    max_delay_s: float = 20.0
    jitter_s: float = 0.25

    def delay_for(self, attempt: int, rng: random.Random) -> float:
        raw: float = min(self.max_delay_s, self.base_delay_s * (2.0 ** (attempt - 1)))
        return float(raw + rng.uniform(0.0, self.jitter_s))


def with_retry(
    fn: Callable[[], T],
    *,
    policy: BackoffPolicy,
    sleep: Callable[[float], None],
    rng: random.Random,
    should_retry: Callable[[Exception], bool] = lambda _exc: True,
) -> T:
    """Calls `fn()`, retrying up to `policy.max_attempts` times with
    exponential backoff + jitter between attempts. Re-raises the last
    exception once attempts are exhausted or `should_retry` rejects it.
    """
    last_exc: Exception | None = None
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return fn()
        except Exception as exc:  # deliberately generic - re-raised below, never swallowed
            last_exc = exc
            if attempt >= policy.max_attempts or not should_retry(exc):
                raise
            sleep(policy.delay_for(attempt, rng))
    # Unreachable: the loop above always either returns or raises. Kept only
    # so mypy sees every path returning/raising.
    assert last_exc is not None
    raise last_exc


class CircuitBreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    """Raised when the breaker is open and calls are being short-circuited
    without even attempting the network — the whole point of a circuit
    breaker being to stop hammering a source that has already demonstrated
    it is down.
    """


class CircuitBreaker:
    """Opens after `failure_threshold` *consecutive* failures, then
    short-circuits (`before_call` raises `CircuitOpenError`) for
    `reset_timeout_s` before allowing one trial ("half-open") call through.
    """

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        reset_timeout_s: float = 60.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._reset_timeout_s = reset_timeout_s
        self._clock = clock or (lambda: datetime.now(UTC))
        self._consecutive_failures = 0
        self._state = CircuitBreakerState.CLOSED
        self._opened_at: datetime | None = None
        self._last_success: datetime | None = None
        self._last_error: str | None = None

    def before_call(self) -> None:
        """Raises `CircuitOpenError` if the breaker is open and the
        cool-down has not yet elapsed; otherwise allows the call through
        (transitioning `OPEN` -> `HALF_OPEN` once the cool-down has
        elapsed, so the next `on_success`/`on_failure` decides the
        outcome).
        """
        if self._state is not CircuitBreakerState.OPEN:
            return
        assert self._opened_at is not None
        elapsed = (self._clock() - self._opened_at).total_seconds()
        if elapsed < self._reset_timeout_s:
            raise CircuitOpenError(
                f"circuit open after {self._consecutive_failures} consecutive failures "
                f"(last: {self._last_error}); retry in {self._reset_timeout_s - elapsed:.1f}s"
            )
        self._state = CircuitBreakerState.HALF_OPEN

    def on_success(self) -> None:
        self._consecutive_failures = 0
        self._state = CircuitBreakerState.CLOSED
        self._opened_at = None
        self._last_success = self._clock()
        self._last_error = None

    def on_failure(self, error: str) -> None:
        self._consecutive_failures += 1
        self._last_error = error
        if self._consecutive_failures >= self._failure_threshold:
            self._state = CircuitBreakerState.OPEN
            self._opened_at = self._clock()

    def health(self, source_name: str) -> SourceHealth:
        return SourceHealth(
            source_name=source_name,
            ok=self._consecutive_failures == 0,
            last_success_utc=self._last_success,
            consecutive_failures=self._consecutive_failures,
            last_error=self._last_error,
        )


class MinIntervalLimiter:
    """The simplest useful per-source rate limiter: blocks (via `sleep`)
    until at least `min_interval_s` has elapsed since the previous call.
    `min_interval_s <= 0` disables it (e.g. ccxt exchanges already rate-limit
    themselves internally when constructed with `enableRateLimit=True`).
    """

    def __init__(
        self,
        min_interval_s: float,
        *,
        clock: Callable[[], datetime],
        sleep: Callable[[float], None],
    ) -> None:
        self._min_interval_s = min_interval_s
        self._clock = clock
        self._sleep = sleep
        self._last_call: datetime | None = None

    def wait(self) -> None:
        if self._min_interval_s <= 0:
            return
        now = self._clock()
        if self._last_call is not None:
            remaining = self._min_interval_s - (now - self._last_call).total_seconds()
            if remaining > 0:
                self._sleep(remaining)
        self._last_call = self._clock()

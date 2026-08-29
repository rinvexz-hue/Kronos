"""Tests for `resilience.py`: backoff, retry, circuit breaker, rate
limiter. All fully deterministic - no real sleeping, no real clock.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest

from kmd.data.resilience import (
    BackoffPolicy,
    CircuitBreaker,
    CircuitOpenError,
    MinIntervalLimiter,
    with_retry,
)

from .fakes import FrozenClock, RecordingSleep


def test_backoff_delay_grows_exponentially_and_is_capped() -> None:
    policy = BackoffPolicy(max_attempts=5, base_delay_s=1.0, max_delay_s=10.0, jitter_s=0.0)
    rng = random.Random(0)
    delays = [policy.delay_for(attempt, rng) for attempt in range(1, 6)]
    assert delays == [1.0, 2.0, 4.0, 8.0, 10.0]  # capped at max_delay_s on attempt 5


def test_backoff_jitter_is_bounded() -> None:
    policy = BackoffPolicy(max_attempts=1, base_delay_s=1.0, max_delay_s=10.0, jitter_s=0.5)
    rng = random.Random(1)
    for attempt in range(1, 4):
        delay = policy.delay_for(attempt, rng)
        base = min(10.0, 1.0 * (2 ** (attempt - 1)))
        assert base <= delay <= base + 0.5


def test_with_retry_succeeds_after_transient_failures() -> None:
    calls = {"n": 0}

    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("transient")
        return "ok"

    sleep = RecordingSleep()
    result = with_retry(
        flaky, policy=BackoffPolicy(max_attempts=5, jitter_s=0.0), sleep=sleep, rng=random.Random(0)
    )
    assert result == "ok"
    assert calls["n"] == 3
    assert len(sleep.delays) == 2  # slept between attempt 1->2 and 2->3


def test_with_retry_raises_after_exhausting_attempts() -> None:
    def always_fails() -> None:
        raise RuntimeError("permanent")

    sleep = RecordingSleep()
    with pytest.raises(RuntimeError, match="permanent"):
        with_retry(
            always_fails,
            policy=BackoffPolicy(max_attempts=3, jitter_s=0.0),
            sleep=sleep,
            rng=random.Random(0),
        )
    assert len(sleep.delays) == 2  # 3 attempts, 2 inter-attempt sleeps


def test_with_retry_respects_should_retry_predicate() -> None:
    calls = {"n": 0}

    def fails_with_two_error_types() -> None:
        calls["n"] += 1
        raise ValueError("not retryable")

    with pytest.raises(ValueError, match="not retryable"):
        with_retry(
            fails_with_two_error_types,
            policy=BackoffPolicy(max_attempts=5, jitter_s=0.0),
            sleep=RecordingSleep(),
            rng=random.Random(0),
            should_retry=lambda exc: isinstance(exc, RuntimeError),
        )
    assert calls["n"] == 1  # gave up immediately, never retried a non-matching error


def test_circuit_breaker_opens_after_threshold_and_blocks_calls() -> None:
    clock = FrozenClock(datetime(2024, 1, 1, tzinfo=UTC))
    breaker = CircuitBreaker(failure_threshold=3, reset_timeout_s=60.0, clock=clock)

    for _ in range(3):
        breaker.before_call()  # still closed, must not raise
        breaker.on_failure("boom")

    with pytest.raises(CircuitOpenError):
        breaker.before_call()

    health = breaker.health("test-source")
    assert health.ok is False
    assert health.consecutive_failures == 3
    assert health.last_error == "boom"


def test_circuit_breaker_half_opens_after_cooldown_and_recovers() -> None:
    clock = FrozenClock(datetime(2024, 1, 1, tzinfo=UTC))
    breaker = CircuitBreaker(failure_threshold=2, reset_timeout_s=30.0, clock=clock)

    breaker.on_failure("e1")
    breaker.on_failure("e2")
    with pytest.raises(CircuitOpenError):
        breaker.before_call()

    clock.advance(timedelta(seconds=31))
    breaker.before_call()  # cooldown elapsed - half-open, must not raise
    breaker.on_success()

    health = breaker.health("test-source")
    assert health.ok is True
    assert health.consecutive_failures == 0
    assert health.last_success_utc == clock.now


def test_circuit_breaker_success_resets_consecutive_failures() -> None:
    clock = FrozenClock(datetime(2024, 1, 1, tzinfo=UTC))
    breaker = CircuitBreaker(failure_threshold=5, reset_timeout_s=60.0, clock=clock)
    breaker.on_failure("e1")
    breaker.on_failure("e2")
    breaker.on_success()
    assert breaker.health("s").consecutive_failures == 0
    assert breaker.health("s").ok is True


def test_min_interval_limiter_sleeps_only_when_called_too_soon() -> None:
    clock = FrozenClock(datetime(2024, 1, 1, tzinfo=UTC))
    sleep = RecordingSleep(clock=clock)
    limiter = MinIntervalLimiter(1.0, clock=clock, sleep=sleep)

    limiter.wait()  # first call: never waits
    assert sleep.delays == []

    limiter.wait()  # immediately again: must wait ~1s
    assert sleep.delays == [1.0]


def test_min_interval_limiter_disabled_when_non_positive() -> None:
    clock = FrozenClock(datetime(2024, 1, 1, tzinfo=UTC))
    sleep = RecordingSleep(clock=clock)
    limiter = MinIntervalLimiter(0.0, clock=clock, sleep=sleep)
    limiter.wait()
    limiter.wait()
    assert sleep.delays == []

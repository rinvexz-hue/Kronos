"""Tests for `sessions.py`. `SessionSpec` instances are built directly
(not loaded from YAML) so each test is self-contained.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from kmd.data.markets_config import SessionSpec
from kmd.data.sessions import SessionConfigError, is_market_open

CRYPTO = SessionSpec(always_open=True)
FX = SessionSpec(
    always_open=False, weekday_open="sun 22:00", weekday_close="fri 22:00", timezone="UTC"
)
METALS = SessionSpec(
    always_open=False,
    weekday_open="sun 23:00",
    weekday_close="fri 22:00",
    timezone="America/Chicago",
)


def test_crypto_is_always_open_even_on_a_weekend() -> None:
    saturday = datetime(2024, 1, 6, 3, 0, tzinfo=UTC)
    assert is_market_open(CRYPTO, saturday)


def test_fx_open_midweek() -> None:
    wednesday_noon = datetime(2024, 1, 3, 12, 0, tzinfo=UTC)
    assert is_market_open(FX, wednesday_noon)


def test_fx_closed_on_saturday() -> None:
    saturday_noon = datetime(2024, 1, 6, 12, 0, tzinfo=UTC)
    assert not is_market_open(FX, saturday_noon)


def test_fx_opens_exactly_at_sunday_22_00_utc() -> None:
    sunday = datetime(2024, 1, 7, 22, 0, tzinfo=UTC)
    just_before = datetime(2024, 1, 7, 21, 59, tzinfo=UTC)
    assert is_market_open(FX, sunday)
    assert not is_market_open(FX, just_before)


def test_fx_closes_exactly_at_friday_22_00_utc() -> None:
    friday = datetime(2024, 1, 5, 22, 0, tzinfo=UTC)
    just_before = datetime(2024, 1, 5, 21, 59, tzinfo=UTC)
    assert not is_market_open(FX, friday)
    assert is_market_open(FX, just_before)


def test_metals_dst_shift_winter_vs_summer_chicago() -> None:
    """America/Chicago is UTC-6 (CST) in January and UTC-5 (CDT) in July,
    so the same local "sun 23:00" open lands at a *different* UTC instant
    depending on the date - a fixed-UTC-offset implementation (e.g. always
    treating it as UTC-6) would get the summer case wrong. This is exactly
    why `is_market_open` must convert via `zoneinfo` per-call rather than
    precompute a UTC offset once.
    """
    # Sunday 2024-01-07 23:00 CST == Monday 2024-01-08 05:00 UTC
    winter_just_before = datetime(2024, 1, 8, 4, 59, tzinfo=UTC)
    winter_just_after = datetime(2024, 1, 8, 5, 1, tzinfo=UTC)
    assert not is_market_open(METALS, winter_just_before)
    assert is_market_open(METALS, winter_just_after)

    # Sunday 2024-07-07 23:00 CDT == Monday 2024-07-08 04:00 UTC
    summer_just_before = datetime(2024, 7, 8, 3, 59, tzinfo=UTC)
    summer_just_after = datetime(2024, 7, 8, 4, 1, tzinfo=UTC)
    assert not is_market_open(METALS, summer_just_before)
    assert is_market_open(METALS, summer_just_after)


def test_malformed_weekday_time_raises_session_config_error() -> None:
    bad = SessionSpec(
        always_open=False, weekday_open="whoops", weekday_close="fri 22:00", timezone="UTC"
    )
    with pytest.raises(SessionConfigError):
        is_market_open(bad, datetime(2024, 1, 1, tzinfo=UTC))


def test_naive_now_raises_value_error() -> None:
    with pytest.raises(ValueError, match="tz-aware"):
        is_market_open(FX, datetime(2024, 1, 3, 12, 0))  # noqa: DTZ001 - deliberately naive

"""Hand-constructed known-answer tests for `kmd.analysis.setup`. Default
behaviour (no setup) must win every ambiguous or under-threshold case —
these tests exercise every reason `compute_setup` can return `None`, not
just the happy path.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from kmd.analysis.setup import compute_setup
from kmd.dto import ForecastMetrics, Level, Regime

BASE_TS = datetime(2026, 1, 1, tzinfo=UTC)
MIN_RR = 2.0
RISK_PCT = 2.0


def _forecast(*, p_up: float, q10: float, q50: float, q90: float) -> ForecastMetrics:
    return ForecastMetrics(
        p_up_24h=p_up,
        q10=q10,
        q50=q50,
        q90=q90,
        p_vol_expansion=0.3,
        band_width_pct=(q90 - q10) / q50,
        n_paths=30,
        model_name="fake-model",
        generated_at_utc=BASE_TS,
        last_closed_bar_ts_utc=BASE_TS,
    )


def _regime(label: str) -> Regime:
    return Regime(label=label, vol_regime="normal", reason="test fixture")  # type: ignore[arg-type]


def test_long_setup_emitted_when_rr_clears_threshold() -> None:
    entry = 100.0
    levels = [Level(price=90.0, kind="swing_low", reason="test support")]
    forecast = _forecast(p_up=0.7, q10=95.0, q50=110.0, q90=130.0)
    setup = compute_setup(
        entry, _regime("trend_up"), levels, forecast, min_rr=MIN_RR, risk_pct=RISK_PCT
    )
    assert setup is not None
    assert setup.direction == "long"
    assert setup.entry == 100.0
    assert setup.invalidation == 90.0
    assert setup.target == 130.0
    assert setup.rr == pytest.approx(3.0)  # reward 30 / risk 10
    assert setup.risk_pct == RISK_PCT


def test_long_setup_withheld_below_rr_threshold() -> None:
    entry = 100.0
    levels = [Level(price=90.0, kind="swing_low", reason="test support")]
    forecast = _forecast(p_up=0.7, q10=95.0, q50=105.0, q90=110.0)  # reward 10 / risk 10 = RR 1.0
    setup = compute_setup(
        entry, _regime("trend_up"), levels, forecast, min_rr=MIN_RR, risk_pct=RISK_PCT
    )
    assert setup is None


def test_long_setup_withheld_without_support_level() -> None:
    entry = 100.0
    forecast = _forecast(p_up=0.7, q10=95.0, q50=110.0, q90=130.0)
    setup = compute_setup(
        entry, _regime("trend_up"), [], forecast, min_rr=MIN_RR, risk_pct=RISK_PCT
    )
    assert setup is None


def test_long_setup_withheld_when_forecast_disagrees_with_trend() -> None:
    entry = 100.0
    levels = [Level(price=90.0, kind="swing_low", reason="test support")]
    forecast = _forecast(p_up=0.3, q10=95.0, q50=110.0, q90=130.0)  # p_up <= 0.5
    setup = compute_setup(
        entry, _regime("trend_up"), levels, forecast, min_rr=MIN_RR, risk_pct=RISK_PCT
    )
    assert setup is None


def test_long_setup_withheld_when_target_does_not_clear_entry() -> None:
    entry = 100.0
    levels = [Level(price=90.0, kind="swing_low", reason="test support")]
    forecast = _forecast(p_up=0.7, q10=95.0, q50=99.0, q90=99.5)  # q90 < entry
    setup = compute_setup(
        entry, _regime("trend_up"), levels, forecast, min_rr=MIN_RR, risk_pct=RISK_PCT
    )
    assert setup is None


def test_short_setup_emitted_when_rr_clears_threshold() -> None:
    entry = 100.0
    levels = [Level(price=110.0, kind="swing_high", reason="test resistance")]
    forecast = _forecast(p_up=0.3, q10=70.0, q50=90.0, q90=105.0)
    setup = compute_setup(
        entry, _regime("trend_down"), levels, forecast, min_rr=MIN_RR, risk_pct=RISK_PCT
    )
    assert setup is not None
    assert setup.direction == "short"
    assert setup.invalidation == 110.0
    assert setup.target == 70.0
    assert setup.rr == pytest.approx(3.0)  # reward 30 / risk 10


def test_short_setup_withheld_without_resistance_level() -> None:
    entry = 100.0
    forecast = _forecast(p_up=0.3, q10=70.0, q50=90.0, q90=105.0)
    setup = compute_setup(
        entry, _regime("trend_down"), [], forecast, min_rr=MIN_RR, risk_pct=RISK_PCT
    )
    assert setup is None


@pytest.mark.parametrize("label", ["range", "unknown"])
def test_no_setup_in_non_trending_regime(label: str) -> None:
    entry = 100.0
    levels = [
        Level(price=90.0, kind="swing_low", reason="support"),
        Level(price=110.0, kind="swing_high", reason="resistance"),
    ]
    forecast = _forecast(p_up=0.7, q10=95.0, q50=110.0, q90=130.0)
    setup = compute_setup(
        entry, _regime(label), levels, forecast, min_rr=MIN_RR, risk_pct=RISK_PCT
    )
    assert setup is None


def test_nearest_support_picks_closest_below_entry() -> None:
    entry = 100.0
    levels = [
        Level(price=80.0, kind="swing_low", reason="far support"),
        Level(price=95.0, kind="swing_low", reason="near support"),
        Level(price=105.0, kind="swing_low", reason="above entry, ignored"),
    ]
    forecast = _forecast(p_up=0.7, q10=90.0, q50=110.0, q90=130.0)
    setup = compute_setup(
        entry, _regime("trend_up"), levels, forecast, min_rr=MIN_RR, risk_pct=RISK_PCT
    )
    assert setup is not None
    assert setup.invalidation == 95.0  # nearest support below entry, not the farthest

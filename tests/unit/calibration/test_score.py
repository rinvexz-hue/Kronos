"""Tests for `kmd.calibration.score`.

`test_score_single_matches_hand_computed_values` verifies Brier/MAE/
coverage against 10 fully hand-computed forecast/outcome pairs (see the
comment above the table for the by-hand arithmetic). The rest of this
file verifies `score_matured_forecasts`' own look-ahead check: a
forecast's horizon being in the past by wall-clock `now` is NOT enough to
score it — a real `is_closed=True` bar must exist at exactly that
timestamp.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kmd.calibration.logger import CalibrationLogger, ForecastLogRecord
from kmd.calibration.score import (
    MAX_HORIZON_CATCHUP,
    aggregate_calibration_stats,
    score_matured_forecasts,
    score_single,
)
from kmd.data.base import Timeframe
from tests.support import FakeMarketStore, make_bar

BASE_TS = datetime(2026, 1, 1, tzinfo=UTC)

# (p_up, q10, q50, q90, last_close, realized_close,
#  expected_brier, expected_mae, expected_in_band)
#
# Hand computation for each row:
#  1. actual_up=1 (110>100); brier=(0.9-1)^2=0.01; mae=|108-110|=2;      110 in [100,115] -> True
#  2. actual_up=0 (90<100);  brier=(0.1-0)^2=0.01; mae=|95-90|=5;         90 in [85,100]  -> True
#  3. actual_up=0 (100 not >100); brier=(0.5-0)^2=0.25; mae=|100-100|=0; 100 in [90,110]  -> True
#  4. actual_up=0 (40<50);   brier=(0.8-0)^2=0.64; mae=|55-40|=15;        40 in [45,60]   -> False
#  5. actual_up=1 (65>50);   brier=(0.2-1)^2=0.64; mae=|48-65|=17;        65 in [40,52]   -> False
#  6. actual_up=1 (210>200); brier=(0.6-1)^2=0.16; mae=|205-210|=5;      210 in [190,220] -> True
#  7. actual_up=0 (195<200); brier=(0.4-0)^2=0.16; mae=|198-195|=3;      195 in [185,210] -> True
#  8. actual_up=1 (12>10);   brier=(1.0-1)^2=0.00; mae=|11-12|=1;         12 in [9,13]    -> True
#  9. actual_up=0 (8<10);    brier=(0.0-0)^2=0.00; mae=|9-8|=1;            8 in [7,9.5]   -> True
# 10. actual_up=1 (1500>1000); brier=(0.5-1)^2=0.25; mae=|1010-1500|=490; 1500 in [990,1030] -> False
SYNTHETIC_PAIRS = [
    (0.9, 100.0, 108.0, 115.0, 100.0, 110.0, 0.01, 2.0, True),
    (0.1, 85.0, 95.0, 100.0, 100.0, 90.0, 0.01, 5.0, True),
    (0.5, 90.0, 100.0, 110.0, 100.0, 100.0, 0.25, 0.0, True),
    (0.8, 45.0, 55.0, 60.0, 50.0, 40.0, 0.64, 15.0, False),
    (0.2, 40.0, 48.0, 52.0, 50.0, 65.0, 0.64, 17.0, False),
    (0.6, 190.0, 205.0, 220.0, 200.0, 210.0, 0.16, 5.0, True),
    (0.4, 185.0, 198.0, 210.0, 200.0, 195.0, 0.16, 3.0, True),
    (1.0, 9.0, 11.0, 13.0, 10.0, 12.0, 0.0, 1.0, True),
    (0.0, 7.0, 9.0, 9.5, 10.0, 8.0, 0.0, 1.0, True),
    (0.5, 990.0, 1010.0, 1030.0, 1000.0, 1500.0, 0.25, 490.0, False),
]


def test_score_single_matches_hand_computed_values() -> None:
    for p_up, q10, q50, q90, last_close, realized, exp_brier, exp_mae, exp_in_band in SYNTHETIC_PAIRS:
        result = score_single(p_up, q10, q50, q90, last_close, realized)
        assert result.brier_score == pytest.approx(exp_brier)
        assert result.mae_q50 == pytest.approx(exp_mae)
        assert result.in_band is exp_in_band


def test_aggregate_calibration_stats_matches_hand_computed_means() -> None:
    """Mean Brier = 2.12/10 = 0.212; mean MAE = 539/10 = 53.9;
    coverage = 7/10 = 0.7 (7 of the 10 rows above are `in_band`).
    """
    records = []
    for i, (p_up, q10, q50, q90, last_close, realized, *_rest) in enumerate(SYNTHETIC_PAIRS):
        result = score_single(p_up, q10, q50, q90, last_close, realized)
        records.append(
            ForecastLogRecord(
                id=i,
                symbol="BTC/USDT",
                timeframe=Timeframe.H1,
                generated_at_utc=BASE_TS,
                last_closed_ts=BASE_TS,
                horizon_ts=BASE_TS + timedelta(hours=24),
                lookback_bars=400,
                pred_len=24,
                model_name="fake-model",
                temperature=1.0,
                top_p=0.9,
                top_k=0,
                n_paths=30,
                last_close=last_close,
                p_up_24h=p_up,
                q10=q10,
                q50=q50,
                q90=q90,
                p_vol_expansion=0.3,
                band_width_pct=(q90 - q10) / q50,
                scored_at_utc=BASE_TS,
                brier_score=result.brier_score,
                mae_q50=result.mae_q50,
                in_band=result.in_band,
            )
        )

    stats = aggregate_calibration_stats(records, min_observations_for_display=30)
    assert stats.n_observations == 10
    assert stats.brier_score == pytest.approx(0.212)
    assert stats.mae_q50 == pytest.approx(53.9)
    assert stats.band_coverage == pytest.approx(0.7)
    assert stats.sufficient_data is False  # 10 < 30


def test_aggregate_calibration_stats_sufficient_data_flag() -> None:
    records = [
        ForecastLogRecord(
            id=i,
            symbol="BTC/USDT",
            timeframe=Timeframe.H1,
            generated_at_utc=BASE_TS,
            last_closed_ts=BASE_TS,
            horizon_ts=BASE_TS + timedelta(hours=24),
            lookback_bars=400,
            pred_len=24,
            model_name="fake-model",
            temperature=1.0,
            top_p=0.9,
            top_k=0,
            n_paths=30,
            last_close=100.0,
            p_up_24h=0.5,
            q10=95.0,
            q50=100.0,
            q90=105.0,
            p_vol_expansion=0.3,
            band_width_pct=0.1,
            scored_at_utc=BASE_TS,
            brier_score=0.1,
            mae_q50=1.0,
            in_band=True,
        )
        for i in range(30)
    ]
    stats = aggregate_calibration_stats(records, min_observations_for_display=30)
    assert stats.sufficient_data is True


def test_aggregate_calibration_stats_empty() -> None:
    stats = aggregate_calibration_stats([], min_observations_for_display=30)
    assert stats.n_observations == 0
    assert stats.sufficient_data is False
    assert stats.brier_score is None
    assert stats.mae_q50 is None
    assert stats.band_coverage is None


def _log_one_forecast(logger: CalibrationLogger, *, horizon_ts: datetime) -> int:
    logger.log_forecast(
        ForecastLogRecord(
            symbol="BTC/USDT",
            timeframe=Timeframe.H1,
            generated_at_utc=BASE_TS,
            last_closed_ts=BASE_TS,
            horizon_ts=horizon_ts,
            lookback_bars=400,
            pred_len=24,
            model_name="fake-model",
            temperature=1.0,
            top_p=0.9,
            top_k=0,
            n_paths=30,
            last_close=100.0,
            p_up_24h=0.6,
            q10=95.0,
            q50=105.0,
            q90=115.0,
            p_vol_expansion=0.3,
            band_width_pct=0.19,
        )
    )
    [record] = logger.get_unscored_matured(horizon_ts + timedelta(days=1))
    assert record.id is not None
    return record.id


def test_score_matured_forecasts_scores_when_closed_bar_exists(tmp_path) -> None:  # type: ignore[no-untyped-def]
    horizon_ts = BASE_TS + timedelta(hours=24)
    logger = CalibrationLogger(tmp_path / "cal.sqlite3")
    _log_one_forecast(logger, horizon_ts=horizon_ts)

    store = FakeMarketStore()
    store.set_bars(
        "BTC/USDT",
        Timeframe.H1,
        [make_bar(ts_utc=horizon_ts, open_=110, high=112, low=108, close=110, is_closed=True)],
    )

    scored = score_matured_forecasts(logger, store, now=horizon_ts + timedelta(hours=1))
    assert scored == 1
    [record] = logger.get_scored("BTC/USDT", Timeframe.H1)
    assert record.brier_score == pytest.approx(0.16)  # (0.6-1)^2, since 110 > 100
    assert record.mae_q50 == pytest.approx(5.0)  # |105-110|


def test_score_matured_forecasts_never_scores_before_horizon_elapses(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Look-ahead regression test #2 (the first is in `test_engine.py`):
    `now` before the claimed horizon must never be scored, even if a bar
    already happens to exist at that timestamp (e.g. a source that
    returned future-dated data by mistake).
    """
    horizon_ts = BASE_TS + timedelta(hours=24)
    logger = CalibrationLogger(tmp_path / "cal.sqlite3")
    _log_one_forecast(logger, horizon_ts=horizon_ts)

    store = FakeMarketStore()
    store.set_bars(
        "BTC/USDT",
        Timeframe.H1,
        [make_bar(ts_utc=horizon_ts, open_=110, high=112, low=108, close=110, is_closed=True)],
    )

    # `now` is BEFORE the horizon -> get_unscored_matured must exclude it,
    # so score_matured_forecasts has nothing to do here regardless of the
    # (already-present) closed bar.
    scored = score_matured_forecasts(logger, store, now=horizon_ts - timedelta(hours=1))
    assert scored == 0
    assert logger.get_scored("BTC/USDT", Timeframe.H1) == []


def test_score_matured_forecasts_never_scores_against_an_unclosed_bar(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Look-ahead regression test #3: even with `now` safely past the
    horizon, a bar at that timestamp which is NOT YET CLOSED must not be
    used to score — this is the exact bug a naive
    `ts_utc == horizon_ts` check (without `is_closed`) would have.
    """
    horizon_ts = BASE_TS + timedelta(hours=24)
    logger = CalibrationLogger(tmp_path / "cal.sqlite3")
    _log_one_forecast(logger, horizon_ts=horizon_ts)

    store = FakeMarketStore()
    store.set_bars(
        "BTC/USDT",
        Timeframe.H1,
        [make_bar(ts_utc=horizon_ts, open_=110, high=112, low=108, close=110, is_closed=False)],
    )

    scored = score_matured_forecasts(logger, store, now=horizon_ts + timedelta(hours=1))
    assert scored == 0
    assert logger.get_scored("BTC/USDT", Timeframe.H1) == []

    # Once the bar actually closes, a later run picks it up.
    store.set_bars(
        "BTC/USDT",
        Timeframe.H1,
        [make_bar(ts_utc=horizon_ts, open_=110, high=112, low=108, close=110, is_closed=True)],
    )
    scored_again = score_matured_forecasts(logger, store, now=horizon_ts + timedelta(hours=2))
    assert scored_again == 1


def test_score_matured_forecasts_leaves_unscored_when_no_bar_at_horizon_yet(tmp_path) -> None:  # type: ignore[no-untyped-def]
    horizon_ts = BASE_TS + timedelta(hours=24)
    logger = CalibrationLogger(tmp_path / "cal.sqlite3")
    _log_one_forecast(logger, horizon_ts=horizon_ts)

    store = FakeMarketStore()  # no bars at all yet
    scored = score_matured_forecasts(logger, store, now=horizon_ts + timedelta(hours=1))
    assert scored == 0
    assert len(logger.get_unscored_matured(horizon_ts + timedelta(hours=1))) == 1


def test_score_matured_forecasts_resolves_weekend_horizon_against_first_reopen_bar(tmp_path: Path) -> None:
    """Regression test for red-team Round 1 finding #3.

    EUR/USD's `fx` session (see `config/markets.yaml`) closes Friday
    22:00 UTC and reopens Sunday 22:00 UTC. A forecast generated Friday
    morning with `pred_len=24` (1h timeframe) gets a nominal `horizon_ts`
    that lands Saturday - squarely inside the closed weekend - where no
    bar can ever exist at that exact timestamp. It must still eventually
    get scored, against the first real bar once trading resumes, rather
    than being silently and permanently excluded from calibration.
    """
    last_closed_ts = datetime(2026, 1, 2, 3, 0, tzinfo=UTC)  # Friday 03:00 UTC
    horizon_ts = last_closed_ts + timedelta(hours=24)  # Saturday 03:00 UTC
    assert horizon_ts.weekday() == 5  # Saturday - confirms the fixture actually lands mid-weekend

    logger = CalibrationLogger(tmp_path / "cal.sqlite3")
    logger.log_forecast(
        ForecastLogRecord(
            symbol="EUR/USD",
            timeframe=Timeframe.H1,
            generated_at_utc=last_closed_ts,
            last_closed_ts=last_closed_ts,
            horizon_ts=horizon_ts,
            lookback_bars=400,
            pred_len=24,
            model_name="fake-model",
            temperature=1.0,
            top_p=0.9,
            top_k=0,
            n_paths=30,
            last_close=1.0800,
            p_up_24h=0.55,
            q10=1.0750,
            q50=1.0820,
            q90=1.0900,
            p_vol_expansion=0.2,
            band_width_pct=0.014,
        )
    )

    # No bar exists (or ever will) at the exact Saturday horizon - the
    # market is closed. The first real bar is Sunday evening, once
    # trading resumes.
    reopen_bar_ts = datetime(2026, 1, 4, 23, 0, tzinfo=UTC)  # Sunday 23:00 UTC
    store = FakeMarketStore()
    store.set_bars(
        "EUR/USD",
        Timeframe.H1,
        [
            make_bar(
                symbol="EUR/USD",
                ts_utc=reopen_bar_ts,
                open_=1.0850,
                high=1.0860,
                low=1.0840,
                close=1.0855,
                is_closed=True,
            )
        ],
    )

    # Before reopen: matured by the clock, but no bar yet - must stay
    # pending, not be marked unscorable (still well within the catch-up
    # window).
    still_weekend_now = horizon_ts + timedelta(hours=6)
    assert score_matured_forecasts(logger, store, now=still_weekend_now) == 0
    assert len(logger.get_unscored_matured(still_weekend_now)) == 1
    assert logger.get_unscorable("EUR/USD", Timeframe.H1) == []

    # Once the reopen bar exists, it resolves the forecast.
    scored = score_matured_forecasts(logger, store, now=reopen_bar_ts + timedelta(hours=1))
    assert scored == 1
    [record] = logger.get_scored("EUR/USD", Timeframe.H1)
    assert record.mae_q50 == pytest.approx(abs(1.0820 - 1.0855))
    assert record.in_band is True  # 1.0855 is within [1.0750, 1.0900]
    assert logger.get_unscored_matured(reopen_bar_ts + timedelta(hours=1)) == []


def test_score_matured_forecasts_marks_unscorable_after_catchup_window_elapses(tmp_path: Path) -> None:
    """If no bar EVER arrives within `MAX_HORIZON_CATCHUP` (a genuine
    multi-day data outage, not just a weekly close), the forecast is
    marked unscorable instead of being rescanned forever.
    """
    horizon_ts = BASE_TS + timedelta(hours=24)
    logger = CalibrationLogger(tmp_path / "cal.sqlite3")
    logger.log_forecast(_gold_forecast_record(horizon_ts))

    store = FakeMarketStore()  # never gets any bars in this test

    just_before_deadline = horizon_ts + MAX_HORIZON_CATCHUP - timedelta(minutes=1)
    assert score_matured_forecasts(logger, store, now=just_before_deadline) == 0
    assert logger.get_unscorable("GOUD", Timeframe.H1) == []  # not yet - still within the window

    just_after_deadline = horizon_ts + MAX_HORIZON_CATCHUP + timedelta(minutes=1)
    assert score_matured_forecasts(logger, store, now=just_after_deadline) == 0
    [unscorable] = logger.get_unscorable("GOUD", Timeframe.H1)
    assert unscorable.unscorable_reason is not None
    assert unscorable.scored_at_utc is None  # unscorable and scored are mutually exclusive

    # Bounded scan: it must not show up in get_unscored_matured anymore.
    assert logger.get_unscored_matured(just_after_deadline + timedelta(days=1)) == []

    # Even if a bar shows up much later, an already-unscorable row is not
    # retroactively scored (it would be a stale/unrelated data point).
    store.set_bars(
        "GOUD",
        Timeframe.H1,
        [make_bar(symbol="GOUD", ts_utc=horizon_ts + timedelta(days=10), open_=2000, high=2001, low=1999, close=2000, is_closed=True)],
    )
    assert score_matured_forecasts(logger, store, now=horizon_ts + timedelta(days=11)) == 0
    assert logger.get_scored("GOUD", Timeframe.H1) == []


def test_score_matured_forecasts_never_uses_a_bar_after_now(tmp_path: Path) -> None:
    """Defensive look-ahead check: even a real closed bar within the
    catch-up window must not be used to score if its own timestamp is
    after the caller's `now` (a clock-skew edge case, not exercised by
    normal operation, but the invariant should hold regardless)."""
    horizon_ts = BASE_TS + timedelta(hours=24)
    logger = CalibrationLogger(tmp_path / "cal.sqlite3")
    logger.log_forecast(_gold_forecast_record(horizon_ts))

    store = FakeMarketStore()
    future_bar_ts = horizon_ts + timedelta(hours=2)
    store.set_bars(
        "GOUD",
        Timeframe.H1,
        [make_bar(symbol="GOUD", ts_utc=future_bar_ts, open_=2000, high=2001, low=1999, close=2000, is_closed=True)],
    )

    # `now` is BEFORE the only candidate bar's own timestamp.
    scored = score_matured_forecasts(logger, store, now=horizon_ts + timedelta(hours=1))
    assert scored == 0
    assert logger.get_scored("GOUD", Timeframe.H1) == []



def _gold_forecast_record(horizon_ts: datetime) -> ForecastLogRecord:
    return ForecastLogRecord(
        symbol="GOUD",
        timeframe=Timeframe.H1,
        generated_at_utc=horizon_ts - timedelta(hours=24),
        last_closed_ts=horizon_ts - timedelta(hours=24),
        horizon_ts=horizon_ts,
        lookback_bars=400,
        pred_len=24,
        model_name="fake-model",
        temperature=1.0,
        top_p=0.9,
        top_k=0,
        n_paths=30,
        last_close=2000.0,
        p_up_24h=0.5,
        q10=1990.0,
        q50=2000.0,
        q90=2010.0,
        p_vol_expansion=0.2,
        band_width_pct=0.01,
    )

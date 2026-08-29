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

import pytest

from kmd.calibration.logger import CalibrationLogger, ForecastLogRecord
from kmd.calibration.score import (
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

"""Tests for `kmd.calibration.logger`. Covers idempotent logging (a cache
hit must never double-log the same closed bar) and persistence across a
process restart.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from kmd.calibration.logger import CalibrationLogger, ForecastLogRecord
from kmd.data.base import Timeframe

BASE_TS = datetime(2026, 1, 1, tzinfo=UTC)


def _record(**overrides: object) -> ForecastLogRecord:
    defaults: dict[str, object] = {
        "symbol": "BTC/USDT",
        "timeframe": Timeframe.H1,
        "generated_at_utc": BASE_TS,
        "last_closed_ts": BASE_TS,
        "horizon_ts": BASE_TS + timedelta(hours=24),
        "lookback_bars": 400,
        "pred_len": 24,
        "model_name": "fake-model",
        "temperature": 1.0,
        "top_p": 0.9,
        "top_k": 0,
        "n_paths": 30,
        "last_close": 100.0,
        "p_up_24h": 0.6,
        "q10": 95.0,
        "q50": 105.0,
        "q90": 115.0,
        "p_vol_expansion": 0.3,
        "band_width_pct": 0.19,
    }
    defaults.update(overrides)
    return ForecastLogRecord(**defaults)  # type: ignore[arg-type]


def test_log_and_read_back_unscored(tmp_path: Path) -> None:
    logger = CalibrationLogger(tmp_path / "cal.sqlite3")
    logger.log_forecast(_record())

    matured = logger.get_unscored_matured(BASE_TS + timedelta(hours=25))
    assert len(matured) == 1
    assert matured[0].symbol == "BTC/USDT"
    assert matured[0].id is not None


def test_get_unscored_matured_excludes_future_horizons(tmp_path: Path) -> None:
    logger = CalibrationLogger(tmp_path / "cal.sqlite3")
    logger.log_forecast(_record())

    # `now` is before the horizon has elapsed -> must not be returned.
    matured = logger.get_unscored_matured(BASE_TS + timedelta(hours=1))
    assert matured == []


def test_duplicate_log_forecast_is_idempotent(tmp_path: Path) -> None:
    """A cache hit (same closed bar re-served) must never double-log."""
    logger = CalibrationLogger(tmp_path / "cal.sqlite3")
    logger.log_forecast(_record())
    logger.log_forecast(_record())  # identical (symbol, timeframe, last_closed_ts)

    matured = logger.get_unscored_matured(BASE_TS + timedelta(hours=25))
    assert len(matured) == 1


def test_different_last_closed_ts_logs_separately(tmp_path: Path) -> None:
    logger = CalibrationLogger(tmp_path / "cal.sqlite3")
    logger.log_forecast(_record())
    logger.log_forecast(
        _record(
            last_closed_ts=BASE_TS + timedelta(hours=1),
            horizon_ts=BASE_TS + timedelta(hours=25),
        )
    )
    matured = logger.get_unscored_matured(BASE_TS + timedelta(hours=26))
    assert len(matured) == 2


def test_mark_scored_removes_from_unscored_and_appears_in_scored(tmp_path: Path) -> None:
    logger = CalibrationLogger(tmp_path / "cal.sqlite3")
    logger.log_forecast(_record())
    [pending] = logger.get_unscored_matured(BASE_TS + timedelta(hours=25))
    assert pending.id is not None

    logger.mark_scored(
        pending.id,
        scored_at_utc=BASE_TS + timedelta(hours=25),
        brier_score=0.04,
        mae_q50=3.0,
        in_band=True,
    )

    assert logger.get_unscored_matured(BASE_TS + timedelta(hours=25)) == []
    [scored] = logger.get_scored("BTC/USDT", Timeframe.H1)
    assert scored.brier_score == 0.04
    assert scored.mae_q50 == 3.0
    assert scored.in_band is True
    assert scored.scored_at_utc == BASE_TS + timedelta(hours=25)


def test_persists_across_reopening_the_same_file(tmp_path: Path) -> None:
    db_path = tmp_path / "cal.sqlite3"
    logger_a = CalibrationLogger(db_path)
    logger_a.log_forecast(_record())
    logger_a.close()

    logger_b = CalibrationLogger(db_path)
    matured = logger_b.get_unscored_matured(BASE_TS + timedelta(hours=25))
    assert len(matured) == 1

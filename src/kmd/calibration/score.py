"""Calibration scoring: join matured forecasts against realized outcomes
and compute Brier score (on `p_up` vs. realized up/down), MAE (on `q50`
vs. realized close), and q10-q90 band coverage.

The look-ahead check here is the second one in the system (the first is
in `forecast/engine.py::select_closed_lookback`): a forecast's horizon
timestamp being in the past according to wall-clock `now` is NOT enough
to score it — the bar that actually covers that horizon must exist AND be
`is_closed=True` in the store. `get_unscored_matured` filters on `now`;
`score_matured_forecasts` re-verifies against the real bar before ever
computing a score, so a forecast can never be scored against a bar that
didn't exist (or hadn't closed) yet at query time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from kmd.calibration.logger import CalibrationLogger, ForecastLogRecord
from kmd.data.base import MarketStore
from kmd.dto import CalibrationStats


@dataclass(frozen=True)
class ScoreResult:
    brier_score: float
    mae_q50: float
    in_band: bool


def score_single(
    p_up: float,
    q10: float,
    q50: float,
    q90: float,
    last_close: float,
    realized_close: float,
) -> ScoreResult:
    """Pure scoring of one forecast against its realized outcome.

    - Brier score: `(p_up - actual_up) ** 2`, `actual_up = 1` iff
      `realized_close > last_close`.
    - MAE: `abs(q50 - realized_close)`.
    - Band coverage: whether `realized_close` fell within `[q10, q90]`.
    """
    actual_up = 1.0 if realized_close > last_close else 0.0
    brier_score = (p_up - actual_up) ** 2
    mae_q50 = abs(q50 - realized_close)
    in_band = q10 <= realized_close <= q90
    return ScoreResult(brier_score=brier_score, mae_q50=mae_q50, in_band=in_band)


def score_matured_forecasts(
    logger: CalibrationLogger,
    store: MarketStore,
    now: datetime,
    *,
    bars_lookup_limit: int = 1000,
) -> int:
    """Score every forecast whose claimed horizon is `<= now` AND for
    which a `is_closed=True` bar at exactly `horizon_ts` already exists in
    the store. A forecast that is matured by the clock but not yet backed
    by real closed-bar data is left unscored (it will be picked up on a
    later run) rather than scored against a still-forming or absent bar.

    Returns the number of forecasts newly scored.
    """
    scored_count = 0
    for record in logger.get_unscored_matured(now):
        bars = store.get_latest_bars(record.symbol, record.timeframe, bars_lookup_limit)
        realized_bar = next(
            (b for b in bars if b.ts_utc == record.horizon_ts and b.is_closed),
            None,
        )
        if realized_bar is None:
            continue

        result = score_single(
            p_up=record.p_up_24h,
            q10=record.q10,
            q50=record.q50,
            q90=record.q90,
            last_close=record.last_close,
            realized_close=realized_bar.close,
        )
        assert record.id is not None  # always set for a row read back from the DB
        logger.mark_scored(
            record.id,
            scored_at_utc=now,
            brier_score=result.brier_score,
            mae_q50=result.mae_q50,
            in_band=result.in_band,
        )
        scored_count += 1
    return scored_count


def aggregate_calibration_stats(
    scored_records: list[ForecastLogRecord],
    min_observations_for_display: int,
) -> CalibrationStats:
    """Aggregate already-scored records into the `CalibrationStats` DTO.
    Below `min_observations_for_display`, `sufficient_data` is False and
    the dashboard must say so explicitly rather than show a misleadingly
    precise number — the numeric fields are still populated (so an
    operator/debug view can see them) but the frontend must gate display
    on `sufficient_data`, not on the numbers being present.
    """
    n = len(scored_records)
    if n == 0:
        return CalibrationStats(
            n_observations=0,
            brier_score=None,
            mae_q50=None,
            band_coverage=None,
            sufficient_data=False,
        )

    brier_values: list[float] = []
    mae_values: list[float] = []
    in_band_count = 0
    for record in scored_records:
        if record.brier_score is None or record.mae_q50 is None or record.in_band is None:
            raise ValueError(f"scored record {record.id} is missing a score field")
        brier_values.append(record.brier_score)
        mae_values.append(record.mae_q50)
        if record.in_band:
            in_band_count += 1

    return CalibrationStats(
        n_observations=n,
        brier_score=sum(brier_values) / n,
        mae_q50=sum(mae_values) / n,
        band_coverage=in_band_count / n,
        sufficient_data=n >= min_observations_for_display,
    )

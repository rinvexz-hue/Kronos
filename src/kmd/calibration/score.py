"""Calibration scoring: join matured forecasts against realized outcomes
and compute Brier score (on `p_up` vs. realized up/down), MAE (on `q50`
vs. realized close), and q10-q90 band coverage.

The look-ahead check here is the second one in the system (the first is
in `forecast/engine.py::select_closed_lookback`): a forecast's horizon
timestamp being in the past according to wall-clock `now` is NOT enough
to score it — the bar used to score it must exist, be `is_closed=True`,
AND itself be at or before `now` (never a bar the caller couldn't
possibly have seen yet). `get_unscored_matured` filters on `now`;
`score_matured_forecasts` re-verifies against the real bar before ever
computing a score.

**Closed-session horizons (red-team Round 1, finding #3).** A non-24/7
instrument's (FX, metals futures) nominal `horizon_ts` can land inside
that instrument's closed weekly session — no bar can ever exist at that
exact timestamp, since the market simply isn't trading then. Requiring an
EXACT `ts_utc == horizon_ts` match (the original implementation) meant
those forecasts could never be scored and accumulated in `forecast_log`
forever, silently biasing `CalibrationStats` for EUR/USD, USD/JPY, GOUD,
ZILVER (roughly 20% of their weekly forecasts). Fixed by scoring against
the FIRST real closed bar AT OR AFTER `horizon_ts`, within a bounded
`MAX_HORIZON_CATCHUP` window — i.e. "the first real price once trading
resumed" stands in for the nominal instant that could never itself have a
price. A forecast that still has no such bar once the catch-up window has
fully elapsed (a genuine multi-day data outage, not just a weekly close)
is marked `unscorable` instead of being rescanned forever — see
`DECISIONS.md` for the alternatives considered and why this one was
chosen over teaching `forecast/engine.py` session-awareness.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from kmd.calibration.logger import CalibrationLogger, ForecastLogRecord
from kmd.data.base import MarketStore
from kmd.dto import CalibrationStats

# Matches `quality.py`'s own `_WEEKEND_ALLOWANCE_S` (3 days) — long enough
# to cover any FX/metals weekly closure (Friday evening to Sunday evening,
# plus slack) but bounded, so a genuine multi-day outage doesn't leave a
# forecast pending indefinitely.
MAX_HORIZON_CATCHUP = timedelta(days=3)


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
    """Score every forecast whose claimed horizon is `<= now`, against the
    first `is_closed=True` bar at-or-after `horizon_ts` (and at-or-before
    `now`, so a bar can never be used before the caller could possibly
    have seen it) within `MAX_HORIZON_CATCHUP`. A forecast that is matured
    by the clock but not yet backed by any such bar is left unscored (it
    will be picked up on a later run) — UNLESS the catch-up window has
    already fully elapsed, in which case it is marked `unscorable`
    (see `CalibrationLogger.mark_unscorable`) so it stops being rescanned
    forever.

    Returns the number of forecasts newly scored (not counting any newly
    marked unscorable).
    """
    scored_count = 0
    for record in logger.get_unscored_matured(now):
        bars = store.get_latest_bars(record.symbol, record.timeframe, bars_lookup_limit)
        catchup_deadline = record.horizon_ts + MAX_HORIZON_CATCHUP
        candidates = sorted(
            (
                b
                for b in bars
                if b.is_closed
                and record.horizon_ts <= b.ts_utc <= catchup_deadline
                and b.ts_utc <= now
            ),
            key=lambda b: b.ts_utc,
        )
        assert record.id is not None  # always set for a row read back from the DB

        if candidates:
            realized_bar = candidates[0]
            result = score_single(
                p_up=record.p_up_24h,
                q10=record.q10,
                q50=record.q50,
                q90=record.q90,
                last_close=record.last_close,
                realized_close=realized_bar.close,
            )
            logger.mark_scored(
                record.id,
                scored_at_utc=now,
                brier_score=result.brier_score,
                mae_q50=result.mae_q50,
                in_band=result.in_band,
            )
            scored_count += 1
        elif now > catchup_deadline:
            logger.mark_unscorable(
                record.id,
                at_utc=now,
                reason=(
                    f"no closed bar within {MAX_HORIZON_CATCHUP} of horizon_ts "
                    f"{record.horizon_ts.isoformat()} (extended session closure or data outage)"
                ),
            )
        # else: matured but no bar yet and still within the catch-up
        # window - leave pending for a later cycle.
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

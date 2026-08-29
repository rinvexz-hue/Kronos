"""Refresh scheduling: one job per configured timeframe, aligned to that
timeframe's actual candle close — never naive fixed-interval polling. A
job fires shortly AFTER the close so `MarketStore.get_last_closed_ts` has
genuinely advanced by the time ingest/forecast/analysis run; the forecast
cache (`forecast/cache.py`) is what actually prevents redundant Kronos
runs if a job ever fires twice for the same closed bar.

Each refresh cycle: ingest -> score any newly-matured calibration
forecasts -> (primary timeframe only) forecast + analysis + rebuild the
snapshot -> hand it to `snapshot_sink` (e.g. persisted to disk for
`api.py` to serve). Non-primary timeframes still ingest and score, since
their closes matter for data freshness and calibration even though the
current `SnapshotDTO` only carries one (primary-timeframe) forecast per
asset.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from kmd.calibration.logger import CalibrationLogger
from kmd.calibration.score import score_matured_forecasts
from kmd.config import Settings
from kmd.data.base import MarketStore, Timeframe
from kmd.data.ingest import SourceRegistry, run_incremental_update
from kmd.data.markets_config import MarketsConfig
from kmd.data.store import SqliteStore
from kmd.forecast.cache import ForecastCache
from kmd.forecast.engine import PredictorProtocol
from kmd.snapshot import SnapshotDTO, build_snapshot

logger = logging.getLogger(__name__)

PRIMARY_TIMEFRAME = Timeframe.H1

# One minute or two past each timeframe's close, giving the ingest step a
# small buffer against a source's own reporting lag.
REFRESH_CRON: dict[Timeframe, CronTrigger] = {
    Timeframe.H1: CronTrigger(minute=1),
    Timeframe.H4: CronTrigger(hour="0,4,8,12,16,20", minute=2),
    Timeframe.D1: CronTrigger(hour=0, minute=5),
}

IngestFn = Callable[[Timeframe], None]
SnapshotSink = Callable[[SnapshotDTO], None]


def build_ingest_fn(
    store: SqliteStore,
    registry: SourceRegistry,
    config: MarketsConfig,
) -> IngestFn:
    """Wraps builder-data's `kmd.data.ingest.run_incremental_update` (never
    reimplemented here) into the `IngestFn` shape `run_refresh_cycle`
    expects: one timeframe in, nothing out. `store`/`registry` are the
    concrete data-layer objects `ingest.py` itself requires (it also calls
    `store.record_source_health`, which is not part of the `MarketStore`
    Protocol) — everything else in this module only depends on that
    Protocol, so this is the one seam where a concrete data-layer type is
    unavoidable.
    """

    def _ingest(timeframe: Timeframe) -> None:
        run_incremental_update(config, registry, store, timeframes=[timeframe])

    return _ingest


def run_refresh_cycle(
    *,
    timeframe: Timeframe,
    store: MarketStore,
    markets_config: MarketsConfig,
    settings: Settings,
    predictor: PredictorProtocol,
    forecast_cache: ForecastCache,
    calibration_logger: CalibrationLogger,
    ingest_fn: IngestFn,
    snapshot_sink: SnapshotSink | None,
    now: datetime | None = None,
) -> SnapshotDTO | None:
    """Run one full refresh cycle for `timeframe`. Returns the rebuilt
    snapshot when one was built (primary timeframe only), else `None`.
    """
    now = now if now is not None else datetime.now(UTC)
    ingest_fn(timeframe)

    scored = score_matured_forecasts(calibration_logger, store, now)
    if scored:
        logger.info("scored %d matured forecast(s) as of %s refresh", scored, timeframe.value)

    if timeframe != PRIMARY_TIMEFRAME:
        return None

    snapshot = build_snapshot(
        store=store,
        markets_config=markets_config,
        settings=settings,
        predictor=predictor,
        forecast_cache=forecast_cache,
        calibration_logger=calibration_logger,
        timeframe=timeframe,
        now=now,
    )
    if snapshot_sink is not None:
        snapshot_sink(snapshot)
    return snapshot


def build_scheduler(
    *,
    store: MarketStore,
    markets_config: MarketsConfig,
    settings: Settings,
    predictor: PredictorProtocol,
    forecast_cache: ForecastCache,
    calibration_logger: CalibrationLogger,
    snapshot_sink: SnapshotSink,
    ingest_fn: IngestFn,
) -> BackgroundScheduler:
    """Build (but do not start) a `BackgroundScheduler` with one
    close-aligned refresh job per timeframe in `settings.refresh_timeframe_list`.
    `ingest_fn` is typically `build_ingest_fn(...)` wired to the real data
    layer; tests pass a fake.
    """
    scheduler = BackgroundScheduler(timezone=ZoneInfo("UTC"))

    for tf_str in settings.refresh_timeframe_list:
        timeframe = Timeframe(tf_str)
        trigger = REFRESH_CRON[timeframe]

        def _job(timeframe: Timeframe = timeframe) -> None:
            try:
                run_refresh_cycle(
                    timeframe=timeframe,
                    store=store,
                    markets_config=markets_config,
                    settings=settings,
                    predictor=predictor,
                    forecast_cache=forecast_cache,
                    calibration_logger=calibration_logger,
                    ingest_fn=ingest_fn,
                    snapshot_sink=snapshot_sink,
                )
            except Exception:
                logger.exception("refresh cycle failed for timeframe %s", timeframe.value)

        scheduler.add_job(_job, trigger, id=f"refresh_{timeframe.value}", replace_existing=True)

    return scheduler

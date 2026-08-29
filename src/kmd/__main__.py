"""Application entrypoint: wires the data layer, forecast engine,
calibration store, and scheduler together, then serves the FastAPI app.

Run with `python -m kmd`. Kept deliberately thin — everything it calls is
independently testable (`kmd.scheduler`, `kmd.snapshot`, `kmd.api`,
builder-data's `kmd.data.*`); this module only does composition, so it has
no unit tests of its own (it is exercised by the manual/integration run
described in `NOTES/kronos_api.md` and `README.md`, not by `pytest`) —
`kmd.scheduler.run_startup_sequence`, which this module wires with real
functions, DOES have unit tests, against injected fakes.

Startup ordering is deliberate and load-bearing (red-team Round 1,
finding #1): `uvicorn.run(...)` starts BEFORE backfill or the Kronos model
have loaded. Backfill + model load + starting the scheduler happen in a
background thread (`run_startup_sequence`) that retries indefinitely and
never raises out of the thread, so `/healthz` is reachable within
milliseconds of process start (reporting `status="starting"` /
`"backfilling"` / `"loading_model"` / `"error"` / `"ok"`) regardless of
how slow or broken the network or Hugging Face is — the previous version
ran both synchronously before `uvicorn.run` was ever reached, so a fully
down network could block for tens of minutes and then crash the process
outright (see `REVIEW.md` Round 1 #1 for the traced blast radius).
"""

from __future__ import annotations

import logging
import threading

import uvicorn

from kmd.api import ReadinessState, SnapshotFileStore, create_app
from kmd.calibration.logger import CalibrationLogger
from kmd.config import Settings
from kmd.data.ingest import SourceRegistry, build_default_source_registry, run_full_backfill
from kmd.data.markets_config import MarketsConfig, load_markets_config
from kmd.data.store import SqliteStore
from kmd.forecast.cache import ForecastCache
from kmd.forecast.engine import PredictorProtocol, load_predictor
from kmd.scheduler import (
    StartSchedulerFn,
    build_ingest_fn,
    build_scheduler,
    run_startup_sequence,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _make_start_scheduler_fn(
    *,
    store: SqliteStore,
    markets_config: MarketsConfig,
    settings: Settings,
    registry: SourceRegistry,
    snapshot_store: SnapshotFileStore,
) -> StartSchedulerFn:
    """Returns a `StartSchedulerFn` closing over everything needed once the
    predictor is available. Kept as a factory (rather than one flat
    function) so `main()` reads as one wiring pass.
    """

    def _start_scheduler(predictor: PredictorProtocol) -> None:
        forecast_db_path = settings.db_path.parent / "kmd_forecast.sqlite3"
        forecast_cache = ForecastCache(forecast_db_path)
        calibration_logger = CalibrationLogger(forecast_db_path)
        ingest_fn = build_ingest_fn(store, registry, markets_config)

        scheduler = build_scheduler(
            store=store,
            markets_config=markets_config,
            settings=settings,
            predictor=predictor,
            forecast_cache=forecast_cache,
            calibration_logger=calibration_logger,
            snapshot_sink=snapshot_store.save,
            ingest_fn=ingest_fn,
        )
        scheduler.start()
        logger.info("scheduler started, timeframes=%s", settings.refresh_timeframe_list)

    return _start_scheduler


def main() -> None:
    settings = Settings()
    markets_config = load_markets_config()

    store = SqliteStore(settings.db_path, markets_config=markets_config)
    registry = build_default_source_registry(
        markets_config,
        ccxt_api_key=settings.ccxt_api_key,
        ccxt_api_secret=settings.ccxt_api_secret,
    )
    snapshot_store = SnapshotFileStore(settings.db_path.parent / "snapshot.json")
    readiness = ReadinessState()

    def _backfill() -> None:
        logger.info("running startup backfill (a no-op per symbol once history exists)")
        run_full_backfill(markets_config, registry, store)

    def _load_predictor() -> PredictorProtocol:
        logger.info("loading Kronos predictor (%s)", settings.model_name)
        return load_predictor(settings)

    start_scheduler_fn = _make_start_scheduler_fn(
        store=store,
        markets_config=markets_config,
        settings=settings,
        registry=registry,
        snapshot_store=snapshot_store,
    )

    startup_thread = threading.Thread(
        target=run_startup_sequence,
        kwargs={
            "readiness": readiness,
            "backfill_fn": _backfill,
            "load_predictor_fn": _load_predictor,
            "start_scheduler_fn": start_scheduler_fn,
        },
        name="kmd-startup",
        daemon=True,
    )
    startup_thread.start()

    app = create_app(snapshot_store.load, readiness=readiness)
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()

"""Application entrypoint: wires the data layer, forecast engine,
calibration store, and scheduler together, then serves the FastAPI app.

Run with `python -m kmd`. Kept deliberately thin — everything it calls is
independently testable (`kmd.scheduler`, `kmd.snapshot`, `kmd.api`,
builder-data's `kmd.data.*`); this module only does composition, so it has
no unit tests of its own (it is exercised by the manual/integration run
described in `NOTES/kronos_api.md` and `README.md`, not by `pytest`).
"""

from __future__ import annotations

import logging

import uvicorn

from kmd.api import SnapshotFileStore, create_app
from kmd.calibration.logger import CalibrationLogger
from kmd.config import Settings
from kmd.data.ingest import build_default_source_registry, run_full_backfill
from kmd.data.markets_config import load_markets_config
from kmd.data.store import SqliteStore
from kmd.forecast.cache import ForecastCache
from kmd.forecast.engine import load_predictor
from kmd.scheduler import build_ingest_fn, build_scheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def main() -> None:
    settings = Settings()
    markets_config = load_markets_config()

    store = SqliteStore(settings.db_path, markets_config=markets_config)
    registry = build_default_source_registry(
        markets_config,
        ccxt_api_key=settings.ccxt_api_key,
        ccxt_api_secret=settings.ccxt_api_secret,
    )

    logger.info("running startup backfill (a no-op per symbol once history exists)")
    run_full_backfill(markets_config, registry, store)

    logger.info("loading Kronos predictor (%s)", settings.model_name)
    predictor = load_predictor(settings)

    forecast_db_path = settings.db_path.parent / "kmd_forecast.sqlite3"
    forecast_cache = ForecastCache(forecast_db_path)
    calibration_logger = CalibrationLogger(forecast_db_path)
    snapshot_store = SnapshotFileStore(settings.db_path.parent / "snapshot.json")
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

    app = create_app(snapshot_store.load)
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()

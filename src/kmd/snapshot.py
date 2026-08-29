"""Assembly of the dashboard's `SnapshotDTO`. The DTO shapes themselves
live in `kmd.dto` (re-exported below, so `from kmd.snapshot import
SnapshotDTO` etc. keeps working) — they were split out of this module so
`kmd.analysis.*` (regime/levels/setup) can depend on the shapes without
importing `kmd.snapshot` itself, which would be circular since this
module imports `kmd.analysis.*`.

`GET /api/snapshot` returns exactly `SnapshotDTO.model_dump(mode="json")`;
the frontend reads nothing else. `build_snapshot()` is builder-core's
implementation, assembled from the forecast/analysis/calibration layers
plus data-source status — it must never call into the data layer's source
adapters or SQLite schema directly, only through `kmd.data.base.MarketStore`
and the forecast/analysis modules.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from kmd.analysis.levels import compute_levels
from kmd.analysis.regime import compute_regime
from kmd.analysis.setup import compute_setup
from kmd.calibration.logger import CalibrationLogger, ForecastLogRecord
from kmd.calibration.score import aggregate_calibration_stats
from kmd.config import Settings
from kmd.data.base import Bar, MarketStore, SourceHealth, Timeframe
from kmd.data.markets_config import Instrument, MarketsConfig
from kmd.data.sessions import is_market_open
from kmd.dto import (
    AssetSnapshot,
    CalibrationStats,
    DataSourceStatus,
    ForecastMetrics,
    Level,
    LevelKind,
    Regime,
    RegimeLabel,
    SetupCard,
    SnapshotDTO,
    VolRegime,
)
from kmd.forecast import metrics as fmetrics
from kmd.forecast.cache import ForecastCache, ForecastCacheKey, result_to_cached
from kmd.forecast.engine import TIMEFRAME_DELTAS, PredictorProtocol, run_monte_carlo

__all__ = [
    "AssetSnapshot",
    "CalibrationStats",
    "DataSourceStatus",
    "ForecastMetrics",
    "InsufficientDataError",
    "Level",
    "LevelKind",
    "Regime",
    "RegimeLabel",
    "SetupCard",
    "SnapshotDTO",
    "VolRegime",
    "build_asset_snapshot",
    "build_snapshot",
]

logger = logging.getLogger(__name__)

# Context instruments (e.g. DXY, S&P 500) are fetched/stored like any other
# symbol but are shown as an analysis overlay, never as a first-class
# forecast tile — see `config/markets.yaml`'s own comment on the `context`
# group. `kmd.data.markets_config.MarketsConfig` has no built-in notion of
# this (it is a builder-core presentation rule, not a data-layer one), so
# it is filtered on here by group name.
_CONTEXT_GROUP = "context"


class InsufficientDataError(ValueError):
    """Raised (and caught per-instrument in `build_snapshot`) when a
    symbol does not yet have enough closed bars to forecast/analyze."""


def _sparkline(bars: list[Bar], length: int = 50) -> list[float]:
    closed = sorted((b for b in bars if b.is_closed), key=lambda b: b.ts_utc)
    return [b.close for b in closed[-length:]]


def _pct_change(bars: list[Bar], current_price: float, lookback_seconds: float, now: datetime) -> float | None:
    """Percent change from the closed bar nearest at-or-before
    `now - lookback_seconds` to `current_price`. `None` if no bar old
    enough exists yet (e.g. not enough history backfilled).
    """
    closed = sorted((b for b in bars if b.is_closed), key=lambda b: b.ts_utc)
    if not closed:
        return None
    target_ts = now.timestamp() - lookback_seconds
    candidates = [b for b in closed if b.ts_utc.timestamp() <= target_ts]
    if not candidates:
        return None
    reference = max(candidates, key=lambda b: b.ts_utc)
    if reference.close == 0:
        return None
    return (current_price - reference.close) / reference.close * 100.0


def _match_source_health(
    instrument: Instrument, health_list: list[SourceHealth]
) -> SourceHealth | None:
    """Best-effort match of an instrument to its `SourceHealth` row. The
    exact `source_name` convention is builder-data's to define; this tries
    the ccxt exchange name first, then the generic source kind
    ("yfinance"), and gives up (returning `None`, handled as "unknown
    status" by the caller) rather than guessing further.
    """
    candidates = {c for c in (instrument.exchange, instrument.source.value) if c is not None}
    for health in health_list:
        if health.source_name in candidates:
            return health
    return None


def _build_source_status(
    instrument: Instrument,
    health_list: list[SourceHealth],
    markets_config: MarketsConfig,
    timeframe: Timeframe,
    now: datetime,
) -> DataSourceStatus:
    session = markets_config.sessions[instrument.session_name]
    session_open = is_market_open(session, now)
    health = _match_source_health(instrument, health_list)
    if health is None:
        return DataSourceStatus(
            source_name=instrument.exchange or instrument.source.value,
            last_update_utc=None,
            is_stale=True,
            error_count_last_hour=0,
            market_session_open=session_open,
        )

    stale_after = TIMEFRAME_DELTAS[timeframe] * 2
    is_stale = health.last_success_utc is None or (now - health.last_success_utc) > stale_after
    return DataSourceStatus(
        source_name=health.source_name,
        last_update_utc=health.last_success_utc,
        is_stale=is_stale,
        # MarketSource/MarketStore only exposes `consecutive_failures`, not a
        # true rolling last-hour error count — used as the best available
        # proxy until builder-data's health telemetry exposes a real window.
        error_count_last_hour=health.consecutive_failures,
        market_session_open=session_open,
    )


def _forecast_metrics_from_paths(
    close_paths: list[list[float]],
    last_close: float,
    recent_closes: list[float],
    pred_len: int,
    model_name: str,
    generated_at_utc: datetime,
    last_closed_ts: datetime,
    n_paths: int,
) -> ForecastMetrics:
    p_up = fmetrics.p_up_24h(close_paths, last_close)
    q10, q50, q90 = fmetrics.horizon_quantiles(close_paths)
    bw = fmetrics.band_width_pct(q10, q50, q90)
    hist_vol = fmetrics.historical_realized_vol(recent_closes, window=pred_len)
    p_vol = fmetrics.p_vol_expansion(close_paths, last_close, hist_vol)
    return ForecastMetrics(
        p_up_24h=p_up,
        q10=q10,
        q50=q50,
        q90=q90,
        p_vol_expansion=p_vol,
        band_width_pct=bw,
        n_paths=n_paths,
        model_name=model_name,
        generated_at_utc=generated_at_utc,
        last_closed_bar_ts_utc=last_closed_ts,
    )


def _get_or_compute_forecast(
    *,
    instrument: Instrument,
    timeframe: Timeframe,
    bars: list[Bar],
    settings: Settings,
    lookback_bars: int,
    pred_len: int,
    predictor: PredictorProtocol,
    forecast_cache: ForecastCache,
    calibration_logger: CalibrationLogger,
    now: datetime,
) -> ForecastMetrics:
    """`lookback_bars`/`pred_len` come from `config/markets.yaml` (the
    single source of truth for forecast window sizing), not `Settings` —
    `Settings.lookback_bars`/`pred_len` only exist as the matching `.env`
    defaults for that same config and are not read here. Model identity/
    sampling parameters (`model_name`, `temperature`, `top_p`, `top_k`,
    `mc_paths`, `seed`) are not instrument config, so those DO come from
    `Settings`.
    """
    closed = [b for b in bars if b.is_closed]
    if not closed:
        raise InsufficientDataError(f"{instrument.display_symbol}: no closed bars available")
    last_closed = max(closed, key=lambda b: b.ts_utc)
    recent_closes = [b.close for b in sorted(closed, key=lambda b: b.ts_utc)]

    key = ForecastCacheKey(
        symbol=instrument.display_symbol,
        timeframe=timeframe,
        last_closed_ts=last_closed.ts_utc,
        model_name=settings.model_name,
        temperature=settings.temperature,
        top_p=settings.top_p,
        n_paths=settings.mc_paths,
        lookback_bars=lookback_bars,
        pred_len=pred_len,
    )
    cached = forecast_cache.get(key)
    if cached is not None:
        return _forecast_metrics_from_paths(
            cached.close_paths,
            cached.last_close,
            recent_closes,
            pred_len,
            settings.model_name,
            cached.generated_at_utc,
            cached.last_closed_ts,
            settings.mc_paths,
        )

    mc_result = run_monte_carlo(
        predictor,
        instrument.display_symbol,
        timeframe,
        bars,
        lookback_bars=lookback_bars,
        pred_len=pred_len,
        n_paths=settings.mc_paths,
        temperature=settings.temperature,
        top_p=settings.top_p,
        top_k=settings.top_k,
        seed=settings.seed,
        model_name=settings.model_name,
    )
    forecast_cache.put(key, result_to_cached(mc_result, now))

    close_paths = [path["close"].tolist() for path in mc_result.paths]
    forecast = _forecast_metrics_from_paths(
        close_paths,
        mc_result.last_close,
        recent_closes,
        pred_len,
        settings.model_name,
        now,
        mc_result.last_closed_ts,
        settings.mc_paths,
    )

    calibration_logger.log_forecast(
        ForecastLogRecord(
            symbol=instrument.display_symbol,
            timeframe=timeframe,
            generated_at_utc=now,
            last_closed_ts=mc_result.last_closed_ts,
            horizon_ts=mc_result.y_timestamps[-1],
            lookback_bars=lookback_bars,
            pred_len=pred_len,
            model_name=settings.model_name,
            temperature=settings.temperature,
            top_p=settings.top_p,
            top_k=settings.top_k,
            n_paths=settings.mc_paths,
            last_close=mc_result.last_close,
            p_up_24h=forecast.p_up_24h,
            q10=forecast.q10,
            q50=forecast.q50,
            q90=forecast.q90,
            p_vol_expansion=forecast.p_vol_expansion,
            band_width_pct=forecast.band_width_pct,
        )
    )
    return forecast


def build_asset_snapshot(
    *,
    instrument: Instrument,
    bars: list[Bar],
    forecast: ForecastMetrics,
    calibration: CalibrationStats,
    source_status: DataSourceStatus,
    risk_min_rr: float,
    risk_default_pct: float,
    now: datetime,
) -> AssetSnapshot:
    """Assemble one `AssetSnapshot` from already-computed forecast/
    calibration/source-status inputs. Split out from `build_snapshot` so
    tests can build a full, realistic snapshot from fakes without needing
    a real predictor, store, or SQLite cache.
    """
    closed = [b for b in bars if b.is_closed]
    if not closed:
        raise InsufficientDataError(f"{instrument.display_symbol}: no closed bars available")
    last_closed = max(closed, key=lambda b: b.ts_utc)
    price = last_closed.close

    regime = compute_regime(bars)
    levels = compute_levels(bars, price, instrument.decimals)
    setup = compute_setup(
        price,
        regime,
        levels,
        forecast,
        min_rr=risk_min_rr,
        risk_pct=risk_default_pct,
    )

    return AssetSnapshot(
        display_symbol=instrument.display_symbol,
        group=instrument.group,
        decimals=instrument.decimals,
        price=price,
        change_1h_pct=_pct_change(bars, price, 3600, now),
        change_24h_pct=_pct_change(bars, price, 24 * 3600, now),
        change_7d_pct=_pct_change(bars, price, 7 * 24 * 3600, now),
        sparkline=_sparkline(bars),
        regime=regime,
        levels=levels,
        forecast=forecast,
        calibration=calibration,
        setup=setup,
        source_status=source_status,
    )


def build_snapshot(
    *,
    store: MarketStore,
    markets_config: MarketsConfig,
    settings: Settings,
    predictor: PredictorProtocol,
    forecast_cache: ForecastCache,
    calibration_logger: CalibrationLogger,
    timeframe: Timeframe = Timeframe.H1,
    now: datetime | None = None,
) -> SnapshotDTO:
    """Assemble the current `SnapshotDTO` from the data/forecast/analysis/
    calibration layers, for every forecastable instrument in
    `markets_config` (excludes the `context` group per its own docstring).

    A single instrument failing (insufficient history, a data-source
    exception) is logged and skipped rather than aborting the whole
    snapshot — the dashboard must degrade per-tile, never show nothing
    because one symbol had a bad day.
    """
    now = now if now is not None else datetime.now(UTC)
    health_list = store.source_health()
    assets: list[AssetSnapshot] = []
    lookback_bars = markets_config.forecast.lookback_bars
    pred_len = markets_config.forecast.pred_len

    forecastable = [i for i in markets_config.all_instruments() if i.group != _CONTEXT_GROUP]
    for instrument in forecastable:
        try:
            bars = store.get_latest_bars(instrument.display_symbol, timeframe, lookback_bars + 5)
            forecast = _get_or_compute_forecast(
                instrument=instrument,
                timeframe=timeframe,
                bars=bars,
                settings=settings,
                lookback_bars=lookback_bars,
                pred_len=pred_len,
                predictor=predictor,
                forecast_cache=forecast_cache,
                calibration_logger=calibration_logger,
                now=now,
            )
            scored = calibration_logger.get_scored(instrument.display_symbol, timeframe)
            calibration = aggregate_calibration_stats(
                scored, markets_config.calibration.min_observations_for_display
            )
            source_status = _build_source_status(instrument, health_list, markets_config, timeframe, now)
            assets.append(
                build_asset_snapshot(
                    instrument=instrument,
                    bars=bars,
                    forecast=forecast,
                    calibration=calibration,
                    source_status=source_status,
                    risk_min_rr=markets_config.risk.min_rr_for_setup,
                    risk_default_pct=markets_config.risk.default_risk_pct,
                    now=now,
                )
            )
        except Exception:
            logger.exception("failed to build snapshot for %s, skipping", instrument.display_symbol)
            continue

    return SnapshotDTO(generated_at_utc=now, correlation_id=str(uuid.uuid4()), assets=assets)

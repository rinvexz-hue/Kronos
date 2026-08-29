"""Tests for `kmd.scheduler`. `run_refresh_cycle` is tested against fakes
(no real ingest, no real Kronos); `build_ingest_fn` is tested against a
real (in-memory) `SqliteStore` + a fake `MarketSource`, since it exists
specifically to wire the real `kmd.data.ingest` module (never
reimplemented) to a concrete store.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from kmd.calibration.logger import CalibrationLogger
from kmd.config import Settings
from kmd.data.base import Bar, SourceHealth, Timeframe
from kmd.data.ingest import SourceRegistry
from kmd.data.markets_config import (
    CalibrationSpec,
    DataSource,
    ForecastSpec,
    GroupSpec,
    InstrumentSpec,
    MarketsConfig,
    RiskSpec,
    SessionSpec,
    TimeframesSpec,
)
from kmd.data.store import SqliteStore
from kmd.forecast.cache import ForecastCache
from kmd.scheduler import PRIMARY_TIMEFRAME, build_ingest_fn, run_refresh_cycle
from kmd.snapshot import SnapshotDTO
from tests.support import FakeMarketStore, FakePredictor, make_bar

BASE_TS = datetime(2026, 1, 1, tzinfo=UTC)
LOOKBACK = 20


def _markets_config() -> MarketsConfig:
    return MarketsConfig(
        timeframes=TimeframesSpec(primary=Timeframe.H1, secondary=[Timeframe.H4]),
        forecast=ForecastSpec(lookback_bars=LOOKBACK, pred_len=3),
        sessions={"crypto": SessionSpec(always_open=True)},
        groups={
            "crypto": GroupSpec(
                session="crypto",
                instruments=[
                    InstrumentSpec(
                        display_symbol="BTC/USDT",
                        decimals=2,
                        source=DataSource.CCXT,
                        source_symbol="BTC/USDT",
                        exchange="binance",
                    )
                ],
            )
        },
        risk=RiskSpec(min_rr_for_setup=2.0, default_risk_pct=2.0),
        calibration=CalibrationSpec(min_observations_for_display=30, target_band_coverage=0.8),
    )


def _store_with_history() -> FakeMarketStore:
    store = FakeMarketStore()
    bars = [
        make_bar(
            symbol="BTC/USDT",
            timeframe=Timeframe.H1,
            ts_utc=BASE_TS + timedelta(hours=i),
            open_=100.0 + i,
            high=101.0 + i,
            low=99.0 + i,
            close=100.0 + i,
            is_closed=True,
        )
        for i in range(LOOKBACK + 5)
    ]
    store.set_bars("BTC/USDT", Timeframe.H1, bars)
    return store


def _refresh_kwargs(tmp_path: Path, store: FakeMarketStore, calls: list[Timeframe]) -> dict:  # type: ignore[type-arg]
    return {
        "store": store,
        "markets_config": _markets_config(),
        "settings": Settings(mc_paths=3),
        "predictor": FakePredictor(),
        "forecast_cache": ForecastCache(tmp_path / "forecast.sqlite3"),
        "calibration_logger": CalibrationLogger(tmp_path / "forecast.sqlite3"),
        "ingest_fn": lambda tf: calls.append(tf),
    }


def test_primary_timeframe_refresh_builds_and_sinks_snapshot(tmp_path: Path) -> None:
    store = _store_with_history()
    calls: list[Timeframe] = []
    sunk: list[SnapshotDTO] = []

    result = run_refresh_cycle(
        timeframe=PRIMARY_TIMEFRAME,
        snapshot_sink=sunk.append,
        now=BASE_TS + timedelta(hours=LOOKBACK + 2),
        **_refresh_kwargs(tmp_path, store, calls),
    )

    assert calls == [PRIMARY_TIMEFRAME]
    assert result is not None
    assert len(sunk) == 1
    assert sunk[0] is result
    assert {a.display_symbol for a in result.assets} == {"BTC/USDT"}


def test_non_primary_timeframe_refresh_does_not_build_snapshot(tmp_path: Path) -> None:
    store = _store_with_history()
    calls: list[Timeframe] = []
    sunk: list[SnapshotDTO] = []

    result = run_refresh_cycle(
        timeframe=Timeframe.H4,
        snapshot_sink=sunk.append,
        now=BASE_TS + timedelta(hours=LOOKBACK + 2),
        **_refresh_kwargs(tmp_path, store, calls),
    )

    assert calls == [Timeframe.H4]  # ingest still runs for every timeframe
    assert result is None
    assert sunk == []


def test_refresh_cycle_scores_matured_forecasts(tmp_path: Path) -> None:
    """A forecast logged on an earlier (fake) refresh, whose horizon has
    now elapsed with a real closed bar behind it, gets scored during the
    next refresh cycle.
    """
    store = _store_with_history()
    calls: list[Timeframe] = []
    kwargs = _refresh_kwargs(tmp_path, store, calls)

    first_now = BASE_TS + timedelta(hours=LOOKBACK + 2)
    run_refresh_cycle(timeframe=PRIMARY_TIMEFRAME, snapshot_sink=lambda _dto: None, now=first_now, **kwargs)

    unscored = kwargs["calibration_logger"].get_unscored_matured(first_now + timedelta(days=2))
    assert len(unscored) == 1
    horizon_ts = unscored[0].horizon_ts

    # Extend history so the horizon bar actually exists and is closed.
    extra_bars = [
        make_bar(
            symbol="BTC/USDT",
            timeframe=Timeframe.H1,
            ts_utc=BASE_TS + timedelta(hours=i),
            open_=100.0 + i,
            high=101.0 + i,
            low=99.0 + i,
            close=100.0 + i,
            is_closed=True,
        )
        for i in range(LOOKBACK + 5, LOOKBACK + 20)
    ]
    store.set_bars("BTC/USDT", Timeframe.H1, store.get_latest_bars("BTC/USDT", Timeframe.H1, 10_000) + extra_bars)

    run_refresh_cycle(
        timeframe=PRIMARY_TIMEFRAME,
        snapshot_sink=lambda _dto: None,
        now=horizon_ts + timedelta(hours=1),
        **kwargs,
    )
    assert kwargs["calibration_logger"].get_unscored_matured(horizon_ts + timedelta(hours=1)) == []
    assert len(kwargs["calibration_logger"].get_scored("BTC/USDT", PRIMARY_TIMEFRAME)) == 1


class _FakeSource:
    name = "binance"

    def __init__(self, bars: list[Bar]) -> None:
        self._bars = bars
        self.fetch_calls = 0

    def fetch_ohlcv(self, source_symbol: str, timeframe: Timeframe, since, limit: int) -> list[Bar]:  # type: ignore[no-untyped-def]
        self.fetch_calls += 1
        return self._bars

    def health(self) -> SourceHealth:
        return SourceHealth(
            source_name=self.name, ok=True, last_success_utc=BASE_TS, consecutive_failures=0, last_error=None
        )


def test_build_ingest_fn_wraps_real_ingest_module(tmp_path: Path) -> None:
    config = _markets_config()
    store = SqliteStore(":memory:", markets_config=config)
    fake_bars = [
        make_bar(
            symbol="BTC/USDT",
            timeframe=Timeframe.H1,
            ts_utc=BASE_TS + timedelta(hours=i),
            open_=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
            is_closed=True,
        )
        for i in range(5)
    ]
    source = _FakeSource(fake_bars)
    registry = SourceRegistry()
    registry.register_ccxt("binance", source)

    ingest_fn = build_ingest_fn(store, registry, config)
    ingest_fn(Timeframe.H1)

    assert source.fetch_calls == 1
    stored = store.get_latest_bars("BTC/USDT", Timeframe.H1, 10)
    assert len(stored) == 5

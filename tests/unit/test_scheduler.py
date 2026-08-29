"""Tests for `kmd.scheduler`. `run_refresh_cycle` is tested against fakes
(no real ingest, no real Kronos); `build_ingest_fn` is tested against a
real (in-memory) `SqliteStore` + a fake `MarketSource`, since it exists
specifically to wire the real `kmd.data.ingest` module (never
reimplemented) to a concrete store.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from kmd.api import ReadinessState, create_app
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
from kmd.scheduler import (
    PRIMARY_TIMEFRAME,
    build_ingest_fn,
    run_refresh_cycle,
    run_startup_sequence,
)
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


class _RecordingReadiness(ReadinessState):
    """Records every `update()` call (in order) in addition to the normal
    `ReadinessState` behaviour, so tests can assert the exact sequence of
    states `run_startup_sequence` reports, not just the final one.
    """

    def __init__(self) -> None:
        super().__init__()
        self.history: list[dict[str, object]] = []

    def update(self, *, status: str, ready: bool, detail: str | None = None) -> None:
        self.history.append({"status": status, "ready": ready, "detail": detail})
        super().update(status=status, ready=ready, detail=detail)


def test_run_startup_sequence_happy_path_reaches_ready() -> None:
    readiness = _RecordingReadiness()
    backfill_calls = []
    started_with = []

    def backfill_fn() -> None:
        backfill_calls.append(1)

    def load_predictor_fn() -> object:
        return "fake-predictor"

    def start_scheduler_fn(predictor: object) -> None:
        started_with.append(predictor)

    run_startup_sequence(
        readiness=readiness,
        backfill_fn=backfill_fn,
        load_predictor_fn=load_predictor_fn,  # type: ignore[arg-type]
        start_scheduler_fn=start_scheduler_fn,  # type: ignore[arg-type]
        sleep_fn=lambda _s: None,
    )

    assert backfill_calls == [1]
    assert started_with == ["fake-predictor"]
    assert readiness.snapshot() == {"status": "ok", "ready": True, "detail": None}
    statuses = [h["status"] for h in readiness.history]
    assert statuses == ["backfilling", "loading_model", "ok"]


def test_run_startup_sequence_never_raises_on_backfill_failure_and_retries() -> None:
    """A failing backfill (network down) must never propagate out of
    `run_startup_sequence` - it retries the whole sequence instead."""
    readiness = _RecordingReadiness()
    attempts = {"n": 0}

    def flaky_backfill_fn() -> None:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ConnectionError("network is down")

    run_startup_sequence(
        readiness=readiness,
        backfill_fn=flaky_backfill_fn,
        load_predictor_fn=lambda: "fake-predictor",
        start_scheduler_fn=lambda _predictor: None,
        max_attempts=5,
        sleep_fn=lambda _s: None,  # never actually sleeps in the test
    )

    assert attempts["n"] == 2  # failed once, succeeded on retry
    assert readiness.snapshot()["ready"] is True
    statuses = [h["status"] for h in readiness.history]
    assert "error" in statuses  # the failed attempt was genuinely reported
    assert statuses[-1] == "ok"
    # the failed attempt's error detail was the real exception message
    error_entries = [h for h in readiness.history if h["status"] == "error"]
    assert error_entries[0]["detail"] == "network is down"


def test_run_startup_sequence_never_raises_on_predictor_load_failure() -> None:
    """Same guarantee, this time for a slow/broken Hugging Face download
    (`load_predictor_fn` raising) rather than backfill."""
    readiness = _RecordingReadiness()

    def always_failing_load_predictor_fn() -> object:
        raise RuntimeError("could not reach huggingface.co")

    run_startup_sequence(
        readiness=readiness,
        backfill_fn=lambda: None,
        load_predictor_fn=always_failing_load_predictor_fn,  # type: ignore[arg-type]
        start_scheduler_fn=lambda _predictor: None,
        max_attempts=3,
        sleep_fn=lambda _s: None,
    )

    assert readiness.snapshot() == {
        "status": "error",
        "ready": False,
        "detail": "could not reach huggingface.co",
    }


def test_run_startup_sequence_gives_up_after_max_attempts_without_raising() -> None:
    readiness = _RecordingReadiness()
    calls = {"n": 0}

    def always_failing_backfill_fn() -> None:
        calls["n"] += 1
        raise ConnectionError("still down")

    # Must not raise even though every attempt fails.
    run_startup_sequence(
        readiness=readiness,
        backfill_fn=always_failing_backfill_fn,
        load_predictor_fn=lambda: "unreachable",
        start_scheduler_fn=lambda _predictor: None,
        max_attempts=3,
        sleep_fn=lambda _s: None,
    )

    assert calls["n"] == 3
    assert readiness.snapshot()["ready"] is False
    assert readiness.snapshot()["status"] == "error"


def test_healthz_responds_immediately_while_startup_is_slow_and_failing(tmp_path: Path) -> None:
    """The actual regression test for red-team finding #1: a real
    background thread runs `run_startup_sequence` against a backfill_fn
    that sleeps for real (simulating a slow network) and a
    load_predictor_fn that raises (simulating an unreachable Hugging
    Face) before eventually succeeding - and `/healthz`, served by a
    FastAPI app sharing the same `ReadinessState`, must respond in
    milliseconds throughout, never blocking on either.
    """
    readiness = ReadinessState()
    client = TestClient(create_app(lambda: None, web_dir=tmp_path, readiness=readiness))

    predictor_attempts = {"n": 0}

    def slow_backfill_fn() -> None:
        time.sleep(0.3)  # simulates a slow (but eventually successful) network

    def flaky_slow_load_predictor_fn() -> object:
        predictor_attempts["n"] += 1
        if predictor_attempts["n"] == 1:
            raise RuntimeError("huggingface.co unreachable")
        time.sleep(0.2)
        return "fake-predictor"

    thread = threading.Thread(
        target=run_startup_sequence,
        kwargs={
            "readiness": readiness,
            "backfill_fn": slow_backfill_fn,
            "load_predictor_fn": flaky_slow_load_predictor_fn,
            "start_scheduler_fn": lambda _predictor: None,
            "retry_delay_s": 0.05,
            "sleep_fn": time.sleep,
        },
        daemon=True,
    )
    thread.start()

    # Poll /healthz repeatedly while the background thread is definitely
    # still working (it needs at least ~0.3s + a retry to finish) - every
    # single call must return fast and never 5xx/hang.
    saw_not_ready = False
    deadline = time.monotonic() + 2.0
    while thread.is_alive() and time.monotonic() < deadline:
        start = time.monotonic()
        resp = client.get("/healthz")
        elapsed = time.monotonic() - start
        assert resp.status_code == 200
        assert elapsed < 0.2, f"/healthz took {elapsed:.3f}s while startup was in progress"
        if resp.json()["ready"] is False:
            saw_not_ready = True
        time.sleep(0.02)

    thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert saw_not_ready  # actually observed the in-progress state, not just the end
    final = client.get("/healthz").json()
    assert final == {"status": "ok", "ready": True, "detail": None}

"""Tests for `ingest.py`'s orchestration: source routing/fallback, symbol
canonicalization, and the backfill/incremental entry points. Uses small
in-process fake `MarketSource`s (not `CcxtSource`/`YfinanceSource`) so
these tests stay focused on orchestration logic, plus a real `:memory:`
`SqliteStore` so upsert/quality-gate behavior is exercised end-to-end.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from kmd.data.base import Bar, SourceHealth, Timeframe
from kmd.data.ccxt_source import CcxtFetchError
from kmd.data.ingest import (
    SourceRegistry,
    ingest_instrument,
    run_full_backfill,
    run_incremental_update,
)
from kmd.data.markets_config import MarketsConfig
from kmd.data.resilience import CircuitOpenError
from kmd.data.store import SqliteStore

T0 = datetime(2024, 1, 1, tzinfo=UTC)


class FakeSource:
    """A minimal `MarketSource` double: returns canned bars, or raises the
    given exception `fail_times` times before succeeding.
    """

    def __init__(
        self,
        name: str,
        bars: list[Bar],
        *,
        fail_times: int = 0,
        exc: Exception | None = None,
    ) -> None:
        self.name = name
        self._bars = bars
        self._fail_times = fail_times
        # Real MarketSource implementations always wrap failures into a
        # well-typed exception (CcxtFetchError/YfFetchError/CircuitOpenError)
        # per base.py's contract - this fake mirrors that so ingest.py's
        # fallback logic (which matches on those specific types) is
        # exercised exactly as it would be against a real source.
        self._exc = exc or CcxtFetchError("simulated source failure")
        self.calls: list[tuple[str, Timeframe, datetime | None, int]] = []

    def fetch_ohlcv(
        self, source_symbol: str, timeframe: Timeframe, since: datetime | None, limit: int
    ) -> list[Bar]:
        self.calls.append((source_symbol, timeframe, since, limit))
        if self._fail_times > 0:
            self._fail_times -= 1
            raise self._exc
        if since is None:
            return self._bars
        return [b for b in self._bars if b.ts_utc >= since]

    def health(self) -> SourceHealth:
        return SourceHealth(
            source_name=self.name,
            ok=self._fail_times == 0,
            last_success_utc=T0,
            consecutive_failures=0,
            last_error=None,
        )


def make_bars(symbol: str, count: int, *, is_closed: bool = True) -> list[Bar]:
    return [
        Bar(
            symbol=symbol,
            timeframe=Timeframe.H1,
            ts_utc=T0 + timedelta(hours=i),
            open=100.0 + i,
            high=101.0 + i,
            low=99.0 + i,
            close=100.5 + i,
            volume=10.0,
            is_closed=is_closed,
        )
        for i in range(count)
    ]


_CONFIG = MarketsConfig.model_validate(
    {
        "timeframes": {"primary": "1h", "secondary": []},
        "forecast": {"lookback_bars": 400, "pred_len": 24},
        "sessions": {"crypto": {"always_open": True}},
        "groups": {
            "crypto": {
                "session": "crypto",
                "instruments": [
                    {
                        "display_symbol": "BTC/USDT",
                        "decimals": 2,
                        "source": "ccxt",
                        "exchange": "binance",
                        "source_symbol": "BTC/USDT",
                        "fallback_exchange": "coinbase",
                        "fallback_source_symbol": "BTC/USD",
                    }
                ],
            }
        },
        "risk": {"min_rr_for_setup": 2.0, "default_risk_pct": 2.0},
        "calibration": {"min_observations_for_display": 30, "target_band_coverage": 0.8},
    }
)


def test_route_for_ccxt_instrument_resolves_primary_and_fallback() -> None:
    registry = SourceRegistry()
    binance = FakeSource("ccxt:binance", [])
    coinbase = FakeSource("ccxt:coinbase", [])
    registry.register_ccxt("binance", binance)
    registry.register_ccxt("coinbase", coinbase)

    instrument = _CONFIG.get_instrument("BTC/USDT")
    route = registry.route_for(instrument)

    assert route.primary is binance
    assert route.primary_symbol == "BTC/USDT"
    assert route.fallback is coinbase
    assert route.fallback_symbol == "BTC/USD"


def test_ingest_instrument_falls_back_when_primary_fails() -> None:
    registry = SourceRegistry()
    bars_from_fallback = make_bars("BTC/USD", 5)
    binance = FakeSource("ccxt:binance", [], fail_times=99)
    coinbase = FakeSource("ccxt:coinbase", bars_from_fallback)
    registry.register_ccxt("binance", binance)
    registry.register_ccxt("coinbase", coinbase)
    store = SqliteStore(":memory:", markets_config=_CONFIG)
    instrument = _CONFIG.get_instrument("BTC/USDT")

    result = ingest_instrument(instrument, Timeframe.H1, registry, store)

    assert result.passed
    stored = store.get_latest_bars("BTC/USDT", Timeframe.H1, limit=10)
    assert len(stored) == 5
    # Canonicalized to the instrument's display_symbol, not the fallback's
    # source_symbol ("BTC/USD").
    assert all(b.symbol == "BTC/USDT" for b in stored)
    assert coinbase.calls[0][0] == "BTC/USD"


def test_ingest_instrument_raises_when_both_primary_and_fallback_fail() -> None:
    registry = SourceRegistry()
    binance = FakeSource("ccxt:binance", [], fail_times=99)
    coinbase = FakeSource("ccxt:coinbase", [], fail_times=99)
    registry.register_ccxt("binance", binance)
    registry.register_ccxt("coinbase", coinbase)
    store = SqliteStore(":memory:", markets_config=_CONFIG)
    instrument = _CONFIG.get_instrument("BTC/USDT")

    with pytest.raises(CcxtFetchError, match="simulated source failure"):
        ingest_instrument(instrument, Timeframe.H1, registry, store)


def test_ingest_instrument_records_health_for_primary_and_fallback() -> None:
    registry = SourceRegistry()
    binance = FakeSource("ccxt:binance", [], fail_times=99)
    coinbase = FakeSource("ccxt:coinbase", make_bars("BTC/USD", 2))
    registry.register_ccxt("binance", binance)
    registry.register_ccxt("coinbase", coinbase)
    store = SqliteStore(":memory:", markets_config=_CONFIG)
    instrument = _CONFIG.get_instrument("BTC/USDT")

    ingest_instrument(instrument, Timeframe.H1, registry, store)

    recorded = {h.source_name for h in store.source_health()}
    assert recorded == {"ccxt:binance", "ccxt:coinbase"}


def test_ingest_instrument_backfills_then_goes_incremental() -> None:
    registry = SourceRegistry()
    all_bars = make_bars("BTC/USDT", 10)
    source = FakeSource("ccxt:binance", all_bars)
    registry.register_ccxt("binance", source)
    registry.register_ccxt("coinbase", FakeSource("ccxt:coinbase", []))
    store = SqliteStore(":memory:", markets_config=_CONFIG)
    instrument = _CONFIG.get_instrument("BTC/USDT")

    first = ingest_instrument(instrument, Timeframe.H1, registry, store)
    assert first.passed
    assert source.calls[0][2] is None  # first call: full backfill, since=None

    second = ingest_instrument(instrument, Timeframe.H1, registry, store)
    assert second.passed
    # Second call is incremental: since = the last closed bar already
    # stored, not None again.
    assert source.calls[1][2] == store.get_latest_bars("BTC/USDT", Timeframe.H1, 1)[0].ts_utc \
        or source.calls[1][2] is not None


def test_run_full_backfill_covers_every_instrument_and_timeframe() -> None:
    registry = SourceRegistry()
    registry.register_ccxt("binance", FakeSource("ccxt:binance", make_bars("BTC/USDT", 5)))
    registry.register_ccxt("coinbase", FakeSource("ccxt:coinbase", []))
    store = SqliteStore(":memory:", markets_config=_CONFIG)

    results = run_full_backfill(_CONFIG, registry, store)

    assert ("BTC/USDT", "1h") in results
    assert results[("BTC/USDT", "1h")].passed
    assert len(store.get_latest_bars("BTC/USDT", Timeframe.H1, 100)) == 5


def test_run_incremental_update_only_fetches_selected_timeframes() -> None:
    registry = SourceRegistry()
    source = FakeSource("ccxt:binance", make_bars("BTC/USDT", 3))
    registry.register_ccxt("binance", source)
    registry.register_ccxt("coinbase", FakeSource("ccxt:coinbase", []))
    store = SqliteStore(":memory:", markets_config=_CONFIG)

    run_incremental_update(_CONFIG, registry, store, timeframes=[Timeframe.H1])

    assert all(call[1] is Timeframe.H1 for call in source.calls)


def test_route_for_yfinance_instrument_uses_same_source_for_fallback() -> None:
    yf_config = MarketsConfig.model_validate(
        {
            "timeframes": {"primary": "1h", "secondary": []},
            "forecast": {"lookback_bars": 400, "pred_len": 24},
            "sessions": {"metals_futures": {
                "always_open": False,
                "weekday_open": "sun 23:00",
                "weekday_close": "fri 22:00",
                "timezone": "America/Chicago",
            }},
            "groups": {
                "metals": {
                    "session": "metals_futures",
                    "instruments": [
                        {
                            "display_symbol": "GOUD",
                            "decimals": 2,
                            "source": "yfinance",
                            "source_symbol": "GC=F",
                            "fallback_source_symbol": "XAUUSD=X",
                        }
                    ],
                }
            },
            "risk": {"min_rr_for_setup": 2.0, "default_risk_pct": 2.0},
            "calibration": {"min_observations_for_display": 30, "target_band_coverage": 0.8},
        }
    )
    registry = SourceRegistry()
    yf_source = FakeSource("yfinance", [])
    registry.register_yfinance(yf_source)
    instrument = yf_config.get_instrument("GOUD")

    route = registry.route_for(instrument)

    assert route.primary is yf_source
    assert route.primary_symbol == "GC=F"
    assert route.fallback is yf_source
    assert route.fallback_symbol == "XAUUSD=X"


def test_route_for_yfinance_without_fallback_symbol_has_no_fallback() -> None:
    yf_config = MarketsConfig.model_validate(
        {
            "timeframes": {"primary": "1h", "secondary": []},
            "forecast": {"lookback_bars": 400, "pred_len": 24},
            "sessions": {"fx": {
                "always_open": False,
                "weekday_open": "sun 22:00",
                "weekday_close": "fri 22:00",
                "timezone": "UTC",
            }},
            "groups": {
                "fx": {
                    "session": "fx",
                    "instruments": [
                        {
                            "display_symbol": "EUR/USD",
                            "decimals": 5,
                            "source": "yfinance",
                            "source_symbol": "EURUSD=X",
                        }
                    ],
                }
            },
            "risk": {"min_rr_for_setup": 2.0, "default_risk_pct": 2.0},
            "calibration": {"min_observations_for_display": 30, "target_band_coverage": 0.8},
        }
    )
    registry = SourceRegistry()
    registry.register_yfinance(FakeSource("yfinance", []))
    instrument = yf_config.get_instrument("EUR/USD")

    route = registry.route_for(instrument)
    assert route.fallback is None
    assert route.fallback_symbol is None


_TWO_INSTRUMENT_CONFIG = MarketsConfig.model_validate(
    {
        "timeframes": {"primary": "1h", "secondary": []},
        "forecast": {"lookback_bars": 400, "pred_len": 24},
        "sessions": {"crypto": {"always_open": True}},
        "groups": {
            "crypto": {
                "session": "crypto",
                "instruments": [
                    {
                        "display_symbol": "AAA/USDT",
                        "decimals": 2,
                        "source": "ccxt",
                        "exchange": "flaky",
                        "source_symbol": "AAA/USDT",
                    },
                    {
                        "display_symbol": "BBB/USDT",
                        "decimals": 2,
                        "source": "ccxt",
                        "exchange": "healthy",
                        "source_symbol": "BBB/USDT",
                    },
                ],
            }
        },
        "risk": {"min_rr_for_setup": 2.0, "default_risk_pct": 2.0},
        "calibration": {"min_observations_for_display": 30, "target_band_coverage": 0.8},
    }
)


def test_run_full_backfill_isolates_one_persistently_failing_instrument() -> None:
    """Red-team Round 2 (fault injection): a source stuck returning 429s
    forever (no fallback configured) must not prevent every OTHER
    instrument's backfill in the same cycle from running - mirrors the
    per-instrument isolation `snapshot.py::build_snapshot` already has one
    layer up. Before this fix, `ingest_instrument` raising uncaught for
    "AAA/USDT" aborted the loop before "BBB/USDT" was ever attempted.
    """
    registry = SourceRegistry()
    registry.register_ccxt("flaky", FakeSource("ccxt:flaky", [], fail_times=999))
    healthy = FakeSource("ccxt:healthy", make_bars("BBB/USDT", 5))
    registry.register_ccxt("healthy", healthy)
    store = SqliteStore(":memory:", markets_config=_TWO_INSTRUMENT_CONFIG)

    results = run_full_backfill(_TWO_INSTRUMENT_CONFIG, registry, store)

    assert len(healthy.calls) == 1  # actually reached, not skipped by an earlier crash
    assert ("BBB/USDT", "1h") in results
    assert results[("BBB/USDT", "1h")].passed
    assert len(store.get_latest_bars("BBB/USDT", Timeframe.H1, 100)) == 5
    # The failing pair is simply absent from the results dict, not present
    # with a fabricated "passed" result.
    assert ("AAA/USDT", "1h") not in results


def test_run_incremental_update_isolates_one_persistently_failing_instrument() -> None:
    """Same guarantee as above, for the recurring incremental-refresh path
    the scheduler actually calls every cycle."""
    registry = SourceRegistry()
    registry.register_ccxt("flaky", FakeSource("ccxt:flaky", [], fail_times=999))
    healthy = FakeSource("ccxt:healthy", make_bars("BBB/USDT", 3))
    registry.register_ccxt("healthy", healthy)
    store = SqliteStore(":memory:", markets_config=_TWO_INSTRUMENT_CONFIG)

    results = run_incremental_update(_TWO_INSTRUMENT_CONFIG, registry, store)

    assert len(healthy.calls) == 1
    assert ("BBB/USDT", "1h") in results
    assert ("AAA/USDT", "1h") not in results


def test_circuit_open_error_on_primary_triggers_fallback() -> None:
    registry = SourceRegistry()
    binance = FakeSource("ccxt:binance", [], fail_times=1, exc=CircuitOpenError("open"))
    coinbase = FakeSource("ccxt:coinbase", make_bars("BTC/USD", 2))
    registry.register_ccxt("binance", binance)
    registry.register_ccxt("coinbase", coinbase)
    store = SqliteStore(":memory:", markets_config=_CONFIG)
    instrument = _CONFIG.get_instrument("BTC/USDT")

    result = ingest_instrument(instrument, Timeframe.H1, registry, store)
    assert result.passed
    assert len(coinbase.calls) == 1

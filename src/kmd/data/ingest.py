"""Ingestion orchestration: backfill and incremental updates across every
(instrument, timeframe) pair declared in `config/markets.yaml`.

This is the module a scheduler (builder-core's future `scheduler.py`) is
expected to call — `run_full_backfill` for a fresh store, then
`run_incremental_update` on a recurring cadence. Both are built on the
lower-level `ingest_instrument`, which is itself safe to call repeatedly:
it backfills when there's no history yet and incrementally updates
otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import structlog

from kmd.data.base import Bar, MarketSource, QualityGateResult, Timeframe
from kmd.data.ccxt_source import CcxtFetchError, CcxtSource, build_ccxt_exchange
from kmd.data.markets_config import DataSource, Instrument, MarketsConfig
from kmd.data.resilience import CircuitOpenError
from kmd.data.store import SqliteStore
from kmd.data.yfinance_source import YfFetchError, YfinanceSource

logger = structlog.get_logger(__name__)

#: "Backfill at least 1000 bars per symbol/timeframe" per the project brief.
MIN_BACKFILL_BARS = 1000

_FETCH_ERRORS: tuple[type[Exception], ...] = (CcxtFetchError, YfFetchError, CircuitOpenError)


@dataclass(frozen=True)
class SourceRoute:
    """The primary source (+ optional fallback) resolved for one
    instrument, with the exact `source_symbol` string each one expects.
    """

    primary: MarketSource
    primary_symbol: str
    fallback: MarketSource | None
    fallback_symbol: str | None


class SourceRegistry:
    """Maps each instrument to the `MarketSource`(s) that can fetch it.
    Built once by the caller (e.g. at scheduler startup) via
    `register_ccxt`/`register_yfinance`, then handed to `ingest_instrument`.
    """

    def __init__(self) -> None:
        self._ccxt_sources: dict[str, MarketSource] = {}
        self._yfinance_source: MarketSource | None = None

    def register_ccxt(self, exchange_id: str, source: MarketSource) -> None:
        self._ccxt_sources[exchange_id] = source

    def register_yfinance(self, source: MarketSource) -> None:
        self._yfinance_source = source

    def route_for(self, instrument: Instrument) -> SourceRoute:
        if instrument.source is DataSource.CCXT:
            if instrument.exchange is None:
                raise ValueError(f"{instrument.display_symbol}: ccxt instrument missing `exchange`")
            primary = self._ccxt_sources[instrument.exchange]
            fallback = (
                self._ccxt_sources.get(instrument.fallback_exchange)
                if instrument.fallback_exchange
                else None
            )
            return SourceRoute(
                primary=primary,
                primary_symbol=instrument.source_symbol,
                fallback=fallback,
                fallback_symbol=instrument.fallback_source_symbol,
            )

        if self._yfinance_source is None:
            raise RuntimeError("no yfinance source registered on this SourceRegistry")
        has_fallback_symbol = instrument.fallback_source_symbol is not None
        return SourceRoute(
            primary=self._yfinance_source,
            primary_symbol=instrument.source_symbol,
            fallback=self._yfinance_source if has_fallback_symbol else None,
            fallback_symbol=instrument.fallback_source_symbol,
        )


def _fetch_with_fallback(
    route: SourceRoute,
    timeframe: Timeframe,
    since: datetime | None,
    limit: int,
) -> list[Bar]:
    try:
        return route.primary.fetch_ohlcv(route.primary_symbol, timeframe, since, limit)
    except _FETCH_ERRORS as primary_exc:
        if route.fallback is None or route.fallback_symbol is None:
            raise
        logger.warning(
            "primary source failed, trying fallback",
            error=str(primary_exc),
            fallback_symbol=route.fallback_symbol,
        )
        return route.fallback.fetch_ohlcv(route.fallback_symbol, timeframe, since, limit)


def _canonicalize(bars: list[Bar], display_symbol: str) -> list[Bar]:
    """Rewrites each `Bar.symbol` to the instrument's canonical
    `display_symbol` before it reaches the store: the store is keyed on one
    identity per instrument regardless of which upstream `source_symbol`
    (primary or fallback) actually served the data for this batch.
    """
    return [bar.model_copy(update={"symbol": display_symbol}) for bar in bars]


def _record_health(route: SourceRoute, store: SqliteStore) -> None:
    store.record_source_health(route.primary.health())
    if route.fallback is not None and route.fallback is not route.primary:
        store.record_source_health(route.fallback.health())


def ingest_instrument(
    instrument: Instrument,
    timeframe: Timeframe,
    registry: SourceRegistry,
    store: SqliteStore,
    *,
    backfill_bars: int = MIN_BACKFILL_BARS,
) -> QualityGateResult:
    """Backfills (if the store has no closed history yet for this
    symbol/timeframe) or incrementally updates it (fetching only what's new
    since the last stored closed bar) otherwise. Safe to call repeatedly -
    this is exactly what a scheduler calls on every refresh tick.
    """
    route = registry.route_for(instrument)
    last_closed = store.get_last_closed_ts(instrument.display_symbol, timeframe)
    since = last_closed  # None => full backfill; a timestamp => incremental

    bars = _fetch_with_fallback(route, timeframe, since, backfill_bars)
    _record_health(route, store)

    canonical_bars = _canonicalize(bars, instrument.display_symbol)
    return store.upsert_bars(canonical_bars)


def build_default_source_registry(
    config: MarketsConfig,
    *,
    ccxt_api_key: str = "",
    ccxt_api_secret: str = "",
) -> SourceRegistry:
    """Builds a `SourceRegistry` wired to real `CcxtSource`/`YfinanceSource`
    adapters for every exchange/provider actually referenced by
    `config`'s instruments — one `CcxtSource` per distinct ccxt exchange id
    (primary or fallback) and a single shared `YfinanceSource` if any
    instrument uses yfinance. Convenience for the scheduler; nothing stops
    a caller from constructing a `SourceRegistry` by hand for tests or a
    non-default wiring (e.g. injected fakes).
    """
    registry = SourceRegistry()
    exchange_ids: set[str] = set()
    needs_yfinance = False
    for instrument in config.all_instruments():
        if instrument.source is DataSource.CCXT:
            if instrument.exchange:
                exchange_ids.add(instrument.exchange)
            if instrument.fallback_exchange:
                exchange_ids.add(instrument.fallback_exchange)
        else:
            needs_yfinance = True

    # `fetch_ohlcv` is a public endpoint on every exchange ccxt supports, so
    # no exchange here strictly needs an API key at all; `ccxt_api_key`/
    # `ccxt_api_secret` (Settings' single configured credential pair) is
    # passed to every exchange built here only as a convenience for a
    # deployment that has one, not because a fallback exchange (e.g.
    # Coinbase) is expected to accept the primary's (e.g. Binance's) key.
    for exchange_id in sorted(exchange_ids):
        exchange = build_ccxt_exchange(
            exchange_id, api_key=ccxt_api_key, api_secret=ccxt_api_secret
        )
        registry.register_ccxt(exchange_id, CcxtSource(exchange))

    if needs_yfinance:
        registry.register_yfinance(YfinanceSource())

    return registry


def run_full_backfill(
    config: MarketsConfig,
    registry: SourceRegistry,
    store: SqliteStore,
    *,
    backfill_bars: int = MIN_BACKFILL_BARS,
) -> dict[tuple[str, str], QualityGateResult]:
    """Backfills every (instrument, timeframe) pair declared in
    `config/markets.yaml`. Intended for a fresh store with no history, but
    safe to call again later (it degrades to an incremental update for any
    pair that already has closed bars).
    """
    results: dict[tuple[str, str], QualityGateResult] = {}
    for instrument in config.all_instruments():
        for timeframe in config.timeframes.all:
            try:
                result = ingest_instrument(
                    instrument, timeframe, registry, store, backfill_bars=backfill_bars
                )
            except Exception:
                # One instrument/timeframe pair whose source is persistently
                # down/rate-limited (raises uncaught past its own circuit
                # breaker - e.g. CircuitOpenError with no configured
                # fallback) must not abort backfill for every OTHER
                # instrument still queued behind it in this loop. Mirrors
                # the per-instrument isolation `snapshot.py::build_snapshot`
                # already applies one layer up (red-team Round 2).
                logger.exception(
                    "ingest failed, skipping this pair for this cycle",
                    symbol=instrument.display_symbol,
                    timeframe=timeframe.value,
                )
                continue
            results[(instrument.display_symbol, timeframe.value)] = result
            if not result.passed:
                logger.warning(
                    "quality gate rejected backfill batch",
                    symbol=instrument.display_symbol,
                    timeframe=timeframe.value,
                    issues=[issue.kind for issue in result.issues],
                )
    return results


def run_incremental_update(
    config: MarketsConfig,
    registry: SourceRegistry,
    store: SqliteStore,
    *,
    timeframes: list[Timeframe] | None = None,
) -> dict[tuple[str, str], QualityGateResult]:
    """Fetches only new bars since each (instrument, timeframe)'s last
    closed bar. Intended to run on the scheduler's regular cadence.
    """
    results: dict[tuple[str, str], QualityGateResult] = {}
    selected_timeframes = timeframes or config.timeframes.all
    for instrument in config.all_instruments():
        for timeframe in selected_timeframes:
            try:
                result = ingest_instrument(instrument, timeframe, registry, store)
            except Exception:
                # See the matching comment in `run_full_backfill`: one
                # persistently-failing source (e.g. a rate-limited exchange
                # with no configured fallback, whose CircuitOpenError has no
                # `_FETCH_ERRORS` handler left to catch it) must not abort
                # the incremental refresh for every OTHER instrument queued
                # behind it in this loop (red-team Round 2).
                logger.exception(
                    "incremental ingest failed, skipping this pair for this cycle",
                    symbol=instrument.display_symbol,
                    timeframe=timeframe.value,
                )
                continue
            results[(instrument.display_symbol, timeframe.value)] = result
            if not result.passed:
                logger.warning(
                    "quality gate rejected incremental batch",
                    symbol=instrument.display_symbol,
                    timeframe=timeframe.value,
                    issues=[issue.kind for issue in result.issues],
                )
    return results

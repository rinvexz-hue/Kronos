"""Contract between the data layer (builder-data) and everything downstream
(builder-core). builder-data implements `MarketSource` and `MarketStore`
against these exact shapes; builder-core reads through `MarketStore` only
and never reaches into a source adapter or the SQLite schema directly.

This module intentionally contains no logic beyond validation — it is the
interface, not an implementation.
"""

from __future__ import annotations

import math
from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ValidationInfo, field_validator


class Timeframe(StrEnum):
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"


class Bar(BaseModel):
    """One OHLCV candle. `ts_utc` is the bar's OPEN time, always UTC and
    tz-aware — this is a hard invariant, not a convention, and is enforced
    by the validator below.

    `is_closed` must be conservative: a bar that might still be forming
    (e.g. the most recent bar returned by a source before its interval has
    elapsed) MUST be marked `is_closed=False`. Downstream forecast code is
    only allowed to use `is_closed=True` bars as context — this is the
    system's primary look-ahead-bias defense, and it lives here, at the
    boundary, on purpose.
    """

    symbol: str
    timeframe: Timeframe
    ts_utc: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    is_closed: bool

    @field_validator("ts_utc")
    @classmethod
    def must_be_utc_aware(cls, v: datetime) -> datetime:
        # NOTE (builder-data): reads `utcoffset()` once into a local instead
        # of calling it a second time on the next line. Behavior is
        # unchanged; this only lets mypy --strict narrow the `None` case
        # instead of seeing a second, distinct `timedelta | None` call it
        # can't prove non-None. See DECISIONS.md for why this contract file
        # was touched.
        offset = v.utcoffset()
        if v.tzinfo is None or offset is None:
            raise ValueError("Bar.ts_utc must be timezone-aware")
        if offset.total_seconds() != 0:
            raise ValueError("Bar.ts_utc must be normalized to UTC (offset must be 0)")
        return v

    @field_validator("open", "high", "low", "close", "volume")
    @classmethod
    def must_be_finite(cls, v: float, info: ValidationInfo) -> float:
        # A real market reading is never NaN or +-Inf. A source occasionally
        # returns NaN for a genuinely illiquid period (observed in practice
        # from yfinance) rather than raising; silently accepting it here
        # would let a single poisoned field propagate through every
        # downstream computation (vol, quantiles, the model's own input
        # tensor) as an untyped, unflagged failure instead of the well-typed
        # fetch error `MarketSource.fetch_ohlcv`'s own contract requires
        # (red-team Round 2, fault-injection finding on NaN propagation).
        if math.isnan(v) or math.isinf(v):
            raise ValueError(f"Bar.{info.field_name} must be a finite number, got {v!r}")
        return v


class SourceHealth(BaseModel):
    source_name: str
    ok: bool
    last_success_utc: datetime | None
    consecutive_failures: int
    last_error: str | None


@runtime_checkable
class MarketSource(Protocol):
    """One upstream market-data provider (a ccxt exchange, yfinance, ...)."""

    name: str

    def fetch_ohlcv(
        self,
        source_symbol: str,
        timeframe: Timeframe,
        since: datetime | None,
        limit: int,
    ) -> list[Bar]:
        """Fetch up to `limit` bars at/after `since` (or the most recent
        `limit` bars if `since` is None). Must raise a well-typed exception
        on failure (never return a fabricated/interpolated bar) — retry and
        circuit-breaking are the adapter's responsibility, not the caller's.
        """
        ...

    def health(self) -> SourceHealth: ...


QualityIssueKind = Literal["gap", "duplicate", "out_of_order", "revised_history"]


class QualityIssue(BaseModel):
    kind: QualityIssueKind
    symbol: str
    timeframe: Timeframe
    detail: str
    ts_utc: datetime | None


class QualityGateResult(BaseModel):
    passed: bool
    issues: list[QualityIssue]


@runtime_checkable
class MarketStore(Protocol):
    """Persistence + read interface. This is the ONLY thing builder-core
    is allowed to depend on from the data layer.
    """

    def upsert_bars(self, bars: list[Bar]) -> QualityGateResult:
        """Quality-gates and stores bars. Bars that fail the gate are not
        written; the result always reports what happened, never raises for
        an ordinary data-quality issue (it may raise for a genuine
        programming error, e.g. a malformed Bar that pydantic already would
        have rejected upstream).
        """
        ...

    def get_latest_bars(self, symbol: str, timeframe: Timeframe, limit: int) -> list[Bar]:
        """Most recent `limit` bars, oldest first, ascending by ts_utc."""
        ...

    def get_last_closed_ts(self, symbol: str, timeframe: Timeframe) -> datetime | None:
        """Timestamp (bar-open, UTC) of the most recent bar with
        is_closed=True. This is the forecast cache key's anchor — it must
        only ever advance when a genuinely new closed bar lands.
        """
        ...

    def source_health(self) -> list[SourceHealth]: ...

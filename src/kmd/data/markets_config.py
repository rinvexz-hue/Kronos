"""Typed, validated access to `config/markets.yaml` — the single source of
truth for instruments, sessions, timeframes, and forecast/calibration
parameters. No other module (in this package or elsewhere in the
application) should hardcode a symbol, session window, or timeframe list;
everything must come from here.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator

from kmd.config import MARKETS_CONFIG_PATH
from kmd.data.base import Timeframe


class DataSource(StrEnum):
    """Which adapter in `src/kmd/data/` serves this instrument."""

    CCXT = "ccxt"
    YFINANCE = "yfinance"


class TimeframesSpec(BaseModel):
    primary: Timeframe
    secondary: list[Timeframe]

    @property
    def all(self) -> list[Timeframe]:
        return [self.primary, *self.secondary]


class ForecastSpec(BaseModel):
    lookback_bars: int = Field(gt=0)
    pred_len: int = Field(gt=0)


class RiskSpec(BaseModel):
    min_rr_for_setup: float
    default_risk_pct: float


class CalibrationSpec(BaseModel):
    min_observations_for_display: int = Field(ge=0)
    target_band_coverage: float = Field(gt=0, lt=1)


class SessionSpec(BaseModel):
    """One market-hours template. `weekday_open` / `weekday_close` are
    strings like `"sun 23:00"`, in the instrument's home-exchange local
    time named by `timezone` — see `sessions.py`, which converts them to
    UTC via `zoneinfo` at query time (never at load time, since the UTC
    offset depends on the date because of DST).
    """

    always_open: bool
    weekday_open: str | None = None
    weekday_close: str | None = None
    timezone: str | None = None

    @model_validator(mode="after")
    def _validate_hours_present_unless_always_open(self) -> SessionSpec:
        if not self.always_open:
            missing = [
                name
                for name, value in (
                    ("weekday_open", self.weekday_open),
                    ("weekday_close", self.weekday_close),
                    ("timezone", self.timezone),
                )
                if value is None
            ]
            if missing:
                raise ValueError(
                    f"session with always_open=false is missing: {', '.join(missing)}"
                )
        return self


class InstrumentSpec(BaseModel):
    """One instrument exactly as declared under a `groups.<name>.instruments`
    entry — before its group/session are attached. See `Instrument` for the
    denormalized, application-facing form produced by
    `MarketsConfig.all_instruments()`.
    """

    display_symbol: str
    decimals: int = Field(ge=0)
    source: DataSource
    source_symbol: str
    exchange: str | None = None
    fallback_exchange: str | None = None
    fallback_source_symbol: str | None = None

    @model_validator(mode="after")
    def _validate_source_specific_fields(self) -> InstrumentSpec:
        if self.source is DataSource.CCXT and not self.exchange:
            raise ValueError(f"{self.display_symbol}: ccxt instruments require `exchange`")
        if self.source is DataSource.YFINANCE and (self.exchange or self.fallback_exchange):
            raise ValueError(
                f"{self.display_symbol}: `exchange`/`fallback_exchange` only apply to ccxt "
                "instruments"
            )
        return self


class GroupSpec(BaseModel):
    session: str
    instruments: list[InstrumentSpec]


class Instrument(BaseModel):
    """One instrument, fully resolved: its spec plus the group and session
    it belongs to. This — not `InstrumentSpec` — is what the rest of the
    application (including builder-core) should iterate over, via
    `MarketsConfig.all_instruments()`.

    `symbol` (an alias for `display_symbol`) is the canonical identity used
    to key the store and everything downstream of it; `source_symbol` /
    `fallback_source_symbol` are only ever passed to the source adapter
    that actually talks to the upstream API — see `ingest.py`'s
    `_canonicalize` for where the two identities are reconciled.
    """

    display_symbol: str
    decimals: int
    source: DataSource
    source_symbol: str
    exchange: str | None
    fallback_exchange: str | None
    fallback_source_symbol: str | None
    group: str
    session_name: str

    @property
    def symbol(self) -> str:
        return self.display_symbol


class MarketsConfig(BaseModel):
    timeframes: TimeframesSpec
    forecast: ForecastSpec
    sessions: dict[str, SessionSpec]
    groups: dict[str, GroupSpec]
    risk: RiskSpec
    calibration: CalibrationSpec

    @model_validator(mode="after")
    def _validate_group_sessions_exist(self) -> MarketsConfig:
        for group_name, group in self.groups.items():
            if group.session not in self.sessions:
                raise ValueError(
                    f"group {group_name!r} references unknown session {group.session!r}"
                )
        return self

    def all_instruments(self) -> list[Instrument]:
        """Every instrument across every group, denormalized with its group
        and session name attached, in `markets.yaml`'s declared order.
        """
        resolved: list[Instrument] = []
        for group_name, group in self.groups.items():
            for spec in group.instruments:
                resolved.append(
                    Instrument(
                        display_symbol=spec.display_symbol,
                        decimals=spec.decimals,
                        source=spec.source,
                        source_symbol=spec.source_symbol,
                        exchange=spec.exchange,
                        fallback_exchange=spec.fallback_exchange,
                        fallback_source_symbol=spec.fallback_source_symbol,
                        group=group_name,
                        session_name=group.session,
                    )
                )
        return resolved

    def get_instrument(self, display_symbol: str) -> Instrument:
        for instrument in self.all_instruments():
            if instrument.display_symbol == display_symbol:
                return instrument
        raise KeyError(f"no instrument with display_symbol={display_symbol!r}")


def load_markets_config(path: Path | None = None) -> MarketsConfig:
    """Loads and validates `config/markets.yaml`. Raises
    `pydantic.ValidationError` on a malformed file rather than silently
    accepting bad configuration.
    """
    config_path = path or MARKETS_CONFIG_PATH
    raw = yaml.safe_load(config_path.read_text())
    return MarketsConfig.model_validate(raw)


@lru_cache(maxsize=1)
def get_markets_config() -> MarketsConfig:
    """Process-wide cached singleton for the default `config/markets.yaml`
    path. Tests that need a different file should call
    `load_markets_config(path)` directly instead of this.
    """
    return load_markets_config()

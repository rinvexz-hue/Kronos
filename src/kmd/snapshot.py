"""The single DTO that feeds the entire dashboard. `GET /api/snapshot`
returns exactly `SnapshotDTO.model_dump(mode="json")`; the frontend reads
nothing else. This module defines the contract shape; `build_snapshot()`
is builder-core's implementation, assembled from the forecast/analysis/
calibration layers plus data-source status — it must never call into the
data layer's source adapters or SQLite schema directly, only through
`kmd.data.base.MarketStore` and the forecast/analysis modules.

Every field that could be mistaken for a live guarantee for a viewer
either carries its own provenance (e.g. `Level.reason`) or is paired with
a status field showing why it might not be trustworthy right now (e.g.
`DataSourceStatus.is_stale`, `CalibrationStats.sufficient_data`).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

RegimeLabel = Literal["trend_up", "trend_down", "range", "unknown"]
VolRegime = Literal["low", "normal", "high"]
LevelKind = Literal["swing_high", "swing_low", "pdh", "pdl", "ma_cluster", "round_number"]


class Regime(BaseModel):
    label: RegimeLabel
    vol_regime: VolRegime
    reason: str


class Level(BaseModel):
    price: float
    kind: LevelKind
    reason: str


class ForecastMetrics(BaseModel):
    p_up_24h: float
    q10: float
    q50: float
    q90: float
    p_vol_expansion: float
    band_width_pct: float
    n_paths: int
    model_name: str
    generated_at_utc: datetime
    last_closed_bar_ts_utc: datetime


class CalibrationStats(BaseModel):
    n_observations: int
    brier_score: float | None
    mae_q50: float | None
    band_coverage: float | None
    sufficient_data: bool


class SetupCard(BaseModel):
    direction: Literal["long", "short"]
    entry: float
    invalidation: float
    target: float
    rr: float
    risk_pct: float


class DataSourceStatus(BaseModel):
    source_name: str
    last_update_utc: datetime | None
    is_stale: bool
    error_count_last_hour: int
    market_session_open: bool | None


class AssetSnapshot(BaseModel):
    display_symbol: str
    group: str
    decimals: int
    price: float
    change_1h_pct: float | None
    change_24h_pct: float | None
    change_7d_pct: float | None
    sparkline: list[float]
    regime: Regime
    levels: list[Level]
    forecast: ForecastMetrics
    calibration: CalibrationStats
    setup: SetupCard | None
    source_status: DataSourceStatus


class SnapshotDTO(BaseModel):
    generated_at_utc: datetime
    correlation_id: str
    assets: list[AssetSnapshot]


def build_snapshot() -> SnapshotDTO:
    """Assemble the current SnapshotDTO from the data/forecast/analysis/
    calibration layers. Implemented by builder-core.
    """
    raise NotImplementedError("build_snapshot is implemented by builder-core")

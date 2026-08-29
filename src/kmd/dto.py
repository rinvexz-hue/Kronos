"""DTO shapes for the dashboard contract, split out of `kmd.snapshot` so
`kmd.analysis.*` (regime/levels/setup) can depend on the shapes WITHOUT
importing `kmd.snapshot` itself — `kmd.snapshot.build_snapshot` imports
`kmd.analysis.*`, so having those modules import back from `kmd.snapshot`
would be a circular import. `kmd.snapshot` re-exports every name defined
here, so `from kmd.snapshot import SnapshotDTO` (the documented contract
import) keeps working unchanged; this module is otherwise an internal
implementation detail of that split.

Every field that could be mistaken for a live guarantee for a viewer
either carries its own provenance (e.g. `Level.reason`) or is paired with
a status field showing why it might not be trustworthy right now (e.g.
`DataSourceStatus.is_stale`, `CalibrationStats.sufficient_data`).
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ValidationInfo, field_validator

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

    @field_validator("p_up_24h", "q10", "q50", "q90", "p_vol_expansion", "band_width_pct")
    @classmethod
    def must_be_finite(cls, v: float, info: ValidationInfo) -> float:
        # A NaN/Inf here (e.g. Kronos itself producing NaN paths from an
        # extreme/edge-case input, independent of whether the input bars
        # were themselves clean) is not a valid statistic to show as if it
        # were live. Rejecting it here - at DTO construction, inside
        # `snapshot.py::_get_or_compute_forecast`, itself inside
        # `build_snapshot`'s existing per-instrument try/except - degrades
        # to "this one tile skipped, logged, this cycle" rather than: (a)
        # `NaN`/`null` reaching the dashboard as a fabricated-looking value,
        # or (b) surviving `model_dump_json()` (which turns NaN into JSON
        # `null`) only to make `SnapshotFileStore.load()`'s later
        # `model_validate` blow up with an uncaught `ValidationError` on
        # every subsequent `/api/snapshot` request until the next refresh
        # overwrites the file (red-team Round 2, fault-injection finding).
        if math.isnan(v) or math.isinf(v):
            raise ValueError(f"ForecastMetrics.{info.field_name} must be finite, got {v!r}")
        return v


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

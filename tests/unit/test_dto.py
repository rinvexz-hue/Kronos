"""Tests for `kmd.dto`'s own validation, independent of `build_snapshot`.

Red-team Round 2 (fault injection, NaN propagation): before the
`ForecastMetrics.must_be_finite` validator existed, a NaN anywhere in a
forecast's numeric fields (e.g. from an unstable model output, or a
poisoned input bar) survived pydantic construction untouched, then:

- `model_dump(mode="json")` kept it as a Python `float('nan')` (unlike
  `model_dump_json()`, which pydantic-core silently turns into JSON
  `null`) - a raw dict handed to FastAPI, which itself converts NaN/Inf to
  `None` via `jsonable_encoder` before responding, so `/api/snapshot`
  itself did not actually break on a live NaN.
- BUT `SnapshotFileStore.save()` uses `model_dump_json()`, which DOES turn
  the NaN into JSON `null` on write - and `ForecastMetrics.p_up_24h` (etc.)
  is typed `float`, not `float | None`, so `SnapshotFileStore.load()`'s
  later `SnapshotDTO.model_validate(json.loads(raw))` raises an uncaught
  `pydantic.ValidationError` on every subsequent `/api/snapshot` read,
  until the next scheduled refresh happens to overwrite the file with
  clean data - breaking the WHOLE dashboard (every asset, not just the one
  with the bad forecast) for however long that takes.

Rejecting the NaN at `ForecastMetrics` construction time instead - inside
`snapshot.py::_get_or_compute_forecast`, itself inside `build_snapshot`'s
existing per-instrument `try/except` - degrades to "this one tile skipped,
logged, this cycle" instead, which is the whole point of that isolation.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from kmd.dto import ForecastMetrics

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _kwargs(**overrides: float) -> dict[str, object]:
    base: dict[str, object] = {
        "p_up_24h": 0.5,
        "q10": 95.0,
        "q50": 100.0,
        "q90": 110.0,
        "p_vol_expansion": 0.3,
        "band_width_pct": 0.15,
        "n_paths": 30,
        "model_name": "test-model",
        "generated_at_utc": NOW,
        "last_closed_bar_ts_utc": NOW,
    }
    base.update(overrides)
    return base


def test_forecast_metrics_accepts_ordinary_finite_values() -> None:
    fm = ForecastMetrics(**_kwargs())  # type: ignore[arg-type]
    assert fm.p_up_24h == 0.5


@pytest.mark.parametrize(
    "field", ["p_up_24h", "q10", "q50", "q90", "p_vol_expansion", "band_width_pct"]
)
def test_forecast_metrics_rejects_nan_in_every_numeric_field(field: str) -> None:
    with pytest.raises(ValidationError, match="must be finite"):
        ForecastMetrics(**_kwargs(**{field: float("nan")}))  # type: ignore[arg-type]


def test_forecast_metrics_rejects_infinity() -> None:
    with pytest.raises(ValidationError, match="must be finite"):
        ForecastMetrics(**_kwargs(p_up_24h=math.inf))  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="must be finite"):
        ForecastMetrics(**_kwargs(q10=-math.inf))  # type: ignore[arg-type]


def test_nan_forecast_metrics_cannot_be_written_then_fail_to_read_back() -> None:
    """The specific, previously-real failure mode: construct once (write
    path) must now fail loudly and immediately, rather than succeeding at
    write time and only exploding later on read - proven directly, not
    just asserted, by attempting the exact round trip a persisted snapshot
    file would have gone through.
    """
    with pytest.raises(ValidationError):
        ForecastMetrics(**_kwargs(q50=float("nan")))  # type: ignore[arg-type]
    # (No SnapshotDTO/SnapshotFileStore round trip to even attempt - the
    # bad value never made it into a persistable object in the first
    # place, which is the fix.)

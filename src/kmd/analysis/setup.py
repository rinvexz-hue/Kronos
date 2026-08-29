"""Deterministic setup-card computation.

Default behaviour is to show NOTHING: a `SetupCard` is only ever emitted
when a defensible entry/invalidation/target triple exists AND the
resulting reward:risk ratio is at least `min_rr` (config default 2.0).
There is no "best effort" setup with a weak RR — below threshold means
`None`, always. No LLM, no direct forecast-engine call (only reads the
already-computed `ForecastMetrics` and `Level`/`Regime` DTOs).
"""

from __future__ import annotations

from kmd.dto import ForecastMetrics, Level, Regime, SetupCard


def _nearest_support_below(levels: list[Level], price: float) -> float | None:
    candidates = [lvl.price for lvl in levels if lvl.kind == "swing_low" and lvl.price < price]
    return max(candidates) if candidates else None


def _nearest_resistance_above(levels: list[Level], price: float) -> float | None:
    candidates = [lvl.price for lvl in levels if lvl.kind == "swing_high" and lvl.price > price]
    return min(candidates) if candidates else None


def compute_setup(
    last_close: float,
    regime: Regime,
    levels: list[Level],
    forecast: ForecastMetrics,
    *,
    min_rr: float,
    risk_pct: float,
) -> SetupCard | None:
    """Returns a `SetupCard` only when:

    - the regime is trending (not `range`/`unknown`), and
    - the forecast's directional probability agrees with that trend
      (`p_up_24h > 0.5` for `trend_up`, `< 0.5` for `trend_down`), and
    - a swing-based invalidation level exists on the correct side of
      `last_close`, and
    - the forecast quantile on the favorable side clears `last_close`
      (i.e. there is room for a target at all), and
    - the resulting reward:risk ratio is `>= min_rr`.

    Any failure of the above returns `None` — never a low-conviction card.
    """
    if regime.label == "trend_up" and forecast.p_up_24h > 0.5:
        entry = last_close
        invalidation = _nearest_support_below(levels, entry)
        target = forecast.q90
        if invalidation is None or target <= entry:
            return None
        risk = entry - invalidation
        reward = target - entry
        if risk <= 0:
            return None
        rr = reward / risk
        if rr < min_rr:
            return None
        return SetupCard(
            direction="long",
            entry=entry,
            invalidation=invalidation,
            target=target,
            rr=rr,
            risk_pct=risk_pct,
        )

    if regime.label == "trend_down" and forecast.p_up_24h < 0.5:
        entry = last_close
        invalidation = _nearest_resistance_above(levels, entry)
        target = forecast.q10
        if invalidation is None or target >= entry:
            return None
        risk = invalidation - entry
        reward = entry - target
        if risk <= 0:
            return None
        rr = reward / risk
        if rr < min_rr:
            return None
        return SetupCard(
            direction="short",
            entry=entry,
            invalidation=invalidation,
            target=target,
            rr=rr,
            risk_pct=risk_pct,
        )

    return None

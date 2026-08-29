"""Deterministic price-level identification.

Every level returned carries which of `LevelKind` it is and the exact
price/window that produced it in `Level.reason` — a level with no
traceable origin is a bug in this module, not an acceptable omission (per
the brief). No LLM, no forecast dependency: only closed historical bars in,
`Level` DTOs out.
"""

from __future__ import annotations

import math
from datetime import date

from kmd.data.base import Bar
from kmd.dto import Level

SWING_WINDOW = 3  # bars on each side that must be lower/higher for a swing point
SWING_LOOKBACK_BARS = 120  # only look for swings within this many recent closed bars
MAX_SWINGS_PER_SIDE = 3
MA_PERIODS = (20, 50, 100)
MA_CLUSTER_REL_TOLERANCE = 0.005  # 0.5%: MAs within this of each other form one cluster


def _closed_ascending(bars: list[Bar]) -> list[Bar]:
    closed = [b for b in bars if b.is_closed]
    return sorted(closed, key=lambda b: b.ts_utc)


def swing_highs_lows(bars: list[Bar]) -> list[Level]:
    """Local extrema over a `SWING_WINDOW`-bar neighborhood, restricted to
    the most recent `SWING_LOOKBACK_BARS` closed bars and capped at
    `MAX_SWINGS_PER_SIDE` per side (most recent first) to keep the level
    list focused on what is still price-relevant.
    """
    closed = _closed_ascending(bars)[-SWING_LOOKBACK_BARS:]
    levels: list[Level] = []
    highs: list[Level] = []
    lows: list[Level] = []

    for i in range(SWING_WINDOW, len(closed) - SWING_WINDOW):
        window = closed[i - SWING_WINDOW : i + SWING_WINDOW + 1]
        candidate = closed[i]
        neighborhood_highs = [b.high for b in window]
        neighborhood_lows = [b.low for b in window]

        if candidate.high == max(neighborhood_highs) and neighborhood_highs.count(candidate.high) == 1:
            highs.append(
                Level(
                    price=candidate.high,
                    kind="swing_high",
                    reason=(
                        f"swing high {candidate.high:g} at {candidate.ts_utc.isoformat()}, "
                        f"local max of highs over +/-{SWING_WINDOW} bars"
                    ),
                )
            )
        if candidate.low == min(neighborhood_lows) and neighborhood_lows.count(candidate.low) == 1:
            lows.append(
                Level(
                    price=candidate.low,
                    kind="swing_low",
                    reason=(
                        f"swing low {candidate.low:g} at {candidate.ts_utc.isoformat()}, "
                        f"local min of lows over +/-{SWING_WINDOW} bars"
                    ),
                )
            )

    levels.extend(highs[-MAX_SWINGS_PER_SIDE:])
    levels.extend(lows[-MAX_SWINGS_PER_SIDE:])
    return levels


def previous_day_high_low(bars: list[Bar]) -> list[Level]:
    """PDH/PDL computed from the most recent fully-elapsed UTC calendar day
    present in the closed bars (i.e. the calendar day immediately before
    the day of the latest closed bar).
    """
    closed = _closed_ascending(bars)
    if not closed:
        return []

    last_day: date = closed[-1].ts_utc.date()
    prev_day_bars = [b for b in closed if b.ts_utc.date() < last_day]
    if not prev_day_bars:
        return []

    target_day = max(b.ts_utc.date() for b in prev_day_bars)
    day_bars = [b for b in prev_day_bars if b.ts_utc.date() == target_day]
    pdh = max(b.high for b in day_bars)
    pdl = min(b.low for b in day_bars)

    return [
        Level(
            price=pdh,
            kind="pdh",
            reason=f"previous UTC day ({target_day.isoformat()}) high {pdh:g} over {len(day_bars)} bars",
        ),
        Level(
            price=pdl,
            kind="pdl",
            reason=f"previous UTC day ({target_day.isoformat()}) low {pdl:g} over {len(day_bars)} bars",
        ),
    ]


def _sma(closes: list[float], period: int) -> float | None:
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def ma_clusters(bars: list[Bar]) -> list[Level]:
    """Moving-average clusters: whenever two or more of `MA_PERIODS`'
    simple moving averages sit within `MA_CLUSTER_REL_TOLERANCE` of each
    other, emit one `ma_cluster` level at their average, naming exactly
    which MAs and values formed it.
    """
    closed = _closed_ascending(bars)
    closes = [b.close for b in closed]

    mas: dict[int, float] = {}
    for period in MA_PERIODS:
        value = _sma(closes, period)
        if value is not None:
            mas[period] = value

    if len(mas) < 2:
        return []

    periods = sorted(mas)
    used: set[int] = set()
    levels: list[Level] = []
    for i, p1 in enumerate(periods):
        if p1 in used:
            continue
        group = [p1]
        for p2 in periods[i + 1 :]:
            if p2 in used:
                continue
            if abs(mas[p1] - mas[p2]) / mas[p1] <= MA_CLUSTER_REL_TOLERANCE:
                group.append(p2)
        if len(group) >= 2:
            used.update(group)
            cluster_price = sum(mas[p] for p in group) / len(group)
            detail = ", ".join(f"SMA{p}={mas[p]:g}" for p in group)
            levels.append(
                Level(
                    price=cluster_price,
                    kind="ma_cluster",
                    reason=f"{detail} within {MA_CLUSTER_REL_TOLERANCE * 100:.1f}% of each other",
                )
            )
    return levels


def round_number_step(price: float) -> float:
    """Step size for nearby round numbers: one order of magnitude below
    the price's own leading digit (price ~50000 -> step 1000; price ~1.08
    -> step 0.01).
    """
    if price <= 0:
        raise ValueError("price must be positive")
    exponent = math.floor(math.log10(price))
    return float(10 ** (exponent - 1))


def round_numbers(price: float, decimals: int) -> list[Level]:
    """The nearest round number at or below, and directly above, the
    current price, at the step from `round_number_step`.
    """
    step = round_number_step(price)
    below = math.floor(price / step) * step
    above = below + step
    return [
        Level(
            price=round(below, decimals),
            kind="round_number",
            reason=f"round number at step {step:g} below current price {price:g}",
        ),
        Level(
            price=round(above, decimals),
            kind="round_number",
            reason=f"round number at step {step:g} above current price {price:g}",
        ),
    ]


def compute_levels(bars: list[Bar], current_price: float, decimals: int) -> list[Level]:
    """All levels for one instrument, combined. Order: swings, PDH/PDL, MA
    clusters, round numbers.
    """
    levels: list[Level] = []
    levels.extend(swing_highs_lows(bars))
    levels.extend(previous_day_high_low(bars))
    levels.extend(ma_clusters(bars))
    levels.extend(round_numbers(current_price, decimals))
    return levels

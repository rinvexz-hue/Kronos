"""Deterministic trend/range and volatility regime classification.

No LLM, no stochastic sampling, no dependency on `forecast/` — this module
only ever looks at closed historical bars and produces a machine-derived
label plus a human-readable `reason` string carrying the actual numbers
that drove the classification (never a bare label with no justification).
"""

from __future__ import annotations

from kmd.data.base import Bar
from kmd.dto import Regime, RegimeLabel, VolRegime

EMA_FAST_PERIOD = 20
EMA_SLOW_PERIOD = 50
ATR_PERIOD = 14
ATR_HISTORY_WINDOW = 100  # bars of ATR history used for the percentile rank
VOL_LOW_PCTILE = 33.0
VOL_HIGH_PCTILE = 66.0
# Minimum relative EMA separation to call a trend rather than "range noise".
TREND_SEPARATION_THRESHOLD = 0.001  # 0.1% of price


class InsufficientBarsError(ValueError):
    """Raised when there are not enough closed bars to classify a regime."""


def _closed_ascending(bars: list[Bar]) -> list[Bar]:
    closed = [b for b in bars if b.is_closed]
    return sorted(closed, key=lambda b: b.ts_utc)


def ema(values: list[float], period: int) -> list[float]:
    """Standard exponential moving average, seeded with a simple average of
    the first `period` values. Returns a series the same length as
    `values`, with the first `period - 1` entries equal to that seed (no
    NaN padding, since callers only ever read the tail).
    """
    if len(values) < period:
        raise InsufficientBarsError(f"need at least {period} values for EMA{period}")
    alpha = 2.0 / (period + 1)
    seed = sum(values[:period]) / period
    result = [seed] * period
    current = seed
    for value in values[period:]:
        current = alpha * value + (1 - alpha) * current
        result.append(current)
    return result


def true_ranges(bars: list[Bar]) -> list[float]:
    """True range per bar (bar 0 uses high-low only, no prior close)."""
    ranges: list[float] = []
    prev_close: float | None = None
    for bar in bars:
        if prev_close is None:
            tr = bar.high - bar.low
        else:
            tr = max(
                bar.high - bar.low,
                abs(bar.high - prev_close),
                abs(bar.low - prev_close),
            )
        ranges.append(tr)
        prev_close = bar.close
    return ranges


def atr_series(bars: list[Bar], period: int) -> list[float]:
    """Wilder-style ATR (simple-average seed, then smoothed), same length
    convention as `ema`."""
    tr = true_ranges(bars)
    if len(tr) < period:
        raise InsufficientBarsError(f"need at least {period} bars for ATR{period}")
    seed = sum(tr[:period]) / period
    result = [seed] * period
    current = seed
    for value in tr[period:]:
        current = (current * (period - 1) + value) / period
        result.append(current)
    return result


def _percentile_rank(value: float, population: list[float]) -> float:
    if not population:
        raise InsufficientBarsError("empty ATR history for percentile rank")
    below_or_equal = sum(1 for v in population if v <= value)
    return 100.0 * below_or_equal / len(population)


def classify_trend(bars: list[Bar]) -> tuple[RegimeLabel, str]:
    """EMA-structure trend/range classification: compares a fast and slow
    EMA's current level and separation. A trend requires the fast EMA to
    be meaningfully separated from (not just marginally above/below) the
    slow EMA, otherwise it is called `range` to avoid flip-flopping on
    noise.
    """
    closed = _closed_ascending(bars)
    if len(closed) < EMA_SLOW_PERIOD:
        return "unknown", f"only {len(closed)} closed bars available, need >= {EMA_SLOW_PERIOD}"

    closes = [b.close for b in closed]
    fast = ema(closes, EMA_FAST_PERIOD)[-1]
    slow = ema(closes, EMA_SLOW_PERIOD)[-1]
    separation = (fast - slow) / slow

    if separation > TREND_SEPARATION_THRESHOLD:
        return (
            "trend_up",
            f"EMA{EMA_FAST_PERIOD}={fast:.6g} > EMA{EMA_SLOW_PERIOD}={slow:.6g} "
            f"(+{separation * 100:.2f}%, threshold {TREND_SEPARATION_THRESHOLD * 100:.2f}%)",
        )
    if separation < -TREND_SEPARATION_THRESHOLD:
        return (
            "trend_down",
            f"EMA{EMA_FAST_PERIOD}={fast:.6g} < EMA{EMA_SLOW_PERIOD}={slow:.6g} "
            f"({separation * 100:.2f}%, threshold -{TREND_SEPARATION_THRESHOLD * 100:.2f}%)",
        )
    return (
        "range",
        f"EMA{EMA_FAST_PERIOD}={fast:.6g} vs EMA{EMA_SLOW_PERIOD}={slow:.6g} "
        f"separation {separation * 100:.2f}% within +/-{TREND_SEPARATION_THRESHOLD * 100:.2f}% band",
    )


def classify_volatility(bars: list[Bar]) -> tuple[VolRegime, str]:
    """ATR-percentile volatility regime: current ATR14 ranked against its
    own trailing history (up to `ATR_HISTORY_WINDOW` bars of ATR values).
    """
    closed = _closed_ascending(bars)
    min_needed = ATR_PERIOD + 1  # at least one ATR value beyond the seed
    if len(closed) < min_needed:
        return "normal", f"only {len(closed)} closed bars available, need >= {min_needed}"

    atr = atr_series(closed, ATR_PERIOD)
    history = atr[-ATR_HISTORY_WINDOW:]
    current = atr[-1]
    pct = _percentile_rank(current, history)

    if pct <= VOL_LOW_PCTILE:
        return "low", f"ATR{ATR_PERIOD}={current:.6g} at {pct:.0f}th pctile of last {len(history)} bars"
    if pct >= VOL_HIGH_PCTILE:
        return "high", f"ATR{ATR_PERIOD}={current:.6g} at {pct:.0f}th pctile of last {len(history)} bars"
    return "normal", f"ATR{ATR_PERIOD}={current:.6g} at {pct:.0f}th pctile of last {len(history)} bars"


def compute_regime(bars: list[Bar]) -> Regime:
    """Combine trend and volatility classification into the `Regime` DTO."""
    label, trend_reason = classify_trend(bars)
    vol_regime, vol_reason = classify_volatility(bars)
    return Regime(label=label, vol_regime=vol_regime, reason=f"{trend_reason}; {vol_reason}")

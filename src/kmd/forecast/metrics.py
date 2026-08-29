"""Pure functions turning N Monte Carlo close-price paths into the
probabilistic metrics the dashboard shows. Every function here is
deterministic given its inputs — no randomness, no I/O, no model calls —
which is what makes them testable with hand-constructed path sets where
the correct answer is known in advance.

All metrics operate on CLOSE price only. Kronos returns full OHLCV per
path, but every metric specified for this dashboard (`p_up_24h`,
`q10/q50/q90`, `p_vol_expansion`, `band_width_pct`) is defined purely in
terms of the close-price series, so that is the only series carried
through the cache and into these functions.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np


class EmptyPathsError(ValueError):
    """Raised when metrics are requested on zero paths — there is no
    meaningful distribution to summarize."""


def _validate(paths_close: Sequence[Sequence[float]]) -> np.ndarray:
    if len(paths_close) == 0:
        raise EmptyPathsError("at least one Monte Carlo path is required")
    arr = np.asarray(paths_close, dtype=np.float64)
    if arr.ndim != 2:
        raise ValueError("paths_close must be a 2D (n_paths x pred_len) sequence")
    return arr


def p_up_24h(paths_close: Sequence[Sequence[float]], last_close: float) -> float:
    """Fraction of paths whose FINAL predicted close is above `last_close`
    (the last known closed-bar close). Despite the "24h" name this is
    horizon-agnostic — it always refers to the model's `pred_len`-bar-ahead
    horizon, whatever timeframe that maps to (24 bars on 1h == 24h).
    """
    arr = _validate(paths_close)
    final_closes = arr[:, -1]
    return float(np.mean(final_closes > last_close))


def horizon_quantiles(paths_close: Sequence[Sequence[float]]) -> tuple[float, float, float]:
    """(q10, q50, q90) of the FINAL predicted close across all paths."""
    arr = _validate(paths_close)
    final_closes = arr[:, -1]
    q10, q50, q90 = np.percentile(final_closes, [10, 50, 90])
    return float(q10), float(q50), float(q90)


def band_width_pct(q10: float, q50: float, q90: float) -> float:
    """(q90 - q10) / q50 — the width of the 10-90 band relative to the
    median forecast, expressed as a fraction (0.05 == 5%).
    """
    if q50 == 0:
        raise ValueError("q50 must be non-zero to compute a relative band width")
    return (q90 - q10) / q50


def realized_volatility(closes: Sequence[float]) -> float:
    """Population standard deviation of consecutive log returns over the
    given close series. `closes` must have at least 2 points (>=1 return).
    """
    arr = np.asarray(closes, dtype=np.float64)
    if arr.shape[0] < 2:
        raise ValueError("need at least 2 closes to compute a return")
    log_returns = np.diff(np.log(arr))
    return float(np.std(log_returns, ddof=0))


def historical_realized_vol(recent_closes: Sequence[float], window: int) -> float:
    """Recent historical realized volatility: log-return std over the
    trailing `window` bars of ALREADY-CLOSED history (i.e. `recent_closes`
    must be actual historical closes, most recent last, at least
    `window + 1` long so there are `window` returns).
    """
    arr = np.asarray(recent_closes, dtype=np.float64)
    if arr.shape[0] < window + 1:
        raise ValueError(
            f"need at least {window + 1} historical closes for a {window}-bar realized vol"
        )
    return realized_volatility(arr[-(window + 1) :].tolist())


def p_vol_expansion(
    paths_close: Sequence[Sequence[float]],
    last_close: float,
    recent_historical_vol: float,
) -> float:
    """Fraction of paths whose realized volatility OVER THE FORECAST
    HORIZON (log-return std across `[last_close] + path`) exceeds
    `recent_historical_vol` (typically `historical_realized_vol` computed
    over the trailing `pred_len` already-closed bars — "recent historical"
    is defined as that trailing window, passed in by the caller so this
    function stays pure).
    """
    arr = _validate(paths_close)
    n_paths: int = int(arr.shape[0])
    expansions = 0
    for i in range(n_paths):
        path_closes = np.concatenate(([last_close], arr[i])).tolist()
        path_vol = realized_volatility(path_closes)
        if path_vol > recent_historical_vol:
            expansions += 1
    return expansions / n_paths

"""Hand-constructed known-answer tests for `kmd.forecast.metrics`. Every
expected value here is computed independently of the module under test
(either literal arithmetic or a value chosen so the computation has no
ambiguity, e.g. no interpolation in the quantile test).
"""

from __future__ import annotations

import math

import pytest

from kmd.forecast import metrics


def test_p_up_24h_known_fraction() -> None:
    # final closes: 3, 1, 4 vs last_close=2 -> up, down, up -> 2/3
    paths = [[1, 2, 3], [1, 2, 1], [1, 2, 4]]
    assert metrics.p_up_24h(paths, last_close=2.0) == pytest.approx(2 / 3)


def test_p_up_24h_all_up() -> None:
    paths = [[1, 2, 3], [1, 2, 3.5]]
    assert metrics.p_up_24h(paths, last_close=1.0) == 1.0


def test_p_up_24h_none_up() -> None:
    paths = [[1, 0.5], [1, 0.9]]
    assert metrics.p_up_24h(paths, last_close=1.0) == 0.0


def test_p_up_24h_rejects_empty_paths() -> None:
    with pytest.raises(metrics.EmptyPathsError):
        metrics.p_up_24h([], last_close=100.0)


def test_horizon_quantiles_exact_on_evenly_spaced_data() -> None:
    # 11 final closes, evenly spaced 100..200 step 10, chosen so the 10th/
    # 50th/90th percentile land exactly on data points (no interpolation
    # ambiguity to second-guess).
    paths = [[v] for v in range(100, 201, 10)]
    q10, q50, q90 = metrics.horizon_quantiles(paths)
    assert (q10, q50, q90) == (110.0, 150.0, 190.0)


def test_band_width_pct_known_value() -> None:
    assert metrics.band_width_pct(110.0, 150.0, 190.0) == pytest.approx(80.0 / 150.0)


def test_band_width_pct_rejects_zero_median() -> None:
    with pytest.raises(ValueError, match="non-zero"):
        metrics.band_width_pct(-10.0, 0.0, 10.0)


def test_realized_volatility_zero_for_constant_growth_rate() -> None:
    # 100 -> 110 -> 121 is two identical 10% steps: log returns are equal,
    # so the population std of the returns is exactly 0.
    assert metrics.realized_volatility([100.0, 110.0, 121.0]) == pytest.approx(0.0, abs=1e-12)


def test_realized_volatility_known_value_for_symmetric_up_down() -> None:
    # 100 -> 110 -> 100: returns are +ln(1.1) and -ln(1.1); population std
    # of [a, -a] is |a|.
    expected = math.log(1.1)
    assert metrics.realized_volatility([100.0, 110.0, 100.0]) == pytest.approx(expected)


def test_realized_volatility_requires_at_least_two_points() -> None:
    with pytest.raises(ValueError, match="at least 2"):
        metrics.realized_volatility([100.0])


def test_historical_realized_vol_uses_trailing_window_only() -> None:
    # Only the last window+1=3 closes should matter; a very different
    # older prefix must not change the result.
    noisy_prefix = [1.0, 1000.0, 0.001, 5.0]
    tail = [100.0, 110.0, 100.0]
    expected = metrics.realized_volatility(tail)
    assert metrics.historical_realized_vol(noisy_prefix + tail, window=2) == pytest.approx(expected)


def test_historical_realized_vol_requires_enough_history() -> None:
    with pytest.raises(ValueError, match="at least"):
        metrics.historical_realized_vol([100.0, 110.0], window=5)


def test_p_vol_expansion_known_fraction() -> None:
    """Three hand-picked paths from last_close=100:
    - A = [100, 100]: zero realized vol.
    - B = [105, 110]: tiny realized vol (~0.00114).
    - C = [130, 70]: large realized vol (~0.4407).
    A threshold of 0.05 sits strictly between B and C's vol, so exactly
    1 of 3 paths (C) should count as a volatility expansion.
    """
    paths = [[100.0, 100.0], [105.0, 110.0], [130.0, 70.0]]
    result = metrics.p_vol_expansion(paths, last_close=100.0, recent_historical_vol=0.05)
    assert result == pytest.approx(1 / 3)


def test_p_vol_expansion_zero_when_all_paths_calmer_than_history() -> None:
    paths = [[100.0, 101.0], [100.0, 99.0]]
    result = metrics.p_vol_expansion(paths, last_close=100.0, recent_historical_vol=10.0)
    assert result == 0.0


def test_p_vol_expansion_one_when_all_paths_wilder_than_history() -> None:
    paths = [[200.0, 50.0], [300.0, 20.0]]
    result = metrics.p_vol_expansion(paths, last_close=100.0, recent_historical_vol=0.0001)
    assert result == 1.0

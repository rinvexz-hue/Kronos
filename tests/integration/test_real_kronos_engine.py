"""The ONE integration test allowed to load real Kronos weights (see the
brief: "reserve at most one clearly-marked, skippable integration test").

Excluded from the default run via the `network` marker (see
`pyproject.toml`'s `addopts`, `-m "not network"`). Run explicitly with:

    pytest -m network tests/integration/test_real_kronos_engine.py -v -s

This downloads `NeoQuasar/Kronos-small` + its tokenizer from Hugging Face
on first run (no HF token required, but real network access is) and
prints the measured wall-clock time for one `run_monte_carlo` call at the
project's default parameters (lookback=400, pred_len=24, n_paths=30) on
CPU — this is the number `DECISIONS.md`'s performance-budget entry should
be updated with once someone runs this in an environment with HF access
(this sandboxed session's outbound network policy blocks huggingface.co,
confirmed via a 403 policy denial rather than a transient failure, so the
number in DECISIONS.md as written is a documented estimate, not a
real measurement — see that entry for the full explanation).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest

from kmd.config import Settings
from kmd.data.base import Timeframe
from kmd.forecast.engine import load_predictor, run_monte_carlo
from tests.support import make_bar

pytestmark = pytest.mark.network


def test_real_kronos_small_single_symbol_timing() -> None:
    settings = Settings()
    predictor = load_predictor(settings)

    base_ts = datetime(2024, 1, 1, tzinfo=UTC)
    bars = [
        make_bar(
            symbol="BTC/USDT",
            timeframe=Timeframe.H1,
            ts_utc=base_ts + timedelta(hours=i),
            open_=100.0 + (i % 7),
            high=101.0 + (i % 7),
            low=99.0 + (i % 7),
            close=100.0 + (i % 7),
            is_closed=True,
        )
        for i in range(settings.lookback_bars)
    ]

    start = time.monotonic()
    result = run_monte_carlo(
        predictor,
        "BTC/USDT",
        Timeframe.H1,
        bars,
        lookback_bars=settings.lookback_bars,
        pred_len=settings.pred_len,
        n_paths=settings.mc_paths,
        temperature=settings.temperature,
        top_p=settings.top_p,
        top_k=settings.top_k,
        seed=settings.seed,
        model_name=settings.model_name,
    )
    elapsed = time.monotonic() - start

    print(
        f"\n[real Kronos timing] 1 symbol, lookback={settings.lookback_bars}, "
        f"pred_len={settings.pred_len}, n_paths={settings.mc_paths}: {elapsed:.1f}s "
        f"(6 symbols sequentially would be ~{elapsed * 6:.1f}s against the 90s budget)"
    )

    assert len(result.paths) == settings.mc_paths
    final_closes = [float(p["close"].iloc[-1]) for p in result.paths]
    assert len(set(final_closes)) > 1  # genuinely distinct paths, not one repeated

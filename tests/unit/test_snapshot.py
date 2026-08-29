"""End-to-end `build_snapshot` tests, entirely against fakes (no real
Kronos weights, no real data-layer adapters). Covers: full assembly from
fakes, clean pydantic JSON round-trip, per-instrument failure isolation,
and that `context`-group instruments never appear as forecast tiles.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from kmd.calibration.logger import CalibrationLogger
from kmd.config import Settings
from kmd.data.base import Timeframe
from kmd.data.markets_config import (
    CalibrationSpec,
    DataSource,
    ForecastSpec,
    GroupSpec,
    InstrumentSpec,
    MarketsConfig,
    RiskSpec,
    SessionSpec,
    TimeframesSpec,
)
from kmd.forecast.cache import ForecastCache
from kmd.snapshot import SnapshotDTO, build_snapshot
from tests.support import FakeMarketStore, FakePredictor, make_bar

BASE_TS = datetime(2026, 1, 1, tzinfo=UTC)
LOOKBACK = 30
PRED_LEN = 4


def _markets_config(*, include_context: bool = True, include_thin_symbol: bool = True) -> MarketsConfig:
    crypto_instruments = [
        InstrumentSpec(
            display_symbol="BTC/USDT",
            decimals=2,
            source=DataSource.CCXT,
            source_symbol="BTC/USDT",
            exchange="binance",
        ),
    ]
    if include_thin_symbol:
        crypto_instruments.append(
            InstrumentSpec(
                display_symbol="XRP/USDT",
                decimals=4,
                source=DataSource.CCXT,
                source_symbol="XRP/USDT",
                exchange="binance",
            )
        )

    groups = {
        "crypto": GroupSpec(session="crypto", instruments=crypto_instruments),
    }
    sessions = {"crypto": SessionSpec(always_open=True)}
    if include_context:
        groups["context"] = GroupSpec(
            session="index",
            instruments=[
                InstrumentSpec(
                    display_symbol="DXY",
                    decimals=2,
                    source=DataSource.YFINANCE,
                    source_symbol="DX-Y.NYB",
                ),
            ],
        )
        sessions["index"] = SessionSpec(
            always_open=False, weekday_open="mon 13:30", weekday_close="mon 20:00", timezone="UTC"
        )

    return MarketsConfig(
        timeframes=TimeframesSpec(primary=Timeframe.H1, secondary=[Timeframe.H4, Timeframe.D1]),
        forecast=ForecastSpec(lookback_bars=LOOKBACK, pred_len=PRED_LEN),
        sessions=sessions,
        groups=groups,
        risk=RiskSpec(min_rr_for_setup=2.0, default_risk_pct=2.0),
        calibration=CalibrationSpec(min_observations_for_display=30, target_band_coverage=0.8),
    )


def _closed_bars(symbol: str, n: int, *, start_price: float = 100.0):  # type: ignore[no-untyped-def]
    return [
        make_bar(
            symbol=symbol,
            timeframe=Timeframe.H1,
            ts_utc=BASE_TS + timedelta(hours=i),
            open_=start_price + i,
            high=start_price + i + 1,
            low=start_price + i - 1,
            close=start_price + i,
            is_closed=True,
        )
        for i in range(n)
    ]


def _build(tmp_path: Path, store, markets_config: MarketsConfig) -> SnapshotDTO:  # type: ignore[no-untyped-def]
    forecast_cache = ForecastCache(tmp_path / "forecast.sqlite3")
    calibration_logger = CalibrationLogger(tmp_path / "forecast.sqlite3")
    return build_snapshot(
        store=store,
        markets_config=markets_config,
        settings=Settings(mc_paths=5),
        predictor=FakePredictor(),
        forecast_cache=forecast_cache,
        calibration_logger=calibration_logger,
        timeframe=Timeframe.H1,
        now=BASE_TS + timedelta(hours=LOOKBACK + 1),
    )


def test_build_snapshot_end_to_end_from_fakes(tmp_path: Path) -> None:
    store = FakeMarketStore()
    store.set_bars("BTC/USDT", Timeframe.H1, _closed_bars("BTC/USDT", LOOKBACK + 5))
    store.set_bars("XRP/USDT", Timeframe.H1, _closed_bars("XRP/USDT", LOOKBACK + 5, start_price=0.5))

    dto = _build(tmp_path, store, _markets_config())

    symbols = {a.display_symbol for a in dto.assets}
    assert symbols == {"BTC/USDT", "XRP/USDT"}  # DXY (context) excluded
    assert dto.correlation_id

    btc = next(a for a in dto.assets if a.display_symbol == "BTC/USDT")
    assert btc.forecast.n_paths == 5
    assert btc.forecast.model_name
    assert 0.0 <= btc.forecast.p_up_24h <= 1.0
    assert btc.calibration.sufficient_data is False  # no scored history yet
    assert len(btc.sparkline) > 0
    assert all(lvl.reason for lvl in btc.levels)


def test_snapshot_round_trips_through_pydantic_json(tmp_path: Path) -> None:
    store = FakeMarketStore()
    store.set_bars("BTC/USDT", Timeframe.H1, _closed_bars("BTC/USDT", LOOKBACK + 5))
    store.set_bars("XRP/USDT", Timeframe.H1, _closed_bars("XRP/USDT", LOOKBACK + 5, start_price=0.5))

    dto = _build(tmp_path, store, _markets_config())

    raw_json = dto.model_dump_json()
    round_tripped = SnapshotDTO.model_validate_json(raw_json)
    assert round_tripped == dto


def test_build_snapshot_skips_instrument_with_insufficient_history(tmp_path: Path) -> None:
    store = FakeMarketStore()
    store.set_bars("BTC/USDT", Timeframe.H1, _closed_bars("BTC/USDT", LOOKBACK + 5))
    # XRP has almost no history -> must be skipped, not crash the whole run.
    store.set_bars("XRP/USDT", Timeframe.H1, _closed_bars("XRP/USDT", 2, start_price=0.5))

    dto = _build(tmp_path, store, _markets_config())

    symbols = {a.display_symbol for a in dto.assets}
    assert symbols == {"BTC/USDT"}


def test_build_snapshot_excludes_context_group_even_with_full_history(tmp_path: Path) -> None:
    store = FakeMarketStore()
    store.set_bars("BTC/USDT", Timeframe.H1, _closed_bars("BTC/USDT", LOOKBACK + 5))
    store.set_bars("DXY", Timeframe.H1, _closed_bars("DXY", LOOKBACK + 5, start_price=100.0))

    dto = _build(tmp_path, store, _markets_config(include_thin_symbol=False))

    symbols = {a.display_symbol for a in dto.assets}
    assert symbols == {"BTC/USDT"}


class _NanForOneSymbolPredictor:
    """A `PredictorProtocol` double that produces NaN close paths for the
    FIRST instrument `build_snapshot` forecasts (BTC/USDT, per
    `_markets_config`'s instrument order) and ordinary finite paths for
    every instrument after that (XRP/USDT) - simulating a genuinely
    unstable model output (or a poisoned input) affecting exactly one
    instrument, independent of any upstream data-layer NaN guard.
    """

    def __init__(self) -> None:
        self.calls = 0

    def predict_batch(self, df_list, x_timestamp_list, y_timestamp_list, pred_len, **kwargs):  # type: ignore[no-untyped-def]
        import numpy as np
        import pandas as pd

        poisoned = self.calls == 0
        self.calls += 1

        results = []
        for df, y_timestamp in zip(df_list, y_timestamp_list, strict=True):
            last_close = float(df["close"].iloc[-1])
            closes = np.full(pred_len, float("nan")) if poisoned else np.full(pred_len, last_close)
            results.append(
                pd.DataFrame(
                    {
                        "open": closes,
                        "high": closes,
                        "low": closes,
                        "close": closes,
                        "volume": np.zeros(pred_len),
                        "amount": np.zeros(pred_len),
                    },
                    index=pd.Index(y_timestamp),
                )
            )
        return results


def test_build_snapshot_isolates_one_instrument_whose_forecast_comes_back_nan(
    tmp_path: Path,
) -> None:
    """Red-team Round 2 (fault injection): a NaN forecast for one
    instrument (BTC/USDT here) must not corrupt or block the snapshot for
    every other instrument (XRP/USDT) - `ForecastMetrics.must_be_finite`
    raises inside `_get_or_compute_forecast`, which `build_snapshot`'s
    existing per-instrument `try/except` catches exactly like an
    `InsufficientDataError`. Also proves the resulting `SnapshotDTO`
    (containing only the healthy instrument) round-trips through pydantic
    JSON cleanly - the corrupted instrument never reaches the DTO at all,
    rather than reaching it and breaking the whole document.
    """
    store = FakeMarketStore()
    store.set_bars("BTC/USDT", Timeframe.H1, _closed_bars("BTC/USDT", LOOKBACK + 5))
    store.set_bars("XRP/USDT", Timeframe.H1, _closed_bars("XRP/USDT", LOOKBACK + 5, start_price=0.5))

    forecast_cache = ForecastCache(tmp_path / "forecast.sqlite3")
    calibration_logger = CalibrationLogger(tmp_path / "forecast.sqlite3")
    dto = build_snapshot(
        store=store,
        markets_config=_markets_config(),
        settings=Settings(mc_paths=5),
        predictor=_NanForOneSymbolPredictor(),
        forecast_cache=forecast_cache,
        calibration_logger=calibration_logger,
        timeframe=Timeframe.H1,
        now=BASE_TS + timedelta(hours=LOOKBACK + 1),
    )

    symbols = {a.display_symbol for a in dto.assets}
    assert symbols == {"XRP/USDT"}  # BTC/USDT (NaN forecast) skipped, not crashed

    raw_json = dto.model_dump_json()
    round_tripped = SnapshotDTO.model_validate_json(raw_json)
    assert round_tripped == dto

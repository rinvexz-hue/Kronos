"""Tests for `markets_config.py`, both against the real
`config/markets.yaml` (so a schema/data drift there is caught) and against
small hand-built YAML fixtures (to exercise validation failure paths
without depending on the real file's exact contents).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from kmd.config import MARKETS_CONFIG_PATH
from kmd.data.base import Timeframe
from kmd.data.markets_config import (
    DataSource,
    MarketsConfig,
    get_markets_config,
    load_markets_config,
)

_MINIMAL_VALID: dict[str, object] = {
    "timeframes": {"primary": "1h", "secondary": ["4h", "1d"]},
    "forecast": {"lookback_bars": 400, "pred_len": 24},
    "sessions": {
        "crypto": {"always_open": True},
        "fx": {
            "always_open": False,
            "weekday_open": "sun 22:00",
            "weekday_close": "fri 22:00",
            "timezone": "UTC",
        },
    },
    "groups": {
        "crypto": {
            "session": "crypto",
            "instruments": [
                {
                    "display_symbol": "BTC/USDT",
                    "decimals": 2,
                    "source": "ccxt",
                    "exchange": "binance",
                    "source_symbol": "BTC/USDT",
                    "fallback_exchange": "coinbase",
                    "fallback_source_symbol": "BTC/USD",
                }
            ],
        },
        "fx": {
            "session": "fx",
            "instruments": [
                {
                    "display_symbol": "EUR/USD",
                    "decimals": 5,
                    "source": "yfinance",
                    "source_symbol": "EURUSD=X",
                }
            ],
        },
    },
    "risk": {"min_rr_for_setup": 2.0, "default_risk_pct": 2.0},
    "calibration": {"min_observations_for_display": 30, "target_band_coverage": 0.8},
}


def test_real_markets_yaml_loads_and_validates() -> None:
    config = load_markets_config()
    assert config.timeframes.primary == Timeframe.H1
    assert Timeframe.H4 in config.timeframes.secondary
    instruments = config.all_instruments()
    assert len(instruments) >= 6
    symbols = {i.display_symbol for i in instruments}
    assert {"BTC/USDT", "XRP/USDT", "GOUD", "ZILVER", "EUR/USD", "USD/JPY"} <= symbols


def test_real_markets_yaml_path_matches_config_constant() -> None:
    assert MARKETS_CONFIG_PATH.exists()


def test_get_markets_config_is_cached_singleton() -> None:
    assert get_markets_config() is get_markets_config()


def test_all_instruments_denormalizes_group_and_session() -> None:
    config = MarketsConfig.model_validate(_MINIMAL_VALID)
    btc = config.get_instrument("BTC/USDT")
    assert btc.group == "crypto"
    assert btc.session_name == "crypto"
    assert btc.symbol == "BTC/USDT"
    assert btc.source is DataSource.CCXT
    assert btc.exchange == "binance"
    assert btc.fallback_exchange == "coinbase"
    assert btc.fallback_source_symbol == "BTC/USD"

    eurusd = config.get_instrument("EUR/USD")
    assert eurusd.group == "fx"
    assert eurusd.session_name == "fx"
    assert eurusd.source is DataSource.YFINANCE
    assert eurusd.exchange is None


def test_get_instrument_missing_symbol_raises_key_error() -> None:
    config = MarketsConfig.model_validate(_MINIMAL_VALID)
    with pytest.raises(KeyError):
        config.get_instrument("DOES/NOTEXIST")


def test_ccxt_instrument_missing_exchange_is_rejected() -> None:
    bad = yaml.safe_load(yaml.safe_dump(_MINIMAL_VALID))
    del bad["groups"]["crypto"]["instruments"][0]["exchange"]
    with pytest.raises(ValidationError, match="require `exchange`"):
        MarketsConfig.model_validate(bad)


def test_yfinance_instrument_with_exchange_field_is_rejected() -> None:
    bad = yaml.safe_load(yaml.safe_dump(_MINIMAL_VALID))
    bad["groups"]["fx"]["instruments"][0]["exchange"] = "binance"
    with pytest.raises(ValidationError, match="only apply to ccxt"):
        MarketsConfig.model_validate(bad)


def test_group_referencing_unknown_session_is_rejected() -> None:
    bad = yaml.safe_load(yaml.safe_dump(_MINIMAL_VALID))
    bad["groups"]["crypto"]["session"] = "no_such_session"
    with pytest.raises(ValidationError, match="unknown session"):
        MarketsConfig.model_validate(bad)


def test_non_always_open_session_missing_hours_is_rejected() -> None:
    bad = yaml.safe_load(yaml.safe_dump(_MINIMAL_VALID))
    bad["sessions"]["fx"] = {"always_open": False}
    with pytest.raises(ValidationError, match="missing"):
        MarketsConfig.model_validate(bad)


def test_load_markets_config_accepts_explicit_path(tmp_path: Path) -> None:
    custom = tmp_path / "markets.yaml"
    custom.write_text(yaml.safe_dump(_MINIMAL_VALID))
    config = load_markets_config(custom)
    assert config.get_instrument("BTC/USDT").decimals == 2

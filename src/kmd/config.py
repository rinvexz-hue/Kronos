"""Application settings, validated at startup.

All configuration is read from environment variables (see .env.example)
via pydantic-settings, plus the static instrument definitions in
config/markets.yaml. Nothing in here should ever hold a trade- or
withdraw-capable credential — this system is read-only by design.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
KRONOS_VENDOR_ROOT = REPO_ROOT / "third_party" / "kronos"
MARKETS_CONFIG_PATH = REPO_ROOT / "config" / "markets.yaml"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KMD_", env_file=".env", extra="forbid")

    model_name: str = "NeoQuasar/Kronos-small"
    tokenizer_name: str = "NeoQuasar/Kronos-Tokenizer-base"
    model_max_context: int = 512
    device: Literal["cpu", "cuda", "mps"] = "cpu"

    lookback_bars: int = 400
    pred_len: int = 24
    mc_paths: int = Field(default=30, ge=1)
    temperature: float = 1.0
    top_p: float = 0.9
    top_k: int = 0
    seed: int = 1337

    ccxt_exchange: str = "binance"
    ccxt_api_key: str = ""
    ccxt_api_secret: str = ""

    db_path: Path = Path("./data/kmd.sqlite3")

    host: str = "127.0.0.1"
    port: int = 8000

    refresh_timeframes: str = "1h,4h,1d"

    enable_llm_summary: bool = False
    llm_api_key: str = ""

    @property
    def refresh_timeframe_list(self) -> list[str]:
        return [tf.strip() for tf in self.refresh_timeframes.split(",") if tf.strip()]

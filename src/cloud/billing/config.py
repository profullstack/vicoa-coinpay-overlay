"""Overlay configuration.

Read from the same ``.env`` / process environment Vicoa's own ``Settings`` uses,
so a self-hoster adds a few ``COINPAY_*`` lines next to their existing config.
Nothing here is read at import time; call :func:`get_config` when needed so the
alembic ``migrate`` service can import the models without a full environment.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Chains CoinPay can quote. Order here is the order the picker shows them.
KNOWN_BLOCKCHAINS: dict[str, str] = {
    "USDC_BASE": "USDC on Base",
    "USDC_POL": "USDC on Polygon",
    "USDC_ETH": "USDC on Ethereum",
    "USDC_SOL": "USDC on Solana",
    "ETH": "Ether (Ethereum)",
    "POL": "POL (Polygon)",
    "SOL": "Solana",
    "BTC": "Bitcoin",
    "BCH": "Bitcoin Cash",
}


class CoinPayConfig(BaseSettings):
    # --- CoinPay merchant credentials -------------------------------------
    coinpay_base_url: str = "https://coinpayportal.com"
    # ``cp_live_…`` / ``cp_test_…`` merchant API key. The OAuth ``cp_…`` client
    # id looks similar and cannot create payments.
    coinpay_api_key: str = ""
    coinpay_business_id: str = ""
    # Plaintext webhook signing secret for the business (``t=…,v1=…`` HMAC).
    coinpay_webhook_secret: str = ""
    # Signs checkout links. Falls back to the webhook secret when empty.
    coinpay_checkout_secret: str = ""

    # --- What is for sale --------------------------------------------------
    # Comma-separated chains offered on the checkout page.
    coinpay_blockchains: str = "USDC_BASE,USDC_POL,USDC_ETH,ETH,BTC,SOL"
    # List prices. Vicoa's web UI hard-codes $12 / $108 in its pricing cards;
    # change those too if you change these.
    coinpay_pro_monthly_usd: float = 12.0
    coinpay_pro_annual_usd: float = 108.0
    coinpay_monthly_days: int = 30
    coinpay_annual_days: int = 365
    coinpay_pro_agent_limit: int = -1

    # --- Free plan ---------------------------------------------------------
    # User messages a free account may send per calendar month through the
    # web / mobile API. 0 disables the gate (Pro then only changes the label).
    coinpay_free_message_limit: int = 20
    coinpay_free_agent_limit: int = 10

    # --- Operations --------------------------------------------------------
    # Seconds between sweeper runs (lapse expired passes, poll pending payments).
    coinpay_sweep_interval: int = 60
    # Create the overlay's columns / table at startup (idempotent DDL).
    coinpay_auto_schema: bool = True
    # Minutes a checkout link stays valid.
    coinpay_intent_ttl_minutes: int = 30

    # Vicoa's own public URLs (already in a self-hoster's .env).
    public_backend_url: str = "http://localhost:8000"
    public_web_url: str = "http://localhost:3000"

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    @field_validator("coinpay_blockchains")
    @classmethod
    def _upper_chains(cls, v: str) -> str:
        return ",".join(p.strip().upper() for p in v.split(",") if p.strip())

    @property
    def blockchains(self) -> list[str]:
        return [c for c in self.coinpay_blockchains.split(",") if c]

    @property
    def checkout_secret(self) -> str:
        return self.coinpay_checkout_secret or self.coinpay_webhook_secret

    @property
    def checkout_enabled(self) -> bool:
        return bool(
            self.coinpay_api_key
            and self.coinpay_business_id
            and self.checkout_secret
            and self.blockchains
        )

    def chain_label(self, code: str) -> str:
        return KNOWN_BLOCKCHAINS.get(code, code)


@lru_cache(maxsize=1)
def get_config() -> CoinPayConfig:
    return CoinPayConfig()


def reset_config_cache() -> None:
    """Tests change the environment between cases."""
    get_config.cache_clear()

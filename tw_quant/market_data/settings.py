from __future__ import annotations

from dataclasses import dataclass
import os


def normalize_provider(value: str) -> str:
    provider = value.lower().strip()
    if provider == "mock":
        return "replay"
    return provider


@dataclass(frozen=True)
class MarketDataSettings:
    """Configuration owned by the market-data subsystem only."""

    provider: str = "replay"
    symbol: str = "TMF"
    contract: str = "TMFR1"
    replay_csv: str = ""
    replay_speed: float = 8.0
    history_days: int = 30
    history_limit: int = 50_000
    shioaji_api_key: str | None = None
    shioaji_secret_key: str | None = None
    shioaji_production: bool = False

    @classmethod
    def from_env(cls) -> "MarketDataSettings":
        # MARKET_MODE remains a compatibility fallback for existing servers.
        provider = os.getenv("MARKET_DATA_PROVIDER")
        if provider is None:
            provider = os.getenv("MARKET_MODE", "mock")
        environment = os.getenv("PLATFORM_ENVIRONMENT", "production").lower().strip()
        api_key = os.getenv("MARKET_SJ_API_KEY")
        secret_key = os.getenv("MARKET_SJ_SECRET_KEY")
        # Legacy names remain local/test-only. Production must never make live
        # execution secret names available to the public application process.
        if environment != "production":
            api_key = api_key or os.getenv("SJ_API_KEY")
            secret_key = secret_key or os.getenv("SJ_SEC_KEY")
        return cls(
            provider=normalize_provider(provider),
            symbol=os.getenv("MARKET_SYMBOL", "TMF").upper(),
            contract=os.getenv("MARKET_CONTRACT", "TMFR1").upper(),
            replay_csv=os.getenv("MARKET_REPLAY_CSV", ""),
            replay_speed=float(os.getenv("MARKET_REPLAY_SPEED", "8")),
            history_days=int(os.getenv("MARKET_HISTORY_DAYS", "30")),
            history_limit=int(os.getenv("MARKET_HISTORY_LIMIT", "50000")),
            shioaji_api_key=api_key,
            shioaji_secret_key=secret_key,
            shioaji_production=os.getenv(
                "MARKET_SJ_PRODUCTION", os.getenv("SJ_PRODUCTION", "false")
            ).lower()
            in {"1", "true", "yes"},
        )

    def validate(self) -> None:
        if self.provider not in {"replay", "shioaji"}:
            raise ValueError(
                "MARKET_DATA_PROVIDER must be replay or shioaji"
            )
        if self.replay_speed <= 0:
            raise ValueError("MARKET_REPLAY_SPEED must be positive")
        if not 1 <= self.history_days <= 30:
            raise ValueError("MARKET_HISTORY_DAYS must be between 1 and 30")
        if not 1 <= self.history_limit <= 50_000:
            raise ValueError("MARKET_HISTORY_LIMIT must be between 1 and 50000")
        if self.provider == "shioaji" and not (
            self.shioaji_api_key and self.shioaji_secret_key
        ):
            raise ValueError(
                "MARKET_SJ_API_KEY and MARKET_SJ_SECRET_KEY are required for Shioaji"
            )

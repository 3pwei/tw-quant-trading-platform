from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Literal, Mapping

from .identity import BrokerAccountRef


def _enabled(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class BrokerSettings:
    provider: Literal["disabled", "shioaji"] = "disabled"
    live_trading_enabled: bool = False
    account_id: str = ""
    confirmation: str = ""
    allowed_account_ids: frozenset[str] = frozenset()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "BrokerSettings":
        values = os.environ if env is None else env
        provider = values.get("BROKER_PROVIDER", "disabled").strip().lower()
        if provider not in {"disabled", "shioaji"}:
            raise ValueError(f"unsupported BROKER_PROVIDER: {provider}")
        allowed = frozenset(
            item.strip()
            for item in values.get("LIVE_ALLOWED_ACCOUNT_IDS", "").split(",")
            if item.strip()
        )
        return cls(
            provider=provider,  # type: ignore[arg-type]
            live_trading_enabled=_enabled(values.get("LIVE_TRADING_ENABLED")),
            account_id=values.get("LIVE_BROKER_ACCOUNT_ID", "").strip(),
            confirmation=values.get("LIVE_TRADING_CONFIRMATION", "").strip(),
            allowed_account_ids=allowed,
        )

    def to_connection(self) -> "BrokerConnectionSettings":
        """Normalize the legacy single-connection environment contract."""

        return BrokerConnectionSettings(
            connection_id="primary",
            broker_name=self.provider,
            account_id=self.account_id,
            enabled=self.live_trading_enabled,
            secret_ref="environment:primary",
        )


@dataclass(frozen=True)
class BrokerConnectionSettings:
    """Broker-neutral, non-secret configuration for an execution connection."""

    connection_id: str
    broker_name: str
    account_id: str
    enabled: bool = False
    secret_ref: str = "environment:primary"

    def __post_init__(self) -> None:
        connection_id = self.connection_id.strip()
        broker_name = self.broker_name.strip().lower()
        account_id = self.account_id.strip()
        secret_ref = self.secret_ref.strip()
        if not connection_id:
            raise ValueError("connection_id is required")
        if not broker_name:
            raise ValueError("broker_name is required")
        if not secret_ref:
            raise ValueError("secret_ref is required")
        object.__setattr__(self, "connection_id", connection_id)
        object.__setattr__(self, "broker_name", broker_name)
        object.__setattr__(self, "account_id", account_id)
        object.__setattr__(self, "secret_ref", secret_ref)

    @property
    def account_ref(self) -> BrokerAccountRef | None:
        if not self.account_id:
            return None
        return BrokerAccountRef(self.broker_name, self.account_id)

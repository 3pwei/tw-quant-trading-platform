from __future__ import annotations

from typing import Callable

from .disabled import DisabledBroker
from .ports import BrokerPort
from tw_quant_core.broker.registry import BrokerRegistration
from .settings import BrokerSettings
from .shioaji import LiveTradingSafety, ShioajiBrokerAdapter, ShioajiExecutionClient


BrokerAdapterBuilder = Callable[[object], BrokerRegistration]


class BrokerAdapterFactory:
    """Adapter construction registry kept outside execution application logic."""

    def __init__(self) -> None:
        self._builders: dict[str, BrokerAdapterBuilder] = {}

    def register(self, broker_name: str, builder: BrokerAdapterBuilder) -> None:
        name = broker_name.strip().lower()
        if not name:
            raise ValueError("broker_name is required")
        if name in self._builders:
            raise ValueError(f"duplicate broker adapter builder: {name}")
        self._builders[name] = builder

    def build(self, broker_name: str, context: object) -> BrokerRegistration:
        name = broker_name.strip().lower()
        try:
            builder = self._builders[name]
        except KeyError as exc:
            raise ValueError(f"unsupported broker adapter: {name}") from exc
        registration = builder(context)
        if registration.account_ref.broker_name != name:
            raise ValueError("adapter builder returned a different broker identity")
        return registration


def build_broker(
    settings: BrokerSettings,
    *,
    shioaji_client: ShioajiExecutionClient | None = None,
) -> BrokerPort:
    """Build the execution adapter; disabled is the unconditional default."""

    if settings.provider == "disabled" or not settings.live_trading_enabled:
        return DisabledBroker()
    if shioaji_client is None:
        raise ValueError("enabled Shioaji execution requires a client")
    return ShioajiBrokerAdapter(
        shioaji_client,
        LiveTradingSafety(
            account_id=settings.account_id,
            enabled=settings.live_trading_enabled,
            confirmation=settings.confirmation,
            allowed_account_ids=settings.allowed_account_ids,
        ),
    )

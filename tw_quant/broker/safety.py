from __future__ import annotations

from dataclasses import dataclass

from .identity import BrokerAccountRef


LIVE_TRADING_CONFIRMATION = "I_UNDERSTAND_LIVE_ORDERS"


@dataclass(frozen=True)
class BrokerAccountSafety:
    """Broker-neutral admission policy for one execution identity."""

    account: BrokerAccountRef
    enabled: bool = False
    confirmation: str = ""
    allowed_accounts: frozenset[BrokerAccountRef] = frozenset()

    def assert_ordering_allowed(self) -> None:
        if not self.enabled:
            raise RuntimeError("live order execution is disabled")
        if self.confirmation != LIVE_TRADING_CONFIRMATION:
            raise RuntimeError("live trading confirmation is invalid")
        if self.account not in self.allowed_accounts:
            raise RuntimeError("live broker account is not allowlisted")

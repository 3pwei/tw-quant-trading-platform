from __future__ import annotations

from dataclasses import dataclass

from ..broker.identity import BrokerAccountRef
from .redaction import mask_account


@dataclass(frozen=True)
class BrokerConnectionHealth:
    connection_id: str
    broker_name: str
    account: BrokerAccountRef | None
    execution_state: str
    enabled: bool
    locked: bool
    recovery_status: str
    connected: bool = False
    ca_ready: bool = False
    read_only: bool = False
    callback_registered: bool = False
    last_broker_read_time: str | None = None
    last_callback_time: str | None = None

    def to_public_dict(self) -> dict[str, object]:
        return {
            "broker_name": self.broker_name,
            "masked_account_id": mask_account(
                self.account.account_id if self.account is not None else None
            ),
            "execution_state": self.execution_state,
            "enabled": self.enabled,
            "locked": self.locked,
            "recovery_status": self.recovery_status,
            "connected": self.connected,
            "ca_ready": self.ca_ready,
            "read_only": self.read_only,
            "callback_registered": self.callback_registered,
            "last_broker_read_time": self.last_broker_read_time,
            "last_callback_time": self.last_callback_time,
        }


@dataclass(frozen=True)
class ExecutionServiceHealth:
    """Collection model ready for multiple isolated connections in a later PR."""

    connections: tuple[BrokerConnectionHealth, ...]

    def to_public_dict(self) -> dict[str, object]:
        return {
            "broker_accounts": [
                connection.to_public_dict() for connection in self.connections
            ]
        }

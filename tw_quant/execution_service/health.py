from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from uuid import uuid4

from ..broker.identity import BrokerAccountRef
from .redaction import mask_account


GENERATION_PATTERN = re.compile(r"[0-9a-f]{32}")


def valid_generation(value: object) -> bool:
    return isinstance(value, str) and GENERATION_PATTERN.fullmatch(value) is not None


def new_generation() -> str:
    return uuid4().hex


def write_generation_marker(path: str, generation: str | None = None) -> str:
    """Invalidate the old marker, then atomically publish the new generation."""
    current = generation if generation is not None else new_generation()
    if not valid_generation(current):
        raise ValueError("invalid execution generation")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    target.unlink(missing_ok=True)
    temporary.write_text(current + "\n", encoding="utf-8")
    temporary.replace(target)
    return current


def read_generation_marker(path: str) -> str | None:
    try:
        value = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value if valid_generation(value) else None


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

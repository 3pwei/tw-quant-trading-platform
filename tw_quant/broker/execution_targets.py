from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import re
from typing import Mapping

from .identity import BrokerAccountRef


_TARGET_ID = re.compile(r"^exec_[A-Za-z0-9]{16,64}$")


class ExecutionTargetStatus(str, Enum):
    ACTIVE = "active"
    DISABLED = "disabled"
    LOCKED = "locked"


def mask_account_id(account_id: str) -> str:
    account = account_id.strip()
    if not account:
        raise ValueError("account_id is required")
    return "****" + account[-4:]


@dataclass(frozen=True, repr=False)
class ExecutionTarget:
    """Owned routing metadata. Credentials and execution state live elsewhere."""

    target_id: str
    owner_user_id: str
    broker_name: str
    account_id: str = field(repr=False)
    masked_account_id: str
    secret_ref: str = field(repr=False)
    status: ExecutionTargetStatus = ExecutionTargetStatus.ACTIVE
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    connection_id: str | None = None
    display_name: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        target_id = self.target_id.strip()
        owner = self.owner_user_id.strip()
        broker = self.broker_name.strip().lower()
        account = self.account_id.strip()
        secret_ref = self.secret_ref.strip()
        if not _TARGET_ID.fullmatch(target_id):
            raise ValueError("target_id must be an opaque exec_ identifier")
        if not owner:
            raise ValueError("owner_user_id is required")
        if not broker:
            raise ValueError("broker_name is required")
        if not account:
            raise ValueError("account_id is required")
        if not secret_ref:
            raise ValueError("secret_ref is required")
        if self.masked_account_id != mask_account_id(account):
            raise ValueError("masked_account_id does not match account_id")
        for value in (self.created_at, self.updated_at):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("execution target timestamps must be timezone-aware")
        object.__setattr__(self, "target_id", target_id)
        object.__setattr__(self, "owner_user_id", owner)
        object.__setattr__(self, "broker_name", broker)
        object.__setattr__(self, "account_id", account)
        object.__setattr__(self, "secret_ref", secret_ref)
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def account_ref(self) -> BrokerAccountRef:
        return BrokerAccountRef(self.broker_name, self.account_id)

    def to_public_dict(self) -> dict[str, str]:
        return {
            "target_id": self.target_id,
            "provider_label": "configured",
            "masked_account_id": self.masked_account_id,
            "status": self.status.value,
        }

    def __repr__(self) -> str:
        return (
            "ExecutionTarget("
            f"target_id={self.target_id!r}, owner_user_id={self.owner_user_id!r}, "
            f"broker_name={self.broker_name!r}, "
            f"masked_account_id={self.masked_account_id!r}, status={self.status.value!r})"
        )

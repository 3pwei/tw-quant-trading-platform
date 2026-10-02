from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Role(str, Enum):
    RESEARCHER = "researcher"
    TRADER = "trader"
    ADMIN = "admin"


class AccountStatus(str, Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    REVOKED = "revoked"


class AccessRequestStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class TradingMode(str, Enum):
    DISABLED = "disabled"
    PAPER = "paper"
    LIVE = "live"


@dataclass(frozen=True)
class AuthUser:
    user_id: str
    email: str
    role: Role
    status: AccountStatus
    trading_mode: TradingMode
    permissions: tuple[str, ...]
    access_subject: str | None = None
    registered: bool = True

    def to_message(self, authorization_enforced: bool) -> dict[str, object]:
        return {
            "user_id": self.user_id,
            "email": self.email,
            "role": self.role.value,
            "status": self.status.value,
            "trading_mode": self.trading_mode.value,
            "permissions": list(self.permissions),
            "registered": self.registered,
            "identity_bound": self.access_subject is not None,
            "authorization_enforced": authorization_enforced,
        }


@dataclass(frozen=True)
class AccessRequest:
    request_id: str
    access_subject: str
    email: str
    status: AccessRequestStatus
    requested_at: str
    updated_at: str
    resolved_at: str | None = None
    resolved_by_user_id: str | None = None

    def to_message(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "email": self.email,
            "status": self.status.value,
            "requested_at": self.requested_at,
            "updated_at": self.updated_at,
            "resolved_at": self.resolved_at,
            "resolved_by_user_id": self.resolved_by_user_id,
        }

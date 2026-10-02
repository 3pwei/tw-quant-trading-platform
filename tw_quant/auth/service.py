from __future__ import annotations

from .access import AccessIdentity
from .models import AccountStatus, AuthUser, Role, TradingMode
from .permissions import ROLE_PERMISSIONS
from .repository import SQLiteAuthRepository


class AuthorizationError(ValueError):
    """Identity is valid, but the platform account cannot be used."""


class AuthService:
    def __init__(
        self,
        repository: SQLiteAuthRepository,
        *,
        authorization_mode: str = "disabled",
    ) -> None:
        self.repository = repository
        self.authorization_mode = authorization_mode

    @property
    def enforced(self) -> bool:
        return self.authorization_mode == "enforced"

    def identify(self, identity: AccessIdentity) -> AuthUser:
        user = self.repository.resolve_identity(identity)
        if user is None:
            if self.enforced:
                raise AuthorizationError("platform account is not registered")
            return AuthUser(
                user_id=identity.subject,
                access_subject=identity.subject,
                email=identity.email or "",
                role=Role.RESEARCHER,
                status=AccountStatus.ACTIVE,
                trading_mode=TradingMode.DISABLED,
                permissions=(),
                registered=False,
            )
        if user.status is not AccountStatus.ACTIVE and self.enforced:
            raise AuthorizationError(f"platform account is {user.status.value}")
        return user

    def local_development_user(self) -> AuthUser:
        return AuthUser(
            user_id="local-development",
            access_subject="access-disabled",
            email="local@development.invalid",
            role=Role.ADMIN,
            status=AccountStatus.ACTIVE,
            trading_mode=TradingMode.DISABLED,
            permissions=tuple(sorted(ROLE_PERMISSIONS[Role.ADMIN])),
            registered=False,
        )

    def require_permission(self, user: AuthUser, permission: str) -> None:
        """Reject access unless the resolved platform account has permission.

        Authorization is deliberately bypassed only while the rollout switch is
        disabled.  Once enforced, an active, registered account and an explicit
        role permission are both required.
        """
        if not self.enforced:
            return
        if not user.registered:
            raise AuthorizationError("platform account is not registered")
        if user.status is not AccountStatus.ACTIVE:
            raise AuthorizationError(f"platform account is {user.status.value}")
        if permission not in user.permissions:
            raise AuthorizationError(f"permission denied: {permission}")

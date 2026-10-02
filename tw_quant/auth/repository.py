from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from uuid import uuid4

from .access import AccessIdentity
from .models import (
    AccessRequest,
    AccessRequestStatus,
    AccountStatus,
    AuthUser,
    Role,
    TradingMode,
)
from .permissions import PERMISSIONS, ROLE_PERMISSIONS


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def normalize_email(email: str) -> str:
    return email.strip().casefold()


def validate_email(email: str) -> str:
    normalized = normalize_email(email)
    if not normalized or "@" not in normalized or len(normalized) > 254:
        raise ValueError("a valid email of at most 254 characters is required")
    return normalized


def validate_trading_mode(role: Role, trading_mode: TradingMode) -> None:
    if role is Role.RESEARCHER and trading_mode is not TradingMode.DISABLED:
        raise ValueError("researcher accounts cannot enable trading modes")
    if role is Role.ADMIN and trading_mode is TradingMode.LIVE:
        raise ValueError("admin accounts cannot enable live trading")


class SQLiteAuthRepository:
    """Application identities stored beside market data in SQLite.

    The interface is intentionally isolated from ``SQLiteBarRepository`` so a
    future PostgreSQL identity store does not affect market-data persistence.
    """

    def __init__(self, path: str | Path):
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(target, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.lock = Lock()
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self._migrate()

    def _migrate(self) -> None:
        with self.lock:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS auth_schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS app_users (
                    user_id TEXT PRIMARY KEY,
                    access_subject TEXT UNIQUE,
                    email TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    role TEXT NOT NULL CHECK(role IN ('researcher','trader','admin')),
                    status TEXT NOT NULL CHECK(status IN ('active','suspended','revoked')),
                    trading_mode TEXT NOT NULL
                        CHECK(trading_mode IN ('disabled','paper','live')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_seen_at TEXT
                );

                CREATE TABLE IF NOT EXISTS permissions (
                    permission_key TEXT PRIMARY KEY,
                    description TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS role_permissions (
                    role TEXT NOT NULL,
                    permission_key TEXT NOT NULL,
                    PRIMARY KEY(role, permission_key),
                    FOREIGN KEY(permission_key) REFERENCES permissions(permission_key)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS audit_events (
                    event_id TEXT PRIMARY KEY,
                    actor_user_id TEXT,
                    subject_user_id TEXT,
                    action TEXT NOT NULL,
                    resource_type TEXT NOT NULL,
                    resource_id TEXT,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(actor_user_id) REFERENCES app_users(user_id)
                        ON DELETE SET NULL,
                    FOREIGN KEY(subject_user_id) REFERENCES app_users(user_id)
                        ON DELETE SET NULL
                );

                CREATE INDEX IF NOT EXISTS idx_audit_events_created
                    ON audit_events(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_audit_events_actor
                    ON audit_events(actor_user_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS app_user_requests (
                    request_id TEXT PRIMARY KEY,
                    access_subject TEXT NOT NULL,
                    email TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    status TEXT NOT NULL
                        CHECK(status IN ('pending','approved','rejected')),
                    requested_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    resolved_at TEXT,
                    resolved_by_user_id TEXT,
                    FOREIGN KEY(resolved_by_user_id) REFERENCES app_users(user_id)
                        ON DELETE SET NULL
                );

                CREATE INDEX IF NOT EXISTS idx_app_user_requests_status
                    ON app_user_requests(status, requested_at DESC);
                """
            )
            self.connection.execute(
                "INSERT OR IGNORE INTO auth_schema_migrations VALUES (1, ?)",
                (_now(),),
            )
            self.connection.execute(
                "INSERT OR IGNORE INTO auth_schema_migrations VALUES (2, ?)",
                (_now(),),
            )
            self.connection.executemany(
                "INSERT OR IGNORE INTO permissions VALUES (?, ?)",
                PERMISSIONS.items(),
            )
            self.connection.executemany(
                "INSERT OR IGNORE INTO role_permissions VALUES (?, ?)",
                [
                    (role.value, permission)
                    for role, permissions in ROLE_PERMISSIONS.items()
                    for permission in permissions
                ],
            )
            self.connection.commit()

    def bootstrap_admins(self, emails: tuple[str, ...]) -> None:
        now = _now()
        with self.lock:
            for raw_email in emails:
                email = normalize_email(raw_email)
                if not email:
                    continue
                existing = self.connection.execute(
                    "SELECT role FROM app_users WHERE email=?", (email,)
                ).fetchone()
                if existing and existing["role"] != Role.ADMIN.value:
                    raise ValueError(
                        "bootstrap admin email is registered as a non-admin"
                    )
                self.connection.execute(
                    """
                    INSERT OR IGNORE INTO app_users(
                        user_id, access_subject, email, role, status,
                        trading_mode, created_at, updated_at, last_seen_at
                    ) VALUES (?, NULL, ?, 'admin', 'active', 'disabled', ?, ?, NULL)
                    """,
                    (str(uuid4()), email, now, now),
                )
            self.connection.commit()

    def create_user(
        self,
        email: str,
        *,
        role: Role = Role.RESEARCHER,
        status: AccountStatus = AccountStatus.ACTIVE,
        trading_mode: TradingMode = TradingMode.DISABLED,
        actor_user_id: str | None = None,
    ) -> AuthUser:
        normalized = validate_email(email)
        validate_trading_mode(role, trading_mode)
        user_id = str(uuid4())
        now = _now()
        approved_request_id = None
        with self.lock:
            try:
                self.connection.execute(
                    """
                    INSERT INTO app_users(
                        user_id, access_subject, email, role, status,
                        trading_mode, created_at, updated_at, last_seen_at
                    ) VALUES (?, NULL, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        user_id,
                        normalized,
                        role.value,
                        status.value,
                        trading_mode.value,
                        now,
                        now,
                    ),
                )
                request = self.connection.execute(
                    "SELECT request_id FROM app_user_requests "
                    "WHERE email=? AND status='pending'",
                    (normalized,),
                ).fetchone()
                if request is not None:
                    approved_request_id = request["request_id"]
                    self.connection.execute(
                        "UPDATE app_user_requests SET status='approved', "
                        "updated_at=?, resolved_at=?, resolved_by_user_id=? "
                        "WHERE request_id=?",
                        (now, now, actor_user_id, approved_request_id),
                    )
                self.connection.commit()
            except sqlite3.IntegrityError as exc:
                raise ValueError("email is already registered") from exc
        self.append_audit_event(
            "user.created",
            "user",
            actor_user_id=actor_user_id,
            subject_user_id=user_id,
            resource_id=user_id,
            details={
                "email": normalized,
                "role": role.value,
                "status": status.value,
                "trading_mode": trading_mode.value,
            },
        )
        if approved_request_id is not None:
            self.append_audit_event(
                "access_request.approved",
                "access_request",
                actor_user_id=actor_user_id,
                resource_id=approved_request_id,
                details={"email": normalized, "source": "manual_user_creation"},
            )
        user = self.user_by_email(normalized)
        if user is None:  # pragma: no cover - guarded by the successful insert
            raise RuntimeError("created user could not be loaded")
        return user

    def update_user(
        self,
        user_id: str,
        *,
        role: Role,
        status: AccountStatus,
        trading_mode: TradingMode,
        actor_user_id: str | None = None,
    ) -> AuthUser:
        validate_trading_mode(role, trading_mode)
        with self.lock:
            existing = self.connection.execute(
                "SELECT * FROM app_users WHERE user_id=?", (user_id,)
            ).fetchone()
            if existing is None:
                raise ValueError("platform user was not found")
            self.connection.execute(
                "UPDATE app_users SET role=?, status=?, trading_mode=?, "
                "updated_at=? WHERE user_id=?",
                (role.value, status.value, trading_mode.value, _now(), user_id),
            )
            self.connection.commit()
        self.append_audit_event(
            "user.updated",
            "user",
            actor_user_id=actor_user_id,
            subject_user_id=user_id,
            resource_id=user_id,
            details={
                "role": role.value,
                "status": status.value,
                "trading_mode": trading_mode.value,
            },
        )
        updated = self.user_by_id(user_id)
        if updated is None:  # pragma: no cover - guarded by the update
            raise RuntimeError("updated user could not be loaded")
        return updated

    def resolve_identity(self, identity: AccessIdentity) -> AuthUser | None:
        email = normalize_email(identity.email) if identity.email else None
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM app_users WHERE access_subject=?",
                (identity.subject,),
            ).fetchone()
            if row is None and email:
                candidate = self.connection.execute(
                    "SELECT * FROM app_users WHERE email=?", (email,)
                ).fetchone()
                if candidate and candidate["access_subject"] in (
                    None,
                    identity.subject,
                ):
                    self.connection.execute(
                        "UPDATE app_users SET access_subject=?, last_seen_at=?, "
                        "updated_at=? WHERE user_id=?",
                        (identity.subject, _now(), _now(), candidate["user_id"]),
                    )
                    self.connection.commit()
                    row = self.connection.execute(
                        "SELECT * FROM app_users WHERE user_id=?",
                        (candidate["user_id"],),
                    ).fetchone()
            elif row is not None:
                self.connection.execute(
                    "UPDATE app_users SET last_seen_at=? WHERE user_id=?",
                    (_now(), row["user_id"]),
                )
                self.connection.commit()
        return self._to_user(row) if row else None

    def user_by_email(self, email: str) -> AuthUser | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM app_users WHERE email=?",
                (normalize_email(email),),
            ).fetchone()
        return self._to_user(row) if row else None

    def user_by_id(self, user_id: str) -> AuthUser | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM app_users WHERE user_id=?", (user_id,)
            ).fetchone()
        return self._to_user(row) if row else None

    def users(self) -> list[AuthUser]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM app_users ORDER BY created_at, email"
            ).fetchall()
        return [self._to_user(row) for row in rows]


    def submit_access_request(
        self, identity: AccessIdentity
    ) -> AccessRequest:
        try:
            email = validate_email(identity.email or "")
        except ValueError as exc:
            raise ValueError("a verified email is required") from exc
        if len(identity.subject) > 255:
            raise ValueError("verified identity subject is too long")
        now = _now()
        request_id = str(uuid4())
        with self.lock:
            registered = self.connection.execute(
                "SELECT 1 FROM app_users WHERE email=?", (email,)
            ).fetchone()
            if registered is not None:
                raise ValueError("email is already registered")
            existing = self.connection.execute(
                "SELECT * FROM app_user_requests WHERE email=?", (email,)
            ).fetchone()
            if existing is None:
                self.connection.execute(
                    """
                    INSERT INTO app_user_requests(
                        request_id, access_subject, email, status,
                        requested_at, updated_at, resolved_at,
                        resolved_by_user_id
                    ) VALUES (?, ?, ?, 'pending', ?, ?, NULL, NULL)
                    """,
                    (request_id, identity.subject, email, now, now),
                )
            else:
                request_id = existing["request_id"]
                self.connection.execute(
                    "UPDATE app_user_requests SET access_subject=?, "
                    "status='pending', updated_at=?, resolved_at=NULL, "
                    "resolved_by_user_id=NULL WHERE request_id=?",
                    (identity.subject, now, request_id),
                )
            self.connection.commit()
            row = self.connection.execute(
                "SELECT * FROM app_user_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()
        self.append_audit_event(
            "access_request.submitted",
            "access_request",
            resource_id=request_id,
            details={"email": email},
        )
        return self._to_access_request(row)

    def access_requests(
        self, status: AccessRequestStatus | None = None
    ) -> list[AccessRequest]:
        with self.lock:
            if status is None:
                rows = self.connection.execute(
                    "SELECT * FROM app_user_requests "
                    "ORDER BY requested_at DESC, request_id"
                ).fetchall()
            else:
                rows = self.connection.execute(
                    "SELECT * FROM app_user_requests WHERE status=? "
                    "ORDER BY requested_at DESC, request_id",
                    (status.value,),
                ).fetchall()
        return [self._to_access_request(row) for row in rows]

    def approve_access_request(
        self, request_id: str, *, actor_user_id: str
    ) -> tuple[AccessRequest, AuthUser]:
        now = _now()
        user_id = str(uuid4())
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM app_user_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if row is None:
                raise ValueError("access request was not found")
            if row["status"] != AccessRequestStatus.PENDING.value:
                raise ValueError("access request is no longer pending")
            try:
                self.connection.execute(
                    """
                    INSERT INTO app_users(
                        user_id, access_subject, email, role, status,
                        trading_mode, created_at, updated_at, last_seen_at
                    ) VALUES (?, ?, ?, 'researcher', 'active', 'disabled',
                              ?, ?, ?)
                    """,
                    (
                        user_id,
                        row["access_subject"],
                        row["email"],
                        now,
                        now,
                        now,
                    ),
                )
                self.connection.execute(
                    "UPDATE app_user_requests SET status='approved', "
                    "updated_at=?, resolved_at=?, resolved_by_user_id=? "
                    "WHERE request_id=?",
                    (now, now, actor_user_id, request_id),
                )
                self.connection.commit()
            except sqlite3.IntegrityError as exc:
                self.connection.rollback()
                raise ValueError("email is already registered") from exc
            request_row = self.connection.execute(
                "SELECT * FROM app_user_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()
            user_row = self.connection.execute(
                "SELECT * FROM app_users WHERE user_id=?", (user_id,)
            ).fetchone()
        self.append_audit_event(
            "user.created",
            "user",
            actor_user_id=actor_user_id,
            subject_user_id=user_id,
            resource_id=user_id,
            details={
                "email": row["email"],
                "role": Role.RESEARCHER.value,
                "status": AccountStatus.ACTIVE.value,
                "trading_mode": TradingMode.DISABLED.value,
                "source": "access_request",
            },
        )
        self.append_audit_event(
            "access_request.approved",
            "access_request",
            actor_user_id=actor_user_id,
            subject_user_id=user_id,
            resource_id=request_id,
            details={"email": row["email"]},
        )
        return self._to_access_request(request_row), self._to_user(user_row)

    def reject_access_request(
        self, request_id: str, *, actor_user_id: str
    ) -> AccessRequest:
        now = _now()
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM app_user_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if row is None:
                raise ValueError("access request was not found")
            if row["status"] != AccessRequestStatus.PENDING.value:
                raise ValueError("access request is no longer pending")
            self.connection.execute(
                "UPDATE app_user_requests SET status='rejected', "
                "updated_at=?, resolved_at=?, resolved_by_user_id=? "
                "WHERE request_id=?",
                (now, now, actor_user_id, request_id),
            )
            self.connection.commit()
            updated = self.connection.execute(
                "SELECT * FROM app_user_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()
        self.append_audit_event(
            "access_request.rejected",
            "access_request",
            actor_user_id=actor_user_id,
            resource_id=request_id,
            details={"email": row["email"]},
        )
        return self._to_access_request(updated)

    def permissions_for_role(self, role: Role) -> tuple[str, ...]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT permission_key FROM role_permissions WHERE role=? "
                "ORDER BY permission_key",
                (role.value,),
            ).fetchall()
        return tuple(row["permission_key"] for row in rows)

    def append_audit_event(
        self,
        action: str,
        resource_type: str,
        *,
        actor_user_id: str | None = None,
        subject_user_id: str | None = None,
        resource_id: str | None = None,
        details: dict[str, object] | None = None,
    ) -> str:
        event_id = str(uuid4())
        with self.lock:
            self.connection.execute(
                "INSERT INTO audit_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    actor_user_id,
                    subject_user_id,
                    action,
                    resource_type,
                    resource_id,
                    json.dumps(details or {}, ensure_ascii=False, sort_keys=True),
                    _now(),
                ),
            )
            self.connection.commit()
        return event_id

    def audit_events(self, limit: int = 200) -> list[dict[str, object]]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM (SELECT * FROM audit_events "
                "ORDER BY created_at DESC, event_id DESC LIMIT ?) "
                "ORDER BY created_at, event_id",
                (limit,),
            ).fetchall()
        return [
            {
                **dict(row),
                "details": json.loads(row["details_json"]),
            }
            for row in rows
        ]

    @staticmethod
    def _to_access_request(row: sqlite3.Row) -> AccessRequest:
        return AccessRequest(
            request_id=row["request_id"],
            access_subject=row["access_subject"],
            email=row["email"],
            status=AccessRequestStatus(row["status"]),
            requested_at=row["requested_at"],
            updated_at=row["updated_at"],
            resolved_at=row["resolved_at"],
            resolved_by_user_id=row["resolved_by_user_id"],
        )

    def _to_user(self, row: sqlite3.Row) -> AuthUser:
        role = Role(row["role"])
        return AuthUser(
            user_id=row["user_id"],
            access_subject=row["access_subject"],
            email=row["email"],
            role=role,
            status=AccountStatus(row["status"]),
            trading_mode=TradingMode(row["trading_mode"]),
            permissions=self.permissions_for_role(role),
        )

    def close(self) -> None:
        with self.lock:
            self.connection.close()

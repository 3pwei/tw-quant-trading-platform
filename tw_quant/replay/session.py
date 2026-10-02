from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import RLock
from uuid import uuid4

from ..auth import AccountStatus, AuthUser, Role, TradingMode
from ..market import KBar, TAIPEI
from ..paper import PaperOrderCommand, PaperTradingService, SQLitePaperRepository


class ReplaySessionNotFound(KeyError):
    """The replay session does not exist or belongs to another owner."""


@dataclass
class ReplayTradingSession:
    session_id: str
    snapshot_id: str
    owner: AuthUser
    bars: tuple[KBar, ...]
    root: Path
    created_at: datetime = field(default_factory=lambda: datetime.now(TAIPEI))
    strategy_services: object | None = None
    cursor: int = 0
    generation: int = 0
    lock: RLock = field(default_factory=RLock)

    def __post_init__(self) -> None:
        if not self.bars:
            raise ValueError("replay trading session requires at least one bar")
        self.execution_user = AuthUser(
            user_id=self.owner.user_id,
            email=self.owner.email,
            role=Role.TRADER,
            status=AccountStatus.ACTIVE,
            trading_mode=TradingMode.PAPER,
            permissions=("orders.paper", "positions.read.own"),
            access_subject=self.owner.access_subject,
            registered=self.owner.registered,
        )
        self.service = self._new_service()

    def _new_service(self) -> PaperTradingService:
        path = self.root / f"{self.session_id}-{self.generation}.sqlite3"
        self.database_path = path
        service = PaperTradingService(SQLitePaperRepository(path), strategy_services=self.strategy_services)
        service.on_bar(self.bars[0])
        return service

    def _discard_service(self) -> None:
        self.service.close()
        for suffix in ("", "-wal", "-shm"):
            Path(f"{self.database_path}{suffix}").unlink(missing_ok=True)

    @property
    def owner_id(self) -> str:
        return self.owner.user_id

    def state(self, *, rewound: bool = False) -> dict[str, object]:
        bar = self.bars[self.cursor]
        return {
            "session_id": self.session_id,
            "snapshot_id": self.snapshot_id,
            "mode": "replay",
            "isolated_from_live_paper": True,
            "created_at": self.created_at.isoformat(timespec="milliseconds"),
            "cursor": self.cursor,
            "bar_count": len(self.bars),
            "virtual_time": bar.exchange_time.isoformat(timespec="milliseconds"),
            "current_bar": bar.to_message("connected"),
            "rewound": rewound,
            "account": self.service.account(self.owner_id),
            "positions": self.service.positions(self.owner_id),
            "orders": self.service.orders(self.owner_id),
            "fills": self.service.fills(self.owner_id),
        }

    def seek(self, cursor: int) -> dict[str, object]:
        if cursor < 0 or cursor >= len(self.bars):
            raise ValueError("cursor is outside the replay snapshot")
        with self.lock:
            rewound = cursor < self.cursor
            if rewound:
                self._discard_service()
                self.generation += 1
                self.service = self._new_service()
                self.cursor = 0
            for index in range(self.cursor + 1, cursor + 1):
                self.service.on_bar(self.bars[index])
            self.cursor = cursor
            return self.state(rewound=rewound)

    def submit(
        self, command: PaperOrderCommand, *, idempotency_key: str
    ) -> tuple[dict[str, object], bool, dict[str, object]]:
        with self.lock:
            current = self.bars[self.cursor]
            order, created = self.service.submit(
                self.execution_user,
                command,
                idempotency_key=idempotency_key,
                market_bar=current,
                occurred_at=current.received_time,
            )
            return order, created, self.state()

    def reset(self) -> dict[str, object]:
        with self.lock:
            self._discard_service()
            self.generation += 1
            self.cursor = 0
            self.service = self._new_service()
            return self.state(rewound=True)

    def close(self) -> None:
        with self.lock:
            self._discard_service()


class ReplayTradingSessionRegistry:
    """Bounded, owner-scoped replay accounts isolated from live Paper."""

    def __init__(self, *, max_per_owner: int = 3, max_sessions: int = 100, strategy_services=None):
        self.strategy_services = strategy_services
        self.max_per_owner = max_per_owner
        self.max_sessions = max_sessions
        self._temporary = TemporaryDirectory(prefix="tmf-replay-trading-")
        self._root = Path(self._temporary.name)
        self._sessions: dict[str, ReplayTradingSession] = {}
        self._lock = RLock()

    def create(
        self, snapshot_id: str, owner: AuthUser, bars: list[KBar]
    ) -> ReplayTradingSession:
        with self._lock:
            owned = sorted(
                (item for item in self._sessions.values() if item.owner_id == owner.user_id),
                key=lambda item: item.created_at,
            )
            while len(owned) >= self.max_per_owner:
                expired = owned.pop(0)
                self._sessions.pop(expired.session_id, None)
                expired.close()
            while len(self._sessions) >= self.max_sessions:
                expired = min(self._sessions.values(), key=lambda item: item.created_at)
                self._sessions.pop(expired.session_id, None)
                expired.close()
            session = ReplayTradingSession(
                session_id=uuid4().hex,
                snapshot_id=snapshot_id,
                owner=owner,
                bars=tuple(bars),
                root=self._root,
                strategy_services=self.strategy_services,
            )
            self._sessions[session.session_id] = session
            return session

    def get(self, session_id: str, owner_id: str) -> ReplayTradingSession:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.owner_id != owner_id:
                raise ReplaySessionNotFound(session_id)
            return session

    def close(self) -> None:
        with self._lock:
            for session in self._sessions.values():
                session.close()
            self._sessions.clear()
            self._temporary.cleanup()

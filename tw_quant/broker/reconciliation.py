from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from .truth import BrokerTruthStore

from .manager import LiveOrderManager
from .identity import BrokerAccountRef
from .models import BrokerOrder, BrokerOrderStatus, OrderSide
from .ports import LiveOrderStore
from .recovery import RecoveryLockStore, RecoveryState


@dataclass(frozen=True)
class BrokerOrderSnapshot:
    broker_order_id: str | None
    status: BrokerOrderStatus
    filled_quantity: int

    def __post_init__(self) -> None:
        if self.filled_quantity < 0:
            raise ValueError("order filled_quantity cannot be negative")


@dataclass(frozen=True)
class BrokerFillSnapshot:
    fill_id: str
    broker_order_id: str
    contract: str
    side: OrderSide
    quantity: int
    price: float
    occurred_at: datetime

    def __post_init__(self) -> None:
        if not self.fill_id or not self.broker_order_id or not self.contract:
            raise ValueError("fill identity and contract are required")
        if self.side not in {"buy", "sell"}:
            raise ValueError("fill side must be buy or sell")
        if self.quantity < 1 or self.price <= 0:
            raise ValueError("fill quantity and price must be positive")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("fill occurred_at must be timezone-aware")


@dataclass(frozen=True)
class BrokerPositionSnapshot:
    contract: str
    quantity: int

    def __post_init__(self) -> None:
        if not self.contract:
            raise ValueError("position contract is required")


@dataclass(frozen=True)
class BrokerReconciliationSnapshot:
    broker_name: str
    account_id: str
    captured_at: datetime
    orders: tuple[BrokerOrderSnapshot, ...]
    fills: tuple[BrokerFillSnapshot, ...]
    positions: tuple[BrokerPositionSnapshot, ...]

    def __post_init__(self) -> None:
        if not self.broker_name or not self.account_id:
            raise ValueError("snapshot broker and account are required")
        if self.captured_at.tzinfo is None or self.captured_at.utcoffset() is None:
            raise ValueError("snapshot captured_at must be timezone-aware")

    @property
    def account_ref(self) -> BrokerAccountRef:
        return BrokerAccountRef(self.broker_name, self.account_id)


class BrokerReconciliationSource(Protocol):
    async def reconciliation_snapshot(self) -> BrokerReconciliationSnapshot: ...


@dataclass(frozen=True)
class ReconciliationIssue:
    code: str
    detail: str
    broker_order_id: str | None = None
    contract: str | None = None


@dataclass(frozen=True)
class ReconciliationReport:
    state: RecoveryState
    captured_at: datetime
    local_order_count: int
    broker_order_count: int
    broker_fill_count: int
    broker_position_count: int
    issues: tuple[ReconciliationIssue, ...]


class LiveReconciliationService:
    """Compare order, fill, and position truth before releasing Recovery Lock."""

    def __init__(
        self,
        *,
        account_ref: BrokerAccountRef,
        order_store: LiveOrderStore,
        order_manager: LiveOrderManager,
        source: BrokerReconciliationSource,
        recovery_lock: RecoveryLockStore,
        truth_store: BrokerTruthStore | None = None,
        now: Callable[[], datetime] | None = None,
    ):
        self.account_ref = account_ref
        self.order_store = order_store
        self.order_manager = order_manager
        self.source = source
        self.recovery_lock = recovery_lock
        self.truth_store = truth_store
        self.now = now or (lambda: datetime.now(timezone.utc))

    async def reconcile(self) -> ReconciliationReport:
        attempt = self.recovery_lock.begin(
            self.account_ref.broker_name,
            self.account_ref.account_id,
            updated_at=self.now(),
        )
        local_orders: list[BrokerOrder] = []
        try:
            await self.order_manager.reconcile_broker_orders(self.account_ref)
            local_orders = self.order_store.orders(target=self.account_ref)
            snapshot = await self.source.reconciliation_snapshot()
            if self.truth_store is not None and snapshot.account_ref == self.account_ref:
                self.truth_store.save(self.account_ref, snapshot)
            issues = self._issues(local_orders, snapshot)
            if not issues:
                persist_snapshot = getattr(
                    self.order_store, "apply_reconciled_snapshot", None
                )
                if callable(persist_snapshot):
                    persist_snapshot(self.account_ref, snapshot)
        except Exception as exc:
            captured_at = self.now()
            code = getattr(exc, "code", None)
            issues = [ReconciliationIssue(
                code="reconciliation_failed",
                detail=(
                    str(code)
                    if isinstance(code, str) and code
                    else "broker reconciliation source failed"
                ),
            )]
            state = self.recovery_lock.complete(
                self.account_ref.broker_name,
                self.account_ref.account_id,
                [issue.code for issue in issues],
                expected_generation=attempt.generation,
                updated_at=captured_at,
            )
            return ReconciliationReport(
                state=state,
                captured_at=captured_at,
                local_order_count=len(local_orders),
                broker_order_count=0,
                broker_fill_count=0,
                broker_position_count=0,
                issues=tuple(issues),
            )
        state = self.recovery_lock.complete(
            self.account_ref.broker_name,
            self.account_ref.account_id,
            [issue.code for issue in issues],
            expected_generation=attempt.generation,
            updated_at=self.now(),
        )
        return ReconciliationReport(
            state=state,
            captured_at=snapshot.captured_at,
            local_order_count=len(local_orders),
            broker_order_count=len(snapshot.orders),
            broker_fill_count=len(snapshot.fills),
            broker_position_count=len(snapshot.positions),
            issues=tuple(issues),
        )

    def _issues(
        self,
        local_orders: list[BrokerOrder],
        snapshot: BrokerReconciliationSnapshot,
    ) -> list[ReconciliationIssue]:
        issues: list[ReconciliationIssue] = []
        if snapshot.broker_name != self.account_ref.broker_name:
            issues.append(ReconciliationIssue(
                "broker_mismatch",
                f"expected {self.account_ref.broker_name}, received {snapshot.broker_name}",
            ))
        if snapshot.account_id != self.account_ref.account_id:
            issues.append(ReconciliationIssue(
                "account_mismatch",
                "broker snapshot belongs to a different account",
            ))
        local_by_broker = {
            order.broker_order_id: order
            for order in local_orders
            if order.broker_order_id
        }
        broker_by_id: dict[str, BrokerOrderSnapshot] = {}
        for order in snapshot.orders:
            if not order.broker_order_id:
                issues.append(ReconciliationIssue(
                    "broker_order_without_id",
                    "broker returned an order without a durable ID",
                ))
                continue
            if order.broker_order_id in broker_by_id:
                issues.append(ReconciliationIssue(
                    "duplicate_broker_order",
                    "broker snapshot contains a duplicate order ID",
                    broker_order_id=order.broker_order_id,
                ))
            broker_by_id[order.broker_order_id] = order

        for local in local_orders:
            broker_id = local.broker_order_id
            if local.status is BrokerOrderStatus.UNKNOWN and not broker_id:
                issues.append(ReconciliationIssue(
                    "ambiguous_local_order",
                    "UNKNOWN local order has no broker order ID",
                ))
                continue
            if not broker_id or local.status.terminal:
                continue
            broker = broker_by_id.get(broker_id)
            if broker is None:
                issues.append(ReconciliationIssue(
                    "local_order_missing_at_broker",
                    "nonterminal local order is absent from broker snapshot",
                    broker_order_id=broker_id,
                ))

        for broker_id, broker in broker_by_id.items():
            local = local_by_broker.get(broker_id)
            if local is None:
                issues.append(ReconciliationIssue(
                    "unknown_broker_order",
                    "broker order has no local record",
                    broker_order_id=broker_id,
                ))
                continue
            if local.status is not broker.status:
                issues.append(ReconciliationIssue(
                    "order_status_mismatch",
                    f"local={local.status.value}, broker={broker.status.value}",
                    broker_order_id=broker_id,
                ))
            if local.filled_quantity != broker.filled_quantity:
                issues.append(ReconciliationIssue(
                    "order_fill_quantity_mismatch",
                    "local and broker cumulative fill quantities differ",
                    broker_order_id=broker_id,
                ))

        issues.extend(self._fill_issues(snapshot, broker_by_id))
        issues.extend(self._position_issues(local_orders, snapshot))
        return issues

    @staticmethod
    def _fill_issues(
        snapshot: BrokerReconciliationSnapshot,
        broker_by_id: dict[str, BrokerOrderSnapshot],
    ) -> list[ReconciliationIssue]:
        issues: list[ReconciliationIssue] = []
        seen_fill_ids: set[str] = set()
        quantities: dict[str, int] = {}
        for fill in snapshot.fills:
            if fill.fill_id in seen_fill_ids:
                issues.append(ReconciliationIssue(
                    "duplicate_broker_fill",
                    "broker snapshot contains a duplicate fill ID",
                    broker_order_id=fill.broker_order_id,
                ))
            seen_fill_ids.add(fill.fill_id)
            quantities[fill.broker_order_id] = (
                quantities.get(fill.broker_order_id, 0) + fill.quantity
            )
            if fill.broker_order_id not in broker_by_id:
                issues.append(ReconciliationIssue(
                    "orphan_broker_fill",
                    "broker fill has no matching broker order",
                    broker_order_id=fill.broker_order_id,
                ))
        for broker_id, order in broker_by_id.items():
            if quantities.get(broker_id, 0) != order.filled_quantity:
                issues.append(ReconciliationIssue(
                    "broker_deal_quantity_mismatch",
                    "broker deals do not equal broker cumulative fill quantity",
                    broker_order_id=broker_id,
                ))
        return issues

    @staticmethod
    def _position_issues(
        local_orders: list[BrokerOrder],
        snapshot: BrokerReconciliationSnapshot,
    ) -> list[ReconciliationIssue]:
        local: dict[str, int] = {}
        for order in local_orders:
            sign = 1 if order.request.side == "buy" else -1
            local[order.request.contract] = (
                local.get(order.request.contract, 0)
                + sign * order.filled_quantity
            )
        broker: dict[str, int] = {}
        for position in snapshot.positions:
            broker[position.contract] = (
                broker.get(position.contract, 0) + position.quantity
            )
        issues: list[ReconciliationIssue] = []
        for contract in sorted(set(local) | set(broker)):
            if local.get(contract, 0) != broker.get(contract, 0):
                issues.append(ReconciliationIssue(
                    "position_mismatch",
                    f"local={local.get(contract, 0)}, broker={broker.get(contract, 0)}",
                    contract=contract,
                ))
        return issues

from __future__ import annotations

from datetime import datetime
from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence, runtime_checkable

from .models import BrokerOrder, BrokerOrderRequest
from .identity import BrokerAccountRef
from .routing import RoutedBrokerOrder, RoutedBrokerOrderRequest
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .reconciliation import BrokerReconciliationSnapshot


class BrokerAccount(Protocol):
    """Account/position port. It must not be used as a quote source."""

    broker_name: str

    async def account_state(self) -> Mapping[str, object]: ...

    async def positions(self) -> list[Mapping[str, object]]: ...


@runtime_checkable
class BrokerPort(BrokerAccount, Protocol):
    """Typed trading adapter boundary; it must never supply market quotes."""

    async def submit_order(self, order: BrokerOrderRequest) -> BrokerOrder: ...

    async def cancel_order(self, order: BrokerOrder) -> BrokerOrder: ...

    async def refresh_order(self, order: BrokerOrder) -> BrokerOrder: ...


class OrderExecutor(BrokerPort, Protocol):
    """Backward-compatible name for :class:`BrokerPort`."""


class OrderAdmissionGate(Protocol):
    """Application gate checked before a request is durably reserved."""

    def assert_ordering_allowed(self) -> None: ...


@dataclass(frozen=True)
class CompositeOrderAdmissionGate:
    """A single fail-closed admission point for every live order reservation."""

    gates: tuple[OrderAdmissionGate, ...]

    def __init__(self, gates: Sequence[OrderAdmissionGate]):
        if not gates:
            raise ValueError("at least one live order admission gate is required")
        object.__setattr__(self, "gates", tuple(gates))

    def assert_ordering_allowed(self) -> None:
        for gate in self.gates:
            gate.assert_ordering_allowed()

    def assert_order_allowed(self, routed_order: object) -> None:
        for gate in self.gates:
            method = getattr(gate, "assert_order_allowed", None)
            if callable(method):
                method(routed_order)

    def assert_cancel_allowed(self, routed_order: object) -> None:
        for gate in self.gates:
            method = getattr(gate, "assert_cancel_allowed", None)
            if callable(method):
                method(routed_order)


@dataclass(frozen=True)
class LockedOrderAdmissionGate:
    """Terminal gate used while production broker submission is unavailable."""

    reason: str = "production broker submission is not implemented"

    def assert_ordering_allowed(self) -> None:
        raise RuntimeError(self.reason)


class LiveOrderStore(Protocol):
    """Persistence port used by the order manager and implemented by adapters."""

    def reserve(
        self,
        routed_request: RoutedBrokerOrderRequest,
        *,
        occurred_at: datetime | None = None,
    ) -> tuple[BrokerOrder, bool]: ...

    def get(self, owner_id: str, client_order_id: str) -> BrokerOrder | None: ...

    def get_routed(
        self, owner_id: str, client_order_id: str
    ) -> RoutedBrokerOrder | None: ...

    def get_by_broker_order_id(
        self, target: BrokerAccountRef, broker_order_id: str
    ) -> BrokerOrder | None: ...

    def orders(
        self,
        owner_id: str | None = None,
        *,
        target: BrokerAccountRef | None = None,
    ) -> list[BrokerOrder]: ...

    def reconciliation_candidates(
        self, target: BrokerAccountRef, owner_id: str | None = None
    ) -> list[BrokerOrder]: ...

    def claim_next(self, target: BrokerAccountRef) -> RoutedBrokerOrder | None: ...

    def finish_dispatch(self, order: BrokerOrder) -> None: ...

    def block_dispatch(self, order: BrokerOrder, reason: str) -> None: ...

    def block_pending_dispatches(self, target: BrokerAccountRef, reason: str) -> int: ...

    def reserve_cancel(
        self, target: BrokerAccountRef, owner_id: str, client_order_id: str
    ) -> tuple[BrokerOrder, bool]: ...

    def claim_next_cancel(self, target: BrokerAccountRef) -> RoutedBrokerOrder | None: ...

    def finish_cancel(self, order: BrokerOrder) -> None: ...

    def block_cancel(self, order: BrokerOrder, reason: str) -> None: ...

    def save_reconciliation(self, order: BrokerOrder) -> None: ...

    def apply_reconciled_snapshot(
        self, target: BrokerAccountRef, snapshot: "BrokerReconciliationSnapshot"
    ) -> None: ...

    def recover_interrupted_dispatches(self) -> int: ...

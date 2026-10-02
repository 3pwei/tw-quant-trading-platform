from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Protocol

from ..auth.models import AccountStatus, AuthUser, TradingMode
from ..events import (
    DomainEvent,
    EventMetadata,
    FillEvent,
    OrderIntent,
    OrderStatusEvent,
    RiskDecision,
    SessionEvent,
)
from tw_quant_core.futures_costs import FuturesCostConfig
from ..market import TAIPEI


@dataclass(frozen=True)
class AccountRiskConfig:
    max_position_contracts: int = 2
    max_risk_per_trade: float = 5_000.0
    max_daily_loss: float = 10_000.0
    max_trades_per_day: int = 10
    max_consecutive_losses: int = 3
    cooldown_minutes: int = 30

    def __post_init__(self) -> None:
        positive = (
            "max_position_contracts",
            "max_risk_per_trade",
            "max_daily_loss",
            "max_trades_per_day",
            "max_consecutive_losses",
        )
        for name in positive:
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.cooldown_minutes < 0:
            raise ValueError("cooldown_minutes cannot be negative")


@dataclass(frozen=True)
class TradingAccess:
    owner_id: str
    status: AccountStatus
    trading_mode: TradingMode
    permissions: frozenset[str]

    @classmethod
    def from_user(cls, user: AuthUser) -> "TradingAccess":
        return cls(
            owner_id=user.user_id,
            status=user.status,
            trading_mode=user.trading_mode,
            permissions=frozenset(user.permissions),
        )


class TradingAccessRegistry:
    """Small replaceable access snapshot; FastAPI can refresh it from app_users."""

    def __init__(self, users: tuple[AuthUser, ...] = ()):
        self._accounts: dict[str, TradingAccess] = {}
        for user in users:
            self.set_user(user)

    def set_user(self, user: AuthUser) -> None:
        self._accounts[user.user_id] = TradingAccess.from_user(user)

    def set_access(self, access: TradingAccess) -> None:
        self._accounts[access.owner_id] = access

    def resolve(self, owner_id: str) -> TradingAccess | None:
        return self._accounts.get(owner_id)


class PositionKeyView(Protocol):
    owner_id: str
    strategy_id: str
    strategy_version: int
    symbol: str
    contract: str


class PositionView(Protocol):
    key: PositionKeyView
    quantity: int


class TradeView(Protocol):
    owner_id: str
    exit_order_id: str
    exit_time: datetime
    net_pnl: float
    trading_date: date | None


class RiskLedger(Protocol):
    trades: list[TradeView]

    def open_positions(self) -> tuple[PositionView, ...]: ...


@dataclass
class AccountRiskState:
    trading_date: date | None = None
    realized_pnl: float = 0.0
    trades: int = 0
    consecutive_losses: int = 0
    cooldown_until: datetime | None = None
    kill_switch_active: bool = False
    kill_switch_reason: str | None = None
    manual_kill_switch: bool = False
    reserved_entries: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class RiskAuditEntry:
    event_id: str
    occurred_at: datetime
    owner_id: str
    order_id: str
    approved: bool
    approved_quantity: int
    reason: str
    estimated_risk: float | None


@dataclass(frozen=True)
class RiskControlAuditEntry:
    occurred_at: datetime
    owner_id: str
    action: str
    reason: str


@dataclass(frozen=True)
class AccountRiskSnapshot:
    owner_id: str
    trading_date: date | None
    realized_pnl: float
    trades: int
    consecutive_losses: int
    cooldown_until: datetime | None
    kill_switch_active: bool
    kill_switch_reason: str | None
    open_contracts: int
    reserved_contracts: int


class AccountRiskGate:
    """Fail-closed, owner-scoped risk gate for simulated/paper orders."""

    def __init__(
        self,
        ledger: RiskLedger,
        access: TradingAccessRegistry,
        *,
        config: AccountRiskConfig | None = None,
        costs: FuturesCostConfig | None = None,
    ):
        self.ledger = ledger
        self.access = access
        self.config = config or AccountRiskConfig()
        self.costs = costs or FuturesCostConfig()
        self._states: dict[str, AccountRiskState] = {}
        self._seen_order_ids: set[str] = set()
        self._seen_trade_order_ids: set[str] = set()
        self.audit_log: list[RiskAuditEntry] = []
        self.control_log: list[RiskControlAuditEntry] = []

    @staticmethod
    def _owner(event: DomainEvent) -> str:
        owner_id = event.meta.owner_id
        if not owner_id:
            raise ValueError(f"{event.kind} risk event requires owner_id")
        return owner_id

    @staticmethod
    def _trading_date(event: OrderIntent | FillEvent) -> date:
        return event.trading_date or event.meta.occurred_at.astimezone(TAIPEI).date()

    def _state(self, owner_id: str, trading_date: date) -> AccountRiskState:
        state = self._states.setdefault(owner_id, AccountRiskState())
        if state.trading_date != trading_date:
            manual_active = state.kill_switch_active and state.manual_kill_switch
            manual_reason = state.kill_switch_reason if manual_active else None
            state.trading_date = trading_date
            state.realized_pnl = 0.0
            state.trades = 0
            state.consecutive_losses = 0
            state.cooldown_until = None
            state.reserved_entries.clear()
            state.kill_switch_active = manual_active
            state.kill_switch_reason = manual_reason
            state.manual_kill_switch = manual_active
        return state

    def _open_contracts(self, owner_id: str) -> int:
        return sum(
            abs(position.quantity)
            for position in self.ledger.open_positions()
            if position.key.owner_id == owner_id
        )

    def _risk_reducing_quantity(self, order: OrderIntent) -> int:
        owner_id = self._owner(order)
        for position in self.ledger.open_positions():
            if (
                position.key.owner_id == owner_id
                and position.key.strategy_id == order.strategy_id
                and position.key.strategy_version == order.strategy_version
                and position.key.symbol == order.symbol
                and position.key.contract == order.contract
            ):
                closes_long = position.quantity > 0 and order.side == "sell"
                closes_short = position.quantity < 0 and order.side == "buy"
                if closes_long or closes_short:
                    return min(abs(position.quantity), order.quantity)
        return 0

    def _estimated_risk(self, order: OrderIntent) -> float | None:
        if order.reference_price <= 0 or order.stop_loss_price is None:
            return None
        price_risk = (
            abs(order.reference_price - order.stop_loss_price)
            * self.costs.multiplier
            * order.quantity
        )
        entry_commission, entry_tax = self.costs.side_cost(
            order.reference_price, order.quantity
        )
        exit_commission, exit_tax = self.costs.side_cost(
            order.stop_loss_price, order.quantity
        )
        slippage_risk = self.costs.slippage_points * 2 * self.costs.multiplier * order.quantity
        return (
            price_risk
            + entry_commission
            + entry_tax
            + exit_commission
            + exit_tax
            + slippage_risk
        )

    def _decision(
        self,
        order: OrderIntent,
        *,
        approved: bool,
        quantity: int,
        reason: str,
        estimated_risk: float | None = None,
    ) -> list[RiskDecision]:
        metadata = EventMetadata.create(
            kind="risk_decision",
            occurred_at=order.meta.occurred_at,
            source="account_risk",
            source_key=f"{order.meta.event_id}:{reason}",
            causation_id=order.meta.event_id,
            correlation_id=order.meta.correlation_id,
            owner_id=order.meta.owner_id,
        )
        decision = RiskDecision(
            meta=metadata,
            order_id=order.order_id,
            approved=approved,
            approved_quantity=quantity if approved else 0,
            reason=reason,
            estimated_risk=estimated_risk,
            reference_price=(order.reference_price or None),
        )
        self.audit_log.append(
            RiskAuditEntry(
                event_id=metadata.event_id,
                occurred_at=metadata.occurred_at,
                owner_id=self._owner(order),
                order_id=order.order_id,
                approved=approved,
                approved_quantity=decision.approved_quantity,
                reason=reason,
                estimated_risk=estimated_risk,
            )
        )
        return [decision]

    def recheck_before_fill(
        self,
        order: OrderIntent,
        expected_fill_price: float,
        occurred_at: datetime,
        *,
        blocked_reason: str | None = None,
    ) -> RiskDecision:
        """Revalidate an approved entry against the executable next open."""
        owner_id = self._owner(order)
        state = self._state(owner_id, self._trading_date(order))
        adjusted = replace(order, reference_price=expected_fill_price)
        estimated_risk = self._estimated_risk(adjusted)
        reason = blocked_reason
        account = self.access.resolve(owner_id)
        if reason is None and account is None:
            reason = "account_unknown"
        elif reason is None and account.status != AccountStatus.ACTIVE:
            reason = "account_not_active"
        elif reason is None and account.trading_mode != TradingMode.PAPER:
            reason = "paper_mode_required"
        elif reason is None and "orders.paper" not in account.permissions:
            reason = "paper_permission_required"
        elif reason is None and state.kill_switch_active:
            reason = "kill_switch_active"
        elif reason is None and state.realized_pnl <= -self.config.max_daily_loss:
            reason = "daily_loss_limit"
        elif (
            reason is None
            and state.cooldown_until is not None
            and occurred_at < state.cooldown_until
        ):
            reason = "loss_streak_cooldown"
        elif reason is None and order.order_id not in state.reserved_entries:
            reason = "risk_reservation_missing"
        elif reason is None and (
            state.trades + len(state.reserved_entries)
            > self.config.max_trades_per_day
        ):
            reason = "daily_trade_limit"
        elif reason is None and (
            self._open_contracts(owner_id)
            + sum(state.reserved_entries.values())
            > self.config.max_position_contracts
        ):
            reason = "max_position_exceeded"
        elif reason is None and order.stop_loss_price is None:
            reason = "stop_loss_required"
        elif reason is None and not (
            (order.side == "buy" and order.stop_loss_price < expected_fill_price)
            or (order.side == "sell" and order.stop_loss_price > expected_fill_price)
        ):
            reason = "gap_risk_exceeded"
        elif reason is None and (
            estimated_risk is None
            or estimated_risk > self.config.max_risk_per_trade
        ):
            reason = "gap_risk_exceeded"

        approved = reason is None
        reason = reason or "gap_risk_approved"
        metadata = EventMetadata.create(
            kind="risk_decision",
            occurred_at=occurred_at,
            source="account_risk_prefill",
            source_key=f"{order.order_id}:{expected_fill_price}:{reason}",
            causation_id=order.meta.event_id,
            correlation_id=order.meta.correlation_id,
            owner_id=owner_id,
        )
        decision = RiskDecision(
            meta=metadata,
            order_id=order.order_id,
            approved=approved,
            approved_quantity=order.quantity if approved else 0,
            reason=reason,
            estimated_risk=estimated_risk,
            reference_price=expected_fill_price,
            phase="prefill",
        )
        self.audit_log.append(RiskAuditEntry(
            event_id=metadata.event_id,
            occurred_at=occurred_at,
            owner_id=owner_id,
            order_id=order.order_id,
            approved=approved,
            approved_quantity=decision.approved_quantity,
            reason=reason,
            estimated_risk=estimated_risk,
        ))
        return decision

    def restore_reservation(self, order: OrderIntent, quantity: int) -> None:
        if order.reduce_only or quantity <= 0:
            return
        owner_id = self._owner(order)
        state = self._state(owner_id, self._trading_date(order))
        state.reserved_entries[order.order_id] = quantity
        self._seen_order_ids.add(order.order_id)

    def on_order_status(self, event: DomainEvent) -> None:
        if not isinstance(event, OrderStatusEvent):
            raise TypeError("AccountRiskGate.on_order_status requires OrderStatusEvent")
        owner_id = self._owner(event)
        state = self._states.get(owner_id)
        if state is not None:
            state.reserved_entries.pop(event.order_id, None)
        return None

    def on_order(self, event: DomainEvent) -> list[RiskDecision]:
        if not isinstance(event, OrderIntent):
            raise TypeError("AccountRiskGate.on_order requires OrderIntent")
        owner_id = self._owner(event)
        if event.order_id in self._seen_order_ids:
            return self._decision(
                event, approved=False, quantity=0, reason="duplicate_order"
            )
        self._seen_order_ids.add(event.order_id)

        reducing_quantity = self._risk_reducing_quantity(event) if event.reduce_only else 0
        if event.reduce_only:
            if reducing_quantity == 0:
                return self._decision(
                    event,
                    approved=False,
                    quantity=0,
                    reason="reduce_only_position_unavailable",
                )
            return self._decision(
                event,
                approved=True,
                quantity=reducing_quantity,
                reason="risk_reducing_approved",
            )

        trading_date = self._trading_date(event)
        state = self._state(owner_id, trading_date)
        account = self.access.resolve(owner_id)
        if account is None:
            return self._decision(event, approved=False, quantity=0, reason="account_unknown")
        if account.status != AccountStatus.ACTIVE:
            return self._decision(event, approved=False, quantity=0, reason="account_not_active")
        if account.trading_mode != TradingMode.PAPER:
            return self._decision(event, approved=False, quantity=0, reason="paper_mode_required")
        if "orders.paper" not in account.permissions:
            return self._decision(
                event, approved=False, quantity=0, reason="paper_permission_required"
            )
        if state.kill_switch_active:
            return self._decision(event, approved=False, quantity=0, reason="kill_switch_active")

        now = event.meta.occurred_at
        if state.cooldown_until is not None:
            if now < state.cooldown_until:
                return self._decision(
                    event, approved=False, quantity=0, reason="loss_streak_cooldown"
                )
            state.cooldown_until = None
            state.consecutive_losses = 0
        if state.realized_pnl <= -self.config.max_daily_loss:
            self.activate_kill_switch(
                owner_id,
                "daily_loss_limit",
                occurred_at=event.meta.occurred_at,
                manual=False,
            )
            return self._decision(event, approved=False, quantity=0, reason="daily_loss_limit")
        if state.trades + len(state.reserved_entries) >= self.config.max_trades_per_day:
            return self._decision(event, approved=False, quantity=0, reason="daily_trade_limit")

        projected = (
            self._open_contracts(owner_id)
            + sum(state.reserved_entries.values())
            + event.quantity
        )
        if projected > self.config.max_position_contracts:
            return self._decision(
                event, approved=False, quantity=0, reason="max_position_exceeded"
            )
        estimated_risk = self._estimated_risk(event)
        if estimated_risk is None:
            return self._decision(
                event, approved=False, quantity=0, reason="stop_loss_required"
            )
        valid_stop = (
            event.side == "buy" and event.stop_loss_price < event.reference_price
        ) or (
            event.side == "sell" and event.stop_loss_price > event.reference_price
        )
        if not valid_stop:
            return self._decision(
                event,
                approved=False,
                quantity=0,
                reason="invalid_stop_direction",
                estimated_risk=estimated_risk,
            )
        if estimated_risk > self.config.max_risk_per_trade:
            return self._decision(
                event,
                approved=False,
                quantity=0,
                reason="max_trade_risk_exceeded",
                estimated_risk=estimated_risk,
            )

        state.reserved_entries[event.order_id] = event.quantity
        return self._decision(
            event,
            approved=True,
            quantity=event.quantity,
            reason="approved",
            estimated_risk=estimated_risk,
        )

    def on_fill(self, event: DomainEvent) -> None:
        if not isinstance(event, FillEvent):
            raise TypeError("AccountRiskGate.on_fill requires FillEvent")
        owner_id = self._owner(event)
        state = self._state(owner_id, self._trading_date(event))
        state.reserved_entries.pop(event.order_id, None)
        if event.purpose == "entry":
            state.trades += 1

        for trade in self.ledger.trades:
            if trade.exit_order_id in self._seen_trade_order_ids:
                continue
            self._seen_trade_order_ids.add(trade.exit_order_id)
            trade_date = trade.trading_date or trade.exit_time.astimezone(TAIPEI).date()
            trade_state = self._state(trade.owner_id, trade_date)
            trade_state.realized_pnl += trade.net_pnl
            if trade.net_pnl < 0:
                trade_state.consecutive_losses += 1
                if trade_state.consecutive_losses >= self.config.max_consecutive_losses:
                    trade_state.cooldown_until = trade.exit_time + timedelta(
                        minutes=self.config.cooldown_minutes
                    )
            else:
                trade_state.consecutive_losses = 0
                trade_state.cooldown_until = None
            if trade_state.realized_pnl <= -self.config.max_daily_loss:
                self.activate_kill_switch(
                    trade.owner_id,
                    "daily_loss_limit",
                    occurred_at=trade.exit_time,
                    manual=False,
                )
        return None

    def on_session(self, event: DomainEvent) -> None:
        if not isinstance(event, SessionEvent):
            raise TypeError("AccountRiskGate.on_session requires SessionEvent")
        if event.action not in {"closing", "closed"}:
            return None
        for owner_id, state in self._states.items():
            if event.meta.owner_id is None or event.meta.owner_id == owner_id:
                state.reserved_entries.clear()
        return None

    def activate_kill_switch(
        self,
        owner_id: str,
        reason: str = "manual",
        *,
        occurred_at: datetime,
        manual: bool = True,
    ) -> None:
        if not reason:
            raise ValueError("kill switch reason is required")
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise ValueError("kill switch time must be timezone-aware")
        state = self._states.setdefault(owner_id, AccountRiskState())
        state.kill_switch_active = True
        state.kill_switch_reason = reason
        state.manual_kill_switch = manual
        self.control_log.append(
            RiskControlAuditEntry(
                occurred_at=occurred_at,
                owner_id=owner_id,
                action="kill_switch.activated",
                reason=reason,
            )
        )

    def reset_kill_switch(
        self,
        owner_id: str,
        *,
        occurred_at: datetime,
        reason: str = "manual_reset",
    ) -> None:
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise ValueError("kill switch time must be timezone-aware")
        if not reason:
            raise ValueError("kill switch reset reason is required")
        state = self._states.setdefault(owner_id, AccountRiskState())
        state.kill_switch_active = False
        state.kill_switch_reason = None
        state.manual_kill_switch = False
        self.control_log.append(
            RiskControlAuditEntry(
                occurred_at=occurred_at,
                owner_id=owner_id,
                action="kill_switch.reset",
                reason=reason,
            )
        )

    def snapshot(self, owner_id: str) -> AccountRiskSnapshot:
        state = self._states.setdefault(owner_id, AccountRiskState())
        return AccountRiskSnapshot(
            owner_id=owner_id,
            trading_date=state.trading_date,
            realized_pnl=state.realized_pnl,
            trades=state.trades,
            consecutive_losses=state.consecutive_losses,
            cooldown_until=state.cooldown_until,
            kill_switch_active=state.kill_switch_active,
            kill_switch_reason=state.kill_switch_reason,
            open_contracts=self._open_contracts(owner_id),
            reserved_contracts=sum(state.reserved_entries.values()),
        )

    def kill_switch_summary(self) -> dict[str, int]:
        active = [state for state in self._states.values() if state.kill_switch_active]
        return {
            "active": len(active),
            "manual": sum(1 for state in active if state.manual_kill_switch),
            "automatic": sum(1 for state in active if not state.manual_kill_switch),
            "tracked_accounts": len(self._states),
        }

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
from enum import Enum
from hashlib import sha256
import json

from ..execution.live_models import InstrumentSpec, LiveExecutionCandidate
from ..market import ExecutionQuote


class LiveKillSwitchAction(str, Enum):
    HALT_ENTRY = "halt_entry"
    CANCEL_WORKING = "cancel_working"
    FLATTEN = "flatten"


class LiveKillSwitchScope(str, Enum):
    GLOBAL = "global"
    OWNER = "owner"
    BROKER_ACCOUNT = "broker_account"
    EXECUTION_TARGET = "execution_target"


@dataclass(frozen=True)
class LiveKillSwitchState:
    action: LiveKillSwitchAction
    scope: LiveKillSwitchScope
    scope_key: str
    reason: str
    activated_at: datetime

    def __post_init__(self) -> None:
        if not self.scope_key.strip() or not self.reason.strip():
            raise ValueError("kill switch scope key and reason are required")
        if self.activated_at.tzinfo is None:
            raise ValueError("kill switch timestamp must be timezone-aware")


@dataclass(frozen=True)
class LiveRiskConfig:
    """Server-owned Live limits, intentionally separate from Paper risk."""

    allowed_symbols: frozenset[str]
    allowed_contracts: frozenset[str]
    max_order_quantity: int = 1
    max_account_position_contracts: int = 1
    max_owner_portfolio_position_contracts: int = 1
    max_risk_per_trade: float = 5_000.0
    max_daily_loss: float = 10_000.0
    max_daily_trades: int = 10
    max_pending_orders: int = 1
    max_quote_age_seconds: float = 2.0
    max_slippage_ticks: int = 2
    max_spread_ticks: int = 4
    allowed_sessions: frozenset[str] = frozenset({"day", "night"})
    expiry_guard_days: int = 2
    max_active_live_runtimes: int = 1
    reservation_seconds: float = 5.0

    def __post_init__(self) -> None:
        if not self.allowed_symbols or not self.allowed_contracts:
            raise ValueError("live symbol and contract allowlists are required")
        for name in (
            "max_order_quantity",
            "max_account_position_contracts",
            "max_owner_portfolio_position_contracts",
            "max_daily_trades",
            "max_pending_orders",
            "max_active_live_runtimes",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be positive")
        for name in ("max_risk_per_trade", "max_daily_loss", "max_quote_age_seconds", "reservation_seconds"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.max_slippage_ticks < 0 or self.max_spread_ticks < 0:
            raise ValueError("slippage and spread limits cannot be negative")
        if self.expiry_guard_days < 0:
            raise ValueError("expiry_guard_days cannot be negative")
        if not self.allowed_sessions <= {"day", "night"} or not self.allowed_sessions:
            raise ValueError("allowed_sessions must contain day and/or night")

    @property
    def version(self) -> str:
        payload = asdict(self)
        for key, value in tuple(payload.items()):
            if isinstance(value, (set, frozenset)):
                payload[key] = sorted(value)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return "live-risk:" + sha256(encoded.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class LiveRiskContext:
    now: datetime
    recovery_status: str
    broker_connected: bool
    broker_truth_captured_at: datetime | None
    market_status: str
    account_position: int | None
    owner_portfolio_position: int | None
    working_order_quantity: int | None
    owner_working_order_quantity: int | None
    pending_order_count: int | None
    daily_realized_pnl: float | None
    daily_trade_count: int | None
    active_live_runtimes: int
    account_reservation_quantity: int = 0
    portfolio_reservation_quantity: int = 0
    kill_switches: tuple[LiveKillSwitchState, ...] = ()

    def __post_init__(self) -> None:
        if self.now.tzinfo is None:
            raise ValueError("risk context time must be timezone-aware")
        if self.broker_truth_captured_at is not None and self.broker_truth_captured_at.tzinfo is None:
            raise ValueError("broker truth timestamp must be timezone-aware")


@dataclass(frozen=True)
class LiveRiskApproval:
    approved: bool
    reason: str
    policy_version: str
    estimated_risk: float | None = None
    warnings: tuple[str, ...] = ()


class LiveRiskService:
    """Pure, broker-neutral admission calculation over canonical snapshots."""

    def __init__(self, config: LiveRiskConfig):
        self.config = config

    def evaluate(
        self,
        candidate: LiveExecutionCandidate,
        context: LiveRiskContext,
        quote: ExecutionQuote | None,
        instrument: InstrumentSpec,
    ) -> LiveRiskApproval:
        version = self.config.version
        reject = lambda reason: LiveRiskApproval(False, reason, version)
        if context.recovery_status != "ready":
            return reject("recovery_not_ready")
        if context.broker_truth_captured_at is None:
            return reject("broker_truth_unavailable")
        truth_age = (context.now - context.broker_truth_captured_at).total_seconds()
        if truth_age > self.config.max_quote_age_seconds * 2:
            return reject("broker_truth_stale")
        if candidate.symbol != instrument.symbol or candidate.contract != instrument.contract:
            return reject("instrument_spec_mismatch")
        if quote is None or not quote.complete:
            return reject("execution_quote_incomplete")
        quote_age = (context.now - quote.received_at).total_seconds()
        if quote_age < 0 or quote_age > self.config.max_quote_age_seconds:
            return reject("stale_execution_quote")

        if candidate.reduce_only:
            if context.account_position is None:
                return reject("position_unavailable")
            closes = (
                context.account_position > 0 and candidate.side == "sell"
            ) or (
                context.account_position < 0 and candidate.side == "buy"
            )
            if not closes or candidate.quantity > abs(context.account_position):
                return reject("reduce_only_position_unavailable")
            warnings: list[str] = []
            if not context.broker_connected:
                warnings.append("broker_unavailable")
            if context.market_status != "healthy":
                warnings.append(context.market_status or "market_unhealthy")
            return LiveRiskApproval(True, "risk_reducing_approved", version, warnings=tuple(warnings))

        if not context.broker_connected:
            return reject("target_unavailable")
        if context.market_status in {"market_stale", "provider_disconnected", "trading_halted"}:
            return reject(context.market_status)
        if candidate.symbol not in self.config.allowed_symbols:
            return reject("symbol_not_allowed")
        if candidate.contract not in self.config.allowed_contracts:
            return reject("contract_not_allowed")
        if candidate.quantity > self.config.max_order_quantity:
            return reject("max_order_quantity_exceeded")
        if candidate.session not in self.config.allowed_sessions:
            return reject("session_not_allowed")
        if instrument.expiry_date is None:
            return reject("expiry_metadata_unavailable")
        if (instrument.expiry_date - context.now.date()).days <= self.config.expiry_guard_days:
            return reject("expiry_guard")
        if candidate.planned_stop_price is None:
            return reject("stop_required")
        for state in context.kill_switches:
            if state.action in {
                LiveKillSwitchAction.HALT_ENTRY,
                LiveKillSwitchAction.CANCEL_WORKING,
                LiveKillSwitchAction.FLATTEN,
            }:
                return reject(f"kill_switch_{state.action.value}")
        if context.active_live_runtimes > self.config.max_active_live_runtimes:
            return reject("max_active_live_runtimes_exceeded")
        if (
            context.pending_order_count is None
            or context.working_order_quantity is None
            or context.owner_working_order_quantity is None
        ):
            return reject("working_orders_unavailable")
        if context.pending_order_count >= self.config.max_pending_orders:
            return reject("max_pending_orders_exceeded")
        if context.daily_realized_pnl is None:
            return reject("daily_loss_unavailable")
        if context.daily_realized_pnl <= -self.config.max_daily_loss:
            return reject("daily_loss_exceeded")
        if context.daily_trade_count is None:
            return reject("daily_trade_count_unavailable")
        if context.daily_trade_count >= self.config.max_daily_trades:
            return reject("daily_trades_exceeded")
        if context.account_position is None or context.owner_portfolio_position is None:
            return reject("position_unavailable")
        projected_delta = candidate.signed_quantity
        account_projected = (
            context.account_position
            + context.working_order_quantity
            + context.account_reservation_quantity
            + projected_delta
        )
        if abs(account_projected) > self.config.max_account_position_contracts:
            return reject("account_position_limit")
        portfolio_projected = (
            context.owner_portfolio_position
            + context.owner_working_order_quantity
            + context.portfolio_reservation_quantity
            + projected_delta
        )
        if abs(portfolio_projected) > self.config.max_owner_portfolio_position_contracts:
            return reject("portfolio_position_limit")

        reference = quote.best_ask if candidate.side == "buy" else quote.best_bid
        assert reference is not None
        stop = candidate.planned_stop_price
        if (candidate.side == "buy" and stop >= reference) or (
            candidate.side == "sell" and stop <= reference
        ):
            return reject("invalid_stop_direction")
        price_risk = abs(reference - stop) * instrument.multiplier * candidate.quantity
        fees = instrument.commission_per_side * 2 * candidate.quantity
        taxes = (
            (reference + stop)
            * instrument.multiplier
            * instrument.tax_rate
            * candidate.quantity
        )
        slippage = (
            self.config.max_slippage_ticks
            * instrument.tick_size
            * instrument.multiplier
            * candidate.quantity
        )
        estimated = price_risk + fees + taxes + slippage
        if estimated > self.config.max_risk_per_trade:
            return LiveRiskApproval(False, "max_risk_per_trade", version, estimated)
        return LiveRiskApproval(True, "approved", version, estimated)

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol

from ..broker.capabilities import BrokerCapabilities
from ..broker.models import BrokerOrderRequest, ExecutionMode
from ..market import ExecutionQuote
from ..risk.live import LiveRiskApproval
from .live_models import InstrumentSpec, LiveExecutionCandidate


@dataclass(frozen=True)
class ExecutionPolicyResult:
    policy_name: str
    policy_version: str
    request: BrokerOrderRequest | None
    reason: str
    reference_price: float | None = None
    limit_price: float | None = None

    @property
    def accepted(self) -> bool:
        return self.request is not None


class ExecutionPolicy(Protocol):
    name: str
    version: str

    def evaluate(
        self,
        candidate: LiveExecutionCandidate,
        quote: ExecutionQuote | None,
        instrument: InstrumentSpec,
        capabilities: BrokerCapabilities,
        risk: LiveRiskApproval,
    ) -> ExecutionPolicyResult: ...


def _client_order_id(candidate: LiveExecutionCandidate, policy_version: str) -> str:
    raw = "|".join((
        candidate.decision_id,
        candidate.target.broker_name,
        candidate.target.account_id,
        policy_version,
    ))
    return "shadow:" + sha256(raw.encode("utf-8")).hexdigest()


class MarketableLimitIOCPolicy:
    """Cross the top of book by a bounded number of ticks, without fallback."""

    name = "marketable_limit_ioc"
    version = "marketable-limit-ioc:v1"

    def __init__(self, max_slippage_ticks: int, max_spread_ticks: int):
        if max_slippage_ticks < 0 or max_spread_ticks < 0:
            raise ValueError("policy tick limits cannot be negative")
        self.max_slippage_ticks = max_slippage_ticks
        self.max_spread_ticks = max_spread_ticks

    def evaluate(self, candidate, quote, instrument, capabilities, risk):
        reject = lambda reason: ExecutionPolicyResult(
            self.name, self.version, None, reason
        )
        if not risk.approved:
            return reject(risk.reason)
        if quote is None or not quote.complete:
            return reject("execution_quote_incomplete")
        try:
            capabilities.require("supports_limit_orders")
            capabilities.require("supports_ioc")
        except RuntimeError:
            return reject("execution_policy_unsupported")
        assert quote.best_bid is not None and quote.best_ask is not None
        spread_ticks = (quote.best_ask - quote.best_bid) / instrument.tick_size
        if spread_ticks > self.max_spread_ticks + 1e-9:
            return reject("spread_limit_exceeded")
        reference = quote.best_ask if candidate.side == "buy" else quote.best_bid
        movement = self.max_slippage_ticks * instrument.tick_size
        limit = reference + movement if candidate.side == "buy" else reference - movement
        if limit <= 0:
            return reject("slippage_limit_exceeded")
        request = BrokerOrderRequest(
            client_order_id=_client_order_id(candidate, self.version),
            owner_id=candidate.owner_id,
            strategy_id=candidate.strategy_id,
            strategy_version=candidate.strategy_version,
            symbol=candidate.symbol,
            contract=candidate.contract,
            side=candidate.side,
            quantity=candidate.quantity,
            mode=ExecutionMode.LIVE,
            order_type="limit",
            time_in_force="ioc",
            limit_price=round(limit / instrument.tick_size) * instrument.tick_size,
            reference_price=reference,
            risk_stop_price=candidate.planned_stop_price,
            reduce_only=candidate.reduce_only,
            purpose="exit" if candidate.reduce_only else "entry",
            reason=candidate.reason,
            correlation_id=candidate.decision_id,
        )
        return ExecutionPolicyResult(
            self.name,
            self.version,
            request,
            "would_submit",
            reference_price=reference,
            limit_price=request.limit_price,
        )


class MarketPriceIOCPolicy:
    name = "market_price_ioc"
    version = "market-price-ioc:v1"

    def evaluate(self, candidate, quote, instrument, capabilities, risk):
        reject = lambda reason: ExecutionPolicyResult(
            self.name, self.version, None, reason
        )
        if not risk.approved:
            return reject(risk.reason)
        try:
            capabilities.require("supports_market_orders")
            capabilities.require("supports_ioc")
        except RuntimeError:
            return reject("execution_policy_unsupported")
        if quote is None or not quote.complete:
            return reject("execution_quote_incomplete")
        reference = quote.best_ask if candidate.side == "buy" else quote.best_bid
        request = BrokerOrderRequest(
            client_order_id=_client_order_id(candidate, self.version),
            owner_id=candidate.owner_id,
            strategy_id=candidate.strategy_id,
            strategy_version=candidate.strategy_version,
            symbol=candidate.symbol,
            contract=candidate.contract,
            side=candidate.side,
            quantity=candidate.quantity,
            mode=ExecutionMode.LIVE,
            order_type="market",
            time_in_force="ioc",
            reference_price=reference,
            risk_stop_price=candidate.planned_stop_price,
            reduce_only=candidate.reduce_only,
            purpose="exit" if candidate.reduce_only else "entry",
            reason=candidate.reason,
            correlation_id=candidate.decision_id,
        )
        return ExecutionPolicyResult(
            self.name, self.version, request, "would_submit", reference_price=reference
        )


class EmergencyExitPolicy(MarketPriceIOCPolicy):
    """Risk reduction has priority, but remains subject to explicit capability."""

    name = "emergency_exit"
    version = "emergency-exit:v1"

    def evaluate(self, candidate, quote, instrument, capabilities, risk):
        if not candidate.reduce_only:
            return ExecutionPolicyResult(
                self.name, self.version, None, "emergency_exit_requires_reduce_only"
            )
        if not risk.approved:
            return ExecutionPolicyResult(
                self.name, self.version, None, risk.reason
            )
        try:
            capabilities.require("supports_market_orders")
            capabilities.require("supports_ioc")
        except RuntimeError:
            return ExecutionPolicyResult(
                self.name, self.version, None, "execution_policy_unsupported"
            )
        reference = None
        if quote is not None:
            reference = quote.best_ask if candidate.side == "buy" else quote.best_bid
        request = BrokerOrderRequest(
            client_order_id=_client_order_id(candidate, self.version),
            owner_id=candidate.owner_id,
            strategy_id=candidate.strategy_id,
            strategy_version=candidate.strategy_version,
            symbol=candidate.symbol,
            contract=candidate.contract,
            side=candidate.side,
            quantity=candidate.quantity,
            mode=ExecutionMode.LIVE,
            order_type="market",
            time_in_force="ioc",
            reference_price=reference,
            reduce_only=True,
            purpose="liquidation",
            reason=candidate.reason,
            correlation_id=candidate.decision_id,
        )
        return ExecutionPolicyResult(
            self.name, self.version, request, "would_submit", reference_price=reference
        )

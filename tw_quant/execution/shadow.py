from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from time import perf_counter
from threading import Lock
from typing import Protocol

from ..broker import BrokerCapabilities, BrokerOrderRequest
from ..market import ExecutionQuoteView
from ..risk.live import LiveRiskContext, LiveRiskService
from .live_models import InstrumentSpec, LiveExecutionCandidate
from .live_policy import ExecutionPolicy


@dataclass(frozen=True)
class ShadowExecutionResult:
    shadow_id: str
    owner_id: str
    runtime_id: str
    decision_id: str
    broker_name: str
    account_id: str
    strategy_id: str
    strategy_version: int
    symbol: str
    contract: str
    trigger_time: datetime
    action: str
    direction: str
    quantity: int
    risk_status: str
    risk_reason: str
    risk_policy_version: str
    execution_policy: str
    execution_policy_version: str
    best_bid: float | None
    best_ask: float | None
    last_price: float | None
    quote_time: datetime | None
    quote_age_ms: float | None
    reference_price: float | None
    limit_price: float | None
    stop_price: float | None
    order_type: str | None
    time_in_force: str | None
    reduce_only: bool
    result: str
    reason: str
    request: BrokerOrderRequest | None
    created_at: datetime
    evaluation_ms: float

    def public_dict(self) -> dict[str, object]:
        value = asdict(self)
        value.pop("account_id", None)
        value.pop("broker_name", None)
        value.pop("request", None)
        value["masked_account_id"] = "****" + self.account_id[-4:]
        for key in ("trigger_time", "quote_time", "created_at"):
            item = value[key]
            value[key] = item.isoformat(timespec="milliseconds") if item else None
        return value


class ShadowExecutionStore(Protocol):
    def get(self, decision_id: str, broker_name: str, account_id: str, policy_version: str) -> ShadowExecutionResult | None: ...
    def save(self, result: ShadowExecutionResult, reservation_expires_at: datetime | None) -> tuple[ShadowExecutionResult, bool]: ...
    def active_reservation_quantity(self, owner_id: str, symbol: str, contract: str, now: datetime, broker_name: str | None = None, account_id: str | None = None) -> int: ...
    def release_runtime(self, runtime_id: str) -> int: ...


class ShadowRiskContextProvider(Protocol):
    def context(self, candidate: LiveExecutionCandidate, account_reservation: int, portfolio_reservation: int) -> LiveRiskContext: ...


class InstrumentSpecProvider(Protocol):
    def resolve(self, symbol: str, contract: str) -> InstrumentSpec: ...


class BrokerCapabilityView(Protocol):
    def capabilities(self, candidate: LiveExecutionCandidate) -> BrokerCapabilities: ...


class ShadowExecutionService:
    """Single-writer shadow pipeline. No live order store or broker port is accepted."""

    def __init__(self, *, risk: LiveRiskService, policy: ExecutionPolicy,
                 quotes: ExecutionQuoteView, instruments: InstrumentSpecProvider,
                 capabilities: BrokerCapabilityView,
                 context: ShadowRiskContextProvider, store: ShadowExecutionStore):
        self.risk = risk
        self.policy = policy
        self.quotes = quotes
        self.instruments = instruments
        self.capability_view = capabilities
        self.context_provider = context
        self.store = store
        # Fixed lock stripes bound memory while preserving per-owner serialization.
        self._owner_locks = tuple(Lock() for _ in range(64))
        self._metrics_lock = Lock()
        self.evaluations = self.would_submit = self.rejected = 0
        self.total_evaluation_ms = self.max_evaluation_ms = 0.0
        self.reject_reasons: dict[str, int] = {}
        self._owner_metrics: dict[str, dict[str, object]] = {}

    def _lock(self, owner_id: str) -> Lock:
        index = int.from_bytes(
            sha256(owner_id.encode("utf-8")).digest()[:2], "big"
        ) % len(self._owner_locks)
        return self._owner_locks[index]

    def _record_metrics(
        self, candidate: LiveExecutionCandidate,
        result: ShadowExecutionResult, elapsed: float,
    ) -> None:
        with self._metrics_lock:
            self.evaluations += 1
            self.total_evaluation_ms += elapsed
            self.max_evaluation_ms = max(self.max_evaluation_ms, elapsed)
            if result.result == "would_submit":
                self.would_submit += 1
            else:
                self.rejected += 1
                self.reject_reasons[result.reason] = self.reject_reasons.get(result.reason, 0) + 1
            owner = self._owner_metrics.setdefault(candidate.owner_id, {
                "evaluations": 0, "would_submit": 0, "rejected": 0,
                "total_ms": 0.0, "max_ms": 0.0, "reject_reasons": {},
            })
            owner["evaluations"] = int(owner["evaluations"]) + 1
            owner["total_ms"] = float(owner["total_ms"]) + elapsed
            owner["max_ms"] = max(float(owner["max_ms"]), elapsed)
            if result.result == "would_submit":
                owner["would_submit"] = int(owner["would_submit"]) + 1
            else:
                owner["rejected"] = int(owner["rejected"]) + 1
                reasons = owner["reject_reasons"]
                assert isinstance(reasons, dict)
                reasons[result.reason] = int(reasons.get(result.reason, 0)) + 1

    def _persist_preflight_rejection(
        self, candidate: LiveExecutionCandidate, reason: str,
        now: datetime, started: float,
    ) -> ShadowExecutionResult:
        elapsed = (perf_counter() - started) * 1_000
        key = "|".join((
            candidate.decision_id, candidate.target.broker_name,
            candidate.target.account_id, self.policy.version,
        ))
        result = ShadowExecutionResult(
            shadow_id="shadow-rejected:" + sha256(key.encode("utf-8")).hexdigest(),
            owner_id=candidate.owner_id, runtime_id=candidate.runtime_id,
            decision_id=candidate.decision_id,
            broker_name=candidate.target.broker_name,
            account_id=candidate.target.account_id,
            strategy_id=candidate.strategy_id,
            strategy_version=candidate.strategy_version,
            symbol=candidate.symbol, contract=candidate.contract,
            trigger_time=candidate.trigger_time, action=candidate.action,
            direction=candidate.direction, quantity=candidate.quantity,
            risk_status="rejected", risk_reason=reason,
            risk_policy_version=self.risk.config.version,
            execution_policy=self.policy.name,
            execution_policy_version=self.policy.version,
            best_bid=None, best_ask=None, last_price=None, quote_time=None,
            quote_age_ms=None, reference_price=None, limit_price=None,
            stop_price=candidate.planned_stop_price, order_type=None,
            time_in_force=None, reduce_only=candidate.reduce_only,
            result="rejected", reason=reason, request=None,
            created_at=now, evaluation_ms=elapsed,
        )
        saved, _ = self.store.save(result, None)
        self._record_metrics(candidate, saved, elapsed)
        return saved

    def evaluate(self, candidate: LiveExecutionCandidate) -> ShadowExecutionResult:
        started = perf_counter()
        with self._lock(candidate.owner_id):
            existing = self.store.get(
                candidate.decision_id,
                candidate.target.broker_name,
                candidate.target.account_id,
                self.policy.version,
            )
            if existing is not None:
                return existing
            try:
                now = self.context_provider.context(candidate, 0, 0).now
            except Exception:
                return self._persist_preflight_rejection(
                    candidate, "execution_context_unavailable",
                    datetime.now(timezone.utc), started,
                )
            portfolio_reserved = self.store.active_reservation_quantity(
                candidate.owner_id, candidate.symbol, candidate.contract, now
            )
            account_reserved = self.store.active_reservation_quantity(
                candidate.owner_id, candidate.symbol, candidate.contract, now,
                candidate.target.broker_name, candidate.target.account_id,
            )
            try:
                context = self.context_provider.context(
                    candidate, account_reserved, portfolio_reserved
                )
            except Exception:
                return self._persist_preflight_rejection(
                    candidate, "execution_context_unavailable", now, started,
                )
            try:
                quote = self.quotes.get(candidate.symbol, candidate.contract)
            except Exception:
                return self._persist_preflight_rejection(
                    candidate, "execution_quote_unavailable", now, started,
                )
            try:
                instrument = self.instruments.resolve(candidate.symbol, candidate.contract)
            except Exception:
                return self._persist_preflight_rejection(
                    candidate, "instrument_spec_unavailable", now, started,
                )
            try:
                risk_result = self.risk.evaluate(candidate, context, quote, instrument)
            except Exception:
                return self._persist_preflight_rejection(
                    candidate, "live_risk_failed", now, started,
                )
            try:
                policy_result = (
                    self.policy.evaluate(
                        candidate,
                        quote,  # type: ignore[arg-type]
                        instrument,
                        self.capability_view.capabilities(candidate),
                        risk_result,
                    )
                    if quote is not None
                    else None
                )
                request = policy_result.request if policy_result else None
                reason = policy_result.reason if policy_result else risk_result.reason
            except Exception:
                policy_result = None
                request = None
                reason = "execution_policy_failed"
            result_name = "would_submit" if request is not None else "rejected"
            elapsed = (perf_counter() - started) * 1_000
            quote_age = (
                max(0.0, (context.now - quote.received_at).total_seconds() * 1_000)
                if quote is not None else None
            )
            rejected_key = "|".join((
                candidate.decision_id,
                candidate.target.broker_name,
                candidate.target.account_id,
                self.policy.version,
            ))
            shadow_id = request.client_order_id if request is not None else (
                "shadow-rejected:" + sha256(rejected_key.encode("utf-8")).hexdigest()
            )
            result = ShadowExecutionResult(
                shadow_id=shadow_id,
                owner_id=candidate.owner_id,
                runtime_id=candidate.runtime_id,
                decision_id=candidate.decision_id,
                broker_name=candidate.target.broker_name,
                account_id=candidate.target.account_id,
                strategy_id=candidate.strategy_id,
                strategy_version=candidate.strategy_version,
                symbol=candidate.symbol,
                contract=candidate.contract,
                trigger_time=candidate.trigger_time,
                action=candidate.action,
                direction=candidate.direction,
                quantity=candidate.quantity,
                risk_status="approved" if risk_result.approved else "rejected",
                risk_reason=risk_result.reason,
                risk_policy_version=risk_result.policy_version,
                execution_policy=self.policy.name,
                execution_policy_version=self.policy.version,
                best_bid=quote.best_bid if quote else None,
                best_ask=quote.best_ask if quote else None,
                last_price=quote.last_price if quote else None,
                quote_time=quote.received_at if quote else None,
                quote_age_ms=quote_age,
                reference_price=policy_result.reference_price if policy_result else None,
                limit_price=policy_result.limit_price if policy_result else None,
                stop_price=candidate.planned_stop_price,
                order_type=request.order_type if request else None,
                time_in_force=request.time_in_force if request else None,
                reduce_only=candidate.reduce_only,
                result=result_name,
                reason=reason,
                request=request,
                created_at=context.now,
                evaluation_ms=elapsed,
            )
            reservation_expiry = (
                context.now
                + timedelta(seconds=self.risk.config.reservation_seconds)
                if request is not None and not candidate.reduce_only
                else None
            )
            saved, _ = self.store.save(result, reservation_expiry)
            self._record_metrics(candidate, saved, elapsed)
            return saved

    def metrics(self, owner_id: str | None = None) -> dict[str, object]:
        if owner_id is not None:
            with self._metrics_lock:
                owner = dict(self._owner_metrics.get(owner_id, {}))
                if "reject_reasons" in owner:
                    owner["reject_reasons"] = dict(owner["reject_reasons"])
            evaluations = int(owner.get("evaluations", 0))
            total_ms = float(owner.get("total_ms", 0.0))
            return {
                "shadow_evaluation_total": evaluations,
                "shadow_would_submit_total": int(owner.get("would_submit", 0)),
                "shadow_rejected_total": int(owner.get("rejected", 0)),
                "average_shadow_evaluation_ms": round(total_ms / evaluations, 3) if evaluations else 0.0,
                "max_shadow_evaluation_ms": round(float(owner.get("max_ms", 0.0)), 3),
                "reject_reasons": dict(owner.get("reject_reasons", {})),
            }
        with self._metrics_lock:
            evaluations = self.evaluations
            would_submit = self.would_submit
            rejected = self.rejected
            total_ms = self.total_evaluation_ms
            max_ms = self.max_evaluation_ms
            reasons = dict(self.reject_reasons)
        return {
            "shadow_evaluation_total": evaluations,
            "shadow_would_submit_total": would_submit,
            "shadow_rejected_total": rejected,
            "average_shadow_evaluation_ms": round(
                total_ms / evaluations, 3
            ) if evaluations else 0.0,
            "max_shadow_evaluation_ms": round(max_ms, 3),
            "reject_reasons": reasons,
        }

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping, Protocol

from ..broker import (
    BrokerAccountRef,
    BrokerCapabilities,
    BrokerOrderStatus,
    BrokerTruthStore,
    LiveOrderStore,
    RecoveryLockStore,
)
from ..execution.live_models import InstrumentSpec, LiveExecutionCandidate
from ..risk.live import (
    LiveKillSwitchAction,
    LiveKillSwitchScope,
    LiveKillSwitchState,
    LiveRiskContext,
)
from .shadow_store import SQLiteShadowExecutionRepository
from .storage import TradingRuntimeRepository


class MarketStatusView(Protocol):
    def status_message(self) -> dict[str, object]: ...


class ExecutionHealthView(Protocol):
    def snapshot(self) -> dict[str, object]: ...


class LiveExecutionTargetCatalog:
    """Resolve opaque public IDs to server-known durable broker identities."""

    def __init__(
        self,
        truth: BrokerTruthStore,
        owner_target_ids: Mapping[str, frozenset[str]],
    ):
        self.truth = truth
        self.owner_target_ids = owner_target_ids

    def resolve(self, public_id: str, owner_id: str) -> BrokerAccountRef:
        allowed = self.owner_target_ids.get(owner_id, frozenset())
        matches = [
            target for target in self.truth.targets()
            if target.public_id in allowed and target.public_id == public_id
        ]
        if len(matches) != 1:
            raise KeyError("unknown live shadow execution target")
        return matches[0]

    def targets(self, owner_id: str) -> frozenset[BrokerAccountRef]:
        """Return the server-owned account set used for portfolio aggregation."""
        allowed = self.owner_target_ids.get(owner_id, frozenset())
        return frozenset(
            target for target in self.truth.targets()
            if target.public_id in allowed
        )

    def public_targets(self, owner_id: str) -> list[dict[str, str]]:
        return [
            {
                "target_id": target.public_id,
                "provider_label": "configured",
                "masked_account_id": "****" + target.account_id[-4:],
            }
            for target in self.truth.targets()
            if target in self.targets(owner_id)
        ]


@dataclass(frozen=True)
class StaticInstrumentSpecCatalog:
    specs: Mapping[tuple[str, str], InstrumentSpec]

    def resolve(self, symbol: str, contract: str) -> InstrumentSpec:
        try:
            return self.specs[(symbol, contract)]
        except KeyError as exc:
            raise KeyError("instrument specification unavailable") from exc


@dataclass(frozen=True)
class StaticBrokerCapabilityView:
    values: Mapping[BrokerAccountRef, BrokerCapabilities]

    def capabilities(self, candidate: LiveExecutionCandidate) -> BrokerCapabilities:
        try:
            return self.values[candidate.target]
        except KeyError as exc:
            raise KeyError("broker capabilities unavailable") from exc


@dataclass(frozen=True)
class ConfiguredBrokerCapabilityView:
    """Composition-layer provider map; core policy has no broker-name branch."""

    providers: Mapping[str, BrokerCapabilities]

    def capabilities(self, candidate: LiveExecutionCandidate) -> BrokerCapabilities:
        try:
            return self.providers[candidate.target.broker_name]
        except KeyError as exc:
            raise KeyError("broker capabilities unavailable") from exc


class LiveShadowRiskContextProvider:
    """Join cached health with durable broker truth; never calls a broker SDK."""

    def __init__(self, *, truth: BrokerTruthStore, recovery: RecoveryLockStore,
                 orders: LiveOrderStore, runtimes: TradingRuntimeRepository,
                 market: MarketStatusView, execution_health: ExecutionHealthView,
                 shadow_store: SQLiteShadowExecutionRepository,
                 targets: LiveExecutionTargetCatalog,
                 now=None):
        self.truth = truth
        self.recovery = recovery
        self.orders = orders
        self.runtimes = runtimes
        self.market = market
        self.execution_health = execution_health
        self.shadow_store = shadow_store
        self.targets = targets
        self.now = now or (lambda: datetime.now(timezone.utc))

    def _connected(self, target: BrokerAccountRef) -> bool:
        accounts = self.execution_health.snapshot().get("broker_accounts", [])
        return any(
            isinstance(item, Mapping)
            and item.get("target_id") == target.public_id
            and item.get("broker_connected") is True
            for item in accounts
        )

    def _owner_targets(self, owner_id: str) -> set[BrokerAccountRef]:
        return set(self.targets.targets(owner_id))

    def _daily_stats(self, candidate, snapshot, now):
        fills = [item for item in snapshot.fills if item.occurred_at.date() == now.date()]
        matched = []
        for fill in fills:
            order = self.orders.get_by_broker_order_id(candidate.target, fill.broker_order_id)
            if order is None:
                return None, None
            if order.request.owner_id == candidate.owner_id:
                matched.append(fill)
        # Broker reconciliation guarantees identity/order completeness before READY.
        # A flat no-fill day is therefore a reliable zero. Non-flat PnL accounting
        # remains unavailable until a canonical Live realized-PnL ledger exists.
        if not matched:
            return 0.0, 0
        return None, len({item.broker_order_id for item in matched})

    def context(self, candidate, account_reservation, portfolio_reservation):
        now = self.now()
        recovery = self.recovery.state(
            candidate.target.broker_name, candidate.target.account_id
        )
        snapshot = self.truth.get(candidate.target)
        account_position = None
        captured_at = None
        daily_pnl = daily_trades = None
        if snapshot is not None:
            captured_at = snapshot.captured_at
            account_position = sum(
                item.quantity for item in snapshot.positions
                if item.contract == candidate.contract
            )
            daily_pnl, daily_trades = self._daily_stats(candidate, snapshot, now)
        portfolio_position = 0
        portfolio_known = True
        owner_targets = self._owner_targets(candidate.owner_id)
        for target in owner_targets:
            owner_snapshot = self.truth.get(target)
            if owner_snapshot is None:
                portfolio_known = False
                break
            portfolio_position += sum(
                item.quantity for item in owner_snapshot.positions
                if item.contract == candidate.contract
            )
        working = [
            order for order in self.orders.orders(candidate.owner_id, target=candidate.target)
            if not order.status.terminal
            and order.status is not BrokerOrderStatus.CREATED
            and order.request.contract == candidate.contract
        ]
        working_quantity = sum(
            order.request.quantity if order.request.side == "buy" else -order.request.quantity
            for order in working
        )
        owner_working_quantity = 0
        for target in owner_targets:
            owner_working_quantity += sum(
                order.request.quantity if order.request.side == "buy" else -order.request.quantity
                for order in self.orders.orders(candidate.owner_id, target=target)
                if not order.status.terminal
                and order.status is not BrokerOrderStatus.CREATED
                and order.request.contract == candidate.contract
            )
        runtime_count = sum(
            runtime.get("mode") == "live_shadow" and runtime.get("status") != "stopped"
            for runtime in self.runtimes.trading_runtimes(candidate.owner_id)
        )
        account_scope = f"{candidate.target.broker_name}:{candidate.target.account_id}"
        kill_switches = self.shadow_store.kill_switches({
            (LiveKillSwitchScope.GLOBAL.value, "global"),
            (LiveKillSwitchScope.OWNER.value, candidate.owner_id),
            (LiveKillSwitchScope.BROKER_ACCOUNT.value, account_scope),
        })
        return LiveRiskContext(
            now=now,
            recovery_status=recovery.status.value,
            broker_connected=self._connected(candidate.target),
            broker_truth_captured_at=captured_at,
            market_status=str(self.market.status_message().get("service_status") or "market_unhealthy"),
            account_position=account_position,
            owner_portfolio_position=(portfolio_position if portfolio_known else None),
            working_order_quantity=working_quantity,
            owner_working_order_quantity=owner_working_quantity,
            pending_order_count=len(working),
            daily_realized_pnl=daily_pnl,
            daily_trade_count=daily_trades,
            active_live_runtimes=int(runtime_count),
            account_reservation_quantity=account_reservation,
            portfolio_reservation_quantity=portfolio_reservation,
            kill_switches=kill_switches,
        )


class ConfiguredManualCanaryContext:
    """Canary view over cached health and reconciled persistence only."""

    def __init__(self, *, owner_id, target_id, targets, instruments,
                 capabilities, risk_context, truth, recovery, health):
        self.owner_id = owner_id
        self.target_id = target_id
        self.targets = targets
        self.instruments = instruments
        self.capability_view = capabilities
        self.risk_provider = risk_context
        self.shadow_store = risk_context.shadow_store
        self.truth = truth
        self.recovery = recovery
        self.health = health

    def target(self, owner_id: str) -> BrokerAccountRef:
        if owner_id != self.owner_id:
            raise RuntimeError("live_canary_owner_not_allowed")
        try:
            return self.targets.resolve(self.target_id, owner_id)
        except KeyError as exc:
            raise RuntimeError("live_canary_target_unavailable") from exc

    def instrument(self, symbol: str, contract: str) -> InstrumentSpec:
        return self.instruments.resolve(symbol, contract)

    def risk_context(self, candidate: LiveExecutionCandidate) -> LiveRiskContext:
        return self.risk_provider.context(candidate, 0, 0)

    def capabilities(self, target: BrokerAccountRef) -> BrokerCapabilities:
        candidate = type("TargetCandidate", (), {"target": target})()
        return self.capability_view.capabilities(candidate)

    def broker_position(self, target: BrokerAccountRef, contract: str) -> int | None:
        snapshot = self.truth.get(target)
        if snapshot is None:
            return None
        return sum(item.quantity for item in snapshot.positions if item.contract == contract)

    def _account_health(self, target: BrokerAccountRef) -> Mapping[str, object] | None:
        for item in self.health.snapshot().get("broker_accounts", []):
            if isinstance(item, Mapping) and item.get("target_id") == target.public_id:
                return item
        return None

    def assert_arm_ready(self, target: BrokerAccountRef) -> None:
        self.recovery.assert_ready(target.broker_name, target.account_id)
        health = self._account_health(target)
        if health is None or health.get("broker_connected") is not True:
            raise RuntimeError("live_canary_broker_disconnected")
        if health.get("ca_ready") is not True:
            raise RuntimeError("live_canary_ca_not_ready")

    def preflight(
        self, owner_id: str, canary_enabled: bool, allowed_contracts: frozenset[str]
    ) -> dict[str, object]:
        """Return a sanitized, fail-closed readiness decision from durable/cached truth."""
        target = self.target(owner_id)
        recovery = self.recovery.state(target.broker_name, target.account_id)
        health = self._account_health(target) or {}
        snapshot = self.truth.get(target)
        orders = self.risk_provider.orders.orders(owner_id, target=target)
        guardian = health.get("position_guardian", {})
        guardian = guardian if isinstance(guardian, Mapping) else {}
        market_status = str(
            self.risk_provider.market.status_message().get("service_status") or "unknown"
        )
        recovery_issues = set(recovery.issue_codes)
        broker_position = None if snapshot is None else sum(
            position.quantity for position in snapshot.positions
        )
        only_allowed_positions = snapshot is not None and all(
            position.contract in allowed_contracts for position in snapshot.positions
        )
        protected = int(guardian.get("protected_quantity", 0) or 0)
        checks = {
            "canary_config_enabled": canary_enabled,
            "broker_connected": health.get("broker_connected") is True,
            "ca_ready": health.get("ca_ready") is True,
            "callback_registered": health.get("callback_registered") is True,
            "recovery_ready": recovery.ready,
            "broker_truth_reconciled": snapshot is not None and snapshot.account_ref == target,
            "no_unknown_orders": not any(
                order.status is BrokerOrderStatus.UNKNOWN for order in orders
            ),
            "no_unknown_external_orders": "unknown_broker_order" not in recovery_issues,
            "no_unmanaged_external_positions": (
                only_allowed_positions and broker_position == 0
                or (
                    only_allowed_positions
                    and broker_position is not None
                    and guardian.get("enabled") is True
                    and protected == abs(broker_position)
                )
            ),
            "market_healthy": market_status == "healthy",
            "guardian_healthy": guardian.get("enabled") is True
            and int(guardian.get("locked_positions", 0) or 0) == 0,
            "kill_switch_ready": not self.shadow_store.kill_switches({
                (LiveKillSwitchScope.GLOBAL.value, "global"),
                (LiveKillSwitchScope.OWNER.value, owner_id),
                (LiveKillSwitchScope.BROKER_ACCOUNT.value,
                 f"{target.broker_name}:{target.account_id}"),
                (LiveKillSwitchScope.EXECUTION_TARGET.value,
                 f"{owner_id}:{self.target_id}"),
            }),
            "callback_delivery_healthy": int(
                health.get("callbacks_dropped_total", 0) or 0
            ) == 0 and int(health.get("callbacks_failed_total", 0) or 0) == 0,
        }
        blockers = [name for name, passed in checks.items() if not passed]
        return {
            "ready": not blockers,
            "checks": checks,
            "blockers": blockers,
            "broker_name": target.broker_name,
            "masked_account_id": "****" + target.account_id[-4:],
            "market_status": market_status,
            "recovery_issues": list(recovery.issue_codes),
        }

    def public_status(self, owner_id: str) -> dict[str, object]:
        target = self.target(owner_id)
        recovery = self.recovery.state(target.broker_name, target.account_id)
        health = self._account_health(target) or {}
        return {
            "enabled": True,
            "broker_name": target.broker_name,
            "masked_account_id": "****" + target.account_id[-4:],
            "target_id": self.target_id,
            "broker_connected": health.get("broker_connected", False),
            "ca_ready": health.get("ca_ready", False),
            "recovery_status": recovery.status.value,
            "recovery_issues": list(recovery.issue_codes),
            "ordering_enabled": False,
            "position_guardian": health.get(
                "position_guardian", {"enabled": False}
            ),
        }

    def activate_kill_switch(self, owner_id, target, action, reason, now) -> None:
        self.shadow_store.activate_kill_switch(LiveKillSwitchState(
            action=action,
            scope=LiveKillSwitchScope.EXECUTION_TARGET,
            scope_key=f"{owner_id}:{self.target_id}",
            reason=reason,
            activated_at=now,
        ))


class ConfiguredLiveAutoContext:
    """Auto-Live adapter over reconciled canary state and the shared Guardian."""

    def __init__(self, canary: ConfiguredManualCanaryContext, guardian, orders, arms,
                 acceptance_passed: bool = True):
        self.canary = canary
        self.guardian = guardian
        self.orders = orders
        self.arms = arms
        self.acceptance_passed = acceptance_passed

    def preflight(self, runtime):
        owner = str(runtime["owner_user_id"])
        snapshot = runtime.get("strategy_snapshot")
        contract = snapshot.get("execution_contract") if isinstance(snapshot, Mapping) else None
        raw = self.canary.preflight(owner, True, frozenset({str(contract)}))
        checks = dict(raw["checks"])
        checks.update({
            "permission": True,
            "account_allowlist": self._target_binding_matches(runtime, owner),
            "quote_fresh": self.canary.risk_provider.market.status_message().get("service_status") == "healthy",
            "position_reconciled": checks.get("broker_truth_reconciled", False),
            "kill_switch_allows_entry": checks.get("kill_switch_ready", False),
            "live_risk_available": True,
            "production_acceptance_passed": self.acceptance_passed,
        })
        return checks

    def _target_binding_matches(self, runtime, owner):
        target = self.canary.target(owner)
        return (
            runtime.get("execution_target_id") == self.canary.target_id
            and runtime.get("broker_name") == target.broker_name
            and runtime.get("account_id") == target.account_id
        )

    def resolve_target(self, runtime):
        owner = str(runtime["owner_user_id"])
        if not self._target_binding_matches(runtime, owner):
            raise RuntimeError("live_auto_target_mismatch")
        return self.canary.target(owner)

    def risk_context(self, candidate): return self.canary.risk_context(candidate)
    def instrument(self, symbol, contract): return self.canary.instrument(symbol, contract)
    def capabilities(self, target): return self.canary.capabilities(target)
    def active_position(self, owner_id, target, contract): return self.canary.broker_position(target, contract)
    def active_entry_orders(self, owner_id, target, contract):
        return sum(not order.status.terminal and not order.request.reduce_only
                   for order in self.orders.orders(owner_id, target=target)
                   if order.request.contract == contract)
    def request_strategy_exit(self, runtime):
        target = self.resolve_target(runtime)
        positions = [p for p in self.guardian.store.positions(target)
                     if p.owner_id == runtime["owner_user_id"] and p.state.value != "flat"]
        if len(positions) != 1:
            raise RuntimeError("guardian_managed_position_unavailable")
        order, _created = self.guardian.request_strategy_exit(positions[0].position_id)
        return order.request.client_order_id
    def audit(self, event, runtime, detail):
        target = self.resolve_target(runtime)
        self.arms.audit(event, str(runtime["owner_user_id"]), target,
                        request_id=str(runtime["runtime_id"]), detail=dict(detail),
                        occurred_at=datetime.now(timezone.utc))

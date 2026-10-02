from __future__ import annotations

import hashlib
from typing import Mapping, Protocol

from ...auth import AccountStatus, AuthUser, TradingMode
from ...events import OrderIntent
from ...market import KBar
from ...paper import PaperOrderCommand, PaperTradingService
from ...risk import RiskLevels, triggered_exit
from ..storage import TradingRuntimeRepository


class UserDirectory(Protocol):
    def user_by_id(self, user_id: str) -> AuthUser | None: ...


class MarketHealth(Protocol):
    def status_message(self) -> dict[str, object]: ...


def auto_entry_idempotency_key(
    owner_id: str,
    runtime_id: str,
    contract: str,
    trigger_time: str,
    direction: str,
) -> str:
    canonical = "|".join(
        (owner_id, runtime_id, contract, trigger_time, direction, "entry")
    )
    return "auto:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def auto_exit_idempotency_key(
    owner_id: str,
    runtime_id: str,
    contract: str,
    position_opened_at: str,
    trigger_time: str,
    reason: str,
) -> str:
    canonical = "|".join((
        owner_id,
        runtime_id,
        contract,
        position_opened_at,
        trigger_time,
        reason,
        "exit",
    ))
    return "auto:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class PaperAutoExecutionController:
    """Bridge durable decisions and server-owned protection to Paper execution."""

    def __init__(
        self,
        runtimes: TradingRuntimeRepository,
        users: UserDirectory,
        market: MarketHealth,
        paper: PaperTradingService,
    ):
        self.runtimes = runtimes
        self.users = users
        self.market = market
        self.paper = paper
        self.paper.set_auto_entry_guard(self._prefill_block_reason)

    def recover(self) -> dict[str, object]:
        """Lock previously armed runtimes and reconcile durable attribution."""
        symbol = str(self.market.status_message().get("symbol") or "TMF")
        runtimes = self.runtimes.lock_paper_auto_runtimes_for_recovery(symbol)
        by_id = {str(item["runtime_id"]): item for item in runtimes}
        issues: dict[str, set[str]] = {
            runtime_id: set() for runtime_id in by_id
        }
        state = self.paper.strategy_auto_recovery_state()
        records = [*state["orders"], *state["positions"]]
        pending_entries: set[str] = set()
        positioned: set[str] = set()
        for record in records:
            runtime_id = str(record.get("runtime_id") or "")
            owner_id = str(record.get("owner_user_id") or "")
            runtime = by_id.get(runtime_id)
            if runtime is None:
                for candidate_id, candidate in by_id.items():
                    if candidate.get("owner_user_id") == owner_id:
                        issues[candidate_id].add("paper_runtime_missing")
                continue
            mismatch = (
                runtime.get("owner_user_id") != owner_id
                or runtime.get("mode") != "paper_auto"
                or runtime.get("strategy_id") != record.get("strategy_id")
                or runtime.get("symbol") != record.get("symbol")
                or (
                    runtime.get("strategy_version") is not None
                    and runtime.get("strategy_version")
                    != record.get("strategy_version")
                )
            )
            if mismatch:
                issues[runtime_id].add("paper_runtime_attribution_mismatch")
            snapshot = record.get("strategy_snapshot")
            if not isinstance(snapshot, Mapping) or dict(snapshot) != dict(
                runtime["strategy_snapshot"]  # type: ignore[arg-type]
            ):
                issues[runtime_id].add("paper_strategy_snapshot_mismatch")
            decision_id = str(record.get("decision_id") or "")
            if decision_id and not decision_id.startswith("protective:"):
                if self.runtimes.trading_decision(decision_id, owner_id) is None:
                    issues[runtime_id].add("paper_decision_missing")
            if record in state["orders"] and (
                record.get("status") in {"pending_risk", "approved"}
                and not record.get("reduce_only")
            ):
                pending_entries.add(runtime_id)
                if int(record.get("quantity") or 0) != int(runtime["quantity"]):
                    issues[runtime_id].add("paper_runtime_quantity_mismatch")
            if record in state["positions"]:
                positioned.add(runtime_id)

        for runtime_id in pending_entries & positioned:
            issues[runtime_id].add("pending_entry_with_open_position")
        for runtime_id, runtime in by_id.items():
            owner_id = str(runtime["owner_user_id"])
            user = self.users.user_by_id(owner_id)
            if user is not None:
                self.paper.access.set_user(user)
            if self.paper.recovery.issues_by_owner.get(owner_id):
                issues[runtime_id].add("paper_recovery_inconsistent")
            issue = ",".join(sorted(issues[runtime_id])) or None
            self.runtimes.set_trading_runtime_recovery(
                runtime_id, owner_id, issue
            )
        locked_ids = {
            runtime_id for runtime_id, runtime in by_id.items()
            if runtime.get("status") == "recovery_locked"
        }
        rejected = self.paper.reject_pending_auto_entries(
            locked_ids, "stale_or_recovered_signal"
        )
        for order in rejected:
            decision_id = str(order.get("decision_id") or "")
            owner_id = str(order.get("owner_id") or order.get("owner_user_id") or "")
            runtime = by_id.get(str(order.get("runtime_id") or ""))
            if not owner_id and runtime is not None:
                owner_id = str(runtime["owner_user_id"])
            if decision_id and owner_id:
                self.runtimes.update_decision_execution(
                    decision_id,
                    owner_id,
                    {
                        "execution_status": "skipped",
                        "execution_reason": "stale_or_recovered_signal",
                    },
                )
        return {
            "checked_runtimes": len(runtimes),
            "locked_runtimes": sum(
                item.get("status") == "recovery_locked" for item in runtimes
            ),
            "issues": sum(bool(item) for item in issues.values()),
        }

    def _prefill_block_reason(self, order: OrderIntent) -> str | None:
        owner_id = str(order.meta.owner_id or "")
        runtime = self.runtimes.trading_runtime(
            str(order.runtime_id or ""), owner_id
        )
        if runtime is None:
            return "runtime_unavailable"
        if runtime.get("status") != "armed":
            return f"runtime_{runtime.get('status', 'unavailable')}"
        if runtime.get("recovery_issue"):
            return "runtime_recovery_locked"
        user = self.users.user_by_id(owner_id)
        if user is None:
            return "account_unavailable"
        self.paper.access.set_user(user)
        market = self.market.status_message()
        if market.get("service_status") != "healthy":
            return str(market.get("service_status") or "market_unhealthy")
        if market.get("trading_block_reason"):
            return str(market["trading_block_reason"])
        account = self.paper.account(owner_id)
        if account.get("recovery_status") != "healthy":
            return "recovery_degraded"
        return None

    @staticmethod
    def _risk_percentages(
        runtime: Mapping[str, object]
    ) -> tuple[float, float]:
        snapshot = runtime.get("strategy_snapshot")
        if not isinstance(snapshot, Mapping):
            raise ValueError("strategy_snapshot_missing")
        if runtime["strategy_kind"] == "atomic":
            values = snapshot.get("parameters")
        else:
            definition = snapshot.get("definition")
            values = definition.get("risk") if isinstance(definition, Mapping) else None
        if not isinstance(values, Mapping):
            raise ValueError("risk_parameters_missing")
        try:
            stop = float(values["stop_loss_pct"])
            take = float(values["take_profit_pct"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("risk_parameters_missing") from exc
        if not 0 < stop < 1 or not 0 < take < 1:
            raise ValueError("risk_parameters_invalid")
        return stop, take

    def _skip(self, decision: Mapping[str, object], reason: str) -> None:
        self.runtimes.update_decision_execution(
            str(decision["decision_id"]),
            str(decision["owner_user_id"]),
            {"execution_status": "skipped", "execution_reason": reason},
        )

    def _block_reason(
        self,
        runtime: Mapping[str, object],
        decision: Mapping[str, object],
    ) -> tuple[str | None, AuthUser | None]:
        if runtime.get("mode") != "paper_auto":
            return "paper_auto_required", None
        if runtime.get("status") != "armed":
            return f"runtime_{runtime.get('status', 'unavailable')}", None
        user = self.users.user_by_id(str(runtime["owner_user_id"]))
        if user is None:
            return "account_unavailable", None
        if user.status is not AccountStatus.ACTIVE:
            return "account_inactive", user
        if user.trading_mode is not TradingMode.PAPER:
            return "paper_mode_disabled", user
        if "orders.paper" not in user.permissions:
            return "permission_missing", user
        market = self.market.status_message()
        market_reason = market.get("trading_block_reason")
        if market_reason:
            return str(market_reason), user
        if market.get("service_status") not in {None, "healthy"}:
            return str(market["service_status"]), user
        account = self.paper.account(user.user_id)
        if account.get("recovery_status") != "healthy":
            return "recovery_degraded", user
        if account.get("kill_switch_active"):
            return "kill_switch_active", user
        runtime_id = str(runtime["runtime_id"])
        if any(
            position.get("runtime_id") == runtime_id
            and int(position.get("quantity", 0)) != 0
            for position in self.paper.positions(user.user_id)
        ):
            return "position_already_open", user
        if any(
            order.get("runtime_id") == runtime_id
            and order.get("status") in {"pending_risk", "approved"}
            and not order.get("reduce_only")
            and order.get("decision_id") != decision.get("decision_id")
            for order in self.paper.orders(user.user_id)
        ):
            return "entry_order_already_open", user
        return None, user

    def on_decision(
        self,
        runtime: Mapping[str, object],
        decision: Mapping[str, object],
        signal_bar: KBar,
    ) -> None:
        if decision.get("action") == "exit":
            self._on_strategy_exit(runtime, decision, signal_bar)
            return
        if decision.get("action") != "entry":
            return
        current = self.runtimes.trading_runtime(
            str(runtime["runtime_id"]), str(runtime["owner_user_id"])
        )
        if current is None:
            self._skip(decision, "runtime_unavailable")
            return
        reason, user = self._block_reason(current, decision)
        if reason is not None or user is None:
            self._skip(decision, reason or "account_unavailable")
            return
        try:
            reference = float(decision.get("reference_price") or signal_bar.close)
            stop_pct, take_pct = self._risk_percentages(current)
            is_long = decision["direction"] == "long"
            planned_stop = reference * (1 - stop_pct if is_long else 1 + stop_pct)
            key = auto_entry_idempotency_key(
                user.user_id,
                str(current["runtime_id"]),
                str(decision["contract"]),
                str(decision["trigger_time"]),
                str(decision["direction"]),
            )
            order, _created = self.paper.submit(
                user,
                PaperOrderCommand(
                    strategy_id=str(current["strategy_id"]),
                    strategy_version=int(current.get("strategy_version") or 1),
                    side="buy" if is_long else "sell",
                    quantity=int(current["quantity"]),
                    stop_loss_price=planned_stop,
                    reason="strategy_auto_entry",
                    execution_timing="next_bar_open",
                    order_source="strategy_auto",
                    runtime_id=str(current["runtime_id"]),
                    decision_id=str(decision["decision_id"]),
                    reference_price=reference,
                    stop_loss_pct=stop_pct,
                    take_profit_pct=take_pct,
                    strategy_snapshot=dict(current["strategy_snapshot"]),
                    correlation_id=str(decision["decision_id"]),
                    causation_id=str(decision["decision_id"]),
                ),
                idempotency_key=key,
                market_bar=signal_bar,
                occurred_at=signal_bar.received_time,
            )
            status = str(order["status"])
            self.runtimes.update_decision_execution(
                str(decision["decision_id"]), user.user_id,
                {
                    "execution_status": (
                        "submitted" if status == "approved" else "rejected"
                    ),
                    "execution_reason": order["status_reason"],
                    "order_id": order["order_id"],
                    "reference_price": reference,
                    "planned_stop_price": planned_stop,
                },
            )
        except Exception as exc:
            self._skip(decision, f"controller_error:{type(exc).__name__}")

    def _on_strategy_exit(
        self,
        runtime: Mapping[str, object],
        decision: Mapping[str, object],
        signal_bar: KBar,
    ) -> None:
        current = self.runtimes.trading_runtime(
            str(runtime["runtime_id"]), str(runtime["owner_user_id"])
        )
        if current is None:
            self._skip(decision, "runtime_unavailable")
            return
        owner_id = str(current["owner_user_id"])
        position = next((
            item for item in self.paper.positions(owner_id)
            if item.get("runtime_id") == current["runtime_id"]
            and item.get("contract") == decision.get("contract")
            and int(item.get("quantity", 0)) != 0
        ), None)
        if position is None:
            self._skip(decision, "position_not_open")
            return
        quantity = int(position["quantity"])
        direction = "long" if quantity > 0 else "short"
        if decision.get("direction") != direction:
            self._skip(decision, "position_direction_mismatch")
            return
        user = self.users.user_by_id(owner_id)
        if user is None:
            self._skip(decision, "account_unavailable")
            return
        try:
            key = auto_exit_idempotency_key(
                owner_id,
                str(current["runtime_id"]),
                str(decision["contract"]),
                str(position.get("opened_at") or "legacy"),
                str(decision["trigger_time"]),
                str(decision["reason"]),
            )
            order, _created = self.paper.submit(
                user,
                PaperOrderCommand(
                    strategy_id=str(position["strategy_id"]),
                    strategy_version=int(position["strategy_version"]),
                    side="sell" if quantity > 0 else "buy",
                    quantity=abs(quantity),
                    stop_loss_price=None,
                    reduce_only=True,
                    reason=str(decision["reason"]),
                    execution_timing="next_bar_open",
                    order_source="strategy_auto",
                    runtime_id=str(current["runtime_id"]),
                    decision_id=str(decision["decision_id"]),
                    reference_price=float(
                        decision.get("reference_price") or signal_bar.close
                    ),
                    strategy_snapshot=dict(current["strategy_snapshot"]),
                    correlation_id=str(decision["decision_id"]),
                    causation_id=str(decision["decision_id"]),
                ),
                idempotency_key=key,
                market_bar=signal_bar,
                occurred_at=signal_bar.received_time,
            )
            status = str(order["status"])
            self.runtimes.update_decision_execution(
                str(decision["decision_id"]), owner_id,
                {
                    "execution_status": (
                        "submitted" if status == "approved" else "rejected"
                    ),
                    "execution_reason": order["status_reason"],
                    "order_id": order["order_id"],
                },
            )
        except Exception as exc:
            self._skip(decision, f"controller_error:{type(exc).__name__}")

    def _protect_positions(self, bar: KBar) -> None:
        for runtime in self.runtimes.paper_auto_runtimes(bar.symbol):
            owner_id = str(runtime["owner_user_id"])
            user = self.users.user_by_id(owner_id)
            if user is None:
                continue
            for position in self.paper.positions(owner_id):
                if (
                    position.get("runtime_id") != runtime["runtime_id"]
                    or position.get("contract") != bar.contract
                ):
                    continue
                stop = position.get("stop_loss_price")
                take = position.get("take_profit_price")
                if stop is None or take is None:
                    continue
                quantity = int(position["quantity"])
                result = triggered_exit(
                    direction=1 if quantity > 0 else -1,
                    open_price=bar.open,
                    high=bar.high,
                    low=bar.low,
                    levels=RiskLevels(float(stop), float(take)),
                )
                if result is None:
                    continue
                trigger_price, reason = result
                key = auto_exit_idempotency_key(
                    owner_id,
                    str(runtime["runtime_id"]),
                    bar.contract,
                    str(position.get("opened_at") or "legacy"),
                    bar.time.isoformat(timespec="milliseconds"),
                    reason,
                )
                protective_id = f"protective:{key.removeprefix('auto:')}"
                self.paper.submit(
                    user,
                    PaperOrderCommand(
                        strategy_id=str(position["strategy_id"]),
                        strategy_version=int(position["strategy_version"]),
                        side="sell" if quantity > 0 else "buy",
                        quantity=abs(quantity),
                        stop_loss_price=None,
                        reduce_only=True,
                        reason=reason,
                        execution_timing="bar_trigger",
                        order_source="strategy_auto",
                        runtime_id=str(runtime["runtime_id"]),
                        decision_id=protective_id,
                        reference_price=float(trigger_price),
                        strategy_snapshot=(
                            dict(position["strategy_snapshot"])
                            if isinstance(position.get("strategy_snapshot"), dict)
                            else dict(runtime["strategy_snapshot"])
                        ),
                        correlation_id=str(
                            position.get("decision_id") or protective_id
                        ),
                        causation_id=protective_id,
                    ),
                    idempotency_key=key,
                    market_bar=bar,
                    occurred_at=bar.received_time,
                )

    def before_bar(self, bar: KBar) -> None:
        if bar.status == "closed":
            self._protect_positions(bar)

    def after_bar(self, bar: KBar) -> None:
        if bar.status != "closed":
            return
        for decision in self.runtimes.auto_decisions(
            bar.symbol, ("submitted",)
        ):
            order_id = decision.get("order_id")
            if not order_id:
                continue
            order = self.paper.order(
                str(decision["owner_user_id"]), str(order_id)
            )
            if order is None:
                continue
            if order.get("status") == "filled":
                fill = (
                    self.paper.fill(
                        str(decision["owner_user_id"]), str(order["fill_id"])
                    )
                    if order.get("fill_id") else None
                )
                self.runtimes.update_decision_execution(
                    str(decision["decision_id"]),
                    str(decision["owner_user_id"]),
                    {
                        "execution_status": "filled",
                        "execution_reason": "simulated_fill",
                        "actual_fill_price": fill.get("price") if fill else None,
                    },
                )
            elif order.get("status") == "rejected":
                self.runtimes.update_decision_execution(
                    str(decision["decision_id"]),
                    str(decision["owner_user_id"]),
                    {
                        "execution_status": "rejected",
                        "execution_reason": order.get("status_reason"),
                    },
                )
        # A next-open entry filled while processing this bar. Its protective
        # levels are derived from that actual fill, so this same bar can now be
        # evaluated without using the preliminary reference-close stop.
        self._protect_positions(bar)


# Compatibility alias for integrations introduced by PR #99.
PaperAutoEntryController = PaperAutoExecutionController

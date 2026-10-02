from __future__ import annotations

import hashlib
from datetime import timedelta
from typing import Callable, Mapping, Protocol

from ...broker import BrokerAccountRef

from ...market import (
    KBar,
    TIMEFRAME_MINUTES,
    aggregate_kbars,
    timeframe_bucket,
    validate_timeframe,
)
from ...strategy_registry import (
    get_strategy_services,
    supported_strategies,
    evaluate_composite_intents,
    evaluate_strategy_intents,
    validate_strategy_parameters,
)
from ..storage import (
    MarketRepository,
    StrategyRepository,
    TradingRuntimeRepository,
)
from ..runtime_data import strategy_lineage
from .errors import InvalidInputError, ResourceNotFoundError


class ExecutionTargetCatalog(Protocol):
    def resolve(self, public_id: str, owner_id: str) -> BrokerAccountRef: ...


class RuntimeReservationStore(Protocol):
    def release_runtime(self, runtime_id: str) -> int: ...


def decision_fingerprint(
    runtime_id: str,
    strategy_id: str,
    strategy_version: int | None,
    contract: str,
    trigger_time: str,
    action: str,
    direction: str,
) -> str:
    canonical = "|".join((
        runtime_id,
        strategy_id,
        str(strategy_version or "atomic"),
        contract,
        trigger_time,
        action,
        direction,
    ))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def source_bar_fingerprint(
    symbol: str, contract: str, interval: str, source_bar_time: str
) -> str:
    canonical = "|".join((symbol, contract, interval, source_bar_time))
    return "bar:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class TradingRuntimeApplicationService:
    """Persist decisions produced from closed canonical bars."""

    def __init__(
        self,
        runtime_repository: TradingRuntimeRepository,
        strategy_repository: StrategyRepository,
        market_repository: MarketRepository,
        symbol: str,
        history_limit: int = 5000,
        *,
        live_shadow_enabled: bool = False,
        execution_targets: ExecutionTargetCatalog | None = None,
        reservation_store: RuntimeReservationStore | None = None,
        live_auto_enabled: bool = False,
        live_risk_version: str | None = None,
        live_execution_policy_version: str | None = None,
    ):
        self.runtime_repository = runtime_repository
        self.strategy_repository = strategy_repository
        self.market_repository = market_repository
        self.symbol = symbol
        self.history_limit = history_limit
        self.live_shadow_enabled = live_shadow_enabled
        self.execution_targets = execution_targets
        self.reservation_store = reservation_store
        self.live_auto_enabled = live_auto_enabled
        self.live_risk_version = live_risk_version
        self.live_execution_policy_version = live_execution_policy_version
        self._decision_listeners: list[
            Callable[[Mapping[str, object], Mapping[str, object], KBar], None]
        ] = []

    def add_decision_listener(
        self,
        listener: Callable[
            [Mapping[str, object], Mapping[str, object], KBar], None
        ],
    ) -> None:
        if listener not in self._decision_listeners:
            self._decision_listeners.append(listener)

    def create(
        self, request: Mapping[str, object], owner_id: str
    ) -> dict[str, object]:
        kind = str(request.get("strategy_kind", "atomic")).lower()
        if kind not in {"atomic", "composite"}:
            raise InvalidInputError("strategy_kind must be atomic or composite")
        mode = str(request.get("mode", "observe")).lower()
        if mode not in {"observe", "paper_auto", "live_shadow", "live_auto"}:
            raise InvalidInputError("unsupported runtime mode")
        target: BrokerAccountRef | None = None
        if mode in {"live_shadow", "live_auto"}:
            enabled = self.live_shadow_enabled if mode == "live_shadow" else self.live_auto_enabled
            if not enabled or self.execution_targets is None:
                raise InvalidInputError(f"{mode} is disabled")
            target_id = str(request.get("execution_target_id", "")).strip()
            if not target_id:
                raise InvalidInputError(f"{mode} requires execution_target_id")
            try:
                target = self.execution_targets.resolve(target_id, owner_id)
            except KeyError as exc:
                raise InvalidInputError("unknown live execution target") from exc
        symbol = str(request.get("symbol", self.symbol)).upper()
        if symbol != self.symbol:
            raise InvalidInputError("unsupported symbol")
        try:
            interval = validate_timeframe(str(request.get("interval", "1m")))
            quantity = int(request.get("quantity", 1))
        except (TypeError, ValueError) as exc:
            raise InvalidInputError(str(exc)) from exc
        if quantity < 1 or quantity > 100:
            raise InvalidInputError("quantity must be between 1 and 100")
        if mode == "live_auto" and quantity != 1:
            raise InvalidInputError("live_auto quantity must equal 1")

        strategy_id = str(request.get("strategy_id", "")).strip().lower()
        strategy_version: int | None = None
        plugin_identity = get_strategy_services().identity_snapshot()
        if kind == "atomic":
            if strategy_id not in supported_strategies():
                raise InvalidInputError("unsupported atomic strategy")
            parameters = validate_strategy_parameters(
                strategy_id,
                self.strategy_repository.strategy_parameters(owner_id).get(
                    strategy_id
                ),
            )
            version = self.strategy_repository.ensure_strategy_version(
                strategy_id, parameters, plugin_identity, owner_id
            )
            strategy_version = int(version["version"])
            lineage = dict(version["lineage"])
            snapshot: dict[str, object] = {
                "strategy": strategy_id,
                "parameters": parameters,
                "interval": interval,
                "atomic_strategy_version": strategy_version,
            }
        else:
            raw_version = request.get("strategy_version")
            try:
                requested_version = (
                    int(raw_version) if raw_version is not None else None
                )
            except (TypeError, ValueError) as exc:
                raise InvalidInputError(
                    "strategy_version must be an integer"
                ) from exc
            saved = self.strategy_repository.composite_strategy(
                strategy_id, requested_version, owner_id
            )
            if saved is None:
                raise ResourceNotFoundError("composite strategy not found")
            strategy_version = int(saved["version"])
            snapshot = {
                "strategy_id": strategy_id,
                "version": strategy_version,
                "definition": saved["definition"],
            }
            lineage = strategy_lineage(
                "composite", strategy_id, strategy_version, plugin_identity
            )
        if mode == "live_auto":
            if not self.live_risk_version or not self.live_execution_policy_version:
                raise InvalidInputError("live_auto policy versions are unavailable")
            snapshot = {
                **snapshot,
                "execution_contract": str(request.get("contract") or "").strip().upper(),
                "live_risk_config_version": self.live_risk_version,
                "execution_policy_version": self.live_execution_policy_version,
            }
            if not snapshot["execution_contract"]:
                raise InvalidInputError("live_auto requires an immutable contract")

        latest = [
            bar
            for bar in self.market_repository.latest(symbol, self.history_limit)
            if bar.status == "closed"
        ]
        initial_cursor = (
            latest[-1].time.isoformat(timespec="milliseconds")
            if latest
            else None
        )
        snapshot["plugin_identity"] = plugin_identity
        stored = self.runtime_repository.create_trading_runtime({
            "owner_user_id": owner_id,
            "strategy_kind": kind,
            "strategy_id": strategy_id,
            "strategy_version": strategy_version,
            "strategy_snapshot": snapshot,
            "strategy_lineage": lineage,
            "symbol": symbol,
            "interval": interval,
            "quantity": quantity,
            "mode": mode,
            "broker_name": target.broker_name if target else None,
            "account_id": target.account_id if target else None,
            "execution_target_id": target_id if target else None,
            "last_evaluated_bar": initial_cursor,
        })
        return self._public_runtime(stored)

    @staticmethod
    def _public_runtime(runtime: Mapping[str, object]) -> dict[str, object]:
        result = dict(runtime)
        account = result.pop("account_id", None)
        broker = result.pop("broker_name", None)
        if account and broker:
            target = BrokerAccountRef(str(broker), str(account))
            result["execution_target_id"] = result.get("execution_target_id") or target.public_id
            result["masked_account_id"] = "****" + target.account_id[-4:]
            result["provider_label"] = "configured"
        return result

    def list(self, owner_id: str) -> dict[str, object]:
        return {
            "runtimes": [
                self._public_runtime(item)
                for item in self.runtime_repository.trading_runtimes(owner_id)
            ]
        }

    def get(self, runtime_id: str, owner_id: str) -> dict[str, object]:
        runtime = self.runtime_repository.trading_runtime(
            runtime_id, owner_id
        )
        if runtime is None:
            raise ResourceNotFoundError("trading runtime not found")
        return self._public_runtime(runtime)

    def stop(self, runtime_id: str, owner_id: str) -> dict[str, object]:
        existing = self.get(runtime_id, owner_id)
        if existing["mode"] == "live_auto":
            raise InvalidInputError("live_auto must use its dedicated stop control")
        runtime = self.runtime_repository.stop_trading_runtime(
            runtime_id, owner_id
        )
        if runtime is None:
            raise ResourceNotFoundError("trading runtime not found")
        if self.reservation_store is not None:
            self.reservation_store.release_runtime(runtime_id)
        return self._public_runtime(runtime)

    def arm(self, runtime_id: str, owner_id: str) -> dict[str, object]:
        runtime = self.get(runtime_id, owner_id)
        if runtime["mode"] != "paper_auto":
            raise InvalidInputError("only paper_auto runtimes can be armed")
        if runtime["status"] == "stopped":
            raise InvalidInputError("a stopped runtime cannot be armed")
        if runtime.get("recovery_issue"):
            raise InvalidInputError(
                f"runtime recovery is locked: {runtime['recovery_issue']}"
            )
        latest = [
            bar for bar in self.market_repository.latest(
                self.symbol, self.history_limit
            )
            if bar.status == "closed"
        ]
        cursor = (
            latest[-1].time.isoformat(timespec="milliseconds")
            if latest else runtime.get("last_evaluated_bar")
        )
        result = self.runtime_repository.set_trading_runtime_status(
            runtime_id, owner_id, "armed", str(cursor) if cursor else None
        )
        assert result is not None
        return self._public_runtime(result)

    def pause(self, runtime_id: str, owner_id: str) -> dict[str, object]:
        runtime = self.get(runtime_id, owner_id)
        if runtime["mode"] == "live_auto":
            raise InvalidInputError("live_auto must use its dedicated disarm control")
        if runtime["mode"] != "paper_auto":
            raise InvalidInputError("only paper_auto runtimes can be paused")
        if runtime["status"] == "stopped":
            raise InvalidInputError("a stopped runtime cannot be paused")
        result = self.runtime_repository.set_trading_runtime_status(
            runtime_id, owner_id, "paused"
        )
        assert result is not None
        return self._public_runtime(result)

    def decisions(
        self, runtime_id: str, owner_id: str, limit: int = 200
    ) -> dict[str, object]:
        self.get(runtime_id, owner_id)
        return {
            "decisions": self.runtime_repository.trading_decisions(
                runtime_id, owner_id, limit
            )
        }

    def health(self) -> dict[str, object]:
        return self.runtime_repository.runtime_metrics()

    def paper_shadow_metrics(self, owner_user_id: str) -> dict[str, int]:
        return self.runtime_repository.paper_shadow_metrics(owner_user_id)

    @staticmethod
    def _completed_interval(bar: KBar, interval: str) -> bool:
        if interval == "1m":
            return True
        minutes = TIMEFRAME_MINUTES[validate_timeframe(interval)]
        if minutes is not None:
            following = bar.copy(time=bar.time + timedelta(minutes=1))
            return timeframe_bucket(bar, interval) != timeframe_bucket(
                following, interval
            )
        local_time = bar.time.timetz().replace(tzinfo=None)
        daily_close = (
            bar.session == "day"
            and local_time.hour == 13
            and local_time.minute == 44
        )
        if interval == "1d":
            return daily_close
        return daily_close and bar.trading_date.weekday() == 4

    def on_bar(self, bar: KBar) -> None:
        """LiveMarketService listener. It has no order or broker dependency."""
        if bar.status != "closed":
            return
        runtimes = self.runtime_repository.active_trading_runtimes(bar.symbol)
        if not runtimes:
            return
        source = [
            item
            for item in self.market_repository.latest(
                bar.symbol, self.history_limit
            )
            if item.status == "closed"
        ]
        intervals: dict[str, list[KBar]] = {}
        for runtime in runtimes:
            interval = str(runtime["interval"])
            if not self._completed_interval(bar, interval):
                continue
            if interval not in intervals:
                intervals[interval] = aggregate_kbars(source, interval)
            evaluated_bars = intervals[interval]
            if not evaluated_bars:
                continue
            cursor = runtime.get("last_evaluated_bar")
            pending_bars = [
                candidate
                for candidate in evaluated_bars
                if cursor is None
                or candidate.time.isoformat(timespec="milliseconds")
                > str(cursor)
            ]
            if not pending_bars:
                continue
            snapshot = runtime["strategy_snapshot"]
            assert isinstance(snapshot, Mapping)
            get_strategy_services().require_snapshot(snapshot)
            if runtime["strategy_kind"] == "atomic":
                raw_intents = evaluate_strategy_intents(
                    evaluated_bars,
                    str(snapshot["strategy"]),
                    parameters=dict(snapshot["parameters"]),
                    interval=interval,
                )
            else:
                raw_intents = evaluate_composite_intents(
                    source, snapshot["definition"]  # type: ignore[arg-type]
                )
            intents_by_time: dict[str, list[Mapping[str, object]]] = {}
            for intent in raw_intents:
                intents_by_time.setdefault(
                    str(intent["trigger_time"]), []
                ).append(intent)
            for evaluated in pending_bars:
                evaluated_time = evaluated.time.isoformat(
                    timespec="milliseconds"
                )
                is_fresh = (
                    timeframe_bucket(evaluated, interval)
                    == timeframe_bucket(bar, interval)
                )
                decisions = [
                    self._decision(
                        runtime,
                        item,
                        evaluated.close,
                        stale_or_recovered=not is_fresh,
                    )
                    for item in intents_by_time.get(evaluated_time, [])
                ]
                inserted = self.runtime_repository.record_runtime_evaluation(
                    str(runtime["runtime_id"]), evaluated_time, decisions
                )
                for decision in inserted:
                    if decision.get("execution_status") == "skipped":
                        continue
                    for listener in tuple(self._decision_listeners):
                        listener(runtime, decision, evaluated)

    @staticmethod
    def _decision(
        runtime: Mapping[str, object],
        intent: Mapping[str, object],
        reference_price: float,
        *,
        stale_or_recovered: bool = False,
    ) -> dict[str, object]:
        version = runtime.get("strategy_version")
        decision_id = decision_fingerprint(
            str(runtime["runtime_id"]),
            str(runtime["strategy_id"]),
            int(version) if version is not None else None,
            str(intent["contract"]),
            str(intent["trigger_time"]),
            str(intent["action"]),
            str(intent["direction"]),
        )
        decision = {
            "decision_id": decision_id,
            "runtime_id": runtime["runtime_id"],
            "owner_user_id": runtime["owner_user_id"],
            "strategy_id": runtime["strategy_id"],
            "strategy_version": version,
            "symbol": runtime["symbol"],
            "contract": intent["contract"],
            "interval": runtime["interval"],
            "trigger_time": intent["trigger_time"],
            "direction": intent["direction"],
            "action": intent["action"],
            "reason": intent["reason"],
            "context": dict(intent.get("context", {})),
            "source_bar_time": intent["source_bar_time"],
            "source_bar_id": source_bar_fingerprint(
                str(runtime["symbol"]),
                str(intent["contract"]),
                str(runtime["interval"]),
                str(intent["source_bar_time"]),
            ),
            "execution_status": (
                ("skipped" if stale_or_recovered else "pending")
                if (
                    runtime["mode"] in {"paper_auto", "live_shadow", "live_auto"}
                    and intent["action"] in {"entry", "exit"}
                )
                else "not_applicable"
            ),
            "execution_reason": (
                "stale_or_recovered_signal"
                if stale_or_recovered
                and runtime["mode"] in {"paper_auto", "live_shadow", "live_auto"}
                and intent["action"] in {"entry", "exit"}
                else None
            ),
            "reference_price": reference_price,
        }
        return decision

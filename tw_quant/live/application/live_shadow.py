from __future__ import annotations

from datetime import datetime
from queue import Empty, Full, Queue
from threading import Event, Thread
from typing import Mapping

from ...broker import BrokerAccountRef
from ...execution.live_models import LiveExecutionCandidate
from ...execution.shadow import ShadowExecutionService
from ...market import KBar
from ..storage import TradingRuntimeRepository


def _risk_stop(runtime: Mapping[str, object], direction: str, reference: float) -> float | None:
    snapshot = runtime.get("strategy_snapshot")
    if not isinstance(snapshot, Mapping):
        return None
    values: object = snapshot.get("parameters")
    if runtime.get("strategy_kind") == "composite":
        definition = snapshot.get("definition")
        values = definition.get("risk") if isinstance(definition, Mapping) else None
    if not isinstance(values, Mapping):
        return None
    try:
        percentage = float(values["stop_loss_pct"])
    except (KeyError, TypeError, ValueError):
        return None
    if not 0 < percentage < 1:
        return None
    return reference * (1 - percentage if direction == "long" else 1 + percentage)


class LiveShadowExecutionController:
    """Translate durable decisions to shadow candidates; never owns a live sink."""

    def __init__(
        self,
        runtimes: TradingRuntimeRepository,
        shadow: ShadowExecutionService,
    ) -> None:
        self.runtimes = runtimes
        self.shadow = shadow
        self.queue: Queue[tuple[dict[str, object], dict[str, object], KBar] | None] = Queue(maxsize=256)
        self._stop = Event()
        self._thread: Thread | None = None
        self.decisions_received = 0
        self.decisions_dropped = 0

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = Thread(
            target=self._run, name="live-shadow-evaluator", daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> bool:
        """Bounded drain; periodic/restart-safe decisions remain durable."""
        self._stop.set()
        try:
            self.queue.put_nowait(None)
        except Full:
            pass
        if self._thread is not None:
            self._thread.join(timeout)
        return self._thread is None or not self._thread.is_alive()

    def _run(self) -> None:
        while not self._stop.is_set() or not self.queue.empty():
            try:
                item = self.queue.get(timeout=0.1)
            except Empty:
                continue
            try:
                if item is None:
                    continue
                self._process(*item)
            finally:
                self.queue.task_done()

    def on_decision(
        self,
        runtime: Mapping[str, object],
        decision: Mapping[str, object],
        signal_bar: KBar,
    ) -> None:
        if runtime.get("mode") != "live_shadow" or decision.get("action") not in {"entry", "exit"}:
            return
        self.decisions_received += 1
        try:
            self.queue.put_nowait((dict(runtime), dict(decision), signal_bar))
        except Full:
            self.decisions_dropped += 1
            self.runtimes.update_decision_execution(
                str(decision["decision_id"]), str(runtime["owner_user_id"]),
                {"execution_status": "shadow_rejected", "execution_reason": "shadow_queue_full"},
            )

    def _process(
        self,
        runtime: Mapping[str, object],
        decision: Mapping[str, object],
        signal_bar: KBar,
    ) -> None:
        owner_id = str(runtime["owner_user_id"])
        current = self.runtimes.trading_runtime(str(runtime["runtime_id"]), owner_id)
        if current is None or current.get("status") != "active":
            return
        broker_name = str(current.get("broker_name") or "")
        account_id = str(current.get("account_id") or "")
        if not broker_name or not account_id:
            self.runtimes.update_decision_execution(
                str(decision["decision_id"]), owner_id,
                {"execution_status": "shadow_rejected", "execution_reason": "target_unavailable"},
            )
            return
        reference = float(decision.get("reference_price") or signal_bar.close)
        direction = str(decision["direction"])
        candidate = LiveExecutionCandidate(
            owner_id=owner_id,
            runtime_id=str(current["runtime_id"]),
            decision_id=str(decision["decision_id"]),
            target=BrokerAccountRef(broker_name, account_id),
            strategy_id=str(current["strategy_id"]),
            strategy_version=int(current.get("strategy_version") or 1),
            symbol=str(current["symbol"]),
            contract=str(decision["contract"]),
            trigger_time=datetime.fromisoformat(str(decision["trigger_time"])),
            action=str(decision["action"]),  # type: ignore[arg-type]
            direction=direction,  # type: ignore[arg-type]
            quantity=int(current["quantity"]),
            reason=str(decision["reason"]),
            planned_stop_price=(
                None if decision["action"] == "exit"
                else _risk_stop(current, direction, reference)
            ),
            session=signal_bar.session,
        )
        try:
            result = self.shadow.evaluate(candidate)
        except Exception:
            status, reason, shadow_id = (
                "shadow_rejected", "shadow_evaluation_failed", None
            )
        else:
            status = (
                "shadow_would_submit"
                if result.result == "would_submit"
                else "shadow_rejected"
            )
            reason = result.reason
            shadow_id = result.shadow_id
        self.runtimes.update_decision_execution(
            str(decision["decision_id"]), owner_id,
            {
                "execution_status": status,
                "execution_reason": reason,
                "order_id": shadow_id,
                "planned_stop_price": candidate.planned_stop_price,
            },
        )

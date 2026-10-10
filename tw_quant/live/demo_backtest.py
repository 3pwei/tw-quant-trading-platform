"""Generic Demo application port with no product cases or strategy defaults."""
from __future__ import annotations

from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass
from threading import BoundedSemaphore, Lock
from time import monotonic
from typing import Literal, Mapping, Protocol, Sequence

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel
from tw_quant_core.strategy import (
    CompositeAnalysisRequest,
    StrategyEvaluationRequest,
)

from ..strategy_registry import RegistryStrategies, StrategyUnavailable


MAX_CASES = 100
MAX_REQUESTS_PER_MINUTE = 10
MAX_CONCURRENT_RUNS = 2
TIMEOUT_SECONDS = 5
PUBLIC_CATALOG_KEYS = frozenset({
    "case_id", "id", "name", "label", "summary", "description",
    "synthetic_data", "simulated_trades", "performance_claim",
})
PRIVATE_KEY_PARTS = (
    "parameter", "member", "composition", "recipe", "diagnostic",
    "strategy_snapshot", "plugin_identity", "provider_factory", "debug", "trace",
)
PUBLIC_DEMO_CONFIG_KEYS = frozenset({
    "initial_capital", "commission_per_side", "slippage_points", "contract_multiplier",
})


def _private_key(key: object) -> bool:
    normalized = str(key).strip().casefold()
    return any(part in normalized for part in PRIVATE_KEY_PARTS)


def _redact_demo_value(value: object) -> object:
    """Remove private strategy material at the anonymous Demo boundary.

    Admin strategy/settings endpoints use their existing authenticated response
    models and do not pass through this function.
    """
    encoded = jsonable_encoder(value)
    if isinstance(encoded, dict):
        result = {}
        for key, item in encoded.items():
            if str(key).casefold() == "config" and isinstance(item, dict):
                result[key] = {
                    name: _redact_demo_value(value)
                    for name, value in item.items()
                    if name in PUBLIC_DEMO_CONFIG_KEYS
                }
                continue
            if _private_key(key):
                # Keep the stable public UI shape without exposing values.
                if str(key).casefold() in {"parameters", "diagnostics"}:
                    result[key] = [] if isinstance(item, list) else {}
                continue
            result[key] = _redact_demo_value(item)
        return result
    if isinstance(encoded, list):
        return [_redact_demo_value(item) for item in encoded]
    return encoded


def _public_catalog_item(item: Mapping[str, object]) -> dict[str, object]:
    return {
        key: _redact_demo_value(value)
        for key, value in item.items()
        if key in PUBLIC_CATALOG_KEYS
    }


@dataclass(frozen=True)
class DemoExecutionInput:
    case_id: str
    operation: Literal["analyze", "composite_analyze"]
    request: StrategyEvaluationRequest | CompositeAnalysisRequest


class DemoProvider(Protocol):
    """Runtime-owned Demo catalog and exact Core execution inputs."""

    def case_catalog(self) -> Sequence[Mapping[str, object]]:
        ...

    def case_execution_input(self, case_id: str) -> DemoExecutionInput:
        ...


class DemoRequest(BaseModel):
    case_id: str


class DemoService:
    def __init__(
        self,
        provider: DemoProvider | None,
        strategies: RegistryStrategies,
    ):
        self.provider = provider
        self.strategies = strategies
        self._executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_RUNS)
        self._slots = BoundedSemaphore(MAX_CONCURRENT_RUNS)
        self._requests: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def close(self):
        self._executor.shutdown(wait=True, cancel_futures=True)

    def _provider(self) -> DemoProvider:
        if self.provider is None:
            raise StrategyUnavailable("demo provider is unavailable")
        return self.provider

    def _limit(self, actor: str) -> None:
        now = monotonic()
        with self._lock:
            entries = self._requests[actor]
            while entries and now - entries[0] >= 60:
                entries.popleft()
            if len(entries) >= MAX_REQUESTS_PER_MINUTE:
                raise HTTPException(429, "demo rate limit exceeded")
            entries.append(now)

    def catalog(self, actor: str) -> dict[str, object]:
        self._limit(actor)
        cases = tuple(self._provider().case_catalog())
        if len(cases) > MAX_CASES:
            raise StrategyUnavailable("demo catalog exceeds platform limit")
        identifiers = []
        payload = []
        for item in cases:
            if not isinstance(item, Mapping):
                raise StrategyUnavailable("demo catalog contract is invalid")
            case_id = str(item.get("case_id", "")).strip()
            if not case_id:
                raise StrategyUnavailable("demo case identity is invalid")
            identifiers.append(case_id)
            payload.append(_public_catalog_item(item))
        if len(identifiers) != len(set(identifiers)):
            raise StrategyUnavailable("duplicate demo case identity")
        return {"cases": payload}

    def execute(self, case_id: str, actor: str) -> dict[str, object]:
        self._limit(actor)
        normalized = case_id.strip()
        if not normalized:
            raise HTTPException(422, "case_id is required")
        if not self._slots.acquire(blocking=False):
            raise HTTPException(503, "demo busy")
        future = self._executor.submit(self._execute, normalized)
        future.add_done_callback(lambda _: self._slots.release())
        try:
            return future.result(timeout=TIMEOUT_SECONDS)
        except TimeoutError:
            future.cancel()
            raise HTTPException(503, "demo timed out") from None

    def _execute(self, case_id: str) -> dict[str, object]:
        execution = self._provider().case_execution_input(case_id)
        if not isinstance(execution, DemoExecutionInput):
            raise StrategyUnavailable("demo execution input is invalid")
        if execution.case_id != case_id:
            raise StrategyUnavailable("demo result identity mismatch")
        if (
            execution.operation == "analyze"
            and isinstance(execution.request, StrategyEvaluationRequest)
        ):
            analysis = self.strategies.analyze_request(execution.request)
        elif (
            execution.operation == "composite_analyze"
            and isinstance(execution.request, CompositeAnalysisRequest)
        ):
            analysis = self.strategies.analyze_composite(execution.request)
        else:
            raise StrategyUnavailable("demo operation/schema is unavailable")
        return _redact_demo_value({"case_id": case_id, "analysis": analysis})


def build_demo_router(
    provider: DemoProvider | None,
    strategies: RegistryStrategies,
) -> APIRouter:
    service = DemoService(provider, strategies)
    router = APIRouter(prefix="/api/demo", tags=["demo"])

    @router.get("/cases")
    def cases(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        actor = request.client.host if request.client else "unknown"
        return service.catalog(actor)

    @router.post("/backtests")
    def backtest(payload: DemoRequest, request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        actor = request.client.host if request.client else "unknown"
        return service.execute(payload.case_id, actor)

    return router

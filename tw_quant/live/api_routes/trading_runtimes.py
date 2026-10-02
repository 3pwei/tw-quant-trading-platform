from __future__ import annotations

from fastapi import APIRouter, Query, Request

from ..api_context import ApiDependencies
from ..api_errors import application_http_error
from ..api_models import TradingRuntimeCreate
from ..application import ApplicationError


def build_trading_runtime_router(deps: ApiDependencies) -> APIRouter:
    router = APIRouter()

    def owner_id(request: Request) -> str:
        return deps.request_owner_id(request)

    @router.get("/api/trading-runtimes")
    def runtimes(request: Request):
        return deps.runtime_app.list(owner_id(request))

    @router.post("/api/trading-runtimes", status_code=201)
    def create_runtime(update: TradingRuntimeCreate, request: Request):
        try:
            return deps.runtime_app.create(
                update.model_dump(), owner_id(request)
            )
        except ApplicationError as exc:
            raise application_http_error(exc) from exc

    @router.get("/api/trading-runtimes/{runtime_id}")
    def runtime(runtime_id: str, request: Request):
        try:
            return deps.runtime_app.get(runtime_id, owner_id(request))
        except ApplicationError as exc:
            raise application_http_error(exc) from exc

    @router.post("/api/trading-runtimes/{runtime_id}/stop")
    def stop_runtime(runtime_id: str, request: Request):
        try:
            return deps.runtime_app.stop(runtime_id, owner_id(request))
        except ApplicationError as exc:
            raise application_http_error(exc) from exc

    @router.post("/api/trading-runtimes/{runtime_id}/arm")
    def arm_runtime(runtime_id: str, request: Request):
        try:
            return deps.runtime_app.arm(runtime_id, owner_id(request))
        except ApplicationError as exc:
            raise application_http_error(exc) from exc

    @router.post("/api/trading-runtimes/{runtime_id}/pause")
    def pause_runtime(runtime_id: str, request: Request):
        try:
            return deps.runtime_app.pause(runtime_id, owner_id(request))
        except ApplicationError as exc:
            raise application_http_error(exc) from exc

    @router.get("/api/trading-runtimes/{runtime_id}/decisions")
    def decisions(
        runtime_id: str,
        request: Request,
        limit: int = Query(200, ge=1, le=1000),
    ):
        try:
            return deps.runtime_app.decisions(
                runtime_id, owner_id(request), limit
            )
        except ApplicationError as exc:
            raise application_http_error(exc) from exc

    @router.get("/api/live-shadow")
    def live_shadow(request: Request, limit: int = Query(100, ge=1, le=500)):
        owner = owner_id(request)
        return {
            "mode": "live_shadow",
            "ordering_enabled": False,
            "results": deps.shadow_store.list_owner(owner, limit),
            "metrics": {
                **deps.shadow_service.metrics(owner),
                **deps.runtime_app.paper_shadow_metrics(owner),
            },
            "kill_switch_preview": deps.shadow_store.public_kill_switch_preview(
                owner, deps.shadow_targets.targets(owner)
            ),
        }

    @router.get("/api/live-shadow/targets")
    def live_shadow_targets(_request: Request):
        return {
            "targets": deps.shadow_targets.public_targets(owner_id(_request)),
            "ordering_enabled": False,
        }

    return router

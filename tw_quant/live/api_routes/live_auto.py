from fastapi import APIRouter, HTTPException, Request

from ..api_context import ApiDependencies
from ..api_models import LiveAutoArmRequest


def _public_runtime(value: dict[str, object]) -> dict[str, object]:
    result = dict(value)
    result.pop("broker_name", None)
    result.pop("account_id", None)
    result["provider_label"] = "configured"
    return result


def build_live_auto_router(deps: ApiDependencies) -> APIRouter:
    router = APIRouter()

    @router.get("/api/live-auto")
    def status(request: Request):
        owner = deps.request_owner_id(request)
        runtimes = [item for item in deps.runtime_app.list(owner)["runtimes"] if item["mode"] == "live_auto"]
        statuses = [deps.live_auto.status(item) for item in runtimes] if deps.live_auto else runtimes
        return {"enabled": deps.config.live_auto_enabled, "real_money": True,
                "runtimes": [_public_runtime(item) for item in statuses]}

    @router.post("/api/trading-runtimes/{runtime_id}/live-auto/arm")
    def arm(runtime_id: str, update: LiveAutoArmRequest, request: Request):
        if deps.live_auto is None:
            raise HTTPException(503, "live_auto is disabled")
        return _public_runtime(deps.live_auto.arm(runtime_id, deps.request_owner_id(request), update.confirmation))

    @router.post("/api/trading-runtimes/{runtime_id}/live-auto/disarm")
    def disarm(runtime_id: str, request: Request):
        if deps.live_auto is None:
            raise HTTPException(503, "live_auto is disabled")
        return _public_runtime(deps.live_auto.disarm(runtime_id, deps.request_owner_id(request)))

    @router.post("/api/trading-runtimes/{runtime_id}/live-auto/stop")
    def stop(runtime_id: str, request: Request):
        if deps.live_auto is None:
            raise HTTPException(503, "live_auto is disabled")
        return _public_runtime(deps.live_auto.stop(runtime_id, deps.request_owner_id(request)))

    return router

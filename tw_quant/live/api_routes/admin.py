from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from ...auth import AccessRequestStatus, AccountStatus, Role
from ..api_context import ApiDependencies
from ..api_models import AdminUserCreate, AdminUserUpdate


def system_status(
    market: dict[str, object], paper: dict[str, object],
    host: dict[str, object] | None = None,
    automation: dict[str, object] | None = None,
) -> str:
    market_status = str(market["service_status"])
    if market_status in {"provider_disconnected", "market_stale"}:
        return market_status
    if int(paper.get("active_kill_switches", 0)) > 0 or int(paper.get("inconsistent_owners", 0)) > 0:
        return "trading_halted"
    if market_status != "healthy" or paper.get("status") != "healthy":
        return "degraded"
    if automation and int(automation.get("recovery_locked_runtimes", 0)) > 0:
        return "degraded"
    if host and any(
        isinstance(host.get(key), (int, float)) and float(host[key]) >= 90
        for key in ("cpu_percent", "memory_percent", "disk_percent")
    ):
        return "degraded"
    return "healthy"


def _public_live_execution(snapshot: dict[str, object]) -> dict[str, object]:
    result = dict(snapshot)
    accounts = []
    for value in snapshot.get("broker_accounts", []):
        account = dict(value)
        account.pop("broker_name", None)
        account.pop("account_id", None)
        account["provider_label"] = "configured"
        accounts.append(account)
    result["broker_accounts"] = accounts
    return result


def build_admin_router(deps: ApiDependencies) -> APIRouter:
    router = APIRouter()

    @router.get("/api/admin/health")
    async def admin_health():
        market = deps.service.status_message()
        paper_health = deps.paper.health()
        automation = {
            **deps.runtime_app.health(),
            "gap_risk_rejected": paper_health.get("gap_risk_rejected", 0),
            "last_auto_order_time": paper_health.get("last_auto_order_time"),
        }
        host = deps.host_monitor.snapshot()
        return {
            **market,
            "system_status": system_status(
                market, paper_health, host, automation
            ),
            "paper_trading": paper_health,
            "automated_trading": automation,
            "host": host,
            "rate_limiting": deps.limiter.stats(),
            "request_limits": {"max_body_bytes": deps.config.max_request_body_bytes},
            "live_execution": _public_live_execution(deps.execution_worker.snapshot()),
        }

    @router.get("/api/admin/access-requests")
    def admin_access_requests(
        status: AccessRequestStatus | None = AccessRequestStatus.PENDING,
    ):
        return {"requests": [item.to_message() for item in deps.identity_repo.access_requests(status)]}

    @router.post("/api/admin/access-requests/{request_id}/approve")
    def approve_access_request(request_id: str, request: Request):
        try:
            access_request, user = deps.identity_repo.approve_access_request(
                request_id, actor_user_id=request.state.auth_user.user_id
            )
        except ValueError as exc:
            raise HTTPException(status_code=404 if "not found" in str(exc) else 409, detail=str(exc)) from exc
        return {"request": access_request.to_message(), "user": user.to_message(deps.auth_service.enforced)}

    @router.post("/api/admin/access-requests/{request_id}/reject")
    def reject_access_request(request_id: str, request: Request):
        try:
            access_request = deps.identity_repo.reject_access_request(
                request_id, actor_user_id=request.state.auth_user.user_id
            )
        except ValueError as exc:
            raise HTTPException(status_code=404 if "not found" in str(exc) else 409, detail=str(exc)) from exc
        return {"request": access_request.to_message()}

    @router.get("/api/admin/users")
    def admin_users():
        return {"users": [user.to_message(deps.auth_service.enforced) for user in deps.identity_repo.users()]}

    @router.post("/api/admin/users", status_code=201)
    def create_admin_user(update: AdminUserCreate, request: Request):
        try:
            user = deps.identity_repo.create_user(
                update.email, role=update.role, status=update.status,
                trading_mode=update.trading_mode,
                actor_user_id=request.state.auth_user.user_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return user.to_message(deps.auth_service.enforced)

    @router.put("/api/admin/users/{user_id}")
    def update_admin_user(user_id: str, update: AdminUserUpdate, request: Request):
        actor = request.state.auth_user
        if user_id == actor.user_id and (
            update.status is not AccountStatus.ACTIVE or update.role is not Role.ADMIN
        ):
            raise HTTPException(status_code=409, detail="cannot disable or demote your own admin account")
        try:
            user = deps.identity_repo.update_user(
                user_id, role=update.role, status=update.status,
                trading_mode=update.trading_mode, actor_user_id=actor.user_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404 if "not found" in str(exc) else 422, detail=str(exc)) from exc
        return user.to_message(deps.auth_service.enforced)

    @router.get("/api/admin/audit")
    def admin_audit(limit: int = Query(200, ge=1, le=1000)):
        return {"events": deps.identity_repo.audit_events(limit)}

    return router

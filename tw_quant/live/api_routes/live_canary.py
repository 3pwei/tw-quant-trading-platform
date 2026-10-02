from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request

from ...auth import TradingMode
from ..api_context import ApiDependencies
from ..api_models import (
    LiveCanaryActionConfirmation,
    LiveCanaryArmRequest,
    LiveCanaryKillSwitchRequest,
    ManualLiveOrderCreate,
)


def _service(deps: ApiDependencies):
    if deps.live_canary is None:
        raise HTTPException(503, "live_canary_disabled")
    return deps.live_canary


def _public_status(value: dict[str, object]) -> dict[str, object]:
    result = dict(value)
    result.pop("broker_name", None)
    result.pop("account_id", None)
    result["provider_label"] = "configured"
    return result


def _owner(request: Request, deps: ApiDependencies) -> str:
    user = request.state.auth_user
    if not user.registered or user.trading_mode is not TradingMode.LIVE:
        raise HTTPException(403, "live_trading_mode_required")
    return deps.request_owner_id(request)


def _order(order, created: bool | None = None) -> dict[str, object]:
    value: dict[str, object] = {
        "platform_order_id": order.request.client_order_id,
        "broker_order_id": order.broker_order_id,
        "side": order.request.side,
        "requested_quantity": order.request.quantity,
        "filled_quantity": order.filled_quantity,
        "order_type": order.request.order_type,
        "time_in_force": order.request.time_in_force,
        "limit_price": order.request.limit_price,
        "status": order.status.value,
        "status_reason": order.status_reason,
        "source": order.request.source,
        "updated_at": order.updated_at.isoformat(timespec="milliseconds"),
    }
    if created is not None:
        value["created"] = created
    if order.status.value == "unknown":
        value["operator_warning"] = "DO NOT RETRY; RECONCILIATION REQUIRED"
    return value


def build_live_canary_router(deps: ApiDependencies) -> APIRouter:
    router = APIRouter()

    @router.get("/api/live/canary")
    def status(request: Request):
        owner = _owner(request, deps)
        service = _service(deps)
        result = service.status(owner)
        result["orders"] = [
            _order(item)
            for item in service.sink.manager.repository.orders(owner)
            if item.request.source == "manual_live_canary"
        ]
        return _public_status(result)

    @router.post("/api/live/canary/arm")
    def arm(payload: LiveCanaryArmRequest, request: Request):
        owner = _owner(request, deps)
        try:
            session = _service(deps).arm(owner, owner, payload.confirmation)
        except (KeyError, RuntimeError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        return {
            "arm": "active",
            "arm_id": session.arm_id,
            "expires_at": session.expires_at.isoformat(),
            "provider_label": "configured",
            "masked_account_id": "****" + session.account_ref.account_id[-4:],
        }

    @router.delete("/api/live/canary/arm")
    def disarm(request: Request):
        owner = _owner(request, deps)
        return {"arm": "off", "changed": _service(deps).disarm(owner)}

    @router.post("/api/live/orders", status_code=202)
    def create_order(
        payload: ManualLiveOrderCreate,
        request: Request,
        idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
    ):
        owner = _owner(request, deps)
        try:
            order, created = _service(deps).reserve_order(
                owner_id=owner,
                side=payload.side,
                quantity=payload.quantity,
                idempotency_key=idempotency_key,
                confirmation=payload.confirmation,
            )
        except (KeyError, RuntimeError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        return _order(order, created)

    @router.post("/api/live/orders/{client_order_id}/cancel", status_code=202)
    def cancel_order(
        client_order_id: str,
        payload: LiveCanaryActionConfirmation,
        request: Request,
    ):
        del payload
        owner = _owner(request, deps)
        try:
            order, created = _service(deps).reserve_cancel(owner, client_order_id)
        except (KeyError, RuntimeError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        return _order(order, created)

    @router.post("/api/live/position/close", status_code=202)
    def close_position(
        payload: LiveCanaryActionConfirmation,
        request: Request,
        idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
    ):
        del payload
        owner = _owner(request, deps)
        try:
            order, created = _service(deps).reserve_close(owner, idempotency_key)
        except (KeyError, RuntimeError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        return _order(order, created)

    @router.post("/api/live/canary/kill-switch", status_code=202)
    def kill_switch(payload: LiveCanaryKillSwitchRequest, request: Request):
        owner = _owner(request, deps)
        try:
            return _service(deps).activate_kill_switch(
                owner, payload.action, payload.reason
            )
        except (KeyError, RuntimeError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc

    return router

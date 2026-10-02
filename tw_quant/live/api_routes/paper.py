from __future__ import annotations

from fastapi import APIRouter, Header, Query, Request

from ..api_context import ApiDependencies
from ..api_errors import application_http_error
from ..api_models import PaperControlRequest, PaperOrderCreate
from ..application import ApplicationError, PaperOrderInput


def build_paper_router(deps: ApiDependencies) -> APIRouter:
    router = APIRouter()

    @router.get("/api/paper/account")
    def paper_account(request: Request):
        return deps.paper_app.account(request.state.auth_user.user_id)

    @router.get("/api/paper/orders")
    def paper_orders(request: Request):
        return deps.paper_app.orders(request.state.auth_user.user_id)

    @router.get("/api/paper/fills")
    def paper_fills(
        request: Request, limit: int = Query(200, ge=1, le=1000)
    ):
        return deps.paper_app.fills(request.state.auth_user.user_id, limit)

    @router.get("/api/paper/events")
    def paper_events(
        request: Request, limit: int = Query(200, ge=1, le=1000)
    ):
        return deps.paper_app.events(request.state.auth_user.user_id, limit)

    @router.post("/api/paper/orders", status_code=201)
    def create_paper_order(
        payload: PaperOrderCreate,
        request: Request,
        idempotency_key: str = Header(
            ...,
            alias="Idempotency-Key",
            min_length=1,
            max_length=128,
        ),
    ):
        try:
            return deps.paper_app.submit(
                request.state.auth_user,
                PaperOrderInput(
                    client_order_id=idempotency_key,
                    strategy_id=payload.strategy_id,
                    strategy_version=payload.strategy_version,
                    side=payload.side,
                    quantity=payload.quantity,
                    stop_loss_price=payload.stop_loss_price,
                    reduce_only=payload.reduce_only,
                ),
            )
        except ApplicationError as exc:
            raise application_http_error(exc) from exc

    @router.post("/api/paper/kill-switch")
    def activate_paper_kill_switch(
        payload: PaperControlRequest, request: Request
    ):
        try:
            return deps.paper_app.activate_kill_switch(
                request.state.auth_user.user_id, payload.reason
            )
        except ApplicationError as exc:
            raise application_http_error(exc) from exc

    @router.post("/api/paper/kill-switch/reset")
    def reset_paper_kill_switch(
        payload: PaperControlRequest, request: Request
    ):
        try:
            return deps.paper_app.reset_kill_switch(
                request.state.auth_user.user_id, payload.reason
            )
        except ApplicationError as exc:
            raise application_http_error(exc) from exc

    return router

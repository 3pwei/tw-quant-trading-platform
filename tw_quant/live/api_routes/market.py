from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect

from ...auth import AccessTokenError, AuthorizationError
from ...market import TimeframeStreamAggregator, aggregate_kbars, kbar_from_message, source_bar_limit, validate_timeframe
from ...strategy_registry import supported_strategies, analyze_strategies
from ..api_context import ApiDependencies


def build_market_router(deps: ApiDependencies) -> APIRouter:
    router = APIRouter()

    @router.get("/api/health")
    async def health():
        return deps.public_market_status(deps.service.status_message())

    @router.get("/api/kbars")
    async def kbars(symbol: str = "TMF", interval: str = "1m", limit: int = Query(500, ge=1, le=5000)):
        if symbol.upper() != deps.config.symbol:
            raise HTTPException(status_code=404, detail="unsupported symbol")
        try:
            selected_interval = validate_timeframe(interval)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        source_limit = source_bar_limit(selected_interval, limit, deps.config.history_limit)
        return [
            bar.to_message(deps.service.connection_status, selected_interval)
            for bar in aggregate_kbars(deps.market_repo.latest(deps.config.symbol, source_limit), selected_interval, limit)
        ]

    @router.get("/api/strategy-signals")
    async def strategy_signals(
        request: Request, symbol: str = "TMF", strategies: str | None = None,
        interval: str = "1m", limit: int = Query(500, ge=20, le=5000),
    ):
        if symbol.upper() != deps.config.symbol:
            raise HTTPException(status_code=404, detail="unsupported symbol")
        selected = list(supported_strategies()[:2]) if strategies is None else [value.strip().lower() for value in strategies.split(",") if value.strip()]
        if not selected:
            return {"strategies": []}
        unsupported = sorted(set(selected) - set(supported_strategies()))
        if unsupported:
            raise HTTPException(status_code=400, detail=f"unsupported strategies: {', '.join(unsupported)}")
        try:
            selected_interval = validate_timeframe(interval)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        source_limit = source_bar_limit(selected_interval, limit, deps.config.history_limit)
        return analyze_strategies(
            aggregate_kbars(deps.market_repo.latest(deps.config.symbol, source_limit), selected_interval, limit),
            selected, parameters=deps.strategy_repo.strategy_parameters(deps.request_owner_id(request)), interval=selected_interval,
        )

    @router.websocket("/ws/market/{symbol}")
    async def market_socket(websocket: WebSocket, symbol: str, interval: str = "1m"):
        try:
            user = deps.user_from_headers(websocket.headers)
            deps.auth_service.require_permission(user, "market.read")
        except (AccessTokenError, AuthorizationError) as exc:
            await websocket.close(code=1008, reason=str(exc))
            return
        if symbol.upper() != deps.config.symbol:
            await websocket.close(code=1008, reason="unsupported symbol")
            return
        try:
            selected_interval = validate_timeframe(interval)
        except ValueError:
            await websocket.close(code=1008, reason="unsupported interval")
            return
        await websocket.accept()
        queue = deps.service.hub.subscribe()
        transformer = TimeframeStreamAggregator(
            selected_interval, deps.market_repo.latest(deps.config.symbol, deps.config.history_limit)
        )
        await websocket.send_json(deps.public_market_status(deps.service.status_message()))
        try:
            while True:
                message = await queue.get()
                if message.get("type") != "kbar":
                    await websocket.send_json(deps.public_market_status(message))
                    continue
                for bar in transformer.push(kbar_from_message(message)):
                    await websocket.send_json(bar.to_message(deps.service.connection_status, selected_interval))
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            deps.service.hub.unsubscribe(queue)

    return router

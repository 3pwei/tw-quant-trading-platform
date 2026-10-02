from __future__ import annotations

from datetime import date
from typing import Literal

from fastapi import APIRouter, Header, Query, Request

from ...strategy_registry import supported_strategies
from ..api_context import ApiDependencies
from ..api_errors import application_http_error
from ..api_models import (
    BacktestExecutionRequest,
    BacktestRunPurge,
    PaperOrderCreate,
    ReplayCursorUpdate,
    ReplayPrepareRequest,
)
from ..application import (
    ApplicationError,
    BacktestInput,
    ReplayOrderInput,
    ReplayPrepareInput,
)


def build_research_router(deps: ApiDependencies) -> APIRouter:
    router = APIRouter()

    def owner_id(request: Request) -> str:
        return deps.request_owner_id(request)

    @router.get("/api/backtest/options")
    def backtest_options(request: Request, symbol: str = "TMF"):
        try:
            return deps.research_app.backtest_options(
                owner_id(request), symbol
            )
        except ApplicationError as exc:
            raise application_http_error(exc) from exc

    @router.get("/api/replay/options")
    def replay_options(request: Request, symbol: str = "TMF"):
        try:
            return deps.research_app.replay_options(owner_id(request), symbol)
        except ApplicationError as exc:
            raise application_http_error(exc) from exc

    @router.post("/api/replay/prepare")
    def prepare_replay(payload: ReplayPrepareRequest, request: Request):
        try:
            return deps.research_app.prepare_replay(
                ReplayPrepareInput(
                    symbol=payload.symbol,
                    trading_date=payload.trading_date,
                    session=payload.session,
                    interval=payload.interval,
                    strategies=tuple(payload.strategies),
                ),
                owner_id(request),
                request.state.auth_user,
            )
        except ApplicationError as exc:
            raise application_http_error(exc) from exc

    @router.get("/api/replay/sessions/{session_id}")
    def get_replay_session(session_id: str, request: Request):
        try:
            return deps.research_app.replay_state(
                session_id, owner_id(request)
            )
        except ApplicationError as exc:
            raise application_http_error(exc) from exc

    @router.put("/api/replay/sessions/{session_id}/cursor")
    def update_replay_cursor(
        session_id: str,
        payload: ReplayCursorUpdate,
        request: Request,
    ):
        try:
            return deps.research_app.seek_replay(
                session_id, payload.cursor, owner_id(request)
            )
        except ApplicationError as exc:
            raise application_http_error(exc) from exc

    @router.post("/api/replay/sessions/{session_id}/orders", status_code=201)
    def create_replay_order(
        session_id: str,
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
            return deps.research_app.submit_replay_order(
                session_id,
                ReplayOrderInput(
                    strategy_id=payload.strategy_id,
                    strategy_version=payload.strategy_version,
                    side=payload.side,
                    quantity=payload.quantity,
                    stop_loss_price=payload.stop_loss_price,
                    reduce_only=payload.reduce_only,
                ),
                idempotency_key,
                owner_id(request),
            )
        except ApplicationError as exc:
            raise application_http_error(exc) from exc

    @router.post("/api/replay/sessions/{session_id}/reset")
    def reset_replay_session(session_id: str, request: Request):
        try:
            return deps.research_app.reset_replay(
                session_id, owner_id(request)
            )
        except ApplicationError as exc:
            raise application_http_error(exc) from exc

    @router.get("/api/backtest")
    def backtest(
        request: Request,
        symbol: str = "TMF",
        strategy: str | None = None,
        interval: str = "1m",
        start: date = Query(...),
        end: date = Query(...),
    ):
        try:
            return deps.research_app.execute_atomic(
                BacktestInput(symbol, strategy if strategy is not None else supported_strategies()[0], interval, start, end),
                owner_id(request),
            )
        except ApplicationError as exc:
            raise application_http_error(exc) from exc

    @router.get("/api/composite-backtest")
    def composite_backtest(
        strategy_id: str,
        request: Request,
        version: int | None = None,
        symbol: str = "TMF",
        start: date = Query(...),
        end: date = Query(...),
    ):
        try:
            return deps.research_app.execute_composite(
                BacktestInput(
                    symbol,
                    strategy_id,
                    "multi",
                    start,
                    end,
                    version,
                ),
                owner_id(request),
            )
        except ApplicationError as exc:
            raise application_http_error(exc) from exc

    @router.post("/api/backtest-runs", status_code=201)
    def create_backtest_run(
        execution: BacktestExecutionRequest, request: Request
    ):
        try:
            return deps.research_app.create_backtest_run(
                BacktestInput(
                    execution.symbol,
                    execution.strategy,
                    execution.interval,
                    execution.start,
                    execution.end,
                    execution.version,
                ),
                owner_id(request),
            )
        except ApplicationError as exc:
            raise application_http_error(exc) from exc

    @router.get("/api/backtest-runs")
    def backtest_runs(
        request: Request,
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
        strategy_key: str | None = None,
        strategy_kind: Literal["atomic", "composite"] | None = None,
        outcome: Literal["traded", "empty"] | None = None,
        search: str | None = Query(None, max_length=80),
    ):
        return deps.research_app.backtest_runs(
            owner_id(request), limit, offset, strategy_key,
            strategy_kind, outcome, search,
        )

    @router.delete("/api/backtest-runs")
    def delete_backtest_runs(purge: BacktestRunPurge, request: Request):
        try:
            return deps.research_app.delete_backtest_runs(
                purge.run_ids, purge.delete_all, owner_id(request),
                purge.exclude_run_ids,
            )
        except ApplicationError as exc:
            raise application_http_error(exc) from exc

    @router.get("/api/backtest-runs/{run_id}")
    def backtest_run(run_id: str, request: Request):
        try:
            return deps.research_app.backtest_run(
                run_id, owner_id(request)
            )
        except ApplicationError as exc:
            raise application_http_error(exc) from exc

    @router.get("/api/backtest-runs/{run_id}/chart")
    def backtest_run_chart(
        run_id: str,
        request: Request,
        trade_index: int = Query(..., ge=0),
    ):
        try:
            return deps.research_app.backtest_run_chart(
                run_id, trade_index, owner_id(request)
            )
        except ApplicationError as exc:
            raise application_http_error(exc) from exc

    @router.delete("/api/backtest-runs/{run_id}")
    def delete_backtest_run(run_id: str, request: Request):
        try:
            return deps.research_app.delete_backtest_run(
                run_id, owner_id(request)
            )
        except ApplicationError as exc:
            raise application_http_error(exc) from exc

    return router

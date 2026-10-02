from __future__ import annotations

from fastapi import APIRouter, Query, Request

from ..api_context import ApiDependencies
from ..api_errors import application_http_error
from ..api_models import (
    CompositeStrategyPurge,
    CompositeStrategyUpdate,
    StrategyParametersUpdate,
)
from ..application import ApplicationError


def build_strategy_router(deps: ApiDependencies) -> APIRouter:
    router = APIRouter()

    def owner_id(request: Request) -> str:
        return deps.request_owner_id(request)

    @router.get("/api/strategies")
    def strategies_catalog(request: Request):
        return deps.strategy_app.catalog(owner_id(request))

    @router.put("/api/strategies/{strategy}")
    def update_strategy_parameters(
        strategy: str,
        update: StrategyParametersUpdate,
        request: Request,
    ):
        try:
            return deps.strategy_app.update_parameters(
                strategy, update.parameters, owner_id(request)
            )
        except ApplicationError as exc:
            raise application_http_error(exc) from exc

    @router.get("/api/composite-strategies")
    def composite_strategies(request: Request):
        return deps.strategy_app.composites(owner_id(request))

    @router.get("/api/composite-strategies/{strategy_id}/versions")
    def composite_strategy_versions(strategy_id: str, request: Request):
        try:
            return deps.strategy_app.composite_versions(
                strategy_id, owner_id(request)
            )
        except ApplicationError as exc:
            raise application_http_error(exc) from exc

    @router.get("/api/composite-strategies/{strategy_id}")
    def composite_strategy(
        strategy_id: str,
        request: Request,
        version: int | None = None,
    ):
        try:
            return deps.strategy_app.composite(
                strategy_id, version, owner_id(request)
            )
        except ApplicationError as exc:
            raise application_http_error(exc) from exc

    @router.get("/api/composite-strategy-signals/{strategy_id}")
    def composite_strategy_signals(
        strategy_id: str,
        request: Request,
        version: int | None = None,
        symbol: str = "TMF",
        limit: int = Query(5000, ge=20, le=5000),
    ):
        try:
            return deps.strategy_app.composite_signals(
                strategy_id,
                version,
                symbol,
                limit,
                owner_id(request),
            )
        except ApplicationError as exc:
            raise application_http_error(exc) from exc

    @router.post("/api/composite-strategies", status_code=201)
    def create_composite_strategy(
        update: CompositeStrategyUpdate, request: Request
    ):
        try:
            return deps.strategy_app.create_composite(
                update.definition, owner_id(request)
            )
        except ApplicationError as exc:
            raise application_http_error(exc) from exc

    @router.put("/api/composite-strategies/{strategy_id}")
    def update_composite_strategy(
        strategy_id: str,
        update: CompositeStrategyUpdate,
        request: Request,
    ):
        try:
            return deps.strategy_app.update_composite(
                strategy_id, update.definition, owner_id(request)
            )
        except ApplicationError as exc:
            raise application_http_error(exc) from exc

    @router.delete("/api/composite-strategies/{strategy_id}")
    def archive_composite_strategy(strategy_id: str, request: Request):
        try:
            return deps.strategy_app.archive_composite(
                strategy_id, owner_id(request)
            )
        except ApplicationError as exc:
            raise application_http_error(exc) from exc

    @router.post("/api/composite-strategies/purge")
    def purge_composite_strategies(
        purge: CompositeStrategyPurge, request: Request
    ):
        try:
            return deps.strategy_app.purge_composites(
                purge.strategy_ids, owner_id(request)
            )
        except ApplicationError as exc:
            raise application_http_error(exc) from exc

    return router

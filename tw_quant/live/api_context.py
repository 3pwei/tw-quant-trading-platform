from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from ..auth import AccessIdentity, AccessTokenError, AccessValidator, AuthService, SQLiteAuthRepository
from ..broker import ExecutionWorkerMonitor
from ..paper import PaperTradingService
from ..replay import ReplayTradingSessionRegistry
from .application import (
    PaperApplicationService,
    ResearchApplicationService,
    StrategyApplicationService,
    TradingRuntimeApplicationService,
)
from .monitoring import HostResourceMonitor
from .shadow_context import LiveExecutionTargetCatalog
from .shadow_store import SQLiteShadowExecutionRepository
from ..execution.shadow import ShadowExecutionService
from .application.live_canary import ManualLiveCanaryService
from .application.live_auto import LiveAutoService
from .rate_limit import SlidingWindowRateLimiter
from .service import LiveMarketService
from .settings import LiveSettings
from .storage import DEFAULT_OWNER_ID, MarketRepository, StrategyRepository


@dataclass(frozen=True)
class ApiDependencies:
    config: LiveSettings
    market_repo: MarketRepository
    strategy_repo: StrategyRepository
    validator: AccessValidator
    identity_repo: SQLiteAuthRepository
    auth_service: AuthService
    service: LiveMarketService
    paper: PaperTradingService
    replay_trading: ReplayTradingSessionRegistry
    host_monitor: HostResourceMonitor
    limiter: SlidingWindowRateLimiter
    paper_app: PaperApplicationService
    research_app: ResearchApplicationService
    strategy_app: StrategyApplicationService
    runtime_app: TradingRuntimeApplicationService
    execution_worker: ExecutionWorkerMonitor
    shadow_store: SQLiteShadowExecutionRepository
    shadow_targets: LiveExecutionTargetCatalog
    shadow_service: ShadowExecutionService
    live_canary: ManualLiveCanaryService | None
    live_auto: LiveAutoService | None = None

    def identity_from_headers(self, headers) -> AccessIdentity | None:
        token = headers.get("cf-access-jwt-assertion")
        if self.config.access_mode != "disabled":
            if not token:
                raise AccessTokenError("missing authenticated request identity")
            # Forward-auth headers are client-controllable on a direct API
            # request. The verified assertion alone determines identity.
            return self.validator.authenticate(token)
        return self.validator.authenticate(token) if token else None

    def user_from_headers(self, headers):
        identity = self.identity_from_headers(headers)
        return (
            self.auth_service.local_development_user()
            if identity is None
            else self.auth_service.identify(identity)
        )

    @staticmethod
    def public_market_status(status: dict[str, object]) -> dict[str, object]:
        return {
            key: status[key]
            for key in (
                "type", "service_status", "symbol", "contract", "connection_status",
                "last_tick_time", "last_bar_time", "last_heartbeat_time",
                "server_time", "latency_ms", "market_latency_seconds",
                "tick_age_ms", "tick_age_seconds", "bar_age_seconds",
                "stale_after_seconds", "trading_block_reason",
                "history_bars_loaded",
            )
        }

    @staticmethod
    def request_owner_id(request: Request) -> str:
        user = request.state.auth_user
        return user.user_id if user.registered else DEFAULT_OWNER_ID

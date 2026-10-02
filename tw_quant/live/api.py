from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
import json
import os

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from ..auth import (
    AccessValidator,
    AuthService,
    CloudflareAccessValidator,
    DisabledAccessValidator,
    SQLiteAuthRepository,
)
from ..market import TradingCalendar, SQLiteExecutionQuoteRepository
from ..broker import (
    BrokerRegistration,
    BrokerRegistry,
    BrokerRuntimeState,
    CanaryOrderAdmissionGate,
    CompositeOrderAdmissionGate,
    LiveCanaryConfig,
    LiveOrderManager,
    LockedBroker,
    LockedInstrumentMapper,
    SHIOAJI_TECHNICAL_CAPABILITIES,
    SHIOAJI_CANARY_CAPABILITIES,
    SQLiteCanaryArmRepository,
    SQLiteBrokerTruthRepository,
    SQLiteLiveOrderRepository,
    SQLiteRecoveryLockRepository,
    SQLitePositionGuardianRepository,
    SQLiteExecutionTargetRepository,
    OwnedExecutionTargetCatalog,
)
from ..execution.live_models import InstrumentSpec
from ..execution.live_policy import MarketableLimitIOCPolicy
from ..execution.canary import LiveExecutionSink, StrategyLiveExecutionSink
from ..execution.guardian import GuardianExecutionSink, LivePositionGuardian, LivePositionGuardianConfig, default_guardian_policies
from ..execution.shadow import ShadowExecutionService
from ..risk import LiveRiskConfig, LiveRiskService
from ..risk.live import LiveKillSwitchAction, LiveKillSwitchScope
from ..market_data import HistoricalMarketDataProvider, LiveMarketDataProvider, build_market_data_provider
from ..paper import PaperTradingService, SQLitePaperRepository
from ..replay import ReplayTradingSessionRegistry
from .api_context import ApiDependencies
from .application import (
    PaperApplicationService,
    PaperAutoExecutionController,
    ResearchApplicationService,
    StrategyApplicationService,
    TradingRuntimeApplicationService,
    LiveShadowExecutionController,
    ManualLiveCanaryService,
    LiveAutoService,
)
from .api_models import (
    AdminUserCreate,
    AdminUserUpdate,
    BacktestExecutionRequest,
    BacktestRunPurge,
    CompositeStrategyPurge,
    CompositeStrategyUpdate,
    PaperControlRequest,
    PaperOrderCreate,
    ReplayCursorUpdate,
    ReplayPrepareRequest,
    StrategyParametersUpdate,
    TradingRuntimeCreate,
)
from .api_routes import (
    build_admin_router,
    build_market_router,
    build_paper_router,
    build_research_router,
    build_strategy_router,
    build_system_router,
    build_trading_runtime_router,
    build_live_canary_router,
    build_live_auto_router,
)
from .api_routes.admin import system_status
from .api_security import (
    install_authorization_middleware,
    rate_limit_scope as _rate_limit_scope,
)
from .monitoring import ExecutionHealthFileMonitor, HostResourceMonitor
from .rate_limit import RateLimitRule, SlidingWindowRateLimiter
from .request_limit import RequestBodyLimitMiddleware
from .service import LiveMarketService
from .settings import LiveSettings
from .shadow_context import (
    ConfiguredBrokerCapabilityView,
    ConfiguredLiveAutoContext,
    ConfiguredManualCanaryContext,
    LiveExecutionTargetCatalog,
    LiveShadowRiskContextProvider,
    StaticInstrumentSpecCatalog,
)
from .shadow_store import SQLiteShadowExecutionRepository
from .storage import ApplicationRepository, SQLiteBarRepository

__all__ = [
    "AdminUserCreate", "AdminUserUpdate", "BacktestExecutionRequest",
    "BacktestRunPurge",
    "CompositeStrategyPurge", "CompositeStrategyUpdate", "PaperControlRequest",
    "PaperOrderCreate", "ReplayCursorUpdate", "ReplayPrepareRequest",
    "StrategyParametersUpdate", "_rate_limit_scope", "create_app", "system_status",
    "TradingRuntimeCreate",
]


def _build_access_validator(
    config: LiveSettings, validator: AccessValidator | None
) -> AccessValidator:
    if validator is not None:
        return validator
    if config.access_mode == "cloudflare":
        return CloudflareAccessValidator(
            config.cloudflare_access_team_domain or "",
            config.cloudflare_access_audience or "",
        )
    return DisabledAccessValidator()


def _build_rate_limiter(config: LiveSettings) -> SlidingWindowRateLimiter:
    return SlidingWindowRateLimiter(
        {
            "access_requests": RateLimitRule(
                config.rate_limit_access_requests_per_hour, 60 * 60
            ),
            "backtests": RateLimitRule(
                config.rate_limit_backtests_per_minute, 60
            ),
            "replay_prepares": RateLimitRule(
                config.rate_limit_replay_prepares_per_minute, 60
            ),
            "orders": RateLimitRule(
                config.rate_limit_orders_per_minute, 60
            ),
            "live_orders": RateLimitRule(
                config.live_canary_requests_per_minute, 60
            ),
        }
    )


def create_app(
    settings: LiveSettings | None = None,
    feed: LiveMarketDataProvider | None = None,
    history_provider: HistoricalMarketDataProvider | None = None,
    repository: ApplicationRepository | None = None,
    access_validator: AccessValidator | None = None,
    auth_repository: SQLiteAuthRepository | None = None,
    rate_limiter: SlidingWindowRateLimiter | None = None,
    strategy_registry=None,
    strategy_provider=None,
    demo_provider=None,
) -> FastAPI:
    from ..strategy_registry import (
        RegistryStrategies, StrategyScopeMiddleware, StrategyUnavailable,
    )
    from .demo_backtest import build_demo_router
    strategy_services = RegistryStrategies(strategy_registry, provider=strategy_provider)
    config = settings or LiveSettings.from_env()
    config.validate()
    repo = repository or SQLiteBarRepository(config.db_path)
    market_feed = feed or build_market_data_provider(config.market_data)
    if history_provider is None:
        capabilities = getattr(market_feed, "capabilities", None)
        if getattr(capabilities, "historical_bars", False):
            history_provider = market_feed

    validator = _build_access_validator(config, access_validator)
    identity_repo = auth_repository or SQLiteAuthRepository(config.db_path)
    identity_repo.bootstrap_admins(config.bootstrap_admin_emails)
    if config.bootstrap_admin_emails:
        bootstrap_owner = identity_repo.user_by_email(config.bootstrap_admin_emails[0])
        if bootstrap_owner is not None:
            repo.claim_legacy_ownership(bootstrap_owner.user_id)

    auth_service = AuthService(
        identity_repo, authorization_mode=config.authorization_mode
    )
    execution_quotes = SQLiteExecutionQuoteRepository(config.db_path)
    service = LiveMarketService(
        market_feed, repo, config.symbol, config.heartbeat_seconds,
        TradingCalendar(config.holidays), config.history_limit,
        history_provider=history_provider,
        stale_after_seconds=config.stale_after_seconds,
        execution_quote_sink=execution_quotes,
    )
    execution_worker = ExecutionHealthFileMonitor(config.execution_health_path)
    broker_truth = SQLiteBrokerTruthRepository(config.db_path)
    recovery = SQLiteRecoveryLockRepository(config.db_path)
    live_orders = SQLiteLiveOrderRepository(config.db_path)
    shadow_store = SQLiteShadowExecutionRepository(config.db_path)
    try:
        raw_assignments = (
            json.loads(config.live_shadow_owner_targets_json)
            if config.live_shadow_enabled else {}
        )
        owner_targets = {
            str(owner): frozenset(
                str(target_id) for target_id in values
            )
            for owner, values in raw_assignments.items()
        }
    except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("LIVE_SHADOW_OWNER_TARGETS_JSON is invalid") from exc
    if config.live_canary_enabled:
        existing = set(owner_targets.get(config.live_canary_owner_id, frozenset()))
        existing.add(config.live_canary_target_id)
        owner_targets[config.live_canary_owner_id] = frozenset(existing)
    shadow_targets = LiveExecutionTargetCatalog(broker_truth, owner_targets)
    owned_target_repository = SQLiteExecutionTargetRepository(config.db_path)
    owned_targets = OwnedExecutionTargetCatalog(owned_target_repository)
    live_risk_config = LiveRiskConfig(
        allowed_symbols=config.live_shadow_allowed_symbols,
        allowed_contracts=config.live_shadow_allowed_contracts or frozenset({config.contract}),
        max_order_quantity=config.live_shadow_max_order_quantity,
        max_account_position_contracts=config.live_shadow_max_account_position,
        max_owner_portfolio_position_contracts=config.live_shadow_max_portfolio_position,
        max_risk_per_trade=config.live_shadow_max_risk_per_trade,
        max_daily_loss=config.live_shadow_max_daily_loss,
        max_daily_trades=config.live_shadow_max_daily_trades,
        max_pending_orders=config.live_shadow_max_pending_orders,
        max_quote_age_seconds=config.live_shadow_max_quote_age_seconds,
        max_slippage_ticks=config.live_shadow_max_slippage_ticks,
        max_spread_ticks=config.live_shadow_max_spread_ticks,
        allowed_sessions=config.live_shadow_allowed_sessions,
        expiry_guard_days=config.live_shadow_expiry_guard_days,
        max_active_live_runtimes=config.live_shadow_max_active_runtimes,
    )
    instrument_catalog = StaticInstrumentSpecCatalog({
        (symbol, contract): InstrumentSpec(
            symbol=symbol,
            contract=contract,
            tick_size=config.live_shadow_tick_size,
            multiplier=config.live_shadow_multiplier,
            expiry_date=config.live_shadow_contract_expiry,
        )
        for symbol in config.live_shadow_allowed_symbols
        for contract in (config.live_shadow_allowed_contracts or frozenset({config.contract}))
    })
    shadow_context = LiveShadowRiskContextProvider(
        truth=broker_truth,
        recovery=recovery,
        orders=live_orders,
        runtimes=repo,
        market=service,
        execution_health=execution_worker,
        shadow_store=shadow_store,
        targets=shadow_targets,
    )
    shadow_service = ShadowExecutionService(
        risk=LiveRiskService(live_risk_config),
        policy=MarketableLimitIOCPolicy(
            live_risk_config.max_slippage_ticks,
            live_risk_config.max_spread_ticks,
        ),
        quotes=service.execution_quotes,
        instruments=instrument_catalog,
        capabilities=ConfiguredBrokerCapabilityView({
            "shioaji": SHIOAJI_TECHNICAL_CAPABILITIES,
        }),
        context=shadow_context,
        store=shadow_store,
    )
    canary_arms = None
    live_canary = None
    live_auto = None
    guardian_store = None
    if config.live_canary_enabled:
        try:
            canary_target = owned_targets.resolve(
                config.live_canary_target_id, config.live_canary_owner_id
            )
        except KeyError:
            canary_target = None
        if canary_target is not None:
            canary_config = LiveCanaryConfig(
                enabled=True,
                allowed_owner_ids=frozenset({config.live_canary_owner_id}),
                allowed_broker_accounts=frozenset({canary_target}),
                allowed_symbols=config.live_canary_allowed_symbols,
                allowed_contracts=config.live_canary_allowed_contracts,
                max_quantity=config.live_canary_max_quantity,
                arm_ttl_seconds=config.live_canary_arm_ttl_seconds,
                protective_stop_ticks=config.live_canary_protective_stop_ticks,
                requests_per_minute=config.live_canary_requests_per_minute,
            )
            canary_arms = SQLiteCanaryArmRepository(config.db_path)
            canary_registry = BrokerRegistry()
            canary_registry.register(BrokerRegistration(
                account_ref=canary_target,
                port=LockedBroker(canary_target.broker_name),
                capabilities=SHIOAJI_CANARY_CAPABILITIES,
                instrument_mapper=LockedInstrumentMapper(),
                state=BrokerRuntimeState.READY,
            ))
            canary_registry.freeze()
            def canary_readiness():
                for item in execution_worker.snapshot().get("broker_accounts", []):
                    if isinstance(item, dict) and item.get("target_id") == canary_target.public_id:
                        return {
                            "connected": item.get("broker_connected", False),
                            "ca_ready": item.get("ca_ready", False),
                            "unknown_orders": sum(
                                order.status.value == "unknown"
                                for order in live_orders.orders(target=canary_target)
                            ),
                        }
                return {"connected": False, "ca_ready": False}
            def kill_switch_blocks(owner_id: str, reduce_only: bool) -> bool:
                account_scope = f"{canary_target.broker_name}:{canary_target.account_id}"
                states = shadow_store.kill_switches({
                    (LiveKillSwitchScope.GLOBAL.value, "global"),
                    (LiveKillSwitchScope.OWNER.value, owner_id),
                    (LiveKillSwitchScope.BROKER_ACCOUNT.value, account_scope),
                    (LiveKillSwitchScope.EXECUTION_TARGET.value,
                     f"{owner_id}:{config.live_canary_target_id}"),
                })
                return any(
                    state.action in {LiveKillSwitchAction.CANCEL_WORKING, LiveKillSwitchAction.FLATTEN}
                    or (state.action is LiveKillSwitchAction.HALT_ENTRY and not reduce_only)
                    for state in states
                )
            gate = CanaryOrderAdmissionGate(
                config=canary_config,
                arms=canary_arms,
                target=canary_target,
                assert_recovery_ready=lambda: recovery.assert_ready(
                    canary_target.broker_name, canary_target.account_id
                ),
                readiness=canary_readiness,
                kill_switch_blocks=kill_switch_blocks,
                now=lambda: datetime.now(timezone.utc),
                execution_target_id=config.live_canary_target_id,
            )
            canary_manager = LiveOrderManager(
                live_orders,
                canary_registry,
                {canary_target: CompositeOrderAdmissionGate((gate,))},
                recover_interrupted=False,
            )
            canary_context = ConfiguredManualCanaryContext(
                owner_id=config.live_canary_owner_id,
                target_id=config.live_canary_target_id,
                targets=owned_targets,
                instruments=instrument_catalog,
                capabilities=ConfiguredBrokerCapabilityView({
                    "shioaji": SHIOAJI_CANARY_CAPABILITIES,
                }),
                risk_context=shadow_context,
                truth=broker_truth,
                recovery=recovery,
                health=execution_worker,
            )
            live_canary = ManualLiveCanaryService(
                config=canary_config,
                arms=canary_arms,
                sink=LiveExecutionSink(canary_manager),
                quotes=service.execution_quotes,
                risk=LiveRiskService(live_risk_config),
                policy=MarketableLimitIOCPolicy(
                    live_risk_config.max_slippage_ticks,
                    live_risk_config.max_spread_ticks,
                ),
                context=canary_context,
            )
            if config.live_auto_enabled:
                guardian_store = SQLitePositionGuardianRepository(config.db_path)
                guardian_config = LivePositionGuardianConfig(enabled=True)
                normal_guardian_policy, emergency_guardian_policy = default_guardian_policies(guardian_config)
                api_guardian = LivePositionGuardian(
                    account_ref=canary_target, config=guardian_config,
                    store=guardian_store, order_store=live_orders,
                    truth_store=broker_truth, recovery=recovery,
                    registry=canary_registry, sink=GuardianExecutionSink(canary_manager),
                    quotes=service.execution_quotes,
                    instrument=instrument_catalog.resolve(
                        next(iter(config.live_canary_allowed_symbols)),
                        next(iter(config.live_canary_allowed_contracts)),
                    ),
                    normal_policy=normal_guardian_policy,
                    emergency_policy=emergency_guardian_policy,
                    readiness=lambda: canary_readiness(),
                    kill_switches=None,
                    owner_ids=frozenset({config.live_canary_owner_id}),
                )
                live_auto = LiveAutoService(
                    enabled=True, runtimes=repo,
                    context=ConfiguredLiveAutoContext(
                        canary_context, api_guardian, live_orders, canary_arms,
                        acceptance_passed=config.live_auto_production_acceptance_passed,
                    ),
                    quotes=service.execution_quotes,
                    risk=LiveRiskService(live_risk_config),
                    policy=MarketableLimitIOCPolicy(
                        live_risk_config.max_slippage_ticks,
                        live_risk_config.max_spread_ticks,
                    ),
                    sink=StrategyLiveExecutionSink(canary_manager),
                    arm_ttl_seconds=config.live_auto_arm_ttl_seconds,
                    shared_arms=canary_arms,
                )
    paper = PaperTradingService(SQLitePaperRepository(config.db_path), strategy_services=strategy_services)
    replay_trading = ReplayTradingSessionRegistry(strategy_services=strategy_services)
    limiter = rate_limiter or _build_rate_limiter(config)
    paper_app = PaperApplicationService(
        repo,
        paper,
        service,
        config.symbol,
        config.stale_after_seconds,
    )
    research_app = ResearchApplicationService(
        repo, repo, repo, replay_trading, config.symbol
    )
    strategy_app = StrategyApplicationService(repo, repo, config.symbol)
    runtime_app = TradingRuntimeApplicationService(
        repo, repo, repo, config.symbol, config.history_limit,
        live_shadow_enabled=config.live_shadow_enabled,
        execution_targets=owned_targets,
        reservation_store=shadow_store,
        live_auto_enabled=config.live_auto_enabled,
        live_risk_version=live_risk_config.version,
        live_execution_policy_version="marketable-limit-ioc:v1",
    )
    research_app = strategy_services.scope_service(research_app)
    strategy_app = strategy_services.scope_service(strategy_app)
    runtime_app = strategy_services.scope_service(runtime_app)
    paper_auto = PaperAutoExecutionController(
        repo, identity_repo, service, paper
    )
    paper_auto.recover()
    runtime_app.add_decision_listener(paper_auto.on_decision)
    live_shadow = LiveShadowExecutionController(repo, shadow_service)
    runtime_app.add_decision_listener(live_shadow.on_decision)
    if live_auto is not None:
        repo.disarm_live_auto_runtimes_for_restart(config.symbol)
        runtime_app.add_decision_listener(live_auto.on_decision)
    service.add_bar_listener(paper_auto.before_bar)
    service.add_bar_listener(paper.on_bar)
    service.add_bar_listener(paper_auto.after_bar)
    service.add_bar_listener(runtime_app.on_bar)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        live_shadow.start()
        await execution_worker.start()
        await service.start()
        try:
            yield
        finally:
            await service.stop()
            live_shadow.stop()
            await execution_worker.stop()
            service.remove_bar_listener(paper_auto.before_bar)
            service.remove_bar_listener(paper.on_bar)
            service.remove_bar_listener(paper_auto.after_bar)
            service.remove_bar_listener(runtime_app.on_bar)
            replay_trading.close()
            paper.close()
            shadow_store.close()
            if canary_arms is not None:
                canary_arms.close()
            if guardian_store is not None:
                guardian_store.close()
            broker_truth.close()
            recovery.close()
            live_orders.close()
            owned_target_repository.close()
            execution_quotes.close()
            repo.close()
            identity_repo.close()

    app = FastAPI(
        title="TMF Live Market API",
        version="0.9.0",
        description="Provider-neutral market data and isolated paper trading API.",
        lifespan=lifespan,
    )
    deps = ApiDependencies(
        config=config,
        market_repo=repo,
        strategy_repo=repo,
        validator=validator,
        identity_repo=identity_repo,
        auth_service=auth_service,
        service=service,
        paper=paper,
        replay_trading=replay_trading,
        host_monitor=HostResourceMonitor(Path(config.db_path)),
        limiter=limiter,
        paper_app=paper_app,
        research_app=research_app,
        strategy_app=strategy_app,
        runtime_app=runtime_app,
        execution_worker=execution_worker,
        shadow_store=shadow_store,
        shadow_targets=owned_targets,
        shadow_service=shadow_service,
        live_canary=live_canary,
        live_auto=live_auto,
    )
    @app.exception_handler(StrategyUnavailable)
    async def unavailable_strategy(_request, _error):
        return JSONResponse(status_code=503, content={"detail": "strategy capability unavailable"})

    # Request context propagates into FastAPI's threadpool; callback proxies bind
    # the same registry for background bars without any process-global plugin.
    app.add_middleware(StrategyScopeMiddleware, services=strategy_services)
    app.state.strategy_services = strategy_services
    app.state.strategy_registry = strategy_services.registry
    app.state.api_dependencies = deps
    app.state.market_service = service
    app.state.repository = repo
    app.state.auth_repository = identity_repo
    app.state.auth_service = auth_service
    app.state.paper_trading = paper
    app.state.replay_trading = replay_trading
    app.state.host_monitor = deps.host_monitor
    app.state.rate_limiter = limiter
    app.state.trading_runtime = runtime_app
    app.state.paper_auto_entry = paper_auto
    app.state.live_shadow = shadow_service
    app.state.live_canary = live_canary

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(config.allowed_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )
    install_authorization_middleware(app, deps)
    for router in (
        build_system_router(deps),
        build_admin_router(deps),
        build_paper_router(deps),
        build_market_router(deps),
        build_strategy_router(deps),
        build_research_router(deps),
        build_trading_runtime_router(deps),
        build_live_canary_router(deps),
        build_live_auto_router(deps),
        build_demo_router(demo_provider, strategy_services),
    ):
        app.include_router(router)

    # Added last so it is the outermost application middleware and rejects
    # oversized bodies before authentication, JSON parsing, or route work.
    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_body_bytes=config.max_request_body_bytes,
    )
    return app

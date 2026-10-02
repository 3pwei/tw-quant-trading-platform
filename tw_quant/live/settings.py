from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, replace
from datetime import date

from ..market_data.settings import MarketDataSettings, normalize_provider


def _split_origins(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _split_emails(value: str) -> tuple[str, ...]:
    return tuple(
        item.strip().casefold() for item in value.split(",") if item.strip()
    )


@dataclass(frozen=True, init=False)
class LiveSettings:
    """Application settings composed with an isolated market-data config.

    Legacy keyword arguments and properties remain available so deployed
    environments and external callers can migrate without a flag day.
    """

    market_data: MarketDataSettings
    db_path: str
    heartbeat_seconds: float
    stale_after_seconds: float
    allowed_origins: tuple[str, ...]
    holidays: frozenset[date]
    access_mode: str
    cloudflare_access_team_domain: str | None
    cloudflare_access_audience: str | None
    authorization_mode: str
    bootstrap_admin_emails: tuple[str, ...]
    environment: str
    rate_limit_access_requests_per_hour: int
    rate_limit_backtests_per_minute: int
    rate_limit_replay_prepares_per_minute: int
    rate_limit_orders_per_minute: int
    max_request_body_bytes: int
    execution_health_path: str
    live_shadow_enabled: bool
    live_shadow_allowed_symbols: frozenset[str]
    live_shadow_allowed_contracts: frozenset[str]
    live_shadow_contract_expiry: date | None
    live_shadow_tick_size: float
    live_shadow_multiplier: float
    live_shadow_max_order_quantity: int
    live_shadow_max_account_position: int
    live_shadow_max_portfolio_position: int
    live_shadow_max_risk_per_trade: float
    live_shadow_max_daily_loss: float
    live_shadow_max_daily_trades: int
    live_shadow_max_pending_orders: int
    live_shadow_max_quote_age_seconds: float
    live_shadow_max_slippage_ticks: int
    live_shadow_max_spread_ticks: int
    live_shadow_allowed_sessions: frozenset[str]
    live_shadow_expiry_guard_days: int
    live_shadow_max_active_runtimes: int
    live_shadow_owner_targets_json: str
    live_canary_enabled: bool
    live_canary_owner_id: str
    live_canary_target_id: str
    live_canary_allowed_symbols: frozenset[str]
    live_canary_allowed_contracts: frozenset[str]
    live_canary_max_quantity: int
    live_canary_arm_ttl_seconds: int
    live_canary_protective_stop_ticks: int
    live_canary_requests_per_minute: int
    live_auto_enabled: bool
    live_auto_arm_ttl_seconds: int
    live_auto_production_acceptance_passed: bool

    def __init__(
        self,
        *,
        market_data: MarketDataSettings | None = None,
        db_path: str = "output/live_market.sqlite3",
        heartbeat_seconds: float = 5.0,
        stale_after_seconds: float = 120.0,
        allowed_origins: tuple[str, ...] = ("http://localhost:3000",),
        holidays: frozenset[date] = frozenset(),
        access_mode: str = "disabled",
        cloudflare_access_team_domain: str | None = None,
        cloudflare_access_audience: str | None = None,
        authorization_mode: str = "disabled",
        bootstrap_admin_emails: tuple[str, ...] = (),
        environment: str = "development",
        rate_limit_access_requests_per_hour: int = 5,
        rate_limit_backtests_per_minute: int = 10,
        rate_limit_replay_prepares_per_minute: int = 10,
        rate_limit_orders_per_minute: int = 30,
        max_request_body_bytes: int = 256 * 1024,
        execution_health_path: str = "/run/tw-quant-execution/health.json",
        live_shadow_enabled: bool = False,
        live_shadow_allowed_symbols: frozenset[str] = frozenset({"TMF"}),
        live_shadow_allowed_contracts: frozenset[str] = frozenset(),
        live_shadow_contract_expiry: date | None = None,
        live_shadow_tick_size: float = 1.0,
        live_shadow_multiplier: float = 10.0,
        live_shadow_max_order_quantity: int = 1,
        live_shadow_max_account_position: int = 1,
        live_shadow_max_portfolio_position: int = 1,
        live_shadow_max_risk_per_trade: float = 5_000.0,
        live_shadow_max_daily_loss: float = 10_000.0,
        live_shadow_max_daily_trades: int = 10,
        live_shadow_max_pending_orders: int = 1,
        live_shadow_max_quote_age_seconds: float = 2.0,
        live_shadow_max_slippage_ticks: int = 2,
        live_shadow_max_spread_ticks: int = 4,
        live_shadow_allowed_sessions: frozenset[str] = frozenset({"day", "night"}),
        live_shadow_expiry_guard_days: int = 2,
        live_shadow_max_active_runtimes: int = 1,
        live_shadow_owner_targets_json: str = "{}",
        live_canary_enabled: bool = False,
        live_canary_owner_id: str = "",
        live_canary_target_id: str = "",
        live_canary_allowed_symbols: frozenset[str] = frozenset(),
        live_canary_allowed_contracts: frozenset[str] = frozenset(),
        live_canary_max_quantity: int = 1,
        live_canary_arm_ttl_seconds: int = 600,
        live_canary_protective_stop_ticks: int = 20,
        live_canary_requests_per_minute: int = 4,
        live_auto_enabled: bool = False,
        live_auto_arm_ttl_seconds: int = 600,
        live_auto_production_acceptance_passed: bool = False,
        # Compatibility inputs from the pre-provider settings model.
        mode: str | None = None,
        symbol: str | None = None,
        contract: str | None = None,
        replay_csv: str | None = None,
        replay_speed: float | None = None,
        history_days: int | None = None,
        history_limit: int | None = None,
        shioaji_api_key: str | None = None,
        shioaji_secret_key: str | None = None,
        shioaji_production: bool | None = None,
    ):
        data = market_data or MarketDataSettings()
        overrides: dict[str, object] = {}
        if mode is not None:
            overrides["provider"] = normalize_provider(mode)
        for name, value in (
            ("symbol", symbol),
            ("contract", contract),
            ("replay_csv", replay_csv),
            ("replay_speed", replay_speed),
            ("history_days", history_days),
            ("history_limit", history_limit),
            ("shioaji_api_key", shioaji_api_key),
            ("shioaji_secret_key", shioaji_secret_key),
            ("shioaji_production", shioaji_production),
        ):
            if value is not None:
                overrides[name] = value
        if overrides:
            data = replace(data, **overrides)
        object.__setattr__(self, "market_data", data)
        object.__setattr__(self, "db_path", db_path)
        object.__setattr__(self, "heartbeat_seconds", heartbeat_seconds)
        object.__setattr__(self, "stale_after_seconds", stale_after_seconds)
        object.__setattr__(self, "allowed_origins", allowed_origins)
        object.__setattr__(self, "holidays", holidays)
        object.__setattr__(self, "access_mode", access_mode)
        object.__setattr__(
            self, "cloudflare_access_team_domain", cloudflare_access_team_domain
        )
        object.__setattr__(
            self, "cloudflare_access_audience", cloudflare_access_audience
        )
        object.__setattr__(self, "authorization_mode", authorization_mode)
        object.__setattr__(
            self, "bootstrap_admin_emails", bootstrap_admin_emails
        )
        object.__setattr__(self, "environment", environment.lower().strip())
        object.__setattr__(
            self,
            "rate_limit_access_requests_per_hour",
            rate_limit_access_requests_per_hour,
        )
        object.__setattr__(
            self,
            "rate_limit_backtests_per_minute",
            rate_limit_backtests_per_minute,
        )
        object.__setattr__(
            self,
            "rate_limit_replay_prepares_per_minute",
            rate_limit_replay_prepares_per_minute,
        )
        object.__setattr__(
            self, "rate_limit_orders_per_minute", rate_limit_orders_per_minute
        )
        object.__setattr__(
            self, "max_request_body_bytes", max_request_body_bytes
        )
        object.__setattr__(self, "execution_health_path", execution_health_path)
        for name, value in (
            ("live_shadow_enabled", live_shadow_enabled),
            ("live_shadow_allowed_symbols", live_shadow_allowed_symbols),
            ("live_shadow_allowed_contracts", live_shadow_allowed_contracts),
            ("live_shadow_contract_expiry", live_shadow_contract_expiry),
            ("live_shadow_tick_size", live_shadow_tick_size),
            ("live_shadow_multiplier", live_shadow_multiplier),
            ("live_shadow_max_order_quantity", live_shadow_max_order_quantity),
            ("live_shadow_max_account_position", live_shadow_max_account_position),
            ("live_shadow_max_portfolio_position", live_shadow_max_portfolio_position),
            ("live_shadow_max_risk_per_trade", live_shadow_max_risk_per_trade),
            ("live_shadow_max_daily_loss", live_shadow_max_daily_loss),
            ("live_shadow_max_daily_trades", live_shadow_max_daily_trades),
            ("live_shadow_max_pending_orders", live_shadow_max_pending_orders),
            ("live_shadow_max_quote_age_seconds", live_shadow_max_quote_age_seconds),
            ("live_shadow_max_slippage_ticks", live_shadow_max_slippage_ticks),
            ("live_shadow_max_spread_ticks", live_shadow_max_spread_ticks),
            ("live_shadow_allowed_sessions", live_shadow_allowed_sessions),
            ("live_shadow_expiry_guard_days", live_shadow_expiry_guard_days),
            ("live_shadow_max_active_runtimes", live_shadow_max_active_runtimes),
            ("live_shadow_owner_targets_json", live_shadow_owner_targets_json),
            ("live_canary_enabled", live_canary_enabled),
            ("live_canary_owner_id", live_canary_owner_id),
            ("live_canary_target_id", live_canary_target_id),
            ("live_canary_allowed_symbols", live_canary_allowed_symbols),
            ("live_canary_allowed_contracts", live_canary_allowed_contracts),
            ("live_canary_max_quantity", live_canary_max_quantity),
            ("live_canary_arm_ttl_seconds", live_canary_arm_ttl_seconds),
            ("live_canary_protective_stop_ticks", live_canary_protective_stop_ticks),
            ("live_canary_requests_per_minute", live_canary_requests_per_minute),
            ("live_auto_enabled", live_auto_enabled),
            ("live_auto_arm_ttl_seconds", live_auto_arm_ttl_seconds),
            ("live_auto_production_acceptance_passed", live_auto_production_acceptance_passed),
        ):
            object.__setattr__(self, name, value)

    @classmethod
    def from_env(cls) -> "LiveSettings":
        try:
            from dotenv import load_dotenv

            load_dotenv()
        except ImportError:
            pass
        access_mode = os.getenv("MARKET_ACCESS_MODE", "disabled").lower().strip()
        expiry = os.getenv("LIVE_SHADOW_CONTRACT_EXPIRY", "").strip()
        split = lambda name, default="": frozenset(
            item.strip().upper() for item in os.getenv(name, default).split(",")
            if item.strip()
        )
        return cls(
            market_data=MarketDataSettings.from_env(),
            db_path=os.getenv("MARKET_DB_PATH", "output/live_market.sqlite3"),
            heartbeat_seconds=float(os.getenv("MARKET_HEARTBEAT_SECONDS", "5")),
            stale_after_seconds=float(
                os.getenv("MARKET_STALE_AFTER_SECONDS", "120")
            ),
            allowed_origins=_split_origins(
                os.getenv("MARKET_ALLOWED_ORIGINS", "http://localhost:3000")
            ),
            holidays=frozenset(
                date.fromisoformat(item.strip())
                for item in os.getenv("MARKET_HOLIDAYS", "").split(",")
                if item.strip()
            ),
            access_mode=access_mode,
            cloudflare_access_team_domain=os.getenv("CF_ACCESS_TEAM_DOMAIN"),
            cloudflare_access_audience=os.getenv("CF_ACCESS_AUD"),
            authorization_mode=os.getenv(
                "PLATFORM_AUTHORIZATION_MODE", "disabled"
            ).lower().strip(),
            bootstrap_admin_emails=_split_emails(
                os.getenv("PLATFORM_BOOTSTRAP_ADMIN_EMAILS", "")
            ),
            # Secure by default: local development must explicitly opt out.
            environment=os.getenv(
                "PLATFORM_ENVIRONMENT", "production"
            ),
            rate_limit_access_requests_per_hour=int(
                os.getenv("RATE_LIMIT_ACCESS_REQUESTS_PER_HOUR", "5")
            ),
            rate_limit_backtests_per_minute=int(
                os.getenv("RATE_LIMIT_BACKTESTS_PER_MINUTE", "10")
            ),
            rate_limit_replay_prepares_per_minute=int(
                os.getenv("RATE_LIMIT_REPLAY_PREPARES_PER_MINUTE", "10")
            ),
            rate_limit_orders_per_minute=int(
                os.getenv("RATE_LIMIT_ORDERS_PER_MINUTE", "30")
            ),
            max_request_body_bytes=int(
                os.getenv("API_MAX_REQUEST_BODY_BYTES", str(256 * 1024))
            ),
            execution_health_path=os.getenv(
                "LIVE_EXECUTION_HEALTH_PATH",
                "/run/tw-quant-execution/health.json",
            ).strip(),
            live_shadow_enabled=os.getenv("LIVE_SHADOW_ENABLED", "false").lower() in {"1", "true", "yes", "on"},
            live_shadow_allowed_symbols=split("LIVE_SHADOW_ALLOWED_SYMBOLS", "TMF"),
            live_shadow_allowed_contracts=split("LIVE_SHADOW_ALLOWED_CONTRACTS"),
            live_shadow_contract_expiry=date.fromisoformat(expiry) if expiry else None,
            live_shadow_tick_size=float(os.getenv("LIVE_SHADOW_TICK_SIZE", "1")),
            live_shadow_multiplier=float(os.getenv("LIVE_SHADOW_MULTIPLIER", "10")),
            live_shadow_max_order_quantity=int(os.getenv("LIVE_SHADOW_MAX_ORDER_QUANTITY", "1")),
            live_shadow_max_account_position=int(os.getenv("LIVE_SHADOW_MAX_ACCOUNT_POSITION", "1")),
            live_shadow_max_portfolio_position=int(os.getenv("LIVE_SHADOW_MAX_PORTFOLIO_POSITION", "1")),
            live_shadow_max_risk_per_trade=float(os.getenv("LIVE_SHADOW_MAX_RISK_PER_TRADE", "5000")),
            live_shadow_max_daily_loss=float(os.getenv("LIVE_SHADOW_MAX_DAILY_LOSS", "10000")),
            live_shadow_max_daily_trades=int(os.getenv("LIVE_SHADOW_MAX_DAILY_TRADES", "10")),
            live_shadow_max_pending_orders=int(os.getenv("LIVE_SHADOW_MAX_PENDING_ORDERS", "1")),
            live_shadow_max_quote_age_seconds=float(os.getenv("LIVE_SHADOW_MAX_QUOTE_AGE_SECONDS", "2")),
            live_shadow_max_slippage_ticks=int(os.getenv("LIVE_SHADOW_MAX_SLIPPAGE_TICKS", "2")),
            live_shadow_max_spread_ticks=int(os.getenv("LIVE_SHADOW_MAX_SPREAD_TICKS", "4")),
            live_shadow_allowed_sessions=frozenset(
                item.strip().lower()
                for item in os.getenv(
                    "LIVE_SHADOW_ALLOWED_SESSIONS", "day,night"
                ).split(",") if item.strip()
            ),
            live_shadow_expiry_guard_days=int(os.getenv("LIVE_SHADOW_EXPIRY_GUARD_DAYS", "2")),
            live_shadow_max_active_runtimes=int(os.getenv("LIVE_SHADOW_MAX_ACTIVE_RUNTIMES", "1")),
            live_shadow_owner_targets_json=os.getenv(
                "LIVE_SHADOW_OWNER_TARGETS_JSON", "{}"
            ).strip(),
            live_canary_enabled=os.getenv("LIVE_CANARY_ENABLED", "false").lower() in {"1", "true", "yes", "on"},
            live_canary_owner_id=os.getenv("LIVE_CANARY_ALLOWED_OWNER_ID", "").strip(),
            live_canary_target_id=os.getenv("LIVE_CANARY_TARGET_ID", "").strip(),
            live_canary_allowed_symbols=split("LIVE_CANARY_ALLOWED_SYMBOLS"),
            live_canary_allowed_contracts=split("LIVE_CANARY_ALLOWED_CONTRACTS"),
            live_canary_max_quantity=int(os.getenv("LIVE_CANARY_MAX_QUANTITY", "1")),
            live_canary_arm_ttl_seconds=int(os.getenv("LIVE_CANARY_ARM_TTL_SECONDS", "600")),
            live_canary_protective_stop_ticks=int(os.getenv("LIVE_CANARY_PROTECTIVE_STOP_TICKS", "20")),
            live_canary_requests_per_minute=int(os.getenv("LIVE_ORDER_REQUESTS_PER_MINUTE", "4")),
            live_auto_enabled=os.getenv("LIVE_AUTO_ENABLED", "false").lower() in {"1", "true", "yes", "on"},
            live_auto_arm_ttl_seconds=int(os.getenv("LIVE_AUTO_ARM_TTL_SECONDS", "600")),
            live_auto_production_acceptance_passed=os.getenv("LIVE_AUTO_PRODUCTION_ACCEPTANCE_PASSED", "false").lower() in {"1", "true", "yes", "on"},
        )

    def validate(self) -> None:
        self.market_data.validate()
        if self.environment not in {"development", "test", "production"}:
            raise ValueError(
                "PLATFORM_ENVIRONMENT must be development, test or production"
            )
        if self.heartbeat_seconds <= 0:
            raise ValueError("MARKET_HEARTBEAT_SECONDS must be positive")
        if self.stale_after_seconds <= 0:
            raise ValueError("MARKET_STALE_AFTER_SECONDS must be positive")
        for name, value in (
            (
                "RATE_LIMIT_ACCESS_REQUESTS_PER_HOUR",
                self.rate_limit_access_requests_per_hour,
            ),
            (
                "RATE_LIMIT_BACKTESTS_PER_MINUTE",
                self.rate_limit_backtests_per_minute,
            ),
            (
                "RATE_LIMIT_REPLAY_PREPARES_PER_MINUTE",
                self.rate_limit_replay_prepares_per_minute,
            ),
            ("RATE_LIMIT_ORDERS_PER_MINUTE", self.rate_limit_orders_per_minute),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        if not 1024 <= self.max_request_body_bytes <= 1024 * 1024:
            raise ValueError(
                "API_MAX_REQUEST_BODY_BYTES must be between 1024 and 1048576"
            )
        if not self.execution_health_path:
            raise ValueError("LIVE_EXECUTION_HEALTH_PATH is required")
        if self.live_shadow_enabled and (
            not self.live_shadow_allowed_contracts
            or self.live_shadow_contract_expiry is None
        ):
            raise ValueError(
                "live shadow requires contract allowlist and expiry metadata"
            )
        if self.live_shadow_enabled:
            try:
                assignments = json.loads(self.live_shadow_owner_targets_json)
            except json.JSONDecodeError as exc:
                raise ValueError("LIVE_SHADOW_OWNER_TARGETS_JSON is invalid") from exc
            valid = isinstance(assignments, dict) and all(
                isinstance(owner, str)
                and owner.strip()
                and isinstance(values, list)
                and all(
                    isinstance(value, str)
                    and re.fullmatch(r"target:[0-9a-f]{24}", value)
                    for value in values
                )
                for owner, values in assignments.items()
            )
            if not valid:
                raise ValueError(
                    "LIVE_SHADOW_OWNER_TARGETS_JSON must map owners to opaque target IDs"
                )
        if self.live_canary_enabled:
            if not self.authorization_mode == "enforced":
                raise ValueError("live canary requires enforced authorization")
            if not self.live_canary_owner_id:
                raise ValueError("live canary requires one allowed owner")
            if not re.fullmatch(r"target:[0-9a-f]{24}", self.live_canary_target_id):
                raise ValueError("live canary requires one opaque target ID")
            if (
                len(self.live_canary_allowed_symbols) != 1
                or len(self.live_canary_allowed_contracts) != 1
                or self.live_canary_max_quantity != 1
            ):
                raise ValueError(
                    "live canary requires one symbol, one contract, and max quantity 1"
                )
            if not 300 <= self.live_canary_arm_ttl_seconds <= 900:
                raise ValueError("live canary ARM TTL must be 5-15 minutes")
            if self.live_canary_protective_stop_ticks < 1:
                raise ValueError("live canary protective stop ticks must be positive")
            if not 1 <= self.live_canary_requests_per_minute <= 10:
                raise ValueError("live canary rate limit must be 1-10")
            if self.live_shadow_contract_expiry is None:
                raise ValueError("live canary requires canonical contract expiry")
        if self.live_auto_enabled:
            if not self.live_canary_enabled:
                raise ValueError("live_auto requires the accepted live canary boundary")
            if not 60 <= self.live_auto_arm_ttl_seconds <= 900:
                raise ValueError("live_auto ARM TTL must be 1-15 minutes")
            if not self.live_auto_production_acceptance_passed:
                raise ValueError("live_auto requires explicit production acceptance evidence")
        if self.access_mode not in {"disabled", "cloudflare"}:
            raise ValueError("MARKET_ACCESS_MODE must be disabled or cloudflare")
        if self.access_mode == "cloudflare" and not (
            self.cloudflare_access_team_domain and self.cloudflare_access_audience
        ):
            raise ValueError(
                "CF_ACCESS_TEAM_DOMAIN and CF_ACCESS_AUD are required in cloudflare mode"
            )
        if self.authorization_mode not in {"disabled", "enforced"}:
            raise ValueError(
                "PLATFORM_AUTHORIZATION_MODE must be disabled or enforced"
            )
        if self.authorization_mode == "enforced" and not (
            self.bootstrap_admin_emails
        ):
            raise ValueError(
                "PLATFORM_BOOTSTRAP_ADMIN_EMAILS is required when platform "
                "authorization is enforced"
            )
        if self.environment == "production":
            if self.access_mode != "cloudflare":
                raise ValueError(
                    "production requires MARKET_ACCESS_MODE=cloudflare"
                )
            if self.authorization_mode != "enforced":
                raise ValueError(
                    "production requires PLATFORM_AUTHORIZATION_MODE=enforced"
                )

    # Compatibility properties. New code should use ``settings.market_data``.
    @property
    def mode(self) -> str:
        return (
            "mock"
            if self.market_data.provider == "replay"
            else self.market_data.provider
        )

    @property
    def symbol(self) -> str:
        return self.market_data.symbol

    @property
    def contract(self) -> str:
        return self.market_data.contract

    @property
    def replay_csv(self) -> str:
        return self.market_data.replay_csv

    @property
    def replay_speed(self) -> float:
        return self.market_data.replay_speed

    @property
    def history_days(self) -> int:
        return self.market_data.history_days

    @property
    def history_limit(self) -> int:
        return self.market_data.history_limit

    @property
    def shioaji_api_key(self) -> str | None:
        return self.market_data.shioaji_api_key

    @property
    def shioaji_secret_key(self) -> str | None:
        return self.market_data.shioaji_secret_key

    @property
    def shioaji_production(self) -> bool:
        return self.market_data.shioaji_production

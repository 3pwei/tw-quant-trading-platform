from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Mapping

from ..broker.identity import BrokerAccountRef
from ..broker.canary import LIVE_CANARY_CONFIRMATION, LiveCanaryConfig
from ..broker.settings import BrokerConnectionSettings


def _enabled(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class ExecutionServiceSettings:
    """Non-secret settings owned only by the dedicated execution process."""

    broker_name: str = "disabled"
    owner_user_id: str = ""
    target_id: str = ""
    connection_id: str = "primary"
    account_id: str = ""
    secret_ref: str = "environment:primary"
    live_trading_enabled: bool = False
    production_read_only_enabled: bool = False
    read_only_confirmation: str = ""
    instrument_map_json: str = ""
    confirmation: str = ""
    allowed_account_ids: frozenset[str] = frozenset()
    database_path: str = "/data/market.sqlite3"
    health_path: str = "/run/tw-quant-execution/health.json"
    generation_path: str = "/tmp/tw-quant-execution-generation"
    broker_secret_root: str = "/run/live-secrets/brokers"
    heartbeat_seconds: float = 5.0
    reconciliation_interval_seconds: float = 45.0
    reconciliation_timeout_seconds: float = 20.0
    reconciliation_stale_seconds: float = 120.0
    callback_queue_size: int = 1024
    shutdown_drain_seconds: float = 5.0
    live_canary_enabled: bool = False
    live_canary_confirmation: str = ""
    live_canary_allowed_owner_ids: frozenset[str] = frozenset()
    live_canary_allowed_symbols: frozenset[str] = frozenset()
    live_canary_allowed_contracts: frozenset[str] = frozenset()
    live_canary_max_quantity: int = 1
    live_canary_arm_ttl_seconds: int = 600
    live_canary_protective_stop_ticks: int = 20
    live_canary_requests_per_minute: int = 4
    live_auto_enabled: bool = False
    live_position_guardian_enabled: bool = False
    live_guardian_stop_loss_ticks: int = 20
    live_guardian_take_profit_ticks: int = 40
    live_guardian_quote_stale_seconds: float = 2.0
    live_guardian_poll_seconds: float = 0.25
    live_guardian_tick_size: float = 1.0
    live_guardian_multiplier: float = 10.0

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None
    ) -> "ExecutionServiceSettings":
        values = os.environ if env is None else env
        allowed = frozenset(
            item.strip()
            for item in values.get("LIVE_ALLOWED_ACCOUNT_IDS", "").split(",")
            if item.strip()
        )
        split = lambda name: frozenset(
            item.strip() for item in values.get(name, "").split(",") if item.strip()
        )
        return cls(
            broker_name=values.get("BROKER_PROVIDER", "disabled").strip().lower(),
            owner_user_id=values.get("LIVE_EXECUTION_OWNER_USER_ID", "").strip(),
            target_id=values.get("LIVE_EXECUTION_TARGET_ID", "").strip(),
            connection_id=values.get(
                "LIVE_BROKER_CONNECTION_ID", "primary"
            ).strip(),
            account_id=values.get("LIVE_BROKER_ACCOUNT_ID", "").strip(),
            secret_ref=values.get(
                "LIVE_BROKER_SECRET_REF", "environment:primary"
            ).strip(),
            live_trading_enabled=_enabled(values.get("LIVE_TRADING_ENABLED")),
            production_read_only_enabled=_enabled(
                values.get("LIVE_BROKER_READ_ONLY_ENABLED")
            ),
            read_only_confirmation=values.get(
                "LIVE_BROKER_READ_ONLY_CONFIRMATION", ""
            ).strip(),
            instrument_map_json=values.get(
                "LIVE_BROKER_INSTRUMENT_MAP_JSON", ""
            ).strip(),
            confirmation=values.get("LIVE_TRADING_CONFIRMATION", "").strip(),
            allowed_account_ids=allowed,
            database_path=values.get(
                "LIVE_EXECUTION_DB_PATH", "/data/market.sqlite3"
            ).strip(),
            health_path=values.get(
                "LIVE_EXECUTION_HEALTH_PATH",
                "/run/tw-quant-execution/health.json",
            ).strip(),
            generation_path=values.get(
                "LIVE_EXECUTION_GENERATION_PATH",
                "/tmp/tw-quant-execution-generation",
            ).strip(),
            broker_secret_root=values.get(
                "LIVE_BROKER_SECRET_ROOT", "/run/live-secrets/brokers"
            ).strip(),
            heartbeat_seconds=float(
                values.get("LIVE_EXECUTION_HEARTBEAT_SECONDS", "5")
            ),
            reconciliation_interval_seconds=float(
                values.get("LIVE_RECONCILIATION_INTERVAL_SECONDS", "45")
            ),
            reconciliation_timeout_seconds=float(
                values.get("LIVE_RECONCILIATION_TIMEOUT_SECONDS", "20")
            ),
            reconciliation_stale_seconds=float(
                values.get("LIVE_RECONCILIATION_STALE_SECONDS", "120")
            ),
            callback_queue_size=int(
                values.get("LIVE_CALLBACK_QUEUE_SIZE", "1024")
            ),
            shutdown_drain_seconds=float(
                values.get("LIVE_CALLBACK_SHUTDOWN_DRAIN_SECONDS", "5")
            ),
            live_canary_enabled=_enabled(values.get("LIVE_CANARY_ENABLED")),
            live_canary_confirmation=values.get(
                "LIVE_CANARY_CONFIRMATION", ""
            ).strip(),
            live_canary_allowed_owner_ids=split("LIVE_CANARY_ALLOWED_OWNER_IDS"),
            live_canary_allowed_symbols=split("LIVE_CANARY_ALLOWED_SYMBOLS"),
            live_canary_allowed_contracts=split("LIVE_CANARY_ALLOWED_CONTRACTS"),
            live_canary_max_quantity=int(values.get("LIVE_CANARY_MAX_QUANTITY", "1")),
            live_canary_arm_ttl_seconds=int(values.get("LIVE_CANARY_ARM_TTL_SECONDS", "600")),
            live_canary_protective_stop_ticks=int(values.get("LIVE_CANARY_PROTECTIVE_STOP_TICKS", "20")),
            live_canary_requests_per_minute=int(values.get("LIVE_ORDER_REQUESTS_PER_MINUTE", "4")),
            live_auto_enabled=_enabled(values.get("LIVE_AUTO_ENABLED")),
            live_position_guardian_enabled=_enabled(
                values.get("LIVE_POSITION_GUARDIAN_ENABLED")
            ),
            live_guardian_stop_loss_ticks=int(
                values.get("LIVE_GUARDIAN_STOP_LOSS_TICKS", "20")
            ),
            live_guardian_take_profit_ticks=int(
                values.get("LIVE_GUARDIAN_TAKE_PROFIT_TICKS", "40")
            ),
            live_guardian_quote_stale_seconds=float(
                values.get("LIVE_GUARDIAN_QUOTE_STALE_SECONDS", "2")
            ),
            live_guardian_poll_seconds=float(
                values.get("LIVE_GUARDIAN_POLL_SECONDS", "0.25")
            ),
            live_guardian_tick_size=float(
                values.get("LIVE_GUARDIAN_TICK_SIZE", "1")
            ),
            live_guardian_multiplier=float(
                values.get("LIVE_GUARDIAN_MULTIPLIER", "10")
            ),
        )

    @property
    def provider(self) -> str:
        """Backward-compatible name for the legacy BROKER_PROVIDER input."""

        return self.broker_name

    @property
    def connection(self) -> BrokerConnectionSettings:
        return BrokerConnectionSettings(
            connection_id=self.connection_id,
            broker_name=self.broker_name,
            account_id=self.account_id,
            enabled=self.live_trading_enabled or self.production_read_only_enabled,
            secret_ref=self.secret_ref,
        )

    @property
    def allowed_accounts(self) -> frozenset[BrokerAccountRef]:
        if not self.broker_name:
            return frozenset()
        return frozenset(
            BrokerAccountRef(self.broker_name, account_id)
            for account_id in self.allowed_account_ids
        )

    @property
    def canary_config(self) -> LiveCanaryConfig:
        accounts = (
            frozenset({BrokerAccountRef(self.broker_name, self.account_id)})
            if self.account_id else frozenset()
        )
        return LiveCanaryConfig(
            enabled=self.live_canary_enabled,
            allowed_owner_ids=self.live_canary_allowed_owner_ids,
            allowed_broker_accounts=accounts,
            allowed_symbols=self.live_canary_allowed_symbols,
            allowed_contracts=self.live_canary_allowed_contracts,
            max_quantity=self.live_canary_max_quantity,
            arm_ttl_seconds=self.live_canary_arm_ttl_seconds,
            protective_stop_ticks=self.live_canary_protective_stop_ticks,
            requests_per_minute=self.live_canary_requests_per_minute,
        )

    def validation_issues(self) -> tuple[str, ...]:
        issues: list[str] = []
        if not self.broker_name:
            issues.append("missing_broker_name")
        if not self.connection_id:
            issues.append("missing_broker_connection_id")
        if not self.secret_ref:
            issues.append("missing_broker_secret_ref")
        connection_enabled = (
            self.live_trading_enabled or self.production_read_only_enabled
        )
        if connection_enabled and not self.owner_user_id:
            issues.append("missing_execution_owner_user_id")
        if connection_enabled and not self.target_id:
            issues.append("missing_execution_target_id")
        # Production routing identity is loaded from ExecutionTarget. Legacy
        # account settings are deliberately ignored by the canonical path.
        if self.live_trading_enabled and self.production_read_only_enabled:
            issues.append("read_only_conflicts_with_live_trading")
        if self.live_canary_enabled:
            if not self.live_trading_enabled:
                issues.append("live_canary_requires_live_trading_enabled")
            if self.production_read_only_enabled:
                issues.append("live_canary_conflicts_with_read_only")
            if self.live_canary_confirmation != LIVE_CANARY_CONFIRMATION:
                issues.append("invalid_live_canary_confirmation")
            if not self.instrument_map_json:
                issues.append("missing_broker_instrument_map")
            if self.live_canary_allowed_owner_ids and self.live_canary_allowed_owner_ids != frozenset({self.owner_user_id}):
                issues.append("live_canary_owner_target_mismatch")
            if len(self.live_canary_allowed_symbols) != 1 or len(self.live_canary_allowed_contracts) != 1:
                issues.append("invalid_live_canary_config")
            if self.live_canary_max_quantity != 1 or not 300 <= self.live_canary_arm_ttl_seconds <= 900:
                issues.append("invalid_live_canary_config")
        if self.live_auto_enabled and not self.live_canary_enabled:
            issues.append("live_auto_requires_live_canary")
        if self.live_position_guardian_enabled:
            if not self.live_canary_enabled:
                issues.append("guardian_requires_live_canary")
            if len(self.live_canary_allowed_symbols) != 1 or len(self.live_canary_allowed_contracts) != 1:
                issues.append("guardian_requires_single_instrument")
            if min(self.live_guardian_stop_loss_ticks, self.live_guardian_take_profit_ticks) < 1:
                issues.append("invalid_guardian_protection_ticks")
            if not 0.1 <= self.live_guardian_quote_stale_seconds <= 30:
                issues.append("invalid_guardian_quote_stale_seconds")
            if not 0.05 <= self.live_guardian_poll_seconds <= 5:
                issues.append("invalid_guardian_poll_seconds")
            if self.live_guardian_tick_size <= 0 or self.live_guardian_multiplier <= 0:
                issues.append("invalid_guardian_instrument_spec")
        if (
            self.production_read_only_enabled
            and self.read_only_confirmation != "I_UNDERSTAND_PRODUCTION_READ_ONLY"
        ):
            issues.append("invalid_read_only_confirmation")
        if self.production_read_only_enabled and not self.instrument_map_json:
            issues.append("missing_broker_instrument_map")
        if not self.database_path:
            issues.append("missing_execution_database_path")
        if not self.health_path:
            issues.append("missing_execution_health_path")
        if not self.generation_path or not os.path.isabs(self.generation_path):
            issues.append("invalid_execution_generation_path")
        if not self.broker_secret_root or not os.path.isabs(self.broker_secret_root):
            issues.append("invalid_broker_secret_root")
        if self.heartbeat_seconds <= 0:
            issues.append("invalid_execution_heartbeat")
        if not 30 <= self.reconciliation_interval_seconds <= 3600:
            issues.append("invalid_reconciliation_interval")
        if not 1 <= self.reconciliation_timeout_seconds < self.reconciliation_interval_seconds:
            issues.append("invalid_reconciliation_timeout")
        if self.reconciliation_stale_seconds < self.reconciliation_interval_seconds * 2:
            issues.append("invalid_reconciliation_stale_threshold")
        if not 1 <= self.callback_queue_size <= 100_000:
            issues.append("invalid_callback_queue_size")
        if not 0.1 <= self.shutdown_drain_seconds <= 60:
            issues.append("invalid_callback_shutdown_drain")
        return tuple(issues)

"""Broker-account boundaries, intentionally separate from market data."""

from .audit import (
    BrokerEventAuditRecord,
    BrokerEventAuditStatus,
    BrokerEventAuditStore,
    SQLiteBrokerEventAuditRepository,
)
from .callback_consumer import BrokerCallbackConsumer
from .canary import (
    LIVE_CANARY_CONFIRMATION,
    CanaryArmSession,
    CanaryArmStore,
    CanaryOrderAdmissionGate,
    LiveCanaryConfig,
    arm_expiry,
)
from .canary_repository import SQLiteCanaryArmRepository
from .capabilities import BrokerCapabilities
from .disabled import DisabledBroker, LockedBroker
from .events import BrokerEvent, broker_event_id
from .factory import BrokerAdapterFactory, build_broker
from .identity import BrokerAccountRef
from .execution_targets import ExecutionTarget, ExecutionTargetStatus, mask_account_id
from .execution_target_repository import (
    ExecutionTargetRepository,
    OwnedExecutionTargetResolver,
    OwnedExecutionTargetCatalog,
    SQLiteExecutionTargetRepository,
    bootstrap_legacy_execution_target,
    legacy_target_id,
)
from .guardian_repository import SQLitePositionGuardianRepository
from .guardian_models import (
    GuardianExitReason,
    ManagedLivePosition,
    ManagedPositionState,
    PositionGuardianStore,
)
from .instruments import (
    BrokerInstrumentMapper,
    CanonicalInstrument,
    LockedInstrumentMapper,
)
from .lifecycle import InvalidOrderTransition, transition_order
from .manager import LiveOrderManager
from .models import (
    BrokerAccountSnapshot,
    BrokerOrder,
    BrokerOrderRequest,
    BrokerOrderStatus,
    ExecutionMode,
    OrderPurpose,
    TimeInForce,
    canonical_paper_status,
)
from .ports import (
    BrokerAccount,
    BrokerPort,
    CompositeOrderAdmissionGate,
    LiveOrderStore,
    LockedOrderAdmissionGate,
    OrderAdmissionGate,
    OrderExecutor,
)
from .reconciliation import (
    BrokerFillSnapshot,
    BrokerOrderSnapshot,
    BrokerPositionSnapshot,
    BrokerReconciliationSnapshot,
    BrokerReconciliationSource,
    LiveReconciliationService,
    ReconciliationIssue,
    ReconciliationReport,
)
from .recovery import (
    RecoveryLockStore,
    RecoveryOrderGate,
    RecoveryState,
    RecoveryStatus,
    SQLiteRecoveryLockRepository,
)
from .repository import SQLiteLiveOrderRepository
from tw_quant_core.broker.registry import BrokerRegistration, BrokerRegistry, BrokerRuntimeState
from .routing import RoutedBrokerOrder, RoutedBrokerOrderRequest
from .secrets import (
    BrokerSecretMaterial,
    BrokerSecretProvider,
    SecretConfigurationError,
    SecretResolver,
)
from .secret_resolver import PerTargetSecretResolver
from .settings import BrokerConnectionSettings, BrokerSettings
from .safety import BrokerAccountSafety, LIVE_TRADING_CONFIRMATION
from .shioaji import (
    ExternalOrderReport,
    LiveTradingSafety,
    ShioajiBrokerAdapter,
    ShioajiExecutionClient,
)
from .shioaji_simulation import (
    ShioajiCallbackBridge,
    ShioajiCallbackEvent,
    ShioajiSimulationExecutionClient,
    normalize_callback,
    normalize_trade,
)
from .shioaji_production import (
    READ_ONLY_ERROR,
    SHIOAJI_READ_ONLY_CAPABILITIES,
    SHIOAJI_CANARY_CAPABILITIES,
    SHIOAJI_TECHNICAL_CAPABILITIES,
    ShioajiCallbackMetrics,
    ShioajiInstrumentMapper,
    ShioajiProductionCallbackBridge,
    ShioajiProductionError,
    ShioajiProductionExecutionClient,
    normalize_production_callback,
)
from .worker import (
    BrokerAccountWorker,
    CanaryBrokerAccountWorker,
    DisabledExecutionWorker,
    ExecutionSupervisor,
    ExecutionRuntime,
    ExecutionWorker,
    ExecutionWorkerMonitor,
    ExecutionWorkerSettings,
    build_execution_runtime,
)
from .truth import BrokerTruthStore, SQLiteBrokerTruthRepository

__all__ = [
    "BrokerAdapterFactory",
    "BrokerAccount",
    "BrokerAccountRef",
    "BrokerAccountSafety",
    "BrokerAccountSnapshot",
    "BrokerAccountWorker",
    "CanaryBrokerAccountWorker",
    "BrokerCapabilities",
    "BrokerCallbackConsumer",
    "CanaryArmSession",
    "CanaryArmStore",
    "CanaryOrderAdmissionGate",
    "BrokerEvent",
    "BrokerEventAuditRecord",
    "BrokerEventAuditStatus",
    "BrokerEventAuditStore",
    "BrokerFillSnapshot",
    "BrokerInstrumentMapper",
    "BrokerOrder",
    "BrokerOrderRequest",
    "BrokerOrderStatus",
    "BrokerOrderSnapshot",
    "BrokerPositionSnapshot",
    "BrokerPort",
    "CompositeOrderAdmissionGate",
    "BrokerReconciliationSnapshot",
    "BrokerReconciliationSource",
    "BrokerRegistration",
    "BrokerRegistry",
    "BrokerRuntimeState",
    "BrokerConnectionSettings",
    "BrokerSettings",
    "BrokerSecretMaterial",
    "BrokerSecretProvider",
    "SecretResolver",
    "PerTargetSecretResolver",
    "CanonicalInstrument",
    "DisabledBroker",
    "DisabledExecutionWorker",
    "ExecutionMode",
    "ExecutionTarget",
    "ExecutionTargetRepository",
    "ExecutionTargetStatus",
    "ExecutionRuntime",
    "ExecutionSupervisor",
    "ExecutionWorker",
    "ExecutionWorkerMonitor",
    "ExecutionWorkerSettings",
    "ExternalOrderReport",
    "InvalidOrderTransition",
    "GuardianExitReason",
    "ManagedLivePosition",
    "ManagedPositionState",
    "PositionGuardianStore",
    "LiveTradingSafety",
    "LiveCanaryConfig",
    "LIVE_CANARY_CONFIRMATION",
    "LIVE_TRADING_CONFIRMATION",
    "LiveOrderManager",
    "LiveOrderStore",
    "LockedOrderAdmissionGate",
    "LockedBroker",
    "LockedInstrumentMapper",
    "LiveReconciliationService",
    "OrderAdmissionGate",
    "OrderExecutor",
    "OrderPurpose",
    "TimeInForce",
    "ReconciliationIssue",
    "ReconciliationReport",
    "RecoveryLockStore",
    "RecoveryOrderGate",
    "RecoveryState",
    "RecoveryStatus",
    "RoutedBrokerOrder",
    "RoutedBrokerOrderRequest",
    "ShioajiBrokerAdapter",
    "ShioajiCallbackBridge",
    "ShioajiCallbackEvent",
    "ShioajiExecutionClient",
    "ShioajiCallbackMetrics",
    "ShioajiInstrumentMapper",
    "ShioajiProductionCallbackBridge",
    "ShioajiProductionError",
    "ShioajiProductionExecutionClient",
    "ShioajiSimulationExecutionClient",
    "SHIOAJI_READ_ONLY_CAPABILITIES",
    "SHIOAJI_CANARY_CAPABILITIES",
    "SHIOAJI_TECHNICAL_CAPABILITIES",
    "READ_ONLY_ERROR",
    "SecretConfigurationError",
    "SQLiteBrokerEventAuditRepository",
    "SQLiteExecutionTargetRepository",
    "SQLiteCanaryArmRepository",
    "SQLiteLiveOrderRepository",
    "SQLitePositionGuardianRepository",
    "SQLiteRecoveryLockRepository",
    "canonical_paper_status",
    "bootstrap_legacy_execution_target",
    "legacy_target_id",
    "mask_account_id",
    "OwnedExecutionTargetResolver",
    "OwnedExecutionTargetCatalog",
    "normalize_callback",
    "normalize_production_callback",
    "normalize_trade",
    "transition_order",
    "build_broker",
    "build_execution_runtime",
    "broker_event_id",
    "arm_expiry",
    "BrokerTruthStore",
    "SQLiteBrokerTruthRepository",
]

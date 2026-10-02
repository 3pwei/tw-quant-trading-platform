"""Execution adapters and simulators."""

from tw_quant_core.execution.liquidator import PositionLiquidator
from .guardian import (
    GuardianExecutionSink,
    GuardianExitReason,
    GuardianKillSwitchView,
    LivePositionGuardian,
    LivePositionGuardianConfig,
    ManagedLivePosition,
    ManagedPositionState,
    PositionGuardianStore,
    default_guardian_policies,
)
from tw_quant_core.execution.pipeline import SimulatedExecutionPipeline
from .policy import SignalSimulationPolicy
from .position_ledger import PositionKey, PositionLedger, PositionState, RealizedTrade
from tw_quant_core.execution.risk_gates import DisabledRiskGate, PassThroughRiskGate, ResearchRiskGate, RiskGate
from tw_quant_core.execution.signal_router import SignalOrderRouter
from tw_quant_core.execution.simulated_broker import OrderRecord, SimulatedBroker
from tw_quant_core.execution.simulator import simulate_signals

__all__ = [
    "OrderRecord",
    "GuardianExecutionSink",
    "GuardianExitReason",
    "GuardianKillSwitchView",
    "LivePositionGuardian",
    "LivePositionGuardianConfig",
    "ManagedLivePosition",
    "ManagedPositionState",
    "PositionGuardianStore",
    "default_guardian_policies",
    "DisabledRiskGate",
    "PassThroughRiskGate",
    "PositionKey",
    "PositionLedger",
    "PositionLiquidator",
    "PositionState",
    "ResearchRiskGate",
    "RealizedTrade",
    "RiskGate",
    "SignalOrderRouter",
    "SignalSimulationPolicy",
    "SimulatedBroker",
    "SimulatedExecutionPipeline",
    "simulate_signals",
]

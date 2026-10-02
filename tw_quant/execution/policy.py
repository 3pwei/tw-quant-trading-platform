"""Stable Private import path for Public Core simulation policy."""

from tw_quant_core.execution import (
    DEFAULT_SIGNAL_SIMULATION_POLICY,
    SignalSimulationPolicy,
)

USING_PUBLIC_CORE = True

__all__ = [
    "DEFAULT_SIGNAL_SIMULATION_POLICY", "SignalSimulationPolicy",
    "USING_PUBLIC_CORE",
]

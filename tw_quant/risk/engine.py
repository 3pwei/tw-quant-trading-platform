"""Stable Private import path for Public Core generic risk calculations."""

from typing import Literal, TypeAlias

from tw_quant_core.risk import (
    DEFAULT_RISK,
    RiskConfig,
    RiskLevels,
    calculate_levels,
    triggered_exit,
)

Direction: TypeAlias = Literal[1, -1]
USING_PUBLIC_CORE = True

__all__ = [
    "DEFAULT_RISK", "Direction", "RiskConfig", "RiskLevels",
    "USING_PUBLIC_CORE", "calculate_levels", "triggered_exit",
]

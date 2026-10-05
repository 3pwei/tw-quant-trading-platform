"""Dedicated, non-HTTP live execution process boundary."""

from .config import ExecutionServiceSettings
from .health import BrokerConnectionHealth, ExecutionServiceHealth
from .secrets import (
    BrokerSecretMaterial,
    BrokerSecretProvider,
    SecretConfigurationError,
)


def __getattr__(name: str):
    if name in {"ExecutionServiceRuntime", "build_execution_service"}:
        from . import runtime

        return getattr(runtime, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "BrokerSecretMaterial",
    "BrokerSecretProvider",
    "BrokerConnectionHealth",
    "ExecutionServiceRuntime",
    "ExecutionServiceHealth",
    "ExecutionServiceSettings",
    "SecretConfigurationError",
    "build_execution_service",
]

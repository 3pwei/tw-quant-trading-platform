"""Compatibility exports for the broker-neutral secret boundary."""

from ..broker.secrets import (
    BrokerSecretMaterial,
    BrokerSecretProvider,
    SecretConfigurationError,
)

__all__ = [
    "BrokerSecretMaterial",
    "BrokerSecretProvider",
    "SecretConfigurationError",
]

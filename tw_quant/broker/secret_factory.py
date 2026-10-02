from __future__ import annotations

from typing import Mapping

from .secrets import BrokerSecretProvider, SecretConfigurationError
from .settings import BrokerConnectionSettings
from .shioaji_secrets import ShioajiEnvironmentSecretProvider


def build_broker_secret_provider(
    connection: BrokerConnectionSettings,
    *,
    env: Mapping[str, str] | None = None,
) -> BrokerSecretProvider:
    """Build the credential adapter for a supported broker connection."""

    if connection.broker_name == "shioaji":
        return ShioajiEnvironmentSecretProvider(env)
    raise SecretConfigurationError(("unknown_broker_provider",))

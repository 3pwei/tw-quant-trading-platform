from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .execution_targets import ExecutionTarget

from .settings import BrokerConnectionSettings


class SecretConfigurationError(ValueError):
    """Raised with stable issue codes and without credential material."""

    def __init__(self, issue_codes: tuple[str, ...]):
        self.issue_codes = issue_codes
        super().__init__(
            "invalid live broker secret configuration: " + ",".join(issue_codes)
        )


@dataclass(frozen=True)
class BrokerSecretMaterial:
    """Opaque broker-specific material returned only to the adapter layer."""

    values: tuple[tuple[str, str], ...] = field(repr=False)
    files: tuple[Path, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        if not self.values or any(not name or not value for name, value in self.values):
            raise ValueError("broker secret material must contain named values")

    @property
    def redaction_values(self) -> tuple[str | Path, ...]:
        return tuple(value for _, value in self.values) + self.files


class BrokerSecretProvider(Protocol):
    """Resolve credentials for exactly one configured broker connection."""

    def load(self, connection: BrokerConnectionSettings) -> BrokerSecretMaterial: ...


class SecretResolver(Protocol):
    """Resolve material for one exact durable target inside the execution service."""

    def resolve(
        self, target: ExecutionTarget, connection: BrokerConnectionSettings
    ) -> BrokerSecretMaterial: ...

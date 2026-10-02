from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import stat
from typing import Mapping

from .secrets import BrokerSecretMaterial, SecretConfigurationError
from .settings import BrokerConnectionSettings


@dataclass(frozen=True)
class ShioajiEnvironmentSecretProvider:
    """Shioaji env/file credentials, isolated from execution application code."""

    env: Mapping[str, str] | None = None

    def load(self, connection: BrokerConnectionSettings) -> BrokerSecretMaterial:
        if connection.broker_name != "shioaji":
            raise SecretConfigurationError(("secret_broker_mismatch",))
        if not connection.secret_ref.startswith("environment:"):
            raise SecretConfigurationError(("unsupported_secret_ref",))

        values = os.environ if self.env is None else self.env
        ca_path_value = (
            values.get("SJ_CA_CERT_PATH", "").strip()
            or values.get("CA_CERT_PATH", "").strip()
        )
        ca_password = (
            values.get("SJ_CA_PASSWORD", "").strip()
            or values.get("CA_PASSWORD", "").strip()
        )
        raw = {
            "missing_api_key": values.get("SJ_API_KEY", "").strip(),
            "missing_secret_key": values.get("SJ_SECRET_KEY", "").strip(),
            "missing_ca_certificate": ca_path_value,
            "missing_ca_password": ca_password,
        }
        missing = tuple(code for code, value in raw.items() if not value)
        if missing:
            raise SecretConfigurationError(missing)

        ca_path = Path(ca_path_value)
        issues: list[str] = []
        try:
            ca_stat = ca_path.stat()
        except OSError:
            issues.append("ca_certificate_unavailable")
        else:
            if not stat.S_ISREG(ca_stat.st_mode) or ca_path.is_symlink():
                issues.append("ca_certificate_not_regular_file")
            if stat.S_IMODE(ca_stat.st_mode) & 0o077:
                issues.append("ca_certificate_permissions_too_open")
        if issues:
            raise SecretConfigurationError(tuple(issues))

        return BrokerSecretMaterial(
            values=(
                ("api_key", raw["missing_api_key"]),
                ("secret_key", raw["missing_secret_key"]),
                ("ca_password", raw["missing_ca_password"]),
            ),
            files=(ca_path,),
        )

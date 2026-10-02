from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat
from typing import Mapping

from .execution_targets import ExecutionTarget
from .secret_factory import build_broker_secret_provider
from .secrets import BrokerSecretMaterial, SecretConfigurationError
from .settings import BrokerConnectionSettings


_FILE_REF = re.compile(r"^file:broker-secrets/(exec_[A-Za-z0-9]{16,64})$")
_ENV_REF = "environment:primary"
_REQUIRED_VALUES = ("SJ_API_KEY", "SJ_SECRET_KEY", "SJ_CA_PASSWORD")


def _secure_regular_file(path: Path, unavailable: str, insecure: str) -> None:
    try:
        value = path.lstat()
    except OSError as exc:
        raise SecretConfigurationError((unavailable,)) from exc
    if stat.S_ISLNK(value.st_mode) or not stat.S_ISREG(value.st_mode):
        raise SecretConfigurationError(("secret_file_not_regular",))
    if stat.S_IMODE(value.st_mode) & 0o077:
        raise SecretConfigurationError((insecure,))


def _read_credentials(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise SecretConfigurationError(("credentials_file_unavailable",)) from exc
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise SecretConfigurationError(("invalid_credentials_file",))
        name, value = line.split("=", 1)
        name, value = name.strip(), value.strip()
        if name not in _REQUIRED_VALUES or name in result or not value:
            raise SecretConfigurationError(("invalid_credentials_file",))
        result[name] = value
    missing = tuple(
        "missing_" + name.removeprefix("SJ_").lower()
        for name in _REQUIRED_VALUES
        if name not in result
    )
    if missing:
        raise SecretConfigurationError(missing)
    return result


@dataclass(frozen=True)
class PerTargetSecretResolver:
    """Exact file/environment resolver with no cross-target or backend fallback."""

    secret_root: Path
    env: Mapping[str, str] | None = None

    def resolve(
        self, target: ExecutionTarget, connection: BrokerConnectionSettings
    ) -> BrokerSecretMaterial:
        if target.account_ref != connection.account_ref:
            raise SecretConfigurationError(("secret_target_identity_mismatch",))
        if target.secret_ref != connection.secret_ref:
            raise SecretConfigurationError(("secret_ref_mismatch",))
        if target.secret_ref == _ENV_REF:
            provider = build_broker_secret_provider(connection, env=self.env)
            return provider.load(connection)

        match = _FILE_REF.fullmatch(target.secret_ref)
        if match is None:
            raise SecretConfigurationError(("unsupported_secret_ref",))
        if match.group(1) != target.target_id:
            raise SecretConfigurationError(("secret_target_ref_mismatch",))
        if connection.broker_name != "shioaji":
            raise SecretConfigurationError(("secret_broker_mismatch",))

        root = self.secret_root
        if not root.is_absolute():
            raise SecretConfigurationError(("secret_root_not_absolute",))
        try:
            root_stat = root.lstat()
        except OSError as exc:
            raise SecretConfigurationError(("secret_root_unavailable",)) from exc
        if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
            raise SecretConfigurationError(("secret_root_not_directory",))

        target_dir = root / target.target_id
        try:
            directory_stat = target_dir.lstat()
        except OSError as exc:
            raise SecretConfigurationError(("target_secret_unavailable",)) from exc
        if stat.S_ISLNK(directory_stat.st_mode) or not stat.S_ISDIR(directory_stat.st_mode):
            raise SecretConfigurationError(("target_secret_not_directory",))
        try:
            target_dir.resolve(strict=True).relative_to(root.resolve(strict=True))
        except (OSError, ValueError) as exc:
            raise SecretConfigurationError(("secret_path_escape",)) from exc

        credentials_path = target_dir / "credentials.env"
        ca_path = target_dir / "shioaji-ca.pfx"
        _secure_regular_file(
            credentials_path,
            "credentials_file_unavailable",
            "credentials_file_permissions_too_open",
        )
        _secure_regular_file(
            ca_path,
            "ca_certificate_unavailable",
            "ca_certificate_permissions_too_open",
        )
        values = _read_credentials(credentials_path)
        return BrokerSecretMaterial(
            values=(
                ("api_key", values["SJ_API_KEY"]),
                ("secret_key", values["SJ_SECRET_KEY"]),
                ("ca_password", values["SJ_CA_PASSWORD"]),
            ),
            files=(ca_path,),
        )

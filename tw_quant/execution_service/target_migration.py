from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import shutil

from ..broker import (
    BrokerAccountRef,
    ExecutionTarget,
    ExecutionTargetStatus,
    SQLiteExecutionTargetRepository,
    legacy_target_id,
    mask_account_id,
)


@dataclass(frozen=True)
class ProductionTargetMigration:
    """Idempotent, non-destructive adoption of one externally supplied account."""

    database_path: Path
    secret_root: Path

    def apply(
        self, *, owner_user_id: str, broker_name: str, account_id: str,
        credentials_file: Path, ca_file: Path,
    ):
        account = BrokerAccountRef(broker_name, account_id)
        repository = SQLiteExecutionTargetRepository(self.database_path)
        try:
            existing = repository.find_by_account_ref(account)
            if existing is not None:
                if existing.owner_user_id != owner_user_id:
                    raise PermissionError("production target owner mismatch")
                return existing
            target_id = legacy_target_id(owner_user_id, account)
            target = ExecutionTarget(
                target_id=target_id, owner_user_id=owner_user_id,
                broker_name=account.broker_name, account_id=account.account_id,
                masked_account_id=mask_account_id(account.account_id),
                secret_ref=f"file:broker-secrets/{target_id}",
                status=ExecutionTargetStatus.LOCKED,
                metadata={"source": "explicit_account_adoption"},
            )
            target_dir = self.secret_root / target.target_id
            if target_dir.exists():
                raise RuntimeError("target secret directory already exists")
            target_dir.mkdir(parents=True, mode=0o700)
            try:
                shutil.copyfile(credentials_file, target_dir / "credentials.env")
                shutil.copyfile(ca_file, target_dir / "shioaji-ca.pfx")
                os.chmod(target_dir / "credentials.env", 0o600)
                os.chmod(target_dir / "shioaji-ca.pfx", 0o600)
                repository.create(target)
                repository.update_status(target.target_id, ExecutionTargetStatus.ACTIVE)
                migrated = repository.get(target.target_id)
                assert migrated is not None
                return migrated
            except Exception:
                if repository.exists(target.target_id):
                    repository.update_status(target.target_id, ExecutionTargetStatus.LOCKED)
                if target_dir.exists():
                    target_dir.rename(target_dir.with_name(target_dir.name + ".failed"))
                raise
        finally:
            repository.close()

    def rollback(self, *, owner_user_id: str, target_id: str) -> None:
        repository = SQLiteExecutionTargetRepository(self.database_path)
        try:
            target = repository.get_owned(owner_user_id, target_id)
            if target is None:
                raise KeyError("unknown owned execution target")
            repository.update_status(target_id, ExecutionTargetStatus.LOCKED)
            source = self.secret_root / target_id
            if source.exists():
                destination = self.secret_root / f"{target_id}.rollback"
                if destination.exists():
                    raise RuntimeError("rollback destination already exists")
                source.rename(destination)
        finally:
            repository.close()

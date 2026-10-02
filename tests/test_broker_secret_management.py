from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import tempfile
import unittest

from tw_quant.broker import (
    BrokerConnectionSettings,
    ExecutionTarget,
    ExecutionTargetStatus,
    PerTargetSecretResolver,
    SecretConfigurationError,
    SQLiteExecutionTargetRepository,
    mask_account_id,
)
from tw_quant.execution_service import build_execution_service


NOW = datetime(2026, 9, 14, tzinfo=timezone.utc)


def make_target(target_id: str, account_id: str, *, status=ExecutionTargetStatus.ACTIVE):
    return ExecutionTarget(
        target_id=target_id,
        owner_user_id="owner-1",
        broker_name="shioaji",
        account_id=account_id,
        masked_account_id=mask_account_id(account_id),
        secret_ref=f"file:broker-secrets/{target_id}",
        status=status,
        created_at=NOW,
        updated_at=NOW,
    )


class PerTargetSecretResolverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "brokers"
        self.root.mkdir(mode=0o700)

    def tearDown(self):
        self.temp.cleanup()

    def provision(self, target: ExecutionTarget, marker: str = "a") -> None:
        directory = self.root / target.target_id
        directory.mkdir(mode=0o700)
        credentials = directory / "credentials.env"
        credentials.write_text(
            f"SJ_API_KEY=api-{marker}\nSJ_SECRET_KEY=secret-{marker}\n"
            f"SJ_CA_PASSWORD=password-{marker}\n",
            encoding="utf-8",
        )
        credentials.chmod(0o600)
        certificate = directory / "shioaji-ca.pfx"
        certificate.write_bytes(f"certificate-{marker}".encode())
        certificate.chmod(0o600)

    @staticmethod
    def connection(target: ExecutionTarget) -> BrokerConnectionSettings:
        return BrokerConnectionSettings(
            target.target_id, target.broker_name, target.account_id, True, target.secret_ref
        )

    def test_exact_file_ref_resolves_only_its_target(self):
        first = make_target("exec_aaaaaaaaaaaaaaaa", "account-a")
        second = make_target("exec_bbbbbbbbbbbbbbbb", "account-b")
        self.provision(first, "a")
        self.provision(second, "b")
        resolver = PerTargetSecretResolver(self.root, {
            "SJ_API_KEY": "must-not-fallback",
            "SJ_SECRET_KEY": "must-not-fallback",
            "SJ_CA_PASSWORD": "must-not-fallback",
        })
        first_material = resolver.resolve(first, self.connection(first))
        second_material = resolver.resolve(second, self.connection(second))
        self.assertEqual(dict(first_material.values)["api_key"], "api-a")
        self.assertEqual(dict(second_material.values)["api_key"], "api-b")
        with self.assertRaisesRegex(SecretConfigurationError, "secret_target_identity_mismatch"):
            resolver.resolve(first, self.connection(second))

    def test_ref_parser_rejects_traversal_and_cross_target_ref(self):
        target = make_target("exec_aaaaaaaaaaaaaaaa", "account-a")
        for secret_ref, issue in (
            ("file:broker-secrets/../../environment", "unsupported_secret_ref"),
            ("file:broker-secrets/exec_bbbbbbbbbbbbbbbb", "secret_target_ref_mismatch"),
            ("file:/opt/platform/secrets/brokers/exec_aaaaaaaaaaaaaaaa", "unsupported_secret_ref"),
        ):
            changed = ExecutionTarget(
                **{**target.__dict__, "secret_ref": secret_ref}
            )
            with self.subTest(secret_ref=secret_ref), self.assertRaisesRegex(
                SecretConfigurationError, issue
            ):
                PerTargetSecretResolver(self.root).resolve(
                    changed,
                    BrokerConnectionSettings(
                        changed.target_id, "shioaji", changed.account_id, True, secret_ref
                    ),
                )

    def test_missing_file_never_falls_back_to_environment(self):
        target = make_target("exec_aaaaaaaaaaaaaaaa", "account-a")
        (self.root / target.target_id).mkdir(mode=0o700)
        resolver = PerTargetSecretResolver(self.root, {
            "SJ_API_KEY": "env-api", "SJ_SECRET_KEY": "env-secret",
            "SJ_CA_PASSWORD": "env-password",
        })
        with self.assertRaisesRegex(SecretConfigurationError, "credentials_file_unavailable"):
            resolver.resolve(target, self.connection(target))

    def test_symlinks_and_open_permissions_fail_closed(self):
        target = make_target("exec_aaaaaaaaaaaaaaaa", "account-a")
        self.provision(target)
        credentials = self.root / target.target_id / "credentials.env"
        credentials.chmod(0o644)
        with self.assertRaisesRegex(SecretConfigurationError, "permissions_too_open"):
            PerTargetSecretResolver(self.root).resolve(target, self.connection(target))
        credentials.unlink()
        source = Path(self.temp.name) / "outside.env"
        source.write_text("SJ_API_KEY=x\nSJ_SECRET_KEY=y\nSJ_CA_PASSWORD=z\n")
        source.chmod(0o600)
        credentials.symlink_to(source)
        with self.assertRaisesRegex(SecretConfigurationError, "secret_file_not_regular"):
            PerTargetSecretResolver(self.root).resolve(target, self.connection(target))

    def test_material_repr_contains_no_credentials_or_paths(self):
        target = make_target("exec_aaaaaaaaaaaaaaaa", "account-a")
        self.provision(target, "private")
        material = PerTargetSecretResolver(self.root).resolve(target, self.connection(target))
        serialized = repr(material)
        for value in ("api-private", "secret-private", "password-private", str(self.root)):
            self.assertNotIn(value, serialized)


class TargetRuntimeCompositionTests(unittest.IsolatedAsyncioTestCase):
    async def test_multiple_active_targets_fail_before_secret_resolution(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "live.sqlite3"
            repository = SQLiteExecutionTargetRepository(database)
            repository.create(make_target("exec_aaaaaaaaaaaaaaaa", "account-a"))
            repository.create(make_target("exec_bbbbbbbbbbbbbbbb", "account-b"))
            repository.close()
            runtime = build_execution_service(env={
                "BROKER_PROVIDER": "shioaji",
                "LIVE_BROKER_READ_ONLY_ENABLED": "true",
                "LIVE_BROKER_READ_ONLY_CONFIRMATION": "I_UNDERSTAND_PRODUCTION_READ_ONLY",
                "LIVE_ALLOWED_ACCOUNT_IDS": "account-a,account-b",
                "LIVE_BROKER_INSTRUMENT_MAP_JSON": json.dumps([{
                    "symbol": "TMF", "contract": "TMF-202609", "broker_contract": "TMFU6"
                }]),
                "LIVE_EXECUTION_DB_PATH": str(database),
                "LIVE_EXECUTION_HEALTH_PATH": str(root / "health.json"),
                "LIVE_BROKER_SECRET_ROOT": str(root / "missing-root"),
            })
            try:
                self.assertIn("active_execution_target_limit_exceeded", runtime.issues)
                self.assertIsNone(runtime.manager)
                self.assertEqual(runtime.worker.snapshot()["dispatches"], 0)
            finally:
                await runtime.close()

    async def test_disabled_target_does_not_fallback_to_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "live.sqlite3"
            repository = SQLiteExecutionTargetRepository(database)
            repository.create(make_target(
                "exec_aaaaaaaaaaaaaaaa", "account-a", status=ExecutionTargetStatus.DISABLED
            ))
            repository.close()
            runtime = build_execution_service(env={
                "BROKER_PROVIDER": "shioaji", "LIVE_TRADING_ENABLED": "true",
                "LIVE_TRADING_CONFIRMATION": "I_UNDERSTAND_LIVE_ORDERS",
                "LIVE_ALLOWED_ACCOUNT_IDS": "account-a",
                "LIVE_EXECUTION_DB_PATH": str(database),
                "LIVE_EXECUTION_HEALTH_PATH": str(root / "health.json"),
                "SJ_API_KEY": "fallback-api", "SJ_SECRET_KEY": "fallback-secret",
                "SJ_CA_PASSWORD": "fallback-password",
            })
            try:
                self.assertIn("execution_target_not_active", runtime.issues)
                self.assertIsNone(runtime.manager)
                self.assertNotIn("fallback-api", repr(runtime))
            finally:
                await runtime.close()


if __name__ == "__main__":
    unittest.main()

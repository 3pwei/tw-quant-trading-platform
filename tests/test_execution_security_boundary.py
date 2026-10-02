from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from tw_quant.broker import (
    BrokerAccountRef,
    BrokerConnectionSettings,
    BrokerOrderRequest,
    BrokerSecretMaterial,
    CompositeOrderAdmissionGate,
    ExecutionMode,
    ExecutionTargetStatus,
    ExecutionTarget,
    SQLiteExecutionTargetRepository,
    legacy_target_id,
    mask_account_id,
    LockedOrderAdmissionGate,
    RecoveryStatus,
    RoutedBrokerOrderRequest,
    SQLiteRecoveryLockRepository,
)
from tw_quant.execution_service import build_execution_service
from tw_quant.execution_service.__main__ import _healthcheck
from tw_quant.execution_service.config import ExecutionServiceSettings
from tw_quant.execution_service.health import (
    BrokerConnectionHealth,
    ExecutionServiceHealth,
)
from tw_quant.execution_service.redaction import SecretRedactionFilter
from tw_quant.market_data.settings import MarketDataSettings
from tw_quant.paper import SQLitePaperRepository


ROOT = Path(__file__).resolve().parents[1]
CONFIRMATION = "I_UNDERSTAND_LIVE_ORDERS"


class CountingGate:
    def __init__(self, *, reject: bool = False):
        self.calls = 0
        self.reject = reject

    def assert_ordering_allowed(self) -> None:
        self.calls += 1
        if self.reject:
            raise RuntimeError("rejected")


class ExecutionSecurityBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.ca = self.root / "broker-ca.pfx"
        self.ca.write_bytes(b"test-certificate-placeholder")
        self.ca.chmod(0o600)

    def tearDown(self):
        self.temp.cleanup()

    def environment(self, **changes: str) -> dict[str, str]:
        owner = changes.pop("LIVE_EXECUTION_OWNER_USER_ID", "owner-1")
        account = BrokerAccountRef("shioaji", "account-1234")
        target_id = legacy_target_id(owner, account)
        database = self.root / "live.sqlite3"
        repository = SQLiteExecutionTargetRepository(database)
        if not repository.exists(target_id) and not repository.list_all():
            repository.create(ExecutionTarget(
                target_id, owner, account.broker_name, account.account_id,
                mask_account_id(account.account_id),
                f"file:broker-secrets/{target_id}",
            ))
        repository.close()
        secret_dir = self.root / "brokers" / target_id
        secret_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        missing_api_key = changes.pop("SJ_API_KEY", None)
        missing_secret_key = changes.pop("SJ_SECRET_KEY", None)
        missing_ca_password = changes.pop("SJ_CA_PASSWORD", None)
        if any(
            value not in (None, "")
            for value in (missing_api_key, missing_secret_key, missing_ca_password)
        ):
            raise ValueError("credential fixture overrides may only make a value missing")
        credentials = secret_dir / "credentials.env"
        credentials.write_text(
            ("SJ_API_KEY=\n" if missing_api_key == "" else "SJ_API_KEY=api-fixture\n")
            + (
                "SJ_SECRET_KEY=\n"
                if missing_secret_key == ""
                else "SJ_SECRET_KEY=secret-fixture\n"
            )
            + (
                "SJ_CA_PASSWORD=\n"
                if missing_ca_password == ""
                else "SJ_CA_PASSWORD=password-fixture\n"
            )
        )
        credentials.chmod(0o600)
        ca_target = secret_dir / "shioaji-ca.pfx"
        if ca_target.exists():
            ca_target.unlink()
        source_ca = Path(changes.pop("SJ_CA_CERT_PATH", self.ca))
        if source_ca.exists():
            ca_target.write_bytes(source_ca.read_bytes())
            ca_target.chmod(source_ca.stat().st_mode & 0o777)
        values = {
            "BROKER_PROVIDER": "shioaji",
            "LIVE_EXECUTION_OWNER_USER_ID": owner,
            "LIVE_EXECUTION_TARGET_ID": target_id,
            "LIVE_TRADING_ENABLED": "true",
            "LIVE_TRADING_CONFIRMATION": CONFIRMATION,
            "LIVE_BROKER_ACCOUNT_ID": "account-1234",
            "LIVE_ALLOWED_ACCOUNT_IDS": "account-1234",
            "LIVE_EXECUTION_DB_PATH": str(self.root / "live.sqlite3"),
            "LIVE_BROKER_SECRET_ROOT": str(self.root / "brokers"),
            "LIVE_EXECUTION_HEALTH_PATH": str(self.root / "health.json"),
        }
        values.update(changes)
        return values

    async def test_execution_composition_root_starts_locked(self):
        runtime = build_execution_service(env=self.environment())
        try:
            await runtime.start()
            health = runtime.public_health()
            self.assertTrue(health["enabled"])
            self.assertTrue(health["locked"])
            self.assertEqual(health["recovery_status"], "locked")
            self.assertEqual(health["broker_name"], "shioaji")
            self.assertEqual(health["execution_state"], "locked")
            self.assertEqual(runtime.worker.snapshot()["dispatches"], 0)
        finally:
            await runtime.close()

    async def test_owned_target_loads_without_enabling_orders(self):
        runtime = build_execution_service(env=self.environment(
            LIVE_CANARY_ALLOWED_OWNER_IDS="owner-1",
        ))
        try:
            targets = runtime.execution_target_repository.list_for_owner("owner-1")
            self.assertEqual(len(targets), 1)
            self.assertEqual(targets[0].account_ref, BrokerAccountRef("shioaji", "account-1234"))
            self.assertTrue(runtime.public_health()["locked"])
            self.assertEqual(runtime.worker.snapshot()["dispatches"], 0)
        finally:
            await runtime.close()

    async def test_missing_canonical_owner_does_not_route_or_unlock(self):
        env = self.environment()
        env["LIVE_EXECUTION_OWNER_USER_ID"] = ""
        runtime = build_execution_service(env=env)
        try:
            self.assertIn("missing_execution_owner_user_id", runtime.issues)
            self.assertIsNone(runtime.manager)
            self.assertTrue(runtime.public_health()["locked"])
        finally:
            await runtime.close()

    async def test_disabled_owned_target_blocks_execution_composition(self):
        env = self.environment(LIVE_CANARY_ALLOWED_OWNER_IDS="owner-1")
        first = build_execution_service(env=env)
        first.execution_target_repository.update_status(
            first.execution_target_repository.list_for_owner("owner-1")[0].target_id,
            ExecutionTargetStatus.DISABLED,
        )
        await first.close()
        runtime = build_execution_service(env=env)
        try:
            self.assertIn("execution_target_not_active", runtime.issues)
            self.assertIsNone(runtime.manager)
            self.assertTrue(runtime.public_health()["locked"])
        finally:
            await runtime.close()

    async def test_default_configuration_remains_disabled(self):
        runtime = build_execution_service(
            env={
                "LIVE_EXECUTION_DB_PATH": str(self.root / "disabled.sqlite3"),
                "LIVE_EXECUTION_HEALTH_PATH": str(
                    self.root / "disabled-health.json"
                ),
            }
        )
        try:
            health = runtime.public_health()
            self.assertEqual(health["broker_name"], "disabled")
            self.assertEqual(health["execution_state"], "disabled")
            self.assertTrue(health["locked"])
            self.assertIn("execution_disabled", runtime.issues)
            self.assertEqual(runtime.worker.snapshot()["dispatches"], 0)
        finally:
            await runtime.close()

    async def test_every_invalid_configuration_is_locked(self):
        cases = {
            "invalid_credentials_file": {"SJ_API_KEY": ""},
            "invalid_credentials_file": {"SJ_SECRET_KEY": ""},
            "ca_certificate_unavailable": {
                "SJ_CA_CERT_PATH": str(self.root / "missing.pfx")
            },
            "invalid_live_trading_confirmation": {
                "LIVE_TRADING_CONFIRMATION": "wrong"
            },
            "execution_target_owner_or_id_mismatch": {
                "LIVE_EXECUTION_TARGET_ID": "exec_9999999999999999"
            },
        }
        for expected_issue, changes in cases.items():
            with self.subTest(case=expected_issue):
                runtime = build_execution_service(env=self.environment(**changes))
                try:
                    self.assertTrue(runtime.locked)
                    self.assertIn(expected_issue, runtime.issues)
                    self.assertEqual(runtime.worker.snapshot()["dispatches"], 0)
                finally:
                    await runtime.close()

    async def test_reconciliation_settings_validate_fail_closed(self):
        cases = {
            "invalid_reconciliation_interval": {
                "LIVE_RECONCILIATION_INTERVAL_SECONDS": "1"
            },
            "invalid_reconciliation_timeout": {
                "LIVE_RECONCILIATION_TIMEOUT_SECONDS": "60"
            },
            "invalid_reconciliation_stale_threshold": {
                "LIVE_RECONCILIATION_STALE_SECONDS": "45"
            },
            "invalid_callback_queue_size": {"LIVE_CALLBACK_QUEUE_SIZE": "0"},
        }
        for expected, change in cases.items():
            with self.subTest(expected=expected):
                runtime = build_execution_service(env=self.environment(**change))
                try:
                    self.assertIn(expected, runtime.issues)
                    self.assertIsNone(runtime.read_only_client)
                    self.assertEqual(runtime.worker.snapshot()["dispatches"], 0)
                finally:
                    await runtime.close()

    async def test_open_ca_permissions_fail_closed(self):
        self.ca.chmod(0o644)
        runtime = build_execution_service(env=self.environment())
        try:
            self.assertIn("ca_certificate_permissions_too_open", runtime.issues)
            self.assertIsNone(runtime.manager)
        finally:
            await runtime.close()

    async def test_legacy_credential_environment_is_not_required(self):
        env = self.environment()
        runtime = build_execution_service(env=env)
        try:
            self.assertIsNotNone(runtime.manager)
            self.assertNotIn("missing_ca_certificate", runtime.issues)
            self.assertNotIn("missing_ca_password", runtime.issues)
            self.assertEqual(runtime.worker.snapshot()["dispatches"], 0)
        finally:
            await runtime.close()

    async def test_even_complete_configuration_has_no_submit_path(self):
        runtime = build_execution_service(env=self.environment())
        try:
            self.assertIsNotNone(runtime.manager)
            self.assertIsNotNone(runtime.recovery_repository)
            account = BrokerAccountRef("shioaji", "account-1234")
            registration = runtime.manager.registry.registration(account)
            self.assertEqual(registration.port.broker_name, "shioaji")
            self.assertEqual(registration.state.value, "locked")
            attempt = runtime.recovery_repository.begin(
                "shioaji", "account-1234", updated_at=datetime.now(timezone.utc)
            )
            runtime.recovery_repository.complete(
                "shioaji",
                "account-1234",
                (),
                expected_generation=attempt.generation,
                updated_at=datetime.now(timezone.utc),
            )
            request = BrokerOrderRequest(
                client_order_id="never-submitted",
                owner_id="owner",
                strategy_id="strategy",
                strategy_version=1,
                symbol="TMF",
                contract="TMFR1",
                side="buy",
                quantity=1,
                mode=ExecutionMode.LIVE,
            )
            with self.assertRaisesRegex(RuntimeError, "locked"):
                runtime.manager.create(RoutedBrokerOrderRequest(account, request))
            self.assertEqual(runtime.order_repository.orders(), [])
            self.assertEqual(runtime.worker.snapshot()["dispatches"], 0)
        finally:
            await runtime.close()

    async def test_paper_and_live_persistence_are_separate(self):
        runtime = build_execution_service(env=self.environment())
        paper = SQLitePaperRepository(self.root / "live.sqlite3")
        try:
            with sqlite3.connect(self.root / "live.sqlite3") as connection:
                tables = {
                    row[0] for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
            self.assertTrue({
                "live_orders", "live_order_outbox", "live_recovery_lock"
            }.issubset(tables))
            self.assertTrue({
                "paper_events", "paper_order_read_model",
                "paper_fill_read_model", "paper_position_read_model",
            }.issubset(tables))
        finally:
            paper.close()
            await runtime.close()

    async def test_health_masks_account_and_never_serializes_secrets(self):
        env = self.environment()
        runtime = build_execution_service(env=env)
        try:
            health = runtime.public_health()
            self.assertEqual(health["masked_account_id"], "****1234")
            self.assertEqual(
                set(health),
                {
                    "broker_name",
                    "masked_account_id",
                    "execution_state",
                    "enabled",
                    "locked",
                    "recovery_status",
                    "connected",
                    "ca_ready",
                    "read_only",
                    "callback_registered",
                    "last_broker_read_time",
                    "last_callback_time",
                    "ordering_enabled",
                    "broker_accounts",
                },
            )
            payload = json.dumps(health)
            for key in ("SJ_API_KEY", "SJ_SECRET_KEY", "CA_PASSWORD"):
                self.assertNotIn(key, payload)
            for value in (
                "api-test-value", "secret-test-value", "ca-test-value",
                "account-1234", str(self.ca),
            ):
                self.assertNotIn(value, payload)
        finally:
            await runtime.close()

    async def test_container_health_accepts_read_only_ready_only_with_zero_writes(self):
        path = self.root / "container-health.json"
        path.write_text(json.dumps({
            "heartbeat_at": datetime.now(timezone.utc).isoformat(),
            "locked": True,
            "execution_state": "ready_read_only",
            "external_order_calls": 0,
            "external_cancel_calls": 0,
        }), encoding="utf-8")
        settings = ExecutionServiceSettings(health_path=str(path))
        self.assertEqual(_healthcheck(settings), 0)
        document = json.loads(path.read_text(encoding="utf-8"))
        document["external_cancel_calls"] = 1
        path.write_text(json.dumps(document), encoding="utf-8")
        self.assertEqual(_healthcheck(settings), 1)

    async def test_fake_broker_secret_lookup_is_isolated_by_full_identity(self):
        first = BrokerAccountRef("broker-a", "account-1")
        second = BrokerAccountRef("broker-b", "account-1")
        materials = {
            first: BrokerSecretMaterial((("token", "credential-a"),)),
            second: BrokerSecretMaterial((("token", "credential-b"),)),
        }

        class FakeSecretProvider:
            def load(self, connection: BrokerConnectionSettings) -> BrokerSecretMaterial:
                account = connection.account_ref
                if account is None or account not in materials:
                    raise AssertionError("credential lookup crossed connection identity")
                return materials[account]

        provider = FakeSecretProvider()
        first_material = provider.load(BrokerConnectionSettings(
            "connection-a", "broker-a", "account-1", True, "fake:a"
        ))
        second_material = provider.load(BrokerConnectionSettings(
            "connection-b", "broker-b", "account-1", True, "fake:b"
        ))
        self.assertNotEqual(first, second)
        self.assertEqual(first_material.values[0][1], "credential-a")
        self.assertEqual(second_material.values[0][1], "credential-b")

    async def test_recovery_is_isolated_by_broker_and_account(self):
        repository = SQLiteRecoveryLockRepository(self.root / "recovery.sqlite3")
        now = datetime.now(timezone.utc)
        try:
            attempt = repository.begin("broker-a", "account-1", updated_at=now)
            repository.complete(
                "broker-a", "account-1", (),
                expected_generation=attempt.generation, updated_at=now,
            )
            first = repository.state("broker-a", "account-1")
            second = repository.state("broker-b", "account-1")
            self.assertEqual(first.status, RecoveryStatus.READY)
            self.assertEqual(second.status, RecoveryStatus.LOCKED)
            self.assertEqual(first.account_id, second.account_id)
            self.assertNotEqual(first.broker_name, second.broker_name)
        finally:
            repository.close()

    async def test_health_collection_represents_same_account_at_two_brokers(self):
        health = ExecutionServiceHealth((
            BrokerConnectionHealth(
                "connection-a", "broker-a",
                BrokerAccountRef("broker-a", "account-0001"),
                "ready", True, False, "ready",
            ),
            BrokerConnectionHealth(
                "connection-b", "broker-b",
                BrokerAccountRef("broker-b", "account-0001"),
                "locked", True, True, "locked",
            ),
        )).to_public_dict()
        connections = health["broker_accounts"]
        self.assertEqual(len(connections), 2)
        self.assertEqual(connections[0]["broker_name"], "broker-a")
        self.assertEqual(connections[1]["broker_name"], "broker-b")
        self.assertEqual(connections[0]["masked_account_id"], "****0001")
        self.assertEqual(connections[1]["masked_account_id"], "****0001")

    async def test_operational_log_has_masked_broker_account_context(self):
        runtime = build_execution_service(env=self.environment())
        try:
            with self.assertLogs("tw_quant.execution_service", logging.INFO) as logs:
                task = asyncio.create_task(runtime.serve())
                await asyncio.sleep(0)
                await runtime.close()
                await task
            payload = " ".join(logs.output)
            self.assertIn("broker=shioaji", payload)
            self.assertIn("account=****1234", payload)
            self.assertNotIn("account-1234", payload)
        finally:
            await runtime.close()


class BoundaryArchitectureTests(unittest.TestCase):
    def test_composite_gate_stops_at_first_rejection(self):
        first = CountingGate(reject=True)
        second = CountingGate()
        gate = CompositeOrderAdmissionGate((first, second))
        with self.assertRaisesRegex(RuntimeError, "rejected"):
            gate.assert_ordering_allowed()
        self.assertEqual(first.calls, 1)
        self.assertEqual(second.calls, 0)

    def test_locked_gate_always_rejects(self):
        with self.assertRaisesRegex(RuntimeError, "not implemented"):
            LockedOrderAdmissionGate().assert_ordering_allowed()

    def test_public_application_does_not_construct_production_execution(self):
        source = (ROOT / "tw_quant/live/api.py").read_text(encoding="utf-8")
        self.assertIn("ExecutionHealthFileMonitor", source)
        self.assertNotIn("ShioajiBrokerAdapter", source)
        self.assertNotIn("execution_service", source)
        self.assertNotIn("CA_CERT_PATH", source)



    def test_market_production_does_not_read_live_secret_names(self):
        with mock.patch.dict(
            "os.environ",
            {
                "PLATFORM_ENVIRONMENT": "production",
                "MARKET_DATA_PROVIDER": "shioaji",
                "SJ_API_KEY": "live-only-api-key",
                "SJ_SEC_KEY": "legacy-live-secret",
            },
            clear=True,
        ):
            settings = MarketDataSettings.from_env()
        self.assertIsNone(settings.shioaji_api_key)
        self.assertIsNone(settings.shioaji_secret_key)

    def test_secret_redaction_removes_values_from_logs(self):
        record = logging.LogRecord(
            "test", logging.INFO, __file__, 1,
            "login api-value secret-value /sensitive/ca.pfx account-1234", (), None,
        )
        redactor = SecretRedactionFilter((
            "api-value", "secret-value", "/sensitive/ca.pfx", "account-1234"
        ))
        self.assertTrue(redactor.filter(record))
        message = record.getMessage()
        for value in (
            "api-value", "secret-value", "/sensitive/ca.pfx", "account-1234"
        ):
            self.assertNotIn(value, message)

    def test_execution_service_core_never_imports_shioaji_sdk(self):
        for path in (ROOT / "tw_quant/execution_service").glob("*.py"):
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("import shioaji", source, path.name)
            self.assertNotIn("from shioaji", source, path.name)
            self.assertNotIn("broker.shioaji", source, path.name)
            self.assertNotIn("SJ_API_KEY", source, path.name)
            self.assertNotIn("SJ_SECRET_KEY", source, path.name)

    def test_production_recovery_worker_has_no_dispatch_or_write_call(self):
        source = (ROOT / "tw_quant/broker/worker.py").read_text(encoding="utf-8")
        read_only_worker = source.split("class BrokerAccountWorker:", 1)[1].split(
            "class ExecutionSupervisor:", 1
        )[0]
        for fscriptedidden in ("dispatch_once", "submit_order", "cancel_order", "replace"):
            self.assertNotIn(fscriptedidden, read_only_worker)
        composition = (ROOT / "tw_quant/execution_service/runtime.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("dispatch_once", composition)


if __name__ == "__main__":
    unittest.main()

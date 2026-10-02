from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from tw_quant.broker import (
    BrokerAccountRef,
    BrokerCapabilities,
    BrokerConnectionSettings,
    BrokerRegistration,
    BrokerRegistry,
    BrokerRuntimeState,
    ExecutionTarget,
    ExecutionTargetStatus,
    LockedBroker,
    LockedInstrumentMapper,
    OwnedExecutionTargetResolver,
    SQLiteExecutionTargetRepository,
    bootstrap_legacy_execution_target,
    mask_account_id,
)


NOW = datetime(2026, 9, 14, tzinfo=timezone.utc)
ACCOUNT = BrokerAccountRef("shioaji", "account-secret-1234")


def target(
    target_id: str = "exec_0123456789abcdef",
    owner: str = "user-a",
    account: BrokerAccountRef = ACCOUNT,
    status: ExecutionTargetStatus = ExecutionTargetStatus.ACTIVE,
) -> ExecutionTarget:
    return ExecutionTarget(
        target_id=target_id,
        owner_user_id=owner,
        broker_name=account.broker_name,
        account_id=account.account_id,
        masked_account_id=mask_account_id(account.account_id),
        secret_ref="environment:primary",
        status=status,
        created_at=NOW,
        updated_at=NOW,
    )


class ExecutionTargetModelTests(unittest.TestCase):
    def test_valid_target_converts_to_existing_broker_identity(self):
        value = target()
        self.assertEqual(value.account_ref, ACCOUNT)
        self.assertEqual(value.to_public_dict(), {
            "target_id": value.target_id,
            "provider_label": "configured",
            "masked_account_id": "****1234",
            "status": "active",
        })

    def test_required_identity_and_masking_validation(self):
        with self.assertRaisesRegex(ValueError, "opaque"):
            target(target_id="shioaji_account-secret-1234")
        with self.assertRaisesRegex(ValueError, "owner_user_id"):
            target(owner="")
        with self.assertRaisesRegex(ValueError, "broker_name"):
            ExecutionTarget("exec_0123456789abcdef", "owner", "", "account", "****ount", "ref")
        with self.assertRaisesRegex(ValueError, "account_id"):
            ExecutionTarget("exec_0123456789abcdef", "owner", "broker", "", "****", "ref")
        with self.assertRaisesRegex(ValueError, "masked_account_id"):
            ExecutionTarget("exec_0123456789abcdef", "owner", "broker", "account", "account", "ref")

    def test_repr_and_public_shape_hide_account_and_secret_ref(self):
        value = target()
        serialized = repr(value) + repr(value.to_public_dict())
        self.assertNotIn(value.account_id, serialized)
        self.assertNotIn(value.secret_ref, serialized)


class ExecutionTargetRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "live.sqlite3"
        self.repository = SQLiteExecutionTargetRepository(self.path)

    def tearDown(self):
        self.repository.close()
        self.temp.cleanup()

    def test_create_read_owned_list_status_and_restart(self):
        created = self.repository.create(target())
        self.assertEqual(self.repository.get(created.target_id), created)
        self.assertEqual(self.repository.get_owned("user-a", created.target_id), created)
        self.assertIsNone(self.repository.get_owned("user-b", created.target_id))
        self.assertEqual(self.repository.list_for_owner("user-a"), [created])
        self.assertEqual(self.repository.find_by_account_ref(ACCOUNT), created)
        self.assertTrue(self.repository.exists(created.target_id))
        disabled = self.repository.update_status(created.target_id, ExecutionTargetStatus.DISABLED)
        self.assertEqual(disabled.status, ExecutionTargetStatus.DISABLED)
        self.repository.close()
        self.repository = SQLiteExecutionTargetRepository(self.path)
        self.assertEqual(self.repository.get(created.target_id).status, ExecutionTargetStatus.DISABLED)

    def test_owner_isolation_and_duplicates_are_rejected(self):
        self.repository.create(target())
        account_b = BrokerAccountRef("future-broker", "account-secret-5678")
        target_b = target("exec_fedcba9876543210", "user-b", account_b)
        self.repository.create(target_b)
        self.assertIsNotNone(self.repository.get_owned("user-a", "exec_0123456789abcdef"))
        self.assertIsNone(self.repository.get_owned("user-a", target_b.target_id))
        self.assertIsNotNone(self.repository.get_owned("user-b", target_b.target_id))
        self.assertIsNone(self.repository.get_owned("user-b", "exec_0123456789abcdef"))
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.repository.create(target("exec_1111111111111111"))
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.repository.create(target("exec_0123456789abcdef", account=account_b))

    def test_schema_migration_is_idempotent_and_preserves_existing_live_data(self):
        with self.repository.connection:
            self.repository.connection.execute("CREATE TABLE live_orders_sentinel (id TEXT PRIMARY KEY)")
            self.repository.connection.execute("INSERT INTO live_orders_sentinel VALUES ('keep')")
        self.repository.close()
        self.repository = SQLiteExecutionTargetRepository(self.path)
        self.repository._migrate()
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT id FROM live_orders_sentinel").fetchone()[0], "keep")
            versions = connection.execute("SELECT version FROM execution_target_schema_migrations").fetchall()
        self.assertEqual(versions, [(1,)])


class ExecutionTargetRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repository = SQLiteExecutionTargetRepository(Path(self.temp.name) / "live.sqlite3")
        self.registry = BrokerRegistry()

    def tearDown(self):
        self.repository.close()
        self.temp.cleanup()

    def register(self, account=ACCOUNT, state=BrokerRuntimeState.READY):
        self.registry.register(BrokerRegistration(
            account, LockedBroker(account.broker_name), BrokerCapabilities(),
            LockedInstrumentMapper(), state,
        ))

    def test_exact_active_owned_target_resolves_without_fallback(self):
        value = self.repository.create(target())
        self.register()
        resolver = OwnedExecutionTargetResolver(self.repository, self.registry)
        self.assertEqual(resolver.resolve("user-a", value.target_id), ACCOUNT)
        for owner, target_id in (("user-b", value.target_id), ("user-a", "exec_9999999999999999")):
            with self.subTest(owner=owner, target_id=target_id), self.assertRaises(KeyError):
                resolver.resolve(owner, target_id)

    def test_disabled_locked_unknown_broker_and_broker_mismatch_are_blocked(self):
        for status in (ExecutionTargetStatus.DISABLED, ExecutionTargetStatus.LOCKED):
            with self.subTest(status=status):
                value = self.repository.create(target(status=status))
                self.register()
                with self.assertRaises(RuntimeError):
                    OwnedExecutionTargetResolver(self.repository, self.registry).resolve("user-a", value.target_id)
                self.repository.connection.execute("DELETE FROM execution_targets")
                self.repository.connection.commit()
                self.registry = BrokerRegistry()
        value = self.repository.create(target())
        with self.assertRaises(KeyError):
            OwnedExecutionTargetResolver(self.repository, self.registry).resolve("user-a", value.target_id)
        self.register(state=BrokerRuntimeState.LOCKED)
        with self.assertRaises(RuntimeError):
            OwnedExecutionTargetResolver(self.repository, self.registry).resolve("user-a", value.target_id)


class LegacyExecutionTargetBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repository = SQLiteExecutionTargetRepository(Path(self.temp.name) / "live.sqlite3")
        self.connection = BrokerConnectionSettings(
            "primary", "shioaji", ACCOUNT.account_id, enabled=False,
            secret_ref="environment:primary",
        )

    def tearDown(self):
        self.repository.close()
        self.temp.cleanup()

    def test_bootstrap_is_deterministic_idempotent_and_does_not_activate_trading(self):
        first = bootstrap_legacy_execution_target(
            self.repository, owner_user_ids=frozenset({"user-a"}), connection=self.connection,
        )
        second = bootstrap_legacy_execution_target(
            self.repository, owner_user_ids=frozenset({"user-a"}), connection=self.connection,
        )
        self.assertEqual(first.target_id, second.target_id)
        self.assertEqual(len(self.repository.list_for_owner("user-a")), 1)
        self.assertFalse(self.connection.enabled)
        self.assertEqual(first.status, ExecutionTargetStatus.ACTIVE)

    def test_unknown_or_changed_owner_fails_closed(self):
        for owners in (frozenset(), frozenset({"a", "b"})):
            with self.subTest(owners=owners), self.assertRaisesRegex(ValueError, "exactly one owner"):
                bootstrap_legacy_execution_target(self.repository, owner_user_ids=owners, connection=self.connection)
        bootstrap_legacy_execution_target(
            self.repository, owner_user_ids=frozenset({"user-a"}), connection=self.connection,
        )
        with self.assertRaisesRegex(PermissionError, "owner mismatch"):
            bootstrap_legacy_execution_target(
                self.repository, owner_user_ids=frozenset({"user-b"}), connection=self.connection,
            )


if __name__ == "__main__":
    unittest.main()

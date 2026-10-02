from pathlib import Path
import tempfile
import unittest

from tw_quant.broker import ExecutionTargetStatus, SQLiteExecutionTargetRepository
from tw_quant.execution_service.target_migration import ProductionTargetMigration


class ProductionTargetMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.credentials = self.root / "legacy.env"
        self.credentials.write_text(
            "SJ_API_KEY=not-real\nSJ_SECRET_KEY=not-real\nSJ_CA_PASSWORD=not-real\n"
        )
        self.ca = self.root / "legacy.pfx"
        self.ca.write_bytes(b"not-a-real-certificate")
        self.migration = ProductionTargetMigration(
            self.root / "live.sqlite3", self.root / "brokers"
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_migration_is_idempotent_and_keeps_legacy_sources(self):
        first = self.migration.apply(
            owner_user_id="owner-1", broker_name="example-broker", account_id="account-1",
            credentials_file=self.credentials, ca_file=self.ca,
        )
        second = self.migration.apply(
            owner_user_id="owner-1", broker_name="example-broker", account_id="account-1",
            credentials_file=self.credentials, ca_file=self.ca,
        )
        self.assertEqual(first.target_id, second.target_id)
        self.assertEqual(first.secret_ref, f"file:broker-secrets/{first.target_id}")
        self.assertTrue(self.credentials.exists())
        self.assertEqual((self.root / "brokers" / first.target_id).stat().st_mode & 0o777, 0o700)
        self.assertEqual(
            (self.root / "brokers" / first.target_id / "credentials.env").stat().st_mode & 0o777,
            0o600,
        )

    def test_duplicate_owner_rejected_and_rollback_locks_target(self):
        target = self.migration.apply(
            owner_user_id="owner-1", broker_name="example-broker", account_id="account-1",
            credentials_file=self.credentials, ca_file=self.ca,
        )
        with self.assertRaises(PermissionError):
            self.migration.apply(
                owner_user_id="owner-2", broker_name="example-broker", account_id="account-1",
                credentials_file=self.credentials, ca_file=self.ca,
            )
        self.migration.rollback(owner_user_id="owner-1", target_id=target.target_id)
        repository = SQLiteExecutionTargetRepository(self.root / "live.sqlite3")
        try:
            self.assertEqual(repository.get(target.target_id).status, ExecutionTargetStatus.LOCKED)
        finally:
            repository.close()
        self.assertTrue((self.root / "brokers" / f"{target.target_id}.rollback").exists())

    def test_no_secret_values_leak_from_result(self):
        target = self.migration.apply(
            owner_user_id="owner-1", broker_name="example-broker", account_id="account-1",
            credentials_file=self.credentials, ca_file=self.ca,
        )
        self.assertNotIn("not-real", repr(target))


if __name__ == "__main__":
    unittest.main()

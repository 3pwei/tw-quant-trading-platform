from __future__ import annotations

import sqlite3
from pathlib import Path
import tempfile
import unittest

from tw_quant.level2_acceptance import run_level2_soak
from tw_quant.maintenance import backup_sqlite, restore_sqlite, verify_sqlite


class Level2SoakAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_tick_callback_queue_and_sqlite_stay_within_ci_budget(self):
        report = await run_level2_soak(
            0.25,
            tick_interval_seconds=0.005,
        )
        self.assertTrue(report.passed, report.failures)
        self.assertGreaterEqual(report.emitted_ticks, 10)
        self.assertEqual(report.processed_ticks, report.emitted_ticks)
        self.assertEqual(report.dropped_ticks, 0)
        self.assertEqual(report.final_queue_depth, 0)
        self.assertLess(report.max_callback_ms, 10)


class SQLiteRecoveryAcceptanceTests(unittest.TestCase):
    @staticmethod
    def value(path: Path) -> str:
        connection = sqlite3.connect(path)
        try:
            return str(connection.execute("SELECT value FROM state").fetchone()[0])
        finally:
            connection.close()

    def test_backup_restore_and_pre_restore_rollback_are_verified(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "market.sqlite3"
            backup = Path(temp) / "market.backup.sqlite3"
            connection = sqlite3.connect(target)
            connection.execute("CREATE TABLE state(value TEXT NOT NULL)")
            connection.execute("INSERT INTO state VALUES ('before')")
            connection.commit()
            connection.close()

            backup_sqlite(target, backup)
            verify_sqlite(backup)
            connection = sqlite3.connect(target)
            connection.execute("UPDATE state SET value='after'")
            connection.commit()
            connection.close()

            rollback = restore_sqlite(backup, target)
            self.assertIsNotNone(rollback)
            assert rollback is not None
            self.assertEqual(self.value(target), "before")
            self.assertEqual(self.value(rollback), "after")
            verify_sqlite(target)
            verify_sqlite(rollback)

    def test_restore_refuses_same_source_and_target(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "same.sqlite3"
            sqlite3.connect(path).close()
            with self.assertRaisesRegex(ValueError, "must differ"):
                restore_sqlite(path, path)

    def test_verify_does_not_create_a_missing_database(self):
        with tempfile.TemporaryDirectory() as temp:
            missing = Path(temp) / "missing.sqlite3"
            with self.assertRaises(FileNotFoundError):
                verify_sqlite(missing)
            self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()

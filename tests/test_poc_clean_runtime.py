"""Runtime-backed P9 clean database initialization regressions."""
from __future__ import annotations

from contextlib import redirect_stdout
import io
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'deploy/production'))
import poc_clean  # noqa: E402
from tw_quant.auth import SQLiteAuthRepository  # noqa: E402
from tw_quant.live.settings import LiveSettings  # noqa: E402


class FreshDatabaseRuntimeTests(unittest.TestCase):
    def execute_initializer(self, path, email='owner@example.com'):
        settings = types.SimpleNamespace(
            db_path=str(path), bootstrap_admin_emails=(email,), validate=lambda: None,
        )
        composition = types.ModuleType('staging_composition')

        def create_app():
            repository = SQLiteAuthRepository(path)
            repository.bootstrap_admins((email,))
            repository.close()

        composition.create_app = create_app
        try:
            sys.modules['staging_composition'] = composition
            output = io.StringIO()
            with patch.object(LiveSettings, 'from_env', return_value=settings), \
                 redirect_stdout(output):
                exec(poc_clean.INIT_CODE.decode(), {})
            return output.getvalue()
        finally:
            sys.modules.pop('staging_composition', None)

    def test_initializer_creates_one_disabled_admin_and_refuses_repeat(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'platform.sqlite3'
            # Host runtime always sees this exact container path; substitute it in
            # the in-memory test without weakening the deployed guard.
            code = poc_clean.INIT_CODE.replace(
                b"Path('/data/platform.sqlite3')", ("Path(" + repr(str(path)) + ")").encode()
            )
            with patch.object(poc_clean, 'INIT_CODE', code):
                self.assertEqual(self.execute_initializer(path), 'P9_CLEAN_INIT=PASS\n')
                connection = sqlite3.connect(path)
                self.assertEqual(connection.execute(
                    "SELECT email,role,status,trading_mode FROM app_users"
                ).fetchall(), [('owner@example.com', 'admin', 'active', 'disabled')])
                connection.close()
                with self.assertRaises(SystemExit):
                    self.execute_initializer(path)


if __name__ == '__main__':
    unittest.main()

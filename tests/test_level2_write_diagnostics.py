import asyncio
from contextlib import redirect_stderr
from io import StringIO
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from level2_write_diagnostics import observe_level2_writes, TimedConnection, WriteTimings
from tw_quant import level2_acceptance as level2


class Level2WriteDiagnosticTests(unittest.TestCase):
    def test_real_writes_keep_counts_settings_and_report_boundary(self):
        original_repository, original_report = level2.SQLiteBarRepository, level2.SoakReport
        stream = StringIO()
        with observe_level2_writes(stream) as timings:
            # Probe the unchanged settings without exposing database paths or rows.
            with tempfile.TemporaryDirectory() as root:
                repository = level2.SQLiteBarRepository(root + '/synthetic.sqlite3')
                try:
                    self.assertEqual(repository.connection.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
                    self.assertEqual(repository.connection.execute('PRAGMA synchronous').fetchone()[0], 2)
                    self.assertEqual(repository.connection.execute('PRAGMA busy_timeout').fetchone()[0], 5000)
                finally:
                    repository.close()
            report = asyncio.run(level2.run_level2_soak(.05, tick_interval_seconds=.005))
        self.assertIs(level2.SQLiteBarRepository, original_repository)
        self.assertIs(level2.SoakReport, original_report)
        self.assertGreater(report.processed_ticks, 0)
        for phase in ('remember', 'save'):
            for stage in ('total', 'lock', 'sql', 'commit'):
                self.assertEqual(timings.at_report[phase + '-' + stage][0], report.processed_ticks)
        # stop() saves once after the acceptance report; don't conflate it with the sample.
        self.assertEqual(timings.rows['save-commit'][0], report.processed_ticks + 1)
        self.assertEqual(len(stream.getvalue().splitlines()), 8)
        self.assertNotIn('synthetic.sqlite3', stream.getvalue())
        self.assertIn('scope=report', stream.getvalue())

    def test_known_stage_wall_and_cpu_are_distinguished_without_sensitive_output(self):
        for stage, method in (('sql', 'execute'), ('commit', 'commit')):
            timings = WriteTimings()
            timings.phase = 'remember'
            class Connection:
                def execute(self, *args): return 42
                def commit(self): return 42
            with patch('level2_write_diagnostics.perf_counter_ns', side_effect=[0, 100_000_000]), \
                    patch('level2_write_diagnostics.thread_time_ns', side_effect=[0, 1_000_000]):
                call = getattr(TimedConnection(Connection(), timings), method)
                self.assertEqual(call('PRIVATE SQL', ('PRIVATE PARAMETER',)) if method == 'execute' else call(), 42)
            stream = StringIO()
            timings.emit(stream)
            self.assertEqual(timings.rows['remember-' + stage][:4], [1, 100_000_000, 1_000_000, 100_000_000])
            self.assertNotIn('PRIVATE', stream.getvalue())

    def test_sqlite_categories_preserve_exceptions_and_partial_diagnostics(self):
        for code, category in ((sqlite3.SQLITE_BUSY, 'sqlite-busy'),
                               (sqlite3.SQLITE_LOCKED, 'sqlite-locked'),
                               (sqlite3.SQLITE_CORRUPT, 'sqlite-error')):
            error = sqlite3.OperationalError('PRIVATE EXCEPTION')
            error.sqlite_errorcode = code
            timings = WriteTimings()
            timings.phase = 'save'
            with self.assertRaises(sqlite3.OperationalError) as raised:
                with timings.measure('commit'):
                    raise error
            self.assertIs(raised.exception, error)
            stream = StringIO()
            timings.emit(stream)
            self.assertIn('scope=partial', stream.getvalue())
            self.assertIn('errors=1 category=' + category, stream.getvalue())
            self.assertNotIn('PRIVATE', stream.getvalue())

    def test_context_restores_patches_and_emits_completed_steps_on_failure(self):
        original_repository, original_report = level2.SQLiteBarRepository, level2.SoakReport
        stream = StringIO()
        with self.assertRaisesRegex(ValueError, 'synthetic failure'):
            with redirect_stderr(stream), observe_level2_writes() as timings:
                timings.phase = 'remember'
                with timings.measure('sql'):
                    pass
                raise ValueError('synthetic failure')
        self.assertIs(level2.SQLiteBarRepository, original_repository)
        self.assertIs(level2.SoakReport, original_report)
        self.assertIn('scope=partial step=remember-sql count=1', stream.getvalue())
        self.assertNotIn('synthetic failure', stream.getvalue())

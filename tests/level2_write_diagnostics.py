"""Test-only timings for synthetic writes; never log SQL, parameters or errors."""
from contextlib import contextmanager
from copy import deepcopy
import sqlite3
import sys
from time import perf_counter_ns, thread_time_ns
from unittest.mock import patch

from tw_quant import level2_acceptance as level2


class WriteTimings:
    def __init__(self):
        self.phase = None
        self.rows = {phase + '-' + stage: [0, 0, 0, 0, 0, 'none']
                     for phase in ('remember', 'save')
                     for stage in ('total', 'lock', 'sql', 'commit')}
        self.at_report = None

    @contextmanager
    def measure(self, stage):
        if self.phase is None:
            yield
            return
        row = self.rows[self.phase + '-' + stage]
        started, cpu = perf_counter_ns(), thread_time_ns()
        try:
            yield
        except Exception as error:
            category = 'operation-failed'
            if isinstance(error, sqlite3.Error):
                code = getattr(error, 'sqlite_errorcode', 0) & 0xff
                category = {sqlite3.SQLITE_BUSY: 'sqlite-busy',
                            sqlite3.SQLITE_LOCKED: 'sqlite-locked'}.get(code, 'sqlite-error')
            row[4] += 1
            row[5] = category
            raise
        finally:
            elapsed_cpu = thread_time_ns() - cpu
            elapsed = perf_counter_ns() - started
            row[0] += 1
            row[1] += elapsed
            row[2] += elapsed_cpu
            row[3] = max(row[3], elapsed)

    def emit(self, stream):
        scope = 'report' if self.at_report is not None else 'partial'
        for step, row in (self.at_report if self.at_report is not None else self.rows).items():
            count, elapsed, cpu, peak, errors, category = row
            print(f'LEVEL2_WRITE_STEP scope={scope} step={step} count={count} '
                  f'elapsed_ms={elapsed / 1e6:.3f} cpu_ms={cpu / 1e6:.3f} '
                  f'max_ms={peak / 1e6:.3f} errors={errors} category={category}',
                  file=stream, flush=True)


class TimedConnection:
    def __init__(self, connection, timings):
        self.connection, self.timings = connection, timings

    def execute(self, *args, **kwargs):
        with self.timings.measure('sql'):
            return self.connection.execute(*args, **kwargs)

    def commit(self):
        with self.timings.measure('commit'):
            return self.connection.commit()

    def __getattr__(self, name):
        return getattr(self.connection, name)


class TimedLock:
    def __init__(self, lock, timings):
        self.lock, self.timings = lock, timings

    def __enter__(self):
        with self.timings.measure('lock'):
            self.lock.acquire()
        return self

    def __exit__(self, *args):
        self.lock.release()


@contextmanager
def observe_level2_writes(stream=None):
    """Keep real repository calls and freeze diagnostics at the existing report boundary.

    Totals contain the nested lock/SQL/commit timings; do not sum them together.
    Wall minus thread CPU includes I/O, lock and scheduler waits, not just disk I/O.
    Initialization and the final stop-time save are outside the acceptance report.
    No settings, durability, sampling, retry or pass/fail policy is changed.
    """
    timings = WriteTimings()

    class Repository(level2.SQLiteBarRepository):
        def __init__(self, path):
            super().__init__(path)
            self.connection = TimedConnection(self.connection, timings)
            self.lock = TimedLock(self.lock, timings)

        def measured(self, phase, operation, *args):
            previous, timings.phase = timings.phase, phase
            try:
                with timings.measure('total'):
                    return operation(*args)
            finally:
                timings.phase = previous

        def remember_tick(self, *args):
            return self.measured('remember', super().remember_tick, *args)

        def save(self, *args):
            return self.measured('save', super().save, *args)

    original_report = level2.SoakReport

    def capture_report(**kwargs):
        timings.at_report = deepcopy(timings.rows)
        return original_report(**kwargs)

    try:
        with patch.object(level2, 'SQLiteBarRepository', Repository), \
                patch.object(level2, 'SoakReport', capture_report):
            yield timings
    finally:
        timings.emit(sys.stderr if stream is None else stream)

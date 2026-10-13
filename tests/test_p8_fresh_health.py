"""Regression coverage for long integrity scans and independent soak sampling."""
import copy
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest.mock import Mock, patch

import test_p8_evidence as fixtures
from test_p8_evidence import evidence, durable, snapshot


class CaptureFreshnessTests(unittest.TestCase):
    def capture(self, *, delay=39, failure=None, full=True):
        clock = [datetime(2026, 10, 13, tzinfo=timezone.utc)]
        initial = clock[0]
        health_reads = []
        calls = []
        containers = snapshot()['containers']
        for value in containers.values():
            value.update(started=(initial - timedelta(seconds=60)).isoformat(), running=True, health='healthy')
        approved = {'RELEASE_NAME': 'known_good', 'CONFIGURATION_IDENTITY': 'a' * 64,
                    'STAGING_RUNTIME_IMAGE': 'runtime', 'STAGING_RUNTIME_CONFIG_DIGEST': 'b' * 64,
                    'STAGING_GATEWAY_IMAGE': 'gateway', 'STAGING_GATEWAY_CONFIG_DIGEST': 'c' * 64}
        scanned = False

        def run(step, argv, **kwargs):
            nonlocal scanned
            calls.append(step)
            if step == 'manifest-active':
                return approved
            for service, value in containers.items():
                if step == 'container-query-' + service:
                    return 'f' * 64 if scanned and failure == 'replacement' else value['id']
                if step == 'container-inspect-' + service:
                    result = copy.deepcopy(value)
                    if scanned and failure == 'restart':
                        result['started'] = clock[0].isoformat()
                    if scanned and failure == 'unhealthy':
                        result['health'] = 'unhealthy'
                    return result
                if step == 'image-inspect-' + service:
                    return value['image']
            if step == 'worker-health':
                health_reads.append(clock[0])
                health = {'locked': True, 'enabled': False, 'ordering_enabled': False, 'connected': False,
                          'execution_state': 'disabled', 'recovery_status': 'locked', 'broker_name': 'disabled',
                          'external_order_calls': 0, 'external_cancel_calls': 0,
                          'issue_codes': ['execution_disabled', 'execution_target_not_active'],
                          'generation': 'a' * 32, 'heartbeat_at': clock[0].isoformat()}
                if failure == 'initial-stale' or (scanned and failure == 'stale'):
                    health['heartbeat_at'] = (clock[0] - timedelta(seconds=31)).isoformat()
                if scanned:
                    if failure == 'generation': health['generation'] = 'b' * 32
                    if failure == 'unlocked': health['locked'] = False
                    if failure == 'enabled': health['enabled'] = True
                    if failure == 'external-call': health['external_order_calls'] = 1
                    if failure == 'future': health['heartbeat_at'] = (clock[0] + timedelta(seconds=1)).isoformat()
                return health
            if step in ('durable-snapshot', 'durable-targets'):
                scanned = True
                clock[0] += timedelta(seconds=delay)
                if failure == 'timeout': raise evidence.CollectionStepError(step, 'timeout', 120000)
                return {'locked_target_sha256': 'f' * 64, 'target_count': 1, 'active_targets': 0,
                        **({'sqlite_integrity': 'corrupt' if failure == 'corrupt' else 'ok'} if full else {})}
            raise AssertionError(step)

        real_check = evidence.check_health
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'bundle').mkdir()
            (root / 'bundle/durable_probe.py').write_text('reviewed probe')
            with patch.object(evidence, 'release_values', return_value=approved), \
                    patch.object(evidence, 'record_hashes', return_value=snapshot()['records']), \
                    patch.object(evidence, 'run', side_effect=run), \
                    patch.object(evidence, 'check_health', side_effect=lambda h, s: real_check(h, s, clock[0])):
                try:
                    result = evidence.capture(root, full_integrity=full)
                finally:
                    self.calls = calls
        self.assertEqual(health_reads, [initial, clock[0]])
        self.assertEqual(result['execution']['heartbeat_at'], clock[0].isoformat())
        return result

    def test_slow_full_scan_uses_fresh_health_without_relaxing_age(self):
        for delay in (39, 119):
            with self.subTest(delay=delay):
                self.assertEqual(self.capture(delay=delay)['durable']['sqlite_integrity'], 'ok')

    def test_latest_bad_health_or_runtime_change_still_fails(self):
        failures = {'stale': 'stale-worker-heartbeat', 'future': 'stale-worker-heartbeat',
                    'generation': 'worker-generation-changed', 'restart': 'container-changed-during-probe',
                    'replacement': 'container-replaced', 'unhealthy': 'container-health',
                    'unlocked': 'execution-not-disabled-locked', 'enabled': 'execution-not-disabled-locked',
                    'external-call': 'external-calls', 'corrupt': 'durable-state-invalid'}
        for failure, expected in failures.items():
            with self.subTest(failure=failure), self.assertRaisesRegex(ValueError, expected):
                self.capture(failure=failure)

    def test_initial_failure_prevents_scan_and_probe_timeout_is_not_retried(self):
        with self.assertRaisesRegex(ValueError, 'stale-worker-heartbeat'):
            self.capture(failure='initial-stale')
        self.assertNotIn('durable-snapshot', self.calls)
        with self.assertRaises(evidence.CollectionStepError):
            self.capture(failure='timeout')
        self.assertEqual(self.calls.count('durable-snapshot'), 1)

    def test_target_only_capture_does_not_claim_integrity_pass(self):
        result = self.capture(delay=1, full=False)
        self.assertNotIn('sqlite_integrity', result['durable'])
        self.assertIn('durable-targets', self.calls)
        self.assertNotIn('durable-snapshot', self.calls)

    def test_shared_worker_health_preserves_generation_for_restart_consumers(self):
        now = datetime.now(timezone.utc)
        before = snapshot()
        after = copy.deepcopy(before)
        for value in before['containers'].values():
            value['started'] = (now - timedelta(seconds=20)).isoformat()
        for value in after['containers'].values():
            value['started'] = (now - timedelta(seconds=10)).isoformat()
        health = {'locked': True, 'enabled': False, 'ordering_enabled': False, 'connected': False,
                  'execution_state': 'disabled', 'recovery_status': 'locked', 'broker_name': 'disabled',
                  'external_order_calls': 0, 'external_cancel_calls': 0,
                  'issue_codes': ['execution_disabled', 'execution_target_not_active']}
        for sample, generation, age in ((before, 'a' * 32, 15), (after, 'b' * 32, 5)):
            with patch.object(evidence, 'run', return_value={**health, 'generation': generation,
                    'heartbeat_at': (now - timedelta(seconds=age)).isoformat()}):
                sample['execution'] = evidence.worker_health(sample['containers']['execution-worker'])
            self.assertEqual(sample['execution']['generation'], generation)
        evidence.continuity(before, after, restarted=True)
        after['execution']['generation'] = before['execution']['generation']
        with self.assertRaisesRegex(ValueError, 'worker-generation-drift'):
            evidence.continuity(before, after, restarted=True)


class TargetProbeTests(unittest.TestCase):
    def test_target_probe_is_readonly_without_integrity_sql_and_detects_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            path = fixtures.DurableDiagnosticTests().fixture(directory)
            statements = []
            original = sqlite3.connect
            def connect(database, **kwargs):
                self.assertTrue(database.endswith('?mode=ro'))
                connection = original(database, **kwargs)
                connection.set_trace_callback(statements.append)
                return connection
            before = path.read_bytes()
            with patch.object(durable.sqlite3, 'connect', side_effect=connect):
                result = durable.snapshot(path, full_integrity=False)
            self.assertNotIn('sqlite_integrity', result)
            self.assertFalse(any('integrity_check' in sql for sql in statements))
            self.assertEqual(path.read_bytes(), before)
            full = durable.snapshot(path)
            self.assertEqual(result, {k: v for k, v in full.items() if k != 'sqlite_integrity'})
            with original(path) as connection:
                connection.execute("UPDATE execution_targets SET status='active'")
            with self.assertRaisesRegex(ValueError, 'synthetic target drift'):
                durable.snapshot(path, full_integrity=False)

    def test_target_probe_has_container_watchdog_inside_short_transport_budget(self):
        argv = ['docker', 'exec', '-i', 'a' * 64, 'python', '-', 'targets']
        for code in (0, 124):
            with patch.object(evidence.subprocess, 'run', return_value=subprocess.CompletedProcess(argv, code)) as run, \
                    patch.object(evidence, 'start_resource_sampling') as sampler, redirect_stderr(io.StringIO()):
                if code:
                    with self.assertRaises(evidence.CollectionStepError) as raised:
                        evidence.run('durable-targets', argv)
                    self.assertEqual(raised.exception.category, 'timeout')
                else:
                    evidence.run('durable-targets', argv)
                self.assertEqual(run.call_args.args[0][4:8],
                                 ['/usr/bin/timeout', '--signal=TERM', '--kill-after=2s', '10s'])
                self.assertEqual(run.call_args.kwargs['timeout'], 15)
                run.assert_called_once()
                sampler.assert_not_called()


class SoakBoundaryTests(unittest.TestCase):
    def test_full_scans_bracket_complete_window_and_any_failure_prevents_ledger(self):
        for failure in (None, 'before', 'after', 'drift', 'stale', 'gap'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                fixtures.LedgerTests().fixture(root)
                clock = [0.0]
                scans, samples = [], []
                logs = Mock()
                logs.stats = {s: {'complete': True} for s in evidence.SERVICES}
                def capture(root, *, full_integrity=True):
                    if full_integrity:
                        boundary = 'before' if not scans else 'after'
                        scans.append(clock[0])
                        clock[0] += 39
                        if failure == boundary: raise ValueError('integrity-failed')
                    else:
                        samples.append(clock[0])
                        clock[0] += 31 if failure == 'gap' else 1
                        if failure == 'stale': raise ValueError('stale-worker-heartbeat')
                    result = snapshot()
                    if not full_integrity:
                        result['durable'].pop('sqlite_integrity')
                        if failure == 'drift': result['durable']['locked_target_sha256'] = 'e' * 64
                    return result
                def sleep(seconds): clock[0] += seconds
                with patch.object(evidence, 'capture', side_effect=capture), \
                        patch.object(evidence, 'LogMonitor', return_value=logs), \
                        patch.object(evidence, 'run'), patch.object(evidence, 'append') as append, \
                        patch.object(evidence.time, 'monotonic', side_effect=lambda: clock[0]), \
                        patch.object(evidence.time, 'sleep', side_effect=sleep):
                    if failure:
                        with self.assertRaises(ValueError): evidence.soak(30, root)
                        append.assert_not_called()
                    else:
                        evidence.soak(30, root)
                        result = append.call_args.args[1]
                        self.assertEqual(len(scans), 2)
                        self.assertEqual(samples[0], 39)
                        self.assertGreaterEqual(scans[1] - 39, 1800)
                        self.assertEqual(result['elapsed_seconds'], scans[1] - 39)
                        self.assertLessEqual(result['max_sample_gap_seconds'], 30)
                        self.assertGreaterEqual(clock[0], 1878)
                        for boundary in ('before', 'after'):
                            broken = copy.deepcopy(result)
                            broken[boundary]['durable'].pop('sqlite_integrity')
                            with self.assertRaisesRegex(ValueError, 'soak-integrity-boundary-missing'):
                                evidence.validate_soak(broken, 30)
                if failure != 'before': logs.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()

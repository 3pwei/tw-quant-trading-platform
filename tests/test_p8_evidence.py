"""Fault injection for continuity, safe diagnostics and complete acceptance evidence."""
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

STAGING = Path(__file__).resolve().parents[1] / 'deploy/staging'


def load(name):
    spec = importlib.util.spec_from_file_location(name, STAGING / (name + '.py'))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


evidence = load('acceptance_evidence')
durable = load('durable_probe')
diagnostics = load('diagnostics')


def snapshot():
    return {'containers': {s: {'id': str(i) * 64, 'image': 'sha256:' + str(i) * 64,
        'started': '2026-10-04T01:00:00Z', 'restarts': 0, 'network_mode': 'none' if s == 'execution-worker' else 'internal'}
        for i, s in enumerate(evidence.SERVICES, 1)}, 'durable': {'locked_target_sha256': 'f' * 64},
        'records': {'current': 'a' * 64, 'active-release': 'a' * 64, 'previous': 'b' * 64},
        'release': {'name': 'known_good'}, 'expected_images': {},
        'execution': {'heartbeat_at': '2026-10-04T01:00:05Z'}}


class ContinuityTests(unittest.TestCase):
    def test_disabled_service_reopens_persisted_lock_without_loading_secrets(self):
        import asyncio
        from tw_quant.execution_service import ExecutionServiceSettings, build_execution_service
        class NoSecrets:
            def load(self, *args):
                raise AssertionError('disabled service attempted secret loading')
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'staging.sqlite3')
            with patch.object(durable, 'PATH', path), patch.dict(os.environ, {'BROKER_PROVIDER': 'disabled', 'LIVE_TRADING_ENABLED': 'false'}):
                before = durable.seed()
                async def exercise():
                    runtime = build_execution_service(ExecutionServiceSettings(broker_name='disabled', database_path=path,
                        health_path=str(Path(directory) / 'health.json')), secret_provider=NoSecrets())
                    try:
                        await runtime.start()
                        health = runtime.state_document()
                        evidence.check_health(health, (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat())
                    finally:
                        await runtime.close()
                asyncio.run(exercise())
                asyncio.run(exercise())
                self.assertEqual(before, durable.snapshot())

    def test_replacement_restart_state_and_active_record_drift_fail(self):
        before = snapshot()
        evidence.continuity(before, copy.deepcopy(before))
        mutations = [lambda d: d['containers']['gateway'].update(id='f' * 64),
                     lambda d: d['containers']['execution-worker'].update(started='2026-10-04T01:00:10Z'),
                     lambda d: d['containers']['market-api'].update(restarts=1),
                     lambda d: d['containers']['market-api'].update(image='sha256:' + 'f' * 64),
                     lambda d: d['records'].update({'active-release': 'f' * 64}),
                     lambda d: d['durable'].update(locked_target_sha256='a' * 64)]
        for mutate in mutations:
            after = copy.deepcopy(before)
            mutate(after)
            with self.assertRaises(ValueError): evidence.continuity(before, after)
        with self.assertRaises(ValueError): evidence.continuity(before, before, restarted=True)
        after = copy.deepcopy(before)
        for service in evidence.SERVICES:
            after['containers'][service]['started'] = '2026-10-04T01:00:10Z'
        after['execution']['heartbeat_at'] = '2026-10-04T01:00:15Z'
        evidence.continuity(before, after, restarted=True)

    def test_health_requires_fresh_disabled_worker_and_persisted_lock_observation(self):
        now = datetime.now(timezone.utc)
        health = {'locked': True, 'enabled': False, 'ordering_enabled': False, 'connected': False,
            'execution_state': 'disabled', 'recovery_status': 'locked', 'broker_name': 'disabled',
            'external_order_calls': 0, 'external_cancel_calls': 0,
            'issue_codes': ['execution_disabled', 'execution_target_not_active'], 'heartbeat_at': now.isoformat()}
        start = (now - timedelta(seconds=10)).isoformat()
        evidence.check_health(health, start, now)
        for key, value in [('locked', False), ('external_order_calls', False), ('external_cancel_calls', 1),
                           ('heartbeat_at', (now - timedelta(seconds=31)).isoformat()), ('issue_codes', ['execution_disabled']),
                           ('ordering_enabled', True), ('recovery_status', 'ready')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                evidence.check_health({**health, key: value}, start, now)

    def test_durable_snapshot_detects_missing_or_unlocked_synthetic_target(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.sqlite3'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE execution_targets(target_id, owner_user_id, broker_name, account_id, secret_ref, status)')
                db.execute('INSERT INTO execution_targets VALUES(?,?,?,?,?,?)', (durable.TARGET, 'p8-synthetic-owner', 'disabled',
                           'P8-SYNTHETIC', 'unavailable:p8-synthetic', 'locked'))
            before = durable.snapshot(path)
            self.assertEqual(before, durable.snapshot(path))
            with sqlite3.connect(path) as db:
                db.execute("UPDATE execution_targets SET status='active'")
            with self.assertRaises(ValueError): durable.snapshot(path)

    def test_log_policy_reads_beyond_old_tail_limit_and_across_chunks(self):
        class Stream:
            def __init__(self): self.parts = iter([b'Trace', b'back\n' + b'normal\n' * 3000, b''])
            def read1(self, size): return next(self.parts)
        monitor = object.__new__(evidence.LogMonitor)
        monitor.closed = False
        monitor.stats = {'gateway': {'bytes': 0, 'policy_failed': False, 'overflow': False, 'complete': False}}
        process = type('Process', (), {'stdout': Stream()})()
        monitor.consume('gateway', process)
        self.assertTrue(monitor.stats['gateway']['policy_failed'])
        self.assertGreater(monitor.stats['gateway']['bytes'], 20000)

    def test_log_reader_failure_or_early_eof_cannot_report_complete(self):
        for error in (OSError('sensitive tool details'), None):
            with self.subTest(error=type(error).__name__):
                monitor = object.__new__(evidence.LogMonitor)
                monitor.closed = False
                monitor.stats = {'gateway': {'bytes': 0, 'policy_failed': False,
                    'overflow': False, 'complete': False, 'read_failed': False, 'ended_early': False}}
                process = Mock()
                process.poll.return_value = None
                process.stdout.read1.side_effect = error
                process.stdout.read1.return_value = b''
                monitor.processes = {'gateway': process}
                monitor.threads = []
                monitor.consume('gateway', process)
                with self.assertRaises(ValueError): monitor.check()
                monitor.close()
                self.assertFalse(monitor.stats['gateway']['complete'])

    def test_log_shutdown_drains_readers_before_marking_coverage_complete(self):
        monitor = object.__new__(evidence.LogMonitor)
        monitor.closed = False
        monitor.processes, monitor.stats, monitor.threads = {}, {}, []
        for service in evidence.SERVICES:
            process = Mock()
            process.poll.return_value = None
            process.stdout.read1.side_effect = [b'normal log\n', b'']
            monitor.processes[service] = process
            monitor.stats[service] = {'bytes': 0, 'policy_failed': False, 'overflow': False,
                'complete': False, 'read_failed': False, 'ended_early': False}
            thread = Mock()
            thread.is_alive.return_value = False
            thread.join.side_effect = lambda timeout, s=service, p=process: monitor.consume(s, p)
            monitor.threads.append(thread)
        monitor.check()
        monitor.close()
        self.assertTrue(all(s['complete'] and s['bytes'] > 0 for s in monitor.stats.values()))
        monitor.close()
        for process in monitor.processes.values(): process.terminate.assert_called_once()

    def test_log_startup_failure_closes_already_started_followers(self):
        process = Mock()
        process.poll.return_value = None
        with patch.object(evidence.subprocess, 'Popen', side_effect=[process, OSError('private details')]), \
                patch('threading.Thread'):
            with self.assertRaises(OSError): evidence.LogMonitor(snapshot()['containers'], '2026-10-04T01:00:00Z')
        process.terminate.assert_called_once()
        process.wait.assert_called_once()
        process.stdout.close.assert_called_once()

    def test_diagnostics_does_not_export_secrets_or_raw_tool_errors(self):
        def command(argv):
            if argv[:3] == ['docker', 'ps', '-aq']: return 0, 'a' * 64
            if argv[:2] == ['docker', 'inspect']:
                return 0, json.dumps({'running': True, 'exit': 0, 'oom': False, 'health': 'healthy',
                                      'environment': 'secret-value', 'restarts': 0})
            return 1, 'secret-value'
        with patch.object(diagnostics, 'command', side_effect=command):
            result = json.dumps(diagnostics.collect())
        self.assertNotIn('secret-value', result)
        self.assertNotIn('environment', result)
        self.assertIn('healthy', result)


class LedgerTests(unittest.TestCase):
    def fixture(self, root):
        (root / 'bundle').mkdir()
        (root / 'bundle/acceptance-session').write_text('123-1')
        (root / 'bundle/candidate-manifest.json').write_text('{}')
        directory = root / 'deployments/evidence/123-1'
        directory.mkdir(parents=True)
        envelope = {'session': '123-1', 'candidate_run_id': '456', 'pipeline': 'a' * 40,
                    'manifest_sha256': hashlib.sha256(b'{}').hexdigest()}
        (directory / 'session.json').write_text(json.dumps(envelope))
        records = [('current', 'known_good', 'rollback'), ('active-release', 'known_good', 'rollback'), ('previous', 'candidate', 'deploy')]
        for name, role, mode in records:
            (root / 'deployments' / (name + '.env')).write_text('RELEASE_NAME=' + role + '\nDEPLOYMENT_MODE=' + mode + '\n')
        rows = []
        durable_state = snapshot()['durable']
        for index, phase in enumerate(evidence.PHASES):
            before = snapshot()
            if index == 1:
                before['release']['name'] = 'candidate'
                for service in ('market-api', 'execution-worker'):
                    before['containers'][service]['image'] = 'sha256:' + 'b' * 64
            after = copy.deepcopy(before)
            for service in evidence.SERVICES: after['containers'][service]['started'] = '2026-10-04T01:00:10Z'
            after['execution']['heartbeat_at'] = '2026-10-04T01:00:15Z'
            committed = copy.deepcopy(after)
            committed['records'] = {'current': str(index + 4) * 64, 'active-release': str(index + 4) * 64,
                                    'previous': before['records']['current']}
            for stage, data in [('verified', before), ('restart-before', before), ('restart-after', after), ('committed', committed)]:
                rows.append({**envelope, 'event': phase + '/' + stage, 'data': copy.deepcopy(data)})
        after = committed
        rows.append({**envelope, 'event': 'soak', 'data': {'before': after, 'after': after, 'requested_minutes': 30,
            'elapsed_seconds': 1800, 'samples': 181, 'max_sample_gap_seconds': 10,
            'log_coverage': {s: {'complete': True} for s in evidence.SERVICES}}})
        return directory, rows, after

    def test_full_ordered_ledger_and_final_a_b_records_are_required(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output, baseline, after = self.fixture(root)
            def check(rows):
                (output / 'ledger.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in rows))
                final_snapshot = rows[-1]['data']['after'] if rows[-1]['event'] == 'soak' else after
                with patch.object(evidence, 'capture', return_value=final_snapshot), patch.object(evidence, 'run', side_effect=lambda argv: 'RELEASE_NAME=' + argv[-1] + '\n'):
                    return evidence.final_check(root)
            self.assertEqual(check(baseline)['acceptance'], 'PASS')
            bad_cases = [baseline[:-1], [baseline[1], baseline[0], *baseline[2:]]]
            for change in (lambda d: d[-1]['data'].update(elapsed_seconds=1799),
                           lambda d: d[-1]['data']['log_coverage']['gateway'].update(complete=False),
                           lambda d: d[0].update(pipeline='b' * 40),
                           lambda d: d[8]['data']['containers']['gateway'].update(image='sha256:' + 'f' * 64)):
                modified = copy.deepcopy(baseline)
                change(modified)
                bad_cases.append(modified)
            # Mutate a whole segment so its internal checks still pass: each gap
            # must bind the preceding runtime to the following observation.
            for indices in ((0,), (1, 2, 3), (3,), (12,)):
                modified = copy.deepcopy(baseline)
                for index in indices:
                    snapshots = [modified[index]['data']] if index != 12 else [modified[index]['data']['before'], modified[index]['data']['after']]
                    for item in snapshots:
                        item['containers']['gateway']['id'] = 'f' * 64
                bad_cases.append(modified)
            for key in ('current', 'active-release', 'previous'):
                modified = copy.deepcopy(baseline)
                modified[3]['data']['records'][key] = 'f' * 64
                bad_cases.append(modified)
            for rows in bad_cases:
                with self.assertRaises(ValueError): check(rows)
            (root / 'deployments/previous.env').write_text('RELEASE_NAME=known_good\n')
            with self.assertRaises(ValueError): check(baseline)


if __name__ == '__main__': unittest.main()

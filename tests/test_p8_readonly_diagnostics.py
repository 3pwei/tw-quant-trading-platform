"""Isolated checks of the temporary diagnostic entry point; never SSH to a host."""
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
STAGING = ROOT / 'deploy/staging'


def load(name):
    spec = importlib.util.spec_from_file_location(name, STAGING / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


diagnostic = load('readonly_diagnostics')
evidence = load('acceptance_evidence')
SHA = 'a' * 40


def request():
    return dict(GITHUB_SHA=SHA, GITHUB_REPOSITORY=diagnostic.REPOSITORY,
                GITHUB_EVENT_NAME='workflow_dispatch', GITHUB_REF='refs/heads/master',
                GITHUB_RUN_ATTEMPT='1', GITHUB_RUN_ID='123', EXPECTED_SHA=SHA,
                CONFIRMATION='DIAGNOSE staging ' + SHA)


def api(path):
    if path == 'git/ref/heads/master':
        return {'object': {'sha': SHA}}
    workflow = path.split('/')[2]
    return {'workflow_runs': [dict(head_sha=SHA, head_branch='master', event='push',
        path='.github/workflows/' + workflow, status='completed', conclusion='success')]}


class ControlTests(unittest.TestCase):
    def test_manual_exact_sha_successful_master_checks_and_second_master_read(self):
        get = Mock(side_effect=api)
        self.assertEqual(diagnostic.check_request(request(), get), SHA)
        self.assertEqual(get.call_count, 4)
        self.assertEqual(get.call_args_list[0], get.call_args_list[-1])

    def test_bad_requests_fail_before_api_or_ssh(self):
        for key, value in [('GITHUB_REPOSITORY', 'other/repo'), ('GITHUB_EVENT_NAME', 'push'),
                           ('GITHUB_REF', 'refs/heads/feature'), ('GITHUB_RUN_ATTEMPT', '2'),
                           ('GITHUB_SHA', 'not-a-sha'), ('EXPECTED_SHA', 'b' * 40),
                           ('CONFIRMATION', 'DIAGNOSE production ' + SHA), ('GITHUB_RUN_ID', 'invalid')]:
            with self.subTest(key=key):
                get = Mock()
                with self.assertRaises(ValueError):
                    diagnostic.check_request({**request(), key: value}, get)
                get.assert_not_called()

    def test_incomplete_wrong_or_failed_ci_security_and_master_drift_fail_closed(self):
        for field, value in [('status', 'in_progress'), ('conclusion', 'failure'), ('head_sha', 'b' * 40),
                             ('event', 'pull_request'), ('head_branch', 'feature'), ('path', 'other.yml')]:
            for workflow in ('ci.yml', 'security.yml'):
                def get(path):
                    result = api(path)
                    if workflow in path:
                        result['workflow_runs'][0][field] = value
                    return result
                with self.subTest(field=field, workflow=workflow), self.assertRaises(ValueError):
                    diagnostic.check_request(request(), get)
        for index in (0, 3):
            answers = [api('git/ref/heads/master'), api('actions/workflows/ci.yml/runs'),
                       api('actions/workflows/security.yml/runs'), api('git/ref/heads/master')]
            answers[index] = {'object': {'sha': 'b' * 40}}
            with self.assertRaises(ValueError):
                diagnostic.check_request(request(), Mock(side_effect=answers))

    def test_workflow_is_manual_staging_only_serial_and_has_no_write_permissions(self):
        import yaml
        document = yaml.load((ROOT / '.github/workflows/staging-readonly-diagnostics.yml').read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(set(document['on']), {'workflow_dispatch'})
        self.assertEqual(document['permissions'], {'actions': 'read', 'contents': 'read'})
        self.assertEqual(document['concurrency'], {'group': 'p8-staging', 'cancel-in-progress': 'false'})
        job = document['jobs']['inspect']
        self.assertEqual(job['environment'], 'staging')
        self.assertIn("github.run_attempt == 1", job['if'])
        self.assertIn("github.ref == 'refs/heads/master'", job['if'])
        self.assertEqual(job['timeout-minutes'], '5')
        steps = job['steps']
        self.assertNotIn('STAGING_SSH_PRIVATE_KEY', str(steps[:2]))
        self.assertIn('readonly_diagnostics.py check', steps[1]['run'])
        self.assertEqual(steps[2]['run'], 'python -B deploy/staging/readonly_diagnostics.py inspect')
        self.assertEqual(steps[3]['with']['path'], '${{ runner.temp }}/p8-readonly-diagnostics.json')
        self.assertNotIn('PRODUCTION_', str(document))
        self.assertNotIn('PRIVATE_PROVIDER', str(document))


class RemoteTests(unittest.TestCase):
    def fixture(self, directory):
        root = Path(directory)
        path = root / 'fixture.sqlite3'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE execution_targets(target_id, owner_user_id, broker_name, account_id, secret_ref, status)')
            db.execute('INSERT INTO execution_targets VALUES(?,?,?,?,?,?)', (
                'exec_P8SyntheticLocked0001', 'p8-synthetic-owner', 'disabled', 'P8-SYNTHETIC',
                'unavailable:p8-synthetic', 'locked'))
        probe = (STAGING / 'durable_probe.py').read_text().replace(
            'PATH = "/data/staging.sqlite3"', 'PATH = ' + repr(str(path))).encode()
        cid = '1' * 64
        now = datetime.now(timezone.utc)
        state = dict(id=cid, network_mode='none', project='platform-staging', service='execution-worker',
                     running=True, health='healthy', restarts=0,
                     started=(now - timedelta(seconds=5)).isoformat(),
                     mounts=[dict(Destination='/data', Type='volume', Name='platform-staging_staging-data')])
        health = dict(locked=True, enabled=False, ordering_enabled=False, connected=False,
                      execution_state='disabled', recovery_status='locked', broker_name='disabled',
                      external_order_calls=0, external_cancel_calls=0, heartbeat_at=now.isoformat(),
                      issue_codes=['execution_disabled', 'execution_target_not_active'])
        calls = []
        def run(step, argv, **kwargs):
            calls.append((step, argv, kwargs))
            if step == 'container-query-execution-worker': return cid
            if step == 'container-inspect-execution-worker': return state
            if step == 'worker-health': return health
            self.assertEqual(argv[:2], ['docker', 'exec'])
            return evidence.run(step, [sys.executable, *argv[argv.index('python') + 1:]], **kwargs)
        namespace = {**vars(evidence), 'ROOT': root, 'compose': lambda: ['docker', 'compose'], 'run': run}
        return namespace, probe, state, health, calls, path

    def test_real_python_argv_stdin_sqlite_sequence_and_database_bytes_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            namespace, probe, state, health, calls, path = self.fixture(directory)
            before = path.read_bytes()
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                diagnostic.remote_main(namespace, probe)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(out.getvalue(), '')
            self.assertEqual(err.getvalue().splitlines()[-1], 'P8_DIAGNOSTIC=PASS acceptance=false')
            self.assertEqual([line for line in err.getvalue().splitlines() if line.startswith('P8_DIAGNOSTIC_CASE=')],
                             ['P8_DIAGNOSTIC_CASE=' + case for case in diagnostic.CASES])
            self.assertEqual(len(calls), 6)
            self.assertEqual(calls[-1][1], ['docker', 'exec', '-i', '1' * 64, 'python', '-', 'snapshot'])
            self.assertEqual(calls[-1][2]['data'], probe)
            self.assertNotIn('unavailable:p8-synthetic', err.getvalue())

    def test_bad_identity_mount_or_unlocked_worker_prevents_all_python_probes(self):
        for mutation in ('project', 'network_mode', 'mount', 'locked', 'health'):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                namespace, probe, state, health, calls, path = self.fixture(directory)
                if mutation == 'mount': state['mounts'][0]['Name'] = 'other-data'
                elif mutation == 'locked': health['locked'] = False
                else: state[mutation] = 'invalid'
                with self.assertRaises(ValueError): diagnostic.remote_main(namespace, probe)
                self.assertTrue(all(call[0] != 'durable-snapshot' for call in calls))

    def test_first_probe_failure_stops_without_retry_or_later_cases(self):
        for fail_case in range(3):
            with self.subTest(case=fail_case), tempfile.TemporaryDirectory() as directory:
                namespace, probe, state, health, calls, path = self.fixture(directory)
                original = namespace['run']
                probes = []
                def run(step, argv, **kwargs):
                    if step == 'durable-snapshot':
                        probes.append(argv)
                        if len(probes) == fail_case + 1:
                            raise evidence.CollectionStepError(step, 'timeout', 15000)
                    return original(step, argv, **kwargs)
                namespace['run'] = run
                with redirect_stderr(io.StringIO()), self.assertRaises(evidence.CollectionStepError):
                    diagnostic.remote_main(namespace, probe)
                self.assertEqual(len(probes), fail_case + 1)

    def test_payload_loads_reviewed_source_in_memory_without_running_main(self):
        source = diagnostic.payload()
        compile(source, '<payload>', 'exec')
        self.assertIn(b"'__name__': 'p8_diagnostic'", source)
        self.assertTrue(source.endswith(b"namespace['entrypoint'](lambda: remote_main(namespace, probe))\n"))
        self.assertNotIn(b'STAGING_SSH_PRIVATE_KEY', source)
        self.assertNotIn(b'GITHUB_TOKEN', source)

    def test_host_start_and_completion_share_collector_stderr(self):
        with tempfile.TemporaryDirectory() as directory:
            namespace, probe, *_ = self.fixture(directory)
            out, err = io.StringIO(), io.StringIO()
            # Execute the payload prelude itself, rather than restating its print.
            prelude = diagnostic.payload().decode().split("namespace =", 1)[0]
            with redirect_stdout(out), redirect_stderr(err):
                exec(compile(prelude, '<diagnostic-prelude>', 'exec'), {})
                diagnostic.remote_main(namespace, probe)
            self.assertEqual(out.getvalue(), '')
            lines = diagnostic.safe_lines(err.getvalue().encode())
            self.assertEqual(lines[0], 'P8_DIAGNOSTIC_HOST=START')
            self.assertEqual(lines[-1], 'P8_DIAGNOSTIC=PASS acceptance=false')
            self.assertTrue(lines[-2].startswith('P8_EVIDENCE_STEP=PASS step=durable-snapshot '))


class TransportTests(unittest.TestCase):
    def exercise(self, action):
        with tempfile.TemporaryDirectory() as directory:
            env = {**request(), 'RUNNER_TEMP': directory, 'STAGING_HOST': 'staging.invalid', 'STAGING_USER': 'ubuntu',
                   'STAGING_SSH_PRIVATE_KEY': 'private-key-never-log', 'STAGING_SSH_HOST_KEY': 'host-key-never-log',
                   'GITHUB_TOKEN': 'token-never-log'}
            paths = []
            def execute(argv, **kwargs):
                self.assertEqual(kwargs['timeout'], 120)
                self.assertEqual(set(kwargs['env']), {'PATH', 'LANG'})
                self.assertEqual(argv[-2:], ['staging.invalid', 'sudo -n python3 -B -'])
                self.assertIn('StrictHostKeyChecking=yes', argv)
                self.assertIn('IdentitiesOnly=yes', argv)
                key = Path(argv[argv.index('-i') + 1])
                self.assertEqual(key.stat().st_mode & 0o777, 0o600)
                paths.append(key.parent)
                return action(argv, kwargs)
            out = io.StringIO()
            with patch.dict(os.environ, env), patch.object(diagnostic, 'check_request', return_value=SHA), \
                    patch.object(diagnostic.subprocess, 'run', side_effect=execute) as process, redirect_stdout(out):
                code = diagnostic.inspect_once()
            process.assert_called_once()
            document = json.loads(Path(directory, 'p8-readonly-diagnostics.json').read_text())
            self.assertFalse(document['acceptance'])
            for secret in ('private-key-never-log', 'host-key-never-log', 'token-never-log', 'raw-secret'):
                self.assertNotIn(secret, json.dumps(document) + out.getvalue())
            self.assertTrue(all(not path.exists() for path in paths))
            return code, document

    def test_timeout_retains_completed_records_and_removes_runner_ssh_material(self):
        safe = b'P8_DURABLE_STEP=PASS step=sqlite-connect elapsed_ms=3 category=none\n'
        def execute(argv, kwargs):
            kwargs['stdout'].write(safe + b'raw-secret\n')
            raise subprocess.TimeoutExpired('raw-secret', 120)
        code, document = self.exercise(execute)
        self.assertEqual(code, 1)
        self.assertEqual(document['reason'], 'transport-timeout')
        self.assertEqual(document['stages'], [safe.decode().strip()])

    def test_success_requires_zero_exit_complete_evidence_and_unique_success(self):
        complete = 'P8_DIAGNOSTIC_HOST=START\n'
        complete += ''.join('P8_EVIDENCE_STEP=PASS step=' + step + ' elapsed_ms=1\n' for step in (
            'container-query-execution-worker', 'container-inspect-execution-worker', 'worker-health'))
        for case in diagnostic.CASES:
            complete += 'P8_DIAGNOSTIC_CASE=' + case + '\n'
            if case == 'durable':
                complete += ''.join('P8_DURABLE_STEP=PASS step=' + step + ' elapsed_ms=1 category=none\n'
                                    for step in diagnostic.STAGES)
            complete += 'P8_EVIDENCE_STEP=PASS step=durable-snapshot elapsed_ms=1\n'
        complete += 'P8_DIAGNOSTIC=PASS acceptance=false\n'
        # Run #2: SSH delivered stdout's success before stderr's final timing.
        records = complete.splitlines(keepends=True)
        reordered = ''.join(records[:-2] + [records[-1], records[-2]])
        for content, exit_code, expected in [(complete, 0, 'completed'), (complete, 1, 'remote-failed'),
                                            (reordered, 0, 'completed'),
                                            (reordered, 1, 'remote-failed'),
                                            (complete, 255, 'transport-failed'),
                                            ('P8_DIAGNOSTIC=PASS acceptance=false\n', 0, 'incomplete-output'),
                                            (complete.replace('step=targets-query', 'step=invalid'), 0, 'incomplete-output'),
                                            ('P8_EVIDENCE=FAIL check=worker-isolation-invalid\n' + complete, 0, 'incomplete-output'),
                                            (complete + 'x' * 65536, 0, 'output-limit')]:
            def execute(argv, kwargs):
                kwargs['stdout'].write(content.encode())
                return subprocess.CompletedProcess(argv, exit_code)
            with self.subTest(expected=expected):
                code, document = self.exercise(execute)
                self.assertEqual(document['reason'], expected)
                self.assertEqual(code == 0, expected == 'completed')

        # A success marker can never compensate for missing/duplicated records,
        # a broken per-kind order, truncated output or any explicit failure.
        for index, record in enumerate(records):
            for content in (''.join(records[:index] + records[index + 1:]),
                            ''.join(records[:index] + [record] + records[index:])):
                def execute(argv, kwargs):
                    kwargs['stdout'].write(content.encode())
                    return subprocess.CompletedProcess(argv, 0)
                with self.subTest(record=record, content=content):
                    code, document = self.exercise(execute)
                    self.assertEqual(code, 1)
                    self.assertEqual(document['reason'], 'incomplete-output')

        for content in (
            complete[:-1],
            complete.replace('step=python-imports', 'step=swap').replace(
                'step=sqlite-connect', 'step=python-imports').replace('step=swap', 'step=sqlite-connect'),
            complete.replace('P8_DIAGNOSTIC_CASE=python-argv', 'P8_DIAGNOSTIC_CASE=swap').replace(
                'P8_DIAGNOSTIC_CASE=python-stdin', 'P8_DIAGNOSTIC_CASE=python-argv').replace(
                'P8_DIAGNOSTIC_CASE=swap', 'P8_DIAGNOSTIC_CASE=python-stdin'),
            complete.replace('step=container-query-execution-worker', 'step=swap').replace(
                'step=container-inspect-execution-worker', 'step=container-query-execution-worker').replace(
                'step=swap', 'step=container-inspect-execution-worker'),
            reordered + 'P8_DURABLE_STEP=FAIL step=sqlite-integrity elapsed_ms=1 category=sqlite-error\n',
            reordered + 'P8_DURABLE_STEP=FAIL step=sqlite-integrity-fetchone elapsed_ms=1 '
                        'category=sqlite-busy sqlite_errorcode=517\n',
            reordered + 'P8_EVIDENCE=FAIL step=durable-snapshot category=timeout elapsed_ms=15002\n',
            reordered + 'P8_EVIDENCE_FAIL check=container-health service=execution-worker '
                        'expected=running:true,health:healthy actual=running:true,health:unhealthy,restarts:0\n',
        ):
            def execute(argv, kwargs):
                kwargs['stdout'].write(content.encode())
                return subprocess.CompletedProcess(argv, 0)
            with self.subTest(content=content):
                code, document = self.exercise(execute)
                self.assertEqual(code, 1)
                self.assertEqual(document['reason'], 'incomplete-output')

    def test_sanitizer_drops_raw_exceptions_values_partial_lines_and_fake_steps(self):
        raw = (b'P8_DIAGNOSTIC_HOST=START\nTraceback raw-secret\n'
               b'P8_DIAGNOSTIC_CASE=private-value\n'
               b'P8_EVIDENCE=FAIL check=secret-code\n'
               b'P8_EVIDENCE=FAIL check=worker-isolation-invalid\n'
               b'P8_DIAGNOSTIC=PASS acceptance=false')
        self.assertEqual(diagnostic.safe_lines(raw), ['P8_DIAGNOSTIC_HOST=START',
                        'P8_EVIDENCE=FAIL check=worker-isolation-invalid'])

    def test_integrity_call_records_and_codes_survive_both_sanitizers(self):
        for stage in ('execute', 'fetchone'):
            for code in ('5', '517', 'unknown'):
                record = ('P8_DURABLE_STEP=FAIL step=sqlite-integrity-' + stage +
                          ' elapsed_ms=12 category=sqlite-busy sqlite_errorcode=' + code + '\n').encode()
                log = io.StringIO()
                with redirect_stderr(log):
                    evidence.emit_durable_diagnostics(io.BytesIO(record))
                self.assertEqual(diagnostic.safe_lines(log.getvalue().encode()), [record.decode().strip()])
                for invalid in ('secret', '-1', '12345678901', '5 token=secret'):
                    self.assertEqual(diagnostic.safe_lines(record.replace(
                        ('sqlite_errorcode=' + code).encode(), ('sqlite_errorcode=' + invalid).encode())), [])

    def test_control_failure_never_creates_ssh_material_or_invokes_transport(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'RUNNER_TEMP': directory}), \
                patch.object(diagnostic, 'check_request', side_effect=ValueError('raw-secret')), \
                patch.object(diagnostic, 'ssh_command') as configure, \
                patch.object(diagnostic.subprocess, 'run') as execute, redirect_stdout(io.StringIO()):
            self.assertEqual(diagnostic.inspect_once(), 1)
            configure.assert_not_called()
            execute.assert_not_called()
            result = json.loads(Path(directory, 'p8-readonly-diagnostics.json').read_text())
            self.assertEqual(result['reason'], 'control-gate-failed')
            self.assertNotIn('raw-secret', str(result))


if __name__ == '__main__':
    unittest.main()

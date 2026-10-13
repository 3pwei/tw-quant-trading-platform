"""Real container-local timeout boundaries; no deployed services or databases."""
from contextlib import redirect_stderr
import importlib.util
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('bounded_evidence', ROOT / 'deploy/staging/acceptance_evidence.py')
evidence = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evidence)
ARGV = ['docker', 'exec', '-i', 'a' * 64, 'python', '-', 'snapshot']


class BudgetTests(unittest.TestCase):
    def test_only_exact_durable_exec_gets_container_watchdog_and_long_budget(self):
        cases = [(ARGV, 140, True),
                 (['docker', 'exec', 'a' * 64, 'python', '-c', 'print(1)'], 15, False),
                 (['docker', 'exec', '-i', 'a' * 64, 'python', '-'], 15, False),
                 ([sys.executable, '-c', 'print(1)'], 15, False)]
        for argv, timeout, wrapped in cases:
            with self.subTest(argv=argv), patch.object(evidence, 'start_resource_sampling'), \
                    patch.object(evidence, 'finish_resource_sampling'), \
                    patch.object(evidence.subprocess, 'run', return_value=subprocess.CompletedProcess(argv, 0)) as run, \
                    redirect_stderr(io.StringIO()):
                evidence.run('durable-snapshot', argv)
                self.assertEqual(run.call_args.kwargs['timeout'], timeout)
                command = run.call_args.args[0]
                if wrapped:
                    self.assertEqual(command[4:8], ['/usr/bin/timeout', '--signal=TERM', '--kill-after=5s', '120s'])
                    self.assertEqual(command[8:], ['python', '-', 'snapshot'])
                else:
                    self.assertEqual(command, argv)

    def test_deadline_and_unknown_kill_are_failures_without_retry_or_pass(self):
        for code, category in ((124, 'timeout'), (137, 'nonzero-exit'), (127, 'nonzero-exit')):
            log = io.StringIO()
            with patch.object(evidence, 'start_resource_sampling'), patch.object(evidence, 'finish_resource_sampling'), \
                    patch.object(evidence.subprocess, 'run', return_value=subprocess.CompletedProcess(ARGV, code)) as run, \
                    redirect_stderr(log), self.assertRaises(evidence.CollectionStepError) as raised:
                evidence.run('durable-snapshot', ARGV)
            self.assertEqual(raised.exception.category, category)
            run.assert_called_once()
            self.assertNotIn('P8_EVIDENCE_STEP=PASS', log.getvalue())

    def test_outer_diagnostic_budget_exceeds_all_serial_commands_and_cleanup(self):
        import yaml
        spec = importlib.util.spec_from_file_location('bounded_readonly', ROOT / 'deploy/staging/readonly_diagnostics.py')
        diagnostic = importlib.util.module_from_spec(spec); spec.loader.exec_module(diagnostic)
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {'RUNNER_TEMP': directory, 'GITHUB_RUN_ID': '1'}), \
                patch.object(diagnostic, 'check_request', return_value='a' * 40), \
                patch.object(diagnostic, 'ssh_command', return_value=['ssh']), \
                patch.object(diagnostic.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1)) as run, \
                redirect_stdout_stderr():
            self.assertEqual(diagnostic.inspect_once(), 1)
        ssh_budget = run.call_args.kwargs['timeout']
        self.assertGreater(ssh_budget, 5 * 15 + evidence.DURABLE_TRANSPORT_SECONDS + 30)
        workflow = yaml.load((ROOT / '.github/workflows/staging-readonly-diagnostics.yml').read_text(), Loader=yaml.BaseLoader)
        self.assertGreater(int(workflow['jobs']['inspect']['timeout-minutes']) * 60, ssh_budget + 120)
        self.assertGreater(evidence.DURABLE_TRANSPORT_SECONDS,
                           evidence.DURABLE_SECONDS + evidence.DURABLE_KILL_SECONDS + 10)
        p8 = yaml.load((ROOT / '.github/workflows/deploy-staging.yml').read_text(), Loader=yaml.BaseLoader)
        deployment = next(s for s in p8['jobs']['deploy']['steps'] if s['name'].startswith('Deploy A,'))['run']
        self.assertEqual(deployment.count('--kill-after=30s 40m ssh'), 3)
        # Four captures per deployment, each with eighteen small commands plus a durable probe.
        self.assertGreater(40 * 60, 4 * (18 * 15 + evidence.DURABLE_TRANSPORT_SECONDS) + 5 * 60)
        # Current requested 30-minute soak plus pull/deploy/rollback/setup/output allowance.
        self.assertGreater(int(p8['jobs']['deploy']['timeout-minutes']), 20 + 3 * 40 + 45 + 30)


def redirect_stdout_stderr():
    from contextlib import ExitStack, redirect_stdout
    stack = ExitStack()
    stack.enter_context(redirect_stdout(io.StringIO()))
    stack.enter_context(redirect_stderr(io.StringIO()))
    return stack


@unittest.skipUnless(shutil.which('timeout') and sys.platform == 'linux', 'GNU timeout on Linux required')
class LocalWatchdogTests(unittest.TestCase):
    def test_snapshot_over_old_15_second_limit_can_complete(self):
        # A local Docker stand-in only forwards exec; the production timer is real.
        with tempfile.TemporaryDirectory() as directory:
            docker = Path(directory) / 'docker'
            docker.write_text('#!' + sys.executable + '\nimport os,sys\nos.execv(sys.argv[4], sys.argv[4:])\n')
            docker.chmod(0o755)
            with patch.dict(os.environ, {'PATH': directory + os.pathsep + os.environ['PATH']}), \
                    patch.object(evidence, 'start_resource_sampling', return_value=None), redirect_stderr(io.StringIO()):
                result = evidence.run('durable-snapshot', ARGV,
                    data=b'import time; time.sleep(16); print("complete")\n')
            self.assertEqual(result.strip(), 'complete')

    def test_term_interrupts_sqlite_native_work_and_kill_escalates_for_ignored_term(self):
        for ignore in (False, True):
            source = ('import signal,sqlite3\n' +
                      ('signal.signal(signal.SIGTERM, signal.SIG_IGN)\n' if ignore else '') +
                      'sqlite3.connect(":memory:").execute("WITH RECURSIVE c(x) AS '
                      '(VALUES(0) UNION ALL SELECT x+1 FROM c) SELECT sum(x) FROM c")\n')
            with self.subTest(ignore=ignore):
                started = time.monotonic()
                result = subprocess.run(['/usr/bin/timeout', '--signal=TERM', '--kill-after=1s', '1s',
                                         sys.executable, '-c', source], capture_output=True, timeout=5)
                self.assertIn(result.returncode, (137, -9) if ignore else (124,))
                self.assertLess(time.monotonic() - started, 4)


@unittest.skipUnless(os.environ.get('P8_PROBE_TEST_IMAGE'), 'Run in exact-image Docker CI step')
class ContainerWatchdogTests(unittest.TestCase):
    def test_client_disconnect_cannot_leave_a_running_sqlite_probe(self):
        image = os.environ['P8_PROBE_TEST_IMAGE']
        cid = subprocess.check_output(['docker', 'run', '-d', '--network', 'none', '--read-only',
            '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
            '--tmpfs', '/tmp:rw,noexec,nosuid,nodev,size=16m,uid=10001,gid=10001',
            image, 'python', '-c', 'import time; time.sleep(120)'], text=True).strip()
        try:
            # TERM normally stops native SQLite. Ignoring it exercises KILL escalation.
            for ignore in (False, True):
                marker = '/tmp/probe-' + str(int(ignore))
                source = ('import os,signal,sqlite3\nfrom pathlib import Path\n' +
                          ('signal.signal(signal.SIGTERM, signal.SIG_IGN)\n' if ignore else '') +
                          'Path(' + repr(marker) + ').write_text(str(os.getpid()))\n' +
                          'sqlite3.connect(":memory:").execute("WITH RECURSIVE c(x) AS '
                          '(VALUES(0) UNION ALL SELECT x+1 FROM c) SELECT sum(x) FROM c")\n')
                with patch.object(evidence, 'DURABLE_SECONDS', 3), patch.object(evidence, 'DURABLE_KILL_SECONDS', 1):
                    command = evidence.bounded_probe_command(['docker', 'exec', '-i', cid, 'python', '-', 'snapshot'])
                client = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                try:
                    client.stdin.write(source.encode()); client.stdin.close()
                    deadline = time.monotonic() + 2
                    pid = None
                    while time.monotonic() < deadline:
                        result = subprocess.run(['docker', 'exec', cid, 'cat', marker], capture_output=True, text=True, timeout=5)
                        if result.returncode == 0:
                            pid = int(result.stdout); break
                        time.sleep(0.05)
                    self.assertIsNotNone(pid, 'Probe must start before disconnect')
                    client.kill(); client.wait(timeout=5)  # reproduce the old outer-timeout behavior
                    deadline = time.monotonic() + 6
                    while True:
                        check = ('from pathlib import Path\np=Path("/proc/' + str(pid) + '/stat")\n'
                                 'try: print(p.read_text().rsplit(")",1)[1].split()[0])\n'
                                 'except FileNotFoundError: print("gone")\n')
                        state = subprocess.check_output(['docker', 'exec', cid, 'python', '-c', check], text=True, timeout=5).strip()
                        if state in ('gone', 'Z'): break  # zombie is not a surviving scan
                        self.assertLess(time.monotonic(), deadline, 'Probe survived its container deadline')
                        time.sleep(0.1)
                    self.assertEqual(subprocess.check_output(['docker', 'inspect', '--format', '{{.State.Running}}', cid], text=True).strip(), 'true')
                finally:
                    if client.poll() is None: client.kill(); client.wait(timeout=5)
                    if not client.stdin.closed: client.stdin.close()
        finally:
            subprocess.run(['docker', 'rm', '-f', cid], check=True, capture_output=True, timeout=15)


if __name__ == '__main__':
    unittest.main()

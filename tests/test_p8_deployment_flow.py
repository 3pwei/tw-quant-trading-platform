"""Complete shell orchestration with explicit external-boundary doubles.

CI runs this inside a network-none disposable container at the literal staging
path. Local execution remaps only that literal path into a temporary directory;
no production script gains a bypass switch. Neither mode is live acceptance.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

REPOSITORY = Path(__file__).resolve().parents[1]
STAGING = REPOSITORY / 'deploy/staging'
DEFAULT_ROOT = Path('/srv/trading-platform-staging')


@unittest.skipUnless(os.environ.get('P8_DEPLOYMENT_CONTRACT_TEST') == '1', 'mandatory in isolated public-images CI container')
class DeploymentFlowTests(unittest.TestCase):
    def test_a_b_rollback_acceptance_and_failed_b_compensation(self):
        with tempfile.TemporaryDirectory(prefix='p8-flow-') as directory:
            literal = os.environ.get('P8_LITERAL_STAGING_ROOT') == '1'
            if literal:
                self.assertTrue(Path('/.dockerenv').is_file(), 'literal staging path is only allowed in a disposable Docker container')
            root = DEFAULT_ROOT if literal else Path(directory) / 'staging'
            # Never adopt or delete pre-existing host state.
            self.assertFalse(root.exists(), 'contract test requires an empty disposable root')
            root.mkdir()
            try:
                self.exercise(root, literal)
            finally:
                shutil.rmtree(root)

    def exercise(self, root, literal):
        bundle = root / 'bundle'; bundle.mkdir()
        for path in STAGING.iterdir():
            if path.is_file():
                raw = path.read_bytes()
                content = raw if literal else raw.replace(str(DEFAULT_ROOT).encode(), str(root).encode())
                (bundle / path.name).write_bytes(content)
                (bundle / path.name).chmod(path.stat().st_mode & 0o777)
                if literal: self.assertEqual(hashlib.sha256(raw).digest(), hashlib.sha256(content).digest())
        for name in ('config', 'deployments', 'provider', 'bin'):
            (root / name).mkdir()
        (root / 'config/compose.env').write_text('')
        (root / 'provider/factory').write_text('inert-public-fixture')
        (bundle / 'acceptance-session').write_text('123-1')
        boundary = REPOSITORY / 'tests/p8_deployment_boundary.py'
        for command in ('docker', 'python3', 'systemctl', 'stat', 'chown'):
            launcher = root / 'bin' / command
            launcher.write_text('#!' + sys.executable + '\nimport os,sys\nos.execv(' + repr(sys.executable) +
                ', [' + repr(sys.executable) + ', ' + repr(str(boundary)) + ', ' + repr(command) + ', *sys.argv[1:]])\n')
            launcher.chmod(0o755)
        env = {**os.environ, 'P8_FIXTURE_ROOT': str(root), 'INSTALL_ROOT': str(root),
               'PATH': str(root / 'bin') + os.pathsep + os.environ['PATH']}
        # Valid manifest and real config archive bytes; only registry/engine are doubles.
        args = [sys.executable, str(bundle / 'candidate_manifest.py'), 'create', '--output', str(bundle / 'candidate-manifest.json'),
                '--pipeline-revision', '1' * 40, '--run-id', '123', '--run-attempt', '1']
        for name, role in (('runtime-a', 'runtime-a'), ('runtime-b', 'runtime-b'), ('gateway', 'gateway')):
            payload = json.dumps({'fixture': name}).encode()
            digest = 'sha256:' + hashlib.sha256(name.encode()).hexdigest()
            config = hashlib.sha256(payload).hexdigest()
            (root / (config + '.json')).write_bytes(payload)
            ref = 'ghcr.io/3pwei/tw-quant-trading-platform-staging:' + role + '-123-1@' + digest
            args += ['--' + name + '-ref', ref, '--' + name + '-digest', digest,
                     '--' + name + '-config-digest', 'sha256:' + config]
        self.run_process(args, env)
        self.run_process([sys.executable, str(bundle / 'acceptance_evidence.py'), 'init', '--minutes', '30'], env)
        script = str(bundle / 'deploy.sh')
        manifest = str(bundle / 'candidate-manifest.json')
        for role in ('known_good', 'candidate'):
            self.run_process(['bash', script, 'deploy', manifest, role], env)
        self.run_process(['bash', script, 'rollback'], env)
        self.run_process(['bash', str(bundle / 'soak.sh'), '30'], env)
        evidence = root / 'deployments/evidence/123-1'
        report = json.loads((evidence / 'acceptance.json').read_text())
        self.assertEqual(report['acceptance'], 'PASS')
        self.assertEqual(report['final_release'], 'known_good')
        self.assertEqual(report['previous_release'], 'candidate')
        self.assertEqual(report['soak']['requested_minutes'], 30)
        rows = [json.loads(line) for line in (evidence / 'ledger.jsonl').read_text().splitlines()]
        self.assertEqual(len(rows), 13)
        self.assertEqual([row['event'] for row in rows], report['stages'])
        current = (root / 'deployments/current.env').read_bytes()
        previous = (root / 'deployments/previous.env').read_bytes()
        self.assertEqual((root / 'deployments/active-release.env').read_bytes(), current)
        self.assertIn(b'RELEASE_NAME=known_good\n', current)
        self.assertIn(b'RELEASE_NAME=candidate\n', previous)
        self.assertEqual(current.count(b'DEPLOYMENT_MODE='), 1)
        self.assertIn(b'DEPLOYMENT_MODE=rollback\n', current)
        self.assertTrue((root / 'private-boundary-calls').is_file())
        ledger = (evidence / 'ledger.jsonl').read_bytes()
        # Fault after B is created: actual trap must restore A and all records.
        (root / 'fail-candidate').touch()
        failed = self.run_process(['bash', script, 'deploy', manifest, 'candidate'], env, success=False)
        self.assertIn('P8_RESTORE=PASS acceptance=false', failed.stderr)
        for name, expected in (('current', current), ('previous', previous), ('active-release', current)):
            self.assertEqual((root / 'deployments' / (name + '.env')).read_bytes(), expected)
        self.assertEqual(json.loads((root / 'engine.json').read_text())['role'], 'known_good')
        self.assertEqual((evidence / 'ledger.jsonl').read_bytes(), ledger)
        self.run_process([sys.executable, str(bundle / 'acceptance_evidence.py'), 'final'], env, success=False)
        self.assertFalse((evidence / 'acceptance.json').exists(), 'stale PASS must be invalidated')
        print('P8_DEPLOYMENT_CONTRACT=PASS A-B-rollback-ledger-and-compensation fixture=true live_acceptance=false')

    def run_process(self, argv, env, success=True):
        started = time.monotonic()
        print('P8_CONTRACT_STEP=' + ' '.join(argv), flush=True)
        # Each observation launches real subprocesses at the CLI boundary. The
        # container runner can take much longer than the local mapped-path run;
        # this watchdog is separate from every production acceptance budget.
        try:
            result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired as error:
            self.fail('contract watchdog expired: ' + str(error) + '\n' +
                      repr(error.stdout)[-6000:] + '\n' + repr(error.stderr)[-6000:])
        print(f'P8_CONTRACT_STEP_SECONDS={time.monotonic() - started:.3f}', flush=True)
        self.assertEqual(result.returncode == 0, success, result.stdout[-6000:] + result.stderr[-6000:])
        return result


if __name__ == '__main__':
    unittest.main()

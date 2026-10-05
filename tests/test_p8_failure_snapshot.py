"""Failure-time evidence survives restoration and never replaces the failure."""
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

STAGING = Path(__file__).resolve().parents[1] / 'deploy/staging'
spec = importlib.util.spec_from_file_location('diagnostics', STAGING / 'diagnostics.py')
diag = importlib.util.module_from_spec(spec); spec.loader.exec_module(diag)


class FailureSnapshotTests(unittest.TestCase):
    def test_execution_probe_reports_staleness_and_generation_without_raw_document(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); health = root / 'health.json'; marker = root / 'marker'
            heartbeat = (datetime.now(timezone.utc) - timedelta(seconds=45)).isoformat()
            health.write_text(json.dumps({'heartbeat_at': heartbeat, 'generation': 'a' * 32,
                'locked': True, 'external_order_calls': 0, 'secret': 'never-export-this'}))
            marker.write_text('b' * 32)
            script = diag.EXECUTION_PROBE.replace('/run/tw-quant-execution/health.json', str(health)).replace('/tmp/tw-quant-execution-generation', str(marker))
            result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
            self.assertGreaterEqual(data['heartbeat_age_seconds'], 45)
            self.assertFalse(data['generation_match'])
            self.assertNotIn('never-export-this', result.stdout)
            self.assertNotIn('a' * 32, result.stdout)
            marker.write_text('a' * 32)
            data = json.loads(subprocess.check_output([sys.executable, '-c', script], text=True))
            self.assertTrue(data['generation_match'])

    def test_history_and_probe_filter_unsafe_values(self):
        probe = {'secret': 'never-export-this', 'heartbeat_at': 'token=secret',
            'locked': 'secret', 'pid1_state': 'secret', 'external_order_calls': 0,
            'health_file': {'size': 100, 'secret': 'never-export-this'}}
        history = '"2026-10-05T06:35:00Z" "2026-10-05T06:35:01Z" 1\nsecret-output\n'
        with patch.object(diag, 'command', side_effect=[(0, json.dumps(probe)), (0, history)]):
            data = diag.execution_probe('a' * 64)
        raw = json.dumps(data)
        self.assertNotIn('secret', raw)
        self.assertEqual(data['health_history'][0]['exit'], 1)
        self.assertEqual(data['health_file'], {'size': 100})

    def test_probe_timeout_or_bad_document_remains_diagnostic(self):
        for response in ((-1, ''), (0, 'bad JSON')):
            with patch.object(diag, 'command', side_effect=[response, (-1, '')]):
                result = diag.execution_probe('a' * 64)
            self.assertTrue(result['parse_failed'])
            self.assertEqual(result['health_history'], [])

    def test_snapshot_precedes_restore_and_diagnostic_failure_preserves_exit(self):
        script = (STAGING / 'deploy.sh').read_text()
        function = script[script.index('restore_prior() {'):script.index('trap restore_prior EXIT')]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'prior').write_text('STAGING_RUNTIME_IMAGE=known-good-A\n')
            (root / 'active').write_text('STAGING_RUNTIME_IMAGE=candidate-B\n')
            (root / 'verify.sh').write_text('#!/bin/bash\nexit 0\n')
            (root / 'verify.sh').chmod(0o755)
            (root / 'diagnostics.py').write_text('import os,pathlib\np=pathlib.Path(os.environ["TEST_ROOT"])\n(p/"captured").write_bytes((p/"active").read_bytes())\nraise SystemExit(int(os.environ["PROBE_EXIT"]))\n')
            for code in ('0', '23'):
                (root / 'prior').write_text('STAGING_RUNTIME_IMAGE=known-good-A\n')
                (root / 'active').write_text('STAGING_RUNTIME_IMAGE=candidate-B\n')
                shell = 'set -e\n' + function + '''
prior="$TEST_ROOT/prior"; ACTIVE="$TEST_ROOT/active"; CURRENT="$TEST_ROOT/current"
PREVIOUS="$TEST_ROOT/previous"; prior_previous=""; target="$TEST_ROOT/target"
BUNDLE="$TEST_ROOT"; COMPOSE_ENV="$TEST_ROOT/env"; COMPOSE_FILE="$TEST_ROOT/compose"
P8_PHASE=deploy-candidate
docker() { return 0; }
trap restore_prior EXIT
exit 17
'''
                result = subprocess.run(['bash', '-c', shell], env={**os.environ, 'TEST_ROOT': directory, 'PROBE_EXIT': code}, capture_output=True, text=True)
                self.assertEqual(result.returncode, 17, result.stderr)
                self.assertIn('P8_RESTORE=PASS', result.stderr)
                self.assertIn('candidate-B', (root / 'captured').read_text())
                self.assertIn('known-good-A', (root / 'active').read_text())


if __name__ == '__main__': unittest.main()

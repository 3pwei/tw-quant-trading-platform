from datetime import date
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'deploy/security/exception_policy.py'
SPEC = importlib.util.spec_from_file_location('expiry_sources_policy', SCRIPT)
POLICY = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = POLICY
SPEC.loader.exec_module(POLICY)


class SecurityExpirySourceTests(unittest.TestCase):
    def fixture(self, root):
        row = {'id': 'GHSA-test-test-test', 'package': 'braces', 'version': '3.0.3',
               'severity': 'high', 'dependency_scope': 'dev', 'affected_packages': ['braces'],
               'expired_at': '2026-10-18',
               'statement': 'Synthetic dev dependency exception with a fixed review deadline.'}
        (root / 'npm.json').write_text(json.dumps({'schema_version': 1, 'exceptions': [row]}))
        (root / 'lock.json').write_text(json.dumps({'packages': {'node_modules/braces': {'version': '3.0.3', 'dev': True}}}))
        (root / 'trivy.yaml').write_text('vulnerabilities:\n')

    def check(self, root, day, *extra):
        return subprocess.run([sys.executable, str(SCRIPT), 'check',
            '--ignore-file', str(root / 'trivy.yaml'), '--npm-exceptions', str(root / 'npm.json'),
            '--lock-file', str(root / 'lock.json'), '--report', str(root / 'report.json'),
            '--as-of', day, *extra], capture_output=True, text=True)

    def test_npm_only_cli_warning_windows_and_expiry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self.fixture(root)
            for day, urgency, code in [('2026-10-03', 'ok', 0), ('2026-10-04', 'warning', 0),
                                      ('2026-10-11', 'high', 0), ('2026-10-15', 'urgent', 0),
                                      ('2026-10-18', 'expired', 1)]:
                with self.subTest(day=day):
                    result = self.check(root, day, '--fail-on-expired')
                    self.assertEqual(result.returncode, code, result.stderr)
                    report = json.loads((root / 'report.json').read_text())
                    self.assertEqual(report['total'], 1)
                    self.assertEqual(report['entries'][0]['source'], 'npm')
                    self.assertEqual(report['entries'][0]['urgency'], urgency)
                    self.assertEqual(report['notification_required'], urgency != 'ok')
            # Scheduled check emits the expired report for notification, and the
            # independent always() enforcement still blocks it afterward.
            self.assertEqual(self.check(root, '2026-10-18').returncode, 0)
            result = subprocess.run([sys.executable, str(SCRIPT), 'enforce', '--report', str(root / 'report.json')], capture_output=True)
            self.assertEqual(result.returncode, 1)

    def test_invalid_or_missing_npm_input_removes_stale_pass(self):
        for fault in ('missing', 'malformed', 'version', 'scope'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); self.fixture(root)
                self.assertEqual(self.check(root, '2026-10-03').returncode, 0)
                if fault == 'missing': (root / 'npm.json').unlink()
                elif fault == 'malformed': (root / 'npm.json').write_text('{}')
                else:
                    lock = json.loads((root / 'lock.json').read_text())
                    entry = lock['packages']['node_modules/braces']
                    entry['version' if fault == 'version' else 'dev'] = '0.0.0' if fault == 'version' else False
                    (root / 'lock.json').write_text(json.dumps(lock))
                self.assertNotEqual(self.check(root, '2026-10-03').returncode, 0)
                self.assertFalse((root / 'report.json').exists())
                result = subprocess.run([sys.executable, str(SCRIPT), 'enforce', '--report', str(root / 'report.json')], capture_output=True)
                self.assertNotEqual(result.returncode, 0)

    def test_same_advisory_in_two_sources_remains_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self.fixture(root)
            (root / 'trivy.yaml').write_text('vulnerabilities:\n  - id: GHSA-test-test-test\n    statement: fixture\n    expired_at: 2026-10-18\n')
            self.assertEqual(self.check(root, '2026-10-04').returncode, 0)
            report = json.loads((root / 'report.json').read_text())
            self.assertEqual(report['total'], 2)
            self.assertEqual({x['source'] for x in report['entries']}, {'npm', 'trivy'})
            summary = POLICY.render_summary(report)
            self.assertIn('| npm |', summary); self.assertIn('| trivy |', summary)
            one = POLICY.build_report([POLICY.VulnerabilityException('GHSA-test-test-test', date(2026, 10, 18), None, 'npm')], date(2026, 10, 4))
            other = POLICY.build_report([POLICY.VulnerabilityException('GHSA-test-test-test', date(2026, 10, 18), 2)], date(2026, 10, 4))
            self.assertNotEqual(POLICY.issue_state_marker(one), POLICY.issue_state_marker(other))

    def test_npm_notifications_deduplicate_escalate_and_close(self):
        item = POLICY.VulnerabilityException('GHSA-test-test-test', date(2026, 10, 18), None, 'npm')
        warning = POLICY.build_report([item], date(2026, 10, 4))
        existing = {'number': 7, 'body': POLICY.render_issue_body(warning, 'owner/repo')}
        cases = [(warning, 'unchanged', ['GET']),
                 (POLICY.build_report([item], date(2026, 10, 11)), 'updated', ['GET', 'POST', 'PATCH']),
                 (POLICY.build_report([], date(2026, 10, 11)), 'closed', ['GET', 'PATCH'])]
        for report, expected, methods in cases:
            calls = []
            def fake(method, url, token, payload=None):
                calls.append((method, payload))
                return [existing] if method == 'GET' else None
            with self.subTest(expected=expected), patch.object(POLICY, 'github_request', side_effect=fake):
                self.assertEqual(POLICY.sync_tracking_issue(report, 'owner/repo', 'fixture-token'), expected)
            self.assertEqual([x[0] for x in calls], methods)
        with patch.object(POLICY, 'github_request', side_effect=[[], None]) as request:
            self.assertEqual(POLICY.sync_tracking_issue(warning, 'owner/repo', 'fixture-token'), 'created')
            self.assertIn('| npm |', request.call_args.args[3]['body'])


if __name__ == '__main__':
    unittest.main()

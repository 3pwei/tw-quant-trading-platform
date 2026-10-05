"""Production request/evidence and transaction regression tests; no host credentials."""
import copy
import hashlib
import importlib.util
import io
import json
import os
import subprocess
from pathlib import Path
import re
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'deploy/production'))
import evidence
import cutover
import durable_state

PINS = json.loads((ROOT / 'deploy/production/approved-p8.json').read_text())


def manifest():
    return (json.dumps(PINS['manifest'], indent=2, sort_keys=True) + '\n').encode()


def acceptance_fixture():
    pins = copy.deepcopy(PINS)
    session = {'session': pins['staging_run_id'] + '-1', 'candidate_run_id': pins['candidate_run_id'],
               'pipeline': pins['platform_sha']}
    files = {'candidate-manifest.json': manifest(), 'ledger.jsonl': b'{"synthetic":"fixture"}\n',
             'session.json': json.dumps(session).encode()}
    report = {**session, 'acceptance': 'PASS', 'final_release': pins['release'],
              'manifest_sha256': pins['manifest_sha256'], 'ledger_sha256': evidence.sha(files['ledger.jsonl']),
              'soak': {'elapsed_seconds': 3601, 'max_sample_gap_seconds': 15}}
    files['acceptance.json'] = json.dumps(report).encode()
    pins['acceptance_files'] = {k: evidence.sha(v) for k, v in files.items()}
    return files, pins


class IdentityTests(unittest.TestCase):
    def test_approved_exact_identity(self):
        evidence.request(PINS, PINS['platform_sha'], PINS['candidate_run_id'], PINS['manifest_sha256'], evidence.confirmation(PINS))
        evidence.verify_manifest(manifest(), PINS)

    def test_wrong_sha_candidate_manifest_confirmation_rejected(self):
        values = [PINS['platform_sha'], PINS['candidate_run_id'], PINS['manifest_sha256'], evidence.confirmation(PINS)]
        for i, wrong in enumerate(['0' * 40, '37306446380', '0' * 64, 'DEPLOY production']):
            bad = values[:]; bad[i] = wrong
            with self.subTest(field=i), self.assertRaises(ValueError):
                evidence.request(PINS, *bad)

    def test_wrong_image_config_registry_digest_and_provenance_rejected(self):
        for field in ('digest', 'config_digest', 'ref'):
            bad = copy.deepcopy(PINS['manifest'])
            image = bad['images']['releases']['known_good']['runtime']
            image[field] = 'sha256:' + '0' * 64 if field != 'ref' else image['ref'].replace('f1894', '00000')
            payload = json.dumps(bad).encode()
            pins = copy.deepcopy(PINS); pins['manifest_sha256'] = evidence.sha(payload)
            with self.subTest(field=field), self.assertRaises(ValueError):
                evidence.verify_manifest(payload, pins)
        for key in ('core', 'private_provider'):
            bad = copy.deepcopy(PINS['manifest']); bad[key]['wheel_sha256'] = '0' * 64
            with self.assertRaises(ValueError):
                evidence.verify_manifest(json.dumps(bad).encode(), PINS)

    def test_missing_p8_acceptance_checksum_and_failed_report(self):
        files, pins = acceptance_fixture()
        evidence.verify_acceptance(files, pins)
        for name in files:
            bad = files.copy(); del bad[name]
            with self.subTest(name=name), self.assertRaises(ValueError):
                evidence.verify_acceptance(bad, pins)
        bad = files.copy(); bad['ledger.jsonl'] += b'x'
        with self.assertRaises(ValueError):
            evidence.verify_acceptance(bad, pins)
        report = json.loads(files['acceptance.json']); report['acceptance'] = 'FAIL'
        files['acceptance.json'] = json.dumps(report).encode()
        pins['acceptance_files']['acceptance.json'] = evidence.sha(files['acceptance.json'])
        with self.assertRaises(ValueError):
            evidence.verify_acceptance(files, pins)

    def test_latest_master_ci_security_required(self):
        def run(name, number, conclusion='success'):
            return {'id': number, 'name': name, 'path': '.github/workflows/' + name.lower() + '.yml',
                    'head_sha': PINS['platform_sha'], 'head_branch': 'master', 'event': 'push',
                    'repository': {'full_name': PINS['repository']}, 'run_number': number, 'run_attempt': 1,
                    'status': 'completed', 'conclusion': conclusion}
        rows = [run('CI', 1), run('Security', 2)]
        with patch.object(evidence, 'api', return_value={'total_count': 2, 'workflow_runs': rows}):
            self.assertEqual(evidence.master_gates(PINS['platform_sha']), {'CI': 1, 'Security': 2})
        rows.append(run('CI', 3, 'failure'))
        with patch.object(evidence, 'api', return_value={'total_count': 3, 'workflow_runs': rows}), self.assertRaises(ValueError):
            evidence.master_gates(PINS['platform_sha'])
        with patch.object(evidence, 'api', return_value={'total_count': 4, 'workflow_runs': rows}), self.assertRaises(ValueError):
            evidence.master_gates(PINS['platform_sha'])


class HostGateTests(unittest.TestCase):
    def test_retained_rollback_uses_original_ids_even_without_release_env(self):
        original = {s: {'id': str(i) * 64, 'image_id': 'sha256:' + str(i) * 64} for i, s in enumerate(cutover.SERVICES, 1)}
        calls = []
        def run(argv, *args, **kwargs):
            calls.append(argv)
            return b'4' * 64 if argv[:3] == ['docker', 'ps', '-aq'] else b''
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'transaction.json').write_text(json.dumps({'durable': {'durable': {'targets': 'same'}}}))
            with patch.object(cutover, 'ROOT', root), patch.object(cutover, 'run', side_effect=run), \
                 patch.object(cutover, 'legacy_state', return_value=original), patch.object(cutover, 'healthy'), \
                 patch.object(cutover, 'snapshot', return_value={'durable': {'targets': 'same'}}):
                cutover.rollback(PINS, {}, b'fixture')
            self.assertEqual([c for c in calls if c[:2] == ['docker', 'start']],
                             [['docker', 'start', original[s]['id']] for s in cutover.SERVICES])
            self.assertTrue((root / 'rollback-result.json').exists())
            self.assertFalse(any('compose' in c or 'build' in c or 'rm' in c for c in calls))

    def test_real_order_and_canary_enabled_hard_fail(self):
        baseline = {'BROKER_PROVIDER': 'disabled', 'LIVE_TRADING_ENABLED': 'false'}
        cutover.disabled(baseline, execution=True)
        for key in cutover.DISABLED:
            for value in ('true', '1', 'yes', 'on', 'FALSE'):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    cutover.disabled({**baseline, key: value}, execution=True)
        with self.assertRaises(ValueError):
            cutover.disabled({**baseline, 'BROKER_PROVIDER': 'shioaji'}, execution=True)
        with self.assertRaises(ValueError):
            cutover.disabled({**baseline, 'LIVE_TRADING_CONFIRMATION': 'LIVE'}, execution=True)

    def test_symlink_or_wrong_config_digest_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config'; path.write_bytes(b'separate production config'); path.chmod(0o600)
            cutover.protected(path, cutover.file_sha(path), uid=path.stat().st_uid)
            with self.assertRaises(ValueError):
                cutover.protected(path, '0' * 64, uid=path.stat().st_uid)
            link = Path(tmp) / 'link'; link.symlink_to(path)
            with self.assertRaises(ValueError):
                cutover.protected(link, uid=path.stat().st_uid)

    def test_production_revision_mismatch_before_container_commands(self):
        with patch.object(cutover, 'run', return_value=b'0' * 40), self.assertRaisesRegex(ValueError, 'production-revision-mismatch'):
            cutover.legacy_state(PINS, {})

    def test_terminal_health_failure_has_no_retry(self):
        for status in ('unhealthy', None):
            with patch.object(cutover, 'inspect', return_value={'State': {'Status': 'running', 'Running': True,
                 'Health': {'Status': status}}}), patch.object(cutover.time, 'sleep') as sleep, self.assertRaises(ValueError):
                cutover.healthy('a' * 64)
            sleep.assert_not_called()

    def test_wrong_registry_or_image_config_digest_rejected(self):
        image = PINS['manifest']['images']['gateway']
        document = {'Id': 'sha256:' + 'a' * 64, 'RepoDigests': [], 'Config': {'Labels': {}}}
        with patch.object(cutover, 'run', return_value=json.dumps([document]).encode()), self.assertRaisesRegex(ValueError, 'registry-digest-mismatch'):
            cutover.local_image(image, PINS, gateway=True)
        document['RepoDigests'] = [image['ref'].split(':', 1)[0] + '@' + image['digest']]
        class Process:
            stdout = io.BytesIO()
            def poll(self): return 0
        import image_config_digest
        with patch.object(cutover, 'run', return_value=json.dumps([document]).encode()), \
             patch.object(cutover.subprocess, 'Popen', return_value=Process()), \
             patch.object(image_config_digest, 'config_digest', return_value='sha256:' + '0' * 64), \
             self.assertRaisesRegex(ValueError, 'image-config-digest-mismatch'):
            cutover.local_image(image, PINS, gateway=True)

    def test_sqlite_integrity_locked_targets_and_durable_drift(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'db.sqlite3'
            con = sqlite3.connect(path)
            con.execute('CREATE TABLE execution_targets (target_id TEXT, status TEXT)')
            con.execute("INSERT INTO execution_targets VALUES ('owner-target', 'locked')"); con.commit()
            first = durable_state.snapshot(path)
            con.execute("UPDATE execution_targets SET status='active'"); con.commit()
            with self.assertRaisesRegex(ValueError, 'target-not-locked'):
                durable_state.snapshot(path)
            con.close()
            self.assertEqual(first['active_targets'], 0)


class Backend:
    def __init__(self, fail=None):
        self.calls = []; self.fail = fail; self.original = {'revision': PINS['legacy_revision'], 'image': 'legacy', 'data': 'legacy-data'}
    def stage(self, name):
        self.calls.append(name)
        if name == self.fail:
            raise ValueError('injected-invariant')
    def preflight(self): self.stage('preflight')
    def preserve(self): self.stage('preserve'); self.saved = self.original.copy()
    def stop_legacy(self): self.stage('stop')
    def deploy(self): self.stage('deploy')
    def verify(self): self.stage('verify'); return {'snapshot': True}
    def restart(self): self.stage('restart')
    def public_verify(self): self.stage('public')
    def commit(self, before, after): self.stage('commit')
    def failure(self, exc): self.stage('failure')
    def rollback(self): self.stage('rollback'); self.original = self.saved.copy()


class TransactionTests(unittest.TestCase):
    def test_every_first_failure_preserves_exact_rollback_and_stops_forward_progress(self):
        for stage in ('stop', 'deploy', 'verify', 'restart', 'public', 'commit'):
            backend = Backend(stage)
            with self.subTest(stage=stage), patch.object(cutover, 'continuity'), self.assertRaises(ValueError):
                cutover.transaction(backend)
            self.assertEqual(backend.calls[-2:], ['failure', 'rollback'])
            self.assertEqual(backend.original, backend.saved)
            self.assertEqual(backend.calls.count(stage), 1)

    def test_preflight_failure_never_writes_or_stops(self):
        backend = Backend('preflight')
        with self.assertRaises(ValueError): cutover.transaction(backend)
        self.assertEqual(backend.calls, ['preflight'])

    def test_failure_reporting_error_still_recovers(self):
        backend = Backend('deploy')
        backend.failure = lambda exc: (_ for _ in ()).throw(OSError('disk full'))
        with self.assertRaises(OSError): cutover.transaction(backend)
        self.assertEqual(backend.calls[-1], 'rollback')

    def test_rollback_failure_is_hard_failure(self):
        backend = Backend('deploy')
        backend.rollback = lambda: (_ for _ in ()).throw(ValueError('recovery-failed'))
        with self.assertRaisesRegex(ValueError, 'recovery-failed'): cutover.transaction(backend)
        self.assertNotIn('commit', backend.calls)

    def test_success_order_requires_restart_and_public_gate(self):
        backend = Backend()
        with patch.object(cutover, 'continuity') as continuity:
            cutover.transaction(backend)
        self.assertEqual(backend.calls, ['preflight', 'preserve', 'stop', 'deploy', 'verify', 'restart', 'verify', 'public', 'commit'])
        continuity.assert_called_once()

    def test_restart_generation_or_durable_mismatch_rejected(self):
        a = {'durable': {'target': 'one'}, 'execution': {'generation': 'one'},
             'containers': {s: {'id': s, 'image_id': 'image', 'started': '2026-10-05T00:00:00Z', 'restarts': 0} for s in cutover.SERVICES}}
        b = copy.deepcopy(a)
        for s in cutover.SERVICES: b['containers'][s]['started'] = '2026-10-05T00:00:01Z'
        b['execution']['generation'] = 'two'
        cutover.continuity(a, b)
        for field in ('generation', 'durable', 'image'):
            bad = copy.deepcopy(b)
            if field == 'generation': bad['execution']['generation'] = 'one'
            elif field == 'durable': bad['durable']['target'] = 'other'
            else: bad['containers']['gateway']['image_id'] = 'other'
            with self.subTest(field=field), self.assertRaises(ValueError): cutover.continuity(a, bad)


class WorkflowTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('CADDY_BIN'), 'Caddy artifact binary integration requires CI')
    def test_accepted_gateway_binary_adapts_production_auth_tls_overlay(self):
        env = {**os.environ, 'MARKET_DOMAIN': 'production.example.invalid', 'ACME_EMAIL': 'operator@example.invalid'}
        p = subprocess.run([os.environ['CADDY_BIN'], 'adapt', '--config', str(ROOT / 'deploy/production/Caddyfile'),
                            '--adapter', 'caddyfile'], env=env, capture_output=True, check=True, text=True)
        adapted = json.loads(p.stdout)
        encoded = json.dumps(adapted)
        self.assertIn('/internal/auth/cloudflare', encoded)
        self.assertIn('X-Authenticated-Role', encoded)
        self.assertIn('Strict-Transport-Security', encoded)
        self.assertIn('127.0.0.1:8080', encoded)
        self.assertNotIn('18080', encoded)
        self.assertNotIn('staging-ingress', encoded)

    def test_manual_only_and_separate_production_secrets(self):
        text = (ROOT / '.github/workflows/deploy-production.yml').read_text()
        events = text.split('\non:\n')[1].split('\npermissions:')[0]
        self.assertEqual(re.findall(r'^  ([a-z_]+):', events, re.M), ['workflow_dispatch'])
        self.assertNotIn('workflow_run', text)
        self.assertNotIn('secrets.STAGING', text)
        self.assertIn('environment: lightsail-production', text)
        self.assertIn('group: lightsail-production', text)
        self.assertIn('cancel-in-progress: false', text)
        self.assertIn('--attempts 1', text)
        self.assertIn('transport.py recover', text)
        self.assertLess(text.index('id: public_origin'), text.index('id: finalize'))

    def test_no_rebuild_on_production_and_health_contract_preserved(self):
        d = json.loads((ROOT / 'deploy/production/docker-compose.yml').read_text())
        for service in d['services'].values():
            self.assertNotIn('build', service)
            self.assertEqual(service['pull_policy'], 'never')
            self.assertEqual(service['healthcheck']['timeout'], '5s')
        self.assertEqual(d['services']['execution-worker']['network_mode'], 'none')
        text = (ROOT / 'deploy/production/cutover.py').read_text()
        self.assertIn("['up', '--no-build', '--pull', 'never', '--detach']", text)
        for operation in ('build', 'checkout', 'reset', 'rm', 'down'):
            self.assertNotRegex(text, r"\['docker'(?:, [^\]]+)? , '" + operation + "'")
        self.assertNotIn('docker build', (ROOT / '.github/workflows/deploy-production.yml').read_text())
        self.assertIn('forward_auth', (ROOT / 'deploy/production/Caddyfile').read_text())


if __name__ == '__main__':
    unittest.main()

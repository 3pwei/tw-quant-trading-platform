"""Filesystem publication tests with real SQLite/rename; no Production access."""
import copy
import contextlib
import fcntl
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import types
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, REPO / path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    sys.modules[name] = result
    return result


with mock.patch.dict(sys.modules):
    image = load('image_config_digest', 'deploy/staging/image_config_digest.py')
    db = load('readonly_sqlite', 'deploy/production/readonly_sqlite.py')
    validation = load('p9_validation', 'deploy/production/cutover.py')
    provision = load('provision_host', 'deploy/production/provision_host.py')
    host = load('refresh_rollback_host', 'deploy/production/refresh_rollback_host.py')
    runner_base = load('provision_prerequisites', 'deploy/production/provision_prerequisites.py')
    runner = load('refresh_rollback', 'deploy/production/refresh_rollback.py')


class RefreshFilesystemTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'production'
        self.root.mkdir(mode=0o700)
        for name in ('config', 'provider', 'rollback'):
            (self.root / name).mkdir(mode=0o700)
        self.legacy = Path(self.tmp.name) / 'legacy'
        (self.legacy / 'config').mkdir(parents=True)
        (self.legacy / 'deployments').mkdir()
        (self.legacy / 'deployments/current.env').write_text('synthetic deployment')
        self.lock = Path(self.tmp.name) / 'deploy.lock'
        self.lock.touch()
        self.dbfile = Path(self.tmp.name) / 'live.sqlite3'
        with sqlite3.connect(self.dbfile) as con:
            con.executescript("CREATE TABLE execution_targets(id INTEGER,status TEXT);"
                              "INSERT INTO execution_targets VALUES(1,'locked');"
                              "CREATE TABLE user_state(value TEXT);"
                              "INSERT INTO user_state VALUES('before');")
        rollback = self.root / 'rollback'
        (rollback / 'data.sqlite3').write_bytes(self.dbfile.read_bytes())
        with tarfile.open(rollback / 'config.tar', 'w:') as archive:
            for name in ('compose.env', 'market.env', 'execution.env', 'gateway.env'):
                data = b'synthetic config'
                (self.legacy / 'config' / name).write_bytes(data)
                info = tarfile.TarInfo('config/' + name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        self.containers = {}
        for i, name in enumerate(provision.SERVICES):
            (rollback / ('image-' + name + '.tar')).write_bytes(b'synthetic sealed image')
            self.containers[name] = {'Id': str(i + 1) * 64, 'Image': 'sha256:' + 'a' * 64,
                                     'State': {'StartedAt': 'synthetic generation'}}
        self.pins = {'legacy_revision': 'b' * 40}
        document = {'schema_version': 1, 'revision': self.pins['legacy_revision'],
                    'domain': 'example.invalid',
                    'record_sha256': provision.sha(self.legacy / 'deployments/current.env'),
                    'containers': {s: {'id': v['Id'], 'image_id': v['Image'],
                                      'config_digest': 'sha256:' + 'c' * 64}
                                   for s, v in self.containers.items()},
                    'files': {n: provision.sha(rollback / n) for n in provision.SEALED}}
        (rollback / 'rollback.json').write_text(json.dumps(document, sort_keys=True) + '\n')
        for path in rollback.iterdir():
            path.chmod(0o600)
        self.before = {p.name: p.read_bytes() for p in rollback.iterdir()}
        self.previous = provision.sha(rollback / 'rollback.json')
        self.hashes = {n: 'd' * 64 for n in runner_base.SOURCES}
        self.payload = {'control_sha': 'e' * 40, 'pins': self.pins,
                        'hashes': self.hashes, 'previous_sha256': self.previous}
        self.stack = self.enterContext(contextlib.ExitStack())
        for obj, name, value in [(host, 'ROOT', self.root), (host, 'LOCK', self.lock),
                                 (validation, 'ROOT', self.root), (validation, 'LEGACY', self.legacy),
                                 (provision, 'LEGACY', self.legacy)]:
            self.stack.enter_context(mock.patch.object(obj, name, value))
        # CI fixtures are owned by the runner UID, not root. Keep path/mode checks.
        def directory(path):
            self.assertTrue(path.is_dir() and not path.is_symlink() and path.resolve() == path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        self.stack.enter_context(mock.patch.object(host, 'directory', side_effect=directory))
        protected = validation.protected
        self.stack.enter_context(mock.patch.object(validation, 'protected',
            side_effect=lambda path, digest=None, uid=0: protected(path, digest, uid=os.getuid())))
        self.stack.enter_context(mock.patch.object(host.os, 'geteuid', return_value=0))
        self.stack.enter_context(mock.patch.object(provision.os, 'fchown'))
        self.config = self.stack.enter_context(mock.patch.object(validation, 'config_check',
            return_value={'market.env': {'MARKET_DATA_PROVIDER': 'shioaji'}}))
        self.stack.enter_context(mock.patch.object(provision, 'current_state', side_effect=lambda pins:
            (self.pins['legacy_revision'], copy.deepcopy(self.containers), {}, {},
             {'external_order_calls': 0, 'external_cancel_calls': 0})))
        self.capture = self.stack.enter_context(mock.patch.object(provision, 'stable_database',
            side_effect=lambda *args: (self.dbfile.read_bytes(), db.snapshot(self.dbfile))))

    def update_db(self):
        with sqlite3.connect(self.dbfile) as con:
            con.execute("UPDATE user_state SET value='after'")

    def assert_original(self):
        self.assertEqual({p.name: p.read_bytes() for p in (self.root / 'rollback').iterdir()}, self.before)

    def test_refresh_archives_original_and_preserves_legacy_bytes(self):
        self.update_db()
        legacy_bytes = self.dbfile.read_bytes()
        result = host.refresh(self.payload)
        self.assertTrue(result['changed'])
        self.assertFalse(result['approval_updated'])
        self.assertEqual(result['previous_sha256'], self.previous)
        self.assertEqual(self.dbfile.read_bytes(), legacy_bytes)
        self.assertEqual((self.root / 'rollback/data.sqlite3').read_bytes(), legacy_bytes)
        archive = self.root / 'rollback-history' / self.previous / 'rollback'
        self.assertEqual({p.name: p.read_bytes() for p in archive.iterdir()}, self.before)
        self.assertFalse(list(self.root.glob('.rollback-refresh-*')))
        # Old approval cannot validate refreshed canonical bytes.
        with self.assertRaises(ValueError):
            validation.verify_backup(self.pins, self.previous)
        validation.verify_backup(self.pins, result['rollback_sha256'])
        with self.assertRaises(host.RefreshBlocked):
            host.refresh(self.payload)

    def test_unchanged_snapshot_is_noop_with_same_digest(self):
        result = host.refresh(self.payload)
        self.assertFalse(result['changed'])
        self.assertEqual(result['rollback_sha256'], self.previous)
        self.assert_original()
        host.refresh(self.payload)  # Existing history is checked, never overwritten.
        self.assert_original()

    def test_multiple_refreshes_retain_each_predecessor(self):
        self.update_db()
        first = host.refresh(self.payload)
        prior = {p.name: p.read_bytes() for p in (self.root / 'rollback').iterdir()}
        with sqlite3.connect(self.dbfile) as con:
            con.execute("UPDATE user_state SET value='third'")
        second = host.refresh(dict(self.payload, previous_sha256=first['rollback_sha256']))
        self.assertNotEqual(first['rollback_sha256'], second['rollback_sha256'])
        history = self.root / 'rollback-history'
        self.assertEqual({p.name: p.read_bytes() for p in (history / self.previous / 'rollback').iterdir()},
                         self.before)
        self.assertEqual({p.name: p.read_bytes() for p in
                          (history / first['rollback_sha256'] / 'rollback').iterdir()}, prior)

    def test_exchange_failure_keeps_old_canonical_and_archive(self):
        self.update_db()
        with mock.patch.object(host, 'exchange', side_effect=host.RefreshBlocked('refresh-exchange-failed')):
            with self.assertRaises(host.RefreshBlocked):
                host.refresh(self.payload)
        self.assert_original()
        self.assertTrue((self.root / 'rollback-history' / self.previous / 'rollback/rollback.json').is_file())

    def test_post_exchange_failure_retains_both_versions_and_blocks_retry(self):
        self.update_db()
        exchange = host.exchange
        def exchange_and_fail_fsync(a, b):
            exchange(a, b)
            # Simulate durability failure after successful publication.
            provision.fsync_dir.side_effect = OSError('synthetic disk failure')
        with mock.patch.object(provision, 'fsync_dir', wraps=provision.fsync_dir), \
             mock.patch.object(host, 'exchange', side_effect=exchange_and_fail_fsync):
            with self.assertRaises(OSError):
                host.refresh(self.payload)
        self.assertTrue(list(self.root.glob('.rollback-refresh-*')))
        archived = self.root / 'rollback-history' / self.previous / 'rollback'
        self.assertEqual({p.name: p.read_bytes() for p in archived.iterdir()}, self.before)
        self.assertEqual((self.root / 'rollback/data.sqlite3').read_bytes(), self.dbfile.read_bytes())
        with self.assertRaisesRegex(host.RefreshBlocked, 'incomplete-preparation'):
            host.refresh(self.payload)

    def test_database_changes_during_capture_prevent_publication(self):
        calls = 0
        def capture(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.update_db()
            return self.dbfile.read_bytes(), db.snapshot(self.dbfile)
        self.capture.side_effect = capture
        with self.assertRaisesRegex(host.RefreshBlocked, 'snapshot-failed'):
            host.refresh(self.payload)
        self.assert_original()

    def test_approval_mismatch_and_cutover_residue_are_write_free(self):
        for bad in [dict(self.payload, previous_sha256='f' * 64), self.payload]:
            if bad == self.payload:
                (self.root / 'transaction.json').write_text('{}')
            with self.assertRaises(host.RefreshBlocked):
                host.refresh(bad)
            self.assert_original()
            self.assertFalse((self.root / 'rollback-history').exists())

    def test_locked_or_invalid_database_is_rejected(self):
        with self.lock.open('rb') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(host.RefreshBlocked, 'lock-unavailable'):
                host.refresh(self.payload)
        with sqlite3.connect(self.dbfile) as con:
            con.execute("UPDATE execution_targets SET status='active'")
        with self.assertRaisesRegex(host.RefreshBlocked, 'snapshot-failed'):
            host.refresh(self.payload)
        self.assert_original()

    def test_archive_corruption_is_not_overwritten(self):
        host.refresh(self.payload)
        archive = self.root / 'rollback-history' / self.previous / 'rollback/data.sqlite3'
        archive.write_bytes(b'corrupt fixture')
        with self.assertRaisesRegex(host.RefreshBlocked, 'history-conflict'):
            host.refresh(self.payload)
        self.assertEqual(archive.read_bytes(), b'corrupt fixture')
        self.assert_original()


class RunnerTests(unittest.TestCase):
    def test_remote_exceptions_are_sanitized_in_actual_process(self):
        for exception, reason in [
            ("RefreshBlocked('refresh-snapshot-failed')", 'refresh-snapshot-failed'),
            ("RuntimeError('private fixture')", 'refresh-host-failed'),
            ("RefreshBlocked('private fixture')", 'refresh-host-failed'),
        ]:
            source = ("class RefreshBlocked(Exception): pass\n"
                      "REASONS={'refresh-snapshot-failed','refresh-host-failed'}\n"
                      f"def refresh(payload): raise {exception}\n")
            original = runner_base.module
            def stub(name, unused):
                return original(name, source.encode() if name == 'refresh_rollback_host' else b'')
            with mock.patch.object(runner_base, 'module', side_effect=stub):
                program = runner.remote_program({})
            result = subprocess.run([sys.executable, '-B', '-'], input=program,
                                    capture_output=True, check=False, timeout=10)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stderr, b'')
            self.assertEqual(json.loads(result.stdout), {'status': 'BLOCKED', 'reason': reason})

    def test_manual_workflow_and_safety_boundaries(self):
        source = (REPO / '.github/workflows/production-rollback-refresh.yml').read_text()
        self.assertIn('workflow_dispatch:', source)
        self.assertNotIn('\n  push:', source)
        self.assertIn('environment: lightsail-production', source)
        self.assertIn('group: lightsail-production', source)
        self.assertIn('LEGACY_ROLLBACK_INVENTORY_SHA256', source)
        self.assertNotIn('PRODUCTION_MARKET_ENV_B64', source)
        text = (REPO / 'deploy/production/refresh_rollback_host.py').read_text()
        for forbidden in ("'docker', 'stop'", "'docker', 'pull'", "'docker', 'restart'", 'shutil.rmtree(ROOT'):
            self.assertNotIn(forbidden, text)

    def test_untrusted_response_is_rejected(self):
        for body, status in [({'status': 'BLOCKED', 'reason': 'private fixture'}, 1),
                             ({'status': 'BLOCKED', 'reason': 'refresh-host-failed', 'extra': 'private'}, 1),
                             ({'status': 'PASS'}, 0),
                             ({'status': 'BLOCKED', 'reason': 'refresh-host-failed'}, 0)]:
            with self.assertRaises(runner.RefreshBlocked):
                runner.response(types.SimpleNamespace(returncode=status, stdout=json.dumps(body).encode()),
                                'a' * 64, {})

    def test_runner_gates_precede_ssh(self):
        sha, previous = 'a' * 40, 'b' * 64
        env = {'GITHUB_SHA': sha, 'GITHUB_EVENT_NAME': 'workflow_dispatch',
               'GITHUB_REF': 'refs/heads/master', 'LEGACY_ROLLBACK_INVENTORY_SHA256': previous,
               'REFRESH_PREVIOUS_SHA256': previous,
               'PRODUCTION_APPROVED_CONFIG_SHA256_JSON': json.dumps({n: 'c' * 64 for n in runner_base.SOURCES}),
               'REFRESH_CONFIRMATION': f'REFRESH production rollback {sha} from {previous}'}
        evidence = types.ModuleType('evidence')
        evidence.api = mock.Mock(return_value={'object': {'sha': sha}})
        evidence.master_gates = mock.Mock()
        transport = types.ModuleType('transport')
        transport.configure = mock.Mock(return_value=['synthetic-ssh'])
        for change in ({'GITHUB_REF': 'refs/heads/other'}, {'REFRESH_PREVIOUS_SHA256': 'd' * 64},
                       {'REFRESH_CONFIRMATION': 'wrong'}, {'PRODUCTION_APPROVED_CONFIG_SHA256_JSON': '{}'}):
            with mock.patch.dict(sys.modules, {'evidence': evidence, 'transport': transport}), \
                 mock.patch.dict(os.environ, dict(env, **change), clear=True), \
                 mock.patch.object(runner.subprocess, 'run') as ssh:
                with self.assertRaises(runner.RefreshBlocked):
                    runner.inspect_once()
                ssh.assert_not_called()
        with mock.patch.dict(sys.modules, {'evidence': evidence, 'transport': transport}), \
             mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(runner.subprocess, 'run') as ssh:
            evidence.api.side_effect = [{'object': {'sha': sha}}, {'object': {'sha': 'd' * 40}}]
            with self.assertRaisesRegex(runner.RefreshBlocked, 'master-moved'):
                runner.inspect_once()
            ssh.assert_not_called()


if __name__ == '__main__':
    unittest.main()

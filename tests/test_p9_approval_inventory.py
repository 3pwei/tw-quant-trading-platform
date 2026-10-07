"""Isolated real-file capture and manual workflow regressions; no host access."""
import ast
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'deploy/production'))
import production_approval_inventory as inventory
import transport
import evidence as gates

SHA = 'a' * 40
ENV = {'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_REF': 'refs/heads/master', 'GITHUB_SHA': SHA}


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'production'
        self.root.mkdir(mode=0o700)
        for directory in ('config', 'provider', 'rollback'):
            (self.root / directory).mkdir(mode=0o700)
        self.contents = {}
        self.owners = {}
        for label, directory, name, uid in inventory.FILES:
            content = b'PRIVATE_SYNTHETIC_CONTENT_' + label.encode()
            if label == 'rollback.json':
                content = json.dumps({'schema_version': 1, 'revision': inventory.LEGACY_REVISION,
                                      'files': {k: 'b' * 64 for k in inventory.SEALED_FILES}}).encode()
            path = self.root / directory / name
            path.write_bytes(content)
            path.chmod(0o600)
            os.utime(path, ns=(1000000000, 1000000000))
            self.contents[label] = content
            self.owners[path.stat().st_ino] = uid
        for path in (self.root, *(self.root / d for d in ('config', 'provider', 'rollback'))):
            self.owners[path.stat().st_ino] = 0
        self.original_stat, self.original_fstat = os.stat, os.fstat
        self.original_open, self.original_read = os.open, os.read

    def virtual_owner(self, result):
        # Tests do not require root/chown. Only ownership is virtual; file bytes,
        # O_NOFOLLOW, O_NOATIME, metadata and descriptors exercise the real OS.
        data = {k: getattr(result, k) for k in dir(result) if k.startswith('st_')}
        data['st_uid'] = self.owners.get(result.st_ino, result.st_uid)
        return types.SimpleNamespace(**data)

    @contextlib.contextmanager
    def fixture(self, open_hook=None, read_hook=None, stat_hook=None):
        def opened(path, flags, **kwargs):
            self.assertEqual(flags & os.O_ACCMODE, os.O_RDONLY)
            self.assertTrue(flags & os.O_NOFOLLOW)
            self.assertTrue(flags & os.O_NOATIME)
            self.assertFalse(flags & (os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            if open_hook:
                open_hook(path, flags)
            # CI users cannot O_NOATIME unowned ancestor directories (/ and /tmp).
            # File reads retain the actual O_NOATIME flag under the fixture owner.
            actual = flags & ~os.O_NOATIME if flags & os.O_DIRECTORY else flags
            return self.original_open(path, actual, **kwargs)
        def stated(*args, **kwargs):
            if stat_hook:
                stat_hook(*args, **kwargs)
            return self.virtual_owner(self.original_stat(*args, **kwargs))
        with patch.object(os, 'geteuid', return_value=0), \
             patch.object(os, 'stat', side_effect=stated), \
             patch.object(os, 'fstat', side_effect=lambda *a, **k: self.virtual_owner(self.original_fstat(*a, **k))), \
             patch.object(os, 'open', side_effect=opened), \
             patch.object(os, 'read', side_effect=read_hook or self.original_read):
            yield

    def capture(self):
        return inventory.capture(self.root.parts[1:])

    def test_exact_six_hashes_readonly_flags_and_unchanged_files(self):
        paths = [self.root / directory / name for _, directory, name, _ in inventory.FILES]
        before = {p: (p.read_bytes(), inventory.identity(p.stat())) for p in paths}
        for p in paths:
            os.utime(p, ns=(1000000000, 1000000000))
        atime = {p: p.stat().st_atime_ns for p in paths}
        metadata = {p: inventory.identity(p.stat()) for p in paths}
        opened = []
        with self.fixture(open_hook=lambda p, f: opened.append(str(p))):
            hashes = self.capture()
        self.assertEqual(hashes, {k: hashlib.sha256(v).hexdigest() for k, v in self.contents.items()})
        self.assertEqual(opened[-6:], [name for _, _, name, _ in inventory.FILES])
        self.assertEqual(atime, {p: p.stat().st_atime_ns for p in paths})
        self.assertEqual(metadata, {p: inventory.identity(p.stat()) for p in paths})
        self.assertEqual({p: v[0] for p, v in before.items()}, {p: p.read_bytes() for p in paths})
        self.assertEqual(len(list(self.root.rglob('*'))), 9)
        self.assertNotIn('PRIVATE_SYNTHETIC_CONTENT', json.dumps(inventory.evidence(hashes)))

    def test_root_path_components_have_exact_sanitized_reasons(self):
        self.assertEqual(('/', *inventory.ROOT_PARTS), ('/', 'srv', 'trading-platform-p9'))
        self.assertEqual(inventory.ROOT_ACCESS_REASONS, (
            'production-root-slash-access',
            'production-root-srv-access',
        ))
        self.assertEqual(inventory.PLATFORM_PATH_REASONS, (
            'production-root-platform-missing',
            'production-root-platform-symlink',
            'production-root-platform-not-directory',
            'production-root-platform-permission',
            'production-root-platform-open-failed',
        ))
        cases = (
            ('/', 'production-root-slash-access'),
            (self.root.parts[1], 'production-root-srv-access'),
        )
        for blocked_path, reason in cases:
            with self.subTest(path=blocked_path, reason=reason):
                def reject(path, _flags):
                    if path == blocked_path:
                        raise OSError('PRIVATE_SYNTHETIC_CONTENT')
                with self.fixture(open_hook=reject,
                                  read_hook=Mock(side_effect=AssertionError('read before validation'))):
                    with self.assertRaisesRegex(inventory.CaptureBlocked, '^' + reason + '$'):
                        self.capture()

    def test_platform_path_missing_has_exact_sanitized_reason(self):
        moved = self.root.with_name(self.root.name + '-fixture')
        self.root.rename(moved)
        try:
            with self.fixture(read_hook=Mock(side_effect=AssertionError('read before validation'))):
                with self.assertRaisesRegex(inventory.CaptureBlocked,
                                            '^production-root-platform-missing$'):
                    self.capture()
        finally:
            moved.rename(self.root)

    def test_platform_path_symlink_has_exact_sanitized_reason(self):
        target = self.root.with_name(self.root.name + '-fixture')
        self.root.rename(target)
        self.root.symlink_to(target, target_is_directory=True)
        try:
            with self.fixture(read_hook=Mock(side_effect=AssertionError('read before validation'))):
                with self.assertRaisesRegex(inventory.CaptureBlocked,
                                            '^production-root-platform-symlink$'):
                    self.capture()
        finally:
            self.root.unlink()
            target.rename(self.root)

    def test_platform_path_regular_file_has_exact_sanitized_reason(self):
        target = self.root.with_name(self.root.name + '-fixture')
        self.root.rename(target)
        self.root.write_bytes(b'PRIVATE_SYNTHETIC_CONTENT')
        try:
            with self.fixture(read_hook=Mock(side_effect=AssertionError('read before validation'))):
                with self.assertRaisesRegex(inventory.CaptureBlocked,
                                            '^production-root-platform-not-directory$'):
                    self.capture()
        finally:
            self.root.unlink()
            target.rename(self.root)

    def test_platform_directory_permission_failure_has_exact_sanitized_reason(self):
        def reject(path, _flags):
            if path == self.root.parts[-1]:
                raise PermissionError('PRIVATE_SYNTHETIC_CONTENT')
        with self.fixture(open_hook=reject,
                          read_hook=Mock(side_effect=AssertionError('read before validation'))):
            with self.assertRaisesRegex(inventory.CaptureBlocked,
                                        '^production-root-platform-permission$'):
                self.capture()

    def test_platform_directory_generic_open_failure_has_exact_sanitized_reason(self):
        def reject(path, _flags):
            if path == self.root.parts[-1]:
                raise OSError('PRIVATE_SYNTHETIC_CONTENT')
        with self.fixture(open_hook=reject,
                          read_hook=Mock(side_effect=AssertionError('read before validation'))):
            with self.assertRaisesRegex(inventory.CaptureBlocked,
                                        '^production-root-platform-open-failed$'):
                self.capture()

    def test_valid_platform_directory_continues_existing_owner_mode_validation(self):
        self.root.chmod(0o755)
        try:
            with self.fixture(read_hook=Mock(side_effect=AssertionError('read before validation'))):
                with self.assertRaisesRegex(inventory.CaptureBlocked,
                                            '^production-root-owner-mode$'):
                    self.capture()
        finally:
            self.root.chmod(0o700)

    def test_platform_diagnostic_open_is_exactly_read_only(self):
        platform_flags = []
        def record(path, flags):
            if path == self.root.parts[-1]:
                platform_flags.append(flags)
        with self.fixture(open_hook=record):
            self.capture()
        self.assertEqual(platform_flags, [
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NOATIME,
        ])

    def test_platform_metadata_is_relative_to_parent_and_never_follows_symlinks(self):
        platform_stats = []
        def record(path, **kwargs):
            if path == self.root.parts[-1]:
                platform_stats.append(kwargs)
        with self.fixture(stat_hook=record):
            self.capture()
        self.assertGreaterEqual(len(platform_stats), 1)
        self.assertIsInstance(platform_stats[0]['dir_fd'], int)
        self.assertIs(platform_stats[0]['follow_symlinks'], False)

    def test_owner_and_mode_rejected_before_any_hash_read(self):
        path = self.root / 'config/market.env'
        for invalid in ('uid', 'mode', 'directory'):
            with self.subTest(invalid=invalid):
                original = self.owners[path.stat().st_ino]
                if invalid == 'uid':
                    self.owners[path.stat().st_ino] = 10001
                elif invalid == 'mode':
                    path.chmod(0o640)
                else:
                    self.root.chmod(0o755)
                try:
                    with self.fixture(read_hook=Mock(side_effect=AssertionError('read before validation'))):
                        with self.assertRaises(inventory.CaptureBlocked):
                            self.capture()
                finally:
                    self.owners[path.stat().st_ino] = original
                    path.chmod(0o600); self.root.chmod(0o700)

    def test_missing_symlink_fifo_rejected_before_any_hash_read(self):
        path = self.root / 'config/market.env'
        for kind in ('missing', 'symlink', 'fifo'):
            with self.subTest(kind=kind):
                path.unlink()
                if kind == 'symlink':
                    path.symlink_to(self.root / 'config/execution.env')
                elif kind == 'fifo':
                    os.mkfifo(path, 0o600)
                try:
                    with self.fixture(read_hook=Mock(side_effect=AssertionError('read before validation'))):
                        with self.assertRaises((inventory.CaptureBlocked, OSError)):
                            self.capture()
                finally:
                    if path.is_symlink() or path.exists():
                        path.unlink()
                    path.write_bytes(self.contents['market.env']); path.chmod(0o600)
                    self.owners[path.stat().st_ino] = 0

    def test_parent_symlink_rejected(self):
        path = self.root / 'provider'
        path.rename(self.root / 'staging-provider')
        path.symlink_to(self.root / 'staging-provider', target_is_directory=True)
        with self.fixture(read_hook=Mock(side_effect=AssertionError('read before validation'))):
            with self.assertRaisesRegex(inventory.CaptureBlocked, 'provider-path-access'):
                self.capture()

    def test_incomplete_sealed_inventory_rejected(self):
        document = json.loads(self.contents['rollback.json'])
        document['files'].pop('image-gateway.tar')
        (self.root / 'rollback/rollback.json').write_text(json.dumps(document))
        with self.fixture():
            with self.assertRaisesRegex(inventory.CaptureBlocked, 'rollback-inventory-structure-invalid'):
                self.capture()

    def test_duplicate_json_keys_rejected(self):
        (self.root / 'rollback/rollback.json').write_bytes(b'{"files":{},"files":{}}')
        with self.fixture():
            with self.assertRaisesRegex(inventory.CaptureBlocked, 'rollback-duplicate-key'):
                self.capture()

    def test_mutation_during_read_emits_no_approvals(self):
        mutated = False
        def read(fd, size):
            nonlocal mutated
            result = self.original_read(fd, size)
            if not mutated:
                mutated = True
                (self.root / 'config/market.env').write_bytes(b'changed synthetic fixture')
            return result
        with self.fixture(read_hook=read):
            with self.assertRaisesRegex(inventory.CaptureBlocked, 'changed-during-capture'):
                self.capture()

    def test_hardlink_staging_reuse_and_replacement_rejected(self):
        path = self.root / 'config/market.env'
        os.link(path, self.root / 'staging.env')
        with self.fixture(read_hook=Mock(side_effect=AssertionError('read before validation'))):
            with self.assertRaisesRegex(inventory.CaptureBlocked, 'shared-file'):
                self.capture()
        (self.root / 'staging.env').unlink()
        replaced = False
        def read(fd, size):
            nonlocal replaced
            result = self.original_read(fd, size)
            if not replaced:
                replaced = True
                path.rename(self.root / 'old.env')
                path.write_bytes(self.contents['market.env']); path.chmod(0o600)
            return result
        with self.fixture(read_hook=read):
            with self.assertRaisesRegex(inventory.CaptureBlocked, 'changed-during-capture'):
                self.capture()

    def test_invalid_json_schema_and_hashes_rejected(self):
        document = json.loads(self.contents['rollback.json'])
        variants = [b'not json', b'[]', json.dumps({**document, 'schema_version': True}).encode(),
                    json.dumps({**document, 'revision': 'staging'}).encode(),
                    json.dumps({**document, 'files': {k: '../secret' for k in inventory.SEALED_FILES}}).encode()]
        for data in variants:
            with self.subTest(data=data):
                (self.root / 'rollback/rollback.json').write_bytes(data)
                with self.fixture():
                    with self.assertRaises(inventory.CaptureBlocked):
                        self.capture()


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = (ROOT / '.github/workflows/production-approval-inventory.yml').read_text()
        self.safe = inventory.evidence({label: 'b' * 64 for label, *_ in inventory.FILES})

    def test_manual_master_environment_permissions_and_shared_concurrency(self):
        events = self.workflow.split('\non:\n')[1].split('\npermissions:')[0]
        self.assertEqual(re.findall(r'^  (\w+):', events, re.M), ['workflow_dispatch'])
        self.assertIn("github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/master'", self.workflow)
        self.assertIn('environment: lightsail-production', self.workflow)
        self.assertIn('group: lightsail-production\n  cancel-in-progress: false', self.workflow)
        self.assertIn('permissions:\n  actions: read\n  contents: read', self.workflow)
        for action in re.findall(r'uses: (\S+)', self.workflow):
            self.assertRegex(action, r'^[\w./-]+@[0-9a-f]{40}$')
        for unsafe in ('workflow_run', 'pull_request', 'schedule:', 'continue-on-error', 'write', 'upload-artifact'):
            self.assertNotIn(unsafe, self.workflow)

    def test_exact_checkout_verifier_precedes_secrets_and_only_runner_command(self):
        self.assertIn('ref: ${{ github.sha }}', self.workflow)
        self.assertIn('persist-credentials: false', self.workflow)
        self.assertLess(self.workflow.index('python tools/verify_public_candidate.py'), self.workflow.index('secrets.'))
        self.assertEqual(re.findall(r'run: (.+)', self.workflow), [
            'python tools/verify_public_candidate.py',
            'python -B deploy/production/production_approval_inventory.py --runner'])
        for name in ('PRODUCTION_HOST', 'PRODUCTION_USER', 'STAGING_HOST_IDENTITY'):
            self.assertIn(name + ': ${{ vars.' + name + ' }}', self.workflow)
        for name in ('PRODUCTION_SSH_PRIVATE_KEY', 'PRODUCTION_SSH_HOST_KEY'):
            self.assertIn(name + ': ${{ secrets.' + name + ' }}', self.workflow)
        for name in ('PRODUCTION_APPROVED_CONFIG_SHA256_JSON', 'LEGACY_ROLLBACK_INVENTORY_SHA256'):
            self.assertNotIn(name, self.workflow)

    def test_exact_paths_and_remote_command_exclude_writes_or_lifecycle(self):
        self.assertEqual(inventory.ROOT_PARTS, ('srv', 'trading-platform-p9'))
        self.assertEqual([(d, n, uid) for _, d, n, uid in inventory.FILES], [
            ('config', 'market.env', 0), ('config', 'execution.env', 0), ('config', 'gateway.env', 0),
            ('provider', 'factory', 10001), ('config', 'replay.csv', 10001), ('rollback', 'rollback.json', 0)])
        self.assertEqual(inventory.REMOTE_COMMAND, 'sudo -n env PYTHONDONTWRITEBYTECODE=1 python3 -B -')
        source = Path(inventory.__file__).read_text()
        self.assertNotIn('docker', source.lower())
        self.assertNotIn('scp', source)
        tree = ast.parse(source)
        dangerous = {'mkdir', 'makedirs', 'chmod', 'chown', 'write_text', 'write_bytes', 'unlink', 'rename', 'system', 'popen'}
        self.assertFalse(any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and
                             n.func.attr in dangerous for n in ast.walk(tree)))

    def test_one_ssh_stdin_reviewed_script_and_sanitized_output(self):
        response = types.SimpleNamespace(returncode=0, stdout=json.dumps(self.safe).encode())
        with patch.dict(os.environ, ENV, clear=True), \
             patch.object(gates, 'api', return_value={'object': {'sha': SHA}}) as api, \
             patch.object(gates, 'master_gates') as master, \
             patch.object(transport, 'configure', return_value=['ssh', 'fixture@production']) as configure, \
             patch.object(subprocess, 'run', return_value=response) as run:
            self.assertEqual(inventory.inspect_once(), self.safe)
        configure.assert_called_once(); master.assert_called_once_with(SHA)
        self.assertEqual(api.call_count, 2)
        run.assert_called_once_with(['ssh', 'fixture@production', inventory.REMOTE_COMMAND],
                                   input=Path(inventory.__file__).read_bytes(), stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, timeout=120, check=False)

    def test_nonmaster_nonmanual_or_moved_master_cannot_connect(self):
        cases = [({**ENV, 'GITHUB_EVENT_NAME': 'push'}, SHA),
                 ({**ENV, 'GITHUB_REF': 'refs/heads/staging'}, SHA),
                 (ENV, 'c' * 40), ({**ENV, 'GITHUB_SHA': 'invalid'}, SHA)]
        for env, sha in cases:
            with self.subTest(env=env), patch.dict(os.environ, env, clear=True), \
                 patch.object(gates, 'api', return_value={'object': {'sha': sha}}), \
                 patch.object(gates, 'master_gates'), patch.object(transport, 'configure') as configure, \
                 patch.object(subprocess, 'run') as run:
                with self.assertRaises(inventory.CaptureBlocked):
                    inventory.inspect_once()
                configure.assert_not_called(); run.assert_not_called()

    def test_failed_master_gates_or_move_before_ssh_cannot_inspect(self):
        for failed_gate in (True, False):
            with self.subTest(failed_gate=failed_gate), patch.dict(os.environ, ENV, clear=True), \
                 patch.object(gates, 'api', side_effect=[{'object': {'sha': SHA}}, {'object': {'sha': 'c' * 40}}]), \
                 patch.object(gates, 'master_gates', side_effect=ValueError('gate') if failed_gate else None), \
                 patch.object(transport, 'configure', return_value=['ssh']) as configure, \
                 patch.object(subprocess, 'run') as run:
                with self.assertRaises((ValueError, inventory.CaptureBlocked)):
                    inventory.inspect_once()
                run.assert_not_called()
                if failed_gate:
                    configure.assert_not_called()

    def test_malformed_secret_or_partial_host_evidence_never_logged(self):
        unsafe = [b'PRIVATE_SYNTHETIC_CONTENT', b'{"credentials":"PRIVATE_SYNTHETIC_CONTENT"}',
                  json.dumps({**self.safe, 'secret': 'PRIVATE_SYNTHETIC_CONTENT'}).encode(),
                  json.dumps({**self.safe, 'market.env SHA256': 'PRIVATE_SYNTHETIC_CONTENT'}).encode(),
                  json.dumps({k: v for k, v in self.safe.items() if k != 'factory SHA256'}).encode(),
                  json.dumps({**self.safe, inventory.CHECKS[0]: 'FAIL'}).encode(),
                  b'x' * 4097, b'{"factory SHA256":"' + b'b' * 64 + b'","factory SHA256":"secret"}']
        for output in unsafe:
            with self.subTest(output=output[:32]), patch.dict(os.environ, ENV, clear=True), \
                 patch.object(gates, 'api', return_value={'object': {'sha': SHA}}), \
                 patch.object(gates, 'master_gates'), patch.object(transport, 'configure', return_value=['ssh']), \
                patch.object(subprocess, 'run', return_value=types.SimpleNamespace(returncode=0, stdout=output)), \
                 patch.object(sys, 'argv', ['inventory', '--runner']), contextlib.redirect_stdout(io.StringIO()) as log:
                self.assertEqual(inventory.main(), 1)
                self.assertEqual(json.loads(log.getvalue()), inventory.failure(
                    'unsafe-host-evidence', inventory.RUNNER_REASONS))

    def test_ssh_failure_timeout_and_path_override_emit_only_fixed_reasons(self):
        for error in (subprocess.TimeoutExpired('synthetic secret', 120), OSError('synthetic secret')):
            with patch.dict(os.environ, ENV, clear=True), \
                 patch.object(gates, 'api', return_value={'object': {'sha': SHA}}), \
                 patch.object(gates, 'master_gates'), patch.object(transport, 'configure', return_value=['ssh']), \
                 patch.object(subprocess, 'run', side_effect=error), \
                 patch.object(sys, 'argv', ['inventory', '--runner']), contextlib.redirect_stdout(io.StringIO()) as log:
                self.assertEqual(inventory.main(), 1)
                self.assertEqual(json.loads(log.getvalue()), inventory.failure(
                    'ssh-inspection-failed', inventory.RUNNER_REASONS))
        with patch.object(sys, 'argv', ['inventory', '/srv/staging']), patch.object(inventory, 'capture') as capture, \
             contextlib.redirect_stdout(io.StringIO()) as log:
            self.assertEqual(inventory.main(), 1)
            capture.assert_not_called()
            self.assertNotIn('/srv/staging', log.getvalue())
            self.assertEqual(json.loads(log.getvalue())['reason'], 'no-path-overrides-permitted')

    def test_nonzero_ssh_cannot_publish_even_valid_success_payload(self):
        with patch.dict(os.environ, ENV, clear=True), \
             patch.object(gates, 'api', return_value={'object': {'sha': SHA}}), \
             patch.object(gates, 'master_gates'), patch.object(transport, 'configure', return_value=['ssh']), \
             patch.object(subprocess, 'run', return_value=types.SimpleNamespace(
                 returncode=1, stdout=json.dumps(self.safe).encode())):
            with self.assertRaisesRegex(inventory.CaptureBlocked, 'unsafe-host-evidence'):
                inventory.inspect_once()

    def test_exit_one_accepts_only_complete_allowlisted_failure_without_hashes(self):
        remote = inventory.failure('market.env-owner-mode', inventory.REMOTE_REASONS)
        with patch.dict(os.environ, ENV, clear=True), \
             patch.object(gates, 'api', return_value={'object': {'sha': SHA}}), \
             patch.object(gates, 'master_gates'), patch.object(transport, 'configure', return_value=['ssh']), \
             patch.object(subprocess, 'run', return_value=types.SimpleNamespace(
                 returncode=1, stdout=json.dumps(remote).encode())):
            self.assertEqual(inventory.inspect_once(), remote)
        self.assertFalse(any('SHA256' in key for key in remote))

    def test_every_failure_class_maps_to_reviewed_literal_and_no_partial_hash(self):
        secret = 'PRIVATE_SYNTHETIC_CONTENT arbitrary exception /unapproved/path'
        for runner, reasons in ((False, inventory.REMOTE_REASONS), (True, inventory.RUNNER_REASONS)):
            for reason in reasons:
                with self.subTest(runner=runner, reason=reason), \
                     patch.object(sys, 'argv', ['inventory', '--runner'] if runner else ['-']), \
                     patch.object(inventory, 'inspect_once' if runner else 'capture',
                                  side_effect=inventory.CaptureBlocked(reason)), \
                     contextlib.redirect_stdout(io.StringIO()) as log:
                    self.assertEqual(inventory.main(), 1)
                    document = json.loads(log.getvalue())
                    self.assertEqual(document, inventory.failure(reason, reasons))
                    self.assertFalse(any('SHA256' in key for key in document))
                    self.assertNotIn(secret, log.getvalue())

    def test_arbitrary_exception_text_and_unknown_reason_cannot_reach_output(self):
        secret = 'PRIVATE_SYNTHETIC_CONTENT /srv/private-staging/account-token'
        cases = ((False, RuntimeError(secret), 'capture-failed'),
                 (False, inventory.CaptureBlocked(secret), 'capture-failed'),
                 (True, RuntimeError(secret), 'unsafe-host-evidence'),
                 (True, inventory.CaptureBlocked(secret), 'unsafe-host-evidence'))
        for runner, error, expected in cases:
            with self.subTest(runner=runner, error=type(error).__name__), \
                 patch.object(sys, 'argv', ['inventory', '--runner'] if runner else ['-']), \
                 patch.object(inventory, 'inspect_once' if runner else 'capture', side_effect=error), \
                 contextlib.redirect_stdout(io.StringIO()) as log:
                self.assertEqual(inventory.main(), 1)
                self.assertEqual(json.loads(log.getvalue())['reason'], expected)
                self.assertNotIn(secret, log.getvalue())

    def test_runner_reason_classes_are_separate_and_fail_before_remote_execution(self):
        cases = [
            ('control-gate-failed', {**ENV, 'GITHUB_EVENT_NAME': 'push'}, None, None),
            ('master-moved', ENV, [{'object': {'sha': 'c' * 40}}], None),
            ('control-gate-failed', ENV, [RuntimeError('PRIVATE_SYNTHETIC_CONTENT')], None),
            ('ssh-config-unavailable', ENV, [{'object': {'sha': SHA}}], RuntimeError('PRIVATE_SYNTHETIC_CONTENT')),
        ]
        for reason, env, api_effects, configure_error in cases:
            with self.subTest(reason=reason), patch.dict(os.environ, env, clear=True), \
                 patch.object(gates, 'api', side_effect=api_effects) if api_effects is not None else contextlib.nullcontext(), \
                 patch.object(gates, 'master_gates'), \
                 patch.object(transport, 'configure', side_effect=configure_error) as configure, \
                 patch.object(subprocess, 'run') as run:
                with self.assertRaisesRegex(inventory.CaptureBlocked, '^' + reason + '$'):
                    inventory.inspect_once()
                run.assert_not_called()

    def test_pass_schema_is_byte_for_byte_unchanged(self):
        hashes = {label: 'b' * 64 for label, *_ in inventory.FILES}
        expected = {**{label + ' SHA256': 'b' * 64 for label, *_ in inventory.FILES},
                    'path/ownership checks': 'PASS', 'rollback inventory structure': 'PASS'}
        self.assertEqual(inventory.evidence(hashes), expected)
        self.assertEqual(inventory.safe_evidence(expected, expected='pass'), expected)

    def test_remote_entrypoint_outputs_only_complete_sanitized_evidence(self):
        hashes = {label: 'b' * 64 for label, *_ in inventory.FILES}
        with patch.object(sys, 'argv', ['-']), patch.object(inventory, 'capture', return_value=hashes) as capture, \
             patch.object(inventory, 'inspect_once') as runner, contextlib.redirect_stdout(io.StringIO()) as log:
            self.assertEqual(inventory.main(), 0)
            self.assertEqual(json.loads(log.getvalue()), self.safe)
            capture.assert_called_once_with(); runner.assert_not_called()

    def test_existing_transport_rejects_staging_alias_before_key_material(self):
        base = {'PRODUCTION_HOST': 'production.example', 'PRODUCTION_USER': 'ubuntu',
                'STAGING_HOST_IDENTITY': 'staging.example'}
        for same_name in (True, False):
            env = {**base, 'STAGING_HOST_IDENTITY': 'production.example'} if same_name else base
            with patch.dict(os.environ, env, clear=True), \
                 patch.object(transport.socket, 'getaddrinfo', return_value=[(0, 0, 0, '', ('192.0.2.1', 0))]), \
                 patch.object(Path, 'mkdir') as mkdir:
                with self.assertRaisesRegex(ValueError, 'production-host-not-isolated|production-staging-host-overlap'):
                    transport.configure()
                mkdir.assert_not_called()


if __name__ == '__main__':
    unittest.main()

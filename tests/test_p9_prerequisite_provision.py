import ast
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
HOST = ROOT / 'deploy/production/provision_host.py'
RUNNER = ROOT / 'deploy/production/provision_prerequisites.py'
WORKFLOW = ROOT / '.github/workflows/production-prerequisite-provision.yml'


def load_runner():
    spec = importlib.util.spec_from_file_location('provision_runner_test', RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ProvisionProtocolTests(unittest.TestCase):
    def setUp(self):
        self.runner = load_runner()

    def response(self, document, code=1):
        return types.SimpleNamespace(returncode=code,
                                     stdout=json.dumps(document).encode())

    def test_all_allowed_failures_survive_runner_and_artifact(self):
        for reason, stage in self.runner.HOST_REASONS.items():
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as temp:
                document = {'status': 'BLOCKED', 'stage': stage, 'reason': reason}
                def inspect():
                    return self.runner.host_response(self.response(document))
                with mock.patch.object(self.runner, 'inspect_once', side_effect=inspect), \
                     mock.patch.dict('os.environ', {'RUNNER_TEMP': temp}), \
                     mock.patch.object(sys, 'argv', ['runner', '--runner']), \
                     contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(self.runner.main(), 1)
                self.assertEqual(json.loads(output.getvalue()), document)
                artifact = Path(temp) / 'production-prerequisite-provision.json'
                self.assertEqual(json.loads(artifact.read_text()), document)
                self.assertEqual(artifact.stat().st_mode & 0o777, 0o600)

    def test_untrusted_or_inconsistent_documents_are_rejected(self):
        valid = {'status': 'BLOCKED', 'stage': 'snapshot',
                 'reason': 'sqlite-snapshot-failed'}
        cases = [
            (dict(valid, reason='sensitive fixture value'), 1),
            (dict(valid, stage='sensitive fixture value'), 1),
            (dict(valid, traceback='sensitive fixture value'), 1),
            (dict(valid, reason=['sqlite-snapshot-failed']), 1),
            (valid, 0), (valid, 255), ([], 1), ({'status': 'PASS'}, 1),
        ]
        for document, code in cases:
            with self.subTest(document=document, code=code):
                with self.assertRaises(self.runner.ProvisionBlocked) as exc:
                    self.runner.host_response(self.response(document, code))
                self.assertNotIn('sensitive fixture value', str(exc.exception))
                self.assertIn(str(exc.exception),
                              {'unsafe-host-evidence', 'ssh-provision-failed'})

    def test_invalid_json_duplicate_keys_and_transport_failures(self):
        for code, raw in [(1, b''), (255, b'host error'), (1, b'not json'),
                          (1, b'x' * 8193),
                          (1, b'{"status":"PASS","status":"BLOCKED"}')]:
            with self.subTest(code=code, raw=raw[:60]):
                with self.assertRaises(self.runner.ProvisionBlocked):
                    self.runner.host_response(types.SimpleNamespace(
                        returncode=code, stdout=raw))

    def test_success_contract_is_preserved(self):
        document = {
            'status': 'PASS', 'root': '/srv/trading-platform-production',
            'legacy_revision': 'a' * 40, 'market_provider': 'shioaji',
            'config_sha256': {key: 'b' * 64 for key in self.runner.SOURCES},
            'rollback_sha256': 'c' * 64, 'execution_locked': True,
            'external_order_calls': 0, 'external_cancel_calls': 0,
        }
        self.assertEqual(self.runner.host_response(self.response(document, 0)), document)

    def test_generated_ssh_program_sanitizes_real_process_output(self):
        # Stub only the host entry point: no SSH, Production paths, or containers.
        for error, expected in [
            ("ProvisionBlocked('production-root-exists')", 'production-root-exists'),
            ("ProvisionBlocked('sqlite-snapshot-failed')", 'sqlite-snapshot-failed'),
            ("ProvisionBlocked('sensitive fixture value')", 'host-provision-failed'),
            ("RuntimeError('production-root-exists')", 'host-provision-failed'),
            ("RuntimeError('sensitive fixture value')", 'host-provision-failed'),
        ]:
            source = ("class ProvisionBlocked(Exception): pass\n"
                      "def no_duplicate_keys(pairs): return dict(pairs)\n"
                      f"def provision(payload): raise {error}\n")
            original = self.runner.module
            def stub(name, unused):
                return original(name, source.encode() if name == 'provision_host' else b'')
            with mock.patch.object(self.runner, 'module', side_effect=stub):
                program = self.runner.remote_program({})
            result = subprocess.run([sys.executable, '-B', '-'], input=program,
                                    capture_output=True, check=False, timeout=10)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stderr, b'')
            self.assertNotIn(b'sensitive fixture value', result.stdout)
            self.assertEqual(json.loads(result.stdout), {
                'status': 'BLOCKED', 'reason': expected,
                'stage': self.runner.HOST_REASONS[expected],
            })


class ProductionPrerequisiteProvisionTests(unittest.TestCase):
    def setUp(self):
        self.host = HOST.read_text()
        self.runner = RUNNER.read_text()
        self.workflow = WORKFLOW.read_text()

    def test_long_lived_root_and_existing_root_refusal(self):
        self.assertIn("ROOT = Path('/srv/trading-platform-production')", self.host)
        self.assertIn("'production-root-exists'", self.host)
        self.assertNotIn('/srv/trading-platform-p9', self.host + self.runner + self.workflow)

    def test_workflow_is_manual_master_environment_only(self):
        self.assertIn('workflow_dispatch:', self.workflow)
        self.assertNotIn('\npush:', self.workflow)
        self.assertNotIn('\npull_request:', self.workflow)
        self.assertIn("github.ref == 'refs/heads/master'", self.workflow)
        self.assertIn('environment: lightsail-production', self.workflow)
        self.assertIn('PRODUCTION_PROVISION_CONFIRMATION', self.workflow)

    def test_production_sources_are_explicit_environment_secrets(self):
        for name in (
            'PRODUCTION_MARKET_ENV_B64',
            'PRODUCTION_EXECUTION_ENV_B64',
            'PRODUCTION_GATEWAY_ENV_B64',
            'PRODUCTION_PROVIDER_FACTORY_B64',
            'PRODUCTION_REPLAY_CSV_B64',
        ):
            self.assertIn('secrets.' + name, self.workflow)
            self.assertIn(name, self.runner)
        self.assertNotIn('deploy/lightsail/market.env.example', self.runner + self.host)

    def test_no_legacy_lifecycle_mutation_commands(self):
        source = self.host + self.runner
        for token in (
            "docker', 'stop", "docker', 'start", "docker', 'restart",
            "docker', 'pull", "docker', 'build", 'docker compose up',
            'git reset', 'git checkout',
        ):
            self.assertNotIn(token, source)

    def test_execution_lock_and_zero_call_requirements_are_explicit(self):
        self.assertIn('p9_validation.worker_locked(', self.host)
        self.assertIn('legacy=True', self.host)
        for fragment in (
            "'external_order_calls': health['external_order_calls']",
            "'external_cancel_calls': health['external_cancel_calls']",
        ):
            self.assertIn(fragment, self.host)

    def test_staging_reuse_remains_rejected_by_reviewed_config_check(self):
        self.assertIn('p9_validation.config_check(pins, config_hashes)', self.host)
        self.assertIn("MARKET_DATA_PROVIDER'] ==", self.host)
        self.assertNotIn('STAGING_HOST', self.host)

    def test_rollback_inventory_is_complete(self):
        for name in (
            'data.sqlite3', 'config.tar', 'image-market-api.tar',
            'image-execution-worker.tar', 'image-gateway.tar',
        ):
            self.assertIn(name, self.host)
        self.assertIn('p9_validation.verify_backup(pins, rollback_sha)', self.host)

    def test_sqlite_snapshot_is_ram_reconstructed_and_locked(self):
        self.assertIn('readonly_sqlite.committed_image', self.host)
        self.assertIn('readonly_sqlite.inventory', self.host)
        self.assertIn("inventory.get('active_targets') == 0", self.host)
        self.assertIn("readonly_sqlite.snapshot(rollback / 'data.sqlite3')", self.host)

    def test_atomic_publish_is_noreplace(self):
        self.assertIn('RENAME_NOREPLACE = 1', self.host)
        self.assertIn("getattr(libc, 'renameat2', None)", self.host)
        self.assertIn('publish_noreplace(temp, ROOT)', self.host)

    def test_failure_cleanup_cannot_publish_partial_final_root(self):
        tree = ast.parse(self.host)
        attrs = {n.func.attr for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        self.assertIn('rmtree', attrs)
        self.assertIn("if temp is not None and temp.exists():\n                shutil.rmtree(temp)",
                      self.host)

    def test_runner_never_logs_source_secret_values(self):
        self.assertNotIn('print(os.environ', self.runner)
        self.assertNotIn('print(payload', self.runner)
        self.assertIn('stderr=subprocess.DEVNULL', self.runner)
        self.assertIn("'unsafe-host-evidence'", self.runner)

    def test_confirmation_is_exact_master_sha(self):
        self.assertIn("'PROVISION production prerequisites ' + sha", self.runner)
        self.assertIn("api('git/ref/heads/master')['object']['sha'] == sha", self.runner)

    def test_scripts_compile(self):
        compile(self.host, str(HOST), 'exec')
        compile(self.runner, str(RUNNER), 'exec')


if __name__ == '__main__':
    unittest.main()

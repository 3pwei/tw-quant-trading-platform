import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
HOST = ROOT / 'deploy/production/provision_host.py'
RUNNER = ROOT / 'deploy/production/provision_prerequisites.py'
WORKFLOW = ROOT / '.github/workflows/production-prerequisite-provision.yml'


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
        for fragment in (
            "'locked': True", "'enabled': False", "'ordering_enabled': False",
            "'external_order_calls': 0", "'external_cancel_calls': 0",
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

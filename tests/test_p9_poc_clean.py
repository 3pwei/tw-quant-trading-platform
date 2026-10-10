"""P9 PoC clean-deploy regressions; no Production credentials or host access."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'deploy/production'))
import poc_clean  # noqa: E402
import poc_clean_transport  # noqa: E402


PINS = json.loads((ROOT / 'deploy/production/approved-p8.json').read_text())


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def config_files(root):
    (root / 'config').mkdir()
    (root / 'provider').mkdir()
    market = {
        'MARKET_DATA_PROVIDER': 'shioaji',
        'MARKET_SJ_API_KEY': 'test-market-key',
        'MARKET_SJ_SECRET_KEY': 'test-market-secret',
        'MARKET_SJ_PRODUCTION': 'true',
        'CF_ACCESS_TEAM_DOMAIN': 'team.cloudflareaccess.com',
        'CF_ACCESS_AUD': 'test-audience',
        'PLATFORM_BOOTSTRAP_ADMIN_EMAILS': 'owner@example.com',
        'PLATFORM_ENVIRONMENT': 'production',
        'PLATFORM_AUTHORIZATION_MODE': 'enforced',
        'MARKET_ACCESS_MODE': 'cloudflare',
        'MARKET_DB_PATH': '/data/platform.sqlite3',
        'PRIVATE_PROVIDER_WHEEL_SHA256': PINS['manifest']['private_provider']['wheel_sha256'],
    }
    execution = {
        'BROKER_PROVIDER': 'disabled',
        'LIVE_TRADING_ENABLED': 'false',
        'LIVE_EXECUTION_DB_PATH': '/data/platform.sqlite3',
        'LIVE_EXECUTION_HEALTH_PATH': '/run/tw-quant-execution/health.json',
    }
    for name, values in (('market.env', market), ('execution.env', execution),
                         ('gateway.env', {'MARKET_DOMAIN': 'production.example.invalid'})):
        (root / 'config' / name).write_text(
            ''.join(key + '=' + value + '\n' for key, value in values.items())
        )
        os.chmod(root / 'config' / name, 0o600)
    (root / 'provider/factory').write_text('private_runtime:create_app\n')
    os.chmod(root / 'provider/factory', 0o600)
    hashes = {name: digest(root / ('provider/factory' if name == 'factory'
                                   else 'config/' + name))
              for name in poc_clean.REQUIRED_HASHES}
    return market, execution, hashes


class CleanConfigTests(unittest.TestCase):
    def checked(self, root, hashes):
        def protected(path, expected=None, uid=0):
            self.assertTrue(path.is_file())
            if expected:
                self.assertEqual(digest(path), expected)
        with patch.object(poc_clean, 'ROOT', root), \
             patch.object(poc_clean.common, 'protected', side_effect=protected):
            return poc_clean.clean_config_check(PINS, hashes)

    def test_shioaji_config_requires_admin_auth_and_market_only_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, _, hashes = config_files(root)
            envs = self.checked(root, hashes)
            self.assertEqual(envs['market.env']['MARKET_DATA_PROVIDER'], 'shioaji')
            self.assertNotIn('MARKET_REPLAY_CSV', envs['market.env'])

            cases = {
                'clean-required-setting-missing': ('market.env', 'PLATFORM_BOOTSTRAP_ADMIN_EMAILS', ''),
                'clean-shioaji-replay-not-required': ('market.env', 'MARKET_REPLAY_CSV', '/tmp/fake.csv'),
                'market-credential-isolation': ('execution.env', 'MARKET_SJ_API_KEY', 'leak'),
                'staging-config-reuse': ('gateway.env', 'UNRELATED', '/srv/staging/config'),
            }
            for reason, (name, key, value) in cases.items():
                path = root / 'config' / name
                original = path.read_text()
                lines = [line for line in original.splitlines()
                         if not line.startswith(key + '=')]
                lines.append(key + '=' + value)
                path.write_text('\n'.join(lines) + '\n')
                changed = dict(hashes, **{name: digest(path)})
                with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                    self.checked(root, changed)
                path.write_text(original)

    def test_clean_hash_contract_has_no_replay(self):
        hashes = {name: 'a' * 64 for name in poc_clean.REQUIRED_HASHES}
        with patch.dict(os.environ, {
            'PRODUCTION_POC_APPROVED_CONFIG_SHA256_JSON': json.dumps(hashes),
            'LEGACY_ROLLBACK_INVENTORY_SHA256': 'b' * 64,
        }, clear=True):
            command = poc_clean_transport.command('preflight')
        self.assertIn('--config-hashes', command)
        self.assertNotIn('replay.csv', command)


class FreshDatabaseTests(unittest.TestCase):
    def test_sqlite_backup_is_integrity_checked_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'legacy.sqlite3'
            target = Path(temporary) / 'backup.sqlite3'
            connection = sqlite3.connect(source)
            connection.execute('CREATE TABLE marker(value TEXT)')
            connection.execute("INSERT INTO marker VALUES ('legacy')")
            connection.commit()
            connection.close()
            poc_clean.sqlite_backup(source, target)
            copied = sqlite3.connect(target)
            self.assertEqual(copied.execute('SELECT value FROM marker').fetchone(), ('legacy',))
            copied.close()
            with self.assertRaisesRegex(ValueError, 'legacy-backup-collision'):
                poc_clean.sqlite_backup(source, target)


class FailureRecoveryTests(unittest.TestCase):
    def test_preflight_refuses_initialized_or_partial_environment(self):
        host = poc_clean.CleanHost(PINS, 'a' * 64, {}, b'', {
            'platform_sha': PINS['platform_sha'], 'control_sha': PINS['platform_sha'],
            'master_gates': {PINS['platform_sha']: {'CI': 1, 'Security': 2}},
        })
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ('config', 'provider', 'rollback', 'data'):
                (root / name).mkdir(mode=0o700)
            with patch.object(poc_clean, 'ROOT', root), \
                 patch.object(poc_clean, 'LEGACY', Path('/opt/tw-quant')), \
                 patch.object(poc_clean.os, 'geteuid', return_value=0), \
                 self.assertRaisesRegex(ValueError, 'clean-environment-already-initialized'):
                host.preflight()

    def test_recovery_starts_exact_legacy_ids_and_retains_partial_database(self):
        legacy = {service: {'id': str(index) * 64}
                  for index, service in enumerate(poc_clean.SERVICES, 1)}
        calls = []

        def run(argv, *args, **kwargs):
            calls.append(argv)
            if argv[:3] == ['docker', 'ps', '-aq']:
                return ('9' * 64 + '\n').encode()
            return b''

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'data').mkdir()
            with patch.object(poc_clean, 'ROOT', root), \
                 patch.object(poc_clean.common, 'run', side_effect=run), \
                 patch.object(poc_clean.common, 'legacy_state', return_value=legacy), \
                 patch.object(poc_clean.common, 'healthy'):
                poc_clean.recover(PINS, {})
            self.assertEqual([call for call in calls if call[:2] == ['docker', 'start']],
                             [['docker', 'start', legacy[service]['id']]
                              for service in poc_clean.SERVICES])
            self.assertTrue((root / 'data').exists())
            self.assertTrue((root / 'rollback-clean-result.json').exists())


class SourceBoundaryTests(unittest.TestCase):
    def test_clean_entry_is_separate_and_runtime_never_copies_legacy_db(self):
        source = (ROOT / 'deploy/production/poc_clean.py').read_text()
        compose = (ROOT / 'deploy/production/docker-compose.poc-clean.yml').read_text()
        self.assertIn("'market.sqlite3', 'execution.sqlite3'", source)
        self.assertIn("path.exists()", source)
        self.assertNotIn("rollback/data.sqlite3', ROOT / 'data/platform.sqlite3", source)
        self.assertNotIn('replay.csv', compose)

    def test_workflow_is_manual_exact_p8_and_has_failure_recovery(self):
        workflow = (ROOT / '.github/workflows/deploy-production-poc-clean.yml').read_text()
        self.assertIn('workflow_dispatch:', workflow)
        self.assertNotIn('push:', workflow)
        self.assertIn('git merge-base --is-ancestor "$P9_SOURCE" "$GITHUB_SHA"', workflow)
        self.assertIn(":(exclude)deploy/production/approved-p8.json", workflow)
        self.assertIn('PRODUCTION_POC_APPROVED_CONFIG_SHA256_JSON', workflow)
        self.assertIn('poc_clean_transport.py recover', workflow)
        self.assertNotIn('docker build', workflow)


if __name__ == '__main__':
    unittest.main()

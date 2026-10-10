"""P9 PoC clean-deploy regressions; no Production credentials or host access."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'deploy/production'))
import poc_clean  # noqa: E402
import poc_clean_prepare  # noqa: E402
import poc_clean_prepare_host  # noqa: E402
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
        }, clear=True):
            command = poc_clean_transport.command('preflight')
        self.assertIn('--config-hashes', command)
        self.assertNotIn('replay.csv', command)
        self.assertNotIn('rollback', command)


class FirstPrepareTests(unittest.TestCase):
    def test_missing_root_is_created_with_exactly_four_validated_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            source = workspace / 'source'
            source.mkdir()
            _, _, hashes = config_files(source)
            target = workspace / 'host/trading-platform-production'
            target.parent.mkdir()
            lock = workspace / 'deploy.lock'
            lock.write_bytes(b'')
            sources = {
                name: base64.b64encode(
                    (source / ('provider/factory' if name == 'factory'
                               else 'config/' + name)).read_bytes()).decode()
                for name in poc_clean.REQUIRED_HASHES
            }
            payload = {'control_sha': 'a' * 40, 'pins': PINS, 'sources': sources}
            def protected(path, expected=None, uid=0):
                self.assertTrue(path.is_file())
                if expected:
                    self.assertEqual(digest(path), expected)

            with patch.object(poc_clean_prepare_host, 'ROOT', target), \
                 patch.object(poc_clean_prepare_host, 'PARENT', target.parent), \
                 patch.object(poc_clean_prepare_host, 'LOCK', lock), \
                 patch.object(poc_clean_prepare_host.os, 'fchown'), \
                 patch.object(poc_clean.common, 'protected', side_effect=protected):
                result = poc_clean_prepare_host.prepare(payload)
                self.assertEqual(result['config_sha256'], hashes)
                self.assertEqual(sorted(path.relative_to(target).as_posix()
                                        for path in target.rglob('*') if path.is_file()), [
                    'config/execution.env', 'config/gateway.env', 'config/market.env',
                    'provider/factory',
                ])
                self.assertFalse((target / 'rollback').exists())
                with self.assertRaisesRegex(
                        poc_clean_prepare_host.PrepareBlocked, 'production-root-exists'):
                    poc_clean_prepare_host.prepare(payload)

    def test_prepare_requires_all_four_sources(self):
        with self.assertRaisesRegex(
                poc_clean_prepare_host.PrepareBlocked, 'payload-invalid'):
            poc_clean_prepare_host.decode_sources({'market.env': 'YQ=='})

    def test_prepare_evidence_is_hash_only_and_rejects_extra_fields(self):
        document = {
            'status': 'PASS',
            'root': '/srv/trading-platform-production',
            'config_sha256': {name: 'a' * 64 for name in poc_clean.REQUIRED_HASHES},
            'market_provider': 'shioaji',
            'real_order': 'disabled',
        }
        self.assertEqual(poc_clean_prepare.safe_evidence(document), document)
        with self.assertRaisesRegex(poc_clean_prepare.PrepareBlocked,
                                    'unsafe-host-evidence'):
            poc_clean_prepare.safe_evidence({**document, 'admin_email': 'private@example.com'})

    def test_missing_required_setting_is_named_and_root_is_not_published(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            source = workspace / 'source'
            source.mkdir()
            config_files(source)
            market = source / 'config/market.env'
            market.write_text('\n'.join(
                line for line in market.read_text().splitlines()
                if not line.startswith('PLATFORM_BOOTSTRAP_ADMIN_EMAILS=')) + '\n')
            target = workspace / 'host/trading-platform-production'
            target.parent.mkdir()
            lock = workspace / 'deploy.lock'
            lock.write_bytes(b'')
            sources = {
                name: base64.b64encode(
                    (source / ('provider/factory' if name == 'factory'
                               else 'config/' + name)).read_bytes()).decode()
                for name in poc_clean.REQUIRED_HASHES
            }
            payload = {'control_sha': 'a' * 40, 'pins': PINS, 'sources': sources}

            def protected(path, expected=None, uid=0):
                if expected:
                    self.assertEqual(digest(path), expected)

            with patch.object(poc_clean_prepare_host, 'ROOT', target), \
                 patch.object(poc_clean_prepare_host, 'PARENT', target.parent), \
                 patch.object(poc_clean_prepare_host, 'LOCK', lock), \
                 patch.object(poc_clean_prepare_host.os, 'fchown'), \
                 patch.object(poc_clean.common, 'protected', side_effect=protected), \
                 self.assertRaisesRegex(poc_clean_prepare_host.PrepareBlocked,
                                        'clean-required-setting-missing'):
                poc_clean_prepare_host.prepare(payload)
            self.assertFalse(target.exists())
            self.assertFalse(list(target.parent.glob(
                '.trading-platform-production.clean-prepare-*')))


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

    def test_backup_captures_two_distinct_legacy_databases_without_old_rollback(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / 'production'
            root.mkdir(mode=0o700)
            legacy = workspace / 'legacy'
            (legacy / 'config').mkdir(parents=True)
            for name in poc_clean.LEGACY_CONFIG_MEMBERS:
                (legacy / 'config' / name).write_text('VALUE=' + name + '\n')
            for name, marker in (('market.sqlite3', 'market'),
                                 ('execution.sqlite3', 'execution')):
                connection = sqlite3.connect(workspace / name)
                connection.execute('CREATE TABLE marker(value TEXT)')
                connection.execute('INSERT INTO marker VALUES (?)', (marker,))
                connection.commit()
                connection.close()
            containers = {
                'market-api': {
                    'Config': {'Env': ['MARKET_DB_PATH=/data/market.sqlite3']},
                    'Mounts': [{'Destination': '/data', 'Source': str(workspace),
                                'Type': 'bind'}],
                },
                'execution-worker': {
                    'Config': {'Env': ['LIVE_EXECUTION_DB_PATH=/worker/execution.sqlite3']},
                    'Mounts': [{'Destination': '/worker', 'Source': str(workspace),
                                'Type': 'bind'}],
                },
            }
            ids = {service: str(index) * 64
                   for index, service in enumerate(poc_clean.SERVICES, 1)}
            identity = {
                'legacy_revision': PINS['legacy_revision'],
                'deployment_record_sha256': 'a' * 64,
                'domain': 'production.example.invalid',
                'config_sha256': {name: digest(legacy / 'config' / name)
                                  for name in poc_clean.LEGACY_CONFIG_MEMBERS},
                'containers': {service: {'id': ids[service],
                                         'image_id': 'sha256:' + str(index) * 64}
                               for index, service in enumerate(poc_clean.SERVICES, 1)},
            }

            def inspect(container_id):
                service = next(name for name, value in ids.items() if value == container_id)
                return containers[service]

            def archive(_image_id, destination):
                destination.write_bytes(b'image-archive')
                os.chmod(destination, 0o600)
                return 'sha256:' + 'f' * 64

            with patch.object(poc_clean, 'ROOT', root), \
                 patch.object(poc_clean, 'LEGACY', legacy), \
                 patch.object(poc_clean.common, 'inspect', side_effect=inspect), \
                 patch.object(poc_clean, 'image_archive', side_effect=archive), \
                 patch.object(poc_clean, 'verify_clean_backup'):
                result = poc_clean.capture_legacy_backup(PINS, identity)
            self.assertEqual(result['database_count'], 2)
            for name, expected in (('market.sqlite3', 'market'),
                                   ('execution.sqlite3', 'execution')):
                connection = sqlite3.connect(root / 'legacy-clean-backup' / name)
                self.assertEqual(connection.execute('SELECT value FROM marker').fetchone(),
                                 (expected,))
                connection.close()
            inventory = json.loads(
                (root / 'legacy-clean-backup/inventory.json').read_text())
            self.assertEqual(inventory['schema_version'], 2)
            self.assertEqual(set(inventory['files']), poc_clean.BACKUP_FILES)
            self.assertNotIn('rollback', json.dumps(inventory).lower())


class LegacyIdentityTests(unittest.TestCase):
    def test_current_record_containers_and_images_are_bound_before_stop(self):
        with tempfile.TemporaryDirectory() as temporary:
            legacy = Path(temporary)
            (legacy / 'repo').mkdir()
            (legacy / 'deployments').mkdir()
            (legacy / 'config').mkdir()
            (legacy / 'deployments/current.env').write_text(
                'DEPLOYED_SHA=' + PINS['legacy_revision'] + '\n')
            for name in poc_clean.LEGACY_CONFIG_MEMBERS:
                text = ('MARKET_DOMAIN=production.example.invalid\n'
                        if name == 'gateway.env' else 'SAFE_VALUE=production\n')
                (legacy / 'config' / name).write_text(text)
            ids = {service: str(index) * 64
                   for index, service in enumerate(poc_clean.SERVICES, 1)}
            images = {service: 'sha256:' + str(index + 3) * 64
                      for index, service in enumerate(poc_clean.SERVICES, 1)}
            containers = {}
            for service in poc_clean.SERVICES:
                env = ['BROKER_PROVIDER=disabled']
                if service == 'market-api':
                    env.append('MARKET_DATA_PROVIDER=shioaji')
                if service == 'execution-worker':
                    env.append('LIVE_TRADING_ENABLED=false')
                containers[ids[service]] = {
                    'Id': ids[service], 'Image': images[service],
                    'Config': {'Env': env, 'Labels': {
                        'com.docker.compose.service': service,
                    }},
                    'HostConfig': {'ReadonlyRootfs': True, 'CapDrop': ['ALL'],
                                   'SecurityOpt': ['no-new-privileges:true']},
                    'State': {'Running': True, 'Health': {'Status': 'healthy'}},
                }

            def run(argv, *args, **kwargs):
                if argv[0] == 'git':
                    return (PINS['legacy_revision'] + '\n').encode()
                if argv[:3] == ['docker', 'ps', '-aq']:
                    return ('\n'.join(ids.values()) + '\n').encode()
                if argv[:3] == ['docker', 'image', 'inspect']:
                    return json.dumps([{'Config': {'Labels': {
                        'org.opencontainers.image.revision': PINS['legacy_revision'],
                    }}}]).encode()
                if argv[0] == 'curl':
                    return b'ok'
                self.fail('unexpected command: ' + repr(argv))

            with patch.object(poc_clean, 'LEGACY', legacy), \
                 patch.object(poc_clean.common, 'run', side_effect=run), \
                 patch.object(poc_clean.common, 'inspect', side_effect=containers.get), \
                 patch.object(poc_clean.common, 'worker_locked') as worker_locked:
                identity = poc_clean.legacy_identity(PINS)
            self.assertEqual(identity['legacy_revision'], PINS['legacy_revision'])
            self.assertEqual(identity['containers'], {
                service: {'id': ids[service], 'image_id': images[service]}
                for service in poc_clean.SERVICES
            })
            worker_locked.assert_called_once_with(ids['execution-worker'], legacy=True)


class FailureRecoveryTests(unittest.TestCase):
    def test_preflight_refuses_initialized_or_partial_environment(self):
        host = poc_clean.CleanHost(PINS, {}, b'', {
            'platform_sha': PINS['platform_sha'], 'control_sha': PINS['platform_sha'],
            'master_gates': {PINS['platform_sha']: {'CI': 1, 'Security': 2}},
        })
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ('config', 'provider', 'data'):
                (root / name).mkdir(mode=0o700)
            original_stat = Path.stat

            def root_owned_stat(path, *args, **kwargs):
                result = original_stat(path, *args, **kwargs)
                return types.SimpleNamespace(st_mode=result.st_mode, st_uid=0)

            with patch.object(poc_clean, 'ROOT', root), \
                 patch.object(poc_clean, 'LEGACY', Path('/opt/tw-quant')), \
                 patch.object(poc_clean.os, 'geteuid', return_value=0), \
                 patch.object(poc_clean.Path, 'stat', new=root_owned_stat), \
                 self.assertRaisesRegex(ValueError, 'clean-environment-already-initialized'):
                host.preflight()

    def test_recovery_starts_exact_legacy_ids_and_retains_partial_database(self):
        identity = {
            'legacy_revision': PINS['legacy_revision'],
            'deployment_record_sha256': 'a' * 64,
            'domain': 'production.example.invalid',
            'config_sha256': {name: 'b' * 64 for name in poc_clean.LEGACY_CONFIG_MEMBERS},
            'containers': {service: {'id': str(index) * 64,
                                     'image_id': 'sha256:' + str(index) * 64}
                           for index, service in enumerate(poc_clean.SERVICES, 1)},
        }
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
                 patch.object(poc_clean, 'legacy_identity', return_value=identity), \
                 patch.object(poc_clean.common, 'healthy'):
                poc_clean.recover(PINS, identity)
            self.assertEqual([call for call in calls if call[:2] == ['docker', 'start']],
                             [['docker', 'start', identity['containers'][service]['id']]
                              for service in poc_clean.SERVICES])
            self.assertTrue((root / 'data').exists())
            self.assertTrue((root / 'rollback-clean-result.json').exists())

    def test_recovery_reverifies_completed_new_backup_digest(self):
        identity = {
            'legacy_revision': PINS['legacy_revision'],
            'deployment_record_sha256': 'a' * 64,
            'domain': 'production.example.invalid',
            'config_sha256': {name: 'b' * 64 for name in poc_clean.LEGACY_CONFIG_MEMBERS},
            'containers': {service: {'id': str(index) * 64,
                                     'image_id': 'sha256:' + str(index) * 64}
                           for index, service in enumerate(poc_clean.SERVICES, 1)},
        }

        def run(argv, *args, **kwargs):
            if argv[:3] == ['docker', 'ps', '-aq']:
                return b''
            return b''

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'backup-clean.json').write_text(json.dumps({
                'inventory_sha256': 'c' * 64,
            }))
            with patch.object(poc_clean, 'ROOT', root), \
                 patch.object(poc_clean.common, 'run', side_effect=run), \
                 patch.object(poc_clean, 'legacy_identity', return_value=identity), \
                 patch.object(poc_clean, 'verify_clean_backup') as verify, \
                 patch.object(poc_clean.common, 'healthy'):
                poc_clean.recover(PINS, identity)
            verify.assert_called_once_with(PINS, 'c' * 64, identity)
            result = json.loads((root / 'rollback-clean-result.json').read_text())
            self.assertTrue(result['legacy_backup_verified'])


class SourceBoundaryTests(unittest.TestCase):
    def test_clean_entry_is_separate_and_runtime_never_copies_legacy_db(self):
        source = (ROOT / 'deploy/production/poc_clean.py').read_text()
        compose = (ROOT / 'deploy/production/docker-compose.poc-clean.yml').read_text()
        self.assertIn("'market.sqlite3', 'execution.sqlite3'", source)
        self.assertIn("path.exists()", source)
        self.assertNotIn("rollback/data.sqlite3', ROOT / 'data/platform.sqlite3", source)
        self.assertNotIn("ROOT / 'rollback'", source)
        self.assertNotIn('replay.csv', compose)

    def test_workflow_is_manual_exact_p8_and_has_failure_recovery(self):
        workflow = (ROOT / '.github/workflows/deploy-production-poc-clean.yml').read_text()
        self.assertIn('workflow_dispatch:', workflow)
        self.assertNotIn('push:', workflow)
        self.assertIn('git merge-base --is-ancestor "$P9_SOURCE" "$GITHUB_SHA"', workflow)
        self.assertIn(":(exclude)deploy/production/approved-p8.json", workflow)
        self.assertIn('PRODUCTION_POC_APPROVED_CONFIG_SHA256_JSON', workflow)
        self.assertNotIn('LEGACY_ROLLBACK_INVENTORY_SHA256', workflow)
        self.assertIn('poc_clean_transport.py recover', workflow)
        self.assertNotIn('docker build', workflow)

        prepare = (ROOT / '.github/workflows/prepare-production-poc-clean.yml').read_text()
        self.assertIn('workflow_dispatch:', prepare)
        self.assertIn('poc_clean_prepare.py --runner', prepare)
        self.assertNotIn('PRODUCTION_REPLAY_CSV_B64', prepare)
        self.assertLess(prepare.index('python tools/verify_public_candidate.py'),
                        prepare.index('secrets.PRODUCTION_POC_MARKET_ENV_B64'))


if __name__ == '__main__':
    unittest.main()

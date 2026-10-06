"""Real SQLite/WAL and fail-closed host/transport regressions; no Production access."""
import ast
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import tarfile
import types
import unittest
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'deploy/production'))
sys.path.insert(0, str(ROOT / 'deploy/staging'))
import readonly_preflight as runner
validation = types.ModuleType('p9_validation')
exec(runner.reviewed_validation_source(), validation.__dict__)
sys.modules['p9_validation'] = validation
import readonly_host as host
import readonly_sqlite as database

PINS = json.loads((ROOT / 'deploy/production/approved-p8.json').read_text())


def fixture_db(path, status='locked'):
    con = sqlite3.connect(path)
    con.execute('CREATE TABLE execution_targets (id INTEGER, status TEXT)')
    con.execute('INSERT INTO execution_targets VALUES (1, ?)', (status,))
    con.execute('CREATE TABLE user_state (payload TEXT)')
    con.execute("INSERT INTO user_state VALUES ('private account fixture')")
    con.commit()
    return con


class SQLiteTests(unittest.TestCase):
    def test_disk_files_unchanged_and_no_sidecars_or_disk_sqlite_connect(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'data.sqlite3'
            fixture_db(path).close()
            before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in path.parent.iterdir()}
            original = database.sqlite3.connect
            def memory_only(name, *args, **kwargs):
                self.assertEqual(name, ':memory:')
                return original(name, *args, **kwargs)
            with patch.object(database.sqlite3, 'connect', side_effect=memory_only):
                state = database.snapshot(path)
            self.assertEqual(state['sqlite_integrity'], 'ok')
            self.assertEqual(state['targets'], 1)
            self.assertEqual(state['active_targets'], 0)
            self.assertEqual(before, {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in path.parent.iterdir()})
            self.assertNotIn('private account fixture', json.dumps(state))

    def test_committed_wal_state_is_used_instead_of_stale_main_db(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'data.sqlite3'
            con = fixture_db(path)
            con.execute('PRAGMA journal_mode=WAL')
            con.execute('PRAGMA wal_autocheckpoint=0')
            con.execute("UPDATE execution_targets SET status='active'"); con.commit()
            try:
                before = {p.name: p.read_bytes() for p in path.parent.iterdir()}
                with self.assertRaisesRegex(ValueError, '^target-not-locked$'):
                    database.snapshot(path)
                self.assertEqual(before, {p.name: p.read_bytes() for p in path.parent.iterdir()})
            finally:
                con.close()

    def test_wal_growth_and_uncommitted_tail(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'data.sqlite3'
            con = fixture_db(path)
            con.execute('PRAGMA journal_mode=WAL'); con.execute('PRAGMA wal_autocheckpoint=0')
            con.execute("INSERT INTO user_state VALUES (?)", ('x' * 15000,)); con.commit()
            try:
                state = database.snapshot(path)
                self.assertEqual(state['targets'], 1)
                image = database.committed_image(path.read_bytes(), Path(str(path) + '-wal').read_bytes())
                memory = sqlite3.connect(':memory:'); memory.deserialize(image)
                self.assertEqual(memory.execute('SELECT count(*) FROM user_state').fetchone(), (2,))
                memory.close()
                # A trailing valid frame without commit must not replace committed pages.
                wal = Path(str(path) + '-wal').read_bytes()
                page_size = int.from_bytes(wal[8:12], 'big')
                frame = wal[-(page_size + 24):]
                header = bytearray(frame[:24]); header[4:8] = b'\x00' * 4
                endian = '<' if wal[:4] == bytes.fromhex('377f0682') else '>'
                import struct
                checksum = database.checksum(bytes(header[:8]) + frame[24:], struct.unpack('>II', frame[16:24]), endian)
                header[16:24] = struct.pack('>II', *checksum)
                self.assertEqual(database.committed_image(path.read_bytes(), wal + bytes(header) + frame[24:]), image)
            finally:
                con.close()

    def test_null_unlocked_corrupt_db_wal_and_concurrent_change_fail(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'data.sqlite3'
            fixture_db(path, None).close()
            with self.assertRaisesRegex(ValueError, 'target-not-locked'):
                database.snapshot(path)
            data = path.read_bytes()
            with self.assertRaises(ValueError):
                database.committed_image(b'bad', b'')
            with self.assertRaises(ValueError):
                database.committed_image(data, b'bad')
            path.unlink(); fixture_db(path).close()
            original = database.read_file
            calls = []
            def changed(p):
                value = original(p); calls.append(p)
                return value if len(calls) == 1 else value + b'changed'
            with patch.object(database, 'read_file', side_effect=changed), self.assertRaisesRegex(ValueError, 'sqlite-changed-during-read'):
                database.snapshot(path)


def container(service, number, provider='replay'):
    env = ['BROKER_PROVIDER=disabled', 'LIVE_TRADING_ENABLED=false',
           'MARKET_DB_PATH=/data/market.sqlite3', 'LIVE_EXECUTION_DB_PATH=/data/market.sqlite3',
           'MARKET_REPLAY_CSV=/data/replay.csv', 'MARKET_DATA_PROVIDER=' + provider]
    return {'Id': str(number) * 64, 'Image': 'sha256:' + str(number + 3) * 64,
            'Config': {'Labels': {'com.docker.compose.service': service}, 'Env': env},
            'State': {'Running': True, 'Health': {'Status': 'healthy'},
                      'StartedAt': (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()},
            'HostConfig': {'ReadonlyRootfs': True, 'CapDrop': ['ALL'], 'SecurityOpt': ['no-new-privileges:true']},
            'Mounts': []}


class HostTests(unittest.TestCase):
    def setUp(self):
        self.containers = {s: container(s, i) for i, s in enumerate(host.SERVICES, 1)}

    def test_revision_mismatch_is_first_and_no_container_or_file_reads(self):
        with patch.object(host, 'read_command', return_value=b'0' * 40), \
                patch.object(host, 'running_containers') as containers, \
                patch.object(validation, 'config_check') as configs:
            with self.assertRaisesRegex(ValueError, '^production-revision-mismatch$'):
                host.inspect_host(PINS, {}, '', {})
            containers.assert_not_called(); configs.assert_not_called()

    def test_sdk_stops_before_approvals_backup_sqlite_or_other_commands(self):
        unapproved = copy.deepcopy(PINS); unapproved.pop('market_capabilities')
        for provider in ('shioaji', 'broker-sdk', 'other-sdk'):
            self.containers['market-api'] = container('market-api', 1, provider)
            result = {}
            with patch.object(host, 'read_command', return_value=host.REVISION.encode()) as command, \
                    patch.object(host, 'running_containers', return_value=self.containers), \
                    patch.object(validation, 'config_check') as configs, \
                    patch.object(validation, 'verify_backup') as backup, \
                    patch.object(database, 'snapshot') as sqlite:
                with self.assertRaisesRegex(ValueError, '^accepted-runtime-market-capability$'):
                    host.inspect_host(unapproved, {}, '', result)
                self.assertEqual(command.call_count, 1)
                configs.assert_not_called(); backup.assert_not_called(); sqlite.assert_not_called()
                self.assertNotIn(provider, json.dumps(result)) if provider != 'shioaji' else None

    def test_command_guard_rejects_every_mutation_and_docker_exec(self):
        forbidden = [['docker', op, 'x'] for op in ('pull', 'build', 'stop', 'start', 'restart', 'exec', 'rm')]
        forbidden += [['docker', 'compose', 'up'], ['git', 'reset', '--hard'], ['touch', '/tmp/file']]
        with patch.object(host.subprocess, 'run') as run:
            for cmd in forbidden:
                with self.subTest(cmd=cmd), self.assertRaisesRegex(ValueError, 'readonly-command-rejected'):
                    host.read_command(cmd)
            run.assert_not_called()

    def test_database_requires_same_physical_mount_and_path(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = Path(d) / 'one', Path(d) / 'two'; a.mkdir(); b.mkdir()
            fixture_db(a / 'market.sqlite3').close(); fixture_db(b / 'market.sqlite3').close()
            for service, source in [('market-api', a), ('execution-worker', b)]:
                self.containers[service]['Mounts'] = [{'Destination': '/data', 'Source': str(source)}]
            with self.assertRaisesRegex(ValueError, 'legacy-database-boundary-mismatch'):
                host.database(self.containers)
            self.containers['execution-worker']['Mounts'][0]['Source'] = str(a)
            self.assertEqual(host.database(self.containers), a / 'market.sqlite3')
            with self.assertRaises(ValueError):
                host.mapped_path(self.containers['market-api'], '/data/../private')

    def test_execution_unsafe_flags_calls_stale_heartbeat_and_identity_redacted(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'health.json'
            self.containers['execution-worker']['Mounts'] = [{'Destination': '/run/tw-quant-execution', 'Source': d}]
            health = {'locked': True, 'enabled': False, 'ordering_enabled': False, 'connected': False,
                      'execution_state': 'disabled', 'recovery_status': 'locked', 'external_order_calls': 0,
                      'external_cancel_calls': 0, 'heartbeat_at': datetime.now(timezone.utc).isoformat(),
                      'masked_account_id': 'private account fixture', 'broker_accounts': ['secret fixture']}
            path.write_text(json.dumps(health))
            self.assertNotIn('fixture', json.dumps(host.execution_safety(self.containers)))
            for flag in (*validation.DISABLED, 'LIVE_TRADING_CONFIRMATION', 'BROKER_PROVIDER'):
                c = copy.deepcopy(self.containers)
                env = host.env_of(c['execution-worker']); env[flag] = 'true'
                c['execution-worker']['Config']['Env'] = [k + '=' + v for k, v in env.items()]
                with self.subTest(flag=flag), self.assertRaises(ValueError):
                    host.execution_safety(c)
            for name, value in [('external_order_calls', 1), ('external_cancel_calls', 1), ('locked', False),
                                ('heartbeat_at', (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat())]:
                bad = {**health, name: value}; path.write_text(json.dumps(bad))
                with self.subTest(name=name), self.assertRaises(ValueError):
                    host.execution_safety(self.containers)

    def test_missing_lock_never_created_and_exception_secret_not_emitted(self):
        with patch.object(host, 'initial_state'), patch.object(Path, 'is_file', return_value=False), patch.object(Path, 'open') as opened:
            result = host.collect(PINS, {}, '')
            self.assertEqual(result['reason'], 'deployment-lock-unavailable'); opened.assert_not_called()
        fake = Mock(); fake.__enter__ = Mock(return_value=fake); fake.__exit__ = Mock(return_value=False)
        fake.fileno.return_value = 1
        with patch.object(host, 'initial_state'), patch.object(Path, 'is_file', return_value=True), patch.object(Path, 'is_symlink', return_value=False), \
                patch.object(Path, 'open', return_value=fake) as opened, patch.object(host.fcntl, 'flock'), \
                patch.object(host, 'inspect_host', side_effect=ValueError('private-token-and-account')):
            result = host.collect(PINS, {}, '')
            self.assertEqual(result['reason'], 'inspection-failed'); self.assertNotIn('private', json.dumps(result))
            self.assertEqual(opened.call_args.args, ('rb',))

    def test_complete_preflight_pass_has_no_file_writes_and_inventory_drift_blocks(self):
        self.complete_preflight('replay')
        self.complete_preflight('shioaji')

    def complete_preflight(self, provider):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary); root = base / 'p9'; legacy = base / 'legacy'; data = base / 'data'; data.mkdir()
            for directory in (root / 'config', root / 'provider', root / 'rollback', legacy / 'config', legacy / 'deployments'):
                directory.mkdir(parents=True, mode=0o700)
            fixture_db(data / 'market.sqlite3').close()
            (data / 'replay.csv').write_text('Production replay fixture\n')
            (root / 'config/replay.csv').write_bytes((data / 'replay.csv').read_bytes())
            (root / 'rollback/data.sqlite3').write_bytes((data / 'market.sqlite3').read_bytes())
            (legacy / 'deployments/current.env').write_text('deployed_sha=' + host.REVISION + '\n')
            config = {
                'market.env': 'PLATFORM_ENVIRONMENT=production\nPLATFORM_AUTHORIZATION_MODE=enforced\nMARKET_ACCESS_MODE=cloudflare\n'
                    'MARKET_DATA_PROVIDER=replay\nMARKET_REPLAY_CSV=/run/production-market/replay.csv\n'
                    'MARKET_DB_PATH=/data/platform.sqlite3\nPRIVATE_PROVIDER_WHEEL_SHA256=' + PINS['manifest']['private_provider']['wheel_sha256'] + '\n',
                'execution.env': 'BROKER_PROVIDER=disabled\nLIVE_TRADING_ENABLED=false\nLIVE_EXECUTION_DB_PATH=/data/platform.sqlite3\n'
                    'LIVE_EXECUTION_HEALTH_PATH=/run/tw-quant-execution/health.json\n',
                'gateway.env': 'MARKET_DOMAIN=production.example\n'}
            config['execution.env'] += ''.join(k + '=false\n' for k in validation.DISABLED[1:5])
            if provider == 'shioaji':
                config['market.env'] = config['market.env'].replace('MARKET_DATA_PROVIDER=replay', 'MARKET_DATA_PROVIDER=shioaji')
                config['market.env'] = config['market.env'].replace('MARKET_REPLAY_CSV=/run/production-market/replay.csv\n', '')
                config['market.env'] += 'MARKET_SJ_API_KEY=synthetic-market-key\nMARKET_SJ_SECRET_KEY=synthetic-market-secret\nMARKET_SJ_PRODUCTION=true\n'
                env = host.env_of(self.containers['market-api'])
                env['MARKET_DATA_PROVIDER'] = 'shioaji'; env.pop('MARKET_REPLAY_CSV', None)
                self.containers['market-api']['Config']['Env'] = [k + '=' + v for k, v in env.items()]
                # Active Legacy has no replay source; only sealed P9 fallback remains.
                (data / 'replay.csv').unlink()
            for name, content in config.items():
                (root / 'config' / name).write_text(content)
            (root / 'provider/factory').write_text('fixture_provider:factory\n')
            for name in ('compose', 'market', 'execution', 'gateway'):
                (legacy / 'config' / (name + '.env')).write_text('FIXTURE=private\n')
            with tarfile.open(root / 'rollback/config.tar', 'w') as archive:
                for name in ('compose', 'market', 'execution', 'gateway'):
                    archive.add(legacy / 'config' / (name + '.env'), arcname='config/' + name + '.env')
            config_body = b'{"architecture":"amd64","Env":["PRIVATE=identity fixture"]}'
            config_hash = hashlib.sha256(config_body).hexdigest()
            for service in host.SERVICES:
                with tarfile.open(root / 'rollback' / ('image-' + service + '.tar'), 'w') as archive:
                    for name, payload in [(config_hash + '.json', config_body),
                                          ('manifest.json', json.dumps([{'Config': config_hash + '.json'}]).encode())]:
                        member = tarfile.TarInfo(name); member.size = len(payload); archive.addfile(member, io.BytesIO(payload))
            backup = {'schema_version': 1, 'revision': host.REVISION, 'domain': 'production.example',
                      'record_sha256': validation.file_sha(legacy / 'deployments/current.env'),
                      'containers': {s: {'id': c['Id'], 'image_id': c['Image'], 'config_digest': 'sha256:' + config_hash}
                                     for s, c in self.containers.items()},
                      'files': {name: validation.file_sha(root / 'rollback' / name) for name in
                                ['data.sqlite3', 'config.tar', *('image-' + s + '.tar' for s in host.SERVICES)]}}
            (root / 'rollback/rollback.json').write_text(json.dumps(backup))
            lock_path = base / 'existing.lock'; lock_path.write_text('')
            for p in base.rglob('*'):
                if p.is_file():
                    p.chmod(0o600)
            hashes = {name: validation.file_sha(root / ('provider/factory' if name == 'factory' else 'config/' + name))
                      for name in ('market.env', 'execution.env', 'gateway.env', 'factory', 'replay.csv')}
            rollback_hash = validation.file_sha(root / 'rollback/rollback.json')
            health_dir = base / 'health'; health_dir.mkdir()
            health = {'locked': True, 'enabled': False, 'ordering_enabled': False, 'connected': False,
                      'execution_state': 'disabled', 'recovery_status': 'locked', 'external_order_calls': 0,
                      'external_cancel_calls': 0, 'heartbeat_at': datetime.now(timezone.utc).isoformat()}
            (health_dir / 'health.json').write_text(json.dumps(health))
            for service in ('market-api', 'execution-worker'):
                self.containers[service]['Mounts'] = [{'Destination': '/data', 'Source': str(data)},
                     {'Destination': '/run/tw-quant-execution', 'Source': str(health_dir)}]
            protected = validation.protected
            opened = Path.open
            before = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in base.rglob('*') if p.is_file()}
            def reads_only(p, mode='r', *args, **kwargs):
                self.assertNotRegex(mode, r'[wax+]')
                if str(p) == '/var/lock/tw-quant-deploy.lock':
                    p = lock_path
                return opened(p, mode, *args, **kwargs)
            def commands(argv):
                if argv[0] == 'git':
                    return host.REVISION.encode()
                self.assertEqual(argv[:3], ['docker', 'image', 'inspect'])
                return json.dumps([{'Config': {'Labels': {'org.opencontainers.image.revision': host.REVISION}}}]).encode()
            with patch.object(host, 'ROOT', root), patch.object(host, 'LEGACY', legacy), \
                    patch.object(validation, 'ROOT', root), patch.object(validation, 'LEGACY', legacy), \
                    patch.object(host, 'directory_check'), patch.object(host, 'read_command', side_effect=commands), \
                    patch.object(host, 'running_containers', return_value=self.containers), \
                    patch.object(host, 'image_digest', return_value='sha256:' + config_hash), \
                    patch.object(validation, 'protected', side_effect=lambda p, digest=None, uid=0: protected(p, digest, uid=os.getuid())), \
                    patch.object(Path, 'open', reads_only), patch.object(Path, 'is_file', autospec=True,
                         side_effect=lambda p: True if str(p) == '/var/lock/tw-quant-deploy.lock' else p.stat().st_mode & 0o170000 == 0o100000):
                result = host.collect(PINS, hashes, rollback_hash)
                self.assertEqual(result['P9_PREREQUISITE'], 'PASS', result)
                self.assertEqual(result['market_provider'], provider)
                runner.safe_evidence(result)
                self.assertNotIn('identity fixture', json.dumps(result))
                self.assertNotIn('synthetic-market', json.dumps(result))
                self.assertEqual(before, {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in base.rglob('*') if p.is_file()})
                for service in host.SERVICES:
                    wrong = copy.deepcopy(self.containers); wrong[service]['Image'] = 'sha256:' + '0' * 64
                    with patch.object(host, 'running_containers', return_value=wrong):
                        self.assertEqual(host.collect(PINS, hashes, rollback_hash)['reason'], 'production-container-mismatch')
                state = database.snapshot(data / 'market.sqlite3')
                with patch.object(database, 'snapshot', side_effect=[state, {**state, 'tables': {'changed': 'x'}}]):
                    self.assertEqual(host.collect(PINS, hashes, rollback_hash)['reason'], 'backup-stale-or-different')


class RunnerTests(unittest.TestCase):
    def test_control_gate_precedes_ssh_and_key_files_are_runner_only_and_cleaned(self):
        with patch.dict(os.environ, {'GITHUB_EVENT_NAME': 'push'}), patch.object(runner.transport, 'configure') as configure:
            with self.assertRaisesRegex(ValueError, 'control-gate-failed'):
                runner.inspect_once()
            configure.assert_not_called()
        import evidence
        with tempfile.TemporaryDirectory() as temporary:
            env = {'RUNNER_TEMP': temporary, 'GITHUB_EVENT_NAME': 'workflow_dispatch',
                   'GITHUB_REF': 'refs/heads/master', 'GITHUB_SHA': 'a' * 40,
                   'PRODUCTION_HOST': 'production.example', 'PRODUCTION_USER': 'fixture', 'STAGING_HOST_IDENTITY': 'staging.example',
                   'PRODUCTION_SSH_PRIVATE_KEY': 'private-key-fixture', 'PRODUCTION_SSH_HOST_KEY': 'host-key-fixture',
                   'PRODUCTION_APPROVED_CONFIG_SHA256_JSON': json.dumps({name: 'b' * 64 for name in
                       ('market.env', 'execution.env', 'gateway.env', 'factory', 'replay.csv')}),
                   'LEGACY_ROLLBACK_INVENTORY_SHA256': 'c' * 64}
            addresses = lambda name, port: [(None, None, None, None, ('10.0.0.1' if name == 'production.example' else '10.0.0.2', 0))]
            result = {'schema_version': 1, 'P9_PREREQUISITE': 'BLOCKED', 'reason': 'accepted-runtime-market-capability'}
            def ssh_only(argv, **kwargs):
                self.assertEqual(argv[0], 'ssh')
                self.assertEqual(argv[-1], 'sudo -n env PYTHONDONTWRITEBYTECODE=1 python3 -B -')
                key = Path(argv[2]); self.assertTrue(key.is_relative_to(temporary)); self.assertTrue(key.exists())
                self.assertEqual(key.stat().st_mode & 0o777, 0o600)
                self.assertEqual(kwargs['stderr'], subprocess.DEVNULL)
                self.assertNotIn(b'private-key-fixture', kwargs['input'])
                return types.SimpleNamespace(returncode=0, stdout=json.dumps(result).encode())
            with patch.dict(os.environ, env), patch.object(evidence, 'api', return_value={'object': {'sha': 'a' * 40}}), \
                    patch.object(evidence, 'master_gates'), patch.object(runner.transport.socket, 'getaddrinfo', side_effect=addresses), \
                    patch.object(runner.subprocess, 'run', side_effect=ssh_only), \
                    patch.object(runner, 'in_memory', wraps=runner.in_memory) as payload:
                self.assertEqual(runner.inspect_once(), result)
                payload.assert_called_once_with(PINS, json.loads(env['PRODUCTION_APPROVED_CONFIG_SHA256_JSON']), 'c' * 64)
                self.assertEqual(list(Path(temporary).iterdir()), [])
                with patch.object(runner.subprocess, 'run', side_effect=RuntimeError('private-key-fixture')):
                    with self.assertRaises(RuntimeError):
                        runner.inspect_once()
                self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_main_rejects_raw_host_output_and_emits_only_sanitized_runner_artifact(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {'RUNNER_TEMP': temporary}), \
                    patch.object(runner, 'inspect_once', side_effect=ValueError('private-token')), \
                    patch('sys.stdout', new_callable=io.StringIO) as stdout:
                self.assertEqual(runner.main(), 1)
                self.assertNotIn('private-token', stdout.getvalue())
                self.assertEqual(json.loads(stdout.getvalue())['reason'], 'inspection-failed')
            self.assertEqual([p.name for p in Path(temporary).iterdir()], ['p9-readonly-evidence.json'])

    def test_payload_has_only_reviewed_read_functions_and_no_cutover_main(self):
        selected = runner.reviewed_validation_source()
        functions = {n.name for n in ast.parse(selected).body if isinstance(n, ast.FunctionDef)}
        self.assertEqual(functions, set(runner.VALIDATORS))
        self.assertNotIn(b'write_json', selected); self.assertNotIn(b'docker\', \'exec', selected)
        compile(runner.in_memory(PINS, {}, ''), '<payload>', 'exec')
        self.assertNotIn(b'__main__', selected)

    def test_every_unapproved_output_field_and_exception_is_rejected(self):
        safe = {'schema_version': 1, 'P9_PREREQUISITE': 'BLOCKED', 'reason': 'accepted-runtime-market-capability',
                'legacy_revision': host.REVISION, 'market_provider': 'shioaji',
                'market_provider_source': 'running-container-environment'}
        self.assertEqual(runner.safe_evidence(safe), safe)
        for changes in ({'account': 'secret'}, {'reason': 'private-token'}, {'market_provider': 'private-token'},
                        {'config_sha256': {'factory': 'secret'}}, {'P9_PREREQUISITE': 'PASS'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                runner.safe_evidence({**safe, **changes})

    def test_workflow_is_manual_master_guarded_separate_and_exact_artifact_path(self):
        text = (ROOT / '.github/workflows/production-readonly-preflight.yml').read_text()
        events = text.split('\non:\n')[1].split('\npermissions:')[0]
        self.assertEqual(events.strip(), 'workflow_dispatch:')
        self.assertIn('environment: lightsail-production', text)
        self.assertIn("github.ref == 'refs/heads/master'", text)
        self.assertIn('path: ${{ runner.temp }}/p9-readonly-evidence.json', text)
        self.assertLess(text.index('python tools/verify_public_candidate.py'), text.index('secrets.'))
        for forbidden in ('transport.py cutover', 'scp', 'rsync', 'docker pull', 'docker build', 'continue-on-error'):
            self.assertNotIn(forbidden, text)


if __name__ == '__main__':
    unittest.main()

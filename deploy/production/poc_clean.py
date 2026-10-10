"""P9 PoC clean Production deployment with retained Legacy recovery.

This is deliberately separate from ``cutover.py``.  It never copies a Legacy
SQLite database into the new platform.  Legacy is stopped, captured as a
root-only two-database recovery set, and retained until the fresh platform has
passed restart and public-origin gates.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tarfile
import tempfile

import cutover as common


ROOT = common.ROOT
LEGACY = common.LEGACY
SERVICES = common.SERVICES
REQUIRED_HASHES = frozenset({'market.env', 'execution.env', 'gateway.env', 'factory'})
BACKUP_FILES = frozenset({
    'market.sqlite3', 'execution.sqlite3', 'config.tar',
    'image-market-api.tar', 'image-execution-worker.tar', 'image-gateway.tar',
})
LEGACY_CONFIG_MEMBERS = ('compose.env', 'market.env', 'execution.env', 'gateway.env')


def require(condition, code):
    if not condition:
        raise ValueError(code)


def fsync_dir(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_private(path, data):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            require(written > 0, 'private-write-failed')
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def clean_config_check(pins, hashes):
    """Validate only inputs used by the Shioaji clean PoC path."""
    require(set(hashes) == REQUIRED_HASHES and
            all(re.fullmatch('[0-9a-f]{64}', value) for value in hashes.values()),
            'clean-config-approval-missing')
    envs = {}
    for name in ('market.env', 'execution.env', 'gateway.env'):
        path = ROOT / 'config' / name
        common.protected(path, hashes[name])
        envs[name] = common.parse_env(path.read_text())
        common.disabled(envs[name], execution=name == 'execution.env')
    factory = ROOT / 'provider/factory'
    common.protected(factory, hashes['factory'], uid=10001)
    require(re.fullmatch('[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*\n?',
                         factory.read_text()) is not None, 'factory-invalid')

    market = envs['market.env']
    require(common.market_compatibility(market, pins) == 'shioaji',
            'clean-market-provider-not-shioaji')
    required = (
        'MARKET_SJ_API_KEY', 'MARKET_SJ_SECRET_KEY', 'CF_ACCESS_TEAM_DOMAIN',
        'CF_ACCESS_AUD', 'PLATFORM_BOOTSTRAP_ADMIN_EMAILS',
    )
    require(all(market.get(name, '').strip() for name in required),
            'clean-required-setting-missing')
    require(market.get('MARKET_SJ_PRODUCTION') == 'true',
            'clean-shioaji-production-required')
    require(market.get('PLATFORM_ENVIRONMENT') == 'production' and
            market.get('PLATFORM_AUTHORIZATION_MODE') == 'enforced' and
            market.get('MARKET_ACCESS_MODE') == 'cloudflare',
            'clean-production-auth-missing')
    admins = tuple(value.strip().casefold() for value in
                   market['PLATFORM_BOOTSTRAP_ADMIN_EMAILS'].split(',') if value.strip())
    require(len(admins) == 1 and len(set(admins)) == 1 and '@' in admins[0] and
            len(admins[0]) <= 254, 'clean-admin-email-invalid')
    require(not market.get('MARKET_REPLAY_CSV', '').strip(),
            'clean-shioaji-replay-not-required')
    require(market.get('PRIVATE_PROVIDER_WHEEL_SHA256') ==
            pins['manifest']['private_provider']['wheel_sha256'],
            'provider-config-provenance')
    require(market.get('MARKET_DB_PATH') == '/data/platform.sqlite3' and
            envs['execution.env'].get('LIVE_EXECUTION_DB_PATH') == '/data/platform.sqlite3' and
            envs['execution.env'].get('LIVE_EXECUTION_HEALTH_PATH') ==
            '/run/tw-quant-execution/health.json', 'production-data-path')
    require(all(not any(key.startswith(('MARKET_SJ_', 'SJ_')) for key in envs[name])
                for name in ('execution.env', 'gateway.env')),
            'market-credential-isolation')
    for env in envs.values():
        require(not any('staging' in value.lower() or 'p8-synthetic' in value.lower()
                        for value in env.values()), 'staging-config-reuse')
    return envs


def mapped_database(container, env_name):
    env = dict(value.split('=', 1) for value in container['Config']['Env'])
    configured = Path(env.get(env_name, ''))
    require(configured.is_absolute() and '..' not in configured.parts,
            'legacy-database-path')
    mounts = [mount for mount in container['Mounts']
              if configured.is_relative_to(mount['Destination'])]
    require(bool(mounts), 'legacy-data-mount')
    mount = max(mounts, key=lambda item: len(item['Destination']))
    source = Path(mount['Source']) / configured.relative_to(mount['Destination'])
    require(source.is_absolute() and source.resolve() == source and source.is_file() and
            not source.is_symlink(), 'legacy-database-path')
    return source


def sqlite_backup(source, destination):
    require(not destination.exists() and not destination.is_symlink(),
            'legacy-backup-collision')
    try:
        source_connection = sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)
        destination_connection = sqlite3.connect(destination)
        try:
            source_connection.backup(destination_connection)
            destination_connection.commit()
            require(destination_connection.execute('PRAGMA integrity_check').fetchone() == ('ok',),
                    'legacy-backup-integrity')
        finally:
            destination_connection.close()
            source_connection.close()
        os.chmod(destination, 0o600)
        descriptor = os.open(destination, os.O_RDONLY | os.O_CLOEXEC)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except Exception:
        destination.unlink(missing_ok=True)
        raise ValueError('legacy-backup-failed') from None


def container_env(container):
    entries = container['Config']['Env']
    require(all(isinstance(value, str) and '=' in value for value in entries),
            'legacy-env-invalid')
    result = dict(value.split('=', 1) for value in entries)
    require(len(result) == len(entries), 'legacy-env-invalid')
    return result


def legacy_identity(pins, *, expected=None, stopped=False, recovering=False):
    """Bind the current Legacy deployment without an old Provision inventory."""
    revision = common.run(['git', '--no-optional-locks', '-C', str(LEGACY / 'repo'),
                           'rev-parse', 'HEAD']).decode().strip()
    require(revision == pins['legacy_revision'], 'legacy-revision-mismatch')
    record = LEGACY / 'deployments/current.env'
    require(record.is_file() and not record.is_symlink() and record.resolve() == record,
            'legacy-deployment-record-invalid')
    require(common.parse_env(record.read_text()).get('DEPLOYED_SHA') == pins['legacy_revision'],
            'legacy-deployment-record-revision')
    config_hashes = {}
    for name in LEGACY_CONFIG_MEMBERS:
        path = LEGACY / 'config' / name
        require(path.is_file() and not path.is_symlink() and path.resolve() == path and
                path.stat().st_size <= 1024 * 1024, 'legacy-config-invalid')
        config_hashes[name] = common.file_sha(path)
    domain = common.parse_env((LEGACY / 'config/gateway.env').read_text()).get(
        'MARKET_DOMAIN', '')
    require(re.fullmatch('[a-z0-9.-]{1,253}', domain) is not None,
            'legacy-domain-invalid')

    ids = common.run(['docker', 'ps', '-aq', '--no-trunc', '--filter',
                      'label=com.docker.compose.project=tw-quant-lightsail']).decode().split()
    require(len(ids) == len(SERVICES) and
            all(re.fullmatch('[0-9a-f]{64}', value) for value in ids),
            'legacy-container-mismatch')
    if expected is not None:
        require(set(ids) == {expected['containers'][service]['id'] for service in SERVICES},
                'legacy-container-mismatch')
    containers = {}
    market_provider = None
    for container_id in ids:
        data = common.inspect(container_id)
        service = data['Config']['Labels'].get('com.docker.compose.service')
        require(service in SERVICES and service not in containers and
                re.fullmatch('sha256:[0-9a-f]{64}', data['Image']) is not None,
                'legacy-container-mismatch')
        require(recovering or data['State']['Running'] is (not stopped),
                'legacy-container-state')
        image = json.loads(common.run(['docker', 'image', 'inspect', data['Image']]))[0]
        if service != 'gateway':
            require(image['Config']['Labels'].get('org.opencontainers.image.revision') ==
                    pins['legacy_revision'], 'legacy-image-revision')
        env = container_env(data)
        common.disabled(env, execution=service == 'execution-worker')
        require(data['HostConfig']['ReadonlyRootfs'] is True and
                'ALL' in data['HostConfig']['CapDrop'] and
                'no-new-privileges:true' in data['HostConfig']['SecurityOpt'],
                'legacy-runtime-security')
        if service == 'market-api':
            market_provider = common.market_compatibility(env, pins)
        if not stopped and not recovering and service != 'gateway':
            require(data['State'].get('Health', {}).get('Status') == 'healthy',
                    'legacy-unhealthy')
        containers[service] = {'id': data['Id'], 'image_id': data['Image']}
    require(set(containers) == set(SERVICES) and market_provider == 'shioaji',
            'legacy-market-provider')
    identity = {
        'legacy_revision': revision,
        'deployment_record_sha256': common.file_sha(record),
        'domain': domain,
        'config_sha256': config_hashes,
        'containers': containers,
    }
    if expected is not None:
        require(identity == expected, 'legacy-identity-drift')
    if not stopped and not recovering:
        common.worker_locked(containers['execution-worker']['id'], legacy=True)
        require(common.run(['curl', '--fail', '--silent', '--show-error', '--max-time',
                            '10', '--resolve', domain + ':443:127.0.0.1',
                            'https://' + domain + '/healthz']) == b'ok',
                'legacy-gateway-health')
    return identity


def legacy_config_archive(destination):
    with tarfile.open(destination, 'x:') as archive:
        for name in LEGACY_CONFIG_MEMBERS:
            source = LEGACY / 'config' / name
            require(source.is_file() and not source.is_symlink() and
                    source.resolve() == source and source.stat().st_size <= 1024 * 1024,
                    'legacy-config-invalid')
            data = source.read_bytes()
            information = tarfile.TarInfo('config/' + name)
            information.size = len(data)
            information.mode = 0o600
            information.uid = information.gid = information.mtime = 0
            archive.addfile(information, io.BytesIO(data))
    os.chmod(destination, 0o600)
    descriptor = os.open(destination, os.O_RDONLY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def image_archive(image_id, destination):
    import image_config_digest

    require(re.fullmatch('sha256:[0-9a-f]{64}', image_id) is not None and
            not destination.exists() and not destination.is_symlink(),
            'legacy-image-identity')
    try:
        with destination.open('xb') as output:
            result = subprocess.run(['docker', 'image', 'save', image_id], stdout=output,
                                    stderr=subprocess.DEVNULL, timeout=300, check=False)
            require(result.returncode == 0, 'legacy-image-backup-failed')
            output.flush()
            os.fsync(output.fileno())
        os.chmod(destination, 0o600)
        with destination.open('rb') as stream:
            return image_config_digest.config_digest(stream)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise ValueError('legacy-image-backup-failed') from None


def verify_clean_backup(pins, expected_sha=None, expected_identity=None):
    backup = ROOT / 'legacy-clean-backup'
    require(backup.is_dir() and not backup.is_symlink() and
            backup.stat().st_uid == 0 and backup.stat().st_mode & 0o777 == 0o700,
            'legacy-backup-missing')
    inventory_path = backup / 'inventory.json'
    common.protected(inventory_path, expected_sha)
    inventory = json.loads(inventory_path.read_text())
    require(inventory.get('schema_version') == 2 and
            inventory.get('purpose') == 'p9-poc-clean-legacy-recovery' and
            inventory.get('legacy_revision') == pins['legacy_revision'] and
            set(inventory.get('containers', {})) == set(SERVICES) and
            set(inventory.get('files', {})) == BACKUP_FILES,
            'legacy-backup-identity')
    if expected_identity is not None:
        require(all(inventory.get(key) == expected_identity[key] for key in
                    ('legacy_revision', 'deployment_record_sha256', 'domain',
                     'config_sha256')) and
                all(inventory['containers'][service]['id'] ==
                    expected_identity['containers'][service]['id'] and
                    inventory['containers'][service]['image_id'] ==
                    expected_identity['containers'][service]['image_id']
                    for service in SERVICES), 'legacy-backup-identity')
    for name, digest in inventory['files'].items():
        common.protected(backup / name, digest)
    for name in ('market.sqlite3', 'execution.sqlite3'):
        connection = sqlite3.connect((backup / name).resolve().as_uri() + '?mode=ro', uri=True)
        try:
            require(connection.execute('PRAGMA integrity_check').fetchone() == ('ok',),
                    'legacy-backup-integrity')
        finally:
            connection.close()
    with tarfile.open(backup / 'config.tar', 'r:') as archive:
        require(archive.getnames() == ['config/' + name for name in LEGACY_CONFIG_MEMBERS],
                'legacy-backup-config-members')
        for member in archive.getmembers():
            source = LEGACY / member.name
            require(member.isfile() and member.size <= 1024 * 1024 and
                    source.resolve() == source and not source.is_symlink() and
                    archive.extractfile(member).read() == source.read_bytes(),
                    'legacy-backup-config-drift')
    import image_config_digest
    for service in SERVICES:
        metadata = inventory['containers'][service]
        require(re.fullmatch('sha256:[0-9a-f]{64}', metadata['image_id']) is not None and
                re.fullmatch('sha256:[0-9a-f]{64}', metadata['config_digest']) is not None,
                'legacy-backup-image-identity')
        with (backup / ('image-' + service + '.tar')).open('rb') as stream:
            require(image_config_digest.config_digest(stream) == metadata['config_digest'],
                    'legacy-backup-image-digest')
    return inventory


def capture_legacy_backup(pins, identity):
    final = ROOT / 'legacy-clean-backup'
    require(not final.exists() and not final.is_symlink(), 'legacy-backup-exists')
    temporary = Path(tempfile.mkdtemp(prefix='.legacy-clean-backup.prepare-', dir=ROOT))
    os.chmod(temporary, 0o700)
    try:
        market = common.inspect(identity['containers']['market-api']['id'])
        worker = common.inspect(identity['containers']['execution-worker']['id'])
        sqlite_backup(mapped_database(market, 'MARKET_DB_PATH'), temporary / 'market.sqlite3')
        sqlite_backup(mapped_database(worker, 'LIVE_EXECUTION_DB_PATH'),
                      temporary / 'execution.sqlite3')
        legacy_config_archive(temporary / 'config.tar')
        image_digests = {}
        for service in SERVICES:
            image_digests[service] = image_archive(
                identity['containers'][service]['image_id'],
                temporary / ('image-' + service + '.tar'))
        inventory = {
            'schema_version': 2,
            'purpose': 'p9-poc-clean-legacy-recovery',
            'legacy_revision': identity['legacy_revision'],
            'deployment_record_sha256': identity['deployment_record_sha256'],
            'domain': identity['domain'],
            'config_sha256': identity['config_sha256'],
            'containers': {
                service: {
                    'id': identity['containers'][service]['id'],
                    'image_id': identity['containers'][service]['image_id'],
                    'config_digest': image_digests[service],
                } for service in SERVICES
            },
            'files': {name: common.file_sha(temporary / name)
                      for name in sorted(BACKUP_FILES)},
            'captured_at': datetime.now(timezone.utc).isoformat(),
        }
        write_private(temporary / 'inventory.json',
                      (json.dumps(inventory, sort_keys=True) + '\n').encode())
        require(set(inventory['files']) == BACKUP_FILES and
                all(common.file_sha(temporary / name) == digest
                    for name, digest in inventory['files'].items()),
                'legacy-backup-incomplete')
        fsync_dir(temporary)
        temporary.rename(final)
        fsync_dir(ROOT)
        inventory_sha256 = common.file_sha(final / 'inventory.json')
        verify_clean_backup(pins, inventory_sha256, identity)
        return {'verified': True, 'database_count': 2,
                'legacy_revision': pins['legacy_revision'],
                'inventory_sha256': inventory_sha256}
    except BaseException:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise


def copy_gateway_state(legacy_containers):
    gateway = common.inspect(legacy_containers['gateway']['id'])
    for destination, name in (('/data', 'gateway-data'), ('/config', 'gateway-config')):
        mounts = [mount for mount in gateway['Mounts'] if mount['Destination'] == destination]
        require(len(mounts) == 1 and mounts[0]['Type'] == 'volume', 'legacy-gateway-volume')
        source = Path(mounts[0]['Source'])
        require(source.is_dir() and source.resolve() == source and
                not any(path.is_symlink() for path in source.rglob('*')),
                'gateway-volume-symlink')
        shutil.copytree(source, ROOT / name, dirs_exist_ok=True)
        for path in (ROOT / name).rglob('*'):
            require(path.is_file() or path.is_dir(), 'gateway-volume-special-file')
            os.chown(path, 10000, 10000)


INIT_CODE = rb'''import os, sqlite3
from pathlib import Path
from staging_composition import create_app
from tw_quant.live.settings import LiveSettings

settings = LiveSettings.from_env()
settings.validate()
path = Path(settings.db_path)
if path != Path('/data/platform.sqlite3') or path.exists():
    raise SystemExit('P9_CLEAN_INIT=FAIL')
if len(settings.bootstrap_admin_emails) != 1:
    raise SystemExit('P9_CLEAN_INIT=FAIL')
create_app()
connection = sqlite3.connect(path)
try:
    if connection.execute('PRAGMA integrity_check').fetchone() != ('ok',):
        raise SystemExit('P9_CLEAN_INIT=FAIL')
    rows = connection.execute(
        "SELECT email,role,status,trading_mode FROM app_users ORDER BY email"
    ).fetchall()
    expected = [(settings.bootstrap_admin_emails[0], 'admin', 'active', 'disabled')]
    if rows != expected:
        raise SystemExit('P9_CLEAN_INIT=FAIL')
finally:
    connection.close()
print('P9_CLEAN_INIT=PASS')
'''


VERIFY_FRESH_CODE = rb'''import json, os, sqlite3
connection = sqlite3.connect(os.environ['MARKET_DB_PATH'])
try:
    integrity = connection.execute('PRAGMA integrity_check').fetchone()[0]
    admins = connection.execute(
        "SELECT count(*) FROM app_users WHERE role='admin' AND status='active' AND trading_mode='disabled'"
    ).fetchone()[0]
    users = connection.execute('SELECT count(*) FROM app_users').fetchone()[0]
    unsafe_targets = connection.execute(
        "SELECT count(*) FROM execution_targets WHERE status != 'locked'"
    ).fetchone()[0]
    print(json.dumps({'sqlite_integrity': integrity, 'users': users,
                      'disabled_admins': admins, 'unsafe_targets': unsafe_targets}, sort_keys=True))
finally:
    connection.close()
'''


def fresh_state(container_id):
    state = json.loads(common.run(['docker', 'exec', '-i', container_id, 'python', '-'],
                                  VERIFY_FRESH_CODE))
    require(state == {'sqlite_integrity': 'ok', 'users': 1,
                      'disabled_admins': 1, 'unsafe_targets': 0},
            'fresh-database-invalid')
    return state


def continuity(before, after):
    common.continuity(before, after)
    require(before['fresh_database'] == after['fresh_database'],
            'fresh-database-drift')


def recover(pins, identity):
    ids = common.run(['docker', 'ps', '-aq', '--no-trunc', '--filter',
                      'label=com.docker.compose.project=platform-p9-production']).decode().split()
    for container_id in ids:
        require(re.fullmatch('[0-9a-f]{64}', container_id), 'invalid-p9-container')
        common.run(['docker', 'stop', '--time', '30', container_id], timeout=60)
    state = legacy_identity(pins, expected=identity, recovering=True)
    for service in SERVICES:
        common.run(['docker', 'start', state['containers'][service]['id']], timeout=90)
    for service in ('market-api', 'execution-worker'):
        common.healthy(state['containers'][service]['id'])
    restored = legacy_identity(pins, expected=identity)
    marker = ROOT / 'backup-clean.json'
    backup_verified = False
    inventory_sha256 = None
    if marker.exists():
        document = json.loads(marker.read_text())
        inventory_sha256 = document.get('inventory_sha256')
        require(re.fullmatch('[0-9a-f]{64}', str(inventory_sha256)) is not None,
                'legacy-backup-marker-invalid')
        verify_clean_backup(pins, inventory_sha256, identity)
        backup_verified = True
    (ROOT / 'acceptance-clean.json').unlink(missing_ok=True)
    common.write_json(ROOT / 'rollback-clean-result.json', {
        'rollback': 'PASS', 'legacy_revision': pins['legacy_revision'],
        'legacy_containers_restored': set(restored['containers']) == set(SERVICES),
        'legacy_backup_verified': backup_verified,
        'legacy_backup_inventory_sha256': inventory_sha256,
        'fresh_state_retained_for_review': (ROOT / 'data').exists(),
    })


class CleanHost:
    def __init__(self, pins, hashes, durable_code, gate):
        self.pins = pins
        self.hashes = hashes
        self.durable_code = durable_code
        self.gate = gate

    def preflight(self):
        require(os.geteuid() == 0 and ROOT.resolve() == ROOT and LEGACY.resolve() == LEGACY,
                'production-root')
        require(self.gate.get('platform_sha') == self.pins['platform_sha'] and
                self.gate.get('control_sha'), 'clean-p8-control-sha-mismatch')
        require(set(self.gate.get('master_gates', {})) ==
                {self.pins['platform_sha'], self.gate['control_sha']} and
                all(set(value) == {'CI', 'Security'}
                    for value in self.gate['master_gates'].values()),
                'master-gates-missing')
        for directory in (ROOT, ROOT / 'config', ROOT / 'provider'):
            require(directory.is_dir() and not directory.is_symlink() and
                    directory.stat().st_uid == 0 and directory.stat().st_mode & 0o777 == 0o700,
                    'production-directory-isolation')
        blockers = ('transaction-clean.json', 'backup-clean.json',
                    'acceptance-clean.json', 'failure-clean.json',
                    'rollback-clean-result.json', 'legacy-clean-backup', 'data', 'health',
                    'gateway-data', 'gateway-config', 'release-clean.env')
        require(not any(os.path.lexists(ROOT / name) for name in blockers) and
                not list(ROOT.glob('.legacy-clean-backup.prepare-*')),
                'clean-environment-already-initialized')
        self.envs = clean_config_check(self.pins, self.hashes)
        self.legacy = legacy_identity(self.pins)
        legacy_market = dict(value.split('=', 1) for value in
                             common.inspect(
                                 self.legacy['containers']['market-api']['id'])['Config']['Env'])
        require(common.market_compatibility(legacy_market, self.pins) == 'shioaji',
                'production-market-provider-mismatch')
        require(common.parse_env((ROOT / 'config/gateway.env').read_text()).get('MARKET_DOMAIN') ==
                self.legacy['domain'], 'production-domain-mismatch')

    def preserve(self):
        common.write_json(ROOT / 'transaction-clean.json', {
            'status': 'PREPARED', 'mode': 'p9-poc-clean',
            'legacy_revision': self.pins['legacy_revision'],
            'legacy_identity': self.legacy,
            'fresh_database_required': True,
        })

    def stop_legacy(self):
        for service in reversed(SERVICES):
            common.run(['docker', 'stop', '--time', '30',
                        self.legacy['containers'][service]['id']], timeout=60)
        legacy_identity(self.pins, expected=self.legacy, stopped=True)
        self.clean_backup = capture_legacy_backup(self.pins, self.legacy)
        common.write_json(ROOT / 'backup-clean.json', self.clean_backup)

    def deploy(self):
        images = self.pins['manifest']['images']
        runtime = images['releases'][self.pins['release']]['runtime']
        for image, gateway in ((runtime, False), (images['gateway'], True)):
            common.run(['docker', 'pull', image['ref']], timeout=600)
            image_id = common.local_image(image, self.pins, gateway=gateway)
            if not gateway:
                common.offline_market_capability(image_id)
        for name, uid in (('data', 10001), ('health', 10001),
                          ('gateway-data', 10000), ('gateway-config', 10000)):
            path = ROOT / name
            path.mkdir(mode=0o750, exist_ok=False)
            os.chown(path, uid, uid)
        copy_gateway_state(self.legacy['containers'])
        values = {
            'PRODUCTION_RUNTIME_IMAGE': runtime['ref'],
            'PRODUCTION_GATEWAY_IMAGE': images['gateway']['ref'],
            **{'PRODUCTION_' + name.upper() + '_ENV_FILE':
               str(ROOT / 'config' / (name + '.env'))
               for name in ('market', 'execution', 'gateway')},
        }
        release = ROOT / 'release-clean.env'
        write_private(release, ''.join(key + '=' + value + '\n'
                                      for key, value in values.items()).encode())
        common.run(common.compose() + ['config', '--quiet'])
        initialized = common.run(common.compose() + [
            'run', '--rm', '--no-deps', '--no-build', '-T', 'market-api', 'python', '-'
        ], INIT_CODE, timeout=120)
        require(initialized == b'P9_CLEAN_INIT=PASS\n', 'clean-database-init-failed')
        common.run(common.compose() + ['run', '--rm', '--no-deps', '--no-build', '-T',
                   'execution-worker', 'python', '-m', 'tw_quant.execution_service', 'validate'],
                   timeout=60)
        common.run(common.compose() + ['run', '--rm', '--no-deps', '--no-build', '-T',
                   'gateway', 'caddy', 'validate', '--config', '/etc/caddy/Caddyfile',
                   '--adapter', 'caddyfile'], timeout=60)
        common.run(common.compose() + ['up', '--no-build', '--pull', 'never', '--detach'],
                   timeout=180)

    def verify(self):
        clean_config_check(self.pins, self.hashes)
        result = common.verify(self.pins, self.durable_code)
        result['fresh_database'] = fresh_state(result['containers']['market-api']['id'])
        return result

    def restart(self):
        common.run(common.compose() + ['restart', *SERVICES], timeout=120)

    def public_verify(self):
        url = 'https://' + self.legacy['domain'] + '/healthz'
        output = common.run(['curl', '--fail', '--silent', '--show-error', '--max-time',
                             '15', '--dump-header', '-', url])
        headers, body = output.rsplit(b'\r\n\r\n', 1)
        require(body == b'ok' and b'strict-transport-security:' in headers.lower() and
                b'x-content-type-options: nosniff' in headers.lower(),
                'public-origin-health')

    def commit(self, before, after):
        result = {
            'acceptance': 'PASS', 'p9_status': 'POC_CLEAN_DEPLOY_ACCEPTED',
            'mode': 'p9-poc-clean', 'gate': self.gate,
            'manifest_sha256': self.pins['manifest_sha256'],
            'legacy_backup_verified': self.clean_backup,
            'fresh_database': after['fresh_database'],
            'before_restart': before, 'after_restart': after,
            'real_order': 'disabled', 'legacy_retained': True,
            'accepted_at': datetime.now(timezone.utc).isoformat(),
        }
        common.write_json(ROOT / 'pending-clean.json', result)
        print('P9_POC_CLEAN_READY_FOR_EXTERNAL_GATE ' + self.legacy['domain'], flush=True)
        acknowledgement = sys.stdin.readline().strip()
        require(acknowledgement == 'P9_POC_CLEAN_EXTERNAL_GATE_PASS ' +
                self.pins['manifest_sha256'], 'external-runner-gate-failed')
        clean_config_check(self.pins, self.hashes)
        verify_clean_backup(self.pins, self.clean_backup['inventory_sha256'], self.legacy)
        legacy_identity(self.pins, expected=self.legacy, stopped=True)
        final = self.verify()
        require(final['fresh_database'] == after['fresh_database'] and
                final['containers'] == after['containers'], 'finalize-runtime-drift')
        result['external_runner_public_gate'] = 'PASS'
        common.write_json(ROOT / 'acceptance-clean.json', result)

    def failure(self, exc):
        reason = (str(exc) if isinstance(exc, ValueError) and
                  re.fullmatch('[a-z0-9-]+', str(exc)) else 'forward-stage-failed')
        common.write_json(ROOT / 'failure-clean.json', {
            'acceptance': 'FAIL', 'first_invariant': reason,
            'real_order': 'disabled', 'manual_cleanup_required': True,
        })
        (ROOT / 'acceptance-clean.json').unlink(missing_ok=True)

    def rollback(self):
        recover(self.pins, self.legacy)


def compose_override():
    original = common.compose

    def clean_compose():
        command = original()
        command[command.index(str(ROOT / 'release.env'))] = str(ROOT / 'release-clean.env')
        command[command.index(str(ROOT / 'bundle/docker-compose.yml'))] = str(
            ROOT / 'bundle-poc-clean/docker-compose.yml'
        )
        return command

    common.compose = clean_compose


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('preflight', 'deploy', 'recover'))
    parser.add_argument('--pins', required=True)
    parser.add_argument('--config-hashes', required=True)
    parser.add_argument('--durable-code', required=True)
    parser.add_argument('--gate')
    arguments = parser.parse_args()
    pins = json.loads(base64.b64decode(arguments.pins))
    hashes = json.loads(base64.b64decode(arguments.config_hashes))
    durable_code = base64.b64decode(arguments.durable_code)
    compose_override()
    if arguments.mode == 'recover':
        journal = json.loads((ROOT / 'transaction-clean.json').read_text())
        identity = journal.get('legacy_identity')
        require(journal.get('status') == 'PREPARED' and journal.get('mode') == 'p9-poc-clean' and
                isinstance(identity, dict) and identity.get('legacy_revision') ==
                pins['legacy_revision'],
                'recovery-journal-mismatch')
        recover(pins, identity)
    else:
        require(arguments.gate is not None, 'gate-required')
        gate = json.loads(base64.b64decode(arguments.gate))
        host = CleanHost(pins, hashes, durable_code, gate)
        if arguments.mode == 'preflight':
            host.preflight()
        else:
            def interrupted(_signum, _frame):
                raise ValueError('clean-deploy-interrupted')
            signal.signal(signal.SIGTERM, interrupted)
            signal.signal(signal.SIGHUP, interrupted)
            common.transaction(host)
    print('P9_POC_CLEAN_HOST_GATE=PASS mode=' + arguments.mode)


if __name__ == '__main__':
    try:
        main()
    except BaseException:
        raise SystemExit('P9_POC_CLEAN_HOST_GATE=FAIL')

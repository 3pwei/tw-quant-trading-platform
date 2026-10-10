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
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import sys
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


def capture_legacy_backup(pins, approved, legacy_containers):
    final = ROOT / 'legacy-clean-backup'
    require(not final.exists() and not final.is_symlink(), 'legacy-backup-exists')
    temporary = Path(tempfile.mkdtemp(prefix='.legacy-clean-backup.prepare-', dir=ROOT))
    os.chmod(temporary, 0o700)
    try:
        market = common.inspect(legacy_containers['market-api']['id'])
        worker = common.inspect(legacy_containers['execution-worker']['id'])
        sqlite_backup(mapped_database(market, 'MARKET_DB_PATH'), temporary / 'market.sqlite3')
        sqlite_backup(mapped_database(worker, 'LIVE_EXECUTION_DB_PATH'),
                      temporary / 'execution.sqlite3')
        for name in BACKUP_FILES - {'market.sqlite3', 'execution.sqlite3'}:
            source = ROOT / 'rollback' / name
            common.protected(source, approved['files'][name.replace('market.sqlite3', 'data.sqlite3')]
                             if name in approved['files'] else None)
            shutil.copyfile(source, temporary / name)
            os.chmod(temporary / name, 0o600)
        inventory = {
            'schema_version': 1,
            'purpose': 'p9-poc-clean-legacy-recovery',
            'legacy_revision': pins['legacy_revision'],
            'deployment_record_sha256': approved['record_sha256'],
            'containers': {
                service: {
                    'id': approved['containers'][service]['id'],
                    'image_id': approved['containers'][service]['image_id'],
                    'config_digest': approved['containers'][service]['config_digest'],
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
        return {'verified': True, 'database_count': 2, 'legacy_revision': pins['legacy_revision']}
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


def recover(pins, backup):
    ids = common.run(['docker', 'ps', '-aq', '--no-trunc', '--filter',
                      'label=com.docker.compose.project=platform-p9-production']).decode().split()
    for container_id in ids:
        require(re.fullmatch('[0-9a-f]{64}', container_id), 'invalid-p9-container')
        common.run(['docker', 'stop', '--time', '30', container_id], timeout=60)
    state = common.legacy_state(pins, backup, recovering=True)
    for service in SERVICES:
        common.run(['docker', 'start', state[service]['id']], timeout=90)
    for service in ('market-api', 'execution-worker'):
        common.healthy(state[service]['id'])
    restored = common.legacy_state(pins, backup)
    (ROOT / 'acceptance-clean.json').unlink(missing_ok=True)
    common.write_json(ROOT / 'rollback-clean-result.json', {
        'rollback': 'PASS', 'legacy_revision': pins['legacy_revision'],
        'legacy_containers_restored': set(restored) == set(SERVICES),
        'fresh_state_retained_for_review': (ROOT / 'data').exists(),
    })


class CleanHost:
    def __init__(self, pins, rollback_digest, hashes, durable_code, gate):
        self.pins = pins
        self.rollback_digest = rollback_digest
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
        for directory in (ROOT, ROOT / 'config', ROOT / 'provider', ROOT / 'rollback'):
            require(directory.is_dir() and not directory.is_symlink() and
                    directory.stat().st_uid == 0 and directory.stat().st_mode & 0o777 == 0o700,
                    'production-directory-isolation')
        blockers = ('transaction-clean.json', 'acceptance-clean.json', 'failure-clean.json',
                    'rollback-clean-result.json', 'legacy-clean-backup', 'data', 'health',
                    'gateway-data', 'gateway-config', 'release-clean.env')
        require(not any(os.path.lexists(ROOT / name) for name in blockers) and
                not list(ROOT.glob('.legacy-clean-backup.prepare-*')),
                'clean-environment-already-initialized')
        self.envs = clean_config_check(self.pins, self.hashes)
        self.backup = common.verify_backup(self.pins, self.rollback_digest)
        self.legacy = common.legacy_state(self.pins, self.backup)
        legacy_market = dict(value.split('=', 1) for value in
                             common.inspect(self.legacy['market-api']['id'])['Config']['Env'])
        require(common.market_compatibility(legacy_market, self.pins) == 'shioaji',
                'production-market-provider-mismatch')
        require(common.parse_env((ROOT / 'config/gateway.env').read_text()).get('MARKET_DOMAIN') ==
                self.backup['domain'], 'production-domain-mismatch')

    def preserve(self):
        common.write_json(ROOT / 'transaction-clean.json', {
            'status': 'PREPARED', 'mode': 'p9-poc-clean',
            'rollback_sha256': self.rollback_digest,
            'legacy_revision': self.pins['legacy_revision'],
            'legacy_containers': self.legacy,
            'fresh_database_required': True,
        })

    def stop_legacy(self):
        for service in reversed(SERVICES):
            common.run(['docker', 'stop', '--time', '30', self.legacy[service]['id']], timeout=60)
        common.legacy_state(self.pins, self.backup, stopped=True)
        self.clean_backup = capture_legacy_backup(self.pins, self.backup, self.legacy)

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
        copy_gateway_state(self.legacy)
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
        url = 'https://' + self.backup['domain'] + '/healthz'
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
        print('P9_POC_CLEAN_READY_FOR_EXTERNAL_GATE ' + self.backup['domain'], flush=True)
        acknowledgement = sys.stdin.readline().strip()
        require(acknowledgement == 'P9_POC_CLEAN_EXTERNAL_GATE_PASS ' +
                self.pins['manifest_sha256'], 'external-runner-gate-failed')
        clean_config_check(self.pins, self.hashes)
        common.verify_backup(self.pins, self.rollback_digest)
        common.legacy_state(self.pins, self.backup, stopped=True)
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
        recover(self.pins, self.backup)


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
    parser.add_argument('--rollback-sha256', required=True)
    parser.add_argument('--config-hashes', required=True)
    parser.add_argument('--durable-code', required=True)
    parser.add_argument('--gate')
    arguments = parser.parse_args()
    pins = json.loads(base64.b64decode(arguments.pins))
    hashes = json.loads(base64.b64decode(arguments.config_hashes))
    durable_code = base64.b64decode(arguments.durable_code)
    require(re.fullmatch('[0-9a-f]{64}', arguments.rollback_sha256),
            'rollback-digest-missing')
    compose_override()
    if arguments.mode == 'recover':
        journal = json.loads((ROOT / 'transaction-clean.json').read_text())
        require(journal.get('rollback_sha256') == arguments.rollback_sha256,
                'recovery-journal-mismatch')
        backup = common.verify_backup(pins, arguments.rollback_sha256)
        recover(pins, backup)
    else:
        require(arguments.gate is not None, 'gate-required')
        gate = json.loads(base64.b64decode(arguments.gate))
        host = CleanHost(pins, arguments.rollback_sha256, hashes, durable_code, gate)
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

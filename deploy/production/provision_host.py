"""First-time Production prerequisite provisioning host transaction.

Executed from reviewed runner bytes through SSH stdin. Legacy containers, images,
configuration, and data are read but never mutated. Only a temporary directory
under /srv is written; the final root is atomically published after complete
config, rollback, SQLite, execution-lock, and identity validation.
"""
import base64
import ctypes
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile

import image_config_digest
import p9_validation
import readonly_sqlite

ROOT = Path('/srv/trading-platform-production')
LEGACY = Path('/opt/tw-quant')
SERVICES = ('market-api', 'execution-worker', 'gateway')
SEALED = ('data.sqlite3', 'config.tar', 'image-market-api.tar',
          'image-execution-worker.tar', 'image-gateway.tar')
AT_FDCWD = -100
RENAME_NOREPLACE = 1


class ProvisionBlocked(Exception):
    pass


def require(condition, reason):
    if not condition:
        raise ProvisionBlocked(reason)


def no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'payload-invalid')
        result[key] = value
    return result


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def run(argv, *, stdout=subprocess.PIPE, timeout=120):
    try:
        result = subprocess.run(argv, stdout=stdout, stderr=subprocess.DEVNULL,
                                timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise ProvisionBlocked('host-command-failed') from None
    require(result.returncode == 0, 'host-command-failed')
    return result.stdout


def env_of(container):
    entries = container['Config']['Env']
    require(all(isinstance(v, str) and '=' in v for v in entries), 'legacy-env-invalid')
    result = dict(v.split('=', 1) for v in entries)
    require(len(result) == len(entries), 'legacy-env-invalid')
    return result


def mapped_path(container, value):
    path = Path(value)
    require(path.is_absolute() and '..' not in path.parts, 'database-boundary')
    mounts = [m for m in container['Mounts'] if path.is_relative_to(m['Destination'])]
    require(bool(mounts), 'database-boundary')
    mount = max(mounts, key=lambda m: len(m['Destination']))
    result = Path(mount['Source']) / path.relative_to(mount['Destination'])
    require(result.is_absolute() and result.resolve() == result and
            'staging' not in str(result).lower(), 'database-boundary')
    return result


def write_file(path, data, *, uid=0, mode=0o600):
    path = Path(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(path, flags, mode)
    try:
        os.fchmod(fd, mode)
        os.fchown(fd, uid, uid if uid else 0)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            require(written > 0, 'write-failed')
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def decode_sources(payload):
    require(set(payload) == {'market.env', 'execution.env', 'gateway.env',
                             'factory', 'replay.csv'}, 'payload-invalid')
    result = {}
    for label, encoded in payload.items():
        require(isinstance(encoded, str) and
                re.fullmatch(r'[A-Za-z0-9+/=]+', encoded), 'payload-invalid')
        try:
            raw = base64.b64decode(encoded, validate=True)
        except Exception:
            raise ProvisionBlocked('payload-invalid') from None
        limit = 128 * 1024 * 1024 if label == 'replay.csv' else 1024 * 1024
        require(raw and len(raw) <= limit, 'payload-invalid')
        result[label] = raw
    return result


def current_state(pins):
    revision = run(['git', '--no-optional-locks', '-C', str(LEGACY / 'repo'),
                    'rev-parse', 'HEAD']).decode().strip()
    require(revision == pins['legacy_revision'], 'legacy-revision-mismatch')
    ids = run(['docker', 'ps', '-aq', '--no-trunc', '--filter',
               'label=com.docker.compose.project=tw-quant-lightsail']).decode().split()
    require(len(ids) == 3, 'legacy-container-mismatch')
    containers = {}
    for cid in ids:
        require(re.fullmatch(r'[0-9a-f]{64}', cid) is not None, 'legacy-container-mismatch')
        data = json.loads(run(['docker', 'inspect', cid]))[0]
        service = data['Config']['Labels'].get('com.docker.compose.service')
        require(service in SERVICES and service not in containers and
                data['State']['Running'] is True, 'legacy-container-mismatch')
        require(re.fullmatch(r'sha256:[0-9a-f]{64}', data['Image']) is not None,
                'legacy-container-mismatch')
        containers[service] = data
    market_env = env_of(containers['market-api'])
    worker_env = env_of(containers['execution-worker'])
    try:
        p9_validation.disabled(worker_env, execution=True)
        provider = p9_validation.market_compatibility(market_env, pins)
    except Exception:
        raise ProvisionBlocked('legacy-safety-gate') from None
    require(provider == 'shioaji', 'legacy-market-provider')
    health_path = mapped_path(containers['execution-worker'],
                              worker_env.get('LIVE_EXECUTION_HEALTH_PATH',
                                             '/run/tw-quant-execution/health.json'))
    try:
        health = json.loads(health_path.read_text())
    except Exception:
        raise ProvisionBlocked('legacy-safety-gate') from None
    expected = {'locked': True, 'enabled': False, 'ordering_enabled': False,
                'connected': False, 'execution_state': 'disabled',
                'recovery_status': 'locked', 'external_order_calls': 0,
                'external_cancel_calls': 0}
    require(all(type(health.get(k)) is type(v) and health.get(k) == v
                for k, v in expected.items()), 'legacy-safety-gate')
    return revision, containers, market_env, worker_env, health


def stable_database(containers, market_env, worker_env):
    market_db = market_env.get('MARKET_DB_PATH')
    worker_db = worker_env.get('LIVE_EXECUTION_DB_PATH')
    require(market_db and market_db == worker_db, 'database-boundary')
    a = mapped_path(containers['market-api'], market_db)
    b = mapped_path(containers['execution-worker'], worker_db)
    require(a == b and a.samefile(b), 'database-boundary')
    journal = a.with_name(a.name + '-journal')
    wal = a.with_name(a.name + '-wal')
    require(not journal.exists(), 'sqlite-snapshot-failed')
    def read_pair():
        return (readonly_sqlite.read_file(a),
                readonly_sqlite.read_file(wal) if wal.exists() else b'')
    first = read_pair()
    try:
        image = readonly_sqlite.committed_image(*first)
        inventory = readonly_sqlite.inventory(image)
    except Exception:
        raise ProvisionBlocked('sqlite-snapshot-failed') from None
    require(inventory.get('sqlite_integrity') == 'ok' and
            inventory.get('active_targets') == 0 and
            read_pair() == first and not journal.exists(), 'sqlite-snapshot-failed')
    return image, inventory


def image_archive(image_id, path):
    with Path(path).open('xb') as output:
        run(['docker', 'image', 'save', image_id], stdout=output, timeout=300)
        output.flush()
        os.fsync(output.fileno())
    os.chmod(path, 0o600)
    with Path(path).open('rb') as stream:
        try:
            digest = image_config_digest.config_digest(stream)
        except Exception:
            raise ProvisionBlocked('rollback-capture-failed') from None
    return digest


def legacy_config_archive(path):
    members = ('compose.env', 'market.env', 'execution.env', 'gateway.env')
    with tarfile.open(path, 'w:') as archive:
        for name in members:
            source = LEGACY / 'config' / name
            require(source.is_file() and not source.is_symlink() and
                    source.resolve() == source, 'legacy-config-invalid')
            data = source.read_bytes()
            require(len(data) <= 1024 * 1024, 'legacy-config-invalid')
            info = tarfile.TarInfo('config/' + name)
            info.size = len(data)
            info.mode = 0o600
            info.uid = info.gid = 0
            info.mtime = 0
            archive.addfile(info, io.BytesIO(data))
    os.chmod(path, 0o600)


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def publish_noreplace(source, target):
    libc = ctypes.CDLL(None, use_errno=True)
    fn = getattr(libc, 'renameat2', None)
    require(fn is not None, 'atomic-publish-unavailable')
    result = fn(AT_FDCWD, os.fsencode(source), AT_FDCWD, os.fsencode(target),
                RENAME_NOREPLACE)
    require(result == 0, 'atomic-publish-failed')


def provision(payload):
    require(isinstance(payload, dict) and
            set(payload) == {'control_sha', 'pins', 'sources'}, 'payload-invalid')
    pins = payload['pins']
    require(isinstance(pins, dict) and
            re.fullmatch(r'[0-9a-f]{40}', payload['control_sha']), 'payload-invalid')
    data = decode_sources(payload['sources'])
    require(not ROOT.exists() and not ROOT.is_symlink(), 'production-root-exists')
    lock_path = Path('/var/lock/tw-quant-deploy.lock')
    require(lock_path.is_file() and not lock_path.is_symlink(),
            'deployment-lock-unavailable')
    temp = None
    with lock_path.open('rb') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ProvisionBlocked('deployment-lock-unavailable') from None
        require(not ROOT.exists() and not ROOT.is_symlink(), 'production-root-exists')
        revision, containers, market_env, worker_env, health = current_state(pins)
        database_image, database_inventory = stable_database(
            containers, market_env, worker_env)
        temp = Path(tempfile.mkdtemp(prefix='.trading-platform-production.prepare-',
                                    dir='/srv'))
        try:
            os.chmod(temp, 0o700)
            for name in ('config', 'provider', 'rollback'):
                path = temp / name
                path.mkdir(mode=0o700)
                os.chown(path, 0, 0)
            write_file(temp / 'config/market.env', data['market.env'])
            write_file(temp / 'config/execution.env', data['execution.env'])
            write_file(temp / 'config/gateway.env', data['gateway.env'])
            write_file(temp / 'provider/factory', data['factory'], uid=10001)
            write_file(temp / 'config/replay.csv', data['replay.csv'], uid=10001)
            config_hashes = {
                'market.env': sha(temp / 'config/market.env'),
                'execution.env': sha(temp / 'config/execution.env'),
                'gateway.env': sha(temp / 'config/gateway.env'),
                'factory': sha(temp / 'provider/factory'),
                'replay.csv': sha(temp / 'config/replay.csv'),
            }
            original_root = p9_validation.ROOT
            try:
                p9_validation.ROOT = temp
                try:
                    envs = p9_validation.config_check(pins, config_hashes)
                    require(p9_validation.market_compatibility(envs['market.env'], pins) ==
                            'shioaji', 'config-validation-failed')
                except ProvisionBlocked:
                    raise
                except Exception:
                    raise ProvisionBlocked('config-validation-failed') from None
            finally:
                p9_validation.ROOT = original_root
            require(envs['market.env']['MARKET_DATA_PROVIDER'] ==
                    p9_validation.market_compatibility(market_env, pins),
                    'config-validation-failed')
            rollback = temp / 'rollback'
            write_file(rollback / 'data.sqlite3', database_image)
            require(readonly_sqlite.snapshot(rollback / 'data.sqlite3') ==
                    database_inventory, 'sqlite-snapshot-failed')
            legacy_config_archive(rollback / 'config.tar')
            image_meta = {}
            for service in SERVICES:
                container = containers[service]
                archive = rollback / ('image-' + service + '.tar')
                digest = image_archive(container['Image'], archive)
                image_meta[service] = {
                    'id': container['Id'],
                    'image_id': container['Image'],
                    'config_digest': digest,
                }
            gateway = p9_validation.parse_env(
                (LEGACY / 'config/gateway.env').read_text())
            domain = gateway.get('MARKET_DOMAIN', '')
            require(re.fullmatch(r'[a-z0-9.-]+', domain) is not None,
                    'legacy-config-invalid')
            record = LEGACY / 'deployments/current.env'
            require(record.is_file() and not record.is_symlink(),
                    'legacy-config-invalid')
            files = {name: sha(rollback / name) for name in SEALED}
            inventory = {
                'schema_version': 1,
                'revision': pins['legacy_revision'],
                'domain': domain,
                'record_sha256': sha(record),
                'containers': image_meta,
                'files': files,
            }
            write_file(rollback / 'rollback.json',
                       (json.dumps(inventory, sort_keys=True) + '\n').encode())
            rollback_sha = sha(rollback / 'rollback.json')
            original_root = p9_validation.ROOT
            try:
                p9_validation.ROOT = temp
                try:
                    checked = p9_validation.verify_backup(pins, rollback_sha)
                except Exception:
                    raise ProvisionBlocked('rollback-capture-failed') from None
            finally:
                p9_validation.ROOT = original_root
            require(checked['files'] == files, 'rollback-capture-failed')
            for directory in (temp, temp / 'config', temp / 'provider',
                              temp / 'rollback'):
                require(directory.stat().st_uid == 0 and
                        stat.S_IMODE(directory.stat().st_mode) == 0o700,
                        'owner-mode-invalid')
            for directory in (temp / 'config', temp / 'provider', temp / 'rollback', temp):
                fsync_dir(directory)
            require(not ROOT.exists() and not ROOT.is_symlink(),
                    'production-root-exists')
            publish_noreplace(temp, ROOT)
            temp = None
            fsync_dir('/srv')
            return {
                'status': 'PASS',
                'root': str(ROOT),
                'legacy_revision': revision,
                'market_provider': 'shioaji',
                'config_sha256': config_hashes,
                'rollback_sha256': rollback_sha,
                'execution_locked': True,
                'external_order_calls': health['external_order_calls'],
                'external_cancel_calls': health['external_cancel_calls'],
            }
        finally:
            if temp is not None and temp.exists():
                shutil.rmtree(temp)

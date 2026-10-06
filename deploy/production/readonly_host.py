"""Production inspection only; injected reviewed validators never run cutover.

All subprocesses are allowlisted reads. SQLite is inspected in RAM. Output is a
fixed schema of booleans, counts, identities and digests, never runtime payloads.
"""
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

import p9_validation as validation
import image_config_digest
import readonly_sqlite

ROOT = validation.ROOT
LEGACY = validation.LEGACY
REVISION = '683bb4ebc4c4980480a4786136701ff458338a14'
SERVICES = validation.SERVICES
REASONS = frozenset('''production-revision-mismatch production-record-revision
production-record-mismatch production-container-mismatch production-image-or-state-mismatch
production-image-revision legacy-security legacy-unhealthy accepted-runtime-market-capability
execution-not-disabled real-order-enabled live-confirmation-present execution-not-locked-zero-calls
stale-heartbeat production-root production-directory-isolation prior-p9-transaction
config-hashes-missing config-isolation config-digest invalid-env duplicate-or-unsafe-env
unsafe-env-interpolation factory-invalid production-auth-missing provider-config-provenance
market-credentials-missing market-credential-isolation production-market-provider-mismatch
production-data-path staging-config-reuse rollback-identity invalid-domain backup-incomplete
backup-config-members backup-config-invalid legacy-config-drift rollback-image-identity
rollback-config-digest legacy-config-digest production-domain-mismatch production-market-source-mismatch
legacy-database-boundary-mismatch mount-isolation host-command-failed readonly-command-rejected
invalid-container-id inspection-failed config-approval-missing rollback-approval-missing
sqlite-file-boundary sqlite-snapshot-too-large sqlite-wal-invalid sqlite-header-invalid
sqlite-integrity durable-lock-missing target-not-locked sqlite-journal-present
sqlite-changed-during-read backup-stale-or-different deployment-lock-unavailable
production-state-changed production-path-not-isolated'''.split())


def require(condition, code):
    validation.require(condition, code)


def read_command(argv):
    allowed = argv == ['git', '--no-optional-locks', '-C', str(LEGACY / 'repo'), 'rev-parse', 'HEAD']
    allowed |= argv == ['docker', 'ps', '-aq', '--no-trunc', '--filter',
                        'label=com.docker.compose.project=tw-quant-lightsail']
    allowed |= len(argv) == 3 and argv[:2] == ['docker', 'inspect'] and bool(re.fullmatch('[0-9a-f]{64}', argv[2]))
    allowed |= len(argv) == 4 and argv[:3] == ['docker', 'image', 'inspect'] and bool(re.fullmatch('sha256:[0-9a-f]{64}', argv[3]))
    require(allowed, 'readonly-command-rejected')
    result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            timeout=30, env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'})
    require(result.returncode == 0, 'host-command-failed')
    return result.stdout


validation.run = read_command


def env_of(container):
    entries = container['Config']['Env']
    require(all(isinstance(v, str) and '=' in v for v in entries), 'invalid-env')
    result = dict(v.split('=', 1) for v in entries)
    require(len(result) == len(entries), 'duplicate-or-unsafe-env')
    return result


def mapped_path(container, name):
    path = Path(name)
    require(path.is_absolute() and '..' not in path.parts, 'mount-isolation')
    matches = [m for m in container['Mounts'] if path.is_relative_to(m['Destination'])]
    require(bool(matches), 'mount-isolation')
    mount = max(matches, key=lambda m: len(m['Destination']))
    source = Path(mount['Source']) / path.relative_to(mount['Destination'])
    require(source.is_absolute() and source.resolve() == source and
            'staging' not in str(source).lower(), 'mount-isolation')
    return source


def running_containers():
    ids = read_command(['docker', 'ps', '-aq', '--no-trunc', '--filter',
                        'label=com.docker.compose.project=tw-quant-lightsail']).decode().split()
    require(len(ids) == 3, 'production-container-mismatch')
    containers = {}
    for cid in ids:
        d = validation.inspect(cid)
        service = d['Config']['Labels'].get('com.docker.compose.service')
        require(service in SERVICES and service not in containers and d['State']['Running'] is True,
                'production-container-mismatch')
        require(d['Id'] == cid and re.fullmatch('sha256:[0-9a-f]{64}', d['Image']),
                'production-image-or-state-mismatch')
        containers[service] = d
    return containers


def execution_safety(containers):
    for service, d in containers.items():
        validation.disabled(env_of(d), execution=service == 'execution-worker')
        require(d['HostConfig']['ReadonlyRootfs'] is True and
                'ALL' in d['HostConfig']['CapDrop'] and
                'no-new-privileges:true' in d['HostConfig']['SecurityOpt'], 'legacy-security')
        require(all('staging' not in m['Source'].lower() for m in d['Mounts']), 'mount-isolation')
        if service != 'gateway':
            require(d['State'].get('Health', {}).get('Status') == 'healthy', 'legacy-unhealthy')
    worker = containers['execution-worker']
    env = env_of(worker)
    health = json.loads(mapped_path(worker, env.get('LIVE_EXECUTION_HEALTH_PATH',
                     '/run/tw-quant-execution/health.json')).read_text())
    expected = {'locked': True, 'enabled': False, 'ordering_enabled': False,
                'connected': False, 'execution_state': 'disabled', 'recovery_status': 'locked',
                'external_order_calls': 0, 'external_cancel_calls': 0}
    require(all(type(health.get(k)) is type(v) and health[k] == v for k, v in expected.items()),
            'execution-not-locked-zero-calls')
    if 'broker_name' in health:
        require(health['broker_name'] == 'disabled', 'execution-not-disabled')
    heartbeat = datetime.fromisoformat(health['heartbeat_at'].replace('Z', '+00:00'))
    require(heartbeat.tzinfo is not None and
            0 <= (datetime.now(timezone.utc) - heartbeat).total_seconds() <= 30 and
            heartbeat >= datetime.fromisoformat(worker['State']['StartedAt'].replace('Z', '+00:00')),
            'stale-heartbeat')
    return {'real_order_disabled': True, 'canary_disabled': True, 'auto_disabled': True,
            'guardian_disabled': True, 'broker_read_only_disabled': True,
            'broker_provider': 'disabled', 'live_trading_enabled': False,
            'live_confirmation_empty': True, 'external_order_calls': 0, 'external_cancel_calls': 0}


def database(containers):
    market, worker = containers['market-api'], containers['execution-worker']
    a, b = env_of(market).get('MARKET_DB_PATH'), env_of(worker).get('LIVE_EXECUTION_DB_PATH')
    require(a and a == b, 'legacy-database-boundary-mismatch')
    market_path, worker_path = mapped_path(market, a), mapped_path(worker, b)
    require(market_path == worker_path and market_path.samefile(worker_path),
            'legacy-database-boundary-mismatch')
    return worker_path


def image_digest(image_id):
    require(bool(re.fullmatch('sha256:[0-9a-f]{64}', image_id)), 'rollback-image-identity')
    # Stream existing image bytes into RAM; no archive file or daemon mutation.
    process = subprocess.Popen(['docker', 'image', 'save', image_id],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        digest = image_config_digest.config_digest(process.stdout)
        require(process.wait(timeout=30) == 0, 'host-command-failed')
        return digest
    finally:
        process.stdout.close()
        if process.poll() is None:
            process.kill()
            process.wait()


def initial_state(pins, result):
    # The first host command must observe Legacy revision, before any config/backup read.
    revision = read_command(['git', '--no-optional-locks', '-C', str(LEGACY / 'repo'),
                             'rev-parse', 'HEAD']).decode().strip()
    if re.fullmatch('[0-9a-f]{40}', revision):
        result['legacy_revision'] = revision
    require(revision == REVISION == pins['legacy_revision'], 'production-revision-mismatch')
    containers = running_containers()
    market = containers['market-api']
    market_env = env_of(market)
    provider = market_env.get('MARKET_DATA_PROVIDER', market_env.get('MARKET_MODE', 'mock')).lower().strip()
    result['market_provider'] = provider if provider in ('mock', 'replay', 'shioaji') else 'unsupported'
    result['market_provider_source'] = 'running-container-environment'
    validation.market_compatibility(market_env, pins)
    return containers, market_env


def inspect_host(pins, hashes, rollback_hash, result):
    containers, market_env = initial_state(pins, result)
    market = containers['market-api']
    # SDK capability must be proven by approved P8 before config/rollback reads.
    for service, d in containers.items():
        image = json.loads(read_command(['docker', 'image', 'inspect', d['Image']]))[0]
        if service != 'gateway':
            require(image['Config']['Labels'].get('org.opencontainers.image.revision') == REVISION,
                    'production-image-revision')
    require(set(hashes) == {'market.env', 'execution.env', 'gateway.env', 'factory', 'replay.csv'} and
            all(isinstance(v, str) and re.fullmatch('[0-9a-f]{64}', v) for v in hashes.values()),
            'config-approval-missing')
    require(bool(re.fullmatch('[0-9a-f]{64}', rollback_hash)), 'rollback-approval-missing')
    directory_check()
    envs = validation.config_check(pins, hashes)
    provider = validation.market_compatibility(market_env, pins)
    require(provider == validation.market_compatibility(envs['market.env'], pins) or
            {provider, envs['market.env']['MARKET_DATA_PROVIDER']} <= {'mock', 'replay'},
            'production-market-provider-mismatch')
    result['config_sha256'] = {name: validation.file_sha(ROOT / ('provider/factory' if name == 'factory'
                             else 'config/' + name)) for name in hashes}
    result['execution'] = execution_safety(containers)
    current_path = database(containers)
    replay_path = None
    if provider in ('mock', 'replay'):
        replay_path = mapped_path(market, market_env['MARKET_REPLAY_CSV'])
        require(validation.file_sha(replay_path) == hashes['replay.csv'], 'production-market-source-mismatch')
    current = readonly_sqlite.snapshot(current_path)
    result['sqlite'] = {'integrity_check': 'ok', 'all_targets_locked': True, 'active_targets': 0,
                        'target_count': current['targets'], 'db_boundary_consistent': True}
    backup = validation.verify_backup(pins, rollback_hash)
    record = LEGACY / 'deployments/current.env'
    require(validation.file_sha(record) == backup['record_sha256'], 'production-record-mismatch')
    values = validation.parse_env('\n'.join(k.upper() + '=' + v for k, v in
                                   (line.split('=', 1) for line in record.read_text().splitlines())))
    require(values['DEPLOYED_SHA'] == REVISION, 'production-record-revision')
    require(envs['gateway.env']['MARKET_DOMAIN'] == backup['domain'], 'production-domain-mismatch')
    sealed_path = ROOT / 'rollback/data.sqlite3'
    require(not sealed_path.with_name('data.sqlite3-wal').exists() and
            not sealed_path.with_name('data.sqlite3-shm').exists(), 'backup-stale-or-different')
    require(readonly_sqlite.snapshot(sealed_path) == current, 'backup-stale-or-different')
    identities = {}
    for service, d in containers.items():
        approved = backup['containers'][service]
        require(d['Id'] == approved['id'] and d['Image'] == approved['image_id'], 'production-container-mismatch')
        with (ROOT / 'rollback' / ('image-' + service + '.tar')).open('rb') as stream:
            require(image_config_digest.config_digest(stream) == approved['config_digest'], 'rollback-config-digest')
        require(image_digest(d['Image']) == approved['config_digest'], 'legacy-config-digest')
        identities[service] = {k: approved[k] for k in ('id', 'image_id', 'config_digest')}
    # Approvals and sealed rollback must never point at current mutable data.
    require(not sealed_path.samefile(current_path) and
            (replay_path is None or not replay_path.samefile(ROOT / 'config/replay.csv')),
            'production-path-not-isolated')
    require(all(not (ROOT / 'config' / name).samefile(LEGACY / 'config' / name)
                for name in ('market.env', 'execution.env', 'gateway.env')), 'production-path-not-isolated')
    after = running_containers()
    require(all(after[s]['Id'] == containers[s]['Id'] and after[s]['Image'] == containers[s]['Image'] and
                after[s]['State']['StartedAt'] == containers[s]['State']['StartedAt'] for s in SERVICES),
            'production-state-changed')
    execution_safety(after)
    require(readonly_sqlite.snapshot(current_path) == current, 'sqlite-changed-during-read')
    validation.config_check(pins, hashes)
    validation.verify_backup(pins, rollback_hash)
    require(readonly_sqlite.snapshot(sealed_path) == current, 'backup-stale-or-different')
    require(validation.file_sha(record) == backup['record_sha256'] and
            validation.file_sha(ROOT / 'rollback/rollback.json') == rollback_hash, 'production-state-changed')
    result['rollback'] = {'verified': True, 'rollback_json_sha256': rollback_hash,
                          'record_sha256': backup['record_sha256'], 'containers': identities,
                          'files_sha256': {name: backup['files'][name] for name in sorted(backup['files'])}}
    result['isolation'] = {'production_staging_hosts': True, 'production_staging_paths': True,
                           'production_configs_independent': True}


def directory_check():
    require(os.geteuid() == 0 and ROOT.resolve() == ROOT and LEGACY.resolve() == LEGACY, 'production-root')
    for directory in (ROOT, ROOT / 'config', ROOT / 'provider', ROOT / 'rollback'):
        require(directory.is_dir() and not directory.is_symlink() and directory.stat().st_uid == 0 and
                directory.stat().st_mode & 0o777 == 0o700, 'production-directory-isolation')
    require(not (ROOT / 'acceptance.json').exists() and not (ROOT / 'transaction.json').exists(), 'prior-p9-transaction')


def collect(pins, hashes, rollback_hash):
    result = {'schema_version': 1, 'P9_PREREQUISITE': 'BLOCKED', 'reason': 'inspection-failed'}
    try:
        # Classify revision/provider before even requiring a deploy lock or P9 files.
        # Repeat under the existing lock before the rest of the inspection.
        initial_state(pins, result)
        # Open existing deploy lock read-only; never create a flock file.
        lock_path = Path('/var/lock/tw-quant-deploy.lock')
        require(lock_path.is_file() and not lock_path.is_symlink(), 'deployment-lock-unavailable')
        with lock_path.open('rb') as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
            except OSError:
                raise ValueError('deployment-lock-unavailable') from None
            inspect_host(pins, hashes, rollback_hash, result)
        result.update(P9_PREREQUISITE='PASS', reason='all-prerequisites-satisfied')
    except Exception as exc:
        code = str(exc) if isinstance(exc, ValueError) else ''
        result['reason'] = code if code in REASONS else 'inspection-failed'
    return result

"""Production host transaction: retained Legacy rollback, isolated P9 data, no build.

Preflight can run from stdin and only reads host state. No helper from Staging
or previously installed P9 code is trusted for the preflight decision.
"""
import argparse
import base64
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tarfile
import time

ROOT = Path('/srv/trading-platform-p9')
LEGACY = Path('/opt/tw-quant')
SERVICES = ('market-api', 'execution-worker', 'gateway')
DISABLED = ('LIVE_TRADING_ENABLED', 'LIVE_CANARY_ENABLED', 'LIVE_AUTO_ENABLED',
            'LIVE_POSITION_GUARDIAN_ENABLED', 'LIVE_BROKER_READ_ONLY_ENABLED',
            'LIVE_SHADOW_ENABLED', 'LIVE_AUTO_PRODUCTION_ACCEPTANCE_PASSED')


def require(condition, code):
    if not condition:
        raise ValueError(code)


def run(argv, data=None, timeout=30):
    # Never expose command output/config/env/credentials on failures.
    result = subprocess.run(argv, input=data, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout)
    require(result.returncode == 0, 'host-command-failed')
    return result.stdout


def file_sha(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def parse_env(text):
    result = {}
    for line in text.splitlines():
        if not line or line.startswith('#'):
            continue
        require('=' in line, 'invalid-env')
        k, v = line.split('=', 1)
        require(re.fullmatch('[A-Z][A-Z0-9_]*', k) is not None and k not in result, 'duplicate-or-unsafe-env')
        require(not any(c in v for c in ('\r', '\x00', '$', '`', '"', "'")), 'unsafe-env-interpolation')
        result[k] = v
    return result


def disabled(env, *, execution=False):
    require(all(env.get(k, 'false') == 'false' for k in DISABLED), 'real-order-enabled')
    require(not env.get('LIVE_TRADING_CONFIRMATION', ''), 'live-confirmation-present')
    if execution:
        require(env.get('BROKER_PROVIDER') == 'disabled' and env.get('LIVE_TRADING_ENABLED') == 'false',
                'execution-not-disabled')


def protected(path, digest=None, uid=0):
    require(path.is_file() and not path.is_symlink() and path.resolve() == path and
            path.stat().st_uid == uid and path.stat().st_mode & 0o777 in (0o400, 0o600), 'config-isolation')
    if digest:
        require(re.fullmatch('[0-9a-f]{64}', digest) and file_sha(path) == digest, 'config-digest')


def inspect(cid):
    require(re.fullmatch('[0-9a-f]{64}', cid) is not None, 'invalid-container-id')
    return json.loads(run(['docker', 'inspect', cid]))[0]


def worker_locked(cid, *, legacy=False):
    run(['docker', 'exec', cid, 'python', '-m', 'tw_quant.execution_service', 'healthcheck'])
    health = json.loads(run(['docker', 'exec', cid, 'cat', '/run/tw-quant-execution/health.json']))
    expected = {'locked': True, 'enabled': False, 'ordering_enabled': False, 'connected': False,
                'execution_state': 'disabled', 'recovery_status': 'locked', 'broker_name': 'disabled',
                'external_order_calls': 0, 'external_cancel_calls': 0}
    if legacy:
        expected.pop('broker_name')
    require(all(type(health.get(k)) is type(v) and health[k] == v for k, v in expected.items()),
            'execution-not-locked-zero-calls')
    started = inspect(cid)['State']['StartedAt']
    heartbeat = datetime.fromisoformat(health['heartbeat_at'].replace('Z', '+00:00'))
    require(heartbeat.tzinfo is not None and
            0 <= (datetime.now(timezone.utc) - heartbeat).total_seconds() <= 30 and
            heartbeat >= datetime.fromisoformat(started.replace('Z', '+00:00')), 'stale-heartbeat')
    # Generation healthcheck above validates heartbeat against current process.
    return {**expected, 'generation': health.get('generation'), 'heartbeat_at': health['heartbeat_at']}


def legacy_state(pins, rollback, *, stopped=False, recovering=False):
    require(run(['git', '-C', str(LEGACY / 'repo'), 'rev-parse', 'HEAD']).decode().strip() == pins['legacy_revision'],
            'production-revision-mismatch')
    record = LEGACY / 'deployments/current.env'
    require(file_sha(record) == rollback['record_sha256'], 'production-record-mismatch')
    values = parse_env('\n'.join(k.upper() + '=' + v for k, v in (line.split('=', 1) for line in record.read_text().splitlines())))
    require(values['DEPLOYED_SHA'] == pins['legacy_revision'], 'production-record-revision')
    current = {}
    ids = run(['docker', 'ps', '-aq', '--no-trunc', '--filter', 'label=com.docker.compose.project=tw-quant-lightsail']).decode().split()
    require(set(ids) == {rollback['containers'][s]['id'] for s in SERVICES}, 'production-container-mismatch')
    for service in SERVICES:
        approved = rollback['containers'][service]
        d = inspect(approved['id'])
        require(d['Image'] == approved['image_id'] and
                d['Config']['Labels']['com.docker.compose.service'] == service and
                (recovering or d['State']['Running'] is (not stopped)), 'production-image-or-state-mismatch')
        image = json.loads(run(['docker', 'image', 'inspect', approved['image_id']]))[0]
        if service != 'gateway':
            require(image['Config']['Labels'].get('org.opencontainers.image.revision') == pins['legacy_revision'],
                    'production-image-revision')
        env = dict(v.split('=', 1) for v in d['Config']['Env'])
        disabled(env)
        if service == 'execution-worker':
            require(env.get('LIVE_TRADING_ENABLED') == 'false', 'legacy-live-enabled')
        require(d['HostConfig']['ReadonlyRootfs'] is True and 'ALL' in d['HostConfig']['CapDrop'] and
                'no-new-privileges:true' in d['HostConfig']['SecurityOpt'], 'legacy-security')
        if not stopped and not recovering and service != 'gateway':
            require(d['State'].get('Health', {}).get('Status') == 'healthy', 'legacy-unhealthy')
        current[service] = {'id': d['Id'], 'image_id': d['Image'], 'started': d['State']['StartedAt']}
    if not stopped and not recovering:
        worker_locked(current['execution-worker']['id'], legacy=True)
        require(run(['curl', '--fail', '--silent', '--show-error', '--max-time', '10', '--resolve',
                     rollback['domain'] + ':443:127.0.0.1', 'https://' + rollback['domain'] + '/healthz']) == b'ok',
                'legacy-gateway-health')
    return current


def verify_backup(pins, expected):
    path = ROOT / 'rollback/rollback.json'
    protected(path, expected)
    d = json.loads(path.read_text())
    require(d['schema_version'] == 1 and d['revision'] == pins['legacy_revision'] and
            set(d['containers']) == set(SERVICES), 'rollback-identity')
    require(re.fullmatch('[a-z0-9.-]+', d['domain']) is not None, 'invalid-domain')
    # Root-only sealed inventory of artifacts, config and database; original mounts retained.
    required = {'data.sqlite3', 'config.tar', *('image-' + s + '.tar' for s in SERVICES)}
    require(set(d['files']) == required, 'backup-incomplete')
    for name, digest in d['files'].items():
        protected(ROOT / 'rollback' / name, digest)
    with tarfile.open(ROOT / 'rollback/config.tar', 'r:') as archive:
        names = archive.getnames()
        require(sorted(names) == ['config/compose.env', 'config/execution.env', 'config/gateway.env', 'config/market.env'], 'backup-config-members')
        for member in archive.getmembers():
            require(member.isfile() and member.size <= 1024 * 1024, 'backup-config-invalid')
            path = LEGACY / member.name
            require(path.resolve() == path and not path.is_symlink() and
                    archive.extractfile(member).read() == path.read_bytes(), 'legacy-config-drift')
    for service in SERVICES:
        require(re.fullmatch('sha256:[0-9a-f]{64}', d['containers'][service]['image_id']) is not None and
                re.fullmatch('sha256:[0-9a-f]{64}', d['containers'][service]['config_digest']) is not None,
                'rollback-image-identity')
    return d


def config_check(pins, hashes):
    require(set(hashes) == {'market.env', 'execution.env', 'gateway.env', 'factory', 'replay.csv'}, 'config-hashes-missing')
    envs = {}
    for name in ('market.env', 'execution.env', 'gateway.env'):
        path = ROOT / 'config' / name
        protected(path, hashes[name])
        envs[name] = parse_env(path.read_text())
        disabled(envs[name], execution=name == 'execution.env')
    factory = ROOT / 'provider/factory'
    protected(factory, hashes['factory'], uid=10001)
    require(re.fullmatch('[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*\n?', factory.read_text()), 'factory-invalid')
    protected(ROOT / 'config/replay.csv', hashes['replay.csv'], uid=10001)
    market = envs['market.env']
    require(market.get('MARKET_DATA_PROVIDER') == 'replay' and
            market.get('MARKET_REPLAY_CSV') == '/run/production-market/replay.csv', 'accepted-runtime-market-capability')
    require(market.get('PLATFORM_ENVIRONMENT') == 'production' and
            market.get('PLATFORM_AUTHORIZATION_MODE') == 'enforced' and
            market.get('MARKET_ACCESS_MODE') == 'cloudflare', 'production-auth-missing')
    require(market.get('PRIVATE_PROVIDER_WHEEL_SHA256') == pins['manifest']['private_provider']['wheel_sha256'],
            'provider-config-provenance')
    require(market.get('MARKET_DB_PATH') == '/data/platform.sqlite3' and
            envs['execution.env'].get('LIVE_EXECUTION_DB_PATH') == '/data/platform.sqlite3' and
            envs['execution.env'].get('LIVE_EXECUTION_HEALTH_PATH') == '/run/tw-quant-execution/health.json', 'production-data-path')
    for env in envs.values():
        require(not any('staging' in v.lower() or 'p8-synthetic' in v.lower() for v in env.values()), 'staging-config-reuse')
    return envs


def snapshot(container, code):
    return json.loads(run(['docker', 'exec', '-i', container, 'python', '-'], code))


def market_compatibility(env):
    provider = env.get('MARKET_DATA_PROVIDER', env.get('MARKET_MODE', 'mock')).lower().strip()
    require(provider in ('mock', 'replay'), 'accepted-runtime-market-capability')


def preflight(pins, expected_rollback, hashes, durable_code):
    require(os.geteuid() == 0 and ROOT.resolve() == ROOT and LEGACY.resolve() == LEGACY, 'production-root')
    for directory in (ROOT, ROOT / 'config', ROOT / 'provider', ROOT / 'rollback'):
        require(directory.is_dir() and not directory.is_symlink() and directory.stat().st_uid == 0 and
                directory.stat().st_mode & 0o777 == 0o700, 'production-directory-isolation')
    require(not (ROOT / 'acceptance.json').exists() and not (ROOT / 'transaction.json').exists(), 'prior-p9-transaction')
    config_check(pins, hashes)
    backup = verify_backup(pins, expected_rollback)
    state = legacy_state(pins, backup)
    market = inspect(state['market-api']['id'])
    market_env = dict(v.split('=', 1) for v in market['Config']['Env'])
    market_compatibility(market_env)
    worker_env = dict(v.split('=', 1) for v in inspect(state['execution-worker']['id'])['Config']['Env'])
    require(market_env.get('MARKET_DB_PATH') == worker_env.get('LIVE_EXECUTION_DB_PATH') and
            market_env.get('MARKET_DB_PATH'), 'legacy-database-boundary-mismatch')
    # Preserve the approved existing Production replay bytes; never substitute Staging fixtures.
    probe = b"import hashlib,os; from pathlib import Path; print(hashlib.sha256(Path(os.environ['MARKET_REPLAY_CSV']).read_bytes()).hexdigest())"
    replay_hash = run(['docker', 'exec', '-i', state['market-api']['id'], 'python', '-'], probe).decode().strip()
    require(replay_hash == hashes['replay.csv'], 'production-market-source-mismatch')
    durable = snapshot(state['execution-worker']['id'], durable_code)
    # Compare every table to a fresh sealed backup; fail on lost user/order/strategy state.
    backup_state = json.loads(run(['python3', '-c', durable_code.decode(), str(ROOT / 'rollback/data.sqlite3')]))
    require(durable == backup_state, 'backup-stale-or-different')
    require(parse_env((ROOT / 'config/gateway.env').read_text()).get('MARKET_DOMAIN') == backup['domain'], 'production-domain-mismatch')
    for service in SERVICES:
        with (ROOT / 'rollback' / ('image-' + service + '.tar')).open('rb') as f:
            # The helper is delivered from reviewed runner bytes, executed in-memory.
            from image_config_digest import config_digest
            require(config_digest(f) == backup['containers'][service]['config_digest'], 'rollback-config-digest')
        process = subprocess.Popen(['docker', 'image', 'save', backup['containers'][service]['image_id']],
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            require(config_digest(process.stdout) == backup['containers'][service]['config_digest'], 'legacy-config-digest')
            require(process.wait(timeout=30) == 0, 'legacy-image-save')
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.kill(); process.wait()
    return backup, state, durable


def compose():
    # All interpolation comes from reviewed/config-digest-checked files, never shell overrides.
    clean = ['env']
    for name in ('PRODUCTION_RUNTIME_IMAGE', 'PRODUCTION_GATEWAY_IMAGE', 'PRODUCTION_MARKET_ENV_FILE',
                 'PRODUCTION_EXECUTION_ENV_FILE', 'PRODUCTION_GATEWAY_ENV_FILE', 'COMPOSE_FILE', 'COMPOSE_PROJECT_NAME'):
        clean += ['-u', name]
    return clean + ['docker', 'compose', '--env-file', str(ROOT / 'release.env'), '-f', str(ROOT / 'bundle/docker-compose.yml')]


def local_image(image, pins, *, gateway=False):
    d = json.loads(run(['docker', 'image', 'inspect', image['ref']]))[0]
    canonical = image['ref'].split(':', 1)[0] + '@' + image['digest']
    require(canonical in d['RepoDigests'], 'registry-digest-mismatch')
    from image_config_digest import config_digest
    process = subprocess.Popen(['docker', 'image', 'save', image['ref']], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        require(config_digest(process.stdout) == image['config_digest'], 'image-config-digest-mismatch')
        require(process.wait(timeout=30) == 0, 'image-save-failed')
    finally:
        process.stdout.close()
        if process.poll() is None:
            process.kill(); process.wait()
    labels = d['Config']['Labels']
    expected = {'org.opencontainers.image.revision': pins['platform_sha'], 'io.tw-quant.pipeline.revision': pins['platform_sha']}
    if not gateway:
        m = pins['manifest']
        expected.update({'io.tw-quant.configuration.identity': m['images']['releases'][pins['release']]['configuration_identity'],
                         'io.tw-quant.core.sha256': m['core']['wheel_sha256'],
                         'io.tw-quant.private-provider.sha256': m['private_provider']['wheel_sha256'],
                         'io.tw-quant.p7.acceptance': m['private_provider']['p7_acceptance_sha']})
    require(all(labels.get(k) == v for k, v in expected.items()), 'image-provenance-mismatch')
    return d['Id']


def healthy(cid):
    for _ in range(45):
        d = inspect(cid)
        status = d['State'].get('Health', {}).get('Status')
        if status == 'healthy' and d['State']['Running'] is True:
            return d
        require(status not in ('unhealthy', None) and d['State']['Status'] not in ('exited', 'dead'), 'health-terminal-failure')
        time.sleep(2)
    raise ValueError('health-timeout')


def verify(pins, durable_code):
    m = pins['manifest']['images']
    runtime = m['releases'][pins['release']]['runtime']
    ids = {s: local_image(m['gateway'] if s == 'gateway' else runtime, pins, gateway=s == 'gateway') for s in SERVICES}
    containers = {}
    for service in SERVICES:
        cid = run(compose() + ['ps', '-q', service]).decode().strip()
        d = healthy(cid)
        require(d['Image'] == ids[service], 'running-image-mismatch')
        disabled(dict(v.split('=', 1) for v in d['Config']['Env']), execution=service == 'execution-worker')
        require(d['Config']['User'] == ('10000:10000' if service == 'gateway' else '10001:10001') and
                d['HostConfig']['ReadonlyRootfs'] is True and 'ALL' in d['HostConfig']['CapDrop'] and
                'no-new-privileges:true' in d['HostConfig']['SecurityOpt'], 'runtime-security')
        require(d['HostConfig']['CapAdd'] in (None, [], ['NET_BIND_SERVICE']) and
                (service == 'gateway' or not d['HostConfig']['CapAdd']), 'runtime-capabilities')
        require(all('/staging' not in mount['Source'] and 'trading-platform-staging' not in mount['Source'] and
                    not mount['Destination'].startswith('/run/live-secrets') for mount in d['Mounts']), 'mount-isolation')
        if service == 'execution-worker':
            require(d['HostConfig']['NetworkMode'] == 'none' and not d['NetworkSettings']['Ports'], 'worker-network')
        containers[service] = {'id': cid, 'image_id': d['Image'], 'started': d['State']['StartedAt'], 'restarts': d['RestartCount']}
    execution = worker_locked(containers['execution-worker']['id'])
    durable = snapshot(containers['execution-worker']['id'], durable_code)
    gateway = containers['gateway']['id']
    domain = parse_env((ROOT / 'config/gateway.env').read_text())['MARKET_DOMAIN']
    require(re.fullmatch('[a-z0-9.-]+', domain) is not None, 'invalid-domain')
    require(run(['docker', 'exec', gateway, 'curl', '--fail', '--silent', '--show-error', '--max-time', '10',
                 '--resolve', domain + ':443:127.0.0.1', 'https://' + domain + '/healthz']) == b'ok', 'gateway-health')
    # Enforced auth must still block the protected backend and static document.
    for path in ('/api/auth/me', '/'):
        code = run(['docker', 'exec', gateway, 'curl', '--silent', '--show-error', '--max-time', '10',
                    '--resolve', domain + ':443:127.0.0.1', '--output', '/dev/null', '--write-out', '%{http_code}',
                    'https://' + domain + path]).decode()
        require(code in ('401', '403'), 'production-auth-bypass')
    return {'containers': containers, 'execution': execution, 'durable': durable,
            'live_reconciliation': 'not-applicable-disabled'}


def continuity(before, after):
    require(before['durable']['durable'] == after['durable']['durable'], 'restart-durable-drift')
    for service in SERVICES:
        a, b = before['containers'][service], after['containers'][service]
        require(a['id'] == b['id'] and a['image_id'] == b['image_id'] and a['restarts'] == b['restarts'] and
                b['started'] > a['started'], 'restart-image-or-process-drift')
    require(before['execution']['generation'] != after['execution']['generation'], 'restart-generation-unchanged')


def write_json(path, document):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as f:
        os.chmod(temporary, 0o600)
        f.write(json.dumps(document, sort_keys=True) + '\n'); f.flush(); os.fsync(f.fileno())
    temporary.replace(path)


def rollback(pins, backup, durable_code):
    # No git checkout, compose up, image rebuild or mutable tag during rollback.
    # Legacy containers, env and data were retained intact, so start exact IDs.
    p9_ids = run(['docker', 'ps', '-aq', '--no-trunc', '--filter', 'label=com.docker.compose.project=platform-p9-production']).decode().split()
    for cid in p9_ids:
        require(re.fullmatch('[0-9a-f]{64}', cid), 'invalid-p9-container')
        run(['docker', 'stop', '--time', '30', cid], timeout=60)
    state = legacy_state(pins, backup, recovering=True)
    for service in ('market-api', 'execution-worker', 'gateway'):
        run(['docker', 'start', state[service]['id']], timeout=90)
    for service in ('market-api', 'execution-worker'):
        healthy(state[service]['id'])
    restored = legacy_state(pins, backup)
    durable = snapshot(restored['execution-worker']['id'], durable_code)
    journal = json.loads((ROOT / 'transaction.json').read_text())
    require(durable['durable'] == journal['durable']['durable'], 'rollback-durable-drift')
    (ROOT / 'acceptance.json').unlink(missing_ok=True)
    write_json(ROOT / 'rollback-result.json', {'rollback': 'PASS', 'revision': pins['legacy_revision'],
        'containers': restored, 'durable': durable})


def transaction(backend):
    """First failure stops forward progress; independent recovery remains available."""
    backend.preflight()
    backend.preserve()
    try:
        backend.stop_legacy()
        backend.deploy()
        before = backend.verify()
        backend.restart()
        after = backend.verify()
        continuity(before, after)
        backend.public_verify()
        backend.commit(before, after)
    except BaseException as exc:
        try:
            backend.failure(exc)
        finally:
            backend.rollback()
        raise


class Host:
    def __init__(self, pins, rollback_digest, hashes, durable_code, gate):
        self.pins, self.rollback_digest, self.hashes, self.durable_code, self.gate = pins, rollback_digest, hashes, durable_code, gate

    def preflight(self):
        require(set(self.gate['master_gates']) == {self.pins['platform_sha'], self.gate['control_sha']} and
                all(set(g) == {'CI', 'Security'} for g in self.gate['master_gates'].values()), 'master-gates-missing')
        self.backup, self.legacy, self.durable = preflight(self.pins, self.rollback_digest, self.hashes, self.durable_code)
        require(self.gate['platform_sha'] == self.pins['platform_sha'] and
                self.gate['candidate_run_id'] == self.pins['candidate_run_id'] and
                self.gate['manifest_sha256'] == self.pins['manifest_sha256'], 'prehost-gate-mismatch')

    def preserve(self):
        # Durable recovery journal is written before stopping any container.
        write_json(ROOT / 'transaction.json', {'status': 'PREPARED', 'rollback_sha256': self.rollback_digest,
            'legacy_revision': self.pins['legacy_revision'], 'legacy_containers': self.legacy, 'durable': self.durable, 'gate': self.gate})

    def stop_legacy(self):
        for service in reversed(SERVICES):
            run(['docker', 'stop', '--time', '30', self.legacy[service]['id']], timeout=60)
        legacy_state(self.pins, self.backup, stopped=True)
        # Catch writes between backup/preflight and quiescence before accepting any data loss.
        worker = inspect(self.legacy['execution-worker']['id'])
        data = [m for m in worker['Mounts'] if m['Destination'] == '/data']
        require(len(data) == 1, 'legacy-data-mount')
        env = dict(v.split('=', 1) for v in worker['Config']['Env'])
        relative = Path(env['LIVE_EXECUTION_DB_PATH'])
        require(relative.is_absolute() and relative.parts[:2] == ('/', 'data') and len(relative.parts) == 3 and
                relative.name not in ('.', '..'), 'legacy-database-path')
        database = Path(data[0]['Source']) / relative.name
        current = json.loads(run(['python3', '-c', self.durable_code.decode(), str(database)]))
        require(current == self.durable, 'quiesced-backup-drift')

    def deploy(self):
        # Every candidate path must be newly created; no retained or Staging state.
        import shutil
        for name, uid in [('data', 10001), ('health', 10001), ('gateway-data', 10000), ('gateway-config', 10000)]:
            path = ROOT / name
            path.mkdir(mode=0o750, exist_ok=False); os.chown(path, uid, uid)
        gateway = inspect(self.legacy['gateway']['id'])
        for destination, name in [('/data', 'gateway-data'), ('/config', 'gateway-config')]:
            mounts = [m for m in gateway['Mounts'] if m['Destination'] == destination]
            require(len(mounts) == 1 and mounts[0]['Type'] == 'volume', 'legacy-gateway-volume')
            source = Path(mounts[0]['Source'])
            require(source.is_dir() and source.resolve() == source and
                    not any(p.is_symlink() for p in source.rglob('*')), 'gateway-volume-symlink')
            shutil.copytree(source, ROOT / name, dirs_exist_ok=True)
            for path in (ROOT / name).rglob('*'):
                require(path.is_file() or path.is_dir(), 'gateway-volume-special-file')
                os.chown(path, 10000, 10000)
        shutil.copyfile(ROOT / 'rollback/data.sqlite3', ROOT / 'data/platform.sqlite3')
        os.chown(ROOT / 'data/platform.sqlite3', 10001, 10001); os.chmod(ROOT / 'data/platform.sqlite3', 0o600)
        m = self.pins['manifest']['images']
        runtime = m['releases'][self.pins['release']]['runtime']
        for image, gateway in [(runtime, False), (m['gateway'], True)]:
            run(['docker', 'pull', image['ref']], timeout=600)
            local_image(image, self.pins, gateway=gateway)
        values = {'PRODUCTION_RUNTIME_IMAGE': runtime['ref'], 'PRODUCTION_GATEWAY_IMAGE': m['gateway']['ref'],
                  **{'PRODUCTION_' + n.upper() + '_ENV_FILE': str(ROOT / 'config' / (n + '.env')) for n in ('market', 'execution', 'gateway')}}
        (ROOT / 'release.env').write_text(''.join(k + '=' + v + '\n' for k, v in values.items())); os.chmod(ROOT / 'release.env', 0o600)
        run(compose() + ['config', '--quiet'])
        run(compose() + ['run', '--rm', '--no-deps', '--no-build', '-T', 'market-api', 'python', '-c',
                        'from tw_quant.live.settings import LiveSettings; LiveSettings.from_env().validate()'], timeout=60)
        run(compose() + ['run', '--rm', '--no-deps', '--no-build', '-T', 'execution-worker', 'python', '-m',
                        'tw_quant.execution_service', 'validate'], timeout=60)
        # TLS/auth production overlay preserves the exact gateway image bytes.
        run(compose() + ['run', '--rm', '--no-deps', '--no-build', '-T', 'gateway', 'caddy', 'validate',
                        '--config', '/etc/caddy/Caddyfile', '--adapter', 'caddyfile'], timeout=60)
        run(compose() + ['up', '--no-build', '--pull', 'never', '--detach'], timeout=180)

    def verify(self):
        config_check(self.pins, self.hashes)
        result = verify(self.pins, self.durable_code)
        require(result['durable']['durable'] == self.durable['durable'], 'cutover-durable-drift')
        return result

    def restart(self):
        run(compose() + ['restart', *SERVICES], timeout=120)

    def public_verify(self):
        url = 'https://' + self.backup['domain'] + '/healthz'
        # No retries after a first public invariant failure.
        output = run(['curl', '--fail', '--silent', '--show-error', '--max-time', '15', '--dump-header', '-', url])
        headers, body = output.rsplit(b'\r\n\r\n', 1)
        require(body == b'ok' and b'strict-transport-security:' in headers.lower() and
                b'x-content-type-options: nosniff' in headers.lower(), 'public-origin-health')

    def commit(self, before, after):
        result = {'acceptance': 'PASS', 'p9_status': 'CUTOVER_ACCEPTED', 'gate': self.gate,
                  'manifest_sha256': self.pins['manifest_sha256'], 'rollback_sha256': self.rollback_digest,
                  'before_restart': before, 'after_restart': after, 'real_order': 'disabled',
                  'legacy_retained': True, 'accepted_at': datetime.now(timezone.utc).isoformat()}
        write_json(ROOT / 'pending.json', result)
        # Keep the host flock held while the authenticated runner verifies public origin.
        print('P9_READY_FOR_EXTERNAL_GATE ' + self.backup['domain'], flush=True)
        acknowledgement = sys.stdin.readline().strip()
        require(acknowledgement == 'P9_EXTERNAL_GATE_PASS ' + self.pins['manifest_sha256'], 'external-runner-gate-failed')
        config_check(self.pins, self.hashes)
        verify_backup(self.pins, self.rollback_digest)
        legacy_state(self.pins, self.backup, stopped=True)
        final = self.verify()
        require(final['durable']['durable'] == after['durable']['durable'] and
                final['containers'] == after['containers'], 'finalize-runtime-drift')
        result['external_runner_public_gate'] = 'PASS'
        write_json(ROOT / 'acceptance.json', result)

    def failure(self, exc):
        write_json(ROOT / 'failure.json', {'acceptance': 'FAIL', 'first_invariant': str(exc) if isinstance(exc, ValueError) and re.fullmatch('[a-z-]+', str(exc)) else 'forward-stage-failed',
            'rollback_sha256': self.rollback_digest, 'real_order': 'disabled'})
        (ROOT / 'acceptance.json').unlink(missing_ok=True)

    def rollback(self):
        rollback(self.pins, self.backup, self.durable_code)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('preflight', 'cutover', 'recover'))
    parser.add_argument('--pins', required=True)
    parser.add_argument('--rollback-sha256', required=True)
    parser.add_argument('--config-hashes', required=True)
    parser.add_argument('--durable-code', required=True)
    parser.add_argument('--gate')
    args = parser.parse_args()
    pins = json.loads(base64.b64decode(args.pins))
    hashes = json.loads(base64.b64decode(args.config_hashes))
    durable_code = base64.b64decode(args.durable_code)
    require(re.fullmatch('[0-9a-f]{64}', args.rollback_sha256), 'rollback-digest-missing')
    if args.mode == 'preflight':
        preflight(pins, args.rollback_sha256, hashes, durable_code)
    elif args.mode == 'recover':
        journal = json.loads((ROOT / 'transaction.json').read_text())
        require(journal['rollback_sha256'] == args.rollback_sha256, 'recovery-journal-mismatch')
        backup = verify_backup(pins, args.rollback_sha256)
        rollback(pins, backup, durable_code)
    else:
        require(args.gate is not None, 'gate-required')
        gate = json.loads(base64.b64decode(args.gate))
        host = Host(pins, args.rollback_sha256, hashes, durable_code, gate)
        def interrupted(signum, frame):
            raise ValueError('cutover-interrupted')
        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGHUP, interrupted)
        transaction(host)
    print('P9_HOST_GATE=PASS mode=' + args.mode)


if __name__ == '__main__':
    try:
        main()
    except BaseException:
        raise SystemExit('P9_HOST_GATE=FAIL')

#!/usr/bin/env python3
"""Bounded, non-secret host evidence and fail-closed P8 acceptance ledger."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from time import monotonic_ns as collection_clock_ns

ROOT = Path('/srv/trading-platform-staging')
SERVICES = ('market-api', 'execution-worker', 'gateway')
PHASES = ('deploy-known_good', 'deploy-candidate', 'rollback-rollback')
INSPECT = ('{"id":{{json .Id}},"image":{{json .Image}},"started":{{json .State.StartedAt}},'
           '"restarts":{{.RestartCount}},"running":{{.State.Running}},'
           '"health":{{if .State.Health}}{{json .State.Health.Status}}{{else}}null{{end}},'
           '"network_mode":{{json .HostConfig.NetworkMode}}}')
COLLECTOR_STEPS = frozenset({
    'manifest-active', 'manifest-final-known-good', 'manifest-final-candidate',
    'container-query-market-api', 'container-query-execution-worker', 'container-query-gateway',
    'container-inspect-market-api', 'container-inspect-execution-worker', 'container-inspect-gateway',
    'image-inspect-market-api', 'image-inspect-execution-worker', 'image-inspect-gateway',
    'worker-health', 'durable-snapshot', 'soak-ingress',
})
COLLECTOR_FAILURES = frozenset({
    'timeout', 'nonzero-exit', 'output-limit', 'parse-failed', 'invocation-failed',
})
DURABLE_DIAGNOSTIC = re.compile(
    rb'P8_DURABLE_STEP=(START|PASS|FAIL) '
    rb'step=(python-imports|sqlite-connect|sqlite-integrity|sqlite-integrity-execute|sqlite-integrity-fetchone|targets-query|targets-validate|targets-digest|sqlite-close|result) '
    rb'elapsed_ms=([0-9]{1,9}) category=(none|sqlite-busy|sqlite-locked|sqlite-error|validation-failed|operation-failed)'
    rb'(?: sqlite_errorcode=(?:[0-9]{1,10}|unknown))?\n'
)


def emit_durable_diagnostics(stream):
    """Forward only complete allowlisted records, including after an outer timeout."""
    stream.seek(0)
    # Inspect a bounded prefix; never forward arbitrary tool stderr or a partial line.
    for line in stream.read(8192).splitlines(keepends=True)[:32]:
        if DURABLE_DIAGNOSTIC.fullmatch(line):
            print(line.decode('ascii').rstrip('\n'), file=sys.stderr, flush=True)


def require(condition, code):
    if not condition:
        raise ValueError(code)


class ContainerHealthError(ValueError):
    """Safe, structured container health failure without raw inspect data."""
    def __init__(self, service, running, health, restarts):
        self.service = service if service in SERVICES else 'invalid'
        self.running = 'true' if running is True else 'false' if running is False else 'invalid'
        self.health = (health if isinstance(health, str) and
                       re.fullmatch('[a-z][a-z0-9_-]{0,31}', health) else
                       'none' if health is None else 'invalid')
        self.restarts = str(restarts) if type(restarts) is int and restarts >= 0 else 'invalid'
        super().__init__('container-health')

    def diagnostic(self):
        return ('P8_EVIDENCE_FAIL check=container-health service=' + self.service +
                ' expected=running:true,health:healthy actual=running:' + self.running +
                ',health:' + self.health + ',restarts:' + self.restarts)


class CollectionStepError(Exception):
    """A fixed, non-secret collector failure suitable for workflow logs."""
    def __init__(self, step, category, elapsed_ms):
        self.step = step if step in COLLECTOR_STEPS else 'invalid'
        self.category = category if category in COLLECTOR_FAILURES else 'invalid'
        self.elapsed_ms = elapsed_ms if type(elapsed_ms) is int and elapsed_ms >= 0 else 0
        super().__init__('collector-step-failed')

    def diagnostic(self):
        return ('P8_EVIDENCE=FAIL step=' + self.step + ' category=' + self.category +
                ' elapsed_ms=' + str(self.elapsed_ms))


def require_container_health(service, state):
    if state.get('running') is not True or state.get('health') != 'healthy':
        raise ContainerHealthError(service, state.get('running'), state.get('health'), state.get('restarts'))


def run(step, argv, *, data=None, limit=65536, parser=None):
    """Run one allowlisted collection step without exposing tool inputs or outputs."""
    require(step in COLLECTOR_STEPS, 'invalid-collector-step')
    started = collection_clock_ns()

    def elapsed_ms():
        return max(0, (collection_clock_ns() - started) // 1_000_000)

    # Never propagate stderr, raw response data, provider settings, command lines or exceptions.
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as diagnostic_output:
        try:
            result = subprocess.run(argv, input=data, stdout=output,
                stderr=diagnostic_output if step == 'durable-snapshot' else subprocess.DEVNULL, timeout=15)
        except subprocess.TimeoutExpired:
            raise CollectionStepError(step, 'timeout', elapsed_ms()) from None
        except OSError:
            raise CollectionStepError(step, 'invocation-failed', elapsed_ms()) from None
        finally:
            if step == 'durable-snapshot':
                emit_durable_diagnostics(diagnostic_output)
        if result.returncode != 0:
            raise CollectionStepError(step, 'nonzero-exit', elapsed_ms())
        if output.tell() > limit:
            raise CollectionStepError(step, 'output-limit', elapsed_ms())
        output.seek(0)
        try:
            value = output.read().decode()
        except UnicodeError:
            raise CollectionStepError(step, 'parse-failed', elapsed_ms()) from None
    if parser is not None:
        try:
            value = parser(value)
        except (ValueError, TypeError, KeyError, IndexError, UnicodeError):
            raise CollectionStepError(step, 'parse-failed', elapsed_ms()) from None
    duration = elapsed_ms()
    print('P8_EVIDENCE_STEP=PASS step=' + step + ' elapsed_ms=' + str(duration), file=sys.stderr)
    return value


def compose(root=ROOT):
    # Clear shell overrides before invoking Compose interpolation.
    return ['env', '-u', 'STAGING_RUNTIME_IMAGE', '-u', 'STAGING_GATEWAY_IMAGE',
            'docker', 'compose', '--env-file', str(root / 'config/compose.env'),
            '--env-file', str(root / 'deployments/active-release.env'), '-f', str(root / 'bundle/docker-compose.yml')]


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def parse_time(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    require(result.tzinfo is not None, 'timestamp-not-aware')
    return result


def check_health(health, started, now=None):
    required = {'locked': True, 'enabled': False, 'ordering_enabled': False,
                'connected': False, 'execution_state': 'disabled', 'recovery_status': 'locked',
                'broker_name': 'disabled'}
    require(all(type(health.get(k)) is type(v) and health[k] == v for k, v in required.items()), 'execution-not-disabled-locked')
    require(all(type(health.get(k)) is int and health[k] == 0 for k in ('external_order_calls', 'external_cancel_calls')), 'external-calls')
    require('execution_disabled' in health.get('issue_codes', []) and
            'execution_target_not_active' in health.get('issue_codes', []), 'persisted-lock-not-observed')
    heartbeat = parse_time(health['heartbeat_at'])
    age = ((now or datetime.now(timezone.utc)) - heartbeat).total_seconds()
    require(0 <= age <= 30 and heartbeat >= parse_time(started), 'stale-worker-heartbeat')
    return {**required, 'external_order_calls': 0, 'external_cancel_calls': 0,
            'heartbeat_at': health['heartbeat_at'], 'persisted_lock_observed': True,
            'live_reconciliation': 'not-applicable-disabled'}


def record_hashes(root):
    result = {}
    for name in ('current', 'active-release', 'previous'):
        path = root / 'deployments' / (name + '.env')
        result[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    return result


def parse_release_values(value):
    values = {}
    for line in value.splitlines():
        key, item = line.split('=', 1)
        if not re.fullmatch('[A-Z][A-Z0-9_]*', key) or key in values:
            raise ValueError('invalid-release-field')
        values[key] = item
    required = {'CONFIGURATION_IDENTITY', 'STAGING_RUNTIME_IMAGE', 'STAGING_RUNTIME_CONFIG_DIGEST',
                'STAGING_GATEWAY_IMAGE', 'STAGING_GATEWAY_CONFIG_DIGEST', 'RELEASE_NAME'}
    if not required.issubset(values):
        raise ValueError('missing-release-field')
    return values


def parse_json_object(value):
    document = json.loads(value)
    if not isinstance(document, dict):
        raise ValueError('json-object-required')
    return document


def capture(root=ROOT):
    values = release_values(root / 'deployments/active-release.env')
    role = values.get('RELEASE_NAME')
    require(role in ('known_good', 'candidate'), 'invalid-active-release')
    approved = run('manifest-active',
        ['python3', str(root / 'bundle/candidate_manifest.py'), 'emit-env',
         str(root / 'bundle/candidate-manifest.json'), '--release', role],
        parser=parse_release_values)
    require(all(values.get(k) == v for k, v in approved.items()), 'active-manifest-mismatch')
    containers = {}
    expected_images = {}
    for service in SERVICES:
        cid = run('container-query-' + service, [*compose(root), 'ps', '-q', service], parser=str.strip)
        require(re.fullmatch('[0-9a-f]{12,64}', cid) is not None, 'missing-or-ambiguous-container')
        d = run('container-inspect-' + service,
                ['docker', 'inspect', '--format', INSPECT, cid], parser=parse_json_object)
        require({'id', 'image', 'started', 'restarts', 'running', 'health', 'network_mode'}.issubset(d),
                'container-inspect-invalid')
        require(re.fullmatch('[0-9a-f]{64}', d['id']) is not None and
                re.fullmatch('sha256:[0-9a-f]{64}', d['image']) is not None, 'container-identity-invalid')
        require_container_health(service, d)
        require(type(d['restarts']) is int and d['restarts'] >= 0, 'restart-count-invalid')
        parse_time(d['started'])
        require(re.fullmatch('[A-Za-z0-9_-]+', d['network_mode']) is not None, 'network-mode-invalid')
        containers[service] = d
        prefix = 'STAGING_GATEWAY' if service == 'gateway' else 'STAGING_RUNTIME'
        expected_id = run('image-inspect-' + service,
            ['docker', 'image', 'inspect', '--format', '{{.Id}}', approved[prefix + '_IMAGE']],
            parser=str.strip)
        require(expected_id == d['image'], 'running-image-mismatch')
        expected_images[service] = {'ref': approved[prefix + '_IMAGE'],
                                    'config_digest': approved[prefix + '_CONFIG_DIGEST'], 'local_image_id': expected_id}
    worker = containers['execution-worker']
    require(worker['network_mode'] == 'none', 'worker-network-exposed')
    health = run('worker-health',
        ['docker', 'exec', worker['id'], 'cat', '/run/tw-quant-execution/health.json'], parser=parse_json_object)
    durable = run('durable-snapshot',
        ['docker', 'exec', '-i', worker['id'], 'python', '-', 'snapshot'],
        data=(root / 'bundle/durable_probe.py').read_bytes(), parser=parse_json_object)
    require(set(durable) == {'locked_target_sha256', 'sqlite_integrity', 'target_count', 'active_targets'} and
            re.fullmatch('[0-9a-f]{64}', durable['locked_target_sha256']) is not None and
            durable['sqlite_integrity'] == 'ok' and durable['target_count'] == 1 and durable['active_targets'] == 0,
            'durable-state-invalid')
    return {'observed_at': timestamp(), 'containers': containers, 'expected_images': expected_images,
            'release': {'name': role, 'configuration_identity': approved['CONFIGURATION_IDENTITY']},
            'execution': check_health(health, worker['started']), 'durable': durable,
            'records': record_hashes(root)}


def continuity(before, after, *, restarted=False, committed=False):
    require(before['durable'] == after['durable'], 'durable-state-drift')
    if committed:
        records = after['records']
        require(records['current'] is not None and records['current'] == records['active-release'] and
                records['previous'] == before['records']['current'], 'commit-record-transition-invalid')
    else:
        require(before['records'] == after['records'], 'release-record-drift')
    require(before['release'] == after['release'] and before['expected_images'] == after['expected_images'], 'release-image-drift')
    for service in SERVICES:
        a, b = before['containers'][service], after['containers'][service]
        require(a['id'] == b['id'] and a['image'] == b['image'], 'container-replaced')
        require(a['network_mode'] == b['network_mode'], 'network-mode-drift')
        require(a['restarts'] == b['restarts'], 'unexpected-automatic-restart')
        if restarted:
            require(parse_time(b['started']) > parse_time(a['started']), 'restart-not-observed')
        else:
            require(a['started'] == b['started'], 'container-restarted')
    if restarted:
        require(parse_time(after['execution']['heartbeat_at']) > parse_time(before['execution']['heartbeat_at']),
                'heartbeat-not-renewed')


def session_dir(root=ROOT):
    session = (root / 'bundle/acceptance-session').read_text().strip()
    require(re.fullmatch('[1-9][0-9]*-[1-9][0-9]*', session) is not None, 'invalid-session')
    return root / 'deployments/evidence' / session


def append(event, data, root=ROOT):
    require(event in [p + '/' + stage for p in PHASES for stage in ('verified', 'restart-before', 'restart-after', 'committed')] + ['soak'], 'unknown-event')
    directory = session_dir(root)
    envelope = json.loads((directory / 'session.json').read_text())
    row = {'session': envelope['session'], 'candidate_run_id': envelope['candidate_run_id'],
           'pipeline': envelope['pipeline'], 'event': event, 'data': data}
    with (directory / 'ledger.jsonl').open('a') as f:
        f.write(json.dumps(row, sort_keys=True) + '\n')
        f.flush()
        os.fsync(f.fileno())


def init(minutes, root=ROOT):
    require(type(minutes) is int and 30 <= minutes <= 360, 'invalid-soak-duration')
    from candidate_manifest import validate
    document = json.loads((root / 'bundle/candidate-manifest.json').read_text())
    validate(document)
    directory = session_dir(root)
    directory.mkdir(parents=True, mode=0o700, exist_ok=False)
    (directory / 'session.json').write_text(json.dumps({'session': directory.name,
        'candidate_run_id': document['provenance']['run_id'], 'pipeline': document['pipeline_revision'],
        'manifest_sha256': hashlib.sha256((root / 'bundle/candidate-manifest.json').read_bytes()).hexdigest(),
        'requested_minutes': minutes}))


def release_values(path):
    values = {}
    for line in path.read_text().splitlines():
        key, value = line.split('=', 1)
        require(key not in values, 'duplicate-release-field')
        values[key] = value
    return values


def validate_soak(soak, requested_minutes):
    require(type(requested_minutes) is int and 30 <= requested_minutes <= 360, 'invalid-soak-duration')
    require(type(soak.get('requested_minutes')) is int and
            soak['requested_minutes'] == requested_minutes, 'soak-duration-mismatch')
    offsets = soak.get('sample_offsets_seconds')
    require(isinstance(offsets, list) and 2 <= len(offsets) <= 2162, 'soak-samples-invalid')
    numeric = lambda value: type(value) in (int, float) and math.isfinite(value) and value >= 0
    require(all(numeric(value) for value in offsets), 'soak-samples-invalid')
    gaps = [offsets[0], *(b - a for a, b in zip(offsets, offsets[1:]))]
    require(all(gap > 0 for gap in gaps[1:]) and max(gaps) <= 30, 'soak-sample-gap')
    require(requested_minutes * 60 <= offsets[-1] <= requested_minutes * 60 + 30, 'soak-coverage-incomplete')
    require(type(soak.get('samples')) is int and soak['samples'] == len(offsets), 'soak-sample-count-mismatch')
    for field, expected in (('elapsed_seconds', offsets[-1]), ('max_sample_gap_seconds', max(gaps))):
        require(numeric(soak.get(field)) and math.isclose(soak[field], expected, rel_tol=0, abs_tol=1e-6),
                'soak-summary-mismatch')
    return {'requested_minutes': requested_minutes, 'elapsed_seconds': offsets[-1],
            'samples': len(offsets), 'max_sample_gap_seconds': max(gaps)}


def final_check(root=ROOT):
    directory = session_dir(root)
    (directory / 'acceptance.json').unlink(missing_ok=True)
    envelope = json.loads((directory / 'session.json').read_text())
    require(hashlib.sha256((root / 'bundle/candidate-manifest.json').read_bytes()).hexdigest() == envelope['manifest_sha256'], 'manifest-changed')
    rows = [json.loads(line) for line in (directory / 'ledger.jsonl').read_text().splitlines()]
    expected = [p + '/' + stage for p in PHASES for stage in ('verified', 'restart-before', 'restart-after', 'committed')] + ['soak']
    require([row['event'] for row in rows] == expected, 'acceptance-stage-order-incomplete')
    for row in rows:
        require(all(row[k] == envelope[k] for k in ('session', 'candidate_run_id', 'pipeline')), 'ledger-provenance-drift')
    for offset in (0, 4, 8):
        continuity(rows[offset]['data'], rows[offset + 1]['data'])
        continuity(rows[offset + 1]['data'], rows[offset + 2]['data'], restarted=True)
        continuity(rows[offset + 2]['data'], rows[offset + 3]['data'], committed=True)
        role = 'candidate' if offset == 4 else 'known_good'
        require(all(rows[i]['data']['release']['name'] == role for i in range(offset, offset + 4)), 'stage-release-mismatch')
    durable = rows[0]['data']['durable']
    require(all(row['data']['durable'] == durable for row in rows[:-1]), 'cross-release-durable-drift')
    before, after = rows[-1]['data']['before'], rows[-1]['data']['after']
    continuity(rows[11]['data'], before)
    continuity(before, after)
    require(after['durable'] == durable, 'soak-durable-drift')
    current, active, previous = [release_values(root / 'deployments' / (x + '.env')) for x in ('current', 'active-release', 'previous')]
    require(current == active, 'active-current-disagree')
    for role, actual in (('known_good', current), ('candidate', previous)):
        expected_record = run('manifest-final-' + role.replace('_', '-'),
            ['python3', str(root / 'bundle/candidate_manifest.py'), 'emit-env',
             str(root / 'bundle/candidate-manifest.json'), '--release', role],
            parser=parse_release_values)
        require(all(actual.get(k) == v for k, v in expected_record.items()), 'final-release-identity-mismatch')
    require(current.get('DEPLOYMENT_MODE') == 'rollback', 'final-not-rollback')
    require(rows[0]['data']['containers']['market-api']['image'] != rows[4]['data']['containers']['market-api']['image'], 'a-b-image-not-distinct')
    for service in SERVICES:
        require(rows[0]['data']['containers'][service]['image'] == rows[8]['data']['containers'][service]['image'], 'rollback-image-mismatch')
    soak = rows[-1]['data']
    soak_summary = validate_soak(soak, envelope.get('requested_minutes'))
    require(all(x['complete'] is True for x in soak['log_coverage'].values()) and
            set(soak['log_coverage']) == set(SERVICES), 'log-coverage-incomplete')
    continuity(after, capture(root))
    result = {**envelope, 'acceptance': 'PASS', 'stages': expected, 'soak': soak_summary,
              'final_release': 'known_good', 'previous_release': 'candidate',
              'production_deployments': 'unknown-not-queried',
              'live_reconciliation': 'not-applicable-disabled',
              'ledger_sha256': hashlib.sha256((directory / 'ledger.jsonl').read_bytes()).hexdigest()}
    temporary = directory / 'acceptance.json.tmp'
    temporary.write_text(json.dumps(result, sort_keys=True, indent=2) + '\n')
    temporary.replace(directory / 'acceptance.json')
    return result


class LogMonitor:
    """Consume the entire observation window, without tail truncation or raw output."""
    def __init__(self, containers, since):
        import threading
        self.processes = {}
        self.threads = []
        self.stats = {}
        self.closed = False
        try:
            for service in SERVICES:
                process = subprocess.Popen(['docker', 'logs', '--follow', '--timestamps', '--since', since,
                    containers[service]['id']], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                self.processes[service] = process
                self.stats[service] = {'bytes': 0, 'policy_failed': False, 'overflow': False,
                                      'read_failed': False, 'ended_early': False, 'complete': False}
                thread = threading.Thread(target=self.consume, args=(service, process), daemon=True)
                thread.start()
                self.threads.append(thread)
        except Exception:
            self.close()
            raise

    def consume(self, service, process):
        state = self.stats[service]
        tail = b''
        pattern = re.compile(rb'(traceback|secret[=:][^\s]+|token[=:][^\s]+|credential[=:][^\s]+)', re.I)
        try:
            while True:
                chunk = process.stdout.read1(65536)
                if not chunk:
                    state['ended_early'] = not self.closed
                    break
                state['bytes'] += len(chunk)
                state['overflow'] |= state['bytes'] > 64 * 1024 * 1024
                state['policy_failed'] |= pattern.search(tail + chunk) is not None
                tail = (tail + chunk)[-256:]
        except Exception:
            # A reader failure must reach the acceptance gate, never leak a raw
            # exception from a background thread or leave a false complete flag.
            state['read_failed'] = True

    def check(self):
        require(all(p.poll() is None for p in self.processes.values()), 'log-stream-ended-early')
        require(all(not s['read_failed'] and not s['ended_early'] for s in self.stats.values()), 'log-reader-failed')
        require(all(not s['policy_failed'] and not s['overflow'] for s in self.stats.values()), 'log-policy-or-limit-failed')

    def close(self):
        if self.closed:
            return
        self.closed = True
        alive = len(self.processes) == len(SERVICES) and all(p.poll() is None for p in self.processes.values())
        for process in self.processes.values():
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                alive = False
        for thread in self.threads:
            thread.join(timeout=5)
            alive &= not thread.is_alive()
        for service, state in self.stats.items():
            state['complete'] = (alive and not state['policy_failed'] and not state['overflow']
                                 and not state['read_failed'] and not state['ended_early'])
            self.processes[service].stdout.close()


def soak(minutes, root=ROOT):
    require(type(minutes) is int and 30 <= minutes <= 360, 'invalid-soak-duration')
    envelope = json.loads((session_dir(root) / 'session.json').read_text())
    requested = envelope.get('requested_minutes')
    require(type(requested) is int and minutes == requested, 'soak-duration-mismatch')
    before = capture(root)
    require(before['records']['current'] == before['records']['active-release'] and
            before['records']['current'] is not None, 'release-records-not-aligned')
    logs = LogMonitor(before['containers'], before['observed_at'])
    started = previous = time.monotonic()
    offsets, max_gap = [], 0.0
    try:
        while True:
            after = capture(root)
            continuity(before, after)
            run('soak-ingress', ['python3', str(root / 'bundle/ingress_probe.py')])
            logs.check()
            now = time.monotonic()
            max_gap = max(max_gap, now - previous)
            require(max_gap <= 30, 'soak-sample-gap')
            previous = now
            offsets.append(now - started)
            if now - started >= minutes * 60:
                break
            time.sleep(min(10, max(0, minutes * 60 - (now - started))))
    finally:
        logs.close()
    require(all(s['complete'] for s in logs.stats.values()), 'log-coverage-incomplete')
    data = {'before': before, 'after': after, 'samples': len(offsets), 'requested_minutes': minutes,
            'sample_offsets_seconds': offsets, 'elapsed_seconds': now - started,
            'max_sample_gap_seconds': max_gap, 'log_coverage': logs.stats}
    validate_soak(data, requested)
    append('soak', data, root)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('init', 'capture', 'restart-check', 'soak', 'final'))
    parser.add_argument('--event')
    parser.add_argument('--minutes', type=int)
    args = parser.parse_args()
    require(os.environ.get('INSTALL_ROOT', str(ROOT)) == str(ROOT), 'non-staging-root')
    if args.command == 'init':
        init(args.minutes)
    elif args.command == 'capture':
        append(args.event, capture())
    elif args.command == 'restart-check':
        rows = [json.loads(x) for x in (session_dir() / 'ledger.jsonl').read_text().splitlines()]
        require(rows[-1]['event'] == args.event.replace('restart-after', 'restart-before'), 'restart-before-missing')
        after = capture()
        continuity(rows[-1]['data'], after, restarted=True)
        append(args.event, after)
    elif args.command == 'soak':
        soak(args.minutes)
    else:
        final_check()
    print('P8_EVIDENCE=PASS operation=' + args.command)


def entrypoint(action=None):
    try:
        (action or main)()
    except Exception as exc:
        if isinstance(exc, CollectionStepError):
            raise SystemExit(exc.diagnostic())
        if isinstance(exc, ContainerHealthError):
            raise SystemExit(exc.diagnostic())
        # All explicit invariant messages are fixed codes; never stringify tool exceptions.
        code = str(exc) if isinstance(exc, ValueError) and re.fullmatch('[a-z-]+', str(exc)) else 'collection-failed'
        raise SystemExit('P8_EVIDENCE=FAIL check=' + code)


if __name__ == '__main__':
    entrypoint()

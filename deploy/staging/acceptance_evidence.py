#!/usr/bin/env python3
"""Bounded, non-secret host evidence and fail-closed P8 acceptance ledger."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

ROOT = Path('/srv/trading-platform-staging')
SERVICES = ('market-api', 'execution-worker', 'gateway')
PHASES = ('deploy-known_good', 'deploy-candidate', 'rollback-rollback')
INSPECT = ('{"id":{{json .Id}},"image":{{json .Image}},"started":{{json .State.StartedAt}},'
           '"restarts":{{.RestartCount}},"running":{{.State.Running}},'
           '"health":{{if .State.Health}}{{json .State.Health.Status}}{{else}}null{{end}},'
           '"network_mode":{{json .HostConfig.NetworkMode}}}')


def require(condition, code):
    if not condition:
        raise ValueError(code)


def run(argv, *, data=None, limit=65536):
    # Never propagate stderr, raw response data, provider settings or command lines.
    with tempfile.TemporaryFile() as output:
        result = subprocess.run(argv, input=data, stdout=output, stderr=subprocess.DEVNULL, timeout=15)
        require(result.returncode == 0, 'command-failed')
        require(output.tell() <= limit, 'command-output-too-large')
        output.seek(0)
        return output.read().decode()


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


def capture(root=ROOT):
    values = release_values(root / 'deployments/active-release.env')
    role = values.get('RELEASE_NAME')
    require(role in ('known_good', 'candidate'), 'invalid-active-release')
    emitted = run(['python3', str(root / 'bundle/candidate_manifest.py'), 'emit-env',
                   str(root / 'bundle/candidate-manifest.json'), '--release', role])
    approved = dict(line.split('=', 1) for line in emitted.splitlines())
    require(all(values.get(k) == v for k, v in approved.items()), 'active-manifest-mismatch')
    containers = {}
    expected_images = {}
    for service in SERVICES:
        cid = run([*compose(root), 'ps', '-q', service]).strip()
        require(re.fullmatch('[0-9a-f]{12,64}', cid) is not None, 'missing-or-ambiguous-container')
        d = json.loads(run(['docker', 'inspect', '--format', INSPECT, cid]))
        require(re.fullmatch('[0-9a-f]{64}', d['id']) is not None and
                re.fullmatch('sha256:[0-9a-f]{64}', d['image']) is not None, 'container-identity-invalid')
        require(d['running'] is True and d['health'] == 'healthy', 'container-unhealthy')
        require(type(d['restarts']) is int and d['restarts'] >= 0, 'restart-count-invalid')
        parse_time(d['started'])
        require(re.fullmatch('[A-Za-z0-9_-]+', d['network_mode']) is not None, 'network-mode-invalid')
        containers[service] = d
        prefix = 'STAGING_GATEWAY' if service == 'gateway' else 'STAGING_RUNTIME'
        expected_id = run(['docker', 'image', 'inspect', '--format', '{{.Id}}', approved[prefix + '_IMAGE']]).strip()
        require(expected_id == d['image'], 'running-image-mismatch')
        expected_images[service] = {'ref': approved[prefix + '_IMAGE'],
                                    'config_digest': approved[prefix + '_CONFIG_DIGEST'], 'local_image_id': expected_id}
    worker = containers['execution-worker']
    require(worker['network_mode'] == 'none', 'worker-network-exposed')
    health = json.loads(run(['docker', 'exec', worker['id'], 'cat', '/run/tw-quant-execution/health.json']))
    durable = json.loads(run(['docker', 'exec', '-i', worker['id'], 'python', '-', 'snapshot'],
        data=(root / 'bundle/durable_probe.py').read_bytes()))
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


def init(root=ROOT):
    from candidate_manifest import validate
    document = json.loads((root / 'bundle/candidate-manifest.json').read_text())
    validate(document)
    directory = session_dir(root)
    directory.mkdir(parents=True, mode=0o700, exist_ok=False)
    (directory / 'session.json').write_text(json.dumps({'session': directory.name,
        'candidate_run_id': document['provenance']['run_id'], 'pipeline': document['pipeline_revision'],
        'manifest_sha256': hashlib.sha256((root / 'bundle/candidate-manifest.json').read_bytes()).hexdigest()}))


def release_values(path):
    values = {}
    for line in path.read_text().splitlines():
        key, value = line.split('=', 1)
        require(key not in values, 'duplicate-release-field')
        values[key] = value
    return values


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
        output = run(['python3', str(root / 'bundle/candidate_manifest.py'), 'emit-env',
                      str(root / 'bundle/candidate-manifest.json'), '--release', role])
        expected_record = dict(line.split('=', 1) for line in output.splitlines())
        require(all(actual.get(k) == v for k, v in expected_record.items()), 'final-release-identity-mismatch')
    require(current.get('DEPLOYMENT_MODE') == 'rollback', 'final-not-rollback')
    require(rows[0]['data']['containers']['market-api']['image'] != rows[4]['data']['containers']['market-api']['image'], 'a-b-image-not-distinct')
    for service in SERVICES:
        require(rows[0]['data']['containers'][service]['image'] == rows[8]['data']['containers'][service]['image'], 'rollback-image-mismatch')
    soak = rows[-1]['data']
    require(soak['elapsed_seconds'] >= soak['requested_minutes'] * 60 and
            30 <= soak['requested_minutes'] <= 360 and soak['samples'] >= 2 and
            soak['max_sample_gap_seconds'] <= 30, 'soak-coverage-incomplete')
    require(all(x['complete'] is True for x in soak['log_coverage'].values()) and
            set(soak['log_coverage']) == set(SERVICES), 'log-coverage-incomplete')
    continuity(after, capture(root))
    result = {**envelope, 'acceptance': 'PASS', 'stages': expected,
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
    require(30 <= minutes <= 360, 'invalid-soak-duration')
    before = capture(root)
    require(before['records']['current'] == before['records']['active-release'] and
            before['records']['current'] is not None, 'release-records-not-aligned')
    logs = LogMonitor(before['containers'], before['observed_at'])
    started = previous = time.monotonic()
    samples, max_gap = 0, 0.0
    try:
        while True:
            after = capture(root)
            continuity(before, after)
            run(['python3', str(root / 'bundle/ingress_probe.py')])
            logs.check()
            now = time.monotonic()
            max_gap = max(max_gap, now - previous)
            require(max_gap <= 30, 'soak-sample-gap')
            previous = now
            samples += 1
            if now - started >= minutes * 60:
                break
            time.sleep(min(10, max(0, minutes * 60 - (now - started))))
    finally:
        logs.close()
    require(all(s['complete'] for s in logs.stats.values()), 'log-coverage-incomplete')
    append('soak', {'before': before, 'after': after, 'samples': samples, 'requested_minutes': minutes,
                   'elapsed_seconds': now - started, 'max_sample_gap_seconds': max_gap,
                   'log_coverage': logs.stats}, root)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('init', 'capture', 'restart-check', 'soak', 'final'))
    parser.add_argument('--event')
    parser.add_argument('--minutes', type=int)
    args = parser.parse_args()
    require(os.environ.get('INSTALL_ROOT', str(ROOT)) == str(ROOT), 'non-staging-root')
    if args.command == 'init':
        init()
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


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # All explicit invariant messages are fixed codes; never stringify tool exceptions.
        code = str(exc) if isinstance(exc, ValueError) and re.fullmatch('[a-z-]+', str(exc)) else 'collection-failed'
        raise SystemExit('P8_EVIDENCE=FAIL check=' + code)

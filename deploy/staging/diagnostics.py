#!/usr/bin/env python3
"""Read only allowlisted status; no env, raw logs, labels or HTTP bodies."""
import argparse
import os
import json
import re
import subprocess
import stat
from pathlib import Path
import urllib.request
from datetime import datetime, timezone


def command(argv, input_text=None):
    try:
        result = subprocess.run(argv, input=input_text, capture_output=True, text=True, timeout=5)
        return result.returncode, result.stdout[:16384]
    except (OSError, subprocess.SubprocessError):
        return -1, ''


# Read fixed paths only; emit no raw health document, env, log output or errors.
EXECUTION_PROBE = r'''
import json, os, re, stat
from pathlib import Path
from datetime import datetime, timezone
result = {}
path = Path('/run/tw-quant-execution/health.json')
try:
    info = path.stat()
    result['health_file'] = {'regular': stat.S_ISREG(info.st_mode), 'uid': info.st_uid,
        'gid': info.st_gid, 'mode': stat.S_IMODE(info.st_mode), 'size': info.st_size,
        'writable': os.access(path, os.W_OK)}
    with path.open() as stream:
        raw = stream.read(16385)
    if len(raw) > 16384: raise ValueError()
    document = json.loads(raw)
    heartbeat = datetime.fromisoformat(document['heartbeat_at'])
    if heartbeat.tzinfo is None: raise ValueError()
    result['heartbeat_at'] = heartbeat.isoformat()
    result['heartbeat_age_seconds'] = round((datetime.now(timezone.utc) - heartbeat).total_seconds(), 3)
    for key in ('locked', 'enabled', 'ordering_enabled'):
        if type(document.get(key)) is bool: result[key] = document[key]
    for key in ('external_order_calls', 'external_cancel_calls'):
        if type(document.get(key)) is int and 0 <= document[key] <= 2**63-1: result[key] = document[key]
    generation = document.get('generation')
    result['health_generation_valid'] = isinstance(generation, str) and re.fullmatch('[0-9a-f]{32}', generation) is not None
    try:
        with Path('/tmp/tw-quant-execution-generation').open() as stream: marker = stream.read(128).strip()
        result['marker_valid'] = re.fullmatch('[0-9a-f]{32}', marker) is not None
        result['generation_match'] = result['marker_valid'] and result['health_generation_valid'] and marker == generation
    except OSError:
        result['marker_missing'] = True
except (OSError, ValueError, TypeError, KeyError):
    result['health_unreadable_or_invalid'] = True
try:
    state = Path('/proc/1/stat').read_text().rsplit(')', 1)[1].split()[0]
    if state in ('R', 'S', 'D', 'Z', 'T', 't', 'X', 'I'): result['pid1_state'] = state
except (OSError, IndexError):
    result['pid1_unavailable'] = True
print(json.dumps(result, sort_keys=True))
'''


def execution_probe(container_id):
    code, value = command(['docker', 'exec', '-i', container_id, 'python', '-'], EXECUTION_PROBE)
    result = {'probe_exit': code}
    try:
        document = json.loads(value)
        # Defense in depth: do not trust even the fixed subprocess output.
        for key in ('locked', 'enabled', 'ordering_enabled', 'health_generation_valid', 'marker_valid',
                    'generation_match', 'marker_missing', 'health_unreadable_or_invalid', 'pid1_unavailable'):
            if type(document.get(key)) is bool: result[key] = document[key]
        for key in ('external_order_calls', 'external_cancel_calls'):
            if type(document.get(key)) is int and 0 <= document[key] <= 2**63-1: result[key] = document[key]
        timestamp = document.get('heartbeat_at')
        if isinstance(timestamp, str) and re.fullmatch(r'[0-9T:+.\-Z]{20,40}', timestamp):
            result['heartbeat_at'] = timestamp
        age = document.get('heartbeat_age_seconds')
        if type(age) in (int, float) and abs(age) < 10**9: result['heartbeat_age_seconds'] = age
        if document.get('pid1_state') in ('R', 'S', 'D', 'Z', 'T', 't', 'X', 'I'):
            result['pid1_state'] = document['pid1_state']
        info = document.get('health_file', {})
        if isinstance(info, dict):
            result['health_file'] = {k: v for k, v in info.items()
                if (k in ('regular', 'writable') and type(v) is bool) or
                   (k in ('uid', 'gid', 'mode', 'size') and type(v) is int and 0 <= v <= 2**63-1)}
    except (ValueError, TypeError, AttributeError):
        result['parse_failed'] = True
    # Docker's health history is already bounded; omit Output entirely.
    code, value = command(['docker', 'inspect', '--format',
        '{{if .State.Health}}{{range .State.Health.Log}}{{json .Start}} {{json .End}} {{.ExitCode}}{{println}}{{end}}{{end}}', container_id])
    history = []
    for line in value.splitlines()[-5:]:
        match = re.fullmatch(r'"([0-9T:+.\-Z]{20,40})" "([0-9T:+.\-Z]{20,40})" (-?[0-9]{1,10})', line)
        if match: history.append({'start': match[1], 'end': match[2], 'exit': int(match[3])})
    result['health_history_exit'] = code
    result['health_history'] = history
    return result


def collect(pre_compensation=False):
    result = {'diagnostic_only': True, 'observed_at': datetime.now(timezone.utc).isoformat(), 'services': {}}
    for service in (('execution-worker',) if pre_compensation else ('market-api', 'execution-worker', 'gateway')):
        code, value = command(['docker', 'ps', '-aq', '--filter', 'label=com.docker.compose.project=platform-staging',
                               '--filter', 'label=com.docker.compose.service=' + service])
        ids = value.split()
        state = {'query_exit': code, 'container_count': len(ids)}
        if code == 0 and len(ids) == 1 and re.fullmatch('[0-9a-f]{12,64}', ids[0]):
            fmt = ('{"running":{{.State.Running}},"exit":{{.State.ExitCode}},"oom":{{.State.OOMKilled}},'
                   '"restarts":{{.RestartCount}},"started":{{json .State.StartedAt}},"health":{{if .State.Health}}{{json .State.Health.Status}}{{else}}null{{end}},'
                   '"port_binding_count":{{len .HostConfig.PortBindings}},"network_count":{{len .NetworkSettings.Networks}},'
                   '"requested":{{json .HostConfig.PortBindings}},"effective":{{json .NetworkSettings.Ports}}}')
            code, value = command(['docker', 'inspect', '--format', fmt, ids[0]])
            state['inspect_exit'] = code
            try:
                data = json.loads(value)
                for key in ('running', 'oom'):
                    if type(data.get(key)) is bool: state[key] = data[key]
                for key in ('exit', 'restarts', 'port_binding_count', 'network_count'):
                    if type(data.get(key)) is int: state[key] = data[key]
                if data.get('health') in ('healthy', 'unhealthy', 'starting', None): state['health'] = data.get('health')
                if isinstance(data.get('started'), str) and re.fullmatch(r'[0-9T:+.\-Z]{20,40}', data['started']):
                    state['started'] = data['started']
                requested = data.get('requested') or {}
                effective = data.get('effective') or {}
                state['requested_loopback_18080'] = {'HostIp': '127.0.0.1', 'HostPort': '18080'} in requested.get('8080/tcp', [])
                state['effective_published_port'] = any(effective.values())
            except (ValueError, TypeError):
                state['parse_failed'] = True
            if service == 'execution-worker':
                state['execution'] = execution_probe(ids[0])
            code, names = command(['docker', 'inspect', '--format', '{{range $n,$v := .NetworkSettings.Networks}}{{println $n}}{{end}}', ids[0]])
            networks = names.split()
            if code == 0 and len(networks) <= 4 and all(re.fullmatch('[A-Za-z0-9_-]+', n) for n in networks):
                flags = [command(['docker', 'network', 'inspect', '--format', '{{.Internal}}', n]) for n in networks if n != 'none']
                state['all_bridges_internal'] = bool(flags) and all(c == 0 and v.strip() == 'true' for c, v in flags)
        result['services'][service] = state
    if pre_compensation:
        return result
    for unit in ('socket', 'service'):
        code, state = command(['systemctl', 'is-active', 'p8-staging-ingress.' + unit])
        result[unit] = {'query_exit': code, 'state': state.strip() if state.strip() in
                        ('active', 'inactive', 'failed', 'activating', 'deactivating', 'unknown') else 'unavailable'}
    code, value = command(['systemctl', 'show', '--property=ExecMainStatus', '--value', 'p8-staging-ingress.service'])
    result['proxy_exit'] = int(value.strip()) if code == 0 and value.strip().isdigit() else None
    try:
        info = Path('/var/lib/tw-quant-staging-ingress/gateway.sock').lstat()
        result['unix_socket'] = {'is_socket': stat.S_ISSOCK(info.st_mode),
            'expected_owner': info.st_uid == info.st_gid == 10000, 'expected_mode': stat.S_IMODE(info.st_mode) == 0o600}
    except OSError:
        result['unix_socket'] = {'missing': True}
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    for endpoint in ('/healthz', '/health/live'):
        try:
            with opener.open('http://127.0.0.1:18080' + endpoint, timeout=3) as response:
                body = response.read(4097)
                result[endpoint] = {'http_status': response.status, 'exact_ok': body == b'ok', 'oversize': len(body) > 4096}
        except Exception:
            result[endpoint] = {'connection_or_http_failure': True}
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--pre-compensation', action='store_true')
    parser.add_argument('--phase', default='unknown')
    args = parser.parse_args()
    result = collect(args.pre_compensation)
    if args.pre_compensation:
        root = Path('/srv/trading-platform-staging')
        session = (root / 'bundle/acceptance-session').read_text().strip()
        if re.fullmatch(r'[1-9][0-9]*-[1-9][0-9]*', session) is None:
            raise SystemExit(2)
        result['phase'] = args.phase if args.phase in ('deploy-known_good', 'deploy-candidate', 'rollback-rollback') else 'unknown'
        result['pre_compensation'] = True
        target = root / 'deployments/evidence' / session / 'pre-compensation.json'
        # Keep the first failure snapshot, including if restore subsequently fails.
        with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as stream:
            stream.write(json.dumps(result, sort_keys=True) + '\n')
    else:
        print(json.dumps(result, sort_keys=True))

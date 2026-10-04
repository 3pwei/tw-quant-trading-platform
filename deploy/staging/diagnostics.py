#!/usr/bin/env python3
"""Read only allowlisted status; no env, raw logs, labels or HTTP bodies."""
import json
import re
import subprocess
import stat
from pathlib import Path
import urllib.request
from datetime import datetime, timezone


def command(argv):
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=10)
        return result.returncode, result.stdout[:16384]
    except (OSError, subprocess.SubprocessError):
        return -1, ''


def collect():
    result = {'diagnostic_only': True, 'observed_at': datetime.now(timezone.utc).isoformat(), 'services': {}}
    for service in ('market-api', 'execution-worker', 'gateway'):
        code, value = command(['docker', 'ps', '-aq', '--filter', 'label=com.docker.compose.project=platform-staging',
                               '--filter', 'label=com.docker.compose.service=' + service])
        ids = value.split()
        state = {'query_exit': code, 'container_count': len(ids)}
        if code == 0 and len(ids) == 1 and re.fullmatch('[0-9a-f]{12,64}', ids[0]):
            fmt = ('{"running":{{.State.Running}},"exit":{{.State.ExitCode}},"oom":{{.State.OOMKilled}},'
                   '"restarts":{{.RestartCount}},"health":{{if .State.Health}}{{json .State.Health.Status}}{{else}}null{{end}},'
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
                requested = data.get('requested') or {}
                effective = data.get('effective') or {}
                state['requested_loopback_18080'] = {'HostIp': '127.0.0.1', 'HostPort': '18080'} in requested.get('8080/tcp', [])
                state['effective_published_port'] = any(effective.values())
            except (ValueError, TypeError):
                state['parse_failed'] = True
            code, names = command(['docker', 'inspect', '--format', '{{range $n,$v := .NetworkSettings.Networks}}{{println $n}}{{end}}', ids[0]])
            networks = names.split()
            if code == 0 and len(networks) <= 4 and all(re.fullmatch('[A-Za-z0-9_-]+', n) for n in networks):
                flags = [command(['docker', 'network', 'inspect', '--format', '{{.Internal}}', n]) for n in networks if n != 'none']
                state['all_bridges_internal'] = bool(flags) and all(c == 0 and v.strip() == 'true' for c, v in flags)
        result['services'][service] = state
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
    print(json.dumps(collect(), sort_keys=True))

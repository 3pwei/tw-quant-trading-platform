#!/usr/bin/env python3
"""Temporary manual diagnostic transport; no deployment or acceptance writes."""
import inspect
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = Path(__file__).resolve().parent
REPOSITORY = '3pwei/tw-quant-trading-platform'
CASES = ('python-argv', 'python-stdin', 'durable')
STAGES = ('python-imports', 'sqlite-connect', 'sqlite-integrity-execute',
          'sqlite-integrity-fetchone', 'sqlite-integrity', 'targets-query',
          'targets-validate', 'targets-digest', 'sqlite-close', 'result')
SAFE_LINE = re.compile(
    r'(?:P8_DIAGNOSTIC_HOST=START|P8_DIAGNOSTIC=PASS acceptance=false|'
    r'P8_DIAGNOSTIC_CASE=(?:python-argv|python-stdin|durable)|'
    r'P8_DURABLE_STEP=(?:START|PASS|FAIL) step=(?:' + '|'.join(STAGES) + r') '
    r'elapsed_ms=[0-9]{1,9} category=(?:none|sqlite-busy|sqlite-locked|sqlite-error|validation-failed|operation-failed)'
    r'(?: sqlite_errorcode=(?:[0-9]{1,10}|unknown))?|'
    r'P8_EVIDENCE_STEP=PASS step=(?:container-query-execution-worker|container-inspect-execution-worker|worker-health|durable-snapshot) elapsed_ms=[0-9]{1,9}|'
    r'P8_EVIDENCE=FAIL step=(?:container-query-execution-worker|container-inspect-execution-worker|worker-health|durable-snapshot) '
    r'category=(?:timeout|nonzero-exit|output-limit|parse-failed|invocation-failed) elapsed_ms=[0-9]{1,9}|'
    r'P8_EVIDENCE=FAIL check=(?:staging-root-invalid|container-invalid|worker-isolation-invalid|staging-volume-invalid|'
    r'execution-not-disabled-locked|external-calls|persisted-lock-not-observed|timestamp-not-aware|stale-worker-heartbeat|'
    r'diagnostic-result-invalid|durable-state-invalid|collection-failed)|'
    r'P8_EVIDENCE_FAIL check=container-health service=execution-worker '
    r'expected=running:true,health:healthy actual=running:(?:true|false|invalid),'
    r'health:(?:healthy|unhealthy|starting|none|invalid),restarts:(?:[0-9]{1,9}|invalid))\n'
)


def require(value):
    if not value:
        raise ValueError('control-gate-failed')


def api(path):
    request = urllib.request.Request('https://api.github.com/repos/' + REPOSITORY + '/' + path,
        headers={'Authorization': 'Bearer ' + os.environ['GITHUB_TOKEN'],
                 'Accept': 'application/vnd.github+json'})
    with urllib.request.urlopen(request, timeout=15) as response:
        raw = response.read(2_000_001)
    require(len(raw) <= 2_000_000)
    return json.loads(raw)


def check_request(env=None, get=api):
    env = os.environ if env is None else env
    sha = env.get('GITHUB_SHA', '')
    require(re.fullmatch('[0-9a-f]{40}', sha) is not None)
    require(env.get('GITHUB_REPOSITORY') == REPOSITORY and
            env.get('GITHUB_EVENT_NAME') == 'workflow_dispatch' and
            env.get('GITHUB_REF') == 'refs/heads/master' and env.get('GITHUB_RUN_ATTEMPT') == '1')
    require(re.fullmatch('[1-9][0-9]{0,19}', env.get('GITHUB_RUN_ID', '')) is not None)
    require(env.get('EXPECTED_SHA') == sha and env.get('CONFIRMATION') == 'DIAGNOSE staging ' + sha)
    require(get('git/ref/heads/master')['object']['sha'] == sha)
    for workflow in ('ci.yml', 'security.yml'):
        runs = get('actions/workflows/' + workflow + '/runs?head_sha=' + sha + '&event=push&per_page=1')['workflow_runs']
        require(len(runs) == 1)
        run = runs[0]
        require(run['head_sha'] == sha and run['head_branch'] == 'master' and run['event'] == 'push' and
                run['path'] == '.github/workflows/' + workflow and
                run['status'] == 'completed' and run['conclusion'] == 'success')
    # Detect a master advance during gate inspection, before SSH material is created.
    require(get('git/ref/heads/master')['object']['sha'] == sha)
    return sha


def remote_main(namespace, probe):
    """Only existing container metadata/health reads and three single exec probes."""
    import re
    import sys
    run, need, parse = namespace['run'], namespace['require'], namespace['parse_json_object']
    root = namespace['ROOT']
    need(root.is_dir() and root.resolve() == root, 'staging-root-invalid')
    cid = run('container-query-execution-worker',
              [*namespace['compose'](), 'ps', '-q', 'execution-worker'], parser=str.strip)
    need(re.fullmatch('[0-9a-f]{64}', cid) is not None, 'container-invalid')
    fmt = namespace['INSPECT'][:-1] + (
        ',"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
        '"service":{{json (index .Config.Labels "com.docker.compose.service")}},"mounts":{{json .Mounts}}}')
    state = run('container-inspect-execution-worker',
                ['docker', 'inspect', '--format', fmt, cid], parser=parse)
    namespace['require_container_health']('execution-worker', state)
    need(state['id'] == cid and state['network_mode'] == 'none' and
         state['project'] == 'platform-staging' and state['service'] == 'execution-worker', 'worker-isolation-invalid')
    mounts = [m for m in state['mounts'] if m.get('Destination') == '/data']
    need(len(mounts) == 1 and mounts[0].get('Type') == 'volume' and
         mounts[0].get('Name') == 'platform-staging_staging-data', 'staging-volume-invalid')
    health = run('worker-health', ['docker', 'exec', cid, 'cat', '/run/tw-quant-execution/health.json'], parser=parse)
    namespace['check_health'](health, state['started'])
    ready = b'print("P8_PYTHON_READY")\n'
    for case, argv, data in (
        ('python-argv', ['docker', 'exec', cid, 'python', '-c', ready.decode()], None),
        ('python-stdin', ['docker', 'exec', '-i', cid, 'python', '-'], ready),
        ('durable', ['docker', 'exec', '-i', cid, 'python', '-', 'snapshot'], probe),
    ):
        print('P8_DIAGNOSTIC_CASE=' + case, file=sys.stderr, flush=True)
        value = run('durable-snapshot', argv, data=data, parser=parse if case == 'durable' else str.strip)
        if case == 'durable':
            need(set(value) == {'locked_target_sha256', 'sqlite_integrity', 'target_count', 'active_targets'} and
                 re.fullmatch('[0-9a-f]{64}', value['locked_target_sha256']) is not None and
                 value['sqlite_integrity'] == 'ok' and type(value['target_count']) is int and value['target_count'] == 1 and
                 type(value['active_targets']) is int and value['active_targets'] == 0, 'durable-state-invalid')
        else:
            need(value == 'P8_PYTHON_READY', 'diagnostic-result-invalid')
    # Keep host markers on the collector's stream; SSH transports stdout and
    # stderr independently, so merging them does not preserve cross-stream order.
    print('P8_DIAGNOSTIC=PASS acceptance=false', file=sys.stderr, flush=True)


def payload():
    # Ship reviewed source in memory; never read/copy factory, env or private artifacts.
    collector = (HERE / 'acceptance_evidence.py').read_text()
    probe = (HERE / 'durable_probe.py').read_bytes()
    return ("import sys\n"
            "print('P8_DIAGNOSTIC_HOST=START', file=sys.stderr, flush=True)\n"
            "namespace = {'__name__': 'p8_diagnostic'}\n"
            'exec(compile(' + repr(collector) + ", '<reviewed-collector>', 'exec'), namespace)\n" +
            'probe = ' + repr(probe) + '\n' + inspect.getsource(remote_main) +
            "\nnamespace['entrypoint'](lambda: remote_main(namespace, probe))\n").encode()


def ssh_command(directory, env):
    host, user = env.get('STAGING_HOST', ''), env.get('STAGING_USER', '')
    if (not re.fullmatch('[A-Za-z0-9][A-Za-z0-9.:-]{0,252}', host) or
            not re.fullmatch('[a-z_][a-z0-9_-]{0,31}', user) or
            not env.get('STAGING_SSH_PRIVATE_KEY') or not env.get('STAGING_SSH_HOST_KEY')):
        raise ValueError('ssh-config-unavailable')
    key, known = directory / 'key', directory / 'known_hosts'
    for path, data in ((key, env['STAGING_SSH_PRIVATE_KEY']), (known, env['STAGING_SSH_HOST_KEY'])):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(data + '\n')
    return ['ssh', '-F', '/dev/null', '-T', '-i', str(key), '-l', user,
            '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes', '-o', 'ForwardAgent=no',
            '-o', 'StrictHostKeyChecking=yes', '-o', 'UserKnownHostsFile=' + str(known),
            '-o', 'GlobalKnownHostsFile=/dev/null', '-o', 'ConnectTimeout=15',
            '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2',
            host, 'sudo -n python3 -B -']


def safe_lines(raw):
    return [line.decode('ascii').rstrip('\n') for line in raw[:65536].splitlines(keepends=True)[:128]
            if len(line) <= 256 and SAFE_LINE.fullmatch(line.decode('ascii', errors='replace'))]


def inspect_once():
    started = time.monotonic_ns()
    lines, reason, sha = [], 'control-gate-failed', None
    try:
        sha = check_request()
        reason = 'ssh-config-unavailable'
        with tempfile.TemporaryDirectory(prefix='p8-readonly-') as directory, tempfile.TemporaryFile() as output:
            argv = ssh_command(Path(directory), os.environ)
            data = payload()
            reason = 'transport-failed'
            try:
                result = subprocess.run(argv, input=data, stdout=output, stderr=output, timeout=120,
                    env={'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'LANG': 'C'})
                reason = ('transport-failed' if result.returncode == 255 else
                          'remote-failed' if result.returncode else 'incomplete-output')
            except subprocess.TimeoutExpired:
                reason = 'transport-timeout'
            finally:
                output.seek(0)
                raw = output.read(65537)
                lines = safe_lines(raw)
            if len(raw) > 65536:
                reason = 'output-limit'
            elif reason == 'incomplete-output':
                # Completion requires every ordered evidence sequence, one host
                # start, one success marker and no failure. Do not use the last
                # merged line: a collector record can arrive after stdout's
                # success marker even though it was emitted first (run #2).
                cases = [line for line in lines if line.startswith('P8_DIAGNOSTIC_CASE=')]
                collected = [line.split('step=')[1].split()[0] for line in lines
                             if line.startswith('P8_EVIDENCE_STEP=PASS')]
                finished = [line.split('step=')[1].split()[0] for line in lines
                            if line.startswith('P8_DURABLE_STEP=PASS')]
                if (cases == ['P8_DIAGNOSTIC_CASE=' + case for case in CASES] and
                        collected == ['container-query-execution-worker', 'container-inspect-execution-worker',
                                      'worker-health', 'durable-snapshot', 'durable-snapshot', 'durable-snapshot'] and
                        finished == list(STAGES) and lines.count('P8_DIAGNOSTIC_HOST=START') == 1 and
                        lines.count('P8_DIAGNOSTIC=PASS acceptance=false') == 1 and
                        not any(line.startswith(('P8_EVIDENCE=FAIL', 'P8_EVIDENCE_FAIL', 'P8_DURABLE_STEP=FAIL')) for line in lines)):
                    reason = 'completed'
    except Exception:
        # No raw exceptions or transport output, including authentication errors.
        pass
    document = {'schema_version': 1, 'result': 'PASS' if reason == 'completed' else 'FAIL',
                'reason': reason, 'acceptance': False, 'elapsed_ms': (time.monotonic_ns() - started) // 1_000_000,
                'stages': lines}
    if sha is not None:
        document.update(pipeline_sha=sha, run_id=os.environ['GITHUB_RUN_ID'], attempt=1)
    for line in lines:
        print(line, flush=True)
    Path(os.environ['RUNNER_TEMP'], 'p8-readonly-diagnostics.json').write_text(json.dumps(document, sort_keys=True) + '\n')
    print('P8_READONLY_DIAGNOSTICS=' + document['result'] + ' reason=' + reason + ' acceptance=false', flush=True)
    return 0 if reason == 'completed' else 1


if __name__ == '__main__':
    try:
        if sys.argv[1:] == ['check']:
            check_request()
            print('P8_DIAGNOSTIC_REQUEST=PASS')
        elif sys.argv[1:] == ['inspect']:
            raise SystemExit(inspect_once())
        else:
            raise ValueError('invalid-operation')
    except Exception:
        raise SystemExit('P8_READONLY_DIAGNOSTICS=FAIL reason=control-gate-failed acceptance=false')

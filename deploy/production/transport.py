"""Runner-side strict SSH transport; read-only preflight precedes bundle writes."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import socket
import selectors
import time
import urllib.parse
import subprocess
import sys
import tarfile
import tempfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def require(condition, code):
    if not condition:
        raise ValueError(code)


def run(argv, data=None, timeout=60):
    r = subprocess.run(argv, input=data, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout)
    require(r.returncode == 0, 'ssh-operation-failed')
    return r.stdout


def b64(value):
    return base64.b64encode(value).decode()


def command(mode, gate=None):
    hashes = json.loads(os.environ['PRODUCTION_APPROVED_CONFIG_SHA256_JSON'])
    require(set(hashes) == {'market.env', 'execution.env', 'gateway.env', 'factory', 'replay.csv'} and
            all(re.fullmatch('[0-9a-f]{64}', str(v)) for v in hashes.values()), 'config-approval-missing')
    rollback = os.environ['LEGACY_ROLLBACK_INVENTORY_SHA256']
    require(re.fullmatch('[0-9a-f]{64}', rollback), 'rollback-approval-missing')
    args = [mode, '--pins', b64((HERE / 'approved-p8.json').read_bytes()), '--rollback-sha256', rollback,
            '--config-hashes', b64(json.dumps(hashes).encode()), '--durable-code', b64((HERE / 'durable_state.py').read_bytes())]
    if gate is not None:
        args += ['--gate', b64(gate)]
    return ' '.join(shlex.quote(a) for a in args)


def configure():
    host, user = os.environ['PRODUCTION_HOST'], os.environ['PRODUCTION_USER']
    require(re.fullmatch('[a-zA-Z0-9.-]+', host) and re.fullmatch('[a-z_][a-z0-9_-]*', user), 'ssh-target-invalid')
    require(host != os.environ.get('STAGING_HOST_IDENTITY') and os.environ.get('STAGING_HOST_IDENTITY'), 'production-host-not-isolated')
    production_addresses = {x[4][0] for x in socket.getaddrinfo(host, None)}
    staging_addresses = {x[4][0] for x in socket.getaddrinfo(os.environ['STAGING_HOST_IDENTITY'], None)}
    require(production_addresses and staging_addresses and not production_addresses & staging_addresses, 'production-staging-host-overlap')
    key, known = os.environ['PRODUCTION_SSH_PRIVATE_KEY'], os.environ['PRODUCTION_SSH_HOST_KEY']
    require(key and known and '\n' not in host and '\n' not in user, 'ssh-key-missing')
    directory = Path(os.environ['RUNNER_TEMP']) / 'p9-ssh'
    directory.mkdir(mode=0o700, exist_ok=True)
    (directory / 'key').write_text(key); (directory / 'known_hosts').write_text(known)
    for name in ('key', 'known_hosts'):
        os.chmod(directory / name, 0o600)
    return ['ssh', '-i', str(directory / 'key'), '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'UserKnownHostsFile=' + str(directory / 'known_hosts'), '-o', 'ConnectTimeout=15',
            '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=4', user + '@' + host]


def in_memory():
    helper = (ROOT / 'deploy/staging/image_config_digest.py').read_bytes()
    return ("import base64,sys,types\nm=types.ModuleType('image_config_digest')\n"
            "exec(base64.b64decode(" + repr(b64(helper)) + "),m.__dict__)\n"
            "sys.modules['image_config_digest']=m\n").encode() + (HERE / 'cutover.py').read_bytes()


def main():
    p = argparse.ArgumentParser()
    p.add_argument('mode', choices=('cutover', 'recover', 'capture'))
    p.add_argument('--evidence', type=Path, required=True)
    args = p.parse_args()
    ssh = configure()
    gate = (args.evidence / 'gate.json').read_bytes()
    lock = 'sudo flock -w 30 /var/lock/tw-quant-deploy.lock '
    if args.mode == 'capture':
        # Only selected sanitized JSON, never rollback archives/config/provider details.
        args.evidence.joinpath('host').mkdir(mode=0o700, exist_ok=True)
        for name in ('acceptance.json', 'failure.json', 'rollback-result.json', 'transaction.json'):
            r = subprocess.run(ssh + ['sudo cat /srv/trading-platform-p9/' + name],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30)
            if r.returncode == 0:
                require(len(r.stdout) <= 1024 * 1024, 'evidence-too-large')
                json.loads(r.stdout)
                (args.evidence / 'host' / name).write_bytes(r.stdout)
        return
    if args.mode == 'recover':
        # Recover uses current reviewed bytes from the runner, not possibly partial upload.
        remote = 'sudo flock -w 300 /var/lock/tw-quant-deploy.lock python3 - ' + command(args.mode, gate)
        run(ssh + [remote], in_memory(), timeout=600)
        return
    # Verify gates twice: immediately before host preflight and again before writes.
    pins = json.loads((HERE / 'approved-p8.json').read_text())
    from evidence import api, master_gates, verify_acceptance
    require(api('git/ref/heads/master')['object']['sha'] == json.loads(gate)['control_sha'], 'master-moved')
    for sha in {pins['platform_sha'], json.loads(gate)['control_sha']}:
        master_gates(sha)
    verify_acceptance({k: (args.evidence / k).read_bytes() for k in pins['acceptance_files']}, pins)
    run(ssh + [lock + 'python3 - ' + command('preflight')], in_memory())
    require(api('git/ref/heads/master')['object']['sha'] == json.loads(gate)['control_sha'], 'master-moved')
    for sha in {pins['platform_sha'], json.loads(gate)['control_sha']}:
        master_gates(sha)
    # Bundle is control-plane code only; runtime images remain pinned to P8 source.
    with tempfile.TemporaryFile() as f:
        with tarfile.open(fileobj=f, mode='w:gz') as z:
            for name in ('Caddyfile', 'docker-compose.yml', 'approved-p8.json', 'cutover.py', 'durable_state.py'):
                z.add(HERE / name, arcname=name, recursive=False)
            z.add(ROOT / 'deploy/staging/image_config_digest.py', arcname='image_config_digest.py', recursive=False)
        f.seek(0); bundle = f.read()
    install = ('sudo install -d -m 700 /srv/trading-platform-p9/bundle && '
               'sudo tar -xzf - -C /srv/trading-platform-p9/bundle --no-same-owner && '
               'sudo chmod -R go-rwx /srv/trading-platform-p9/bundle && '
               'sudo chmod 755 /srv/trading-platform-p9/bundle && '
               'sudo chmod 644 /srv/trading-platform-p9/bundle/Caddyfile')
    run(ssh + [install], bundle)
    # Token is stdin-only, temporary registry auth is removed on success/failure.
    actor = os.environ['GITHUB_ACTOR']
    require(re.fullmatch('[A-Za-z0-9-]+', actor), 'registry-actor')
    script = ('set -eu; registry_dir=$(mktemp -d /run/p9-registry.XXXXXX); '
              'trap \'rm -rf "$registry_dir"\' EXIT; export DOCKER_CONFIG="$registry_dir"; '
              'IFS= read -r registry_token; printf \'%s\' \"$registry_token\" | docker login ghcr.io --username ' + shlex.quote(actor) + ' --password-stdin >/dev/null; unset registry_token; '
              'timeout --signal=TERM --kill-after=90s 15m flock -w 30 /var/lock/tw-quant-deploy.lock python3 /srv/trading-platform-p9/bundle/cutover.py ' + command('cutover', gate))
    # Mark attempts before sending SSH so interrupted clients still invoke independent recovery.
    (args.evidence / 'cutover-attempted').write_text('yes\n')
    process = subprocess.Popen(ssh + ['sudo bash -c ' + shlex.quote(script)], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    ready = False
    external_failed = False
    deadline = time.monotonic() + 1200
    try:
        process.stdin.write(os.environ['GITHUB_TOKEN'].encode() + b'\n'); process.stdin.flush()
        while process.poll() is None:
            require(time.monotonic() < deadline, 'cutover-transport-deadline')
            if not selector.select(timeout=1):
                continue
            line = process.stdout.readline(4097)
            require(len(line) <= 4096, 'unexpected-host-output')
            if line.startswith(b'P9_READY_FOR_EXTERNAL_GATE '):
                require(not ready, 'duplicate-external-gate-request')
                ready = True
                try:
                    domain = line.decode('ascii').strip().split(' ', 1)[1]
                    origin = urllib.parse.urlparse(os.environ['PUBLIC_DASHBOARD_URL'])
                    require(re.fullmatch('[a-z0-9.-]{1,253}', domain) and origin.scheme == 'https' and
                            origin.hostname == domain and origin.port in (None, 443) and
                            origin.path in ('', '/') and not origin.query and not origin.fragment and
                            origin.username is None and origin.password is None, 'external-origin-identity')
                    run([sys.executable, str(ROOT / 'deploy/lightsail/verify-public-origin.py'),
                         '--base-url', os.environ['PUBLIC_DASHBOARD_URL'], '--attempts', '1',
                         '--timeout', '10', '--retry-delay', '0'], timeout=30)
                    ack = 'P9_EXTERNAL_GATE_PASS ' + pins['manifest_sha256']
                except Exception:
                    external_failed = True
                    ack = 'P9_EXTERNAL_GATE_FAIL'
                process.stdin.write((ack + '\n').encode()); process.stdin.flush()
        require(process.returncode == 0 and ready and not external_failed, 'cutover-or-public-gate-failed')
    finally:
        selector.close()
        process.stdin.close(); process.stdout.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait()



if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('P9_TRANSPORT=FAIL')

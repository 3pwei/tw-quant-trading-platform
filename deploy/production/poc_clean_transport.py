"""Runner-side transport for the independently gated P9 PoC clean deploy."""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
HASH_NAMES = frozenset({'market.env', 'execution.env', 'gateway.env', 'factory'})


def require(condition, code):
    if not condition:
        raise ValueError(code)


def run(argv, data=None, timeout=60):
    result = subprocess.run(argv, input=data, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, timeout=timeout)
    require(result.returncode == 0, 'ssh-operation-failed')
    return result.stdout


def b64(value):
    return base64.b64encode(value).decode()


def command(mode, gate=None):
    hashes = json.loads(os.environ['PRODUCTION_POC_APPROVED_CONFIG_SHA256_JSON'])
    require(set(hashes) == HASH_NAMES and
            all(re.fullmatch('[0-9a-f]{64}', str(value)) for value in hashes.values()),
            'clean-config-approval-missing')
    arguments = [
        mode, '--pins', b64((HERE / 'approved-p8.json').read_bytes()),
        '--config-hashes', b64(json.dumps(hashes).encode()),
        '--durable-code', b64((HERE / 'durable_state.py').read_bytes()),
    ]
    if gate is not None:
        arguments += ['--gate', b64(gate)]
    return ' '.join(shlex.quote(value) for value in arguments)


def configure():
    host, user = os.environ['PRODUCTION_HOST'], os.environ['PRODUCTION_USER']
    require(re.fullmatch('[a-zA-Z0-9.-]+', host) and
            re.fullmatch('[a-z_][a-z0-9_-]*', user), 'ssh-target-invalid')
    staging = os.environ.get('STAGING_HOST_IDENTITY')
    require(staging and host != staging, 'production-host-not-isolated')
    production_addresses = {item[4][0] for item in socket.getaddrinfo(host, None)}
    staging_addresses = {item[4][0] for item in socket.getaddrinfo(staging, None)}
    require(production_addresses and staging_addresses and
            not production_addresses & staging_addresses,
            'production-staging-host-overlap')
    key = os.environ['PRODUCTION_SSH_PRIVATE_KEY']
    known = os.environ['PRODUCTION_SSH_HOST_KEY']
    require(key and known and '\n' not in host and '\n' not in user, 'ssh-key-missing')
    directory = Path(os.environ['RUNNER_TEMP']) / 'p9-poc-clean-ssh'
    directory.mkdir(mode=0o700, exist_ok=True)
    (directory / 'key').write_text(key)
    (directory / 'known_hosts').write_text(known)
    for name in ('key', 'known_hosts'):
        os.chmod(directory / name, 0o600)
    return [
        'ssh', '-i', str(directory / 'key'), '-o', 'BatchMode=yes',
        '-o', 'StrictHostKeyChecking=yes',
        '-o', 'UserKnownHostsFile=' + str(directory / 'known_hosts'),
        '-o', 'ConnectTimeout=15', '-o', 'ServerAliveInterval=15',
        '-o', 'ServerAliveCountMax=4', user + '@' + host,
    ]


def in_memory():
    image_helper = (ROOT / 'deploy/staging/image_config_digest.py').read_bytes()
    cutover = (HERE / 'cutover.py').read_bytes()
    clean = (HERE / 'poc_clean.py').read_bytes()
    prefix = (
        "import base64,sys,types\n"
        "image=types.ModuleType('image_config_digest')\n"
        "exec(base64.b64decode(" + repr(b64(image_helper)) + "),image.__dict__)\n"
        "sys.modules['image_config_digest']=image\n"
        "cutover=types.ModuleType('cutover')\n"
        "exec(base64.b64decode(" + repr(b64(cutover)) + "),cutover.__dict__)\n"
        "sys.modules['cutover']=cutover\n"
    ).encode()
    return prefix + clean


def capture(ssh, evidence):
    evidence.joinpath('host').mkdir(mode=0o700, exist_ok=True)
    for name in ('acceptance-clean.json', 'failure-clean.json', 'backup-clean.json',
                 'rollback-clean-result.json', 'transaction-clean.json'):
        result = subprocess.run(
            ssh + ['sudo cat /srv/trading-platform-production/' + name],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30,
        )
        if result.returncode == 0:
            require(len(result.stdout) <= 1024 * 1024, 'evidence-too-large')
            document = json.loads(result.stdout)
            require('email' not in json.dumps(document).lower() and
                    'credential' not in json.dumps(document).lower(),
                    'unsafe-host-evidence')
            (evidence / 'host' / name).write_bytes(result.stdout)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('deploy', 'recover', 'capture'))
    parser.add_argument('--evidence', type=Path, required=True)
    arguments = parser.parse_args()
    ssh = configure()
    gate = (arguments.evidence / 'gate.json').read_bytes()
    lock = 'sudo flock -w 30 /var/lock/tw-quant-deploy.lock '
    if arguments.mode == 'capture':
        capture(ssh, arguments.evidence)
        return
    if arguments.mode == 'recover':
        remote = ('sudo flock -w 300 /var/lock/tw-quant-deploy.lock python3 - ' +
                  command('recover', gate))
        run(ssh + [remote], in_memory(), timeout=600)
        return

    pins = json.loads((HERE / 'approved-p8.json').read_text())
    gate_document = json.loads(gate)
    from evidence import api, master_gates, verify_acceptance
    require(pins['platform_sha'] == gate_document['platform_sha'] and
            gate_document['control_sha'], 'clean-p8-control-sha-mismatch')
    require(api('git/ref/heads/master')['object']['sha'] ==
            gate_document['control_sha'], 'master-moved')
    for revision in {pins['platform_sha'], gate_document['control_sha']}:
        master_gates(revision)
    verify_acceptance({name: (arguments.evidence / name).read_bytes()
                       for name in pins['acceptance_files']}, pins)
    run(ssh + [lock + 'python3 - ' + command('preflight', gate)], in_memory())
    require(api('git/ref/heads/master')['object']['sha'] ==
            gate_document['control_sha'], 'master-moved')
    for revision in {pins['platform_sha'], gate_document['control_sha']}:
        master_gates(revision)

    with tempfile.TemporaryFile() as stream:
        with tarfile.open(fileobj=stream, mode='w:gz') as archive:
            files = {
                'Caddyfile': HERE / 'Caddyfile',
                'docker-compose.yml': HERE / 'docker-compose.poc-clean.yml',
                'approved-p8.json': HERE / 'approved-p8.json',
                'cutover.py': HERE / 'cutover.py',
                'poc_clean.py': HERE / 'poc_clean.py',
                'durable_state.py': HERE / 'durable_state.py',
                'image_config_digest.py': ROOT / 'deploy/staging/image_config_digest.py',
                'shioaji_capability.py': ROOT / 'deploy/staging/shioaji_capability.py',
            }
            for archive_name, source in files.items():
                archive.add(source, arcname=archive_name, recursive=False)
        stream.seek(0)
        bundle = stream.read()
    install = (
        'sudo install -d -m 700 /srv/trading-platform-production/bundle-poc-clean && '
        'sudo tar -xzf - -C /srv/trading-platform-production/bundle-poc-clean '
        '--no-same-owner && '
        'sudo chmod -R go-rwx /srv/trading-platform-production/bundle-poc-clean && '
        'sudo chmod 755 /srv/trading-platform-production/bundle-poc-clean && '
        'sudo chmod 644 /srv/trading-platform-production/bundle-poc-clean/Caddyfile'
    )
    run(ssh + [install], bundle)
    actor = os.environ['GITHUB_ACTOR']
    require(re.fullmatch('[A-Za-z0-9-]+', actor), 'registry-actor')
    script = (
        'set -eu; registry_dir=$(mktemp -d /run/p9-poc-clean-registry.XXXXXX); '
        'trap \'rm -rf "$registry_dir"\' EXIT; export DOCKER_CONFIG="$registry_dir"; '
        'IFS= read -r registry_token; printf \'%s\' "$registry_token" | '
        'docker login ghcr.io --username ' + shlex.quote(actor) +
        ' --password-stdin >/dev/null; unset registry_token; '
        'timeout --signal=TERM --kill-after=90s 20m flock -w 30 '
        '/var/lock/tw-quant-deploy.lock python3 '
        '/srv/trading-platform-production/bundle-poc-clean/poc_clean.py ' +
        command('deploy', gate)
    )
    (arguments.evidence / 'clean-deploy-attempted').write_text('yes\n')
    process = subprocess.Popen(
        ssh + ['sudo bash -c ' + shlex.quote(script)], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    ready = False
    external_failed = False
    deadline = time.monotonic() + 1500
    try:
        process.stdin.write(os.environ['GITHUB_TOKEN'].encode() + b'\n')
        process.stdin.flush()
        while process.poll() is None:
            require(time.monotonic() < deadline, 'clean-deploy-transport-deadline')
            if not selector.select(timeout=1):
                continue
            line = process.stdout.readline(4097)
            require(len(line) <= 4096, 'unexpected-host-output')
            if line.startswith(b'P9_POC_CLEAN_READY_FOR_EXTERNAL_GATE '):
                require(not ready, 'duplicate-external-gate-request')
                ready = True
                try:
                    domain = line.decode('ascii').strip().split(' ', 1)[1]
                    origin = urllib.parse.urlparse(os.environ['PUBLIC_DASHBOARD_URL'])
                    require(re.fullmatch('[a-z0-9.-]{1,253}', domain) and
                            origin.scheme == 'https' and origin.hostname == domain and
                            origin.port in (None, 443) and origin.path in ('', '/') and
                            not origin.query and not origin.fragment and
                            origin.username is None and origin.password is None,
                            'external-origin-identity')
                    run([sys.executable,
                         str(ROOT / 'deploy/lightsail/verify-public-origin.py'),
                         '--base-url', os.environ['PUBLIC_DASHBOARD_URL'],
                         '--attempts', '1', '--timeout', '10', '--retry-delay', '0'],
                        timeout=30)
                    acknowledgement = ('P9_POC_CLEAN_EXTERNAL_GATE_PASS ' +
                                       pins['manifest_sha256'])
                except Exception:
                    external_failed = True
                    acknowledgement = 'P9_POC_CLEAN_EXTERNAL_GATE_FAIL'
                process.stdin.write((acknowledgement + '\n').encode())
                process.stdin.flush()
        require(process.returncode == 0 and ready and not external_failed,
                'clean-deploy-or-public-gate-failed')
    finally:
        selector.close()
        process.stdin.close()
        process.stdout.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('P9_POC_CLEAN_TRANSPORT=FAIL')

"""Runner transport for first-time P9 clean PoC Production configuration."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import types


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
REMOTE_COMMAND = 'sudo -n env PYTHONDONTWRITEBYTECODE=1 python3 -B -'
SOURCES = {
    'market.env': 'PRODUCTION_POC_MARKET_ENV_B64',
    'execution.env': 'PRODUCTION_POC_EXECUTION_ENV_B64',
    'gateway.env': 'PRODUCTION_POC_GATEWAY_ENV_B64',
    'factory': 'PRODUCTION_POC_PROVIDER_FACTORY_B64',
}
HOST_REASONS = {
    'payload-invalid': 'prepare',
    'deployment-lock-unavailable': 'lock',
    'production-parent-invalid': 'prepare',
    'production-root-exists': 'prepare',
    'production-prepare-state-exists': 'prepare',
    'config-validation-failed': 'config',
    'config-write-failed': 'config',
    'owner-mode-invalid': 'publish',
    'atomic-publish-unavailable': 'publish',
    'atomic-publish-failed': 'publish',
    'host-prepare-failed': 'host',
    **{reason: 'config' for reason in (
        'clean-config-approval-missing', 'config-isolation', 'config-digest',
        'factory-invalid', 'clean-market-provider-not-shioaji',
        'clean-required-setting-missing', 'clean-shioaji-production-required',
        'clean-production-auth-missing', 'clean-admin-email-invalid',
        'clean-shioaji-replay-not-required', 'provider-config-provenance',
        'production-data-path', 'market-credential-isolation', 'staging-config-reuse',
        'real-order-enabled', 'live-confirmation-present', 'execution-not-disabled',
    )},
}


class PrepareBlocked(Exception):
    def __init__(self, reason, stage=None):
        super().__init__(reason)
        self.stage = stage


def require(condition, reason):
    if not condition:
        raise PrepareBlocked(reason)


def no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'unsafe-host-evidence')
        result[key] = value
    return result


def module(name, source):
    encoded = base64.b64encode(source).decode()
    return (f"m=types.ModuleType({name!r})\n"
            f"m.__file__={name!r}\n"
            f"exec(compile(base64.b64decode({encoded!r}), {name!r}, 'exec'),m.__dict__)\n"
            f"sys.modules[{name!r}]=m\n")


def sources():
    result = {}
    for label, variable in SOURCES.items():
        value = os.environ.get(variable, '')
        require(value and len(value) <= 2 * 1024 * 1024 and
                re.fullmatch('[A-Za-z0-9+/=]+', value) is not None,
                'production-config-source-missing')
        try:
            raw = base64.b64decode(value, validate=True)
        except Exception:
            raise PrepareBlocked('production-config-source-invalid') from None
        require(raw and len(raw) <= 1024 * 1024,
                'production-config-source-invalid')
        result[label] = value
    return result


def remote_program(payload):
    program = b'import base64,json,sys,types\n'
    for name, path in (
        ('cutover', HERE / 'cutover.py'),
        ('poc_clean', HERE / 'poc_clean.py'),
        ('poc_clean_prepare_host', HERE / 'poc_clean_prepare_host.py'),
    ):
        program += module(name, path.read_bytes()).encode()
    encoded = base64.b64encode(json.dumps(payload, sort_keys=True).encode()).decode()
    program += (
        f"allowed={HOST_REASONS!r}\n"
        "host=sys.modules['poc_clean_prepare_host']\n"
        "try:\n"
        f" payload=json.loads(base64.b64decode({encoded!r}),object_pairs_hook="
        "host.no_duplicate_keys)\n"
        " result=host.prepare(payload)\n"
        "except Exception as exc:\n"
        " reason=str(exc) if isinstance(exc,host.PrepareBlocked) else ''\n"
        " if reason not in allowed: reason='host-prepare-failed'\n"
        " result={'status':'BLOCKED','stage':allowed[reason],'reason':reason}\n"
        "print(json.dumps(result,sort_keys=True))\n"
        "raise SystemExit(0 if result.get('status')=='PASS' else 1)\n"
    ).encode()
    return program


def safe_evidence(document):
    require(isinstance(document, dict) and set(document) ==
            {'status', 'root', 'config_sha256', 'market_provider', 'real_order'} and
            document['status'] == 'PASS' and
            document['root'] == '/srv/trading-platform-production' and
            document['market_provider'] == 'shioaji' and
            document['real_order'] == 'disabled' and
            set(document['config_sha256']) == set(SOURCES) and
            all(re.fullmatch('[0-9a-f]{64}', value)
                for value in document['config_sha256'].values()),
            'unsafe-host-evidence')
    return document


def host_response(result):
    require(result.returncode in (0, 1) and 0 < len(result.stdout) <= 8192,
            'ssh-prepare-failed')
    try:
        document = json.loads(result.stdout, object_pairs_hook=no_duplicate_keys)
    except Exception:
        raise PrepareBlocked('unsafe-host-evidence') from None
    if document.get('status') == 'BLOCKED':
        reason = document.get('reason')
        require(result.returncode == 1 and
                set(document) == {'status', 'stage', 'reason'} and
                reason in HOST_REASONS and document['stage'] == HOST_REASONS[reason],
                'unsafe-host-evidence')
        raise PrepareBlocked(reason, document['stage'])
    require(result.returncode == 0, 'unsafe-host-evidence')
    return safe_evidence(document)


def inspect_once():
    from evidence import api, master_gates
    import poc_clean_transport

    require(os.environ.get('GITHUB_EVENT_NAME') == 'workflow_dispatch' and
            os.environ.get('GITHUB_REF') == 'refs/heads/master',
            'control-gate-failed')
    sha = os.environ.get('GITHUB_SHA', '')
    require(re.fullmatch('[0-9a-f]{40}', sha) is not None, 'control-gate-failed')
    require(os.environ.get('PRODUCTION_POC_PREPARE_CONFIRMATION') ==
            'PREPARE P9 POC CLEAN production ' + sha,
            'confirmation-mismatch')
    pins = json.loads((HERE / 'approved-p8.json').read_text())
    try:
        require(api('git/ref/heads/master')['object']['sha'] == sha, 'master-moved')
        master_gates(sha)
        master_gates(pins['platform_sha'])
        require(subprocess.run(
            ['git', 'merge-base', '--is-ancestor', pins['platform_sha'], sha],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False).returncode == 0, 'control-gate-failed')
        changed = subprocess.run(
            ['git', 'diff', '--name-only', pins['platform_sha'], sha, '--', '.'],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=30, check=False)
        require(changed.returncode == 0 and
                set(changed.stdout.decode().splitlines()) <=
                {'deploy/production/approved-p8.json', 'public-candidate.json'},
                'control-gate-failed')
        ssh = poc_clean_transport.configure()
        require(api('git/ref/heads/master')['object']['sha'] == sha, 'master-moved')
    except PrepareBlocked:
        raise
    except Exception:
        raise PrepareBlocked('control-gate-failed') from None
    payload = {'control_sha': sha, 'pins': pins, 'sources': sources()}
    try:
        result = subprocess.run(ssh + [REMOTE_COMMAND], input=remote_program(payload),
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=180, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise PrepareBlocked('ssh-prepare-failed') from None
    return host_response(result)


def main():
    output = Path(os.environ.get('RUNNER_TEMP', '.')) / 'p9-poc-clean-prepare.json'
    try:
        require(sys.argv[1:] == ['--runner'], 'runner-only')
        document = inspect_once()
        code = 0
    except PrepareBlocked as exc:
        reason = str(exc)
        allowed = {'runner-only', 'control-gate-failed', 'confirmation-mismatch',
                   'master-moved', 'production-config-source-missing',
                   'production-config-source-invalid', 'ssh-prepare-failed',
                   'unsafe-host-evidence'}
        document = {'status': 'BLOCKED',
                    'reason': reason if reason in allowed else 'unsafe-host-evidence'}
        if reason in HOST_REASONS and exc.stage == HOST_REASONS[reason]:
            document = {'status': 'BLOCKED', 'stage': exc.stage, 'reason': reason}
        code = 1
    except Exception:
        document = {'status': 'BLOCKED', 'reason': 'unsafe-host-evidence'}
        code = 1
    output.write_text(json.dumps(document, sort_keys=True) + '\n')
    os.chmod(output, 0o600)
    if code == 0:
        print('P9_POC_CLEAN_PREPARE=PASS')
    else:
        print(json.dumps(document, sort_keys=True))
    return code


if __name__ == '__main__':
    raise SystemExit(main())

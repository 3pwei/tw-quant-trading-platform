"""Manual runner for first-time Production prerequisite provisioning.

The runner requires exact current master gates and an explicit confirmation. Five
Production-only source files come only from lightsail-production secrets, are sent
through SSH stdin, and are never printed. The host implementation refuses an
existing final root and publishes it only after complete validation.
"""
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
    'market.env': 'PRODUCTION_MARKET_ENV_B64',
    'execution.env': 'PRODUCTION_EXECUTION_ENV_B64',
    'gateway.env': 'PRODUCTION_GATEWAY_ENV_B64',
    'factory': 'PRODUCTION_PROVIDER_FACTORY_B64',
    'replay.csv': 'PRODUCTION_REPLAY_CSV_B64',
}
MAX_ENCODED = 256 * 1024
HOST_REASONS = {
    'payload-invalid': 'prepare',
    'production-root-exists': 'prepare',
    'deployment-lock-unavailable': 'lock',
    'legacy-revision-mismatch': 'legacy',
    'legacy-container-mismatch': 'legacy',
    'legacy-env-invalid': 'legacy',
    'legacy-safety-gate': 'legacy',
    'legacy-market-provider': 'legacy',
    'database-boundary': 'snapshot',
    'sqlite-snapshot-failed': 'snapshot',
    'config-validation-failed': 'config',
    'legacy-config-invalid': 'rollback',
    'rollback-capture-failed': 'rollback',
    'owner-mode-invalid': 'publish',
    'atomic-publish-unavailable': 'publish',
    'atomic-publish-failed': 'publish',
    'host-command-failed': 'host',
    'write-failed': 'host',
    'host-provision-failed': 'host',
}


class ProvisionBlocked(Exception):
    def __init__(self, reason, stage=None):
        super().__init__(reason)
        self.stage = stage


def require(condition, reason):
    if not condition:
        raise ProvisionBlocked(reason)


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
    for label, name in SOURCES.items():
        value = os.environ.get(name, '')
        require(value and len(value) <= MAX_ENCODED and
                re.fullmatch(r'[A-Za-z0-9+/=]+', value) is not None,
                'production-config-source-missing')
        try:
            raw = base64.b64decode(value, validate=True)
        except Exception:
            raise ProvisionBlocked('production-config-source-invalid') from None
        require(raw and len(raw) <= 128 * 1024 * 1024,
                'production-config-source-invalid')
        result[label] = value
    return result


def remote_program(payload):
    program = b'import base64,json,sys,types\n'
    for name, path in (
        ('image_config_digest', ROOT / 'deploy/staging/image_config_digest.py'),
        ('readonly_sqlite', HERE / 'readonly_sqlite.py'),
        ('p9_validation', HERE / 'cutover.py'),
        ('provision_host', HERE / 'provision_host.py'),
    ):
        program += module(name, path.read_bytes()).encode()
    encoded = base64.b64encode(json.dumps(payload, sort_keys=True).encode()).decode()
    program += (
        f"allowed={HOST_REASONS!r}\n"
        "host=sys.modules['provision_host']\n"
        "try:\n"
        f" payload=json.loads(base64.b64decode({encoded!r}),object_pairs_hook="
        "sys.modules['provision_host'].no_duplicate_keys)\n"
        " result=host.provision(payload)\n"
        "except Exception as exc:\n"
        " reason=str(exc) if isinstance(exc,host.ProvisionBlocked) else ''\n"
        " if reason not in allowed: reason='host-provision-failed'\n"
        " result={'status':'BLOCKED','stage':allowed[reason],'reason':reason}\n"
        "print(json.dumps(result,sort_keys=True))\n"
        "raise SystemExit(0 if result.get('status')=='PASS' else 1)\n"
    ).encode()
    return program


def safe_evidence(document):
    require(isinstance(document, dict), 'unsafe-host-evidence')
    expected = {'status', 'root', 'legacy_revision', 'market_provider',
                'config_sha256', 'rollback_sha256', 'execution_locked',
                'external_order_calls', 'external_cancel_calls'}
    require(set(document) == expected and document['status'] == 'PASS' and
            document['root'] == '/srv/trading-platform-production' and
            re.fullmatch(r'[0-9a-f]{40}', document['legacy_revision']) and
            document['market_provider'] == 'shioaji' and
            document['execution_locked'] is True and
            document['external_order_calls'] == 0 and
            document['external_cancel_calls'] == 0,
            'unsafe-host-evidence')
    require(set(document['config_sha256']) ==
            {'market.env', 'execution.env', 'gateway.env', 'factory', 'replay.csv'},
            'unsafe-host-evidence')
    require(all(isinstance(v, str) and re.fullmatch(r'[0-9a-f]{64}', v)
                for v in document['config_sha256'].values()) and
            isinstance(document['rollback_sha256'], str) and
            re.fullmatch(r'[0-9a-f]{64}', document['rollback_sha256']),
            'unsafe-host-evidence')
    return document


def host_response(result):
    # SSH/bootstrap failures have no trusted protocol response. Never log stderr.
    require(result.returncode in (0, 1) and len(result.stdout) <= 8192,
            'ssh-provision-failed')
    if not result.stdout:
        raise ProvisionBlocked('ssh-provision-failed')
    try:
        document = json.loads(result.stdout, object_pairs_hook=no_duplicate_keys)
    except Exception:
        raise ProvisionBlocked('unsafe-host-evidence') from None
    require(isinstance(document, dict), 'unsafe-host-evidence')
    if document.get('status') == 'BLOCKED':
        reason = document.get('reason')
        require(result.returncode == 1 and
                set(document) == {'status', 'stage', 'reason'} and
                isinstance(reason, str) and reason in HOST_REASONS and
                document['stage'] == HOST_REASONS[reason], 'unsafe-host-evidence')
        raise ProvisionBlocked(reason, document['stage'])
    require(result.returncode == 0, 'unsafe-host-evidence')
    return safe_evidence(document)


def inspect_once():
    from evidence import api, master_gates
    import transport
    require(os.environ.get('GITHUB_EVENT_NAME') == 'workflow_dispatch' and
            os.environ.get('GITHUB_REF') == 'refs/heads/master',
            'control-gate-failed')
    sha = os.environ.get('GITHUB_SHA', '')
    require(re.fullmatch(r'[0-9a-f]{40}', sha) is not None,
            'control-gate-failed')
    require(os.environ.get('PRODUCTION_PROVISION_CONFIRMATION') ==
            'PROVISION production prerequisites ' + sha,
            'confirmation-mismatch')
    try:
        require(api('git/ref/heads/master')['object']['sha'] == sha, 'master-moved')
        master_gates(sha)
    except ProvisionBlocked:
        raise
    except Exception:
        raise ProvisionBlocked('control-gate-failed') from None
    payload = {
        'control_sha': sha,
        'pins': json.loads((HERE / 'approved-p8.json').read_text()),
        'sources': sources(),
    }
    try:
        ssh = transport.configure()
    except Exception:
        raise ProvisionBlocked('ssh-config-unavailable') from None
    try:
        require(api('git/ref/heads/master')['object']['sha'] == sha, 'master-moved')
    except ProvisionBlocked:
        raise
    except Exception:
        raise ProvisionBlocked('control-gate-failed') from None
    try:
        result = subprocess.run(
            ssh + [REMOTE_COMMAND],
            input=remote_program(payload),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=1500,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ProvisionBlocked('ssh-provision-failed') from None
    return host_response(result)


def main():
    output = Path(os.environ.get('RUNNER_TEMP', '.')) / 'production-prerequisite-provision.json'
    try:
        require(sys.argv[1:] == ['--runner'], 'runner-only')
        document = inspect_once()
        code = 0
    except ProvisionBlocked as exc:
        reason = str(exc)
        allowed = {'runner-only', 'control-gate-failed', 'confirmation-mismatch',
                   'master-moved', 'production-config-source-missing',
                   'production-config-source-invalid', 'ssh-config-unavailable',
                   'ssh-provision-failed', 'unsafe-host-evidence'}
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
    print(json.dumps(document, sort_keys=True))
    return code


if __name__ == '__main__':
    raise SystemExit(main())

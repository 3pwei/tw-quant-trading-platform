"""Manual, exact-master-gated rollback refresh runner. No automatic approval."""
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import provision_prerequisites as provision

HERE = Path(__file__).resolve().parent
REASONS = frozenset('''runner-only control-gate-failed confirmation-mismatch
master-moved refresh-approval-missing ssh-config-unavailable ssh-refresh-failed
unsafe-host-evidence refresh-invalid refresh-boundary refresh-prior-transaction
refresh-lock-unavailable refresh-approval-mismatch refresh-legacy-drift
refresh-snapshot-failed refresh-history-conflict refresh-archive-invalid
refresh-exchange-unavailable refresh-exchange-failed refresh-host-failed
refresh-incomplete-preparation'''.split())


class RefreshBlocked(Exception):
    pass


def require(condition, reason):
    if not condition:
        raise RefreshBlocked(reason)


def digest(value, length=64):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{' + str(length) + '}', value)


def hashes_valid(hashes):
    return (isinstance(hashes, dict) and set(hashes) == set(provision.SOURCES) and
            all(digest(v) for v in hashes.values()))


def remote_program(payload):
    program = b'import base64,json,sys,types\n'
    for name, path in (
        ('image_config_digest', provision.ROOT / 'deploy/staging/image_config_digest.py'),
        ('readonly_sqlite', HERE / 'readonly_sqlite.py'),
        ('p9_validation', HERE / 'cutover.py'),
        ('provision_host', HERE / 'provision_host.py'),
        ('refresh_rollback_host', HERE / 'refresh_rollback_host.py'),
    ):
        program += provision.module(name, path.read_bytes()).encode()
    encoded = base64.b64encode(json.dumps(payload, sort_keys=True).encode()).decode()
    program += (
        "host=sys.modules['refresh_rollback_host']\n"
        "try:\n"
        f" payload=json.loads(base64.b64decode({encoded!r}))\n"
        " result=host.refresh(payload)\n"
        "except Exception as exc:\n"
        " reason=str(exc) if isinstance(exc,host.RefreshBlocked) else ''\n"
        " if reason not in host.REASONS: reason='refresh-host-failed'\n"
        " result={'status':'BLOCKED','reason':reason}\n"
        "print(json.dumps(result,sort_keys=True))\n"
        "raise SystemExit(0 if result.get('status')=='PASS' else 1)\n"
    ).encode()
    return program


def response(result, previous, hashes):
    require(result.returncode in (0, 1) and result.stdout and len(result.stdout) <= 8192,
            'ssh-refresh-failed')
    try:
        doc = json.loads(result.stdout, object_pairs_hook=provision.no_duplicate_keys)
    except Exception:
        raise RefreshBlocked('unsafe-host-evidence') from None
    require(isinstance(doc, dict), 'unsafe-host-evidence')
    if doc.get('status') == 'BLOCKED':
        reason = doc.get('reason')
        require(result.returncode == 1 and set(doc) == {'status', 'reason'} and
                isinstance(reason, str) and reason in REASONS and reason.startswith('refresh-'),
                'unsafe-host-evidence')
        raise RefreshBlocked(reason)
    require(result.returncode == 0 and set(doc) == {
        'status', 'previous_sha256', 'rollback_sha256', 'changed', 'config_sha256',
        'execution_locked', 'external_order_calls', 'external_cancel_calls', 'approval_updated'},
        'unsafe-host-evidence')
    require(doc['status'] == 'PASS' and doc['previous_sha256'] == previous and
            digest(doc['rollback_sha256']) and doc['config_sha256'] == hashes and
            type(doc['changed']) is bool and
            doc['changed'] == (doc['rollback_sha256'] != previous) and
            doc['execution_locked'] is True and doc['approval_updated'] is False and
            type(doc['external_order_calls']) is int and doc['external_order_calls'] == 0 and
            type(doc['external_cancel_calls']) is int and doc['external_cancel_calls'] == 0,
            'unsafe-host-evidence')
    return doc


def inspect_once():
    from evidence import api, master_gates
    import transport
    sha = os.environ.get('GITHUB_SHA', '')
    require(os.environ.get('GITHUB_EVENT_NAME') == 'workflow_dispatch' and
            os.environ.get('GITHUB_REF') == 'refs/heads/master' and digest(sha, 40),
            'control-gate-failed')
    previous = os.environ.get('LEGACY_ROLLBACK_INVENTORY_SHA256', '')
    requested = os.environ.get('REFRESH_PREVIOUS_SHA256', '')
    try:
        hashes = json.loads(os.environ.get('PRODUCTION_APPROVED_CONFIG_SHA256_JSON', '{}'),
                            object_pairs_hook=provision.no_duplicate_keys)
    except Exception:
        raise RefreshBlocked('refresh-approval-missing') from None
    require(digest(previous) and requested == previous and hashes_valid(hashes),
            'refresh-approval-missing')
    require(os.environ.get('REFRESH_CONFIRMATION') ==
            f'REFRESH production rollback {sha} from {previous}', 'confirmation-mismatch')
    try:
        require(api('git/ref/heads/master')['object']['sha'] == sha, 'master-moved')
        master_gates(sha)
    except RefreshBlocked:
        raise
    except Exception:
        raise RefreshBlocked('control-gate-failed') from None
    try:
        ssh = transport.configure()
    except Exception:
        raise RefreshBlocked('ssh-config-unavailable') from None
    payload = {'control_sha': sha, 'pins': json.loads((HERE / 'approved-p8.json').read_text()),
               'hashes': hashes, 'previous_sha256': previous}
    program = remote_program(payload)
    try:
        require(api('git/ref/heads/master')['object']['sha'] == sha, 'master-moved')
    except RefreshBlocked:
        raise
    except Exception:
        raise RefreshBlocked('control-gate-failed') from None
    try:
        result = subprocess.run(ssh + [provision.REMOTE_COMMAND], input=program,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=1500, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise RefreshBlocked('ssh-refresh-failed') from None
    return response(result, previous, hashes)


def main():
    try:
        require(sys.argv[1:] == ['--runner'], 'runner-only')
        document = inspect_once()
        code = 0
    except Exception as exc:
        reason = str(exc) if isinstance(exc, RefreshBlocked) else ''
        document = {'status': 'BLOCKED',
                    'reason': reason if reason in REASONS else 'unsafe-host-evidence'}
        code = 1
    output = Path(os.environ.get('RUNNER_TEMP', '.')) / 'production-rollback-refresh.json'
    output.write_text(json.dumps(document, sort_keys=True) + '\n')
    os.chmod(output, 0o600)
    print(json.dumps(document, sort_keys=True))
    return code


if __name__ == '__main__':
    raise SystemExit(main())

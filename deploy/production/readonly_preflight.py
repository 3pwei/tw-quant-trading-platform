"""Manual runner for one in-memory SSH inspection; no upload/cutover path."""
import ast
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

import transport

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
VALIDATORS = ('require', 'file_sha', 'parse_env', 'disabled', 'protected',
              'inspect', 'verify_backup', 'config_check', 'shioaji_capability', 'market_compatibility')


def module(name, source):
    encoded = base64.b64encode(source).decode()
    return (f"m=types.ModuleType({name!r})\n"
            f"exec(compile(base64.b64decode({encoded!r}), {name!r}, 'exec'),m.__dict__)\n"
            f"sys.modules[{name!r}]=m\n")


def reviewed_validation_source():
    source = (HERE / 'cutover.py').read_text()
    tree = ast.parse(source)
    functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    # Exclude transaction, healthcheck, docker exec, write and deployment functions.
    text = 'import hashlib,json,os,re,tarfile\nfrom pathlib import Path\n'
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and
                t.id in ('ROOT', 'LEGACY', 'SERVICES', 'DISABLED') for t in node.targets):
            text += ast.get_source_segment(source, node) + '\n'
    for name in VALIDATORS:
        text += ast.get_source_segment(source, functions[name]) + '\n'
    return text.encode()


def in_memory(pins, hashes, rollback):
    payload = b'import base64,json,sys,types\n'
    for name, source in [('p9_validation', reviewed_validation_source()),
                         ('image_config_digest', (ROOT / 'deploy/staging/image_config_digest.py').read_bytes()),
                         ('readonly_sqlite', (HERE / 'readonly_sqlite.py').read_bytes()),
                         ('readonly_host', (HERE / 'readonly_host.py').read_bytes())]:
        payload += module(name, source).encode()
    args = base64.b64encode(json.dumps([pins, hashes, rollback]).encode()).decode()
    payload += (f"args=json.loads(base64.b64decode({args!r}))\n"
                "result=sys.modules['readonly_host'].collect(*args)\n"
                "print(json.dumps(result,sort_keys=True))\n").encode()
    return payload


def safe_evidence(document):
    """Validate every output key/value before it reaches logs or runner artifacts."""
    def need(value):
        if not value:
            raise ValueError('unsafe-host-evidence')
    def digest(value, prefix=''):
        need(isinstance(value, str) and re.fullmatch(re.escape(prefix) + '[0-9a-f]{64}', value))
    def count(value):
        need(type(value) is int and value >= 0)
    need(isinstance(document, dict))
    allowed = {'schema_version', 'P9_PREREQUISITE', 'reason', 'legacy_revision', 'market_provider',
               'market_provider_source', 'config_sha256', 'execution', 'sqlite', 'rollback', 'isolation'}
    need(set(document) <= allowed and {'schema_version', 'P9_PREREQUISITE', 'reason'} <= set(document))
    need(type(document['schema_version']) is int and document['schema_version'] == 1)
    need(document['P9_PREREQUISITE'] in ('PASS', 'BLOCKED'))
    # Use reviewed fixed reason literals only; exception text is never an evidence field.
    reason_source = ast.parse((HERE / 'readonly_host.py').read_text())
    assignment = next(n for n in reason_source.body if isinstance(n, ast.Assign) and
                      any(isinstance(t, ast.Name) and t.id == 'REASONS' for t in n.targets))
    reasons = assignment.value.args[0].func.value.value.split()
    need(document['reason'] in reasons + ['all-prerequisites-satisfied', 'ssh-config-unavailable',
        'ssh-inspection-failed', 'control-gate-failed', 'unsafe-host-evidence'])
    if 'legacy_revision' in document:
        need(isinstance(document['legacy_revision'], str) and re.fullmatch('[0-9a-f]{40}', document['legacy_revision']))
    if 'market_provider' in document:
        need(document['market_provider'] in ('mock', 'replay', 'shioaji', 'unsupported'))
        need(document.get('market_provider_source') == 'running-container-environment')
    if 'market_provider_source' in document:
        need('market_provider' in document)
    if 'config_sha256' in document:
        need(set(document['config_sha256']) == {'market.env', 'execution.env', 'gateway.env', 'factory', 'replay.csv'})
        for value in document['config_sha256'].values():
            digest(value)
    if 'execution' in document:
        need(document['execution'] == {'real_order_disabled': True, 'canary_disabled': True,
            'auto_disabled': True, 'guardian_disabled': True, 'broker_read_only_disabled': True,
            'broker_provider': 'disabled', 'live_trading_enabled': False, 'live_confirmation_empty': True,
            'external_order_calls': 0, 'external_cancel_calls': 0})
        for name, value in document['execution'].items():
            need(type(value) is (str if name == 'broker_provider' else int if name.endswith('_calls') else bool))
    if 'sqlite' in document:
        d = document['sqlite']
        need(set(d) == {'integrity_check', 'all_targets_locked', 'active_targets', 'target_count', 'db_boundary_consistent'})
        need(d['integrity_check'] == 'ok' and d['all_targets_locked'] is True and d['db_boundary_consistent'] is True)
        count(d['target_count']); count(d['active_targets']); need(d['active_targets'] == 0)
    if 'rollback' in document:
        d = document['rollback']
        need(set(d) == {'verified', 'rollback_json_sha256', 'record_sha256', 'containers', 'files_sha256'})
        need(d['verified'] is True); digest(d['rollback_json_sha256']); digest(d['record_sha256'])
        need(set(d['containers']) == {'market-api', 'execution-worker', 'gateway'})
        for c in d['containers'].values():
            need(set(c) == {'id', 'image_id', 'config_digest'})
            digest(c['id']); digest(c['image_id'], 'sha256:'); digest(c['config_digest'], 'sha256:')
        need(set(d['files_sha256']) == {'data.sqlite3', 'config.tar', 'image-market-api.tar',
                                        'image-execution-worker.tar', 'image-gateway.tar'})
        for value in d['files_sha256'].values():
            digest(value)
    if 'isolation' in document:
        need(document['isolation'] == {'production_staging_hosts': True, 'production_staging_paths': True,
                                      'production_configs_independent': True})
        need(all(v is True for v in document['isolation'].values()))
    if document['P9_PREREQUISITE'] == 'PASS':
        need(set(document) == allowed and document['reason'] == 'all-prerequisites-satisfied')
        pins = json.loads((HERE / 'approved-p8.json').read_text())
        need(document['legacy_revision'] == pins['legacy_revision'])
        import cutover
        cutover.market_compatibility({'MARKET_DATA_PROVIDER': document['market_provider']}, pins)
    else:
        need(document['reason'] != 'all-prerequisites-satisfied')
    return document


def inspect_once():
    # Exact master CI/Security and manual-only control identity before SSH.
    from evidence import api, master_gates
    if (os.environ.get('GITHUB_EVENT_NAME') != 'workflow_dispatch' or
            os.environ.get('GITHUB_REF') != 'refs/heads/master' or
            api('git/ref/heads/master')['object']['sha'] != os.environ.get('GITHUB_SHA')):
        raise ValueError('control-gate-failed')
    master_gates(os.environ['GITHUB_SHA'])
    pins = json.loads((HERE / 'approved-p8.json').read_text())
    try:
        hashes = json.loads(os.environ.get('PRODUCTION_APPROVED_CONFIG_SHA256_JSON', '{}') or '{}')
        if not isinstance(hashes, dict):
            hashes = {}
    except ValueError:
        hashes = {}
    rollback = os.environ.get('LEGACY_ROLLBACK_INVENTORY_SHA256', '')
    # SSH key/known_hosts files are runner-only and removed on every outcome.
    with tempfile.TemporaryDirectory(prefix='p9-readonly-', dir=os.environ['RUNNER_TEMP']) as temporary:
        previous = os.environ['RUNNER_TEMP']
        try:
            os.environ['RUNNER_TEMP'] = temporary
            try:
                ssh = transport.configure()
            except Exception:
                raise ValueError('ssh-config-unavailable') from None
        finally:
            os.environ['RUNNER_TEMP'] = previous
        remote = 'sudo -n env PYTHONDONTWRITEBYTECODE=1 python3 -B -'
        r = subprocess.run(ssh + [remote], input=in_memory(pins, hashes, rollback),
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=600)
        if r.returncode != 0 or len(r.stdout) > 32 * 1024:
            raise ValueError('ssh-inspection-failed')
        return safe_evidence(json.loads(r.stdout))


def main():
    try:
        result = inspect_once()
    except Exception as exc:
        # Never emit SSH stdout/stderr, exception details, tokens or account data.
        reasons = {'ssh-config-unavailable', 'ssh-inspection-failed', 'control-gate-failed', 'unsafe-host-evidence'}
        code = str(exc) if isinstance(exc, ValueError) else ''
        result = {'schema_version': 1, 'P9_PREREQUISITE': 'BLOCKED',
                  'reason': code if code in reasons else 'inspection-failed'}
    result = safe_evidence(result)
    text = json.dumps(result, sort_keys=True) + '\n'
    # These are the only evidence files: both reside on the ephemeral runner.
    path = Path(os.environ['RUNNER_TEMP']) / 'p9-readonly-evidence.json'
    path.write_text(text)
    os.chmod(path, 0o600)
    print(text, end='')
    return 0 if result['P9_PREREQUISITE'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())

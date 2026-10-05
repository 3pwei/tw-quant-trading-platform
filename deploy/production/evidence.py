"""Pinned P8 bytes and successful exact-master gates, before Production access."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy/staging'))
from candidate_manifest import require, validate


def sha(data):
    return hashlib.sha256(data).hexdigest()


def confirmation(pins):
    return f"DEPLOY production {pins['platform_sha']} candidate {pins['candidate_run_id']} manifest {pins['manifest_sha256']}"


def request(pins, source, candidate, manifest, text):
    require(source == pins['platform_sha'], 'wrong-source-sha')
    require(candidate == pins['candidate_run_id'], 'wrong-candidate')
    require(manifest == pins['manifest_sha256'], 'wrong-manifest')
    require(text == confirmation(pins), 'wrong-confirmation')


def verify_manifest(payload, pins):
    require(sha(payload) == pins['manifest_sha256'], 'manifest-checksum')
    document = json.loads(payload)
    validate(document)
    require(document == pins['manifest'], 'image-or-provenance-mismatch')
    return document


def verify_acceptance(files, pins):
    require(set(files) == set(pins['acceptance_files']), 'missing-p8-acceptance')
    require(all(sha(files[k]) == v for k, v in pins['acceptance_files'].items()), 'p8-checksum')
    verify_manifest(files['candidate-manifest.json'], pins)
    report = json.loads(files['acceptance.json'])
    session = json.loads(files['session.json'])
    require(report['acceptance'] == 'PASS' and report['final_release'] == pins['release'], 'p8-not-accepted')
    require(report['manifest_sha256'] == pins['manifest_sha256'] and
            report['ledger_sha256'] == sha(files['ledger.jsonl']), 'p8-ledger-checksum')
    for item in (report, session):
        require(item['session'] == pins['staging_run_id'] + '-1' and
                item['pipeline'] == pins['platform_sha'] and
                item['candidate_run_id'] == pins['candidate_run_id'], 'p8-source-mismatch')
    require(report['soak']['elapsed_seconds'] >= 3600 and report['soak']['max_sample_gap_seconds'] <= 30,
            'p8-soak-incomplete')


def api(path):
    token = os.environ.get('GITHUB_TOKEN', '')
    require(bool(token), 'missing-api-token')
    req = urllib.request.Request('https://api.github.com/repos/3pwei/tw-quant-trading-platform/' + path,
        headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
                 'User-Agent': 'p9-cutover-gate'})
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.load(response)


def master_gates(revision):
    # Check workflow path, repository, master push and newest run; reject truncated pages.
    document = api('actions/runs?head_sha=' + revision + '&event=push&per_page=100')
    require(document['total_count'] == len(document['workflow_runs']), 'gate-pagination-incomplete')
    result = {}
    for name, path in [('CI', '.github/workflows/ci.yml'), ('Security', '.github/workflows/security.yml')]:
        runs = [r for r in document['workflow_runs'] if r['name'] == name and r['path'] == path
                and r['head_sha'] == revision and r['head_branch'] == 'master' and r['event'] == 'push'
                and r['repository']['full_name'] == '3pwei/tw-quant-trading-platform']
        require(bool(runs), 'missing-master-gate')
        run = max(runs, key=lambda r: (r['run_number'], r['run_attempt']))
        require(run['status'] == 'completed' and run['conclusion'] == 'success', 'master-gate-failed')
        result[name] = run['id']
    return result


def archive(pins, kind, run_id, path, name):
    run = api('actions/runs/' + run_id)
    require(str(run['id']) == run_id and run['repository']['full_name'] == pins['repository'] and
            run['head_sha'] == pins['platform_sha'] and run['head_branch'] == 'master' and
            run['path'] == path and run['name'] == name and run['event'] == 'workflow_dispatch' and
            run['run_attempt'] == 1 and run['status'] == 'completed' and run['conclusion'] == 'success',
            'p8-run-mismatch')
    document = api('actions/runs/' + run_id + '/artifacts?per_page=100')
    require(document['total_count'] == len(document['artifacts']), 'artifact-pagination-incomplete')
    pin = pins[kind + '_artifact']
    matches = [a for a in document['artifacts'] if a['name'] == pin['name']]
    require(len(matches) == 1, 'missing-or-ambiguous-p8-artifact')
    artifact = matches[0]
    require(all(artifact[k] == pin[k] for k in ('id', 'name', 'digest')) and artifact['expired'] is False and
            str(artifact['workflow_run']['id']) == run_id and
            artifact['workflow_run']['head_sha'] == pins['platform_sha'], 'artifact-identity-mismatch')
    url = f"https://api.github.com/repos/{pins['repository']}/actions/artifacts/{pin['id']}/zip"
    require(artifact['archive_download_url'] == url, 'artifact-url-mismatch')
    # Fetch authenticated redirect URL without forwarding the GitHub token to storage.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    from urllib.error import HTTPError
    import urllib.parse
    req = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + os.environ['GITHUB_TOKEN']})
    try:
        urllib.request.build_opener(NoRedirect).open(req, timeout=15)
        raise ValueError('artifact-redirect-missing')
    except HTTPError as exc:
        require(exc.code == 302, 'artifact-download-failed')
        location = exc.headers['Location']
    parsed = urllib.parse.urlparse(location)
    require(parsed.scheme == 'https' and parsed.hostname and
            (parsed.hostname.endswith('.blob.core.windows.net') or parsed.hostname.endswith('.githubusercontent.com')),
            'artifact-storage-host')
    with urllib.request.urlopen(location, timeout=30) as response:
        payload = response.read(2 * 1024 * 1024 + 1)
    require(len(payload) <= 2 * 1024 * 1024 and 'sha256:' + sha(payload) == pin['digest'], 'archive-checksum')
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        names = z.namelist()
        expected = (['candidate-manifest.json', 'candidate-manifest.sha256'] if kind == 'candidate'
                    else sorted(pins['acceptance_files']))
        require(sorted(names) == expected and all(i.file_size <= 1024 * 1024 for i in z.infolist()), 'archive-members')
        return {n: z.read(n) for n in names}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--source', required=True)
    p.add_argument('--candidate', required=True)
    p.add_argument('--manifest', required=True)
    p.add_argument('--control-sha', required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    pins = json.loads((Path(__file__).parent / 'approved-p8.json').read_text())
    require(os.environ['GITHUB_EVENT_NAME'] == 'workflow_dispatch' and
            os.environ['GITHUB_REF'] == 'refs/heads/master', 'manual-master-only')
    request(pins, args.source, args.candidate, args.manifest, os.environ['P9_CONFIRMATION'])
    require(api('git/ref/heads/master')['object']['sha'] == args.control_sha, 'control-master-moved')
    gates = {s: master_gates(s) for s in {pins['platform_sha'], args.control_sha}}
    candidate = archive(pins, 'candidate', pins['candidate_run_id'], '.github/workflows/staging-candidate.yml', 'P8 Staging Candidate')
    verify_manifest(candidate['candidate-manifest.json'], pins)
    require(candidate['candidate-manifest.sha256'].decode() == 'manifest_sha256=' + pins['manifest_sha256'] + '\n', 'manifest-sidecar')
    accepted = archive(pins, 'acceptance', pins['staging_run_id'], '.github/workflows/deploy-staging.yml', 'P8 Deploy Staging')
    verify_acceptance(accepted, pins)
    args.output.mkdir(mode=0o700)
    for name, data in accepted.items():
        (args.output / name).write_bytes(data)
    (args.output / 'gate.json').write_text(json.dumps({'control_sha': args.control_sha, 'master_gates': gates,
        'platform_sha': pins['platform_sha'], 'candidate_run_id': pins['candidate_run_id'],
        'manifest_sha256': pins['manifest_sha256']}, sort_keys=True) + '\n')
    print('P9_PREHOST_GATE=PASS')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('P9_PREHOST_GATE=FAIL')

"""Behavior regressions: compensation precedence and candidate provenance."""
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

STAGING = Path(__file__).resolve().parents[1] / "deploy/staging"

def module(name):
    spec = importlib.util.spec_from_file_location(name, STAGING / (name + ".py"))
    result = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {name: result}):
        spec.loader.exec_module(result)
    return result

manifest = module("candidate_manifest")
with patch.dict(sys.modules, {"candidate_manifest": manifest}):
    artifact_gate = module("verify_candidate_artifact")


def candidate():
    identities = manifest.IDENTITIES
    def image(role, digest):
        digest = "sha256:" + digest * 64
        return {"ref": manifest.IMAGE_REPOSITORY + ":" + role + "-123-1@" + digest,
                "digest": digest, "config_digest": digest}
    return {"schema_version": 2, "pipeline_revision": "1" * 40,
        "platform_source_sha": "1" * 40,
        "p7_platform_source_sha": identities["P7_PLATFORM_SOURCE_SHA"],
        "core": {"version": identities["CORE_VERSION"], "wheel_sha256": identities["CORE_WHEEL_SHA256"]},
        "private_provider": {"version": identities["PRIVATE_PROVIDER_VERSION"],
            "wheel_sha256": identities["PRIVATE_PROVIDER_WHEEL_SHA256"], "p7_acceptance_sha": identities["P7_ACCEPTANCE_SHA"]},
        "provenance": {"run_id": "123", "run_attempt": "1", "build_once": True,
                       "builder": "github-actions/hosted-linux-x64"},
        "images": {"gateway": image("gateway", "c"), "releases": {
            "known_good": {"configuration_identity": "p8-a-123-1", "runtime": image("runtime-a", "a")},
            "candidate": {"configuration_identity": "p8-b-123-1", "runtime": image("runtime-b", "b")}}}}


class ProvenanceTests(unittest.TestCase):
    def test_false_build_attempts_swapped_roles_and_foreign_registry_fail_closed(self):
        manifest.validate(candidate())
        for key, value in (("build_once", False), ("build_once", 1), ("run_id", "999"),
                           ("run_attempt", "0"), ("run_attempt", "01"), ("run_id", 123)):
            d = candidate()
            d["provenance"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                manifest.validate(d)
        d = candidate()
        d["images"]["releases"]["known_good"]["configuration_identity"] = "p8-b-123-1"
        with self.assertRaises(ValueError): manifest.validate(d)
        d = candidate()
        d["images"]["gateway"]["ref"] = d["images"]["gateway"]["ref"].replace("3pwei", "foreign")
        with self.assertRaises(ValueError): manifest.validate(d)

    def test_archive_and_requested_run_binding(self):
        payload = json.dumps(candidate()).encode()
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as z:
            z.writestr("candidate-manifest.json", payload)
            z.writestr("candidate-manifest.sha256", "manifest_sha256=" + hashlib.sha256(payload).hexdigest() + "\n")
        archive = stream.getvalue()
        artifact = {"id": 456, "name": "p8-staging-candidate-manifest", "expired": False,
            "digest": "sha256:" + hashlib.sha256(archive).hexdigest(),
            "workflow_run": {"id": 123, "head_sha": "1" * 40},
            "archive_download_url": "https://api.github.com/repos/3pwei/tw-quant-trading-platform/actions/artifacts/456/zip"}
        artifacts = {"total_count": 1, "artifacts": [artifact]}
        workflow = {"id": 789, "path": artifact_gate.WORKFLOW}
        run = {"id": 123, "repository": {"full_name": artifact_gate.REPOSITORY},
            "workflow_id": 789, "path": artifact_gate.WORKFLOW, "name": "P8 Staging Candidate",
            "event": "workflow_dispatch", "status": "completed", "conclusion": "success",
            "head_branch": "master", "head_sha": "1" * 40, "run_attempt": 1}
        def verify(a=archive, ar=artifacts, r=run, w=workflow):
            return artifact_gate.verify_archive(a, ar, r, w, "123", "1" * 40)
        self.assertEqual(verify(), payload)
        for key, value in (("id", 999), ("workflow_id", 999), ("path", "other.yml"),
                           ("run_attempt", 2), ("head_sha", "2" * 40), ("conclusion", "failure")):
            with self.subTest(key=key), self.assertRaises(ValueError): verify(r={**run, key: value})
        with self.assertRaises(ValueError): verify(a=archive + b"changed")
        with self.assertRaises(ValueError): verify(ar={**artifacts, "total_count": 2})
        with self.assertRaises(ValueError): verify(ar={"total_count": 2, "artifacts": [artifact, artifact]})
        bad = copy.deepcopy(artifacts)
        bad["artifacts"][0]["archive_download_url"] = "https://example.invalid/zip"
        with self.assertRaises(ValueError): verify(ar=bad)
        for filename, checksum in (("../candidate-manifest.json", True), ("candidate-manifest.json", False)):
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, "w") as z:
                z.writestr(filename, payload)
                z.writestr("candidate-manifest.sha256", "manifest_sha256=" + (hashlib.sha256(payload).hexdigest() if checksum else "0" * 64) + "\n")
            raw = stream.getvalue()
            updated = copy.deepcopy(artifacts)
            updated["artifacts"][0]["digest"] = "sha256:" + hashlib.sha256(raw).hexdigest()
            with self.assertRaises(ValueError): verify(a=raw, ar=updated)


class RecoveryTests(unittest.TestCase):
    def test_rollback_record_replaces_metadata_without_duplicate_fields(self):
        source = (STAGING / 'deploy.sh').read_text()
        commit_record = source[source.index('record="$(mktemp'):source.index('\ntrap - EXIT\nrm -f')]
        evidence = module('acceptance_evidence')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            a = 'RELEASE_NAME=known_good\nCONFIGURATION_IDENTITY=p8-a-123-1\nDEPLOYMENT_MODE=deploy\nVERIFIED_AT=2026-10-01T00:00:00Z\n'
            b = a.replace('known_good', 'candidate').replace('p8-a-', 'p8-b-')
            (root / 'target').write_text(a)
            (root / 'current').write_text(b)
            result = subprocess.run(['bash', '-eu', '-c', commit_record], env={**os.environ,
                'DEPLOYMENTS': directory, 'target': str(root / 'target'), 'CURRENT': str(root / 'current'),
                'ACTIVE': str(root / 'active'), 'PREVIOUS': str(root / 'previous'), 'ACTION': 'rollback'},
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            current = evidence.release_values(root / 'current')
            self.assertEqual(current['DEPLOYMENT_MODE'], 'rollback')
            self.assertEqual(current['RELEASE_NAME'], 'known_good')
            self.assertNotEqual(current['VERIFIED_AT'], '2026-10-01T00:00:00Z')
            self.assertEqual((root / 'previous').read_text(), b)
            self.assertEqual((root / 'active').read_text(), (root / 'current').read_text())

    def test_compensation_reloads_a_and_preserves_original_failure(self):
        source = (STAGING / "deploy.sh").read_text()
        function = source[source.index("restore_prior() {"):source.index("trap restore_prior EXIT")]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prior = root / "prior"
            prior.write_text("STAGING_RUNTIME_IMAGE=known-good-A\nSTAGING_GATEWAY_IMAGE=gateway-A\n")
            verify = root / "verify.sh"
            verify.write_text('#!/bin/bash\n[[ "$STAGING_RUNTIME_IMAGE" == known-good-A ]] || exit 99\nexit "${VERIFY_EXIT:-0}"\n')
            verify.chmod(0o755)
            for compose_exit, verify_exit in ((0, 0), (23, 0), (0, 31)):
                prior.write_text("STAGING_RUNTIME_IMAGE=known-good-A\nSTAGING_GATEWAY_IMAGE=gateway-A\n")
                script = "set -e\n" + function + '''
export STAGING_RUNTIME_IMAGE=candidate-B STAGING_GATEWAY_IMAGE=gateway-B
prior="$TEST_ROOT/prior"
ACTIVE="$TEST_ROOT/active"
CURRENT="$TEST_ROOT/current"
PREVIOUS="$TEST_ROOT/previous"
prior_previous=""
target="$TEST_ROOT/target"
BUNDLE="$TEST_ROOT"
COMPOSE_ENV="$TEST_ROOT/compose.env"
COMPOSE_FILE="$TEST_ROOT/compose.yml"
docker() { echo "selected=$STAGING_RUNTIME_IMAGE gateway=$STAGING_GATEWAY_IMAGE"; return "$COMPOSE_EXIT"; }
trap restore_prior EXIT
exit 17
'''
                result = subprocess.run(["bash", "-c", script], env={**os.environ, "TEST_ROOT": directory,
                    "COMPOSE_EXIT": str(compose_exit), "VERIFY_EXIT": str(verify_exit)}, capture_output=True, text=True)
                self.assertEqual(result.returncode, 17)
                self.assertIn("selected=known-good-A gateway=gateway-A", result.stdout)
                self.assertIn("P8_RESTORE=" + ("PASS" if compose_exit == verify_exit == 0 else "FAIL"), result.stderr)
                self.assertIn("acceptance=false", result.stderr)
                self.assertIn("known-good-A", (root / "active").read_text())


@unittest.skipUnless(os.environ.get('P8_RECOVERY_DOCKER_TEST') == '1', 'requires disposable Docker CI host')
class RecoveryDockerTests(unittest.TestCase):
    def test_actual_running_image_returns_to_a_after_b_failure(self):
        source = (STAGING / 'deploy.sh').read_text()
        function = source[source.index('restore_prior() {'):source.index('trap restore_prior EXIT')]
        with tempfile.TemporaryDirectory(prefix='p8-compensation-') as directory:
            root = Path(directory)
            a, b = 'platform-market-api:ci', 'platform-execution-worker:ci'
            def run(argv, **kwargs):
                return subprocess.run(argv, capture_output=True, text=True, timeout=90, **kwargs)
            a_id = run(['docker', 'image', 'inspect', '--format', '{{.Id}}', a]).stdout.strip()
            b_id = run(['docker', 'image', 'inspect', '--format', '{{.Id}}', b]).stdout.strip()
            self.assertTrue(a_id)
            self.assertNotEqual(a_id, b_id)
            config = root / 'compose.yml'
            config.write_text('name: p8-compensation-' + str(os.getpid()) + '\nservices:\n  worker:\n'
                '    image: ${STAGING_RUNTIME_IMAGE:?}\n    pull_policy: never\n    network_mode: none\n'
                '    read_only: true\n    cap_drop: [ALL]\n    security_opt: [no-new-privileges:true]\n'
                '    command: [python, -c, "import time; time.sleep(180)"]\n')
            (root / 'compose.env').write_text('')
            prior = 'STAGING_RUNTIME_IMAGE=' + a + '\nSTAGING_GATEWAY_IMAGE=unused\n'
            (root / 'prior').write_text(prior)
            (root / 'current').write_text(prior)
            (root / 'active').write_text('STAGING_RUNTIME_IMAGE=' + b + '\nSTAGING_GATEWAY_IMAGE=unused\n')
            verify = root / 'verify.sh'
            verify.write_text('''#!/bin/bash
set -euo pipefail
cid="$(docker compose --env-file "$ACTIVE" -f "$COMPOSE_FILE" ps -q worker)"
actual="$(docker inspect --format '{{.Image}}' "$cid")"
expected="$(docker image inspect --format '{{.Id}}' platform-market-api:ci)"
test "$actual" = "$expected"
''')
            verify.chmod(0o755)
            env = {**os.environ, 'STAGING_RUNTIME_IMAGE': b, 'TEST_ROOT': directory}
            compose = ['docker', 'compose', '--env-file', str(root / 'active'), '-f', str(config)]
            try:
                result = run([*compose, 'up', '--detach', '--no-build', '--pull', 'never'], env=env)
                self.assertEqual(result.returncode, 0, result.stderr)
                cid = run([*compose, 'ps', '-q', 'worker'], env=env).stdout.strip()
                self.assertEqual(run(['docker', 'inspect', '--format', '{{.Image}}', cid]).stdout.strip(), b_id)
                script = 'set -e\n' + function + '''
export prior="$TEST_ROOT/prior" ACTIVE="$TEST_ROOT/active" CURRENT="$TEST_ROOT/current"
export PREVIOUS="$TEST_ROOT/previous" BUNDLE="$TEST_ROOT" COMPOSE_ENV="$TEST_ROOT/compose.env"
export COMPOSE_FILE="$TEST_ROOT/compose.yml" target="$TEST_ROOT/target" prior_previous=""
trap restore_prior EXIT
exit 17
'''
                result = run(['bash', '-c', script], env=env)
                self.assertEqual(result.returncode, 17)
                self.assertIn('P8_RESTORE=PASS', result.stderr)
                self.assertEqual((root / 'active').read_text(), prior)
                self.assertEqual((root / 'current').read_text(), prior)
                cid = run([*compose, 'ps', '-q', 'worker'], env=env).stdout.strip()
                self.assertEqual(run(['docker', 'inspect', '--format', '{{.Image}}', cid]).stdout.strip(), a_id)
            finally:
                cleanup = run([*compose, 'down', '--volumes'], env=env)
                self.assertEqual(cleanup.returncode, 0)


if __name__ == "__main__": unittest.main()

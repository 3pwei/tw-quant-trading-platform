from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
STAGING = ROOT / "deploy/staging"
SOURCE_SHA = "7d490254130d6fd3da5f8f88d9903cb1a35a2f88"
CORE_SHA = "63645e42755068c308d66d74ded5133395dfef816360dc8106e0bbc247ee49bd"
PRIVATE_SHA = "a5cbd8147bfd517e0297f6e70fdbbf33b63c22ab58e570a35bfdbdfc26993ee5"
P7_SHA = "97ba63f514c6adeb531666e9b10d8b05578cde76"


class P8IdentityTests(unittest.TestCase):
    def test_approved_identities_are_single_source_and_pinned(self):
        identities = (STAGING / "identities.conf").read_text()
        self.assertIn(f"PLATFORM_SOURCE_SHA={SOURCE_SHA}", identities)
        self.assertIn(f"CORE_WHEEL_SHA256={CORE_SHA}", identities)
        self.assertIn(f"PRIVATE_PROVIDER_WHEEL_SHA256={PRIVATE_SHA}", identities)
        self.assertIn(f"P7_ACCEPTANCE_SHA={P7_SHA}", identities)

    def test_manifest_rejects_floating_or_same_rollback_images(self):
        script = STAGING / "candidate_manifest.py"
        digest_a = "sha256:" + "a" * 64
        digest_b = "sha256:" + "b" * 64
        digest_g = "sha256:" + "c" * 64
        config_a = "sha256:" + "d" * 64
        config_b = "sha256:" + "e" * 64
        config_g = "sha256:" + "f" * 64
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidate.json"
            command = [
                str(script), "create", "--output", str(path),
                "--pipeline-revision", "1" * 40, "--run-id", "123", "--run-attempt", "1",
                "--runtime-a-ref", "ghcr.io/example/runtime:a@" + digest_a,
                "--runtime-a-digest", digest_a, "--runtime-a-config-digest", config_a,
                "--runtime-b-ref", "ghcr.io/example/runtime:b@" + digest_b,
                "--runtime-b-digest", digest_b, "--runtime-b-config-digest", config_b,
                "--gateway-ref", "ghcr.io/example/gateway:g@" + digest_g,
                "--gateway-digest", digest_g, "--gateway-config-digest", config_g,
            ]
            created = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(created.returncode, 0, created.stderr)
            verified = subprocess.run([str(script), "verify", str(path)], capture_output=True, text=True)
            self.assertEqual(verified.returncode, 0, verified.stderr)
            bundle = Path(directory) / "bundle"
            bundle.mkdir()
            shutil.copy2(script, bundle / "candidate_manifest.py")
            shutil.copy2(STAGING / "identities.conf", bundle / "identities.conf")
            deployed = subprocess.run(
                [str(bundle / "candidate_manifest.py"), "verify", str(path)],
                capture_output=True, text=True,
            )
            self.assertEqual(deployed.returncode, 0, deployed.stderr)
            document = json.loads(path.read_text())
            document["images"]["releases"]["candidate"]["runtime"] = document["images"]["releases"]["known_good"]["runtime"]
            path.write_text(json.dumps(document))
            rejected = subprocess.run([str(script), "verify", str(path)], capture_output=True, text=True)
            self.assertNotEqual(rejected.returncode, 0)


class P8BuildBoundaryTests(unittest.TestCase):
    def test_private_wheel_and_token_are_build_secrets_not_args_or_copies(self):
        dockerfile = (STAGING / "Dockerfile.runtime").read_text()
        workflow = (ROOT / ".github/workflows/staging-candidate.yml").read_text()
        self.assertIn("--mount=type=secret,id=private_provider_wheel", dockerfile)
        self.assertIn('provider_filename="$(python -c', dockerfile)
        self.assertIn('"/tmp/${provider_filename}"', dockerfile)
        self.assertIn('rm -f "/tmp/${provider_filename}"', dockerfile)
        self.assertNotIn("--no-deps /run/secrets/provider.whl", dockerfile)
        self.assertIn("rm -rf /usr/local/lib/python3.14/ensurepip", dockerfile)
        self.assertIn("/usr/local/bin/python -m pip uninstall", dockerfile)
        self.assertLess(
            dockerfile.index("rm -rf /usr/local/lib/python3.14/ensurepip"),
            dockerfile.index("/usr/local/bin/python -m pip uninstall"),
        )
        self.assertNotIn("&& python -m pip uninstall", dockerfile)
        self.assertNotIn("COPY provider.whl", dockerfile)
        self.assertNotIn("ARG PRIVATE_PROVIDER_READ_TOKEN", dockerfile)
        self.assertNotIn("--build-arg PRIVATE_PROVIDER_READ_TOKEN", workflow)
        factory_path = '"${RUNNER_TEMP}/provider-factory"'
        create = workflow.index(f"install -m 600 /dev/null {factory_path}")
        write = workflow.index(f"printf '%s' \"$PRIVATE_PROVIDER_FACTORY\" > {factory_path}")
        chown = workflow.index(f"sudo chown 10001:10001 {factory_path}")
        lock = workflow.index(f"sudo chmod 0400 {factory_path}")
        self.assertLess(create, write)
        self.assertLess(write, chown)
        self.assertLess(chown, lock)
        self.assertNotIn(f"install -m 400 /dev/null {factory_path}", workflow)
        self.assertIn("docker history --no-trunc", workflow)
        self.assertIn("CycloneDX", workflow)
        self.assertIn("trivy-action@", workflow)

    def test_candidate_uses_exact_source_archive_and_pushes_without_rebuild(self):
        workflow = (ROOT / ".github/workflows/staging-candidate.yml").read_text()
        self.assertIn(f"git archive {SOURCE_SHA}", workflow)
        self.assertEqual(workflow.count('docker build "${common[@]}"'), 2)
        self.assertIn("for image in \"$RUNTIME_A_TAG\" \"$RUNTIME_B_TAG\" \"$GATEWAY_TAG\"; do docker push", workflow)
        push = workflow.index("docker push")
        self.assertNotIn("DOCKER_BUILDKIT=1 docker build", workflow[push:])

    def test_public_runtime_image_remains_private_provider_free(self):
        public = (ROOT / "Dockerfile").read_text()
        self.assertNotIn("private_provider_wheel", public)
        self.assertIn("catalog() == []", public)


class P8StagingIsolationTests(unittest.TestCase):
    def test_compose_cannot_build_or_reach_execution_network(self):
        compose = (STAGING / "docker-compose.yml").read_text()
        self.assertNotIn("build:", compose)
        self.assertEqual(compose.count("pull_policy: never"), 3)
        execution = compose.split("  execution-worker:", 1)[1].split("\n  gateway:", 1)[0]
        self.assertIn("network_mode: none", execution)
        self.assertNotIn("ports:", execution)
        self.assertNotIn("live-secrets", compose)
        self.assertIn('127.0.0.1:${STAGING_GATEWAY_PORT:-18080}:8080', compose)
        self.assertIn("staging-data:/data", compose)
        self.assertNotIn("platform-production", compose)

    def test_host_scripts_hard_reject_non_staging_root(self):
        for name in ("prepare-host.sh", "deploy.sh", "verify.sh", "soak.sh"):
            path = STAGING / name
            self.assertNotEqual(path.stat().st_mode & 0o111, 0, name)
            text = path.read_text()
            self.assertIn("/srv/trading-platform-staging", text)
            self.assertRegex(text, r'(?:==|!=) \*production\*')

    def test_host_python_calls_use_ubuntu_python3(self):
        prepare = (STAGING / "prepare-host.sh").read_text()
        deploy = (STAGING / "deploy.sh").read_text()
        verify = (STAGING / "verify.sh").read_text()
        soak = (STAGING / "soak.sh").read_text()
        self.assertIn("command -v python3", prepare)
        self.assertIn('python3 "${BUNDLE}/candidate_manifest.py" verify', deploy)
        self.assertIn('python3 "${BUNDLE}/candidate_manifest.py" emit-env', deploy)
        self.assertIn("python3 -c 'import json,sys", verify)
        self.assertIn("python3 -c 'import json,sys", soak)

    def test_deployment_has_atomic_records_rollback_and_no_build(self):
        script = (STAGING / "deploy.sh").read_text()
        self.assertIn("previous.env", script)
        self.assertIn("restore_prior", script)
        self.assertIn("up --no-build --pull never", script)
        self.assertNotIn("docker build", script)
        self.assertIn('cp "${CURRENT}" "${PREVIOUS}.tmp"', script)
        self.assertLess(script.index('"${BUNDLE}/verify.sh" restart'), script.index('mv "${record}" "${CURRENT}"'))

    def test_workflow_uses_only_staging_environment_and_secrets(self):
        workflow = (ROOT / ".github/workflows/deploy-staging.yml").read_text()
        self.assertIn("environment: staging", workflow)
        self.assertIn("secrets.STAGING_SSH_PRIVATE_KEY", workflow)
        self.assertIn("vars.STAGING_HOST", workflow)
        self.assertNotIn("LIGHTSAIL", workflow)
        self.assertNotIn("lightsail-production", workflow)
        self.assertIn("deploy.sh deploy", workflow)
        self.assertIn("deploy.sh rollback", workflow)
        self.assertIn("soak.sh", workflow)
        for name in (
            "test_broker_contract.py", "test_execution_worker.py",
            "test_live_canary.py", "test_live_position_guardian.py",
            "test_live_reconciliation.py", "test_live_recovery_worker.py",
        ):
            self.assertIn(name, workflow)
        verifier = (STAGING / "verify.sh").read_text()
        self.assertIn("/runtime-tests", verifier)
        self.assertIn("--network none --read-only", verifier)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
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
                "--runtime-a-ref", "ghcr.io/3pwei/tw-quant-trading-platform-staging:runtime-a-123-1@" + digest_a,
                "--runtime-a-digest", digest_a, "--runtime-a-config-digest", config_a,
                "--runtime-b-ref", "ghcr.io/3pwei/tw-quant-trading-platform-staging:runtime-b-123-1@" + digest_b,
                "--runtime-b-digest", digest_b, "--runtime-b-config-digest", config_b,
                "--gateway-ref", "ghcr.io/3pwei/tw-quant-trading-platform-staging:gateway-123-1@" + digest_g,
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

    def test_candidate_and_staging_share_runtime_acceptance_path_before_push(self):
        workflow = (ROOT / ".github/workflows/staging-candidate.yml").read_text()
        verifier = (STAGING / "verify.sh").read_text()
        target = "target=/app/p8_runtime_acceptance.py,readonly"
        command = "python /app/p8_runtime_acceptance.py"

        self.assertIn('for image in "$RUNTIME_A_TAG" "$RUNTIME_B_TAG"; do', workflow)
        for text in (workflow, verifier):
            self.assertIn(target, text)
            self.assertIn(command, text)
            self.assertNotIn("target=/runtime_acceptance.py,readonly", text)
            self.assertNotIn("python /runtime_acceptance.py", text)
        self.assertLess(workflow.index(target), workflow.index("docker push"))

    def test_runtime_acceptance_preserves_candidate_and_staging_security_boundary(self):
        workflow = (ROOT / ".github/workflows/staging-candidate.yml").read_text()
        verifier = (STAGING / "verify.sh").read_text()
        dockerfile = (STAGING / "Dockerfile.runtime").read_text()
        candidate_start = workflow.index(
            "      - name: Verify installed private composition and zero credential persistence"
        )
        candidate_end = workflow.index("\n      - name:", candidate_start + 1)
        candidate = workflow[candidate_start:candidate_end]
        staging_start = verifier.index(
            "  docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges"
        )
        staging_end = verifier.index("\n  docker run", staging_start + 1)
        staging = verifier[staging_start:staging_end]

        for text in (candidate, staging):
            for expected in (
                "--network none --read-only --cap-drop ALL",
                "--security-opt no-new-privileges",
                "--tmpfs /tmp:rw,noexec,nosuid,nodev,size=128m,uid=10001,gid=10001",
                "target=/run/staging-provider/factory,readonly",
                "target=/app/p8_runtime_acceptance.py,readonly",
                "PRIVATE_PROVIDER_WHEEL_SHA256=",
                "python /app/p8_runtime_acceptance.py",
            ):
                self.assertIn(expected, text)
            self.assertNotIn("--user 0", text)
        self.assertIn("USER 10001:10001", dockerfile)

    def test_gateway_candidate_smoke_matches_staging_security_boundary(self):
        dockerfile = (STAGING / "Dockerfile.gateway").read_text()
        workflow = (ROOT / ".github/workflows/staging-candidate.yml").read_text()

        self.assertNotIn("setcap", dockerfile)
        self.assertNotIn("libcap", dockerfile)

        start = workflow.index(
            "      - name: Smoke gateway under exact staging security boundary"
        )
        end = workflow.index("\n      - name:", start + 1)
        self.assertIn('bash deploy/staging/smoke-gateway.sh "$GATEWAY_TAG"', workflow[start:end])
        smoke = (STAGING / "smoke-gateway.sh").read_text()
        for expected in (
            '--network none --read-only --cap-drop ALL',
            '--security-opt no-new-privileges',
            '--tmpfs /data:rw,noexec,nosuid,nodev,size=16m,uid=10000,gid=10000,mode=0700',
            '--tmpfs /config:rw,noexec,nosuid,nodev,size=16m,uid=10000,gid=10000,mode=0700',
            '--tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m,uid=10000,gid=10000,mode=1770',
            'curl --fail --silent --show-error --max-time 2',
            'http://127.0.0.1:8080/healthz',
            'cmp -s',
            "test \"$(docker inspect --format '{{.Config.User}}' \"$gateway_smoke\")\" = 10000:10000",
            'test "$(docker exec "$gateway_smoke" id -u)" = 10000',
            "touch /data/.p8-smoke /config/.p8-smoke /tmp/.p8-smoke",
        ):
            self.assertIn(expected, smoke)
        self.assertIn('docker run --detach --name "$gateway_smoke"', smoke)
        self.assertIn('"$GATEWAY_TAG" >/dev/null', smoke)
        self.assertLess(start, workflow.index("      - name: Scan gateway"))
        self.assertLess(start, workflow.index("docker push"))

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
        self.assertNotIn('    ports:', compose)
        self.assertIn('STAGING_INGRESS_DIRECTORY', compose)
        self.assertIn('ListenStream=127.0.0.1:18080', (STAGING / 'p8-staging-ingress.socket').read_text())
        self.assertIn("staging-data:/data", compose)
        self.assertNotIn("platform-production", compose)

    def test_compose_quotes_required_path_interpolation(self):
        compose = (STAGING / "docker-compose.yml").read_text()
        for variable in (
            "STAGING_MARKET_ENV_FILE",
            "STAGING_EXECUTION_ENV_FILE",
            "STAGING_GATEWAY_ENV_FILE",
        ):
            self.assertIn(
                f'env_file: ["${{{variable}:?',
                compose,
                variable,
            )
        self.assertIn(
            '- "${STAGING_PROVIDER_FACTORY_FILE:?provider factory file is required}:'
            '/run/staging-provider/factory:ro"',
            compose,
        )
        ci = (ROOT / ".github/workflows/ci.yml").read_text()
        self.assertIn(
            "docker compose --file deploy/staging/docker-compose.yml config --quiet",
            ci,
        )

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
        self.assertIn('python3 "${BUNDLE}/acceptance_evidence.py" soak', soak)

    def test_deployment_has_atomic_records_rollback_and_no_build(self):
        script = (STAGING / "deploy.sh").read_text()
        self.assertIn("previous.env", script)
        self.assertIn("restore_prior", script)
        self.assertIn("up --no-build --pull never", script)
        self.assertNotIn("docker build", script)
        self.assertIn('cp "${CURRENT}" "${PREVIOUS}.tmp"', script)
        self.assertLess(script.index('"${BUNDLE}/verify.sh" restart'), script.index('mv "${record}" "${CURRENT}"'))

    def test_deployment_reports_pre_verifier_failures_without_sensitive_output(self):
        script = (STAGING / "deploy.sh").read_text()
        self.assertIn(
            "P8_DEPLOY_FAIL check=%s service=%s expected=%s actual=%s",
            script,
        )
        self.assertIn("fail_deploy compose-up-failed compose success failed", script)
        self.assertIn(
            "fail_deploy verifier-invocation-failed verify success failed",
            script,
        )
        self.assertIn(
            "fail_deploy verifier-invocation-failed restart success failed",
            script,
        )
        self.assertIn('if ! "${compose[@]}" up --no-build --pull never', script)
        self.assertIn('if ! "${BUNDLE}/verify.sh" verify; then', script)
        self.assertIn('if ! "${BUNDLE}/verify.sh" restart; then', script)

        function_start = script.index("fail_deploy() {")
        function_end = script.index("\n}\n", function_start) + len("\n}\n")
        result = subprocess.run(
            [
                "bash",
                "-c",
                script[function_start:function_end]
                + "\nfail_deploy compose-up-failed compose success failed",
            ],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            result.stderr,
            "P8_DEPLOY_FAIL check=compose-up-failed service=compose "
            "expected=success actual=failed\n",
        )

    def test_runtime_verifier_compares_canonical_registry_digest(self):
        verifier = (STAGING / "verify.sh").read_text()
        self.assertIn('tagged_ref="${exact_ref%@*}"', verifier)
        self.assertIn('repository="${tagged_ref%:*}"', verifier)
        self.assertIn('canonical_ref="${repository}@${exact_ref##*@}"', verifier)
        self.assertIn('grep -Fxq "${canonical_ref}" <<<"${repo_digests}"', verifier)
        self.assertNotIn('[[ "${repo_digests}" == *"${exact_ref}"* ]]', verifier)

    def test_runtime_verifier_has_stable_fail_closed_diagnostics(self):
        verifier = (STAGING / "verify.sh").read_text()
        self.assertIn("set -euo pipefail", verifier)
        self.assertIn(
            "P8_VERIFY_FAIL check=%s service=%s expected=%s actual=%s",
            verifier,
        )
        sections = {
            "wait_for_health": (
                "service-health-terminal",
                "service-health-timeout",
            ),
            "verify_image": (
                "image-running-id-mismatch",
                "image-config-unreadable",
                "local-image-config-mismatch",
                "canonical-repodigest-mismatch",
            ),
            "verify_labels": (
                "source-revision-mismatch",
                "pipeline-revision-mismatch",
                "configuration-identity-mismatch",
                "core-digest-mismatch",
                "private-provider-digest-mismatch",
                "p7-acceptance-mismatch",
            ),
            "verify_container_security": (
                "uid-mismatch",
                "readonly-rootfs-mismatch",
                "no-new-privileges-missing",
                "cap-drop-all-missing",
            ),
            "network": (
                "execution-ports-exposed",
                "execution-network-mode-mismatch",
            ),
            "health": (
                "execution-not-locked",
                "external-order-call-detected",
                "external-cancel-call-detected",
                "gateway-ingress-listener-inactive",
                "gateway-ingress-socket-invalid",
            ),
        }
        for section, checks in sections.items():
            with self.subTest(section=section):
                for check in checks:
                    self.assertIn(f"fail_check {check} ", verifier)

        wait_start = verifier.index("wait_for_health() {")
        wait_end = verifier.index("\n}\n", wait_start) + len("\n}\n")
        self.assertNotIn("return 1", verifier[wait_start:wait_end])

        for forbidden in (
            "PRIVATE_PROVIDER_FACTORY",
            "docker inspect --format '{{json .Config.Env}}'",
            "cat ${INSTALL_ROOT}/provider/factory",
        ):
            self.assertNotIn(forbidden, verifier)

        function_start = verifier.index("fail_check() {")
        function_end = verifier.index("\n}\n", function_start) + len("\n}\n")
        result = subprocess.run(
            [
                "bash",
                "-c",
                "set -e\n"
                + verifier[function_start:function_end]
                + "\nfail_check canonical-repodigest-mismatch market-api sha256:expected missing",
            ],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            result.stderr,
            "P8_VERIFY_FAIL check=canonical-repodigest-mismatch "
            "service=market-api expected=sha256:expected actual=missing\n",
        )

        wait_function = verifier[wait_start:wait_end]
        terminal = subprocess.run(
            [
                "bash",
                "-c",
                "set -e\n"
                + verifier[function_start:function_end]
                + wait_function
                + "\nfake_compose() { printf 'container-id\\n'; }"
                + "\ndocker() { printf 'exited\\n'; }"
                + "\nsleep() { :; }"
                + "\ncompose=(fake_compose)"
                + "\nwait_for_health execution-worker",
            ],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(terminal.returncode, 0)
        self.assertEqual(
            terminal.stderr,
            "P8_VERIFY_FAIL check=service-health-terminal "
            "service=execution-worker expected=healthy actual=exited\n",
        )

        timeout = subprocess.run(
            [
                "bash",
                "-c",
                "set -e\n"
                + verifier[function_start:function_end]
                + wait_function
                + "\nfake_compose() { return 0; }"
                + "\nsleep() { :; }"
                + "\ncompose=(fake_compose)"
                + "\nwait_for_health gateway",
            ],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(timeout.returncode, 0)
        self.assertEqual(
            timeout.stderr,
            "P8_VERIFY_FAIL check=service-health-timeout "
            "service=gateway expected=healthy actual=missing\n",
        )

    def test_runtime_health_convergence_requires_healthy(self):
        verifier = (STAGING / "verify.sh").read_text()
        fail_start = verifier.index("fail_check() {")
        fail_end = verifier.index("\n}\n", fail_start) + len("\n}\n")
        wait_start = verifier.index("wait_for_health() {")
        wait_end = verifier.index("\n}\n", wait_start) + len("\n}\n")
        functions = verifier[fail_start:fail_end] + verifier[wait_start:wait_end]

        def run(
            states: list[str], service: str = "execution-worker"
        ) -> subprocess.CompletedProcess[str]:
            with tempfile.TemporaryDirectory() as directory:
                state_file = Path(directory) / "states"
                state_file.write_text("\n".join(states) + "\n")
                quoted_state_file = shlex.quote(str(state_file))
                return subprocess.run(
                    [
                        "bash",
                        "-c",
                        "set -e\n"
                        + functions
                        + "\nfake_compose() { printf 'container-id\\n'; }"
                        + f"\nstate_file={quoted_state_file}"
                        + "\ndocker() {\n"
                        + "  state=$(head -n 1 \"${state_file}\")\n"
                        + "  tail -n +2 \"${state_file}\" > \"${state_file}.next\"\n"
                        + "  mv \"${state_file}.next\" \"${state_file}\"\n"
                        + "  printf '%s\\n' \"${state}\"\n"
                        + "}"
                        + "\nsleep() { :; }"
                        + "\ncompose=(fake_compose)"
                        + f"\nwait_for_health {shlex.quote(service)}",
                    ],
                    capture_output=True,
                    text=True,
                )

        with self.subTest(state="healthy"):
            healthy = run(["healthy"])
            self.assertEqual(healthy.returncode, 0, healthy.stderr)

        with self.subTest(state="starting-then-healthy"):
            starting = run(["starting", "healthy"])
            self.assertEqual(starting.returncode, 0, starting.stderr)

        with self.subTest(state="running"):
            running = run(["running"] * 45)
            self.assertNotEqual(running.returncode, 0)
            self.assertEqual(
                running.stderr,
                "P8_VERIFY_FAIL check=service-health-timeout "
                "service=execution-worker expected=healthy actual=running\n",
            )

        with self.subTest(state="unhealthy"):
            unhealthy = run(["unhealthy"])
            self.assertNotEqual(unhealthy.returncode, 0)
            self.assertEqual(
                unhealthy.stderr,
                "P8_VERIFY_FAIL check=service-health-terminal "
                "service=execution-worker expected=healthy actual=unhealthy\n",
            )

        with self.subTest(state="timeout"):
            timeout = run(["starting"] * 45, service="gateway")
            self.assertNotEqual(timeout.returncode, 0)
            self.assertEqual(
                timeout.stderr,
                "P8_VERIFY_FAIL check=service-health-timeout "
                "service=gateway expected=healthy actual=starting\n",
            )

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

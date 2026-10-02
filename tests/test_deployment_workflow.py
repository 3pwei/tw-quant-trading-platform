import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class DeploymentWorkflowTests(unittest.TestCase):
    def test_remote_script_is_not_executed_from_stdin(self):
        workflow = (ROOT / ".github/workflows/deploy-lightsail.yml").read_text()
        self.assertNotIn("deploy.sh' | sudo bash -s", workflow)
        self.assertIn("> /tmp/tw-quant-deploy.sh", workflow)
        self.assertIn("bash /tmp/tw-quant-deploy.sh", workflow)

    def test_approved_public_revision_is_staged_without_host_credentials(self):
        workflow = (ROOT / ".github/workflows/deploy-lightsail.yml").read_text()
        script = (ROOT / "deploy/lightsail/deploy.sh").read_text()

        self.assertIn("persist-credentials: false", workflow)
        self.assertIn("git bundle create", workflow)
        self.assertIn("git bundle verify", workflow)
        self.assertIn("lightsail-production:${remote_bundle}", workflow)
        self.assertIn("fetch '${remote_bundle}' HEAD", workflow)
        self.assertIn("trap 'rm -f ${remote_bundle}", workflow)
        self.assertNotIn("fetch origin", workflow)
        self.assertNotIn("fetch origin", script)
        self.assertIn('cat-file -e "${COMMIT_SHA}^{commit}"', script)

    def test_deploy_recreates_and_verifies_running_images(self):
        script = (ROOT / "deploy/lightsail/deploy.sh").read_text()
        self.assertIn("run --rm --no-deps -T market-api", script)
        self.assertIn("--force-recreate", script)
        self.assertIn("docker image inspect --format '{{.Id}}'", script)
        self.assertIn("docker inspect --format '{{.Image}}'", script)

    def test_execution_service_is_built_validated_and_has_no_public_route(self):
        compose = (ROOT / "deploy/lightsail/docker-compose.yml").read_text()
        caddy = (ROOT / "deploy/lightsail/Caddyfile").read_text()
        workflow = (ROOT / ".github/workflows/ci.yml").read_text()
        script = (ROOT / "deploy/lightsail/deploy.sh").read_text()

        self.assertIn("execution-worker:", compose)
        execution = compose.split("  execution-worker:", 1)[1].split(
            "\n  gateway:", 1
        )[0]
        self.assertNotIn("ports:", execution)
        self.assertNotIn("expose:", execution)
        self.assertIn("execution-egress", execution)
        market = compose.split("  market-api:", 1)[1].split(
            "\n  execution-worker:", 1
        )[0]
        self.assertIn(
            "execution-health:/run/tw-quant-execution:ro", market
        )
        self.assertNotIn("live-secrets", market)
        self.assertNotIn("execution-worker", caddy)
        self.assertIn("--target execution-worker", workflow)
        self.assertIn("tw_quant.execution_service validate", script)
        self.assertIn("published_ports", script)

    def test_host_preparation_runs_only_after_approved_revision_checkout(self):
        workflow = (ROOT / ".github/workflows/deploy-lightsail.yml").read_text()
        script = (ROOT / "deploy/lightsail/deploy.sh").read_text()

        self.assertNotIn("prepare-host.sh' | sudo bash", workflow)
        self.assertIn('checkout --detach "${COMMIT_SHA}"', script)
        self.assertIn('"${REPOSITORY}/deploy/lightsail/prepare-host.sh"', script)
        checkout = script.index('checkout --detach "${COMMIT_SHA}"')
        prepare = script.index('"${REPOSITORY}/deploy/lightsail/prepare-host.sh"')
        first_compose = script.index("docker compose")
        self.assertLess(checkout, prepare)
        self.assertLess(prepare, first_compose)

    def test_remote_transport_is_fail_fast_and_time_bounded(self):
        workflow = (ROOT / ".github/workflows/deploy-lightsail.yml").read_text()

        self.assertIn("ConnectTimeout 15", workflow)
        self.assertIn("ServerAliveInterval 15", workflow)
        self.assertIn("ServerAliveCountMax 4", workflow)
        self.assertIn("timeout --signal=TERM --kill-after=30s 2m scp", workflow)
        self.assertIn("timeout --signal=TERM --kill-after=30s 20m ssh", workflow)
        self.assertIn("flock -w 30 /var/lock/tw-quant-deploy.lock", workflow)
        self.assertIn("flock -w 300 /var/lock/tw-quant-deploy.lock", workflow)

    def test_forward_deploy_failure_triggers_verified_known_good_rollback(self):
        workflow = (ROOT / ".github/workflows/deploy-lightsail.yml").read_text()

        self.assertIn("id: deploy_revision", workflow)
        self.assertIn("Rollback after deployment failure", workflow)
        self.assertIn("steps.deploy_revision.conclusion == 'failure'", workflow)
        self.assertIn("steps.public_origin.conclusion == 'failure'", workflow)
        self.assertIn("needs.verify.outputs.deployment_mode == 'deploy'", workflow)
        self.assertIn("${{ steps.known_good.outputs.sha }}", workflow)
        self.assertIn("verify-deployment-record.sh", workflow)

    def test_failure_diagnostics_are_bounded_and_do_not_dump_container_environment(self):
        workflow = (ROOT / ".github/workflows/deploy-lightsail.yml").read_text()
        diagnostics = (ROOT / "deploy/lightsail/diagnose-production.sh").read_text()

        self.assertIn("Capture sanitized deployment failure context", workflow)
        self.assertIn("timeout --signal=TERM --kill-after=10s 90s ssh", workflow)
        self.assertIn("sanitized_deployment_context_begin", diagnostics)
        self.assertNotIn("docker logs", diagnostics)
        self.assertNotIn(".Config.Env", diagnostics)
        self.assertNotIn("env |", diagnostics)


    def test_host_preparation_script_is_executable(self):
        script = ROOT / "deploy/lightsail/prepare-host.sh"

        self.assertNotEqual(script.stat().st_mode & 0o111, 0)

    def test_host_preparation_migrates_compatibility_market_secret_names(self):
        script = (ROOT / "deploy/lightsail/prepare-host.sh").read_text()

        self.assertIn('migrate_market_key "SJ_API_KEY" "MARKET_SJ_API_KEY"', script)
        self.assertIn(
            'migrate_market_key "SJ_SEC_KEY" "MARKET_SJ_SECRET_KEY"', script
        )
        self.assertIn(
            'migrate_market_key "SJ_PRODUCTION" "MARKET_SJ_PRODUCTION"', script
        )
        self.assertNotIn('cat "${MARKET_ENV}"', script)

        with tempfile.TemporaryDirectory() as directory:
            install_root = Path(directory)
            config = install_root / "config"
            example = install_root / "repo/deploy/lightsail/execution.env.example"
            config.mkdir(parents=True)
            example.parent.mkdir(parents=True)
            example.write_text("BROKER_PROVIDER=disabled\n")
            market_env = config / "market.env"
            market_env.write_text(
                "SJ_API_KEY=legacy-key\n"
                "SJ_SEC_KEY=legacy-secret\n"
                "SJ_PRODUCTION=false\n"
            )

            # This test covers key migration/redaction, not privileged host
            # ownership; container integration separately verifies real UID/modes.
            # A root-only install shim keeps synthetic mounts portable in CaaS.
            fixture_environment = {**os.environ, "INSTALL_ROOT": str(install_root)}
            if os.geteuid() == 0:
                shim = install_root / "fixture-bin"
                shim.mkdir()
                installer = shim / "install"
                installer.write_text("#!/usr/bin/env bash\nargs=()\nwhile (( $# )); do\ncase \"$1\" in -o|-g) shift 2;; *) args+=(\"$1\"); shift;; esac\ndone\nexec /usr/bin/install \"${args[@]}\"\n")
                installer.chmod(0o755)
                fixture_environment["PATH"] = str(shim) + os.pathsep + os.environ["PATH"]
            result = subprocess.run(
                [str(ROOT / "deploy/lightsail/prepare-host.sh")],
                env=fixture_environment,
                check=True,
                capture_output=True,
                text=True,
            )
            migrated = market_env.read_text()

        self.assertIn("MARKET_SJ_API_KEY=legacy-key", migrated)
        self.assertIn("MARKET_SJ_SECRET_KEY=legacy-secret", migrated)
        self.assertIn("MARKET_SJ_PRODUCTION=false", migrated)
        self.assertNotRegex(migrated, r"(?m)^SJ_(?:API_KEY|SEC_KEY|PRODUCTION)=")
        self.assertNotIn("legacy-key", result.stdout + result.stderr)
        self.assertNotIn("legacy-secret", result.stdout + result.stderr)

if __name__ == "__main__":
    unittest.main()

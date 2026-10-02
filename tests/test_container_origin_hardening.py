from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ContainerOriginHardeningTests(unittest.TestCase):
    def test_python_runtime_is_non_root(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("useradd --uid 10001", dockerfile)
        self.assertIn("USER 10001:10001", dockerfile)

    def test_python_runtime_uses_only_tmpfs_for_user_caches(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        for setting in (
            "HOME=/tmp",
            "XDG_CACHE_HOME=/tmp/.cache",
            "MPLCONFIGDIR=/tmp/matplotlib",
            "NUMBA_CACHE_DIR=/tmp/numba",
        ):
            self.assertIn(setting, dockerfile)
        self.assertNotIn("HOME=/root", dockerfile)

    def test_hardened_runtime_regression_covers_start_restart_and_diagnostics(self) -> None:
        script = (
            ROOT / "deploy/lightsail/test-hardened-runtime.sh"
        ).read_text(encoding="utf-8")
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        for contract in (
            "up --no-build --detach --wait",
            "wait_healthy market-api",
            "restart market-api gateway",
            "Content-Security-Policy",
            "ReadonlyRootfs",
            "CapDrop",
            "no-new-privileges:true",
            "stat -c '%u:%g' /data",
            "docker logs --tail 100",
            "sanitize",
        ):
            self.assertIn(contract, script)
        self.assertIn("test-hardened-runtime.sh", workflow)
        self.assertIn("test-gateway-auth.sh", workflow)

    def test_deploy_failure_diagnostics_are_sanitized(self) -> None:
        deploy = (ROOT / "deploy/lightsail/deploy.sh").read_text(encoding="utf-8")
        self.assertIn("dump_startup_diagnostics", deploy)
        self.assertIn("sanitize_runtime_output", deploy)
        self.assertIn("docker logs --tail 100", deploy)
        self.assertNotIn("docker inspect --format '{{json .Config.Env}}'", deploy)

    def test_gateway_runtime_is_non_root(self) -> None:
        dockerfile = (ROOT / "deploy/lightsail/Dockerfile.gateway").read_text(
            encoding="utf-8"
        )
        self.assertIn("adduser -u 10000", dockerfile)
        self.assertIn("USER 10000:10000", dockerfile)

    def test_compose_applies_least_privilege_to_every_service(self) -> None:
        compose = (ROOT / "deploy/lightsail/docker-compose.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("read_only: true", compose)
        self.assertIn("cap_drop:\n    - ALL", compose)
        self.assertIn("no-new-privileges:true", compose)
        self.assertEqual(compose.count("<<: *runtime-security"), 3)
        self.assertEqual(compose.count("pids_limit:"), 1)
        self.assertEqual(compose.count("mem_limit:"), 3)
        self.assertEqual(compose.count("cpus:"), 3)
        self.assertEqual(compose.count('max-size: "10m"'), 1)
        self.assertEqual(compose.count("NET_BIND_SERVICE"), 1)
        self.assertNotIn("privileged:", compose)
        self.assertNotIn("network_mode: host", compose)
        self.assertNotIn("/var/run/docker.sock", compose)

    def test_secret_mount_is_execution_only_and_read_only(self) -> None:
        compose = (ROOT / "deploy/lightsail/docker-compose.yml").read_text(
            encoding="utf-8"
        )
        self.assertEqual(compose.count("/run/live-secrets:ro"), 1)
        execution = compose.split("  execution-worker:", 1)[1].split(
            "  gateway:", 1
        )[0]
        self.assertIn("/run/live-secrets:ro", execution)
        self.assertNotIn("ports:", execution)

    def test_origin_headers_cover_static_api_and_websocket_requirements(self) -> None:
        caddyfile = (ROOT / "deploy/lightsail/Caddyfile").read_text(
            encoding="utf-8"
        )
        for header in (
            "Content-Security-Policy",
            "Permissions-Policy",
            "Cross-Origin-Opener-Policy",
            "Cross-Origin-Resource-Policy",
        ):
            self.assertIn(header, caddyfile)
        self.assertIn("script-src 'self' 'unsafe-inline'", caddyfile)
        self.assertIn("connect-src 'self' https: wss: ws:", caddyfile)
        self.assertIn("frame-ancestors 'none'", caddyfile)

    def test_production_verifier_checks_runtime_security(self) -> None:
        verify = (ROOT / "deploy/lightsail/verify-production.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("verify_container_security", verify)
        self.assertIn("verify_mount_boundaries", verify)
        self.assertIn("ReadonlyRootfs", verify)
        self.assertIn("CapDrop", verify)
        self.assertIn("PidsLimit", verify)
        self.assertIn("/run/live-secrets false", verify)
        self.assertIn("restart market-api execution-worker gateway", verify)

    def test_host_and_volume_permissions_are_migrated_explicitly(self) -> None:
        prepare = (ROOT / "deploy/lightsail/prepare-host.sh").read_text(
            encoding="utf-8"
        )
        deploy = (ROOT / "deploy/lightsail/deploy.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("RUNTIME_UID=10001", prepare)
        self.assertIn("-exec chmod 0400", prepare)
        self.assertIn("prepare_named_volume", deploy)
        self.assertFalse(
            any(line.startswith("+#") for line in deploy.splitlines()),
            "diff markers must not become executable shell tokens",
        )
        for volume in (
            "platform-production_market-data",
            "platform-production_execution-health",
            "platform-production_caddy-data",
            "platform-production_caddy-config",
        ):
            self.assertIn(volume, deploy)


if __name__ == "__main__":
    unittest.main()

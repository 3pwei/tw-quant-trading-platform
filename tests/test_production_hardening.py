from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
CORE_SHA256 = "63645e42755068c308d66d74ded5133395dfef816360dc8106e0bbc247ee49bd"


class ProductionHardeningTests(unittest.TestCase):
    def test_images_are_labeled_with_revision_and_pinned_core(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        compose = (ROOT / "deploy/lightsail/docker-compose.yml").read_text(
            encoding="utf-8"
        )
        requirement = (ROOT / "uv.lock").read_text(encoding="utf-8")

        self.assertIn("ARG DEPLOY_COMMIT_SHA", dockerfile)
        self.assertIn("org.opencontainers.image.revision", dockerfile)
        self.assertIn("io.tw-quant.core.version=\"1.2.0\"", dockerfile)
        self.assertIn(CORE_SHA256, dockerfile)
        self.assertGreaterEqual(compose.count("DEPLOY_COMMIT_SHA:"), 2)
        self.assertIn("uv sync --locked", dockerfile)
        self.assertIn(f"sha256:{CORE_SHA256}", requirement)

    def test_manual_rollback_verifies_the_selected_revision(self) -> None:
        workflow = (ROOT / ".github/workflows/deploy-lightsail.yml").read_text(
            encoding="utf-8"
        )
        checkout_target = 'git checkout --detach "$sha"'
        dependency_install = "uv sync --locked"

        self.assertIn(checkout_target, workflow)
        self.assertLess(workflow.index(checkout_target), workflow.index(dependency_install))
        self.assertIn('git merge-base --is-ancestor "$sha" master', workflow)
        self.assertIn("deployment_mode", workflow)

    def test_deployment_runs_identity_fail_closed_and_restart_gates(self) -> None:
        deploy = (ROOT / "deploy/lightsail/deploy.sh").read_text(encoding="utf-8")
        verify = (ROOT / "deploy/lightsail/verify-production.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn('bash "${REPOSITORY}/deploy/lightsail/verify-production.sh" "${COMMIT_SHA}" verify', deploy)
        self.assertIn('bash "${REPOSITORY}/deploy/lightsail/verify-production.sh" "${COMMIT_SHA}" restart', deploy)
        self.assertIn("tw_quant.execution_service healthcheck", verify)
        self.assertIn("restart market-api execution-worker", verify)
        self.assertIn("tw_quant_core.__version__ == \"1.2.0\"", verify)
        self.assertIn("previous_known_good_sha", deploy)
        self.assertNotIn("TW_QUANT_CORE_ROLLBACK", verify)


if __name__ == "__main__":
    unittest.main()

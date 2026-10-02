"""Public policy rejection, synthetic provenance and gate phase regression."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from tw_quant.synthetic_data import write_ticks

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("publication_policy", ROOT / "tools/verify_publication_policy.py")
policy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(policy)


class PublicationPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        index = json.loads((ROOT / "public-candidate.json").read_text())
        cls.output = {row["path"]: (ROOT / row["path"]).read_bytes() for row in index["files"]}

    def test_native_codeql_is_a_p6_acceptance_and_policy_is_unconditional(self):
        result = policy.check(self.output, syntax=True)
        self.assertEqual(result["codeql_policy"], "PASS")
        self.assertEqual(result["native_codeql"], "NOT_RUN_P5; REQUIRED_P6")
        self.assertIn("native CodeQL python", result["p6_acceptance"])
        self.assertIn("native CodeQL javascript-typescript", result["p6_acceptance"])

    def test_codeql_cannot_be_skipped_or_change_languages_upload_permissions(self):
        text = self.output[".github/workflows/security.yml"].decode()
        for changed in (
            text.replace("  codeql:\n", "  codeql:\n    if: false\n"),
            text.replace("security-events: write", "security-events: read"),
            text.replace("language: [python, javascript-typescript]", "language: [python]"),
            text.replace("upload: true", "upload: false"),
            text.replace("if-no-files-found: error", "if-no-files-found: warn"),
            text.replace("--quality-threshold error", "--quality-threshold warning"),
        ):
            with self.subTest(change=changed), self.assertRaises(ValueError):
                policy.check_codeql(changed)

    def test_automatic_deployment_or_unattested_rollback_fails_policy(self):
        text = self.output[".github/workflows/deploy-lightsail.yml"].decode()
        for changed in (
            text.replace("  workflow_dispatch:", "  push:\n  workflow_dispatch:"),
            text.replace('test "${#sha}" -eq 40', 'test "${#sha}" -eq 7'),
            text.replace("DEPLOY lightsail-production ${sha}", "DEPLOY"),
            text.replace("ROLLBACK_SHA: ${{ steps.known_good.outputs.sha }}", "ROLLBACK_SHA: " + "a" * 40),
            text.replace("steps.known_good.conclusion == 'success'", "true"),
            text.replace("needs.verify.result == 'success'", "true"),
        ):
            with self.subTest(change=changed), self.assertRaises(ValueError):
                policy.check_deploy(changed)

    def test_synthetic_runtime_output_is_deterministic_and_has_no_external_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            one, two = root / "one.csv", root / "two.csv"
            write_ticks(one); write_ticks(two)
            self.assertEqual(one.read_bytes(), two.read_bytes())
            self.assertIn("2000-01-03T09:00:00+08:00", one.read_text())
            self.assertEqual(len(one.read_text().splitlines()), 121)
            with self.assertRaises(ValueError): write_ticks(one, count=0)
        self.assertNotIn("data/" + "mock_tmf_ticks.csv", self.output)
        self.assertFalse(any(p.endswith((".sqlite3", "-wal", "-shm")) for p in self.output))

    def test_notice_coverage_is_bound_to_actual_locks(self):
        changed = dict(self.output)
        notice = json.loads(changed["docs/dependency-notices.json"])
        notice["python"].pop()
        changed["docs/dependency-notices.json"] = json.dumps(notice).encode()
        with self.assertRaisesRegex(ValueError, "Python notices coverage"):
            policy.check(changed)
        changed = dict(self.output)
        changed["uv.lock"] += b"\n# change\n"
        with self.assertRaisesRegex(ValueError, "notice input changed"):
            policy.check(changed)
        changed = dict(self.output)
        notice = json.loads(changed["docs/dependency-notices.json"])
        notice["container_artifacts"][0]["sha256"] = "0" * 64
        changed["docs/dependency-notices.json"] = json.dumps(notice).encode()
        with self.assertRaisesRegex(ValueError, "Caddy release identity"):
            policy.check(changed)

    def test_social_and_visual_exclusions_have_no_dangling_reference(self):
        self.assertNotIn("dashboard/public/" + "og.png", self.output)
        self.assertNotIn(".github/workflows/" + "demo-visual.yml", self.output)
        self.assertNotIn("dashboard/scripts/" + "verify-demo-visual.mjs", self.output)
        self.assertNotIn("og.png", self.output["dashboard/app/layout.tsx"].decode())


if __name__ == "__main__":
    unittest.main()

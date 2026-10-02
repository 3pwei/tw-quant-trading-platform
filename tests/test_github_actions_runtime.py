from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = ROOT / ".github" / "workflows"
USES_RE = re.compile(r"^\s*(?:-\s*)?uses:\s*([^@\s]+)@([^\s#]+)(?:\s+#\s*(\S+))?\s*$")

NODE24_ACTIONS = {
    "actions/checkout": ("3d3c42e5aac5ba805825da76410c181273ba90b1", "v7.0.1"),
    "actions/setup-python": ("5fda3b95a4ea91299a34e894583c3862153e4b97", "v7.0.0"),
    "actions/setup-node": ("820762786026740c76f36085b0efc47a31fe5020", "v7.0.0"),
    "actions/upload-artifact": ("043fb46d1a93c77aae656e7c1c64a875d1fc6a0a", "v7.0.1"),
    "github/codeql-action/init": ("1c5b675653bb5c22dbe9b12b556ec555138e09fd", "v4.38.1"),
    "github/codeql-action/analyze": ("1c5b675653bb5c22dbe9b12b556ec555138e09fd", "v4.38.1"),
    "gitleaks/gitleaks-action": ("e0c47f4f8be36e29cdc102c57e68cb5cbf0e8d1e", "v3.0.0"),
    "anchore/sbom-action": ("3ad7283483fc7af8ff2b4ea19663c2d5ca935e26", "v0.24.2"),
}


def workflow_uses() -> list[tuple[Path, int, str, str, str | None]]:
    found: list[tuple[Path, int, str, str, str | None]] = []
    for path in sorted(WORKFLOW_DIR.glob("*.y*ml")):
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "uses:" not in line:
                continue
            match = USES_RE.match(line)
            if match is None:
                raise AssertionError(f"unparseable uses reference: {path}:{line_number}: {line}")
            action, revision, version = match.groups()
            if action.startswith("./"):
                continue
            found.append((path, line_number, action, revision, version))
    return found


class GitHubActionsRuntimeTests(unittest.TestCase):
    def test_external_actions_remain_immutable_and_versioned(self) -> None:
        references = workflow_uses()
        self.assertTrue(references)
        for path, line_number, action, revision, version in references:
            with self.subTest(path=path.name, line=line_number, action=action):
                self.assertRegex(revision, r"^[0-9a-f]{40}$")
                self.assertIsNotNone(version)

    def test_known_javascript_actions_use_reviewed_node24_releases(self) -> None:
        references = workflow_uses()
        seen: set[str] = set()
        for path, line_number, action, revision, version in references:
            if action not in NODE24_ACTIONS:
                continue
            seen.add(action)
            expected_revision, expected_version = NODE24_ACTIONS[action]
            with self.subTest(path=path.name, line=line_number, action=action):
                self.assertEqual(revision, expected_revision)
                self.assertEqual(version, expected_version)
        self.assertEqual(seen, set(NODE24_ACTIONS))

    def test_workflows_do_not_restore_node20_opt_out(self) -> None:
        workflows = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted(WORKFLOW_DIR.glob("*.y*ml"))
        )
        self.assertNotIn("ACTIONS_ALLOW_USE_UNSECURE_NODE_VERSION", workflows)


if __name__ == "__main__":
    unittest.main()

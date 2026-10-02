from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "deploy" / "security" / "codeql_sarif_gate.py"
WORKFLOW = ROOT / ".github" / "workflows" / "security.yml"


def sarif(*rules: tuple[str, str | None, str], message: str = "private finding") -> dict:
    reporting_rules = []
    results = []
    for index, (rule_id, security_severity, quality_level) in enumerate(rules):
        properties = {"problem.severity": quality_level}
        if security_severity is not None:
            properties["security-severity"] = security_severity
        reporting_rules.append(
            {
                "id": rule_id,
                "defaultConfiguration": {"level": quality_level},
                "properties": properties,
            }
        )
        results.append(
            {
                "ruleId": rule_id,
                "rule": {
                    "id": rule_id,
                    "index": index,
                    "toolComponent": {"index": 0},
                },
                "message": {"text": message},
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {"uri": "private/source.py"},
                            "region": {"startLine": 99},
                        }
                    }
                ],
            }
        )
    return {
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {"name": "CodeQL", "rules": []},
                    "extensions": [{"name": "queries", "rules": reporting_rules}],
                },
                "results": results,
            }
        ],
    }


class CodeqlSarifGateTests(unittest.TestCase):
    def run_gate(self, document: object | None, *, raw: str | None = None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            if document is not None or raw is not None:
                (root / "results.sarif").write_text(
                    raw if raw is not None else json.dumps(document), encoding="utf-8"
                )
            return subprocess.run(
                [
                    sys.executable,
                    str(GATE),
                    "--input",
                    str(root),
                    "--security-threshold",
                    "high",
                    "--quality-threshold",
                    "error",
                ],
                check=False,
                capture_output=True,
                text=True,
            )

    def test_empty_results_pass(self) -> None:
        result = self.run_gate(sarif())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("blocked=0", result.stdout)

    def test_high_and_critical_security_findings_fail(self) -> None:
        for score in ("7.0", "9.0"):
            with self.subTest(score=score):
                result = self.run_gate(sarif(("security/rule", score, "error")))
                self.assertEqual(result.returncode, 1)
                self.assertIn("blocked=1", result.stdout)

    def test_medium_security_finding_does_not_become_quality_error(self) -> None:
        result = self.run_gate(sarif(("security/rule", "6.9", "error")))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("security_medium=1", result.stdout)

    def test_error_quality_finding_fails(self) -> None:
        result = self.run_gate(sarif(("quality/rule", None, "error")))
        self.assertEqual(result.returncode, 1)
        self.assertIn("quality_error=1", result.stdout)

    def test_warning_quality_finding_passes(self) -> None:
        result = self.run_gate(sarif(("quality/rule", None, "warning")))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_and_malformed_sarif_fail_closed(self) -> None:
        missing = self.run_gate(None)
        malformed = self.run_gate(None, raw="not-json")
        self.assertEqual(missing.returncode, 2)
        self.assertEqual(malformed.returncode, 2)

    def test_output_never_discloses_finding_message_or_location(self) -> None:
        secret = "DO_NOT_PRINT_FINDING_CONTENT"
        result = self.run_gate(
            sarif(("security/rule", "9.0", "error"), message=secret)
        )
        combined = result.stdout + result.stderr
        self.assertNotIn(secret, combined)
        self.assertNotIn("private/source.py", combined)
        self.assertNotIn("security/rule", combined)

    def test_security_workflow_uploads_and_enforces_results(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        codeql_job = workflow.split("  codeql:\n", 1)[1].split("\n  secrets:\n", 1)[0]
        self.assertIn("security-events: write", codeql_job)
        self.assertIn("upload: true", codeql_job)
        self.assertNotIn("upload: false", codeql_job)
        self.assertIn("Enforce CodeQL finding policy", codeql_job)
        self.assertIn("--security-threshold high", codeql_job)
        self.assertIn("--quality-threshold error", codeql_job)
        self.assertNotIn("continue-on-error", codeql_job)


if __name__ == "__main__":
    unittest.main()

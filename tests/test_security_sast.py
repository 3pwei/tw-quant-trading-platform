from __future__ import annotations

import contextlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "deploy" / "security" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


eligibility = load("codeql_eligibility")
gate = load("python_sast")


class SecuritySastTests(unittest.TestCase):
    def test_personal_private_is_explicitly_na_even_with_override(self):
        for override in ("", "true"):
            enabled, status = eligibility.evaluate({"private": True, "owner": {"type": "User"}}, override)
            self.assertFalse(enabled)
            self.assertIn("N/A", status)

    def test_public_and_attested_organization_require_native_analysis(self):
        self.assertTrue(eligibility.evaluate({"private": False, "owner": {"type": "User"}}, "")[0])
        self.assertTrue(eligibility.evaluate({"private": True, "owner": {"type": "Organization"}}, "true")[0])

    def test_unknown_context_or_unattested_organization_fails_closed(self):
        for repo in ({}, {"private": "false", "owner": {"type": "User"}}, {"private": True, "owner": {"type": "Organization"}}):
            with self.subTest(repo=repo), self.assertRaises(ValueError):
                eligibility.evaluate(repo, "")

    def test_high_findings_block_at_every_confidence_without_disclosure(self):
        for confidence in ("LOW", "MEDIUM", "HIGH"):
            output = io.StringIO()
            report = {"errors": [], "metrics": {"_totals": {"loc": 1}}, "results": [
                {"issue_severity": "HIGH", "issue_confidence": confidence, "code": "PRIVATE_FINDING"},
            ]}
            with contextlib.redirect_stdout(output):
                self.assertEqual(gate.enforce(report), 1)
            self.assertNotIn("PRIVATE_FINDING", output.getvalue())

    def test_lower_severity_reported_and_clean_scan_passes(self):
        for severity in ("LOW", "MEDIUM", None):
            report = {"errors": [], "metrics": {"_totals": {"loc": 1}}, "results": []}
            if severity:
                report["results"].append({"issue_severity": severity, "issue_confidence": "LOW"})
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(gate.enforce(report), 0)

    def test_empty_invalid_or_partial_report_fails_closed(self):
        for report in ({}, {"errors": ["parse error"]}, {"errors": [], "results": [], "metrics": {}},
                       {"errors": [], "metrics": {"_totals": {"loc": 1}}, "results": [{"issue_severity": "UNKNOWN"}]}):
            with self.subTest(report=report), self.assertRaises(ValueError):
                gate.enforce(report)

    @unittest.skipUnless(os.environ.get("TW_QUANT_SAST_INTEGRATION") == "1", "requires pinned Bandit scanner")
    def test_real_scanner_blocks_synthetic_high_and_nosec_and_parse_error(self):
        # Generated temporary snippets are scanned, never executed or committed.
        cases = [
            ("import os\nos.system(input())\n", 1),
            ("import os\nos.system(input())  # nosec\n", 1),
            ("def broken(:\n", 2),
            ("answer = 42\n", 0),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "probe.py"
            for source, expected in cases:
                path.write_text(source, encoding="utf-8")
                result = subprocess.run([sys.executable, str(ROOT / "deploy/security/python_sast.py"), str(path)],
                                        check=False, capture_output=True, text=True)
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                self.assertNotIn(source.strip(), result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()

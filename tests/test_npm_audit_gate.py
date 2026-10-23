from __future__ import annotations

from datetime import date
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "deploy/security/npm_audit_gate.py"
SPEC = importlib.util.spec_from_file_location("npm_audit_gate", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
GATE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = GATE
SPEC.loader.exec_module(GATE)


class NpmAuditGateTests(unittest.TestCase):
    def fixture(self, directory: Path, *, expiry: str = "2026-10-18"):
        exceptions = {
            "schema_version": 1,
            "exceptions": [
                {
                    "id": "GHSA-vfj7-8cjw-p6xm",
                    "package": "braces",
                    "version": "3.0.3",
                    "severity": "high",
                    "dependency_scope": "dev",
                    "affected_packages": ["braces", "micromatch"],
                    "expired_at": expiry,
                    "statement": "A sufficiently detailed, time-bounded dev-only exception statement.",
                }
            ],
        }
        lock = {
            "lockfileVersion": 3,
            "packages": {"node_modules/braces": {"version": "3.0.3", "dev": True}},
        }
        report = {
            "auditReportVersion": 2,
            "vulnerabilities": {
                "braces": {
                    "severity": "high",
                    "via": [
                        {
                            "url": "https://github.com/advisories/GHSA-vfj7-8cjw-p6xm",
                            "severity": "high",
                        }
                    ],
                },
                "micromatch": {"severity": "high", "via": ["braces"]},
            },
            "metadata": {"vulnerabilities": {"high": 2, "critical": 0}},
        }
        paths = {}
        for name, value in (
            ("exceptions", exceptions),
            ("lock", lock),
            ("report", report),
        ):
            path = directory / f"{name}.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            paths[name] = path
        return paths

    def test_exact_time_bounded_dev_chain_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self.fixture(Path(temporary))
            result = GATE.enforce(
                paths["report"],
                paths["lock"],
                paths["exceptions"],
                date(2026, 10, 3),
            )
        self.assertEqual(result, {"high": 2, "critical": 0, "exceptions": 1})

    def test_expired_exception_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self.fixture(Path(temporary), expiry="2026-10-03")
            with self.assertRaisesRegex(ValueError, "expired"):
                GATE.enforce(
                    paths["report"],
                    paths["lock"],
                    paths["exceptions"],
                    date(2026, 10, 3),
                )

    def test_unexpected_advisory_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self.fixture(Path(temporary))
            report = json.loads(paths["report"].read_text(encoding="utf-8"))
            report["vulnerabilities"]["other"] = {
                "severity": "critical",
                "via": [
                    {
                        "url": "https://github.com/advisories/GHSA-aaaa-bbbb-cccc",
                        "severity": "critical",
                    }
                ],
            }
            report["metadata"]["vulnerabilities"]["critical"] = 1
            paths["report"].write_text(json.dumps(report), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "advisory set mismatch"):
                GATE.enforce(
                    paths["report"],
                    paths["lock"],
                    paths["exceptions"],
                    date(2026, 10, 3),
                )

    def test_package_version_or_scope_drift_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self.fixture(Path(temporary))
            lock = json.loads(paths["lock"].read_text(encoding="utf-8"))
            lock["packages"]["node_modules/braces"]["dev"] = False
            paths["lock"].write_text(json.dumps(lock), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "no longer dev-only"):
                GATE.enforce(
                    paths["report"],
                    paths["lock"],
                    paths["exceptions"],
                    date(2026, 10, 3),
                )

    def test_repository_exception_matches_current_lock(self) -> None:
        exceptions = GATE.load_exceptions(
            ROOT / "deploy/security/npm-audit-exceptions.json"
        )
        GATE.validate_lock(exceptions, ROOT / "dashboard/package-lock.json")
        GATE.validate_expiry(exceptions, date(2026, 10, 3), fail_on_expired=True)

    def test_no_exceptions_accepts_clean_audit_but_rejects_returning_advisory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self.fixture(Path(temporary))
            paths["exceptions"].write_text(json.dumps({"schema_version": 1, "exceptions": []}))
            original_report = paths["report"].read_text()
            paths["report"].write_text(json.dumps({
                "auditReportVersion": 2,
                "vulnerabilities": {},
                "metadata": {"vulnerabilities": {"high": 0, "critical": 0}},
            }))
            self.assertEqual(GATE.enforce(
                paths["report"], paths["lock"], paths["exceptions"], date(2026, 10, 24)
            ), {"high": 0, "critical": 0, "exceptions": 0})
            paths["report"].write_text(original_report)
            with self.assertRaisesRegex(ValueError, "unexpected=GHSA-vfj7-8cjw-p6xm"):
                GATE.enforce(
                    paths["report"], paths["lock"], paths["exceptions"], date(2026, 10, 24)
                )


if __name__ == "__main__":
    unittest.main()

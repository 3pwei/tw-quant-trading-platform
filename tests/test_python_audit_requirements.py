from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "audit_requirements", ROOT / "deploy/security/audit_requirements.py"
)
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)
URL = ("https://github.com/3pwei/tw-quant-core/releases/download/"
       "v1.2.0/tw_quant_core-1.2.0-py3-none-any.whl")
DIGEST = "sha256:" + "a" * 64


class AuditRequirementsTests(unittest.TestCase):
    def fixture(self, directory):
        root = Path(directory)
        lock = root / "uv.lock"
        lock.write_text(
            '[[package]]\nname = "tw-quant-core"\nversion = "1.2.0"\n'
            f'source = {{ url = "{URL}" }}\n'
            f'wheels = [{{ url = "{URL}", hash = "{DIGEST}" }}]\n'
        )
        source = root / "export.txt"
        source.write_text(
            'shioaji==1.7.4 \\\n    --hash=sha256:' + "b" * 64 + '\n'
            f'tw-quant-core @ {URL} \\\n    --hash={DIGEST}\n'
            'numpy==2.4.6 ; python_full_version < "3.12" \\\n    --hash=sha256:' + "c" * 64 + '\n'
        )
        return lock, source, root / "audit.txt"

    def test_core_is_audited_by_locked_identity_without_dropping_other_packages(self):
        with tempfile.TemporaryDirectory() as directory:
            lock, source, output = self.fixture(directory)
            original = source.read_bytes()
            AUDIT.prepare(lock, source, output)
            expected = original.decode().replace(f"tw-quant-core @ {URL}", "tw-quant-core==1.2.0")
            self.assertEqual(output.read_text(), expected)
            self.assertEqual(source.read_bytes(), original)
            self.assertIn("shioaji==1.7.4", output.read_text())

    def test_identity_or_hash_drift_fails_closed_and_removes_stale_output(self):
        for fault in ("source_url", "export_hash", "missing", "duplicate", "lock_source"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                lock, source, output = self.fixture(directory)
                output.write_text("stale PASS input")
                text = source.read_text()
                if fault == "source_url": text = text.replace(URL, "https://example.invalid/core.whl")
                elif fault == "export_hash": text = text.replace(DIGEST, "sha256:" + "d" * 64)
                elif fault == "missing": text = "shioaji==1.7.4\n"
                elif fault == "duplicate": text += text
                elif fault == "lock_source": lock.write_text(lock.read_text().replace(URL, "https://example.invalid/core.whl"))
                source.write_text(text)
                with self.assertRaises(ValueError):
                    AUDIT.prepare(lock, source, output)
                self.assertFalse(output.exists())

    def test_workflow_keeps_complete_locked_scope_strict_checks_and_failure_evidence(self):
        workflow = (ROOT / ".github/workflows/security.yml").read_text()
        job = workflow.split("  python-audit:\n", 1)[1].split("  npm-audit:\n", 1)[0]
        for token in ("python tools/verify_public_candidate.py", "uv lock --check",
                      "--locked --all-extras --no-dev --no-emit-project",
                      "--strict --disable-pip --require-hashes", "--vulnerability-service osv",
                      "if: always()", "name: python-audit-evidence", "if-no-files-found: error"):
            self.assertIn(token, job)
        for token in ("--ignore-vuln", "--no-hashes", "continue-on-error", "|| true", "--no-emit-package"):
            self.assertNotIn(token, job)


if __name__ == "__main__":
    unittest.main()

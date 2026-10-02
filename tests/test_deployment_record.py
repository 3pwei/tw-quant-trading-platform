from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy/lightsail/verify-deployment-record.sh"
DEPLOYED_SHA = "a" * 40
PREVIOUS_SHA = "b" * 40


class DeploymentRecordTests(unittest.TestCase):
    def write_record(self, root: Path, *, sha: str = DEPLOYED_SHA, mode: str = "deploy") -> Path:
        directory = root / "deployments"
        directory.mkdir(mode=0o700)
        directory.chmod(0o700)
        record = directory / "current.env"
        record.write_text(
            f"deployment_mode={mode}\n"
            f"deployed_sha={sha}\n"
            f"previous_known_good_sha={PREVIOUS_SHA}\n"
            "public_core_version=1.0.0\n"
            "public_core_sha256=redacted-test-value\n"
            "verified_at=2026-09-24T03:38:06Z\n"
        )
        record.chmod(0o600)
        return record

    def run_verifier(self, root: Path, sha: str = DEPLOYED_SHA, mode: str = "deploy") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(SCRIPT), sha, mode],
            env={**os.environ, "INSTALL_ROOT": str(root)},
            capture_output=True,
            text=True,
        )

    def test_exact_revision_and_mode_are_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_record(root)
            result = self.run_verifier(root)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(DEPLOYED_SHA, result.stdout)

    def test_stale_revision_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_record(root, sha="c" * 40)
            result = self.run_verifier(root)

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("c" * 40, result.stderr)

    def test_insecure_record_permissions_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = self.write_record(root)
            record.chmod(0o644)
            result = self.run_verifier(root)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("permissions are invalid", result.stderr)

    def test_duplicate_revision_field_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = self.write_record(root)
            with record.open("a") as stream:
                stream.write(f"deployed_sha={DEPLOYED_SHA}\n")
            result = self.run_verifier(root)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid deployed_sha field", result.stderr)


if __name__ == "__main__":
    unittest.main()

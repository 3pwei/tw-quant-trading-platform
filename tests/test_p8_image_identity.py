from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
STAGING = ROOT / "deploy/staging"
SPEC = importlib.util.spec_from_file_location("image_config_digest", STAGING / "image_config_digest.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
CONFIG = b'{"architecture":"amd64","os":"linux","config":{"Env":["TEST=synthetic"]}}'
CONFIG_HEX = hashlib.sha256(CONFIG).hexdigest()
CONFIG_DIGEST = "sha256:" + CONFIG_HEX
REGISTRY_DIGEST = "sha256:" + "a" * 64
REF = "registry.example:5000/public/fixture:one@" + REGISTRY_DIGEST
CANONICAL = "registry.example:5000/public/fixture@" + REGISTRY_DIGEST


def archive_bytes(oci=False, corrupt=False, missing=False, multiple=False, duplicate=False):
    path = "blobs/sha256/" + CONFIG_HEX if oci else CONFIG_HEX + ".json"
    manifest = [{"Config": path, "RepoTags": ["fixture:one"], "Layers": []}]
    if multiple:
        manifest.append(manifest[0])
    entries = [] if missing else [(path, CONFIG + (b" " if corrupt else b""))]
    entries.append(("manifest.json", json.dumps(manifest).encode()))
    if duplicate:
        entries.append(entries[-1])
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, body in entries:
            member = tarfile.TarInfo(name)
            member.size = len(body)
            archive.addfile(member, io.BytesIO(body))
    return output.getvalue()


def function(name):
    source = (STAGING / "verify.sh").read_text()
    start = source.index(name + "() {")
    end = source.index("\n}\n", start) + 3
    return source[start:end]


class ConfigBytesTests(unittest.TestCase):
    def test_classic_and_oci_exports_hash_identical_original_bytes(self):
        for oci in (False, True):
            with self.subTest(oci=oci):
                self.assertEqual(MODULE.config_digest(io.BytesIO(archive_bytes(oci))), CONFIG_DIGEST)

    def test_corrupt_missing_ambiguous_and_duplicate_metadata_fail_closed(self):
        for option in ("corrupt", "missing", "multiple", "duplicate"):
            with self.subTest(option=option), self.assertRaises(ValueError):
                MODULE.config_digest(io.BytesIO(archive_bytes(**{option: True})))

    def test_malformed_archive_emits_only_safe_diagnostic(self):
        result = subprocess.run(
            ["python3", str(STAGING / "image_config_digest.py")],
            input=b"synthetic-private-marker invalid tar", capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr, b"P8_IMAGE_CONFIG_FAIL invalid-image-archive\n")


class VerifyImageTests(unittest.TestCase):
    def verify(self, backend="classic", failure=""):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "image.tar").write_bytes(archive_bytes(oci=backend == "containerd"))
            (root / "bundle").mkdir()
            (root / "bundle/image_config_digest.py").write_bytes((STAGING / "image_config_digest.py").read_bytes())
            fake = root / "docker"
            fake.write_text("""#!/usr/bin/env python3
import os, sys
from pathlib import Path
args = sys.argv[1:]
failure = os.environ['FAILURE']
if args[:2] == ['image', 'save']:
    sys.stdout.buffer.write(Path(os.environ['ARCHIVE']).read_bytes())
    sys.exit(1 if failure == 'save' else 0)
elif args[:2] == ['inspect', '--format']:
    print('sha256:' + 'b' * 64 if failure == 'running' else os.environ['LOCAL_ID'])
elif args[:2] == ['image', 'inspect'] and args[3] == '{{.Id}}':
    print(os.environ['LOCAL_ID'])
elif args[:2] == ['image', 'inspect'] and 'RepoDigests' in args[3]:
    print('registry.example:5000/other@' + os.environ['REGISTRY_DIGEST'] if failure == 'repo' else os.environ['CANONICAL'])
else:
    sys.exit(2)
""")
            fake.chmod(0o755)
            return subprocess.run(
                ["bash", "-c", "set -euo pipefail\n" + function("fail_check") + function("verify_image")
                 + '\nfake_compose() { echo container-id; }\ncompose=(fake_compose)\n'
                 + 'verify_image market-api "$EXACT_REF" "$EXPECTED_CONFIG"\necho VERIFIED'],
                env={**os.environ, "PATH": str(root) + ":" + os.environ["PATH"],
                     "INSTALL_ROOT": str(root), "ARCHIVE": str(root / "image.tar"),
                     "FAILURE": failure, "EXACT_REF": REF, "CANONICAL": CANONICAL,
                     "REGISTRY_DIGEST": REGISTRY_DIGEST,
                     "LOCAL_ID": CONFIG_DIGEST if backend == "classic" else REGISTRY_DIGEST,
                     "EXPECTED_CONFIG": REGISTRY_DIGEST if failure == "config" else CONFIG_DIGEST},
                capture_output=True, text=True,
            )

    def test_both_backend_ids_verify_same_config_bytes(self):
        for backend in ("classic", "containerd"):
            with self.subTest(backend=backend):
                result = self.verify(backend)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "VERIFIED\n")

    def test_identity_mismatches_and_export_failure_stop_verification(self):
        checks = {"running": "image-running-id-mismatch", "repo": "canonical-repodigest-mismatch",
                  "config": "local-image-config-mismatch", "save": "image-config-unreadable"}
        for backend in ("classic", "containerd"):
            for failure, check in checks.items():
                with self.subTest(backend=backend, failure=failure):
                    result = self.verify(backend, failure)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn("VERIFIED", result.stdout)
                    self.assertIn("P8_VERIFY_FAIL check=" + check, result.stderr)


@unittest.skipUnless(os.environ.get("P8_IMAGE_STORE_INTEGRATION") == "1", "requires isolated CI Docker registry")
class RealImageStoreTests(unittest.TestCase):
    def test_pushed_pulled_image_and_wrong_config_with_real_engine(self):
        ref = os.environ["P8_TEST_IMAGE_REF"]
        expected = os.environ["P8_TEST_CONFIG_DIGEST"]
        local_id = subprocess.check_output(["docker", "image", "inspect", "--format", "{{.Id}}", ref], text=True).strip()
        expected_id = ref.split("@", 1)[1] if os.environ["P8_CONTAINERD"] == "true" else expected
        self.assertEqual(local_id, expected_id, "test must exercise the selected backend ID semantics")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bundle").mkdir()
            (root / "bundle/image_config_digest.py").write_bytes((STAGING / "image_config_digest.py").read_bytes())
            script = "set -euo pipefail\n" + function("fail_check") + function("verify_image")
            script += '\nfake_compose() { echo "$P8_TEST_CONTAINER"; }\ncompose=(fake_compose)\n'
            script += 'verify_image fixture "$P8_TEST_IMAGE_REF" "$TEST_EXPECTED"\necho VERIFIED'
            for digest, success in ((expected, True), ("sha256:" + "0" * 64, False)):
                result = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                                        env={**os.environ, "INSTALL_ROOT": str(root), "TEST_EXPECTED": digest})
                self.assertEqual(result.returncode == 0, success, result.stderr)
                if success:
                    self.assertIn("VERIFIED", result.stdout)
                else:
                    self.assertIn("local-image-config-mismatch", result.stderr)


if __name__ == "__main__":
    unittest.main()

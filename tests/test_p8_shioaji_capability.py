"""Regressions for offline capability provenance and the exact-image boundary."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import re
import subprocess
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("p8_shioaji_capability", ROOT / "deploy/staging/shioaji_capability.py")
capability = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(capability)


class ShioajiExactImageTests(unittest.TestCase):
    def inspection(self, **changes):
        document = {"Id": "sha256:" + "a" * 64,
                    "Config": {"User": "10001:10001", "Labels": dict(capability.EXPECTED_LABELS)}}
        document.update(changes)
        return subprocess.CompletedProcess([], 0, json.dumps([document]), "")

    def test_runs_inspected_content_id_with_all_security_constraints(self):
        with patch.object(capability.subprocess, "run", side_effect=[self.inspection(), None]) as run:
            result = capability.verify_image("runtime-a:fixture")
        command = run.call_args_list[1].args[0]
        for flag, value in (("--network", "none"), ("--cap-drop", "ALL"),
                            ("--security-opt", "no-new-privileges"), ("--entrypoint", "python")):
            self.assertEqual(command[command.index(flag) + 1], value)
        self.assertIn("--read-only", command)
        self.assertIn("uid=10001,gid=10001", command[command.index("--tmpfs") + 1])
        self.assertIn("readonly", command[command.index("--mount") + 1])
        self.assertIn(result["image_id"], command)
        self.assertNotIn("runtime-a:fixture", command)
        self.assertTrue(run.call_args.kwargs["check"])

    def test_missing_or_wrong_labels_and_root_user_fail_before_probe(self):
        for labels, user in (({}, "10001:10001"),
                             ({**capability.EXPECTED_LABELS, "io.tw-quant.shioaji.version": "1.7.3"}, "10001:10001"),
                             (capability.EXPECTED_LABELS, "0:0")):
            with self.subTest(labels=labels, user=user), patch.object(
                capability.subprocess, "run", return_value=self.inspection(Config={"User": user, "Labels": labels})
            ) as run:
                with self.assertRaises(ValueError):
                    capability.verify_image("runtime-b:fixture")
                self.assertEqual(run.call_count, 1)

    def test_failed_exact_image_probe_cannot_pass(self):
        with patch.object(capability.subprocess, "run", side_effect=[
            self.inspection(), subprocess.CalledProcessError(1, ["docker", "run"])
        ]):
            with self.assertRaises(subprocess.CalledProcessError):
                capability.verify_image("runtime-a:fixture")

    def test_wrong_installed_version_fails_before_sdk_import(self):
        with patch.object(capability, "version", return_value="1.7.3"), patch.object(capability.importlib, "import_module") as load:
            with self.assertRaises(ValueError):
                capability.probe()
            load.assert_not_called()

    def test_missing_installed_sdk_fails_closed(self):
        with patch.object(capability, "version", return_value="1.7.4"), patch.object(
            capability.importlib, "import_module", side_effect=ImportError("missing sdk")
        ):
            with self.assertRaises(ImportError):
                capability.probe()

    def test_candidate_checks_both_runtime_images_before_scan_and_push(self):
        workflow = (ROOT / ".github/workflows/staging-candidate.yml").read_text()
        start = workflow.index("      - name: Verify offline Shioaji capability")
        end = workflow.index("\n      - name:", start + 1)
        self.assertIn('for image in "$RUNTIME_A_TAG" "$RUNTIME_B_TAG"; do', workflow[start:end])
        self.assertIn('shioaji_capability.py image --image "$image"', workflow[start:end])
        self.assertLess(end, workflow.index("      - name: Scan runtime A"))
        self.assertLess(end, workflow.index("docker push"))
        self.assertIsNone(re.search(r"\bdocker build\s", workflow[end:]))

    def test_locked_sdk_install_and_labels_do_not_enable_execution(self):
        dockerfile = (ROOT / "deploy/staging/Dockerfile.runtime").read_text()
        self.assertIn("uv sync --locked --no-dev --extra server --extra shioaji --no-editable", dockerfile)
        for name, value in capability.EXPECTED_LABELS.items():
            self.assertIn(f'{name}="{value}"', dockerfile)
        for forbidden in ("LIVE_TRADING_ENABLED=true", "LIVE_CANARY_ENABLED=true", "BROKER_PROVIDER=shioaji"):
            self.assertNotIn(forbidden, dockerfile)
        lock = (ROOT / "uv.lock").read_text()
        self.assertIn('name = "shioaji"\nversion = "1.7.4"', lock)


if __name__ == "__main__":
    unittest.main()

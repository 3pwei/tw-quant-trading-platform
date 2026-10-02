from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy/lightsail/verify-workflow-gates.py"
SPEC = importlib.util.spec_from_file_location("workflow_gate_verifier", SCRIPT)
assert SPEC and SPEC.loader
verifier = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = verifier
SPEC.loader.exec_module(verifier)


SHA = "a" * 40


def run(
    name: str,
    status: str,
    conclusion: str | None,
    *,
    sha: str = SHA,
    event: str = "push",
    branch: str = "master",
    number: int = 1,
    attempt: int = 1,
) -> dict[str, object]:
    return {
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "head_sha": sha,
        "event": event,
        "head_branch": branch,
        "run_number": number,
        "run_attempt": attempt,
    }


class DeploymentWorkflowGateTests(unittest.TestCase):
    def test_ci_success_cannot_mask_pending_security(self) -> None:
        states = verifier.evaluate_runs(
            {
                "workflow_runs": [
                    run("CI", "completed", "success"),
                    run("Security", "in_progress", None),
                ]
            },
            SHA,
        )

        self.assertTrue(states["CI"].successful)
        self.assertFalse(states["Security"].successful)
        self.assertFalse(
            all(states.get(name) and states[name].successful for name in verifier.REQUIRED_WORKFLOWS)
        )

    def test_both_exact_revision_push_gates_must_succeed(self) -> None:
        states = verifier.evaluate_runs(
            {
                "workflow_runs": [
                    run("CI", "completed", "success"),
                    run("Security", "completed", "success"),
                    run("CI", "completed", "success", sha="b" * 40, number=99),
                    run("Security", "completed", "success", event="schedule", number=99),
                ]
            },
            SHA,
        )

        self.assertEqual(set(states), {"CI", "Security"})
        self.assertTrue(all(state.successful for state in states.values()))

    def test_latest_failed_attempt_blocks_deployment(self) -> None:
        states = verifier.evaluate_runs(
            {
                "workflow_runs": [
                    run("CI", "completed", "success", number=4),
                    run("Security", "completed", "success", number=4, attempt=1),
                    run("Security", "completed", "failure", number=4, attempt=2),
                ]
            },
            SHA,
        )

        self.assertTrue(states["Security"].terminal_failure)
        self.assertFalse(states["Security"].successful)

    def test_diagnostics_expose_only_gate_state(self) -> None:
        output = verifier.sanitized_state(
            2,
            {
                "CI": verifier.GateState("CI", "completed", "success"),
            },
        )
        parsed = json.loads(output)

        self.assertEqual(parsed["attempt"], 2)
        self.assertEqual(parsed["gates"]["Security"]["status"], "missing")
        self.assertNotIn("sha", output.lower())
        self.assertNotIn("token", output.lower())
        self.assertNotIn("url", output.lower())

    def test_workflow_is_manual_only_and_checks_both_gates_for_every_revision(self) -> None:
        workflow = (ROOT / ".github/workflows/deploy-lightsail.yml").read_text()

        triggers = workflow.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
        events = [line.strip().rstrip(":") for line in triggers.splitlines()
                  if line.startswith("  ") and not line.startswith("   ")]
        self.assertEqual(events, ["workflow_dispatch"])
        self.assertNotIn("workflow_run", workflow)
        self.assertNotIn("${REQUESTED_SHA:-", workflow)
        self.assertIn("github.ref == 'refs/heads/master'", workflow)
        self.assertIn("needs.verify.result == 'success'", workflow)
        self.assertIn("actions: read", workflow)
        self.assertIn("Verify CI and Security gates for revision", workflow)
        self.assertIn("verify-workflow-gates.py", workflow)
        gate = workflow.split("      - name: Verify CI and Security gates for revision", 1)[1]
        gate = gate.split("      - uses:", 1)[0]
        self.assertNotIn("if:", gate)
        self.assertIn("GATE_SHA: ${{ steps.revision.outputs.sha }}", gate)
        self.assertIn('git merge-base --is-ancestor "$sha" master', workflow)
        self.assertIn("    needs: verify", workflow)

    def test_missing_or_unsuccessful_gate_cannot_authorize_a_revision(self) -> None:
        for name in verifier.REQUIRED_WORKFLOWS:
            for status, conclusion in [("in_progress", None), ("completed", "failure"),
                                       ("completed", "cancelled"), ("completed", "skipped")]:
                with self.subTest(name=name, status=status, conclusion=conclusion):
                    states = verifier.evaluate_runs({"workflow_runs": [
                        run(other, status if other == name else "completed",
                            conclusion if other == name else "success")
                        for other in verifier.REQUIRED_WORKFLOWS
                    ]}, SHA)
                    self.assertFalse(all(states[key].successful for key in verifier.REQUIRED_WORKFLOWS))
            states = verifier.evaluate_runs({"workflow_runs": [
                run(other, "completed", "success")
                for other in verifier.REQUIRED_WORKFLOWS if other != name
            ]}, SHA)
            self.assertNotIn(name, states)


class ManualProductionRequestTests(unittest.TestCase):
    """Execute the actual workflow guard without checkout, SSH, or GitHub access."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = (ROOT / ".github/workflows/deploy-lightsail.yml").read_text()
        cls.guard = cls.workflow.split("      - name: Validate manual Production request\n", 1)[1]
        cls.guard = textwrap.dedent(cls.guard.split("        run: |\n", 1)[1].split("      - uses:", 1)[0])

    def request(self, **overrides: str) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            env = {**os.environ, "REQUEST_EVENT": "workflow_dispatch",
                   "WORKFLOW_REF": "refs/heads/master", "REQUESTED_SHA": SHA,
                   "PRODUCTION_CONFIRMATION": f"DEPLOY lightsail-production {SHA}",
                   **overrides, "GITHUB_OUTPUT": str(output)}
            result = subprocess.run(["bash", "-euo", "pipefail", "-c", self.guard],
                                    env=env, capture_output=True, text=True)
            return result.returncode, output.read_text() if output.exists() else ""

    def test_valid_manual_confirmation_emits_only_exact_sha(self) -> None:
        self.assertEqual(self.request(), (0, f"sha={SHA}\n"))

    def test_automatic_events_and_non_master_dispatch_fail_closed(self) -> None:
        for event in ["push", "workflow_run", "pull_request", "schedule", ""]:
            with self.subTest(event=event):
                status, output = self.request(REQUEST_EVENT=event)
                self.assertNotEqual(status, 0)
                self.assertEqual(output, "")
        status, output = self.request(WORKFLOW_REF="refs/heads/feature/candidate")
        self.assertNotEqual(status, 0)
        self.assertEqual(output, "")

    def test_empty_short_uppercase_ref_and_injected_sha_fail_closed(self) -> None:
        for sha in ["", "a" * 39, "a" * 41, "A" * 40, "master", "a" * 40 + "; exit 0", "a" * 40 + "\n"]:
            with self.subTest(sha=sha):
                status, output = self.request(REQUESTED_SHA=sha)
                self.assertNotEqual(status, 0)
                self.assertEqual(output, "")

    def test_confirmation_is_case_sensitive_and_bound_to_exact_revision(self) -> None:
        for confirmation in ["", "true", "DEPLOY", f"deploy lightsail-production {SHA}",
                             f"DEPLOY lightsail-production {'b' * 40}",
                             f"DEPLOY lightsail-production {SHA} ",
                             f"DEPLOY lightsail-production {SHA}\n"]:
            with self.subTest(confirmation=confirmation):
                status, output = self.request(PRODUCTION_CONFIRMATION=confirmation)
                self.assertNotEqual(status, 0)
                self.assertEqual(output, "")

    def test_guard_precedes_checkout_and_secret_access_and_inputs_are_required(self) -> None:
        guard_start = self.workflow.index("      - name: Validate manual Production request")
        self.assertLess(guard_start, self.workflow.index("      - uses: actions/checkout"))
        self.assertLess(guard_start, self.workflow.index("secrets."))
        inputs = self.workflow.split("    inputs:\n", 1)[1].split("\npermissions:", 1)[0]
        self.assertEqual(inputs.count("required: true"), 2)
        self.assertIn("production_confirmation:", inputs)
        self.assertIn("REQUESTED_SHA: ${{ steps.request.outputs.sha }}", self.workflow)


if __name__ == "__main__":
    unittest.main()

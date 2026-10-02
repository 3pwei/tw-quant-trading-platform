from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy/lightsail/verify-public-origin.py"
SPEC = importlib.util.spec_from_file_location("public_origin_verifier", SCRIPT)
assert SPEC and SPEC.loader
verifier = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = verifier
SPEC.loader.exec_module(verifier)


class PublicOriginVerificationTests(unittest.TestCase):
    def test_public_target_cannot_be_internal_or_ambiguous(self) -> None:
        for target in (
            "http://public.example.com",
            "https://localhost",
            "https://127.0.0.1",
            "https://" + ".".join(("10", "0", "0", "1")),
            "https://public.example.com/not-health",
            "https://" + "user" + ":" + "secret" + "@public.example.com",
        ):
            with self.subTest(target=target), self.assertRaises(verifier.VerificationError):
                verifier.validate_public_base_url(target)

    def test_internal_200_does_not_mask_external_502(self) -> None:
        internal = verifier.ProbeResult(200, "ok", {}, 0)
        external = verifier.ProbeResult(502, "bad gateway", {"server": "cloudflare", "cf-ray": "redacted"}, 0)
        self.assertEqual(internal.status, 200)
        with self.assertRaises(verifier.VerificationError) as caught:
            verifier.validate_result(external)
        self.assertEqual(caught.exception.layer, "edge_proxy")

    def test_gateway_502_is_distinguished_from_edge_502(self) -> None:
        self.assertEqual(verifier.classify(502, {"server": "cloudflare"}), "edge_proxy")
        self.assertEqual(verifier.classify(502, {"server": "Caddy"}), "gateway_or_origin")

    def test_required_security_headers_and_exact_body(self) -> None:
        headers = {name: expected or "policy" for name, expected in verifier.REQUIRED_HEADERS.items()}
        verifier.validate_result(verifier.ProbeResult(200, "ok", headers, 0))
        with self.assertRaises(verifier.VerificationError):
            verifier.validate_result(verifier.ProbeResult(200, "OK", headers, 0))

    def test_diagnostics_are_allowlisted(self) -> None:
        event = verifier.sanitized_event(
            layer="external_public", reason="failed", status=502,
            url="https://origin.internal", token="secret", address="192.0.2.1",
        )
        decoded = json.loads(event)
        self.assertEqual(decoded, {"layer": "external_public", "reason": "failed", "status": 502})

    def test_workflow_uses_distinct_targets_and_rolls_back(self) -> None:
        workflow = (ROOT / ".github/workflows/deploy-lightsail.yml").read_text()
        gateway = (ROOT / "deploy/lightsail/verify-gateway-path.sh").read_text()
        self.assertIn("python deploy/lightsail/verify-public-origin.py", workflow)
        self.assertIn('--base-url "$PUBLIC_DASHBOARD_URL"', workflow)
        self.assertNotIn("continue-on-error", workflow)
        self.assertIn("Rollback after deployment failure", workflow)
        self.assertIn("if: always()", workflow)
        self.assertIn("steps.public_origin.conclusion == 'failure'", workflow)
        self.assertIn("${{ steps.known_good.outputs.sha }}", workflow)
        self.assertIn('--resolve "${domain}:443:127.0.0.1"', gateway)
        self.assertNotIn("127.0.0.1", workflow.split("Verify formal public user path", 1)[1].split("Rollback after", 1)[0])


if __name__ == "__main__":
    unittest.main()

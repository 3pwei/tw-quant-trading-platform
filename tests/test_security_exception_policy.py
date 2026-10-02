from __future__ import annotations

from datetime import date
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "deploy/security/exception_policy.py"
SPEC = importlib.util.spec_from_file_location("security_exception_policy", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
POLICY = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = POLICY
SPEC.loader.exec_module(POLICY)


class SecurityExceptionPolicyTests(unittest.TestCase):
    def exception(self, expiry: str = "2026-10-18"):
        return POLICY.VulnerabilityException(
            "CVE-2099-0001", date.fromisoformat(expiry), 2
        )

    def test_warning_windows_and_expiry_are_fail_closed(self) -> None:
        cases = (
            ("2026-10-03", "ok", False, False),
            ("2026-10-04", "warning", True, False),
            ("2026-10-11", "high", True, False),
            ("2026-10-15", "urgent", True, False),
            ("2026-10-18", "expired", True, True),
            ("2026-10-19", "expired", True, True),
        )
        for as_of, expected, notification, expired in cases:
            with self.subTest(as_of=as_of):
                report = POLICY.build_report(
                    [self.exception()], date.fromisoformat(as_of)
                )
                self.assertEqual(report["entries"][0]["urgency"], expected)
                self.assertEqual(report["notification_required"], notification)
                self.assertEqual(report["expired"], expired)

    def test_repository_ignore_file_is_well_formed_and_time_bounded(self) -> None:
        exceptions = POLICY.load_exceptions(ROOT / ".trivyignore.yaml")
        self.assertEqual(len(exceptions), 21)
        self.assertTrue(all(item.expired_at == date(2026, 10, 18) for item in exceptions))

    def test_malformed_or_duplicate_entries_are_rejected(self) -> None:
        fixtures = (
            "vulnerabilities:\n  - id: CVE-1\n    statement: reason\n",
            "vulnerabilities:\n  - id: CVE-1\n    expired_at: 2026-10-18\n",
            (
                "vulnerabilities:\n"
                "  - id: CVE-1\n    statement: reason\n    expired_at: 2026-10-18\n"
                "  - id: CVE-1\n    statement: reason\n    expired_at: 2026-10-19\n"
            ),
        )
        for content in fixtures:
            with self.subTest(content=content):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "ignore.yaml"
                    path.write_text(content, encoding="utf-8")
                    with self.assertRaises(ValueError):
                        POLICY.load_exceptions(path)

    def test_tracking_issue_uses_marker_and_repository_owner_mention(self) -> None:
        report = POLICY.build_report(
            [self.exception()], date.fromisoformat("2026-10-15")
        )
        body = POLICY.render_issue_body(report, "3pwei/example")
        self.assertIn(POLICY.ISSUE_MARKER, body)
        self.assertIn("Owner: @3pwei", body)
        self.assertIn("CVE-2099-0001", body)
        issues = [
            {"number": 1, "body": "unrelated"},
            {"number": 2, "body": body},
        ]
        self.assertEqual(POLICY.select_tracking_issue(issues)["number"], 2)

    def test_tracking_issue_is_not_updated_twice_in_the_same_state(self) -> None:
        report = POLICY.build_report(
            [self.exception()], date.fromisoformat("2026-10-04")
        )
        existing = {
            "number": 7,
            "body": POLICY.render_issue_body(report, "3pwei/example"),
        }
        calls = []
        original = POLICY.github_request

        def fake_request(method, url, token, payload=None):
            calls.append((method, url, payload))
            return [existing] if method == "GET" else None

        POLICY.github_request = fake_request
        try:
            result = POLICY.sync_tracking_issue(report, "3pwei/example", "token")
        finally:
            POLICY.github_request = original

        self.assertEqual(result, "unchanged")
        self.assertEqual([call[0] for call in calls], ["GET"])

    def test_tracking_issue_updates_when_urgency_band_changes(self) -> None:
        warning = POLICY.build_report(
            [self.exception()], date.fromisoformat("2026-10-04")
        )
        high = POLICY.build_report(
            [self.exception()], date.fromisoformat("2026-10-11")
        )
        existing = {
            "number": 7,
            "body": POLICY.render_issue_body(warning, "3pwei/example"),
        }
        calls = []
        original = POLICY.github_request

        def fake_request(method, url, token, payload=None):
            calls.append((method, url, payload))
            return [existing] if method == "GET" else None

        POLICY.github_request = fake_request
        try:
            result = POLICY.sync_tracking_issue(high, "3pwei/example", "token")
        finally:
            POLICY.github_request = original

        self.assertEqual(result, "updated")
        self.assertEqual([call[0] for call in calls], ["GET", "POST", "PATCH"])
        self.assertTrue(calls[1][1].endswith("/issues/7/comments"))
        self.assertIn("@3pwei", calls[1][2]["body"])

    def test_tracking_issue_rejects_untrusted_repository_path(self) -> None:
        report = POLICY.build_report(
            [self.exception()], date.fromisoformat("2026-10-04")
        )
        with self.assertRaisesRegex(ValueError, "owner/name"):
            POLICY.sync_tracking_issue(report, "../other/path", "token")



if __name__ == "__main__":
    unittest.main()

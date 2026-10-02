#!/usr/bin/env python3
"""Validate expiring Trivy exceptions and maintain one tracking issue."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ISSUE_MARKER = "<!-- tw-quant-security-exception-expiry -->"
ISSUE_STATE_PREFIX = "<!-- tw-quant-security-exception-state:"
ISSUE_TITLE = "Security vulnerability exceptions require review"
_ID = re.compile(r"^\s*-\s+id:\s*['\"]?([^'\"\s]+)['\"]?\s*$")
_EXPIRY = re.compile(r"^\s+expired_at:\s*['\"]?(\d{4}-\d{2}-\d{2})['\"]?\s*$")
_STATEMENT = re.compile(r"^\s+statement:\s*(?:.*)$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


@dataclass(frozen=True)
class VulnerabilityException:
    advisory_id: str
    expired_at: date
    source_line: int


def load_exceptions(path: Path) -> list[VulnerabilityException]:
    """Read the deliberately small Trivy YAML contract without extra packages."""

    text = path.read_text(encoding="utf-8")
    if not re.search(r"(?m)^vulnerabilities:\s*$", text):
        raise ValueError("ignore file must contain a vulnerabilities section")

    parsed: list[VulnerabilityException] = []
    current_id: str | None = None
    current_expiry: date | None = None
    current_line = 0
    has_statement = False

    def finish() -> None:
        nonlocal current_id, current_expiry, current_line, has_statement
        if current_id is None:
            return
        if current_expiry is None:
            raise ValueError(f"{current_id} is missing expired_at")
        if not has_statement:
            raise ValueError(f"{current_id} is missing statement")
        parsed.append(VulnerabilityException(current_id, current_expiry, current_line))

    for line_number, line in enumerate(text.splitlines(), start=1):
        id_match = _ID.match(line)
        if id_match:
            finish()
            current_id = id_match.group(1)
            current_expiry = None
            current_line = line_number
            has_statement = False
            continue
        if current_id is None:
            continue
        expiry_match = _EXPIRY.match(line)
        if expiry_match:
            try:
                current_expiry = date.fromisoformat(expiry_match.group(1))
            except ValueError as exc:
                raise ValueError(
                    f"{current_id} has invalid expired_at at line {line_number}"
                ) from exc
        elif _STATEMENT.match(line):
            has_statement = True
    finish()

    seen: set[str] = set()
    duplicates: list[str] = []
    for item in parsed:
        if item.advisory_id in seen:
            duplicates.append(item.advisory_id)
        seen.add(item.advisory_id)
    if duplicates:
        raise ValueError(
            "duplicate vulnerability exception IDs: " + ", ".join(sorted(set(duplicates)))
        )
    return parsed


def urgency(days_remaining: int) -> str:
    if days_remaining <= 0:
        return "expired"
    if days_remaining <= 3:
        return "urgent"
    if days_remaining <= 7:
        return "high"
    if days_remaining <= 14:
        return "warning"
    return "ok"


def build_report(
    exceptions: list[VulnerabilityException], as_of: date
) -> dict[str, Any]:
    entries = []
    for item in sorted(exceptions, key=lambda value: (value.expired_at, value.advisory_id)):
        days_remaining = (item.expired_at - as_of).days
        entries.append(
            {
                "id": item.advisory_id,
                "expired_at": item.expired_at.isoformat(),
                "days_remaining": days_remaining,
                "urgency": urgency(days_remaining),
                "source_line": item.source_line,
            }
        )
    actionable = [item for item in entries if item["urgency"] != "ok"]
    expired = [item for item in entries if item["urgency"] == "expired"]
    return {
        "as_of": as_of.isoformat(),
        "total": len(entries),
        "actionable_count": len(actionable),
        "expired_count": len(expired),
        "notification_required": bool(actionable),
        "expired": bool(expired),
        "next_expiry": min((item["expired_at"] for item in entries), default=None),
        "entries": entries,
    }


def render_summary(report: dict[str, Any]) -> str:
    lines = [
        "# Vulnerability exception expiry policy",
        "",
        f"- Evaluation date: `{report['as_of']}`",
        f"- Tracked exceptions: `{report['total']}`",
        f"- Requiring attention: `{report['actionable_count']}`",
        f"- Expired: `{report['expired_count']}`",
        f"- Next expiry: `{report['next_expiry'] or 'none'}`",
    ]
    actionable = [
        item for item in report["entries"] if item["urgency"] != "ok"
    ]
    if actionable:
        lines.extend(
            [
                "",
                "| Advisory | Expiry | Days remaining | Status |",
                "|---|---:|---:|---|",
            ]
        )
        for item in actionable:
            lines.append(
                f"| `{item['id']}` | {item['expired_at']} | "
                f"{item['days_remaining']} | {item['urgency']} |"
            )
    return "\n".join(lines) + "\n"


def render_issue_body(report: dict[str, Any], repository: str) -> str:
    owner = repository.split("/", 1)[0]
    return "\n".join(
        [
            ISSUE_MARKER,
            issue_state_marker(report),
            f"Owner: @{owner}",
            "",
            "The automated expiry policy found vulnerability exceptions that "
            "require human review.",
            "",
            render_summary(report).rstrip(),
            "",
            "Required action: refresh the affected image, remove resolved entries, "
            "or submit a separately reviewed time-bounded renewal with current "
            "exploitability evidence. Do not extend dates automatically.",
            "",
            "This issue is updated in place to avoid duplicate notifications.",
        ]
    ) + "\n"


def issue_state_marker(report: dict[str, Any]) -> str:
    actionable = [
        {
            "id": item["id"],
            "expired_at": item["expired_at"],
            "urgency": item["urgency"],
        }
        for item in report["entries"]
        if item["urgency"] != "ok"
    ]
    digest = hashlib.sha256(
        json.dumps(actionable, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    return f"{ISSUE_STATE_PREFIX}{digest} -->"


def select_tracking_issue(issues: list[dict[str, Any]]) -> dict[str, Any] | None:
    for issue in issues:
        if ISSUE_MARKER in (issue.get("body") or ""):
            return issue
    return None


def render_notification_comment(report: dict[str, Any], repository: str) -> str:
    owner = repository.split("/", 1)[0]
    states = {item["urgency"] for item in report["entries"]}
    state = next(
        value for value in ("expired", "urgent", "high", "warning") if value in states
    )
    return (
        f"@{owner} security exception status changed to **{state}**. "
        f"{report['actionable_count']} exception(s) require review; "
        f"next expiry: `{report['next_expiry']}`. The issue body has the current matrix."
    )


def github_request(
    method: str, url: str, token: str, payload: dict[str, Any] | None = None
) -> Any:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        url,
        data=data,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "tw-quant-security-exception-policy",
        },
    )
    try:
        with urlopen(request, timeout=20) as response:
            body = response.read()
    except HTTPError as exc:
        # The response body can contain request-specific data. Keep logs bounded.
        raise RuntimeError(f"GitHub API request failed with status {exc.code}") from exc
    return json.loads(body) if body else None


def sync_tracking_issue(report: dict[str, Any], repository: str, token: str) -> str:
    if not _REPOSITORY.fullmatch(repository):
        raise ValueError("repository must use the owner/name form")
    query = urlencode({"state": "open", "per_page": 100})
    base = f"https://api.github.com/repos/{repository}/issues"
    issues = github_request("GET", f"{base}?{query}", token)
    existing = select_tracking_issue(issues)

    if report["notification_required"]:
        payload = {"title": ISSUE_TITLE, "body": render_issue_body(report, repository)}
        if existing is None:
            github_request("POST", base, token, payload)
            return "created"
        if issue_state_marker(report) in (existing.get("body") or ""):
            return "unchanged"
        github_request(
            "POST",
            f"{base}/{existing['number']}/comments",
            token,
            {"body": render_notification_comment(report, repository)},
        )
        github_request("PATCH", f"{base}/{existing['number']}", token, payload)
        return "updated"

    if existing is not None:
        body = (existing.get("body") or "") + (
            f"\nResolved automatically on {report['as_of']}: no exception is "
            "within the 14-day notification window.\n"
        )
        github_request(
            "PATCH",
            f"{base}/{existing['number']}",
            token,
            {"state": "closed", "body": body},
        )
        return "closed"
    return "unchanged"


def _read_report(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_summary(path: Path | None, summary: str) -> None:
    if path is not None:
        path.write_text(summary, encoding="utf-8")
    github_summary = os.getenv("GITHUB_STEP_SUMMARY")
    if github_summary:
        with Path(github_summary).open("a", encoding="utf-8") as stream:
            stream.write(summary)


def check_command(args: argparse.Namespace) -> int:
    as_of = date.fromisoformat(args.as_of) if args.as_of else date.today()
    report = build_report(load_exceptions(args.ignore_file), as_of)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    summary = render_summary(report)
    _write_summary(args.summary, summary)
    print(summary, end="")
    if report["expired"]:
        print("::error title=Expired vulnerability exceptions::Review or remove expired exceptions")
    elif report["notification_required"]:
        print("::warning title=Vulnerability exceptions expire soon::Human review is required")
    return 1 if args.fail_on_expired and report["expired"] else 0


def enforce_command(args: argparse.Namespace) -> int:
    report = _read_report(args.report)
    if report["expired"]:
        print("Expired vulnerability exceptions block the Security gate", file=sys.stderr)
        return 1
    return 0


def sync_command(args: argparse.Namespace) -> int:
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("GITHUB_TOKEN is required to synchronize the tracking issue")
    result = sync_tracking_issue(_read_report(args.report), args.repository, token)
    print(f"Security exception tracking issue: {result}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)

    check = commands.add_parser("check")
    check.add_argument("--ignore-file", type=Path, default=Path(".trivyignore.yaml"))
    check.add_argument("--as-of", help="ISO date override used by regression tests")
    check.add_argument("--report", type=Path, required=True)
    check.add_argument("--summary", type=Path)
    check.add_argument("--fail-on-expired", action="store_true")
    check.set_defaults(handler=check_command)

    enforce = commands.add_parser("enforce")
    enforce.add_argument("--report", type=Path, required=True)
    enforce.set_defaults(handler=enforce_command)

    sync = commands.add_parser("sync-issue")
    sync.add_argument("--report", type=Path, required=True)
    sync.add_argument("--repository", required=True)
    sync.set_defaults(handler=sync_command)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return args.handler(args)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"security exception policy error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

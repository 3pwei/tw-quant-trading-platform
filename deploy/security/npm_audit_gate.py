#!/usr/bin/env python3
"""Fail-closed npm audit gate with exact, expiring build-tool exceptions."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date
import json
from pathlib import Path
import re
import sys
from typing import Any


_ADVISORY_URL = re.compile(r"^https://github\.com/advisories/(GHSA-[A-Za-z0-9-]+)$")
_SEVERITIES = {"high", "critical"}


@dataclass(frozen=True)
class AuditException:
    advisory_id: str
    package: str
    version: str
    severity: str
    dependency_scope: str
    affected_packages: frozenset[str]
    expired_at: date
    statement: str


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON: {path}") from exc


def load_exceptions(path: Path) -> list[AuditException]:
    document = _read_json(path)
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ValueError("unsupported npm audit exception schema")
    rows = document.get("exceptions")
    if not isinstance(rows, list):
        raise ValueError("exceptions must be a list")

    parsed: list[AuditException] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("each exception must be an object")
        required = {
            "id",
            "package",
            "version",
            "severity",
            "dependency_scope",
            "affected_packages",
            "expired_at",
            "statement",
        }
        if set(row) != required:
            raise ValueError("npm audit exception fields do not match the schema")
        advisory_id = row["id"]
        affected = row["affected_packages"]
        statement = row["statement"]
        if not isinstance(advisory_id, str) or not advisory_id.startswith("GHSA-"):
            raise ValueError("exception id must be a GHSA identifier")
        if not isinstance(affected, list) or not affected or not all(
            isinstance(item, str) and item for item in affected
        ):
            raise ValueError(f"{advisory_id} has invalid affected_packages")
        if len(set(affected)) != len(affected):
            raise ValueError(f"{advisory_id} has duplicate affected packages")
        if not isinstance(statement, str) or len(statement.strip()) < 40:
            raise ValueError(f"{advisory_id} requires a meaningful statement")
        try:
            expired_at = date.fromisoformat(row["expired_at"])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{advisory_id} has invalid expired_at") from exc
        if row["severity"] not in _SEVERITIES:
            raise ValueError(f"{advisory_id} has invalid severity")
        if row["dependency_scope"] != "dev":
            raise ValueError(f"{advisory_id} must remain a dev-only exception")
        parsed.append(
            AuditException(
                advisory_id=advisory_id,
                package=row["package"],
                version=row["version"],
                severity=row["severity"],
                dependency_scope=row["dependency_scope"],
                affected_packages=frozenset(affected),
                expired_at=expired_at,
                statement=statement,
            )
        )

    identifiers = [item.advisory_id for item in parsed]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("duplicate npm audit exception identifiers")
    return parsed


def validate_lock(exceptions: list[AuditException], lock_path: Path) -> None:
    lock = _read_json(lock_path)
    packages = lock.get("packages") if isinstance(lock, dict) else None
    if not isinstance(packages, dict):
        raise ValueError("package lock is missing packages")
    for item in exceptions:
        entry = packages.get(f"node_modules/{item.package}")
        if not isinstance(entry, dict):
            raise ValueError(f"{item.advisory_id} package is absent from the lock")
        if entry.get("version") != item.version:
            raise ValueError(f"{item.advisory_id} package version drift")
        if entry.get("dev") is not True:
            raise ValueError(f"{item.advisory_id} package is no longer dev-only")


def validate_expiry(
    exceptions: list[AuditException], as_of: date, fail_on_expired: bool
) -> None:
    expired = [item.advisory_id for item in exceptions if item.expired_at <= as_of]
    if expired and fail_on_expired:
        raise ValueError("expired npm audit exceptions: " + ", ".join(expired))


def _blocking_vulnerabilities(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if report.get("auditReportVersion") != 2:
        raise ValueError("unsupported or incomplete npm audit report")
    vulnerabilities = report.get("vulnerabilities")
    metadata = report.get("metadata")
    if not isinstance(vulnerabilities, dict) or not isinstance(metadata, dict):
        raise ValueError("npm audit report is missing required sections")
    return {
        name: value
        for name, value in vulnerabilities.items()
        if isinstance(value, dict) and value.get("severity") in _SEVERITIES
    }


def _root_advisories(
    vulnerabilities: dict[str, dict[str, Any]],
) -> dict[str, tuple[str, str]]:
    advisories: dict[str, tuple[str, str]] = {}
    for package, vulnerability in vulnerabilities.items():
        via = vulnerability.get("via")
        if not isinstance(via, list):
            raise ValueError(f"{package} has malformed npm audit provenance")
        for cause in via:
            if not isinstance(cause, dict):
                continue
            match = _ADVISORY_URL.fullmatch(str(cause.get("url", "")))
            if match is None:
                raise ValueError(f"{package} has an unrecognized advisory URL")
            advisory_id = match.group(1)
            value = (package, str(cause.get("severity", "")))
            if advisory_id in advisories and advisories[advisory_id] != value:
                raise ValueError(f"{advisory_id} has conflicting audit roots")
            advisories[advisory_id] = value
    return advisories


def enforce(
    report_path: Path,
    lock_path: Path,
    exception_path: Path,
    as_of: date,
) -> dict[str, int]:
    exceptions = load_exceptions(exception_path)
    validate_lock(exceptions, lock_path)
    validate_expiry(exceptions, as_of, fail_on_expired=True)

    report = _read_json(report_path)
    if not isinstance(report, dict):
        raise ValueError("npm audit report must be an object")
    vulnerabilities = _blocking_vulnerabilities(report)
    advisories = _root_advisories(vulnerabilities)
    configured = {item.advisory_id: item for item in exceptions}

    if set(advisories) != set(configured):
        unexpected = sorted(set(advisories) - set(configured))
        stale = sorted(set(configured) - set(advisories))
        details = []
        if unexpected:
            details.append("unexpected=" + ",".join(unexpected))
        if stale:
            details.append("stale=" + ",".join(stale))
        raise ValueError("npm advisory set mismatch: " + " ".join(details))

    blocking_names = frozenset(vulnerabilities)
    for advisory_id, (root_package, root_severity) in advisories.items():
        item = configured[advisory_id]
        if root_package != item.package or root_severity != item.severity:
            raise ValueError(f"{advisory_id} root identity drift")
        if blocking_names != item.affected_packages:
            raise ValueError(f"{advisory_id} affected package chain drift")

    metadata = report["metadata"].get("vulnerabilities", {})
    if not isinstance(metadata, dict):
        raise ValueError("npm audit vulnerability counts are missing")
    expected_high = sum(
        1 for value in vulnerabilities.values() if value.get("severity") == "high"
    )
    expected_critical = sum(
        1 for value in vulnerabilities.values() if value.get("severity") == "critical"
    )
    if metadata.get("high") != expected_high or metadata.get("critical") != expected_critical:
        raise ValueError("npm audit vulnerability counts do not match the report")
    return {
        "high": expected_high,
        "critical": expected_critical,
        "exceptions": len(exceptions),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "enforce"):
        command = subparsers.add_parser(name)
        command.add_argument("--exceptions", type=Path, required=True)
        command.add_argument("--lock-file", type=Path, required=True)
        command.add_argument("--as-of")
        if name == "check":
            command.add_argument("--fail-on-expired", action="store_true")
        else:
            command.add_argument("--audit-report", type=Path, required=True)
    args = parser.parse_args()
    as_of = date.fromisoformat(args.as_of) if args.as_of else date.today()
    try:
        exceptions = load_exceptions(args.exceptions)
        validate_lock(exceptions, args.lock_file)
        if args.command == "check":
            validate_expiry(exceptions, as_of, args.fail_on_expired)
            print(f"npm audit exception policy: PASS ({len(exceptions)} tracked)")
        else:
            result = enforce(
                args.audit_report, args.lock_file, args.exceptions, as_of
            )
            print(
                "npm dependency audit: PASS; "
                f"high={result['high']} critical={result['critical']} "
                f"time-bounded-exceptions={result['exceptions']}"
            )
    except ValueError as exc:
        print(f"npm audit gate failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

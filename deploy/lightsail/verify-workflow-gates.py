#!/usr/bin/env python3
"""Require successful CI and Security runs for one Production revision."""

from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


REQUIRED_WORKFLOWS = ("CI", "Security")
TERMINAL_FAILURES = {
    "action_required",
    "cancelled",
    "failure",
    "neutral",
    "skipped",
    "stale",
    "startup_failure",
    "timed_out",
}


class GateError(RuntimeError):
    """A sanitized deployment-gate failure."""


@dataclass(frozen=True)
class GateState:
    name: str
    status: str
    conclusion: str | None

    @property
    def successful(self) -> bool:
        return self.status == "completed" and self.conclusion == "success"

    @property
    def terminal_failure(self) -> bool:
        return self.status == "completed" and self.conclusion in TERMINAL_FAILURES


def validate_inputs(repository: str, sha: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise GateError("invalid_repository")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise GateError("invalid_revision")


def evaluate_runs(payload: dict[str, Any], sha: str) -> dict[str, GateState]:
    """Return the newest push run for every required workflow and exact SHA."""
    matching: dict[str, dict[str, Any]] = {}
    for run in payload.get("workflow_runs", []):
        name = run.get("name")
        if (
            name not in REQUIRED_WORKFLOWS
            or run.get("head_sha") != sha
            or run.get("event") != "push"
            or run.get("head_branch") != "master"
        ):
            continue
        current = matching.get(name)
        candidate_key = (run.get("run_number", 0), run.get("run_attempt", 0))
        current_key = (
            (current.get("run_number", 0), current.get("run_attempt", 0))
            if current
            else (-1, -1)
        )
        if candidate_key >= current_key:
            matching[name] = run

    return {
        name: GateState(
            name=name,
            status=str(matching[name].get("status", "unknown")),
            conclusion=matching[name].get("conclusion"),
        )
        for name in REQUIRED_WORKFLOWS
        if name in matching
    }


def fetch_runs(repository: str, sha: str, token: str, timeout: float) -> dict[str, Any]:
    query = urllib.parse.urlencode(
        {"head_sha": sha, "event": "push", "per_page": "100"}
    )
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/actions/runs?{query}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "tw-quant-production-gate/1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise GateError("actions_api_unavailable") from exc


def sanitized_state(attempt: int, states: dict[str, GateState]) -> str:
    return json.dumps(
        {
            "attempt": attempt,
            "gates": {
                name: {
                    "status": states[name].status,
                    "conclusion": states[name].conclusion,
                }
                if name in states
                else {"status": "missing", "conclusion": None}
                for name in REQUIRED_WORKFLOWS
            },
        },
        sort_keys=True,
    )


def verify(
    repository: str,
    sha: str,
    token: str,
    attempts: int,
    timeout: float,
    retry_delay: float,
) -> None:
    validate_inputs(repository, sha)
    if not token:
        raise GateError("actions_token_missing")

    for attempt in range(1, attempts + 1):
        states = evaluate_runs(fetch_runs(repository, sha, token, timeout), sha)
        print(sanitized_state(attempt, states))
        if all(states.get(name) and states[name].successful for name in REQUIRED_WORKFLOWS):
            return
        if any(state.terminal_failure for state in states.values()):
            raise GateError("required_workflow_failed")
        if attempt < attempts:
            time.sleep(retry_delay)
    raise GateError("required_workflow_not_successful")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--attempts", type=int, default=30)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--retry-delay", type=float, default=10.0)
    args = parser.parse_args()
    if (
        args.attempts < 1
        or args.attempts > 60
        or args.timeout <= 0
        or args.timeout > 30
        or args.retry_delay < 0
        or args.retry_delay > 30
    ):
        parser.error("retry and timeout bounds exceeded")
    try:
        verify(
            args.repository,
            args.sha,
            os.environ.get("GITHUB_TOKEN", ""),
            args.attempts,
            args.timeout,
            args.retry_delay,
        )
    except GateError as exc:
        print(json.dumps({"gate": "deployment", "reason": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps({"gate": "deployment", "status": "passed"}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

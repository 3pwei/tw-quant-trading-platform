"""Report plan eligibility without representing skipped native scanning as passed."""
from __future__ import annotations

import json
import os
from pathlib import Path


def evaluate(repository: dict, private_enabled: str) -> tuple[bool, str]:
    private = repository.get("private")
    owner_type = repository.get("owner", {}).get("type")
    if not isinstance(private, bool) or owner_type not in {"User", "Organization"}:
        raise ValueError("Missing or invalid repository eligibility context")
    if not private:
        return True, "CodeQL native scanning: eligible public repository; analysis/upload required"
    if owner_type == "User":
        return False, "CodeQL native scanning: N/A — GitHub personal private repository is ineligible"
    if private_enabled == "true":
        return True, "CodeQL native scanning: organization entitlement attested; analysis/upload required"
    raise ValueError("Private organization requires verified Code Security entitlement and CODEQL_PRIVATE_ELIGIBLE=true")


def main() -> int:
    try:
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
        enabled, status = evaluate(event["repository"], os.environ.get("CODEQL_PRIVATE_ELIGIBLE", ""))
        with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
            output.write(f"enabled={str(enabled).lower()}\n")
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as summary:
            summary.write(status + "\n\nPython static security scan and JS/TS static security/lint remain required.\n")
        print(status)
        return 0
    except (KeyError, OSError, ValueError, TypeError, AttributeError):
        print("CodeQL eligibility: unresolved; verify repository context and organization entitlement")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

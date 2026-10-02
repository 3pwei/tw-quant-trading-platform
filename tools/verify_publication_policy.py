"""Fail-closed public publication policy; native CodeQL execution is a P6 gate."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import tomllib


P6_ACCEPTANCE = (
    "first CI", "first Security", "native CodeQL python", "native CodeQL javascript-typescript",
    "dependency audit", "Gitleaks", "Trivy", "SBOM",
)


def ensure(condition: bool, rule: str) -> None:
    if not condition:
        raise ValueError("Public publication policy: " + rule)


def job(text: str, name: str) -> str:
    match = re.search(r"(?ms)^  " + re.escape(name) + r":\n(.*?)(?=^  [a-z][\w-]*:\n|\Z)", text)
    ensure(match is not None, "required job missing: " + name)
    return match.group(1)


def check_codeql(text: str) -> None:
    block = job(text, "codeql")
    ensure(not re.search(r"(?m)^    (?:if|needs|continue-on-error):", block), "CodeQL must be unconditional")
    ensure("continue-on-error" not in block and "CODEQL_PRIVATE_ELIGIBLE" not in text and "codeql-eligibility:" not in text,
           "eligibility/skip cannot satisfy Public CodeQL")
    for token in ("actions: read", "contents: read", "security-events: write", "fail-fast: false",
                  "language: [python, javascript-typescript]", "languages: ${{ matrix.language }}",
                  "category: /language:${{ matrix.language }}", "output: codeql-results", "upload: true",
                  "if-no-files-found: error", "--input codeql-results", "--security-threshold high", "--quality-threshold error"):
        ensure(token in block, "CodeQL contract missing: " + token)
    ensure(block.count("github/codeql-action/init@") == 1 and block.count("github/codeql-action/analyze@") == 1,
           "native analyze/upload required")
    ensure("push:" in text and "pull_request:" in text, "first push/PR security trigger required")


def check_deploy(text: str) -> None:
    events = text.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
    ensure(re.findall(r"(?m)^  ([\w_]+):", events) == ["workflow_dispatch"], "manual-only trigger required")
    ensure("workflow_run" not in text, "automatic deployment forbidden")
    for token in ('test "${#sha}" -eq 40', "*[!0-9a-f]*", 'DEPLOY lightsail-production ${sha}',
                  "test \"$REQUEST_EVENT\" = 'workflow_dispatch'", "test \"$WORKFLOW_REF\" = 'refs/heads/master'",
                  'git merge-base --is-ancestor "$sha" master', "Verify CI and Security gates for revision",
                  'GATE_SHA: ${{ steps.revision.outputs.sha }}', "needs.verify.result == 'success'",
                  "Verify previous known-good revision", "steps.known_good.conclusion == 'success'",
                  'ROLLBACK_SHA: ${{ steps.known_good.outputs.sha }}', "verify-deployment-record.sh",
                  "StrictHostKeyChecking yes", "Rollback after deployment failure"):
        ensure(token in text, "deployment contract missing: " + token)
    ensure(events.count("required: true") == 2, "required SHA/confirmation inputs")
    guard = text.index("Validate manual Production request")
    ensure(guard < text.index("uses: actions/checkout") < text.index("secrets."), "guard precedes checkout/secrets")
    ensure("continue-on-error" not in text, "failure cannot authorize deployment")


def check(output: dict[str, bytes], *, syntax: bool = False) -> dict:
    required = {".github/workflows/security.yml", ".github/workflows/deploy-lightsail.yml", "THIRD_PARTY_NOTICES.md",
                "docs/dependency-notices.json", "docs/third-party-license-texts.txt", "docs/deployment.md", "docs/provenance.md", "tw_quant/synthetic_data.py"}
    ensure(required <= set(output), "publication surface missing")
    check_codeql(output[".github/workflows/security.yml"].decode())
    check_deploy(output[".github/workflows/deploy-lightsail.yml"].decode())
    for path, content in output.items():
        if path.startswith(".github/workflows/"):
            text = content.decode()
            for action in re.findall(r"\buses:\s*([^\s#]+)", text):
                ensure(re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", action) is not None, "unpinned action: " + path)
            ensure("pull_request_target" not in text and "continue-on-error" not in text, "unsafe workflow policy: " + path)
            if syntax:
                import yaml
                document = yaml.safe_load(text)
                # PyYAML 1.1 parses the GitHub key `on` as True.
                ensure(isinstance(document, dict) and isinstance(document.get("jobs"), dict)
                       and isinstance(document.get("on", document.get(True)), dict), "workflow syntax: " + path)
                for block in document["jobs"].values():
                    ensure(isinstance(block, dict) and isinstance(block.get("steps"), list), "workflow jobs: " + path)
    ensure("dashboard/public/" + "og.png" not in output and "data/" + "mock_tmf_ticks.csv" not in output,
           "rights-unknown payload excluded")
    layout = output["dashboard/app/layout.tsx"].decode()
    ensure("og.png" not in layout and 'card: "summary"' in layout, "text-only social metadata")
    ensure("dashboard/scripts/" + "verify-demo-visual.mjs" not in output and ".github/workflows/" + "demo-visual.yml" not in output,
           "Legacy concrete visual acceptance excluded")
    notices = json.loads(output["docs/dependency-notices.json"])
    python = tomllib.loads(output["uv.lock"].decode())["package"]
    expected_python = {(p["name"], p["version"]) for p in python if p["source"] != {"editable": "."}}
    ensure({(p["name"], p["version"]) for p in notices["python"]} == expected_python, "Python notices coverage")
    npm = json.loads(output["dashboard/package-lock.json"])["packages"]
    ensure({p["path"] for p in notices["npm"]} == set(npm) - {""}, "npm notices coverage")
    for p in notices["npm"]:
        ensure(p["version"] == npm[p["path"]]["version"] and p["license"] == npm[p["path"]].get("license", "NOASSERTION"),
               "npm locked notice identity")
    for path in ("uv.lock", "dashboard/package-lock.json", "Dockerfile", "deploy/lightsail/Dockerfile.gateway"):
        ensure(notices["inputs"][path] == hashlib.sha256(output[path]).hexdigest(), "notice input changed: " + path)
    ensure(notices["acceptance"]["license_texts_sha256"] == hashlib.sha256(output["docs/third-party-license-texts.txt"]).hexdigest(),
           "upstream notice texts changed")
    artifacts = notices.get("container_artifacts", [])
    ensure(len(artifacts) == 1 and artifacts[0]["name"] == "caddy" and artifacts[0]["version"] == "2.11.6"
           and artifacts[0]["sha256"] == "22c84f8d2d4e4e0e2d422f8049fdd0fc1ed8d5665d0fe166f506c7fd863b4555",
           "Caddy release identity/notice")
    gateway = output["deploy/lightsail/Dockerfile.gateway"].decode()
    ensure(artifacts[0]["source"] in gateway and artifacts[0]["sha256"] in gateway
           and "alpine:3.23.6@sha256:85fe1e81d6758c208f3e1eed4338a1997e19d4be002d4dd32d3100c9a8c010a0" in gateway,
           "gateway inputs must be checksum/digest pinned")
    license_texts = output["docs/third-party-license-texts.txt"].decode()
    ensure(all(("SHA-256 " + item["sha256"]) in license_texts for item in artifacts[0]["license_files"]),
           "Caddy license text must be retained")
    ensure("--extra shioaji" not in output["Dockerfile"].decode(), "unknown SDK redistribution excluded from images")
    for path in ("Dockerfile", "deploy/lightsail/Dockerfile.gateway"):
        ensure("THIRD_PARTY_NOTICES.md" in output[path].decode() and "third-party-license-texts.txt" in output[path].decode(),
               "image notices must be retained")
    ensure(notices["acceptance"]["scope"] == "source publication; no third-party binary redistribution in source"
           and notices["acceptance"]["preserve_installed_license_files"] is True,
           "source publication license scope")
    expiry = output[".github/workflows/security-exception-expiry.yml"].decode()
    ensure("exception_policy.py enforce" in expiry and "if: always()" in expiry, "exception expiry must fail closed even after issue-sync failure")
    return {"codeql_policy": "PASS", "native_codeql": "NOT_RUN_P5; REQUIRED_P6", "p6_acceptance": list(P6_ACCEPTANCE),
            "license_notice_scope": notices["acceptance"]["scope"], "social_asset": "EXCLUDED; text-only metadata",
            "market_fixture": "EXCLUDED; deterministic runtime generator", "deployment_policy": "PASS; manual exact revision"}


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    index = json.loads((root / "public-candidate.json").read_text())
    output = {row["path"]: (root / row["path"]).read_bytes() for row in index["files"]}
    print(json.dumps(check(output, syntax=True), sort_keys=True))

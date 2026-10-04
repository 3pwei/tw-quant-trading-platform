#!/usr/bin/env python3
"""Bind downloaded bytes to the requested successful workflow before host access."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import zipfile

from candidate_manifest import require, validate

REPOSITORY = "3pwei/tw-quant-trading-platform"
WORKFLOW = ".github/workflows/staging-candidate.yml"


def select_artifact(document, run_id):
    require(isinstance(document.get("artifacts"), list), "artifact list missing")
    # Refuse incomplete pagination rather than selecting an unobserved duplicate.
    require(document.get("total_count") == len(document["artifacts"]), "artifact list incomplete")
    matches = [a for a in document["artifacts"] if a.get("name") == "p8-staging-candidate-manifest"]
    require(len(matches) == 1, "candidate artifact is ambiguous")
    artifact = matches[0]
    require(artifact.get("expired") is False, "artifact expired")
    require(str(artifact.get("workflow_run", {}).get("id")) == run_id, "artifact run mismatch")
    require(type(artifact.get("id")) is int and artifact["id"] > 0, "artifact ID invalid")
    require(artifact.get("archive_download_url") ==
            f"https://api.github.com/repos/{REPOSITORY}/actions/artifacts/{artifact['id']}/zip",
            "artifact URL is outside approved repository")
    require(re.fullmatch(r"sha256:[0-9a-f]{64}", str(artifact.get("digest"))) is not None,
            "artifact digest missing")
    return artifact


def verify_archive(archive, artifacts, run, workflow, run_id, pipeline):
    artifact = select_artifact(artifacts, run_id)
    require(str(run.get("id")) == run_id, "requested run mismatch")
    require(run.get("repository", {}).get("full_name") == REPOSITORY, "run repository mismatch")
    require(workflow.get("path") == WORKFLOW and type(workflow.get("id")) is int,
            "workflow identity mismatch")
    require(run.get("workflow_id") == workflow["id"] and run.get("path") == WORKFLOW,
            "run workflow mismatch")
    require(run.get("name") == "P8 Staging Candidate" and run.get("event") == "workflow_dispatch",
            "run event mismatch")
    require(run.get("status") == "completed" and run.get("conclusion") == "success", "run not successful")
    require(run.get("head_branch") == "master" and run.get("head_sha") == pipeline,
            "run is not this exact master pipeline")
    require(artifact["workflow_run"].get("head_sha") == pipeline, "artifact pipeline mismatch")
    require("sha256:" + hashlib.sha256(archive).hexdigest() == artifact["digest"], "artifact bytes mismatch")
    import io
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        names = zipped.namelist()
        require(sorted(names) == ["candidate-manifest.json", "candidate-manifest.sha256"], "unexpected archive members")
        require(all(i.file_size <= 65536 for i in zipped.infolist()), "manifest archive too large")
        payload = zipped.read("candidate-manifest.json")
        checksum = zipped.read("candidate-manifest.sha256").decode("ascii")
    require(checksum == "manifest_sha256=" + hashlib.sha256(payload).hexdigest() + "\n", "manifest checksum mismatch")
    document = json.loads(payload)
    validate(document)
    require(document["pipeline_revision"] == pipeline, "manifest pipeline mismatch")
    require(document["provenance"]["run_id"] == run_id, "manifest run mismatch")
    require(type(run.get("run_attempt")) is int and run["run_attempt"] >= 1 and
            document["provenance"]["run_attempt"] == str(run["run_attempt"]), "manifest attempt mismatch")
    return payload


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--run", type=Path)
    parser.add_argument("--workflow", type=Path)
    parser.add_argument("--pipeline")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    artifacts = json.loads(args.artifacts.read_text())
    if args.archive is None:
        print(select_artifact(artifacts, args.run_id)["archive_download_url"])
        return
    require(all((args.run, args.workflow, args.pipeline, args.output)), "verification arguments missing")
    require(args.archive.stat().st_size <= 131072, "artifact archive too large")
    payload = verify_archive(args.archive.read_bytes(), artifacts, json.loads(args.run.read_text()),
                             json.loads(args.workflow.read_text()), args.run_id, args.pipeline)
    args.output.write_bytes(payload)
    print("P8_CANDIDATE_BINDING=PASS")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError, zipfile.BadZipFile):
        raise SystemExit("P8_CANDIDATE_BINDING=FAIL (invalid provenance or artifact)")

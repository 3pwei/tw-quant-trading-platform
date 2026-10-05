#!/usr/bin/env python3
"""Create and verify the immutable P8 staging candidate manifest."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[2]
IDENTITY_FILE = Path(__file__).resolve().with_name("identities.conf")
if not IDENTITY_FILE.is_file():
    IDENTITY_FILE = ROOT / "deploy/staging/identities.conf"
IDENTITIES = {}
IMAGE_REPOSITORY = "ghcr.io/3pwei/tw-quant-trading-platform-staging"
for line in IDENTITY_FILE.read_text().splitlines():
    if line and not line.startswith("#"):
        key, value = line.split("=", 1)
        IDENTITIES[key] = value


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def image(ref: str, digest: str, config_digest: str) -> dict[str, str]:
    for value in (digest, config_digest):
        require(re.fullmatch(r"sha256:[0-9a-f]{64}", value) is not None, "invalid image digest")
    require(
        re.fullmatch(
            r"ghcr\.io/[a-z0-9][a-z0-9._/-]*:[a-z0-9][a-z0-9_.-]*@sha256:[0-9a-f]{64}",
            ref,
        )
        is not None,
        "image reference is not an approved digest-bound registry reference",
    )
    require(ref.endswith("@" + digest), "image reference digest mismatch")
    return {"ref": ref, "digest": digest, "config_digest": config_digest}


def validate(document: dict) -> None:
    require(isinstance(document, dict), "manifest must be an object")
    require(type(document.get("schema_version")) is int and document["schema_version"] == 2, "unknown manifest schema")
    require(document.get("p7_platform_source_sha") == IDENTITIES["P7_PLATFORM_SOURCE_SHA"], "P7 Platform baseline mismatch")
    require(document.get("platform_source_sha") == document.get("pipeline_revision"), "runtime source is not this exact pipeline")
    require(re.fullmatch(r"[0-9a-f]{40}", str(document.get("pipeline_revision", ""))) is not None, "invalid pipeline revision")
    require(document.get("core") == {
        "version": IDENTITIES["CORE_VERSION"],
        "wheel_sha256": IDENTITIES["CORE_WHEEL_SHA256"],
    }, "Core identity mismatch")
    require(document.get("private_provider") == {
        "version": IDENTITIES["PRIVATE_PROVIDER_VERSION"],
        "wheel_sha256": IDENTITIES["PRIVATE_PROVIDER_WHEEL_SHA256"],
        "p7_acceptance_sha": IDENTITIES["P7_ACCEPTANCE_SHA"],
    }, "private provider identity mismatch")
    images = document.get("images")
    provenance = document.get("provenance")
    require(isinstance(provenance, dict), "provenance is missing")
    require(provenance.get("build_once") is True, "build-once declaration is required")
    require(provenance.get("builder") == "github-actions/hosted-linux-x64", "builder mismatch")
    for key in ("run_id", "run_attempt"):
        require(isinstance(provenance.get(key), str) and re.fullmatch(r"[1-9][0-9]*", provenance[key]) is not None,
                "candidate " + key + " is invalid")
    suffix = provenance["run_id"] + "-" + provenance["run_attempt"]
    require(isinstance(images, dict), "images are missing")
    gateway = images.get("gateway")
    require(isinstance(gateway, dict), "gateway image is missing")
    image(gateway.get("ref", ""), gateway.get("digest", ""), gateway.get("config_digest", ""))
    require(gateway["ref"] == IMAGE_REPOSITORY + ":gateway-" + suffix + "@" + gateway["digest"],
            "gateway repository or run tag mismatch")
    releases = images.get("releases")
    require(isinstance(releases, dict) and set(releases) == {"known_good", "candidate"}, "release pair is invalid")
    identities = set()
    runtime_digests = set()
    for name, release in releases.items():
        require(isinstance(release, dict), "release is invalid")
        config = release.get("configuration_identity")
        role = "a" if name == "known_good" else "b"
        require(config == "p8-" + role + "-" + suffix, "configuration identity does not match provenance")
        identities.add(config)
        runtime = release.get("runtime")
        require(isinstance(runtime, dict), "runtime image is missing")
        image(runtime.get("ref", ""), runtime.get("digest", ""), runtime.get("config_digest", ""))
        require(runtime["ref"] == IMAGE_REPOSITORY + ":runtime-" + role + "-" + suffix + "@" + runtime["digest"],
                "runtime repository or run tag mismatch")
        runtime_digests.add(runtime["digest"])
    require(len(identities) == 2 and len(runtime_digests) == 2, "rollback releases must be distinct immutable images")


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--pipeline-revision", required=True)
    create.add_argument("--run-id", required=True)
    create.add_argument("--run-attempt", required=True)
    for name in ("runtime-a", "runtime-b", "gateway"):
        create.add_argument(f"--{name}-ref", required=True)
        create.add_argument(f"--{name}-digest", required=True)
        create.add_argument(f"--{name}-config-digest", required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("manifest", type=Path)
    emit = subparsers.add_parser("emit-env")
    emit.add_argument("manifest", type=Path)
    emit.add_argument("--release", choices=("known_good", "candidate"), required=True)
    args = parser.parse_args()

    if args.command == "create":
        run_suffix = f"{args.run_id}-{args.run_attempt}"
        document = {
            "schema_version": 2,
            "platform_source_sha": args.pipeline_revision,
            "p7_platform_source_sha": IDENTITIES["P7_PLATFORM_SOURCE_SHA"],
            "pipeline_revision": args.pipeline_revision,
            "core": {"version": IDENTITIES["CORE_VERSION"], "wheel_sha256": IDENTITIES["CORE_WHEEL_SHA256"]},
            "private_provider": {
                "version": IDENTITIES["PRIVATE_PROVIDER_VERSION"],
                "wheel_sha256": IDENTITIES["PRIVATE_PROVIDER_WHEEL_SHA256"],
                "p7_acceptance_sha": IDENTITIES["P7_ACCEPTANCE_SHA"],
            },
            "images": {
                "gateway": image(args.gateway_ref, args.gateway_digest, args.gateway_config_digest),
                "releases": {
                    "known_good": {
                        "configuration_identity": "p8-a-" + run_suffix,
                        "runtime": image(args.runtime_a_ref, args.runtime_a_digest, args.runtime_a_config_digest),
                    },
                    "candidate": {
                        "configuration_identity": "p8-b-" + run_suffix,
                        "runtime": image(args.runtime_b_ref, args.runtime_b_digest, args.runtime_b_config_digest),
                    },
                },
            },
            "provenance": {
                "builder": "github-actions/hosted-linux-x64",
                "run_id": str(args.run_id),
                "run_attempt": str(args.run_attempt),
                "build_once": True,
            },
        }
        validate(document)
        payload = json.dumps(document, sort_keys=True, indent=2) + "\n"
        args.output.write_text(payload)
        print("manifest_sha256=" + hashlib.sha256(payload.encode()).hexdigest())
        return 0

    document = json.loads(args.manifest.read_text())
    validate(document)
    if args.command == "verify":
        print("P8 candidate manifest: PASS")
        return 0

    release = document["images"]["releases"][args.release]
    values = {
        "PLATFORM_SOURCE_SHA": document["platform_source_sha"],
        "PIPELINE_REVISION": document["pipeline_revision"],
        "CORE_VERSION": document["core"]["version"],
        "CORE_WHEEL_SHA256": document["core"]["wheel_sha256"],
        "PRIVATE_PROVIDER_VERSION": document["private_provider"]["version"],
        "PRIVATE_PROVIDER_WHEEL_SHA256": document["private_provider"]["wheel_sha256"],
        "P7_ACCEPTANCE_SHA": document["private_provider"]["p7_acceptance_sha"],
        "CONFIGURATION_IDENTITY": release["configuration_identity"],
        "STAGING_RUNTIME_IMAGE": release["runtime"]["ref"],
        "STAGING_RUNTIME_DIGEST": release["runtime"]["digest"],
        "STAGING_RUNTIME_CONFIG_DIGEST": release["runtime"]["config_digest"],
        "STAGING_GATEWAY_IMAGE": document["images"]["gateway"]["ref"],
        "STAGING_GATEWAY_DIGEST": document["images"]["gateway"]["digest"],
        "STAGING_GATEWAY_CONFIG_DIGEST": document["images"]["gateway"]["config_digest"],
        "CANDIDATE_RUN_ID": document["provenance"]["run_id"],
        "CANDIDATE_RUN_ATTEMPT": document["provenance"]["run_attempt"],
        "RELEASE_NAME": args.release,
    }
    for key, value in values.items():
        require("\n" not in value and "\r" not in value, "unsafe manifest value")
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"candidate manifest rejected: {exc}", file=sys.stderr)
        sys.exit(2)

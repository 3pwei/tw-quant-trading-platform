"""Prepare an audit-only identity for the verified Core wheel, retaining all hashes."""
from __future__ import annotations

import argparse
from pathlib import Path
import tomllib


def prepare(lock_path: Path, source_path: Path, output_path: Path) -> None:
    # A failed preparation must never leave stale input for a subsequent audit.
    output_path.unlink(missing_ok=True)
    lock = tomllib.loads(lock_path.read_text())
    core = [p for p in lock["package"] if p["name"] == "tw-quant-core"]
    if len(core) != 1:
        raise ValueError("expected one locked Core identity")
    package = core[0]
    version = package["version"]
    url = ("https://github.com/3pwei/tw-quant-core/releases/download/"
           f"v{version}/tw_quant_core-{version}-py3-none-any.whl")
    wheels = package.get("wheels", [])
    if package["source"] != {"url": url} or len(wheels) != 1 or wheels[0]["url"] != url:
        raise ValueError("unexpected Core wheel source")
    digest = wheels[0]["hash"]
    if not digest.startswith("sha256:") or len(digest) != 71:
        raise ValueError("invalid Core wheel digest")
    source = source_path.read_text()
    stanza = f"tw-quant-core @ {url} \\\n    --hash={digest}\n"
    if source.count(stanza) != 1 or source.count("tw-quant-core @ ") != 1:
        raise ValueError("exported Core identity/hash does not match the lock")
    # pip-audit cannot audit URL requirements without pip resolution. Map only
    # this verified public wheel to its locked OSV name/version; no dependency
    # is excluded and this file is never used to install or build an image.
    output_path.write_text(source.replace(
        stanza, f"tw-quant-core=={version} \\\n    --hash={digest}\n"
    ))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock-file", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    prepare(args.lock_file, args.input, args.output)

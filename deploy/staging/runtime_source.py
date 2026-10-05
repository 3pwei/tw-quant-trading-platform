#!/usr/bin/env python3
"""Bind runtime package bytes to the exact reviewed candidate archive."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re


def require(condition, message):
    if not condition:
        raise ValueError(message)


def verify(root, receipt, revision):
    require(re.fullmatch(r"[0-9a-f]{40}", revision) is not None, "invalid revision")
    require(receipt.get("platform_source_sha") == revision, "source revision mismatch")
    files = receipt.get("files")
    require(isinstance(files, dict) and bool(files), "runtime files missing")
    actual = {str(p.relative_to(root)) for p in (root / "tw_quant").rglob("*")
              if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"}
    require(actual == set(files), "runtime file set mismatch")
    for name, digest in files.items():
        path = Path(name)
        require(path.parts[0] == "tw_quant" and ".." not in path.parts and not path.is_absolute(), "invalid path")
        require(re.fullmatch(r"[0-9a-f]{64}", str(digest)) is not None, "invalid hash")
        require(not (root / path).is_symlink(), "runtime symlink")
        require(hashlib.sha256((root / path).read_bytes()).hexdigest() == digest, "runtime bytes mismatch")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("create", "verify"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.command == "create":
        manifest = json.loads((root / "public-candidate.json").read_text())
        receipt = {"platform_source_sha": args.revision, "files": {
            row["path"]: row["sha256"] for row in manifest["files"]
            if row["path"].startswith("tw_quant/")}}
        verify(root, receipt, args.revision)
        (args.output or root / "runtime-source.json").write_text(json.dumps(receipt, sort_keys=True) + "\n")
    else:
        receipt = json.loads((root / "runtime-source.json").read_text())
        verify(root, receipt, args.revision)
        spec = importlib.util.find_spec("tw_quant")
        require(spec is not None and spec.origin is not None and
                Path(spec.origin).resolve() == root / "tw_quant/__init__.py", "runtime import path mismatch")
    print("P8_RUNTIME_SOURCE=PASS")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError):
        raise SystemExit("P8_RUNTIME_SOURCE=FAIL")

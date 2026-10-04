"""Hash the actual config bytes from a single-image Docker save stream, offline.

Engine image IDs are backend-specific: classic uses a config digest, containerd
uses a manifest/index digest. Neither an inspect JSON reserialization nor an
image ID is a portable substitute for the original config bytes.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import tarfile


CONFIG_PATH = re.compile(r"(?:([0-9a-f]{64})\.json|blobs/sha256/([0-9a-f]{64}))")
MAX_METADATA = 8 * 1024 * 1024


def config_digest(stream) -> str:
    manifest = None
    hashes = {}
    seen = set()
    with tarfile.open(fileobj=stream, mode="r|") as archive:
        for member in archive:
            match = CONFIG_PATH.fullmatch(member.name)
            if member.name != "manifest.json" and not match:
                continue
            if member.name in seen or not member.isfile():
                raise ValueError("duplicate or non-regular metadata")
            seen.add(member.name)
            if member.size > MAX_METADATA:
                # Large content-addressed layer blobs are not config metadata.
                if member.name == "manifest.json":
                    raise ValueError("oversized manifest")
                continue
            body = archive.extractfile(member).read()
            if member.name == "manifest.json":
                manifest = json.loads(body)
            else:
                hashes[member.name] = hashlib.sha256(body).hexdigest()
    # Drain tar padding so docker save can exit cleanly (no SIGPIPE).
    while stream.read(1024 * 1024):
        pass
    if not isinstance(manifest, list) or len(manifest) != 1:
        raise ValueError("expected exactly one image manifest")
    path = manifest[0].get("Config", "")
    match = CONFIG_PATH.fullmatch(path)
    actual = hashes.get(path)
    if not match or actual is None or actual != (match[1] or match[2]):
        raise ValueError("missing or corrupt config bytes")
    return "sha256:" + actual


if __name__ == "__main__":
    try:
        result = config_digest(sys.stdin.buffer)
    except (ValueError, TypeError, AttributeError, KeyError, OSError, tarfile.TarError):
        # Never print config contents, environment, layer data or parser errors.
        print("P8_IMAGE_CONFIG_FAIL invalid-image-archive", file=sys.stderr)
        sys.exit(1)
    print(result)

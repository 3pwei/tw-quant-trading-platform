"""Verify reviewed public source bytes and dependency boundaries without Git history."""
from __future__ import annotations
import argparse
import ast
import hashlib
import importlib
import importlib.metadata
import json
from pathlib import Path
import re
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]
CORE_URL = "https://github.com/3pwei/tw-quant-core/releases/download/v1.2.0/tw_quant_core-1.2.0-py3-none-any.whl"
CORE_HASH = "sha256:63645e42755068c308d66d74ded5133395dfef816360dc8106e0bbc247ee49bd"

def core_modules():
    import tw_quant_core.broker as core_0
    import tw_quant_core.broker.registry as core_1
    import tw_quant_core.events as core_2
    import tw_quant_core.execution as core_3
    import tw_quant_core.execution.liquidator as core_4
    import tw_quant_core.execution.pipeline as core_5
    import tw_quant_core.execution.risk_gates as core_6
    import tw_quant_core.execution.signal_router as core_7
    import tw_quant_core.execution.simulated_broker as core_8
    import tw_quant_core.execution.simulator as core_9
    import tw_quant_core.futures_costs as core_10
    import tw_quant_core.market as core_11
    import tw_quant_core.market.quotes as core_12
    import tw_quant_core.metrics as core_13
    import tw_quant_core.risk as core_14
    import tw_quant_core.strategy as core_15
    import tw_quant_core.config as core_16
    import tw_quant_core.costs as core_17
    import tw_quant_core.data as core_18
    import tw_quant_core.futures as core_19
    import tw_quant_core.market.sessions as core_20
    return {
        'tw_quant_core.config': core_16,
        'tw_quant_core.costs': core_17,
        'tw_quant_core.data': core_18,
        'tw_quant_core.futures': core_19,
        'tw_quant_core.market.sessions': core_20,
        'tw_quant_core.broker': core_0,
        'tw_quant_core.broker.registry': core_1,
        'tw_quant_core.events': core_2,
        'tw_quant_core.execution': core_3,
        'tw_quant_core.execution.liquidator': core_4,
        'tw_quant_core.execution.pipeline': core_5,
        'tw_quant_core.execution.risk_gates': core_6,
        'tw_quant_core.execution.signal_router': core_7,
        'tw_quant_core.execution.simulated_broker': core_8,
        'tw_quant_core.execution.simulator': core_9,
        'tw_quant_core.futures_costs': core_10,
        'tw_quant_core.market': core_11,
        'tw_quant_core.market.quotes': core_12,
        'tw_quant_core.metrics': core_13,
        'tw_quant_core.risk': core_14,
        'tw_quant_core.strategy': core_15,
    }

def check(root: Path, runtime: bool = False) -> None:
    index = json.loads((root / "public-candidate.json").read_text())
    if index["p6_entry_gate"] != "PASS" or index["remaining_gates"]:
        raise ValueError("P6 entry contract is incomplete")
    import importlib.util
    spec = importlib.util.spec_from_file_location("public_policy", root / "tools/verify_publication_policy.py")
    policy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(policy)
    files = {r["path"]: r["sha256"] for r in index["files"]}
    if len(files) != len(index["files"]):
        raise ValueError("Duplicate public source path")
    paths = set(files)
    ignored = {".git", ".venv", "node_modules", ".next", "out", "build", "dist", ".validation-cache", ".validation-tools"}
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()
              and not any(part in ignored or part.endswith(".egg-info") or part == "__pycache__" for part in p.relative_to(root).parts)}
    if actual != paths | {"public-candidate.json"}:
        raise ValueError("Unexpected or missing public source file")
    modules = {p[:-12].replace("/", ".") if p.endswith("/__init__.py") else p[:-3].replace("/", "."): p
               for p in paths if p.endswith(".py")}
    for name, digest in files.items():
        path = root / name
        if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("Reviewed source mismatch: " + name)
        text = path.read_text()
        forbidden = ("tw_quant_" + "strategies", "tw-quant-" + "strategies",
                     "PRIVATE_STRATEGY_" + "READ_TOKEN", "strategy_" + "artifacts")
        if any(value in text for value in forbidden):
            raise ValueError("Non-public dependency: " + name)
        if name not in {".gitignore", ".dockerignore"} and "." + "artifacts/" in text:
            raise ValueError("Artifact dependency: " + name)
        if not name.endswith(".py"):
            continue
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [item.name for item in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    package = name[:-3].replace("/", ".").rpartition(".")[0]
                    if name.endswith("/__init__.py"):
                        package = name[:-12].replace("/", ".")
                    parts = package.split(".")
                    base = ".".join(parts[:len(parts)-node.level+1] + ([base] if base else []))
                names = [base]
                if runtime and base.startswith("tw_quant_core"):
                    module = core_modules()[base]
                    if any(not hasattr(module, item.name) for item in node.names):
                        raise ValueError("Unavailable Core contract: " + name)
            else:
                continue
            for module in names:
                if module.startswith("tw_quant.") and module not in modules:
                    raise ValueError("Missing application module: " + name)
    policy.check({row["path"]: (root / row["path"]).read_bytes() for row in index["files"]}, syntax=runtime)
    metadata = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    if metadata["name"] != "tw-quant-trading-platform" or "tw-quant-core @ " + CORE_URL not in metadata["dependencies"]:
        raise ValueError("Public package identity/dependency mismatch")
    lock = tomllib.loads((root / "uv.lock").read_text())
    core = [p for p in lock["package"] if p["name"] == "tw-quant-core"]
    if len(core) != 1 or core[0]["version"] != "1.2.0" or core[0]["source"] != {"url": CORE_URL}:
        raise ValueError("Core lock identity mismatch")
    if core[0]["wheels"] != [{"url": CORE_URL, "hash": CORE_HASH}]:
        raise ValueError("Core lock digest mismatch")
    for p in lock["package"]:
        source = p["source"]
        if source not in ({"url": CORE_URL}, {"editable": "."}, {"registry": "https://pypi.org/simple"}):
            raise ValueError("Unapproved package source")
    if runtime and importlib.metadata.version("tw-quant-core") != "1.2.0":
        raise ValueError("Core runtime version mismatch")
    print("Public source/digests/dependency closure: PASS; P6 execution/native CodeQL acceptance remains required")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", action="store_true")
    args = parser.parse_args()
    check(ROOT, args.runtime)

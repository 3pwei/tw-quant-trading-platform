"""Offline Shioaji capability gate for exact, already-built P8 image bytes."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
from dataclasses import replace
import importlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
from unittest.mock import patch


EXPECTED_VERSION = "1.7.4"
EXPECTED_LABELS = {
    "io.tw-quant.capability.market.shioaji": "true",
    "io.tw-quant.shioaji.version": EXPECTED_VERSION,
}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def forbidden_call(*args, **kwargs):
    raise AssertionError("capability loading attempted a network or broker call")


def probe() -> dict:
    """Load the real installed SDK, without constructing or logging into a client."""
    require(version("shioaji") == EXPECTED_VERSION, "installed Shioaji version mismatch")
    # Supply only cache/log paths. No keys, account identity or CA is provided.
    with tempfile.TemporaryDirectory(prefix="p8-shioaji-") as directory, ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {
            "XDG_CACHE_HOME": directory,
            "MPLCONFIGDIR": directory, "SJ_LOG_PATH": str(Path(directory) / "shioaji.log"),
        }, clear=True))
        for target in ("create_connection", "getaddrinfo"):
            stack.enter_context(patch.object(socket, target, side_effect=forbidden_call))
        for target in ("connect", "connect_ex"):
            stack.enter_context(patch.object(socket.socket, target, side_effect=forbidden_call))
        sdk = importlib.import_module("shioaji")
        stack.enter_context(patch.object(sdk, "Shioaji", side_effect=forbidden_call))
        market = importlib.import_module("tw_quant.market_data.providers.shioaji")
        require(callable(market.ShioajiMarketDataProvider), "Shioaji market provider unavailable")
        for module in (
            "tw_quant.broker.shioaji", "tw_quant.broker.shioaji_production",
            "tw_quant.broker.shioaji_secrets", "tw_quant.broker.secret_factory",
        ):
            importlib.import_module(module)
        service = importlib.import_module("tw_quant.execution_service")
        settings = service.ExecutionServiceSettings.from_env()
        require(settings.broker_name == "disabled", "default broker is enabled")
        for name in ("live_trading_enabled", "production_read_only_enabled",
                     "live_canary_enabled", "live_auto_enabled", "live_position_guardian_enabled"):
            require(getattr(settings, name) is False, "default execution flag enabled: " + name)
        settings = replace(settings, database_path=str(Path(directory) / "execution.sqlite3"),
                           health_path=str(Path(directory) / "health.json"),
                           generation_path=str(Path(directory) / "generation"))

        async def check_disabled():
            runtime = service.build_execution_service(settings)
            try:
                await runtime.start()
                state = runtime.state_document()
                require(state["locked"] is True, "execution is not locked")
                require("execution_disabled" in runtime.issues, "disabled execution issue missing")
                require(runtime.read_only_client is None, "broker client created by default")
                require(state["external_order_calls"] == 0, "external order call occurred")
                require(state["external_cancel_calls"] == 0, "external cancel call occurred")
            finally:
                await runtime.close()

        asyncio.run(check_disabled())
    return {"P8_SHIOAJI_CAPABILITY": "PASS", "shioaji_version": EXPECTED_VERSION,
            "execution_locked": True, "external_order_calls": 0, "external_cancel_calls": 0}


def verify_image(image: str) -> dict:
    inspected = subprocess.run(["docker", "image", "inspect", image],
                               check=True, capture_output=True, text=True)
    documents = json.loads(inspected.stdout)
    require(len(documents) == 1, "ambiguous runtime image")
    document = documents[0]
    image_id = document["Id"]
    require(image_id.startswith("sha256:") and len(image_id) == 71, "invalid image ID")
    labels = document["Config"].get("Labels") or {}
    require(all(labels.get(key) == value for key, value in EXPECTED_LABELS.items()),
            "Shioaji capability labels missing or mismatched")
    require(document["Config"].get("User") == "10001:10001", "runtime user mismatch")
    # Run by inspected content ID, never resolve a mutable tag again for this test.
    subprocess.run([
        "docker", "run", "--rm", "--network", "none", "--read-only",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=128m,uid=10001,gid=10001",
        "--mount", f"type=bind,source={Path(__file__).resolve()},target=/app/p8_shioaji_capability.py,readonly",
        "--entrypoint", "python", image_id, "/app/p8_shioaji_capability.py", "probe",
    ], check=True)
    return {"image_id": image_id, "labels": EXPECTED_LABELS, "P8_SHIOAJI_EXACT_IMAGE": "PASS"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("probe", "image"))
    parser.add_argument("--image")
    args = parser.parse_args()
    if args.command == "image":
        require(bool(args.image), "image is required")
        result = verify_image(args.image)
    else:
        result = probe()
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()

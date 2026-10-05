from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from .config import ExecutionServiceSettings
from .health import read_generation_marker, valid_generation, write_generation_marker


def _healthcheck(settings: ExecutionServiceSettings) -> int:
    path = Path(settings.health_path)
    try:
        generation = read_generation_marker(settings.generation_path)
        document = json.loads(path.read_text(encoding="utf-8"))
        heartbeat = datetime.fromisoformat(str(document["heartbeat_at"]))
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return 1
    if (
        generation is None
        or not valid_generation(document.get("generation"))
        or document["generation"] != generation
        or heartbeat.tzinfo is None
    ):
        return 1
    age = (datetime.now(timezone.utc) - heartbeat).total_seconds()
    valid_state = document.get("execution_state") in {
            "disabled", "locked", "read_only_ready", "ready_read_only", "degraded"
        }
    if settings.live_canary_enabled:
        safe_state = (
            valid_state
            and isinstance(document.get("external_order_calls"), int)
            and isinstance(document.get("external_cancel_calls"), int)
            and int(document["external_order_calls"]) >= 0
            and int(document["external_cancel_calls"]) >= 0
        )
    else:
        safe_state = (
            document.get("locked") is True
            and valid_state
            and document.get("external_order_calls") == 0
            and document.get("external_cancel_calls") == 0
        )
    return 0 if safe_state and age <= max(30.0, settings.heartbeat_seconds * 4) else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=("run", "validate", "healthcheck"),
        nargs="?",
        default="run",
    )
    args = parser.parse_args()
    settings = ExecutionServiceSettings.from_env()
    if args.command == "healthcheck":
        return _healthcheck(settings)
    import asyncio

    from .runtime import build_execution_service

    generation = (
        write_generation_marker(settings.generation_path)
        if args.command == "run"
        else None
    )
    runtime = build_execution_service(settings, generation=generation)
    if args.command == "validate":
        print(json.dumps(runtime.public_health(), sort_keys=True))
        asyncio.run(runtime.close())
        return 0
    try:
        asyncio.run(runtime.serve())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

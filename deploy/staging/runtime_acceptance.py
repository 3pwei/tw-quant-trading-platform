#!/usr/bin/env python3
"""Runtime-only P8 checks against the installed exact provider and Public APIs."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from staging_composition import load_provider
from tw_quant.auth import AccountStatus, AuthUser, Role, TradingMode
from tw_quant.backtest import run_strategy_backtest
from tw_quant.execution_service import ExecutionServiceSettings, build_execution_service
from tw_quant.live.storage import SQLiteBarRepository
from tw_quant.market import KBar, TAIPEI
from tw_quant.paper import PaperOrderCommand
from tw_quant.replay import ReplayTradingSessionRegistry
from tw_quant.strategy_registry import RegistryStrategies, StrategyUnavailable, strategy_scope


EXPECTED_DIGEST = "sha256:" + os.environ["PRIVATE_PROVIDER_WHEEL_SHA256"]


def bars() -> list[KBar]:
    start = datetime(2026, 8, 24, 8, 45, tzinfo=TAIPEI)
    values = [100.0, 100.0, 102.0, 103.0, 104.0, 105.0]
    return [
        KBar(
            symbol="SYNTHETIC", contract="P8-STAGING", time=start + timedelta(minutes=index),
            open=value - 0.1, high=value + 0.3, low=value - 0.3, close=value,
            volume=500 if index == 2 else 100, status="closed", session="day",
            trading_date=(start + timedelta(minutes=index)).date(),
            first_tick_time=start + timedelta(minutes=index),
            last_tick_time=start + timedelta(minutes=index, seconds=50),
            exchange_time=start + timedelta(minutes=index, seconds=50),
            received_time=start + timedelta(minutes=index, seconds=50), latency_ms=0,
        )
        for index, value in enumerate(values)
    ]


def main() -> None:
    services = RegistryStrategies(provider=load_provider())
    catalog = services.catalog()
    assert catalog, "private registry is empty"
    identity = services.identity_snapshot()
    assert identity["artifact_digest"] == EXPECTED_DIGEST
    key = next(item["key"] for item in catalog if item["key"] != "composite")
    parameters = services.template(key)
    services.evaluate(bars(), key, parameters=parameters)
    analysis = services.analyze(bars(), (key,), parameters={key: parameters})
    assert analysis["strategies"][0]["diagnostics"][0]["diagnostic_id"] == "platform-analysis"
    with strategy_scope(services):
        result = run_strategy_backtest(
            bars(), key, bars()[0].trading_date, bars()[-1].trading_date,
            parameters=parameters, source="P8 synthetic authorized fixture",
        )
    assert result["execution"]["engine"] == "deterministic_event_engine"

    owner = AuthUser(
        user_id="p8-owner", email="p8-owner@example.invalid", role=Role.TRADER,
        status=AccountStatus.ACTIVE, trading_mode=TradingMode.PAPER,
        permissions=("orders.paper", "positions.read.own"),
    )
    replay = ReplayTradingSessionRegistry(strategy_services=services)
    try:
        session = replay.create("p8-synthetic", owner, bars())
        command = PaperOrderCommand(key, 1, "buy", 1, 99.0)
        order, created, state = session.submit(command, idempotency_key="p8-once")
        assert created and order["status"] == "filled" and state["positions"]
        duplicate, created_again, _ = session.submit(command, idempotency_key="p8-once")
        assert not created_again and duplicate["order_id"] == order["order_id"]
        assert session.seek(3)["cursor"] == 3
        assert session.seek(1)["rewound"] is True
    finally:
        replay.close()

    with tempfile.TemporaryDirectory(prefix="p8-runtime-") as temporary:
        database = Path(temporary) / "runtime.sqlite3"
        repository = SQLiteBarRepository(database)
        try:
            for item in bars():
                repository.save(item)
            assert repository.latest("SYNTHETIC", limit=10)
        finally:
            repository.close()
        connection = sqlite3.connect(database)
        try:
            assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for required in (
                "strategy_versions", "portfolio_versions", "risk_profiles",
                "risk_profile_bindings", "strategy_runtime_atomic_lineage",
            ):
                assert required in tables, required
        finally:
            connection.close()

        settings = ExecutionServiceSettings(
            broker_name="disabled", database_path=str(database),
            health_path=str(Path(temporary) / "health.json"),
        )
        runtime = build_execution_service(settings)
        try:
            state = runtime.state_document()
            assert state["locked"] is True
            assert state["external_order_calls"] == 0
            assert state["external_cancel_calls"] == 0
            assert "execution_disabled" in runtime.issues
        finally:
            asyncio.run(runtime.close())

    wrong = dict(identity, artifact_digest="sha256:" + "0" * 64)
    try:
        services.require_snapshot({
            "strategy": key, "atomic_strategy_version": 1,
            "parameters": parameters, "plugin_identity": wrong,
        })
    except StrategyUnavailable:
        pass
    else:
        raise AssertionError("ambiguous provider identity did not fail closed")

    print(json.dumps({
        "P8_RUNTIME_ACCEPTANCE": "PASS",
        "registry_count": len(catalog),
        "private_artifact_digest": EXPECTED_DIGEST,
        "backtest_replay_paper": "PASS",
        "sqlite_lineage": "PASS",
        "execution_locked": True,
        "external_order_calls": 0,
    }, sort_keys=True))


if __name__ == "__main__":
    main()

"""P5 candidate-only behavior probe; inert Core provider, no private import.

Invoked in a fresh process against a temporary reconstruction. Only inert responses are provided; strategy implementation is external.
"""
from __future__ import annotations

import ast
import asyncio
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timedelta
import importlib
import importlib.abc
from pathlib import Path
import sys
import tempfile
import unittest

CANDIDATE = Path(sys.argv.pop(1)).resolve()
sys.path.insert(0, str(CANDIDATE))


class RejectPrivateImports(importlib.abc.MetaPathFinder):
    attempts = []

    def find_spec(self, fullname, path=None, target=None):
        if (fullname.startswith("tw_quant_" + "strategies") or
                fullname.startswith("tw_quant.strategy.") or
                fullname in {"tw_quant.strategy", "tw_quant.strategy_" + "artifacts"}):
            self.attempts.append(fullname)
            raise AssertionError("candidate attempted a private/Legacy strategy import")
        return None


REJECT = RejectPrivateImports()
sys.meta_path.insert(0, REJECT)

from fastapi.testclient import TestClient
from tw_quant_core.strategy import (
    CompositeAnalysisRequest, CompositeEvaluationRequest, CompositeMember,
    DiagnosticRecord, ParameterField, ParameterKind, ParameterSchemaMetadata,
    ParameterNormalizationResult, ParameterTemplateResult,
    ParameterValidationResult, PluginArtifactIdentity, StrategyAnalysisResult,
    StrategyCapability, StrategyCapabilityError, StrategyDescriptor,
    StrategyEvaluationRequest, StrategyEvaluationResult, StrategyIdentity,
    StrategyIntent, StrategyReference, StrategyRegistry,
)
from tw_quant.auth import AccountStatus, AuthUser, Role, TradingMode
from tw_quant.backtest import run_strategy_backtest, run_composite_backtest
from tw_quant.live.api import create_app
from tw_quant.live.demo_backtest import DemoExecutionInput
from tw_quant.live.runtime_data import known_plugin_lineage
from tw_quant.live.settings import LiveSettings
from tw_quant.live.storage import SQLiteBarRepository
from tw_quant.market import KBar, TAIPEI
from tw_quant.paper import PaperOrderCommand, PaperTradingService, SQLitePaperRepository
from tw_quant.replay import ReplaySessionNotFound, ReplayTradingSessionRegistry
from tw_quant.execution_service import ExecutionServiceSettings, build_execution_service
from tw_quant.strategy_registry import (
    RegistryStrategies, StrategyUnavailable, get_strategy_services,
    strategy_scope, supported_strategies,
)


class InertProvider:
    """Scripted hold/empty diagnostics. It has no market-dependent rule."""

    def __init__(
        self,
        key="scripted",
        capabilities=None,
        schema="1",
        fields=None,
        template=None,
        normalize=None,
        result_reference=None,
        strategy_version="1",
    ):
        self.identity = PluginArtifactIdentity("inert-"+key, "1", "inert-double", "1", "sha256:"+"a"*64)
        self.reference = StrategyReference(
            self.identity, StrategyIdentity(key, strategy_version)
        )
        self.descriptor = StrategyDescriptor(
            self.reference, "Scripted test response", ParameterSchemaMetadata(schema, fields or (
                ParameterField("force_close_last", ParameterKind.BOOLEAN, required=False),
            )), frozenset(capabilities or {
                StrategyCapability.EVALUATE,
                StrategyCapability.ANALYZE,
                StrategyCapability.NORMALIZE_PARAMETERS,
                StrategyCapability.PARAMETER_TEMPLATE,
            }))
        self.calls = []
        self.template_values = dict(
            {"force_close_last": False} if template is None else template
        )
        self.normalizer = normalize or (lambda values: dict(values))
        self.result_reference = result_reference

    def descriptors(self):
        return (self.descriptor,)

    def validate_parameters(self, reference, schema, parameters):
        self.calls.append((reference, schema, dict(parameters)))
        fields = {field.name: field for field in self.descriptor.parameter_schema.fields}
        errors = []
        if set(parameters) - set(fields):
            errors.append("undeclared parameter")
        if any(field.required and name not in parameters for name, field in fields.items()):
            errors.append("required parameter missing")
        return ParameterValidationResult(not errors, tuple(errors))

    def normalize_parameters(self, request):
        return ParameterNormalizationResult(
            self.result_reference or request.reference,
            request.parameter_schema_version,
            request.supplied_parameters,
            self.normalizer(request.supplied_parameters),
        )

    def parameter_template(self, request):
        return ParameterTemplateResult(
            self.result_reference or request.reference,
            request.parameter_schema_version,
            self.template_values,
        )

    def evaluate(self, request):
        return StrategyEvaluationResult(request.reference, (StrategyIntent("hold", "scripted hold"),))

    def analyze(self, request):
        return StrategyAnalysisResult(self.result_reference or request.reference, (
            DiagnosticRecord("platform-analysis", "1", {"signals": []}),))

    def evaluate_composite(self, request):
        return StrategyEvaluationResult(request.evaluator, (StrategyIntent("hold", "scripted composite hold"),))

    def analyze_composite(self, request):
        return StrategyAnalysisResult(
            self.result_reference or request.evaluator,
            (DiagnosticRecord("platform-analysis", "1", {"signals": []}),),
        )


class IncompleteOptionalProvider:
    """Advertises optional Core capabilities without implementing their ports."""

    def __init__(self, capability):
        base = InertProvider(capabilities={
            StrategyCapability.EVALUATE,
            StrategyCapability.ANALYZE,
            capability,
        })
        self.identity = base.identity
        self.reference = base.reference
        self.descriptor = base.descriptor

    def descriptors(self):
        return (self.descriptor,)

    def validate_parameters(self, reference, schema, parameters):
        return ParameterValidationResult(True)

    def evaluate(self, request):
        return StrategyEvaluationResult(
            request.reference, (StrategyIntent("hold", "scripted hold"),)
        )

    def analyze(self, request):
        return StrategyAnalysisResult(
            request.reference,
            (DiagnosticRecord("platform-analysis", "1", {"signals": []}),),
        )

    def evaluate_composite(self, request):
        return StrategyEvaluationResult(
            request.evaluator,
            (StrategyIntent("hold", "scripted composite hold"),),
        )


class InertDemoProvider:
    def __init__(self, provider):
        self.provider = provider

    def case_catalog(self):
        return ({"case_id": "inert-case", "name": "Inert case"},)

    def case_execution_input(self, case_id):
        return DemoExecutionInput(
            case_id,
            "analyze",
            StrategyEvaluationRequest(
                self.provider.reference,
                "1",
                {"force_close_last": False},
                tuple(bars()),
            ),
        )


class InertFeed:
    provider_name = "inert"
    contract = "EXAMPLE"

    async def start(self, on_tick, on_status):
        on_status("connected")

    async def stop(self):
        pass

    async def heartbeat(self):
        return True


def bars():
    at = datetime(2026, 9, 1, 9, 0, tzinfo=TAIPEI)
    return [KBar(symbol="TMF", contract="EXAMPLE", time=at+timedelta(minutes=i),
                 open=20000, high=20010, low=19990, close=20000, volume=1,
                 status="closed", session="day", trading_date=at.date(),
                 first_tick_time=at+timedelta(minutes=i), last_tick_time=at+timedelta(minutes=i),
                 exchange_time=at+timedelta(minutes=i),
                 received_time=at+timedelta(minutes=i), latency_ms=0) for i in range(3)]


def trader(owner):
    return AuthUser(user_id=owner, email=owner+"@example.com", role=Role.TRADER,
                    status=AccountStatus.ACTIVE, trading_mode=TradingMode.PAPER,
                    permissions=("orders.paper", "positions.read.own"))


class CandidateRuntimeTests(unittest.TestCase):
    def test_execution_worker_disabled_and_missing_target_never_load_secrets_or_client(self):
        class RejectSecrets:
            def load(self, connection):
                raise AssertionError("unowned worker attempted secret loading")
        def reject_client(*args, **kwargs):
            raise AssertionError("unowned worker attempted broker client construction")
        for enabled in (False, True):
            with self.subTest(enabled=enabled), tempfile.TemporaryDirectory() as temp:
                settings=ExecutionServiceSettings(
                    broker_name="shioaji" if enabled else "disabled", live_trading_enabled=enabled,
                    database_path=str(Path(temp)/"worker.sqlite3"), health_path=str(Path(temp)/"health.json"))
                runtime=build_execution_service(settings,secret_provider=RejectSecrets(),production_client_factory=reject_client)
                try:
                    health=runtime.state_document()
                    self.assertTrue(health["locked"])
                    self.assertEqual(health["external_order_calls"],0)
                    self.assertEqual(health["external_cancel_calls"],0)
                    self.assertIsNone(runtime.manager)
                    self.assertIn("execution_disabled",runtime.issues)
                finally:
                    asyncio.run(runtime.close())

    def test_every_core_imported_symbol_exists_and_no_private_import(self):
        for name, module in list(sys.modules.items()):
            if name == "tw_quant" or name.startswith("tw_quant."):
                self.assertTrue(Path(module.__file__).resolve().is_relative_to(CANDIDATE))
        self.assertEqual(REJECT.attempts, [])

    def test_empty_registry_and_unknown_version_schema_capability_fail_closed(self):
        services = RegistryStrategies()
        self.assertEqual(services.catalog(), [])
        for call in (lambda: services.evaluate([], "missing"), lambda: services.analyze([]),
                     lambda: services.identity_snapshot(),
                     lambda: services.template("missing"),
                     lambda: services.normalize("missing", {})):
            with self.assertRaises(StrategyUnavailable): call()
        provider = InertProvider(capabilities={StrategyCapability.EVALUATE})
        services = RegistryStrategies(provider=provider)
        with self.assertRaises(StrategyUnavailable): services.reference("unknown")
        with self.assertRaises(StrategyUnavailable): services.request("scripted", [], {}, schema_version="unknown")
        with self.assertRaises(StrategyUnavailable): services.analyze([], ["scripted"])
        with self.assertRaises(RuntimeError): services.registry.register(InertProvider("other"))

    def test_normalization_and_template_use_exact_provider_results(self):
        provider = InertProvider(
            fields=(ParameterField("input", ParameterKind.NUMBER),),
            template={"input": 3},
            normalize=lambda values: (
                {"input": values.get("input", values.get("alias"))}
                if "input" in values or "alias" in values
                else dict(values)
            ),
        )
        services = RegistryStrategies(provider=provider)
        with self.assertRaises(StrategyUnavailable): services.validate("scripted", {"unknown": 1})
        self.assertEqual(services.validate("scripted", {"input": 7}), {"input": 7})
        self.assertEqual(services.validate("scripted", {"alias": 7}), {"input": 7})
        self.assertEqual(services.template("scripted"), {"input": 3})
        self.assertEqual(services.catalog()[0]["parameters"], {"input": 3})
        self.assertEqual(
            services.catalog({"scripted": {"alias": 9}})[0]["parameters"],
            {"input": 9},
        )
        with self.assertRaises(StrategyUnavailable): services.require_snapshot({})
        snapshot = {
            "strategy": "scripted",
            "atomic_strategy_version": 1,
            "parameters": {"input": 7},
            "plugin_identity": services.identity_snapshot(),
        }
        services.require_snapshot(snapshot)
        with self.assertRaises(StrategyUnavailable):
            services.require_snapshot(dict(snapshot, parameters={"alias": 7}))
        snapshot["plugin_identity"] = dict(snapshot["plugin_identity"], parameter_schema_version="wrong")
        with self.assertRaises(StrategyUnavailable): services.require_snapshot(snapshot)

    def test_atomic_configuration_version_is_not_provider_implementation_version(self):
        provider = InertProvider(
            fields=(ParameterField("input", ParameterKind.NUMBER),),
            normalize=lambda values: (
                {"input": values.get("input", values.get("alias"))}
                if "input" in values or "alias" in values
                else dict(values)
            ),
            strategy_version="0.2.0",
        )
        services = RegistryStrategies(provider=provider)
        identity = services.identity_snapshot()
        snapshot = {
            "strategy": "scripted",
            "atomic_strategy_version": 1,
            "parameters": {"input": 7},
            "plugin_identity": identity,
        }

        services.require_snapshot(snapshot)
        services.require_snapshot(dict(snapshot, atomic_strategy_version=2))

        for invalid in (0, -1, True, 1.0, "1"):
            with self.subTest(invalid_configuration_version=invalid), self.assertRaises(
                StrategyUnavailable
            ):
                services.require_snapshot(
                    dict(snapshot, atomic_strategy_version=invalid)
                )

        provider_mismatch = dict(
            identity,
            strategy_versions={"scripted": "0.2.1"},
        )
        with self.assertRaises(StrategyUnavailable):
            services.require_snapshot(
                dict(snapshot, plugin_identity=provider_mismatch)
            )
        for field, value in (
            ("plugin_id", "other-plugin"),
            ("artifact_digest", "sha256:" + "b" * 64),
        ):
            with self.subTest(identity_field=field), self.assertRaises(
                StrategyUnavailable
            ):
                services.require_snapshot(
                    dict(
                        snapshot,
                        plugin_identity=dict(identity, **{field: value}),
                    )
                )
        with self.assertRaises(StrategyUnavailable):
            services.require_snapshot(
                dict(
                    snapshot,
                    plugin_identity=dict(
                        identity, parameter_schema_version="2"
                    ),
                )
            )
        with self.assertRaises(StrategyUnavailable):
            services.require_snapshot(
                dict(snapshot, parameters={"alias": 7})
            )

    def test_persisted_runtime_keeps_configuration_and_exact_provider_identity(self):
        provider = InertProvider(
            fields=(ParameterField("input", ParameterKind.NUMBER),),
            strategy_version="0.2.0",
        )
        services = RegistryStrategies(provider=provider)
        identity = services.identity_snapshot()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "runtime.sqlite3"
            repo = SQLiteBarRepository(path)
            first = repo.ensure_strategy_version(
                "scripted", {"input": 7}, identity, "owner"
            )
            second = repo.ensure_strategy_version(
                "scripted", {"input": 8}, identity, "owner"
            )
            snapshot = {
                "strategy": "scripted",
                "atomic_strategy_version": second["version"],
                "parameters": second["parameters"],
                "plugin_identity": identity,
            }
            created = repo.create_trading_runtime({
                "owner_user_id": "owner",
                "strategy_kind": "atomic",
                "strategy_id": "scripted",
                "strategy_version": second["version"],
                "strategy_snapshot": snapshot,
                "strategy_lineage": second["lineage"],
                "symbol": "TMF",
                "interval": "1m",
                "quantity": 1,
                "mode": "observe",
            })
            repo.close()

            restarted = SQLiteBarRepository(path)
            try:
                restored = restarted.trading_runtime(
                    created["runtime_id"], "owner"
                )
                self.assertEqual(first["version"], 1)
                self.assertEqual(restored["strategy_version"], 2)
                self.assertEqual(
                    restored["strategy_snapshot"]["atomic_strategy_version"],
                    2,
                )
                self.assertEqual(
                    restored["strategy_lineage"]["strategy_version"], 2
                )
                self.assertEqual(
                    restored["strategy_snapshot"]["plugin_identity"],
                    identity,
                )
                services.require_snapshot(restored["strategy_snapshot"])
                changed_provider = RegistryStrategies(provider=InertProvider(
                    fields=(ParameterField("input", ParameterKind.NUMBER),),
                    strategy_version="0.2.1",
                ))
                with self.assertRaises(StrategyUnavailable):
                    changed_provider.require_snapshot(
                        restored["strategy_snapshot"]
                    )
            finally:
                restarted.close()

    def test_composite_snapshots_still_require_provider_semantic_versions(self):
        provider = InertProvider(
            capabilities={
                StrategyCapability.COMPOSITE_ANALYZE,
                StrategyCapability.COMPOSITE_EVALUATE,
                StrategyCapability.NORMALIZE_PARAMETERS,
                StrategyCapability.PARAMETER_TEMPLATE,
            },
            strategy_version="0.2.0",
        )
        services = RegistryStrategies(provider=provider)
        evaluator = {
            "strategy": "scripted",
            "strategy_version": "0.2.0",
            "parameter_schema_version": "1",
            "parameters": {},
        }
        member = {"member_id": "member", **evaluator}
        definition = {
            "composite_id": "composite",
            "composite_version": 1,
            "evaluator": evaluator,
            "members": [member],
        }
        services.composite_request(definition, bars(), analysis=False)
        for location in ("evaluator", "member"):
            changed = dict(definition)
            if location == "evaluator":
                changed["evaluator"] = dict(
                    evaluator, strategy_version="0.2.1"
                )
            else:
                changed["members"] = [dict(
                    member, strategy_version="0.2.1"
                )]
            with self.subTest(location=location), self.assertRaises(
                StrategyUnavailable
            ):
                services.composite_request(changed, bars(), analysis=False)

    def test_optional_protocol_absence_and_result_identity_mismatch_fail_closed(self):
        for capability, call in (
            (
                StrategyCapability.NORMALIZE_PARAMETERS,
                lambda services: services.normalize("scripted", {}),
            ),
            (
                StrategyCapability.PARAMETER_TEMPLATE,
                lambda services: services.template("scripted"),
            ),
        ):
            services = RegistryStrategies(
                provider=IncompleteOptionalProvider(capability)
            )
            with self.assertRaises(StrategyUnavailable):
                call(services)
        other = InertProvider("other").reference
        services = RegistryStrategies(
            provider=InertProvider(result_reference=other)
        )
        with self.assertRaises(StrategyUnavailable):
            services.normalize("scripted", {})
        with self.assertRaises(StrategyUnavailable):
            services.template("scripted")

    def test_ambiguous_versions_and_invalid_digest_are_rejected(self):
        provider = InertProvider()
        provider.descriptors = lambda: (provider.descriptor, replace(provider.descriptor,
            reference=StrategyReference(provider.identity, StrategyIdentity("scripted", "2"))))
        with self.assertRaises(StrategyUnavailable): RegistryStrategies(provider=provider)
        provider = InertProvider()
        provider.identity = replace(provider.identity, artifact_digest="sha256:short")
        provider.reference = replace(provider.reference, plugin=provider.identity)
        provider.descriptor = replace(provider.descriptor, reference=provider.reference)
        with self.assertRaises(StrategyUnavailable): RegistryStrategies(provider=provider)

    def test_generic_backtest_and_unsupported_analysis_options(self):
        provider = InertProvider(); services = RegistryStrategies(provider=provider)
        with strategy_scope(services):
            result = run_strategy_backtest(bars(), "scripted", bars()[0].trading_date, bars()[0].trading_date)
            self.assertEqual(result["trades"], [])
            self.assertEqual(result["config"]["parameters"], {"force_close_last": True})
            self.assertNotIn("opening_range_minutes", result["config"])
            self.assertEqual(services.evaluate(bars(), "scripted"), [])
            with self.assertRaises(StrategyUnavailable): services.analyze(bars(), interval="5m")
            with self.assertRaises(StrategyUnavailable):
                run_composite_backtest(bars(), {}, "sample", 1, bars()[0].trading_date, bars()[0].trading_date)
        with self.assertRaises(StrategyUnavailable): supported_strategies()

    def test_composite_evaluation_and_analysis_use_exact_core_contracts(self):
        provider=InertProvider(capabilities={
            StrategyCapability.EVALUATE,
            StrategyCapability.COMPOSITE_EVALUATE,
            StrategyCapability.COMPOSITE_ANALYZE,
            StrategyCapability.NORMALIZE_PARAMETERS,
            StrategyCapability.PARAMETER_TEMPLATE,
        })
        services=RegistryStrategies(provider=provider)
        member=CompositeMember("member",provider.reference,"1",{})
        request=CompositeEvaluationRequest(provider.reference,"1",{},"instance",1,(member,),tuple(bars()))
        self.assertEqual(services.evaluate_composite(request),[])
        analysis=CompositeAnalysisRequest(
            provider.reference,"1",{},"instance",1,(member,),tuple(bars())
        )
        self.assertEqual(services.analyze_composite(analysis)["signals"],[])
        with self.assertRaises(ValueError):
            services.evaluate_composite(replace(request,members=(replace(member,parameter_schema_version="unknown"),)))
        with self.assertRaises(StrategyUnavailable):services.evaluate_composite({})
        without_port=RegistryStrategies(
            provider=IncompleteOptionalProvider(
                StrategyCapability.COMPOSITE_ANALYZE
            )
        )
        unavailable_reference = without_port.reference("scripted")
        bad_member=CompositeMember("member",unavailable_reference,"1",{})
        with self.assertRaises(StrategyUnavailable):
            without_port.analyze_composite(
                CompositeAnalysisRequest(
                    unavailable_reference,
                    "1",
                    {},
                    "instance",
                    1,
                    (bad_member,),
                    tuple(bars()),
                )
            )

    def test_api_scopes_multiple_injections_and_missing_plugin(self):
        with tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
            apps=[]; clients=[]
            for key in (None, "one", "two"):
                provider = InertProvider(key) if key else None
                app=create_app(LiveSettings(db_path=str(Path(temp)/(str(key)+".sqlite3"))),
                               feed=InertFeed(), strategy_provider=provider,
                               demo_provider=InertDemoProvider(provider) if key == "one" else None)
                apps.append(app); clients.append(stack.enter_context(TestClient(app)))
            self.assertEqual(clients[0].get("/api/strategies").json()["strategies"], [])
            self.assertEqual(clients[0].get("/api/strategy-signals").status_code, 503)
            self.assertEqual(clients[0].get("/api/composite-strategies").status_code, 503)
            for index,key in ((1,"one"),(2,"two"),(1,"one")):
                self.assertEqual(clients[index].get("/api/strategies").json()["strategies"][0]["key"],key)
                self.assertEqual(apps[index].state.api_dependencies.strategy_app.catalog("owner")["strategies"][0]["key"],key)
            self.assertIs(apps[1].state.trading_runtime.on_bar, apps[1].state.trading_runtime.on_bar)
            self.assertEqual(get_strategy_services().catalog(), [])
            self.assertEqual(clients[0].get("/health/live").status_code, 200)
            self.assertEqual(clients[0].get("/api/demo/cases").status_code, 503)
            self.assertEqual(
                clients[1].get("/api/demo/cases").json()["cases"][0]["case_id"],
                "inert-case",
            )
            response = clients[1].post(
                "/api/demo/backtests", json={"case_id": "inert-case"}
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["case_id"], "inert-case")

    def test_sqlite_immutable_versions_portfolios_risk_and_owner_isolation(self):
        services=RegistryStrategies(provider=InertProvider()); identity=services.identity_snapshot()
        with tempfile.TemporaryDirectory() as temp:
            repo=SQLiteBarRepository(Path(temp)/"runtime.sqlite3")
            try:
                first=repo.ensure_strategy_version("scripted", {}, identity, "a")
                second=repo.ensure_strategy_version("scripted", {"input":7}, identity, "a")
                self.assertEqual((first["version"],second["version"]),(1,2))
                self.assertIsNone(repo.strategy_version("scripted",1,"b"))
                with self.assertRaises(ValueError): repo.ensure_strategy_version("bad", {}, {}, "a")
                with self.assertRaises(ValueError): known_plugin_lineage(dict(identity,artifact_digest="sha256:short"))
                portfolio=repo.save_portfolio_version("p","Example",[{"kind":"atomic","key":"scripted","version":1,"weight":1}],"a")
                self.assertIsNone(repo.portfolio("p",portfolio["version"],"b"))
                risk=repo.save_risk_profile_version("r","Example","paper",{"max_daily_loss":100},"a")
                binding=repo.bind_risk_profile("r",risk["version"],{"kind":"atomic","key":"scripted","version":1},"a")
                self.assertFalse(binding["live_authorized"])
                with self.assertRaises(ValueError): repo.bind_risk_profile("r",1,{"kind":"atomic","key":"scripted","version":1},"b")
            finally: repo.close()

    def test_manual_paper_and_replay_keep_risk_isolation_and_rewind(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"paper.sqlite3"
            service=PaperTradingService(SQLitePaperRepository(path))
            bar=bars()[0]; service.on_bar(bar)
            try:
                order,created=service.submit(trader("a"),PaperOrderCommand("manual",1,"buy",1,19950),
                    idempotency_key="first",market_bar=bar,occurred_at=bar.received_time)
                self.assertTrue(created);self.assertEqual(order["status"],"filled")
                self.assertEqual(service.positions("b"),[])
                service.activate_kill_switch("a","test")
                blocked,_=service.submit(trader("a"),PaperOrderCommand("manual",1,"buy",1,19950),
                    idempotency_key="blocked",market_bar=bar,occurred_at=bar.received_time)
                self.assertEqual(blocked["status"],"rejected")
            finally: service.close()
            restored=PaperTradingService(SQLitePaperRepository(path))
            self.assertTrue(restored.recovery.healthy);self.assertEqual(len(restored.positions("a")),1)
            restored.close()
            registry=ReplayTradingSessionRegistry()
            try:
                session=registry.create("snapshot",trader("a"),bars())
                other=registry.create("snapshot",trader("b"),bars())
                with self.assertRaises(ReplaySessionNotFound):registry.get(session.session_id,"b")
                _,_,state=session.submit(PaperOrderCommand("manual",1,"buy",1,19950),idempotency_key="replay")
                self.assertEqual(len(state["positions"]),1);self.assertEqual(other.state()["positions"],[])
                session.seek(2);self.assertEqual(session.seek(0)["positions"],[])
            finally:registry.close()

    def test_auto_paper_missing_plugin_or_version_blocks_before_order_write(self):
        with tempfile.TemporaryDirectory() as temp:
            services=RegistryStrategies(provider=InertProvider())
            service=PaperTradingService(SQLitePaperRepository(Path(temp)/"paper.sqlite3"), strategy_services=services)
            try:
                command=PaperOrderCommand("scripted",1,"buy",1,19950,order_source="strategy_auto",runtime_id="r",decision_id="d")
                bar=bars()[0]
                with self.assertRaises(ValueError):service.submit(trader("a"),command,idempotency_key="missing",market_bar=bar)
                snapshot={"strategy":"scripted","parameters":{},"atomic_strategy_version":2,"plugin_identity":services.identity_snapshot()}
                with self.assertRaises(ValueError):service.submit(trader("a"),replace(command,strategy_snapshot=snapshot),idempotency_key="version",market_bar=bar)
                snapshot["atomic_strategy_version"]=1
                snapshot["parameters"]={"undeclared":1}
                with self.assertRaises(ValueError):service.submit(trader("a"),replace(command,strategy_snapshot=snapshot),idempotency_key="schema",market_bar=bar)
                snapshot["parameters"]={}
                service.strategy_services=RegistryStrategies(provider=InertProvider(capabilities={StrategyCapability.ANALYZE}))
                with self.assertRaises(ValueError):service.submit(trader("a"),replace(command,strategy_snapshot=snapshot),idempotency_key="capability",market_bar=bar)
                self.assertEqual(service.orders("a"),[])
            finally:service.close()

    def test_auto_paper_rechecks_plugin_contract_before_pending_fill(self):
        with tempfile.TemporaryDirectory() as temp:
            services=RegistryStrategies(provider=InertProvider())
            service=PaperTradingService(SQLitePaperRepository(Path(temp)/"paper.sqlite3"),strategy_services=services)
            try:
                snapshot={"strategy":"scripted","parameters":{},"atomic_strategy_version":1,"plugin_identity":services.identity_snapshot()}
                command=PaperOrderCommand("scripted",1,"buy",1,19950,order_source="strategy_auto",
                    runtime_id="r",decision_id="d",execution_timing="next_bar_open",strategy_snapshot=snapshot)
                first=bars()[0]
                order,_=service.submit(trader("a"),command,idempotency_key="pending",market_bar=first,occurred_at=first.received_time)
                self.assertEqual(order["status"],"approved")
                service.strategy_services=RegistryStrategies()
                service.on_bar(bars()[1])
                self.assertEqual(service.positions("a"),[])
                self.assertEqual(service.fills("a"),[])
                self.assertEqual(service.orders("a")[0]["status"],"rejected")
            finally:service.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)

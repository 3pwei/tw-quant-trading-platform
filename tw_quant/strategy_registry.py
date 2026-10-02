"""Core-only plugin boundary. Runtime composition injects approved providers.

No discovery, installation, default parameters or evaluator lives in this module.
Context binding carries one immutable registry through HTTP and bar callbacks.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict
from functools import wraps
import re
from uuid import uuid4

from tw_quant_core.strategy import (
    CompositeAnalysisRequest, CompositeEvaluationRequest, CompositeMember,
    DiagnosticRecord, ParameterNormalizationRequest,
    ParameterNormalizationResult, ParameterTemplateRequest,
    ParameterTemplateResult, StrategyAnalysisResult,
    StrategyCapability, StrategyEvaluationRequest, StrategyEvaluationResult,
    StrategyRegistry, StrategyReference,
)


class StrategyUnavailable(ValueError):
    """No approved exact plugin contract can satisfy the requested operation."""


class RegistryStrategies:
    def __init__(self, registry=None, *, provider=None):
        if registry is not None and provider is not None:
            raise ValueError("inject a registry or a provider, not both")
        self.registry = registry if registry is not None else StrategyRegistry()
        if provider is not None:
            self.registry.register(provider)
        self.registry.freeze()
        self._references = {}
        for reference, descriptor in self.registry.descriptors.items():
            key = reference.strategy.strategy_id
            if key != key.lower() or key in self._references:
                raise StrategyUnavailable("ambiguous strategy key or implementation version")
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", reference.plugin.artifact_digest):
                raise StrategyUnavailable("plugin requires exact sha256 artifact identity")
            self._references[key] = reference
        self.keys = tuple(sorted(self._references))

    def reference(self, key):
        try:
            reference = self._references[key]
        except KeyError as exc:
            raise StrategyUnavailable("strategy plugin/version is unavailable") from exc
        self.registry.descriptor(reference)
        return reference

    def require_capability(self, key, capability):
        reference = self.reference(key)
        if capability not in self.registry.descriptor(reference).capabilities:
            raise StrategyUnavailable("requested strategy capability is unavailable")
        return reference

    def request(self, key, bars, parameters, *, schema_version=None):
        reference = self.reference(key)
        schema = self.registry.descriptor(reference).parameter_schema.schema_version
        if schema_version is not None and schema_version != schema:
            raise StrategyUnavailable("parameter schema version mismatch")
        return StrategyEvaluationRequest(reference, schema, parameters, tuple(bars))

    def normalize(self, key, values=None, *, schema_version=None):
        self.require_capability(key, StrategyCapability.NORMALIZE_PARAMETERS)
        reference = self.reference(key)
        descriptor = self.registry.descriptor(reference)
        schema = descriptor.parameter_schema.schema_version
        if schema_version is not None and schema_version != schema:
            raise StrategyUnavailable("parameter schema version mismatch")
        request = ParameterNormalizationRequest(reference, schema, dict(values or {}))
        try:
            result = self.registry.normalize_parameters(request)
        except RuntimeError as exc:
            raise StrategyUnavailable(
                "parameter normalization capability is unavailable"
            ) from exc
        if not isinstance(result, ParameterNormalizationResult):
            raise StrategyUnavailable("invalid parameter normalization result")
        return dict(result.normalized_parameters)

    def template(self, key, *, schema_version=None):
        self.require_capability(key, StrategyCapability.PARAMETER_TEMPLATE)
        reference = self.reference(key)
        descriptor = self.registry.descriptor(reference)
        schema = descriptor.parameter_schema.schema_version
        if schema_version is not None and schema_version != schema:
            raise StrategyUnavailable("parameter schema version mismatch")
        try:
            result = self.registry.parameter_template(
                ParameterTemplateRequest(reference, schema)
            )
        except RuntimeError as exc:
            raise StrategyUnavailable(
                "parameter template capability is unavailable"
            ) from exc
        if not isinstance(result, ParameterTemplateResult):
            raise StrategyUnavailable("invalid parameter template result")
        return dict(result.template_parameters)

    def validate(self, key, values=None):
        # Compatibility entry point: canonical values always come from the
        # provider-owned Core v1.2 normalization result.
        return self.normalize(key, values)

    def catalog(self, overrides=None):
        result = []
        for key in self.keys:
            descriptor = self.registry.descriptor(self.reference(key))
            values = (
                self.normalize(key, (overrides or {})[key])
                if key in (overrides or {})
                else self.template(key)
            )
            result.append({
                "key": key, "name": descriptor.display_name,
                "reference": asdict(descriptor.reference),
                "parameter_schema": asdict(descriptor.parameter_schema),
                "capabilities": sorted(item.value for item in descriptor.capabilities),
                "parameters": values,
            })
        return result

    def identity_snapshot(self):
        # Compatibility consumers without a key may only use one artifact/schema.
        # Mixed catalogs fail closed rather than guessing a persistence identity.
        descriptors = tuple(self.registry.descriptors.values())
        identities = {(item.reference.plugin, item.parameter_schema.schema_version)
                      for item in descriptors}
        if len(identities) != 1:
            raise StrategyUnavailable("an unambiguous plugin/schema binding is required")
        identity, schema = next(iter(identities))
        return {**asdict(identity), "parameter_schema_version": schema,
                "strategy_versions": {key: self.reference(key).strategy.strategy_version
                                      for key in self.keys}}

    def require_snapshot(self, snapshot):
        if snapshot.get("plugin_identity") != self.identity_snapshot():
            raise StrategyUnavailable("runtime plugin/version/schema identity mismatch")
        if snapshot.get("strategy") not in self._references:
            raise StrategyUnavailable("runtime requires an exact atomic strategy binding")
        # SQLite configuration lineage is independent of the provider semantic
        # version, which is already bound exactly by plugin_identity above.
        configuration_version = snapshot.get("atomic_strategy_version")
        if configuration_version is not None and (
            isinstance(configuration_version, bool)
            or not isinstance(configuration_version, int)
            or configuration_version < 1
        ):
            raise StrategyUnavailable("runtime configuration version is invalid")
        parameters = snapshot.get("parameters")
        if not isinstance(parameters, dict):
            raise StrategyUnavailable("canonical parameter snapshot is required")
        if self.normalize(snapshot["strategy"], parameters) != parameters:
            raise StrategyUnavailable(
                "runtime parameters are not the provider canonical snapshot"
            )

    def analyze(self, bars, selected=None, *, force_close_last=False,
                parameters=None, interval="1m"):
        requested = tuple(dict.fromkeys(self.keys if selected is None else selected))
        if not requested:
            raise StrategyUnavailable("analysis plugin is unavailable")
        bars = tuple(bars)
        results = []
        for key in requested:
            self.require_capability(key, StrategyCapability.ANALYZE)
            configured = (parameters or {}).get(key)
            values = (
                self.template(key) if configured is None else dict(configured)
            )
            fields = {item.name for item in self.registry.descriptor(
                self.reference(key)).parameter_schema.fields}
            for name, value, needed in (("interval", interval, interval != "1m"),
                                        ("force_close_last", force_close_last, force_close_last)):
                if name in fields:
                    values[name] = value
                elif needed:
                    raise StrategyUnavailable("requested analysis option is not declared by the plugin")
            values = self.normalize(key, values)
            try:
                result = self.registry.analyze(self.request(key, bars, values))
            except RuntimeError as exc:
                raise StrategyUnavailable("analysis plugin contract is unavailable") from exc
            results.append(self.analysis_payload(result, values))
        return {"strategies": results}

    def analyze_request(self, request):
        if not isinstance(request, StrategyEvaluationRequest):
            raise StrategyUnavailable("exact Core analysis request is required")
        self.require_capability(
            request.reference.strategy.strategy_id,
            StrategyCapability.ANALYZE,
        )
        try:
            result = self.registry.analyze(request)
        except RuntimeError as exc:
            raise StrategyUnavailable(
                "analysis capability is unavailable"
            ) from exc
        return self.analysis_payload(result, request.parameters)

    def analysis_payload(self, result, parameters):
        if not isinstance(result, StrategyAnalysisResult):
            raise StrategyUnavailable("versioned analysis response is required")
        records = tuple(result.diagnostics)
        if not records or not all(isinstance(item, DiagnosticRecord) for item in records):
            raise StrategyUnavailable("versioned diagnostics are required")
        platform = [
            item for item in records
            if (item.diagnostic_id, item.schema_version) == ("platform-analysis", "1")
        ]
        if len(platform) != 1:
            raise StrategyUnavailable("analysis diagnostic schema is unavailable")
        payload = dict(platform[0].payload)
        if not isinstance(payload.get("signals"), (tuple, list)):
            raise StrategyUnavailable("analysis signals contract is unavailable")
        descriptor = self.registry.descriptor(result.reference)
        return {**payload, "key": result.reference.strategy.strategy_id,
                "name": descriptor.display_name, "parameters": dict(parameters),
                "reference": asdict(result.reference),
                "diagnostics": [{
                    "diagnostic_id": item.diagnostic_id,
                    "schema_version": item.schema_version,
                    "payload": dict(item.payload),
                } for item in records]}

    @staticmethod
    def intents(result):
        if not isinstance(result, StrategyEvaluationResult):
            raise StrategyUnavailable("evaluation result contract is unavailable")
        return [{**dict(item.attributes),
                 "action": "entry" if item.action == "enter" else "exit",
                 "direction": item.direction, "reason": item.reason}
                for item in result.intents if item.action != "hold"]

    def evaluate(self, bars, key, *, parameters=None, interval="1m"):
        self.require_capability(key, StrategyCapability.EVALUATE)
        values = self.template(key) if parameters is None else dict(parameters)
        fields = {item.name for item in self.registry.descriptor(
            self.reference(key)).parameter_schema.fields}
        if "interval" in fields:
            values["interval"] = interval
        elif interval != "1m":
            raise StrategyUnavailable("requested interval is not declared by the plugin")
        values = self.normalize(key, values)
        try:
            result = self.registry.evaluate(self.request(key, bars, values))
        except RuntimeError as exc:
            raise StrategyUnavailable("evaluation plugin contract is unavailable") from exc
        return self.intents(result)

    def evaluate_composite(self, request):
        if not isinstance(request, CompositeEvaluationRequest):
            raise StrategyUnavailable("exact Core composite request is required")
        self.require_capability(request.evaluator.strategy.strategy_id,
                                StrategyCapability.COMPOSITE_EVALUATE)
        try:
            result = self.registry.evaluate_composite(request)
        except RuntimeError as exc:
            raise StrategyUnavailable("composite plugin contract is unavailable") from exc
        return self.intents(result)

    def analyze_composite(self, request):
        if not isinstance(request, CompositeAnalysisRequest):
            raise StrategyUnavailable("exact Core composite analysis request is required")
        self.require_capability(
            request.evaluator.strategy.strategy_id,
            StrategyCapability.COMPOSITE_ANALYZE,
        )
        try:
            result = self.registry.analyze_composite(request)
        except RuntimeError as exc:
            raise StrategyUnavailable(
                "composite analysis capability is unavailable"
            ) from exc
        return self.analysis_payload(result, request.evaluator_parameters)

    def _snapshot(self, raw, *, member=False):
        if not isinstance(raw, dict):
            raise StrategyUnavailable("composite reference snapshot is invalid")
        allowed = {
            "strategy", "strategy_version",
            "parameter_schema_version", "parameters",
        } | ({"member_id"} if member else set())
        if set(raw) != allowed:
            raise StrategyUnavailable("composite reference snapshot is invalid")
        if member and str(raw["member_id"]).strip() == "":
            raise StrategyUnavailable("composite member identity is required")
        key = str(raw["strategy"]).strip().lower()
        reference = self.reference(key)
        if str(raw["strategy_version"]) != reference.strategy.strategy_version:
            raise StrategyUnavailable("composite strategy version mismatch")
        schema = self.registry.descriptor(reference).parameter_schema.schema_version
        if str(raw["parameter_schema_version"]) != schema:
            raise StrategyUnavailable("composite parameter schema mismatch")
        parameters = self.normalize(
            key, raw["parameters"], schema_version=schema
        )
        if parameters != dict(raw["parameters"]):
            raise StrategyUnavailable(
                "composite parameters are not the provider canonical snapshot"
            )
        return reference, schema, parameters

    def composite_request(self, definition, bars, *, analysis):
        if not isinstance(definition, dict):
            raise StrategyUnavailable("composite definition is invalid")
        allowed = {
            "composite_id", "composite_version", "evaluator", "members"
        }
        if set(definition) - {"name", "description"} != allowed:
            raise StrategyUnavailable(
                "unknown or nested composite definition is unavailable"
            )
        evaluator, schema, parameters = self._snapshot(
            definition["evaluator"]
        )
        members = []
        raw_members = definition["members"]
        if not isinstance(raw_members, list) or not raw_members:
            raise StrategyUnavailable("composite members are unavailable")
        for raw in raw_members:
            reference, member_schema, member_parameters = self._snapshot(
                raw, member=True
            )
            members.append(
                CompositeMember(
                    str(raw["member_id"]),
                    reference,
                    member_schema,
                    member_parameters,
                )
            )
        request_type = (
            CompositeAnalysisRequest if analysis else CompositeEvaluationRequest
        )
        return request_type(
            evaluator,
            schema,
            parameters,
            str(definition["composite_id"]),
            int(definition["composite_version"]),
            tuple(members),
            tuple(bars),
        )

    def normalize_composite_definition(self, raw):
        # Construction validates exact references, schemas and provider-owned
        # canonical parameters. No ALL/ANY or nested evaluator semantics live
        # in the Platform.
        request = self.composite_request(raw, (), analysis=True)
        return {
            **({"name": str(raw["name"]).strip(), "description": str(raw.get("description", ""))} if "name" in raw else {}),
            "composite_id": request.composite_id,
            "composite_version": request.composite_version,
            "evaluator": {
                "strategy": request.evaluator.strategy.strategy_id,
                "strategy_version": request.evaluator.strategy.strategy_version,
                "parameter_schema_version":
                    request.evaluator_parameter_schema_version,
                "parameters": dict(request.evaluator_parameters),
            },
            "members": [
                {
                    "member_id": member.member_id,
                    "strategy": member.strategy.strategy.strategy_id,
                    "strategy_version":
                        member.strategy.strategy.strategy_version,
                    "parameter_schema_version":
                        member.parameter_schema_version,
                    "parameters": dict(member.parameters),
                }
                for member in request.members
            ],
        }

    def composite_template(self):
        candidates = [
            key for key in self.keys
            if StrategyCapability.COMPOSITE_ANALYZE
            in self.registry.descriptor(self.reference(key)).capabilities
        ]
        if len(candidates) != 1:
            raise StrategyUnavailable(
                "an exact composite evaluator template is unavailable"
            )
        key = candidates[0]
        reference = self.reference(key)
        schema = self.registry.descriptor(reference).parameter_schema.schema_version
        return {
            "name": "", "description": "",
            "composite_id": "",
            "composite_version": 1,
            "evaluator": {
                "strategy": key,
                "strategy_version": reference.strategy.strategy_version,
                "parameter_schema_version": schema,
                "parameters": self.template(key, schema_version=schema),
            },
            "members": [],
        }

    def scope_service(self, service):
        return _ScopedService(self, service)


class _ScopedService:
    def __init__(self, strategies, service):
        self._strategies, self._service = strategies, service
        self._methods = {}

    def __getattr__(self, name):
        value = getattr(self._service, name)
        if not callable(value):
            return value
        if name in self._methods:
            return self._methods[name]
        @wraps(value)
        def call(*args, **kwargs):
            with strategy_scope(self._strategies):
                return value(*args, **kwargs)
        self._methods[name] = call
        return call


_CURRENT = ContextVar("strategy_services", default=None)


@contextmanager
def strategy_scope(services):
    token = _CURRENT.set(services)
    try:
        yield services
    finally:
        _CURRENT.reset(token)


def get_strategy_services():
    return _CURRENT.get() or RegistryStrategies()


class StrategyScopeMiddleware:
    def __init__(self, app, *, services):
        self.app, self.services = app, services

    async def __call__(self, scope, receive, send):
        with strategy_scope(self.services):
            await self.app(scope, receive, send)


def supported_strategies():
    keys = get_strategy_services().keys
    if not keys:
        raise StrategyUnavailable("strategy plugin is unavailable")
    return keys


def analyze_strategies(bars, selected=None, **kwargs):
    return get_strategy_services().analyze(bars, selected, **kwargs)


def evaluate_strategy_intents(bars, strategy, **kwargs):
    return get_strategy_services().evaluate(bars, strategy, **kwargs)


def strategy_catalog(overrides=None):
    return get_strategy_services().catalog(overrides)


def validate_strategy_parameters(strategy, values=None):
    return get_strategy_services().validate(strategy, values)


def default_strategy_parameters(strategy):
    return get_strategy_services().template(strategy)


def _provider_composite_signals(bars, definition):
    services = get_strategy_services()
    request = services.composite_request(definition, bars, analysis=True)
    result = services.analyze_composite(request)
    return result["signals"], result["diagnostics"]


generate_composite_signals = _provider_composite_signals


def evaluate_composite_intents(bars, definition):
    services = get_strategy_services()
    request = services.composite_request(definition, bars, analysis=False)
    return services.evaluate_composite(request)


def validate_composite_definition(raw, *_args, **_kwargs):
    return get_strategy_services().normalize_composite_definition(raw)


def validate_composite_dependencies(definition, *_args, **_kwargs):
    get_strategy_services().composite_request(definition, (), analysis=True)


def default_composite_definition():
    return get_strategy_services().composite_template()


def new_composite_id():
    return uuid4().hex

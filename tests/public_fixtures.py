"""Candidate-only synthetic fixtures. No market-dependent evaluator is defined."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from datetime import datetime, timedelta
from pathlib import Path
import csv
import tempfile
import unittest

from tw_quant_core.strategy import (
    DiagnosticRecord, ParameterField, ParameterKind, ParameterSchemaMetadata,
    ParameterNormalizationResult, ParameterTemplateResult, ParameterValidationResult,
    PluginArtifactIdentity, StrategyAnalysisResult, StrategyCapability,
    StrategyDescriptor, StrategyEvaluationResult, StrategyIdentity, StrategyIntent,
    StrategyReference,
)
from tw_quant.strategy_registry import RegistryStrategies, strategy_scope


class InertProvider:
    """Always hold/empty diagnostics, independent of prices. Values are explicit test data."""
    def __init__(self, *, composite=False):
        identity = PluginArtifactIdentity("inert-scripted", "1", "inert-double", "1", "sha256:" + "a" * 64)
        self.identity = identity
        self.reference = StrategyReference(identity, StrategyIdentity("scripted", "1"))
        self.values = {"window": 15, "interval": "1m", "force_close_last": False,
                       "stop_loss_pct": 0.006, "take_profit_pct": 0.012}
        capabilities = {StrategyCapability.EVALUATE, StrategyCapability.ANALYZE,
            StrategyCapability.NORMALIZE_PARAMETERS, StrategyCapability.PARAMETER_TEMPLATE}
        if composite:
            capabilities.update({StrategyCapability.COMPOSITE_EVALUATE, StrategyCapability.COMPOSITE_ANALYZE})
        self.descriptor = StrategyDescriptor(self.reference, "Scripted test response", ParameterSchemaMetadata("1", (
            ParameterField("window", ParameterKind.INTEGER, required=False),
            ParameterField("interval", ParameterKind.STRING, required=False),
            ParameterField("force_close_last", ParameterKind.BOOLEAN, required=False),
            ParameterField("stop_loss_pct", ParameterKind.NUMBER, required=False),
            ParameterField("take_profit_pct", ParameterKind.NUMBER, required=False),
        )), frozenset(capabilities))

    def descriptors(self): return (self.descriptor,)
    def validate_parameters(self, reference, schema, parameters):
        errors = () if set(parameters) <= set(self.values) else ("unknown field",)
        return ParameterValidationResult(not errors, errors)
    def normalize_parameters(self, request):
        return ParameterNormalizationResult(request.reference, request.parameter_schema_version,
            request.supplied_parameters, self.values | dict(request.supplied_parameters))
    def parameter_template(self, request):
        return ParameterTemplateResult(request.reference, request.parameter_schema_version, self.values)
    def evaluate(self, request):
        return StrategyEvaluationResult(request.reference, (StrategyIntent("hold", "scripted hold"),))
    def analyze(self, request):
        return StrategyAnalysisResult(request.reference, (DiagnosticRecord("platform-analysis", "1", {"signals": []}),))
    def evaluate_composite(self, request):
        return StrategyEvaluationResult(request.evaluator, (StrategyIntent("hold", "scripted hold"),))
    def analyze_composite(self, request):
        return StrategyAnalysisResult(request.evaluator, (DiagnosticRecord("platform-analysis", "1", {"signals": []}),))


def services(): return RegistryStrategies(provider=InertProvider())


def generic_definition():
    """Opaque repository snapshot, never evaluated or interpreted as a trading rule."""
    return {"name": "test snapshot", "members": [{"strategy": "scripted", "window": 1}]}


_TEMP = ContextVar("public_test_temporary", default=None)


@contextmanager
def test_context():
    with tempfile.TemporaryDirectory(prefix="public-test-") as temporary:
        token = _TEMP.set(Path(temporary))
        try:
            with strategy_scope(services()): yield
        finally:
            _TEMP.reset(token)


class PublicTestCase(unittest.TestCase):
    def run(self, result=None):
        with test_context(): return super().run(result)


class PublicAsyncTestCase(unittest.IsolatedAsyncioTestCase):
    def run(self, result=None):
        with test_context():
            self._asyncioTestContext = copy_context()
            return super().run(result)


def synthetic_csv():
    root = _TEMP.get()
    if root is None: raise RuntimeError("synthetic CSV requires a temporary test context")
    path = root / "synthetic.csv"
    if not path.exists():
        with path.open("w", newline="", encoding="utf-8") as output:
            writer = csv.DictWriter(output, fieldnames=("exchange_time", "symbol", "contract", "price", "volume", "sequence"))
            writer.writeheader()
            anchor = datetime.fromisoformat("2026-08-24T15:00:00+08:00")
            for index in range(100):
                writer.writerow({"exchange_time": (anchor + timedelta(seconds=index*10)).isoformat(),
                    "symbol": "TMF", "contract": "TMFTEST", "price": 100, "volume": 1, "sequence": str(index)})
    return path

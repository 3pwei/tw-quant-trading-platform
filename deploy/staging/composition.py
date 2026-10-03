"""Generic staging composition boundary for one exact, pre-verified provider.

The provider module and factory name live in an owner-only mounted file.  This
Public module never discovers packages, downloads code, or contains a fallback
evaluator.  It constructs exactly one configured provider with the artifact
digest already verified by the candidate build.
"""
from __future__ import annotations

from dataclasses import dataclass
import importlib
import os
from pathlib import Path
import re

from tw_quant_core.strategy import DiagnosticRecord, StrategyAnalysisResult


FACTORY_PATH = Path(
    os.environ.get("STAGING_PROVIDER_FACTORY_FILE", "/run/staging-provider/factory")
)


class ProviderCompositionError(RuntimeError):
    """The exact private provider cannot be composed safely."""


@dataclass(frozen=True)
class FactoryReference:
    module: str
    attribute: str

    @classmethod
    def parse(cls, value: str) -> "FactoryReference":
        if not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*", value
        ):
            raise ProviderCompositionError("provider factory reference is invalid")
        module, attribute = value.split(":", 1)
        return cls(module, attribute)


class AnalysisEnvelopeAdapter:
    """Delegate calculations and project only the generic analysis envelope."""

    def __init__(self, provider):
        self._provider = provider

    @property
    def identity(self):
        return self._provider.identity

    def descriptors(self):
        return self._provider.descriptors()

    def validate_parameters(self, reference, schema, parameters):
        return self._provider.validate_parameters(reference, schema, parameters)

    def normalize_parameters(self, request):
        return self._provider.normalize_parameters(request)

    def parameter_template(self, request):
        return self._provider.parameter_template(request)

    def evaluate(self, request):
        return self._provider.evaluate(request)

    def evaluate_composite(self, request):
        return self._provider.evaluate_composite(request)

    @staticmethod
    def _project(result: StrategyAnalysisResult, expected: str):
        if not isinstance(result, StrategyAnalysisResult):
            raise ProviderCompositionError("provider returned an invalid analysis envelope")
        if len(result.diagnostics) != 1:
            raise ProviderCompositionError("provider returned an ambiguous analysis envelope")
        diagnostic = result.diagnostics[0]
        if diagnostic.diagnostic_id != expected or diagnostic.schema_version != "1":
            raise ProviderCompositionError("provider diagnostic schema is unavailable")
        return StrategyAnalysisResult(
            result.reference,
            (DiagnosticRecord("platform-analysis", "1", dict(diagnostic.payload)),),
        )

    def analyze(self, request):
        return self._project(self._provider.analyze(request), "legacy-analysis")

    def analyze_composite(self, request):
        return self._project(
            self._provider.analyze_composite(request), "composite-analysis"
        )


def load_provider(*, factory_path: Path = FACTORY_PATH):
    digest = os.environ.get("PRIVATE_PROVIDER_WHEEL_SHA256", "").strip()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ProviderCompositionError("provider artifact digest is invalid")
    try:
        reference = FactoryReference.parse(factory_path.read_text().strip())
    except OSError as exc:
        raise ProviderCompositionError("provider factory is unavailable") from exc
    try:
        factory = getattr(importlib.import_module(reference.module), reference.attribute)
        provider = factory("sha256:" + digest)
    except Exception as exc:
        raise ProviderCompositionError("provider construction failed") from exc
    if getattr(provider, "identity", None) is None:
        raise ProviderCompositionError("provider identity is unavailable")
    if provider.identity.artifact_digest != "sha256:" + digest:
        raise ProviderCompositionError("provider artifact identity mismatch")
    return AnalysisEnvelopeAdapter(provider)


def create_app():
    from tw_quant.live.api import create_app as create_platform_app

    return create_platform_app(strategy_provider=load_provider())

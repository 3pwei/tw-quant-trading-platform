"""Provider-neutral compatibility surface for strategy analysis."""
from __future__ import annotations

from ..strategy_registry import analyze_strategies, supported_strategies


def analyze_live_strategies(*args, **kwargs):
    return analyze_strategies(*args, **kwargs)


def available_strategy_ids():
    return supported_strategies()


__all__ = [
    "analyze_live_strategies",
    "analyze_strategies",
    "available_strategy_ids",
]

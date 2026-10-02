#!/usr/bin/env python3
"""Fail CI on disallowed CodeQL SARIF findings without printing finding data."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
from typing import Any, Iterable


SECURITY_BANDS = (
    (9.0, "critical"),
    (7.0, "high"),
    (4.0, "medium"),
    (0.1, "low"),
    (0.0, "none"),
)
SECURITY_THRESHOLDS = {"critical": 9.0, "high": 7.0, "medium": 4.0, "low": 0.1}
QUALITY_LEVELS = {"none": 0, "note": 1, "warning": 2, "error": 3}


class SarifPolicyError(ValueError):
    """Raised when SARIF cannot be evaluated safely."""


def _mapping(value: Any, description: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SarifPolicyError(f"invalid {description}")
    return value


def _sequence(value: Any, description: str) -> list[Any]:
    if not isinstance(value, list):
        raise SarifPolicyError(f"invalid {description}")
    return value


def _rule_components(run: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    tool = _mapping(run.get("tool"), "run tool")
    driver = _mapping(tool.get("driver"), "tool driver")
    extensions = [
        _mapping(extension, "tool extension")
        for extension in _sequence(tool.get("extensions", []), "tool extensions")
    ]
    return driver, extensions


def _rules(component: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _mapping(rule, "reporting rule")
        for rule in _sequence(component.get("rules", []), "reporting rules")
    ]


def _resolve_rule(result: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    driver, extensions = _rule_components(run)
    reference = result.get("rule")
    rule_id = result.get("ruleId")
    index = result.get("ruleIndex")
    component = driver

    if reference is not None:
        reference = _mapping(reference, "result rule reference")
        rule_id = reference.get("id", rule_id)
        index = reference.get("index", index)
        component_reference = reference.get("toolComponent")
        if component_reference is not None:
            component_index = _mapping(
                component_reference, "tool component reference"
            ).get("index")
            if not isinstance(component_index, int) or component_index < 0:
                raise SarifPolicyError("invalid tool component index")
            if component_index >= len(extensions):
                raise SarifPolicyError("tool component index is out of range")
            component = extensions[component_index]

    candidates = _rules(component)
    if isinstance(index, int) and 0 <= index < len(candidates):
        rule = candidates[index]
        if rule_id is None or rule.get("id") == rule_id:
            return rule

    if isinstance(rule_id, str) and rule_id:
        for candidate_component in (driver, *extensions):
            for candidate in _rules(candidate_component):
                if candidate.get("id") == rule_id:
                    return candidate

    raise SarifPolicyError("result references an unknown rule")


def _security_score(rule: dict[str, Any]) -> float | None:
    properties = _mapping(rule.get("properties", {}), "rule properties")
    value = properties.get("security-severity")
    if value is None:
        return None
    try:
        score = float(value)
    except (TypeError, ValueError) as exc:
        raise SarifPolicyError("invalid security severity") from exc
    if not 0.0 <= score <= 10.0:
        raise SarifPolicyError("security severity is out of range")
    return score


def _security_band(score: float) -> str:
    return next(name for minimum, name in SECURITY_BANDS if score >= minimum)


def _quality_level(result: dict[str, Any], rule: dict[str, Any]) -> str:
    default = _mapping(rule.get("defaultConfiguration", {}), "rule configuration")
    properties = _mapping(rule.get("properties", {}), "rule properties")
    level = result.get("level") or default.get("level") or properties.get("problem.severity")
    if level is None:
        return "none"
    if level not in QUALITY_LEVELS:
        raise SarifPolicyError("invalid quality severity")
    return str(level)


def evaluate(
    documents: Iterable[dict[str, Any]],
    *,
    security_threshold: str,
    quality_threshold: str,
) -> tuple[Counter[str], int]:
    counts: Counter[str] = Counter()
    blocked = 0
    security_minimum = SECURITY_THRESHOLDS[security_threshold]
    quality_minimum = QUALITY_LEVELS[quality_threshold]

    for document in documents:
        if document.get("version") != "2.1.0":
            raise SarifPolicyError("unsupported SARIF version")
        runs = _sequence(document.get("runs"), "SARIF runs")
        if not runs:
            raise SarifPolicyError("SARIF contains no runs")
        for raw_run in runs:
            run = _mapping(raw_run, "SARIF run")
            for raw_result in _sequence(run.get("results", []), "SARIF results"):
                result = _mapping(raw_result, "SARIF result")
                rule = _resolve_rule(result, run)
                score = _security_score(rule)
                if score is not None:
                    band = _security_band(score)
                    counts[f"security_{band}"] += 1
                    blocked += score >= security_minimum
                    continue
                level = _quality_level(result, rule)
                counts[f"quality_{level}"] += 1
                blocked += QUALITY_LEVELS[level] >= quality_minimum

    return counts, blocked


def _load_documents(root: Path) -> list[dict[str, Any]]:
    paths = [root] if root.is_file() else sorted(root.rglob("*.sarif")) if root.is_dir() else []
    if not paths:
        raise SarifPolicyError("no SARIF files found")
    documents: list[dict[str, Any]] = []
    for path in paths:
        try:
            with path.open(encoding="utf-8") as handle:
                documents.append(_mapping(json.load(handle), "SARIF document"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SarifPolicyError("SARIF file is unreadable or malformed") from exc
    return documents


def _summary(counts: Counter[str], blocked: int) -> str:
    keys = (
        "security_critical",
        "security_high",
        "security_medium",
        "security_low",
        "security_none",
        "quality_error",
        "quality_warning",
        "quality_note",
        "quality_none",
    )
    details = " ".join(f"{key}={counts[key]}" for key in keys)
    return f"CodeQL SARIF policy: {details} blocked={blocked}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument(
        "--security-threshold", choices=tuple(SECURITY_THRESHOLDS), default="high"
    )
    parser.add_argument(
        "--quality-threshold", choices=tuple(QUALITY_LEVELS), default="error"
    )
    args = parser.parse_args(argv)
    try:
        counts, blocked = evaluate(
            _load_documents(args.input),
            security_threshold=args.security_threshold,
            quality_threshold=args.quality_threshold,
        )
    except SarifPolicyError as exc:
        print(f"CodeQL SARIF policy error: {exc}", file=sys.stderr)
        return 2
    print(_summary(counts, blocked))
    if blocked:
        print("CodeQL SARIF policy rejected blocking findings.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

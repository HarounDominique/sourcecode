from __future__ import annotations

from dataclasses import replace
from typing import Literal

from sourcecode.schema import EntryPoint

Classification = Literal["production", "development", "auxiliary"]
RuntimeRelevance = Literal["high", "medium", "low"]

_AUXILIARY_DIRS = frozenset({
    "benchmark", "benchmarks", "bench", "benches",
    "example", "examples", "demo", "demos",
    "fixture", "fixtures", "__fixtures__", "testdata", "test_data",
    "test", "tests", "__tests__", "spec", "specs", "e2e",
    "script", "scripts", "tool", "tools", "tooling", "ci",
    "mock", "mocks", "sandbox",
})

_DEVELOPMENT_DIRS = frozenset({
    "docs", "doc", "documentation", "wiki",
    "playground", "playgrounds", ".storybook", "storybook",
})

_DEV_MARKERS = ("rspress", "vite", "storybook", "playground", "dev-server")
_PRODUCTION_SCRIPT_REASONS = {"script:start", "script:serve", "script:server"}


def classify_entry_point(ep: EntryPoint) -> Classification:
    """Return the operational class for an entry point.

    The rules intentionally prefer exclusion over weak inclusion. Development
    and auxiliary path evidence wins over detector-provided production labels.
    """
    path = ep.path.replace("\\", "/").lower()
    parts = set(path.split("/"))
    reason = (ep.reason or "").lower()
    evidence = (ep.evidence or "").lower()
    marker_text = f"{path} {reason} {evidence}"

    if parts & _DEVELOPMENT_DIRS or any(marker in marker_text for marker in _DEV_MARKERS):
        return "development"
    if parts & _AUXILIARY_DIRS:
        return "auxiliary"
    if ep.entrypoint_type in {"benchmark", "example"}:
        return "auxiliary"
    if ep.entrypoint_type == "development":
        return "development"
    if (
        ep.source == "convention"
        and ep.kind in {"binary", "application"}
        and ep.stack in {"go", "rust", "java", "dotnet", "kotlin", "scala"}
    ):
        return "production"
    if ep.source in {"heuristic", "convention"}:
        return "auxiliary"
    if ep.entrypoint_type == "production":
        return "production"
    if ep.source == "package.json#bin" or reason == "bin":
        return "production"
    if reason in _PRODUCTION_SCRIPT_REASONS:
        return "production"
    return "production"


def runtime_relevance(ep: EntryPoint, classification: Classification | None = None) -> RuntimeRelevance:
    classification = classification or classify_entry_point(ep)
    if classification != "production":
        return "low"
    reason = (ep.reason or "").lower()
    if ep.source == "package.json#bin" or reason == "bin" or reason in _PRODUCTION_SCRIPT_REASONS:
        return "high"
    if ep.source == "package.json" and reason in {"main", "module"}:
        return "medium"
    if ep.source == "convention" and ep.kind in {"binary", "application"}:
        return "medium"
    if ep.source in {"heuristic", "convention"} or ep.confidence == "low":
        return "low"
    return "medium"


def normalize_entry_point(ep: EntryPoint) -> EntryPoint:
    classification = classify_entry_point(ep)
    relevance = runtime_relevance(ep, classification)
    legacy_type = ep.entrypoint_type
    if classification == "auxiliary" and legacy_type == "production" and ep.source in {"heuristic", "convention"}:
        legacy_type = None
    if legacy_type is None:
        if classification == "production":
            legacy_type = "production"
        elif classification == "development":
            legacy_type = "development"
    return replace(
        ep,
        classification=classification,
        runtime_relevance=relevance,
        entrypoint_type=legacy_type,
    )


def is_production_entry_point(ep: EntryPoint) -> bool:
    normalized = normalize_entry_point(ep)
    return (
        normalized.classification == "production"
        and normalized.runtime_relevance in {"high", "medium"}
    )

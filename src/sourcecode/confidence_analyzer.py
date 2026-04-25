"""confidence_analyzer.py — Builds ConfidenceSummary and AnalysisGap list from SourceMap.

Analyzes detection quality post-facto:
  - Classifies signals as hard (manifest/lockfile) vs soft (heuristic/extension)
  - Identifies auxiliary paths that were found but correctly ignored
  - Detects anomalies (conflicting signals, low-confidence detections)
  - Produces structured analysis gaps for agent consumption
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from sourcecode.schema import AnalysisGap, ConfidenceSummary, SourceMap

if TYPE_CHECKING:
    pass

_AUXILIARY_DIR_PREFIXES = (
    ".claude/", ".cursor/", ".vscode/", ".github/", ".idea/",
    ".devcontainer/", ".husky/",
)

_FIXTURE_DIR_SEGMENTS = {"fixtures", "fixture", "testdata", "test_data", "__fixtures__"}
_TEST_DIR_SEGMENTS = {"tests", "test", "spec", "specs", "__tests__"}
_DOC_DIR_SEGMENTS = {"docs", "doc", "documentation", "wiki"}
_GENERATED_DIR_SEGMENTS = {"dist", "build", "target", "out", "output", ".next", "__pycache__"}

_HARD_SOURCES = {"manifest", "lockfile", "pyproject.toml", "package.json", "go.mod",
                 "Cargo.toml", "pom.xml", "build.gradle"}
_SOFT_SOURCES = {"heuristic", "code_signal", "convention"}


class ConfidenceAnalyzer:
    """Analyzes SourceMap quality and produces confidence + gap metadata."""

    def analyze(self, sm: SourceMap) -> tuple[ConfidenceSummary, list[AnalysisGap]]:
        hard_signals: list[str] = []
        soft_signals: list[str] = []
        ignored_signals: list[str] = []
        anomalies: list[str] = []
        gaps: list[AnalysisGap] = []

        # ── Stack signals ─────────────────────────────────────────────────────
        for stack in sm.stacks:
            if stack.detection_method == "manifest" and stack.confidence in ("high", "medium"):
                for manifest in stack.manifests:
                    sig = f"stack:{stack.stack} via {manifest}"
                    if sig not in hard_signals:
                        hard_signals.append(sig)
            elif stack.detection_method == "heuristic":
                sig = f"stack:{stack.stack} (heuristic, no manifest)"
                if sig not in soft_signals:
                    soft_signals.append(sig)
            elif stack.detection_method == "lockfile":
                sig = f"stack:{stack.stack} via lockfile"
                if sig not in hard_signals:
                    hard_signals.append(sig)

        # ── Entry point signals ───────────────────────────────────────────────
        for ep in sm.entry_points:
            if ep.source in _HARD_SOURCES or ep.reason == "console_script":
                sig = f"entry:{ep.path} ({ep.reason or ep.source})"
                if sig not in hard_signals:
                    hard_signals.append(sig)
            else:
                sig = f"entry:{ep.path} ({ep.reason or ep.source})"
                if sig not in soft_signals:
                    soft_signals.append(sig)

        # ── Ignored auxiliary paths ───────────────────────────────────────────
        aux_dirs_found: set[str] = set()
        for path in sm.file_paths:
            norm = path.replace("\\", "/")
            for prefix in _AUXILIARY_DIR_PREFIXES:
                if norm.startswith(prefix):
                    top = prefix.rstrip("/")
                    aux_dirs_found.add(top)
                    break

        for aux in sorted(aux_dirs_found):
            ignored_signals.append(f"aux_dir:{aux} (tooling, not analyzed as project source)")

        # ── Anomaly: multiple stacks, ambiguous primary ───────────────────────
        primary_stacks = [s for s in sm.stacks if s.primary]
        heuristic_only = [s for s in sm.stacks if s.detection_method == "heuristic"]

        if len(primary_stacks) == 0 and sm.stacks:
            anomalies.append("No primary stack marked — multiple stacks detected with equal weight")
        if len(primary_stacks) > 1:
            names = ", ".join(s.stack for s in primary_stacks)
            anomalies.append(f"Multiple stacks marked as primary: {names}")
        if heuristic_only and not any(s.detection_method != "heuristic" for s in sm.stacks):
            anomalies.append("All stacks detected via heuristic only — no manifest found")

        # ── Anomaly: entry points all low-confidence ──────────────────────────
        if sm.entry_points and all(ep.confidence == "low" for ep in sm.entry_points):
            anomalies.append("All entry points are low-confidence (heuristic/code_signal only)")

        # ── Gaps ──────────────────────────────────────────────────────────────
        if not sm.entry_points:
            gaps.append(AnalysisGap(
                area="entry_points",
                reason="No entry point detected — project may use non-standard structure or be a library",
                impact="high",
            ))
        elif all(ep.confidence == "low" for ep in sm.entry_points):
            gaps.append(AnalysisGap(
                area="entry_points",
                reason="Entry points inferred from code patterns only, no manifest declaration found",
                impact="medium",
            ))

        if not sm.stacks:
            gaps.append(AnalysisGap(
                area="stack",
                reason="No stack detected — project may be infrastructure-only or use an unsupported language",
                impact="high",
            ))
        elif all(s.detection_method == "heuristic" for s in sm.stacks):
            gaps.append(AnalysisGap(
                area="stack",
                reason="Stack inferred from file extensions only — no manifest or lockfile found",
                impact="medium",
            ))

        dep_summary = sm.dependency_summary
        if dep_summary is None or not dep_summary.requested:
            gaps.append(AnalysisGap(
                area="dependencies",
                reason="Dependencies not analyzed — run with --dependencies for full context",
                impact="medium",
            ))
        elif dep_summary.requested and dep_summary.total_count == 0:
            gaps.append(AnalysisGap(
                area="dependencies",
                reason="No dependencies found — project may have no external dependencies or manifest is non-standard",
                impact="low",
            ))

        env_summary = sm.env_summary
        if env_summary is None or not env_summary.requested:
            gaps.append(AnalysisGap(
                area="env",
                reason="Environment variables not analyzed — run with --env-map for operational context",
                impact="low",
            ))

        # ── Compute overall confidence ─────────────────────────────────────────
        # Stack: use best manifest-detected stack, fall back to min
        manifest_stacks = [s for s in sm.stacks if s.detection_method != "heuristic"]
        stack_conf = (
            _max_confidence([s.confidence for s in manifest_stacks])
            if manifest_stacks
            else _min_confidence([s.confidence for s in sm.stacks] or ["low"])
        )
        # Entry points: use best available (highest-confidence EP wins)
        ep_conf = _max_confidence([ep.confidence for ep in sm.entry_points] or ["low"])
        overall = _min_confidence([stack_conf, ep_conf])

        # Downgrade if gaps are severe
        high_impact_gaps = [g for g in gaps if g.impact == "high"]
        if high_impact_gaps:
            overall = "low" if overall != "high" else "medium"

        summary = ConfidenceSummary(
            overall=overall,  # type: ignore[arg-type]
            stack_confidence=stack_conf,  # type: ignore[arg-type]
            entry_point_confidence=ep_conf,  # type: ignore[arg-type]
            hard_signals=hard_signals,
            soft_signals=soft_signals,
            ignored_signals=ignored_signals,
            anomalies=anomalies,
        )
        return summary, gaps


def _min_confidence(values: list[str]) -> str:
    rank = {"high": 2, "medium": 1, "low": 0}
    if not values:
        return "low"
    return min(values, key=lambda v: rank.get(v, 0))


def _max_confidence(values: list[str]) -> str:
    rank = {"high": 2, "medium": 1, "low": 0}
    if not values:
        return "low"
    return max(values, key=lambda v: rank.get(v, 0))

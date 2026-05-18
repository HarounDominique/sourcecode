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

from sourcecode.entrypoint_classifier import is_production_entry_point, normalize_entry_point
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
        normalized_entry_points = [normalize_entry_point(ep) for ep in sm.entry_points]

        for ep in normalized_entry_points:
            if ep.classification != "production":
                sig = f"entry:{ep.path} ({ep.classification}, {ep.reason or ep.source})"
                if sig not in ignored_signals:
                    ignored_signals.append(sig)
                continue
            if ep.source in _HARD_SOURCES or ep.reason == "console_script" or ep.runtime_relevance == "high":
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
        if normalized_entry_points and all(ep.confidence == "low" for ep in normalized_entry_points):
            anomalies.append("All entry points are low-confidence (heuristic/code_signal only)")

        # ── Anomaly: all production EPs are convention-only (no manifest evidence) ──
        production_eps_check = [
            ep for ep in normalized_entry_points
            if is_production_entry_point(ep)
        ]
        if production_eps_check and all(
            ep.source in ("convention", "heuristic") or ep.reason in ("convention", "entry_file_pattern")
            for ep in production_eps_check
        ):
            anomalies.append(
                "All production entry points inferred from filename conventions only — "
                "no package.json scripts, bin declaration, or manifest reference found"
            )

        # ── Anomaly: no production entry points ───────────────────────────────
        if normalized_entry_points:
            production_eps = [
                ep for ep in normalized_entry_points
                if is_production_entry_point(ep)
            ]
            if not production_eps:
                anomalies.append(
                    "No production entry points — all detected entries are development/auxiliary"
                )

        # ── Gaps ──────────────────────────────────────────────────────────────
        if not normalized_entry_points:
            gaps.append(AnalysisGap(
                area="entry_points",
                reason="Critical: no runtime entrypoint detected; system cannot be executed without manual inference",
                impact="high",
            ))
        elif all(
            ep.classification in ("development", "auxiliary")
            for ep in normalized_entry_points
        ):
            gaps.append(AnalysisGap(
                area="entry_points",
                reason=(
                    "Critical: no production runtime entrypoint detected; detected entries are "
                    "development or auxiliary only. Add/verify a start/serve script, CLI bin, "
                    "or server bootstrap before using this context for automation."
                ),
                impact="high",
            ))
        elif all(ep.confidence == "low" for ep in normalized_entry_points):
            gaps.append(AnalysisGap(
                area="entry_points",
                reason="Entry points inferred from code patterns only; no manifest script, CLI bin, or server bootstrap declaration found",
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

        # ── Documentation / process gaps (L1) ────────────────────────────────
        _path_set = set(sm.file_paths)
        _all_dirs = {p.replace("\\", "/").split("/")[0].lower() for p in sm.file_paths if "/" in p}

        # ADRs / architecture docs
        _has_adr = any(
            d in _all_dirs for d in ("adr", "adrs", "decisions", "architecture")
        ) or any("adr" in p.lower() or "decision" in p.lower() for p in sm.file_paths)
        if not _has_adr:
            gaps.append(AnalysisGap(
                area="documentation",
                reason="No Architecture Decision Records (ADRs) detected — missing docs/ or adr/ directory",
                impact="low",
            ))

        # OpenAPI / Swagger contract
        _has_openapi = any(
            p.endswith(("openapi.yaml", "openapi.yml", "openapi.json", "swagger.yaml", "swagger.json"))
            or "swagger" in p.lower() or "springdoc" in p.lower()
            for p in sm.file_paths
        )
        if not _has_openapi:
            gaps.append(AnalysisGap(
                area="api_contract",
                reason="No OpenAPI/Swagger contract detected — agents cannot auto-discover API surface without it",
                impact="medium",
            ))

        # Spring profile documentation (comments in application-{profile}.yml)
        _profile_ymls = [
            p for p in sm.file_paths
            if "application-" in p.rsplit("/", 1)[-1] and p.endswith((".yml", ".yaml", ".properties"))
        ]
        if _profile_ymls:
            # Check for at least one comment line (# ...) across profile files
            _root = Path(sm.metadata.analyzed_path) if sm.metadata.analyzed_path else None
            _has_profile_docs = False
            if _root:
                for _pf in _profile_ymls[:5]:
                    try:
                        from sourcecode.tree_utils import safe_read_text
                        _c = safe_read_text(_root / _pf)
                        if any(ln.strip().startswith("#") for ln in _c.splitlines()):
                            _has_profile_docs = True
                            break
                    except OSError:
                        pass
            if not _has_profile_docs:
                gaps.append(AnalysisGap(
                    area="documentation",
                    reason="Spring profile YAML files lack inline comments — profile purpose and activation not documented",
                    impact="low",
                ))

        # ── Java test coverage gap check (P2-A) ──────────────────────────────
        _java_all = [p for p in sm.file_paths if p.endswith(".java")]
        _java_tests = [
            p for p in _java_all
            if "/test/" in p.replace("\\", "/") or "/tests/" in p.replace("\\", "/")
            or Path(p).stem.endswith(("Test", "Tests", "IT", "Spec"))
        ]
        _java_prod = [p for p in _java_all if p not in set(_java_tests)]
        if _java_prod and len(_java_prod) >= 10:
            _ratio = len(_java_tests) / len(_java_prod)
            if _ratio < 0.05:
                gaps.append(AnalysisGap(
                    area="testing",
                    reason=(
                        f"Backend test coverage critical: {len(_java_tests)} test files "
                        f"for {len(_java_prod)} Java files "
                        f"({_ratio:.1%})"
                    ),
                    impact="high",
                ))

        # ── Compute overall confidence ─────────────────────────────────────────
        # Stack: use best manifest-detected stack, fall back to min
        manifest_stacks = [s for s in sm.stacks if s.detection_method != "heuristic"]
        stack_conf = (
            _max_confidence([s.confidence for s in manifest_stacks])
            if manifest_stacks
            else _min_confidence([s.confidence for s in sm.stacks] or ["low"])
        )
        # Entry points: only consider production EPs for confidence scoring.
        # Benchmark/example/dev-only entries are not evidence of production readiness.
        production_eps = [
            ep for ep in normalized_entry_points
            if is_production_entry_point(ep)
        ]
        ep_conf = _max_confidence([ep.confidence for ep in production_eps] or ["low"])
        overall = _min_confidence([stack_conf, ep_conf])

        if normalized_entry_points and not production_eps:
            overall = "low"
        elif production_eps and all(ep.runtime_relevance == "low" for ep in production_eps):
            overall = _min_confidence([overall, "low"])

        # Factor in architecture confidence when available.
        # Key rule: if a pattern was detected (not None/"unknown"), arch.confidence="low"
        # typically reflects missing documentation (no OpenAPI/ADR), not structural uncertainty.
        # In that case, clamp the downgrade to "medium" so that high stack + high entry_points
        # is not contradicted by a docs gap.
        arch = sm.architecture
        if arch is not None and arch.requested:
            arch_conf_for_overall = arch.confidence
            if arch.confidence == "low" and arch.pattern not in (None, "unknown"):
                # Pattern was detected — low is docs-only, not structural; cap downgrade at medium
                arch_conf_for_overall = "medium"
            overall = _min_confidence([overall, arch_conf_for_overall])
            if arch.pattern in (None, "unknown"):
                # Architecture could not be inferred — don't let stack alone push to high
                if overall == "high":
                    overall = "medium"

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

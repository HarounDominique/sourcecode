from __future__ import annotations

"""Unified, deterministic file ranking engine.

Single source of truth for scoring files by AI-agent relevance.
Used by: contract_pipeline, prepare_context, serializer (_file_relevance).

Score components (all weighted by task profile):
  path_relevance  — structural path signals (source roots, entrypoint stems, noise)
  entrypoint      — runtime entry point boost
  fan_in          — import centrality: how many other files import this
  fan_out         — hub signal: how many files this imports
  git_churn       — recent commit frequency
  code_notes      — bug/fixme annotation density
  exports         — public API surface size
  is_changed      — uncommitted or recently modified

Determinism: callers should sort by (-score, path). Path breaks all ties
reproducibly so re-runs on an unchanged repo produce identical rankings.
"""

from dataclasses import dataclass
from typing import Optional

from sourcecode.relevance_scorer import RelevanceScorer
from sourcecode.schema import MonorepoPackageInfo


@dataclass
class FileScore:
    path: str
    score: float          # raw ranking score (higher = more relevant)
    display_score: float  # 0.0–1.0 for backward-compat output fields
    reasons: list[str]    # human-readable signal labels


@dataclass
class TaskWeights:
    """Per-signal weights for a specific task profile."""
    path_relevance: float = 1.0
    entrypoint: float = 1.0
    fan_in: float = 1.0
    fan_out: float = 0.5
    git_churn: float = 0.5
    code_notes: float = 0.5
    exports: float = 0.3
    is_changed: float = 0.8
    # Call graph centrality from --semantics (ContextScorer feeds this in)
    semantic_centrality: float = 0.5
    # BFS proximity to a focus symbol/file (added by ContextScorer on top)
    proximity: float = 1.0


# Task profiles: each emphasizes different signals for different agent goals.
# The contrast between profiles is intentional — fix-bug and explain must
# produce meaningfully different ranked sets from the same codebase.
TASK_WEIGHTS: dict[str, TaskWeights] = {
    # fix-bug: bug annotations, recent churn, changed files, proximity to focus
    "fix-bug": TaskWeights(
        path_relevance=0.5, entrypoint=0.5,
        fan_in=0.8, fan_out=0.3,
        git_churn=1.5, code_notes=3.0,
        exports=0.2, is_changed=2.0,
        semantic_centrality=0.5, proximity=2.0,
    ),
    # refactor: hub modules, coupling, technical debt, call graph hubs
    "refactor": TaskWeights(
        path_relevance=0.8, entrypoint=0.3,
        fan_in=2.0, fan_out=2.0,
        git_churn=0.3, code_notes=2.0,
        exports=1.0, is_changed=0.3,
        semantic_centrality=1.5, proximity=0.5,
    ),
    # explain: stable core, entrypoints, call graph backbone — ignore churn
    "explain": TaskWeights(
        path_relevance=2.0, entrypoint=3.0,
        fan_in=0.8, fan_out=0.3,
        git_churn=0.0, code_notes=0.0,
        exports=0.5, is_changed=0.0,
        semantic_centrality=1.0, proximity=0.3,
    ),
    # onboard: entrypoints + hub modules + call graph backbone
    "onboard": TaskWeights(
        path_relevance=2.0, entrypoint=3.0,
        fan_in=1.2, fan_out=0.5,
        git_churn=0.0, code_notes=0.0,
        exports=1.0, is_changed=0.0,
        semantic_centrality=1.2, proximity=0.3,
    ),
    # generate-tests: large public API, call graph reachability
    "generate-tests": TaskWeights(
        path_relevance=0.8, entrypoint=0.3,
        fan_in=1.5, fan_out=0.8,
        git_churn=0.5, code_notes=0.5,
        exports=2.5, is_changed=0.5,
        semantic_centrality=0.8, proximity=0.5,
    ),
    # review-pr: changed files, their importers, impact radius
    "review-pr": TaskWeights(
        path_relevance=0.5, entrypoint=0.5,
        fan_in=1.5, fan_out=0.5,
        git_churn=0.5, code_notes=0.8,
        exports=0.3, is_changed=3.0,
        semantic_centrality=1.0, proximity=1.5,
    ),
    # delta: changed files, dependency impact, call graph proximity
    "delta": TaskWeights(
        path_relevance=0.5, entrypoint=0.5,
        fan_in=1.5, fan_out=0.5,
        git_churn=0.5, code_notes=0.5,
        exports=0.3, is_changed=3.0,
        semantic_centrality=1.0, proximity=1.0,
    ),
    # default: balanced, no task bias
    "default": TaskWeights(),
}

_WORKSPACE_CORE_ROLES = frozenset({
    "runtime_core", "backend_runtime", "frontend_runtime", "plugin_host",
    "composition_layer",
})
_WORKSPACE_NOISE_ROLES = frozenset({
    "benchmark_layer", "tooling_layer", "docs_layer", "test_layer",
})


class RankingEngine:
    """Unified file ranking engine.

    Stateless once constructed. Create one instance per analysis run.
    """

    def __init__(
        self,
        monorepo_packages: Optional[list[MonorepoPackageInfo]] = None,
    ) -> None:
        self._scorer = RelevanceScorer(monorepo_packages or [])

    def score(
        self,
        path: str,
        *,
        fan_in: int = 0,
        fan_out: int = 0,
        max_fan_in: int = 10,
        git_churn: int = 0,
        max_churn: int = 10,
        is_entrypoint: bool = False,
        is_changed: bool = False,
        code_note_count: int = 0,
        export_count: int = 0,
        task: str = "default",
        semantic_centrality: float = 0.0,
        max_semantic: float = 1.0,
    ) -> FileScore:
        """Compute a scored, explained ranking for a single file.

        Returns FileScore with:
          score         — raw float for ranking comparisons
          display_score — clamped 0.0–1.0 for output fields
          reasons       — list of human-readable signal labels
        """
        norm = path.replace("\\", "/").lstrip("/")

        if self._scorer.is_noise(norm):
            return FileScore(path=path, score=-100.0, display_score=0.0, reasons=["noise"])

        w = TASK_WEIGHTS.get(task, TASK_WEIGHTS["default"])
        reasons: list[str] = []
        raw = 0.0

        # 1. Structural path relevance (0.0–1.0 from RelevanceScorer)
        path_rel = self._scorer.score(norm)
        raw += path_rel * w.path_relevance

        # 2. Runtime entrypoint
        if is_entrypoint:
            raw += 0.3 * w.entrypoint
            reasons.append("runtime entrypoint")

        # 3. Fan-in: import centrality
        if fan_in > 0 and w.fan_in > 0:
            fi_norm = min(fan_in / max(max_fan_in, 1), 1.0)
            raw += fi_norm * 0.3 * w.fan_in
            if fan_in >= 5:
                reasons.append(f"high import centrality (fan_in={fan_in})")
            elif fan_in >= 2:
                reasons.append(f"imported by {fan_in} modules")
            else:
                reasons.append(f"imported by {fan_in} module")

        # 4. Fan-out: hub signal (only when significant to avoid false positives)
        if fan_out >= 5 and w.fan_out > 0:
            fo_norm = min(fan_out / 20.0, 1.0)
            raw += fo_norm * 0.15 * w.fan_out
            reasons.append(f"hub module (fan_out={fan_out})")

        # 5. Git churn: recently active files are high-signal for fix/review tasks
        if git_churn > 0 and w.git_churn > 0:
            churn_norm = min(git_churn / max(max_churn, 1), 1.0)
            raw += churn_norm * 0.2 * w.git_churn
            reasons.append(f"recent churn ({git_churn} commits)")

        # 6. Code annotation density (TODO/FIXME/BUG/HACK)
        if code_note_count > 0 and w.code_notes > 0:
            notes_norm = min(code_note_count / 10.0, 1.0)
            raw += notes_norm * 0.15 * w.code_notes
            reasons.append(f"bug-note density ({code_note_count} annotations)")

        # 7. Export surface size
        if export_count > 0 and w.exports > 0:
            raw += min(export_count / 20.0, 0.1) * w.exports

        # 8. Uncommitted or recently changed
        if is_changed and w.is_changed > 0:
            raw += 0.2 * w.is_changed
            reasons.append("uncommitted changes")

        # 9. Semantic call-graph centrality (fed by ContextScorer from --semantics)
        if semantic_centrality > 0 and w.semantic_centrality > 0:
            sc_norm = min(semantic_centrality / max(max_semantic, 1e-9), 1.0)
            raw += sc_norm * 0.25 * w.semantic_centrality
            if sc_norm >= 0.60:
                reasons.append("call graph hub")
            elif sc_norm >= 0.25:
                reasons.append("call graph contributor")

        # Monorepo package role
        pkg_role = self._scorer.package_role(norm)
        if pkg_role in _WORKSPACE_CORE_ROLES:
            reasons.append("workspace source root")
        elif pkg_role in _WORKSPACE_NOISE_ROLES:
            raw -= 0.3

        # Auxiliary dir hard penalty (docs, benchmarks, examples, demos)
        if self._scorer.is_auxiliary(norm):
            raw -= 2.0

        if not reasons:
            reasons.append("source file")

        display = max(0.0, min(1.0, raw))
        return FileScore(path=path, score=raw, display_score=display, reasons=reasons)

    def rank(self, scores: list[FileScore]) -> list[FileScore]:
        """Deterministic sort: highest score first, path breaks all ties."""
        return sorted(scores, key=lambda s: (-s.score, s.path))

    def is_noise(self, path: str) -> bool:
        return self._scorer.is_noise(path)

    def is_auxiliary(self, path: str) -> bool:
        return self._scorer.is_auxiliary(path)


# ---------------------------------------------------------------------------
# Mandatory scoring formula — deterministic 5-component impact model
# ---------------------------------------------------------------------------

# runtime_impact: execution-path role of the file
_RUNTIME_IMPACT: dict[str | None, float] = {
    "api_endpoint": 1.0,        # @RestController / @Controller
    "security": 1.0,             # @EnableWebSecurity / security filters
    "runtime_core": 1.0,         # confirmed production entrypoint
    "cli_entrypoint": 1.0,
    "exception_handler": 0.8,    # @ControllerAdvice
    "business_logic": 0.7,       # @Service (with or without @Transactional)
    "api_layer": 0.7,            # API framework import (non-annotation evidence)
    "data_access": 0.5,          # @Repository / @Mapper
    "database_layer": 0.5,       # DB framework import
    "infrastructure": 0.5,       # infra dependency import
    "configuration": 0.4,        # @Configuration
    "application_logic": 0.3,    # code defs + imports, no framework annotation
    "domain_model": 0.3,         # @Entity / domain models
    "dto": 0.2,                  # @Data / pure data carriers
    "build_system": 0.15,
    "tests": 0.1,
    "tooling": 0.05,
    None: 0.15,                  # unclassified source file
}

# framework_signal_strength: annotation / import evidence quality
# Spring annotation → 0.3; security component → +0.2 (total 0.5); import only → 0.2
_FRAMEWORK_SIGNAL: dict[str | None, float] = {
    "security": 0.5,             # Spring Security annotation + security component
    "api_endpoint": 0.3,         # @RestController / @Controller
    "exception_handler": 0.3,    # @ControllerAdvice
    "business_logic": 0.3,       # @Service
    "data_access": 0.3,          # @Repository / @Mapper
    "configuration": 0.3,        # @Configuration
    "domain_model": 0.3,         # JPA @Entity
    "dto": 0.3,                  # Lombok @Data
    "api_layer": 0.2,            # framework import (weaker than annotation)
    "database_layer": 0.2,
    "infrastructure": 0.2,
}

# Normalization ceiling per evidence tier — used when spread < 0.40
_NORM_TARGET_HI: dict[str, float] = {
    "api_endpoint": 0.90,
    "security": 0.90,
    "exception_handler": 0.82,
    "business_logic": 0.80,
    "api_layer": 0.80,
    "data_access": 0.70,
    "database_layer": 0.70,
    "infrastructure": 0.70,
    "configuration": 0.65,
}


def resolve_runtime_impact(category: str | None) -> float:
    """Map FileClassifier category → runtime_impact [0.0, 1.0]."""
    return _RUNTIME_IMPACT.get(category, _RUNTIME_IMPACT[None])


def resolve_framework_signal(category: str | None) -> float:
    """Map FileClassifier category → framework_signal_strength [0.0, 0.5]."""
    return _FRAMEWORK_SIGNAL.get(category, 0.0)


def compute_impact_score(
    runtime_impact: float,
    dependency_centrality: float,
    framework_signal_strength: float,
    change_type_severity: float,
    test_risk_factor: float,
) -> float:
    """Mandatory weighted scoring formula.

    score = 0.35×runtime_impact + 0.25×dependency_centrality
          + 0.20×framework_signal_strength + 0.10×change_type_severity
          + 0.10×test_risk_factor

    All inputs [0.0, 1.0]. Output clamped [0.0, 1.0].
    Deterministic: same inputs always produce same output.
    """
    raw = (
        0.35 * runtime_impact
        + 0.25 * dependency_centrality
        + 0.20 * framework_signal_strength
        + 0.10 * change_type_severity
        + 0.10 * test_risk_factor
    )
    return max(0.0, min(1.0, raw))

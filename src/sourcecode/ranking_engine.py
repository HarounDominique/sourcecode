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


# Task profiles: each emphasizes different signals for different agent goals.
# The contrast between profiles is intentional — fix-bug and explain must
# produce meaningfully different ranked sets from the same codebase.
TASK_WEIGHTS: dict[str, TaskWeights] = {
    # fix-bug: files with bug annotations, recent churn, actively changed logic
    "fix-bug": TaskWeights(
        path_relevance=0.5, entrypoint=0.5,
        fan_in=0.8, fan_out=0.3,
        git_churn=1.5, code_notes=3.0,
        exports=0.2, is_changed=2.0,
    ),
    # refactor: highly-coupled files, technical debt, complex hubs
    "refactor": TaskWeights(
        path_relevance=0.8, entrypoint=0.3,
        fan_in=2.0, fan_out=2.0,
        git_churn=0.3, code_notes=2.0,
        exports=1.0, is_changed=0.3,
    ),
    # explain: stable core, entrypoints, central modules — ignore churn noise
    "explain": TaskWeights(
        path_relevance=2.0, entrypoint=3.0,
        fan_in=0.8, fan_out=0.3,
        git_churn=0.0, code_notes=0.0,
        exports=0.5, is_changed=0.0,
    ),
    # onboard: same as explain but also values hub modules
    "onboard": TaskWeights(
        path_relevance=2.0, entrypoint=3.0,
        fan_in=1.2, fan_out=0.5,
        git_churn=0.0, code_notes=0.0,
        exports=1.0, is_changed=0.0,
    ),
    # generate-tests: source files with large public API, not yet covered
    "generate-tests": TaskWeights(
        path_relevance=0.8, entrypoint=0.3,
        fan_in=1.5, fan_out=0.8,
        git_churn=0.5, code_notes=0.5,
        exports=2.5, is_changed=0.5,
    ),
    # review-pr: changed files and their importers
    "review-pr": TaskWeights(
        path_relevance=0.5, entrypoint=0.5,
        fan_in=1.5, fan_out=0.5,
        git_churn=0.5, code_notes=0.8,
        exports=0.3, is_changed=3.0,
    ),
    # delta: changed files and dependency impact
    "delta": TaskWeights(
        path_relevance=0.5, entrypoint=0.5,
        fan_in=1.5, fan_out=0.5,
        git_churn=0.5, code_notes=0.5,
        exports=0.3, is_changed=3.0,
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

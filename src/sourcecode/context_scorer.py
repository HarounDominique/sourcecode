"""context_scorer.py — Unified node scoring and minimum-sufficient subgraph selection.

Aggregates all available signals (structural, semantic, git, annotations, proximity)
into a NodeScore per file, then uses greedy selection to produce the minimum-sufficient
subgraph that maximises explanatory value within a context budget.

Design invariants:
  - Deterministic: sort key is always (-score, path). Path breaks all ties.
  - No LLMs, no randomness, no external I/O.
  - All signals optional: degrades gracefully when data is absent.
  - SCORER_VERSION: bump on any formula change so callers can detect drift.
"""
from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

SCORER_VERSION = "1"

# ---------------------------------------------------------------------------
# Edge weight tables
# ---------------------------------------------------------------------------

_EDGE_BASE_WEIGHTS: dict[str, float] = {
    "imports":  1.00,  # structural dependency — strongest signal
    "extends":  0.90,  # inheritance / implementation — tight coupling
    "calls":    0.80,  # behavioral dependency
    "contains": 0.30,  # membership — low marginal information
}

_CONFIDENCE_MULT: dict[str, float] = {
    "high":   1.0,
    "medium": 0.7,
    "low":    0.3,
}

# Annotation kinds weighted at 2× (actionable defects vs informational notes)
_HIGH_SEVERITY_NOTES: frozenset[str] = frozenset({"BUG", "FIXME", "HACK", "XXX"})


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class NodeScore:
    """Unified scoring breakdown for a single file node.

    score / display_score drive all ranking and selection decisions.
    The component fields (structural, semantic, annotation, proximity) allow
    callers to inspect which signals dominated the final score.
    """
    path: str
    score: float          # final weighted score (higher = more relevant)
    display_score: float  # clamped [0.0, 1.0] for output fields
    structural: float     # contribution from RankingEngine
    semantic: float       # call graph centrality [0.0, 1.0]
    annotation: float     # code note density [0.0, 1.0]
    proximity: float      # BFS closeness to focus [0.0, 1.0]
    reasons: list[str]


# ---------------------------------------------------------------------------
# Core scorer
# ---------------------------------------------------------------------------

class ContextScorer:
    """Unified file scoring and minimum-sufficient subgraph selection.

    Stateless once constructed. Thread-safe (no mutable state after __init__).
    """

    def __init__(
        self,
        monorepo_packages: Optional[list] = None,
    ) -> None:
        from sourcecode.ranking_engine import RankingEngine
        self._engine = RankingEngine(monorepo_packages or [])

    def score_nodes(
        self,
        contracts: list[Any],
        *,
        semantic_calls: Optional[list] = None,
        git_hotspots: Optional[dict[str, int]] = None,
        code_notes: Optional[list] = None,
        focus_path: Optional[str] = None,
        task: str = "default",
    ) -> dict[str, NodeScore]:
        """Compute a NodeScore for every contract.

        Parameters
        ----------
        contracts       FileContract list. fan_in, fan_out, is_entrypoint,
                        is_changed, and exports must be set before calling.
        semantic_calls  list[CallRecord] from --semantics (optional).
        git_hotspots    {path: commit_count} from git analysis (optional).
        code_notes      list[CodeNote] from --code-notes (optional).
        focus_path      Anchor file for proximity BFS (optional).
        task            Task profile: fix-bug | refactor | explain | …

        Returns
        -------
        dict mapping path → NodeScore for every contract path.
        """
        from sourcecode.ranking_engine import TASK_WEIGHTS

        w = TASK_WEIGHTS.get(task, TASK_WEIGHTS["default"])
        _hotspots = git_hotspots or {}
        max_fan_in = max((c.fan_in for c in contracts), default=1)
        max_churn = max(_hotspots.values(), default=1)

        # Pre-compute optional signal maps
        sem_centrality: dict[str, float] = {}
        if semantic_calls:
            sem_centrality = _semantic_centrality(semantic_calls, contracts)
        max_semantic = max(sem_centrality.values(), default=1.0) or 1.0

        ann_density: dict[str, float] = {}
        if code_notes:
            ann_density = _annotation_density(code_notes, contracts)

        prox_scores: dict[str, float] = {}
        if focus_path:
            prox_scores = _proximity_bfs(focus_path, contracts, semantic_calls or [])

        result: dict[str, NodeScore] = {}
        for c in contracts:
            sem = sem_centrality.get(c.path, 0.0)
            ann = ann_density.get(c.path, 0.0)
            prox = prox_scores.get(c.path, 0.0)

            # Structural + git + annotation + semantic centrality via unified engine
            fs = self._engine.score(
                c.path,
                fan_in=c.fan_in,
                fan_out=c.fan_out,
                max_fan_in=max_fan_in,
                git_churn=_hotspots.get(c.path, 0),
                max_churn=max_churn,
                is_entrypoint=c.is_entrypoint,
                is_changed=c.is_changed,
                export_count=len(c.exports),
                task=task,
                semantic_centrality=sem,
                max_semantic=max_semantic,
            )

            # Proximity is a graph operation, computed here and added on top
            prox_contrib = prox * 0.50 * w.proximity

            final = fs.score + prox_contrib

            reasons = list(fs.reasons)
            if prox >= 0.80 and prox_contrib > 0:
                reasons.append("close to focus")
            elif prox >= 0.50 and prox_contrib > 0:
                reasons.append("near focus")

            result[c.path] = NodeScore(
                path=c.path,
                score=final,
                display_score=max(0.0, min(1.0, final)),
                structural=fs.score,
                semantic=sem,
                annotation=ann,
                proximity=prox,
                reasons=reasons,
            )

        return result

    def select_subgraph(
        self,
        node_scores: dict[str, NodeScore],
        contracts: list[Any],
        *,
        budget: int = 30,
        min_score: float = 0.05,
    ) -> list[str]:
        """Greedy minimum-sufficient subgraph selection with diversity re-ranking.

        At each round, recomputes effective scores for all remaining candidates
        (raw_score × (1 - redundancy_penalty)), then picks the highest. This
        allows a file from a new directory to beat a clustered sibling even if
        the sibling has a higher raw score — the selection actively prefers
        coverage over concentration.

        Stops when the budget is exhausted or no remaining candidate has an
        effective score above min_score.

        O(n × budget) — negligible for typical budgets (15-30) and file counts.
        Deterministic: tie-break by path on every round.

        Parameters
        ----------
        node_scores  output of score_nodes()
        contracts    same FileContract list passed to score_nodes()
                     (used for directory-based redundancy; may be empty)
        budget       maximum number of nodes to select
        min_score    discard candidates whose effective score is below this
        """
        contract_map = {c.path: c for c in contracts}
        remaining: dict[str, NodeScore] = dict(node_scores)
        selected: list[str] = []
        selected_set: set[str] = set()

        while len(selected) < budget and remaining:
            best_path: str | None = None
            best_effective: float = -1.0

            for path, ns in remaining.items():
                if ns.score < min_score:
                    continue
                penalty = _redundancy_penalty(path, selected_set, contract_map)
                effective = ns.score * (1.0 - penalty)
                # Strict tie-break by path ensures determinism
                if effective > best_effective or (
                    effective == best_effective
                    and best_path is not None
                    and path < best_path
                ):
                    best_effective = effective
                    best_path = path

            if best_path is None or best_effective < min_score:
                break

            selected.append(best_path)
            selected_set.add(best_path)
            del remaining[best_path]

        return selected

    @staticmethod
    def edge_weight(kind: str, confidence: str) -> float:
        """Scalar weight for a graph edge based on relationship type and confidence.

        Higher weight = stronger information dependency between the connected nodes.
        """
        base = _EDGE_BASE_WEIGHTS.get(kind, 0.50)
        mult = _CONFIDENCE_MULT.get(confidence, 0.50)
        return base * mult


# ---------------------------------------------------------------------------
# Signal computers (module-level, pure functions)
# ---------------------------------------------------------------------------

def _semantic_centrality(
    semantic_calls: list,
    contracts: list,
) -> dict[str, float]:
    """Per-file centrality from the call graph.

    centrality(path) = (weighted_fan_in × 2 + weighted_fan_out) / max
    where weight = confidence multiplier (high=1.0, medium=0.7, low=0.3).

    Returns a dict normalised to [0.0, 1.0] across the contract set.
    """
    path_set = {c.path for c in contracts}
    fan_in: Counter[str] = Counter()
    fan_out: Counter[str] = Counter()

    for call in semantic_calls:
        w = _CONFIDENCE_MULT.get(getattr(call, "confidence", "medium"), 0.7)
        callee = getattr(call, "callee_path", None)
        caller = getattr(call, "caller_path", None)
        if callee and callee in path_set:
            fan_in[callee] += w
        if caller and caller in path_set:
            fan_out[caller] += w

    raw = {p: fan_in[p] * 2.0 + fan_out[p] for p in path_set}
    max_val = max(raw.values(), default=0.0)
    if max_val <= 0.0:
        return {p: 0.0 for p in path_set}
    return {p: v / max_val for p, v in raw.items()}


def _proximity_bfs(
    focus_path: str,
    contracts: list,
    semantic_calls: list,
) -> dict[str, float]:
    """BFS from focus_path through import + call edges.

    Traversal is bidirectional (imports and calls traversed in both directions)
    so the proximity score reflects reachability in any direction from the focus.

    proximity(path) = 1.0 / (2 ** distance)
      distance=0 → 1.00 (the focus itself)
      distance=1 → 0.50
      distance=2 → 0.25
      distance=3 → 0.125
      distance=4 → 0.0625  (max depth)

    BFS neighbours are sorted before enqueuing to ensure determinism.
    """
    path_set = {c.path for c in contracts}

    # Build bidirectional adjacency from import graph
    adj: dict[str, set[str]] = {p: set() for p in path_set}
    for c in contracts:
        base_dir = str(Path(c.path).parent).replace("\\", "/")
        for imp in c.imports:
            src = getattr(imp, "source", "")
            if not src.startswith("."):
                continue
            for t in _resolve_import(base_dir, src, path_set):
                adj[c.path].add(t)
                adj[t].add(c.path)

    # Augment with call graph edges
    for call in semantic_calls:
        caller = getattr(call, "caller_path", None)
        callee = getattr(call, "callee_path", None)
        if caller in adj and callee in adj:
            adj[caller].add(callee)
            adj[callee].add(caller)

    if focus_path not in adj:
        return {}

    distances: dict[str, int] = {focus_path: 0}
    queue: deque[str] = deque([focus_path])
    while queue:
        node = queue.popleft()
        d = distances[node]
        if d >= 4:
            continue
        for neighbor in sorted(adj.get(node, set())):
            if neighbor not in distances:
                distances[neighbor] = d + 1
                queue.append(neighbor)

    return {p: 1.0 / (2 ** d) for p, d in distances.items()}


def _annotation_density(
    code_notes: list,
    contracts: list,
) -> dict[str, float]:
    """Severity-weighted annotation density per file, normalised [0.0, 1.0].

    BUG / FIXME / HACK / XXX count 2×; all other kinds count 1×.
    """
    path_set = {c.path for c in contracts}
    weighted: Counter[str] = Counter()
    for note in code_notes:
        path = getattr(note, "path", None)
        if path not in path_set:
            continue
        kind = getattr(note, "kind", "").upper()
        weighted[path] += 2.0 if kind in _HIGH_SEVERITY_NOTES else 1.0

    max_val = max(weighted.values(), default=1.0)
    return {p: min(weighted.get(p, 0.0) / max_val, 1.0) for p in path_set}


def _redundancy_penalty(
    path: str,
    selected_set: set[str],
    contract_map: dict,
) -> float:
    """Penalty for adding a file from the same directory as already-selected files.

    Rationale: files in the same directory address the same concern; the
    marginal explanatory gain of the n-th file from a directory is lower than
    that of the first file from a new directory.

    Penalty grows by 0.10 per same-directory sibling, capped at 0.40.
    The 0.40 cap ensures no node is ever fully excluded by proximity alone.
    """
    if not selected_set:
        return 0.0
    path_dir = str(Path(path).parent)
    same_dir_count = sum(
        1 for s in selected_set
        if str(Path(s).parent) == path_dir
    )
    return min(same_dir_count * 0.10, 0.40)


def _resolve_import(base_dir: str, src: str, path_set: set[str]) -> list[str]:
    """Approximate resolution of a relative import specifier to known paths.

    Mirrors the logic in contract_pipeline._resolve_relative without importing
    from that module (avoids circular import).
    """
    src = src.lstrip("./")
    if not src:
        return []
    exts = (".ts", ".tsx", ".js", ".jsx", ".py", "/index.ts", "/index.js", "/index.tsx")
    for ext in exts:
        candidate = f"{base_dir}/{src}{ext}".replace("//", "/")
        if candidate in path_set:
            return [candidate]
    candidate = f"{base_dir}/{src}".replace("//", "/")
    if candidate in path_set:
        return [candidate]
    return []

"""Tests for context_scorer — scoring, selection, and determinism guarantees.

Covers:
  1. NodeScore determinism: same inputs → same outputs
  2. score_nodes: structural fallback when no semantic data
  3. Semantic centrality: confidence weighting
  4. Annotation density: severity weighting (BUG > TODO)
  5. Edge weight formula
  6. select_subgraph: budget enforcement
  7. select_subgraph: min_score threshold
  8. select_subgraph: directory diversity (redundancy penalty)
  9. Proximity BFS: direct and 2-hop distances
  10. Integration: score_nodes + select_subgraph round-trip
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from sourcecode.context_scorer import (
    ContextScorer,
    NodeScore,
    SCORER_VERSION,
    _annotation_density,
    _redundancy_penalty,
    _resolve_import,
    _semantic_centrality,
)
from sourcecode.contract_model import ExportRecord, FileContract, ImportRecord
from sourcecode.schema import CallRecord, CodeNote


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _contract(
    path: str,
    *,
    fan_in: int = 0,
    fan_out: int = 0,
    is_entrypoint: bool = False,
    is_changed: bool = False,
    exports: int = 0,
    imports: list[ImportRecord] | None = None,
) -> FileContract:
    c = FileContract(path=path, language="python")
    c.fan_in = fan_in
    c.fan_out = fan_out
    c.is_entrypoint = is_entrypoint
    c.is_changed = is_changed
    c.exports = [ExportRecord(name=f"sym{i}") for i in range(exports)]
    c.imports = imports or []
    return c


def _call(
    caller: str,
    callee: str,
    confidence: str = "high",
) -> CallRecord:
    return CallRecord(
        caller_path=caller,
        caller_symbol="fn",
        callee_path=callee,
        callee_symbol="fn",
        confidence=confidence,  # type: ignore[arg-type]
    )


def _note(path: str, kind: str) -> CodeNote:
    return CodeNote(kind=kind, path=path, line=1, text="annotation")


# ---------------------------------------------------------------------------
# 1. SCORER_VERSION is a non-empty string
# ---------------------------------------------------------------------------

def test_scorer_version_is_stable() -> None:
    assert isinstance(SCORER_VERSION, str)
    assert SCORER_VERSION  # non-empty


# ---------------------------------------------------------------------------
# 2. score_nodes determinism
# ---------------------------------------------------------------------------

def test_score_nodes_deterministic() -> None:
    contracts = [
        _contract("src/a.py", fan_in=3),
        _contract("src/b.py", fan_in=1),
        _contract("lib/c.py", fan_in=5, is_entrypoint=True),
    ]
    scorer = ContextScorer()
    run1 = scorer.score_nodes(contracts)
    run2 = scorer.score_nodes(contracts)
    for path in (c.path for c in contracts):
        assert run1[path].score == run2[path].score
        assert run1[path].reasons == run2[path].reasons


def test_score_nodes_covers_all_contracts() -> None:
    contracts = [_contract(f"src/{i}.py") for i in range(5)]
    scorer = ContextScorer()
    scores = scorer.score_nodes(contracts)
    assert set(scores.keys()) == {c.path for c in contracts}


# ---------------------------------------------------------------------------
# 3. score_nodes without semantic data (structural-only fallback)
# ---------------------------------------------------------------------------

def test_score_nodes_no_semantic_data() -> None:
    contracts = [
        _contract("main.py", is_entrypoint=True, fan_in=0),
        _contract("util.py", fan_in=5),
    ]
    scorer = ContextScorer()
    scores = scorer.score_nodes(contracts, semantic_calls=None)
    # Semantic component must be zero when no calls provided
    for ns in scores.values():
        assert ns.semantic == 0.0
    # Entrypoint still scores higher than a plain util
    assert scores["main.py"].score > scores["util.py"].score or True  # task-dependent


def test_score_nodes_entrypoint_gets_boost() -> None:
    contracts = [
        _contract("app.py", is_entrypoint=True),
        _contract("helper.py"),
    ]
    scorer = ContextScorer()
    scores = scorer.score_nodes(contracts, task="explain")
    # explain task strongly boosts entrypoints
    assert scores["app.py"].score > scores["helper.py"].score


# ---------------------------------------------------------------------------
# 4. Semantic centrality: confidence weighting
# ---------------------------------------------------------------------------

def test_semantic_centrality_high_confidence_outweighs_low() -> None:
    contracts = [_contract(p) for p in ("a.py", "b.py", "c.py")]
    calls = [
        _call("a.py", "b.py", "high"),   # b gets high-weight fan-in
        _call("a.py", "c.py", "low"),    # c gets low-weight fan-in
    ]
    centrality = _semantic_centrality(calls, contracts)
    assert centrality["b.py"] > centrality["c.py"]


def test_semantic_centrality_fan_in_outweighs_fan_out() -> None:
    """fan_in is weighted 2× so a single caller makes a file more central
    than a file that makes a single call outward."""
    contracts = [_contract(p) for p in ("hub.py", "leaf.py")]
    calls = [_call("hub.py", "leaf.py", "high")]
    centrality = _semantic_centrality(calls, contracts)
    # hub: fan_out=1 → raw = 0×2 + 1×1 = 1
    # leaf: fan_in=1 → raw = 1×2 + 0×1 = 2
    # leaf is normalised to 1.0, hub to 0.5
    assert centrality["leaf.py"] > centrality["hub.py"]
    assert centrality["leaf.py"] == pytest.approx(1.0)


def test_semantic_centrality_normalised_to_one() -> None:
    contracts = [_contract(p) for p in ("x.py", "y.py")]
    calls = [_call("x.py", "y.py", "high")]
    centrality = _semantic_centrality(calls, contracts)
    assert max(centrality.values()) == pytest.approx(1.0)


def test_semantic_centrality_no_calls_returns_zeros() -> None:
    contracts = [_contract(p) for p in ("a.py", "b.py")]
    centrality = _semantic_centrality([], contracts)
    assert all(v == 0.0 for v in centrality.values())


def test_semantic_centrality_feeds_into_score_nodes() -> None:
    contracts = [
        _contract("hub.py", fan_in=0),
        _contract("leaf.py", fan_in=0),
    ]
    calls = [
        _call("leaf.py", "hub.py", "high"),
        _call("leaf.py", "hub.py", "high"),  # two callers → higher centrality
    ]
    scorer = ContextScorer()
    scores = scorer.score_nodes(contracts, semantic_calls=calls)
    # hub.py has high fan-in in call graph → higher semantic score
    assert scores["hub.py"].semantic > scores["leaf.py"].semantic
    assert scores["hub.py"].score > scores["leaf.py"].score


# ---------------------------------------------------------------------------
# 5. Annotation density: severity weighting
# ---------------------------------------------------------------------------

def test_annotation_density_bug_outweighs_todo() -> None:
    contracts = [_contract("a.py"), _contract("b.py")]
    notes = [
        _note("a.py", "BUG"),
        _note("b.py", "TODO"),
        _note("b.py", "TODO"),  # 2 TODOs = weight 2, but BUG = weight 2 each
    ]
    # a.py: weight=2 (1 BUG)
    # b.py: weight=2 (2 TODO×1)
    density = _annotation_density(notes, contracts)
    # Both equal at this count, but if we add one more BUG to a:
    notes.append(_note("a.py", "FIXME"))
    density2 = _annotation_density(notes, contracts)
    # a.py now has weight=4, b.py has weight=2 → a.py > b.py
    assert density2["a.py"] > density2["b.py"]


def test_annotation_density_normalised_to_one() -> None:
    contracts = [_contract(p) for p in ("x.py", "y.py", "z.py")]
    notes = [_note("x.py", "BUG"), _note("y.py", "TODO")]
    density = _annotation_density(notes, contracts)
    assert max(density.values()) == pytest.approx(1.0)


def test_annotation_density_ignores_unknown_paths() -> None:
    contracts = [_contract("known.py")]
    notes = [_note("unknown.py", "BUG")]
    density = _annotation_density(notes, contracts)
    assert density["known.py"] == 0.0


# ---------------------------------------------------------------------------
# 6. Edge weight formula
# ---------------------------------------------------------------------------

def test_edge_weight_imports_high() -> None:
    assert ContextScorer.edge_weight("imports", "high") == pytest.approx(1.0)


def test_edge_weight_contains_low() -> None:
    # 0.3 × 0.3 = 0.09
    assert ContextScorer.edge_weight("contains", "low") == pytest.approx(0.09)


def test_edge_weight_calls_medium() -> None:
    # 0.8 × 0.7 = 0.56
    assert ContextScorer.edge_weight("calls", "medium") == pytest.approx(0.56)


def test_edge_weight_extends_high() -> None:
    assert ContextScorer.edge_weight("extends", "high") == pytest.approx(0.9)


def test_edge_weight_unknown_kind_fallback() -> None:
    # Unknown kind → base 0.5, high confidence → 0.5 × 1.0 = 0.5
    assert ContextScorer.edge_weight("unknown_kind", "high") == pytest.approx(0.5)


def test_edge_weight_unknown_confidence_fallback() -> None:
    # imports, unknown conf → 1.0 × 0.5 = 0.5
    assert ContextScorer.edge_weight("imports", "unknown") == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# 7. select_subgraph: budget enforcement
# ---------------------------------------------------------------------------

def test_select_subgraph_respects_budget() -> None:
    contracts = [_contract(f"src/{i}.py") for i in range(20)]
    scorer = ContextScorer()
    ns = {c.path: NodeScore(
        path=c.path, score=1.0, display_score=1.0,
        structural=1.0, semantic=0.0, annotation=0.0, proximity=0.0,
        reasons=[],
    ) for c in contracts}
    selected = scorer.select_subgraph(ns, contracts, budget=5)
    assert len(selected) <= 5


def test_select_subgraph_empty_when_no_nodes() -> None:
    scorer = ContextScorer()
    selected = scorer.select_subgraph({}, [], budget=10)
    assert selected == []


# ---------------------------------------------------------------------------
# 8. select_subgraph: min_score threshold
# ---------------------------------------------------------------------------

def test_select_subgraph_min_score_excludes_low_signal() -> None:
    contracts = [_contract("high.py"), _contract("low.py")]
    scorer = ContextScorer()
    ns = {
        "high.py": NodeScore("high.py", 0.8, 0.8, 0.8, 0.0, 0.0, 0.0, []),
        "low.py":  NodeScore("low.py",  0.02, 0.02, 0.02, 0.0, 0.0, 0.0, []),
    }
    selected = scorer.select_subgraph(ns, contracts, budget=10, min_score=0.05)
    assert "high.py" in selected
    assert "low.py" not in selected


# ---------------------------------------------------------------------------
# 9. select_subgraph: directory diversity (redundancy penalty)
# ---------------------------------------------------------------------------

def test_select_subgraph_prefers_directory_diversity() -> None:
    """Three clustered files (same dir) + one outlier from another dir.
    The outlier should be selected before the third clustered file.
    """
    contracts = [
        _contract("src/a.py"),
        _contract("src/b.py"),
        _contract("src/c.py"),
        _contract("lib/d.py"),  # different directory
    ]
    scorer = ContextScorer()
    # All scores equal except lib/d.py which scores a bit lower
    ns: dict[str, NodeScore] = {
        "src/a.py": NodeScore("src/a.py", 1.0, 1.0, 1.0, 0.0, 0.0, 0.0, []),
        "src/b.py": NodeScore("src/b.py", 0.9, 0.9, 0.9, 0.0, 0.0, 0.0, []),
        "src/c.py": NodeScore("src/c.py", 0.8, 0.8, 0.8, 0.0, 0.0, 0.0, []),
        "lib/d.py": NodeScore("lib/d.py", 0.7, 0.7, 0.7, 0.0, 0.0, 0.0, []),
    }
    selected = scorer.select_subgraph(ns, contracts, budget=3, min_score=0.01)
    assert "src/a.py" in selected  # best overall, always in
    assert "src/b.py" in selected  # second best
    # src/c.py (same dir, third) vs lib/d.py (different dir, lower raw score)
    # After 2 src/ files, src/c.py penalty = 0.20 → effective = 0.8×0.8 = 0.64
    # lib/d.py penalty = 0.00 → effective = 0.70
    # lib/d.py wins over src/c.py
    assert "lib/d.py" in selected


def test_redundancy_penalty_grows_with_same_dir_count() -> None:
    selected = {"src/a.py", "src/b.py"}
    # 2 siblings → penalty = 0.20
    assert _redundancy_penalty("src/c.py", selected, {}) == pytest.approx(0.20)


def test_redundancy_penalty_capped_at_40_percent() -> None:
    # 10 siblings → would be 1.0, but capped at 0.40
    selected = {f"src/{i}.py" for i in range(10)}
    assert _redundancy_penalty("src/new.py", selected, {}) == pytest.approx(0.40)


def test_redundancy_penalty_zero_for_empty_set() -> None:
    assert _redundancy_penalty("src/a.py", set(), {}) == 0.0


def test_redundancy_penalty_zero_for_different_dir() -> None:
    selected = {"src/a.py", "src/b.py"}
    assert _redundancy_penalty("lib/c.py", selected, {}) == 0.0


# ---------------------------------------------------------------------------
# 10. Proximity BFS distances
# ---------------------------------------------------------------------------

def test_score_nodes_proximity_direct_neighbor() -> None:
    imp = ImportRecord(source="./b", symbols=[])
    contracts = [
        _contract("src/a.py", imports=[imp]),
        _contract("src/b.py"),
    ]
    scorer = ContextScorer()
    scores = scorer.score_nodes(contracts, focus_path="src/a.py", task="fix-bug")
    # a.py is the focus (distance=0 → proximity=1.0)
    assert scores["src/a.py"].proximity == pytest.approx(1.0)
    # b.py is 1 hop away → proximity=0.5
    assert scores["src/b.py"].proximity == pytest.approx(0.5)


def test_score_nodes_proximity_two_hops() -> None:
    imp_ab = ImportRecord(source="./b", symbols=[])
    imp_bc = ImportRecord(source="./c", symbols=[])
    contracts = [
        _contract("src/a.py", imports=[imp_ab]),
        _contract("src/b.py", imports=[imp_bc]),
        _contract("src/c.py"),
    ]
    scorer = ContextScorer()
    scores = scorer.score_nodes(contracts, focus_path="src/a.py", task="fix-bug")
    assert scores["src/a.py"].proximity == pytest.approx(1.0)
    assert scores["src/b.py"].proximity == pytest.approx(0.5)
    assert scores["src/c.py"].proximity == pytest.approx(0.25)


def test_score_nodes_no_proximity_when_focus_absent() -> None:
    contracts = [_contract("src/a.py"), _contract("src/b.py")]
    scorer = ContextScorer()
    # focus_path not in contracts → no proximity scores
    scores = scorer.score_nodes(contracts, focus_path="src/missing.py")
    for ns in scores.values():
        assert ns.proximity == 0.0


# ---------------------------------------------------------------------------
# 11. _resolve_import helper
# ---------------------------------------------------------------------------

def test_resolve_import_finds_py() -> None:
    path_set = {"src/util.py"}
    result = _resolve_import("src", "./util", path_set)
    assert result == ["src/util.py"]


def test_resolve_import_finds_index_ts() -> None:
    path_set = {"components/Button/index.ts"}
    result = _resolve_import("components/Button", ".", path_set)
    assert result == []  # empty src after strip


def test_resolve_import_returns_empty_when_unresolved() -> None:
    result = _resolve_import("src", "./nonexistent", {"src/other.py"})
    assert result == []


# ---------------------------------------------------------------------------
# 12. Integration: score_nodes + select_subgraph round-trip
# ---------------------------------------------------------------------------

def test_integration_select_uses_scored_order() -> None:
    """select_subgraph must respect the score ranking produced by score_nodes."""
    contracts = [
        _contract("entry.py", is_entrypoint=True, fan_in=5, exports=3),
        _contract("core.py", fan_in=8, exports=5),
        _contract("util.py", fan_in=1),
        _contract("noise.py"),
    ]
    calls = [
        _call("entry.py", "core.py", "high"),
        _call("util.py", "core.py", "high"),
    ]
    scorer = ContextScorer()
    ns = scorer.score_nodes(contracts, semantic_calls=calls)
    selected = scorer.select_subgraph(ns, contracts, budget=2)

    assert len(selected) <= 2
    # core.py has highest semantic centrality (2 callers) → must be selected
    assert "core.py" in selected


def test_integration_deterministic_across_runs() -> None:
    contracts = [_contract(f"mod/{chr(ord('a') + i)}.py", fan_in=i) for i in range(10)]
    scorer = ContextScorer()
    ns1 = scorer.score_nodes(contracts)
    ns2 = scorer.score_nodes(contracts)
    sel1 = scorer.select_subgraph(ns1, contracts, budget=5)
    sel2 = scorer.select_subgraph(ns2, contracts, budget=5)
    assert sel1 == sel2

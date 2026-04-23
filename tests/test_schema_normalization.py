from __future__ import annotations

"""Regression tests for normalize_source_map / validate_source_map.

Covers the three project archetypes requested:
  - empty project  (SourceMap with all defaults)
  - python project (stacks + entry_points, no optional analyzers run)
  - multi-stack    (python + nodejs, partial optional fields populated)

Each test asserts that after normalization the schema contracts hold and that
validate_source_map() does not raise.
"""

from dataclasses import replace

import pytest

from sourcecode.schema import (
    ArchitectureAnalysis,
    ArchitectureDomain,
    EntryPoint,
    GraphEdge,
    GraphNode,
    ModuleGraph,
    ModuleGraphSummary,
    SourceMap,
    StackDetection,
)
from sourcecode.serializer import normalize_source_map, validate_source_map


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalized_valid(sm: SourceMap) -> SourceMap:
    """Normalize then validate; raises if validation fails."""
    out = normalize_source_map(sm)
    validate_source_map(out)  # must not raise
    return out


# ---------------------------------------------------------------------------
# 1. Empty project
# ---------------------------------------------------------------------------

class TestEmptyProject:
    def test_architecture_not_null(self) -> None:
        sm = _normalized_valid(SourceMap())
        assert sm.architecture is not None

    def test_architecture_not_requested(self) -> None:
        sm = _normalized_valid(SourceMap())
        assert sm.architecture.requested is False

    def test_architecture_has_valid_confidence(self) -> None:
        sm = _normalized_valid(SourceMap())
        assert sm.architecture.confidence in ("high", "medium", "low")

    def test_architecture_domains_is_list(self) -> None:
        sm = _normalized_valid(SourceMap())
        assert isinstance(sm.architecture.domains, list)

    def test_module_graph_not_null(self) -> None:
        sm = _normalized_valid(SourceMap())
        assert sm.module_graph is not None

    def test_module_graph_nodes_is_list(self) -> None:
        sm = _normalized_valid(SourceMap())
        assert isinstance(sm.module_graph.nodes, list)

    def test_module_graph_edges_is_list(self) -> None:
        sm = _normalized_valid(SourceMap())
        assert isinstance(sm.module_graph.edges, list)

    def test_module_graph_summary_synced(self) -> None:
        sm = _normalized_valid(SourceMap())
        assert sm.module_graph_summary is not None
        assert sm.module_graph_summary.requested is False

    def test_dependencies_is_list(self) -> None:
        sm = _normalized_valid(SourceMap())
        assert isinstance(sm.dependencies, list)

    def test_idempotent(self) -> None:
        sm = SourceMap()
        once = normalize_source_map(sm)
        twice = normalize_source_map(once)
        # Normalizing again must not change anything
        assert once.architecture is twice.architecture
        assert once.module_graph is twice.module_graph


# ---------------------------------------------------------------------------
# 2. Python project (no optional analyzers run)
# ---------------------------------------------------------------------------

class TestPythonProject:
    def _sm(self) -> SourceMap:
        return SourceMap(
            stacks=[
                StackDetection(
                    stack="python",
                    detection_method="manifest",
                    confidence="high",
                    primary=True,
                )
            ],
            project_type="cli",
            entry_points=[
                EntryPoint(path="src/sourcecode/cli.py", stack="python", kind="cli")
            ],
        )

    def test_contracts_hold(self) -> None:
        _normalized_valid(self._sm())

    def test_architecture_default_not_requested(self) -> None:
        sm = _normalized_valid(self._sm())
        assert sm.architecture.requested is False

    def test_module_graph_empty_default(self) -> None:
        sm = _normalized_valid(self._sm())
        assert sm.module_graph.nodes == []
        assert sm.module_graph.edges == []

    def test_dependencies_list_even_when_empty(self) -> None:
        sm = _normalized_valid(self._sm())
        assert sm.dependencies == []

    def test_stacks_preserved(self) -> None:
        sm = _normalized_valid(self._sm())
        assert len(sm.stacks) == 1
        assert sm.stacks[0].stack == "python"


# ---------------------------------------------------------------------------
# 3. Multi-stack project (python + nodejs, partial optional fields)
# ---------------------------------------------------------------------------

class TestMultiStackProject:
    def _sm(self) -> SourceMap:
        return SourceMap(
            stacks=[
                StackDetection(stack="python", detection_method="manifest", primary=True),
                StackDetection(stack="nodejs", detection_method="manifest"),
            ],
            project_type="fullstack",
        )

    def test_contracts_hold(self) -> None:
        _normalized_valid(self._sm())

    def test_both_stacks_preserved(self) -> None:
        sm = _normalized_valid(self._sm())
        names = {s.stack for s in sm.stacks}
        assert names == {"python", "nodejs"}

    def test_architecture_null_filled(self) -> None:
        sm = _normalized_valid(self._sm())
        assert sm.architecture is not None

    def test_module_graph_null_filled(self) -> None:
        sm = _normalized_valid(self._sm())
        assert sm.module_graph is not None


# ---------------------------------------------------------------------------
# Populated fields must be preserved (not overwritten by normalization)
# ---------------------------------------------------------------------------

class TestPopulatedFieldsPreserved:
    def test_existing_architecture_kept(self) -> None:
        arch = ArchitectureAnalysis(
            requested=True,
            pattern="layered",
            confidence="high",
            domains=[ArchitectureDomain(name="cli"), ArchitectureDomain(name="core")],
        )
        sm = _normalized_valid(SourceMap(architecture=arch))
        assert sm.architecture.requested is True
        assert sm.architecture.pattern == "layered"
        assert len(sm.architecture.domains) == 2

    def test_existing_module_graph_kept(self) -> None:
        node = GraphNode(id="cli", kind="module", language="python", path="src/cli.py")
        graph = ModuleGraph(nodes=[node], summary=ModuleGraphSummary(requested=True))
        sm = _normalized_valid(SourceMap(module_graph=graph))
        assert len(sm.module_graph.nodes) == 1
        assert sm.module_graph.nodes[0].id == "cli"

    def test_module_graph_summary_synced_from_graph(self) -> None:
        summary = ModuleGraphSummary(requested=True, node_count=5)
        graph = ModuleGraph(summary=summary)
        sm = normalize_source_map(SourceMap(module_graph=graph))
        # module_graph_summary must be synced to the graph's own summary
        assert sm.module_graph_summary is not None
        assert sm.module_graph_summary.node_count == 5


# ---------------------------------------------------------------------------
# validate_source_map catches violations explicitly
# ---------------------------------------------------------------------------

class TestValidateContracts:
    def test_raises_on_null_architecture(self) -> None:
        sm = SourceMap()
        # module_graph also None, but architecture is checked first
        with pytest.raises(ValueError, match="architecture"):
            validate_source_map(sm)

    def test_raises_on_null_module_graph(self) -> None:
        sm = replace(
            SourceMap(),
            architecture=ArchitectureAnalysis(requested=False),
        )
        with pytest.raises(ValueError, match="module_graph"):
            validate_source_map(sm)

    def test_error_lists_all_violations(self) -> None:
        # Both architecture and module_graph are None → both must appear in message
        try:
            validate_source_map(SourceMap())
        except ValueError as exc:
            msg = str(exc)
            assert "architecture" in msg
            assert "module_graph" in msg
        else:
            pytest.fail("validate_source_map should have raised")

    def test_passes_after_normalize(self) -> None:
        # normalize then validate must never raise for a default SourceMap
        validate_source_map(normalize_source_map(SourceMap()))

    def test_invalid_confidence_caught(self) -> None:
        bad_arch = ArchitectureAnalysis(
            requested=True,
            confidence="very_high",  # type: ignore[arg-type]
        )
        sm = replace(
            normalize_source_map(SourceMap()),
            architecture=bad_arch,
        )
        with pytest.raises(ValueError, match="confidence"):
            validate_source_map(sm)

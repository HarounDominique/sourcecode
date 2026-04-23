from __future__ import annotations

"""Tests for validate_cross_analyzer_consistency.

Three rules under test:
  Rule 1 — dependency_graph:
    GraphNode file-paths must be in file_tree; when the graph emits
    external-package edge targets, those must match declared dependencies.
  Rule 2 — semantic_file_tree:
    SymbolLink.importer_path and (non-external) source_path must be in file_tree.
  Rule 3 — architecture_graph:
    Every file in ArchitectureDomain.files must be in file_tree.
"""

from dataclasses import replace

import pytest

from sourcecode.schema import (
    ArchitectureAnalysis,
    ArchitectureDomain,
    DependencyRecord,
    DependencySummary,
    GraphEdge,
    GraphNode,
    ModuleGraph,
    ModuleGraphSummary,
    SourceMap,
    SymbolLink,
)
from sourcecode.serializer import (
    normalize_source_map,
    validate_cross_analyzer_consistency,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _base_sm(**kwargs) -> SourceMap:
    """Return a normalised SourceMap; override any field via kwargs."""
    return normalize_source_map(SourceMap(**kwargs))


# ---------------------------------------------------------------------------
# Baseline: clean SourceMap produces no findings
# ---------------------------------------------------------------------------

def test_no_findings_for_empty_source_map() -> None:
    findings = validate_cross_analyzer_consistency(_base_sm())
    assert findings == []


def test_no_findings_when_no_analyzers_requested() -> None:
    sm = _base_sm(file_paths=["src/main.py", "src/utils.py"])
    findings = validate_cross_analyzer_consistency(sm)
    assert findings == []


# ---------------------------------------------------------------------------
# Rule 1a — graph node paths must be in file_tree
# ---------------------------------------------------------------------------

class TestRule1aGraphNodeFileTree:
    def _sm_with_phantom_node(self) -> SourceMap:
        graph = ModuleGraph(
            nodes=[
                GraphNode(id="real",   kind="module", language="python", path="src/main.py"),
                GraphNode(id="ghost",  kind="module", language="python", path="src/ghost.py"),
            ],
            edges=[
                GraphEdge(source="src/main.py", target="src/ghost.py", kind="imports"),
            ],
            summary=ModuleGraphSummary(requested=True),
        )
        return normalize_source_map(SourceMap(
            file_paths=["src/main.py"],   # ghost.py is NOT here
            module_graph=graph,
            module_graph_summary=graph.summary,
        ))

    def test_phantom_node_produces_finding(self) -> None:
        findings = validate_cross_analyzer_consistency(self._sm_with_phantom_node())
        assert any("ghost.py" in f and "dependency_graph" in f for f in findings)

    def test_real_node_produces_no_finding(self) -> None:
        findings = validate_cross_analyzer_consistency(self._sm_with_phantom_node())
        assert not any("main.py" in f for f in findings)

    def test_directory_node_never_flagged(self) -> None:
        # Nodes with no code extension (directory nodes) must not trigger rule 1a
        graph = ModuleGraph(
            nodes=[GraphNode(id="pkg", kind="module", language="python", path="src")],
            edges=[],
            summary=ModuleGraphSummary(requested=True),
        )
        sm = normalize_source_map(SourceMap(
            file_paths=["src/main.py"],
            module_graph=graph,
            module_graph_summary=graph.summary,
        ))
        findings = validate_cross_analyzer_consistency(sm)
        assert not any("dependency_graph" in f for f in findings)

    def test_skipped_when_graph_not_requested(self) -> None:
        # Graph not requested → rule 1 must be silent
        sm = normalize_source_map(SourceMap(file_paths=["src/main.py"]))
        assert sm.module_graph.summary.requested is False
        findings = validate_cross_analyzer_consistency(sm)
        assert not any("dependency_graph" in f for f in findings)


# ---------------------------------------------------------------------------
# Rule 1b — mismatched dependency vs graph external edges
# ---------------------------------------------------------------------------

class TestRule1bDependencyGraph:
    def _sm_with_mismatch(self) -> SourceMap:
        """Graph has external edges (pkg-like targets) but dependency not in them."""
        graph = ModuleGraph(
            nodes=[GraphNode(id="m", kind="module", language="python", path="src/main.py")],
            edges=[
                # 'requests' is an external package target (no slash, no extension)
                GraphEdge(source="src/main.py", target="requests", kind="imports"),
            ],
            summary=ModuleGraphSummary(requested=True),
        )
        sm = SourceMap(
            file_paths=["src/main.py"],
            dependencies=[
                DependencyRecord(name="my_lib", ecosystem="python", source="manifest"),
            ],
            dependency_summary=DependencySummary(requested=True, total_count=1),
            module_graph=graph,
            module_graph_summary=graph.summary,
        )
        return normalize_source_map(sm)

    def test_missing_dep_in_graph_produces_finding(self) -> None:
        findings = validate_cross_analyzer_consistency(self._sm_with_mismatch())
        assert any(
            "my_lib" in f and "dependency_graph" in f
            for f in findings
        ), f"Expected my_lib finding, got: {findings}"

    def test_transitive_deps_not_checked(self) -> None:
        graph = ModuleGraph(
            edges=[GraphEdge(source="a.py", target="requests", kind="imports")],
            summary=ModuleGraphSummary(requested=True),
        )
        sm = normalize_source_map(SourceMap(
            file_paths=["a.py"],
            dependencies=[
                DependencyRecord(
                    name="transitive_pkg",
                    ecosystem="python",
                    source="lockfile",
                    scope="transitive",
                ),
            ],
            dependency_summary=DependencySummary(requested=True),
            module_graph=graph,
            module_graph_summary=graph.summary,
        ))
        findings = validate_cross_analyzer_consistency(sm)
        assert not any("transitive_pkg" in f for f in findings)

    def test_internal_only_graph_skips_dep_check(self) -> None:
        # Graph edges only have file-path targets → rule 1b must stay silent
        graph = ModuleGraph(
            edges=[
                GraphEdge(source="src/a.py", target="src/b.py", kind="imports"),
            ],
            summary=ModuleGraphSummary(requested=True),
        )
        sm = normalize_source_map(SourceMap(
            file_paths=["src/a.py", "src/b.py"],
            dependencies=[DependencyRecord(name="typer", ecosystem="python", source="manifest")],
            dependency_summary=DependencySummary(requested=True),
            module_graph=graph,
            module_graph_summary=graph.summary,
        ))
        findings = validate_cross_analyzer_consistency(sm)
        # No external pkg targets → dep↔edge check must be skipped
        assert not any("dependency_graph" in f for f in findings)

    def test_dep_present_in_external_edges_no_finding(self) -> None:
        graph = ModuleGraph(
            edges=[GraphEdge(source="src/main.py", target="typer", kind="imports")],
            summary=ModuleGraphSummary(requested=True),
        )
        sm = normalize_source_map(SourceMap(
            file_paths=["src/main.py"],
            dependencies=[DependencyRecord(name="typer", ecosystem="python", source="manifest")],
            dependency_summary=DependencySummary(requested=True),
            module_graph=graph,
            module_graph_summary=graph.summary,
        ))
        findings = validate_cross_analyzer_consistency(sm)
        assert not any("typer" in f for f in findings)


# ---------------------------------------------------------------------------
# Rule 2 — orphan semantic links
# ---------------------------------------------------------------------------

class TestRule2SemanticFileTree:
    def test_orphan_importer_path_flagged(self) -> None:
        sm = _base_sm(
            file_paths=["src/main.py"],
            semantic_links=[
                SymbolLink(
                    importer_path="src/ghost.py",   # not in file_paths
                    symbol="helper",
                    is_external=False,
                )
            ],
        )
        findings = validate_cross_analyzer_consistency(sm)
        assert any("ghost.py" in f and "semantic_file_tree" in f for f in findings)

    def test_orphan_source_path_flagged(self) -> None:
        sm = _base_sm(
            file_paths=["src/main.py"],
            semantic_links=[
                SymbolLink(
                    importer_path="src/main.py",
                    symbol="helper",
                    source_path="src/phantom.py",   # not in file_paths
                    is_external=False,
                )
            ],
        )
        findings = validate_cross_analyzer_consistency(sm)
        assert any("phantom.py" in f and "semantic_file_tree" in f for f in findings)

    def test_external_source_path_not_flagged(self) -> None:
        sm = _base_sm(
            file_paths=["src/main.py"],
            semantic_links=[
                SymbolLink(
                    importer_path="src/main.py",
                    symbol="Typer",
                    source_path="typer/__init__.py",  # outside project
                    is_external=True,                  # marked external → skip
                )
            ],
        )
        findings = validate_cross_analyzer_consistency(sm)
        assert not any("semantic_file_tree" in f for f in findings)

    def test_valid_link_produces_no_finding(self) -> None:
        sm = _base_sm(
            file_paths=["src/main.py", "src/utils.py"],
            semantic_links=[
                SymbolLink(
                    importer_path="src/main.py",
                    symbol="helper",
                    source_path="src/utils.py",
                    is_external=False,
                )
            ],
        )
        findings = validate_cross_analyzer_consistency(sm)
        assert not any("semantic_file_tree" in f for f in findings)

    def test_no_links_no_findings(self) -> None:
        sm = _base_sm(file_paths=["src/main.py"])
        assert validate_cross_analyzer_consistency(sm) == []


# ---------------------------------------------------------------------------
# Rule 3 — inconsistent architecture domains
# ---------------------------------------------------------------------------

class TestRule3ArchitectureGraph:
    def test_phantom_domain_file_flagged(self) -> None:
        arch = ArchitectureAnalysis(
            requested=True,
            pattern="layered",
            confidence="medium",
            domains=[
                ArchitectureDomain(name="phantom", files=["src/nonexistent.py"]),
                ArchitectureDomain(name="real",    files=["src/main.py"]),
            ],
        )
        sm = normalize_source_map(SourceMap(
            file_paths=["src/main.py"],
            architecture=arch,
        ))
        findings = validate_cross_analyzer_consistency(sm)
        assert any("nonexistent.py" in f and "architecture_graph" in f for f in findings)

    def test_real_domain_file_not_flagged(self) -> None:
        arch = ArchitectureAnalysis(
            requested=True,
            pattern="modular",
            confidence="medium",
            domains=[ArchitectureDomain(name="core", files=["src/main.py"])],
        )
        sm = normalize_source_map(SourceMap(
            file_paths=["src/main.py"],
            architecture=arch,
        ))
        findings = validate_cross_analyzer_consistency(sm)
        assert not any("core" in f for f in findings)

    def test_not_requested_architecture_skipped(self) -> None:
        # architecture.requested=False → rule 3 must be silent even if domains exist
        arch = ArchitectureAnalysis(
            requested=False,
            domains=[ArchitectureDomain(name="ghost", files=["nowhere.py"])],
        )
        sm = normalize_source_map(SourceMap(
            file_paths=["src/main.py"],
            architecture=arch,
        ))
        findings = validate_cross_analyzer_consistency(sm)
        assert not any("architecture_graph" in f for f in findings)


# ---------------------------------------------------------------------------
# strict=True / strict=False mode
# ---------------------------------------------------------------------------

class TestStrictMode:
    def _sm_with_orphan(self) -> SourceMap:
        return _base_sm(
            file_paths=["src/main.py"],
            semantic_links=[
                SymbolLink(importer_path="ghost.py", symbol="x", is_external=False)
            ],
        )

    def test_strict_false_returns_findings_not_raises(self) -> None:
        findings = validate_cross_analyzer_consistency(
            self._sm_with_orphan(), strict=False
        )
        assert len(findings) > 0

    def test_strict_true_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="consistency"):
            validate_cross_analyzer_consistency(self._sm_with_orphan(), strict=True)

    def test_strict_true_error_lists_all_violations(self) -> None:
        sm = _base_sm(
            file_paths=["src/main.py"],
            semantic_links=[
                SymbolLink(importer_path="ghost1.py", symbol="a", is_external=False),
                SymbolLink(importer_path="ghost2.py", symbol="b", is_external=False),
            ],
        )
        try:
            validate_cross_analyzer_consistency(sm, strict=True)
        except ValueError as exc:
            msg = str(exc)
            assert "ghost1.py" in msg
            assert "ghost2.py" in msg
        else:
            pytest.fail("should have raised")

    def test_clean_sm_strict_true_does_not_raise(self) -> None:
        sm = normalize_source_map(SourceMap(file_paths=["src/main.py"]))
        validate_cross_analyzer_consistency(sm, strict=True)  # must not raise

from __future__ import annotations

from pathlib import Path

from sourcecode.graph_analyzer import GraphAnalyzer
from sourcecode.scanner import FileScanner


def _scan_tree(root: Path) -> dict[str, object]:
    return FileScanner(root, max_depth=6).scan_tree()


def test_go_graph_builds_internal_import_edges(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module example.com/demo\n\ngo 1.22\n")
    (tmp_path / "internal").mkdir()
    (tmp_path / "internal" / "helper.go").write_text(
        """
package helper

func Helper() {}
        """.strip()
    )
    (tmp_path / "main.go").write_text(
        """
package main

import "example.com/demo/internal"

func main() {
    helper.Helper()
}
        """.strip()
    )

    graph = GraphAnalyzer().analyze(tmp_path, _scan_tree(tmp_path), detail="full")

    edge_pairs = {(edge.source, edge.target, edge.kind) for edge in graph.edges}
    assert ("module:main.go", "module:internal/helper.go", "imports") in edge_pairs
    assert any(node.language == "go" for node in graph.nodes)


def test_jvm_graph_builds_imports_and_extends_edges(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "Base.java").write_text(
        """
package demo;

public class Base {}
        """.strip()
    )
    (tmp_path / "src" / "App.java").write_text(
        """
package demo;

import demo.Base;

public class App extends Base {}
        """.strip()
    )

    graph = GraphAnalyzer().analyze(tmp_path, _scan_tree(tmp_path), detail="full")

    edge_pairs = {(edge.source, edge.target, edge.kind) for edge in graph.edges}
    assert ("module:src/App.java", "module:src/Base.java", "imports") in edge_pairs
    assert ("class:src/App.java:App", "class:src/Base.java:Base", "extends") in edge_pairs


def test_graph_respects_workspace_context(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "main.py").write_text("def run():\n    return 1\n")

    graph = GraphAnalyzer().analyze(
        tmp_path,
        _scan_tree(tmp_path),
        workspace="packages/api",
        detail="full",
    )

    assert all(node.workspace == "packages/api" for node in graph.nodes)


def test_high_detail_applies_node_budget_and_marks_truncation(tmp_path: Path) -> None:
    for index in range(6):
        package_dir = tmp_path / f"pkg_{index}"
        package_dir.mkdir()
        (package_dir / "__init__.py").write_text("")
        import_line = f"from pkg_{index + 1}.main import helper\n\n" if index < 5 else ""
        (package_dir / "main.py").write_text(
            f"{import_line}def helper():\n    return {index}\n"
        )

    graph = GraphAnalyzer().analyze(
        tmp_path,
        _scan_tree(tmp_path),
        detail="high",
        max_nodes=3,
    )

    assert len(graph.nodes) <= 3
    assert graph.summary.truncated is True
    assert any(item.startswith("node_budget_applied:") for item in graph.summary.limitations)

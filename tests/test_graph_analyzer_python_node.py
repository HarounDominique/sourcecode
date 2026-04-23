from __future__ import annotations

from pathlib import Path

from sourcecode.graph_analyzer import GraphAnalyzer
from sourcecode.scanner import FileScanner


def _scan_tree(root: Path) -> dict[str, object]:
    return FileScanner(root, max_depth=6).scan_tree()


def test_python_graph_builds_imports_calls_and_extends(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("")
    (tmp_path / "app" / "utils.py").write_text(
        """
class Base:
    pass

def helper():
    return 1
        """.strip()
    )
    (tmp_path / "app" / "main.py").write_text(
        """
from .utils import Base, helper

class Derived(Base):
    pass

def run():
    return helper()
        """.strip()
    )

    graph = GraphAnalyzer().analyze(tmp_path, _scan_tree(tmp_path), detail="full")

    node_ids = {node.id for node in graph.nodes}
    edge_pairs = {(edge.source, edge.target, edge.kind) for edge in graph.edges}
    assert "module:app/main.py" in node_ids
    assert "function:app/main.py:run" in node_ids
    assert "class:app/main.py:Derived" in node_ids
    assert ("module:app/main.py", "module:app/utils.py", "imports") in edge_pairs
    assert ("function:app/main.py:run", "function:app/utils.py:helper", "calls") in edge_pairs
    assert any(edge.kind == "calls" for edge in graph.edges)
    assert any(edge.kind == "extends" for edge in graph.edges)
    assert "python" in graph.summary.languages


def test_python_graph_reports_parse_errors_without_crashing(tmp_path: Path) -> None:
    (tmp_path / "broken.py").write_text("def broken(:\n    pass\n")

    graph = GraphAnalyzer().analyze(tmp_path, _scan_tree(tmp_path), detail="full")

    assert graph.summary.requested is True
    assert any(item == "python_parse_error:broken.py" for item in graph.summary.limitations)


def test_node_graph_builds_imports_and_contains(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "utils.ts").write_text("export function helper() { return 1; }\n")
    (tmp_path / "src" / "index.ts").write_text(
        """
import { helper } from "./utils";
export function run() { return helper(); }
        """.strip()
    )

    graph = GraphAnalyzer().analyze(tmp_path, _scan_tree(tmp_path), detail="full")

    node_ids = {node.id for node in graph.nodes}
    edge_pairs = {(edge.source, edge.target, edge.kind) for edge in graph.edges}
    assert "module:src/index.ts" in node_ids
    assert "function:src/index.ts:run" in node_ids
    assert ("module:src/index.ts", "module:src/utils.ts", "imports") in edge_pairs
    assert "nodejs" in graph.summary.languages


def test_node_graph_marks_unresolved_relative_imports_in_summary(tmp_path: Path) -> None:
    (tmp_path / "index.js").write_text("const missing = require('./missing');\n")

    graph = GraphAnalyzer().analyze(tmp_path, _scan_tree(tmp_path), detail="full")

    assert any(item == "node_unresolved:index.js:./missing" for item in graph.summary.limitations)


def test_high_detail_keeps_modules_and_imports_only(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("")
    (tmp_path / "app" / "utils.py").write_text("def helper():\n    return 1\n")
    (tmp_path / "app" / "main.py").write_text("from .utils import helper\n\ndef run():\n    return helper()\n")

    graph = GraphAnalyzer().analyze(
        tmp_path,
        _scan_tree(tmp_path),
        detail="high",
        entry_points=[],
    )

    assert all(node.kind == "module" for node in graph.nodes)
    assert all(edge.kind == "imports" for edge in graph.edges)
    assert graph.summary.detail == "high"


def test_medium_detail_keeps_key_functions_but_filters_contains_edges(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("")
    (tmp_path / "app" / "utils.py").write_text("def helper():\n    return 1\n")
    (tmp_path / "app" / "main.py").write_text("from .utils import helper\n\ndef run():\n    return helper()\n")

    graph = GraphAnalyzer().analyze(
        tmp_path,
        _scan_tree(tmp_path),
        detail="medium",
        edge_kinds={"imports", "calls"},
        entry_points=[],
    )

    assert any(node.kind == "function" for node in graph.nodes)
    assert any(edge.kind == "calls" for edge in graph.edges)
    assert not any(edge.kind == "contains" for edge in graph.edges)

from __future__ import annotations

import json

from sourcecode.schema import GraphEdge, GraphNode, ModuleGraph, ModuleGraphSummary, SourceMap
from sourcecode.serializer import compact_view, to_json


def test_source_map_graph_defaults_are_backward_compatible() -> None:
    source_map = SourceMap()

    assert source_map.module_graph is None


def test_module_graph_serializes_nodes_edges_and_summary() -> None:
    source_map = SourceMap(
        module_graph=ModuleGraph(
            nodes=[
                GraphNode(
                    id="module:src/main.py",
                    kind="module",
                    language="python",
                    path="src/main.py",
                    display_name="src/main.py",
                ),
                GraphNode(
                    id="function:src/main.py:run",
                    kind="function",
                    language="python",
                    path="src/main.py",
                    symbol="run",
                    display_name="run",
                    importance="high",
                ),
            ],
            edges=[
                GraphEdge(
                    source="module:src/main.py",
                    target="function:src/main.py:run",
                    kind="contains",
                    confidence="high",
                    method="ast",
                ),
                GraphEdge(
                    source="function:src/main.py:run",
                    target="module:src/utils.py",
                    kind="imports",
                    confidence="medium",
                    method="heuristic",
                ),
            ],
            summary=ModuleGraphSummary(
                requested=True,
                node_count=2,
                edge_count=2,
                languages=["python"],
                methods=["ast", "heuristic"],
                main_flows=["src/main.py -> src/utils.py"],
                layers=["src"],
                entry_points_count=1,
                truncated=False,
                detail="high",
                max_nodes_applied=80,
                edge_kinds=["imports"],
            ),
        ),
        module_graph_summary=ModuleGraphSummary(requested=True, detail="high"),
    )

    data = json.loads(to_json(source_map))

    assert data["module_graph"]["nodes"][0]["kind"] == "module"
    assert data["module_graph"]["edges"][0]["kind"] == "contains"
    assert data["module_graph"]["summary"]["requested"] is True
    assert data["module_graph"]["summary"]["edge_count"] == 2
    assert data["module_graph"]["nodes"][1]["importance"] == "high"
    assert data["module_graph"]["summary"]["main_flows"] == ["src/main.py -> src/utils.py"]
    assert data["module_graph_summary"]["detail"] == "high"


def test_compact_view_excludes_module_graph() -> None:
    source_map = SourceMap(module_graph=ModuleGraph(), module_graph_summary=ModuleGraphSummary())

    data = compact_view(source_map)

    assert "module_graph" not in data
    assert "module_graph_summary" not in data

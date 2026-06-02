"""test_spring_model.py — Tests for spring_model.py.

Coverage:
  CA  CallAdjacency — build from CIR call_graph
  IG  InheritanceGraph — build from CIR dependencies
  BG  BeanGraph — build from _raw_ir nodes + injects edges
  SM  SpringSemanticModel — umbrella build, tx_index reuse
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from sourcecode.spring_model import (
    BeanGraph,
    BeanNode,
    CallAdjacency,
    InheritanceGraph,
    SpringSemanticModel,
)
from sourcecode.spring_semantic import TransactionBoundaryIndex


# ---------------------------------------------------------------------------
# Shared fake CIR
# ---------------------------------------------------------------------------

class _FakeCIR:
    def __init__(
        self,
        nodes: list[dict] | None = None,
        edges: list[dict] | None = None,
        dependencies: list[dict] | None = None,
    ):
        self._raw_ir = {"graph": {"nodes": nodes or [], "edges": edges or []}}
        self.cir_hash = "deadbeef00000000"
        self.call_graph = edges or []
        self.dependencies = dependencies or []
        self.metadata: dict = {}


# ---------------------------------------------------------------------------
# CA — CallAdjacency
# ---------------------------------------------------------------------------

class TestCallAdjacency:
    def test_empty(self):
        cir = _FakeCIR()
        adj = CallAdjacency.build(cir)
        assert adj.adjacency == {}

    def test_call_edge_captured(self):
        edges = [{"from": "pkg.A#foo", "to": "pkg.B#bar", "type": "calls"}]
        cir = _FakeCIR(edges=edges)
        adj = CallAdjacency.build(cir)
        assert "pkg.A#foo" in adj.adjacency
        assert "pkg.B#bar" in adj.adjacency["pkg.A#foo"]

    def test_structural_edges_excluded(self):
        edges = [
            {"from": "pkg.A", "to": "pkg.B", "type": "annotated_with"},
            {"from": "pkg.C", "to": "pkg.D", "type": "mapped_to"},
            {"from": "pkg.E", "to": "pkg.F", "type": "contained_in"},
        ]
        cir = _FakeCIR(edges=edges)
        adj = CallAdjacency.build(cir)
        assert adj.adjacency == {}

    def test_multiple_callees(self):
        edges = [
            {"from": "A#m", "to": "B#x", "type": "calls"},
            {"from": "A#m", "to": "C#y", "type": "calls"},
        ]
        cir = _FakeCIR(edges=edges)
        adj = CallAdjacency.build(cir)
        assert sorted(adj.adjacency["A#m"]) == ["B#x", "C#y"]

    def test_non_dict_edge_skipped(self):
        cir = _FakeCIR(edges=["not-a-dict", None])  # type: ignore[arg-type]
        adj = CallAdjacency.build(cir)
        assert adj.adjacency == {}

    def test_injects_edge_included(self):
        edges = [{"from": "pkg.A", "to": "pkg.B", "type": "injects"}]
        cir = _FakeCIR(edges=edges)
        adj = CallAdjacency.build(cir)
        assert "pkg.A" in adj.adjacency


# ---------------------------------------------------------------------------
# IG — InheritanceGraph
# ---------------------------------------------------------------------------

class TestInheritanceGraph:
    def test_empty(self):
        cir = _FakeCIR()
        inh = InheritanceGraph.build(cir)
        assert inh.parent_of == {}
        assert inh.generic_parents == set()

    def test_extends_captured(self):
        deps = [{"from": "pkg.Child", "to": "pkg.Parent", "type": "extends"}]
        cir = _FakeCIR(dependencies=deps)
        inh = InheritanceGraph.build(cir)
        assert inh.parent_of["pkg.Child"] == "pkg.Parent"

    def test_generic_parent_detected(self):
        deps = [{"from": "pkg.Child", "to": "pkg.Base<User>", "type": "extends"}]
        cir = _FakeCIR(dependencies=deps)
        inh = InheritanceGraph.build(cir)
        assert "pkg.Child" in inh.generic_parents
        assert inh.has_generic_parent("pkg.Child")

    def test_non_generic_parent_not_flagged(self):
        deps = [{"from": "pkg.Child", "to": "pkg.PlainBase", "type": "extends"}]
        cir = _FakeCIR(dependencies=deps)
        inh = InheritanceGraph.build(cir)
        assert "pkg.Child" not in inh.generic_parents
        assert not inh.has_generic_parent("pkg.Child")

    def test_implements_not_captured(self):
        deps = [{"from": "pkg.Child", "to": "pkg.Iface", "type": "implements"}]
        cir = _FakeCIR(dependencies=deps)
        inh = InheritanceGraph.build(cir)
        assert inh.parent_of == {}

    def test_immediate_parent_api(self):
        deps = [{"from": "pkg.Child", "to": "pkg.Base<T>", "type": "extends"}]
        cir = _FakeCIR(dependencies=deps)
        inh = InheritanceGraph.build(cir)
        assert inh.immediate_parent("pkg.Child") == "pkg.Base<T>"
        assert inh.immediate_parent("unknown") == ""

    def test_non_dict_dep_skipped(self):
        cir = _FakeCIR(dependencies=["not-a-dict"])  # type: ignore[arg-type]
        inh = InheritanceGraph.build(cir)
        assert inh.parent_of == {}


# ---------------------------------------------------------------------------
# BG — BeanGraph
# ---------------------------------------------------------------------------

class TestBeanGraph:
    def _make_node(self, fqn: str, annotations: list[str], sf: str = "Foo.java") -> dict:
        return {"fqn": fqn, "annotations": annotations, "source_file": sf}

    def test_empty_no_beans(self):
        cir = _FakeCIR()
        bg = BeanGraph.build(cir)
        assert bg.beans == {}
        assert bg.injections == {}

    def test_service_detected(self):
        nodes = [self._make_node("pkg.UserService", ["@Service"])]
        cir = _FakeCIR(nodes=nodes)
        bg = BeanGraph.build(cir)
        assert "pkg.UserService" in bg.beans
        assert bg.beans["pkg.UserService"].stereotype == "service"
        assert bg.is_bean("pkg.UserService")

    def test_controller_detected(self):
        nodes = [self._make_node("pkg.FooController", ["@RestController"])]
        cir = _FakeCIR(nodes=nodes)
        bg = BeanGraph.build(cir)
        assert bg.get_stereotype("pkg.FooController") == "restcontroller"

    def test_component_detected(self):
        nodes = [self._make_node("pkg.Worker", ["@Component"])]
        cir = _FakeCIR(nodes=nodes)
        bg = BeanGraph.build(cir)
        assert bg.is_bean("pkg.Worker")
        assert bg.get_stereotype("pkg.Worker") == "component"

    def test_non_bean_ignored(self):
        nodes = [self._make_node("pkg.Util", ["@SomeOtherAnnotation"])]
        cir = _FakeCIR(nodes=nodes)
        bg = BeanGraph.build(cir)
        assert not bg.is_bean("pkg.Util")
        assert bg.get_stereotype("pkg.Util") == ""

    def test_injections_captured(self):
        nodes = [
            self._make_node("pkg.A", ["@Service"]),
            self._make_node("pkg.B", ["@Repository"]),
        ]
        edges = [{"from": "pkg.A", "to": "pkg.B", "type": "injects"}]
        cir = _FakeCIR(nodes=nodes, edges=edges)
        bg = BeanGraph.build(cir)
        assert "pkg.B" in bg.injections.get("pkg.A", [])

    def test_no_fqn_node_skipped(self):
        nodes = [{"annotations": ["@Service"], "source_file": "X.java"}]
        cir = _FakeCIR(nodes=nodes)
        bg = BeanGraph.build(cir)
        assert bg.beans == {}


# ---------------------------------------------------------------------------
# SM — SpringSemanticModel
# ---------------------------------------------------------------------------

class TestSpringSemanticModel:
    def test_build_from_cir(self):
        nodes = [
            {
                "fqn": "pkg.Svc",
                "symbol_kind": "class",
                "annotations": ["@Transactional", "@Service"],
                "annotation_values": {"@Transactional": ""},
                "modifiers": [],
                "source_file": "Svc.java",
            }
        ]
        edges = [{"from": "pkg.Svc#doWork", "to": "pkg.Repo#save", "type": "calls"}]
        deps = [{"from": "pkg.Child", "to": "pkg.Base<T>", "type": "extends"}]
        cir = _FakeCIR(nodes=nodes, edges=edges, dependencies=deps)

        model = SpringSemanticModel.build(cir)

        assert isinstance(model.tx_index, TransactionBoundaryIndex)
        assert isinstance(model.call_adj, CallAdjacency)
        assert isinstance(model.inheritance, InheritanceGraph)
        assert isinstance(model.bean_graph, BeanGraph)
        assert model.build_time_ms >= 0.0

    def test_tx_index_reuse(self):
        cir = _FakeCIR()
        prebuilt = TransactionBoundaryIndex(repo_id="prebuilt")
        model = SpringSemanticModel.build(cir, tx_index=prebuilt)
        assert model.tx_index is prebuilt

    def test_call_adj_populated(self):
        edges = [{"from": "A#m", "to": "B#n", "type": "calls"}]
        cir = _FakeCIR(edges=edges)
        model = SpringSemanticModel.build(cir)
        assert "A#m" in model.call_adj.adjacency

    def test_inheritance_populated(self):
        deps = [{"from": "pkg.Child", "to": "pkg.Base<T>", "type": "extends"}]
        cir = _FakeCIR(dependencies=deps)
        model = SpringSemanticModel.build(cir)
        assert model.inheritance.has_generic_parent("pkg.Child")

    def test_bean_graph_populated(self):
        nodes = [{"fqn": "pkg.Svc", "annotations": ["@Service"], "source_file": "S.java"}]
        cir = _FakeCIR(nodes=nodes)
        model = SpringSemanticModel.build(cir)
        assert model.bean_graph.is_bean("pkg.Svc")

    def test_never_raises_on_bad_cir(self):
        class _BadCIR:
            _raw_ir = None
            cir_hash = ""
            call_graph = None  # type: ignore[assignment]
            dependencies = None  # type: ignore[assignment]
            metadata: dict = {}

        model = SpringSemanticModel.build(_BadCIR())  # type: ignore[arg-type]
        assert isinstance(model, SpringSemanticModel)

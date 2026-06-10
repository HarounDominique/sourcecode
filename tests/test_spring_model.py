"""test_spring_model.py — Tests for spring_model.py.

Coverage:
  CA  CallAdjacency — build from CIR call_graph
  IG  InheritanceGraph — build from CIR dependencies
  BG  BeanGraph — build from _raw_ir nodes + injects edges
  EI  EndpointIndex — build from CIR endpoints
  EG  EventGraph — build from call_graph event edges
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
    EndpointIndex,
    EventGraph,
    InheritanceGraph,
    SpringSemanticModel,
)
from sourcecode.spring_semantic import TransactionBoundaryIndex


# ---------------------------------------------------------------------------
# Shared fake CIR
# ---------------------------------------------------------------------------

class _FakeEndpoint:
    def __init__(self, controller_class: str, source_file: str = "") -> None:
        self.controller_class = controller_class
        self.source_file = source_file


class _FakeCIR:
    def __init__(
        self,
        nodes: list[dict] | None = None,
        edges: list[dict] | None = None,
        dependencies: list[dict] | None = None,
        endpoints: list | None = None,
        files: list[str] | None = None,
    ):
        self._raw_ir = {"graph": {"nodes": nodes or [], "edges": edges or []}}
        self.cir_hash = "deadbeef00000000"
        self.call_graph = edges or []
        self.dependencies = dependencies or []
        self.endpoints = endpoints or []
        self.files = files or []
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

    def test_meta_service_annotation_detected(self):
        # @DomainService is annotated with @Service — class using it should be a bean
        nodes = [
            {
                "fqn": "com.example.DomainService",
                "symbol_kind": "annotation",
                "annotations": ["@Service", "@Transactional"],
                "source_file": "DomainService.java",
            },
            {
                "fqn": "com.example.PatientServiceImpl",
                "symbol_kind": "class",
                "annotations": ["@DomainService"],
                "source_file": "PatientServiceImpl.java",
            },
        ]
        bg = BeanGraph.build(_FakeCIR(nodes=nodes))
        assert bg.is_bean("com.example.PatientServiceImpl")
        assert bg.get_stereotype("com.example.PatientServiceImpl") == "service"

    def test_meta_repository_annotation_detected(self):
        # @InfrastructureRepository is annotated with @Repository
        nodes = [
            {
                "fqn": "com.example.InfrastructureRepository",
                "symbol_kind": "annotation",
                "annotations": ["@Repository"],
                "source_file": "InfrastructureRepository.java",
            },
            {
                "fqn": "com.example.PatientDaoImpl",
                "symbol_kind": "class",
                "annotations": ["@InfrastructureRepository"],
                "source_file": "PatientDaoImpl.java",
            },
        ]
        bg = BeanGraph.build(_FakeCIR(nodes=nodes))
        assert bg.is_bean("com.example.PatientDaoImpl")
        assert bg.get_stereotype("com.example.PatientDaoImpl") == "repository"

    def test_annotation_type_node_not_added_as_bean(self):
        # The annotation-type definition itself must not appear as a bean
        nodes = [
            {
                "fqn": "com.example.DomainService",
                "symbol_kind": "annotation",
                "annotations": ["@Service"],
                "source_file": "DomainService.java",
            },
        ]
        bg = BeanGraph.build(_FakeCIR(nodes=nodes))
        assert not bg.is_bean("com.example.DomainService")


# ---------------------------------------------------------------------------
# EI — EndpointIndex
# ---------------------------------------------------------------------------

class TestEndpointIndex:
    def test_empty(self):
        cir = _FakeCIR()
        ei = EndpointIndex.build(cir)
        assert ei.by_controller == {}
        assert ei.controller_fqns == frozenset()

    def test_single_endpoint_indexed(self):
        ep = _FakeEndpoint("pkg.FooController", source_file="FooController.java")
        cir = _FakeCIR(endpoints=[ep])
        ei = EndpointIndex.build(cir)
        assert "pkg.FooController" in ei.controller_fqns
        assert ep in ei.endpoints_for("pkg.FooController")

    def test_source_file_from_endpoint(self):
        ep = _FakeEndpoint("pkg.FooController", source_file="FooController.java")
        cir = _FakeCIR(endpoints=[ep])
        ei = EndpointIndex.build(cir)
        assert ei.source_file("pkg.FooController") == "FooController.java"

    def test_source_file_fallback_from_files(self):
        ep = _FakeEndpoint("pkg.FooController", source_file="")
        cir = _FakeCIR(
            endpoints=[ep],
            files=["src/main/java/pkg/FooController.java"],
        )
        ei = EndpointIndex.build(cir)
        assert ei.source_file("pkg.FooController") == "src/main/java/pkg/FooController.java"

    def test_missing_controller_fqn_skipped(self):
        ep = _FakeEndpoint("", source_file="X.java")
        cir = _FakeCIR(endpoints=[ep])
        ei = EndpointIndex.build(cir)
        assert ei.by_controller == {}

    def test_multiple_endpoints_same_controller(self):
        ep1 = _FakeEndpoint("pkg.Ctrl", source_file="Ctrl.java")
        ep2 = _FakeEndpoint("pkg.Ctrl", source_file="Ctrl.java")
        cir = _FakeCIR(endpoints=[ep1, ep2])
        ei = EndpointIndex.build(cir)
        assert len(ei.endpoints_for("pkg.Ctrl")) == 2

    def test_unknown_controller_returns_empty(self):
        cir = _FakeCIR()
        ei = EndpointIndex.build(cir)
        assert ei.endpoints_for("pkg.Unknown") == []
        assert ei.source_file("pkg.Unknown") == ""


# ---------------------------------------------------------------------------
# EG — EventGraph
# ---------------------------------------------------------------------------

class TestEventGraph:
    def test_empty(self):
        cir = _FakeCIR()
        eg = EventGraph.build(cir)
        assert eg.publishers == {}
        assert eg.listeners == {}
        assert eg.event_types == frozenset()
        assert eg.total_edges == 0
        assert not eg.has_events()

    def test_publish_edge_captured(self):
        edges = [{"from": "pkg.OrderService#place", "to": "OrderPlacedEvent", "type": "publishes_event"}]
        cir = _FakeCIR(edges=edges)
        eg = EventGraph.build(cir)
        assert "OrderPlacedEvent" in eg.event_types
        assert "pkg.OrderService#place" in eg.publishers_of("OrderPlacedEvent")
        assert eg.total_edges == 1
        assert eg.has_events()

    def test_listen_edge_captured(self):
        edges = [{"from": "pkg.NotifService#onOrder", "to": "OrderPlacedEvent", "type": "listens_to_event"}]
        cir = _FakeCIR(edges=edges)
        eg = EventGraph.build(cir)
        assert "OrderPlacedEvent" in eg.event_types
        assert "pkg.NotifService#onOrder" in eg.listeners_of("OrderPlacedEvent")

    def test_publisher_and_listener_same_event(self):
        edges = [
            {"from": "pkg.A#pub", "to": "MyEvent", "type": "publishes_event"},
            {"from": "pkg.B#listen", "to": "MyEvent", "type": "listens_to_event"},
        ]
        cir = _FakeCIR(edges=edges)
        eg = EventGraph.build(cir)
        assert "pkg.A#pub" in eg.publishers_of("MyEvent")
        assert "pkg.B#listen" in eg.listeners_of("MyEvent")
        assert eg.total_edges == 2

    def test_non_event_edges_excluded(self):
        edges = [
            {"from": "pkg.A", "to": "pkg.B", "type": "calls"},
            {"from": "pkg.C", "to": "pkg.D", "type": "annotated_with"},
        ]
        cir = _FakeCIR(edges=edges)
        eg = EventGraph.build(cir)
        assert not eg.has_events()

    def test_missing_from_or_to_skipped(self):
        edges = [
            {"from": "", "to": "SomeEvent", "type": "publishes_event"},
            {"from": "pkg.A", "to": "", "type": "publishes_event"},
        ]
        cir = _FakeCIR(edges=edges)
        eg = EventGraph.build(cir)
        assert not eg.has_events()

    def test_unknown_event_returns_empty_list(self):
        cir = _FakeCIR()
        eg = EventGraph.build(cir)
        assert eg.publishers_of("NonExistent") == []
        assert eg.listeners_of("NonExistent") == []


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
        ep = _FakeEndpoint("pkg.FooController", "FooController.java")
        cir = _FakeCIR(nodes=nodes, edges=edges, dependencies=deps, endpoints=[ep])

        model = SpringSemanticModel.build(cir)

        assert isinstance(model.tx_index, TransactionBoundaryIndex)
        assert isinstance(model.call_adj, CallAdjacency)
        assert isinstance(model.inheritance, InheritanceGraph)
        assert isinstance(model.bean_graph, BeanGraph)
        assert isinstance(model.endpoint_index, EndpointIndex)
        assert isinstance(model.event_graph, EventGraph)
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

    def test_endpoint_index_populated(self):
        ep = _FakeEndpoint("pkg.MyCtrl", "MyCtrl.java")
        cir = _FakeCIR(endpoints=[ep])
        model = SpringSemanticModel.build(cir)
        assert "pkg.MyCtrl" in model.endpoint_index.controller_fqns
        assert model.endpoint_index.source_file("pkg.MyCtrl") == "MyCtrl.java"

    def test_event_graph_populated(self):
        edges = [{"from": "pkg.Pub#send", "to": "MyEvent", "type": "publishes_event"}]
        cir = _FakeCIR(edges=edges)
        model = SpringSemanticModel.build(cir)
        assert model.event_graph.has_events()
        assert "pkg.Pub#send" in model.event_graph.publishers_of("MyEvent")

    def test_never_raises_on_bad_cir(self):
        class _BadCIR:
            _raw_ir = None
            cir_hash = ""
            call_graph = None  # type: ignore[assignment]
            dependencies = None  # type: ignore[assignment]
            endpoints = None  # type: ignore[assignment]
            files = None  # type: ignore[assignment]
            metadata: dict = {}

        model = SpringSemanticModel.build(_BadCIR())  # type: ignore[arg-type]
        assert isinstance(model, SpringSemanticModel)

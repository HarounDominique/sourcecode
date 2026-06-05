"""test_explain.py — Tests for explain.py class architectural summary.

Coverage:
  EXP-01  _resolve_fqn — exact match via cir.symbols
  EXP-02  _resolve_fqn — package-qualified suffix match
  EXP-03  _resolve_fqn — no match returns empty string
  EXP-04  _resolve_fqn — ambiguous: multiple matches, first returned
  EXP-05  _resolve_fqn — method symbols (with #) excluded
  EXP-06  _build_public_methods — public methods extracted from raw_ir nodes
  EXP-07  _build_public_methods — non-public methods excluded
  EXP-08  _build_callers — injection dependents returned as simple names
  EXP-09  _build_callers — reverse graph callers included
  EXP-10  _build_deps — injected dependencies returned as simple names
  EXP-11  _build_events_published — event types where class publishes
  EXP-12  _build_events_consumed — event types where class listens
  EXP-13  _build_transactions — method-level @Transactional entries
  EXP-14  _build_transactions — class-level @Transactional entry
  EXP-15  _build_security — security from cir.security_index
  EXP-16  _build_endpoints — REST endpoints from endpoint_index
  EXP-17  explain_class — not found returns ClassExplanation with warning
  EXP-18  explain_class — full happy path with all sections populated
  EXP-19  ClassExplanation.render_text — produces markdown sections
  EXP-20  ClassExplanation.to_dict — all keys present and serializable
  EXP-21  explain_class — ambiguous class name adds warning
  EXP-22  explain_class — never raises on broken CIR (outer guard)
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from sourcecode.cir_graphs import ImplementationGraph, InjectionGraph
from sourcecode.explain import (
    ClassExplanation,
    _build_callers,
    _build_deps,
    _build_endpoints,
    _build_events_consumed,
    _build_events_published,
    _build_public_methods,
    _build_security,
    _build_transactions,
    _resolve_fqn,
    explain_class,
)
from sourcecode.spring_model import (
    BeanGraph,
    BeanNode,
    CallAdjacency,
    EndpointIndex,
    EventGraph,
    InheritanceGraph,
    SpringSemanticModel,
)
from sourcecode.spring_semantic import TransactionBoundary, TransactionBoundaryIndex


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

class _FakeCIR:
    def __init__(
        self,
        symbols: Optional[list[str]] = None,
        reverse_graph: Optional[dict] = None,
        endpoints: Optional[list] = None,
        call_graph: Optional[list[dict]] = None,
        dependencies: Optional[list[dict]] = None,
        files: Optional[list[str]] = None,
        nodes: Optional[list[dict]] = None,
        security_index: Optional[dict] = None,
    ):
        self.symbols = symbols or []
        self.reverse_graph = reverse_graph or {}
        self.endpoints = endpoints or []
        self.call_graph = call_graph or []
        self.dependencies = dependencies or []
        self.files = files or []
        self.metadata = {}
        self.cir_hash = "deadbeef"
        self.security_index = security_index or {}
        _nodes = nodes or []
        self._raw_ir = {"graph": {"nodes": _nodes, "edges": self.call_graph}}
        self.implementation_graph = ImplementationGraph.build(
            self.dependencies, set(self.symbols)
        )
        self.injection_graph = InjectionGraph.build(self.dependencies)


def _make_model(
    tx_index: Optional[TransactionBoundaryIndex] = None,
    event_graph: Optional[EventGraph] = None,
    endpoint_index: Optional[EndpointIndex] = None,
    bean_graph: Optional[BeanGraph] = None,
    call_adj: Optional[CallAdjacency] = None,
    inheritance: Optional[InheritanceGraph] = None,
) -> SpringSemanticModel:
    return SpringSemanticModel(
        tx_index=tx_index or TransactionBoundaryIndex(),
        call_adj=call_adj or CallAdjacency(),
        inheritance=inheritance or InheritanceGraph(),
        bean_graph=bean_graph or BeanGraph(),
        endpoint_index=endpoint_index or EndpointIndex(),
        event_graph=event_graph or EventGraph(),
    )


@dataclass
class _FakeEndpoint:
    method: str = "GET"
    path: str = "/api/foo"
    controller_class: str = "com.example.FooController"
    handler_symbol: str = "com.example.FooController#foo"
    security: None = None
    source_file: str = ""
    id: str = ""


@dataclass
class _FakeSecurity:
    policy: str = "roles_allowed"
    roles: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# EXP-01  _resolve_fqn — exact match
# ---------------------------------------------------------------------------

def test_exp01_resolve_fqn_exact():
    cir = _FakeCIR(symbols=["UserService"])
    fqn, matches = _resolve_fqn("UserService", cir)
    assert fqn == "UserService"
    assert "UserService" in matches


# ---------------------------------------------------------------------------
# EXP-02  _resolve_fqn — package suffix match
# ---------------------------------------------------------------------------

def test_exp02_resolve_fqn_suffix():
    cir = _FakeCIR(symbols=["com.example.service.UserService"])
    fqn, matches = _resolve_fqn("UserService", cir)
    assert fqn == "com.example.service.UserService"
    assert len(matches) == 1


# ---------------------------------------------------------------------------
# EXP-03  _resolve_fqn — no match
# ---------------------------------------------------------------------------

def test_exp03_resolve_fqn_not_found():
    cir = _FakeCIR(symbols=["com.example.OrderService"])
    fqn, matches = _resolve_fqn("UserService", cir)
    assert fqn == ""
    assert matches == []


# ---------------------------------------------------------------------------
# EXP-04  _resolve_fqn — ambiguous: multiple packages
# ---------------------------------------------------------------------------

def test_exp04_resolve_fqn_ambiguous():
    cir = _FakeCIR(symbols=[
        "com.example.a.UserService",
        "com.example.b.UserService",
    ])
    fqn, matches = _resolve_fqn("UserService", cir)
    assert len(matches) == 2
    assert fqn in ("com.example.a.UserService", "com.example.b.UserService")


# ---------------------------------------------------------------------------
# EXP-05  _resolve_fqn — method symbols excluded
# ---------------------------------------------------------------------------

def test_exp05_resolve_fqn_excludes_methods():
    cir = _FakeCIR(symbols=[
        "com.example.UserService",
        "com.example.UserService#save",
        "com.example.UserService#delete",
    ])
    fqn, matches = _resolve_fqn("UserService", cir)
    assert fqn == "com.example.UserService"
    assert all("#" not in m for m in matches)


# ---------------------------------------------------------------------------
# EXP-06  _build_public_methods — public methods extracted
# ---------------------------------------------------------------------------

def test_exp06_public_methods_extracted():
    nodes = [
        {"fqn": "com.example.UserService#save", "symbol_kind": "method", "modifiers": ["public"]},
        {"fqn": "com.example.UserService#delete", "symbol_kind": "method", "modifiers": ["public"]},
    ]
    methods = _build_public_methods("com.example.UserService", nodes)
    assert "save" in methods
    assert "delete" in methods


# ---------------------------------------------------------------------------
# EXP-07  _build_public_methods — non-public excluded
# ---------------------------------------------------------------------------

def test_exp07_private_methods_excluded():
    nodes = [
        {"fqn": "com.example.UserService#save", "symbol_kind": "method", "modifiers": ["public"]},
        {"fqn": "com.example.UserService#_internal", "symbol_kind": "method", "modifiers": ["private"]},
    ]
    methods = _build_public_methods("com.example.UserService", nodes)
    assert "save" in methods
    assert "_internal" not in methods


# ---------------------------------------------------------------------------
# EXP-08  _build_callers — injection dependents
# ---------------------------------------------------------------------------

def test_exp08_callers_from_injection():
    deps = [{"from": "com.example.UserController", "to": "com.example.UserService", "type": "injects"}]
    cir = _FakeCIR(dependencies=deps)
    callers = _build_callers("com.example.UserService", cir)
    assert "UserController" in callers


# ---------------------------------------------------------------------------
# EXP-09  _build_callers — reverse graph
# ---------------------------------------------------------------------------

def test_exp09_callers_from_reverse_graph():
    rev = {"com.example.UserService": {"calls": ["com.example.UserBatchJob#run"]}}
    cir = _FakeCIR(reverse_graph=rev)
    callers = _build_callers("com.example.UserService", cir)
    assert "UserBatchJob" in callers


# ---------------------------------------------------------------------------
# EXP-10  _build_deps — injected dependencies
# ---------------------------------------------------------------------------

def test_exp10_outgoing_deps():
    deps = [
        {"from": "com.example.UserService", "to": "com.example.UserRepository", "type": "injects"},
        {"from": "com.example.UserService", "to": "com.example.NotificationService", "type": "injects"},
    ]
    cir = _FakeCIR(dependencies=deps)
    result = _build_deps("com.example.UserService", cir)
    assert "UserRepository" in result
    assert "NotificationService" in result


# ---------------------------------------------------------------------------
# EXP-11  _build_events_published
# ---------------------------------------------------------------------------

def test_exp11_events_published():
    eg = EventGraph(
        publishers={"com.example.UserCreatedEvent": ["com.example.UserService#createUser"]},
        listeners={},
        event_types=frozenset({"com.example.UserCreatedEvent"}),
    )
    model = _make_model(event_graph=eg)
    result = _build_events_published("com.example.UserService", model)
    assert "UserCreatedEvent" in result


# ---------------------------------------------------------------------------
# EXP-12  _build_events_consumed
# ---------------------------------------------------------------------------

def test_exp12_events_consumed():
    eg = EventGraph(
        publishers={},
        listeners={"com.example.OrderCreatedEvent": ["com.example.NotificationService#onOrderCreated"]},
        event_types=frozenset({"com.example.OrderCreatedEvent"}),
    )
    model = _make_model(event_graph=eg)
    result = _build_events_consumed("com.example.NotificationService", model)
    assert "OrderCreatedEvent" in result


# ---------------------------------------------------------------------------
# EXP-13  _build_transactions — method-level
# ---------------------------------------------------------------------------

def test_exp13_transactions_method_level():
    tx = TransactionBoundaryIndex(
        by_class={"com.example.UserService": [
            TransactionBoundary(
                symbol="com.example.UserService#save",
                scope="method",
                propagation="REQUIRED",
                read_only=False,
            ),
        ]},
        by_symbol={},
        class_level={},
    )
    model = _make_model(tx_index=tx)
    result = _build_transactions("com.example.UserService", model)
    assert any("save()" in r for r in result)


# ---------------------------------------------------------------------------
# EXP-14  _build_transactions — class-level
# ---------------------------------------------------------------------------

def test_exp14_transactions_class_level():
    tx = TransactionBoundaryIndex(
        class_level={"com.example.UserService": TransactionBoundary(
            symbol="com.example.UserService",
            scope="class",
            propagation="REQUIRED",
            read_only=True,
        )},
        by_symbol={},
        by_class={},
    )
    model = _make_model(tx_index=tx)
    result = _build_transactions("com.example.UserService", model)
    assert any("class-level" in r and "readOnly" in r for r in result)


# ---------------------------------------------------------------------------
# EXP-15  _build_security — from security_index
# ---------------------------------------------------------------------------

def test_exp15_security_from_index():
    sec = _FakeSecurity(policy="roles_allowed", roles=["ADMIN"])
    cir = _FakeCIR(security_index={"com.example.UserController#deleteUser": sec})
    nodes: list[dict] = []
    result = _build_security("com.example.UserController", nodes, cir)
    assert any("deleteUser" in r and "ADMIN" in r for r in result)


# ---------------------------------------------------------------------------
# EXP-16  _build_endpoints — from endpoint_index
# ---------------------------------------------------------------------------

def test_exp16_endpoints_from_index():
    ep = _FakeEndpoint(method="POST", path="/api/users", controller_class="com.example.UserController")
    ei = EndpointIndex(
        by_controller={"com.example.UserController": [ep]},
        source_by_controller={},
        controller_fqns=frozenset({"com.example.UserController"}),
    )
    model = _make_model(endpoint_index=ei)
    result = _build_endpoints("com.example.UserController", model)
    assert "POST /api/users" in result


# ---------------------------------------------------------------------------
# EXP-17  explain_class — class not found
# ---------------------------------------------------------------------------

def test_exp17_not_found():
    cir = _FakeCIR(symbols=["com.example.OrderService"])
    model = _make_model()
    result = explain_class("UserService", cir, model)
    assert result.class_fqn == "UserService"
    assert result.warnings
    assert "not found" in result.warnings[0].lower()


# ---------------------------------------------------------------------------
# EXP-18  explain_class — full happy path
# ---------------------------------------------------------------------------

def test_exp18_full_happy_path():
    deps = [
        {"from": "com.example.UserService", "to": "com.example.UserRepository", "type": "injects"},
        {"from": "com.example.UserController", "to": "com.example.UserService", "type": "injects"},
    ]
    nodes = [
        {"fqn": "com.example.UserService", "symbol_kind": "class", "annotations": ["@Service"], "modifiers": []},
        {"fqn": "com.example.UserService#save", "symbol_kind": "method", "annotations": [], "modifiers": ["public"]},
    ]
    eg = EventGraph(
        publishers={"com.example.UserCreatedEvent": ["com.example.UserService#save"]},
        listeners={},
        event_types=frozenset({"com.example.UserCreatedEvent"}),
    )
    tx = TransactionBoundaryIndex(
        by_class={"com.example.UserService": [
            TransactionBoundary(symbol="com.example.UserService#save", scope="method", propagation="REQUIRED"),
        ]},
        by_symbol={},
        class_level={},
    )
    beans = BeanGraph(
        beans={"com.example.UserService": BeanNode(fqn="com.example.UserService", stereotype="service", source_file="")},
        injections={},
    )
    cir = _FakeCIR(
        symbols=["com.example.UserService", "com.example.UserRepository", "com.example.UserController"],
        dependencies=deps,
        nodes=nodes,
    )
    model = _make_model(event_graph=eg, tx_index=tx, bean_graph=beans)
    result = explain_class("UserService", cir, model)
    assert result.class_fqn == "com.example.UserService"
    assert result.stereotype == "service"
    assert "save" in result.public_methods
    assert "UserController" in result.incoming_callers
    assert "UserRepository" in result.outgoing_deps
    assert "UserCreatedEvent" in result.events_published
    assert any("save()" in t for t in result.transactions)


# ---------------------------------------------------------------------------
# EXP-19  ClassExplanation.render_text — markdown sections
# ---------------------------------------------------------------------------

def test_exp19_render_text_sections():
    exp = ClassExplanation(
        class_name="UserService",
        class_fqn="com.example.UserService",
        stereotype="service",
        purpose="Spring @Service — business logic layer",
        public_methods=["save", "delete"],
        incoming_callers=["UserController"],
        outgoing_deps=["UserRepository"],
        events_published=["UserCreatedEvent"],
        transactions=["save() [readOnly]"],
        security_constraints=["deleteUser(): roles_allowed (ADMIN)"],
        rest_endpoints=[],
    )
    text = exp.render_text()
    assert "## UserService" in text
    assert "**Purpose:**" in text
    assert "**Public Methods:**" in text
    assert "* save" in text
    assert "**Used By:**" in text
    assert "* UserController" in text
    assert "**Calls:**" in text
    assert "**Publishes:**" in text
    assert "**Transactions:**" in text
    assert "**Security:**" in text
    # No REST endpoints section when list is empty
    assert "**REST Endpoints:**" not in text


# ---------------------------------------------------------------------------
# EXP-20  ClassExplanation.to_dict — JSON-serializable
# ---------------------------------------------------------------------------

def test_exp20_to_dict_json_serializable():
    exp = ClassExplanation(
        class_name="UserService",
        class_fqn="com.example.UserService",
        stereotype="service",
        purpose="Spring @Service",
        public_methods=["save"],
        incoming_callers=["UserController"],
    )
    d = exp.to_dict()
    # Must be JSON-serializable
    raw = json.dumps(d)
    parsed = json.loads(raw)
    assert parsed["class_name"] == "UserService"
    assert parsed["class_fqn"] == "com.example.UserService"
    assert "public_methods" in parsed
    assert "incoming_callers" in parsed
    assert "events_published" in parsed


# ---------------------------------------------------------------------------
# EXP-21  explain_class — ambiguous name adds warning
# ---------------------------------------------------------------------------

def test_exp21_ambiguous_adds_warning():
    cir = _FakeCIR(symbols=[
        "com.example.a.UserService",
        "com.example.b.UserService",
    ])
    model = _make_model()
    result = explain_class("UserService", cir, model)
    assert result.warnings
    assert any("mbiguous" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# EXP-22  explain_class — never raises on broken CIR
# ---------------------------------------------------------------------------

def test_exp22_never_raises_on_broken_cir():
    class _BrokenCIR:
        symbols = ["com.example.UserService"]
        reverse_graph: dict = {}
        dependencies: list = []
        files: list = []
        metadata: dict = {}
        cir_hash = ""
        security_index: dict = {}
        _raw_ir: dict = {}  # missing graph key

        @property
        def injection_graph(self):
            raise RuntimeError("broken")

    model = _make_model()
    result = explain_class("UserService", _BrokenCIR(), model)  # type: ignore[arg-type]
    # Must not raise; class found from symbols list
    assert result.class_name == "UserService"

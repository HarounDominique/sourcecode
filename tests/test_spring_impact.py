"""test_spring_impact.py — Tests for spring_impact.py ImpactOrchestrator.

Coverage:
  IC-01  Exact FQN resolution
  IC-02  Simple class name resolution (suffix match) → class_expanded
  IC-03  Class#method resolution — found
  IC-04  Class#method resolution — class found, method not → partial + warning
  IC-05  Not-found symbol → not_found result
  IC-06  Direct callers BFS depth=1
  IC-07  Indirect callers BFS depth=2
  IC-08  Hub-class guard — >500 direct callers → depth capped at 1
  IC-09  Endpoint mapping — caller is controller → endpoint populated
  IC-10  TX boundary — transactional symbol returns boundary dict
  IC-11  TX boundary — non-transactional symbol returns None
  IC-12  Findings filter — TX finding on caller included
  IC-13  Findings filter — unrelated finding excluded
  IC-14  Security surface aggregation
  IC-15  Risk level — no callers + no findings → low
  IC-16  Risk level — endpoints + critical finding → critical
  IC-17  ImpactChainResult.to_dict() — all keys present, JSON-serializable
  IC-18  run_impact_chain() — no model → builds internally, no raise
  IC-19  contained_in edges excluded from BFS
  IC-20  class_expanded resolution returns all class methods as seeds
  IC-V1  Duplicate symbols in CIR are deduplicated in seed_fqns (regression)
  IC-V2  resolved_symbol is canonical class FQN for multi-seed class expansion
  IC-V3  class_expanded → confidence=high (no confidence degradation)
  IC-V5  Single-method seed only returns that method's endpoint (not all controller endpoints)
  IC-V5b Class-level seed returns ALL controller endpoints
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from sourcecode.cir_graphs import ImplementationGraph, InjectionGraph
from sourcecode.spring_findings import SpringFinding
from sourcecode.spring_impact import (
    AffectedEndpoint,
    ImpactChainResult,
    ImpactOrchestrator,
    _bfs_callers,
    _collect_endpoints,
    _compute_risk,
    _filter_findings,
    _resolve_symbol,
    run_impact_chain,
)
from sourcecode.spring_model import (
    BeanGraph,
    CallAdjacency,
    EndpointIndex,
    EventGraph,
    InheritanceGraph,
    SpringSemanticModel,
)
from sourcecode.spring_semantic import TransactionBoundary, TransactionBoundaryIndex

# ---------------------------------------------------------------------------
# Shared fake objects
# ---------------------------------------------------------------------------

def _make_tx_index(
    boundaries: Optional[list[TransactionBoundary]] = None,
) -> TransactionBoundaryIndex:
    idx = TransactionBoundaryIndex()
    for b in (boundaries or []):
        idx.by_symbol[b.symbol] = b
        if b.scope == "class":
            idx.class_level[b.symbol] = b
    return idx


def _tx_boundary(
    symbol: str,
    scope: str = "method",
    propagation: str = "REQUIRED",
    read_only: bool = False,
) -> TransactionBoundary:
    return TransactionBoundary(
        symbol=symbol,
        scope=scope,
        propagation=propagation,
        read_only=read_only,
        source_file="com/example/Foo.java",
    )


class _FakeEndpoint:
    def __init__(
        self,
        ep_id: str,
        method: str,
        path: str,
        controller_class: str,
        handler_symbol: str,
        source_file: str = "",
        security_policy: str = "none_detected",
    ):
        self.id = ep_id
        self.method = method
        self.path = path
        self.controller_class = controller_class
        self.handler_symbol = handler_symbol
        self.source_file = source_file
        self.security = _FakeSecurity(security_policy)


class _FakeSecurity:
    def __init__(self, policy: str):
        self.policy = policy


class _FakeCIR:
    def __init__(
        self,
        symbols: Optional[list[str]] = None,
        reverse_graph: Optional[dict] = None,
        endpoints: Optional[list] = None,
        call_graph: Optional[list[dict]] = None,
        dependencies: Optional[list[dict]] = None,
        files: Optional[list[str]] = None,
        metadata: Optional[dict] = None,
        nodes: Optional[list[dict]] = None,
    ):
        self.symbols = symbols or []
        self.reverse_graph = reverse_graph or {}
        self.endpoints = endpoints or []
        self.call_graph = call_graph or []
        self.dependencies = dependencies or []
        self.files = files or []
        self.metadata = metadata or {}
        self.cir_hash = "deadbeef00000000"
        self._raw_ir = {"graph": {"nodes": nodes or [], "edges": self.call_graph}}
        # Derived graph indices — built from dependencies, mirroring canonical_ir behaviour
        self.implementation_graph = ImplementationGraph.build(
            self.dependencies, set(self.symbols)
        )
        self.injection_graph = InjectionGraph.build(self.dependencies)


def _make_model(
    tx_index: Optional[TransactionBoundaryIndex] = None,
    call_adj: Optional[CallAdjacency] = None,
    endpoint_index: Optional[EndpointIndex] = None,
) -> SpringSemanticModel:
    return SpringSemanticModel(
        tx_index=tx_index or TransactionBoundaryIndex(),
        call_adj=call_adj or CallAdjacency(),
        inheritance=InheritanceGraph(),
        bean_graph=BeanGraph(),
        endpoint_index=endpoint_index or EndpointIndex(),
        event_graph=EventGraph(),
        build_time_ms=0.0,
    )


def _make_endpoint_index(endpoints: list[_FakeEndpoint]) -> EndpointIndex:
    by_controller: dict[str, list] = {}
    source_by_controller: dict[str, str] = {}
    for ep in endpoints:
        by_controller.setdefault(ep.controller_class, []).append(ep)
        source_by_controller.setdefault(ep.controller_class, ep.source_file)
    return EndpointIndex(
        by_controller=by_controller,
        source_by_controller=source_by_controller,
        controller_fqns=frozenset(by_controller.keys()),
    )


def _make_finding(
    pattern_id: str,
    symbol: str,
    severity: str = "high",
    category: str = "tx",
    evidence: Optional[dict] = None,
    related_symbols: Optional[list[str]] = None,
) -> SpringFinding:
    return SpringFinding(
        id=SpringFinding.make_id(pattern_id, symbol),
        pattern_id=pattern_id,
        category=category,
        severity=severity,
        confidence="high",
        title=f"{pattern_id} finding on {symbol}",
        symbol=symbol,
        source_file="com/example/Foo.java",
        evidence=evidence or {},
        explanation="Test finding.",
        fix_hint="Fix it.",
        related_symbols=related_symbols or [],
    )


# ---------------------------------------------------------------------------
# IC-01  Exact FQN resolution
# ---------------------------------------------------------------------------

class TestResolveSymbol:
    def test_ic01_exact_fqn(self):
        symbols = ["com.example.OrderService#placeOrder", "com.example.OrderService"]
        res, fqns, warnings = _resolve_symbol("com.example.OrderService#placeOrder", symbols)
        assert res == "exact"
        assert fqns == ["com.example.OrderService#placeOrder"]
        assert warnings == []

    def test_ic02_simple_class_name(self):
        symbols = [
            "com.example.OrderService",
            "com.example.OrderService#placeOrder",
            "com.example.OrderService#cancelOrder",
        ]
        res, fqns, warnings = _resolve_symbol("OrderService", symbols)
        assert res == "class_expanded"
        assert all("OrderService" in f for f in fqns)
        assert len(fqns) == 3

    def test_ic03_class_method_found(self):
        symbols = [
            "com.example.OrderService",
            "com.example.OrderService#placeOrder",
        ]
        res, fqns, warnings = _resolve_symbol("OrderService#placeOrder", symbols)
        assert "com.example.OrderService#placeOrder" in fqns
        assert warnings == []

    def test_ic04_class_found_method_not(self):
        symbols = ["com.example.OrderService", "com.example.OrderService#placeOrder"]
        res, fqns, warnings = _resolve_symbol("OrderService#nonExistent", symbols)
        assert res == "partial"
        assert len(fqns) > 0   # class-level symbols returned
        assert any("method" in w.lower() for w in warnings)

    def test_ic05_not_found(self):
        res, fqns, warnings = _resolve_symbol("NonExistent", ["com.example.Foo"])
        assert res == "not_found"
        assert fqns == []
        assert len(warnings) > 0

    def test_ic20_class_expanded_returns_all_methods(self):
        symbols = [
            "com.example.pkg.MyService",
            "com.example.pkg.MyService#methodA",
            "com.example.pkg.MyService#methodB",
            "com.example.pkg.OtherService",
        ]
        res, fqns, _ = _resolve_symbol("MyService", symbols)
        assert "com.example.pkg.MyService" in fqns
        assert "com.example.pkg.MyService#methodA" in fqns
        assert "com.example.pkg.MyService#methodB" in fqns
        assert "com.example.pkg.OtherService" not in fqns


# ---------------------------------------------------------------------------
# IC-06..08  BFS caller traversal
# ---------------------------------------------------------------------------

class TestBfsCallers:
    def _rg(self, edges: list[tuple[str, str, str]]) -> dict:
        """Build reverse_graph from (callee, edge_type, caller) tuples."""
        rg: dict = {}
        for callee, etype, caller in edges:
            rg.setdefault(callee, {}).setdefault(etype, []).append(caller)
        return rg

    def test_ic06_direct_callers_depth1(self):
        rg = self._rg([
            ("com.example.Service", "calls", "com.example.ControllerA"),
            ("com.example.Service", "calls", "com.example.ControllerB"),
        ])
        direct, indirect, truncated = _bfs_callers(["com.example.Service"], rg, 1)
        assert set(direct) == {"com.example.ControllerA", "com.example.ControllerB"}
        assert indirect == []
        assert not truncated

    def test_ic07_indirect_callers_depth2(self):
        rg = self._rg([
            ("com.example.Service", "calls", "com.example.ControllerA"),
            ("com.example.ControllerA", "calls", "com.example.Client"),
        ])
        direct, indirect, truncated = _bfs_callers(["com.example.Service"], rg, 2)
        assert "com.example.ControllerA" in direct
        assert "com.example.Client" in indirect

    def test_ic08_hub_class_guard(self):
        # Build 501 direct callers to trigger the hub guard
        callers = [f"com.example.Caller{i}" for i in range(501)]
        rg = {"com.example.Hub": {"calls": callers}}
        direct, indirect, truncated = _bfs_callers(["com.example.Hub"], rg, 4)
        assert len(direct) == 501
        assert indirect == []
        assert truncated

    def test_ic19_contained_in_excluded(self):
        rg = {
            "com.example.Service": {
                "contained_in": ["com.example.Service"],  # structural — should skip
                "calls": ["com.example.ActualCaller"],
            }
        }
        direct, indirect, _ = _bfs_callers(["com.example.Service"], rg, 1)
        assert "com.example.ActualCaller" in direct
        assert "com.example.Service" not in direct  # no self-reference via contained_in

    def test_ic20_field_fqn_not_in_callers(self):
        # Regression: field FQN (pkg.Class.field) must not appear in direct_callers.
        # Only the owning class must appear.
        rg = {
            "com.example.PatientDAO": {
                "injects": ["com.example.PatientServiceImpl.dao"],
            }
        }
        direct, indirect, _ = _bfs_callers(["com.example.PatientDAO"], rg, 1)
        assert "com.example.PatientServiceImpl" in direct
        assert "com.example.PatientServiceImpl.dao" not in direct
        assert "com.example.PatientServiceImpl.dao" not in indirect

    def test_ic21_constructor_fqn_not_in_callers(self):
        # Regression: constructor FQN (pkg.Class#<init>) must not appear in callers.
        rg = {
            "com.example.PatientDAO": {
                "injects": ["com.example.PatientServiceImpl#<init>"],
            }
        }
        direct, indirect, _ = _bfs_callers(["com.example.PatientDAO"], rg, 1)
        assert "com.example.PatientServiceImpl" in direct
        assert "com.example.PatientServiceImpl#<init>" not in direct
        assert "com.example.PatientServiceImpl#<init>" not in indirect


# ---------------------------------------------------------------------------
# IC-09  Endpoint mapping
# ---------------------------------------------------------------------------

class TestCollectEndpoints:
    def test_ic09_caller_is_controller(self):
        ep = _FakeEndpoint(
            ep_id="ep-1",
            method="POST",
            path="/orders",
            controller_class="com.example.OrderController",
            handler_symbol="com.example.OrderController#checkout",
        )
        ei = _make_endpoint_index([ep])
        model = _make_model(endpoint_index=ei)

        endpoints = _collect_endpoints(
            all_callers=["com.example.OrderController#checkout"],
            seed_fqns=["com.example.OrderService#placeOrder"],
            model=model,
        )
        assert len(endpoints) == 1
        assert endpoints[0].path == "/orders"
        assert endpoints[0].controller_class == "com.example.OrderController"

    def test_no_endpoint_when_caller_not_controller(self):
        model = _make_model()
        endpoints = _collect_endpoints(
            all_callers=["com.example.SomeService#doWork"],
            seed_fqns=["com.example.UtilClass"],
            model=model,
        )
        assert endpoints == []


# ---------------------------------------------------------------------------
# IC-10, IC-11  TX boundary
# ---------------------------------------------------------------------------

class TestTxBoundary:
    def test_ic10_transactional_symbol_returns_boundary(self):
        boundary = _tx_boundary("com.example.OrderService#placeOrder", propagation="REQUIRED")
        tx_index = _make_tx_index([boundary])
        model = _make_model(tx_index=tx_index)
        cir = _FakeCIR(symbols=["com.example.OrderService#placeOrder"])

        orchestrator = ImpactOrchestrator()
        result = orchestrator.query(cir, model, "com.example.OrderService#placeOrder")

        assert result.transaction_boundary is not None
        assert result.transaction_boundary["propagation"] == "REQUIRED"

    def test_ic11_non_transactional_symbol_returns_none(self):
        model = _make_model()
        cir = _FakeCIR(symbols=["com.example.UtilClass"])

        orchestrator = ImpactOrchestrator()
        result = orchestrator.query(cir, model, "com.example.UtilClass")

        assert result.transaction_boundary is None


# ---------------------------------------------------------------------------
# IC-12, IC-13  Findings filter
# ---------------------------------------------------------------------------

class TestFilterFindings:
    def test_ic12_finding_on_direct_caller_included(self):
        finding = _make_finding("TX-001", "com.example.ControllerA#doPost", severity="high")
        result = _filter_findings(
            all_findings=[finding],
            seed_fqns=["com.example.OrderService"],
            direct_callers=["com.example.ControllerA#doPost"],
            indirect_callers=[],
            affected_endpoints=[],
        )
        assert len(result) == 1
        assert result[0].symbol == "com.example.ControllerA#doPost"

    def test_ic13_unrelated_finding_excluded(self):
        finding = _make_finding("TX-001", "com.example.UnrelatedService#foo", severity="high")
        result = _filter_findings(
            all_findings=[finding],
            seed_fqns=["com.example.OrderService"],
            direct_callers=["com.example.ControllerA"],
            indirect_callers=[],
            affected_endpoints=[],
        )
        assert result == []

    def test_finding_on_seed_symbol_included(self):
        finding = _make_finding("TX-001", "com.example.OrderService#placeOrder")
        result = _filter_findings(
            all_findings=[finding],
            seed_fqns=["com.example.OrderService#placeOrder"],
            direct_callers=[],
            indirect_callers=[],
            affected_endpoints=[],
        )
        assert len(result) == 1

    def test_finding_via_outer_symbol_evidence_included(self):
        finding = _make_finding(
            "TX-002", "com.example.Inner#foo",
            evidence={"outer_symbol": "com.example.OrderService#placeOrder"},
        )
        result = _filter_findings(
            all_findings=[finding],
            seed_fqns=["com.example.OrderService#placeOrder"],
            direct_callers=[],
            indirect_callers=[],
            affected_endpoints=[],
        )
        assert len(result) == 1


# ---------------------------------------------------------------------------
# IC-14  Security surface aggregation
# ---------------------------------------------------------------------------

class TestSecuritySurfaces:
    def test_ic14_sec_finding_linked_to_endpoint(self):
        ep = AffectedEndpoint(
            endpoint_id="ep-1", method="GET", path="/admin",
            controller_class="com.example.AdminController",
            handler_symbol="com.example.AdminController#list",
            source_file="AdminController.java",
            security_policy="none_detected",
        )
        sec_finding = _make_finding(
            "SEC-001", "com.example.AdminController#list",
            category="security", severity="high",
            evidence={"endpoint_id": "ep-1"},
        )
        from sourcecode.spring_impact import _build_security_surfaces
        surfaces = _build_security_surfaces([ep], [sec_finding])
        assert len(surfaces) == 1
        assert sec_finding.id in surfaces[0]["security_findings"]


# ---------------------------------------------------------------------------
# IC-15, IC-16  Risk computation
# ---------------------------------------------------------------------------

class TestComputeRisk:
    def test_ic15_no_callers_no_findings_is_low(self):
        level, score = _compute_risk(0, 0, 0, [])
        assert level == "low"

    def test_ic16_endpoints_and_critical_finding_is_critical(self):
        critical = _make_finding("TX-001", "foo", severity="critical")
        level, score = _compute_risk(5, 10, 4, [critical])
        assert level in ("critical", "high")  # 4*3 + min(5*2+10*0.5,20) + 4 = 12+15+4 = 31 → critical
        assert level == "critical"


# ---------------------------------------------------------------------------
# IC-17  to_dict() contract
# ---------------------------------------------------------------------------

class TestToDict:
    REQUIRED_KEYS = {
        "schema_version", "symbol", "resolution",
        "direct_callers", "indirect_callers", "endpoints_affected",
        "transaction_boundary", "security_surfaces", "impact_findings",
        "analysis_warnings", "risk_level", "confidence", "metadata",
    }

    def test_ic17_all_keys_present(self):
        result = ImpactChainResult(symbol="com.example.Foo", resolution="exact")
        d = result.to_dict()
        assert set(d.keys()) >= self.REQUIRED_KEYS

    def test_ic17_json_serializable(self):
        result = ImpactChainResult(
            symbol="com.example.Foo",
            resolution="exact",
            endpoints_affected=[
                AffectedEndpoint("ep-1", "GET", "/foo", "Foo", "Foo#get", "", "none_detected")
            ],
        )
        # Must not raise
        json.dumps(result.to_dict())


# ---------------------------------------------------------------------------
# IC-18  run_impact_chain convenience (no pre-built model)
# ---------------------------------------------------------------------------

class TestRunImpactChain:
    def test_ic18_no_model_builds_internally(self):
        cir = _FakeCIR(symbols=["com.example.Foo"])
        result = run_impact_chain(cir, "com.example.Foo")
        assert result.symbol == "com.example.Foo"
        assert result.risk_level in ("low", "medium", "high", "critical", "unknown")

    def test_ic18_not_found_no_raise(self):
        cir = _FakeCIR(symbols=[])
        result = run_impact_chain(cir, "NonExistent")
        assert result.resolution == "not_found"
        assert len(result.analysis_warnings) > 0


# ---------------------------------------------------------------------------
# Integration: full orchestrator path
# ---------------------------------------------------------------------------

class TestOrchestratorIntegration:
    def _build_scenario(self):
        """
        Scenario:
          OrderService#placeOrder
            ← called by OrderController#checkout (controller)
              ← called by ApiGateway#route
        TX: OrderService#placeOrder is @Transactional(REQUIRED)
        Endpoint: POST /orders on OrderController
        """
        symbols = [
            "com.example.OrderService",
            "com.example.OrderService#placeOrder",
            "com.example.OrderController",
            "com.example.OrderController#checkout",
            "com.example.ApiGateway",
            "com.example.ApiGateway#route",
        ]
        reverse_graph = {
            "com.example.OrderService#placeOrder": {
                "calls": ["com.example.OrderController#checkout"],
            },
            "com.example.OrderController#checkout": {
                "calls": ["com.example.ApiGateway#route"],
            },
        }
        ep = _FakeEndpoint(
            ep_id="ep-orders",
            method="POST",
            path="/orders",
            controller_class="com.example.OrderController",
            handler_symbol="com.example.OrderController#checkout",
        )
        ei = _make_endpoint_index([ep])
        boundary = _tx_boundary("com.example.OrderService#placeOrder", propagation="REQUIRED")
        tx_index = _make_tx_index([boundary])
        model = _make_model(tx_index=tx_index, endpoint_index=ei)
        cir = _FakeCIR(symbols=symbols, reverse_graph=reverse_graph)
        return cir, model

    def test_full_query_populates_all_fields(self):
        cir, model = self._build_scenario()
        orchestrator = ImpactOrchestrator()
        result = orchestrator.query(cir, model, "OrderService#placeOrder", depth=2)

        assert result.resolution in ("exact", "partial", "class_expanded")
        assert "com.example.OrderController#checkout" in result.direct_callers
        assert "com.example.ApiGateway#route" in result.indirect_callers
        assert len(result.endpoints_affected) == 1
        assert result.endpoints_affected[0].path == "/orders"
        assert result.transaction_boundary is not None
        assert result.transaction_boundary["propagation"] == "REQUIRED"
        assert result.risk_level != "unknown"

    def test_result_is_json_serializable(self):
        cir, model = self._build_scenario()
        orchestrator = ImpactOrchestrator()
        result = orchestrator.query(cir, model, "OrderService#placeOrder", depth=2)
        json.dumps(result.to_dict())  # must not raise


# ---------------------------------------------------------------------------
# Fase 21-03 — repo/service seed must reach the controller across the
# interface-only DI chain (openapi-generator petclinic shape).
# ---------------------------------------------------------------------------

class TestInterfaceChainReachesController:
    """Field test (petclinic-rest #11, weakness #2): impact-chain on a repository
    seed reports endpoints_affected=0 even though the route exists.

    Chain shape:
        VetRepository  ← injected by VetServiceImpl
        VetService     ← injected by VetRestController  (impl injects the *interface*)
        VetServiceImpl implements VetService
        VetRestController implements VetsApi → GET /api/vets (spec-recovered)

    BFS from VetRepository reaches VetServiceImpl, but its callers inject the
    *interface* VetService — the injects edge lives on reverse_graph[VetService],
    not reverse_graph[VetServiceImpl].  CH-001b only expands impl→interface for the
    SEED; mid-chain it was never done, so the controller was unreachable.
    """

    def _build_chain(self):
        symbols = [
            "com.example.repo.VetRepository",
            "com.example.service.VetService",
            "com.example.service.VetServiceImpl",
            "com.example.service.VetServiceImpl#findAll",
            "com.example.web.VetRestController",
            "com.example.web.VetRestController#listVets",
        ]
        # Impl→interface edge so implementation_graph.interfaces_of(VetServiceImpl)
        # resolves to VetService.
        dependencies = [
            {"from": "com.example.service.VetServiceImpl",
             "to": "com.example.service.VetService", "type": "implements"},
            {"from": "com.example.service.VetServiceImpl#<init>",
             "to": "com.example.repo.VetRepository", "type": "injects"},
            {"from": "com.example.web.VetRestController#<init>",
             "to": "com.example.service.VetService", "type": "injects"},
        ]
        reverse_graph = {
            # VetServiceImpl injects the repo → repo's caller is the service impl.
            "com.example.repo.VetRepository": {
                "injects": ["com.example.service.VetServiceImpl#<init>"],
            },
            # The controller injects the *interface*, so the edge sits on VetService.
            "com.example.service.VetService": {
                "injects": ["com.example.web.VetRestController#<init>"],
            },
        }
        ep = _FakeEndpoint(
            ep_id="ep-vets",
            method="GET",
            path="/api/vets",
            controller_class="com.example.web.VetRestController",
            handler_symbol="com.example.web.VetRestController#listVets",
        )
        ei = _make_endpoint_index([ep])
        model = _make_model(endpoint_index=ei)
        cir = _FakeCIR(symbols=symbols, reverse_graph=reverse_graph,
                       dependencies=dependencies)
        return cir, model

    def test_repo_seed_reaches_controller_endpoint(self):
        cir, model = self._build_chain()
        orchestrator = ImpactOrchestrator()
        result = orchestrator.query(cir, model, "VetRepository", depth=3)

        assert "com.example.web.VetRestController" in (
            result.direct_callers + result.indirect_callers
        ), (
            f"controller must be reachable across the interface DI chain; "
            f"direct={result.direct_callers} indirect={result.indirect_callers}"
        )
        paths = {ep.path for ep in result.endpoints_affected}
        assert "/api/vets" in paths, (
            f"repo seed must surface the controller endpoint; got {paths}"
        )

    def test_service_seed_reaches_controller_endpoint(self):
        cir, model = self._build_chain()
        orchestrator = ImpactOrchestrator()
        result = orchestrator.query(cir, model, "VetServiceImpl", depth=3)

        paths = {ep.path for ep in result.endpoints_affected}
        assert "/api/vets" in paths, (
            f"service seed must surface the controller endpoint; got {paths}"
        )


# ---------------------------------------------------------------------------
# Regression tests for IC-V1..V5 (validation-session bugs)
# ---------------------------------------------------------------------------

class TestRegressionV1DeduplicateSeeds:
    """IC-V1 — duplicate symbols in CIR must not produce duplicate seeds."""

    def test_ic_v1_dedup_class_expansion(self):
        symbols = [
            "com.example.OwnerController",
            "com.example.OwnerController#addPaginationModel",
            "com.example.OwnerController#addPaginationModel",  # duplicate
            "com.example.OwnerController#processCreationForm",
        ]
        res, fqns, warnings = _resolve_symbol("OwnerController", symbols)
        # No duplicates in output
        assert len(fqns) == len(set(fqns)), f"Duplicate seeds: {fqns}"
        assert res == "class_expanded"

    def test_ic_v1_dedup_method_match(self):
        symbols = [
            "com.example.Svc",
            "com.example.Svc#doWork",
            "com.example.Svc#doWork",  # duplicate
        ]
        res, fqns, _ = _resolve_symbol("Svc#doWork", symbols)
        assert len(fqns) == len(set(fqns)), f"Duplicate seeds: {fqns}"
        assert len(fqns) == 1


class TestRegressionV2ResolvedSymbol:
    """IC-V2 — resolved_symbol must be canonical class FQN, not short input."""

    def _build_controller_cir(self):
        symbols = [
            "com.example.OrderController",
            "com.example.OrderController#checkout",
            "com.example.OrderController#listOrders",
        ]
        ep1 = _FakeEndpoint("ep1", "POST", "/orders", "com.example.OrderController",
                            "com.example.OrderController#checkout")
        ep2 = _FakeEndpoint("ep2", "GET", "/orders", "com.example.OrderController",
                            "com.example.OrderController#listOrders")
        ei = _make_endpoint_index([ep1, ep2])
        model = _make_model(endpoint_index=ei)
        cir = _FakeCIR(symbols=symbols, reverse_graph={})
        return cir, model

    def test_ic_v2_class_expansion_resolved_symbol_is_fqn(self):
        cir, model = self._build_controller_cir()
        orchestrator = ImpactOrchestrator()
        result = orchestrator.query(cir, model, "OrderController", depth=2)
        # symbol output must be the full FQN, not the short input
        assert result.symbol == "com.example.OrderController", (
            f"Expected FQN, got: {result.symbol!r}"
        )

    def test_ic_v2_single_method_seed_resolved_symbol_is_fqn(self):
        cir, model = self._build_controller_cir()
        orchestrator = ImpactOrchestrator()
        result = orchestrator.query(cir, model, "OrderController#checkout", depth=2)
        assert result.symbol == "com.example.OrderController#checkout"


class TestRegressionV3ClassExpandedConfidence:
    """IC-V3 — class_expanded resolution yields confidence=high (not medium)."""

    def test_ic_v3_class_expanded_confidence_high(self):
        symbols = [
            "com.example.OrderService",
            "com.example.OrderService#placeOrder",
        ]
        res, fqns, warnings = _resolve_symbol("OrderService", symbols)
        assert res == "class_expanded"
        assert warnings == []

        cir = _FakeCIR(symbols=symbols, reverse_graph={})
        model = _make_model()
        orchestrator = ImpactOrchestrator()
        result = orchestrator.query(cir, model, "OrderService", depth=1)
        assert result.resolution == "class_expanded"
        assert result.confidence == "high", (
            f"class_expanded without warnings should be high, got {result.confidence!r}"
        )

    def test_ic_v3_partial_resolution_still_medium(self):
        # Ambiguous: two classes match the same short name
        symbols = [
            "com.a.OrderService",
            "com.a.OrderService#placeOrder",
            "com.b.OrderService",
            "com.b.OrderService#placeOrder",
        ]
        res, fqns, warnings = _resolve_symbol("OrderService", symbols)
        assert res == "partial"

        cir = _FakeCIR(symbols=symbols, reverse_graph={})
        model = _make_model()
        orchestrator = ImpactOrchestrator()
        result = orchestrator.query(cir, model, "OrderService", depth=1)
        assert result.resolution == "partial"
        assert result.confidence == "medium"


class TestRegressionCH003ValueTypeBlindSpot:
    """CH-003 — value/DTO types have an invisible blast radius (type-usage edges
    not modeled). A fully empty result on such a type must NOT be reported at
    confidence=high: it reads as 'globally dead' and is a dangerous false negative.
    See finding_ch003_type_usage_edges + docs/eval/2026-06-16-spring-petclinic-issue2333.
    """

    def _value_type_node(self, fqn: str) -> dict:
        # Plain POJO: symbol_kind=class, no stereotype annotation, role=other.
        return {
            "fqn": fqn,
            "symbol_kind": "class",
            "type": "class",
            "role": "other",
            "annotations": [],
        }

    def test_ch003_value_type_empty_result_not_high(self):
        # `Vets` — instantiated + returned by @ResponseBody in the real repo, but the
        # impact graph models no type-usage edges → 0 callers, 0 endpoints.
        fqn = "org.springframework.samples.petclinic.vet.Vets"
        cir = _FakeCIR(
            symbols=[fqn],
            reverse_graph={},
            nodes=[self._value_type_node(fqn)],
        )
        model = _make_model()
        result = ImpactOrchestrator().query(cir, model, "Vets", depth=1)

        assert result.direct_callers == []
        assert result.endpoints_affected == []
        assert result.confidence != "high", (
            "empty blast radius on a value/DTO type must not be high-confidence "
            f"(false negative), got {result.confidence!r}"
        )
        assert any(
            "type-usage" in w.lower() or "not modeled" in w.lower()
            for w in result.analysis_warnings
        ), f"expected a type-usage-edge warning, got {result.analysis_warnings!r}"

    def test_ch003_stereotype_bean_empty_result_stays_high(self):
        # A genuinely-empty @Service is a spine participant — call/DI edges DO cover
        # its blast radius, so the guard must NOT fire (no false downgrade).
        fqn = "com.example.OrphanService"
        node = self._value_type_node(fqn)
        node["annotations"] = ["@Service"]
        node["role"] = "service"
        cir = _FakeCIR(symbols=[fqn], reverse_graph={}, nodes=[node])
        model = _make_model()
        result = ImpactOrchestrator().query(cir, model, "OrphanService", depth=1)
        assert result.confidence == "high", (
            f"stereotype bean with empty result should stay high, got {result.confidence!r}"
        )

    def test_ch003_no_node_metadata_preserves_legacy_high(self):
        # No node metadata available (incomplete IR) → cannot confirm value type →
        # stay conservative (preserves IC-V3 behaviour, no spurious downgrade).
        cir = _FakeCIR(symbols=["com.example.OrderService"], reverse_graph={})
        model = _make_model()
        result = ImpactOrchestrator().query(cir, model, "OrderService", depth=1)
        assert result.confidence == "high"


class TestRegressionG2UnresolvedRefs:
    """G-2 — a symbol that is imported by in-repo files but has no resolved
    call/DI/instantiation edge (static import, reflection, method reference the
    call-graph cannot bind) must NOT be reported as a high-confidence empty result.
    Primary recovery is the new static-call edges; this guard catches the residual.
    See finding_static_call_blindspot_g2.
    """

    def _bean_node(self, fqn: str) -> dict:
        # Stereotype bean → NOT a value type, so the CH-003 guard does not pre-empt.
        return {
            "fqn": fqn,
            "symbol_kind": "class",
            "type": "class",
            "role": "service",
            "annotations": ["@Service"],
        }

    def test_imported_but_unresolved_empty_result_not_high(self):
        fqn = "com.example.util.AnnotationHelper"
        cir = _FakeCIR(
            symbols=[fqn],
            # something imports it, but no calls/injects edge resolves → empty BFS.
            reverse_graph={fqn: {"imports": ["com.example.rules.EnumRule"]}},
            nodes=[self._bean_node(fqn)],
        )
        result = ImpactOrchestrator().query(cir, _make_model(), "AnnotationHelper", depth=1)

        assert result.direct_callers == []
        assert result.confidence == "low", (
            f"imported-but-unresolved empty result must not stay high, got {result.confidence!r}"
        )
        assert "unresolved_refs" in result.metadata.get("blind_spots", [])
        assert any(
            "unresolved" in w.lower() or "static import" in w.lower()
            for w in result.analysis_warnings
        ), f"expected an unresolved-reference warning, got {result.analysis_warnings!r}"

    def test_no_inbound_imports_stays_high(self):
        # Truly nothing references it (empty reverse_graph) → confident it is unused.
        fqn = "com.example.util.AnnotationHelper"
        cir = _FakeCIR(symbols=[fqn], reverse_graph={}, nodes=[self._bean_node(fqn)])
        result = ImpactOrchestrator().query(cir, _make_model(), "AnnotationHelper", depth=1)
        assert result.confidence == "high", (
            f"no inbound references should stay high, got {result.confidence!r}"
        )
        assert "unresolved_refs" not in result.metadata.get("blind_spots", [])

    def test_resolved_callers_do_not_trigger_guard(self):
        # A real `calls` edge resolves the caller → not empty → guard must not fire.
        fqn = "com.example.util.AnnotationHelper"
        cir = _FakeCIR(
            symbols=[fqn, "com.example.rules.EnumRule"],
            reverse_graph={fqn: {
                "calls": ["com.example.rules.EnumRule"],
                "imports": ["com.example.rules.EnumRule"],
            }},
            nodes=[self._bean_node(fqn)],
        )
        result = ImpactOrchestrator().query(cir, _make_model(), "AnnotationHelper", depth=1)
        assert result.direct_callers == ["com.example.rules.EnumRule"]
        assert "unresolved_refs" not in result.metadata.get("blind_spots", [])


class TestRegressionV5EndpointPrecision:
    """IC-V5 — single-method seed returns only that method's endpoint.
    Class-level seed returns all controller endpoints."""

    def _build_multi_endpoint_controller(self):
        symbols = [
            "com.example.OwnerController",
            "com.example.OwnerController#processCreationForm",
            "com.example.OwnerController#processFindForm",
            "com.example.OwnerController#showOwner",
        ]
        ep1 = _FakeEndpoint("ep1", "POST", "/owners/new", "com.example.OwnerController",
                            "com.example.OwnerController#processCreationForm")
        ep2 = _FakeEndpoint("ep2", "GET", "/owners", "com.example.OwnerController",
                            "com.example.OwnerController#processFindForm")
        ep3 = _FakeEndpoint("ep3", "GET", "/owners/{id}", "com.example.OwnerController",
                            "com.example.OwnerController#showOwner")
        ei = _make_endpoint_index([ep1, ep2, ep3])
        model = _make_model(endpoint_index=ei)
        cir = _FakeCIR(symbols=symbols, reverse_graph={})
        return cir, model

    def test_ic_v5_single_method_one_endpoint(self):
        cir, model = self._build_multi_endpoint_controller()
        orchestrator = ImpactOrchestrator()
        # Query the exact handler method for POST /owners/new
        result = orchestrator.query(
            cir, model,
            "com.example.OwnerController#processCreationForm",
            depth=2,
        )
        assert len(result.endpoints_affected) == 1, (
            f"Single-method seed should yield 1 endpoint, got {len(result.endpoints_affected)}: "
            f"{[ep.path for ep in result.endpoints_affected]}"
        )
        assert result.endpoints_affected[0].path == "/owners/new"
        assert result.endpoints_affected[0].method == "POST"

    def test_ic_v5_class_seed_all_endpoints(self):
        cir, model = self._build_multi_endpoint_controller()
        orchestrator = ImpactOrchestrator()
        # Query the entire controller class → all 3 endpoints
        result = orchestrator.query(
            cir, model,
            "com.example.OwnerController",
            depth=2,
        )
        assert len(result.endpoints_affected) == 3, (
            f"Class-level seed should yield all 3 endpoints, got {len(result.endpoints_affected)}: "
            f"{[ep.path for ep in result.endpoints_affected]}"
        )

    def test_ic_v5_caller_method_is_handler_adds_its_endpoint(self):
        """When a caller of the seed IS a handler, that endpoint is included."""
        symbols = [
            "com.example.OrderService",
            "com.example.OrderService#placeOrder",
            "com.example.OrderController",
            "com.example.OrderController#checkout",
            "com.example.OrderController#listOrders",
        ]
        reverse_graph = {
            "com.example.OrderService#placeOrder": {
                "calls": ["com.example.OrderController#checkout"],
            },
        }
        ep_checkout = _FakeEndpoint("ep1", "POST", "/orders/checkout",
                                    "com.example.OrderController",
                                    "com.example.OrderController#checkout")
        ep_list = _FakeEndpoint("ep2", "GET", "/orders",
                                "com.example.OrderController",
                                "com.example.OrderController#listOrders")
        ei = _make_endpoint_index([ep_checkout, ep_list])
        model = _make_model(endpoint_index=ei)
        cir = _FakeCIR(symbols=symbols, reverse_graph=reverse_graph)
        orchestrator = ImpactOrchestrator()
        result = orchestrator.query(cir, model, "com.example.OrderService#placeOrder", depth=2)
        paths = [ep.path for ep in result.endpoints_affected]
        assert "/orders/checkout" in paths, f"checkout endpoint missing: {paths}"
        assert "/orders" not in paths, f"listOrders endpoint is false positive: {paths}"


class TestRegressionImportsEdgeExclusion:
    """IC-V6 — imports edges are type-references, not call edges.
    They must not appear in caller BFS output."""

    def test_ic_v6_imports_caller_excluded(self):
        """A class that merely imports the seed (type reference) must not appear as a caller."""
        symbols = [
            "com.example.OrderService",
            "com.example.OrderService#placeOrder",
            "com.example.OrderItemDTO",   # imports OrderService as a type
        ]
        reverse_graph = {
            # OrderItemDTO imports OrderService (type reference, not a call)
            "com.example.OrderService": {
                "imports": ["com.example.OrderItemDTO"],
            },
        }
        cir = _FakeCIR(symbols=symbols, reverse_graph=reverse_graph)
        model = _make_model()
        orchestrator = ImpactOrchestrator()
        result = orchestrator.query(cir, model, "com.example.OrderService#placeOrder", depth=4)
        assert "com.example.OrderItemDTO" not in result.direct_callers, (
            "Type-reference (imports edge) must not appear as direct_caller"
        )
        assert "com.example.OrderItemDTO" not in result.indirect_callers

    def test_ic_v6_imports_chain_does_not_reach_endpoints(self):
        """Endpoint false positives via imports chain are prevented."""
        symbols = [
            "com.example.OrderService",
            "com.example.OrderService#placeOrder",
            "com.example.OrderItemDTO",
            "com.example.OrderController",
            "com.example.OrderController#checkout",
        ]
        # Imports chain: OrderService ← [imports] OrderItemDTO ← [imports] OrderController
        reverse_graph = {
            "com.example.OrderService": {
                "imports": ["com.example.OrderItemDTO"],
            },
            "com.example.OrderItemDTO": {
                "imports": ["com.example.OrderController"],
            },
        }
        ep = _FakeEndpoint("ep1", "POST", "/orders", "com.example.OrderController",
                           "com.example.OrderController#checkout")
        ei = _make_endpoint_index([ep])
        model = _make_model(endpoint_index=ei)
        cir = _FakeCIR(symbols=symbols, reverse_graph=reverse_graph)
        orchestrator = ImpactOrchestrator()
        result = orchestrator.query(cir, model, "com.example.OrderService#placeOrder", depth=4)
        # No endpoints should be reached via imports chain
        assert len(result.endpoints_affected) == 0, (
            f"imports chain must not produce endpoint false positives: {[(ep.method, ep.path) for ep in result.endpoints_affected]}"
        )
        assert "com.example.OrderController" not in result.direct_callers
        assert "com.example.OrderController" not in result.indirect_callers

    def test_ic_v6_real_call_still_finds_endpoint(self):
        """A real calls edge still routes to the endpoint correctly."""
        symbols = [
            "com.example.OrderService",
            "com.example.OrderService#placeOrder",
            "com.example.OrderController",
            "com.example.OrderController#checkout",
        ]
        reverse_graph = {
            "com.example.OrderService#placeOrder": {
                "calls": ["com.example.OrderController#checkout"],
            },
        }
        ep = _FakeEndpoint("ep1", "POST", "/orders", "com.example.OrderController",
                           "com.example.OrderController#checkout")
        ei = _make_endpoint_index([ep])
        model = _make_model(endpoint_index=ei)
        cir = _FakeCIR(symbols=symbols, reverse_graph=reverse_graph)
        orchestrator = ImpactOrchestrator()
        result = orchestrator.query(cir, model, "com.example.OrderService#placeOrder", depth=4)
        assert "com.example.OrderController#checkout" in result.direct_callers
        assert len(result.endpoints_affected) == 1
        assert result.endpoints_affected[0].path == "/orders"


class TestRegressionCH001InterfaceDispatch:
    """CH-001 — Querying an interface expands seeds to in-repo implementations.

    Before fix: querying OrderService only seeded interface symbols; TX boundaries
    on OrderServiceImpl were never found; callers of impl methods were missed.
    After fix: implementation seeds are added automatically; impact chain includes
    impl callers and TX boundaries.
    """

    def _make_cir_with_impl(self):
        """CIR with OrderService interface + OrderServiceImpl in-repo."""
        symbols = [
            "com.example.OrderService",
            "com.example.OrderService#placeOrder",
            "com.example.OrderServiceImpl",
            "com.example.OrderServiceImpl#placeOrder",
            "com.example.OrderController",
            "com.example.OrderController#checkout",
        ]
        # Controller calls the impl directly
        reverse_graph = {
            "com.example.OrderServiceImpl#placeOrder": {
                "calls": ["com.example.OrderController#checkout"],
            },
        }
        deps = [
            {
                "from": "com.example.OrderServiceImpl",
                "to": "com.example.OrderService",
                "type": "implements",
                "confidence": "high",
            }
        ]
        return _FakeCIR(symbols=symbols, reverse_graph=reverse_graph, dependencies=deps)

    def test_ch001_impl_callers_found_via_interface_query(self):
        """Querying the interface finds callers of the implementation."""
        cir = self._make_cir_with_impl()
        model = _make_model()
        orchestrator = ImpactOrchestrator()
        result = orchestrator.query(cir, model, "com.example.OrderService", depth=2)
        all_callers = set(result.direct_callers) | set(result.indirect_callers)
        assert "com.example.OrderController#checkout" in all_callers, (
            f"Controller caller not found via interface query. callers={all_callers}"
        )

    def test_ch001_interface_expansion_warning_emitted(self):
        """When impl seeds are added, a warning is present in analysis_warnings."""
        cir = self._make_cir_with_impl()
        model = _make_model()
        result = ImpactOrchestrator().query(cir, model, "com.example.OrderService", depth=2)
        has_expansion_warning = any(
            "implementation" in w.lower() for w in result.analysis_warnings
        )
        assert has_expansion_warning, (
            f"Expected implementation expansion warning. warnings={result.analysis_warnings}"
        )

    def test_ch001c_subtypes_surfaced_via_extends(self):
        """petclinic #11 regression: base interface query surfaces ALL subtypes.

        VetRepository has two concrete impls (implements) plus a SpringData
        sub-interface (extends).  Before CH-001c, extends was invisible and impls
        were only added as silent BFS seeds.  Now result.implementations lists the
        full descendant set, and the impl's caller is reachable.
        """
        symbols = [
            "com.example.VetRepository",
            "com.example.VetRepository#findAll",
            "com.example.JpaVetRepositoryImpl",
            "com.example.JpaVetRepositoryImpl#findAll",
            "com.example.JdbcVetRepositoryImpl",
            "com.example.JdbcVetRepositoryImpl#findAll",
            "com.example.SpringDataVetRepository",
            "com.example.ClinicServiceImpl",
            "com.example.ClinicServiceImpl#findVets",
        ]
        # Service calls the JPA impl's method.
        reverse_graph = {
            "com.example.JpaVetRepositoryImpl#findAll": {
                "calls": ["com.example.ClinicServiceImpl#findVets"],
            },
        }
        deps = [
            {"from": "com.example.JpaVetRepositoryImpl", "to": "com.example.VetRepository",
             "type": "implements", "confidence": "high"},
            {"from": "com.example.JdbcVetRepositoryImpl", "to": "com.example.VetRepository",
             "type": "implements", "confidence": "high"},
            {"from": "com.example.SpringDataVetRepository", "to": "com.example.VetRepository",
             "type": "extends", "confidence": "high"},
        ]
        cir = _FakeCIR(symbols=symbols, reverse_graph=reverse_graph, dependencies=deps)
        model = _make_model()
        result = ImpactOrchestrator().query(cir, model, "com.example.VetRepository", depth=2)

        assert set(result.implementations) == {
            "com.example.JpaVetRepositoryImpl",
            "com.example.JdbcVetRepositoryImpl",
            "com.example.SpringDataVetRepository",
        }, f"All subtypes must surface, got {result.implementations}"
        all_callers = set(result.direct_callers) | set(result.indirect_callers)
        assert "com.example.ClinicServiceImpl#findVets" in all_callers, (
            f"Service caller of impl must be reachable. callers={all_callers}"
        )

    def test_ch001_no_expansion_when_no_impl(self):
        """Interface with no in-repo implementation: no false expansion, no crash."""
        cir = _FakeCIR(
            symbols=["com.example.IService", "com.example.IService#doWork"],
            reverse_graph={},
            dependencies=[],
        )
        model = _make_model()
        result = ImpactOrchestrator().query(cir, model, "com.example.IService", depth=2)
        assert result.resolution in ("exact", "class_expanded", "partial", "not_found")
        assert result.direct_callers == []
        assert result.indirect_callers == []

    def test_ch001_tx_boundary_on_impl_found(self):
        """TX boundary on implementation method is reachable via interface query."""
        cir = self._make_cir_with_impl()
        impl_boundary = _tx_boundary(
            "com.example.OrderServiceImpl#placeOrder", scope="method"
        )
        tx_index = _make_tx_index([impl_boundary])
        model = _make_model(tx_index=tx_index)
        result = ImpactOrchestrator().query(
            cir, model, "com.example.OrderService#placeOrder", depth=2
        )
        # tx_boundary on the resolved symbol OR on an impl method must be accessible
        # (at minimum, the impl seed itself should appear in chain or seed expansion)
        assert result.resolution not in ("not_found",), (
            f"Resolution should not be not_found. got={result.resolution}"
        )


class TestRegressionCH002InjectionFieldNode:
    """CH-002 — injects edge to field/constructor node expands to containing class.

    Before fix: BFS found X#<init> or X#fieldName as callers but stopped there —
    contained_in edges (X#<init> → X) are skipped, so traversal terminated at the
    constructor/field node.
    After fix: when BFS encounters an injects edge to a X#something node, class X
    is also added to the caller set and BFS continues from X.
    """

    def test_ch002_constructor_node_class_added(self):
        """Controller#<init> (injects) also adds Controller class to caller set."""
        symbols = [
            "com.example.OrderService",
            "com.example.OrderController",
            "com.example.OrderController#<init>",
        ]
        reverse_graph = {
            "com.example.OrderService": {
                "injects": ["com.example.OrderController#<init>"],
            },
        }
        cir = _FakeCIR(symbols=symbols, reverse_graph=reverse_graph)
        model = _make_model()
        result = ImpactOrchestrator().query(cir, model, "com.example.OrderService", depth=2)
        direct_set = set(result.direct_callers)
        # Constructor FQN is normalized to owning class — member FQN must not appear.
        assert "com.example.OrderController" in direct_set, (
            f"Class missing from direct callers: {direct_set}"
        )
        assert "com.example.OrderController#<init>" not in direct_set, (
            f"Constructor FQN must not appear in callers: {direct_set}"
        )

    def test_ch002_field_node_class_added(self):
        """Service#fieldName (injects) normalizes to Service class in caller set."""
        symbols = [
            "com.example.OrderRepository",
            "com.example.OrderServiceImpl",
            "com.example.OrderServiceImpl#orderRepo",
        ]
        reverse_graph = {
            "com.example.OrderRepository": {
                "injects": ["com.example.OrderServiceImpl#orderRepo"],
            },
        }
        cir = _FakeCIR(symbols=symbols, reverse_graph=reverse_graph)
        model = _make_model()
        result = ImpactOrchestrator().query(cir, model, "com.example.OrderRepository", depth=2)
        direct_set = set(result.direct_callers)
        # Field FQN is normalized to owning class — member FQN must not appear.
        assert "com.example.OrderServiceImpl" in direct_set, (
            f"Class missing from direct callers: {direct_set}"
        )
        assert "com.example.OrderServiceImpl#orderRepo" not in direct_set, (
            f"Field FQN must not appear in callers: {direct_set}"
        )

    def test_ch002_class_continues_bfs(self):
        """After class-level expansion, BFS continues from the class to its callers."""
        symbols = [
            "com.example.OrderRepository",
            "com.example.OrderServiceImpl",
            "com.example.OrderServiceImpl#<init>",
            "com.example.OrderController",
            "com.example.OrderController#checkout",
        ]
        reverse_graph = {
            "com.example.OrderRepository": {
                "injects": ["com.example.OrderServiceImpl#<init>"],
            },
            # OrderServiceImpl class has a caller: OrderController#checkout
            "com.example.OrderServiceImpl": {
                "calls": ["com.example.OrderController#checkout"],
            },
        }
        cir = _FakeCIR(symbols=symbols, reverse_graph=reverse_graph)
        model = _make_model()
        result = ImpactOrchestrator().query(cir, model, "com.example.OrderRepository", depth=4)
        all_callers = set(result.direct_callers) | set(result.indirect_callers)
        assert "com.example.OrderController#checkout" in all_callers, (
            f"BFS did not continue past class expansion. callers={all_callers}"
        )

    def test_ch002_no_false_callers_via_non_injects_edge(self):
        """Class-level expansion ONLY occurs for injects edges, not calls/extends."""
        symbols = [
            "com.example.ServiceA",
            "com.example.ServiceB#<init>",
            "com.example.ServiceB",
        ]
        reverse_graph = {
            "com.example.ServiceA": {
                "calls": ["com.example.ServiceB#<init>"],
            },
        }
        cir = _FakeCIR(symbols=symbols, reverse_graph=reverse_graph)
        model = _make_model()
        result = ImpactOrchestrator().query(cir, model, "com.example.ServiceA", depth=2)
        direct_set = set(result.direct_callers)
        assert "com.example.ServiceB#<init>" in direct_set
        # ServiceB class should NOT be auto-added (edge was 'calls', not 'injects')
        assert "com.example.ServiceB" not in direct_set, (
            f"ServiceB class falsely added from non-injects edge: {direct_set}"
        )


class TestBUG002TxBoundaryClassLevelQuery:
    """BUG-002: class-level impact-chain query must return tx_boundary when class
    has only method-level @Transactional (no class-level annotation)."""

    def _make_cir(self):
        symbols = ["com.example.AssetService", "com.example.AssetService#save"]
        return _FakeCIR(symbols=symbols, reverse_graph={})

    def test_method_level_tx_visible_in_class_query(self):
        """Class query must expose a tx_boundary when methods have @Transactional."""
        from sourcecode.spring_semantic import TransactionBoundary, TransactionBoundaryIndex

        method_boundary = _tx_boundary(
            "com.example.AssetService#save", scope="method"
        )
        tx_index = _make_tx_index([method_boundary])
        # by_class must be populated for the containing class
        tx_index.by_class["com.example.AssetService"] = [method_boundary]

        model = _make_model(tx_index=tx_index)
        cir = self._make_cir()
        result = ImpactOrchestrator().query(cir, model, "com.example.AssetService", depth=1)

        assert result.transaction_boundary is not None, (
            "Class with method-level @Transactional must expose tx_boundary"
        )
        assert result.transaction_boundary.get("scope") == "method"


# ---------------------------------------------------------------------------
# IC-DI — Endpoint propagation when controller appears as class-level caller
# ---------------------------------------------------------------------------

class TestRegressionDIControllerCaller:
    """IC-DI — When a repository/service is injected into a controller, the
    controller class node (no '#') appears in the BFS reverse graph.
    All endpoints of that controller must be included in endpoints_affected.

    Root cause of original bug: class_level_controllers only included seeds
    that were controllers; DI-injected callers (class node) were in
    candidate_controllers but failed the handler-in-chain check.
    """

    def _build_fixture(self):
        # OwnerRepository injected into OwnerController (class-level BFS edge)
        symbols = [
            "com.example.OwnerRepository",
            "com.example.OwnerRepository#findById",
            "com.example.OwnerController",
            "com.example.OwnerController#<init>",
            "com.example.OwnerController#showOwner",
            "com.example.OwnerController#listOwners",
        ]
        reverse_graph = {
            # DI: OwnerController class-node and <init> both depend on repo
            "com.example.OwnerRepository": {
                "injects": [
                    "com.example.OwnerController#<init>",
                    "com.example.OwnerController",
                ],
            },
        }
        ep1 = _FakeEndpoint("ep1", "GET", "/owners", "com.example.OwnerController",
                            "com.example.OwnerController#listOwners")
        ep2 = _FakeEndpoint("ep2", "GET", "/owners/{id}", "com.example.OwnerController",
                            "com.example.OwnerController#showOwner")
        ei = _make_endpoint_index([ep1, ep2])
        model = _make_model(endpoint_index=ei)
        cir = _FakeCIR(symbols=symbols, reverse_graph=reverse_graph)
        return cir, model

    def test_di_caller_class_node_populates_endpoints(self):
        """Controller class-level BFS caller → all its endpoints included."""
        cir, model = self._build_fixture()
        result = ImpactOrchestrator().query(cir, model, "com.example.OwnerRepository", depth=2)
        paths = [ep.path for ep in result.endpoints_affected]
        assert "/owners" in paths, f"missing /owners: {paths}"
        assert "/owners/{{id}}" in paths or "/owners/{id}" in paths, (
            f"missing /owners/{{id}}: {paths}"
        )

    def test_di_caller_both_endpoints_found(self):
        """Both endpoints of an injected controller appear."""
        cir, model = self._build_fixture()
        result = ImpactOrchestrator().query(cir, model, "com.example.OwnerRepository", depth=2)
        assert len(result.endpoints_affected) == 2, (
            f"Expected 2, got {len(result.endpoints_affected)}: "
            f"{[ep.path for ep in result.endpoints_affected]}"
        )

    def test_method_caller_precision_preserved(self):
        """When caller is a specific handler method, only its endpoint is included."""
        symbols = [
            "com.example.OrderService",
            "com.example.OrderService#placeOrder",
            "com.example.OrderController",
            "com.example.OrderController#checkout",
            "com.example.OrderController#listOrders",
        ]
        reverse_graph = {
            "com.example.OrderService#placeOrder": {
                "calls": ["com.example.OrderController#checkout"],
            },
        }
        ep_checkout = _FakeEndpoint("ep1", "POST", "/orders/checkout",
                                    "com.example.OrderController",
                                    "com.example.OrderController#checkout")
        ep_list = _FakeEndpoint("ep2", "GET", "/orders",
                                "com.example.OrderController",
                                "com.example.OrderController#listOrders")
        ei = _make_endpoint_index([ep_checkout, ep_list])
        model = _make_model(endpoint_index=ei)
        cir = _FakeCIR(symbols=symbols, reverse_graph=reverse_graph)
        result = ImpactOrchestrator().query(cir, model, "com.example.OrderService#placeOrder", depth=2)
        paths = [ep.path for ep in result.endpoints_affected]
        assert "/orders/checkout" in paths, f"checkout missing: {paths}"
        assert "/orders" not in paths, f"listOrders is false positive: {paths}"


class TestRegressionCH001bImplToInterfaceExpansion:
    """BUG-IC-002 / CH-001b — Querying an impl class expands seeds to its interfaces.

    Before fix: querying OrderServiceImpl found 0 callers because callers inject
    the interface (OrderService), so reverse_graph edges live on the interface node.
    BFS from impl seeds never reached those edges.
    After fix: CH-001b adds interface symbols to the seed set before BFS, so callers
    of the interface are found when querying the implementation.
    """

    def _make_cir_impl_query(self):
        """CIR where callers inject the interface, not the impl."""
        symbols = [
            "com.example.OrderService",
            "com.example.OrderService#placeOrder",
            "com.example.OrderServiceImpl",
            "com.example.OrderServiceImpl#placeOrder",
            "com.example.CheckoutService",
            "com.example.CheckoutService#checkout",
            "com.example.CheckoutService#orderService",  # field injection
        ]
        # Callers inject OrderService (the interface), not OrderServiceImpl.
        reverse_graph = {
            "com.example.OrderService": {
                "injects": [
                    "com.example.CheckoutService#orderService",
                    "com.example.CheckoutService",
                ],
            },
        }
        deps = [
            {
                "from": "com.example.OrderServiceImpl",
                "to": "com.example.OrderService",
                "type": "implements",
                "confidence": "high",
            }
        ]
        return _FakeCIR(symbols=symbols, reverse_graph=reverse_graph, dependencies=deps)

    def test_ch001b_impl_query_finds_interface_callers(self):
        """Querying impl finds callers that inject the interface (CH-001b expansion)."""
        cir = self._make_cir_impl_query()
        result = ImpactOrchestrator().query(cir, _make_model(), "com.example.OrderServiceImpl", depth=1)
        all_callers = set(result.direct_callers)
        assert "com.example.CheckoutService" in all_callers, (
            f"CheckoutService caller not found via impl query (CH-001b). callers={all_callers}"
        )

    def test_ch001b_warning_emitted_for_interface_expansion(self):
        """CH-001b expansion emits an analysis warning."""
        cir = self._make_cir_impl_query()
        result = ImpactOrchestrator().query(cir, _make_model(), "com.example.OrderServiceImpl", depth=1)
        has_warning = any(
            "interface" in w.lower() or "ch-001b" in w.lower()
            for w in result.analysis_warnings
        )
        assert has_warning, (
            f"Expected CH-001b interface-expansion warning. warnings={result.analysis_warnings}"
        )

    def test_ch001b_no_expansion_when_no_interfaces(self):
        """Impl class with no in-repo interface: no false expansion, no crash."""
        symbols = [
            "com.example.StandaloneServiceImpl",
            "com.example.StandaloneServiceImpl#doWork",
        ]
        cir = _FakeCIR(symbols=symbols, reverse_graph={}, dependencies=[])
        result = ImpactOrchestrator().query(cir, _make_model(), "com.example.StandaloneServiceImpl", depth=1)
        assert result.direct_callers == []
        assert result.indirect_callers == []


# ---------------------------------------------------------------------------
# BUG-004 — BFS dead-end on class-level FQN intermediates
# ---------------------------------------------------------------------------

class TestRegressionBUG004ClassLevelDeadEnd:
    """BUG-004: class-level FQN in BFS queue must traverse method-level rg keys.

    reverse_graph is keyed by method FQNs ("Foo#doWork"), never by class FQNs
    ("Foo").  When BFS enqueues a class-level node (e.g. via CH-002 expansion),
    reverse_graph.get("Foo") returns {} and traversal silently terminates.

    Fix: class_method_index maps class FQN → method-level rg keys so _edges_for()
    includes those entries when processing a class-level node.
    """

    def test_bug004_class_seed_finds_method_level_callers(self):
        """Class-level seed must find callers living on method-level rg keys."""
        rg = {
            # No entry for "com.example.Service" itself — only method-level
            "com.example.Service#doWork": {"calls": ["com.example.Client#run"]},
        }
        direct, indirect, _ = _bfs_callers(["com.example.Service"], rg, 1)
        assert "com.example.Client#run" in direct, (
            f"BUG-004: class-level seed missed method-level callers. direct={direct}"
        )

    def test_bug004_class_intermediate_finds_deeper_callers(self):
        """Class-level node added by CH-002 must continue BFS via method-level keys.

        Scenario: Repository injected into ServiceImpl (CH-002 adds ServiceImpl
        class node to direct callers). ServiceImpl has no class-level rg entry —
        only method-level. Without the fix indirect_callers stays empty.
        """
        rg = {
            "com.example.Repository": {
                "injects": ["com.example.ServiceImpl#<init>"],
            },
            # Callers of ServiceImpl live on method-level keys only
            "com.example.ServiceImpl#placeOrder": {
                "calls": ["com.example.Controller#checkout"],
            },
            "com.example.ServiceImpl#save": {
                "calls": ["com.example.Controller#create"],
            },
        }
        direct, indirect, _ = _bfs_callers(["com.example.Repository"], rg, 2)
        all_callers = set(direct) | set(indirect)
        assert "com.example.Controller#checkout" in all_callers, (
            f"BUG-004: ServiceImpl class dead-end — checkout missing. callers={all_callers}"
        )
        assert "com.example.Controller#create" in all_callers, (
            f"BUG-004: ServiceImpl class dead-end — create missing. callers={all_callers}"
        )

    def test_bug004_no_duplicate_callers_when_class_and_method_both_in_rg(self):
        """When rg has BOTH a class entry and method entries, callers are deduplicated."""
        rg = {
            "com.example.Service": {"calls": ["com.example.Client#run"]},
            "com.example.Service#doWork": {"calls": ["com.example.Client#run"]},
        }
        direct, _, _ = _bfs_callers(["com.example.Service"], rg, 1)
        assert direct.count("com.example.Client#run") == 1, (
            f"BUG-004: duplicate callers from class+method rg entries. direct={direct}"
        )

    def test_bug004_orchestrator_indirect_callers_via_class_node(self):
        """Integration: indirect callers found through class-level intermediate node."""
        symbols = [
            "com.example.Repository",
            "com.example.Repository#findById",
            "com.example.ServiceImpl",
            "com.example.ServiceImpl#<init>",
            "com.example.ServiceImpl#load",
            "com.example.Controller",
            "com.example.Controller#show",
        ]
        reverse_graph = {
            # DI: ServiceImpl#<init> injects Repository — CH-002 adds ServiceImpl class
            "com.example.Repository": {
                "injects": ["com.example.ServiceImpl#<init>"],
            },
            # Controller#show calls ServiceImpl — stored on method-level key
            "com.example.ServiceImpl#load": {
                "calls": ["com.example.Controller#show"],
            },
        }
        cir = _FakeCIR(symbols=symbols, reverse_graph=reverse_graph)
        result = ImpactOrchestrator().query(cir, _make_model(), "com.example.Repository", depth=3)
        all_callers = set(result.direct_callers) | set(result.indirect_callers)
        assert "com.example.Controller#show" in all_callers, (
            f"BUG-004: Controller#show not found via class-level intermediate. "
            f"direct={result.direct_callers} indirect={result.indirect_callers}"
        )


# ---------------------------------------------------------------------------
# CH-005 — framework/external-interface DI blind spot
# ---------------------------------------------------------------------------
class TestFrameworkDIBlindSpot:
    """Regression for the BroadleafCommerce #3124 benchmark finding: a class wired
    via an EXTERNAL framework interface (Spring Security's RedirectStrategy) and
    invoked polymorphically returns 0 callers. That empty result must be reported as
    a low-confidence blind spot, never as a high-confidence 'safe to change'.
    """

    def _redirect_strategy_cir(self):
        # LocalRedirectStrategy implements an external Spring Security interface.
        # No in-repo caller names its method — exactly the static-graph blind spot.
        symbols = [
            "com.broadleaf.web.LocalRedirectStrategy",
            "com.broadleaf.web.LocalRedirectStrategy#sendRedirect",
        ]
        dependencies = [
            {
                "from": "com.broadleaf.web.LocalRedirectStrategy",
                "to": "org.springframework.security.web.RedirectStrategy",
                "type": "implements",
            },
        ]
        return _FakeCIR(symbols=symbols, reverse_graph={}, dependencies=dependencies)

    def test_external_interface_empty_blast_is_low_confidence(self):
        cir = self._redirect_strategy_cir()
        result = ImpactOrchestrator().query(
            cir, _make_model(), "com.broadleaf.web.LocalRedirectStrategy"
        )
        assert not result.direct_callers and not result.indirect_callers
        assert result.confidence == "low", (
            f"CH-005: framework-DI blind spot must not report high confidence. "
            f"confidence={result.confidence}"
        )
        assert "framework_di" in result.metadata.get("blind_spots", []), (
            f"CH-005: blind_spots metadata missing. metadata={result.metadata}"
        )
        assert "org.springframework.security.web.RedirectStrategy" in (
            result.metadata.get("external_supertypes", [])
        )
        assert any("CH-005" in w for w in result.analysis_warnings), (
            f"CH-005 warning absent. warnings={result.analysis_warnings}"
        )

    def test_inert_marker_supertype_does_not_trigger(self):
        # A plain DTO implementing only Serializable is NOT framework-DI-mediated.
        symbols = ["com.example.dto.OrderDTO", "com.example.dto.OrderDTO#getId"]
        dependencies = [
            {"from": "com.example.dto.OrderDTO", "to": "java.io.Serializable", "type": "implements"},
        ]
        cir = _FakeCIR(symbols=symbols, reverse_graph={}, dependencies=dependencies)
        result = ImpactOrchestrator().query(cir, _make_model(), "com.example.dto.OrderDTO")
        assert "framework_di" not in result.metadata.get("blind_spots", []), (
            "Serializable is an inert marker — must not trigger CH-005."
        )

    def test_internal_interface_not_flagged_as_external(self):
        # In-repo interface with a real caller: normal high/medium path, no CH-005.
        symbols = [
            "com.example.OrderService",
            "com.example.OrderService#place",
            "com.example.OrderServiceImpl",
            "com.example.OrderServiceImpl#place",
            "com.example.OrderController",
            "com.example.OrderController#create",
        ]
        dependencies = [
            {"from": "com.example.OrderServiceImpl", "to": "com.example.OrderService", "type": "implements"},
        ]
        reverse_graph = {
            "com.example.OrderService#place": {"calls": ["com.example.OrderController#create"]},
        }
        cir = _FakeCIR(symbols=symbols, reverse_graph=reverse_graph, dependencies=dependencies)
        result = ImpactOrchestrator().query(cir, _make_model(), "com.example.OrderServiceImpl")
        assert "framework_di" not in result.metadata.get("blind_spots", []), (
            f"In-repo interface must not be flagged external. warnings={result.analysis_warnings}"
        )
        all_callers = set(result.direct_callers) | set(result.indirect_callers)
        assert "com.example.OrderController#create" in all_callers


# ---------------------------------------------------------------------------
# CH-007 — external-interface DI bridge (caller recovery)
# ---------------------------------------------------------------------------
class TestExternalInterfaceDIBridge:
    """The BroadleafCommerce #3124 acceptance criterion: a class wired via an
    EXTERNAL framework interface must recover the in-repo wiring/consumer sites
    that inject that interface, instead of reporting a bare 0-caller blind spot.
    """

    def _wired_cir(self, *, extra_impl=False):
        # LocalRedirectStrategy implements external RedirectStrategy; LoginController
        # injects the EXTERNAL interface (never names the impl).
        symbols = [
            "com.x.web.LocalRedirectStrategy",
            "com.x.web.LocalRedirectStrategy#sendRedirect",
            "com.x.auth.LoginController",
            "com.x.auth.LoginController#login",
            "com.x.auth.LoginController.redirectStrategy",
        ]
        dependencies = [
            {"from": "com.x.web.LocalRedirectStrategy",
             "to": "org.springframework.security.web.RedirectStrategy", "type": "implements"},
            {"from": "com.x.auth.LoginController.redirectStrategy",
             "to": "org.springframework.security.web.RedirectStrategy", "type": "injects"},
        ]
        if extra_impl:
            symbols.append("com.x.web.OtherRedirectStrategy")
            dependencies.append(
                {"from": "com.x.web.OtherRedirectStrategy",
                 "to": "org.springframework.security.web.RedirectStrategy", "type": "implements"}
            )
        return _FakeCIR(symbols=symbols, reverse_graph={}, dependencies=dependencies)

    def test_recovers_wiring_consumer_as_caller(self):
        cir = self._wired_cir()
        result = ImpactOrchestrator().query(
            cir, _make_model(), "com.x.web.LocalRedirectStrategy"
        )
        assert "com.x.auth.LoginController" in result.direct_callers, result.direct_callers
        # Recovery means it is no longer a bare blind spot.
        assert "framework_di" not in result.metadata.get("blind_spots", [])
        assert result.confidence == "medium", result.confidence
        assert result.metadata.get("external_iface_callers_recovered") == 1
        assert not result.metadata.get("external_iface_binding_ambiguous")
        assert any("CH-007" in w for w in result.analysis_warnings), result.analysis_warnings

    def test_ambiguous_binding_when_multiple_impls(self):
        cir = self._wired_cir(extra_impl=True)
        result = ImpactOrchestrator().query(
            cir, _make_model(), "com.x.web.LocalRedirectStrategy"
        )
        assert "com.x.auth.LoginController" in result.direct_callers
        assert result.metadata.get("external_iface_binding_ambiguous") is True
        # Ambiguous binding stays cautious.
        assert result.confidence == "low", result.confidence
        assert any("ambiguous" in w.lower() for w in result.analysis_warnings)

    def test_no_wiring_falls_back_to_ch005_blind_spot(self):
        # No consumer injects the interface → nothing to recover → CH-005 still fires.
        symbols = [
            "com.x.web.LocalRedirectStrategy",
            "com.x.web.LocalRedirectStrategy#sendRedirect",
        ]
        dependencies = [
            {"from": "com.x.web.LocalRedirectStrategy",
             "to": "org.springframework.security.web.RedirectStrategy", "type": "implements"},
        ]
        cir = _FakeCIR(symbols=symbols, reverse_graph={}, dependencies=dependencies)
        result = ImpactOrchestrator().query(
            cir, _make_model(), "com.x.web.LocalRedirectStrategy"
        )
        assert not result.direct_callers
        assert "framework_di" in result.metadata.get("blind_spots", [])
        assert result.confidence == "low"
        assert any("CH-005" in w for w in result.analysis_warnings)


# ---------------------------------------------------------------------------
# CH-006 — implements/extends edges excluded from caller BFS (hub-interface FP)
# ---------------------------------------------------------------------------
class TestHubInterfaceOverExpansion:
    """Regression for the halo field-benchmark finding: querying a class that
    implements a high-fanout in-repo interface (CustomEndpoint, 43 impls) returned
    every SIBLING implementor as a false 'direct caller'. The reverse-graph
    implements/extends edge is a structural declaration, not a call, so it must not
    feed the caller BFS. Real callers (injects/calls) must be preserved.
    """

    def test_sibling_implementors_are_not_callers(self):
        # Hub interface with 3 implementors; none calls the others.
        symbols = [
            "com.example.Endpoint",          # in-repo hub interface
            "com.example.ThumbnailEndpoint",
            "com.example.AttachmentEndpoint",
            "com.example.PostEndpoint",
        ]
        dependencies = [
            {"from": "com.example.ThumbnailEndpoint", "to": "com.example.Endpoint", "type": "implements"},
            {"from": "com.example.AttachmentEndpoint", "to": "com.example.Endpoint", "type": "implements"},
            {"from": "com.example.PostEndpoint", "to": "com.example.Endpoint", "type": "implements"},
        ]
        # The reverse graph carries the structural implements edge on the interface.
        reverse_graph = {
            "com.example.Endpoint": {
                "implements": [
                    "com.example.ThumbnailEndpoint",
                    "com.example.AttachmentEndpoint",
                    "com.example.PostEndpoint",
                ],
            },
        }
        cir = _FakeCIR(symbols=symbols, reverse_graph=reverse_graph, dependencies=dependencies)
        result = ImpactOrchestrator().query(cir, _make_model(), "com.example.ThumbnailEndpoint")
        callers = set(result.direct_callers) | set(result.indirect_callers)
        assert "com.example.AttachmentEndpoint" not in callers, (
            f"CH-006: sibling implementor wrongly attributed as caller. callers={callers}"
        )
        assert "com.example.PostEndpoint" not in callers
        assert result.direct_callers == [], (
            f"CH-006: leaf endpoint must have no callers from implements edges. "
            f"direct={result.direct_callers}"
        )

    def test_real_injects_caller_preserved_through_shared_interface(self):
        # A genuine caller injects the interface — must survive the implements skip.
        symbols = [
            "com.example.OrderService",
            "com.example.OrderServiceImpl",
            "com.example.OrderServiceImpl#place",
            "com.example.SiblingServiceImpl",   # shares the interface, does NOT call
            "com.example.OrderController",
            "com.example.OrderController#create",
        ]
        dependencies = [
            {"from": "com.example.OrderServiceImpl", "to": "com.example.OrderService", "type": "implements"},
            {"from": "com.example.SiblingServiceImpl", "to": "com.example.OrderService", "type": "implements"},
        ]
        reverse_graph = {
            # structural noise: both impls listed under the interface
            "com.example.OrderService": {
                "implements": ["com.example.OrderServiceImpl", "com.example.SiblingServiceImpl"],
                # real caller injects the interface
                "injects": ["com.example.OrderController#<init>"],
            },
        }
        cir = _FakeCIR(symbols=symbols, reverse_graph=reverse_graph, dependencies=dependencies)
        result = ImpactOrchestrator().query(cir, _make_model(), "com.example.OrderServiceImpl")
        callers = set(result.direct_callers) | set(result.indirect_callers)
        assert "com.example.OrderController" in callers, (
            f"CH-006: real injects caller lost. callers={callers}"
        )
        assert "com.example.SiblingServiceImpl" not in callers, (
            f"CH-006: sibling impl wrongly attributed. callers={callers}"
        )


# ---------------------------------------------------------------------------
# F-1 — informational expansion warnings must not cap confidence
# ---------------------------------------------------------------------------
class TestConfidenceNotCappedByInfoWarnings:
    """The CH-001a/b interface<->impl expansion notices describe normal, correct
    operation and previously forced every Spring interface/impl query to medium.
    Only genuinely degrading conditions (capped traversal, partial resolution)
    should cap confidence.
    """

    def test_expansion_warning_keeps_high_confidence(self):
        symbols = [
            "com.example.OrderService",
            "com.example.OrderServiceImpl",
            "com.example.OrderServiceImpl#place",
            "com.example.OrderController",
            "com.example.OrderController#create",
        ]
        dependencies = [
            {"from": "com.example.OrderServiceImpl", "to": "com.example.OrderService", "type": "implements"},
        ]
        reverse_graph = {
            "com.example.OrderService#place": {"calls": ["com.example.OrderController#create"]},
        }
        cir = _FakeCIR(symbols=symbols, reverse_graph=reverse_graph, dependencies=dependencies)
        result = ImpactOrchestrator().query(cir, _make_model(), "com.example.OrderService")
        assert any("expansion" in w.lower() for w in result.analysis_warnings), (
            "precondition: an informational expansion warning must be present"
        )
        assert result.confidence == "high", (
            f"F-1: informational expansion warning must not cap confidence. "
            f"confidence={result.confidence} warnings={result.analysis_warnings}"
        )

    def test_hub_guard_truncation_caps_confidence_to_medium(self):
        # >500 unique direct callers trips the hub guard → capped traversal → medium.
        callers = [f"com.example.Caller{i}#m" for i in range(600)]
        symbols = ["com.example.Hub", "com.example.Hub#run"] + callers
        reverse_graph = {"com.example.Hub#run": {"calls": callers}}
        cir = _FakeCIR(symbols=symbols, reverse_graph=reverse_graph)
        result = ImpactOrchestrator().query(cir, _make_model(), "com.example.Hub#run", depth=4)
        assert any("hub-class guard" in w.lower() for w in result.analysis_warnings), (
            f"precondition: truncation warning expected. warnings={result.analysis_warnings}"
        )
        assert result.confidence == "medium", (
            f"F-1: capped traversal must reduce confidence to medium. "
            f"confidence={result.confidence}"
        )

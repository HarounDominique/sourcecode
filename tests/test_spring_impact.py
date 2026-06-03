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
    ):
        self.symbols = symbols or []
        self.reverse_graph = reverse_graph or {}
        self.endpoints = endpoints or []
        self.call_graph = call_graph or []
        self.dependencies = dependencies or []
        self.files = files or []
        self.metadata = metadata or {}
        self.cir_hash = "deadbeef00000000"
        self._raw_ir = {"graph": {"nodes": [], "edges": self.call_graph}}


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

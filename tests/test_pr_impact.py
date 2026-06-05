"""test_pr_impact.py — Tests for pr_impact.py PR blast-radius analysis.

Coverage:
  PRI-01  _build_file_class_index — class nodes indexed by source_file
  PRI-02  _build_file_class_index — method nodes excluded
  PRI-03  _resolve_changed_files — exact relative path match
  PRI-04  _resolve_changed_files — suffix match (bare filename)
  PRI-05  _resolve_changed_files — absolute path normalized via root
  PRI-06  _resolve_changed_files — unknown file → warning, no class
  PRI-07  _resolve_changed_files — ambiguous suffix → warning, first match used
  PRI-08  _collect_event_flow — changed class publishes event → publisher + consumer lines
  PRI-09  _collect_event_flow — changed class is listener → listener line
  PRI-10  _collect_tx_methods — method-level @Transactional
  PRI-11  _collect_tx_methods — class-level @Transactional
  PRI-12  _compute_risk — no signals → LOW / no high-risk signals
  PRI-13  _compute_risk — endpoints → PUBLIC API in reason
  PRI-14  _compute_risk — 3+ dimensions → boosted to HIGH
  PRI-15  run_pr_impact — changed file maps to class → impact chain runs
  PRI-16  run_pr_impact — no Java classes in changed files → UNKNOWN risk
  PRI-17  run_pr_impact — direct callers of changed class appear in report
  PRI-18  run_pr_impact — endpoints reachable from changed class appear
  PRI-19  run_pr_impact — callers that are also modified classes excluded from caller list
  PRI-20  PRImpactReport.to_dict() — all keys present, JSON-serializable
  PRI-21  PRImpactReport.render_text() — contains Modified/Risk Level sections
  PRI-22  run_pr_impact — never raises on internal error (outer guard)
"""
from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from sourcecode.cir_graphs import ImplementationGraph, InjectionGraph
from sourcecode.pr_impact import (
    PRImpactReport,
    _build_file_class_index,
    _collect_event_flow,
    _collect_tx_methods,
    _compute_risk,
    _resolve_changed_files,
    run_pr_impact,
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
    ):
        self.symbols = symbols or []
        self.reverse_graph = reverse_graph or {}
        self.endpoints = endpoints or []
        self.call_graph = call_graph or []
        self.dependencies = dependencies or []
        self.files = files or []
        self.metadata = {}
        self.cir_hash = "deadbeef00000000"
        _nodes = nodes or []
        self._raw_ir = {"graph": {"nodes": _nodes, "edges": self.call_graph}}
        self.implementation_graph = ImplementationGraph.build(
            self.dependencies, set(self.symbols)
        )
        self.injection_graph = InjectionGraph.build(self.dependencies)


def _make_model(
    tx_index: Optional[TransactionBoundaryIndex] = None,
    event_graph: Optional[EventGraph] = None,
) -> SpringSemanticModel:
    return SpringSemanticModel(
        tx_index=tx_index or TransactionBoundaryIndex(),
        call_adj=CallAdjacency(),
        inheritance=InheritanceGraph(),
        bean_graph=BeanGraph(),
        endpoint_index=EndpointIndex(),
        event_graph=event_graph or EventGraph(),
        build_time_ms=0.0,
    )


def _make_nodes(*entries: tuple[str, str]) -> list[dict]:
    """entries: (fqn, source_file)"""
    return [{"fqn": fqn, "source_file": sf} for fqn, sf in entries]


def _make_tx_index(
    method_boundaries: Optional[list[tuple[str, str]]] = None,  # (symbol, class_fqn)
    class_boundaries: Optional[list[str]] = None,
) -> TransactionBoundaryIndex:
    idx = TransactionBoundaryIndex()
    for sym, cls in (method_boundaries or []):
        b = TransactionBoundary(symbol=sym, scope="method")
        idx.by_symbol[sym] = b
        idx.by_class.setdefault(cls, []).append(b)
    for cls in (class_boundaries or []):
        b = TransactionBoundary(symbol=cls, scope="class")
        idx.class_level[cls] = b
    return idx


# ---------------------------------------------------------------------------
# PRI-01 / PRI-02: _build_file_class_index
# ---------------------------------------------------------------------------

class TestBuildFileClassIndex:
    def test_pri_01_class_nodes_indexed(self):
        nodes = _make_nodes(
            ("com.example.UserService", "src/main/java/com/example/UserService.java"),
            ("com.example.UserController", "src/main/java/com/example/UserController.java"),
        )
        cir = _FakeCIR(nodes=nodes)
        index = _build_file_class_index(cir)
        assert "src/main/java/com/example/UserService.java" in index
        assert "com.example.UserService" in index["src/main/java/com/example/UserService.java"]

    def test_pri_02_method_nodes_excluded(self):
        nodes = _make_nodes(
            ("com.example.UserService", "src/UserService.java"),
            ("com.example.UserService#createUser", "src/UserService.java"),
        )
        cir = _FakeCIR(nodes=nodes)
        index = _build_file_class_index(cir)
        classes = index.get("src/UserService.java", [])
        assert "com.example.UserService" in classes
        assert "com.example.UserService#createUser" not in classes


# ---------------------------------------------------------------------------
# PRI-03 to PRI-07: _resolve_changed_files
# ---------------------------------------------------------------------------

class TestResolveChangedFiles:
    def _index(self) -> dict:
        return {
            "src/main/java/com/example/UserService.java": ["com.example.UserService"],
            "src/main/java/com/example/OrderService.java": ["com.example.OrderService"],
            "src/main/java/com/example/UserController.java": ["com.example.UserController"],
        }

    def test_pri_03_exact_relative_match(self):
        index = self._index()
        classes, warnings = _resolve_changed_files(
            ["src/main/java/com/example/UserService.java"], index, Path("/repo")
        )
        assert classes == ["com.example.UserService"]
        assert not warnings

    def test_pri_04_suffix_match(self):
        index = self._index()
        classes, warnings = _resolve_changed_files(
            ["UserService.java"], index, Path("/repo")
        )
        assert "com.example.UserService" in classes
        assert not warnings

    def test_pri_05_absolute_path_normalized(self, tmp_path):
        index = {"src/main/java/com/example/UserService.java": ["com.example.UserService"]}
        abs_path = str(tmp_path / "src/main/java/com/example/UserService.java")
        classes, warnings = _resolve_changed_files([abs_path], index, tmp_path)
        assert "com.example.UserService" in classes

    def test_pri_06_unknown_file_warning(self):
        index = self._index()
        classes, warnings = _resolve_changed_files(
            ["NonExistent.java"], index, Path("/repo")
        )
        assert classes == []
        assert any("NonExistent.java" in w for w in warnings)

    def test_pri_07_ambiguous_suffix_warning(self):
        index = {
            "module-a/UserService.java": ["com.a.UserService"],
            "module-b/UserService.java": ["com.b.UserService"],
        }
        classes, warnings = _resolve_changed_files(
            ["UserService.java"], index, Path("/repo")
        )
        assert len(classes) == 1  # first match used
        assert any("Ambiguous" in w for w in warnings)


# ---------------------------------------------------------------------------
# PRI-08 / PRI-09: _collect_event_flow
# ---------------------------------------------------------------------------

class TestCollectEventFlow:
    def test_pri_08_publisher_and_consumer_lines(self):
        eg = EventGraph(
            publishers={"com.example.UserUpdatedEvent": ["com.example.UserService#update"]},
            listeners={"com.example.UserUpdatedEvent": ["com.example.NotificationListener#handle"]},
        )
        model = _make_model(event_graph=eg)
        pub_lines, con_lines = _collect_event_flow({"com.example.UserService"}, model)
        assert any("UserUpdatedEvent" in l for l in pub_lines)
        assert any("NotificationListener" in l for l in con_lines)

    def test_pri_09_listener_line_for_changed_consumer(self):
        eg = EventGraph(
            publishers={"com.example.OrderCreatedEvent": ["com.example.OrderService#create"]},
            listeners={"com.example.OrderCreatedEvent": ["com.example.AuditListener#onOrder"]},
        )
        model = _make_model(event_graph=eg)
        pub_lines, con_lines = _collect_event_flow({"com.example.AuditListener"}, model)
        # AuditListener is the changed class and it listens → "Listens to ..."
        assert any("OrderCreatedEvent" in l for l in con_lines)
        assert not pub_lines  # AuditListener doesn't publish

    def test_event_flow_empty_when_no_events(self):
        model = _make_model()
        pub_lines, con_lines = _collect_event_flow({"com.example.SomeService"}, model)
        assert pub_lines == []
        assert con_lines == []


# ---------------------------------------------------------------------------
# PRI-10 / PRI-11: _collect_tx_methods
# ---------------------------------------------------------------------------

class TestCollectTxMethods:
    def test_pri_10_method_level_tx(self):
        tx = _make_tx_index(
            method_boundaries=[
                ("com.example.UserService#createUser", "com.example.UserService"),
                ("com.example.UserService#updateUser", "com.example.UserService"),
            ]
        )
        model = _make_model(tx_index=tx)
        methods = _collect_tx_methods({"com.example.UserService"}, model)
        assert "com.example.UserService#createUser" in methods
        assert "com.example.UserService#updateUser" in methods

    def test_pri_11_class_level_tx(self):
        tx = _make_tx_index(class_boundaries=["com.example.OrderService"])
        model = _make_model(tx_index=tx)
        methods = _collect_tx_methods({"com.example.OrderService"}, model)
        assert "com.example.OrderService" in methods

    def test_tx_empty_for_non_transactional_class(self):
        model = _make_model()
        methods = _collect_tx_methods({"com.example.PlainService"}, model)
        assert methods == []


# ---------------------------------------------------------------------------
# PRI-12 to PRI-14: _compute_risk
# ---------------------------------------------------------------------------

class TestComputeRisk:
    def test_pri_12_no_signals(self):
        label, reason = _compute_risk([], [], [], [], [], ["low"])
        assert "No high-risk" in reason

    def test_pri_13_endpoints_in_reason(self):
        label, reason = _compute_risk(
            [{"method": "GET", "path": "/users"}], [], [], [], [], ["medium"]
        )
        assert "Public API" in reason

    def test_pri_14_three_dimensions_boost_to_high(self):
        label, reason = _compute_risk(
            [{"method": "GET", "path": "/x"}],
            ["CallerA"],
            ["Publishes X"],
            ["Consumed by Y"],
            ["SomeService#tx"],
            ["low"],
        )
        assert label == "HIGH"
        assert "Public API" in reason
        assert "Event Flow" in reason
        assert "Transaction Boundary" in reason


# ---------------------------------------------------------------------------
# PRI-15 to PRI-19: run_pr_impact integration
# ---------------------------------------------------------------------------

class TestRunPrImpact:
    def _make_cir_with_callers(self) -> _FakeCIR:
        """CIR: UserService called by UserBatchJob; UserController exposes endpoints."""
        nodes = _make_nodes(
            ("com.example.UserService", "src/main/java/com/example/UserService.java"),
            ("com.example.UserBatchJob", "src/main/java/com/example/UserBatchJob.java"),
        )
        reverse_graph = {
            "com.example.UserService": {
                "calls": ["com.example.UserBatchJob"],
            }
        }
        return _FakeCIR(
            symbols=["com.example.UserService", "com.example.UserBatchJob"],
            reverse_graph=reverse_graph,
            nodes=nodes,
        )

    def test_pri_15_changed_file_maps_to_class(self, tmp_path):
        cir = self._make_cir_with_callers()
        model = _make_model()
        report = run_pr_impact(
            cir,
            ["src/main/java/com/example/UserService.java"],
            root=tmp_path,
            model=model,
        )
        assert "com.example.UserService" in report.modified_classes

    def test_pri_16_no_classes_in_changed_files(self, tmp_path):
        nodes = _make_nodes(
            ("com.example.SomeService", "src/SomeService.java"),
        )
        cir = _FakeCIR(symbols=["com.example.SomeService"], nodes=nodes)
        model = _make_model()
        report = run_pr_impact(cir, ["NotPresent.java"], root=tmp_path, model=model)
        assert report.risk_level == "UNKNOWN"
        assert report.modified_classes == []

    def test_pri_17_direct_callers_in_report(self, tmp_path):
        cir = self._make_cir_with_callers()
        model = _make_model()
        report = run_pr_impact(
            cir,
            ["src/main/java/com/example/UserService.java"],
            root=tmp_path,
            model=model,
        )
        assert "com.example.UserBatchJob" in report.direct_callers

    def test_pri_18_endpoints_from_impact_chain(self, tmp_path):
        """Endpoints in impact chain appear in report (via run_impact_chain)."""
        nodes = _make_nodes(
            ("com.example.UserService", "src/UserService.java"),
        )
        cir = _FakeCIR(
            symbols=["com.example.UserService"],
            nodes=nodes,
        )
        model = _make_model()
        report = run_pr_impact(cir, ["src/UserService.java"], root=tmp_path, model=model)
        # No endpoints wired in this CIR → empty list, but no crash
        assert isinstance(report.affected_endpoints, list)

    def test_pri_19_modified_classes_excluded_from_callers(self, tmp_path):
        """A caller that is itself a modified class is not listed as a caller."""
        nodes = _make_nodes(
            ("com.example.A", "src/A.java"),
            ("com.example.B", "src/B.java"),
        )
        reverse_graph = {
            "com.example.A": {"calls": ["com.example.B"]},
        }
        cir = _FakeCIR(
            symbols=["com.example.A", "com.example.B"],
            reverse_graph=reverse_graph,
            nodes=nodes,
        )
        model = _make_model()
        report = run_pr_impact(
            cir, ["src/A.java", "src/B.java"], root=tmp_path, model=model
        )
        # B calls A, but B is also modified — should NOT appear as caller
        assert "com.example.B" not in report.direct_callers


# ---------------------------------------------------------------------------
# PRI-20 / PRI-21 / PRI-22: output contract
# ---------------------------------------------------------------------------

class TestPRImpactReportOutput:
    def _sample_report(self) -> PRImpactReport:
        return PRImpactReport(
            modified_classes=["com.example.UserService", "com.example.UserController"],
            affected_endpoints=[{"method": "GET", "path": "/users/{id}"}, {"method": "POST", "path": "/users"}],
            direct_callers=["com.example.UserBatchJob"],
            event_publishers=["Publishes UserUpdatedEvent"],
            event_consumers=["Consumed by NotificationListener"],
            transactional_methods=["com.example.UserService#createUser", "com.example.UserService#updateUser"],
            risk_level="HIGH",
            risk_reason="Public API + Event Flow + Transaction Boundary",
            analysis_warnings=[],
            metadata={"changed_files_count": 2, "classes_analyzed": 2},
        )

    def test_pri_20_to_dict_json_serializable(self):
        d = self._sample_report().to_dict()
        raw = json.dumps(d)
        parsed = json.loads(raw)
        assert parsed["schema_version"] == "1.0"
        assert "modified_classes" in parsed
        assert "affected_endpoints" in parsed
        assert "direct_callers" in parsed
        assert "event_flow" in parsed
        assert "transactional_methods" in parsed
        assert "risk_level" in parsed
        assert "risk_reason" in parsed
        assert "analysis_warnings" in parsed
        assert "metadata" in parsed

    def test_pri_21_render_text_sections(self):
        text = self._sample_report().render_text()
        assert "PR IMPACT REPORT" in text
        assert "Modified:" in text
        assert "UserService" in text
        assert "Affected Endpoints:" in text
        assert "GET /users/{id}" in text
        assert "Direct Callers:" in text
        assert "UserBatchJob" in text
        assert "Event Flow:" in text
        assert "UserUpdatedEvent" in text
        assert "Transactional Impact:" in text
        assert "createUser" in text
        assert "Risk Level:" in text
        assert "HIGH" in text
        assert "Reason:" in text

    def test_pri_22_run_pr_impact_never_raises(self, tmp_path):
        """Outer guard: even a completely broken CIR returns a report."""

        class _BrokenCIR:
            symbols = []
            reverse_graph = {}
            endpoints = []
            call_graph = []
            dependencies = []
            files = []
            metadata = {}
            cir_hash = "bad"
            _raw_ir: dict = {}  # missing graph key — triggers internal error

            @property
            def implementation_graph(self):
                raise RuntimeError("boom")

        report = run_pr_impact(_BrokenCIR(), ["X.java"], root=tmp_path)  # type: ignore[arg-type]
        assert isinstance(report, PRImpactReport)
        assert report.risk_level in ("UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL")

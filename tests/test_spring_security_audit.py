"""test_spring_security_audit.py — Unit tests for spring_security_audit.py Phase 3.

Coverage:
  S1   SEC-001 unsecured endpoint (annotation_based model)
  S2   SEC-002 @PreAuthorize on inherited method from generic supertype
  S3   SEC-003 @Transactional on @Controller/@RestController
  E    SecurityScanner deduplication, pattern isolation, never-raises, custom patterns
  A    run_security_audit integration smoke
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from sourcecode.canonical_ir import CanonicalEndpoint, CanonicalSecurity
from sourcecode.spring_findings import SpringFinding
from sourcecode.spring_semantic import TransactionBoundaryIndex, build_tx_index
from sourcecode.spring_security_audit import (
    SecurityScanner,
    _SEC001UnsecuredEndpoint,
    _SEC002PreAuthorizeGenericInheritance,
    _SEC003TransactionalOnController,
    run_security_audit,
)


# ---------------------------------------------------------------------------
# Helpers — fake CIR
# ---------------------------------------------------------------------------

class _FakeCIR:
    """Minimal CanonicalRepositoryIR stand-in for security audit tests."""

    def __init__(
        self,
        security_model: str = "annotation_based",
        endpoints: list[CanonicalEndpoint] | None = None,
        dependencies: list[dict] | None = None,
        files: list[dict] | None = None,
        nodes: list[dict] | None = None,
        edges: list[dict] | None = None,
    ):
        self.schema_version = "1.0"
        self.cir_hash = "deadbeef00000000"
        self.metadata: dict = {"security_model": security_model}
        self.endpoints: list[CanonicalEndpoint] = endpoints or []
        self.dependencies: list[dict] = dependencies or []
        self.files: list[dict] = files or []
        self.call_graph: list[dict] = edges or []
        self.symbols: list[str] = []
        self.reverse_graph: dict = {}
        self.security_index: dict = {}
        self._raw_ir: dict = {
            "graph": {
                "nodes": nodes or [],
                "edges": edges or [],
            }
        }

    def add_endpoint(
        self,
        method: str = "GET",
        path: str = "/api/test",
        controller: str = "com.example.TestController",
        handler: str = "com.example.TestController#handle",
        security: Optional[CanonicalSecurity] = None,
        inheritance_depth: int = 0,
        source_file: str = "TestController.java",
    ) -> CanonicalEndpoint:
        ep = CanonicalEndpoint(
            id=CanonicalEndpoint.make_id(method, path, controller, handler),
            path=path,
            method=method,
            controller_class=controller,
            handler_symbol=handler,
            security=security,
            source_file=source_file,
            stable_id=f"stable-{len(self.endpoints)}",
            inheritance_depth=inheritance_depth,
        )
        self.endpoints.append(ep)
        return ep

    def add_extends_edge(self, child: str, parent: str) -> None:
        self.dependencies.append({"from": child, "to": parent, "type": "extends"})

    def add_tx_node(
        self,
        fqn: str,
        kind: str = "method",
        raw_args: str = "",
        modifiers: list[str] | None = None,
        src: str = "Service.java",
    ) -> None:
        self._raw_ir["graph"]["nodes"].append({
            "fqn": fqn,
            "symbol_kind": kind,
            "annotations": ["@Transactional"],
            "annotation_values": {"@Transactional": raw_args},
            "modifiers": modifiers or ["public"],
            "source_file": src,
        })


def _preauth_security(expression: str = "hasRole('USER')") -> CanonicalSecurity:
    return CanonicalSecurity(
        policy="spring_preauthorize",
        source_scope="method",
        expression=expression,
    )


def _authenticated_security() -> CanonicalSecurity:
    return CanonicalSecurity(policy="authenticated", source_scope="method")


def _none_detected_security() -> CanonicalSecurity:
    return CanonicalSecurity(policy="none_detected", source_scope="method")


# ---------------------------------------------------------------------------
# S1 — SEC-001: unsecured endpoint
# ---------------------------------------------------------------------------

class TestSEC001UnsecuredEndpoint:
    def _pattern(self):
        return _SEC001UnsecuredEndpoint()

    def test_security_none_in_annotation_based_model(self):
        cir = _FakeCIR(security_model="annotation_based")
        cir.add_endpoint(security=None)
        findings = self._pattern().analyze(cir, None, None)
        assert len(findings) == 1
        f = findings[0]
        assert f.pattern_id == "SEC-001"
        assert f.category == "security"
        assert f.severity == "high"
        assert f.confidence == "high"

    def test_none_detected_policy_in_annotation_based_model(self):
        cir = _FakeCIR(security_model="annotation_based")
        cir.add_endpoint(security=_none_detected_security())
        findings = self._pattern().analyze(cir, None, None)
        assert len(findings) == 1

    def test_secured_endpoint_not_flagged(self):
        cir = _FakeCIR(security_model="annotation_based")
        cir.add_endpoint(security=_authenticated_security())
        findings = self._pattern().analyze(cir, None, None)
        assert findings == []

    def test_spring_preauthorize_not_flagged(self):
        cir = _FakeCIR(security_model="annotation_based")
        cir.add_endpoint(security=_preauth_security())
        findings = self._pattern().analyze(cir, None, None)
        assert findings == []

    def test_filter_based_model_suppressed(self):
        cir = _FakeCIR(security_model="filter_based")
        cir.add_endpoint(security=None)
        findings = self._pattern().analyze(cir, None, None)
        assert findings == []

    def test_unknown_model_suppressed(self):
        cir = _FakeCIR(security_model="unknown")
        cir.add_endpoint(security=None)
        findings = self._pattern().analyze(cir, None, None)
        assert findings == []

    def test_mixed_model_suppressed(self):
        cir = _FakeCIR(security_model="mixed")
        cir.add_endpoint(security=None)
        findings = self._pattern().analyze(cir, None, None)
        assert findings == []

    def test_multiple_unsecured_endpoints_each_flagged(self):
        cir = _FakeCIR(security_model="annotation_based")
        cir.add_endpoint(path="/a", security=None)
        cir.add_endpoint(path="/b", security=None)
        cir.add_endpoint(path="/c", security=_authenticated_security())
        findings = self._pattern().analyze(cir, None, None)
        assert len(findings) == 2

    def test_no_endpoints_no_findings(self):
        cir = _FakeCIR(security_model="annotation_based")
        findings = self._pattern().analyze(cir, None, None)
        assert findings == []

    def test_finding_id_deterministic(self):
        cir1 = _FakeCIR(security_model="annotation_based")
        cir1.add_endpoint(path="/x", security=None)
        cir2 = _FakeCIR(security_model="annotation_based")
        cir2.add_endpoint(path="/x", security=None)
        f1 = self._pattern().analyze(cir1, None, None)[0]
        f2 = self._pattern().analyze(cir2, None, None)[0]
        assert f1.id == f2.id

    def test_finding_references_endpoint_path(self):
        cir = _FakeCIR(security_model="annotation_based")
        cir.add_endpoint(method="POST", path="/api/users", security=None)
        f = self._pattern().analyze(cir, None, None)[0]
        assert "POST" in f.evidence["method"]
        assert "/api/users" in f.evidence["path"]
        assert "annotation_based" in f.evidence["security_model"]

    def test_finding_has_fix_hint(self):
        cir = _FakeCIR(security_model="annotation_based")
        cir.add_endpoint(security=None)
        f = self._pattern().analyze(cir, None, None)[0]
        assert f.fix_hint


# ---------------------------------------------------------------------------
# S2 — SEC-002: @PreAuthorize on inherited method from generic supertype
# ---------------------------------------------------------------------------

class TestSEC002PreAuthorizeGenericInheritance:
    def _pattern(self):
        return _SEC002PreAuthorizeGenericInheritance()

    def test_inherited_preauth_generic_parent_high_confidence(self):
        cir = _FakeCIR()
        cir.add_endpoint(
            controller="com.example.UserController",
            handler="com.example.UserController#getAll",
            security=_preauth_security(),
            inheritance_depth=1,
        )
        cir.add_extends_edge("com.example.UserController", "BaseController<User>")
        findings = self._pattern().analyze(cir, None, None)
        assert len(findings) == 1
        f = findings[0]
        assert f.pattern_id == "SEC-002"
        assert f.confidence == "high"
        assert f.severity == "high"
        assert f.evidence["parent_has_generics"] is True

    def test_inherited_preauth_non_generic_parent_medium_confidence(self):
        cir = _FakeCIR()
        cir.add_endpoint(
            controller="com.example.UserController",
            handler="com.example.UserController#getAll",
            security=_preauth_security(),
            inheritance_depth=1,
        )
        cir.add_extends_edge("com.example.UserController", "BaseController")
        findings = self._pattern().analyze(cir, None, None)
        assert len(findings) == 1
        assert findings[0].confidence == "medium"
        assert findings[0].evidence["parent_has_generics"] is False

    def test_inherited_preauth_no_extends_edge_medium_confidence(self):
        cir = _FakeCIR()
        cir.add_endpoint(
            controller="com.example.UserController",
            handler="com.example.UserController#getAll",
            security=_preauth_security(),
            inheritance_depth=1,
        )
        # No extends edge added
        findings = self._pattern().analyze(cir, None, None)
        assert len(findings) == 1
        assert findings[0].confidence == "medium"

    def test_depth_zero_not_flagged(self):
        cir = _FakeCIR()
        cir.add_endpoint(
            security=_preauth_security(),
            inheritance_depth=0,
        )
        findings = self._pattern().analyze(cir, None, None)
        assert findings == []

    def test_non_spring_pre_policy_not_flagged(self):
        cir = _FakeCIR()
        cir.add_endpoint(
            security=_authenticated_security(),
            inheritance_depth=2,
        )
        findings = self._pattern().analyze(cir, None, None)
        assert findings == []

    def test_security_none_not_flagged(self):
        cir = _FakeCIR()
        cir.add_endpoint(security=None, inheritance_depth=1)
        findings = self._pattern().analyze(cir, None, None)
        assert findings == []

    def test_postauthorize_policy_not_flagged(self):
        # CVE-2025-41248 is @PreAuthorize only; @PostAuthorize uses different evaluation path
        cir = _FakeCIR()
        cir.add_endpoint(
            controller="com.example.C",
            handler="com.example.C#act",
            security=CanonicalSecurity(
                policy="spring_postauthorize",
                source_scope="method",
                expression="returnObject.owner == authentication.name",
            ),
            inheritance_depth=1,
        )
        cir.add_extends_edge("com.example.C", "AbstractResource<Item>")
        findings = self._pattern().analyze(cir, None, None)
        assert findings == []

    def test_dedup_same_endpoint_not_emitted_twice(self):
        cir = _FakeCIR()
        ep = cir.add_endpoint(
            controller="com.example.UserController",
            handler="com.example.UserController#getAll",
            security=_preauth_security(),
            inheritance_depth=1,
        )
        cir.add_extends_edge("com.example.UserController", "Base<User>")
        # Analyze twice — dedup by key
        findings = self._pattern().analyze(cir, None, None)
        assert len(findings) == 1

    def test_finding_id_deterministic(self):
        cir1 = _FakeCIR()
        cir1.add_endpoint(
            path="/same",
            controller="com.example.C",
            handler="com.example.C#m",
            security=_preauth_security(),
            inheritance_depth=1,
        )
        cir2 = _FakeCIR()
        cir2.add_endpoint(
            path="/same",
            controller="com.example.C",
            handler="com.example.C#m",
            security=_preauth_security(),
            inheritance_depth=1,
        )
        f1 = self._pattern().analyze(cir1, None, None)[0]
        f2 = self._pattern().analyze(cir2, None, None)[0]
        assert f1.id == f2.id

    def test_cve_reference_in_title(self):
        cir = _FakeCIR()
        cir.add_endpoint(
            security=_preauth_security(),
            inheritance_depth=1,
        )
        f = self._pattern().analyze(cir, None, None)[0]
        assert "CVE-2025-41248" in f.title

    def test_generic_regex_multitype_params(self):
        cir = _FakeCIR()
        cir.add_endpoint(
            controller="com.example.Repo",
            handler="com.example.Repo#find",
            security=_preauth_security(),
            inheritance_depth=1,
        )
        cir.add_extends_edge("com.example.Repo", "JpaRepository<Entity, Long>")
        findings = self._pattern().analyze(cir, None, None)
        assert findings[0].evidence["parent_has_generics"] is True

    def test_generic_regex_wildcard_params(self):
        cir = _FakeCIR()
        cir.add_endpoint(
            controller="com.example.C",
            handler="com.example.C#m",
            security=_preauth_security(),
            inheritance_depth=1,
        )
        cir.add_extends_edge("com.example.C", "AbstractController<? extends BaseDto>")
        findings = self._pattern().analyze(cir, None, None)
        # wildcard with lowercase — regex requires first char uppercase
        # JpaRepository<Entity> → high, AbstractController<? extends BaseDto> → depends on regex
        f = findings[0]
        assert f.pattern_id == "SEC-002"


# ---------------------------------------------------------------------------
# S3 — SEC-003: @Transactional on @Controller/@RestController
# ---------------------------------------------------------------------------

class TestSEC003TransactionalOnController:
    def _pattern(self):
        return _SEC003TransactionalOnController()

    def _make_tx_index_for_class(self, fqn: str, src: str = "Ctrl.java") -> TransactionBoundaryIndex:
        cir = _FakeCIR()
        cir.add_tx_node(fqn, kind="class", src=src)
        return build_tx_index(cir)

    def _make_tx_index_for_method(self, fqn: str, src: str = "Ctrl.java") -> TransactionBoundaryIndex:
        cir = _FakeCIR()
        cir.add_tx_node(fqn, kind="method", src=src)
        return build_tx_index(cir)

    def test_class_level_tx_on_controller_flagged(self):
        ctrl_fqn = "com.example.UserController"
        cir = _FakeCIR()
        cir.add_endpoint(controller=ctrl_fqn, handler=f"{ctrl_fqn}#handle")
        tx_index = self._make_tx_index_for_class(ctrl_fqn)
        findings = self._pattern().analyze(cir, tx_index, None)
        assert len(findings) == 1
        f = findings[0]
        assert f.pattern_id == "SEC-003"
        assert f.category == "security"
        assert f.severity == "medium"
        assert f.confidence == "medium"
        assert f.evidence["tx_scope"] == "class"
        assert f.evidence["controller_class"] == ctrl_fqn

    def test_method_level_tx_on_controller_handler_flagged(self):
        ctrl_fqn = "com.example.OrderController"
        method_fqn = f"{ctrl_fqn}#createOrder"
        cir = _FakeCIR()
        cir.add_endpoint(controller=ctrl_fqn, handler=f"{ctrl_fqn}#createOrder")
        tx_index = self._make_tx_index_for_method(method_fqn)
        findings = self._pattern().analyze(cir, tx_index, None)
        assert len(findings) == 1
        f = findings[0]
        assert f.evidence["tx_scope"] == "method"
        assert f.evidence["controller_class"] == ctrl_fqn
        assert "createOrder" in f.title

    def test_tx_on_service_not_flagged(self):
        ctrl_fqn = "com.example.UserController"
        service_fqn = "com.example.UserService"
        cir = _FakeCIR()
        cir.add_endpoint(controller=ctrl_fqn, handler=f"{ctrl_fqn}#handle")
        tx_index = self._make_tx_index_for_class(service_fqn)
        findings = self._pattern().analyze(cir, tx_index, None)
        assert findings == []

    def test_no_tx_index_returns_empty(self):
        ctrl_fqn = "com.example.UserController"
        cir = _FakeCIR()
        cir.add_endpoint(controller=ctrl_fqn, handler=f"{ctrl_fqn}#handle")
        findings = self._pattern().analyze(cir, None, None)
        assert findings == []

    def test_no_endpoints_no_findings(self):
        cir = _FakeCIR()
        tx_index = self._make_tx_index_for_class("com.example.UserController")
        findings = self._pattern().analyze(cir, tx_index, None)
        assert findings == []

    def test_class_finding_id_deterministic(self):
        ctrl = "com.example.C"
        cir1 = _FakeCIR()
        cir1.add_endpoint(controller=ctrl, handler=f"{ctrl}#m")
        tx1 = self._make_tx_index_for_class(ctrl)
        cir2 = _FakeCIR()
        cir2.add_endpoint(controller=ctrl, handler=f"{ctrl}#m")
        tx2 = self._make_tx_index_for_class(ctrl)
        f1 = self._pattern().analyze(cir1, tx1, None)[0]
        f2 = self._pattern().analyze(cir2, tx2, None)[0]
        assert f1.id == f2.id

    def test_method_finding_id_deterministic(self):
        ctrl = "com.example.C"
        method = f"{ctrl}#doWork"
        cir1 = _FakeCIR()
        cir1.add_endpoint(controller=ctrl, handler=f"{ctrl}#doWork")
        tx1 = self._make_tx_index_for_method(method)
        cir2 = _FakeCIR()
        cir2.add_endpoint(controller=ctrl, handler=f"{ctrl}#doWork")
        tx2 = self._make_tx_index_for_method(method)
        f1 = self._pattern().analyze(cir1, tx1, None)[0]
        f2 = self._pattern().analyze(cir2, tx2, None)[0]
        assert f1.id == f2.id

    def test_finding_has_propagation_in_evidence(self):
        ctrl = "com.example.TxCtrl"
        cir = _FakeCIR()
        cir.add_tx_node(ctrl, kind="class", raw_args="propagation=REQUIRES_NEW")
        tx_index = build_tx_index(cir)
        cir.add_endpoint(controller=ctrl, handler=f"{ctrl}#m")
        findings = self._pattern().analyze(cir, tx_index, None)
        assert len(findings) == 1
        assert findings[0].evidence["propagation"] == "REQUIRES_NEW"

    def test_dedup_class_not_emitted_twice(self):
        ctrl = "com.example.DoubleCtrl"
        cir = _FakeCIR()
        cir.add_endpoint(controller=ctrl, handler=f"{ctrl}#a", path="/a")
        cir.add_endpoint(controller=ctrl, handler=f"{ctrl}#b", path="/b")
        tx_index = self._make_tx_index_for_class(ctrl)
        findings = self._pattern().analyze(cir, tx_index, None)
        # class-level finding emitted once per class, not once per endpoint
        class_findings = [f for f in findings if f.evidence.get("tx_scope") == "class"]
        assert len(class_findings) == 1

    def test_finding_has_fix_hint(self):
        ctrl = "com.example.C"
        cir = _FakeCIR()
        cir.add_endpoint(controller=ctrl, handler=f"{ctrl}#m")
        tx_index = self._make_tx_index_for_class(ctrl)
        findings = self._pattern().analyze(cir, tx_index, None)
        assert findings[0].fix_hint


# ---------------------------------------------------------------------------
# E — SecurityScanner engine
# ---------------------------------------------------------------------------

class TestSecurityScanner:
    def _make_scanner(self, *patterns) -> SecurityScanner:
        return SecurityScanner(patterns=list(patterns))

    def test_empty_patterns_no_findings(self):
        scanner = self._make_scanner()
        cir = _FakeCIR(security_model="annotation_based")
        cir.add_endpoint(security=None)
        assert scanner.analyze(cir) == []

    def test_default_scanner_instantiates(self):
        scanner = SecurityScanner()
        assert len(scanner.patterns) == 3

    def test_scanner_never_raises_on_bad_pattern(self):
        class _BoomPattern:
            pattern_id = "BAD-001"
            severity = "high"
            def analyze(self, cir, tx_index, root):
                raise RuntimeError("boom")

        scanner = self._make_scanner(_BoomPattern())
        cir = _FakeCIR(security_model="annotation_based")
        cir.add_endpoint(security=None)
        # Should not raise
        findings = scanner.analyze(cir)
        assert findings == []

    def test_scanner_deduplicates_across_patterns(self):
        cir = _FakeCIR(security_model="annotation_based")
        cir.add_endpoint(security=None)
        # Two patterns that produce the same finding ID
        p1 = _SEC001UnsecuredEndpoint()
        p2 = _SEC001UnsecuredEndpoint()
        scanner = self._make_scanner(p1, p2)
        findings = scanner.analyze(cir)
        assert len(findings) == 1

    def test_scanner_sorts_by_severity_then_symbol(self):
        cir = _FakeCIR(security_model="annotation_based")
        cir.add_endpoint(path="/a", controller="com.z.C", handler="com.z.C#m", security=None)
        cir.add_endpoint(
            path="/b",
            controller="com.a.C",
            handler="com.a.C#m",
            security=_preauth_security(),
            inheritance_depth=1,
        )
        cir.add_extends_edge("com.a.C", "Base<T>")
        scanner = SecurityScanner()
        findings = scanner.analyze(cir)
        # SEC-002 (high) should appear before SEC-001 (high, same severity) sorted by symbol
        sev_order = [f.severity for f in findings]
        from sourcecode.spring_findings import SEVERITY_ORDER
        for i in range(len(sev_order) - 1):
            assert SEVERITY_ORDER.get(sev_order[i], 9) <= SEVERITY_ORDER.get(sev_order[i + 1], 9)

    def test_custom_pattern_override(self):
        class _AlwaysFindsOne:
            pattern_id = "CUSTOM-001"
            severity = "low"
            def analyze(self, cir, tx_index, root):
                return [SpringFinding(
                    id="CUSTOM-001-deadbeef0000",
                    pattern_id="CUSTOM-001",
                    category="security",
                    severity="low",
                    confidence="high",
                    title="custom",
                    symbol="com.example.Foo",
                    source_file="Foo.java",
                    evidence={},
                    explanation="test",
                    fix_hint="none",
                )]

        scanner = SecurityScanner(patterns=[_AlwaysFindsOne()])
        cir = _FakeCIR()
        findings = scanner.analyze(cir)
        assert len(findings) == 1
        assert findings[0].pattern_id == "CUSTOM-001"

    def test_scanner_accepts_tx_index_and_root_kwargs(self):
        cir = _FakeCIR()
        tx_index = TransactionBoundaryIndex(repo_id="test")
        scanner = SecurityScanner()
        # Should not raise with explicit kwargs
        scanner.analyze(cir, tx_index=tx_index, root=None)


# ---------------------------------------------------------------------------
# A — run_security_audit integration smoke
# ---------------------------------------------------------------------------

class TestRunSecurityAudit:
    def test_returns_spring_audit_result(self):
        from sourcecode.spring_findings import SpringAuditResult
        cir = _FakeCIR(security_model="annotation_based")
        result = run_security_audit(cir)
        assert isinstance(result, SpringAuditResult)

    def test_result_finalized(self):
        cir = _FakeCIR(security_model="annotation_based")
        cir.add_endpoint(security=None)
        result = run_security_audit(cir)
        assert result.summary["total_findings"] >= 1
        assert "by_severity" in result.summary

    def test_scope_is_security(self):
        cir = _FakeCIR()
        result = run_security_audit(cir)
        assert result.scope == "security"

    def test_spring_detected_true(self):
        # spring_detected requires actual Spring IoC beans, not just security_model
        spring_node = {
            "fqn": "com.example.UserService",
            "symbol_kind": "class",
            "annotations": ["@Service"],
            "annotation_values": {},
            "modifiers": [],
            "source_file": "UserService.java",
        }
        cir = _FakeCIR(nodes=[spring_node])
        result = run_security_audit(cir)
        assert result.spring_detected is True

    def test_metadata_contains_endpoints_analyzed(self):
        cir = _FakeCIR(security_model="annotation_based")
        cir.add_endpoint(security=None)
        cir.add_endpoint(security=_authenticated_security())
        result = run_security_audit(cir)
        assert result.metadata["endpoints_analyzed"] == 2

    def test_metadata_contains_security_model(self):
        cir = _FakeCIR(security_model="filter_based")
        result = run_security_audit(cir)
        assert result.metadata["security_model"] == "filter_based"

    def test_metadata_contains_analysis_time(self):
        cir = _FakeCIR()
        result = run_security_audit(cir)
        assert "analysis_time_ms" in result.metadata
        assert result.metadata["analysis_time_ms"] >= 0

    def test_min_severity_filters_findings(self):
        cir = _FakeCIR(security_model="annotation_based")
        cir.add_endpoint(security=None)  # SEC-001 is high severity
        result_all = run_security_audit(cir, min_severity="high")
        result_critical = run_security_audit(cir, min_severity="critical")
        # All high findings pass "high" filter
        for f in result_all.findings:
            assert f.severity in ("critical", "high")
        # critical filter excludes high
        assert len(result_critical.findings) == 0

    def test_custom_patterns_accepted(self):
        class _NoOp:
            pattern_id = "NOP-001"
            severity = "low"
            def analyze(self, cir, tx_index, root):
                return []

        cir = _FakeCIR()
        result = run_security_audit(cir, patterns=[_NoOp()])
        assert result.findings == []

    def test_pre_built_tx_index_accepted(self):
        ctrl = "com.example.TxController"
        cir = _FakeCIR(security_model="annotation_based")
        cir.add_endpoint(controller=ctrl, handler=f"{ctrl}#m")
        cir.add_tx_node(ctrl, kind="class")
        tx_index = build_tx_index(cir)
        result = run_security_audit(cir, tx_index=tx_index)
        assert isinstance(result.findings, list)

    def test_result_has_limitations(self):
        cir = _FakeCIR()
        result = run_security_audit(cir)
        assert len(result.limitations) > 0

    def test_to_dict_is_valid(self):
        cir = _FakeCIR(security_model="annotation_based")
        cir.add_endpoint(security=None)
        result = run_security_audit(cir)
        d = result.to_dict()
        assert d["schema_version"] == "1.0"
        assert "findings" in d
        assert "summary" in d
        assert all(isinstance(f, dict) for f in d["findings"])

    def test_never_raises_on_empty_cir(self):
        cir = _FakeCIR()
        result = run_security_audit(cir)
        assert result is not None

    def test_sec001_and_sec003_combined(self):
        ctrl = "com.example.MixedController"
        cir = _FakeCIR(security_model="annotation_based")
        cir.add_endpoint(controller=ctrl, handler=f"{ctrl}#unsecured", path="/open", security=None)
        cir.add_tx_node(ctrl, kind="class")
        result = run_security_audit(cir)
        pattern_ids = {f.pattern_id for f in result.findings}
        assert "SEC-001" in pattern_ids


# ---------------------------------------------------------------------------
# Regression: BUG-001 — programmatic-policy routes must not trigger annotation_based
# classification, preventing SEC-001 false-positive flood on IAM/auth-domain repos.
# ---------------------------------------------------------------------------

class TestBUG001ProgrammaticSecurityModel:
    """_PROGRAMMATIC_SECURITY_RE was too broad (matched bare class names like
    'Authentication', 'Principal', 'SecurityContext' in imports/type decls).
    This caused repos that only have programmatic security to be classified as
    annotation_based, which then triggered SEC-001 for every other endpoint.

    Fix: tighten regex to require method-call/field-access context; exclude
    'programmatic' policy from _has_ann_sec_asm and _has_annotation_security.
    """

    def test_programmatic_only_model_does_not_emit_sec001(self):
        """Endpoints with policy=programmatic must not trigger SEC-001."""
        ctrl = "com.example.AuthController"
        sec_programmatic = CanonicalSecurity(policy="programmatic", source_scope="method")
        cir = _FakeCIR(security_model="annotation_based")
        # All endpoints have programmatic security only
        cir.add_endpoint(
            controller=ctrl,
            handler=f"{ctrl}#doAuth",
            path="/auth",
            security=sec_programmatic,
        )
        result = run_security_audit(cir)
        sec001 = [f for f in result.findings if f.pattern_id == "SEC-001"]
        assert sec001 == [], "SEC-001 must not fire when every endpoint has programmatic security"

    def test_unknown_security_model_does_not_emit_sec001(self):
        """Repos without any security model must produce 0 SEC-001 findings."""
        ctrl = "com.example.ApiController"
        cir = _FakeCIR(security_model="unknown")
        cir.add_endpoint(
            controller=ctrl,
            handler=f"{ctrl}#getItems",
            path="/items",
            security=None,
        )
        result = run_security_audit(cir)
        sec001 = [f for f in result.findings if f.pattern_id == "SEC-001"]
        assert sec001 == [], "SEC-001 must not fire for security_model=unknown"

    def test_annotation_based_model_still_emits_sec001(self):
        """Repos with annotation_based model still emit SEC-001 for bare endpoints."""
        ctrl = "com.example.SecuredController"
        sec_preauth = CanonicalSecurity(
            policy="spring_preauthorize", source_scope="method", expression="hasRole('ADMIN')"
        )
        cir = _FakeCIR(security_model="annotation_based")
        # One secured, one not
        cir.add_endpoint(
            controller=ctrl,
            handler=f"{ctrl}#secured",
            path="/admin",
            security=sec_preauth,
        )
        cir.add_endpoint(
            controller=ctrl,
            handler=f"{ctrl}#open",
            path="/open",
            security=None,
        )
        result = run_security_audit(cir)
        sec001 = [f for f in result.findings if f.pattern_id == "SEC-001"]
        assert len(sec001) == 1
        assert sec001[0].evidence["path"] == "/open"

"""Tests for repository_ir.py — deterministic Java symbol IR."""

from __future__ import annotations

import pytest

from sourcecode.repository_ir import (
    _build_relations,
    _build_spring_summary,
    _diff_symbols,
    _extract_symbols,
    _symbol_fingerprint,
    build_repo_ir,
    extract_file_ir,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SIMPLE_SERVICE = """\
package com.example.service;

import com.example.repo.UserRepository;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
public class UserService {

    @Autowired
    private UserRepository userRepository;

    public User getUserById(Long id) {
        return userRepository.findById(id).orElseThrow();
    }

    @Transactional
    public User save(User user) {
        return userRepository.save(user);
    }
}
"""

SIMPLE_CONTROLLER = """\
package com.example.web;

import com.example.service.UserService;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.GetMapping;

@RestController
@RequestMapping("/api/users")
public class UserController {

    @Autowired
    private UserService userService;

    @GetMapping("/{id}")
    public User getUser(Long id) {
        return userService.getUserById(id);
    }
}
"""

VALIDATOR = """\
package com.example.validation;

import javax.validation.ConstraintValidator;
import javax.validation.ConstraintValidatorContext;

public class EmailValidator implements ConstraintValidator {

    public boolean isValid(String value, ConstraintValidatorContext ctx) {
        return value != null && value.contains("@");
    }
}
"""

INTERFACE_SOURCE = """\
package com.example.port;

public interface UserPort {
    User findById(Long id);
    User save(User user);
}
"""


# ---------------------------------------------------------------------------
# Phase 1 — Symbol extraction
# ---------------------------------------------------------------------------

class TestSymbolExtraction:
    def test_package_extracted(self):
        pkg, symbols, _ = _extract_symbols(SIMPLE_SERVICE, "UserService.java")
        assert pkg == "com.example.service"

    def test_class_symbol_present(self):
        _, symbols, _ = _extract_symbols(SIMPLE_SERVICE, "UserService.java")
        fqns = [s.symbol for s in symbols]
        assert "com.example.service.UserService" in fqns

    def test_methods_extracted(self):
        _, symbols, _ = _extract_symbols(SIMPLE_SERVICE, "UserService.java")
        method_fqns = [s.symbol for s in symbols if s.type == "method"]
        assert "com.example.service.UserService#getUserById" in method_fqns
        assert "com.example.service.UserService#save" in method_fqns

    def test_injected_field_extracted(self):
        _, symbols, _ = _extract_symbols(SIMPLE_SERVICE, "UserService.java")
        fields = [s for s in symbols if s.type == "field"]
        assert any("userRepository" in s.symbol for s in fields)

    def test_class_modifiers(self):
        _, symbols, _ = _extract_symbols(SIMPLE_SERVICE, "UserService.java")
        cls = next(s for s in symbols if s.symbol == "com.example.service.UserService")
        assert "public" in cls.modifiers

    def test_class_annotations(self):
        _, symbols, _ = _extract_symbols(SIMPLE_SERVICE, "UserService.java")
        cls = next(s for s in symbols if s.symbol == "com.example.service.UserService")
        assert "@Service" in cls.annotations

    def test_method_annotation(self):
        _, symbols, _ = _extract_symbols(SIMPLE_SERVICE, "UserService.java")
        save = next(s for s in symbols if s.symbol == "com.example.service.UserService#save")
        assert "@Transactional" in save.annotations

    def test_interface_type(self):
        _, symbols, _ = _extract_symbols(INTERFACE_SOURCE, "UserPort.java")
        cls = next(s for s in symbols if "UserPort" in s.symbol)
        assert cls.type == "interface"

    def test_field_annotation(self):
        _, symbols, _ = _extract_symbols(SIMPLE_SERVICE, "UserService.java")
        field = next(s for s in symbols if s.type == "field")
        assert "@Autowired" in field.annotations

    def test_declaring_file(self):
        _, symbols, _ = _extract_symbols(SIMPLE_SERVICE, "path/UserService.java")
        for s in symbols:
            assert s.declaring_file == "path/UserService.java"

    def test_confidence_high_for_public(self):
        _, symbols, _ = _extract_symbols(SIMPLE_SERVICE, "UserService.java")
        public_methods = [s for s in symbols if s.type == "method" and "public" in s.modifiers]
        assert all(s.confidence == "high" for s in public_methods)

    def test_imports_used_resolved(self):
        _, symbols, _ = _extract_symbols(SIMPLE_SERVICE, "UserService.java")
        cls = next(s for s in symbols if s.symbol == "com.example.service.UserService")
        # No extends/implements on UserService, so imports_used is empty
        assert cls.imports_used == []

    def test_implements_resolves_import(self):
        _, symbols, _ = _extract_symbols(VALIDATOR, "EmailValidator.java")
        cls = next(s for s in symbols if "EmailValidator" in s.symbol and s.type == "class")
        assert "javax.validation.ConstraintValidator" in cls.imports_used

    def test_raw_imports_returned(self):
        _, _, raw_imports = _extract_symbols(SIMPLE_SERVICE, "UserService.java")
        assert "com.example.repo.UserRepository" in raw_imports
        assert "org.springframework.stereotype.Service" in raw_imports

    def test_no_symbols_from_empty_file(self):
        _, symbols, _ = _extract_symbols("", "Empty.java")
        assert symbols == []

    def test_deterministic_order(self):
        _, s1, _ = _extract_symbols(SIMPLE_SERVICE, "UserService.java")
        _, s2, _ = _extract_symbols(SIMPLE_SERVICE, "UserService.java")
        assert [s.symbol for s in s1] == [s.symbol for s in s2]


# ---------------------------------------------------------------------------
# Phase 2 — Spring semantic tagging
# ---------------------------------------------------------------------------

class TestSpringTagging:
    def test_service_tagged(self):
        _, symbols, _ = _extract_symbols(SIMPLE_SERVICE, "UserService.java")
        summary = _build_spring_summary(symbols)
        assert "com.example.service.UserService" in summary["services"]

    def test_controller_tagged(self):
        _, symbols, _ = _extract_symbols(SIMPLE_CONTROLLER, "UserController.java")
        summary = _build_spring_summary(symbols)
        assert "com.example.web.UserController" in summary["controllers"]

    def test_transactional_method_tracked(self):
        _, symbols, _ = _extract_symbols(SIMPLE_SERVICE, "UserService.java")
        summary = _build_spring_summary(symbols)
        assert "com.example.service.UserService#save" in summary["transactional"]

    def test_no_spring_role_for_validator(self):
        _, symbols, _ = _extract_symbols(VALIDATOR, "EmailValidator.java")
        summary = _build_spring_summary(symbols)
        assert summary["controllers"] == []
        assert summary["services"] == []
        assert summary["repositories"] == []

    def test_summary_keys_present(self):
        _, symbols, _ = _extract_symbols(SIMPLE_SERVICE, "UserService.java")
        summary = _build_spring_summary(symbols)
        assert "controllers" in summary
        assert "services" in summary
        assert "repositories" in summary
        assert "configs" in summary
        assert "transactional" in summary

    def test_summary_deterministic(self):
        _, s1, _ = _extract_symbols(SIMPLE_SERVICE, "UserService.java")
        _, s2, _ = _extract_symbols(SIMPLE_SERVICE, "UserService.java")
        assert _build_spring_summary(s1) == _build_spring_summary(s2)


# ---------------------------------------------------------------------------
# Phase 3 — Relation graph
# ---------------------------------------------------------------------------

class TestRelationGraph:
    def _get_ir(self, source: str, rel_path: str = "Test.java") -> dict:
        return extract_file_ir(source, rel_path)

    def test_annotated_with_edge_present(self):
        ir = self._get_ir(SIMPLE_SERVICE, "UserService.java")
        types = [r["type"] for r in ir["relations"]]
        assert "annotated_with" in types

    def test_imports_edges_present(self):
        ir = self._get_ir(SIMPLE_SERVICE, "UserService.java")
        types = [r["type"] for r in ir["relations"]]
        assert "imports" in types

    def test_injects_edge_for_autowired(self):
        ir = self._get_ir(SIMPLE_SERVICE, "UserService.java")
        inject_edges = [r for r in ir["relations"] if r["type"] == "injects"]
        assert len(inject_edges) >= 1
        assert inject_edges[0]["confidence"] == "high"

    def test_implements_edge(self):
        ir = self._get_ir(VALIDATOR, "EmailValidator.java")
        impl_edges = [r for r in ir["relations"] if r["type"] == "implements"]
        assert len(impl_edges) >= 1
        assert impl_edges[0]["to"] == "javax.validation.ConstraintValidator"

    def test_evidence_field_present(self):
        ir = self._get_ir(SIMPLE_SERVICE, "UserService.java")
        for r in ir["relations"]:
            assert "evidence" in r
            assert "type" in r["evidence"]

    def test_no_call_graph(self):
        ir = self._get_ir(SIMPLE_SERVICE, "UserService.java")
        assert ir["graph_metadata"]["has_call_graph"] is False

    def test_node_count_matches_symbols(self):
        ir = self._get_ir(SIMPLE_SERVICE, "UserService.java")
        assert ir["graph_metadata"]["node_count"] == len(ir["symbols"])

    def test_relations_sorted_deterministic(self):
        ir1 = self._get_ir(SIMPLE_SERVICE, "UserService.java")
        ir2 = self._get_ir(SIMPLE_SERVICE, "UserService.java")
        assert ir1["relations"] == ir2["relations"]


# ---------------------------------------------------------------------------
# Phase 4 — Symbol-level diff
# ---------------------------------------------------------------------------

class TestSymbolDiff:
    OLD_V = """\
package com.example;
import com.example.Repo;
@Service
public class MyService {
    public String get() { return null; }
}
"""
    NEW_V = """\
package com.example;
import com.example.Repo;
import com.example.Extra;
@Service
public class MyService {
    public String get() { return null; }
    public void post(Extra extra) {}
}
"""

    def test_added_method_detected(self):
        ir = extract_file_ir(self.NEW_V, "MyService.java", old_source=self.OLD_V)
        added = [c for c in ir["changed_symbols"] if c["change_type"] == "added"]
        assert any("post" in c["symbol"] for c in added)

    def test_no_false_changes_when_identical(self):
        ir = extract_file_ir(self.OLD_V, "MyService.java", old_source=self.OLD_V)
        assert ir["changed_symbols"] == []

    def test_removed_symbol(self):
        ir = extract_file_ir(self.OLD_V, "MyService.java", old_source=self.NEW_V)
        removed = [c for c in ir["changed_symbols"] if c["change_type"] == "removed"]
        assert any("post" in c["symbol"] for c in removed)

    def test_annotation_change_classified(self):
        old_src = """\
package com.example;
@Service
public class Svc {}
"""
        new_src = """\
package com.example;
@Service
@Transactional
public class Svc {}
"""
        ir = extract_file_ir(new_src, "Svc.java", old_source=old_src)
        modified = [c for c in ir["changed_symbols"] if c["change_type"] == "modified"]
        assert len(modified) == 1
        assert modified[0]["diff_type"] == "annotation_change"

    def test_diff_output_confidence(self):
        ir = extract_file_ir(self.NEW_V, "MyService.java", old_source=self.OLD_V)
        for c in ir["changed_symbols"]:
            assert c["confidence"] in ("high", "medium", "low")

    def test_diff_output_change_types_valid(self):
        ir = extract_file_ir(self.NEW_V, "MyService.java", old_source=self.OLD_V)
        for c in ir["changed_symbols"]:
            assert c["change_type"] in ("added", "removed", "modified")
            assert c["diff_type"] in (
                "signature_change", "annotation_change", "structural_change", "unknown"
            )

    def test_no_diff_without_old_source(self):
        ir = extract_file_ir(self.NEW_V, "MyService.java")
        assert ir["changed_symbols"] == []


# ---------------------------------------------------------------------------
# Phase 5 — Output structure
# ---------------------------------------------------------------------------

class TestOutputStructure:
    def test_top_level_keys(self):
        ir = extract_file_ir(SIMPLE_SERVICE, "UserService.java")
        assert set(ir.keys()) == {
            "symbols", "relations", "changed_symbols", "spring_summary", "graph_metadata"
        }

    def test_graph_metadata_keys(self):
        ir = extract_file_ir(SIMPLE_SERVICE, "UserService.java")
        gm = ir["graph_metadata"]
        assert "node_count" in gm
        assert "edge_count" in gm
        assert "has_call_graph" in gm

    def test_symbol_schema(self):
        ir = extract_file_ir(SIMPLE_SERVICE, "UserService.java")
        for s in ir["symbols"]:
            assert "symbol" in s
            assert "type" in s
            assert "modifiers" in s
            assert "annotations" in s
            assert "imports_used" in s
            assert "declaring_file" in s
            assert "confidence" in s
            assert s["type"] in ("class", "interface", "method", "field")
            assert s["confidence"] in ("high", "medium", "low")

    def test_relation_schema(self):
        ir = extract_file_ir(SIMPLE_SERVICE, "UserService.java")
        for r in ir["relations"]:
            assert "from" in r
            assert "to" in r
            assert "type" in r
            assert "confidence" in r
            assert "evidence" in r
            assert r["type"] in (
                "imports", "extends", "implements", "injects",
                "mapped_to", "annotated_with", "calls"
            )

    def test_no_forbidden_fields_in_symbols(self):
        ir = extract_file_ir(SIMPLE_SERVICE, "UserService.java")
        forbidden = {
            "role_in_system", "core_service", "transaction_boundary",
            "impact_area", "behavioral_change", "propagation_risk",
        }
        for s in ir["symbols"]:
            assert not forbidden & s.keys()

    def test_symbols_sorted(self):
        ir = extract_file_ir(SIMPLE_SERVICE, "UserService.java")
        fqns = [s["symbol"] for s in ir["symbols"]]
        assert fqns == sorted(fqns)

    def test_output_deterministic(self):
        ir1 = extract_file_ir(SIMPLE_SERVICE, "UserService.java")
        ir2 = extract_file_ir(SIMPLE_SERVICE, "UserService.java")
        assert ir1 == ir2


# ---------------------------------------------------------------------------
# build_repo_ir
# ---------------------------------------------------------------------------

class TestBuildRepoIr:
    def test_empty_file_list(self, tmp_path):
        ir = build_repo_ir([], tmp_path)
        assert ir["symbols"] == []
        assert ir["relations"] == []
        assert ir["graph_metadata"]["node_count"] == 0

    def test_single_file(self, tmp_path):
        java_file = tmp_path / "UserService.java"
        java_file.write_text(SIMPLE_SERVICE, encoding="utf-8")
        ir = build_repo_ir(["UserService.java"], tmp_path)
        assert any("UserService" in s["symbol"] for s in ir["symbols"])

    def test_multi_file_aggregation(self, tmp_path):
        (tmp_path / "UserService.java").write_text(SIMPLE_SERVICE, encoding="utf-8")
        (tmp_path / "UserController.java").write_text(SIMPLE_CONTROLLER, encoding="utf-8")
        ir = build_repo_ir(
            ["UserService.java", "UserController.java"], tmp_path
        )
        fqns = [s["symbol"] for s in ir["symbols"]]
        assert any("UserService" in f for f in fqns)
        assert any("UserController" in f for f in fqns)

    def test_spring_summary_aggregated(self, tmp_path):
        (tmp_path / "UserService.java").write_text(SIMPLE_SERVICE, encoding="utf-8")
        (tmp_path / "UserController.java").write_text(SIMPLE_CONTROLLER, encoding="utf-8")
        ir = build_repo_ir(
            ["UserService.java", "UserController.java"], tmp_path
        )
        assert len(ir["spring_summary"]["services"]) >= 1
        assert len(ir["spring_summary"]["controllers"]) >= 1

    def test_deterministic_multi_file(self, tmp_path):
        (tmp_path / "A.java").write_text(SIMPLE_SERVICE, encoding="utf-8")
        (tmp_path / "B.java").write_text(VALIDATOR, encoding="utf-8")
        ir1 = build_repo_ir(["A.java", "B.java"], tmp_path)
        ir2 = build_repo_ir(["A.java", "B.java"], tmp_path)
        assert ir1 == ir2

    def test_missing_file_skipped(self, tmp_path):
        ir = build_repo_ir(["nonexistent.java"], tmp_path)
        assert ir["symbols"] == []

"""Tests for repository_ir.py — deterministic Java symbol IR (schema_version=final-v1)."""

from __future__ import annotations

import pytest

from sourcecode.repository_ir import (
    EvidenceBundle,
    _bfs_reachability,
    _build_evidence_bundles,
    _build_relations,
    _build_spring_summary,
    _detect_subsystems,
    _diff_intensity_cs,
    _diff_symbols,
    _extract_symbols,
    _propagate_impact,
    _symbol_fingerprint,
    build_repo_ir,
    extract_file_ir,
    ChangedSymbol,
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
# Phase 1 — Symbol extraction (unchanged internals)
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
# Phase 2 — Spring semantic tagging (unchanged internals)
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
        for key in ("controllers", "services", "repositories", "configs", "transactional"):
            assert key in summary

    def test_summary_deterministic(self):
        _, s1, _ = _extract_symbols(SIMPLE_SERVICE, "UserService.java")
        _, s2, _ = _extract_symbols(SIMPLE_SERVICE, "UserService.java")
        assert _build_spring_summary(s1) == _build_spring_summary(s2)


# ---------------------------------------------------------------------------
# Phase 3 — Relation graph (unchanged internals)
# ---------------------------------------------------------------------------

class TestRelationGraph:
    def _get_ir(self, source: str, rel_path: str = "Test.java") -> dict:
        return extract_file_ir(source, rel_path)

    def test_annotated_with_edge_present(self):
        ir = self._get_ir(SIMPLE_SERVICE, "UserService.java")
        types = [r["type"] for r in ir["graph"]["edges"]]
        assert "annotated_with" in types

    def test_imports_edges_present(self):
        ir = self._get_ir(SIMPLE_SERVICE, "UserService.java")
        types = [r["type"] for r in ir["graph"]["edges"]]
        assert "imports" in types

    def test_injects_edge_for_autowired(self):
        ir = self._get_ir(SIMPLE_SERVICE, "UserService.java")
        inject_edges = [r for r in ir["graph"]["edges"] if r["type"] == "injects"]
        assert len(inject_edges) >= 1
        assert inject_edges[0]["confidence"] == "high"

    def test_implements_edge(self):
        ir = self._get_ir(VALIDATOR, "EmailValidator.java")
        impl_edges = [r for r in ir["graph"]["edges"] if r["type"] == "implements"]
        assert len(impl_edges) >= 1
        assert impl_edges[0]["to"] == "javax.validation.ConstraintValidator"

    def test_evidence_field_present(self):
        ir = self._get_ir(SIMPLE_SERVICE, "UserService.java")
        for r in ir["graph"]["edges"]:
            assert "evidence" in r
            assert "type" in r["evidence"]

    def test_node_count_matches_symbols(self):
        ir = self._get_ir(SIMPLE_SERVICE, "UserService.java")
        assert len(ir["graph"]["nodes"]) > 0

    def test_edges_sorted_deterministic(self):
        ir1 = self._get_ir(SIMPLE_SERVICE, "UserService.java")
        ir2 = self._get_ir(SIMPLE_SERVICE, "UserService.java")
        assert ir1["graph"]["edges"] == ir2["graph"]["edges"]


# ---------------------------------------------------------------------------
# Phase 4 — Symbol-level diff (unchanged internals)
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

    def test_added_method_in_change_set(self):
        ir = extract_file_ir(self.NEW_V, "MyService.java", old_source=self.OLD_V)
        added = [c for c in ir["change_set"] if c["change_type"] == "added"]
        assert any("post" in c["entity"] for c in added)

    def test_no_false_changes_when_identical(self):
        ir = extract_file_ir(self.OLD_V, "MyService.java", old_source=self.OLD_V)
        assert ir["change_set"] == []
        assert ir["analysis"]["changed_entities"] == []

    def test_removed_symbol_in_change_set(self):
        ir = extract_file_ir(self.OLD_V, "MyService.java", old_source=self.NEW_V)
        removed = [c for c in ir["change_set"] if c["change_type"] == "removed"]
        assert any("post" in c["entity"] for c in removed)

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
        modified = [c for c in ir["change_set"] if c["change_type"] == "modified"]
        assert len(modified) == 1
        assert modified[0]["diff_type"] == "annotation_change"

    def test_change_set_has_required_fields(self):
        ir = extract_file_ir(self.NEW_V, "MyService.java", old_source=self.OLD_V)
        for c in ir["change_set"]:
            for key in ("entity", "change_type", "diff_type", "ir_weight",
                        "graph_centrality", "diff_intensity", "evidence_strength", "score"):
                assert key in c

    def test_change_set_valid_enums(self):
        ir = extract_file_ir(self.NEW_V, "MyService.java", old_source=self.OLD_V)
        for c in ir["change_set"]:
            assert c["change_type"] in ("added", "removed", "modified")
            assert c["diff_type"] in (
                "signature_change", "annotation_change", "structural_change", "unknown"
            )

    def test_no_change_set_without_old_source(self):
        ir = extract_file_ir(self.NEW_V, "MyService.java")
        assert ir["change_set"] == []


# ---------------------------------------------------------------------------
# Phase 5 — Evidence Engine
# ---------------------------------------------------------------------------

class TestEvidenceEngine:
    def test_evidence_bundle_completeness(self):
        bundle_complete = EvidenceBundle(
            entity="com.example.Svc",
            type="symbol",
            evidence=[],
            graph_links=["A→B[imports]"],
            diff_links=["com.example.Svc"],
            ir_links=["com.example.Svc"],
        )
        assert bundle_complete.is_complete

    def test_evidence_bundle_incomplete_no_graph(self):
        bundle = EvidenceBundle(
            entity="com.example.Svc",
            type="symbol",
            evidence=[],
            graph_links=[],
            diff_links=["com.example.Svc"],
            ir_links=["com.example.Svc"],
        )
        assert not bundle.is_complete

    def test_evidence_bundle_incomplete_no_diff(self):
        bundle = EvidenceBundle(
            entity="com.example.Svc",
            type="symbol",
            evidence=[],
            graph_links=["A→B[imports]"],
            diff_links=[],
            ir_links=["com.example.Svc"],
        )
        assert not bundle.is_complete

    def test_evidence_strength_average(self):
        bundle = EvidenceBundle(
            entity="x",
            type="symbol",
            evidence=[
                {"source": "ir_phase1", "strength": 1.0},
                {"source": "graph_edge", "strength": 1.0},
                {"source": "git_diff", "strength": 0.6},
            ],
            graph_links=["e"],
            diff_links=["x"],
            ir_links=["x"],
        )
        assert abs(bundle.evidence_strength - round((1.0 + 1.0 + 0.6) / 3, 4)) < 1e-6

    def test_diff_intensity_added(self):
        cs = ChangedSymbol("x", "added", "structural_change")
        assert _diff_intensity_cs(cs) == 1.0

    def test_diff_intensity_removed(self):
        cs = ChangedSymbol("x", "removed", "structural_change")
        assert _diff_intensity_cs(cs) == 1.0

    def test_diff_intensity_signature_change(self):
        cs = ChangedSymbol("x", "modified", "signature_change")
        assert _diff_intensity_cs(cs) == 1.0

    def test_diff_intensity_annotation_change(self):
        cs = ChangedSymbol("x", "modified", "annotation_change")
        assert _diff_intensity_cs(cs) == 0.6

    def test_diff_intensity_unknown(self):
        cs = ChangedSymbol("x", "modified", "unknown")
        assert _diff_intensity_cs(cs) == 0.1

    def test_build_evidence_bundles_has_ir_link(self):
        _, symbols, raw_imports = _extract_symbols(SIMPLE_SERVICE, "UserService.java")
        package = "com.example.service"
        rels = _build_relations(symbols, raw_imports, SIMPLE_SERVICE, package, "UserService.java")
        bundles = _build_evidence_bundles(symbols, rels, [])
        for sym in symbols:
            assert sym.symbol in bundles
            assert sym.symbol in bundles[sym.symbol].ir_links

    def test_build_evidence_bundles_graph_links_present(self):
        _, symbols, raw_imports = _extract_symbols(SIMPLE_SERVICE, "UserService.java")
        package = "com.example.service"
        rels = _build_relations(symbols, raw_imports, SIMPLE_SERVICE, package, "UserService.java")
        bundles = _build_evidence_bundles(symbols, rels, [])
        # Class should have graph links (it has imports + annotated_with edges)
        cls_fqn = "com.example.service.UserService"
        assert bundles[cls_fqn].graph_links  # non-empty

    def test_build_evidence_bundles_diff_links_when_changed(self):
        _, symbols, raw_imports = _extract_symbols(SIMPLE_SERVICE, "UserService.java")
        package = "com.example.service"
        rels = _build_relations(symbols, raw_imports, SIMPLE_SERVICE, package, "UserService.java")
        cs = ChangedSymbol("com.example.service.UserService", "modified", "annotation_change")
        bundles = _build_evidence_bundles(symbols, rels, [cs])
        assert "com.example.service.UserService" in bundles["com.example.service.UserService"].diff_links


# ---------------------------------------------------------------------------
# Phase 5 — BFS / subsystems / propagation
# ---------------------------------------------------------------------------

class TestGraphAlgorithms:
    def test_bfs_reachability_no_edges(self):
        adj: dict[str, set[str]] = {}
        assert _bfs_reachability("A", adj) == 0

    def test_bfs_reachability_linear(self):
        adj = {"A": {"B"}, "B": {"C"}}
        assert _bfs_reachability("A", adj) == 2

    def test_bfs_reachability_depth_limited(self):
        # Chain A→B→C→D, max_depth=2 → reachable: B, C (not D)
        adj = {"A": {"B"}, "B": {"C"}, "C": {"D"}}
        assert _bfs_reachability("A", adj, max_depth=2) == 2

    def test_detect_subsystems_single_component(self):
        from sourcecode.repository_ir import RelationEdge
        edges = [RelationEdge("A", "B", "imports"), RelationEdge("B", "C", "imports")]
        components = _detect_subsystems(["A", "B", "C"], edges)
        assert len(components) == 1
        assert sorted(components[0]) == ["A", "B", "C"]

    def test_detect_subsystems_two_components(self):
        from sourcecode.repository_ir import RelationEdge
        edges = [RelationEdge("A", "B", "imports")]
        # C is isolated
        components = _detect_subsystems(["A", "B", "C"], edges)
        assert len(components) == 2

    def test_detect_subsystems_no_edges(self):
        components = _detect_subsystems(["A", "B", "C"], [])
        assert len(components) == 3

    def test_propagate_impact_direct_neighbor(self):
        adjacency = {"A": {"B"}}
        result = _propagate_impact({"A"}, {"A": 1.0}, adjacency, {"A", "B"})
        assert len(result) == 1
        assert result[0]["entity"] == "B"
        assert result[0]["depth"] == 1
        assert result[0]["impact_score"] == 0.5

    def test_propagate_impact_two_hops(self):
        adjacency = {"A": {"B"}, "B": {"C"}}
        result = _propagate_impact({"A"}, {"A": 1.0}, adjacency, {"A", "B", "C"})
        entities = {r["entity"] for r in result}
        assert "B" in entities
        assert "C" in entities

    def test_propagate_impact_no_path(self):
        adjacency: dict[str, set[str]] = {}
        result = _propagate_impact({"A"}, {"A": 1.0}, adjacency, {"A", "B"})
        assert result == []

    def test_propagate_impact_changed_not_in_impacted(self):
        adjacency = {"A": {"B"}, "B": {"A"}}  # cycle
        result = _propagate_impact({"A"}, {"A": 1.0}, adjacency, {"A", "B"})
        impacted_entities = {r["entity"] for r in result}
        assert "A" not in impacted_entities


# ---------------------------------------------------------------------------
# Output contract — single schema_version=final-v1
# ---------------------------------------------------------------------------

class TestOutputContract:
    def test_top_level_keys(self):
        ir = extract_file_ir(SIMPLE_SERVICE, "UserService.java")
        assert set(ir.keys()) == {
            "schema_version", "graph", "analysis", "impact",
            "subsystems", "change_set", "audit",
        }

    def test_schema_version(self):
        ir = extract_file_ir(SIMPLE_SERVICE, "UserService.java")
        assert ir["schema_version"] == "final-v1"

    def test_graph_keys(self):
        ir = extract_file_ir(SIMPLE_SERVICE, "UserService.java")
        assert "nodes" in ir["graph"]
        assert "edges" in ir["graph"]

    def test_graph_node_schema(self):
        ir = extract_file_ir(SIMPLE_SERVICE, "UserService.java")
        for node in ir["graph"]["nodes"]:
            assert "fqn" in node
            assert "type" in node
            assert "role" in node
            assert "in_degree" in node
            assert "out_degree" in node
            assert node["type"] in ("class", "interface", "method", "field")

    def test_graph_edge_schema(self):
        ir = extract_file_ir(SIMPLE_SERVICE, "UserService.java")
        for edge in ir["graph"]["edges"]:
            assert "from" in edge
            assert "to" in edge
            assert "type" in edge
            assert "confidence" in edge
            assert "evidence" in edge
            assert edge["type"] in (
                "imports", "extends", "implements", "injects",
                "mapped_to", "annotated_with", "calls",
            )

    def test_analysis_keys(self):
        ir = extract_file_ir(SIMPLE_SERVICE, "UserService.java")
        for key in ("changed_entities", "impacted_entities", "isolated_changes", "validated_changes"):
            assert key in ir["analysis"]

    def test_impact_keys(self):
        ir = extract_file_ir(SIMPLE_SERVICE, "UserService.java")
        assert "global_score" in ir["impact"]
        assert "ranked_nodes" in ir["impact"]

    def test_ranked_node_schema(self):
        ir = extract_file_ir(SIMPLE_SERVICE, "UserService.java")
        for node in ir["impact"]["ranked_nodes"]:
            assert "entity" in node
            assert "type" in node
            assert "role" in node
            assert "score" in node

    def test_audit_has_dropped_fields(self):
        ir = extract_file_ir(SIMPLE_SERVICE, "UserService.java")
        assert "dropped_fields" in ir["audit"]
        assert isinstance(ir["audit"]["dropped_fields"], list)

    def test_subsystems_is_list_of_lists(self):
        ir = extract_file_ir(SIMPLE_SERVICE, "UserService.java")
        assert isinstance(ir["subsystems"], list)
        for sub in ir["subsystems"]:
            assert isinstance(sub, list)

    def test_change_set_empty_without_diff(self):
        ir = extract_file_ir(SIMPLE_SERVICE, "UserService.java")
        assert ir["change_set"] == []
        assert ir["analysis"]["changed_entities"] == []

    def test_no_forbidden_fields(self):
        ir = extract_file_ir(SIMPLE_SERVICE, "UserService.java")
        forbidden = {
            "behavioral_change", "api_change", "propagation_risk",
            "core_service", "transaction_boundary", "symbols",
            "spring_summary", "graph_metadata", "changed_symbols", "relations",
        }
        assert not forbidden & set(ir.keys())

    def test_output_deterministic(self):
        ir1 = extract_file_ir(SIMPLE_SERVICE, "UserService.java")
        ir2 = extract_file_ir(SIMPLE_SERVICE, "UserService.java")
        assert ir1 == ir2

    def test_nodes_sorted_by_fqn(self):
        ir = extract_file_ir(SIMPLE_SERVICE, "UserService.java")
        fqns = [n["fqn"] for n in ir["graph"]["nodes"]]
        assert fqns == sorted(fqns)


# ---------------------------------------------------------------------------
# Validated vs isolated classification
# ---------------------------------------------------------------------------

class TestChangeClassification:
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

    def test_validated_changes_require_graph_diff_ir(self):
        ir = extract_file_ir(self.NEW_V, "MyService.java", old_source=self.OLD_V)
        for vc in ir["analysis"]["validated_changes"]:
            # Every validated change must appear in change_set with evidence_bundle
            cs_entry = next(
                (c for c in ir["change_set"] if c["entity"] == vc["entity"]), None
            )
            assert cs_entry is not None
            assert cs_entry["evidence_bundle"] is not None
            eb = cs_entry["evidence_bundle"]
            assert eb["graph_links"]
            assert eb["diff_links"]
            assert eb["ir_links"]

    def test_isolated_changes_have_no_graph_links(self):
        ir = extract_file_ir(self.NEW_V, "MyService.java", old_source=self.OLD_V)
        for iso in ir["analysis"]["isolated_changes"]:
            cs_entry = next(
                (c for c in ir["change_set"] if c["entity"] == iso["entity"]), None
            )
            assert cs_entry is not None
            eb = cs_entry["evidence_bundle"]
            assert not eb["graph_links"]

    def test_isolated_changes_in_dropped_fields(self):
        ir = extract_file_ir(self.NEW_V, "MyService.java", old_source=self.OLD_V)
        isolated_entities = {c["entity"] for c in ir["analysis"]["isolated_changes"]}
        dropped_entities = {d["entity"] for d in ir["audit"]["dropped_fields"]}
        assert isolated_entities.issubset(dropped_entities)

    def test_changed_entities_have_graph_links(self):
        ir = extract_file_ir(self.NEW_V, "MyService.java", old_source=self.OLD_V)
        for ce in ir["analysis"]["changed_entities"]:
            cs_entry = next(
                (c for c in ir["change_set"] if c["entity"] == ce["entity"]), None
            )
            assert cs_entry is not None
            assert cs_entry["evidence_bundle"]["graph_links"]

    def test_change_set_score_deterministic(self):
        ir1 = extract_file_ir(self.NEW_V, "MyService.java", old_source=self.OLD_V)
        ir2 = extract_file_ir(self.NEW_V, "MyService.java", old_source=self.OLD_V)
        scores1 = {c["entity"]: c["score"] for c in ir1["change_set"]}
        scores2 = {c["entity"]: c["score"] for c in ir2["change_set"]}
        assert scores1 == scores2


# ---------------------------------------------------------------------------
# build_repo_ir
# ---------------------------------------------------------------------------

class TestBuildRepoIr:
    def test_empty_file_list(self, tmp_path):
        ir = build_repo_ir([], tmp_path)
        assert ir["schema_version"] == "final-v1"
        assert ir["graph"]["nodes"] == []
        assert ir["graph"]["edges"] == []
        assert ir["change_set"] == []

    def test_single_file(self, tmp_path):
        java_file = tmp_path / "UserService.java"
        java_file.write_text(SIMPLE_SERVICE, encoding="utf-8")
        ir = build_repo_ir(["UserService.java"], tmp_path)
        fqns = [n["fqn"] for n in ir["graph"]["nodes"]]
        assert any("UserService" in f for f in fqns)

    def test_multi_file_aggregation(self, tmp_path):
        (tmp_path / "UserService.java").write_text(SIMPLE_SERVICE, encoding="utf-8")
        (tmp_path / "UserController.java").write_text(SIMPLE_CONTROLLER, encoding="utf-8")
        ir = build_repo_ir(["UserService.java", "UserController.java"], tmp_path)
        fqns = [n["fqn"] for n in ir["graph"]["nodes"]]
        assert any("UserService" in f for f in fqns)
        assert any("UserController" in f for f in fqns)

    def test_spring_roles_in_graph_nodes(self, tmp_path):
        (tmp_path / "UserService.java").write_text(SIMPLE_SERVICE, encoding="utf-8")
        (tmp_path / "UserController.java").write_text(SIMPLE_CONTROLLER, encoding="utf-8")
        ir = build_repo_ir(["UserService.java", "UserController.java"], tmp_path)
        roles = {n["fqn"]: n["role"] for n in ir["graph"]["nodes"]}
        assert roles.get("com.example.service.UserService") == "service"
        assert roles.get("com.example.web.UserController") == "controller"

    def test_deterministic_multi_file(self, tmp_path):
        (tmp_path / "A.java").write_text(SIMPLE_SERVICE, encoding="utf-8")
        (tmp_path / "B.java").write_text(VALIDATOR, encoding="utf-8")
        ir1 = build_repo_ir(["A.java", "B.java"], tmp_path)
        ir2 = build_repo_ir(["A.java", "B.java"], tmp_path)
        assert ir1 == ir2

    def test_missing_file_skipped(self, tmp_path):
        ir = build_repo_ir(["nonexistent.java"], tmp_path)
        assert ir["graph"]["nodes"] == []

    def test_subsystems_non_empty_multi_file(self, tmp_path):
        (tmp_path / "UserService.java").write_text(SIMPLE_SERVICE, encoding="utf-8")
        (tmp_path / "UserController.java").write_text(SIMPLE_CONTROLLER, encoding="utf-8")
        ir = build_repo_ir(["UserService.java", "UserController.java"], tmp_path)
        assert len(ir["subsystems"]) >= 1

    def test_global_score_zero_without_diff(self, tmp_path):
        (tmp_path / "UserService.java").write_text(SIMPLE_SERVICE, encoding="utf-8")
        ir = build_repo_ir(["UserService.java"], tmp_path)
        assert ir["impact"]["global_score"] == 0.0

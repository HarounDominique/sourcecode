"""test_context_graph.py — ContextGraph façade contract tests.

The façade must be a faithful, deterministic view over CanonicalRepositoryIR:
it introduces no parsing, no divergent data, and preserves the IR's ordering
guarantees. These tests build a small self-contained Spring-ish repo in tmp so
they run everywhere (no external field-test repos required).

Coverage:
  TestFacadeParity      — façade data equals the underlying CIR (no divergence)
  TestFacadeQueries     — each query returns correct, deterministic results
  TestFacadeAgnostic    — generic API shape (kind/role/relation) works as designed
  TestFacadeConstruction — build / build_from_root / from_cir wiring
"""
from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Generator

import pytest

from sourcecode.canonical_ir import build_canonical_ir
from sourcecode.context_graph import (
    ContextGraph,
    Evidence,
    Relation,
    Symbol,
)
from sourcecode.repository_ir import find_java_files


# ---------------------------------------------------------------------------
# Fixture — a minimal Spring MVC repo with interface/impl, DI, and endpoints
# ---------------------------------------------------------------------------

def _write(root: Path, rel: str, content: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(content), encoding="utf-8")


@pytest.fixture()
def spring_repo(tmp_path: Path) -> Generator[Path, None, None]:
    root = tmp_path / "spring-repo"
    root.mkdir()
    _write(root, "pom.xml", "<project><groupId>test</groupId></project>")

    _write(root, "src/main/java/com/example/GreetingService.java", """
        package com.example;

        public interface GreetingService {
            String greet(String name);
        }
    """)

    _write(root, "src/main/java/com/example/GreetingServiceImpl.java", """
        package com.example;

        import org.springframework.stereotype.Service;

        @Service
        public class GreetingServiceImpl implements GreetingService {
            public String greet(String name) {
                return "hi " + name;
            }
        }
    """)

    _write(root, "src/main/java/com/example/GreetingController.java", """
        package com.example;

        import org.springframework.web.bind.annotation.GetMapping;
        import org.springframework.web.bind.annotation.RequestMapping;
        import org.springframework.web.bind.annotation.RestController;

        @RestController
        @RequestMapping("/api")
        public class GreetingController {

            private final GreetingService service;

            public GreetingController(GreetingService service) {
                this.service = service;
            }

            @GetMapping("/hello")
            public String hello() {
                return service.greet("world");
            }
        }
    """)

    yield root


@pytest.fixture()
def graph(spring_repo: Path) -> ContextGraph:
    return ContextGraph.build_from_root(spring_repo)


# ---------------------------------------------------------------------------
# Parity — façade equals the underlying CIR, adds no divergent data
# ---------------------------------------------------------------------------

class TestFacadeParity:
    def test_symbols_match_cir_exactly(self, graph: ContextGraph):
        """Every façade symbol FQN is a CIR symbol and vice-versa."""
        facade_fqns = {s.fqn for s in graph.symbols()}
        assert facade_fqns == set(graph.cir.symbols)

    def test_relation_count_matches_call_graph(self, graph: ContextGraph):
        assert len(graph.relations()) == len(graph.cir.call_graph)

    def test_endpoints_are_cir_endpoints(self, graph: ContextGraph):
        assert graph.endpoints() == list(graph.cir.endpoints)

    def test_metrics_counts_are_consistent(self, graph: ContextGraph):
        m = graph.metrics()
        assert m["node_count"] == len(graph.symbols())
        assert m["relation_count"] == len(graph.cir.call_graph)
        assert m["endpoint_count"] == len(graph.cir.endpoints)
        assert m["cir_hash"] == graph.cir.cir_hash

    def test_facade_introduces_no_new_parse(self, spring_repo: Path):
        """The façade's CIR is identical (same hash) to one built directly —
        proving it delegates to the same pipeline, not a second parser."""
        files = find_java_files(spring_repo)
        direct = build_canonical_ir(files, spring_repo)
        cg = ContextGraph.build(files, spring_repo)
        assert cg.cir.cir_hash == direct.cir_hash
        assert cg.cir.symbols == direct.symbols


# ---------------------------------------------------------------------------
# Queries — correctness + determinism
# ---------------------------------------------------------------------------

class TestFacadeQueries:
    def test_symbol_lookup(self, graph: ContextGraph):
        s = graph.symbol("com.example.GreetingController")
        assert s is not None
        assert s.role == "controller"
        assert s.has_annotation("RestController")
        assert s.has_annotation("@RestController")

    def test_symbol_lookup_missing_returns_none(self, graph: ContextGraph):
        assert graph.symbol("com.example.DoesNotExist") is None

    def test_role_convenience(self, graph: ContextGraph):
        assert [c.fqn for c in graph.controllers()] == [
            "com.example.GreetingController"
        ]
        # Both the impl and the (name-inferred) interface carry role="service" —
        # this reflects the IR's role taxonomy; the façade surfaces it verbatim.
        service_fqns = {s.fqn for s in graph.services()}
        assert "com.example.GreetingServiceImpl" in service_fqns

    def test_implementations_and_subtypes(self, graph: ContextGraph):
        impls = graph.implementations_of("com.example.GreetingService")
        assert "com.example.GreetingServiceImpl" in impls
        assert "com.example.GreetingService" in graph.interfaces_of(
            "com.example.GreetingServiceImpl"
        )
        assert "com.example.GreetingServiceImpl" in graph.subtypes_of(
            "com.example.GreetingService"
        )

    def test_injection(self, graph: ContextGraph):
        deps = graph.injected_dependencies_of("com.example.GreetingController")
        assert "com.example.GreetingService" in deps
        dependents = graph.dependents_of("com.example.GreetingService")
        assert "com.example.GreetingController" in dependents

    def test_endpoints_of_controller(self, graph: ContextGraph):
        eps = graph.endpoints_of("com.example.GreetingController")
        assert len(eps) == 1
        assert eps[0].path == "/api/hello"
        assert eps[0].method == "GET"

    def test_queries_are_deterministic(self, graph: ContextGraph):
        assert graph.symbols() == graph.symbols()
        assert graph.relations() == graph.relations()
        assert graph.controllers() == graph.controllers()

    def test_evidence_grounds_connected_symbol(self, graph: ContextGraph):
        ev = graph.evidence_for("com.example.GreetingService")
        assert isinstance(ev, Evidence)
        assert ev.is_grounded  # interface is implemented + injected
        # incoming should include the implements edge from the impl class
        assert any(
            r.kind == "implements"
            and r.source == "com.example.GreetingServiceImpl"
            for r in ev.incoming
        )


# ---------------------------------------------------------------------------
# Agnostic API shape — generic kind/role/relation primitives
# ---------------------------------------------------------------------------

class TestFacadeAgnostic:
    def test_symbols_filter_by_kind(self, graph: ContextGraph):
        methods = graph.symbols(kind="method")
        assert all(s.kind == "method" for s in methods)
        assert "com.example.GreetingServiceImpl#greet" in {s.fqn for s in methods}
        # the HTTP handler is a distinct 'endpoint' kind, not a plain method
        assert graph.symbol("com.example.GreetingController#hello").kind == "endpoint"

    def test_symbols_filter_by_annotation(self, graph: ContextGraph):
        annotated = graph.symbols(annotated_with="Service")
        assert {s.fqn for s in annotated} == {"com.example.GreetingServiceImpl"}

    def test_relations_filter_by_kind(self, graph: ContextGraph):
        impl_edges = graph.relations(kind="implements")
        assert all(r.kind == "implements" for r in impl_edges)
        assert any(
            r.source == "com.example.GreetingServiceImpl" for r in impl_edges
        )

    def test_relation_view_shape(self, graph: ContextGraph):
        rels = graph.relations(kind="injects")
        assert rels and isinstance(rels[0], Relation)
        assert rels[0].source and rels[0].target and rels[0].kind == "injects"

    def test_symbol_view_is_immutable(self, graph: ContextGraph):
        s = graph.symbol("com.example.GreetingController")
        assert isinstance(s, Symbol)
        with pytest.raises((AttributeError, TypeError)):
            s.role = "hacked"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Construction wiring
# ---------------------------------------------------------------------------

class TestFacadeConstruction:
    def test_from_cir_does_not_rebuild(self, spring_repo: Path):
        files = find_java_files(spring_repo)
        cir = build_canonical_ir(files, spring_repo)
        cg = ContextGraph.from_cir(cir)
        assert cg.cir is cir
        assert cg.metrics()["build_ms"] == 0.0

    def test_build_records_timing(self, spring_repo: Path):
        files = find_java_files(spring_repo)
        cg = ContextGraph.build(files, spring_repo)
        assert cg.metrics()["build_ms"] >= 0.0

    def test_repr_is_informative(self, graph: ContextGraph):
        r = repr(graph)
        assert "ContextGraph(" in r and "nodes=" in r

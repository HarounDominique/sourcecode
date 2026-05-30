"""test_canonical_ir.py — Canonical IR contract test suite.

Four test classes:
  TestCanonicalIRConsistency  — invariant validation across all views
  TestCanonicalIRDeterminism  — identical repo → identical CIR hash + ordering
  TestProjectionParity        — projections produce semantically equivalent output
  TestFieldMismatchRegression — guard against class/handler vs effective_class/symbol bugs
"""
from __future__ import annotations

import hashlib
import json
import textwrap
from pathlib import Path
from typing import Generator

import pytest

from sourcecode.canonical_ir import (
    IR_SCHEMA_VERSION,
    CanonicalEndpoint,
    CanonicalRepositoryIR,
    CanonicalSecurity,
    build_canonical_ir,
    ir_dict_to_canonical,
    project_blast_radius,
    project_endpoint_surface,
    project_route_surface,
    validate_canonical_ir,
)
from sourcecode.repository_ir import (
    build_repo_ir,
    compute_blast_radius,
    extract_java_endpoints,
    find_java_files,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _write(root: Path, rel: str, content: str) -> None:
    """Write fixture file, dedenting so Java `package` declarations land at column 0."""
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(content), encoding="utf-8")


@pytest.fixture()
def jaxrs_repo(tmp_path: Path) -> Generator[Path, None, None]:
    """Minimal JAX-RS repo with security annotations covering all policies."""
    root = tmp_path / "jaxrs-repo"
    root.mkdir()

    _write(root, "pom.xml", "<project><groupId>test</groupId></project>")

    _write(root, "src/main/java/com/example/UserResource.java", """
        package com.example;

        import jakarta.ws.rs.*;
        import jakarta.annotation.security.RolesAllowed;
        import jakarta.annotation.security.PermitAll;
        import jakarta.annotation.security.DenyAll;

        @Path("/users")
        public class UserResource {

            @GET
            @RolesAllowed("admin")
            public String listUsers() { return "[]"; }

            @POST
            @Path("/register")
            @PermitAll
            public String register() { return "ok"; }

            @DELETE
            @Path("/{id}")
            @DenyAll
            public String delete() { return "denied"; }
        }
    """)

    _write(root, "src/main/java/com/example/AuthResource.java", """
        package com.example;

        import jakarta.ws.rs.*;
        import jakarta.annotation.security.Authenticated;

        @Path("/auth")
        public class AuthResource {

            @POST
            @Path("/login")
            @Authenticated
            public String login() { return "ok"; }

            @GET
            @Path("/status")
            public String status() { return "up"; }
        }
    """)

    _write(root, "src/main/java/com/example/UserService.java", """
        package com.example;

        import jakarta.enterprise.context.ApplicationScoped;

        @ApplicationScoped
        public class UserService {
            public String findAll() { return "[]"; }
            public void delete(String id) {}
        }
    """)

    yield root


@pytest.fixture()
def spring_repo(tmp_path: Path) -> Generator[Path, None, None]:
    """Minimal Spring MVC repo with @PreAuthorize and @Secured."""
    root = tmp_path / "spring-repo"
    root.mkdir()

    _write(root, "pom.xml", "<project><groupId>test</groupId></project>")

    _write(root, "src/main/java/com/example/controller/OrderController.java", """
        package com.example.controller;

        import org.springframework.web.bind.annotation.*;
        import org.springframework.security.access.prepost.PreAuthorize;
        import org.springframework.security.access.annotation.Secured;
        import com.example.service.OrderService;

        @RestController
        @RequestMapping("/api/orders")
        public class OrderController {

            @Autowired
            private OrderService orderService;

            @GetMapping
            @PreAuthorize("hasRole('USER')")
            public String listOrders() { return "[]"; }

            @PostMapping
            @Secured({"ROLE_ADMIN", "ROLE_MANAGER"})
            public String createOrder() { return "created"; }

            @DeleteMapping("/{id}")
            @PreAuthorize("hasRole('ADMIN')")
            public void deleteOrder() {}
        }
    """)

    _write(root, "src/main/java/com/example/service/OrderService.java", """
        package com.example.service;

        import org.springframework.stereotype.Service;

        @Service
        public class OrderService {
            public String findAll() { return "[]"; }
            public String create() { return "created"; }
            public void delete(Long id) {}
        }
    """)

    yield root


@pytest.fixture()
def multi_annotation_repo(tmp_path: Path) -> Generator[Path, None, None]:
    """Repo with Shiro + OpenAPI security annotations to verify full extractor coverage."""
    root = tmp_path / "multi-ann-repo"
    root.mkdir()

    _write(root, "pom.xml", "<project><groupId>test</groupId></project>")

    _write(root, "src/main/java/com/example/AdminResource.java", """
        package com.example;

        import jakarta.ws.rs.*;
        import org.apache.shiro.authz.annotation.RequiresRoles;
        import io.swagger.v3.oas.annotations.security.SecurityRequirement;

        @Path("/admin")
        public class AdminResource {

            @GET
            @Path("/dashboard")
            @RequiresRoles("admin")
            public String dashboard() { return "ok"; }

            @GET
            @Path("/reports")
            @SecurityRequirement(name = "bearerAuth")
            public String reports() { return "ok"; }
        }
    """)

    yield root


# ---------------------------------------------------------------------------
# A. TestCanonicalIRConsistency
# ---------------------------------------------------------------------------

class TestCanonicalIRConsistency:
    """Invariant validation: all views derive from CIR without divergence."""

    def test_validate_canonical_ir_clean(self, jaxrs_repo: Path) -> None:
        """CIR built from a valid repo must pass validate_canonical_ir with no violations."""
        file_paths = find_java_files(jaxrs_repo)
        cir = build_canonical_ir(file_paths, jaxrs_repo)

        violations = validate_canonical_ir(cir)
        assert violations == [], f"CIR validation failed:\n" + "\n".join(violations)

    def test_schema_version_present(self, jaxrs_repo: Path) -> None:
        """CIR schema_version must be IR_SCHEMA_VERSION constant."""
        file_paths = find_java_files(jaxrs_repo)
        cir = build_canonical_ir(file_paths, jaxrs_repo)

        assert cir.schema_version == IR_SCHEMA_VERSION

    def test_endpoint_ids_deterministic(self, jaxrs_repo: Path) -> None:
        """Every endpoint id = METHOD:path:controller_class:handler_symbol."""
        file_paths = find_java_files(jaxrs_repo)
        cir = build_canonical_ir(file_paths, jaxrs_repo)

        for ep in cir.endpoints:
            expected = CanonicalEndpoint.make_id(
                ep.method, ep.path, ep.controller_class, ep.handler_symbol
            )
            assert ep.id == expected, (
                f"Endpoint id not deterministic: stored={ep.id!r} expected={expected!r}"
            )

    def test_route_surface_subset_of_cir_endpoints(self, jaxrs_repo: Path) -> None:
        """Every projected route endpoint must be in cir.endpoints (no phantom routes)."""
        file_paths = find_java_files(jaxrs_repo)
        cir = build_canonical_ir(file_paths, jaxrs_repo)

        cir_endpoint_ids = {ep.id for ep in cir.endpoints}
        projected = project_route_surface(cir)

        for route in projected:
            ep_id = CanonicalEndpoint.make_id(
                route["method"],
                route["path"],
                route["effective_class"],
                route["symbol"],
            )
            assert ep_id in cir_endpoint_ids, (
                f"Projected route not in CIR endpoints: {ep_id!r}"
            )

    def test_blast_radius_endpoints_subset_of_cir(self, jaxrs_repo: Path) -> None:
        """endpoints_affected in blast_radius must all be traceable to cir.endpoints."""
        file_paths = find_java_files(jaxrs_repo)
        cir = build_canonical_ir(file_paths, jaxrs_repo)

        if not cir.endpoints:
            pytest.skip("No endpoints in repo")

        # Pick a target that is likely to have endpoints
        target = cir.endpoints[0].controller_class
        result = project_blast_radius(cir, target)

        # Build a set of (method, path) pairs from cir.endpoints for lookup
        cir_ep_pairs = {(ep.method, ep.path) for ep in cir.endpoints}

        for ep_affected in result.get("endpoints_affected", []):
            pair = (ep_affected.get("method", ""), ep_affected.get("path", ""))
            assert pair in cir_ep_pairs, (
                f"blast_radius endpoint not in CIR endpoints: {pair}"
            )

    def test_security_index_keys_are_handler_symbols(self, jaxrs_repo: Path) -> None:
        """security_index keys must all be valid endpoint handler_symbols."""
        file_paths = find_java_files(jaxrs_repo)
        cir = build_canonical_ir(file_paths, jaxrs_repo)

        handler_syms = {ep.handler_symbol for ep in cir.endpoints}
        for sym in cir.security_index:
            assert sym in handler_syms, (
                f"security_index key {sym!r} not in endpoint handler_symbols"
            )

    def test_no_duplicate_endpoint_ids(self, jaxrs_repo: Path) -> None:
        """No two endpoints in CIR may share the same id."""
        file_paths = find_java_files(jaxrs_repo)
        cir = build_canonical_ir(file_paths, jaxrs_repo)

        ids = [ep.id for ep in cir.endpoints]
        assert len(ids) == len(set(ids)), (
            f"Duplicate endpoint ids: {[i for i in ids if ids.count(i) > 1]}"
        )

    def test_endpoint_required_fields_present(self, jaxrs_repo: Path) -> None:
        """Every endpoint must have non-empty method, path, controller_class, handler_symbol."""
        file_paths = find_java_files(jaxrs_repo)
        cir = build_canonical_ir(file_paths, jaxrs_repo)

        for ep in cir.endpoints:
            assert ep.method, f"Endpoint missing method: {ep.id!r}"
            assert ep.path, f"Endpoint missing path: {ep.id!r}"
            assert ep.controller_class, f"Endpoint missing controller_class: {ep.id!r}"
            assert ep.handler_symbol, f"Endpoint missing handler_symbol: {ep.id!r}"

    def test_symbol_count_matches_nodes(self, jaxrs_repo: Path) -> None:
        """cir.symbols count must match metadata.symbol_count."""
        file_paths = find_java_files(jaxrs_repo)
        cir = build_canonical_ir(file_paths, jaxrs_repo)

        assert len(cir.symbols) == cir.metadata["symbol_count"]

    def test_endpoint_count_matches_metadata(self, jaxrs_repo: Path) -> None:
        """cir.endpoints count must match metadata.endpoint_count."""
        file_paths = find_java_files(jaxrs_repo)
        cir = build_canonical_ir(file_paths, jaxrs_repo)

        assert len(cir.endpoints) == cir.metadata["endpoint_count"]

    def test_spring_repo_invariants_pass(self, spring_repo: Path) -> None:
        """CIR from Spring MVC repo must pass invariants."""
        file_paths = find_java_files(spring_repo)
        cir = build_canonical_ir(file_paths, spring_repo)

        violations = validate_canonical_ir(cir)
        assert violations == [], "\n".join(violations)


# ---------------------------------------------------------------------------
# B. TestCanonicalIRDeterminism
# ---------------------------------------------------------------------------

class TestCanonicalIRDeterminism:
    """Identical repo → identical CIR hash + stable ordering."""

    def test_identical_repo_identical_hash(self, jaxrs_repo: Path) -> None:
        """Building CIR twice from same files must produce identical cir_hash."""
        file_paths = find_java_files(jaxrs_repo)

        cir1 = build_canonical_ir(file_paths, jaxrs_repo)
        cir2 = build_canonical_ir(file_paths, jaxrs_repo)

        assert cir1.cir_hash == cir2.cir_hash, (
            f"Non-deterministic hash: {cir1.cir_hash[:16]} != {cir2.cir_hash[:16]}"
        )

    def test_cir_hash_changes_on_file_change(self, tmp_path: Path) -> None:
        """Adding a new endpoint must change the cir_hash."""
        root = tmp_path / "repo"
        root.mkdir()
        _write(root, "pom.xml", "<project/>")
        _write(root, "src/main/java/com/example/Svc.java", """
            package com.example;
            import jakarta.ws.rs.*;
            @Path("/a")
            public class Svc {
                @GET public String get() { return "ok"; }
            }
        """)

        files_v1 = find_java_files(root)
        cir1 = build_canonical_ir(files_v1, root)

        # Add a new endpoint
        _write(root, "src/main/java/com/example/Svc2.java", """
            package com.example;
            import jakarta.ws.rs.*;
            @Path("/b")
            public class Svc2 {
                @POST public String create() { return "created"; }
            }
        """)

        files_v2 = find_java_files(root)
        cir2 = build_canonical_ir(files_v2, root)

        assert cir1.cir_hash != cir2.cir_hash, "Hash must change when endpoint is added"

    def test_stable_endpoint_ordering(self, jaxrs_repo: Path) -> None:
        """cir.endpoints must be sorted stably by (method, path, controller_class, handler_symbol)."""
        file_paths = find_java_files(jaxrs_repo)
        cir = build_canonical_ir(file_paths, jaxrs_repo)

        expected_order = sorted(
            cir.endpoints,
            key=lambda ep: (ep.method, ep.path, ep.controller_class, ep.handler_symbol),
        )
        actual_ids = [ep.id for ep in cir.endpoints]
        expected_ids = [ep.id for ep in expected_order]
        assert actual_ids == expected_ids, "Endpoints not in stable sorted order"

    def test_stable_symbol_ordering(self, jaxrs_repo: Path) -> None:
        """cir.symbols must be lexicographically sorted."""
        file_paths = find_java_files(jaxrs_repo)
        cir = build_canonical_ir(file_paths, jaxrs_repo)

        assert cir.symbols == sorted(cir.symbols), "Symbols not in sorted order"

    def test_stable_edge_ordering(self, jaxrs_repo: Path) -> None:
        """call_graph edges must be sorted by (from, type, to)."""
        file_paths = find_java_files(jaxrs_repo)
        cir = build_canonical_ir(file_paths, jaxrs_repo)

        actual_keys = [
            (e.get("from", ""), e.get("type", ""), e.get("to", ""))
            for e in cir.call_graph
        ]
        assert actual_keys == sorted(actual_keys), "call_graph edges not in stable sorted order"

    def test_stable_file_ordering(self, jaxrs_repo: Path) -> None:
        """cir.files must be lexicographically sorted."""
        file_paths = find_java_files(jaxrs_repo)
        cir = build_canonical_ir(file_paths, jaxrs_repo)

        assert cir.files == sorted(cir.files), "Files not in sorted order"

    def test_cir_hash_recomputable(self, jaxrs_repo: Path) -> None:
        """validate_canonical_ir must confirm hash is reproducible (no violations)."""
        file_paths = find_java_files(jaxrs_repo)
        cir = build_canonical_ir(file_paths, jaxrs_repo)

        # Validate includes hash recomputation check
        violations = validate_canonical_ir(cir)
        hash_violations = [v for v in violations if "DETERMINISM" in v]
        assert hash_violations == [], f"Hash non-deterministic: {hash_violations}"

    def test_ir_dict_to_canonical_idempotent(self, jaxrs_repo: Path) -> None:
        """ir_dict_to_canonical applied twice to same IR dict must produce identical hash."""
        file_paths = find_java_files(jaxrs_repo)
        ir = build_repo_ir(file_paths, jaxrs_repo)

        cir1 = ir_dict_to_canonical(ir, file_paths=file_paths)
        cir2 = ir_dict_to_canonical(ir, file_paths=file_paths)

        assert cir1.cir_hash == cir2.cir_hash


# ---------------------------------------------------------------------------
# C. TestProjectionParity
# ---------------------------------------------------------------------------

class TestProjectionParity:
    """Before/after parity: projections produce semantically equivalent output."""

    def test_project_endpoint_surface_count_matches_extract(self, jaxrs_repo: Path) -> None:
        """project_endpoint_surface total must match extract_java_endpoints total."""
        file_paths = find_java_files(jaxrs_repo)
        cir = build_canonical_ir(file_paths, jaxrs_repo)

        projected = project_endpoint_surface(cir)
        legacy = extract_java_endpoints(jaxrs_repo)

        assert projected["total"] == legacy["total"], (
            f"Endpoint count mismatch: projected={projected['total']} "
            f"legacy={legacy['total']}"
        )

    def test_project_endpoint_surface_paths_match(self, jaxrs_repo: Path) -> None:
        """Paths in project_endpoint_surface must match extract_java_endpoints paths."""
        file_paths = find_java_files(jaxrs_repo)
        cir = build_canonical_ir(file_paths, jaxrs_repo)

        projected = project_endpoint_surface(cir)
        legacy = extract_java_endpoints(jaxrs_repo)

        projected_paths = sorted(
            (e["method"], e["path"]) for e in projected["endpoints"]
        )
        legacy_paths = sorted(
            (e["method"], e["path"]) for e in legacy["endpoints"]
        )
        assert projected_paths == legacy_paths, (
            f"Path mismatch.\nProjected: {projected_paths}\nLegacy: {legacy_paths}"
        )

    def test_project_endpoint_surface_security_parity(self, jaxrs_repo: Path) -> None:
        """Security policies in project_endpoint_surface must match extract_java_endpoints."""
        file_paths = find_java_files(jaxrs_repo)
        cir = build_canonical_ir(file_paths, jaxrs_repo)

        projected = project_endpoint_surface(cir)
        legacy = extract_java_endpoints(jaxrs_repo)

        # Build (method, path) → policy maps
        def _ep_sec_map(eps: list[dict]) -> dict:
            return {
                (e["method"], e["path"]): (e.get("security") or {}).get("policy", None)
                for e in eps
            }

        proj_map = _ep_sec_map(projected["endpoints"])
        legacy_map = _ep_sec_map(legacy["endpoints"])

        assert proj_map == legacy_map, (
            f"Security policy mismatch.\n"
            f"Projected: {proj_map}\n"
            f"Legacy: {legacy_map}"
        )

    def test_project_route_surface_count_matches_ir(self, jaxrs_repo: Path) -> None:
        """project_route_surface must produce same count as raw ir route_surface."""
        file_paths = find_java_files(jaxrs_repo)
        ir = build_repo_ir(file_paths, jaxrs_repo)
        cir = ir_dict_to_canonical(ir, file_paths=file_paths)

        projected = project_route_surface(cir)
        raw = ir.get("route_surface") or []

        assert len(projected) == len(raw), (
            f"route_surface count mismatch: projected={len(projected)} raw={len(raw)}"
        )

    def test_project_route_surface_paths_match_ir(self, jaxrs_repo: Path) -> None:
        """Paths in project_route_surface must match raw ir route_surface paths."""
        file_paths = find_java_files(jaxrs_repo)
        ir = build_repo_ir(file_paths, jaxrs_repo)
        cir = ir_dict_to_canonical(ir, file_paths=file_paths)

        projected = project_route_surface(cir)
        raw = ir.get("route_surface") or []

        proj_paths = sorted((r["method"], r["path"]) for r in projected)
        raw_paths = sorted((r["method"], r["path"]) for r in raw)

        assert proj_paths == raw_paths, (
            f"route_surface path mismatch.\nProjected: {proj_paths}\nRaw: {raw_paths}"
        )

    def test_no_security_signal_count_correct(self, jaxrs_repo: Path) -> None:
        """no_security_signal must equal total minus secured endpoint count."""
        file_paths = find_java_files(jaxrs_repo)
        cir = build_canonical_ir(file_paths, jaxrs_repo)
        projected = project_endpoint_surface(cir)

        eps = projected["endpoints"]
        manually_unsecured = sum(
            1 for e in eps if e.get("security", {}).get("policy") == "none_detected"
        )
        assert projected["no_security_signal"] == manually_unsecured

    def test_project_blast_radius_structure_present(self, jaxrs_repo: Path) -> None:
        """project_blast_radius must return expected keys."""
        file_paths = find_java_files(jaxrs_repo)
        cir = build_canonical_ir(file_paths, jaxrs_repo)

        if not cir.endpoints:
            pytest.skip("No endpoints")

        target = cir.endpoints[0].controller_class
        result = project_blast_radius(cir, target)

        for key in ("target", "direct_callers", "indirect_callers",
                    "endpoints_affected", "risk_score", "risk_level"):
            assert key in result, f"blast_radius missing key: {key!r}"

    def test_spring_security_projection_parity(self, spring_repo: Path) -> None:
        """Spring @PreAuthorize / @Secured parity between projected and legacy."""
        file_paths = find_java_files(spring_repo)
        cir = build_canonical_ir(file_paths, spring_repo)

        projected = project_endpoint_surface(cir)
        legacy = extract_java_endpoints(spring_repo)

        proj_secured = sum(
            1 for e in projected["endpoints"]
            if e.get("security", {}).get("policy") != "none_detected"
        )
        legacy_secured = sum(
            1 for e in legacy["endpoints"]
            if e.get("security", {}).get("policy") != "none_detected"
        )

        assert proj_secured == legacy_secured, (
            f"Secured endpoint count mismatch: projected={proj_secured} legacy={legacy_secured}"
        )


# ---------------------------------------------------------------------------
# D. TestFieldMismatchRegression
# ---------------------------------------------------------------------------

class TestFieldMismatchRegression:
    """Guard against field-name divergence bugs (class/handler vs effective_class/symbol etc.)."""

    def test_canonical_endpoint_uses_fqn_controller(self, jaxrs_repo: Path) -> None:
        """controller_class must be FQN (contains dot), not simple name."""
        file_paths = find_java_files(jaxrs_repo)
        cir = build_canonical_ir(file_paths, jaxrs_repo)

        for ep in cir.endpoints:
            assert "." in ep.controller_class, (
                f"controller_class is not FQN: {ep.controller_class!r} "
                f"(endpoint: {ep.id!r})"
            )

    def test_canonical_endpoint_handler_is_fqn_method(self, jaxrs_repo: Path) -> None:
        """handler_symbol must be FQN method reference (pkg.Class#method)."""
        file_paths = find_java_files(jaxrs_repo)
        cir = build_canonical_ir(file_paths, jaxrs_repo)

        for ep in cir.endpoints:
            assert "#" in ep.handler_symbol or "." in ep.handler_symbol, (
                f"handler_symbol looks like simple name: {ep.handler_symbol!r} "
                f"(endpoint: {ep.id!r})"
            )

    def test_project_route_surface_uses_effective_class(self, jaxrs_repo: Path) -> None:
        """project_route_surface entries must use 'effective_class' key (not 'class')."""
        file_paths = find_java_files(jaxrs_repo)
        cir = build_canonical_ir(file_paths, jaxrs_repo)
        routes = project_route_surface(cir)

        for r in routes:
            assert "effective_class" in r, f"route missing 'effective_class': {r}"
            assert "class" not in r, f"route uses 'class' instead of 'effective_class': {r}"

    def test_project_route_surface_uses_symbol_not_handler(self, jaxrs_repo: Path) -> None:
        """project_route_surface entries must use 'symbol' key (not 'handler')."""
        file_paths = find_java_files(jaxrs_repo)
        cir = build_canonical_ir(file_paths, jaxrs_repo)
        routes = project_route_surface(cir)

        for r in routes:
            assert "symbol" in r, f"route missing 'symbol' key: {r}"

    def test_project_endpoint_surface_uses_simple_names(self, jaxrs_repo: Path) -> None:
        """project_endpoint_surface must use simple names for controller/handler (backward compat)."""
        file_paths = find_java_files(jaxrs_repo)
        cir = build_canonical_ir(file_paths, jaxrs_repo)
        eps = project_endpoint_surface(cir)["endpoints"]

        for e in eps:
            ctrl = e["controller"]
            hdlr = e["handler"]
            # Simple name: no dots, no hash
            assert "." not in ctrl, f"controller should be simple name, got: {ctrl!r}"
            assert "#" not in hdlr, f"handler should be simple name, got: {hdlr!r}"

    def test_blast_radius_endpoint_class_field(self, jaxrs_repo: Path) -> None:
        """blast_radius endpoints_affected must use 'class' key (internal format)."""
        file_paths = find_java_files(jaxrs_repo)
        cir = build_canonical_ir(file_paths, jaxrs_repo)

        if not cir.endpoints:
            pytest.skip("No endpoints")

        target = cir.endpoints[0].controller_class
        result = project_blast_radius(cir, target)

        for ep in result.get("endpoints_affected", []):
            # blast_radius internal format uses 'class' (kept for backward compat)
            assert "class" in ep or "method" in ep, (
                f"blast_radius endpoint_affected missing expected fields: {ep}"
            )

    def test_route_security_annotations_key_in_route_surface(self, jaxrs_repo: Path) -> None:
        """Routes with security must use 'security_annotations' key in route_surface."""
        file_paths = find_java_files(jaxrs_repo)
        cir = build_canonical_ir(file_paths, jaxrs_repo)
        routes = project_route_surface(cir)

        secured_routes = [r for r in routes if "security_annotations" in r]
        # JAX-RS repo has @RolesAllowed, @PermitAll, @DenyAll, @Authenticated
        assert len(secured_routes) >= 3, (
            f"Expected ≥3 secured routes, got {len(secured_routes)}. "
            f"Routes: {[r['path'] for r in routes]}"
        )

    def test_no_security_key_in_route_surface(self, jaxrs_repo: Path) -> None:
        """route_surface entries must NOT use 'security' key (only 'security_annotations')."""
        file_paths = find_java_files(jaxrs_repo)
        cir = build_canonical_ir(file_paths, jaxrs_repo)
        routes = project_route_surface(cir)

        for r in routes:
            assert "security" not in r, (
                f"route uses 'security' instead of 'security_annotations': {r['path']!r}"
            )

    def test_endpoint_surface_uses_security_key_not_security_annotations(
        self, jaxrs_repo: Path
    ) -> None:
        """endpoint_surface entries must use 'security' key (not 'security_annotations')."""
        file_paths = find_java_files(jaxrs_repo)
        cir = build_canonical_ir(file_paths, jaxrs_repo)
        eps = project_endpoint_surface(cir)["endpoints"]

        for e in eps:
            assert "security_annotations" not in e, (
                f"endpoint uses 'security_annotations' instead of 'security': "
                f"path={e['path']!r}"
            )

    def test_cir_endpoint_id_stable_across_builds(self, jaxrs_repo: Path) -> None:
        """Endpoint IDs must be identical across two independent builds of the same repo."""
        file_paths = find_java_files(jaxrs_repo)

        cir1 = build_canonical_ir(file_paths, jaxrs_repo)
        cir2 = build_canonical_ir(file_paths, jaxrs_repo)

        ids1 = sorted(ep.id for ep in cir1.endpoints)
        ids2 = sorted(ep.id for ep in cir2.endpoints)
        assert ids1 == ids2, f"Endpoint IDs not stable: {set(ids1) ^ set(ids2)}"


# ---------------------------------------------------------------------------
# E. Security annotation coverage (all policies through canonical extractor)
# ---------------------------------------------------------------------------

class TestSecurityAnnotationCoverage:
    """Verify all security annotation policies reach CIR via single extractor."""

    def test_roles_allowed_in_security_index(self, jaxrs_repo: Path) -> None:
        """@RolesAllowed endpoints must appear in security_index with roles_allowed policy."""
        file_paths = find_java_files(jaxrs_repo)
        cir = build_canonical_ir(file_paths, jaxrs_repo)

        roles_allowed_eps = [
            ep for ep in cir.endpoints
            if ep.security and ep.security.policy == "roles_allowed"
        ]
        assert roles_allowed_eps, "No @RolesAllowed endpoints found in CIR"
        # Verify roles are extracted
        for ep in roles_allowed_eps:
            assert ep.security is not None
            assert ep.security.effective_roles, (
                f"@RolesAllowed endpoint has no roles: {ep.id!r}"
            )

    def test_permit_all_policy_present(self, jaxrs_repo: Path) -> None:
        """@PermitAll endpoint must have policy=permit_all in CIR."""
        file_paths = find_java_files(jaxrs_repo)
        cir = build_canonical_ir(file_paths, jaxrs_repo)

        permit_all = [
            ep for ep in cir.endpoints
            if ep.security and ep.security.policy == "permit_all"
        ]
        assert permit_all, "No @PermitAll endpoint found in CIR"

    def test_deny_all_policy_present(self, jaxrs_repo: Path) -> None:
        """@DenyAll endpoint must have policy=deny_all in CIR."""
        file_paths = find_java_files(jaxrs_repo)
        cir = build_canonical_ir(file_paths, jaxrs_repo)

        deny_all = [
            ep for ep in cir.endpoints
            if ep.security and ep.security.policy == "deny_all"
        ]
        assert deny_all, "No @DenyAll endpoint found in CIR"

    def test_authenticated_policy_present(self, jaxrs_repo: Path) -> None:
        """@Authenticated endpoint must have policy=authenticated in CIR."""
        file_paths = find_java_files(jaxrs_repo)
        cir = build_canonical_ir(file_paths, jaxrs_repo)

        authenticated = [
            ep for ep in cir.endpoints
            if ep.security and ep.security.policy == "authenticated"
        ]
        assert authenticated, "No @Authenticated endpoint found in CIR"

    def test_preauthorize_policy_spring(self, spring_repo: Path) -> None:
        """@PreAuthorize endpoints must have spring_preauthorize policy in CIR."""
        file_paths = find_java_files(spring_repo)
        cir = build_canonical_ir(file_paths, spring_repo)

        preauth = [
            ep for ep in cir.endpoints
            if ep.security and "preauthorize" in ep.security.policy
        ]
        assert preauth, "No @PreAuthorize endpoint found in CIR"
        for ep in preauth:
            assert ep.security is not None
            assert ep.security.expression, (
                f"@PreAuthorize endpoint missing expression: {ep.id!r}"
            )

    def test_secured_policy_spring(self, spring_repo: Path) -> None:
        """@Secured endpoints must have policy=secured with roles in CIR."""
        file_paths = find_java_files(spring_repo)
        cir = build_canonical_ir(file_paths, spring_repo)

        secured = [
            ep for ep in cir.endpoints
            if ep.security and ep.security.policy == "secured"
        ]
        assert secured, "No @Secured endpoint found in CIR"
        for ep in secured:
            assert ep.security is not None
            assert ep.security.effective_roles, (
                f"@Secured endpoint missing roles: {ep.id!r}"
            )

    def test_requires_roles_shiro_via_canonical_extractor(
        self, multi_annotation_repo: Path
    ) -> None:
        """@RequiresRoles must be extracted by canonical extractor into CIR."""
        file_paths = find_java_files(multi_annotation_repo)
        cir = build_canonical_ir(file_paths, multi_annotation_repo)

        shiro_eps = [
            ep for ep in cir.endpoints
            if ep.security and "requiresroles" in ep.security.policy
        ]
        assert shiro_eps, (
            "No @RequiresRoles endpoint found in CIR. "
            "Shiro annotation not handled by canonical extractor."
        )

    def test_openapi_security_requirement_via_canonical_extractor(
        self, multi_annotation_repo: Path
    ) -> None:
        """@SecurityRequirement must be extracted by canonical extractor into CIR."""
        file_paths = find_java_files(multi_annotation_repo)
        cir = build_canonical_ir(file_paths, multi_annotation_repo)

        openapi_eps = [
            ep for ep in cir.endpoints
            if ep.security and ep.security.policy == "openapi_security"
        ]
        assert openapi_eps, (
            "No @SecurityRequirement endpoint found in CIR. "
            "OpenAPI annotation not handled by canonical extractor."
        )

    def test_security_consistency_no_divergence(self, jaxrs_repo: Path) -> None:
        """Security from project_endpoint_surface must match security_index policies."""
        file_paths = find_java_files(jaxrs_repo)
        cir = build_canonical_ir(file_paths, jaxrs_repo)
        eps_out = project_endpoint_surface(cir)

        for ep_dict in eps_out["endpoints"]:
            # Find matching CIR endpoint
            matching = [
                ep for ep in cir.endpoints
                if ep.path == ep_dict["path"] and ep.method == ep_dict["method"]
            ]
            if not matching:
                continue
            cir_ep = matching[0]

            # Security must agree ("none_detected" sentinel in projection ≡ None in CIR)
            proj_policy = (ep_dict.get("security") or {}).get("policy")
            if proj_policy == "none_detected":
                proj_policy = None
            cir_policy = cir_ep.security.policy if cir_ep.security else None
            assert proj_policy == cir_policy, (
                f"Security divergence at {ep_dict['method']} {ep_dict['path']}: "
                f"projected={proj_policy!r} cir={cir_policy!r}"
            )


# ---------------------------------------------------------------------------
# F. CanonicalSecurity model tests
# ---------------------------------------------------------------------------

class TestCanonicalSecurityModel:
    """Unit tests for CanonicalSecurity data model."""

    def test_from_policy_dict_roles_allowed(self) -> None:
        d = {"policy": "roles_allowed", "roles": ["admin", "user"]}
        sec = CanonicalSecurity.from_policy_dict(d)
        assert sec.policy == "roles_allowed"
        assert sec.effective_roles == ["admin", "user"]
        assert sec.source_scope == "method"

    def test_from_policy_dict_preauthorize(self) -> None:
        d = {"policy": "spring_preauthorize", "expression": "hasRole('ADMIN')"}
        sec = CanonicalSecurity.from_policy_dict(d, source_scope="class")
        assert sec.policy == "spring_preauthorize"
        assert sec.expression == "hasRole('ADMIN')"
        assert sec.source_scope == "class"

    def test_from_policy_dict_custom_permission(self) -> None:
        d = {"policy": "custom_permission", "required_permission": "CREATE_USER"}
        sec = CanonicalSecurity.from_policy_dict(d)
        assert sec.required_permission == "CREATE_USER"

    def test_to_dict_omits_empty_fields(self) -> None:
        sec = CanonicalSecurity(policy="permit_all", source_scope="method")
        d = sec.to_dict()
        assert d == {"policy": "permit_all"}
        # source_scope not in to_dict (internal field)
        assert "source_scope" not in d

    def test_to_full_dict_includes_source_scope(self) -> None:
        sec = CanonicalSecurity(policy="deny_all", source_scope="class")
        d = sec.to_full_dict()
        assert d["source_scope"] == "class"

    def test_canonical_endpoint_make_id_deterministic(self) -> None:
        id1 = CanonicalEndpoint.make_id("GET", "/users", "com.example.UserResource", "com.example.UserResource#list")
        id2 = CanonicalEndpoint.make_id("GET", "/users", "com.example.UserResource", "com.example.UserResource#list")
        assert id1 == id2
        assert id1 == "GET:/users:com.example.UserResource:com.example.UserResource#list"

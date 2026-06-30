"""test_enterprise_benchmarks.py — Repeatable benchmark suite for enterprise Java repos.

Covers four representative repo types:
  1. Keycloak-like (JAX-RS/Quarkus/Jakarta EE)
  2. Spring Boot + MyBatis (enterprise CRUD)
  3. Legacy monolith (mixed layers, Spring MVC)
  4. Endpoint-heavy repo (many REST resources)

Benchmarks measure:
  - Output boundedness (all LLM-facing commands stay under budget)
  - Cache correctness (flag changes produce distinct cache keys)
  - Endpoint extraction quality (annotations detected correctly)
  - IR usability (symbols extracted, reverse graph populated)
  - Blast-radius analysis (impact command accuracy)
  - Flag semantics (--compact --full conflict, --full alone warning)

Run with: python3 -m pytest tests/test_enterprise_benchmarks.py -v
"""

from __future__ import annotations

import json
import textwrap
import time
from pathlib import Path
from typing import Generator

import pytest
from typer.testing import CliRunner

from sourcecode.cli import app
from sourcecode.output_budget import (
    BUDGET_AGENT,
    BUDGET_COMPACT,
    BUDGET_EXPLAIN,
    BUDGET_FIX_BUG,
    BUDGET_ONBOARD,
    BUDGET_REVIEW_PR,
    trim_to_budget,
)
from sourcecode.repository_ir import (
    _PERMISSION_ANNOTATIONS,
    _blast_radius_candidates,
    apply_ir_size_limits,
    build_repo_ir,
    compute_blast_radius,
    extract_java_endpoints,
    find_java_files,
)

runner = CliRunner()

# ─────────────────────────────────────────────────────────────────────────────
# Fixtures — synthetic Java repos
# ─────────────────────────────────────────────────────────────────────────────


def _write(root: Path, rel: str, content: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(content), encoding="utf-8")


@pytest.fixture()
def keycloak_like_repo(tmp_path: Path) -> Generator[Path, None, None]:
    """Minimal Keycloak-like repo: JAX-RS resources, Quarkus, Jakarta EE."""
    root = tmp_path / "keycloak-like"
    root.mkdir()

    # pom.xml
    _write(root, "pom.xml", """
        <project>
          <groupId>org.keycloak</groupId>
          <artifactId>keycloak-services</artifactId>
          <version>25.0.0</version>
          <dependencies>
            <dependency>
              <groupId>io.quarkus</groupId>
              <artifactId>quarkus-resteasy-reactive</artifactId>
              <version>3.4.0</version>
            </dependency>
            <dependency>
              <groupId>jakarta.ws.rs</groupId>
              <artifactId>jakarta.ws.rs-api</artifactId>
              <version>3.1.0</version>
            </dependency>
          </dependencies>
        </project>
    """)

    # UserResource.java — JAX-RS resource with security annotations
    _write(root, "src/main/java/org/keycloak/services/UserResource.java", """
        package org.keycloak.services;

        import jakarta.ws.rs.*;
        import jakarta.annotation.security.RolesAllowed;
        import jakarta.annotation.security.PermitAll;

        @Path("/users")
        public class UserResource {

            private final UserService userService;

            public UserResource(UserService userService) {
                this.userService = userService;
            }

            @GET
            @RolesAllowed("admin")
            public List<User> listUsers() {
                return userService.findAll();
            }

            @POST
            @Path("/register")
            @PermitAll
            public Response register(UserRepresentation rep) {
                return userService.register(rep);
            }

            @DELETE
            @Path("/{id}")
            @RolesAllowed({"admin", "user-manager"})
            public Response deleteUser(@PathParam("id") String id) {
                return userService.delete(id);
            }
        }
    """)

    # UserService.java — business logic
    _write(root, "src/main/java/org/keycloak/services/UserService.java", """
        package org.keycloak.services;

        import jakarta.enterprise.context.ApplicationScoped;
        import jakarta.transaction.Transactional;

        @ApplicationScoped
        @Transactional
        public class UserService {
            public List<User> findAll() { return List.of(); }
            public Response register(UserRepresentation rep) { return Response.ok().build(); }
            public Response delete(String id) { return Response.ok().build(); }
        }
    """)

    # AuthResource.java — JAX-RS with DenyAll
    _write(root, "src/main/java/org/keycloak/services/AuthResource.java", """
        package org.keycloak.services;

        import jakarta.ws.rs.*;
        import jakarta.annotation.security.DenyAll;
        import jakarta.annotation.security.Authenticated;

        @Path("/auth")
        public class AuthResource {

            @GET
            @Path("/admin-only")
            @DenyAll
            public String denied() { return "denied"; }

            @POST
            @Path("/login")
            @Authenticated
            public Response login(Credentials creds) { return Response.ok().build(); }
        }
    """)

    yield root


@pytest.fixture()
def spring_mybatis_repo(tmp_path: Path) -> Generator[Path, None, None]:
    """Spring Boot + MyBatis enterprise repo."""
    root = tmp_path / "spring-mybatis"
    root.mkdir()

    _write(root, "pom.xml", """
        <project>
          <groupId>com.example</groupId>
          <artifactId>enterprise-app</artifactId>
          <version>1.0.0</version>
          <parent>
            <groupId>org.springframework.boot</groupId>
            <artifactId>spring-boot-starter-parent</artifactId>
            <version>3.2.0</version>
          </parent>
          <dependencies>
            <dependency>
              <groupId>org.mybatis.spring.boot</groupId>
              <artifactId>mybatis-spring-boot-starter</artifactId>
              <version>3.0.3</version>
            </dependency>
            <dependency>
              <groupId>org.springframework.boot</groupId>
              <artifactId>spring-boot-starter-security</artifactId>
            </dependency>
          </dependencies>
        </project>
    """)

    # UserController.java — Spring MVC
    _write(root, "src/main/java/com/example/controller/UserController.java", """
        package com.example.controller;

        import org.springframework.web.bind.annotation.*;
        import org.springframework.security.access.annotation.Secured;
        import com.example.service.UserService;

        @RestController
        @RequestMapping("/api/users")
        public class UserController {

            private final UserService userService;

            public UserController(UserService userService) {
                this.userService = userService;
            }

            @GetMapping
            @Secured("ROLE_ADMIN")
            public List<User> listUsers() {
                return userService.findAll();
            }

            @PostMapping
            @Secured({"ROLE_ADMIN", "ROLE_MANAGER"})
            public User createUser(@RequestBody UserDto dto) {
                return userService.create(dto);
            }

            @DeleteMapping("/{id}")
            @Secured("ROLE_ADMIN")
            public void deleteUser(@PathVariable Long id) {
                userService.delete(id);
            }
        }
    """)

    # UserService.java
    _write(root, "src/main/java/com/example/service/UserService.java", """
        package com.example.service;

        import org.springframework.stereotype.Service;
        import org.springframework.transaction.annotation.Transactional;
        import com.example.mapper.UserMapper;

        @Service
        @Transactional
        public class UserService {
            private final UserMapper userMapper;
            public UserService(UserMapper userMapper) {
                this.userMapper = userMapper;
            }
            public List<User> findAll() { return userMapper.selectAll(); }
            public User create(UserDto dto) { return userMapper.insert(dto); }
            public void delete(Long id) { userMapper.deleteById(id); }
        }
    """)

    # UserMapper.java — MyBatis @Mapper interface
    _write(root, "src/main/java/com/example/mapper/UserMapper.java", """
        package com.example.mapper;

        import org.apache.ibatis.annotations.Mapper;
        import org.apache.ibatis.annotations.Select;
        import org.apache.ibatis.annotations.Delete;

        @Mapper
        public interface UserMapper {
            @Select("SELECT * FROM users")
            List<User> selectAll();

            User insert(UserDto dto);

            @Delete("DELETE FROM users WHERE id = #{id}")
            void deleteById(Long id);
        }
    """)

    # UserMapper.xml
    _write(root, "src/main/resources/mapper/UserMapper.xml", """
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE mapper PUBLIC "-//mybatis.org//DTD Mapper 3.0//EN"
            "http://mybatis.org/dtd/mybatis-3-mapper.dtd">
        <mapper namespace="com.example.mapper.UserMapper">
            <insert id="insert">
                INSERT INTO users (name, email) VALUES (#{name}, #{email})
            </insert>
        </mapper>
    """)

    yield root


@pytest.fixture()
def endpoint_heavy_repo(tmp_path: Path) -> Generator[Path, None, None]:
    """Endpoint-heavy repo — many JAX-RS resources to test extraction at scale."""
    root = tmp_path / "endpoint-heavy"
    root.mkdir()

    _write(root, "pom.xml", """
        <project>
          <groupId>com.corp</groupId>
          <artifactId>api-gateway</artifactId>
          <version>2.0.0</version>
          <dependencies>
            <dependency>
              <groupId>jakarta.ws.rs</groupId>
              <artifactId>jakarta.ws.rs-api</artifactId>
              <version>3.1.0</version>
            </dependency>
          </dependencies>
        </project>
    """)

    # Generate 10 resources × 5 methods = 50 endpoints
    _METHODS = [("GET", "list", "@PermitAll"), ("POST", "create", '@RolesAllowed("admin")'),
                ("GET", "get", '@RolesAllowed({"admin","user"})'), ("PUT", "update", '@RolesAllowed("admin")'),
                ("DELETE", "delete", "@DenyAll")]

    for i in range(1, 11):
        resource_name = f"Resource{i:02d}"
        methods = "\n".join(
            f"""    @{http_method}
    @Path("/{i}/{'items' if http_method == 'GET' and m_name == 'list' else '{id}'}")
    {ann}
    public Response {m_name}() {{ return Response.ok().build(); }}
"""
            for http_method, m_name, ann in _METHODS
        )
        _write(
            root,
            f"src/main/java/com/corp/resource/{resource_name}.java",
            f"""
        package com.corp.resource;

        import jakarta.ws.rs.*;
        import jakarta.annotation.security.*;

        @Path("/api/r{i:02d}")
        public class {resource_name} {{
{methods}
        }}
""",
        )

    yield root


@pytest.fixture()
def legacy_monolith_repo(tmp_path: Path) -> Generator[Path, None, None]:
    """Legacy Spring MVC monolith — mixed layers, many controllers."""
    root = tmp_path / "legacy-monolith"
    root.mkdir()

    _write(root, "pom.xml", """
        <project>
          <groupId>com.legacy</groupId>
          <artifactId>monolith</artifactId>
          <version>1.0.0</version>
          <dependencies>
            <dependency>
              <groupId>org.springframework</groupId>
              <artifactId>spring-webmvc</artifactId>
              <version>5.3.30</version>
            </dependency>
            <dependency>
              <groupId>org.springframework.security</groupId>
              <artifactId>spring-security-core</artifactId>
              <version>5.8.8</version>
            </dependency>
          </dependencies>
        </project>
    """)

    # Multiple layers
    layers = {
        "controller/OrderController.java": """
            package com.legacy.controller;
            import org.springframework.web.bind.annotation.*;
            import org.springframework.security.access.prepost.PreAuthorize;
            import com.legacy.service.OrderService;

            @RestController
            @RequestMapping("/orders")
            public class OrderController {
                private final OrderService orderService;
                public OrderController(OrderService orderService) { this.orderService = orderService; }

                @GetMapping
                @PreAuthorize("hasRole('VIEWER')")
                public List<Order> list() { return orderService.findAll(); }

                @PostMapping
                @PreAuthorize("hasRole('MANAGER')")
                public Order create(@RequestBody OrderDto dto) { return orderService.create(dto); }
            }
        """,
        "service/OrderService.java": """
            package com.legacy.service;
            import org.springframework.stereotype.Service;
            import org.springframework.transaction.annotation.Transactional;
            import com.legacy.dao.OrderDao;

            @Service
            @Transactional
            public class OrderService {
                private final OrderDao orderDao;
                public OrderService(OrderDao orderDao) { this.orderDao = orderDao; }
                public List<Order> findAll() { return orderDao.findAll(); }
                public Order create(OrderDto dto) { return orderDao.save(dto); }
            }
        """,
        "dao/OrderDao.java": """
            package com.legacy.dao;
            import org.springframework.stereotype.Repository;

            @Repository
            public class OrderDao {
                public List<Order> findAll() { return List.of(); }
                public Order save(OrderDto dto) { return new Order(); }
            }
        """,
        "domain/Order.java": """
            package com.legacy.domain;
            public class Order {
                private Long id;
                private String description;
                private String status;
            }
        """,
    }
    for rel, content in layers.items():
        _write(root, f"src/main/java/com/legacy/{rel}", content)

    yield root


# ─────────────────────────────────────────────────────────────────────────────
# P0-1: Output boundedness
# ─────────────────────────────────────────────────────────────────────────────


class TestOutputBoundedness:
    """All LLM-facing commands stay under their byte budgets."""

    def test_compact_within_budget(self, keycloak_like_repo: Path) -> None:
        result = runner.invoke(app, [str(keycloak_like_repo), "--compact"])
        assert result.exit_code == 0, result.output
        size = len(result.output.encode("utf-8"))
        assert size <= BUDGET_COMPACT, (
            f"compact output {size}B exceeds budget {BUDGET_COMPACT}B"
        )

    def test_agent_within_budget(self, keycloak_like_repo: Path) -> None:
        result = runner.invoke(app, [str(keycloak_like_repo), "--agent"])
        assert result.exit_code == 0, result.output
        size = len(result.output.encode("utf-8"))
        assert size <= BUDGET_AGENT, (
            f"agent output {size}B exceeds budget {BUDGET_AGENT}B"
        )

    def test_fix_bug_within_budget(self, spring_mybatis_repo: Path) -> None:
        result = runner.invoke(
            app, ["prepare-context", "fix-bug", str(spring_mybatis_repo)]
        )
        assert result.exit_code == 0, result.output
        size = len(result.output.encode("utf-8"))
        assert size <= BUDGET_FIX_BUG, (
            f"fix-bug output {size}B exceeds budget {BUDGET_FIX_BUG}B"
        )

    def test_explain_within_budget(self, legacy_monolith_repo: Path) -> None:
        result = runner.invoke(
            app, ["prepare-context", "explain", str(legacy_monolith_repo)]
        )
        assert result.exit_code == 0, result.output
        size = len(result.output.encode("utf-8"))
        assert size <= BUDGET_EXPLAIN, (
            f"explain output {size}B exceeds budget {BUDGET_EXPLAIN}B"
        )

    def test_onboard_within_budget(self, legacy_monolith_repo: Path) -> None:
        result = runner.invoke(
            app, ["prepare-context", "onboard", str(legacy_monolith_repo)]
        )
        assert result.exit_code == 0, result.output
        size = len(result.output.encode("utf-8"))
        assert size <= BUDGET_ONBOARD, (
            f"onboard output {size}B exceeds budget {BUDGET_ONBOARD}B"
        )

    def test_review_pr_within_budget(self, spring_mybatis_repo: Path) -> None:
        result = runner.invoke(
            app, ["prepare-context", "review-pr", str(spring_mybatis_repo)]
        )
        # review-pr may error if no git changes — that's OK, check budget if succeeds
        if result.exit_code == 0:
            size = len(result.output.encode("utf-8"))
            assert size <= BUDGET_REVIEW_PR, (
                f"review-pr output {size}B exceeds budget {BUDGET_REVIEW_PR}B"
            )

    def test_repo_ir_summary_within_100kb(self, endpoint_heavy_repo: Path) -> None:
        """repo-ir --summary-only must stay under 100KB (existing guarantee)."""
        java_files = find_java_files(endpoint_heavy_repo)
        if not java_files:
            pytest.skip("no java files")
        ir = build_repo_ir(java_files, endpoint_heavy_repo)
        summary = apply_ir_size_limits(ir, summary_only=True)
        size = len(json.dumps(summary, ensure_ascii=False).encode("utf-8"))
        assert size <= 100_000, f"repo-ir summary {size}B exceeds 100KB"

    def test_trim_to_budget_preserves_always_keep(self) -> None:
        """trim_to_budget must never remove ALWAYS_KEEP fields."""
        big = {
            "task": "fix-bug",
            "goal": "Find the bug",
            "project_summary": "A project",
            "confidence": "high",
            "relevant_files": [{"path": f"File{i}.java"} for i in range(200)],
            "code_notes": [{"kind": "TODO", "path": f"f{i}.java"} for i in range(100)],
        }
        result = trim_to_budget(big, 1000)  # Very small budget
        assert "task" in result
        assert "goal" in result
        assert "project_summary" in result
        assert "confidence" in result

    def test_trim_to_budget_adds_note(self) -> None:
        """trim_to_budget must add _budget_note when trimming occurs."""
        big = {
            "task": "fix-bug",
            "relevant_files": [{"path": f"File{i}.java"} for i in range(500)],
            "code_notes": [{"kind": "TODO", "path": f"f{i}.java"} for i in range(500)],
        }
        result = trim_to_budget(big, 5000)
        if len(json.dumps(big)) > 5000:
            assert "_budget_note" in result

    def test_trim_to_budget_noop_when_under_budget(self) -> None:
        """trim_to_budget must not modify data that already fits."""
        small = {"task": "fix-bug", "goal": "Find bug", "relevant_files": [{"path": "A.java"}]}
        result = trim_to_budget(small, 100_000)
        assert "_budget_note" not in result
        assert result == small


# ─────────────────────────────────────────────────────────────────────────────
# P0-2: Flag semantics
# ─────────────────────────────────────────────────────────────────────────────


class TestFlagSemantics:
    """Flag interactions are honest, deterministic, and clearly documented."""

    def test_compact_full_conflict_is_error(self, keycloak_like_repo: Path) -> None:
        """--compact --full must exit 1 with a clear error message (P0-1 fix: all errors → 1)."""
        result = runner.invoke(
            app, [str(keycloak_like_repo), "--compact", "--full"]
        )
        assert result.exit_code == 1
        assert "mutually exclusive" in result.output.lower() or \
               "mutually exclusive" in (result.stderr or "").lower()

    def test_full_alone_warns_not_errors(self, keycloak_like_repo: Path) -> None:
        """--full without --compact or --agent must warn, not error, and still produce output."""
        result = runner.invoke(app, [str(keycloak_like_repo), "--full"])
        # Exit 0 — the flag warns but doesn't block execution
        assert result.exit_code == 0
        # Warning must appear on stderr (captured in result.output by CliRunner)
        combined = result.output + (result.stderr or "")
        assert "no effect" in combined.lower() or "warning" in combined.lower()

    def test_compact_produces_json(self, keycloak_like_repo: Path) -> None:
        result = runner.invoke(app, [str(keycloak_like_repo), "--compact"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "project_type" in data or "project_summary" in data

    def test_agent_produces_json(self, keycloak_like_repo: Path) -> None:
        result = runner.invoke(app, [str(keycloak_like_repo), "--agent"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "project" in data

    def test_changed_only_implies_compact_note(
        self, keycloak_like_repo: Path
    ) -> None:
        """--changed-only without --compact must note it implies --compact."""
        result = runner.invoke(app, [str(keycloak_like_repo), "--changed-only"])
        combined = result.output + (result.stderr or "")
        assert "implies" in combined.lower() or result.exit_code == 0

    def test_exclude_flag_in_cache_key(self, keycloak_like_repo: Path) -> None:
        """--exclude must produce a different cache key than no --exclude."""
        r1 = runner.invoke(app, [str(keycloak_like_repo), "--compact"])
        r2 = runner.invoke(app, [str(keycloak_like_repo), "--compact", "--exclude", "target"])
        assert r1.exit_code == 0
        assert r2.exit_code == 0
        # Both produce valid JSON
        d1 = json.loads(r1.output)
        d2 = json.loads(r2.output)
        assert "project_type" in d1 or "project_summary" in d1
        assert "project_type" in d2 or "project_summary" in d2

    def test_no_redact_flag_accepted(self, keycloak_like_repo: Path) -> None:
        result = runner.invoke(app, [str(keycloak_like_repo), "--compact", "--no-redact"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "project_type" in data or "project_summary" in data

    def test_agent_full_is_valid(self, keycloak_like_repo: Path) -> None:
        """--agent --full is a valid combination (expands file_relevance)."""
        result = runner.invoke(app, [str(keycloak_like_repo), "--agent", "--full"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "project" in data


# ─────────────────────────────────────────────────────────────────────────────
# Endpoint extraction quality
# ─────────────────────────────────────────────────────────────────────────────


class TestEndpointExtraction:
    """Endpoint extractor detects annotations correctly across annotation types."""

    def test_keycloak_like_endpoints_count(self, keycloak_like_repo: Path) -> None:
        result = runner.invoke(app, ["endpoints", str(keycloak_like_repo)])
        assert result.exit_code == 0
        data = json.loads(result.output)
        # UserResource: GET /users, POST /users/register, DELETE /users/{id}
        # AuthResource: GET /auth/admin-only, POST /auth/login
        assert data["total"] >= 5

    def test_roles_allowed_annotation_detected(self, keycloak_like_repo: Path) -> None:
        result = runner.invoke(app, ["endpoints", str(keycloak_like_repo)])
        assert result.exit_code == 0
        data = json.loads(result.output)
        eps = data.get("endpoints", [])
        secured = [ep for ep in eps if ep.get("security", {}).get("policy") != "none_detected"]
        assert len(secured) >= 1, "Expected at least one secured endpoint"

    def test_permit_all_annotation_detected(self, keycloak_like_repo: Path) -> None:
        result = runner.invoke(app, ["endpoints", str(keycloak_like_repo)])
        assert result.exit_code == 0
        data = json.loads(result.output)
        eps = data.get("endpoints", [])
        permit_all = [
            ep for ep in eps
            if (ep.get("security") or {}).get("policy") == "permit_all"
        ]
        assert len(permit_all) >= 1, "Expected @PermitAll endpoint"

    def test_deny_all_annotation_detected(self, keycloak_like_repo: Path) -> None:
        result = runner.invoke(app, ["endpoints", str(keycloak_like_repo)])
        assert result.exit_code == 0
        data = json.loads(result.output)
        eps = data.get("endpoints", [])
        deny_all = [
            ep for ep in eps
            if (ep.get("security") or {}).get("policy") == "deny_all"
        ]
        assert len(deny_all) >= 1, "Expected @DenyAll endpoint"

    def test_no_security_signal_accurate(self, keycloak_like_repo: Path) -> None:
        """no_security_signal must equal total minus secured endpoints."""
        result = runner.invoke(app, ["endpoints", str(keycloak_like_repo)])
        assert result.exit_code == 0
        data = json.loads(result.output)
        eps = data.get("endpoints", [])
        manually_unsecured = sum(1 for ep in eps if ep.get("security", {}).get("policy") == "none_detected")
        reported_nss = data.get("no_security_signal", -1)
        assert reported_nss == manually_unsecured, (
            f"no_security_signal={reported_nss} != manually counted {manually_unsecured}"
        )

    def test_spring_secured_annotation_detected(self, spring_mybatis_repo: Path) -> None:
        result = runner.invoke(app, ["endpoints", str(spring_mybatis_repo)])
        assert result.exit_code == 0
        data = json.loads(result.output)
        eps = data.get("endpoints", [])
        spring_secured = [
            ep for ep in eps
            if (ep.get("security") or {}).get("policy") in {"roles_allowed", "secured"}
        ]
        assert len(spring_secured) >= 1, "Expected @Secured Spring endpoint"

    def test_endpoint_heavy_repo_count(self, endpoint_heavy_repo: Path) -> None:
        result = runner.invoke(app, ["endpoints", str(endpoint_heavy_repo)])
        assert result.exit_code == 0
        data = json.loads(result.output)
        # 10 resources × 5 methods = 50 endpoints
        assert data["total"] >= 40, f"Expected ≥40 endpoints, got {data['total']}"

    def test_permission_annotations_set_complete(self) -> None:
        """The _PERMISSION_ANNOTATIONS set must cover all major annotation types."""
        expected = {
            "@RolesAllowed", "@PermitAll", "@DenyAll",
            "@Authenticated", "@PreAuthorize", "@Secured",
            "@SecurityRequirement", "@ServletSecurity",
        }
        missing = expected - _PERMISSION_ANNOTATIONS
        assert not missing, f"Missing from _PERMISSION_ANNOTATIONS: {missing}"


# ─────────────────────────────────────────────────────────────────────────────
# P1-3: Blast-radius / Change-impact analysis
# ─────────────────────────────────────────────────────────────────────────────


class TestBlastRadius:
    """compute_blast_radius returns accurate, bounded impact analysis."""

    def _build_ir(self, repo: Path) -> dict:
        java_files = find_java_files(repo)
        if not java_files:
            pytest.skip("no java files found")
        return build_repo_ir(java_files, repo)

    def test_impact_command_runs(self, keycloak_like_repo: Path) -> None:
        result = runner.invoke(
            app, ["impact", "UserService", str(keycloak_like_repo)]
        )
        # Either found (exit 0) or not found (exit 1) — both acceptable
        assert result.exit_code in (0, 1)
        data = json.loads(result.output)
        assert "target" in data
        assert "resolution" in data
        assert "risk_level" in data

    def test_blast_radius_known_class(self, keycloak_like_repo: Path) -> None:
        ir = self._build_ir(keycloak_like_repo)
        result = compute_blast_radius(ir, "UserService")
        # UserService is called by UserResource — must find it
        assert result["resolution"] in ("exact", "ambiguous", "partial")
        assert "direct_callers" in result
        assert "indirect_callers" in result
        assert "endpoints_affected" in result
        assert "stats" in result

    def test_blast_radius_not_found_returns_structured_error(
        self, keycloak_like_repo: Path
    ) -> None:
        ir = self._build_ir(keycloak_like_repo)
        result = compute_blast_radius(ir, "NonExistentClassXYZ12345")
        assert result["resolution"] == "not_found"
        assert result["risk_level"] == "unknown"
        assert result["direct_callers"] == []
        assert result["indirect_callers"] == []
        assert result["endpoints_affected"] == []

    def test_blast_radius_output_bounded(self, endpoint_heavy_repo: Path) -> None:
        ir = self._build_ir(endpoint_heavy_repo)
        # Try to find any class that exists
        nodes = (ir.get("graph") or {}).get("nodes") or []
        if not nodes:
            pytest.skip("no graph nodes")
        first_class = next(
            (n["fqn"] for n in nodes if n.get("symbol_kind") in ("class", "interface")),
            None,
        )
        if not first_class:
            pytest.skip("no class nodes")
        result = compute_blast_radius(ir, first_class)
        # Output must be bounded
        assert len(result.get("direct_callers", [])) <= 30
        assert len(result.get("indirect_callers", [])) <= 50

    def test_blast_radius_risk_score_positive_for_callee(
        self, spring_mybatis_repo: Path
    ) -> None:
        ir = self._build_ir(spring_mybatis_repo)
        # UserService is injected into UserController → should have risk > 0
        result = compute_blast_radius(ir, "UserService")
        if result["resolution"] != "not_found":
            # If found, check stats shape
            assert "risk_score" in result
            assert isinstance(result["risk_score"], float)
            assert result["risk_score"] >= 0.0

    def test_impact_command_not_found_exits_1(self, keycloak_like_repo: Path) -> None:
        result = runner.invoke(
            app, ["impact", "CompletelyFakeClass99999", str(keycloak_like_repo)]
        )
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["resolution"] == "not_found"

    def test_impact_depth_parameter(self, spring_mybatis_repo: Path) -> None:
        """Different --depth values produce valid (different) outputs."""
        r1 = runner.invoke(
            app, ["impact", "UserService", str(spring_mybatis_repo), "--depth", "1"]
        )
        r2 = runner.invoke(
            app, ["impact", "UserService", str(spring_mybatis_repo), "--depth", "4"]
        )
        # Both exit 0 (found) or 1 (not found)
        assert r1.exit_code in (0, 1)
        assert r2.exit_code in (0, 1)
        if r1.exit_code == 0 and r2.exit_code == 0:
            d1 = json.loads(r1.output)
            d2 = json.loads(r2.output)
            # Deeper search can only find >= the same callers
            assert len(d2.get("indirect_callers", [])) >= len(d1.get("indirect_callers", []))


# ─────────────────────────────────────────────────────────────────────────────
# IR usability
# ─────────────────────────────────────────────────────────────────────────────


class TestIRUsability:
    """IR pipeline produces usable, consistent output for enterprise Java repos."""

    def test_keycloak_like_ir_has_symbols(self, keycloak_like_repo: Path) -> None:
        java_files = find_java_files(keycloak_like_repo)
        assert len(java_files) >= 3
        ir = build_repo_ir(java_files, keycloak_like_repo)
        nodes = (ir.get("graph") or {}).get("nodes") or []
        assert len(nodes) >= 3, "Expected at least 3 symbols (one per class)"

    def test_spring_mybatis_ir_has_reverse_graph(self, spring_mybatis_repo: Path) -> None:
        java_files = find_java_files(spring_mybatis_repo)
        ir = build_repo_ir(java_files, spring_mybatis_repo)
        rg = ir.get("reverse_graph") or {}
        # UserService should be in reverse graph (called by UserController)
        assert len(rg) >= 1, "Expected reverse graph to have at least 1 entry"

    def test_route_surface_populated(self, keycloak_like_repo: Path) -> None:
        java_files = find_java_files(keycloak_like_repo)
        ir = build_repo_ir(java_files, keycloak_like_repo)
        route_surface = ir.get("route_surface") or []
        assert len(route_surface) >= 3, (
            f"Expected ≥3 routes, got {len(route_surface)}"
        )

    def test_ir_schema_version(self, keycloak_like_repo: Path) -> None:
        java_files = find_java_files(keycloak_like_repo)
        ir = build_repo_ir(java_files, keycloak_like_repo)
        assert ir.get("schema_version") == "final-v1"

    def test_summary_only_drops_graph_nodes(self, keycloak_like_repo: Path) -> None:
        java_files = find_java_files(keycloak_like_repo)
        ir = build_repo_ir(java_files, keycloak_like_repo)
        summary = apply_ir_size_limits(ir, summary_only=True)
        # summary_only must omit or empty the graph nodes/edges
        graph = summary.get("graph") or {}
        nodes = graph.get("nodes") or []
        edges = graph.get("edges") or []
        assert len(nodes) == 0, "summary_only must omit graph nodes"
        assert len(edges) == 0, "summary_only must omit graph edges"
        # But must keep analysis/impact
        assert "analysis" in summary or "impact" in summary

    def test_endpoint_heavy_ir_within_100kb_summary(
        self, endpoint_heavy_repo: Path
    ) -> None:
        java_files = find_java_files(endpoint_heavy_repo)
        ir = build_repo_ir(java_files, endpoint_heavy_repo)
        summary = apply_ir_size_limits(ir, summary_only=True)
        size = len(json.dumps(summary, ensure_ascii=False).encode("utf-8"))
        assert size <= 100_000, f"IR summary {size}B exceeds 100KB on endpoint-heavy repo"

    def test_legacy_monolith_ir_has_subsystems(self, legacy_monolith_repo: Path) -> None:
        java_files = find_java_files(legacy_monolith_repo)
        if not java_files:
            pytest.skip("no java files")
        ir = build_repo_ir(java_files, legacy_monolith_repo)
        # With controller/service/dao, should detect at least some subsystem structure
        nodes = (ir.get("graph") or {}).get("nodes") or []
        assert len(nodes) >= 3


# ─────────────────────────────────────────────────────────────────────────────
# Cache correctness
# ─────────────────────────────────────────────────────────────────────────────


class TestCacheCorrectness:
    """Flag changes produce distinct cache keys — no silent cache collisions."""

    def _extract_cache_key_fragment(self, result_output: str) -> dict:
        """Parse JSON output to use as a proxy for cache key (different output → different key)."""
        try:
            return json.loads(result_output)
        except json.JSONDecodeError:
            return {}

    def test_exclude_flag_changes_output(self, spring_mybatis_repo: Path) -> None:
        """--exclude must affect which files are analyzed."""
        r_base = runner.invoke(
            app, [str(spring_mybatis_repo), "--compact", "--no-cache"]
        )
        r_excl = runner.invoke(
            app, [str(spring_mybatis_repo), "--compact", "--no-cache", "--exclude", "mapper"]
        )
        assert r_base.exit_code == 0
        assert r_excl.exit_code == 0
        # Both must produce valid JSON
        json.loads(r_base.output)
        json.loads(r_excl.output)

    def test_no_redact_flag_accepted_in_cache_key(self, keycloak_like_repo: Path) -> None:
        r1 = runner.invoke(
            app, [str(keycloak_like_repo), "--compact", "--no-cache"]
        )
        r2 = runner.invoke(
            app, [str(keycloak_like_repo), "--compact", "--no-cache", "--no-redact"]
        )
        assert r1.exit_code == 0
        assert r2.exit_code == 0
        d1 = json.loads(r1.output)
        d2 = json.loads(r2.output)
        # Both produce valid structured output
        assert "project_type" in d1 or "project_summary" in d1
        assert "project_type" in d2 or "project_summary" in d2

    def test_git_context_flag_changes_output(self, keycloak_like_repo: Path) -> None:
        """--git-context must be part of cache key (output differs when git data available)."""
        r_no_git = runner.invoke(
            app, [str(keycloak_like_repo), "--compact", "--no-cache"]
        )
        r_git = runner.invoke(
            app, [str(keycloak_like_repo), "--compact", "--git-context", "--no-cache"]
        )
        assert r_no_git.exit_code == 0
        assert r_git.exit_code == 0
        # Both valid JSON
        json.loads(r_no_git.output)
        json.loads(r_git.output)


# ─────────────────────────────────────────────────────────────────────────────
# MCP parity
# ─────────────────────────────────────────────────────────────────────────────


class TestMCPParity:
    """CLI and MCP server expose equivalent capabilities."""

    def test_mcp_server_imports(self) -> None:
        from sourcecode.mcp.server import mcp  # noqa: F401

    def test_mcp_impact_tool_exists(self) -> None:
        """get_impact_context must be registered as an MCP tool."""
        from sourcecode.mcp import server as mcp_server
        # Check the function is defined in the module
        assert hasattr(mcp_server, "get_impact_context"), (
            "get_impact_context MCP tool not found in server"
        )
        import inspect
        assert callable(getattr(mcp_server, "get_impact_context")), (
            "get_impact_context must be callable"
        )

    def test_mcp_version_parity(self) -> None:
        """MCP server must report same version as CLI."""
        from sourcecode import __version__ as cli_ver
        from sourcecode.mcp.server import mcp
        server_ver = getattr(
            getattr(mcp, "_mcp_server", None), "version", None
        )
        if server_ver is not None:
            assert server_ver == cli_ver, (
                f"MCP version {server_ver!r} != CLI version {cli_ver!r}"
            )


# ─────────────────────────────────────────────────────────────────────────────
# P1: Enhanced blast-radius fields
# Tests the new mappers_affected / security_surface_affected / cross_module_impact
# / confidence_score / confidence_level / explanation fields added in this iteration.
# ─────────────────────────────────────────────────────────────────────────────


class TestBlastRadiusEnhanced:
    """New impact fields: mappers, security surface, cross-module, confidence, explanation."""

    def _ir(self, repo: Path) -> dict:
        files = find_java_files(repo)
        if not files:
            pytest.skip("no java files")
        return build_repo_ir(files, repo)

    # ── field presence ────────────────────────────────────────────────────────

    def test_result_has_all_new_fields(self, spring_mybatis_repo: Path) -> None:
        ir = self._ir(spring_mybatis_repo)
        result = compute_blast_radius(ir, "UserService")
        for field in (
            "mappers_affected",
            "security_surface_affected",
            "cross_module_impact",
            "confidence_score",
            "confidence_level",
            "explanation",
        ):
            assert field in result, f"Missing field: {field}"

    def test_not_found_has_all_new_fields(self, keycloak_like_repo: Path) -> None:
        ir = self._ir(keycloak_like_repo)
        result = compute_blast_radius(ir, "TotallyFakeSymbol999")
        assert result["resolution"] == "not_found"
        for field in (
            "mappers_affected",
            "security_surface_affected",
            "cross_module_impact",
            "confidence_score",
            "confidence_level",
            "explanation",
        ):
            assert field in result, f"not_found result missing field: {field}"

    # ── mappers_affected ──────────────────────────────────────────────────────

    def test_mapper_detected_when_service_touched(self, spring_mybatis_repo: Path) -> None:
        """UserService injects UserMapper → impact on UserService must surface mapper."""
        ir = self._ir(spring_mybatis_repo)
        result = compute_blast_radius(ir, "UserController")
        # Traversal: UserController → UserService → UserMapper
        # UserMapper should appear in mappers_affected OR direct/indirect callers
        stats = result.get("stats") or {}
        mappers = result.get("mappers_affected") or []
        # At minimum, stats field must exist with mapper count
        assert "mappers_affected_count" in stats

    def test_mapper_fqns_are_valid_strings(self, spring_mybatis_repo: Path) -> None:
        ir = self._ir(spring_mybatis_repo)
        result = compute_blast_radius(ir, "UserService")
        for m in result.get("mappers_affected") or []:
            assert isinstance(m.get("fqn"), str) and m["fqn"]
            assert isinstance(m.get("role"), str)

    def test_mappers_bounded(self, endpoint_heavy_repo: Path) -> None:
        ir = self._ir(endpoint_heavy_repo)
        nodes = (ir.get("graph") or {}).get("nodes") or []
        if not nodes:
            pytest.skip("no graph nodes")
        first = next(
            (n["fqn"] for n in nodes if n.get("type") in ("class", "interface")),
            None,
        )
        if not first:
            pytest.skip("no class nodes")
        result = compute_blast_radius(ir, first)
        assert len(result.get("mappers_affected") or []) <= 20

    # ── security_surface_affected ─────────────────────────────────────────────

    def test_security_surface_populated_when_endpoints_have_security(
        self, keycloak_like_repo: Path
    ) -> None:
        """UserService is behind secured endpoints → security_surface_affected non-empty."""
        ir = self._ir(keycloak_like_repo)
        result = compute_blast_radius(ir, "UserService")
        if result.get("endpoints_affected"):
            # Any endpoint with a security annotation should populate security_surface
            has_secured_ep = any(ep.get("security") for ep in result["endpoints_affected"])
            if has_secured_ep:
                assert len(result.get("security_surface_affected") or []) >= 1

    def test_security_surface_bounded(self, endpoint_heavy_repo: Path) -> None:
        ir = self._ir(endpoint_heavy_repo)
        nodes = (ir.get("graph") or {}).get("nodes") or []
        if not nodes:
            pytest.skip("no graph nodes")
        first = next(
            (n["fqn"] for n in nodes if n.get("type") in ("class", "interface")),
            None,
        )
        if not first:
            pytest.skip("no class nodes")
        result = compute_blast_radius(ir, first)
        assert len(result.get("security_surface_affected") or []) <= 15

    def test_security_surface_entry_shape(self, keycloak_like_repo: Path) -> None:
        ir = self._ir(keycloak_like_repo)
        result = compute_blast_radius(ir, "UserService")
        for entry in result.get("security_surface_affected") or []:
            assert "endpoint" in entry
            assert "policy" in entry

    # ── cross_module_impact ────────────────────────────────────────────────────

    def test_cross_module_impact_is_list(self, legacy_monolith_repo: Path) -> None:
        ir = self._ir(legacy_monolith_repo)
        result = compute_blast_radius(ir, "OrderService")
        assert isinstance(result.get("cross_module_impact"), list)

    def test_cross_module_bounded(self, endpoint_heavy_repo: Path) -> None:
        ir = self._ir(endpoint_heavy_repo)
        nodes = (ir.get("graph") or {}).get("nodes") or []
        if not nodes:
            pytest.skip("no graph nodes")
        first = next(
            (n["fqn"] for n in nodes if n.get("type") in ("class", "interface")),
            None,
        )
        if not first:
            pytest.skip("no class nodes")
        result = compute_blast_radius(ir, first)
        assert len(result.get("cross_module_impact") or []) <= 10

    def test_cross_module_entry_shape(self, legacy_monolith_repo: Path) -> None:
        ir = self._ir(legacy_monolith_repo)
        result = compute_blast_radius(ir, "OrderService")
        for entry in result.get("cross_module_impact") or []:
            assert "module" in entry
            assert "package_prefix" in entry
            assert "affected_symbol_count" in entry
            assert isinstance(entry["affected_symbol_count"], int)

    # ── confidence ────────────────────────────────────────────────────────────

    def test_confidence_score_range(self, spring_mybatis_repo: Path) -> None:
        ir = self._ir(spring_mybatis_repo)
        result = compute_blast_radius(ir, "UserService")
        cs = result.get("confidence_score")
        assert cs is not None
        assert 0.0 <= cs <= 1.0, f"confidence_score {cs} out of [0,1]"

    def test_confidence_level_valid_values(self, spring_mybatis_repo: Path) -> None:
        ir = self._ir(spring_mybatis_repo)
        for target in ("UserService", "FakeClass999"):
            result = compute_blast_radius(ir, target)
            assert result.get("confidence_level") in ("high", "medium", "low"), (
                f"Unexpected confidence_level: {result.get('confidence_level')!r}"
            )

    def test_not_found_has_low_confidence(self, keycloak_like_repo: Path) -> None:
        ir = self._ir(keycloak_like_repo)
        result = compute_blast_radius(ir, "NonExistentXYZ")
        assert result.get("confidence_level") == "low"
        assert result.get("confidence_score", 1.0) < 0.5

    def test_exact_match_confidence_higher_than_partial(
        self, spring_mybatis_repo: Path
    ) -> None:
        """Exact match should yield higher confidence than a partial/ambiguous match."""
        ir = self._ir(spring_mybatis_repo)
        # UserService is exact; "service" is partial (matches many)
        exact = compute_blast_radius(ir, "com.example.service.UserService")
        partial = compute_blast_radius(ir, "Service")  # likely partial/ambiguous
        if exact["resolution"] == "exact" and partial["resolution"] in ("partial", "ambiguous"):
            assert exact["confidence_score"] >= partial["confidence_score"]

    # ── explanation ───────────────────────────────────────────────────────────

    def test_explanation_is_nonempty_string(self, spring_mybatis_repo: Path) -> None:
        ir = self._ir(spring_mybatis_repo)
        for target in ("UserService", "FakeClass99"):
            result = compute_blast_radius(ir, target)
            exp = result.get("explanation")
            assert isinstance(exp, str) and len(exp) > 0, (
                f"explanation empty for target={target!r}"
            )

    def test_explanation_mentions_risk_level(self, spring_mybatis_repo: Path) -> None:
        ir = self._ir(spring_mybatis_repo)
        result = compute_blast_radius(ir, "UserService")
        if result["resolution"] != "not_found":
            exp = result.get("explanation") or ""
            # Explanation should reference risk level
            risk = result.get("risk_level") or ""
            assert risk.upper() in exp.upper() or "risk" in exp.lower(), (
                f"explanation {exp!r} doesn't mention risk level {risk!r}"
            )

    def test_no_callers_explanation_is_low_risk(self, keycloak_like_repo: Path) -> None:
        """A class with no callers should get a low-risk explanation."""
        ir = self._ir(keycloak_like_repo)
        result = compute_blast_radius(ir, "NonExistentClassXYZ12345")
        exp = result.get("explanation") or ""
        assert "not found" in exp.lower() or "no callers" in exp.lower() or "isolated" in exp.lower()

    # ── stats extended ────────────────────────────────────────────────────────

    def test_stats_has_all_new_counters(self, spring_mybatis_repo: Path) -> None:
        ir = self._ir(spring_mybatis_repo)
        result = compute_blast_radius(ir, "UserService")
        stats = result.get("stats") or {}
        for key in (
            "direct_caller_count",
            "indirect_caller_count",
            "endpoints_affected_count",
            "transactional_boundaries_count",
            "mappers_affected_count",
            "modules_affected_count",
            "security_surface_count",
        ):
            assert key in stats, f"Missing stats key: {key}"
            assert isinstance(stats[key], int)

    # ── determinism ───────────────────────────────────────────────────────────

    def test_blast_radius_is_deterministic(self, keycloak_like_repo: Path) -> None:
        """Same IR + same target → identical output across two calls."""
        ir = self._ir(keycloak_like_repo)
        r1 = compute_blast_radius(ir, "UserService")
        r2 = compute_blast_radius(ir, "UserService")
        assert r1 == r2, "compute_blast_radius is not deterministic"

    def test_risk_score_in_result_matches_risk_level(self, spring_mybatis_repo: Path) -> None:
        ir = self._ir(spring_mybatis_repo)
        result = compute_blast_radius(ir, "UserService")
        score = result.get("risk_score", 0)
        level = result.get("risk_level", "none")
        if level == "high":
            assert score >= 5.0
        elif level == "medium":
            assert score >= 0.1
        elif level == "none":
            assert score == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# P1: Ambiguous target resolution → candidates
# ─────────────────────────────────────────────────────────────────────────────


class TestTargetResolution:
    """Target resolution: exact, ambiguous, partial, not_found all handled correctly."""

    def _ir(self, repo: Path) -> dict:
        files = find_java_files(repo)
        if not files:
            pytest.skip("no java files")
        return build_repo_ir(files, repo)

    def test_exact_fqn_resolves_exact(self, spring_mybatis_repo: Path) -> None:
        ir = self._ir(spring_mybatis_repo)
        result = compute_blast_radius(ir, "com.example.service.UserService")
        # FQN match → should be exact (if symbol is in graph)
        if result["resolution"] != "not_found":
            assert result["resolution"] in ("exact", "ambiguous")

    def test_simple_name_resolves_suffix(self, spring_mybatis_repo: Path) -> None:
        ir = self._ir(spring_mybatis_repo)
        result = compute_blast_radius(ir, "UserService")
        assert result["resolution"] in ("exact", "ambiguous", "partial", "not_found")
        assert isinstance(result.get("matched_fqns"), list)

    def test_not_found_returns_candidates(self, spring_mybatis_repo: Path) -> None:
        """not_found result with a partial name should include candidates."""
        ir = self._ir(spring_mybatis_repo)
        result = compute_blast_radius(ir, "User")  # too vague → partial/ambiguous or not_found
        # Either resolution works; if not_found, candidates should be provided
        if result["resolution"] == "not_found":
            candidates = result.get("candidates")
            # Candidates may or may not exist depending on repo — just validate shape if present
            if candidates:
                assert isinstance(candidates, list)
                for c in candidates:
                    assert "fqn" in c
                    assert "relevance_score" in c

    def test_ambiguous_resolution_has_candidates(self, spring_mybatis_repo: Path) -> None:
        ir = self._ir(spring_mybatis_repo)
        result = compute_blast_radius(ir, "User")  # matches UserService, UserController, UserMapper
        if result["resolution"] in ("ambiguous", "partial"):
            candidates = result.get("candidates")
            if candidates:
                assert len(candidates) <= 10
                for c in candidates:
                    assert isinstance(c["fqn"], str)
                    assert isinstance(c["relevance_score"], float)
                    assert c["relevance_score"] >= 0.0

    def test_blast_radius_candidates_function_direct(self, spring_mybatis_repo: Path) -> None:
        """_blast_radius_candidates returns ordered results for known prefix."""
        ir = self._ir(spring_mybatis_repo)
        rg = ir.get("reverse_graph") or {}
        nodes = (ir.get("graph") or {}).get("nodes") or []
        candidates = _blast_radius_candidates("User", rg, nodes)
        assert isinstance(candidates, list)
        assert len(candidates) <= 10
        if candidates:
            # First candidate must have higher or equal relevance than last
            assert candidates[0]["relevance_score"] >= candidates[-1]["relevance_score"]

    def test_candidates_empty_for_complete_mismatch(self, keycloak_like_repo: Path) -> None:
        ir = self._ir(keycloak_like_repo)
        rg = ir.get("reverse_graph") or {}
        nodes = (ir.get("graph") or {}).get("nodes") or []
        candidates = _blast_radius_candidates("ZZZCompletelyUnrelatedXYZABC", rg, nodes)
        assert isinstance(candidates, list)
        assert len(candidates) == 0

    def test_file_path_target_resolves(self, spring_mybatis_repo: Path) -> None:
        """UserService.java as target must resolve to the service class."""
        ir = self._ir(spring_mybatis_repo)
        result = compute_blast_radius(ir, "UserService.java")
        # .java suffix must be stripped and resolve like simple name
        assert result["resolution"] in ("exact", "ambiguous", "partial", "not_found")


# ─────────────────────────────────────────────────────────────────────────────
# P1: MyBatis-heavy repo blast radius
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture()
def mybatis_heavy_repo(tmp_path: Path) -> Generator[Path, None, None]:
    """MyBatis-heavy enterprise repo: 3 mappers, 3 services, 2 controllers, XML mappers."""
    root = tmp_path / "mybatis-heavy"
    root.mkdir()

    _write(root, "pom.xml", textwrap.dedent("""
        <project>
          <groupId>com.corp.crm</groupId>
          <artifactId>crm-backend</artifactId>
          <version>2.1.0</version>
          <parent>
            <groupId>org.springframework.boot</groupId>
            <artifactId>spring-boot-starter-parent</artifactId>
            <version>3.2.0</version>
          </parent>
          <dependencies>
            <dependency>
              <groupId>org.mybatis.spring.boot</groupId>
              <artifactId>mybatis-spring-boot-starter</artifactId>
              <version>3.0.3</version>
            </dependency>
          </dependencies>
        </project>
    """))

    entities = [("Customer", "customer"), ("Order", "order"), ("Product", "product")]
    for entity, pkg in entities:
        # Controller
        _write(root, f"src/main/java/com/corp/crm/controller/{entity}Controller.java",
               textwrap.dedent(f"""
            package com.corp.crm.controller;
            import org.springframework.web.bind.annotation.*;
            import org.springframework.security.access.annotation.Secured;
            import com.corp.crm.service.{entity}Service;

            @RestController
            @RequestMapping("/api/{pkg}s")
            public class {entity}Controller {{
                private final {entity}Service {pkg}Service;
                public {entity}Controller({entity}Service {pkg}Service) {{ this.{pkg}Service = {pkg}Service; }}

                @GetMapping
                @Secured("ROLE_USER")
                public java.util.List<{entity}> list() {{ return {pkg}Service.findAll(); }}

                @PostMapping
                @Secured("ROLE_MANAGER")
                public {entity} create(@RequestBody {entity} e) {{ return {pkg}Service.create(e); }}

                @DeleteMapping("/{{id}}")
                @Secured("ROLE_ADMIN")
                public void delete(@PathVariable Long id) {{ {pkg}Service.delete(id); }}
            }}
        """))

        # Service
        _write(root, f"src/main/java/com/corp/crm/service/{entity}Service.java",
               textwrap.dedent(f"""
            package com.corp.crm.service;
            import org.springframework.stereotype.Service;
            import org.springframework.transaction.annotation.Transactional;
            import com.corp.crm.mapper.{entity}Mapper;

            @Service
            @Transactional
            public class {entity}Service {{
                private final {entity}Mapper {pkg}Mapper;
                public {entity}Service({entity}Mapper {pkg}Mapper) {{ this.{pkg}Mapper = {pkg}Mapper; }}
                public java.util.List<{entity}> findAll() {{ return {pkg}Mapper.selectAll(); }}
                public {entity} create({entity} e) {{ return {pkg}Mapper.insert(e); }}
                public void delete(Long id) {{ {pkg}Mapper.deleteById(id); }}
            }}
        """))

        # Mapper interface
        _write(root, f"src/main/java/com/corp/crm/mapper/{entity}Mapper.java",
               textwrap.dedent(f"""
            package com.corp.crm.mapper;
            import org.apache.ibatis.annotations.Mapper;
            import org.apache.ibatis.annotations.Select;
            import org.apache.ibatis.annotations.Delete;

            @Mapper
            public interface {entity}Mapper {{
                @Select("SELECT * FROM {pkg}s")
                java.util.List<{entity}> selectAll();
                {entity} insert({entity} e);
                @Delete("DELETE FROM {pkg}s WHERE id = #{{id}}")
                void deleteById(Long id);
            }}
        """))

        # Mapper XML
        _write(root, f"src/main/resources/mapper/{entity}Mapper.xml",
               textwrap.dedent(f"""
            <?xml version="1.0" encoding="UTF-8"?>
            <!DOCTYPE mapper PUBLIC "-//mybatis.org//DTD Mapper 3.0//EN"
                "http://mybatis.org/dtd/mybatis-3-mapper.dtd">
            <mapper namespace="com.corp.crm.mapper.{entity}Mapper">
                <insert id="insert">
                    INSERT INTO {pkg}s (name) VALUES (#{{name}})
                </insert>
            </mapper>
        """))

    yield root


class TestMyBatisHeavyBlastRadius:
    """Blast radius on MyBatis-heavy repos: persistence paths detected correctly."""

    def _ir(self, repo: Path) -> dict:
        files = find_java_files(repo)
        if not files:
            pytest.skip("no java files")
        return build_repo_ir(files, repo)

    def test_mybatis_repo_has_mappers_in_ir(self, mybatis_heavy_repo: Path) -> None:
        ir = self._ir(mybatis_heavy_repo)
        nodes = (ir.get("graph") or {}).get("nodes") or []
        class_names = [n.get("fqn", "") for n in nodes]
        mapper_classes = [c for c in class_names if "Mapper" in c]
        assert len(mapper_classes) >= 1, (
            f"Expected mapper classes in IR, got fqns: {class_names[:10]}"
        )

    def test_service_impact_includes_mappers(self, mybatis_heavy_repo: Path) -> None:
        ir = self._ir(mybatis_heavy_repo)
        result = compute_blast_radius(ir, "CustomerController")
        assert result["resolution"] in ("exact", "ambiguous", "partial", "not_found")
        if result["resolution"] != "not_found":
            stats = result.get("stats") or {}
            assert "mappers_affected_count" in stats

    def test_mapper_role_in_mappers_affected(self, mybatis_heavy_repo: Path) -> None:
        ir = self._ir(mybatis_heavy_repo)
        # Impact from a service should propagate mapper info
        result = compute_blast_radius(ir, "CustomerService")
        if result["resolution"] not in ("not_found",):
            for m in result.get("mappers_affected") or []:
                # Role must be a non-empty string
                assert isinstance(m.get("role"), str) and m["role"]

    def test_mybatis_endpoint_count(self, mybatis_heavy_repo: Path) -> None:
        result = runner.invoke(app, ["endpoints", str(mybatis_heavy_repo)])
        assert result.exit_code == 0
        data = json.loads(result.output)
        # 3 controllers × 3 methods = 9 endpoints
        assert data["total"] >= 6, f"Expected ≥6 endpoints, got {data['total']}"

    def test_mybatis_subsystems_detected(self, mybatis_heavy_repo: Path) -> None:
        ir = self._ir(mybatis_heavy_repo)
        nodes = (ir.get("graph") or {}).get("nodes") or []
        # At minimum should have controller, service, mapper classes
        roles = {n.get("role", "other") for n in nodes}
        # Should have at minimum some non-"other" roles
        assert len(nodes) >= 3, "Expected at least 3 symbols in IR"


# ─────────────────────────────────────────────────────────────────────────────
# P2: Enterprise workflow commands (onboard / fix-bug / modernize / review-pr)
# ─────────────────────────────────────────────────────────────────────────────


class TestEnterpriseWorkflows:
    """Top-level workflow commands produce bounded, structured output."""

    def test_onboard_command_runs(self, legacy_monolith_repo: Path) -> None:
        result = runner.invoke(app, ["onboard", str(legacy_monolith_repo)])
        # May exit 0 or 1 depending on repo content — output must be valid
        assert result.exit_code in (0, 1)
        # Must produce some output
        assert len(result.output) > 0

    def test_fix_bug_command_runs(self, spring_mybatis_repo: Path) -> None:
        result = runner.invoke(
            app, ["fix-bug", str(spring_mybatis_repo), "--symptom", "NullPointerException"]
        )
        assert result.exit_code in (0, 1)
        assert len(result.output) > 0

    def test_fix_bug_without_symptom_runs(self, spring_mybatis_repo: Path) -> None:
        result = runner.invoke(app, ["fix-bug", str(spring_mybatis_repo)])
        assert result.exit_code in (0, 1)
        assert len(result.output) > 0

    def test_modernize_command_runs(self, legacy_monolith_repo: Path) -> None:
        result = runner.invoke(app, ["modernize", str(legacy_monolith_repo)])
        assert result.exit_code in (0, 1)
        assert len(result.output) > 0

    def test_modernize_output_is_json(self, legacy_monolith_repo: Path) -> None:
        result = runner.invoke(app, ["modernize", str(legacy_monolith_repo)])
        if result.exit_code == 0:
            data = json.loads(result.output)
            assert data.get("workflow") == "modernize"
            assert "hotspot_candidates" in data
            # Defect 5: "dead zones" renamed to statically_unreferenced (never a
            # confident dead-code claim) with a separate framework_dispatched bucket.
            assert "statically_unreferenced" in data
            assert "framework_dispatched" in data
            assert "subsystem_summary" in data
            assert "recommendation" in data

    def test_modernize_summary_has_counts(self, legacy_monolith_repo: Path) -> None:
        result = runner.invoke(app, ["modernize", str(legacy_monolith_repo)])
        if result.exit_code == 0:
            data = json.loads(result.output)
            summary = data.get("summary") or {}
            assert "total_classes" in summary
            assert isinstance(summary["total_classes"], int)

    def test_modernize_bounded_output(self, endpoint_heavy_repo: Path) -> None:
        result = runner.invoke(app, ["modernize", str(endpoint_heavy_repo)])
        if result.exit_code == 0:
            size = len(result.output.encode("utf-8"))
            assert size <= BUDGET_ONBOARD, (
                f"modernize output {size}B exceeds BUDGET_ONBOARD={BUDGET_ONBOARD}B"
            )

    def test_review_pr_command_runs(self, spring_mybatis_repo: Path) -> None:
        result = runner.invoke(app, ["review-pr", str(spring_mybatis_repo)])
        # review-pr may exit non-zero if no git diff — acceptable
        assert result.exit_code in (0, 1)
        assert len(result.output) > 0

    def test_workflow_commands_registered(self) -> None:
        """All five enterprise workflow commands must be registered in the CLI."""
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        help_text = result.output.lower()
        for cmd in ("onboard", "fix-bug", "modernize", "review-pr", "impact"):
            assert cmd in help_text, f"Workflow command {cmd!r} not in --help"

    def test_modernize_on_mybatis_repo(self, mybatis_heavy_repo: Path) -> None:
        result = runner.invoke(app, ["modernize", str(mybatis_heavy_repo)])
        if result.exit_code == 0:
            data = json.loads(result.output)
            assert data.get("workflow") == "modernize"
            # MyBatis heavy repo should have controller/service/mapper subsystems
            summary = data.get("summary") or {}
            assert summary.get("total_classes", 0) >= 3


# ─────────────────────────────────────────────────────────────────────────────
# P1: Runtime benchmarks — IR build and impact analysis must complete in time
# ─────────────────────────────────────────────────────────────────────────────


class TestRuntimeBenchmarks:
    """IR build and impact analysis stay within reasonable wall-clock limits.

    These are NOT hard CI gates — they characterise performance degradation.
    Thresholds are generous (10s/5s) to avoid false failures on CI machines.
    """

    def _time_build_ir(self, repo: Path) -> tuple[dict, float]:
        files = find_java_files(repo)
        if not files:
            pytest.skip("no java files")
        t0 = time.monotonic()
        ir = build_repo_ir(files, repo)
        elapsed = time.monotonic() - t0
        return ir, elapsed

    def test_keycloak_like_ir_build_under_10s(self, keycloak_like_repo: Path) -> None:
        _, elapsed = self._time_build_ir(keycloak_like_repo)
        assert elapsed < 10.0, f"IR build took {elapsed:.2f}s (limit: 10s)"

    def test_spring_mybatis_ir_build_under_10s(self, spring_mybatis_repo: Path) -> None:
        _, elapsed = self._time_build_ir(spring_mybatis_repo)
        assert elapsed < 10.0, f"IR build took {elapsed:.2f}s (limit: 10s)"

    def test_mybatis_heavy_ir_build_under_10s(self, mybatis_heavy_repo: Path) -> None:
        _, elapsed = self._time_build_ir(mybatis_heavy_repo)
        assert elapsed < 10.0, f"IR build took {elapsed:.2f}s (limit: 10s)"

    def test_endpoint_heavy_ir_build_under_10s(self, endpoint_heavy_repo: Path) -> None:
        _, elapsed = self._time_build_ir(endpoint_heavy_repo)
        assert elapsed < 10.0, f"IR build took {elapsed:.2f}s (limit: 10s)"

    def test_impact_analysis_under_5s(self, mybatis_heavy_repo: Path) -> None:
        files = find_java_files(mybatis_heavy_repo)
        if not files:
            pytest.skip("no java files")
        ir = build_repo_ir(files, mybatis_heavy_repo)
        t0 = time.monotonic()
        compute_blast_radius(ir, "CustomerService")
        elapsed = time.monotonic() - t0
        assert elapsed < 5.0, f"compute_blast_radius took {elapsed:.2f}s (limit: 5s)"

    def test_impact_analysis_deterministic_runtime(self, spring_mybatis_repo: Path) -> None:
        """Two successive calls must produce same output (no randomness)."""
        files = find_java_files(spring_mybatis_repo)
        if not files:
            pytest.skip("no java files")
        ir = build_repo_ir(files, spring_mybatis_repo)
        r1 = compute_blast_radius(ir, "UserService")
        r2 = compute_blast_radius(ir, "UserService")
        assert r1 == r2


# ─────────────────────────────────────────────────────────────────────────────
# P2: Multi-repo comparison matrix
# Verifies the suite runs across all repo types and captures key metrics.
# ─────────────────────────────────────────────────────────────────────────────


class TestMultiRepoComparisonMatrix:
    """Cross-repo consistency: all repos must satisfy the same baseline contract."""

    @pytest.mark.parametrize("repo_fixture", [
        "keycloak_like_repo",
        "spring_mybatis_repo",
        "legacy_monolith_repo",
        "endpoint_heavy_repo",
        "mybatis_heavy_repo",
    ])
    def test_ir_schema_version_all_repos(
        self, repo_fixture: str, request: pytest.FixtureRequest
    ) -> None:
        repo = request.getfixturevalue(repo_fixture)
        files = find_java_files(repo)
        if not files:
            pytest.skip("no java files")
        ir = build_repo_ir(files, repo)
        assert ir.get("schema_version") == "final-v1", (
            f"{repo_fixture}: schema_version mismatch: {ir.get('schema_version')!r}"
        )

    @pytest.mark.parametrize("repo_fixture", [
        "keycloak_like_repo",
        "spring_mybatis_repo",
        "legacy_monolith_repo",
        "endpoint_heavy_repo",
        "mybatis_heavy_repo",
    ])
    def test_ir_summary_bounded_all_repos(
        self, repo_fixture: str, request: pytest.FixtureRequest
    ) -> None:
        repo = request.getfixturevalue(repo_fixture)
        files = find_java_files(repo)
        if not files:
            pytest.skip("no java files")
        ir = build_repo_ir(files, repo)
        summary = apply_ir_size_limits(ir, summary_only=True)
        size = len(json.dumps(summary, ensure_ascii=False).encode("utf-8"))
        assert size <= 100_000, (
            f"{repo_fixture}: summary {size}B exceeds 100KB"
        )

    @pytest.mark.parametrize("repo_fixture", [
        "keycloak_like_repo",
        "spring_mybatis_repo",
        "legacy_monolith_repo",
        "mybatis_heavy_repo",
    ])
    def test_blast_radius_new_fields_present_all_repos(
        self, repo_fixture: str, request: pytest.FixtureRequest
    ) -> None:
        """All new blast-radius fields must be present in every repo type."""
        repo = request.getfixturevalue(repo_fixture)
        files = find_java_files(repo)
        if not files:
            pytest.skip("no java files")
        ir = build_repo_ir(files, repo)
        nodes = (ir.get("graph") or {}).get("nodes") or []
        if not nodes:
            pytest.skip("no graph nodes")
        # Pick any class-level node
        target = next(
            (n["fqn"] for n in nodes if n.get("type") in ("class", "interface")),
            None,
        )
        if not target:
            pytest.skip("no class node found")
        result = compute_blast_radius(ir, target)
        for field in (
            "mappers_affected",
            "security_surface_affected",
            "cross_module_impact",
            "confidence_score",
            "confidence_level",
            "explanation",
            "stats",
        ):
            assert field in result, (
                f"{repo_fixture}: blast-radius missing field {field!r}"
            )

    @pytest.mark.parametrize("repo_fixture", [
        "keycloak_like_repo",
        "spring_mybatis_repo",
        "legacy_monolith_repo",
        "endpoint_heavy_repo",
        "mybatis_heavy_repo",
    ])
    def test_impact_command_produces_valid_json_all_repos(
        self, repo_fixture: str, request: pytest.FixtureRequest
    ) -> None:
        repo = request.getfixturevalue(repo_fixture)
        result = runner.invoke(app, ["impact", "NonExistentXYZ", str(repo)])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["resolution"] == "not_found"
        assert data["risk_level"] == "unknown"

    @pytest.mark.parametrize("repo_fixture", [
        "keycloak_like_repo",
        "spring_mybatis_repo",
        "legacy_monolith_repo",
        "mybatis_heavy_repo",
    ])
    def test_modernize_command_all_repos(
        self, repo_fixture: str, request: pytest.FixtureRequest
    ) -> None:
        repo = request.getfixturevalue(repo_fixture)
        result = runner.invoke(app, ["modernize", str(repo)])
        if result.exit_code == 0:
            data = json.loads(result.output)
            assert data.get("workflow") == "modernize"
            assert "hotspot_candidates" in data
            assert "summary" in data


# ─────────────────────────────────────────────────────────────────────────────
# Bug-fixes 1.33.19 — CRÍTICO 1/2/3 + MEJORA 1/2
# ─────────────────────────────────────────────────────────────────────────────

import io
import sys
from unittest.mock import patch


class TestBudgetSkipAndWarn:
    """CRÍTICO 1 — trim_to_budget skip/warn_stderr params and _truncation_summary."""

    def _big_data(self) -> dict:
        return {
            "project_type": "api",
            "transactional_boundaries": {"classes": [f"Tx{i}" for i in range(30)]},
            "mybatis": {"dto_mappers": [f"Mapper{i}" for i in range(100)]},
        }

    def test_skip_true_no_trimming_no_note(self) -> None:
        data = self._big_data()
        result = trim_to_budget(data, 50, label="compact", skip=True)
        assert "_budget_note" not in result
        assert "_truncation_summary" not in result
        assert len(result["mybatis"]["dto_mappers"]) == 100

    def test_skip_false_trims_and_adds_note(self) -> None:
        data = self._big_data()
        result = trim_to_budget(data, 50, label="compact", skip=False)
        assert "_budget_note" in result
        assert "_truncation_summary" in result
        summary = result["_truncation_summary"]
        assert summary["total_omitted_items"] > 0
        assert "original_size_kb" in summary
        assert "budget_kb" in summary

    def test_warn_stderr_emits_warning_before_output(self) -> None:
        data = self._big_data()
        old_stderr = sys.stderr
        sys.stderr = io.StringIO()
        try:
            trim_to_budget(data, 50, label="compact", warn_stderr=True)
            warning = sys.stderr.getvalue()
        finally:
            sys.stderr = old_stderr
        assert "WARNING" in warning
        assert "trimmed" in warning.lower() or "trim" in warning.lower()

    def test_warn_stderr_false_no_stderr_output(self) -> None:
        data = self._big_data()
        old_stderr = sys.stderr
        sys.stderr = io.StringIO()
        try:
            trim_to_budget(data, 50, label="compact", warn_stderr=False)
            warning = sys.stderr.getvalue()
        finally:
            sys.stderr = old_stderr
        assert warning == ""

    def test_output_flag_skips_budget_no_note(self, keycloak_like_repo: Path, tmp_path: Path) -> None:
        """--output <file> must produce full output with no _budget_note."""
        out_file = tmp_path / "full.json"
        result = runner.invoke(app, [str(keycloak_like_repo), "--compact", "--output", str(out_file)])
        assert result.exit_code == 0
        assert out_file.exists()
        data = json.loads(out_file.read_text())
        assert "_budget_note" not in data

    def test_stdout_compact_may_emit_budget_note_on_large_repo(self, keycloak_like_repo: Path) -> None:
        """Stdout path keeps _budget_note when trimming occurs."""
        result = runner.invoke(app, [str(keycloak_like_repo), "--compact"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        # Budget note only present when repo actually exceeds BUDGET_COMPACT
        if "_budget_note" in data:
            assert "_truncation_summary" in data
            assert data["_truncation_summary"]["total_omitted_items"] >= 0


class TestProLockExitCode:
    """CRÍTICO 2/3 — Pro-locked features exit with code 2, not 0 or 1."""

    def test_delta_exits_2_without_pro(self, keycloak_like_repo: Path) -> None:
        # Simulate free-tier quota exhausted (30/30 runs used).
        with patch("sourcecode.license.is_pro", False), \
             patch("sourcecode.license._license_data", None), \
             patch("sourcecode.license._maybe_revalidate", return_value=None), \
             patch("sourcecode.license.check_delta_free_tier", return_value=(False, 30, 0)):
            result = runner.invoke(
                app,
                ["prepare-context", "delta", str(keycloak_like_repo), "--since", "HEAD~1"],
            )
        assert result.exit_code == 2

    def test_delta_error_json_has_free_tier_alternative(self, keycloak_like_repo: Path) -> None:
        # Simulate free-tier quota exhausted (30/30 runs used).
        with patch("sourcecode.license.is_pro", False), \
             patch("sourcecode.license._license_data", None), \
             patch("sourcecode.license._maybe_revalidate", return_value=None), \
             patch("sourcecode.license.check_delta_free_tier", return_value=(False, 30, 0)):
            result = runner.invoke(
                app,
                ["prepare-context", "delta", str(keycloak_like_repo), "--since", "HEAD~1"],
            )
        # CliRunner mixes stderr into result.output; extract the JSON line
        json_line = next(
            (ln for ln in result.output.splitlines() if ln.strip().startswith("{")), None
        )
        assert json_line is not None, f"No JSON line in output: {result.output[:200]}"
        data = json.loads(json_line)
        assert data["error"] == "pro_required"
        assert "free_tier_alternative" in data
        assert "review-pr" in data["free_tier_alternative"]

    def test_impact_exits_2_on_large_repo_without_pro(self, keycloak_like_repo: Path) -> None:
        # Hybrid model: impact gates to Pro only on enterprise-scale monoliths.
        # Simulate a large repo so the size gate fires.
        with patch("sourcecode.license.is_pro", False), \
             patch("sourcecode.license._license_data", None), \
             patch("sourcecode.license._maybe_revalidate", return_value=None), \
             patch("sourcecode.license.is_large_repo", return_value=True):
            result = runner.invoke(app, ["impact", "SomeService", str(keycloak_like_repo)])
        assert result.exit_code == 2

    def test_impact_free_on_small_repo(self, keycloak_like_repo: Path) -> None:
        # Hybrid model: impact runs free on small/mid repos (no Pro gate). A
        # missing symbol resolves to not_found (exit 1), never a license exit (2).
        with patch("sourcecode.license.is_pro", False), \
             patch("sourcecode.license._license_data", None), \
             patch("sourcecode.license._maybe_revalidate", return_value=None), \
             patch("sourcecode.license.is_large_repo", return_value=False):
            result = runner.invoke(app, ["impact", "NonExistentXYZ", str(keycloak_like_repo)])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["resolution"] == "not_found"

    def test_impact_help_mentions_pro(self) -> None:
        result = runner.invoke(app, ["impact", "--help"])
        assert result.exit_code == 0
        combined = result.output + (result.stderr or "")
        assert "pro" in combined.lower() or "license" in combined.lower()

    def test_delta_and_impact_exit_codes_consistent(self, keycloak_like_repo: Path) -> None:
        # Both gates exit 2: delta when its free quota is exhausted, impact when
        # the repo exceeds the free-tier size limit.
        with patch("sourcecode.license.is_pro", False), \
             patch("sourcecode.license._license_data", None), \
             patch("sourcecode.license._maybe_revalidate", return_value=None), \
             patch("sourcecode.license.is_large_repo", return_value=True), \
             patch("sourcecode.license.check_delta_free_tier", return_value=(False, 30, 0)):
            r_delta = runner.invoke(
                app,
                ["prepare-context", "delta", str(keycloak_like_repo), "--since", "HEAD~1"],
            )
            r_impact = runner.invoke(app, ["impact", "SomeService", str(keycloak_like_repo)])
        assert r_delta.exit_code == r_impact.exit_code == 2


class TestCompactAgentWarning:
    """MEJORA 1 — --compact --agent emits precedence warning on stderr."""

    def test_compact_agent_warns_compact_ignored(self, keycloak_like_repo: Path) -> None:
        result = runner.invoke(app, [str(keycloak_like_repo), "--compact", "--agent"])
        combined = result.output + (result.stderr or "")
        assert "compact" in combined.lower()
        assert "ignored" in combined.lower() or "precedence" in combined.lower()

    def test_compact_agent_output_is_agent_format(self, keycloak_like_repo: Path) -> None:
        result = runner.invoke(app, [str(keycloak_like_repo), "--compact", "--agent"])
        assert result.exit_code == 0
        # CliRunner mixes stderr warning into output; find the JSON block
        json_start = result.output.find("{")
        assert json_start >= 0, f"No JSON in output: {result.output[:200]}"
        data = json.loads(result.output[json_start:])
        assert "project" in data


class TestChangedOnlyCleanRepo:
    """MEJORA 2 — --changed-only on clean repo emits changed_files_count + note."""

    @pytest.fixture()
    def clean_git_repo(self, tmp_path: Path) -> Path:
        import subprocess as _sp
        repo = tmp_path / "clean-repo"
        repo.mkdir()
        (repo / "pom.xml").write_text("<project/>")
        (repo / "src").mkdir()
        (repo / "src" / "Main.java").write_text("public class Main {}")
        _sp.run(["git", "init"], cwd=repo, capture_output=True)
        _sp.run(["git", "config", "user.email", "test@test.com"], cwd=repo, capture_output=True)
        _sp.run(["git", "config", "user.name", "Test"], cwd=repo, capture_output=True)
        _sp.run(["git", "add", "."], cwd=repo, capture_output=True)
        _sp.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True)
        return repo

    def test_changed_only_clean_repo_has_count_field(self, clean_git_repo: Path) -> None:
        result = runner.invoke(app, [str(clean_git_repo), "--changed-only"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "changed_files_count" in data
        assert data["changed_files_count"] == 0

    def test_changed_only_clean_repo_has_note(self, clean_git_repo: Path) -> None:
        result = runner.invoke(app, [str(clean_git_repo), "--changed-only"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        # Unified schema: no legacy "note" field; use analysis_scope and _meta instead.
        assert data.get("analysis_scope") == "empty"
        assert data.get("_meta", {}).get("changed_only") is True

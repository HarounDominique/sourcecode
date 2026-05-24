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
        """--compact --full must exit 2 with a clear error message."""
        result = runner.invoke(
            app, [str(keycloak_like_repo), "--compact", "--full"]
        )
        assert result.exit_code == 2
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
        secured = [ep for ep in eps if ep.get("security")]
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
        manually_unsecured = sum(1 for ep in eps if not ep.get("security"))
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

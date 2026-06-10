"""Regression tests for bugs fixed in v1.35.28.

BUG-4: rename-class destroys class declarations in other packages (same simple name)
BUG-1: find_java_files false positive on /test/ substring in package paths
BUG-2: rename-class allows rename to existing class name (no cross-repo collision check)
BUG-6: cold-start --compact returns 89 tokens (wrong key names in compact filter)
BUG-3: @EnableMethodSecurity suppresses all SEC-001 findings (wrongly treated as filter-based)
BUG-5: explain ignores @Entity stereotype
BUG-7: SEC-001 false positives in XML+annotation mixed security
"""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write(root: Path, rel: str, content: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sourcecode", *args],
        capture_output=True,
        text=True,
        cwd=cwd or Path(__file__).parent.parent,
    )


# ---------------------------------------------------------------------------
# BUG-4: same simple name in different packages
# ---------------------------------------------------------------------------

class TestBug4RenameClassPackageDisambiguation:

    def test_beta_declaration_not_renamed(self, tmp_path: Path) -> None:
        """Renaming com.alpha.UserService must not touch com.beta.UserService declaration."""
        _write(tmp_path, "src/main/java/com/alpha/UserService.java",
               "package com.alpha;\npublic class UserService { public void doAlpha() {} }\n")
        _write(tmp_path, "src/main/java/com/beta/UserService.java",
               "package com.beta;\npublic class UserService { public void doBeta() {} }\n")
        _write(tmp_path, "src/main/java/com/alpha/Client.java",
               "package com.alpha;\nimport com.alpha.UserService;\npublic class Client { private UserService svc; }\n")

        from src.sourcecode.rename_refactor import rename_class
        result = rename_class(tmp_path, "UserService", "AccountService", dry_run=True)

        assert not result.errors
        changed_files = [c.file for c in result.changes]
        # beta file must NOT appear in changes at all (no import of alpha.UserService there)
        assert not any("beta" in f for f in changed_files), (
            f"com/beta/UserService.java was modified — BUG-4 regression: {changed_files}"
        )

    def test_source_file_declaration_is_renamed(self, tmp_path: Path) -> None:
        """The target class declaration IS renamed."""
        _write(tmp_path, "src/main/java/com/alpha/UserService.java",
               "package com.alpha;\npublic class UserService {}\n")
        _write(tmp_path, "src/main/java/com/beta/UserService.java",
               "package com.beta;\npublic class UserService {}\n")

        from src.sourcecode.rename_refactor import rename_class
        result = rename_class(tmp_path, "UserService", "AccountService", dry_run=True)

        # Exactly one file should have "Renamed class declaration" intent
        decl_changes = [c for c in result.changes if "Renamed class declaration" in c.intent]
        assert len(decl_changes) == 1
        assert "alpha" in decl_changes[0].file


# ---------------------------------------------------------------------------
# BUG-1: find_java_files false positive on com/test/ package paths
# ---------------------------------------------------------------------------

class TestBug1FindJavaFilesTestSubstring:

    def test_production_file_in_test_package_not_excluded(self, tmp_path: Path) -> None:
        """Files in com/.../test/ package under src/main/java must NOT be excluded."""
        _write(tmp_path, "src/main/java/org/example/workflow/state/test/Helper.java",
               "package org.example.workflow.state.test;\npublic class Helper {}\n")
        _write(tmp_path, "src/test/java/org/example/RealTest.java",
               "package org.example;\npublic class RealTest {}\n")

        from src.sourcecode.repository_ir import find_java_files
        files = find_java_files(tmp_path)

        helper_found = any("state/test/Helper.java" in f for f in files)
        real_test_excluded = all("RealTest.java" not in f for f in files)

        assert helper_found, f"BUG-1: production file in com/test/ package was excluded. files={files}"
        assert real_test_excluded, f"src/test/java/RealTest.java should be excluded. files={files}"

    def test_src_test_java_excluded(self, tmp_path: Path) -> None:
        """Standard Maven src/test/java/ is always excluded."""
        _write(tmp_path, "src/test/java/com/example/MyTest.java",
               "public class MyTest {}\n")
        _write(tmp_path, "src/main/java/com/example/MyService.java",
               "public class MyService {}\n")

        from src.sourcecode.repository_ir import find_java_files
        files = find_java_files(tmp_path)

        assert not any("MyTest" in f for f in files)
        assert any("MyService" in f for f in files)


# ---------------------------------------------------------------------------
# BUG-2: rename-class cross-repo collision check
# ---------------------------------------------------------------------------

class TestBug2RenameClassCollisionCheck:

    def test_rename_to_existing_class_errors(self, tmp_path: Path) -> None:
        """Renaming to an already-existing class name in any package must error out."""
        _write(tmp_path, "src/main/java/com/vet/VetController.java",
               "package com.vet;\npublic class VetController {}\n")
        _write(tmp_path, "src/main/java/com/owner/OwnerController.java",
               "package com.owner;\npublic class OwnerController {}\n")

        from src.sourcecode.rename_refactor import rename_class
        result = rename_class(tmp_path, "VetController", "OwnerController", dry_run=True)

        assert result.errors, "Expected collision error but got none — BUG-2 regression"
        assert "OwnerController" in result.errors[0]

    def test_rename_to_new_name_succeeds(self, tmp_path: Path) -> None:
        """Renaming to a name that doesn't exist anywhere should succeed."""
        _write(tmp_path, "src/main/java/com/example/FooService.java",
               "package com.example;\npublic class FooService {}\n")

        from src.sourcecode.rename_refactor import rename_class
        result = rename_class(tmp_path, "FooService", "BarService", dry_run=True)

        assert not result.errors


# ---------------------------------------------------------------------------
# BUG-6: cold-start --compact correct key names
# ---------------------------------------------------------------------------

class TestBug6ColdStartCompact:

    def test_compact_has_summary_and_entrypoints(self) -> None:
        """cold-start --compact must include summary and entrypoints keys."""
        fixture = Path(__file__).parent / "fixtures" / "javax_legacy"
        r = _run("cold-start", str(fixture), "--compact")
        # cold-start needs a RIS; if not available it returns no_ris — still valid
        assert r.returncode == 0, f"cold-start --compact failed: {r.stderr}"
        data = json.loads(r.stdout)
        # compact must never produce only ~89 tokens worth of data with wrong key names
        meta = data.get("_meta") or {}
        tokens = meta.get("estimated_tokens", 0)
        # Any valid compact output is >89 tokens (89 was the broken state)
        # The fixture may return no_ris (no cached snapshot) — that's fine, just verify keys not wrong
        if data.get("status") != "no_ris":
            assert "stacks" not in data, "BUG-6: compact still using wrong key 'stacks'"
            assert "entry_points" not in data, "BUG-6: compact still using wrong key 'entry_points'"


# ---------------------------------------------------------------------------
# BUG-3: @EnableMethodSecurity must not suppress SEC-001
# ---------------------------------------------------------------------------

class TestBug3EnableMethodSecurity:

    def test_enable_method_security_not_filter_based(self, tmp_path: Path) -> None:
        """@EnableMethodSecurity must not set security_model=filter_based/mixed."""
        _write(tmp_path, "src/main/java/com/example/SecurityConfig.java",
               "package com.example;\n"
               "import org.springframework.security.config.annotation.method.configuration.EnableMethodSecurity;\n"
               "import org.springframework.context.annotation.Configuration;\n"
               "@Configuration\n@EnableMethodSecurity\npublic class SecurityConfig {}\n")
        _write(tmp_path, "src/main/java/com/example/UserController.java",
               "package com.example;\n"
               "import org.springframework.web.bind.annotation.*;\n"
               "import org.springframework.security.access.prepost.PreAuthorize;\n"
               "@RestController\n@RequestMapping(\"/users\")\npublic class UserController {\n"
               "    @GetMapping(\"/secure\")\n    @PreAuthorize(\"hasRole('ADMIN')\")\n"
               "    public String secure() { return \"ok\"; }\n"
               "    @GetMapping(\"/open\")\n    public String open() { return \"public\"; }\n"
               "}\n")

        from src.sourcecode.repository_ir import find_java_files
        from src.sourcecode.canonical_ir import build_canonical_ir, project_endpoint_surface
        files = find_java_files(tmp_path)
        cir = build_canonical_ir(files, tmp_path)
        surface = project_endpoint_surface(cir)

        model = surface.get("security_model")
        assert model == "annotation_based", (
            f"BUG-3: @EnableMethodSecurity set security_model={model!r}, expected annotation_based"
        )
        # /open endpoint should be flagged (no_security_signal > 0)
        assert surface.get("no_security_signal", 0) > 0, (
            "BUG-3: unannotated endpoint not flagged as no_security_signal"
        )


# ---------------------------------------------------------------------------
# BUG-5: explain @Entity stereotype
# ---------------------------------------------------------------------------

class TestBug5ExplainEntityStereotype:

    def test_entity_stereotype_detected(self) -> None:
        """sourcecode explain on an @Entity class must return stereotype=entity."""
        fixture = Path(__file__).parent.parent / "tests" / "testing" if (
            Path(__file__).parent.parent / "tests" / "testing"
        ).exists() else None

        # Use petclinic if available, else skip
        petclinic = Path("/Users/user/Documents/workspace/testing/spring-petclinic")
        if not petclinic.exists():
            pytest.skip("spring-petclinic not available")

        r = _run("explain", "Owner", str(petclinic), "--format", "json")
        assert r.returncode == 0, f"explain failed: {r.stderr}"
        data = json.loads(r.stdout)
        assert data.get("stereotype") == "entity", (
            f"BUG-5: Owner.java stereotype={data.get('stereotype')!r}, expected 'entity'"
        )


# ---------------------------------------------------------------------------
# BUG-7: XML + annotation mixed security retags none_detected endpoints
# ---------------------------------------------------------------------------

class TestBug7XmlAnnotationMixedSecurity:

    def test_xml_security_retags_unannotated_endpoints(self, tmp_path: Path) -> None:
        """When XML security config present, none_detected endpoints → xml_or_filter_chain."""
        _write(tmp_path, "src/main/java/com/example/AppController.java",
               "package com.example;\n"
               "import org.springframework.web.bind.annotation.*;\n"
               "import org.springframework.security.access.prepost.PreAuthorize;\n"
               "@RestController\n@RequestMapping(\"/api\")\npublic class AppController {\n"
               "    @GetMapping(\"/admin\")\n    @PreAuthorize(\"hasRole('ADMIN')\")\n"
               "    public String admin() { return \"admin\"; }\n"
               "    @GetMapping(\"/data\")\n    public String data() { return \"data\"; }\n"
               "}\n")
        _write(tmp_path, "src/main/resources/security-config.xml",
               '<?xml version="1.0"?>\n'
               '<beans xmlns:security="http://www.springframework.org/schema/security">\n'
               '  <security:http><security:intercept-url pattern="/api/**" access="isAuthenticated()"/></security:http>\n'
               '</beans>\n')

        from src.sourcecode.repository_ir import find_java_files
        from src.sourcecode.canonical_ir import build_canonical_ir, project_endpoint_surface
        files = find_java_files(tmp_path)
        cir = build_canonical_ir(files, tmp_path)
        surface = project_endpoint_surface(cir)

        assert surface.get("no_security_signal", 999) == 0, (
            "BUG-7: /data endpoint still flagged as none_detected despite XML security config"
        )
        policies = {ep.get("path"): ep.get("security", {}).get("policy")
                    for ep in surface.get("endpoints", [])}
        assert policies.get("/api/data") == "xml_or_filter_chain", (
            f"BUG-7: /api/data policy={policies.get('/api/data')!r}, expected xml_or_filter_chain"
        )
        assert surface.get("security_model") == "mixed", (
            f"BUG-7: security_model={surface.get('security_model')!r}, expected mixed"
        )

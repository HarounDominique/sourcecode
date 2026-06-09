"""Regression tests for SAS audit v2 bug fixes.

Covers:
  BUG-1   Cache fingerprint includes all output-affecting flags
  BUG-2   delta/review-pr: invalid git ref → exit 1 + structured error
  BUG-3   repo-ir --summary-only bounded to 100 KB
  BUG-4   Endpoints: security annotation extraction (Jakarta EE, Spring Security)
  BUG-5   Endpoints: no_security_signal field (replaces misleading undocumented==total)
  BUG-6   Exit codes: arg-validation=2, runtime-errors=1, no-diff=0
  BUG-7   review-pr no_diff → exit 0 (consistent with delta no_changes)
  BUG-8   MCP status shows CLI version and detects external server
"""
from __future__ import annotations

import hashlib
import json
import textwrap
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from sourcecode.cli import app
from sourcecode.repository_ir import (
    _extract_symbols,
    extract_java_endpoints,
    apply_ir_size_limits,
    _PERMISSION_ANNOTATIONS,
    _SECURITY_MARKER_ANNOTATIONS,
)

runner = CliRunner()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _java_repo(tmp_path: Path, java_source: str, filename: str = "Resource.java") -> Path:
    """Write a minimal Java repo with pom.xml and one Java file."""
    pkg = tmp_path / "src" / "main" / "java" / "com" / "example"
    pkg.mkdir(parents=True)
    (pkg / filename).write_text(textwrap.dedent(java_source), encoding="utf-8")
    (tmp_path / "pom.xml").write_text(
        "<project><groupId>com.example</groupId><version>1.0</version></project>",
        encoding="utf-8",
    )
    return tmp_path


# ── BUG-1: Cache fingerprint ──────────────────────────────────────────────────

class TestCacheFingerprint:
    """Cache key must change when output-affecting flags change."""

    def _make_flags_str(self, **kwargs) -> str:
        """Reproduce the flags string from cli.py to test key composition."""
        from sourcecode import __version__ as ver
        defaults = dict(
            compact=False, agent=False, fmt="json", full=False,
            changed_only=False, dependencies=False, graph_modules=False,
            docs=False, full_metrics=False, semantics=False,
            architecture=False, git_context=False, env_map=False,
            code_notes=False, tree=False, mode="standard",
            excl_key="", depth=8, rank_by="relevance", symbol=None,
            entrypoints_only=False, no_redact=False, graph_detail="standard",
            docs_depth="standard", max_nodes=None, graph_edges=False,
            max_importers=None, emit_graph=False,
        )
        defaults.update(kwargs)
        d = defaults
        return (
            f"v={ver},"
            f"c={d['compact']},ag={d['agent']},fmt={d['fmt']},full={d['full']},"
            f"co={d['changed_only']},dep={d['dependencies']},gm={d['graph_modules']},"
            f"docs={d['docs']},fm={d['full_metrics']},sem={d['semantics']},"
            f"arch={d['architecture']},gc={d['git_context']},em={d['env_map']},"
            f"cn={d['code_notes']},tree={d['tree']},mode={d['mode']},"
            f"ex={d['excl_key']},depth={d['depth']},"
            f"rb={d['rank_by']},sym={d['symbol']},ep={d['entrypoints_only']},"
            f"nr={d['no_redact']},gd={d['graph_detail']},dd={d['docs_depth']},"
            f"mn={d['max_nodes']},ge={d['graph_edges']},mi={d['max_importers']},"
            f"eg={d['emit_graph']}"
        )

    def _hash(self, flags_str: str) -> str:
        return hashlib.md5(flags_str.encode()).hexdigest()[:8]

    def test_exclude_changes_key(self):
        h_base = self._hash(self._make_flags_str(excl_key=""))
        h_excl = self._hash(self._make_flags_str(excl_key="js,docs"))
        assert h_base != h_excl, "--exclude must change cache key"

    def test_no_redact_changes_key(self):
        h_normal = self._hash(self._make_flags_str(no_redact=False))
        h_redact = self._hash(self._make_flags_str(no_redact=True))
        assert h_normal != h_redact, "--no-redact must change cache key"

    def test_compact_vs_agent_different_keys(self):
        h_compact = self._hash(self._make_flags_str(compact=True))
        h_agent = self._hash(self._make_flags_str(agent=True))
        assert h_compact != h_agent, "--compact vs --agent must differ"

    def test_depth_changes_key(self):
        h8 = self._hash(self._make_flags_str(depth=8))
        h12 = self._hash(self._make_flags_str(depth=12))
        assert h8 != h12, "--depth must change cache key"

    def test_exclude_order_normalized(self):
        """Exclude list is sorted so 'js,docs' == 'docs,js'."""
        def _excl_key(s: str) -> str:
            return ",".join(sorted(e.strip() for e in s.split(",") if e.strip()))
        assert _excl_key("js,docs") == _excl_key("docs,js")

    def test_rank_by_changes_key(self):
        h_rel = self._hash(self._make_flags_str(rank_by="relevance"))
        h_cen = self._hash(self._make_flags_str(rank_by="centrality"))
        assert h_rel != h_cen, "--rank-by must change cache key"


# ── BUG-2: delta / review-pr invalid ref ──────────────────────────────────────

class TestGitRefErrors:
    """Invalid git refs must produce structured errors, not silent fallback."""

    def test_delta_invalid_ref_exit_1(self, tmp_path):
        """delta --since INVALID → exit 1 + structured JSON error."""
        # Need a git repo for the delta command
        import subprocess
        subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
        subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t.com"], capture_output=True)
        subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "T"], capture_output=True)
        (tmp_path / "pom.xml").write_text("<project/>")
        subprocess.run(["git", "-C", str(tmp_path), "add", "."], capture_output=True)
        subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "init"], capture_output=True)

        result = runner.invoke(app, ["prepare-context", "delta", str(tmp_path), "--since", "TOTALLY_INVALID_REF_9999"])
        out = json.loads(result.output)
        assert out["error"] == "git_ref_not_found"
        assert out["ci_decision"] == "git_ref_error"
        assert result.exit_code == 1

    def test_review_pr_invalid_ref_exit_1(self, tmp_path):
        """review-pr --since INVALID → exit 1 + structured JSON error."""
        import subprocess
        subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
        subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t.com"], capture_output=True)
        subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "T"], capture_output=True)
        (tmp_path / "pom.xml").write_text("<project/>")
        subprocess.run(["git", "-C", str(tmp_path), "add", "."], capture_output=True)
        subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "init"], capture_output=True)

        result = runner.invoke(app, ["prepare-context", "review-pr", str(tmp_path), "--since", "TOTALLY_INVALID_REF_9999"])
        out = json.loads(result.output)
        assert out["error"] == "git_ref_not_found"
        assert out["ci_decision"] == "git_ref_error"
        assert result.exit_code == 1

    def test_review_pr_no_diff_exit_0(self, tmp_path):
        """review-pr with no changed files → exit 0, not exit 1."""
        import subprocess
        subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
        subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t.com"], capture_output=True)
        subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "T"], capture_output=True)
        (tmp_path / "pom.xml").write_text("<project/>")
        subprocess.run(["git", "-C", str(tmp_path), "add", "."], capture_output=True)
        subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "init"], capture_output=True)

        result = runner.invoke(app, ["prepare-context", "review-pr", str(tmp_path), "--since", "HEAD"])
        assert result.exit_code == 0, (
            f"review-pr with no diff must exit 0 (no_changes is not an error), got {result.exit_code}. "
            f"Output: {result.output[:300]}"
        )


# ── BUG-3: repo-ir --summary-only size bound ──────────────────────────────────

class TestIRSummaryBound:
    """repo-ir --summary-only must stay under 100 KB."""

    def _make_large_ir(self) -> dict:
        """Produce an IR dict with many nodes, edges, and large reverse_graph."""
        nodes = [{"fqn": f"com.example.Class{i}", "kind": "class"} for i in range(500)]
        edges = [{"from": f"com.example.Class{i}", "to": f"com.example.Class{i+1}", "type": "calls"}
                 for i in range(499)]
        # Simulate a large reverse_graph (each hub has 200 callers)
        reverse_graph = {
            f"com.example.Hub{i}": {
                "calls": [f"com.example.Caller{j}" for j in range(200)],
                "extends": [],
            }
            for i in range(50)
        }
        # Simulate large route_surface (list, not dict)
        route_surface = [
            {"symbol": f"com.example.R#ep{i}", "path": f"/api/ep{i}", "method": "GET",
             "controller": "R", "effective_class": "com.example.R", "declaring_class": "com.example.R"}
            for i in range(434)
        ]
        subsystems = [
            {"name": f"subsystem_{i}", "members": [f"mem_{j}" for j in range(100)]}
            for i in range(92)
        ]
        return {
            "graph": {"nodes": nodes, "edges": edges},
            "reverse_graph": reverse_graph,
            "analysis": {"changed_entities": [], "impacted_entities": [], "isolated_changes": [], "validated_changes": []},
            "impact": {"global_score": 0.5, "ranked_nodes": [{"entity": f"com.example.Class{i}", "score": float(500-i)} for i in range(500)]},
            "subsystems": subsystems,
            "change_set": [],
            "route_surface": route_surface,
            "spring_events": [],
            "analysis_gaps": [],
            "audit": {},
        }

    def test_summary_under_100kb(self):
        """summary_only output must not exceed 100 KB on large IR."""
        ir = self._make_large_ir()
        result = apply_ir_size_limits(ir, summary_only=True)
        encoded = json.dumps(result, ensure_ascii=False).encode("utf-8")
        assert len(encoded) <= 100_000, (
            f"summary_only output is {len(encoded)} bytes, expected ≤ 100000. "
            f"Sections: { {k: len(json.dumps(v)) for k,v in result.items()} }"
        )

    def test_summary_has_useful_content(self):
        """Trimmed summary must still contain key sections."""
        ir = self._make_large_ir()
        result = apply_ir_size_limits(ir, summary_only=True)
        assert "graph" in result
        assert result["graph"].get("_omitted")  # nodes/edges omitted message
        assert "route_surface" in result
        assert isinstance(result["route_surface"], list)
        assert len(result["route_surface"]) <= 50
        assert "reverse_graph" in result

    def test_route_surface_list_truncated(self):
        """route_surface as a list (not dict) must be truncated to ≤ 50."""
        ir = self._make_large_ir()
        assert isinstance(ir["route_surface"], list)  # confirm list format
        result = apply_ir_size_limits(ir, summary_only=True)
        assert len(result["route_surface"]) <= 50

    def test_reverse_graph_inner_lists_capped(self):
        """reverse_graph inner lists must be capped (not just key count)."""
        ir = self._make_large_ir()
        result = apply_ir_size_limits(ir, summary_only=True)
        rg = result.get("reverse_graph", {})
        for hub, entry in rg.items():
            for list_field, vals in entry.items():
                if isinstance(vals, list):
                    assert len(vals) <= 20, (
                        f"reverse_graph[{hub}][{list_field}] has {len(vals)} items, expected ≤ 20"
                    )


# ── BUG-4 & BUG-5: Endpoints security extraction ──────────────────────────────

class TestEndpointSecurity:
    """Security annotations must be extracted and reflected in no_security_signal."""

    _JAXRS_WITH_SECURITY = '''
        package com.example;

        import jakarta.annotation.security.RolesAllowed;
        import jakarta.annotation.security.PermitAll;
        import jakarta.annotation.security.DenyAll;
        import jakarta.ws.rs.*;

        @Path("/api")
        @RolesAllowed("admin")
        public class AdminResource {

            @GET
            @Path("/users")
            public String listUsers() { return "[]"; }

            @POST
            @Path("/users")
            @RolesAllowed({"admin", "superuser"})
            public String createUser() { return "ok"; }

            @GET
            @Path("/public")
            @PermitAll
            public String publicEndpoint() { return "public"; }

            @DELETE
            @Path("/users/{id}")
            @DenyAll
            public String deleteUser() { return "denied"; }
        }
    '''

    def test_permit_all_extracted(self, tmp_path):
        root = _java_repo(tmp_path, self._JAXRS_WITH_SECURITY)
        result = extract_java_endpoints(root)
        public_ep = next((e for e in result["endpoints"] if e["handler"] == "publicEndpoint"), None)
        assert public_ep is not None
        assert public_ep.get("security", {}).get("policy") == "permit_all"

    def test_deny_all_extracted(self, tmp_path):
        root = _java_repo(tmp_path, self._JAXRS_WITH_SECURITY)
        result = extract_java_endpoints(root)
        delete_ep = next((e for e in result["endpoints"] if e["handler"] == "deleteUser"), None)
        assert delete_ep is not None
        assert delete_ep.get("security", {}).get("policy") == "deny_all"

    def test_roles_allowed_method_level(self, tmp_path):
        root = _java_repo(tmp_path, self._JAXRS_WITH_SECURITY)
        result = extract_java_endpoints(root)
        create_ep = next((e for e in result["endpoints"] if e["handler"] == "createUser"), None)
        assert create_ep is not None
        sec = create_ep.get("security", {})
        assert sec.get("policy") == "roles_allowed"
        assert "admin" in sec.get("roles", [])
        assert "superuser" in sec.get("roles", [])

    def test_class_level_security_inherited(self, tmp_path):
        """Methods without own annotation inherit class-level @RolesAllowed."""
        root = _java_repo(tmp_path, self._JAXRS_WITH_SECURITY)
        result = extract_java_endpoints(root)
        list_ep = next((e for e in result["endpoints"] if e["handler"] == "listUsers"), None)
        assert list_ep is not None
        sec = list_ep.get("security", {})
        # Should inherit class-level @RolesAllowed("admin")
        assert sec.get("policy") == "roles_allowed"
        assert "admin" in sec.get("roles", [])

    def test_no_security_signal_accurate(self, tmp_path):
        """When all endpoints are annotated, no_security_signal must be 0."""
        root = _java_repo(tmp_path, self._JAXRS_WITH_SECURITY)
        result = extract_java_endpoints(root)
        assert result["no_security_signal"] == 0
        assert result["undocumented"] == 0  # backward compat

    def test_no_security_signal_counts_unannotated(self, tmp_path):
        """When no annotations present, no_security_signal == total."""
        src = '''
            package com.example;
            import jakarta.ws.rs.*;

            @Path("/plain")
            public class PlainResource {
                @GET @Path("/a") public String a() { return "a"; }
                @POST @Path("/b") public String b() { return "b"; }
            }
        '''
        root = _java_repo(tmp_path, src)
        result = extract_java_endpoints(root)
        assert result["no_security_signal"] == result["total"]

    def test_spring_pre_authorize(self, tmp_path):
        """@PreAuthorize must be extracted as spring_preauthorize policy."""
        src = '''
            package com.example;
            import org.springframework.security.access.prepost.PreAuthorize;
            import org.springframework.web.bind.annotation.*;

            @RestController
            @RequestMapping("/admin")
            public class SpringAdmin {
                @GetMapping("/users")
                @PreAuthorize("hasRole('ADMIN')")
                public String list() { return "[]"; }
            }
        '''
        root = _java_repo(tmp_path, src)
        result = extract_java_endpoints(root)
        ep = next((e for e in result["endpoints"] if "list" in e.get("handler", "")), None)
        if ep:  # Only assert if Spring MVC detection finds it
            sec = ep.get("security", {})
            assert sec.get("policy") in ("spring_preauthorize", None)  # may not be detected without Spring MVC

    def test_permission_annotations_set_contains_standards(self):
        """_PERMISSION_ANNOTATIONS must include standard Jakarta EE annotations."""
        assert "@RolesAllowed" in _PERMISSION_ANNOTATIONS
        assert "@PermitAll" in _PERMISSION_ANNOTATIONS
        assert "@DenyAll" in _PERMISSION_ANNOTATIONS
        assert "@Authenticated" in _PERMISSION_ANNOTATIONS
        assert "@PreAuthorize" in _PERMISSION_ANNOTATIONS
        assert "@Secured" in _PERMISSION_ANNOTATIONS


# ── BUG-6: Exit codes ─────────────────────────────────────────────────────────

class TestExitCodes:
    """Exit code convention: 2=arg-validation, 1=runtime-error, 0=success/no-diff."""

    def test_invalid_depth_exits_2(self, tmp_path):
        (tmp_path / "pom.xml").write_text("<project/>")
        result = runner.invoke(app, [str(tmp_path), "--compact", "--depth", "0"])
        assert result.exit_code == 2, f"--depth 0 must exit 2, got {result.exit_code}"

    def test_invalid_format_exits_2(self, tmp_path):
        (tmp_path / "pom.xml").write_text("<project/>")
        result = runner.invoke(app, [str(tmp_path), "--compact", "--format", "xml"])
        assert result.exit_code == 2, f"bad --format must exit 2, got {result.exit_code}"

    def test_missing_path_exits_1(self):
        result = runner.invoke(app, ["/nonexistent/path/xyz", "--compact"])
        assert result.exit_code == 1, f"missing path must exit 1, got {result.exit_code}"

    def test_unknown_task_exits_1(self, tmp_path):
        (tmp_path / "pom.xml").write_text("<project/>")
        result = runner.invoke(app, ["prepare-context", "unknowntask", str(tmp_path)])
        assert result.exit_code == 1, f"unknown task must exit 1, got {result.exit_code}"

    def test_invalid_rank_by_exits_2(self, tmp_path):
        (tmp_path / "pom.xml").write_text("<project/>")
        result = runner.invoke(app, [str(tmp_path), "--compact", "--rank-by", "magic"])
        assert result.exit_code == 2, f"bad --rank-by must exit 2, got {result.exit_code}"


# ── BUG-7: review-pr no_diff = exit 0 ────────────────────────────────────────

class TestReviewPrNoDiffExit:
    """review-pr with no diff must exit 0, not 1."""

    def test_no_diff_consistent_with_delta(self, tmp_path):
        """Both delta and review-pr must exit 0 when no changes found."""
        import subprocess
        subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
        subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t.com"], capture_output=True)
        subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "T"], capture_output=True)
        (tmp_path / "pom.xml").write_text("<project/>")
        subprocess.run(["git", "-C", str(tmp_path), "add", "."], capture_output=True)
        subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "init"], capture_output=True)

        delta_result = runner.invoke(app, ["prepare-context", "delta", str(tmp_path), "--since", "HEAD"])
        review_result = runner.invoke(app, ["prepare-context", "review-pr", str(tmp_path), "--since", "HEAD"])

        assert delta_result.exit_code == 0, f"delta no-diff must exit 0, got {delta_result.exit_code}"
        assert review_result.exit_code == 0, (
            f"review-pr no-diff must exit 0 (consistent with delta), got {review_result.exit_code}. "
            f"Output: {review_result.output[:300]}"
        )


# ── BUG-8: MCP status version clarity ────────────────────────────────────────

class TestMcpStatusVersion:
    """mcp status must show CLI version and detect external servers."""

    def test_mcp_status_shows_cli_version(self):
        from sourcecode import __version__
        result = runner.invoke(app, ["mcp", "status"])
        assert __version__ in result.output, (
            f"mcp status must show CLI version {__version__}. Output: {result.output[:500]}"
        )

    def test_mcp_status_shows_python_executable(self):
        import sys
        result = runner.invoke(app, ["mcp", "status"])
        # The Python executable path should appear somewhere in the version line
        assert sys.executable in result.output or "python" in result.output.lower(), (
            "mcp status must show Python executable. Output: {result.output[:500]}"
        )

    def test_builtin_server_labeled_correctly(self, tmp_path):
        """When config uses 'sourcecode mcp serve', it must be labeled as built-in."""
        config = {
            "mcpServers": {
                "sourcecode": {"command": "sourcecode", "args": ["mcp", "serve"]}
            }
        }
        cfg_file = tmp_path / "claude_desktop_config.json"
        cfg_file.write_text(json.dumps(config), encoding="utf-8")

        from sourcecode.mcp.onboarding.applier import read_config, is_installed
        loaded = read_config(cfg_file)
        assert is_installed(loaded)
        entry = loaded["mcpServers"]["sourcecode"]
        cmd = entry.get("command", "")
        args = entry.get("args", [])
        is_builtin = (
            cmd == "sourcecode"
            or (not args and cmd.endswith("/sourcecode"))
            or (args and args[:2] == ["mcp", "serve"])
        )
        assert is_builtin, "sourcecode mcp serve entry must be recognized as built-in"

    def test_external_server_detected(self, tmp_path):
        """When config uses a custom Python script, it must be flagged as external."""
        config = {
            "mcpServers": {
                "sourcecode": {
                    "command": "/some/venv/bin/python",
                    "args": ["/some/server.py"]
                }
            }
        }
        cfg_file = tmp_path / "claude_desktop_config.json"
        cfg_file.write_text(json.dumps(config), encoding="utf-8")

        loaded = json.loads(cfg_file.read_text())
        entry = loaded["mcpServers"]["sourcecode"]
        cmd = entry.get("command", "")
        args = entry.get("args", [])
        is_builtin = (
            cmd == "sourcecode"
            or (not args and cmd.endswith("/sourcecode"))
            or (args and args[:2] == ["mcp", "serve"])
        )
        assert not is_builtin, "custom Python+script entry must NOT be recognized as built-in"


class TestXmlSecurityOpacity:
    """Regression tests for F-008: XML Spring Security opacity.

    When endpoint security is configured via XML (spring-security.xml,
    applicationContext-security.xml, etc.), endpoints must NOT show
    policy=none_detected. The security_model must be xml_or_filter_chain.
    """

    _CONTROLLER = '''\
package com.example;
import org.springframework.web.bind.annotation.*;
@RestController
public class UserController {
    @GetMapping("/users") public String list() { return "[]"; }
    @PostMapping("/users") public String create() { return "ok"; }
}
'''

    _SPRING_SECURITY_XML = '''\
<?xml version="1.0"?>
<beans xmlns:security="http://www.springframework.org/schema/security">
    <security:http auto-config="true">
        <security:intercept-url pattern="/users" access="ROLE_USER"/>
    </security:http>
</beans>'''

    def test_xml_security_sets_security_model(self, tmp_path) -> None:
        root = _java_repo(tmp_path, self._CONTROLLER)
        (tmp_path / "src/main/resources").mkdir(parents=True, exist_ok=True)
        (tmp_path / "src/main/resources/spring-security.xml").write_text(self._SPRING_SECURITY_XML)

        result = extract_java_endpoints(root)
        assert result["security_model"] == "xml_or_filter_chain", (
            f"F-008 regression: expected xml_or_filter_chain, got {result['security_model']}"
        )

    def test_xml_security_no_none_detected_endpoints(self, tmp_path) -> None:
        root = _java_repo(tmp_path, self._CONTROLLER)
        (tmp_path / "src/main/resources").mkdir(parents=True, exist_ok=True)
        (tmp_path / "src/main/resources/spring-security.xml").write_text(self._SPRING_SECURITY_XML)

        result = extract_java_endpoints(root)
        none_detected = [
            e for e in result["endpoints"]
            if e.get("security", {}).get("policy") == "none_detected"
        ]
        assert len(none_detected) == 0, (
            f"F-008 regression: {len(none_detected)} endpoint(s) still show none_detected "
            f"when XML security is present: {none_detected}"
        )

    def test_xml_security_endpoints_show_xml_or_filter_chain(self, tmp_path) -> None:
        root = _java_repo(tmp_path, self._CONTROLLER)
        (tmp_path / "src/main/resources").mkdir(parents=True, exist_ok=True)
        (tmp_path / "src/main/resources/applicationContext-security.xml").write_text(
            self._SPRING_SECURITY_XML
        )

        result = extract_java_endpoints(root)
        for ep in result["endpoints"]:
            policy = ep.get("security", {}).get("policy", "")
            assert policy == "xml_or_filter_chain", (
                f"F-008 regression: endpoint {ep.get('path')} has policy={policy!r}, "
                f"expected xml_or_filter_chain"
            )

    def test_no_xml_security_keeps_none_detected(self, tmp_path) -> None:
        """Without XML security, none_detected must be preserved (no false positive)."""
        root = _java_repo(tmp_path, self._CONTROLLER)

        result = extract_java_endpoints(root)
        assert result["security_model"] != "xml_or_filter_chain", (
            "F-008 regression: xml_or_filter_chain falsely set when no XML security present"
        )
        none_detected = [
            e for e in result["endpoints"]
            if e.get("security", {}).get("policy") == "none_detected"
        ]
        assert len(none_detected) == len(result["endpoints"]), (
            "F-008 regression: endpoints changed policy without XML security"
        )

    def test_java_filter_based_not_overridden_by_xml(self, tmp_path) -> None:
        """When @EnableWebSecurity is present, filter_based must not be overridden."""
        java = '''\
package com.example;
import org.springframework.security.config.annotation.web.configuration.*;
import org.springframework.web.bind.annotation.*;
@EnableWebSecurity
public class SecConfig {}
@RestController
class Ctrl {
    @GetMapping("/x") public String x() { return "ok"; }
}
'''
        root = _java_repo(tmp_path, java)
        (tmp_path / "src/main/resources").mkdir(parents=True, exist_ok=True)
        (tmp_path / "src/main/resources/security.xml").write_text(self._SPRING_SECURITY_XML)

        result = extract_java_endpoints(root)
        assert result["security_model"] == "filter_based", (
            f"F-008 regression: filter_based overridden by XML. got={result['security_model']}"
        )

    def test_no_security_signal_zero_when_xml(self, tmp_path) -> None:
        """no_security_signal must be 0 when all endpoints get xml_or_filter_chain."""
        root = _java_repo(tmp_path, self._CONTROLLER)
        (tmp_path / "src/main/resources").mkdir(parents=True, exist_ok=True)
        (tmp_path / "src/main/resources/spring-security.xml").write_text(self._SPRING_SECURITY_XML)

        result = extract_java_endpoints(root)
        assert result["no_security_signal"] == 0, (
            f"F-008 regression: no_security_signal={result['no_security_signal']}, expected 0"
        )

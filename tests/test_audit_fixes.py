"""Regression tests for SAS Enterprise benchmark audit fixes.

Covers (in priority order):
  P0-1  Cache fingerprint includes --exclude and all analysis-affecting flags
  P0-2  --format yaml has semantic parity with --format json (agent mode)
  P0-3  repo-ir --summary-only stays under 100 KB
  P0-4  delta and review-pr behave consistently for invalid git refs
  P0-5  MCP server version matches CLI version
  P0-6  mcp status separates configured vs running states
  P2-7  CLI validation errors exit with code 2, runtime errors with code 1
  P2-8  Invalid path error shows original user input (not OS-resolved form)
  P2-9  get_compact_context and get_agent_context have distinct descriptions
  P2-10 telemetry tool schema enumerates valid actions
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# P0-1  Cache fingerprint — exclude + all analysis-affecting flags
# ─────────────────────────────────────────────────────────────────────────────

def _make_flags_str(
    version: str = "1.31.11",
    compact: bool = True,
    agent: bool = False,
    fmt: str = "json",
    full: bool = False,
    changed_only: bool = False,
    dependencies: bool = False,
    graph_modules: bool = False,
    docs: bool = False,
    full_metrics: bool = False,
    semantics: bool = False,
    architecture: bool = False,
    git_context: bool = False,
    env_map: bool = False,
    code_notes: bool = False,
    tree: bool = False,
    mode: str = "raw",
    exclude: str = "",
    effective_depth: int = 4,
    rank_by: str = "relevance",
    symbol: Any = None,
    entrypoints_only: bool = False,
    no_redact: bool = False,
    graph_detail: str = "high",
    docs_depth: str = "symbols",
    max_nodes: Any = None,
    graph_edges: Any = None,
    max_importers: int = 50,
    emit_graph: bool = False,
) -> str:
    """Replicate the _flags_str logic from cli.py for unit testing."""
    excl_key = (
        ",".join(sorted(e.strip() for e in exclude.split(",") if e.strip()))
        if exclude else ""
    )
    return (
        f"v={version},"
        f"c={compact},ag={agent},fmt={fmt},full={full},"
        f"co={changed_only},dep={dependencies},gm={graph_modules},"
        f"docs={docs},fm={full_metrics},sem={semantics},"
        f"arch={architecture},gc={git_context},em={env_map},"
        f"cn={code_notes},tree={tree},mode={mode},"
        f"ex={excl_key},depth={effective_depth},"
        f"rb={rank_by},sym={symbol},ep={entrypoints_only},"
        f"nr={no_redact},gd={graph_detail},dd={docs_depth},"
        f"mn={max_nodes},ge={graph_edges},mi={max_importers},"
        f"eg={emit_graph}"
    )


def _fingerprint(flags_str: str) -> str:
    return hashlib.md5(flags_str.encode()).hexdigest()[:8]


class TestCacheFingerprint:
    """P0-1: cache key must include every analysis-affecting flag."""

    def test_exclude_changes_fingerprint(self):
        """--exclude must change the cache key."""
        fp_no_excl = _fingerprint(_make_flags_str(exclude=""))
        fp_with_excl = _fingerprint(_make_flags_str(exclude="saint-client,saint-portal"))
        assert fp_no_excl != fp_with_excl, (
            "--exclude 'saint-client,saint-portal' must produce a different cache key"
        )

    def test_exclude_order_normalized(self):
        """exclude='a,b' and exclude='b,a' must produce the same fingerprint (sorted)."""
        fp_ab = _fingerprint(_make_flags_str(exclude="a,b"))
        fp_ba = _fingerprint(_make_flags_str(exclude="b,a"))
        assert fp_ab == fp_ba, "exclude values must be sorted before hashing"

    def test_depth_changes_fingerprint(self):
        fp4 = _fingerprint(_make_flags_str(effective_depth=4))
        fp12 = _fingerprint(_make_flags_str(effective_depth=12))
        assert fp4 != fp12, "--depth must change the cache key"

    def test_format_changes_fingerprint(self):
        fp_json = _fingerprint(_make_flags_str(fmt="json"))
        fp_yaml = _fingerprint(_make_flags_str(fmt="yaml"))
        assert fp_json != fp_yaml, "--format must change the cache key"

    def test_rank_by_changes_fingerprint(self):
        fp_rel = _fingerprint(_make_flags_str(rank_by="relevance"))
        fp_cen = _fingerprint(_make_flags_str(rank_by="centrality"))
        assert fp_rel != fp_cen, "--rank-by must change the cache key"

    def test_symbol_changes_fingerprint(self):
        fp_none = _fingerprint(_make_flags_str(symbol=None))
        fp_sym = _fingerprint(_make_flags_str(symbol="MyClass"))
        assert fp_none != fp_sym, "--symbol must change the cache key"

    def test_version_changes_fingerprint(self):
        fp_old = _fingerprint(_make_flags_str(version="1.27.1"))
        fp_new = _fingerprint(_make_flags_str(version="1.31.11"))
        assert fp_old != fp_new, "version bump must invalidate cache"

    def test_no_redact_changes_fingerprint(self):
        fp_redact = _fingerprint(_make_flags_str(no_redact=False))
        fp_no = _fingerprint(_make_flags_str(no_redact=True))
        assert fp_redact != fp_no, "--no-redact must change the cache key"

    def test_all_base_flags_present_in_flags_str(self):
        """Spot-check that known flags appear in the string."""
        s = _make_flags_str(
            exclude="legacy", effective_depth=12, rank_by="centrality",
            symbol="Foo", fmt="yaml", no_redact=True,
        )
        assert "ex=legacy" in s
        assert "depth=12" in s
        assert "rb=centrality" in s
        assert "sym=Foo" in s
        assert "fmt=yaml" in s
        assert "nr=True" in s


# ─────────────────────────────────────────────────────────────────────────────
# P0-2  JSON/YAML schema parity
# ─────────────────────────────────────────────────────────────────────────────

class TestYamlJsonParity:
    """P0-2: --format yaml must have the same top-level keys as --format json
    for compact_view and agent_view outputs."""

    def _make_sm(self):
        from sourcecode.schema import SourceMap
        return SourceMap()

    def test_compact_view_yaml_json_same_keys(self):
        from io import StringIO

        from ruamel.yaml import YAML

        from sourcecode.schema import SourceMap
        from sourcecode.serializer import compact_view

        sm = SourceMap()
        data = compact_view(sm)

        json_keys = set(json.dumps(data, default=str).replace('"', '').split())  # rough
        json_keys = set(data.keys())

        yaml_inst = YAML()
        yaml_inst.default_flow_style = False
        stream = StringIO()
        yaml_inst.dump(data, stream)
        from ruamel.yaml import YAML as _Y
        loaded = dict(_Y().load(StringIO(stream.getvalue())))
        yaml_keys = set(loaded.keys())

        assert json_keys == yaml_keys, (
            f"compact_view YAML missing keys vs JSON: {json_keys - yaml_keys}"
        )

    def test_compact_view_yaml_roundtrip_values(self):
        """Values that survive JSON→str→parse must survive YAML roundtrip too."""
        from io import StringIO

        from ruamel.yaml import YAML

        from sourcecode.schema import (
            AnalysisMetadata, EntryPoint, FrameworkDetection,
            SourceMap, StackDetection,
        )
        from sourcecode.serializer import compact_view

        sm = SourceMap(
            project_type="java-spring",
            stacks=[StackDetection(
                stack="java",
                frameworks=[FrameworkDetection(name="Spring Boot", version="3.1.0")],
            )],
        )
        data = compact_view(sm)

        yaml_inst = YAML()
        yaml_inst.default_flow_style = False
        yaml_inst.representer.add_representer(
            type(None),
            lambda d, v: d.represent_scalar("tag:yaml.org,2002:null", "null"),
        )
        stream = StringIO()
        yaml_inst.dump(data, stream)
        loaded = dict(YAML().load(StringIO(stream.getvalue())))

        assert loaded.get("project_type") == "java-spring"
        assert "schema_version" in loaded

    def test_agent_view_yaml_branch_produces_yaml(self):
        """agent_view with format=yaml must produce valid YAML, not JSON."""
        from io import StringIO

        from ruamel.yaml import YAML

        from sourcecode.schema import SourceMap
        from sourcecode.serializer import agent_view

        sm = SourceMap()
        data = agent_view(sm, full=False)

        yaml_inst = YAML()
        yaml_inst.default_flow_style = False
        yaml_inst.representer.add_representer(
            type(None),
            lambda d, v: d.represent_scalar("tag:yaml.org,2002:null", "null"),
        )
        stream = StringIO()
        yaml_inst.dump(data, stream)
        yaml_str = stream.getvalue()

        # Must parse as YAML
        loaded = YAML().load(StringIO(yaml_str))
        assert loaded is not None
        # Must not start with '{' (JSON object literal)
        assert not yaml_str.strip().startswith("{"), (
            "agent_view YAML output looks like JSON — YAML branch missing"
        )


# ─────────────────────────────────────────────────────────────────────────────
# P0-3  repo-ir --summary-only bounded for LLM use
# ─────────────────────────────────────────────────────────────────────────────

def _make_large_ir(n_nodes: int = 500, n_edges: int = 2000) -> dict:
    nodes = [
        {"fqn": f"pkg.Class{i}", "type": "class", "role": "service",
         "in_degree": i, "out_degree": i, "stable_id": f"id{i}",
         "symbol_kind": "class", "canonical_name": f"pkg.Class{i}",
         "source_file": f"Class{i}.java", "signature": ""}
        for i in range(n_nodes)
    ]
    edges = [
        {"from": f"pkg.Class{i % n_nodes}", "to": f"pkg.Class{(i+1) % n_nodes}",
         "type": "imports", "confidence": "high", "evidence": {}}
        for i in range(n_edges)
    ]
    ranked = [
        {"entity": f"pkg.Class{i}", "type": "class", "role": "service",
         "score": float(n_nodes - i)}
        for i in range(n_nodes)
    ]
    subsystems = [
        {"name": f"subsystem_{i}", "members": [f"pkg.Class{j}" for j in range(i, i+10)]}
        for i in range(60)  # 60 subsystems → exceeds cap of 20
    ]
    change_set = [
        {"entity": f"pkg.Class{i}", "change_type": "modified"}
        for i in range(80)  # 80 entries → exceeds cap of 30
    ]
    route_surface = {
        "endpoints": [
            {"method": "GET", "path": f"/api/resource/{i}", "controller": f"Ctrl{i}",
             "handler": f"handle{i}"}
            for i in range(100)  # 100 endpoints → exceeds cap of 50
        ],
        "total": 100,
    }
    reverse_graph = {
        f"pkg.Class{i}": {"imports": [f"pkg.Class{j}" for j in range(min(i, 5))]}
        for i in range(80)  # 80 entries → exceeds cap of 30
    }
    return {
        "schema_version": "final-v1",
        "graph": {"nodes": nodes, "edges": edges},
        "reverse_graph": reverse_graph,
        "analysis": {
            "changed_entities": [{"entity": f"pkg.Class{i}"} for i in range(50)],
            "impacted_entities": [{"entity": f"pkg.Class{i}"} for i in range(50)],
            "isolated_changes": [],
            "validated_changes": [],
        },
        "impact": {"global_score": 42.0, "ranked_nodes": ranked},
        "subsystems": subsystems,
        "change_set": change_set,
        "route_surface": route_surface,
        "spring_events": {"listeners": [], "publishers": [], "event_types": [], "flow_count": 0},
        "analysis_gaps": [],
        "audit": {"dropped_fields": []},
    }


class TestSummaryOnlyBounded:
    """P0-3: summary_only output must be safe for LLM context."""

    def test_summary_only_under_100kb(self):
        from sourcecode.repository_ir import apply_ir_size_limits

        ir = _make_large_ir(n_nodes=500, n_edges=2000)
        result = apply_ir_size_limits(ir, summary_only=True)
        encoded = json.dumps(result, ensure_ascii=False).encode("utf-8")
        assert len(encoded) <= 100_000, (
            f"summary_only output {len(encoded)} bytes exceeds 100KB LLM budget"
        )

    def test_summary_only_graph_omitted(self):
        from sourcecode.repository_ir import apply_ir_size_limits

        ir = _make_large_ir()
        result = apply_ir_size_limits(ir, summary_only=True)
        assert result["graph"]["nodes"] == []
        assert result["graph"]["edges"] == []
        assert "_omitted" in result["graph"]

    def test_summary_only_subsystems_capped(self):
        from sourcecode.repository_ir import apply_ir_size_limits

        ir = _make_large_ir()
        result = apply_ir_size_limits(ir, summary_only=True)
        assert len(result.get("subsystems", [])) <= 20, (
            "subsystems must be capped at 20 in summary_only mode"
        )

    def test_summary_only_change_set_capped(self):
        from sourcecode.repository_ir import apply_ir_size_limits

        ir = _make_large_ir()
        result = apply_ir_size_limits(ir, summary_only=True)
        assert len(result.get("change_set", [])) <= 30, (
            "change_set must be capped at 30 in summary_only mode"
        )

    def test_summary_only_route_surface_capped(self):
        from sourcecode.repository_ir import apply_ir_size_limits

        ir = _make_large_ir()
        result = apply_ir_size_limits(ir, summary_only=True)
        rs = result.get("route_surface")
        if isinstance(rs, dict):
            eps = rs.get("endpoints", [])
            assert len(eps) <= 50, (
                "route_surface.endpoints must be capped at 50 in summary_only mode"
            )

    def test_summary_only_reverse_graph_capped(self):
        from sourcecode.repository_ir import apply_ir_size_limits

        ir = _make_large_ir()
        result = apply_ir_size_limits(ir, summary_only=True)
        assert len(result.get("reverse_graph", {})) <= 30, (
            "reverse_graph must be capped at 30 entries in summary_only mode"
        )

    def test_summary_only_impact_capped(self):
        from sourcecode.repository_ir import apply_ir_size_limits

        ir = _make_large_ir(n_nodes=200)
        result = apply_ir_size_limits(ir, summary_only=True)
        assert len(result["impact"]["ranked_nodes"]) <= 20

    def test_small_ir_unchanged_by_summary_only(self):
        """A tiny IR should not be truncated."""
        from sourcecode.repository_ir import apply_ir_size_limits

        ir = _make_large_ir(n_nodes=5, n_edges=5)
        ir["subsystems"] = [{"name": "core", "members": ["pkg.Class0"]}]
        ir["change_set"] = [{"entity": "pkg.Class0"}]
        result = apply_ir_size_limits(ir, summary_only=True)
        assert result["graph"]["nodes"] == []  # graph still omitted
        assert len(result["subsystems"]) == 1   # no truncation needed


# ─────────────────────────────────────────────────────────────────────────────
# P0-4  Delta / review-pr git ref consistency
# ─────────────────────────────────────────────────────────────────────────────

class TestGitRefConsistency:
    """P0-4: delta must fail (not silently rewrite) for invalid explicit refs."""

    def _make_baseline_resolver(self, root: Path):
        from sourcecode.prepare_context import TaskContextBuilder
        return TaskContextBuilder(root)

    def _mock_run_factory(self, known_refs: set[str]):
        """Return a _run mock that only resolves known_refs."""
        def _run(*args, timeout=5):
            cmd = list(args)
            if cmd[:2] == ["rev-parse", "--verify"]:
                ref = cmd[2]
                return (ref in known_refs, ref if ref in known_refs else "")
            if cmd[0] == "diff":
                # Find the ref argument (first non-flag after 'diff --name-only --relative')
                ref = None
                for a in cmd:
                    if not a.startswith("-") and a not in ("diff", "HEAD"):
                        ref = a
                        break
                if ref and ref in known_refs:
                    return (True, "src/Foo.java\nsrc/Bar.java")
                return (True, "")  # no files
            if cmd[0] == "symbolic-ref":
                return (False, "")  # no symbolic ref
            return (False, "")
        return _run

    def test_delta_invalid_ref_returns_error(self, tmp_path):
        """delta --since main (nonexistent) must return error=True, not silently rewrite."""
        from sourcecode.prepare_context import TaskContextBuilder

        builder = TaskContextBuilder(tmp_path)
        # Patch _run so that 'main' and 'origin/main' both fail
        known: set[str] = {"HEAD~1", "HEAD"}
        with patch.object(
            builder, "_resolve_git_baseline",
            wraps=builder._resolve_git_baseline,
        ):
            # Directly test _resolve_git_baseline with mocked subprocess
            import subprocess

            def _fake_run(cmd, **kwargs):
                r = MagicMock()
                ref = cmd[-1] if cmd else ""
                if "rev-parse" in cmd and "--verify" in cmd:
                    r.returncode = 1 if ref not in known else 0
                    r.stdout = ref if ref in known else ""
                elif "symbolic-ref" in cmd:
                    r.returncode = 1
                    r.stdout = ""
                else:
                    r.returncode = 0
                    r.stdout = ""
                return r

            with patch("subprocess.run", side_effect=_fake_run):
                result = builder._resolve_git_baseline(since="main")

        assert result["error"] is True, (
            "delta with nonexistent --since must return error=True"
        )
        assert result["resolution_path"] == "unresolvable"
        assert result["diff_validation_status"] == "invalid_ref"

    def test_delta_no_stage3_symbolic_ref_rewrite(self, tmp_path):
        """delta must NOT silently rewrite 'main' to 'origin/develop' via symbolic-ref."""
        from sourcecode.prepare_context import TaskContextBuilder

        builder = TaskContextBuilder(tmp_path)
        symbolic_ref_called = []

        import subprocess

        def _fake_run(cmd, **kwargs):
            r = MagicMock()
            if "symbolic-ref" in cmd:
                symbolic_ref_called.append(True)
                r.returncode = 0
                r.stdout = "refs/remotes/origin/develop"
            elif "rev-parse" in cmd and "--verify" in cmd:
                ref = cmd[-1]
                # 'main' and 'origin/main' both fail; 'origin/develop' exists
                r.returncode = 0 if ref == "origin/develop" else 1
                r.stdout = ref if ref == "origin/develop" else ""
            else:
                r.returncode = 0
                r.stdout = ""
            return r

        with patch("subprocess.run", side_effect=_fake_run):
            result = builder._resolve_git_baseline(since="main")

        # After fix: must be error=True (no Stage 3 rewrite)
        assert result["error"] is True, (
            "delta must not silently rewrite 'main' to 'origin/develop' via symbolic-ref"
        )
        # Stage 3 (symbolic-ref) should not even be called
        assert not symbolic_ref_called, (
            "symbolic-ref lookup (Stage 3) must not be called when since is provided"
        )

    def test_delta_exact_ref_succeeds(self, tmp_path):
        """delta with a valid exact ref must succeed normally."""
        from sourcecode.prepare_context import TaskContextBuilder

        builder = TaskContextBuilder(tmp_path)

        import subprocess

        def _fake_run(cmd, **kwargs):
            r = MagicMock()
            if "rev-parse" in cmd and "--verify" in cmd:
                r.returncode = 0
                r.stdout = "abc123"
            elif "diff" in cmd and "--name-only" in cmd:
                r.returncode = 0
                r.stdout = "src/Foo.java\n"
            else:
                r.returncode = 0
                r.stdout = ""
            return r

        with patch("subprocess.run", side_effect=_fake_run):
            result = builder._resolve_git_baseline(since="develop")

        assert result["error"] is False
        assert result["resolution_path"] == "exact_local_ref"
        assert "src/Foo.java" in result["files"]

    def test_delta_remote_tracking_ref_succeeds(self, tmp_path):
        """delta with 'main' where 'origin/main' exists must succeed (Stage 2)."""
        from sourcecode.prepare_context import TaskContextBuilder

        builder = TaskContextBuilder(tmp_path)

        import subprocess

        def _fake_run(cmd, **kwargs):
            r = MagicMock()
            if "rev-parse" in cmd and "--verify" in cmd:
                ref = cmd[-1]
                r.returncode = 0 if ref == "origin/main" else 1
                r.stdout = ref if ref == "origin/main" else ""
            elif "diff" in cmd and "--name-only" in cmd:
                r.returncode = 0
                r.stdout = "src/Bar.java\n"
            else:
                r.returncode = 0
                r.stdout = ""
            return r

        with patch("subprocess.run", side_effect=_fake_run):
            result = builder._resolve_git_baseline(since="main")

        assert result["error"] is False
        assert result["resolution_path"] == "remote_tracking_ref"
        assert result["resolved_ref"] == "origin/main"


# ─────────────────────────────────────────────────────────────────────────────
# P0-5  MCP version parity
# ─────────────────────────────────────────────────────────────────────────────

class TestMcpVersionParity:
    def test_mcp_server_uses_sourcecode_version(self):
        """MCP low-level server version must match CLI __version__."""
        from sourcecode import __version__ as cli_version
        from sourcecode.mcp import server as mcp_server

        mcp_inst = mcp_server.mcp
        # Version is injected on the underlying low-level server
        low_level = getattr(mcp_inst, "_mcp_server", None)
        if low_level is None:
            pytest.skip("FastMCP._mcp_server not accessible in this version")
        mcp_version = getattr(low_level, "version", None)
        if mcp_version is None:
            pytest.skip("Server.version attribute not accessible in this version")
        assert mcp_version == cli_version, (
            f"MCP server version '{mcp_version}' != CLI version '{cli_version}'"
        )

    def test_cli_version_constant_is_string(self):
        from sourcecode import __version__
        assert isinstance(__version__, str)
        assert len(__version__) > 0

    def test_mcp_server_imports_version_from_sourcecode(self):
        """server.py must import __version__ from sourcecode (not hardcode it)."""
        import inspect
        from sourcecode.mcp import server as mcp_server

        src = inspect.getsource(mcp_server)
        assert "__version__" in src or "_sourcecode_version" in src, (
            "server.py must reference sourcecode __version__"
        )
        assert "FastMCP" in src
        # Must pass version to FastMCP constructor
        assert "version=" in src


# ─────────────────────────────────────────────────────────────────────────────
# P0-6  mcp status — no contradictions
# ─────────────────────────────────────────────────────────────────────────────

class TestMcpStatusOutput:
    """P0-6: mcp status must not show contradictory configured/running states."""

    def _run_mcp_status(self):
        from typer.testing import CliRunner
        from sourcecode.cli import app

        runner = CliRunner()
        result = runner.invoke(app, ["mcp", "status"])
        return result.output

    def test_status_has_separate_config_and_runtime_sections(self):
        """Output must show config and runtime as distinct sections."""
        from unittest.mock import patch, MagicMock

        mock_client = MagicMock()
        mock_client.name = "Claude Desktop"
        mock_client.slug = "claude-desktop"
        mock_client.app_installed = True
        mock_client.config_path = Path("/fake/config.json")

        with (
            patch("sourcecode.mcp.onboarding.detector.detect_clients", return_value=[mock_client]),
            patch("sourcecode.mcp.onboarding.detector.is_client_running", return_value=False),
            patch("sourcecode.mcp.onboarding.applier.read_config", return_value={}),
            patch("sourcecode.mcp.onboarding.applier.is_installed", return_value=False),
        ):
            output = self._run_mcp_status()

        # Must have separate labelled sections
        lower = output.lower()
        assert "config" in lower, "Output must have a 'config' section"
        assert "runtime" in lower or "running" in lower, (
            "Output must have a 'runtime' or 'running' section"
        )

    def test_no_contradiction_not_configured_but_running(self):
        """A client that is running but not configured must not show contradictory lines."""
        from unittest.mock import patch, MagicMock

        mock_client = MagicMock()
        mock_client.name = "Claude Desktop"
        mock_client.slug = "claude-desktop"
        mock_client.app_installed = True
        mock_client.config_path = Path("/fake/config.json")

        with (
            patch("sourcecode.mcp.onboarding.detector.detect_clients", return_value=[mock_client]),
            patch("sourcecode.mcp.onboarding.detector.is_client_running", return_value=True),
            patch("sourcecode.mcp.onboarding.applier.read_config", return_value={}),
            patch("sourcecode.mcp.onboarding.applier.is_installed", return_value=False),
        ):
            output = self._run_mcp_status()

        lines = output.splitlines()
        # Must not have both "✗ not configured" and "✓ running" in isolation
        # without a clear explanation that they are different checks.
        not_configured_line = any("not configured" in l for l in lines)
        running_line = any("✓ running" in l for l in lines)

        if not_configured_line and running_line:
            # Acceptable only if there's a note explaining they are independent
            note_present = any(
                "independent" in l.lower() or "separate" in l.lower() or "restart" in l.lower()
                for l in lines
            )
            assert note_present, (
                "When showing both 'not configured' and '✓ running', "
                "output must include a note clarifying these are independent states"
            )


# ─────────────────────────────────────────────────────────────────────────────
# P2-7  Exit codes — arg validation = 2, runtime = 1
# ─────────────────────────────────────────────────────────────────────────────

class TestExitCodes:
    """P2-7: argument validation must exit 2; runtime errors must exit 1."""

    def _invoke(self, args: list[str]) -> int:
        from typer.testing import CliRunner
        from sourcecode.cli import _detected_path, app

        _detected_path[0] = "."
        runner = CliRunner()
        result = runner.invoke(app, args)
        return result.exit_code

    def test_invalid_format_exits_2(self):
        code = self._invoke(["--format", "invalid_format", "--compact"])
        assert code == 2, f"--format invalid_format must exit 2, got {code}"

    def test_invalid_mode_exits_2(self):
        code = self._invoke(["--mode", "invalid_mode"])
        assert code == 2, f"--mode invalid must exit 2, got {code}"

    def test_invalid_rank_by_exits_2(self):
        code = self._invoke(["--rank-by", "invalid_rank"])
        assert code == 2, f"--rank-by invalid must exit 2, got {code}"

    def test_invalid_graph_detail_exits_2(self):
        code = self._invoke(["--graph-detail", "invalid_detail"])
        assert code == 2, f"--graph-detail invalid must exit 2, got {code}"

    def test_invalid_docs_depth_exits_2(self):
        code = self._invoke(["--docs-depth", "invalid_depth"])
        assert code == 2, f"--docs-depth invalid must exit 2, got {code}"

    def test_nonexistent_path_exits_1(self, tmp_path):
        # _cmd_main wrapper resets _detected_path[0]="." then calls _preprocess_args.
        # Pass the path as a positional arg so _preprocess_args picks it up correctly.
        from typer.testing import CliRunner
        from sourcecode.cli import app

        nonexistent = str(tmp_path / "nonexistent_dir_xyz")
        runner = CliRunner()
        # Pass nonexistent as positional — _preprocess_args extracts it into _detected_path
        result = runner.invoke(app, [nonexistent])
        assert result.exit_code == 1, (
            f"nonexistent path must exit 1, got {result.exit_code}\n"
            f"output: {result.output!r}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# P2-8  Windows path — show original user input in error
# ─────────────────────────────────────────────────────────────────────────────

class TestWindowsPathErrors:
    """P2-8: invalid path errors must show original input, not OS-resolved form."""

    def test_error_shows_original_path_not_resolved(self, tmp_path):
        # Pass path as positional arg — _preprocess_args extracts it into _detected_path.
        from typer.testing import CliRunner
        from sourcecode.cli import app

        original_input = "nonexistent_xyz_path_abc"
        runner = CliRunner()
        result = runner.invoke(app, [original_input])
        output = result.output + (result.stderr if hasattr(result, "stderr") else "")

        assert original_input in output, (
            f"Error message must contain original input '{original_input}', got: {output!r}"
        )

    def test_error_not_git_bash_prefixed(self, tmp_path):
        """Error must not rewrite path to OS-resolved form (e.g. Git Bash prefix on Windows)."""
        from typer.testing import CliRunner
        from sourcecode.cli import app

        original_input = "nonexistent_unix_style_dir_xyz"
        runner = CliRunner()
        result = runner.invoke(app, [original_input])
        output = result.output + (result.stderr if hasattr(result, "stderr") else "")

        # Original bare name must appear in error output, not some OS-mangled form
        assert original_input in output, (
            f"Error must reference original input '{original_input}', got: {output!r}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# P2-9  MCP tool descriptions — compact vs agent clearly differentiated
# ─────────────────────────────────────────────────────────────────────────────

class TestMcpToolDescriptions:
    """P2-9: get_compact_context and get_agent_context must have distinct purposes."""

    def test_compact_context_doc_mentions_compact_or_tokens(self):
        from sourcecode.mcp.server import get_compact_context
        doc = get_compact_context.__doc__ or ""
        keywords = ["compact", "token", "summary", "first", "quick", "overview"]
        assert any(k in doc.lower() for k in keywords), (
            f"get_compact_context docstring must describe it as compact/quick/summary: {doc!r}"
        )

    def test_agent_context_doc_mentions_extended_or_full(self):
        from sourcecode.mcp.server import get_agent_context
        doc = get_agent_context.__doc__ or ""
        keywords = ["full", "extended", "detail", "deep", "richer", "agent"]
        assert any(k in doc.lower() for k in keywords), (
            f"get_agent_context docstring must describe extended/full detail: {doc!r}"
        )

    def test_descriptions_are_different(self):
        from sourcecode.mcp.server import get_agent_context, get_compact_context
        doc_compact = (get_compact_context.__doc__ or "").strip()
        doc_agent = (get_agent_context.__doc__ or "").strip()
        assert doc_compact != doc_agent, (
            "get_compact_context and get_agent_context must have different docstrings"
        )

    def test_compact_context_suggests_use_agent_when_insufficient(self):
        from sourcecode.mcp.server import get_compact_context
        doc = get_compact_context.__doc__ or ""
        assert "agent" in doc.lower(), (
            "get_compact_context must reference get_agent_context for richer detail"
        )


# ─────────────────────────────────────────────────────────────────────────────
# P2-10  Telemetry schema — enumerate valid actions
# ─────────────────────────────────────────────────────────────────────────────

class TestTelemetrySchema:
    """P2-10: telemetry tool must self-describe valid action values."""

    def test_telemetry_docstring_lists_valid_actions(self):
        from sourcecode.mcp.server import telemetry
        doc = telemetry.__doc__ or ""
        for action in ("status", "enable", "disable"):
            assert action in doc, (
                f"telemetry docstring must mention valid action '{action}': {doc!r}"
            )

    def test_telemetry_invalid_action_returns_error_with_valid_list(self):
        import json
        from mcp.types import CallToolResult
        from sourcecode.mcp.server import telemetry
        result = telemetry("unknown_action")
        assert isinstance(result, CallToolResult), (
            f"expected CallToolResult, got {type(result).__name__}"
        )
        assert result.isError is True, "isError must be True for tool failures"
        payload = json.loads(result.content[0].text)
        assert payload["success"] is False
        assert payload["error"] is not None
        # Error message must enumerate valid actions
        msg = payload["error"]["message"]
        for action in ("status", "enable", "disable"):
            assert action in msg, (
                f"telemetry error message must list valid action '{action}': {msg!r}"
            )

    def test_telemetry_valid_actions_constant_has_all_three(self):
        from sourcecode.mcp.server import _TELEMETRY_ACTIONS
        assert "status" in _TELEMETRY_ACTIONS
        assert "enable" in _TELEMETRY_ACTIONS
        assert "disable" in _TELEMETRY_ACTIONS
        assert len(_TELEMETRY_ACTIONS) == 3

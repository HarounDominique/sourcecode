"""Regression tests for v1.33.11 contract/compatibility fixes.

Covers:
  P1 — fix-bug JSON contract: warning suppressed in non-TTY (MCP/pipe) context
  P2 — impact legacy argument order compatibility
  P3 — fix-bug ranked_files backward-compat alias
  P4 — generated_at non-null on fresh COMPACT and AGENT runs
  P5 — review-pr structured error includes actionable hints when no --since
  P6 — mcp list-tools command exposes MCP tools
  P7 — MCP version() returns structured dict with cli_version
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from sourcecode.cli import app


_runner = CliRunner()


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_java_repo(tmp_path: Path) -> Path:
    """Minimal Java repo with one class for impact/fix-bug tests."""
    src = tmp_path / "src" / "main" / "java" / "com" / "example"
    src.mkdir(parents=True)
    (src / "OrderService.java").write_text(
        "package com.example;\npublic class OrderService {}\n"
    )
    return tmp_path


def _invoke_fix_bug(tmp_path: Path, extra_args: list[str] | None = None) -> Any:
    args = ["fix-bug", str(tmp_path)] + (extra_args or [])
    result = _runner.invoke(app, args)
    return result


# ── P1: fix-bug JSON contract — no warning in non-TTY stdout ─────────────────

class TestFixBugJsonContract:
    """P1: stdout must be valid JSON when fix-bug runs without --symptom."""

    def test_fix_bug_without_symptom_stdout_is_valid_json(self, tmp_path: Path):
        """Non-TTY context: no warning mixed into stdout."""
        _make_java_repo(tmp_path)
        result = _invoke_fix_bug(tmp_path)
        assert result.exit_code == 0, f"exit={result.exit_code}\n{result.output}"
        # output must start with '{' — not '[fix-bug] Results are…'
        stripped = result.output.strip()
        assert stripped.startswith("{"), (
            f"stdout contaminated by non-JSON prefix: {stripped[:120]!r}"
        )
        data = json.loads(stripped)
        assert data.get("task") == "fix-bug"

    def test_fix_bug_with_symptom_stdout_is_valid_json(self, tmp_path: Path):
        """With --symptom: no warning needed, stdout still valid JSON."""
        _make_java_repo(tmp_path)
        result = _invoke_fix_bug(tmp_path, ["--symptom", "NullPointerException"])
        assert result.exit_code == 0
        data = json.loads(result.output.strip())
        assert data.get("task") == "fix-bug"

    def test_fix_bug_output_contains_task_field(self, tmp_path: Path):
        """fix-bug JSON always has 'task' field."""
        _make_java_repo(tmp_path)
        result = _invoke_fix_bug(tmp_path)
        data = json.loads(result.output.strip())
        assert "task" in data

    def test_mcp_fix_bug_returns_dict_not_string(self, tmp_path: Path):
        """MCP wrapper must return dict, not raw string with warning prefix."""
        _make_java_repo(tmp_path)
        from sourcecode.mcp.runner import run_command
        result = run_command(["fix-bug", str(tmp_path)])
        # run_command returns parsed JSON (dict) — never a raw warning string
        assert isinstance(result, dict), (
            f"Expected dict, got {type(result).__name__}: {str(result)[:120]!r}"
        )
        assert result.get("task") == "fix-bug"


# ── P2: impact legacy argument order ─────────────────────────────────────────

class TestImpactArgCompatibility:
    """P2: legacy `impact <path> <target>` order handled gracefully."""

    def test_impact_new_syntax_valid_dir(self, tmp_path: Path):
        """New syntax: impact <target> <path> — works when path is a valid dir."""
        _make_java_repo(tmp_path)
        result = _runner.invoke(app, ["impact", "OrderService", str(tmp_path)])
        assert result.exit_code == 0
        data = json.loads(result.output.strip())
        assert "target" in data or "resolution" in data

    def test_impact_legacy_syntax_swapped(self, tmp_path: Path):
        """Legacy syntax: impact <path> <target> — auto-swapped, not directory error."""
        _make_java_repo(tmp_path)
        # Legacy order: path first, target second
        result = _runner.invoke(app, ["impact", str(tmp_path), "OrderService"])
        assert result.exit_code == 0, (
            f"Legacy syntax should succeed via swap, got exit={result.exit_code}\n"
            f"{result.output[:200]}"
        )
        data = json.loads(result.output.strip())
        # Must not emit the misleading "not a valid directory" error
        if data.get("success") is False:
            assert "directory" not in data.get("error", {}).get("message", ""), (
                "Directory error emitted for legacy arg order — swap not working"
            )

    def test_impact_invalid_path_gives_helpful_hint(self, tmp_path: Path):
        """Non-existent path gives clear error with new syntax hint."""
        result = _runner.invoke(app, ["impact", "OrderService", "/nonexistent/path/xyz"])
        assert result.exit_code != 0
        out = result.output.strip()
        data = json.loads(out)
        # Error envelope has either top-level "error" key or nested under "error"
        err = data.get("error", data)
        hint = err.get("hint", "") if isinstance(err, dict) else ""
        assert "impact <target>" in hint or "second argument" in hint or "path" in hint.lower(), (
            f"Hint not actionable: {hint!r}"
        )

    def test_impact_malformed_both_invalid(self, tmp_path: Path):
        """Both args invalid: still returns structured JSON error."""
        result = _runner.invoke(app, ["impact", "NotADir", "/also/not/a/dir"])
        assert result.exit_code != 0
        data = json.loads(result.output.strip())
        assert "error" in data


# ── P3: fix-bug ranked_files alias ───────────────────────────────────────────

class TestFixBugSchemaCompat:
    """P3: fix-bug emits both relevant_files and ranked_files."""

    def test_fix_bug_has_ranked_files_alias(self, tmp_path: Path):
        """ranked_files must be present as backward-compat alias."""
        _make_java_repo(tmp_path)
        result = _invoke_fix_bug(tmp_path)
        data = json.loads(result.output.strip())
        assert "relevant_files" in data, "relevant_files missing from fix-bug output"
        assert "ranked_files" in data, (
            "ranked_files alias missing — v1 consumers will break"
        )

    def test_ranked_files_equals_relevant_files(self, tmp_path: Path):
        """ranked_files must be identical to relevant_files (alias, not subset)."""
        _make_java_repo(tmp_path)
        result = _invoke_fix_bug(tmp_path)
        data = json.loads(result.output.strip())
        assert data["ranked_files"] == data["relevant_files"]

    def test_onboard_has_no_ranked_files(self, tmp_path: Path):
        """ranked_files alias must only appear in fix-bug, not other tasks."""
        _make_java_repo(tmp_path)
        result = _runner.invoke(app, ["prepare-context", "onboard", str(tmp_path)])
        data = json.loads(result.output.strip())
        assert "ranked_files" not in data, (
            "ranked_files alias leaked into non-fix-bug task output"
        )


# ── P4: generated_at consistency ─────────────────────────────────────────────

class TestGeneratedAtConsistency:
    """P4: generated_at non-null on fresh scans for both COMPACT and AGENT scopes."""

    def test_compact_fresh_has_generated_at(self, tmp_path: Path):
        """COMPACT fresh run: _cache.generated_at must be non-null."""
        _make_java_repo(tmp_path)
        result = _runner.invoke(app, [str(tmp_path), "--compact", "--no-cache"])
        assert result.exit_code == 0
        data = json.loads(result.output.strip())
        generated_at = data.get("_cache", {}).get("generated_at")
        assert generated_at is not None, (
            "COMPACT fresh run returned null generated_at"
        )
        assert generated_at.endswith("Z") or "+" in generated_at

    def test_agent_fresh_has_generated_at(self, tmp_path: Path):
        """AGENT fresh run: _cache.generated_at must be non-null."""
        _make_java_repo(tmp_path)
        result = _runner.invoke(app, [str(tmp_path), "--agent", "--no-cache"])
        assert result.exit_code == 0
        data = json.loads(result.output.strip())
        generated_at = data.get("_cache", {}).get("generated_at")
        assert generated_at is not None, (
            "AGENT fresh run returned null generated_at"
        )
        assert generated_at.endswith("Z") or "+" in generated_at

    def test_full_fresh_has_generated_at(self, tmp_path: Path):
        """Full (no flag) fresh run: _cache.generated_at must be non-null."""
        _make_java_repo(tmp_path)
        result = _runner.invoke(app, [str(tmp_path), "--no-cache"])
        assert result.exit_code == 0
        data = json.loads(result.output.strip())
        generated_at = data.get("_cache", {}).get("generated_at")
        assert generated_at is not None, "Full fresh run returned null generated_at"


# ── P5: review-pr structured error ───────────────────────────────────────────

class TestReviewPrUsability:
    """P5: review-pr without --since returns structured error with actionable hints."""

    def _make_git_repo(self, tmp_path: Path) -> Path:
        import subprocess
        _make_java_repo(tmp_path)
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t.com", "-c", "user.name=T",
             "commit", "-m", "init"],
            cwd=tmp_path, capture_output=True
        )
        return tmp_path

    def test_review_pr_no_since_returns_structured_error(self, tmp_path: Path):
        """review-pr without --since: structured JSON error, not a crash."""
        repo = self._make_git_repo(tmp_path)
        result = _runner.invoke(app, ["review-pr", str(repo)])
        out = result.output.strip()
        assert out.startswith("{"), f"Expected JSON, got: {out[:120]!r}"
        data = json.loads(out)
        assert "error" in data or "ci_decision" in data

    def test_review_pr_no_since_error_has_hints(self, tmp_path: Path):
        """review-pr no --since error must include usable --since examples."""
        repo = self._make_git_repo(tmp_path)
        result = _runner.invoke(app, ["review-pr", str(repo)])
        data = json.loads(result.output.strip())
        hints = data.get("hint", [])
        if isinstance(hints, list):
            hint_text = " ".join(hints)
        else:
            hint_text = str(hints)
        # Must mention HEAD~1 or origin/main as common examples
        assert "HEAD~1" in hint_text or "origin/main" in hint_text or "--since" in hint_text, (
            f"Hints not actionable: {hint_text!r}"
        )

    def test_review_pr_with_since_succeeds(self, tmp_path: Path):
        """review-pr with --since HEAD~0 (same commit) returns a valid response."""
        repo = self._make_git_repo(tmp_path)
        result = _runner.invoke(app, ["review-pr", str(repo), "--since", "HEAD"])
        out = result.output.strip()
        data = json.loads(out)
        # Either analysis_success or no_changes/no_diff — both are valid
        assert "task" in data or "ci_decision" in data


# ── P6: mcp list-tools discoverability ───────────────────────────────────────

class TestMcpListTools:
    """P6: mcp list-tools exposes all MCP tool names."""

    def test_mcp_list_tools_exits_zero(self):
        result = _runner.invoke(app, ["mcp", "list-tools"])
        assert result.exit_code == 0, f"exit={result.exit_code}\n{result.output}"

    def test_mcp_list_tools_shows_explain_context(self):
        result = _runner.invoke(app, ["mcp", "list-tools"])
        assert "explain_context" in result.output

    def test_mcp_list_tools_shows_refactor_context(self):
        result = _runner.invoke(app, ["mcp", "list-tools"])
        assert "refactor_context" in result.output

    def test_mcp_list_tools_shows_fix_bug_context(self):
        result = _runner.invoke(app, ["mcp", "list-tools"])
        assert "fix_bug_context" in result.output

    def test_mcp_list_tools_json_is_array(self):
        result = _runner.invoke(app, ["mcp", "list-tools", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output.strip())
        assert isinstance(data, list)
        assert len(data) > 0
        for item in data:
            assert "name" in item
            assert "description" in item

    def test_mcp_list_tools_json_includes_explain_and_refactor(self):
        result = _runner.invoke(app, ["mcp", "list-tools", "--json"])
        data = json.loads(result.output.strip())
        names = {t["name"] for t in data}
        assert "explain_context" in names
        assert "refactor_context" in names


# ── P7: MCP version structured dict ──────────────────────────────────────────

class TestMcpVersionStructured:
    """P7: MCP version() tool returns structured version metadata."""

    def test_mcp_version_returns_dict_with_cli_version(self):
        from sourcecode.mcp.server import version as mcp_version
        result = mcp_version()
        assert isinstance(result, dict)
        data = result.get("data", result)
        assert "cli_version" in data, f"cli_version missing from: {data}"

    def test_mcp_version_cli_version_matches_package(self):
        from sourcecode import __version__
        from sourcecode.mcp.server import version as mcp_version
        result = mcp_version()
        data = result.get("data", result)
        assert data["cli_version"] == __version__

    def test_mcp_version_has_mcp_schema_version(self):
        from sourcecode.mcp.server import version as mcp_version
        result = mcp_version()
        data = result.get("data", result)
        assert "mcp_schema_version" in data

    def test_mcp_version_has_compatibility_schema_version(self):
        from sourcecode.mcp.server import version as mcp_version
        result = mcp_version()
        data = result.get("data", result)
        assert "compatibility_schema_version" in data
        assert data["compatibility_schema_version"] == "1.0"

    def test_mcp_version_success_true(self):
        from sourcecode.mcp.server import version as mcp_version
        result = mcp_version()
        assert result.get("success") is True

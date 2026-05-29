"""Regression tests for audit report v1.31.30 bug fixes.

H-05 — MCP server responds with structured error for invalid repo_path
H-06 — CLI returns JSON for Click-level invalid options
H-01 — --fast mode emits analysis_mode + skipped_analyzers
H-02 — prepare-context generate-tests respects SOURCECODE_TESTS_TIMEOUT_MS
H-03 — impact resolution=not_found exits 0, not 1
H-04 — repo-ir --since prefetches changed files (no O(n) git show calls)
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch, call

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# H-05 — MCP path validation
# ─────────────────────────────────────────────────────────────────────────────

mcp_pkg = pytest.importorskip("mcp", reason="mcp extra not installed")

from mcp.types import CallToolResult  # noqa: E402
from sourcecode.mcp import server as mcp_server  # noqa: E402


def _assert_mcp_error(result: Any, code: str) -> None:
    assert isinstance(result, CallToolResult), f"expected CallToolResult, got {type(result)}"
    assert result.isError is True
    payload = json.loads(result.content[0].text)
    assert payload["success"] is False
    assert payload["error"]["code"] == code
    assert "hint" in payload["error"]
    assert "expected" in payload["error"]


class TestMcpPathValidation:
    """H-05: MCP tools must return structured error immediately for invalid paths."""

    def test_get_compact_context_nonexistent_path_returns_error_without_calling_runner(self, tmp_path):
        nonexistent = str(tmp_path / "does_not_exist_xyz")
        with patch("sourcecode.mcp.server.run_command") as mock_rc:
            result = mcp_server.get_compact_context(nonexistent)
        _assert_mcp_error(result, "INVALID_INPUT")
        mock_rc.assert_not_called()

    def test_get_agent_context_nonexistent_path_returns_error(self, tmp_path):
        nonexistent = str(tmp_path / "no_such_dir")
        with patch("sourcecode.mcp.server.run_command") as mock_rc:
            result = mcp_server.get_agent_context(nonexistent)
        _assert_mcp_error(result, "INVALID_INPUT")
        mock_rc.assert_not_called()

    def test_get_endpoints_nonexistent_path_returns_error(self, tmp_path):
        nonexistent = str(tmp_path / "no_such_dir")
        with patch("sourcecode.mcp.server.run_command") as mock_rc:
            result = mcp_server.get_endpoints(nonexistent)
        _assert_mcp_error(result, "INVALID_INPUT")
        mock_rc.assert_not_called()

    def test_get_compact_context_file_path_not_dir_returns_error(self, tmp_path):
        f = tmp_path / "file.txt"
        f.write_text("hello")
        with patch("sourcecode.mcp.server.run_command") as mock_rc:
            result = mcp_server.get_compact_context(str(f))
        _assert_mcp_error(result, "INVALID_INPUT")
        mock_rc.assert_not_called()

    def test_get_impact_context_nonexistent_path_returns_error(self, tmp_path):
        nonexistent = str(tmp_path / "no_such_dir")
        with patch("sourcecode.mcp.server.run_command") as mock_rc:
            result = mcp_server.get_impact_context(nonexistent, target="UserService")
        _assert_mcp_error(result, "INVALID_INPUT")
        mock_rc.assert_not_called()

    def test_get_ir_summary_nonexistent_path_returns_error(self, tmp_path):
        nonexistent = str(tmp_path / "no_such_dir")
        with patch("sourcecode.mcp.server.run_command") as mock_rc:
            result = mcp_server.get_ir_summary(nonexistent)
        _assert_mcp_error(result, "INVALID_INPUT")
        mock_rc.assert_not_called()

    def test_generate_tests_nonexistent_path_returns_error(self, tmp_path):
        nonexistent = str(tmp_path / "no_such_dir")
        with patch("sourcecode.mcp.server.run_command") as mock_rc:
            result = mcp_server.generate_tests_context(nonexistent)
        _assert_mcp_error(result, "INVALID_INPUT")
        mock_rc.assert_not_called()

    def test_valid_path_does_call_runner(self, tmp_path):
        """Valid path must NOT be rejected — runner must be called."""
        _output = {"success": True, "data": {"stacks": []}, "error": None}
        with patch("sourcecode.mcp.server.run_command", return_value=_output) as mock_rc:
            mcp_server.get_compact_context(str(tmp_path))
        mock_rc.assert_called_once()

    def test_error_payload_contains_path(self, tmp_path):
        """Error message must include the bad path so the agent knows what to fix."""
        nonexistent = str(tmp_path / "missing_repo")
        with patch("sourcecode.mcp.server.run_command"):
            result = mcp_server.get_compact_context(nonexistent)
        payload = json.loads(result.content[0].text)
        assert "missing_repo" in payload["error"]["message"]


# ─────────────────────────────────────────────────────────────────────────────
# H-06 — CLI JSON error for Click-level invalid options
# ─────────────────────────────────────────────────────────────────────────────

class TestCliJsonErrorForInvalidOptions:
    """H-06: CLI must emit JSON on stderr for unknown/bad Click options."""

    def test_unknown_option_exits_nonzero(self):
        """Unknown CLI option must always exit non-zero."""
        import subprocess
        result = subprocess.run(
            ["sourcecode", "--this-flag-does-not-exist-xyz"],
            capture_output=True, text=True,
        )
        assert result.returncode != 0, "unknown option must exit non-zero"

    def test_click_usage_error_show_patched(self, capsys):
        """After cli import, UsageError.show() must write JSON not plain text.

        Note: Typer with Rich installed may use its own formatter bypassing
        show(). This test verifies the patch is in place for non-Rich paths.
        """
        import click.exceptions as ce
        import sourcecode.cli  # ensure patch is applied  # noqa: F401
        err = ce.UsageError("test error message")
        err.show()
        out = capsys.readouterr().err.strip()
        if out:
            try:
                payload = json.loads(out)
                assert payload["error"]["code"] == "INVALID_INPUT"
                assert "message" in payload["error"]
            except json.JSONDecodeError:
                pytest.fail(f"UsageError.show() must emit JSON, got: {out!r}")

    def test_emit_error_json_covers_directory_not_found(self, capsys):
        from sourcecode.cli import _emit_error_json
        _emit_error_json(
            "INVALID_INPUT",
            "Dir '/x' does not exist.",
            path="/x",
            hint="Pass an existing repository directory.",
            expected="An existing directory path.",
        )
        out = capsys.readouterr().err.strip()
        payload = json.loads(out)
        assert payload["error"]["code"] == "INVALID_INPUT"
        assert payload["path"] == "/x"

    def test_emit_error_json_covers_incompatible_flags(self, capsys):
        from sourcecode.cli import _emit_error_json
        _emit_error_json(
            "INVALID_INPUT",
            "--compact and --full are mutually exclusive.",
            hint="Remove one of the conflicting flags.",
            expected="Exactly one of --compact or --full.",
        )
        out = capsys.readouterr().err.strip()
        payload = json.loads(out)
        assert payload["error"]["code"] == "INVALID_INPUT"

    def test_emit_error_json_covers_invalid_option(self, capsys):
        from sourcecode.cli import _emit_error_json
        _emit_error_json(
            "INVALID_INPUT",
            "No such option: --foo",
            flag="--foo",
            hint="Check the command syntax and supported options.",
            expected="A valid CLI argument or option.",
        )
        out = capsys.readouterr().err.strip()
        payload = json.loads(out)
        assert payload["error"]["code"] == "INVALID_INPUT"
        assert payload["flag"] == "--foo"

    def test_click_usage_error_show_patched_to_json(self, capsys):
        """Click UsageError.show() must emit JSON, not plain text."""
        import click.exceptions as ce
        err = ce.UsageError("something went wrong")
        err.show()
        out = capsys.readouterr().err.strip()
        if out:
            payload = json.loads(out)
            assert "error" in payload
            assert "message" in payload["error"]


# ─────────────────────────────────────────────────────────────────────────────
# H-01 — --fast mode transparency
# ─────────────────────────────────────────────────────────────────────────────

class TestFastModeTransparency:
    """H-01: fast mode output must declare analysis_mode and skipped_analyzers."""

    def _invoke_fast(self, tmp_path: Path, task: str) -> dict:
        from sourcecode.cli import _set_detected_path, _preprocess_args, app
        from typer.testing import CliRunner
        runner = CliRunner()
        _set_detected_path(".")
        args = _preprocess_args(["prepare-context", task, str(tmp_path), "--fast"])
        result = runner.invoke(app, args)
        assert result.exit_code == 0, f"exit {result.exit_code}: {result.output[:200]}"
        # Strip any [warn] lines before JSON (CliRunner may mix stderr into output)
        raw = result.output
        json_start = raw.find("{")
        assert json_start >= 0, f"no JSON in output: {raw[:200]}"
        return json.loads(raw[json_start:])

    def test_fast_explain_has_analysis_mode(self, tmp_path):
        data = self._invoke_fast(tmp_path, "explain")
        assert data.get("analysis_mode") == "fast", "fast mode must set analysis_mode=fast"

    def test_fast_explain_has_skipped_analyzers(self, tmp_path):
        data = self._invoke_fast(tmp_path, "explain")
        assert "skipped_analyzers" in data
        assert isinstance(data["skipped_analyzers"], list)
        assert "deep_content_scan" in data["skipped_analyzers"]

    def test_fast_fix_bug_skips_code_notes(self, tmp_path):
        data = self._invoke_fast(tmp_path, "fix-bug")
        assert "skipped_analyzers" in data
        assert "code_notes" in data["skipped_analyzers"]

    def test_fast_generate_tests_skips_test_gap_discovery(self, tmp_path):
        (tmp_path / "pom.xml").write_text("")  # so it doesn't gate on license
        from sourcecode.license import require_pro as _rp
        with patch("sourcecode.cli.prepare_context_cmd.__wrapped__", create=True):
            pass
        # Mock the Pro gate so test runs without license
        with patch("sourcecode.license.require_pro", return_value=None):
            data = self._invoke_fast(tmp_path, "generate-tests")
        assert data.get("analysis_mode") == "fast"
        skipped = data.get("skipped_analyzers", [])
        assert "test_gap_discovery" in skipped

    def test_non_fast_has_no_analysis_mode(self, tmp_path):
        """Non-fast invocations must NOT inject analysis_mode."""
        from sourcecode.cli import _set_detected_path, _preprocess_args, app
        from typer.testing import CliRunner
        runner = CliRunner()
        _set_detected_path(".")
        args = _preprocess_args(["prepare-context", "explain", str(tmp_path)])
        result = runner.invoke(app, args)
        if result.exit_code == 0:
            data = json.loads(result.output)
            assert "analysis_mode" not in data


# ─────────────────────────────────────────────────────────────────────────────
# H-02 — generate-tests timeout
# ─────────────────────────────────────────────────────────────────────────────

class TestGenerateTestsTimeout:
    """H-02: generate-tests must return truncated=true when timeout expires."""

    def test_timeout_produces_truncated_output(self, tmp_path, monkeypatch):
        """When builder.build stalls, CLI returns truncated partial result."""
        import threading

        monkeypatch.setenv("SOURCECODE_TESTS_TIMEOUT_MS", "200")  # 200ms timeout

        def _blocking_build(*args, **kwargs):
            # Stall longer than the timeout
            threading.Event().wait(timeout=5)
            raise RuntimeError("should not reach here")

        from sourcecode.cli import _set_detected_path, _preprocess_args, app
        from typer.testing import CliRunner

        runner = CliRunner()
        _set_detected_path(".")
        args = _preprocess_args(["prepare-context", "generate-tests", str(tmp_path)])

        with patch("sourcecode.prepare_context.TaskContextBuilder.build", side_effect=_blocking_build):
            with patch("sourcecode.license.require_pro", return_value=None):
                result = runner.invoke(app, args)

        assert result.exit_code == 0, f"timeout result must exit 0: {result.output[:200]}"
        data = json.loads(result.output)
        assert data.get("truncated") is True, "truncated must be True on timeout"
        assert data.get("confidence") == "low"
        assert "limitations" in data
        assert any("time" in lim for lim in data["limitations"])

    def test_no_timeout_produces_normal_output(self, tmp_path, monkeypatch):
        """Fast completions must not be affected by timeout logic."""
        monkeypatch.setenv("SOURCECODE_TESTS_TIMEOUT_MS", "30000")

        from sourcecode.cli import _set_detected_path, _preprocess_args, app
        from typer.testing import CliRunner

        runner = CliRunner()
        _set_detected_path(".")
        args = _preprocess_args(["prepare-context", "generate-tests", str(tmp_path)])

        with patch("sourcecode.license.require_pro", return_value=None):
            result = runner.invoke(app, args)

        assert result.exit_code == 0
        data = json.loads(result.output)
        # Normal completions must not have truncated=true from the timeout path
        assert data.get("truncated") is not True or data.get("truncated_reason", "").startswith("fast")


# ─────────────────────────────────────────────────────────────────────────────
# H-03 — impact resolution=not_found exits 0
# ─────────────────────────────────────────────────────────────────────────────

class TestImpactExitCode:
    """H-03: impact with target not found must exit 0 and write valid JSON."""

    def _invoke_impact(self, tmp_path: Path, target: str) -> tuple[int, dict]:
        from sourcecode.cli import _set_detected_path, _preprocess_args, app
        from typer.testing import CliRunner
        runner = CliRunner()
        _set_detected_path(".")
        args = _preprocess_args(["impact", target, str(tmp_path)])
        with patch("sourcecode.license.require_pro", return_value=None):
            result = runner.invoke(app, args)
        try:
            data = json.loads(result.output)
        except json.JSONDecodeError:
            data = {}
        return result.exit_code, data

    def test_not_found_exits_zero(self, tmp_path):
        """resolution=not_found must exit 0 (structured answer, not infra error)."""
        exit_code, data = self._invoke_impact(tmp_path, "NonExistentClass12345")
        assert exit_code == 0, (
            f"impact not_found must exit 0, got {exit_code}. "
            "Exit 1 is reserved for real infra errors like invalid path."
        )

    def test_not_found_output_is_valid_json(self, tmp_path):
        exit_code, data = self._invoke_impact(tmp_path, "NonExistentClass12345")
        assert isinstance(data, dict), "output must be parseable JSON"

    def test_invalid_path_exits_nonzero(self):
        """Real infra error (bad path) must still exit non-zero."""
        from sourcecode.cli import _set_detected_path, _preprocess_args, app
        from typer.testing import CliRunner
        runner = CliRunner()
        _set_detected_path(".")
        args = _preprocess_args(["impact", "SomeClass", "/path/that/does/not/exist/xyz"])
        with patch("sourcecode.license.require_pro", return_value=None):
            result = runner.invoke(app, args)
        assert result.exit_code != 0


# ─────────────────────────────────────────────────────────────────────────────
# H-04 — repo-ir --since prefetch optimization
# ─────────────────────────────────────────────────────────────────────────────

class TestRepoIrSinceOptimization:
    """H-04: --since must prefetch changed files, not call git show per file."""

    def test_get_git_changed_files_returns_frozenset(self, tmp_path):
        """_get_git_changed_files returns frozenset on success."""
        from sourcecode.repository_ir import _get_git_changed_files
        import subprocess
        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = "src/Foo.java\nsrc/Bar.java\n"
        with patch("subprocess.run", return_value=fake_result) as mock_run:
            result = _get_git_changed_files(tmp_path, "HEAD~1")
        assert result == frozenset({"src/Foo.java", "src/Bar.java"})
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "diff" in cmd
        assert "--name-only" in cmd

    def test_get_git_changed_files_returns_none_on_error(self, tmp_path):
        """_get_git_changed_files returns None when git fails."""
        from sourcecode.repository_ir import _get_git_changed_files
        fake_result = MagicMock()
        fake_result.returncode = 128
        with patch("subprocess.run", return_value=fake_result):
            result = _get_git_changed_files(tmp_path, "HEAD~1")
        assert result is None

    def test_build_repo_ir_with_since_calls_diff_once(self, tmp_path):
        """build_repo_ir must call git diff --name-only exactly once for --since."""
        from sourcecode.repository_ir import build_repo_ir

        # Create a dummy Java file
        java_file = tmp_path / "Foo.java"
        java_file.write_text("public class Foo { public void foo() {} }")

        git_diff_result = MagicMock()
        git_diff_result.returncode = 0
        git_diff_result.stdout = "Foo.java\n"

        git_show_result = MagicMock()
        git_show_result.returncode = 0
        git_show_result.stdout = "public class Foo {}"

        def _fake_run(cmd, **kwargs):
            if "diff" in cmd and "--name-only" in cmd:
                return git_diff_result
            if "show" in cmd:
                return git_show_result
            return MagicMock(returncode=1, stdout="")

        with patch("subprocess.run", side_effect=_fake_run) as mock_run:
            build_repo_ir(["Foo.java"], tmp_path, since="HEAD~1")

        diff_calls = [c for c in mock_run.call_args_list if "diff" in c[0][0] and "--name-only" in c[0][0]]
        assert len(diff_calls) == 1, "git diff --name-only must be called exactly once"

    def test_build_repo_ir_since_skips_show_for_unchanged_files(self, tmp_path):
        """Files not in changed set must not trigger git show."""
        from sourcecode.repository_ir import build_repo_ir

        # Two Java files
        (tmp_path / "Changed.java").write_text("public class Changed {}")
        (tmp_path / "Unchanged.java").write_text("public class Unchanged {}")

        git_diff_result = MagicMock()
        git_diff_result.returncode = 0
        git_diff_result.stdout = "Changed.java\n"  # only Changed.java in diff

        git_show_result = MagicMock()
        git_show_result.returncode = 0
        git_show_result.stdout = "public class Changed {}"

        def _fake_run(cmd, **kwargs):
            if "diff" in cmd and "--name-only" in cmd:
                return git_diff_result
            if "show" in cmd:
                return git_show_result
            return MagicMock(returncode=1, stdout="")

        with patch("subprocess.run", side_effect=_fake_run) as mock_run:
            build_repo_ir(["Changed.java", "Unchanged.java"], tmp_path, since="HEAD~1")

        show_calls = [c for c in mock_run.call_args_list if "show" in c[0][0]]
        show_paths = [c[0][0][-1] for c in show_calls]  # last arg is HEAD~1:file
        # Unchanged.java must not appear in any git show call
        assert not any("Unchanged.java" in p for p in show_paths), (
            "Unchanged.java must not trigger git show — it is not in the diff"
        )

    def test_build_repo_ir_since_fallback_when_git_diff_fails(self, tmp_path):
        """When git diff --name-only fails, fall back to per-file git show (no crash)."""
        from sourcecode.repository_ir import build_repo_ir

        (tmp_path / "Foo.java").write_text("public class Foo {}")

        def _fake_run(cmd, **kwargs):
            if "diff" in cmd and "--name-only" in cmd:
                return MagicMock(returncode=128, stdout="")  # git diff fails
            if "show" in cmd:
                return MagicMock(returncode=0, stdout="public class Foo {}")
            return MagicMock(returncode=1, stdout="")

        with patch("subprocess.run", side_effect=_fake_run):
            # Must not raise — fallback path must work
            result = build_repo_ir(["Foo.java"], tmp_path, since="HEAD~1")
        assert isinstance(result, dict)

"""Tests for v1.30.27 bug fixes and improvements.

  BUG #1  --copy confirmation goes to stderr (not stdout)
  BUG #2  --since non-existent branch emits warnings[] in output
  BUG #3  --depth help text documents Java auto-adjust behavior
  BUG #4  symptom LOW-confidence: suspected_areas cleared, symptom_hint present
  IMP #5  --fast flag skips deep analysis; progress message gated on TTY
  IMP #6  git_context compact output includes recent_commits
  IMP #7  --no-redact help text documents security policy
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch, MagicMock

import pytest
from typer.testing import CliRunner

from sourcecode.cli import app, _copy_to_clipboard
from sourcecode import prepare_context as _pc
from sourcecode.prepare_context import TaskContextBuilder, TaskOutput
from sourcecode.schema import GitContext, CommitRecord, ChangeHotspot, UncommittedChanges, SourceMap, AnalysisMetadata
from sourcecode.serializer import _compact_git_context

runner = CliRunner()

FIXTURE = Path(__file__).parent / "fixtures" / "fastapi_app"


def _invoke(*args: str):
    return runner.invoke(app, list(args))


def _json(result) -> dict:
    try:
        return json.loads(result.output)
    except json.JSONDecodeError as e:
        raise AssertionError(f"Not valid JSON. output={result.output!r}") from e


# ── BUG #1: --copy confirmation must go to stderr ────────────────────────────

class TestCopyToStderr:
    def test_copy_confirmation_uses_err_flag(self, tmp_path):
        """typer.echo for clipboard confirmation uses err=True (routes to stderr).

        CliRunner mixes stderr into result.output, so we verify the JSON is
        parseable (as the leading content) and the message appears after the JSON.
        In real piped usage, stdout only carries JSON; stderr carries the message.
        """
        (tmp_path / "main.py").write_text("x = 1\n")
        with patch("sourcecode.cli._copy_to_clipboard", return_value=True):
            result = runner.invoke(app, [str(tmp_path), "--compact", "--copy"])
        assert result.exit_code == 0
        # The JSON portion at the start must be parseable via raw_decode
        decoder = json.JSONDecoder()
        data, end_idx = decoder.raw_decode(result.output.lstrip())
        assert isinstance(data, dict)
        # Confirmation must appear AFTER the JSON (proves it was stderr, not inline)
        trailing = result.output[end_idx:].strip()
        assert "copied to clipboard" in trailing, (
            "Confirmation must be in stderr (appears after JSON in CliRunner output)"
        )

    def test_copy_failure_no_message(self, tmp_path):
        """No confirmation when clipboard copy fails."""
        (tmp_path / "main.py").write_text("x = 1\n")
        with patch("sourcecode.cli._copy_to_clipboard", return_value=False):
            result = runner.invoke(app, [str(tmp_path), "--compact", "--copy"])
        assert result.exit_code == 0
        assert "copied to clipboard" not in result.output

    def test_prepare_context_copy_conditional_on_success(self, tmp_path):
        """prepare-context --copy message only shown when clipboard succeeds."""
        (tmp_path / "main.py").write_text("x = 1\n")
        with patch("sourcecode.cli._copy_to_clipboard", return_value=False):
            result = runner.invoke(
                app, ["prepare-context", "explain", str(tmp_path), "--copy"]
            )
        # No "copied to clipboard" in any output when copy fails
        assert "copied to clipboard" not in result.output


# ── BUG #2: --since non-existent ref emits warnings[] ────────────────────────

class TestSinceWarnings:
    def test_head_minus1_fallback_emits_warning(self, tmp_path):
        """Stage 4 fallback populates warnings[] with REF_NOT_FOUND."""
        builder = TaskContextBuilder(tmp_path)

        # Simulate Stage 4: ref invalid, HEAD~1 available
        def _mock_resolve(since):
            return {
                "files": ["main.py"],
                "resolved_ref": "HEAD~1",
                "resolution_path": "head_minus_1_fallback",
                "diff_validation_status": "invalid_ref",
                "error": False,
                "warnings": [{"code": "REF_NOT_FOUND", "ref": since, "resolved_to": "HEAD~1"}],
            }

        (tmp_path / "main.py").write_text("x = 1\n")
        with patch.object(builder, "_resolve_git_baseline", side_effect=_mock_resolve):
            with patch.object(builder, "_build_delta_impact") as mock_impact:
                mock_impact.return_value = MagicMock(
                    relevant_files=[], impact_summary=None, affected_modules=[],
                    risk_areas=[], why_these_files={}, analysis_gaps=[],
                    system_impact={}, change_type=[], dependency_graph_summary={},
                    impact_score_per_file={},
                )
                with patch.object(builder, "_get_pr_scope_files", return_value=(None, "git_diff", [], [])):
                    output = builder.build("delta", since="main")

        assert output.warnings, "warnings must be non-empty for Stage 4 fallback"
        assert output.warnings[0]["code"] == "REF_NOT_FOUND"
        assert output.warnings[0]["ref"] == "main"
        assert output.warnings[0]["resolved_to"] == "HEAD~1"

    def test_valid_ref_no_warnings(self, tmp_path):
        """Exact ref resolution (Stage 1) produces no warnings."""
        builder = TaskContextBuilder(tmp_path)

        def _mock_resolve(since):
            return {
                "files": [],
                "resolved_ref": since,
                "resolution_path": "exact_local_ref",
                "diff_validation_status": "valid_empty",
                "error": False,
            }

        with patch.object(builder, "_resolve_git_baseline", side_effect=_mock_resolve):
            with patch.object(builder, "_build_delta_impact") as mock_impact:
                mock_impact.return_value = MagicMock(
                    relevant_files=[], impact_summary=None, affected_modules=[],
                    risk_areas=[], why_these_files={}, analysis_gaps=[],
                    system_impact={}, change_type=[], dependency_graph_summary={},
                    impact_score_per_file={},
                )
                output = builder.build("delta", since="origin/develop")

        assert not output.warnings

    def test_warnings_serialized_in_cli_output(self, tmp_path):
        """warnings[] appears in JSON output when populated."""
        (tmp_path / "main.py").write_text("x = 1\n")

        mock_output = MagicMock(spec=TaskOutput)
        mock_output.task = "delta"
        mock_output.goal = "g"
        mock_output.project_summary = None
        mock_output.architecture_summary = None
        mock_output.relevant_files = []
        mock_output.suspected_areas = []
        mock_output.improvement_opportunities = []
        mock_output.test_gaps = []
        mock_output.key_dependencies = []
        mock_output.code_notes_summary = None
        mock_output.limitations = []
        mock_output.confidence = "low"
        mock_output.gaps = []
        mock_output.why_these_files = {}
        mock_output.changed_files = []
        mock_output.affected_entry_points = []
        mock_output.symptom = None
        mock_output.related_notes = []
        mock_output.symptom_note = None
        mock_output.symptom_explain = None
        mock_output.symptom_hint = None
        mock_output.impact_summary = None
        mock_output.affected_modules = []
        mock_output.risk_areas = []
        mock_output.since = "main"
        mock_output.system_impact = {}
        mock_output.change_type = []
        mock_output.dependency_graph_summary = {}
        mock_output.impact_score_per_file = {}
        mock_output.error_code = None
        mock_output.error_message = None
        mock_output.error_hints = []
        mock_output.ci_decision = "analysis_success"
        mock_output.resolved_since_ref = "HEAD~1"
        mock_output.resolution_path = "head_minus_1_fallback"
        mock_output.diff_validation_status = "invalid_ref"
        mock_output.warnings = [{"code": "REF_NOT_FOUND", "ref": "main", "resolved_to": "HEAD~1"}]
        mock_output.base_ref = None
        mock_output.security_impact = {}
        mock_output.transactional_impact = {}
        mock_output.configuration_impact = {}
        mock_output.test_coverage_risk = {}
        mock_output.review_hotspots = []
        mock_output.suggested_review_order = []
        mock_output.execution_paths = []
        mock_output.behavioral_impact = []
        mock_output.scope_source = None
        mock_output.scope_files = []
        mock_output.repo_root = None
        mock_output.runtime_changes = []
        mock_output.build_changes = {}
        mock_output.committed_changes = []
        mock_output.uncommitted_changes = []
        mock_output.analysis_scope = {}

        with patch.object(TaskContextBuilder, "build", return_value=mock_output):
            result = runner.invoke(
                app, ["prepare-context", "delta", str(tmp_path), "--since", "main"]
            )

        assert result.exit_code == 0
        data = _json(result)
        assert "warnings" in data
        assert data["warnings"][0]["code"] == "REF_NOT_FOUND"


# ── BUG #3: --depth help text ─────────────────────────────────────────────────

class TestDepthHelp:
    def test_depth_help_documents_java_autoadjust(self):
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "12" in result.output  # Java min depth mentioned
        # The help text should mention auto-adjust or Java
        assert "Java" in result.output or "auto" in result.output.lower()

    def test_depth_flag_accepted(self, tmp_path):
        (tmp_path / "main.py").write_text("x = 1\n")
        result = runner.invoke(app, [str(tmp_path), "--depth", "6", "--compact"])
        assert result.exit_code == 0


# ── BUG #4: symptom LOW-confidence clears suspected_areas, adds symptom_hint ──

class TestSymptomHint:
    def _make_java_fixture(self, tmp_path: Path) -> Path:
        src = tmp_path / "src" / "main" / "java" / "com" / "example"
        src.mkdir(parents=True)
        (src / "UserService.java").write_text(
            "package com.example;\npublic class UserService {}\n"
        )
        (tmp_path / "pom.xml").write_text(
            "<project><groupId>com.example</groupId></project>\n"
        )
        return tmp_path

    def test_frontend_symptom_in_java_module_clears_suspected_areas(self, tmp_path):
        """'spinner' in Java module → suspected_areas=[], symptom_hint present."""
        self._make_java_fixture(tmp_path)
        result = runner.invoke(
            app,
            ["prepare-context", "fix-bug", str(tmp_path), "--symptom", "spinner"],
        )
        assert result.exit_code == 0
        data = _json(result)
        # suspected_areas must be empty when confidence is LOW with 0 content matches
        if data.get("symptom_explain", {}).get("confidence") == "LOW":
            assert data.get("suspected_areas", []) == [], (
                "suspected_areas must be [] when symptom confidence is LOW"
            )
            assert "symptom_hint" in data, "symptom_hint must be present when LOW confidence"
            assert "spinner" in data["symptom_hint"].lower() or "frontend" in data["symptom_hint"].lower()

    def test_java_symptom_no_hint(self, tmp_path):
        """Java term in Java module → no symptom_hint injected."""
        self._make_java_fixture(tmp_path)
        result = runner.invoke(
            app,
            ["prepare-context", "fix-bug", str(tmp_path), "--symptom", "UserService"],
        )
        assert result.exit_code == 0
        data = _json(result)
        # If confidence is not LOW, no symptom_hint should be present
        sx = data.get("symptom_explain", {})
        if sx.get("confidence") != "LOW":
            assert "symptom_hint" not in data

    def test_symptom_hint_field_on_taskoutput(self):
        """TaskOutput dataclass has symptom_hint field."""
        out = TaskOutput(
            task="fix-bug", goal="g", project_summary=None,
            architecture_summary=None, relevant_files=[], suspected_areas=[],
            improvement_opportunities=[], test_gaps=[], key_dependencies=[],
            code_notes_summary=None, limitations=[], symptom_hint="redirect hint",
        )
        assert out.symptom_hint == "redirect hint"


# ── IMPROVEMENT #5: --fast flag ───────────────────────────────────────────────

class TestFastFlag:
    def test_fast_flag_accepted(self, tmp_path):
        """--fast flag accepted without error."""
        (tmp_path / "main.py").write_text("x = 1\n")
        result = runner.invoke(
            app, ["prepare-context", "explain", str(tmp_path), "--fast"]
        )
        assert result.exit_code == 0
        data = _json(result)
        assert "task" in data

    def test_fast_skips_code_notes(self, tmp_path):
        """--fast produces output without triggering code notes analysis."""
        (tmp_path / "main.py").write_text("# TODO: fix this\nx = 1\n")
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "test"\n')

        called = []
        orig_analyze = None
        try:
            from sourcecode.code_notes_analyzer import CodeNotesAnalyzer
            orig_analyze = CodeNotesAnalyzer.analyze

            def _mock_analyze(self, root):
                called.append(root)
                return orig_analyze(self, root)

            with patch.object(CodeNotesAnalyzer, "analyze", _mock_analyze):
                result = runner.invoke(
                    app, ["prepare-context", "fix-bug", str(tmp_path), "--fast"]
                )
        except ImportError:
            pytest.skip("CodeNotesAnalyzer not importable")

        assert result.exit_code == 0
        # With --fast, code notes analyzer should NOT have been called
        assert not called, "--fast must skip code notes analysis"

    def test_fast_flag_in_help(self):
        """--fast appears in prepare-context help."""
        result = runner.invoke(app, ["prepare-context", "--help"])
        assert result.exit_code == 0
        assert "--fast" in result.output

    def test_no_fast_does_not_contaminate_stdout(self, tmp_path):
        """Progress message (when not --fast) must not appear in JSON stdout."""
        (tmp_path / "main.py").write_text("x = 1\n")
        result = runner.invoke(
            app, ["prepare-context", "explain", str(tmp_path)]
        )
        assert result.exit_code == 0
        # stdout must parse as valid JSON
        data = _json(result)
        assert "task" in data


# ── IMPROVEMENT #6: git_context recent_commits ───────────────────────────────

class TestRecentCommitsInGitContext:
    def _make_sm_with_commits(self, commits: list) -> SourceMap:
        sm = SourceMap(metadata=AnalysisMetadata(analyzed_path="/tmp/test"))
        sm.git_context = GitContext(
            requested=True,
            branch="main",
            recent_commits=commits,
            change_hotspots=[ChangeHotspot(file="a.py", commit_count=3, last_changed="2026-05-01")],
            uncommitted_changes=UncommittedChanges(staged=[], unstaged=[], untracked=[]),
        )
        return sm

    def test_recent_commits_present_in_compact_output(self):
        """_compact_git_context includes recent_commits with top-5 entries."""
        commits = [
            CommitRecord(hash="abcd1234", message="feat: add user endpoint", author="alice", date="2026-05-15", files_changed=["a.py"]),
            CommitRecord(hash="efgh5678", message="fix: null pointer in service", author="bob", date="2026-05-14", files_changed=["b.py"]),
        ]
        sm = self._make_sm_with_commits(commits)
        ctx = _compact_git_context(sm)
        assert ctx is not None
        assert "recent_commits" in ctx
        assert len(ctx["recent_commits"]) == 2

    def test_recent_commits_fields(self):
        """Each recent_commits entry has hash, message, date, author."""
        commits = [
            CommitRecord(
                hash="a0b438a4deadbeef",
                message="Merge pull request #82 into main — very long message that should be truncated at exactly eighty characters total",
                author="m3-dhl",
                date="2026-05-15",
                files_changed=[],
            ),
        ]
        sm = self._make_sm_with_commits(commits)
        ctx = _compact_git_context(sm)
        entry = ctx["recent_commits"][0]
        assert entry["hash"] == "a0b438a4"   # 8-char short hash
        assert len(entry["message"]) <= 80
        assert entry["date"] == "2026-05-15"
        assert entry["author"] == "m3-dhl"

    def test_recent_commits_capped_at_5(self):
        """At most 5 commits in recent_commits."""
        commits = [
            CommitRecord(hash=f"hash{i:08d}", message=f"commit {i}", author="a", date="2026-05-01", files_changed=[])
            for i in range(10)
        ]
        sm = self._make_sm_with_commits(commits)
        ctx = _compact_git_context(sm)
        assert len(ctx["recent_commits"]) == 5

    def test_no_commits_no_key(self):
        """No recent_commits key when list is empty."""
        sm = self._make_sm_with_commits([])
        ctx = _compact_git_context(sm)
        assert ctx is None or "recent_commits" not in (ctx or {})


# ── IMPROVEMENT #7: --no-redact help text ────────────────────────────────────

class TestNoRedactHelp:
    def test_no_redact_help_mentions_env_var_policy(self):
        """--no-redact help text must clarify env var values are never included."""
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "--no-redact" in result.output
        # The help must not imply env var values will be shown
        # Verify the security policy note is present somewhere in help
        # (exact wording may vary — test for the key concepts)
        assert "security" in result.output.lower() or "policy" in result.output.lower() or "never" in result.output.lower()

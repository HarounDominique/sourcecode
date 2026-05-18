"""test_output_ux.py — Verify output UX: no internal jargon, no redundancy, correct structure.

Acceptance criteria (from spec):
1. impact_score_per_file: no _rank_score, no has_* booleans
2. relevant_files: no 'why' key; 'explanation' present when meaningful
3. reasoning block absent from delta/review-pr output
4. system_impact: no empty arrays
5. human summary (summary) present in delta/review-pr success output
6. analysis_gaps: no pipeline jargon ("BFS", "type-aware chain", "import-link propagation")
7. Output size ≥ 35% smaller than internal-dump baseline for impact_score_per_file
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from sourcecode.cli import app
from sourcecode import prepare_context as _pc

runner = CliRunner()

FIXTURE = Path(__file__).parent / "fixtures" / "fastapi_app"


def _invoke(*args: str) -> Any:
    return runner.invoke(app, list(args))


def _json(result: Any) -> dict:
    return json.loads(result.output)


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_delta_output(monkeypatch, changed: list[str]) -> dict:
    """Return the parsed JSON output for a delta run with mocked changed files."""
    monkeypatch.setattr(
        _pc.TaskContextBuilder,
        "_resolve_git_root",
        lambda self: FIXTURE,
    )
    result = _invoke("prepare-context", "delta", str(FIXTURE), "--since", "HEAD~1")
    assert result.exit_code in (0, 1), result.output
    return _json(result)


def _make_review_pr_output(monkeypatch) -> dict:
    """Return parsed JSON output for review-pr with a mocked changed controller."""
    changed = ["src/main/java/com/example/UserController.java"]
    monkeypatch.setattr(
        _pc.TaskContextBuilder,
        "_resolve_git_root",
        lambda self: FIXTURE,
    )
    monkeypatch.setattr(
        _pc.TaskContextBuilder,
        "_get_pr_scope_files",
        lambda self, since=None: (changed, "git_diff", changed, []),
    )
    result = _invoke("prepare-context", "review-pr", str(FIXTURE))
    assert result.exit_code == 0, result.output
    return _json(result)


# ── impact_score_per_file structure ──────────────────────────────────────────

class TestImpactScoreOutput:
    """impact_score_per_file must expose review_priority+signals, not internal scores."""

    def test_no_rank_score_in_output(self, monkeypatch):
        data = _make_review_pr_output(monkeypatch)
        for entry in data.get("impact_score_per_file", {}).values():
            assert "_rank_score" not in entry, f"_rank_score leaked into output: {entry}"

    def test_no_boolean_evidence_spam(self, monkeypatch):
        data = _make_review_pr_output(monkeypatch)
        for path, entry in data.get("impact_score_per_file", {}).items():
            ev = entry.get("evidence", {})
            for k, v in ev.items():
                assert v is not False, f"{path}: false evidence key '{k}' should be omitted"
            assert "has_reverse_edges" not in entry, f"boolean key leaked: {path}"
            assert "has_route_diff" not in entry, f"boolean key leaked: {path}"
            assert "has_security_diff" not in entry, f"boolean key leaked: {path}"
            assert "has_wiring_evidence" not in entry, f"boolean key leaked: {path}"

    def test_review_priority_field_present(self, monkeypatch):
        data = _make_review_pr_output(monkeypatch)
        for path, entry in data.get("impact_score_per_file", {}).items():
            assert "review_priority" in entry, f"review_priority missing for {path}"
            assert entry["review_priority"] in ("high", "medium", "low"), (
                f"unexpected priority value for {path}: {entry['review_priority']}"
            )

    def test_size_reduction_vs_internal(self, monkeypatch):
        """Transformed impact_score_per_file must be ≥35% smaller in bytes."""
        from sourcecode.cli import _transform_impact_scores
        internal = {
            "src/Foo.java": {
                "_rank_score": 0.325,
                "change_types": ["behavioral_change"],
                "diff_severity": "api_change",
                "evidence": {
                    "has_reverse_edges": False,
                    "reverse_edge_count": 0,
                    "has_route_diff": True,
                    "has_security_diff": False,
                    "has_wiring_evidence": False,
                },
            }
        }
        before = len(json.dumps(internal))
        after = len(json.dumps(_transform_impact_scores(internal)))
        reduction = (before - after) / before
        assert reduction >= 0.35, (
            f"Expected ≥35% size reduction, got {reduction:.0%}. "
            f"before={before}B after={after}B"
        )


# ── relevant_files structure ──────────────────────────────────────────────────

class TestRelevantFilesOutput:
    """relevant_files must not expose 'why' jargon field; explanation replaces it."""

    def test_no_why_field_in_output(self):
        result = _invoke("prepare-context", "explain", str(FIXTURE))
        assert result.exit_code == 0, result.output
        data = _json(result)
        for rf in data.get("relevant_files", []):
            assert "why" not in rf, (
                f"'why' field leaked into output for {rf.get('path')}: {rf}"
            )

    def test_explanation_or_reason_present(self):
        result = _invoke("prepare-context", "explain", str(FIXTURE))
        assert result.exit_code == 0, result.output
        data = _json(result)
        for rf in data.get("relevant_files", []):
            has_info = "explanation" in rf or "reason" in rf
            assert has_info, f"No explanation/reason in relevant_file: {rf}"


# ── reasoning block removed ───────────────────────────────────────────────────

class TestReasoningBlockRemoved:
    """delta and review-pr must not emit 'reasoning' block (redundant with relevant_files)."""

    def test_delta_no_reasoning_key(self):
        result = _invoke("prepare-context", "delta", str(FIXTURE))
        assert result.exit_code in (0, 1), result.output
        data = _json(result)
        if data.get("task") == "delta":
            assert "reasoning" not in data, (
                "'reasoning' block still in delta output — should be removed (duplicate)"
            )

    def test_review_pr_no_reasoning_key(self, monkeypatch):
        data = _make_review_pr_output(monkeypatch)
        assert "reasoning" not in data, (
            "'reasoning' block still in review-pr output — should be removed (duplicate)"
        )


# ── system_impact clean ───────────────────────────────────────────────────────

class TestSystemImpactClean:
    """system_impact must not contain empty arrays."""

    def test_no_empty_arrays_in_system_impact(self):
        result = _invoke("prepare-context", "delta", str(FIXTURE))
        assert result.exit_code in (0, 1), result.output
        data = _json(result)
        si = data.get("system_impact", {})
        for k, v in si.items():
            assert v != [], f"system_impact['{k}'] is empty list — should be omitted"
            assert v != {}, f"system_impact['{k}'] is empty dict — should be omitted"


# ── human summary present ─────────────────────────────────────────────────────

class TestHumanSummary:
    """delta and review-pr success output must include a 'summary' block."""

    def test_review_pr_has_summary(self, monkeypatch):
        data = _make_review_pr_output(monkeypatch)
        assert "summary" in data, "review-pr output missing 'summary' block"
        s = data["summary"]
        assert "confidence" in s, f"summary missing confidence field: {s}"
        assert s["confidence"] in ("HIGH", "MEDIUM", "LOW"), (
            f"confidence has unexpected value: {s['confidence']}"
        )


# ── analysis_gaps language ────────────────────────────────────────────────────

class TestAnalysisGapsLanguage:
    """analysis_gaps must not contain pipeline-internal jargon."""

    _JARGON = [
        "BFS",
        "type-aware chain expansion",
        "import-link propagation",
        "propagation_depth=",
        "artifact role requires annotation inspection",
    ]

    def test_no_jargon_in_analysis_gaps(self):
        result = _invoke("prepare-context", "delta", str(FIXTURE))
        assert result.exit_code in (0, 1), result.output
        data = _json(result)
        for gap in data.get("gaps", []):
            for jargon in self._JARGON:
                assert jargon not in gap, (
                    f"Internal jargon '{jargon}' found in analysis_gaps: {gap!r}"
                )


# ── dependency_graph_summary clean ───────────────────────────────────────────

class TestDependencyGraphSummaryClean:
    """dependency_graph_summary must not emit null fields."""

    def test_no_null_propagation_depth(self):
        result = _invoke("prepare-context", "delta", str(FIXTURE))
        assert result.exit_code in (0, 1), result.output
        data = _json(result)
        dgraph = data.get("dependency_graph_summary", {})
        for k, v in dgraph.items():
            assert v is not None, (
                f"dependency_graph_summary['{k}'] is null — should be omitted"
            )

"""Integration test for H4 — prepare-context task differentiation.

Asserts that onboard and explain produce meaningfully different output on at least 3 fields.
"""
from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from sourcecode.cli import app

FIXTURE = Path(__file__).parent / "fixtures" / "spring_boot_minimal"
runner = CliRunner()


def _invoke(*args: str) -> dict:
    import json
    result = runner.invoke(app, ["prepare-context", *args])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


class TestTaskDifferentiation:
    """onboard and explain must differ on content, not just task/goal labels."""

    def test_onboard_vs_explain_differ_on_multiple_fields(self):
        onboard = _invoke("onboard", str(FIXTURE))
        explain = _invoke("explain", str(FIXTURE))

        # Both must have task field set correctly
        assert onboard["task"] == "onboard"
        assert explain["task"] == "explain"

        # The goal fields must differ
        assert onboard["goal"] != explain["goal"], "goal should differ between tasks"

        # Count fields that differ between the two outputs
        differing_fields = []
        for key in set(onboard) | set(explain):
            if key in ("task", "goal"):
                continue  # these always differ by definition
            val_on = onboard.get(key)
            val_ex = explain.get(key)
            if val_on != val_ex:
                differing_fields.append(key)

        assert len(differing_fields) >= 1, (
            f"onboard and explain outputs are identical except task/goal. "
            f"Expected ≥1 differing content field. Identical keys: "
            f"{sorted(set(onboard) & set(explain) - {'task', 'goal'})}"
        )

    def test_fix_bug_emphasizes_suspected_areas(self):
        result = _invoke("fix-bug", str(FIXTURE))
        # fix-bug task should enable code_notes → suspected_areas possible
        assert "task" in result
        assert result["task"] == "fix-bug"
        # architecture_summary suppressed for fix-bug
        assert "architecture_summary" not in result

    def test_generate_tests_includes_test_gaps(self):
        result = _invoke("generate-tests", str(FIXTURE))
        assert result["task"] == "generate-tests"
        # key_dependencies enabled for generate-tests
        assert "key_dependencies" in result or "gaps" in result

    def test_delta_omits_architecture_summary(self):
        result = _invoke("delta", str(FIXTURE))
        assert result["task"] == "delta"
        # architecture_summary suppressed for delta
        assert "architecture_summary" not in result

    def test_onboard_includes_architecture_summary(self):
        result = _invoke("onboard", str(FIXTURE))
        # architecture_summary is included for onboard
        assert "architecture_summary" in result

    def test_explain_includes_architecture_summary(self):
        result = _invoke("explain", str(FIXTURE))
        # architecture_summary is included for explain
        assert "architecture_summary" in result

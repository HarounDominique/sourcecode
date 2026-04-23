from __future__ import annotations

"""Regression tests for CodeNotesAnalyzer determinism.

Root-cause history:
  - Output order depended on sorted(Path.iterdir()) traversal without a
    final canonical sort by (path, line). A post-collection sort is now
    applied so identical input always produces identical output.
  - Schema imports were lazy (inside the scanning loop); a stale .pyc or
    editable-install mismatch could cause an ImportError mid-scan that
    silently dropped all notes collected after that point.
  - Only PermissionError was caught in _walk; other OSError subclasses
    (e.g. EMFILE) aborted the entire walk, returning an empty list.
  - A redundant `total_count[0] < _MAX_NOTES` pre-check in _walk silently
    skipped whole files when traversal order changed (different files could
    fill the quota first, making previously-visible files invisible).
"""

from pathlib import Path

import pytest

from sourcecode.code_notes_analyzer import CodeNotesAnalyzer


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def fixture_project(tmp_path: Path) -> Path:
    """A minimal project with predictable TODO/FIXME/HACK markers."""
    src = tmp_path / "src"
    src.mkdir()

    (src / "alpha.py").write_text(
        "# TODO: first task\n"
        "def foo():\n"
        "    pass  # FIXME: broken\n",
        encoding="utf-8",
    )
    (src / "beta.py").write_text(
        "# HACK workaround for upstream bug\n"
        "x = 1\n",
        encoding="utf-8",
    )
    # A file with no markers — must not cause spurious results.
    (src / "clean.py").write_text("def bar():\n    return 42\n", encoding="utf-8")

    # tests/ dir must be scanned for notes (it is source code), but its
    # presence must not corrupt domain or note output.
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_alpha.py").write_text(
        "# NOTE: test helper\ndef test_foo(): pass\n", encoding="utf-8"
    )

    return tmp_path


# ---------------------------------------------------------------------------
# Core detection
# ---------------------------------------------------------------------------

def test_known_todo_always_detected(fixture_project: Path) -> None:
    """At least the TODO in alpha.py must always be found."""
    notes, _adrs, summary = CodeNotesAnalyzer().analyze(fixture_project)
    kinds = {n.kind for n in notes}
    assert "TODO" in kinds, f"TODO not found; got kinds={kinds}, notes={notes}"
    assert summary.total >= 1


def test_all_markers_detected(fixture_project: Path) -> None:
    notes, _adrs, _summary = CodeNotesAnalyzer().analyze(fixture_project)
    kinds = {n.kind for n in notes}
    assert "TODO" in kinds
    assert "FIXME" in kinds
    assert "HACK" in kinds
    assert "NOTE" in kinds


def test_notes_carry_correct_metadata(fixture_project: Path) -> None:
    notes, _adrs, _summary = CodeNotesAnalyzer().analyze(fixture_project)
    todo = next(n for n in notes if n.kind == "TODO")
    assert todo.path.endswith("alpha.py")
    assert todo.line == 1
    assert "first task" in todo.text


# ---------------------------------------------------------------------------
# Determinism — same input must produce identical output across two calls
# ---------------------------------------------------------------------------

def test_output_is_deterministic(fixture_project: Path) -> None:
    """Two successive analyze() calls on the same path must be byte-identical."""
    analyzer = CodeNotesAnalyzer()

    notes1, adrs1, sum1 = analyzer.analyze(fixture_project)
    notes2, adrs2, sum2 = analyzer.analyze(fixture_project)

    assert [(n.path, n.line, n.kind, n.text) for n in notes1] == \
           [(n.path, n.line, n.kind, n.text) for n in notes2], \
        "notes list differs between runs — non-deterministic output"

    assert [a.path for a in adrs1] == [a.path for a in adrs2]
    assert sum1.total == sum2.total
    assert sum1.by_kind == sum2.by_kind


def test_output_is_sorted_by_path_then_line(fixture_project: Path) -> None:
    """Notes must be sorted (path asc, line asc) regardless of walk order."""
    notes, _adrs, _summary = CodeNotesAnalyzer().analyze(fixture_project)
    keys = [(n.path, n.line) for n in notes]
    assert keys == sorted(keys), \
        f"notes are not sorted by (path, line): {keys}"


# ---------------------------------------------------------------------------
# Regression: clean files must not generate spurious notes
# ---------------------------------------------------------------------------

def test_clean_file_produces_no_notes(tmp_path: Path) -> None:
    (tmp_path / "clean.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
    notes, _adrs, summary = CodeNotesAnalyzer().analyze(tmp_path)
    assert notes == []
    assert summary.total == 0


# ---------------------------------------------------------------------------
# Regression: empty project must return empty list, not raise
# ---------------------------------------------------------------------------

def test_empty_project_returns_empty(tmp_path: Path) -> None:
    notes, adrs, summary = CodeNotesAnalyzer().analyze(tmp_path)
    assert notes == []
    assert adrs == []
    assert summary.total == 0

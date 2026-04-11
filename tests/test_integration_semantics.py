"""Tests de integracion E2E para SemanticAnalyzer y el flag --semantics.

SEM-ACC-01: test_python_project_produces_call_graph
SEM-ACC-02: test_symbol_links_resolve_imports
SEM-ACC-03: test_semantics_self_analysis
SEM-ACC-04: test_large_project_hits_max_files
SEM-ACC-05: test_semantics_flag_no_affect_base
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sourcecode.cli import app
from sourcecode.semantic_analyzer import SemanticAnalyzer

runner = CliRunner()


# ---------------------------------------------------------------------------
# SEM-ACC-01: Python project produces call graph
# ---------------------------------------------------------------------------

def test_python_project_produces_call_graph(tmp_path: Path) -> None:
    """SEM-ACC-01: SemanticAnalyzer produce calls para un proyecto Python con llamadas cross-file."""
    # Fixture: a.py define foo(), b.py importa y llama a foo()
    (tmp_path / "a.py").write_text("def foo():\n    return 42\n", encoding="utf-8")
    (tmp_path / "b.py").write_text(
        "from a import foo\n\ndef bar():\n    foo()\n",
        encoding="utf-8",
    )

    file_tree = {"a.py": None, "b.py": None}
    calls, symbols, links, summary = SemanticAnalyzer().analyze(tmp_path, file_tree)

    assert len(calls) > 0, (
        f"Expected call graph to be non-empty. calls={calls}, "
        f"limitations={summary.limitations}"
    )
    assert summary.language_coverage.get("python") == "full", (
        f"Expected python=full in language_coverage. Got: {summary.language_coverage}"
    )
    assert "python" in summary.languages


# ---------------------------------------------------------------------------
# SEM-ACC-02: Symbol links resolve imports
# ---------------------------------------------------------------------------

def test_symbol_links_resolve_imports(tmp_path: Path) -> None:
    """SEM-ACC-02: analyze() produce SymbolLink con source_path != None e is_external=False para imports internos."""
    # Fixture: pkg/__init__.py, pkg/models.py, consumer.py
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        "from .models import MyModel\n",
        encoding="utf-8",
    )
    (pkg / "models.py").write_text(
        "class MyModel:\n    pass\n",
        encoding="utf-8",
    )
    (tmp_path / "consumer.py").write_text(
        "from pkg.models import MyModel\n\ndef use():\n    m = MyModel()\n",
        encoding="utf-8",
    )

    file_tree = {
        "pkg": {
            "__init__.py": None,
            "models.py": None,
        },
        "consumer.py": None,
    }
    _calls, _symbols, links, summary = SemanticAnalyzer().analyze(tmp_path, file_tree)

    internal_links = [lnk for lnk in links if not lnk.is_external and lnk.source_path is not None]
    assert len(internal_links) > 0, (
        f"Expected at least one internal SymbolLink with source_path != None. "
        f"links={links}, limitations={summary.limitations}"
    )


# ---------------------------------------------------------------------------
# SEM-ACC-03: Self-analysis of project source directory
# ---------------------------------------------------------------------------

def test_semantics_self_analysis() -> None:
    """SEM-ACC-03: SemanticAnalyzer sobre el propio proyecto produce calls > 0."""
    project_root = Path(__file__).parent.parent
    src_dir = project_root / "src" / "sourcecode"

    if not src_dir.exists():
        pytest.skip("src/sourcecode not found — cannot run self-analysis test")

    # Build a minimal file_tree from the src/sourcecode directory (depth=1 only)
    # to keep the test fast (avoids analyzing hundreds of files)
    file_tree: dict = {}
    for p in src_dir.iterdir():
        if p.is_file() and p.suffix == ".py":
            file_tree[p.name] = None

    calls, _symbols, _links, summary = SemanticAnalyzer().analyze(src_dir, file_tree)

    assert summary.files_analyzed > 0, (
        f"Expected files_analyzed > 0. summary={summary}"
    )
    assert "python" in summary.languages, (
        f"Expected 'python' in languages. Got: {summary.languages}"
    )
    assert len(calls) > 0, (
        f"Expected call graph non-empty for self-analysis. "
        f"files_analyzed={summary.files_analyzed}, limitations={summary.limitations}"
    )


# ---------------------------------------------------------------------------
# SEM-ACC-04: Large project hits max_files
# ---------------------------------------------------------------------------

def test_large_project_hits_max_files(tmp_path: Path) -> None:
    """SEM-ACC-04: SemanticAnalyzer(max_files=5) trunca al analizar mas de 5 ficheros."""
    max_files = 5
    total_files = max_files + 5  # 10 files

    file_tree: dict = {}
    for i in range(total_files):
        fname = f"mod_{i}.py"
        (tmp_path / fname).write_text(f"def func_{i}(): pass\n", encoding="utf-8")
        file_tree[fname] = None

    _calls, _symbols, _links, summary = SemanticAnalyzer(max_files=max_files).analyze(
        tmp_path, file_tree
    )

    has_truncation = summary.truncated or any(
        "max_files_reached" in lim for lim in summary.limitations
    )
    assert has_truncation, (
        f"Expected truncation with max_files={max_files} and {total_files} files. "
        f"truncated={summary.truncated}, limitations={summary.limitations}"
    )


# ---------------------------------------------------------------------------
# SEM-ACC-05: --semantics CLI flag integration
# ---------------------------------------------------------------------------

def test_semantics_flag_no_affect_base(tmp_path: Path) -> None:
    """SEM-ACC-05: --semantics produce semantic_summary.requested=True; sin flag, requested=False/None."""
    # Fixture: dos ficheros Python con una llamada cross-file
    (tmp_path / "a.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text(
        "from a import foo\n\ndef bar():\n    foo()\n",
        encoding="utf-8",
    )

    # Sin --semantics: semantic_summary debe ser None o requested=False
    result_base = runner.invoke(app, [str(tmp_path)])
    assert result_base.exit_code == 0, (
        f"Base command failed: {result_base.output}"
    )
    data_base = json.loads(result_base.output)
    sem_base = data_base.get("semantic_summary")
    assert sem_base is None or sem_base.get("requested") is False, (
        f"semantic_summary.requested debe ser False/None sin --semantics. Got: {sem_base}"
    )

    # Con --semantics: semantic_summary.requested=True y semantic_calls es lista
    result_sem = runner.invoke(app, ["--semantics", str(tmp_path)])
    assert result_sem.exit_code == 0, (
        f"--semantics command failed: {result_sem.output}"
    )
    data_sem = json.loads(result_sem.output)
    assert "semantic_summary" in data_sem, "semantic_summary debe estar en el output con --semantics"
    assert data_sem["semantic_summary"] is not None, "semantic_summary no debe ser null con --semantics"
    assert data_sem["semantic_summary"]["requested"] is True, (
        f"semantic_summary.requested debe ser True con --semantics. Got: {data_sem['semantic_summary']}"
    )
    assert isinstance(data_sem.get("semantic_calls"), list), (
        f"semantic_calls debe ser lista con --semantics. Got: {data_sem.get('semantic_calls')}"
    )

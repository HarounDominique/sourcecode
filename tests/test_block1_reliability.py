from __future__ import annotations

"""Block 1 reliability tests — explicit failures, no silent nulls, cross-platform encoding.

Covers:
  RELY-SEM-01  semantics status=ok for working Python project
  RELY-SEM-02  semantics status=failed when no source files
  RELY-SEM-03  semantics status=failed when all files fail to parse
  RELY-SEM-04  semantics status=partial for low coverage (many skipped)
  RELY-SEM-05  Java produces lang_coverage["java"]="heuristic", not empty array
  RELY-SEM-06  semantic_symbols never contains null-field entries
  RELY-SEM-07  semantic_links never contains null-field entries
  RELY-SEM-08  serializer filters null-field entries before emitting
  RELY-GIT-01  hotspots_status="ok" when hotspot analysis succeeds
  RELY-GIT-02  hotspots_status="failed" when hotspot analysis throws
  RELY-GIT-03  change_hotspots=[] + hotspots_status="ok" = explicit "no hotspots"
  RELY-GIT-04  _parse_hotspots handles None/empty input without crash
  RELY-GIT-05  _run_git always returns str, never None
  RELY-ENC-01  _run_git encodes as UTF-8 (non-ASCII filenames don't crash)
  RELY-ENC-02  contract_pipeline subprocess guard handles result.stdout=None
"""

import json
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sourcecode.git_analyzer import GitAnalyzer, _parse_hotspots
from sourcecode.schema import (
    CallRecord,
    ChangeHotspot,
    GitContext,
    SemanticSummary,
    SourceMap,
    SymbolLink,
    SymbolRecord,
)
from sourcecode.semantic_analyzer import SemanticAnalyzer
from sourcecode.serializer import standard_view, normalize_source_map


# ---------------------------------------------------------------------------
# RELY-SEM-01: semantics status="ok" for working Python project
# ---------------------------------------------------------------------------

def test_semantics_status_ok_python(tmp_path: Path) -> None:
    """RELY-SEM-01: Python project with resolvable calls → status="ok"."""
    (tmp_path / "utils.py").write_text("def helper(): return 1\n", encoding="utf-8")
    (tmp_path / "main.py").write_text(
        "from utils import helper\n\ndef run():\n    helper()\n",
        encoding="utf-8",
    )
    file_tree = {"utils.py": None, "main.py": None}
    _calls, symbols, _links, summary = SemanticAnalyzer().analyze(tmp_path, file_tree)

    assert summary.status == "ok", f"Expected status='ok', got {summary.status!r}: {summary.reason}"
    assert summary.reason is None, f"Expected no reason for ok status, got {summary.reason!r}"
    assert summary.files_analyzed > 0
    assert len(symbols) > 0


# ---------------------------------------------------------------------------
# RELY-SEM-02: semantics status="failed" when no source files
# ---------------------------------------------------------------------------

def test_semantics_status_failed_no_source_files(tmp_path: Path) -> None:
    """RELY-SEM-02: No source files → status="failed" with explicit reason."""
    (tmp_path / "README.md").write_text("# Project\n", encoding="utf-8")
    file_tree = {"README.md": None}

    _calls, symbols, _links, summary = SemanticAnalyzer().analyze(tmp_path, file_tree)

    assert summary.status == "failed", (
        f"Expected status='failed' for no-source-files. Got {summary.status!r}"
    )
    assert summary.reason is not None, "Expected a reason string when status=failed"
    assert len(symbols) == 0


# ---------------------------------------------------------------------------
# RELY-SEM-03: semantics status="failed" when all files fail to parse
# ---------------------------------------------------------------------------

def test_semantics_status_failed_all_parse_errors(tmp_path: Path) -> None:
    """RELY-SEM-03: All Python files have syntax errors → status="failed" with reason."""
    (tmp_path / "broken.py").write_text("def (:\n    !!!invalid\n", encoding="utf-8")
    (tmp_path / "also_broken.py").write_text("class }{:\n", encoding="utf-8")
    file_tree = {"broken.py": None, "also_broken.py": None}

    _calls, symbols, _links, summary = SemanticAnalyzer().analyze(tmp_path, file_tree)

    assert summary.status == "failed", (
        f"Expected status='failed' when all files fail to parse. Got {summary.status!r}"
    )
    assert summary.reason is not None
    assert any("syntax_error" in lim for lim in summary.limitations), (
        f"Expected syntax_error in limitations. Got: {summary.limitations}"
    )


# ---------------------------------------------------------------------------
# RELY-SEM-04: semantics status="partial" for very low file coverage
# ---------------------------------------------------------------------------

def test_semantics_status_partial_low_coverage(tmp_path: Path) -> None:
    """RELY-SEM-04: Many files skipped → status="partial" with reason."""
    # 10 files: 1 valid, 9 too large (simulated via max_file_size=1)
    (tmp_path / "good.py").write_text("def ok(): pass\n", encoding="utf-8")
    file_tree: dict = {"good.py": None}
    for i in range(9):
        fname = f"big_{i}.py"
        (tmp_path / fname).write_text("def func(): pass\n" * 100, encoding="utf-8")
        file_tree[fname] = None

    # max_file_size=1 forces all except trivially small files to be skipped
    _calls, _symbols, _links, summary = SemanticAnalyzer(max_file_size=1).analyze(
        tmp_path, file_tree
    )

    # Either failed (all skipped) or partial (few analyzed) — never silent "ok" with 0 data
    assert summary.status in ("failed", "partial"), (
        f"Expected failed or partial when almost all files skipped. Got {summary.status!r}"
    )
    assert summary.reason is not None, "Expected reason when status != ok"


# ---------------------------------------------------------------------------
# RELY-SEM-05: Java uses heuristic analysis, not silent empty
# ---------------------------------------------------------------------------

def test_semantics_java_heuristic_not_empty(tmp_path: Path) -> None:
    """RELY-SEM-05: Java files produce lang_coverage['java']='heuristic', not silent empty."""
    java_src = """\
public class UserService {
    public void createUser(String name) {
        validateName(name);
    }
    private void validateName(String name) {}
}
"""
    (tmp_path / "UserService.java").write_text(java_src, encoding="utf-8")
    file_tree = {"UserService.java": None}

    _calls, symbols, _links, summary = SemanticAnalyzer().analyze(tmp_path, file_tree)

    assert "java" in summary.language_coverage, (
        f"Java files must appear in language_coverage. Got: {summary.language_coverage}"
    )
    assert summary.language_coverage["java"] == "heuristic", (
        f"Java support level must be 'heuristic'. Got: {summary.language_coverage['java']}"
    )
    assert "java" in summary.languages, f"Expected 'java' in languages. Got: {summary.languages}"
    java_symbols = [s for s in symbols if s.language == "java"]
    assert len(java_symbols) > 0, (
        f"Expected at least one Java symbol. Got 0. Check _analyze_java_file."
    )


# ---------------------------------------------------------------------------
# RELY-SEM-06: semantic_symbols never contains null-field entries
# ---------------------------------------------------------------------------

def test_semantics_symbols_no_null_fields(tmp_path: Path) -> None:
    """RELY-SEM-06: All SymbolRecord entries have non-null required fields."""
    (tmp_path / "mod.py").write_text(
        "def alpha(): pass\nclass Beta: pass\n",
        encoding="utf-8",
    )
    file_tree = {"mod.py": None}
    _calls, symbols, _links, summary = SemanticAnalyzer().analyze(tmp_path, file_tree)

    for sym in symbols:
        assert sym.symbol, f"SymbolRecord.symbol must not be null/empty: {sym}"
        assert sym.kind, f"SymbolRecord.kind must not be null/empty: {sym}"
        assert sym.language, f"SymbolRecord.language must not be null/empty: {sym}"
        assert sym.path, f"SymbolRecord.path must not be null/empty: {sym}"


# ---------------------------------------------------------------------------
# RELY-SEM-07: semantic_links never contains null-field entries on required fields
# ---------------------------------------------------------------------------

def test_semantics_links_no_null_required_fields(tmp_path: Path) -> None:
    """RELY-SEM-07: All SymbolLink entries have non-null importer_path and symbol."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "models.py").write_text("class Item: pass\n", encoding="utf-8")
    (tmp_path / "app.py").write_text(
        "from pkg.models import Item\n\ndef use():\n    Item()\n",
        encoding="utf-8",
    )
    file_tree = {"pkg": {"__init__.py": None, "models.py": None}, "app.py": None}
    _calls, _symbols, links, _summary = SemanticAnalyzer().analyze(tmp_path, file_tree)

    for lnk in links:
        assert lnk.importer_path, f"SymbolLink.importer_path must not be null/empty: {lnk}"
        assert lnk.symbol, f"SymbolLink.symbol must not be null/empty: {lnk}"


# ---------------------------------------------------------------------------
# RELY-SEM-08: serializer defensive filter removes null-field entries
# ---------------------------------------------------------------------------

def test_serializer_filters_null_field_symbols() -> None:
    """RELY-SEM-08: standard_view filters SymbolRecord/SymbolLink entries with null required fields."""
    # Construct a SourceMap with intentionally bad entries mixed with valid ones
    good_sym = SymbolRecord(symbol="foo", kind="function", language="python", path="a.py")
    # Simulate a corrupt entry by bypassing the dataclass constructor via object mutation
    bad_sym = SymbolRecord(symbol="ok", kind="function", language="python", path="b.py")
    object.__setattr__(bad_sym, "symbol", "")  # corrupt: empty symbol

    good_link = SymbolLink(importer_path="a.py", symbol="foo", is_external=True)
    bad_link = SymbolLink(importer_path="c.py", symbol="bar", is_external=True)
    object.__setattr__(bad_link, "importer_path", "")  # corrupt: empty importer_path

    summary = SemanticSummary(requested=True, status="ok")

    sm = SourceMap(
        semantic_symbols=[good_sym, bad_sym],
        semantic_links=[good_link, bad_link],
        semantic_summary=summary,
    )
    sm = normalize_source_map(sm)

    result = standard_view(sm)

    syms = result.get("semantic_symbols", [])
    links = result.get("semantic_links", [])

    assert all(s.get("symbol") for s in syms), (
        f"serializer must filter out null/empty symbol entries. Got: {syms}"
    )
    assert all(lnk.get("importer_path") for lnk in links), (
        f"serializer must filter out null/empty importer_path entries. Got: {links}"
    )
    assert len(syms) == 1, f"Expected 1 valid symbol, got {len(syms)}: {syms}"
    assert len(links) == 1, f"Expected 1 valid link, got {len(links)}: {links}"


# ---------------------------------------------------------------------------
# RELY-GIT-01: hotspots_status="ok" when hotspot analysis succeeds
# ---------------------------------------------------------------------------

def test_git_hotspots_status_ok(tmp_path: Path) -> None:
    """RELY-GIT-01: GitAnalyzer.analyze() sets hotspots_status='ok' when no exception."""
    import subprocess

    # Initialize a real git repo
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True)
    (tmp_path / "file.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)

    ctx = GitAnalyzer().analyze(tmp_path, days=90)

    assert ctx.requested is True
    assert ctx.hotspots_status == "ok", (
        f"Expected hotspots_status='ok' for valid git repo. Got {ctx.hotspots_status!r}. "
        f"limitations={ctx.limitations}"
    )


# ---------------------------------------------------------------------------
# RELY-GIT-02: hotspots_status="failed" when hotspot analysis throws
# ---------------------------------------------------------------------------

def test_git_hotspots_status_failed_on_exception(tmp_path: Path) -> None:
    """RELY-GIT-02: hotspots_status='failed' when git log raises an exception."""
    import subprocess
    import sourcecode.git_analyzer as _ga_module

    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True)
    (tmp_path / "file.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)

    # Capture the real function BEFORE patching to avoid recursion
    _real_run_git = _ga_module._run_git

    def _patched_run_git(args, cwd, timeout=15):
        # Let branch/commits/status/contributors succeed; fail only on hotspots log
        if "--since=" in " ".join(args) and "__HOTSPOT__" in " ".join(args):
            raise RuntimeError("simulated hotspot failure")
        return _real_run_git(args, cwd, timeout)

    with patch("sourcecode.git_analyzer._run_git", side_effect=_patched_run_git):
        ctx = GitAnalyzer().analyze(tmp_path, days=90)

    assert ctx.hotspots_status == "failed", (
        f"Expected hotspots_status='failed' after exception. Got {ctx.hotspots_status!r}"
    )
    assert ctx.change_hotspots == [], (
        f"change_hotspots must be [] when analysis failed. Got: {ctx.change_hotspots}"
    )
    assert any("hotspots_error" in lim for lim in ctx.limitations), (
        f"Expected 'hotspots_error:...' in limitations. Got: {ctx.limitations}"
    )


# ---------------------------------------------------------------------------
# RELY-GIT-03: change_hotspots=[] + hotspots_status="ok" = explicit "no hotspots"
# ---------------------------------------------------------------------------

def test_git_no_hotspots_explicit_ok(tmp_path: Path) -> None:
    """RELY-GIT-03: Clean repo with no changes produces change_hotspots=[] + status='ok'."""
    import subprocess

    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True)
    (tmp_path / "file.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)

    # Use 0-day window so no commits are in range
    ctx = GitAnalyzer().analyze(tmp_path, days=0)

    assert ctx.hotspots_status == "ok", (
        f"Expected hotspots_status='ok' even with empty results. Got {ctx.hotspots_status!r}"
    )
    # change_hotspots may be empty — that is semantically different from "failed"
    # Agent can distinguish: status="ok" + [] means "no activity", status="failed" means "broken"
    assert isinstance(ctx.change_hotspots, list)


# ---------------------------------------------------------------------------
# RELY-GIT-04: _parse_hotspots handles None/empty without crash
# ---------------------------------------------------------------------------

def test_parse_hotspots_handles_none() -> None:
    """RELY-GIT-04: _parse_hotspots(None) and _parse_hotspots('') return [] without crash."""
    result_none = _parse_hotspots(None)  # type: ignore[arg-type]
    assert result_none == [], f"Expected [] for None input. Got: {result_none}"

    result_empty = _parse_hotspots("")
    assert result_empty == [], f"Expected [] for empty string. Got: {result_empty}"

    result_whitespace = _parse_hotspots("   \n\n  ")
    assert result_whitespace == [], f"Expected [] for whitespace-only. Got: {result_whitespace}"


# ---------------------------------------------------------------------------
# RELY-GIT-05: _run_git returns str, never None
# ---------------------------------------------------------------------------

def test_run_git_stdout_never_none(tmp_path: Path) -> None:
    """RELY-GIT-05: _run_git always returns a str for stdout (never None)."""
    from sourcecode.git_analyzer import _run_git

    # Valid command on any platform — git --version always produces output
    stdout, _rc = _run_git(["--version"], tmp_path)
    assert stdout is not None, "_run_git must never return None for stdout"
    assert isinstance(stdout, str), f"stdout must be str, got {type(stdout).__name__}"

    # Invalid git command — should still return str (empty or error text)
    stdout_bad, _rc_bad = _run_git(["this-command-does-not-exist-xyz"], tmp_path)
    assert stdout_bad is not None
    assert isinstance(stdout_bad, str)


# ---------------------------------------------------------------------------
# RELY-ENC-01: _run_git encodes as UTF-8 (non-ASCII doesn't crash)
# ---------------------------------------------------------------------------

def test_run_git_utf8_encoding(tmp_path: Path) -> None:
    """RELY-ENC-01: git commands with non-ASCII commit messages don't crash."""
    import subprocess

    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Ñoño Ünïcödé"], cwd=tmp_path, capture_output=True)
    (tmp_path / "file.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
    # Commit with non-ASCII message: accents, ñ, emojis, extended Unicode
    subprocess.run(
        ["git", "commit", "-m", "añadir función: résumé émojis 🚀 中文 ñoño"],
        cwd=tmp_path,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )

    # This must not raise UnicodeDecodeError or any other exception
    ctx = GitAnalyzer().analyze(tmp_path, days=90)

    assert ctx.requested is True, "analyze() must complete without exception"
    # Hotspots status must not be "failed" due to encoding issues
    assert ctx.hotspots_status == "ok", (
        f"Non-ASCII commits must not break hotspot analysis. "
        f"hotspots_status={ctx.hotspots_status!r}, limitations={ctx.limitations}"
    )


# ---------------------------------------------------------------------------
# RELY-ENC-02: _parse_hotspots handles non-ASCII content gracefully
# ---------------------------------------------------------------------------

def test_parse_hotspots_non_ascii_content() -> None:
    """RELY-ENC-02: _parse_hotspots handles non-ASCII filenames and subjects."""
    output = (
        "__HOTSPOT__|2024-01-15T10:00:00+00:00|añadir función básica\n"
        "src/módulo_principal.py\n"
        "src/servicïo.py\n"
        "__HOTSPOT__|2024-01-10T08:00:00+00:00|fix: résumé endpoint\n"
        "api/rëport.py\n"
    )
    result = _parse_hotspots(output)

    assert isinstance(result, list), f"Expected list, got {type(result)}"
    # All entries should be ChangeHotspot with non-empty file paths
    for hotspot in result:
        assert isinstance(hotspot, ChangeHotspot)
        assert hotspot.file, f"ChangeHotspot.file must not be empty: {hotspot}"


# ---------------------------------------------------------------------------
# RELY-SEM-09: SemanticSummary schema backward compatibility
# ---------------------------------------------------------------------------

def test_semantic_summary_schema_backward_compat() -> None:
    """RELY-SEM-09: SemanticSummary with new fields serializes correctly via asdict()."""
    summary = SemanticSummary(
        requested=True,
        status="partial",
        reason="only 2 of 10 files analyzed",
        call_count=5,
        symbol_count=10,
        files_analyzed=2,
        files_skipped=8,
        language_coverage={"python": "full"},
        languages=["python"],
    )

    d = asdict(summary)
    assert d["status"] == "partial"
    assert d["reason"] == "only 2 of 10 files analyzed"
    assert d["requested"] is True
    assert d["call_count"] == 5

    # JSON round-trip must not raise
    serialized = json.dumps(d)
    reloaded = json.loads(serialized)
    assert reloaded["status"] == "partial"


# ---------------------------------------------------------------------------
# RELY-GIT-06: GitContext schema backward compatibility
# ---------------------------------------------------------------------------

def test_git_context_schema_backward_compat() -> None:
    """RELY-GIT-06: GitContext with hotspots_status serializes correctly via asdict()."""
    ctx = GitContext(
        requested=True,
        branch="main",
        change_hotspots=[ChangeHotspot(file="src/main.py", commit_count=5, last_changed="2024-01-15")],
        hotspots_status="ok",
        limitations=[],
    )

    d = asdict(ctx)
    assert d["hotspots_status"] == "ok"
    assert d["branch"] == "main"
    assert d["requested"] is True

    serialized = json.dumps(d)
    reloaded = json.loads(serialized)
    assert reloaded["hotspots_status"] == "ok"

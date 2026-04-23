from __future__ import annotations

"""Tests unitarios para DocAnalyzer — extraccion JS/TS JSDoc/TSDoc.

Plan 02: implementacion real de los 7 tests definidos en las especificaciones.
"""

from pathlib import Path

import pytest

from sourcecode.doc_analyzer import DocAnalyzer


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_ts_module_jsdoc_extracted(tmp_path: Path) -> None:
    """Primer bloque JSDoc del archivo produce DocRecord kind='module', source='docstring'."""
    content = """\
/** Module description. */

export const VERSION = "1.0.0";
"""
    (tmp_path / "mod.ts").write_text(content, encoding="utf-8")
    file_tree = {"mod.ts": None}

    analyzer = DocAnalyzer()
    records, _ = analyzer.analyze(tmp_path, file_tree, depth="module")

    assert len(records) >= 1
    rec = records[0]
    assert rec.kind == "module"
    assert rec.source == "docstring"
    assert "Module description" in (rec.doc_text or "")
    assert rec.language in ("typescript", "javascript")


def test_ts_function_jsdoc_extracted(tmp_path: Path) -> None:
    """JSDoc antes de export function produce DocRecord kind='function', source='docstring'."""
    content = """\
/** Adds two numbers. */
export function add(a: number, b: number): number {
    return a + b;
}
"""
    (tmp_path / "math.ts").write_text(content, encoding="utf-8")
    file_tree = {"math.ts": None}

    analyzer = DocAnalyzer()
    records, _ = analyzer.analyze(tmp_path, file_tree, depth="symbols")

    func_recs = [r for r in records if r.kind == "function"]
    assert len(func_recs) >= 1
    func_rec = func_recs[0]
    assert func_rec.source == "docstring"
    assert "Adds two numbers" in (func_rec.doc_text or "")


def test_ts_method_only_in_full_depth(tmp_path: Path) -> None:
    """JSDoc de constructor/metodo dentro de clase solo aparece en depth='full'."""
    content = """\
/** MyClass docs. */
export class MyClass {
    /** Constructor. */
    constructor() {}
}
"""
    (tmp_path / "myclass.ts").write_text(content, encoding="utf-8")
    file_tree = {"myclass.ts": None}

    analyzer = DocAnalyzer()
    records_symbols, _ = analyzer.analyze(tmp_path, file_tree, depth="symbols")
    records_full, _ = analyzer.analyze(tmp_path, file_tree, depth="full")

    # Constructor should NOT be in symbols
    symbols_kinds = {r.kind for r in records_symbols}
    assert "method" not in symbols_kinds

    # Constructor SHOULD be in full
    full_kinds = {r.kind for r in records_full}
    assert "method" in full_kinds


def test_ts_class_jsdoc_extracted_depth_symbols(tmp_path: Path) -> None:
    """JSDoc antes de class produce DocRecord kind='class' en depth='symbols'."""
    content = """\
/** MyClass docs. */
export class MyClass {
    value: number = 0;
}
"""
    (tmp_path / "myclass.ts").write_text(content, encoding="utf-8")
    file_tree = {"myclass.ts": None}

    analyzer = DocAnalyzer()
    records, _ = analyzer.analyze(tmp_path, file_tree, depth="symbols")

    class_recs = [r for r in records if r.kind == "class"]
    assert len(class_recs) >= 1
    assert class_recs[0].source == "docstring"
    assert "MyClass docs" in (class_recs[0].doc_text or "")


def test_ts_jsdoc_text_cleaned_no_at_tags(tmp_path: Path) -> None:
    """El doc_text no contiene lineas @param ni @returns."""
    content = """\
/**
 * Adds two numbers.
 * @param a First number
 * @param b Second number
 * @returns The sum
 */
export function add(a: number, b: number): number {
    return a + b;
}
"""
    (tmp_path / "add.ts").write_text(content, encoding="utf-8")
    file_tree = {"add.ts": None}

    analyzer = DocAnalyzer()
    records, _ = analyzer.analyze(tmp_path, file_tree, depth="symbols")

    assert len(records) >= 1
    for rec in records:
        if rec.doc_text:
            assert "@param" not in rec.doc_text
            assert "@returns" not in rec.doc_text


def test_unsupported_language_no_records_emitted(tmp_path: Path) -> None:
    """LQN-04: Archivo .go NO produce DocRecord (filtrado del output), limitation registrada."""
    content = """\
package main

// Main entry point
func main() {}
"""
    (tmp_path / "main.go").write_text(content, encoding="utf-8")
    file_tree = {"main.go": None}

    analyzer = DocAnalyzer()
    records, summary = analyzer.analyze(tmp_path, file_tree, depth="symbols")

    # No records emitted for unsupported language
    assert len(records) == 0
    # But limitation is still recorded
    assert any("docs_unavailable" in lim for lim in summary.limitations)
    assert any("main.go" in lim for lim in summary.limitations)


def test_unsupported_language_adds_limitation(tmp_path: Path) -> None:
    """Archivo .go agrega entrada a limitations con 'docs_unavailable'."""
    content = "package main\nfunc main() {}\n"
    (tmp_path / "main.go").write_text(content, encoding="utf-8")
    file_tree = {"main.go": None}

    analyzer = DocAnalyzer()
    _, summary = analyzer.analyze(tmp_path, file_tree, depth="symbols")

    assert any("docs_unavailable" in lim for lim in summary.limitations)
    assert any("main.go" in lim for lim in summary.limitations)


# ---------------------------------------------------------------------------
# Phase 9 Plan 02 — importance inference tests for JS/TS (Tests B1-B3)
# ---------------------------------------------------------------------------


def test_B1_ts_entry_point_importance_high(tmp_path: Path) -> None:
    """B1: analyze() with .ts entry point -> DocRecord for that path has importance='high'."""
    content = "/** Entry module. */\nexport function main() {}\n"
    (tmp_path / "index.ts").write_text(content, encoding="utf-8")
    file_tree = {"index.ts": None}

    analyzer = DocAnalyzer()
    records, _ = analyzer.analyze(
        tmp_path, file_tree, depth="module", entry_points=["index.ts"]
    )

    assert len(records) >= 1
    rec = records[0]
    assert rec.path == "index.ts"
    assert rec.importance == "high"


def test_B2_ts_depth2_function_importance_medium(tmp_path: Path) -> None:
    """B2: analyze() with .ts in 'src/utils/helper.ts' (depth=2) kind='function' -> importance='medium'."""
    src = tmp_path / "src" / "utils"
    src.mkdir(parents=True)
    (src / "helper.ts").write_text(
        "/** Helper function. */\nexport function helper(x: number): number { return x; }\n",
        encoding="utf-8",
    )
    file_tree = {"src": {"utils": {"helper.ts": None}}}

    analyzer = DocAnalyzer()
    records, _ = analyzer.analyze(tmp_path, file_tree, depth="symbols")

    func_recs = [r for r in records if r.kind == "function" and "helper.ts" in r.path]
    assert len(func_recs) >= 1
    assert func_recs[0].importance == "medium"


def test_B3_unsupported_language_no_records_for_file(tmp_path: Path) -> None:
    """B3: analyze() with unsupported language -> records empty for that file."""
    content = "package main\nfunc main() {}\n"
    (tmp_path / "server.go").write_text(content, encoding="utf-8")
    file_tree = {"server.go": None}

    analyzer = DocAnalyzer()
    records, _ = analyzer.analyze(tmp_path, file_tree, depth="symbols")

    go_records = [r for r in records if r.path == "server.go"]
    assert len(go_records) == 0, f"Expected no records for .go file, got: {go_records}"

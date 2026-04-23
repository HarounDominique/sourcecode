from __future__ import annotations

"""Tests unitarios para DocAnalyzer — extraccion Python AST.

Plan 02: implementacion real de los 10 tests definidos en las especificaciones.
"""

import textwrap
from pathlib import Path

import pytest

from sourcecode.doc_analyzer import DocAnalyzer


# ---------------------------------------------------------------------------
# Helper: crea un archivo Python en tmp_path y devuelve el file_tree dict
# ---------------------------------------------------------------------------

def _make_py_file(tmp_path: Path, filename: str, content: str) -> dict:
    """Crea un archivo .py en tmp_path y retorna su file_tree dict."""
    (tmp_path / filename).write_text(textwrap.dedent(content), encoding="utf-8")
    return {filename: None}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_python_module_docstring_extracted(tmp_path: Path) -> None:
    """Docstring de modulo produce DocRecord kind='module', source='docstring'."""
    file_tree = _make_py_file(
        tmp_path,
        "mymodule.py",
        '''\
        """Module docstring."""
        x = 1
        ''',
    )
    analyzer = DocAnalyzer()
    records, summary = analyzer.analyze(tmp_path, file_tree, depth="module")

    assert len(records) == 1
    rec = records[0]
    assert rec.kind == "module"
    assert rec.source == "docstring"
    assert rec.doc_text == "Module docstring."
    assert rec.language == "python"
    assert rec.path == "mymodule.py"


def test_python_class_docstring_extracted_depth_symbols(tmp_path: Path) -> None:
    """Clase con docstring aparece en depth='symbols' con kind='class'."""
    file_tree = _make_py_file(
        tmp_path,
        "mymod.py",
        '''\
        """Module doc."""

        class MyClass:
            """A documented class."""
            pass
        ''',
    )
    analyzer = DocAnalyzer()
    records, _ = analyzer.analyze(tmp_path, file_tree, depth="symbols")

    kinds = {r.kind for r in records}
    assert "class" in kinds

    class_rec = next(r for r in records if r.kind == "class")
    assert class_rec.symbol == "MyClass"
    assert class_rec.source == "docstring"
    assert class_rec.doc_text == "A documented class."


def test_python_method_only_in_full_depth(tmp_path: Path) -> None:
    """Metodo de clase aparece en depth='full' pero NO en depth='symbols'."""
    file_tree = _make_py_file(
        tmp_path,
        "mymod.py",
        '''\
        class MyClass:
            def my_method(self) -> None:
                """Method docstring."""
                pass
        ''',
    )
    analyzer = DocAnalyzer()
    records_symbols, _ = analyzer.analyze(tmp_path, file_tree, depth="symbols")
    records_full, _ = analyzer.analyze(tmp_path, file_tree, depth="full")

    # method NOT in symbols
    symbol_names = {r.symbol for r in records_symbols}
    assert "my_method" not in symbol_names

    # method IS in full
    full_names = {r.symbol for r in records_full}
    assert "my_method" in full_names

    method_rec = next(r for r in records_full if r.symbol == "my_method")
    assert method_rec.kind == "method"


def test_python_function_signature_with_types(tmp_path: Path) -> None:
    """Funcion con anotaciones de tipo produce signature no None."""
    file_tree = _make_py_file(
        tmp_path,
        "typed.py",
        '''\
        def f(x: int) -> str:
            """Documented."""
            return str(x)
        ''',
    )
    analyzer = DocAnalyzer()
    records, _ = analyzer.analyze(tmp_path, file_tree, depth="symbols")

    func_rec = next((r for r in records if r.symbol == "f"), None)
    assert func_rec is not None
    assert func_rec.signature is not None
    assert "x: int" in func_rec.signature
    assert "-> str" in func_rec.signature


def test_python_function_no_signature_without_types(tmp_path: Path) -> None:
    """Funcion sin anotaciones de tipo produce signature=None."""
    file_tree = _make_py_file(
        tmp_path,
        "untyped.py",
        '''\
        def f(x):
            """Documented."""
            return x
        ''',
    )
    analyzer = DocAnalyzer()
    records, _ = analyzer.analyze(tmp_path, file_tree, depth="symbols")

    func_rec = next((r for r in records if r.symbol == "f"), None)
    assert func_rec is not None
    assert func_rec.signature is None


def test_python_docstring_truncated_at_1000_chars(tmp_path: Path) -> None:
    """Docstring de 1200 chars se trunca a 1000 chars + '...[truncated]'."""
    long_doc = "x" * 1200
    file_tree = _make_py_file(
        tmp_path,
        "longdoc.py",
        f'''\
        def big_func():
            """{long_doc}"""
            pass
        ''',
    )
    analyzer = DocAnalyzer()
    records, summary = analyzer.analyze(tmp_path, file_tree, depth="symbols")

    func_rec = next((r for r in records if r.symbol == "big_func"), None)
    assert func_rec is not None
    assert func_rec.doc_text is not None
    assert func_rec.doc_text.endswith("...[truncated]")
    assert len(func_rec.doc_text) <= 1000 + len("...[truncated]")
    assert summary.truncated is True


def test_python_max_50_symbols_per_module(tmp_path: Path) -> None:
    """Modulo con 60 funciones documentadas produce max 50 DocRecords para ese modulo."""
    lines = []
    for i in range(60):
        lines.append(f'def func_{i}():\n    """Doc {i}."""\n    pass\n')
    content = "\n".join(lines)
    (tmp_path / "bigmod.py").write_text(content, encoding="utf-8")
    file_tree = {"bigmod.py": None}

    analyzer = DocAnalyzer()
    records, summary = analyzer.analyze(tmp_path, file_tree, depth="symbols")

    # Only records for bigmod.py functions (kind != "module")
    func_records = [r for r in records if r.kind == "function" and r.path == "bigmod.py"]
    assert len(func_records) <= 50


def test_python_syntax_error_graceful_degradation(tmp_path: Path) -> None:
    """Archivo Python con sintaxis invalida no causa crash; limitations contiene error."""
    (tmp_path / "broken.py").write_text("def broken(\n  x =\n", encoding="utf-8")
    file_tree = {"broken.py": None}

    analyzer = DocAnalyzer()
    records, summary = analyzer.analyze(tmp_path, file_tree, depth="symbols")

    # Should not crash; limitations should contain an error entry for this file
    assert any("python_parse_error" in lim for lim in summary.limitations)


def test_python_no_docstring_source_is_signature_when_typed(tmp_path: Path) -> None:
    """Funcion sin docstring pero con tipos produce source='signature'."""
    file_tree = _make_py_file(
        tmp_path,
        "nosig.py",
        '''\
        def f(x: int) -> str:
            return str(x)
        ''',
    )
    analyzer = DocAnalyzer()
    records, _ = analyzer.analyze(tmp_path, file_tree, depth="symbols")

    func_rec = next((r for r in records if r.symbol == "f"), None)
    assert func_rec is not None
    assert func_rec.source == "signature"
    assert func_rec.doc_text is None
    assert func_rec.signature is not None


def test_python_no_docstring_no_types_source_is_unavailable(tmp_path: Path) -> None:
    """Funcion sin docstring ni tipos produce source='unavailable' o no se emite."""
    file_tree = _make_py_file(
        tmp_path,
        "nothing.py",
        '''\
        def f(x):
            return x
        ''',
    )
    analyzer = DocAnalyzer()
    records, _ = analyzer.analyze(tmp_path, file_tree, depth="symbols")

    func_rec = next((r for r in records if r.symbol == "f"), None)
    # Either not emitted OR source='unavailable'
    if func_rec is not None:
        assert func_rec.source == "unavailable"


# ---------------------------------------------------------------------------
# Phase 9 Plan 02 — importance inference tests (Tests A1-A8)
# ---------------------------------------------------------------------------


def test_A1_entry_point_path_importance_high(tmp_path: Path) -> None:
    """A1: analyze() with entry_points=['main.py'] -> DocRecord for main.py has importance='high'."""
    file_tree = _make_py_file(
        tmp_path,
        "main.py",
        '''\
        """Main module."""
        ''',
    )
    analyzer = DocAnalyzer()
    records, _ = analyzer.analyze(tmp_path, file_tree, depth="module", entry_points=["main.py"])

    assert len(records) >= 1
    rec = records[0]
    assert rec.path == "main.py"
    assert rec.importance == "high"


def test_A2_depth2_function_importance_medium(tmp_path: Path) -> None:
    """A2: analyze() without entry_points -> DocRecord for 'src/core/base.py' (depth=2) kind='function' has importance='medium'."""
    src = tmp_path / "src" / "core"
    src.mkdir(parents=True)
    (src / "base.py").write_text(
        '"""Base module."""\n\ndef my_func(x: int) -> int:\n    """A function."""\n    return x\n',
        encoding="utf-8",
    )
    file_tree = {"src": {"core": {"base.py": None}}}
    analyzer = DocAnalyzer()
    records, _ = analyzer.analyze(tmp_path, file_tree, depth="symbols")

    func_recs = [r for r in records if r.kind == "function" and "base.py" in r.path]
    assert len(func_recs) >= 1
    assert func_recs[0].importance == "medium"


def test_A3_root_depth0_importance_high(tmp_path: Path) -> None:
    """A3: analyze() -> DocRecord for 'main.py' (depth=0) has importance='high'."""
    file_tree = _make_py_file(
        tmp_path,
        "main.py",
        '''\
        """Main module."""
        ''',
    )
    analyzer = DocAnalyzer()
    records, _ = analyzer.analyze(tmp_path, file_tree, depth="module")

    assert len(records) >= 1
    rec = next(r for r in records if r.path == "main.py")
    assert rec.importance == "high"


def test_A4_depth1_importance_high(tmp_path: Path) -> None:
    """A4: analyze() -> DocRecord for 'src/main.py' (depth=1) has importance='high'."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.py").write_text('"""Src main module."""\n', encoding="utf-8")
    file_tree = {"src": {"main.py": None}}
    analyzer = DocAnalyzer()
    records, _ = analyzer.analyze(tmp_path, file_tree, depth="module")

    module_recs = [r for r in records if "src/main.py" in r.path.replace("\\", "/")]
    assert len(module_recs) >= 1
    assert module_recs[0].importance == "high"


def test_A5_depth2_importance_medium_by_depth(tmp_path: Path) -> None:
    """A5: analyze() -> DocRecord for 'src/core/utils.py' (depth=2) has importance='medium' (by depth)."""
    src = tmp_path / "src" / "core"
    src.mkdir(parents=True)
    (src / "utils.py").write_text('"""Utils module."""\n', encoding="utf-8")
    file_tree = {"src": {"core": {"utils.py": None}}}
    analyzer = DocAnalyzer()
    records, _ = analyzer.analyze(tmp_path, file_tree, depth="module")

    module_recs = [r for r in records if "utils.py" in r.path]
    assert len(module_recs) >= 1
    assert module_recs[0].importance == "medium"


def test_A6_depth3_method_importance_low(tmp_path: Path) -> None:
    """A6: analyze() -> DocRecord kind='method' in 'src/core/base/helpers.py' (depth=3) has importance='low'."""
    deep = tmp_path / "src" / "core" / "base"
    deep.mkdir(parents=True)
    (deep / "helpers.py").write_text(
        'class Helper:\n    def do_it(self) -> None:\n        """Does it."""\n        pass\n',
        encoding="utf-8",
    )
    file_tree = {"src": {"core": {"base": {"helpers.py": None}}}}
    analyzer = DocAnalyzer()
    records, _ = analyzer.analyze(tmp_path, file_tree, depth="full")

    method_recs = [r for r in records if r.kind == "method" and "helpers.py" in r.path]
    assert len(method_recs) >= 1
    assert method_recs[0].importance == "low"


def test_A7_unsupported_language_no_records(tmp_path: Path) -> None:
    """A7: analyze() with unsupported language (.go) -> returned records do NOT contain source='unavailable'."""
    (tmp_path / "main.go").write_text("package main\nfunc main() {}\n", encoding="utf-8")
    file_tree = {"main.go": None}
    analyzer = DocAnalyzer()
    records, _ = analyzer.analyze(tmp_path, file_tree, depth="symbols")

    unavail = [r for r in records if r.source == "unavailable"]
    assert len(unavail) == 0, f"Found unexpected unavailable records: {unavail}"


def test_A8_unsupported_language_limitation_present(tmp_path: Path) -> None:
    """A8: analyze() with .go -> DocSummary.limitations contains 'docs_unavailable:{path}:language=go'."""
    (tmp_path / "main.go").write_text("package main\nfunc main() {}\n", encoding="utf-8")
    file_tree = {"main.go": None}
    analyzer = DocAnalyzer()
    _, summary = analyzer.analyze(tmp_path, file_tree, depth="symbols")

    assert any(
        "docs_unavailable" in lim and "language=go" in lim
        for lim in summary.limitations
    ), f"Expected docs_unavailable limitation, got: {summary.limitations}"

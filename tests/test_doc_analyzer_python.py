"""Tests unitarios para DocAnalyzer — extraccion Python AST.

Plan 02: implementacion real de los 10 tests definidos en las especificaciones.
"""
from __future__ import annotations

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

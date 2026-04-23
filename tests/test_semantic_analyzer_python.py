from __future__ import annotations

"""Tests unitarios para SemanticAnalyzer — SEM-PY-01..05 + stubs skip para planes futuros.

Wave 0: Tests SEM-PY-01..05 escritos en 12-01 (RED antes de implementar semantic_analyzer.py).
Los tests pasan de RED a GREEN despues de Tarea 2.

Stubs skip para planes futuros:
  - test_reexport_resolution: plan 12-02
  - test_star_import_expansion: plan 12-02
  - test_js_call_resolution: plan 12-03
  - test_go_heuristic_calls: plan 12-04
  - test_semantics_cli_flag: plan 12-04
"""

import textwrap
from pathlib import Path

import pytest

from sourcecode.semantic_analyzer import SemanticAnalyzer


# ---------------------------------------------------------------------------
# SEM-PY-01: Symbol index construction
# ---------------------------------------------------------------------------

def test_symbol_index_builds(tmp_path: Path):
    """SEM-PY-01: Pass 1 construye indice de simbolos con FunctionDef y ClassDef.

    Dado un fichero Python con 'def foo(): pass' y 'class Bar: pass',
    _build_symbol_index retorna dict con 'foo' y 'Bar' como SymbolRecord
    con kind correcto y line correcta.
    """
    src_file = tmp_path / "mymod.py"
    src_file.write_text(textwrap.dedent("""\
        def foo():
            pass

        class Bar:
            pass
    """), encoding="utf-8")

    file_tree = {"mymod.py": None}
    analyzer = SemanticAnalyzer()
    symbol_index = analyzer._build_symbol_index(tmp_path, ["mymod.py"])

    assert "mymod.py" in symbol_index
    symbols = {sr.symbol: sr for sr in symbol_index["mymod.py"]}

    assert "foo" in symbols
    assert symbols["foo"].kind == "function"
    assert symbols["foo"].line == 1

    assert "Bar" in symbols
    assert symbols["Bar"].kind == "class"
    assert symbols["Bar"].line == 4


# ---------------------------------------------------------------------------
# SEM-PY-02: Direct cross-file call resolution
# ---------------------------------------------------------------------------

def test_direct_call_resolution(tmp_path: Path):
    """SEM-PY-02: analyze() resuelve llamada cross-file via 'from target import greet; greet()'.

    Dado caller.py con 'from target import greet; greet()' y target.py con 'def greet(): pass',
    analyze() produce CallRecord con caller_path=caller, callee_path=target, callee_symbol='greet',
    confidence='high', method='ast'.
    """
    target_file = tmp_path / "target.py"
    target_file.write_text(textwrap.dedent("""\
        def greet():
            pass
    """), encoding="utf-8")

    caller_file = tmp_path / "caller.py"
    caller_file.write_text(textwrap.dedent("""\
        from target import greet

        def main():
            greet()
    """), encoding="utf-8")

    file_tree = {"caller.py": None, "target.py": None}
    analyzer = SemanticAnalyzer()
    calls, symbols, links, summary = analyzer.analyze(tmp_path, file_tree)

    # Buscar el CallRecord que nos interesa
    matching = [
        c for c in calls
        if c.callee_symbol == "greet" and c.caller_path == "caller.py"
    ]
    assert len(matching) >= 1, f"No se encontro CallRecord para greet(); calls={calls}"
    cr = matching[0]
    assert cr.callee_path == "target.py"
    assert cr.confidence == "high"
    assert cr.method == "ast"


# ---------------------------------------------------------------------------
# SEM-PY-03: Same-file call
# ---------------------------------------------------------------------------

def test_same_file_call(tmp_path: Path):
    """SEM-PY-03: analyze() produce CallRecord con caller_path == callee_path para llamadas en el mismo fichero.

    Dado un fichero Python con dos funciones donde una llama a la otra,
    analyze() produce CallRecord con caller_path == callee_path.
    """
    src_file = tmp_path / "utils.py"
    src_file.write_text(textwrap.dedent("""\
        def helper():
            return 42

        def main():
            result = helper()
            return result
    """), encoding="utf-8")

    file_tree = {"utils.py": None}
    analyzer = SemanticAnalyzer()
    calls, symbols, links, summary = analyzer.analyze(tmp_path, file_tree)

    matching = [
        c for c in calls
        if c.callee_symbol == "helper" and c.caller_symbol == "main"
    ]
    assert len(matching) >= 1, f"No se encontro CallRecord para helper(); calls={calls}"
    cr = matching[0]
    assert cr.caller_path == "utils.py"
    assert cr.callee_path == "utils.py"


# ---------------------------------------------------------------------------
# SEM-PY-04: Budget guards
# ---------------------------------------------------------------------------

def test_budget_guards(tmp_path: Path):
    """SEM-PY-04: Guards max_files y max_calls activos.

    max_files=2 y 5 ficheros -> SemanticSummary.truncated=True o limitations contiene 'max_files_reached'.
    max_calls=1 y 3 llamadas -> SemanticSummary.truncated=True y limitations contiene 'call_budget_reached'.
    """
    # Caso max_files: crear 5 ficheros, limitar a 2
    for i in range(5):
        (tmp_path / f"mod{i}.py").write_text(f"def func{i}(): pass\n", encoding="utf-8")

    file_tree = {f"mod{i}.py": None for i in range(5)}
    analyzer = SemanticAnalyzer(max_files=2)
    _calls, _symbols, _links, summary = analyzer.analyze(tmp_path, file_tree)

    has_truncation = summary.truncated or any(
        "max_files_reached" in lim for lim in summary.limitations
    )
    assert has_truncation, (
        f"Expected truncation with max_files=2 and 5 files. "
        f"truncated={summary.truncated}, limitations={summary.limitations}"
    )

    # Caso max_calls: un fichero con 3 llamadas, limitar a 1
    src_file = tmp_path / "calls.py"
    src_file.write_text(textwrap.dedent("""\
        def a(): pass
        def b(): pass
        def c(): pass
        def d(): pass

        def main():
            a()
            b()
            c()
    """), encoding="utf-8")

    file_tree2 = {"calls.py": None}
    analyzer2 = SemanticAnalyzer(max_calls=1)
    calls2, _s, _l, summary2 = analyzer2.analyze(tmp_path, file_tree2)

    assert summary2.truncated, (
        f"Expected truncated=True with max_calls=1. limitations={summary2.limitations}"
    )
    assert any("call_budget_reached" in lim for lim in summary2.limitations), (
        f"Expected 'call_budget_reached' in limitations. Got: {summary2.limitations}"
    )


# ---------------------------------------------------------------------------
# SEM-PY-05: Graceful degradation
# ---------------------------------------------------------------------------

def test_graceful_degradation(tmp_path: Path):
    """SEM-PY-05: SyntaxError, fichero inexistente y dynamic calls -> no excepcion, limitations correctas.

    - Fichero con SyntaxError -> no excepcion, limitations contiene entrada para ese fichero.
    - Fichero inexistente referenciado en file_tree -> no excepcion, limitations contiene 'read_error'.
    - Dynamic call (getattr) -> no call edge emitido, limitations puede contener 'dynamic_call_skipped'.
    """
    # Fichero con SyntaxError
    bad_file = tmp_path / "broken.py"
    bad_file.write_text("def foo(\n    # syntax error never closed\n", encoding="utf-8")

    # Fichero con dynamic call
    dyn_file = tmp_path / "dynamic.py"
    dyn_file.write_text(textwrap.dedent("""\
        def caller():
            fn = getattr(obj, 'method')
            fn()
    """), encoding="utf-8")

    file_tree = {
        "broken.py": None,
        "missing.py": None,   # este fichero no existe en disco
        "dynamic.py": None,
    }

    analyzer = SemanticAnalyzer()
    # No debe lanzar excepciones
    calls, symbols, links, summary = analyzer.analyze(tmp_path, file_tree)

    # SyntaxError registrado en limitations
    syntax_lims = [lim for lim in summary.limitations if "broken.py" in lim]
    assert len(syntax_lims) >= 1, (
        f"Expected limitation for broken.py (SyntaxError). Got: {summary.limitations}"
    )

    # Fichero inexistente registrado en limitations
    read_error_lims = [lim for lim in summary.limitations if "read_error" in lim and "missing.py" in lim]
    assert len(read_error_lims) >= 1, (
        f"Expected 'read_error:missing.py' limitation. Got: {summary.limitations}"
    )

    # Dynamic call no emite CallRecord para fn()
    dynamic_calls = [c for c in calls if c.caller_path == "dynamic.py" and c.callee_symbol == "fn"]
    assert len(dynamic_calls) == 0, (
        f"Expected no CallRecord for dynamic fn() call. Got: {dynamic_calls}"
    )


# ---------------------------------------------------------------------------
# Stubs skip para planes futuros
# ---------------------------------------------------------------------------

@pytest.mark.skip(reason="Implementado en plan 12-02: import resolution avanzada con reexports")
def test_reexport_resolution():
    """SEM-PY-STUB: Resolucion de reexports (plan 12-02).

    Dado pkg/__init__.py que hace 'from .mod import Foo' y caller.py que hace 'from pkg import Foo',
    analyze() debe resolver Foo al fichero original pkg/mod.py.
    Implementado en plan 12-02.
    """
    pass


@pytest.mark.skip(reason="Implementado en plan 12-02: star import expansion")
def test_star_import_expansion():
    """SEM-PY-STUB: Expansion de star imports (plan 12-02).

    Dado 'from pkg.mod import *', analyze() debe expandir los simbolos importados
    usando el __all__ del modulo fuente o sus definiciones publicas.
    Implementado en plan 12-02.
    """
    pass


@pytest.mark.skip(reason="Implementado en plan 12-03: JS/TS call resolution")
def test_js_call_resolution():
    """SEM-JS-STUB: Resolucion de llamadas JS/TS (plan 12-03).

    Dado caller.js con 'import { greet } from ./target; greet()' y target.js con 'export function greet()',
    analyze() debe producir CallRecord con callee_path=target.js y callee_symbol='greet'.
    Implementado en plan 12-03.
    """
    pass


def test_go_heuristic_calls(tmp_path: Path) -> None:
    """SEM-GO: _analyze_go_file emite CallRecord con method='heuristic' para llamadas locales Go."""
    content = "func hello() {}\nfunc main() {\n    hello()\n}\n"
    analyzer = SemanticAnalyzer()
    syms, calls = analyzer._analyze_go_file(content, "main.go")

    func_names = {s.symbol for s in syms}
    assert "hello" in func_names, f"Expected 'hello' in symbols. Got: {func_names}"
    assert "main" in func_names, f"Expected 'main' in symbols. Got: {func_names}"

    heuristic_calls = [c for c in calls if c.method == "heuristic"]
    assert len(heuristic_calls) > 0, f"Expected heuristic calls. Got: {calls}"
    assert all(c.confidence == "low" for c in heuristic_calls), (
        f"All Go calls should have confidence='low'. Got: {heuristic_calls}"
    )


def test_semantics_cli_flag(tmp_path: Path) -> None:
    """SEM-CLI: --semantics produce semantic_summary.requested=True en el output JSON.

    Coverage delegada a test_integration_semantics.py::test_semantics_flag_no_affect_base.
    Este test verifica el contrato minimo directamente via SemanticAnalyzer.
    """
    (tmp_path / "a.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
    file_tree = {"a.py": None}
    calls, symbols, links, summary = SemanticAnalyzer().analyze(tmp_path, file_tree)
    assert summary.requested is True
    assert summary.files_analyzed >= 1

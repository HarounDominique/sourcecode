from __future__ import annotations

"""Tests del schema semantico — SEM-SCHEMA-01..05.

Wave 0: Tests escritos en 12-01 antes de implementar el schema.
Estos tests pasan de RED (ImportError) a GREEN despues de Tarea 1.
"""

import dataclasses

import pytest

from sourcecode.schema import CallRecord, SemanticSummary, SourceMap, SymbolLink, SymbolRecord


# ---------------------------------------------------------------------------
# SEM-SCHEMA-01: SymbolRecord defaults
# ---------------------------------------------------------------------------

def test_symbol_record_defaults():
    """SEM-SCHEMA-01: SymbolRecord inicializa con defaults correctos."""
    sr = SymbolRecord(symbol="my_func", kind="function", language="python", path="src/foo.py")
    assert sr.symbol == "my_func"
    assert sr.kind == "function"
    assert sr.language == "python"
    assert sr.path == "src/foo.py"
    assert sr.line is None
    assert sr.qualified_name is None
    assert sr.exported is True
    assert sr.workspace is None


# ---------------------------------------------------------------------------
# SEM-SCHEMA-02: CallRecord defaults
# ---------------------------------------------------------------------------

def test_call_record_defaults():
    """SEM-SCHEMA-02: CallRecord inicializa con defaults correctos."""
    cr = CallRecord(
        caller_path="src/a.py",
        caller_symbol="foo",
        callee_path="src/b.py",
        callee_symbol="bar",
    )
    assert cr.caller_path == "src/a.py"
    assert cr.caller_symbol == "foo"
    assert cr.callee_path == "src/b.py"
    assert cr.callee_symbol == "bar"
    assert cr.call_line is None
    assert cr.confidence == "medium"
    assert cr.method == "heuristic"
    assert cr.args == []
    assert cr.kwargs == {}
    assert cr.workspace is None


def test_call_record_mutable_defaults_are_independent():
    """SEM-SCHEMA-02b: args y kwargs son instancias independientes (no compartidas)."""
    cr1 = CallRecord(caller_path="a.py", caller_symbol="f", callee_path="b.py", callee_symbol="g")
    cr2 = CallRecord(caller_path="a.py", caller_symbol="f", callee_path="b.py", callee_symbol="g")
    cr1.args.append("x")
    cr1.kwargs["k"] = "v"
    assert cr2.args == []
    assert cr2.kwargs == {}


# ---------------------------------------------------------------------------
# SEM-SCHEMA-03: SymbolLink defaults
# ---------------------------------------------------------------------------

def test_symbol_link_defaults():
    """SEM-SCHEMA-03: SymbolLink inicializa con defaults correctos."""
    sl = SymbolLink(importer_path="src/a.py", symbol="MyClass")
    assert sl.importer_path == "src/a.py"
    assert sl.symbol == "MyClass"
    assert sl.source_path is None
    assert sl.source_line is None
    assert sl.is_external is False
    assert sl.confidence == "high"
    assert sl.method == "ast"
    assert sl.workspace is None


# ---------------------------------------------------------------------------
# SEM-SCHEMA-04: SemanticSummary defaults
# ---------------------------------------------------------------------------

def test_semantic_summary_defaults():
    """SEM-SCHEMA-04: SemanticSummary() inicializa con todos los defaults correctos."""
    ss = SemanticSummary()
    assert ss.requested is False
    assert ss.call_count == 0
    assert ss.symbol_count == 0
    assert ss.link_count == 0
    assert ss.languages == []
    assert ss.language_coverage == {}
    assert ss.files_analyzed == 0
    assert ss.files_skipped == 0
    assert ss.truncated is False
    assert ss.limitations == []


# ---------------------------------------------------------------------------
# SEM-SCHEMA-05: SourceMap backward compatibility
# ---------------------------------------------------------------------------

def test_sourcemap_backward_compat():
    """SEM-SCHEMA-05: SourceMap() tiene nuevos campos semanticos y es backward-compatible."""
    sm = SourceMap()
    # Nuevos campos con defaults correctos
    assert sm.semantic_calls == []
    assert sm.semantic_symbols == []
    assert sm.semantic_links == []
    assert sm.semantic_summary is None

    # dataclasses.asdict incluye las nuevas claves
    d = dataclasses.asdict(sm)
    assert "semantic_calls" in d
    assert "semantic_symbols" in d
    assert "semantic_links" in d
    assert "semantic_summary" in d

    # SourceMap existente con campos previos sigue funcionando
    from sourcecode.schema import StackDetection
    sm2 = SourceMap(
        stacks=[StackDetection(stack="python")],
        project_type="api",
    )
    assert sm2.semantic_calls == []
    assert sm2.semantic_summary is None

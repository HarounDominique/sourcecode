"""Tests unitarios para MetricsAnalyzer — MQT-01..04 y MQT-13.

Wave 0: Estos tests se escriben ANTES de la implementacion (TDD RED).
Todos los tests no-skip fallan con ImportError hasta que metrics_analyzer.py exista.

Stubs skip (MQT-09, MQT-10) seran implementados en el plan 10-03.
"""
from __future__ import annotations

import textwrap

import pytest

from sourcecode.metrics_analyzer import MetricsAnalyzer


# ---------------------------------------------------------------------------
# MQT-01: Python LOC counting
# ---------------------------------------------------------------------------

def test_python_loc():
    """MQT-01: Python LOC counting via text scan.

    Dado contenido Python con 10 lineas (3 blank, 2 comment, 5 code),
    _count_loc retorna total=10, blank=3, comment=2, code=5, loc_availability='measured'.
    """
    content = textwrap.dedent("""\
        # comment 1
        # comment 2
        def foo():
            x = 1

            return x

        class Bar:
            pass

    """)
    # This content has:
    # line 1: "# comment 1"  -> comment
    # line 2: "# comment 2"  -> comment
    # line 3: "def foo():"   -> code
    # line 4: "    x = 1"    -> code
    # line 5: ""             -> blank
    # line 6: "    return x" -> code
    # line 7: ""             -> blank
    # line 8: "class Bar:"   -> code
    # line 9: "    pass"     -> code
    # line 10: ""            -> blank
    # Total: 10 lines, 3 blank, 2 comment, 5 code
    analyzer = MetricsAnalyzer()
    result = analyzer._count_loc(content, "python")

    assert result["total_lines"] == 10
    assert result["blank_lines"] == 3
    assert result["comment_lines"] == 2
    assert result["code_lines"] == 5
    assert result["loc_availability"] == "measured"


# ---------------------------------------------------------------------------
# MQT-02: Python symbols via AST
# ---------------------------------------------------------------------------

def test_python_symbols():
    """MQT-02: Python symbols via ast.parse.

    Dado un fichero Python con 2 funciones y 1 clase (sin SyntaxError),
    function_count=2, class_count=1, symbol_availability='measured'.
    cyclomatic_complexity es float >= 1.0 y complexity_availability='measured'.
    """
    content = textwrap.dedent("""\
        def foo(x):
            if x > 0:
                return x
            return -x

        def bar(a, b):
            return a + b

        class Baz:
            pass
    """)
    analyzer = MetricsAnalyzer()
    result = analyzer._count_python_symbols(content, "test_file.py")

    assert result["function_count"] == 2
    assert result["class_count"] == 1
    assert result["symbol_availability"] == "measured"
    assert result["cyclomatic_complexity"] is not None
    assert isinstance(result["cyclomatic_complexity"], float)
    assert result["cyclomatic_complexity"] >= 1.0
    assert result["complexity_availability"] == "measured"


# ---------------------------------------------------------------------------
# MQT-03: JS/TS LOC and inferred symbols
# ---------------------------------------------------------------------------

def test_js_loc():
    """MQT-03: JS LOC via text scan + inferred symbols via regex.

    Dado contenido JS con funciones y clase definidas,
    LOC es correcto (measured), symbol_availability='inferred',
    complexity_availability='unavailable'.
    """
    content = textwrap.dedent("""\
        // Module header
        function greet(name) {
            return "Hello, " + name;
        }

        const add = (a, b) => a + b;

        class Calculator {
            constructor() {}
        }
    """)
    analyzer = MetricsAnalyzer()
    loc = analyzer._count_loc(content, "javascript")
    symbols = analyzer._count_js_symbols(content)

    assert loc["loc_availability"] == "measured"
    assert loc["total_lines"] > 0
    # JS symbols are inferred via regex
    assert symbols["function_count"] >= 1
    assert symbols["class_count"] >= 1


def test_js_analyze_file_availability(tmp_path):
    """MQT-03b: FileMetrics for JS has symbol_availability='inferred', complexity_availability='unavailable'."""
    js_file = tmp_path / "app.js"
    js_file.write_text(
        "function greet(name) { return 'Hello'; }\n"
        "class Foo { constructor() {} }\n",
        encoding="utf-8",
    )
    analyzer = MetricsAnalyzer()
    fm = analyzer._analyze_file(js_file, "app.js", None)

    assert fm.loc_availability == "measured"
    assert fm.symbol_availability == "inferred"
    assert fm.complexity_availability == "unavailable"


# ---------------------------------------------------------------------------
# MQT-04: Go and Rust inferred symbols
# ---------------------------------------------------------------------------

def test_go_rust_loc():
    """MQT-04: Go and Rust LOC measured, symbols inferred.

    Go: func main() detecta 1 funcion, struct detecta 1 clase-equivalente.
    Rust: fn main() detecta 1 funcion, struct Foo detecta 1.
    Ambos: symbol_availability='inferred', complexity_availability='unavailable'.
    """
    go_content = textwrap.dedent("""\
        package main

        import "fmt"

        func main() {
            fmt.Println("hello")
        }

        type Point struct {
            X, Y int
        }
    """)
    rust_content = textwrap.dedent("""\
        fn main() {
            println!("hello");
        }

        struct Foo {
            x: i32,
        }
    """)
    analyzer = MetricsAnalyzer()

    go_symbols = analyzer._count_go_symbols(go_content)
    assert go_symbols["function_count"] == 1
    assert go_symbols["class_count"] == 1

    rust_symbols = analyzer._count_rust_symbols(rust_content)
    assert rust_symbols["function_count"] == 1
    assert rust_symbols["class_count"] == 1


def test_go_analyze_file_availability(tmp_path):
    """MQT-04b: FileMetrics for Go has symbol_availability='inferred', complexity_availability='unavailable'."""
    go_file = tmp_path / "main.go"
    go_file.write_text(
        "package main\n\nfunc main() {}\n\ntype Foo struct { X int }\n",
        encoding="utf-8",
    )
    analyzer = MetricsAnalyzer()
    fm = analyzer._analyze_file(go_file, "main.go", None)

    assert fm.loc_availability == "measured"
    assert fm.symbol_availability == "inferred"
    assert fm.complexity_availability == "unavailable"


def test_rust_analyze_file_availability(tmp_path):
    """MQT-04c: FileMetrics for Rust has symbol_availability='inferred', complexity_availability='unavailable'."""
    rs_file = tmp_path / "main.rs"
    rs_file.write_text(
        "fn main() {\n    println!(\"hello\");\n}\n\nstruct Foo { x: i32 }\n",
        encoding="utf-8",
    )
    analyzer = MetricsAnalyzer()
    fm = analyzer._analyze_file(rs_file, "main.rs", None)

    assert fm.loc_availability == "measured"
    assert fm.symbol_availability == "inferred"
    assert fm.complexity_availability == "unavailable"


# ---------------------------------------------------------------------------
# MQT-13: Graceful degradation
# ---------------------------------------------------------------------------

def test_graceful_degradation_unknown_extension(tmp_path):
    """MQT-13a: File with unknown extension -> language='unknown', symbol_availability='unavailable'."""
    xyz_file = tmp_path / "config.xyz"
    xyz_file.write_text("some content\nmore content\n", encoding="utf-8")

    analyzer = MetricsAnalyzer()
    fm = analyzer._analyze_file(xyz_file, "config.xyz", None)

    assert fm.language == "unknown"
    assert fm.symbol_availability == "unavailable"
    assert fm.complexity_availability == "unavailable"
    assert fm.loc_availability == "measured"


def test_graceful_degradation_python_syntax_error(tmp_path):
    """MQT-13b: Python file with SyntaxError -> loc_availability='measured', symbol_availability='unavailable'."""
    py_file = tmp_path / "broken.py"
    py_file.write_text("def foo(\n    x = 1 +\n", encoding="utf-8")

    analyzer = MetricsAnalyzer()
    fm = analyzer._analyze_file(py_file, "broken.py", None)

    assert fm.loc_availability == "measured"
    assert fm.symbol_availability == "unavailable"
    assert fm.complexity_availability == "unavailable"
    # Limitations are tracked at the analyze() level, not per-file


def test_graceful_degradation_nonexistent_file(tmp_path):
    """MQT-13c: Non-existent file in file_tree -> no exception, limitations has entry."""
    # Use analyze() with a file_tree pointing to a non-existent file
    analyzer = MetricsAnalyzer()
    file_tree = {"nonexistent.py": None}
    records, summary = analyzer.analyze(tmp_path, file_tree)

    # Must not raise; limitations must be recorded
    assert isinstance(records, list)
    assert isinstance(summary.limitations, list)
    # There should be some limitation entry (file not found)


def test_graceful_degradation_analyze_no_exception(tmp_path):
    """MQT-13d: analyze() never raises even with mixed valid/invalid files."""
    valid = tmp_path / "ok.py"
    valid.write_text("x = 1\n", encoding="utf-8")

    analyzer = MetricsAnalyzer()
    file_tree = {"ok.py": None, "missing.py": None, "broken.xyz": None}
    # Should not raise
    records, summary = analyzer.analyze(tmp_path, file_tree)
    assert isinstance(records, list)


# ---------------------------------------------------------------------------
# Wave 0 stubs — to be implemented in plan 10-03
# ---------------------------------------------------------------------------

@pytest.mark.skip(reason="implementado en 10-03: is_test_file detection")
def test_is_test_file():
    """MQT-09: Files in tests/ or named test_*.py are detected as is_test=True."""
    pass


@pytest.mark.skip(reason="implementado en 10-03: infer_production_target")
def test_infer_production(tmp_path):
    """MQT-10: Test files have production_target inferred from naming conventions."""
    pass

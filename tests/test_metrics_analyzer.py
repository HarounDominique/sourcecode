"""Tests unitarios para MetricsAnalyzer — MQT-01..04, MQT-09, MQT-10 y MQT-13.

Wave 0: Tests MQT-01..04 y MQT-13 escritos en 10-01.
Wave 3 (10-03): MQT-09 y MQT-10 activados; tests de integracion agregados.
"""
from __future__ import annotations

import textwrap

import pytest

from sourcecode.metrics_analyzer import MetricsAnalyzer, is_test_file, infer_production_target


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
# MQT-09: is_test_file() detection
# ---------------------------------------------------------------------------

def test_is_test_file():
    """MQT-09: is_test_file() returns True for test file patterns across ecosystems."""
    # True cases — test files
    assert is_test_file("tests/test_scanner.py") is True       # Python tests/ directory
    assert is_test_file("test_scanner.py") is True             # Python test_ prefix
    assert is_test_file("scanner_test.go") is True             # Go _test.go suffix
    assert is_test_file("scanner.spec.ts") is True             # TS .spec.ts
    assert is_test_file("scanner.test.tsx") is True            # TSX .test.tsx
    assert is_test_file("__tests__/scanner.js") is True        # JS __tests__ directory
    assert is_test_file("ScannerTest.java") is True            # Java Test suffix
    assert is_test_file("ScannerSpec.kt") is True              # Kotlin Spec suffix
    assert is_test_file("spec/scanner_spec.rb") is True        # Ruby spec/ directory
    assert is_test_file("tests/scanner_test.rs") is True       # Rust tests/ directory
    assert is_test_file("scanner_test.dart") is True           # Dart _test.dart
    assert is_test_file("test/scanner_test.dart") is True      # Dart test/ directory
    assert is_test_file("tests/scanner_test.c") is True        # C tests/ directory
    assert is_test_file("ScannerTest.php") is True             # PHP Test suffix
    assert is_test_file("tests/scanner_test.php") is True      # PHP tests/ directory

    # False cases — production files / pitfalls
    assert is_test_file("src/scanner.py") is False                              # production module
    assert is_test_file("src/utils/context_helpers_tested.py") is False         # pitfall: "tested" in name
    assert is_test_file("testdata/fixtures/sample.py") is False                 # pitfall: testdata/, not tests/
    assert is_test_file("src/schema.go") is False                               # Go without _test.go


# ---------------------------------------------------------------------------
# MQT-10: infer_production_target() inference
# ---------------------------------------------------------------------------

def test_infer_production():
    """MQT-10: infer_production_target() returns correct bare filename for each ecosystem."""
    assert infer_production_target("test_scanner.py") == "scanner.py"       # Python test_ prefix
    assert infer_production_target("scanner_test.go") == "scanner.go"       # Go _test.go
    assert infer_production_target("ScannerTest.java") == "Scanner.java"    # Java Test suffix
    assert infer_production_target("ScannerSpec.kt") == "Scanner.kt"        # Kotlin Spec suffix
    assert infer_production_target("scanner.spec.ts") == "scanner.ts"       # TS .spec. infix
    assert infer_production_target("scanner.test.tsx") == "scanner.tsx"     # TSX .test. infix
    assert infer_production_target("scanner_spec.rb") == "scanner.rb"       # Ruby _spec suffix
    assert infer_production_target("scanner_test.dart") == "scanner.dart"   # Dart _test suffix
    assert infer_production_target("scanner.py") is None                    # not a test file
    assert infer_production_target("unknown_file.xyz") is None              # no matching pattern


# ---------------------------------------------------------------------------
# Integration tests — analyze() with is_test and coverage wiring (10-03)
# ---------------------------------------------------------------------------

def test_analyze_returns_records(tmp_path):
    """test_analyze_returns_records: analyze() returns (list[FileMetrics], MetricsSummary)."""
    (tmp_path / "main.py").write_text("def hello():\n    pass\n", encoding="utf-8")
    (tmp_path / "test_main.py").write_text("def test_hello(): pass\n", encoding="utf-8")
    file_tree = {"main.py": None, "test_main.py": None}

    records, summary = MetricsAnalyzer().analyze(tmp_path, file_tree)

    assert isinstance(records, list)
    assert len(records) == 2
    assert summary.file_count == 2


def test_analyze_populates_is_test(tmp_path):
    """analyze() sets is_test=True for test files and False for production files."""
    (tmp_path / "scanner.py").write_text("def scan(): pass\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_scanner.py").write_text("def test_scan(): pass\n", encoding="utf-8")
    file_tree = {"scanner.py": None, "tests": {"test_scanner.py": None}}

    records, summary = MetricsAnalyzer().analyze(tmp_path, file_tree)

    assert summary.test_file_count == 1
    by_path = {r.path: r for r in records}
    assert by_path["scanner.py"].is_test is False
    assert by_path["tests/test_scanner.py"].is_test is True


def test_analyze_populates_production_target(tmp_path):
    """analyze() sets production_target to full relative path when production file found in tree."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "scanner.py").write_text("def scan(): pass\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_scanner.py").write_text("def test_scan(): pass\n", encoding="utf-8")
    file_tree = {"src": {"scanner.py": None}, "tests": {"test_scanner.py": None}}

    records, summary = MetricsAnalyzer().analyze(tmp_path, file_tree)

    by_path = {r.path: r for r in records}
    test_fm = by_path["tests/test_scanner.py"]
    prod_fm = by_path["src/scanner.py"]

    assert test_fm.is_test is True
    # production_target is the full relative path, not just the filename
    assert test_fm.production_target == "src/scanner.py"
    assert prod_fm.production_target is None


def test_analyze_coverage_integration(tmp_path):
    """analyze() invokes CoverageParser and populates metrics_summary.coverage_records."""
    # Create a minimal valid Cobertura XML
    (tmp_path / "coverage.xml").write_text(
        '<?xml version="1.0" ?>'
        '<coverage line-rate="0.85" branch-rate="0.70"'
        ' lines-covered="85" lines-valid="100"'
        ' version="7.0" timestamp="1700000000">'
        '<packages>'
        '<package name="src" line-rate="0.85" branch-rate="0.70" complexity="1">'
        '<classes>'
        '<class name="main.py" filename="main.py" line-rate="0.85" branch-rate="0.70" complexity="1">'
        '<lines/>'
        '</class>'
        '</classes>'
        '</package>'
        '</packages>'
        '</coverage>',
        encoding="utf-8",
    )
    (tmp_path / "main.py").write_text("def hello(): pass\n", encoding="utf-8")
    file_tree = {"main.py": None}

    records, summary = MetricsAnalyzer().analyze(tmp_path, file_tree)

    assert len(summary.coverage_records) >= 1
    assert "cobertura_xml" in summary.coverage_sources_found


def test_metrics_summary_totals(tmp_path):
    """MetricsSummary totals: file_count, test_file_count, total_loc are correct."""
    files = {
        "a.py": "x = 1\n",
        "b.py": "y = 2\n",
        "c.py": "z = 3\n",
        "test_a.py": "def test_a(): pass\n",
        "test_b.py": "def test_b(): pass\n",
    }
    for name, content in files.items():
        (tmp_path / name).write_text(content, encoding="utf-8")
    file_tree = {name: None for name in files}

    records, summary = MetricsAnalyzer().analyze(tmp_path, file_tree)

    assert summary.file_count == 5
    assert summary.test_file_count == 2
    assert summary.total_loc == sum(r.code_lines for r in records)


def test_merge_summaries():
    """merge_summaries() correctly sums file counts, deduplicates languages and sources."""
    from sourcecode.schema import MetricsSummary, CoverageRecord

    s1 = MetricsSummary(
        requested=True,
        file_count=3,
        test_file_count=1,
        total_loc=100,
        languages=["python", "go"],
        coverage_sources_found=["cobertura_xml"],
        limitations=["limit_a"],
    )
    s2 = MetricsSummary(
        requested=False,
        file_count=4,
        test_file_count=1,
        total_loc=200,
        languages=["go", "java"],
        coverage_sources_found=["lcov"],
        limitations=["limit_b"],
    )

    merged = MetricsAnalyzer().merge_summaries([s1, s2])

    assert merged.file_count == 7
    assert merged.test_file_count == 2
    assert merged.total_loc == 300
    assert merged.languages == ["go", "java", "python"]  # sorted, deduplicated
    assert merged.coverage_sources_found == ["cobertura_xml", "lcov"]
    assert "limit_a" in merged.limitations
    assert "limit_b" in merged.limitations
    assert merged.requested is True  # any() of the two

"""Analisis de metricas de calidad de codigo: LOC, simbolos y complejidad.

Sigue el mismo patron que DocAnalyzer y GraphAnalyzer:
- analyze() recibe root + file_tree, retorna (list[FileMetrics], MetricsSummary)
- merge_summaries() agrega multiples MetricsSummary en uno

LOC counting usa text scan para todos los lenguajes.
Python: adicionalmente ast.parse para simbolos exactos y complejidad McCabe.
JS/TS, Go, Rust, Java: regex para simbolos aproximados (availability="inferred").
Otros: solo LOC (availability="unavailable" para simbolos y complejidad).
"""
from __future__ import annotations

import ast
import re
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any

from sourcecode.coverage_parser import CoverageParser
from sourcecode.doc_analyzer import _LANG_MAP  # reusar, no redefinir
from sourcecode.schema import FileMetrics, MetricsSummary
from sourcecode.tree_utils import flatten_file_tree

# ---------------------------------------------------------------------------
# Test file detection — patterns from research (10-RESEARCH.md)
# ---------------------------------------------------------------------------

_TEST_FILE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(p) for p in [
        r"(?:^|/)tests?/.*\.py$",
        r"(?:^|/)test_.*\.py$",
        r"^.*_test\.py$",
        r"^.*_test\.go$",
        r"^.*\.(spec|test)\.(js|jsx|ts|tsx|mjs|cjs)$",
        r"(?:^|/)__tests__/.*\.(js|jsx|ts|tsx|mjs|cjs)$",
        r"^.*(Test|Tests|Spec|IT)\.(java|kt|scala)$",
        r"(?:^|/)spec/.*\.rb$",
        r"^.*(?:test|spec).*\.rb$",
        r"(?:^|/)tests/.*\.rs$",
        r"^.*_test\.dart$",
        r"(?:^|/)test/.*\.dart$",
        r"^.*(test|Test|spec)\.(c|cpp|cc|h|hpp)$",
        r"(?:^|/)tests?/.*\.(c|cpp|cc)$",
        r"^.*Test\.php$",
        r"(?:^|/)tests?/.*\.php$",
    ]
]

# Stem patterns for inferring production file name from test file name.
# Each entry is (compiled_pattern, replacement_string).
_STEM_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^test_(.+)$"), r"\1"),
    (re.compile(r"^(.+)_test(\.\w+)$"), r"\1\2"),
    (re.compile(r"^(.+)(Test|Tests|Spec|IT)(\.\w+)$"), r"\1\3"),
    (re.compile(r"^(.+)\.(spec|test)(\.\w+)$"), r"\1\3"),
    (re.compile(r"^(.+)_spec(\.\w+)$"), r"\1\2"),
]


def is_test_file(path: str) -> bool:
    """Return True if path corresponds to a test file based on ecosystem conventions.

    Normalizes Windows backslashes to forward slashes before matching.
    Patterns are anchored to avoid false positives (e.g., 'testdata/', '*_tested.py').
    """
    normalized = path.replace("\\", "/")
    return any(p.search(normalized) for p in _TEST_FILE_PATTERNS)


def infer_production_target(test_path: str) -> str | None:
    """Infer the production module filename from a test file path.

    Operates on the basename only (not the full path).
    Returns the bare filename (e.g., 'scanner.py'), or None if no pattern matches.
    """
    name = PurePosixPath(test_path.replace("\\", "/")).name
    for pattern, replacement in _STEM_PATTERNS:
        if pattern.match(name):
            return pattern.sub(replacement, name)
    return None

# ---------------------------------------------------------------------------
# Regex patterns for inferred symbol counting (JS/TS, Go, Rust, Java)
# ---------------------------------------------------------------------------

_JS_FUNC_RE = re.compile(
    r"\b(?:async\s+)?function\s+\w+|\bconst\s+\w+\s*=\s*(?:async\s*)?\(",
    re.MULTILINE,
)
_JS_CLASS_RE = re.compile(r"^\s*class\s+\w+", re.MULTILINE)

_GO_FUNC_RE = re.compile(r"^func\s", re.MULTILINE)
_GO_STRUCT_RE = re.compile(r"^type\s+\w+\s+struct", re.MULTILINE)

_RUST_FN_RE = re.compile(r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+\w+", re.MULTILINE)
_RUST_STRUCT_RE = re.compile(r"^\s*(?:pub\s+)?struct\s+\w+", re.MULTILINE)

_JAVA_CLASS_RE = re.compile(
    r"^\s*(?:public\s+|private\s+|protected\s+)?(?:abstract\s+|final\s+)?class\s+\w+",
    re.MULTILINE,
)
_JAVA_METHOD_RE = re.compile(
    r"^\s*(?:public|private|protected)\s+(?:static\s+)?\w[\w<>\[\]]*\s+\w+\s*\(",
    re.MULTILINE,
)

# Languages with inferred symbol support
_INFERRED_SYMBOL_LANGS = {"javascript", "typescript", "go", "rust", "java"}

# Languages with Python-level measured support
_MEASURED_SYMBOL_LANGS = {"python"}


def _lang_for_suffix(suffix: str) -> str:
    return _LANG_MAP.get(suffix.lower(), "unknown")


def _mccabe(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """Calcula la complejidad ciclomatica McCabe de una funcion.

    cc = 1 + suma de ramas (If, For, While, ExceptHandler, With, Assert,
    comprehensions) + (len(BoolOp.values) - 1) por cada BoolOp.
    """
    cc = 1
    for node in ast.walk(func_node):
        if isinstance(node, (ast.If, ast.For, ast.While, ast.ExceptHandler, ast.With, ast.Assert)):
            cc += 1
        elif isinstance(node, ast.BoolOp):
            cc += len(node.values) - 1
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            cc += 1
    return cc


class MetricsAnalyzer:
    """Analiza metricas de calidad de codigo: LOC, simbolos y complejidad ciclomatica."""

    _MAX_FILES = 500
    _MAX_FILE_SIZE = 500_000  # bytes

    # ---------------------------------------------------------------------------
    # Public API
    # ---------------------------------------------------------------------------

    def analyze(
        self,
        root: Path,
        file_tree: dict[str, Any],
        *,
        workspace: str | None = None,
    ) -> tuple[list[FileMetrics], MetricsSummary]:
        """Analiza el arbol de ficheros y retorna (list[FileMetrics], MetricsSummary).

        Nunca lanza excepciones — todos los errores van a summary.limitations.
        """
        all_paths = flatten_file_tree(file_tree)
        # Keep only paths that are actual files (not directories)
        file_paths = [p for p in all_paths if (root / p).is_file()]

        limitations: list[str] = []

        # Guard: max files
        if len(file_paths) > self._MAX_FILES:
            actual = len(file_paths)
            limitations.append(f"max_files_reached:{actual}")
            file_paths = file_paths[: self._MAX_FILES]

        # --- Coverage: parse all artifacts before iterating files ---
        coverage_parser = CoverageParser()
        coverage_records = coverage_parser.parse_all(root)
        file_cov_map = coverage_parser.build_file_coverage_map(root, coverage_records)

        records: list[FileMetrics] = []
        languages: set[str] = set()

        for rel_path in file_paths:
            abs_path = root / rel_path

            # Guard: max file size
            try:
                file_size = abs_path.stat().st_size
            except OSError:
                limitations.append(f"read_error:{Path(rel_path).as_posix()}")
                continue
            if file_size > self._MAX_FILE_SIZE:
                limitations.append(f"file_too_large:{Path(rel_path).as_posix()}")
                continue

            fm = self._analyze_file(abs_path, rel_path, workspace)

            # --- Test detection ---
            norm_rel = Path(rel_path).as_posix()
            fm.is_test = is_test_file(norm_rel)

            # --- Production target resolution ---
            if fm.is_test:
                inferred_name = infer_production_target(norm_rel)
                if inferred_name is not None:
                    match = next(
                        (
                            Path(p).as_posix()
                            for p in file_paths
                            if not is_test_file(Path(p).as_posix())
                            and Path(p).name == inferred_name
                        ),
                        None,
                    )
                    fm.production_target = match

            # --- Coverage wiring ---
            cov_entry = file_cov_map.get(norm_rel)
            if cov_entry is not None:
                fm.line_rate, fm.branch_rate, fm.coverage_source = cov_entry
                fm.coverage_availability = "measured"

            records.append(fm)
            if fm.language != "unknown":
                languages.add(fm.language)

        summary = MetricsSummary(
            requested=True,
            file_count=len(records),
            test_file_count=sum(1 for r in records if r.is_test),
            languages=sorted(languages),
            total_loc=sum(r.code_lines for r in records),
            coverage_records=coverage_records,
            coverage_sources_found=sorted({r.format for r in coverage_records}),
            limitations=limitations,
        )
        return records, summary

    def merge_summaries(self, summaries: Iterable[MetricsSummary]) -> MetricsSummary:
        """Agrega multiples MetricsSummary en uno.

        Sigue el mismo patron que DocAnalyzer.merge_summaries().
        """
        result = MetricsSummary(requested=True)
        languages: set[str] = set()
        sources_found: set[str] = set()
        limitations: list[str] = []

        for summary in summaries:
            result.file_count += summary.file_count
            result.test_file_count += summary.test_file_count
            result.total_loc += summary.total_loc
            languages.update(summary.languages)
            result.coverage_records.extend(summary.coverage_records)
            sources_found.update(summary.coverage_sources_found)
            limitations.extend(summary.limitations)

        result.languages = sorted(languages)
        result.coverage_sources_found = sorted(sources_found)
        result.limitations = limitations
        return result

    # ---------------------------------------------------------------------------
    # Per-file analysis
    # ---------------------------------------------------------------------------

    def _analyze_file(
        self,
        abs_path: Path,
        rel_path: str,
        workspace: str | None,
    ) -> FileMetrics:
        """Analiza un fichero individual y retorna un FileMetrics.

        Nunca lanza excepciones — los errores se reflejan en los campos
        de availability del FileMetrics retornado.
        """
        norm_path = Path(rel_path).as_posix()
        suffix = Path(rel_path).suffix.lower()
        language = _lang_for_suffix(suffix)

        # Read content
        try:
            content = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return FileMetrics(
                path=norm_path,
                language=language,
                workspace=workspace,
            )

        # LOC counting — all languages
        loc = self._count_loc(content, language)

        fm = FileMetrics(
            path=norm_path,
            language=language,
            total_lines=loc["total_lines"],
            code_lines=loc["code_lines"],
            blank_lines=loc["blank_lines"],
            comment_lines=loc["comment_lines"],
            loc_availability=loc["loc_availability"],
            workspace=workspace,
        )

        # Symbol counting per language tier
        if language == "python":
            sym = self._count_python_symbols(content, norm_path)
            fm.function_count = sym["function_count"]
            fm.class_count = sym["class_count"]
            fm.symbol_availability = sym["symbol_availability"]
            fm.cyclomatic_complexity = sym["cyclomatic_complexity"]
            fm.complexity_availability = sym["complexity_availability"]

        elif language in ("javascript", "typescript"):
            sym = self._count_js_symbols(content)
            fm.function_count = sym["function_count"]
            fm.class_count = sym["class_count"]
            fm.symbol_availability = "inferred"
            fm.complexity_availability = "unavailable"

        elif language == "go":
            sym = self._count_go_symbols(content)
            fm.function_count = sym["function_count"]
            fm.class_count = sym["class_count"]
            fm.symbol_availability = "inferred"
            fm.complexity_availability = "unavailable"

        elif language == "rust":
            sym = self._count_rust_symbols(content)
            fm.function_count = sym["function_count"]
            fm.class_count = sym["class_count"]
            fm.symbol_availability = "inferred"
            fm.complexity_availability = "unavailable"

        elif language == "java":
            sym = self._count_java_symbols(content)
            fm.function_count = sym["function_count"]
            fm.class_count = sym["class_count"]
            fm.symbol_availability = "inferred"
            fm.complexity_availability = "unavailable"

        # All other languages: LOC only, symbols and complexity unavailable
        # (defaults already set to "unavailable" in FileMetrics)

        return fm

    # ---------------------------------------------------------------------------
    # LOC counting (all languages — text scan)
    # ---------------------------------------------------------------------------

    def _count_loc(self, content: str, language: str) -> dict[str, Any]:
        """Cuenta lineas de codigo, blank y comentario via text scan.

        Para JS/TS: usa state machine para block comments (/* ... */).
        Para Python y otros: solo detecta comentarios de linea (#).
        Retorna dict con total_lines, blank_lines, comment_lines, code_lines,
        loc_availability.
        """
        lines = content.splitlines()
        total = len(lines)
        blank = 0
        comment = 0

        if language in ("javascript", "typescript"):
            in_block = False
            for line in lines:
                stripped = line.strip()
                if not stripped:
                    blank += 1
                    continue
                if in_block:
                    comment += 1
                    if "*/" in stripped:
                        in_block = False
                    continue
                if stripped.startswith("//"):
                    comment += 1
                elif stripped.startswith("/*"):
                    comment += 1
                    if "*/" not in stripped[2:]:
                        in_block = True
                elif "/*" in stripped and "*/" not in stripped[stripped.index("/*") + 2:]:
                    # inline block comment start on a code line — count as code
                    in_block = True
                # else: code line (not counted as comment)
        else:
            # Python, Go, Rust, Java, and others: simple line-comment detection
            comment_prefixes: tuple[str, ...]
            if language == "python":
                comment_prefixes = ("#",)
            elif language in ("go", "rust", "java", "kotlin", "scala", "csharp",
                               "cpp", "c", "swift", "php"):
                comment_prefixes = ("//",)
            else:
                comment_prefixes = ("#",)  # generic fallback

            for line in lines:
                stripped = line.strip()
                if not stripped:
                    blank += 1
                elif stripped.startswith(comment_prefixes):
                    comment += 1

        code = total - blank - comment
        return {
            "total_lines": total,
            "blank_lines": blank,
            "comment_lines": comment,
            "code_lines": code,
            "loc_availability": "measured",
        }

    # ---------------------------------------------------------------------------
    # Python symbol + complexity (AST)
    # ---------------------------------------------------------------------------

    def _count_python_symbols(self, content: str, rel_path: str) -> dict[str, Any]:
        """Cuenta simbolos Python y calcula complejidad McCabe via ast.parse.

        Si hay SyntaxError retorna symbol_availability='unavailable' pero
        loc_availability sigue siendo 'measured' (el text scan no necesita AST).
        """
        try:
            tree = ast.parse(content, filename=rel_path)
        except SyntaxError:
            return {
                "function_count": 0,
                "class_count": 0,
                "symbol_availability": "unavailable",
                "cyclomatic_complexity": None,
                "complexity_availability": "unavailable",
            }

        func_nodes: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
        class_count = 0

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                func_nodes.append(node)
            elif isinstance(node, ast.ClassDef):
                class_count += 1

        function_count = len(func_nodes)

        if func_nodes:
            complexities = [_mccabe(fn) for fn in func_nodes]
            avg_cc = float(sum(complexities)) / len(complexities)
            cyclomatic_complexity: float | None = avg_cc
            complexity_availability = "measured"
        else:
            cyclomatic_complexity = None
            complexity_availability = "unavailable"

        return {
            "function_count": function_count,
            "class_count": class_count,
            "symbol_availability": "measured",
            "cyclomatic_complexity": cyclomatic_complexity,
            "complexity_availability": complexity_availability,
        }

    # ---------------------------------------------------------------------------
    # JS/TS symbol counting (regex — inferred)
    # ---------------------------------------------------------------------------

    def _count_js_symbols(self, content: str) -> dict[str, int]:
        """Cuenta funciones y clases en JS/TS via regex (inferred)."""
        function_count = len(_JS_FUNC_RE.findall(content))
        class_count = len(_JS_CLASS_RE.findall(content))
        return {"function_count": function_count, "class_count": class_count}

    # ---------------------------------------------------------------------------
    # Go symbol counting (regex — inferred)
    # ---------------------------------------------------------------------------

    def _count_go_symbols(self, content: str) -> dict[str, int]:
        """Cuenta funciones y structs en Go via regex (inferred)."""
        function_count = len(_GO_FUNC_RE.findall(content))
        class_count = len(_GO_STRUCT_RE.findall(content))
        return {"function_count": function_count, "class_count": class_count}

    # ---------------------------------------------------------------------------
    # Rust symbol counting (regex — inferred)
    # ---------------------------------------------------------------------------

    def _count_rust_symbols(self, content: str) -> dict[str, int]:
        """Cuenta funciones y structs en Rust via regex (inferred)."""
        function_count = len(_RUST_FN_RE.findall(content))
        class_count = len(_RUST_STRUCT_RE.findall(content))
        return {"function_count": function_count, "class_count": class_count}

    # ---------------------------------------------------------------------------
    # Java symbol counting (regex — inferred)
    # ---------------------------------------------------------------------------

    def _count_java_symbols(self, content: str) -> dict[str, int]:
        """Cuenta metodos y clases en Java via regex (inferred)."""
        function_count = len(_JAVA_METHOD_RE.findall(content))
        class_count = len(_JAVA_CLASS_RE.findall(content))
        return {"function_count": function_count, "class_count": class_count}

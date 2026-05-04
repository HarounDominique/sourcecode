from __future__ import annotations
"""Extraccion estatica de documentacion de modulos Python y JS/TS.

Sigue el mismo patron que DependencyAnalyzer y GraphAnalyzer:
- analyze() recibe root + file_tree, retorna (list[DocRecord], DocSummary)
- merge_summaries() agrega multiples DocSummary en uno

Plan 02 implementa los parsers Python-AST y JS/TS-regex.
"""

import ast
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal

from sourcecode.schema import DocRecord, DocsDepth, DocSummary
from sourcecode.tree_utils import flatten_file_tree

# ---------------------------------------------------------------------------
# Language helpers
# ---------------------------------------------------------------------------

_LANG_MAP: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".go": "go",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".scala": "scala",
    ".rs": "rust",
    ".rb": "ruby",
    ".cs": "csharp",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".c": "c",
    ".h": "c",
    ".hpp": "cpp",
    ".swift": "swift",
    ".php": "php",
}


def _lang_for_suffix(suffix: str) -> str:
    return _LANG_MAP.get(suffix.lower(), "unknown")


class DocAnalyzer:
    """Extrae documentacion de modulos Python y JS/TS sin ejecutar toolchains."""

    _MAX_FILES = 200
    _MAX_SYMBOLS_PER_MODULE = 50
    _DOCSTRING_MAX_CHARS = 1000
    _TRUNCATION_SUFFIX = "...[truncated]"
    _MAX_FILE_SIZE = 200_000  # bytes — same pattern as GraphAnalyzer

    _PYTHON_EXTENSIONS = {".py"}
    _NODE_EXTENSIONS = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}

    # JS/TS regex patterns
    JSDOC_BLOCK = re.compile(r"/\*\*(.*?)\*/", re.DOTALL)
    TSDOC_LINE = re.compile(r"^///\s*(.+)$", re.MULTILINE)
    DECL_PATTERN = re.compile(
        r"^(?:(?:public|private|protected|static|async|export|default|readonly|override)\s+)*"
        r"(?:(async\s+)?(?:function\*?\s+(\w+)|class\s+(\w+)))"
        r"|(?:(?:export\s+)?(?:const|let|var)\s+(\w+))"
        r"|(?:(?:public|private|protected|static|readonly|override|async)\s+){0,4}(\w+)\s*[(<:]",
        re.MULTILINE,
    )

    # ---------------------------------------------------------------------------
    # Public API
    # ---------------------------------------------------------------------------

    @staticmethod
    def _infer_importance(
        path: str,
        kind: str,
        entry_points: list[str] | None,
    ) -> Literal["high", "medium", "low"]:
        """Infiere importancia desde senales estructurales."""
        if entry_points and path in entry_points:
            return "high"
        depth = path.count("/")
        if depth <= 1:   # raiz (0) o un nivel (1): src/main.py
            return "high"
        if depth == 2 or kind in {"class", "function"}:
            return "medium"
        return "low"

    def analyze(
        self,
        root: Path,
        file_tree: dict[str, Any],
        *,
        workspace: str | None = None,
        depth: DocsDepth = "symbols",
        entry_points: list[str] | None = None,
    ) -> tuple[list[DocRecord], DocSummary]:
        """Extrae DocRecords del arbol de ficheros dado.

        Retorna una tupla (records, summary).
        """
        all_paths = flatten_file_tree(file_tree)

        # Filter to only files (not directories) with known/handled extensions
        # We also emit unavailable for unsupported language files
        source_extensions = (
            self._PYTHON_EXTENSIONS
            | self._NODE_EXTENSIONS
            | set(_LANG_MAP.keys())
        )
        file_paths = [
            p for p in all_paths
            if Path(p).suffix.lower() in source_extensions and (root / p).is_file()
        ]

        truncated = False
        limitations_pre: list[str] = []
        if len(file_paths) > self._MAX_FILES:
            truncated = True
            actual = len(file_paths)
            limitations_pre.append(f"max_files_reached:{actual}>{self._MAX_FILES}")
            file_paths = file_paths[: self._MAX_FILES]

        records: list[DocRecord] = []
        limitations: list[str] = list(limitations_pre)
        languages: set[str] = set()
        # Track per-language support status for honest reporting
        unsupported_langs: set[str] = set()

        for relative_path in file_paths:
            abs_path = root / relative_path
            suffix = Path(relative_path).suffix.lower()
            lang = _lang_for_suffix(suffix)
            norm_path = Path(relative_path).as_posix()

            # Skip large files
            try:
                file_size = abs_path.stat().st_size
            except OSError:
                limitations.append(f"read_error:{norm_path}")
                continue
            if file_size > self._MAX_FILE_SIZE:
                limitations.append(f"file_too_large:{norm_path}")
                continue

            # Read content
            try:
                content = abs_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                limitations.append(f"read_error:{norm_path}")
                continue

            if suffix in self._PYTHON_EXTENSIONS:
                file_records, file_limitations = self._analyze_python_file(
                    norm_path, content, depth, workspace, entry_points
                )
                records.extend(file_records)
                limitations.extend(file_limitations)
                if file_records:
                    languages.add("python")
            elif suffix in self._NODE_EXTENSIONS:
                file_records, file_limitations = self._analyze_node_file(
                    norm_path, content, depth, workspace, entry_points
                )
                records.extend(file_records)
                limitations.extend(file_limitations)
                if file_records:
                    languages.add(lang)
            else:
                # Unsupported language — D-04: no emitir DocRecord, solo registrar limitation
                limitations.append(f"docs_unavailable:{norm_path}:language={lang}")
                languages.add(lang)
                unsupported_langs.add(lang)
                # NO records.append() here

        # Build language_coverage: explicit per-language support status
        _SUPPORTED_LANGS = {"python", "javascript", "typescript"}
        lang_coverage: dict[str, str] = {}
        for lang in languages:
            if lang in _SUPPORTED_LANGS:
                lang_coverage[lang] = "supported"
            else:
                lang_coverage[lang] = "unsupported"

        # Build summary
        symbol_count = sum(1 for r in records if r.kind != "module")
        total_count = len(records)
        # Check if any record triggered truncation
        if any(r.doc_text and r.doc_text.endswith(self._TRUNCATION_SUFFIX) for r in records):
            truncated = True

        # Explicit absence signal: scanned files but found nothing
        if total_count == 0 and file_paths:
            limitations.append(
                f"no_docs_found: {len(file_paths)} file(s) scanned, "
                "no docstrings or JSDoc comments found"
            )

        # Warn explicitly when unsupported languages are present — agents must not
        # assume full coverage when Java/Go/Rust files are in scope but not analyzed.
        if unsupported_langs:
            sorted_unsupported = sorted(unsupported_langs)
            limitations.append(
                f"docs_not_extracted: language(s) {sorted_unsupported} present but not supported; "
                "only Python and JS/TS docstrings are extracted"
            )

        summary = DocSummary(
            requested=True,
            total_count=total_count,
            symbol_count=symbol_count,
            languages=sorted(languages),
            depth=depth,
            truncated=truncated,
            limitations=limitations,
            language_coverage=lang_coverage,
        )
        return records, summary

    def merge_summaries(self, summaries: Iterable[DocSummary]) -> DocSummary:
        """Agrega multiples DocSummary en uno.

        Sigue el mismo patron que DependencyAnalyzer.merge_summaries().
        """
        result = DocSummary(requested=True)
        languages: set[str] = set()
        limitations: list[str] = []
        for summary in summaries:
            result.total_count += summary.total_count
            result.symbol_count += summary.symbol_count
            languages.update(summary.languages)
            if summary.truncated:
                result.truncated = True
            if summary.depth is not None and result.depth is None:
                result.depth = summary.depth
            for limitation in summary.limitations:
                if limitation not in limitations:
                    limitations.append(limitation)
        result.languages = sorted(languages)
        result.limitations = limitations
        return result

    # ---------------------------------------------------------------------------
    # Python AST extractor
    # ---------------------------------------------------------------------------

    def _analyze_python_file(
        self,
        relative_path: str,
        content: str,
        depth: DocsDepth,
        workspace: str | None,
        entry_points: list[str] | None = None,
    ) -> tuple[list[DocRecord], list[str]]:
        """Extrae DocRecords de un archivo Python usando ast."""
        records: list[DocRecord] = []
        limitations: list[str] = []
        truncated_flag = [False]  # use list for mutation in nested helper

        # Parse
        try:
            tree = ast.parse(content, filename=relative_path)
        except SyntaxError:
            limitations.append(f"python_parse_error:{relative_path}")
            return records, limitations

        # Counter for symbols per module (excludes the module-level record itself)
        symbol_count = [0]

        def truncate(text: str) -> str:
            if len(text) > self._DOCSTRING_MAX_CHARS:
                truncated_flag[0] = True
                return text[: self._DOCSTRING_MAX_CHARS] + self._TRUNCATION_SUFFIX
            return text

        def make_record(
            symbol: str,
            kind: str,
            node: ast.AsyncFunctionDef | ast.FunctionDef | ast.ClassDef | ast.Module,
        ) -> DocRecord | None:
            """Build a DocRecord from an AST node. Returns None if nothing to emit."""
            doc_raw = ast.get_docstring(node)
            doc_text = truncate(doc_raw.strip()) if doc_raw else None

            signature: str | None = None
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                signature = self._extract_python_signature(node)

            if doc_text:
                source = "docstring"
            elif signature:
                source = "signature"
            else:
                # No docstring, no signature — decide whether to emit
                # Per spec: source='unavailable' or not emitted
                source = "unavailable"

            imp = self._infer_importance(relative_path, kind, entry_points)
            return DocRecord(
                symbol=symbol,
                kind=kind,
                language="python",
                path=relative_path,
                doc_text=doc_text,
                signature=signature,
                source=source,
                importance=imp,
                workspace=workspace,
            )

        # Module-level docstring — always included regardless of depth
        module_doc_raw = ast.get_docstring(tree)
        if module_doc_raw:
            module_doc = truncate(module_doc_raw.strip())
            records.append(
                DocRecord(
                    symbol=relative_path,
                    kind="module",
                    language="python",
                    path=relative_path,
                    doc_text=module_doc,
                    signature=None,
                    source="docstring",
                    importance=self._infer_importance(relative_path, "module", entry_points),
                    workspace=workspace,
                )
            )

        if depth == "module":
            # Only module docstring — done
            return records, limitations

        # depth == "symbols" or "full": iterate top-level nodes
        for node in tree.body:
            if symbol_count[0] >= self._MAX_SYMBOLS_PER_MODULE:
                break

            if isinstance(node, ast.ClassDef):
                rec = make_record(node.name, "class", node)
                if rec is not None and rec.source != "unavailable":
                    records.append(rec)
                    symbol_count[0] += 1

                if depth == "full":
                    # Iterate class body for methods
                    for child in node.body:
                        if symbol_count[0] >= self._MAX_SYMBOLS_PER_MODULE:
                            break
                        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            rec = make_record(child.name, "method", child)
                            if rec is not None and rec.source != "unavailable":
                                records.append(rec)
                                symbol_count[0] += 1

            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                rec = make_record(node.name, "function", node)
                if rec is not None and rec.source != "unavailable":
                    records.append(rec)
                    symbol_count[0] += 1

        # Propagate truncation to limitations if needed (summary is computed in analyze())
        # We just return records; the analyze() method detects truncation from records.

        return records, limitations

    def _extract_python_signature(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> str | None:
        """Reconstruye la firma tipada de una funcion Python desde su AST.

        Solo emite signature cuando hay al menos una anotacion de tipo.
        Implementacion basada en RESEARCH.md lineas 297-345.
        """
        args = node.args

        # Check if there are any type annotations
        has_annotation = node.returns is not None or any(
            a.annotation is not None
            for a in (
                args.posonlyargs
                + args.args
                + ([args.vararg] if args.vararg else [])
                + args.kwonlyargs
                + ([args.kwarg] if args.kwarg else [])
            )
        )
        if not has_annotation:
            return None

        all_parts: list[str] = []

        def arg_repr(a: ast.arg) -> str:
            base = a.arg
            if a.annotation:
                base += ": " + ast.unparse(a.annotation)
            return base

        # positional-only (antes de /)
        for a in args.posonlyargs:
            all_parts.append(arg_repr(a))
        if args.posonlyargs:
            all_parts.append("/")

        # args regulares con defaults alineados
        n_defaults = len(args.defaults)
        n_regular = len(args.args)
        for i, a in enumerate(args.args):
            di = i - (n_regular - n_defaults)
            s = arg_repr(a)
            if di >= 0:
                s += "=" + ast.unparse(args.defaults[di])
            all_parts.append(s)

        # *args
        if args.vararg:
            all_parts.append("*" + arg_repr(args.vararg))
        elif args.kwonlyargs:
            all_parts.append("*")

        # keyword-only
        for i, a in enumerate(args.kwonlyargs):
            s = arg_repr(a)
            kw_default = args.kw_defaults[i]
            if kw_default is not None:
                s += "=" + ast.unparse(kw_default)
            all_parts.append(s)

        # **kwargs
        if args.kwarg:
            all_parts.append("**" + arg_repr(args.kwarg))

        params = ", ".join(all_parts)
        ret = ""
        if node.returns:
            ret = " -> " + ast.unparse(node.returns)
        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        return f"{prefix} {node.name}({params}){ret}"

    # ---------------------------------------------------------------------------
    # JS/TS regex extractor
    # ---------------------------------------------------------------------------

    def _extract_jsdoc_text(self, raw_comment: str) -> str:
        """Limpia bloque JSDoc: quita *, extrae texto sin @tags."""
        lines = [
            line.strip().lstrip("*").strip()
            for line in raw_comment.strip().splitlines()
        ]
        return " ".join(line for line in lines if line and not line.startswith("@")).strip()

    def _build_depth_map(self, content: str) -> dict[int, int]:
        """Construye un mapa de brace-depth por posicion en el texto."""
        depth = 0
        depth_at: dict[int, int] = {}
        for i, ch in enumerate(content):
            depth_at[i] = depth
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
        return depth_at

    def _analyze_node_file(
        self,
        relative_path: str,
        content: str,
        depth: DocsDepth,
        workspace: str | None,
        entry_points: list[str] | None = None,
    ) -> tuple[list[DocRecord], list[str]]:
        """Extrae DocRecords de un archivo JS/TS usando regex JSDoc."""
        records: list[DocRecord] = []
        limitations: list[str] = []
        suffix = Path(relative_path).suffix.lower()
        lang = _lang_for_suffix(suffix)

        depth_at = self._build_depth_map(content)
        symbol_count = 0

        def truncate(text: str) -> str:
            if len(text) > self._DOCSTRING_MAX_CHARS:
                return text[: self._DOCSTRING_MAX_CHARS] + self._TRUNCATION_SUFFIX
            return text

        jsdoc_matches = list(self.JSDOC_BLOCK.finditer(content))

        for idx, match in enumerate(jsdoc_matches):
            if symbol_count >= self._MAX_SYMBOLS_PER_MODULE:
                break

            raw_comment = match.group(1)
            doc_text_clean = self._extract_jsdoc_text(raw_comment)
            if not doc_text_clean:
                continue

            # Brace depth at start of this JSDoc block
            block_start = match.start()
            brace_depth = depth_at.get(block_start, 0)

            # Depth filtering
            if depth == "module":
                # Only the first JSDoc block
                if idx > 0:
                    break
            elif depth == "symbols" and brace_depth != 0:
                # Only blocks at brace_depth == 0
                continue
            # depth == "full": include all blocks

            # Determine kind from the declaration that follows the JSDoc
            after_comment = content[match.end():].lstrip()
            decl_match = self.DECL_PATTERN.match(after_comment)

            if depth == "module" and idx == 0:
                kind = "module"
            elif decl_match:
                # group(2) = function name, group(3) = class name,
                # group(4) = const/let/var name, group(5) = method-like name
                if decl_match.group(3):
                    kind = "class"
                elif decl_match.group(2) or decl_match.group(4):
                    kind = "function"
                elif brace_depth > 0:
                    kind = "method"
                else:
                    kind = "function"
            elif brace_depth > 0:
                kind = "method"
            else:
                kind = "module"

            doc_text_final = truncate(doc_text_clean) if doc_text_clean else None

            records.append(
                DocRecord(
                    symbol=decl_match.group(2) or decl_match.group(3) or decl_match.group(4) or decl_match.group(5) or relative_path
                    if decl_match
                    else relative_path,
                    kind=kind,
                    language=lang,
                    path=relative_path,
                    doc_text=doc_text_final,
                    signature=None,
                    source="docstring" if doc_text_final else "unavailable",
                    importance=self._infer_importance(relative_path, kind, entry_points),
                    workspace=workspace,
                )
            )
            symbol_count += 1

        return records, limitations

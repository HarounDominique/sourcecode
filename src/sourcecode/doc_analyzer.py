"""Extraccion estatica de documentacion de modulos Python y JS/TS.

Sigue el mismo patron que DependencyAnalyzer y GraphAnalyzer:
- analyze() recibe root + file_tree, retorna (list[DocRecord], DocSummary)
- merge_summaries() agrega multiples DocSummary en uno

Plan 02 implementa los parsers Python-AST y JS/TS-regex.
"""
from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from sourcecode.schema import DocRecord, DocSummary, DocsDepth


class DocAnalyzer:
    """Extrae documentacion de modulos Python y JS/TS sin ejecutar toolchains."""

    _MAX_FILES = 200
    _MAX_SYMBOLS_PER_MODULE = 50
    _DOCSTRING_MAX_CHARS = 1000
    _TRUNCATION_SUFFIX = "...[truncated]"

    _PYTHON_EXTENSIONS = {".py"}
    _NODE_EXTENSIONS = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}

    def analyze(
        self,
        root: Path,
        file_tree: dict[str, Any],
        *,
        workspace: str | None = None,
        depth: DocsDepth = "symbols",
    ) -> tuple[list[DocRecord], DocSummary]:
        """Extrae DocRecords del arbol de ficheros dado.

        Retorna una tupla (records, summary). En el scaffold actual retorna
        registros vacios; la implementacion real se añade en Plan 02.
        """
        return ([], DocSummary(requested=True, depth=depth))

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

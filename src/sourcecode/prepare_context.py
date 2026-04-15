from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from sourcecode.semantic_analyzer import SemanticAnalyzer


# =========================
# Output schema (simple)
# =========================

@dataclass
class ContextSnippet:
    file: str
    symbol: str
    code: str
    reason: str


@dataclass
class ContextFile:
    path: str
    role: str


@dataclass
class ContextResult:
    task: str
    entry_points: List[Dict[str, str]]
    relevant_files: List[ContextFile]
    call_flow: List[Dict[str, Any]]
    snippets: List[ContextSnippet]
    tests: List[Dict[str, str]]
    notes: List[str]


# =========================
# Core Builder
# =========================

class ContextBuilder:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.analyzer = SemanticAnalyzer(root)

    # -------------------------
    # Public API
    # -------------------------

    def prepare(self, task: str) -> ContextResult:
        """
        Entry point principal.
        """
        semantic = self.analyzer.analyze()

        entry_points = self._detect_entrypoints(semantic)
        relevant_files = self._select_relevant_files(task, entry_points)
        snippets = self._extract_snippets(relevant_files)
        tests = self._find_related_tests(relevant_files)
        call_flow = self._build_call_flow(entry_points)

        notes = self._generate_notes(task)

        return ContextResult(
            task=task,
            entry_points=entry_points,
            relevant_files=relevant_files,
            call_flow=call_flow,
            snippets=snippets,
            tests=tests,
            notes=notes,
        )

    # -------------------------
    # Heurísticas básicas
    # -------------------------

    def _detect_entrypoints(self, semantic: Any) -> List[Dict[str, str]]:
        """
        MVP: detecta cli.py o main functions
        """
        entrypoints: List[Dict[str, str]] = []

        for path in semantic.file_paths:
            if path.endswith("cli.py") or path.endswith("__main__.py"):
                entrypoints.append({
                    "file": path,
                    "symbol": "main",
                    "reason": "CLI entrypoint"
                })

        return entrypoints

    def _select_relevant_files(
        self,
        task: str,
        entry_points: List[Dict[str, str]],
    ) -> List[ContextFile]:
        """
        Heurística simple:
        - incluir entrypoints
        - incluir archivos con palabras clave del task
        """
        files: List[ContextFile] = []

        for ep in entry_points:
            files.append(ContextFile(path=ep["file"], role="entrypoint"))

        keywords = task.lower().split()

        for path in self.analyzer.scan.file_paths:  # fallback simple
            if any(k in path.lower() for k in keywords):
                files.append(ContextFile(path=path, role="matched"))

        # deduplicar
        seen = set()
        unique = []
        for f in files:
            if f.path not in seen:
                seen.add(f.path)
                unique.append(f)

        return unique[:8]

    def _extract_snippets(self, files: List[ContextFile]) -> List[ContextSnippet]:
        """
        Extrae primeras ~30 líneas como snippet simple.
        """
        snippets: List[ContextSnippet] = []

        for f in files:
            abs_path = self.root / f.path
            if not abs_path.exists():
                continue

            try:
                content = abs_path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue

            snippet = "\n".join(content.splitlines()[:30])

            snippets.append(ContextSnippet(
                file=f.path,
                symbol="module",
                code=snippet,
                reason=f"contenido inicial de {f.role}"
            ))

        return snippets[:12]

    def _find_related_tests(self, files: List[ContextFile]) -> List[Dict[str, str]]:
        """
        Busca tests por naming convention.
        """
        tests: List[Dict[str, str]] = []

        for f in files:
            name = Path(f.path).name
            test_name = f"test_{name}"

            test_path = self.root / "tests" / test_name
            if test_path.exists():
                tests.append({
                    "file": f"tests/{test_name}",
                    "reason": "posible test relacionado"
                })

        return tests

    def _build_call_flow(self, entry_points: List[Dict[str, str]]) -> List[Dict[str, Any]]:
        """
        MVP: placeholder (hasta que uses call graph real)
        """
        flow: List[Dict[str, Any]] = []

        for ep in entry_points:
            flow.append({
                "from": ep["symbol"],
                "to": ["unknown"]
            })

        return flow

    def _generate_notes(self, task: str) -> List[str]:
        notes: List[str] = []

        if "flag" in task or "--" in task:
            notes.append("probablemente necesitas modificar argumentos CLI")
        if "test" in task:
            notes.append("asegúrate de actualizar tests relacionados")
        if "refactor" in task:
            notes.append("mantener compatibilidad hacia atrás")

        return notes
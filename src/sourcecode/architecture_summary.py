from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

from sourcecode.schema import EntryPoint, SourceMap, StackDetection
from sourcecode.tree_utils import flatten_file_tree

_TOOLING_PREFIXES = (".claude/", ".vscode/", "bin/")
_PYTHON_EXTENSIONS = {".py"}
_NODE_EXTENSIONS = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
_GO_EXTENSIONS = {".go"}
_JAVA_EXTENSIONS = {".java", ".kt", ".scala"}

_CORE_DETECTION_MODULES = {"scanner", "detectors", "classifier", "workspace"}

_OPTIONAL_LABEL_MAP: dict[str, str] = {
    "DependencyAnalyzer": "dependencias",
    "GraphAnalyzer": "grafo de módulos",
    "DocAnalyzer": "docs y docstrings",
    "MetricsAnalyzer": "métricas de calidad",
    "SemanticAnalyzer": "semántica y call graph",
    "ArchitectureAnalyzer": "inferencia arquitectónica",
    "GitAnalyzer": "contexto git",
    "EnvAnalyzer": "variables de entorno",
    "CodeNotesAnalyzer": "anotaciones de código",
}


class ArchitectureSummarizer:
    """Construye un resumen arquitectonico estatico de 3-5 lineas."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def generate(self, sm: SourceMap) -> str | None:
        try:
            return self._build_summary(sm)
        except Exception:
            return None

    def _build_summary(self, sm: SourceMap) -> str | None:
        file_paths = [
            path for path in flatten_file_tree(sm.file_tree)
            if not self._is_tooling_path(path)
        ]
        if not file_paths:
            return None

        entry_points = [
            entry for entry in sm.entry_points
            if not self._is_tooling_path(entry.path)
        ]
        if not entry_points:
            fallback = self._infer_fallback_entry_points(file_paths, sm.stacks)
            entry_points = fallback[:1]

        if not entry_points:
            return "Arquitectura no inferida con suficiente evidencia estatica."

        entry_point = entry_points[0]
        content = self._read_file(entry_point.path)
        if content is None:
            return f"Entry point principal: {entry_point.path}. Arquitectura no inferida con suficiente evidencia estatica."

        suffix = Path(entry_point.path).suffix
        if suffix in _PYTHON_EXTENSIONS:
            lang_lines = self._summarize_python_entry(entry_point.path, content)
        elif suffix in _NODE_EXTENSIONS:
            lang_lines = self._summarize_node_entry(entry_point.path, content)
        elif suffix in _GO_EXTENSIONS:
            lang_lines = self._summarize_go_entry(entry_point.path, content)
        elif suffix in _JAVA_EXTENSIONS:
            lang_lines = self._summarize_java_entry(entry_point.path, content, sm.stacks)
        else:
            lang_lines = []

        if lang_lines:
            # Product-level description available — no need for internal "Entry point: ..." header
            lines = lang_lines
        else:
            lines = [self._describe_entry_point(entry_point, sm.project_type)]
            if not lang_lines:
                lines.append("Orquesta modulos internos no detallados por el analisis estatico disponible.")

        unique_lines: list[str] = []
        seen: set[str] = set()
        for line in lines:
            line = line.strip()
            if not line or line in seen:
                continue
            seen.add(line)
            unique_lines.append(line)
        return "\n".join(unique_lines[:5]) if unique_lines else None

    def _summarize_python_entry(self, path: str, content: str) -> list[str]:
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return []

        package_prefix = self._python_package_prefix(path)
        imported_modules: list[str] = []
        optional_class_names: list[str] = []
        uses_serializer = False
        uses_redactor = False

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and isinstance(node.module, str):
                if package_prefix and node.module.startswith(package_prefix):
                    imported_modules.append(node.module.rsplit(".", 1)[-1])
                if node.module.endswith(".serializer") or "serializer" in node.module:
                    uses_serializer = True
                if node.module.endswith(".redactor") or "redactor" in node.module:
                    uses_redactor = True
            elif isinstance(node, ast.Assign):
                cls_name = self._extract_optional_analyzer_class_name(node)
                if cls_name:
                    optional_class_names.append(cls_name)

        module_set = set(imported_modules)
        lines: list[str] = []

        # Detection line — infer from core modules present
        has_core = bool(module_set & _CORE_DETECTION_MODULES)
        if has_core:
            lines.append("Analiza el árbol del repositorio y detecta stack, entrypoints y tipo de proyecto.")
        elif module_set:
            lines.append("Analiza el proyecto y produce información estructurada.")

        # Output line
        if uses_serializer:
            out = "Produce un SourceMap serializable en JSON/YAML"
            if uses_redactor:
                out += " con redacción de secretos"
            lines.append(out + ".")

        # Optional capabilities line
        opt_labels = [
            _OPTIONAL_LABEL_MAP[cls]
            for cls in optional_class_names
            if cls in _OPTIONAL_LABEL_MAP
        ]
        if opt_labels:
            if len(opt_labels) > 1:
                joined = ", ".join(opt_labels[:-1]) + " y " + opt_labels[-1]
            else:
                joined = opt_labels[0]
            lines.append(f"Opcionalmente añade {joined}.")

        return lines

    def _extract_optional_analyzer_class_name(self, node: ast.Assign) -> str | None:
        value = node.value
        if not isinstance(value, ast.IfExp):
            return None
        if not isinstance(value.body, ast.Call):
            return None
        if not isinstance(value.body.func, ast.Name):
            return None
        cls_name = value.body.func.id
        return cls_name if cls_name.endswith("Analyzer") else None

    def _summarize_node_entry(self, path: str, content: str) -> list[str]:
        imports = re.findall(r"""from\s+['"](\.?\.?/[^'"]+)['"]|require\(['"](\.?\.?/[^'"]+)['"]\)""", content)
        modules = [item for pair in imports for item in pair if item]
        lines: list[str] = []
        if modules:
            formatted = self._format_module_list([self._module_label(module) for module in modules])
            if formatted:
                lines.append(f"Orquesta modulos internos: {formatted}.")
        lines.append("Produce la salida principal del entry point JavaScript/TypeScript detectado.")
        return lines

    def _summarize_java_entry(self, path: str, content: str, stacks: list[StackDetection]) -> list[str]:
        lines: list[str] = []
        frameworks = [f.name for stack in stacks for f in stack.frameworks]
        if frameworks:
            lines.append(f"Frameworks detectados: {', '.join(frameworks)}.")
        annotations = re.findall(r"@(SpringBootApplication|QuarkusMain|MicronautApplication|Application)\b", content)
        if annotations:
            lines.append(f"Anotacion de arranque: @{annotations[0]}.")
        # Detect Spring Boot profile hints
        if "@SpringBootApplication" in content:
            lines.append("Arranca el contexto de Spring con auto-configuracion y component scan.")
        elif not lines:
            lines.append("Orquesta el arranque de la aplicacion JVM.")
        return lines

    def _summarize_go_entry(self, path: str, content: str) -> list[str]:
        imports = re.findall(r'"([^"]+)"', content)
        internal = [module for module in imports if not module.startswith(("fmt", "net/", "os", "context"))]
        lines: list[str] = []
        if internal:
            formatted = self._format_module_list([self._module_label(module) for module in internal])
            if formatted:
                lines.append(f"Orquesta paquetes internos: {formatted}.")
        lines.append("Produce la salida principal del binario Go detectado.")
        return lines

    def _describe_entry_point(self, entry_point: EntryPoint, project_type: str | None) -> str:
        if entry_point.kind == "cli" or entry_point.path.endswith("cli.py"):
            return f"Entry point principal: {entry_point.path} expone la CLI del proyecto."
        if entry_point.kind == "web":
            return f"Entry point principal: {entry_point.path} arranca la interfaz web."
        if project_type == "api" or entry_point.kind == "server":
            return f"Entry point principal: {entry_point.path} arranca el servicio principal."
        if entry_point.kind == "binary":
            return f"Entry point principal: {entry_point.path} arranca el binario principal."
        return f"Entry point principal: {entry_point.path} coordina el flujo principal del proyecto."

    def _extract_optional_analyzer(self, node: ast.Assign) -> str | None:
        value = node.value
        if not isinstance(value, ast.IfExp):
            return None
        if not isinstance(value.test, ast.Name):
            return None
        if not isinstance(value.body, ast.Call):
            return None
        if not isinstance(value.body.func, ast.Name):
            return None
        analyzer_name = value.body.func.id
        if not analyzer_name.endswith("Analyzer"):
            return None
        return f"{analyzer_name} (--{value.test.id.replace('_', '-')})"

    def _infer_fallback_entry_points(
        self, file_paths: list[str], stacks: list[StackDetection]
    ) -> list[EntryPoint]:
        candidates: list[EntryPoint] = []
        stack_name = stacks[0].stack if stacks else "unknown"
        ordered_paths = sorted(file_paths, key=self._fallback_priority)
        for path in ordered_paths:
            if path.endswith(("cli.py", "__main__.py", "main.py")):
                candidates.append(
                    EntryPoint(
                        path=path,
                        stack=stack_name,
                        kind="cli",
                        source="convention",
                        confidence="medium",
                    )
                )
            elif path.endswith(("app/page.tsx", "pages/index.js", "server.js")):
                candidates.append(
                    EntryPoint(
                        path=path,
                        stack=stack_name,
                        kind="web" if "page" in path or "pages/" in path else "server",
                        source="convention",
                        confidence="medium",
                    )
                )
            elif path.endswith("main.go"):
                candidates.append(
                    EntryPoint(
                        path=path,
                        stack=stack_name,
                        kind="binary",
                        source="convention",
                        confidence="medium",
                    )
                )
            elif path.endswith("Application.java") or path.endswith("Main.java"):
                candidates.append(
                    EntryPoint(
                        path=path,
                        stack="java",
                        kind="application",
                        source="convention",
                        confidence="medium",
                    )
                )
        return candidates

    def _fallback_priority(self, path: str) -> tuple[int, int, str]:
        return (
            0 if "/cli.py" in path or path.endswith("cli.py") else 1,
            0 if path.startswith("src/") else 1,
            path,
        )

    def _format_module_list(self, modules: list[str]) -> str:
        normalized = [self._module_label(module) for module in modules]
        filtered = [module for module in normalized if module and not self._is_tooling_path(module)]
        if not filtered:
            return ""
        ordered: list[str] = []
        seen: set[str] = set()
        for module in filtered:
            if module not in seen:
                seen.add(module)
                ordered.append(module)
        return ", ".join(ordered[:8])

    def _module_label(self, module: str) -> str:
        cleaned = module.strip().strip("./")
        if "/" in cleaned:
            return cleaned.split("/")[-1]
        if "." in cleaned:
            return cleaned.rsplit(".", 1)[-1]
        return cleaned

    def _python_package_prefix(self, path: str) -> str:
        parts = Path(path).parts
        if len(parts) >= 3 and parts[0] == "src":
            return f"{parts[1]}."
        if len(parts) >= 2:
            return f"{parts[0]}."
        return ""

    def _read_file(self, relative_path: str) -> str | None:
        try:
            return (self.root / relative_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    def _is_tooling_path(self, path: str | None) -> bool:
        if not path:
            return False
        normalized = path.strip().lstrip("/")
        return normalized.startswith(_TOOLING_PREFIXES)

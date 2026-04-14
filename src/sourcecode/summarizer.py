"""Generador deterministico de resumen en lenguaje natural del proyecto.

Sin llamadas a API — solo templates aplicados sobre SourceMap.
"""
from __future__ import annotations

from pathlib import Path

from sourcecode.detectors.parsers import load_json_file, load_toml_file
from sourcecode.schema import SourceMap

_TOOLING_PREFIXES = (".claude/", ".vscode/", "bin/")


class ProjectSummarizer:
    """Genera project_summary: string NL deterministica desde SourceMap."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root

    def generate(self, sm: SourceMap) -> str:
        """Retorna descripcion NL del proyecto. Nunca lanza excepcion."""
        try:
            return self._build_summary(sm)
        except Exception:
            return "Proyecto analizado."

    def _build_summary(self, sm: SourceMap) -> str:
        description = self._read_project_description()
        if description:
            return self._merge_description_with_structure(description, sm)

        # Determinar stack primario
        non_tooling_stacks = self._filter_non_tooling_stacks(sm)
        primary_stacks = [s for s in non_tooling_stacks if s.primary]
        all_stacks = primary_stacks if primary_stacks else non_tooling_stacks

        if not all_stacks:
            return "Proyecto sin stack detectado."

        # Tipo de proyecto
        project_type = sm.project_type or "Proyecto"
        type_label = {
            "webapp": "Aplicacion web",
            "api": "API",
            "library": "Libreria",
            "cli": "CLI",
            "monorepo": "Monorepo",
            "fullstack": "Proyecto fullstack",
            "unknown": "Proyecto",
        }.get(project_type, project_type.capitalize())

        # Stacks y frameworks
        primary = all_stacks[0]
        stack_name = primary.stack.capitalize()
        frameworks = [f.name for f in primary.frameworks]
        fw_part = f" ({', '.join(frameworks[:3])})" if frameworks else ""

        # Entry points (max 3)
        ep_paths = [ep.path for ep in sm.entry_points if not self._is_tooling_path(ep.path)][:3]
        ep_part = f" Entry points: {', '.join(ep_paths)}." if ep_paths else ""

        # Dependencias
        dep_part = ""
        if sm.dependency_summary and sm.dependency_summary.total_count > 0:
            ds = sm.dependency_summary
            ecosystems = ", ".join(ds.ecosystems[:3])
            dep_part = f" {ds.total_count} dependencias ({ecosystems})."
        elif sm.dependency_summary is None:
            dep_part = ""
        else:
            dep_part = " Sin dependencias detectadas."

        # Monorepo: variante especial
        if project_type == "monorepo":
            stacks_desc = ", ".join(sorted({s.stack.capitalize() for s in non_tooling_stacks}))
            n_ws = len({s.workspace for s in non_tooling_stacks if s.workspace})
            ws_part = f" con {n_ws} workspaces" if n_ws > 0 else ""
            return f"Monorepo{ws_part} en {stacks_desc}.{ep_part}{dep_part}"

        return f"{type_label} en {stack_name}{fw_part}.{ep_part}{dep_part}"

    def _read_project_description(self) -> str | None:
        if self.root is None:
            return None
        pyproject = load_toml_file(self.root / "pyproject.toml")
        if pyproject:
            project = pyproject.get("project", {})
            if isinstance(project, dict):
                description = project.get("description")
                if isinstance(description, str) and description.strip():
                    return description.strip()

        package_json = load_json_file(self.root / "package.json")
        if package_json:
            description = package_json.get("description")
            if isinstance(description, str) and description.strip():
                return description.strip()

        return self._read_readme_paragraph()

    def _read_readme_paragraph(self) -> str | None:
        if self.root is None:
            return None
        for name in ("README.md", "README.rst", "README.txt"):
            path = self.root / name
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            paragraph = self._extract_first_useful_paragraph(content)
            if paragraph:
                return paragraph
        return None

    def _extract_first_useful_paragraph(self, content: str) -> str | None:
        lines: list[str] = []
        in_code_block = False
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if line.startswith("```"):
                in_code_block = not in_code_block
                continue
            if in_code_block or not line or line.startswith(("#", "<!--")):
                if lines:
                    break
                continue
            lines.append(line)
        if not lines:
            return None
        return " ".join(lines).strip()

    def _merge_description_with_structure(self, description: str, sm: SourceMap) -> str:
        parts = [description.rstrip(".")]
        entry_points = [ep.path for ep in sm.entry_points if not self._is_tooling_path(ep.path)][:2]
        if entry_points:
            parts.append(f"Entry points: {', '.join(entry_points)}")

        non_tooling_stacks = self._filter_non_tooling_stacks(sm)
        if non_tooling_stacks:
            primary = self._select_summary_primary_stack(non_tooling_stacks)
            frameworks = [framework.name for framework in primary.frameworks[:2]]
            stack_label = primary.stack.capitalize()
            if frameworks:
                parts.append(f"Stack principal: {stack_label} ({', '.join(frameworks)})")
            else:
                parts.append(f"Stack principal: {stack_label}")

        return ". ".join(parts) + "."

    def _filter_non_tooling_stacks(self, sm: SourceMap) -> list:
        filtered = [
            stack for stack in sm.stacks
            if not self._is_tooling_path(stack.root) and not self._is_tooling_path(stack.workspace)
        ]
        return filtered or sm.stacks

    def _select_summary_primary_stack(self, stacks: list) -> Any:
        def score(stack: Any) -> tuple[int, int, int, int]:
            root_manifest_hits = 0
            if self.root is not None:
                root_manifest_hits = sum(
                    1 for manifest in stack.manifests
                    if (self.root / manifest).is_file()
                )
            framework_hits = len(stack.frameworks)
            primary_hint = 1 if stack.primary else 0
            confidence = {"low": 0, "medium": 1, "high": 2}.get(stack.confidence, 0)
            return (root_manifest_hits, framework_hits, primary_hint, confidence)

        return max(stacks, key=score)

    def _is_tooling_path(self, path: str | None) -> bool:
        if not path:
            return False
        normalized = path.strip().lstrip("/")
        return normalized.startswith(_TOOLING_PREFIXES)

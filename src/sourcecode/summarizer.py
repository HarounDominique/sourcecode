from __future__ import annotations

"""Generador deterministico de resumen en lenguaje natural del proyecto.

Sin llamadas a API — solo templates aplicados sobre SourceMap.
"""

from pathlib import Path

from sourcecode.detectors.parsers import load_json_file, load_toml_file
from sourcecode.schema import SourceMap

_TOOLING_PREFIXES = (".claude/", ".vscode/", "bin/")
_SRC_TRANSPARENT = {"src", "lib", "app", "pkg"}
_GENERIC_NAMES = {"utils", "helpers", "common", "shared", "misc", "core", "root", "", "apps", "packages"}

# Directory names that indicate architectural layers, not business domains
_ARCH_LAYER_NAMES = {
    "api", "controllers", "handlers", "routes", "endpoints",
    "services", "usecases", "application",
    "repositories", "repos", "store", "dao", "storage",
    "models", "entities", "domain",
    "infra", "infrastructure", "persistence", "db", "database",
    "adapters", "ports", "interfaces",
    "frontend", "backend", "client", "server",
    "components", "pages", "views", "templates",
    "tests", "test", "specs", "spec",
    "config", "configs", "settings",
    "middleware", "interceptors",
    "schemas", "types",
    "migrations", "seeds",
    "scripts", "tools",
}

_CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs",
    ".go", ".java", ".kt", ".rs", ".rb",
}

_ARCH_LAYER_PATTERNS: dict[str, dict[str, list[str]]] = {
    "mvc": {
        "controller": ["controllers", "routes", "views", "handlers"],
        "model": ["models", "entities", "domain"],
        "view": ["views", "templates", "pages", "components"],
    },
    "layered": {
        "controller": ["controllers", "api", "routes", "handlers", "endpoints"],
        "service": ["services", "usecases", "application"],
        "repository": ["repositories", "repos", "store", "dao"],
        "infrastructure": ["infra", "infrastructure", "persistence", "db", "database"],
    },
    "hexagonal": {
        "port": ["ports", "interfaces"],
        "adapter": ["adapters"],
        "domain": ["domain", "core", "models"],
    },
    "fullstack": {
        "frontend": ["frontend", "client", "web", "ui", "pages", "components"],
        "backend": ["backend", "server", "api", "services"],
    },
}


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

        non_tooling_stacks = self._filter_non_tooling_stacks(sm)
        primary_stacks = [s for s in non_tooling_stacks if s.primary]
        all_stacks = primary_stacks if primary_stacks else non_tooling_stacks

        if not all_stacks:
            return "Proyecto sin stack detectado."

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

        primary = all_stacks[0]
        stack_name = primary.stack.capitalize()
        frameworks = [f.name for f in primary.frameworks]
        fw_part = f" ({', '.join(frameworks[:3])})" if frameworks else ""

        arch_pattern = self._detect_architecture_pattern(sm.file_paths)
        domains = self._extract_business_domains(sm.file_paths)
        dep_part = self._build_dep_part(sm)

        if project_type == "monorepo":
            stacks_desc = ", ".join(sorted({s.stack.capitalize() for s in non_tooling_stacks}))
            n_ws = len({s.workspace for s in non_tooling_stacks if s.workspace})
            ws_part = f" con {n_ws} workspaces" if n_ws > 0 else ""
            domains_part = f" Dominios: {', '.join(domains)}." if domains else ""
            return f"Monorepo{ws_part} en {stacks_desc}.{domains_part}{dep_part}"

        arch_suffix = f" con arquitectura {arch_pattern}" if arch_pattern else ""
        base = f"{type_label} en {stack_name}{fw_part}{arch_suffix}."

        if domains:
            extra = f" Dominios: {', '.join(domains)}."
        else:
            ep_paths = [ep.path for ep in sm.entry_points if not self._is_tooling_path(ep.path)][:3]
            extra = f" Entry points: {', '.join(ep_paths)}." if ep_paths else ""

        return f"{base}{extra}{dep_part}"

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

        arch_pattern = self._detect_architecture_pattern(sm.file_paths)
        domains = self._extract_business_domains(sm.file_paths)

        non_tooling_stacks = self._filter_non_tooling_stacks(sm)
        if non_tooling_stacks:
            primary = self._select_summary_primary_stack(non_tooling_stacks)
            frameworks = [fw.name for fw in primary.frameworks[:2]]
            stack_label = primary.stack.capitalize()
            arch_str = f" con arquitectura {arch_pattern}" if arch_pattern else ""
            if frameworks:
                parts.append(f"Stack: {stack_label} ({', '.join(frameworks)}){arch_str}")
            else:
                parts.append(f"Stack: {stack_label}{arch_str}")

        if domains:
            parts.append(f"Dominios: {', '.join(domains)}")
        else:
            entry_points = [ep.path for ep in sm.entry_points if not self._is_tooling_path(ep.path)][:2]
            if entry_points:
                parts.append(f"Entry points: {', '.join(entry_points)}")

        return ". ".join(parts) + "."

    def _detect_architecture_pattern(self, file_paths: list[str]) -> str | None:
        """Infer architecture pattern from directory names in file paths."""
        dir_names: set[str] = set()
        for p in file_paths:
            norm = p.replace("\\", "/")
            for seg in norm.split("/")[:-1]:
                dir_names.add(seg.lower())

        best_pattern: str | None = None
        best_score = 0
        for pattern_name, layer_keys in _ARCH_LAYER_PATTERNS.items():
            score = sum(
                1 for keywords in layer_keys.values()
                if any(d in keywords for d in dir_names)
            )
            if score > best_score:
                best_score = score
                best_pattern = pattern_name

        return best_pattern if best_score >= 2 else None

    def _extract_business_domains(self, file_paths: list[str]) -> list[str]:
        """Extract business domain names from the directory structure.

        Returns names only when >=2 distinct domains are found, to avoid
        treating single-package namespaces (e.g. 'sourcecode/') as domains.
        """
        domain_counts: dict[str, int] = {}
        for p in file_paths:
            norm = p.replace("\\", "/")
            if any(norm.startswith(pfix) for pfix in _TOOLING_PREFIXES):
                continue
            if Path(norm).suffix.lower() not in _CODE_EXTENSIONS:
                continue
            parts = norm.split("/")
            if len(parts) < 2:
                continue
            seg = parts[0].lower()
            if seg in _SRC_TRANSPARENT and len(parts) >= 3:
                seg = parts[1].lower()
            if seg in _ARCH_LAYER_NAMES or seg in _GENERIC_NAMES or seg in _SRC_TRANSPARENT:
                continue
            domain_counts[seg] = domain_counts.get(seg, 0) + 1

        # Single namespace = not business domains worth reporting
        if len(domain_counts) < 2:
            return []

        sorted_domains = sorted(domain_counts.items(), key=lambda x: x[1], reverse=True)
        return [name for name, _ in sorted_domains[:5]]

    def _build_dep_part(self, sm: SourceMap) -> str:
        if sm.dependency_summary and sm.dependency_summary.total_count > 0:
            ds = sm.dependency_summary
            ecosystems = ", ".join(ds.ecosystems[:3])
            return f" {ds.total_count} dependencias ({ecosystems})."
        if sm.dependency_summary is None:
            return ""
        return " Sin dependencias detectadas."

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

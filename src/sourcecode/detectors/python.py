from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from sourcecode.detectors.base import (
    AbstractDetector,
    DetectionContext,
    EntryPoint,
    StackDetection,
)
from sourcecode.detectors.parsers import load_toml_file, read_text_lines, unique_strings
from sourcecode.schema import FrameworkDetection
from sourcecode.tree_utils import path_exists_in_tree

_FRAMEWORK_MAP = {
    "fastapi": "FastAPI",
    "django": "Django",
    "flask": "Flask",
    "typer": "Typer",
}


class PythonDetector(AbstractDetector):
    name = "python"
    priority = 30

    def can_detect(self, context: DetectionContext) -> bool:
        supported = {
            "pyproject.toml",
            "requirements.txt",
            "setup.py",
            "Pipfile",
            "Pipfile.lock",
            "uv.lock",
            "poetry.lock",
        }
        return any(manifest in supported for manifest in context.manifests) or any(
            path_exists_in_tree(context.file_tree, path)
            for path in ("poetry.lock", "Pipfile.lock")
        )

    def detect(self, context: DetectionContext) -> tuple[list[StackDetection], list[EntryPoint]]:
        dependencies = self._collect_dependencies(context)
        frameworks = [
            FrameworkDetection(name=label, source="manifest")
            for package_name, label in _FRAMEWORK_MAP.items()
            if package_name in dependencies
        ]
        entry_points = self._collect_entry_points(context)

        manifests = [
            manifest
            for manifest in (
                "pyproject.toml",
                "requirements.txt",
                "setup.py",
                "Pipfile",
                "Pipfile.lock",
                "uv.lock",
                "poetry.lock",
            )
            if manifest in context.manifests or path_exists_in_tree(context.file_tree, manifest)
        ]
        stack = StackDetection(
            stack="python",
            detection_method="manifest",
            confidence="high" if "pyproject.toml" in manifests else "medium",
            frameworks=frameworks,
            package_manager=self._detect_package_manager(context, pyproject=load_toml_file(context.root / "pyproject.toml")),
            manifests=manifests,
        )
        return [stack], entry_points

    def _collect_dependencies(self, context: DetectionContext) -> set[str]:
        deps: set[str] = set()
        pyproject = load_toml_file(context.root / "pyproject.toml")
        if pyproject:
            project = pyproject.get("project", {})
            if isinstance(project, dict):
                deps.update(self._normalize_requirement(dep) for dep in project.get("dependencies", []))
                optional = project.get("optional-dependencies", {})
                if isinstance(optional, dict):
                    for group in optional.values():
                        if isinstance(group, list):
                            deps.update(self._normalize_requirement(dep) for dep in group)
            tool = pyproject.get("tool", {})
            if isinstance(tool, dict):
                poetry = tool.get("poetry", {})
                if isinstance(poetry, dict):
                    deps.update(
                        self._normalize_requirement(dep_name)
                        for dep_name in poetry.get("dependencies", {})
                    )
                    groups = poetry.get("group", {})
                    if isinstance(groups, dict):
                        for group in groups.values():
                            if isinstance(group, dict):
                                deps.update(
                                    self._normalize_requirement(dep_name)
                                    for dep_name in group.get("dependencies", {})
                                )

        deps.update(self._read_requirements(context.root / "requirements.txt"))
        deps.update(self._read_requirements(context.root / "Pipfile"))
        deps.update(self._read_setup_py(context.root / "setup.py"))
        return {dep for dep in deps if dep}

    def _normalize_requirement(self, requirement: Any) -> str:
        if not isinstance(requirement, str):
            return ""
        normalized = requirement.strip().lower()
        normalized = re.split(r"[<>=!~\[\s]", normalized, maxsplit=1)[0]
        return normalized

    def _read_requirements(self, path: Path) -> set[str]:
        result: set[str] = set()
        for line in read_text_lines(path):
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", "[")):
                continue
            result.add(self._normalize_requirement(stripped))
        return result

    def _read_setup_py(self, path: Path) -> set[str]:
        content = "\n".join(read_text_lines(path))
        if not content:
            return set()
        matches = re.findall(r"['\"]([A-Za-z0-9_.-]+)(?:[<>=!~].*?)?['\"]", content)
        return {match.lower() for match in matches}

    def _collect_entry_points(self, context: DetectionContext) -> list[EntryPoint]:
        declared_candidates: list[str] = []
        pyproject = load_toml_file(context.root / "pyproject.toml")
        if pyproject:
            project = pyproject.get("project", {})
            if isinstance(project, dict):
                scripts = project.get("scripts", {})
                if isinstance(scripts, dict):
                    for value in scripts.values():
                        if isinstance(value, str) and ":" in value:
                            module, _callable = value.split(":", 1)
                            declared_candidates.append(module.replace(".", "/") + ".py")

        entry_points: list[EntryPoint] = []
        declared = set()
        for path in unique_strings(declared_candidates):
            if path_exists_in_tree(context.file_tree, path):
                declared.add(path)
                kind = "cli" if path.endswith(("__main__.py", "main.py", "cli.py")) else "app"
                entry_points.append(
                    EntryPoint(
                        path=path,
                        stack="python",
                        kind=kind,
                        source="pyproject.toml",
                        confidence="high",
                    )
                )

        convention_candidates = ["cli.py", "__main__.py", "main.py", "app.py", "manage.py", "src/main.py"]
        for path in unique_strings(convention_candidates):
            if path in declared or not path_exists_in_tree(context.file_tree, path):
                continue
            kind = "cli" if path.endswith(("__main__.py", "main.py", "cli.py")) else "app"
            entry_points.append(
                EntryPoint(
                    path=path,
                    stack="python",
                    kind=kind,
                    source="convention",
                    confidence="medium",
                )
            )
        return entry_points

    def _detect_package_manager(
        self, context: DetectionContext, *, pyproject: dict[str, Any] | None
    ) -> str:
        if path_exists_in_tree(context.file_tree, "poetry.lock"):
            return "poetry"
        if path_exists_in_tree(context.file_tree, "Pipfile.lock") or path_exists_in_tree(
            context.file_tree, "Pipfile"
        ):
            return "pipenv"
        if path_exists_in_tree(context.file_tree, "uv.lock"):
            return "uv"
        if pyproject:
            tool = pyproject.get("tool", {})
            if isinstance(tool, dict) and "poetry" in tool:
                return "poetry"
        return "pip"

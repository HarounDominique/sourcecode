from __future__ import annotations

from typing import Any

from sourcecode.detectors.base import (
    AbstractDetector,
    DetectionContext,
    EntryPoint,
    StackDetection,
)
from sourcecode.detectors.parsers import load_json_file, unique_strings
from sourcecode.schema import FrameworkDetection
from sourcecode.tree_utils import path_exists_in_tree

_FRAMEWORK_MAP = {
    "next": "Next.js",
    "express": "Express",
    "react": "React",
    "vite": "Vite",
    "vue": "Vue",
    "svelte": "Svelte",
    "@nestjs/core": "NestJS",
}


class NodejsDetector(AbstractDetector):
    name = "nodejs"
    priority = 20

    def can_detect(self, context: DetectionContext) -> bool:
        return "package.json" in context.manifests

    def detect(self, context: DetectionContext) -> tuple[list[StackDetection], list[EntryPoint]]:
        package_json = load_json_file(context.root / "package.json")
        if package_json is None:
            return [], []

        dependency_names = self._collect_dependency_names(package_json)
        frameworks = [
            FrameworkDetection(name=label, source="package.json")
            for package_name, label in _FRAMEWORK_MAP.items()
            if package_name in dependency_names
        ]
        package_manager = self._detect_package_manager(context)
        entry_points = self._collect_entry_points(context, package_json)

        stack = StackDetection(
            stack="nodejs",
            detection_method="manifest",
            confidence="high",
            frameworks=frameworks,
            package_manager=package_manager,
            manifests=["package.json"],
        )
        return [stack], entry_points

    def _collect_dependency_names(self, package_json: dict[str, Any]) -> set[str]:
        names: set[str] = set()
        for field in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
            raw = package_json.get(field, {})
            if isinstance(raw, dict):
                names.update(str(name) for name in raw)
        return names

    def _detect_package_manager(self, context: DetectionContext) -> str | None:
        for filename, package_manager in (
            ("bun.lockb", "bun"),
            ("pnpm-lock.yaml", "pnpm"),
            ("package-lock.json", "npm"),
            ("yarn.lock", "yarn"),
        ):
            if path_exists_in_tree(context.file_tree, filename):
                return package_manager
        return None

    def _collect_entry_points(
        self, context: DetectionContext, package_json: dict[str, Any]
    ) -> list[EntryPoint]:
        entry_points: list[EntryPoint] = []
        seen: set[str] = set()
        main = package_json.get("main")
        if isinstance(main, str) and main.strip():
            path = main.strip()
            if path_exists_in_tree(context.file_tree, path):
                seen.add(path)
                entry_points.append(
                    EntryPoint(
                        path=path,
                        stack="nodejs",
                        kind="server",
                        source="package.json",
                        confidence="high",
                    )
                )

        convention_candidates: list[str] = []
        bin_field = package_json.get("bin")
        if isinstance(bin_field, str) and bin_field.strip():
            convention_candidates.append(bin_field.strip())
        elif isinstance(bin_field, dict):
            convention_candidates.extend(
                str(value).strip() for value in bin_field.values() if isinstance(value, str) and value.strip()
            )

        convention_candidates.extend(
            [
                "server.js",
                "src/index.js",
                "src/index.ts",
                "src/main.js",
                "src/main.ts",
                "src/main.tsx",
                "app/page.tsx",
                "pages/index.js",
            ]
        )

        for path in unique_strings(convention_candidates):
            if path in seen or not path_exists_in_tree(context.file_tree, path):
                continue
            kind = "web" if path.startswith(("app/", "pages/")) else "server"
            entry_points.append(
                EntryPoint(
                    path=path,
                    stack="nodejs",
                    kind=kind,
                    source="convention",
                    confidence="medium",
                )
            )
        return entry_points

"""Detector de proyectos PHP."""
from __future__ import annotations

from sourcecode.detectors.base import (
    AbstractDetector,
    DetectionContext,
    EntryPoint,
    StackDetection,
)
from sourcecode.detectors.parsers import load_json_file
from sourcecode.schema import FrameworkDetection
from sourcecode.tree_utils import path_exists_in_tree

_FRAMEWORK_MAP = {
    "laravel/framework": "Laravel",
    "symfony/framework-bundle": "Symfony",
}


class PhpDetector(AbstractDetector):
    name = "php"
    priority = 70

    def can_detect(self, context: DetectionContext) -> bool:
        return "composer.json" in context.manifests

    def detect(self, context: DetectionContext) -> tuple[list[StackDetection], list[EntryPoint]]:
        composer = load_json_file(context.root / "composer.json")
        if composer is None:
            return [], []

        dependencies = {}
        if isinstance(composer.get("require"), dict):
            dependencies.update(composer["require"])
        if isinstance(composer.get("require-dev"), dict):
            dependencies.update(composer["require-dev"])

        frameworks = [
            FrameworkDetection(name=label, source="composer.json")
            for package_name, label in _FRAMEWORK_MAP.items()
            if package_name in dependencies
        ]
        entry_points: list[EntryPoint] = []
        for path in ("artisan", "public/index.php"):
            if path_exists_in_tree(context.file_tree, path):
                entry_points.append(
                    EntryPoint(path=path, stack="php", kind="application", source="composer.json")
                )

        stack = StackDetection(
            stack="php",
            detection_method="manifest",
            confidence="high",
            frameworks=frameworks,
            package_manager="composer",
            manifests=["composer.json"],
        )
        return [stack], entry_points

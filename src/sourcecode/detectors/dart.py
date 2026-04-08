"""Detector de proyectos Dart y Flutter."""
from __future__ import annotations

from sourcecode.detectors.base import (
    AbstractDetector,
    DetectionContext,
    EntryPoint,
    StackDetection,
)
from sourcecode.detectors.parsers import read_text_lines
from sourcecode.schema import FrameworkDetection
from sourcecode.tree_utils import path_exists_in_tree


class DartDetector(AbstractDetector):
    name = "dart"
    priority = 90

    def can_detect(self, context: DetectionContext) -> bool:
        return "pubspec.yaml" in context.manifests

    def detect(self, context: DetectionContext) -> tuple[list[StackDetection], list[EntryPoint]]:
        content = "\n".join(read_text_lines(context.root / "pubspec.yaml")).lower()
        frameworks: list[FrameworkDetection] = []
        if "flutter:" in content:
            frameworks.append(FrameworkDetection(name="Flutter", source="pubspec.yaml"))

        entry_points: list[EntryPoint] = []
        for path in ("lib/main.dart", "bin/main.dart"):
            if path_exists_in_tree(context.file_tree, path):
                entry_points.append(
                    EntryPoint(path=path, stack="dart", kind="application", source="pubspec.yaml")
                )

        stack = StackDetection(
            stack="dart",
            detection_method="manifest",
            confidence="high",
            frameworks=frameworks,
            manifests=["pubspec.yaml"],
        )
        return [stack], entry_points

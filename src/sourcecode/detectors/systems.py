"""Detector de proyectos C / C++ y build systems asociados."""
from __future__ import annotations

from sourcecode.detectors.base import (
    AbstractDetector,
    DetectionContext,
    EntryPoint,
    StackDetection,
)
from sourcecode.detectors.parsers import unique_strings
from sourcecode.tree_utils import flatten_file_tree, path_exists_in_tree


class SystemsDetector(AbstractDetector):
    name = "systems"
    priority = 45

    def can_detect(self, context: DetectionContext) -> bool:
        flat_paths = flatten_file_tree(context.file_tree)
        has_code = any(path.endswith((".c", ".cc", ".cpp", ".cxx", ".hpp", ".h")) for path in flat_paths)
        has_build = any(path in {"CMakeLists.txt", "Makefile", "meson.build"} for path in flat_paths)
        return has_code and has_build

    def detect(self, context: DetectionContext) -> tuple[list[StackDetection], list[EntryPoint]]:
        flat_paths = flatten_file_tree(context.file_tree)
        manifests = [path for path in flat_paths if path in {"CMakeLists.txt", "Makefile", "meson.build"}]
        entry_points: list[EntryPoint] = []
        for path in unique_strings(["main.c", "main.cpp", "src/main.c", "src/main.cpp"]):
            if path_exists_in_tree(context.file_tree, path):
                entry_points.append(
                    EntryPoint(path=path, stack="cpp", kind="cli", source="manifest")
                )

        stack = StackDetection(
            stack="cpp",
            detection_method="manifest",
            confidence="medium",
            package_manager="cmake" if "CMakeLists.txt" in manifests else None,
            manifests=manifests,
        )
        return [stack], entry_points

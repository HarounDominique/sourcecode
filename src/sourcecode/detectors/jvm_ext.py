"""Detector JVM ampliado para Kotlin y Scala."""
from __future__ import annotations

from sourcecode.detectors.base import (
    AbstractDetector,
    DetectionContext,
    EntryPoint,
    StackDetection,
)
from sourcecode.detectors.parsers import read_text_lines, unique_strings
from sourcecode.schema import FrameworkDetection
from sourcecode.tree_utils import flatten_file_tree, path_exists_in_tree


class JvmExtDetector(AbstractDetector):
    name = "jvm_ext"
    priority = 36

    def can_detect(self, context: DetectionContext) -> bool:
        flat_paths = flatten_file_tree(context.file_tree)
        return any(
            path in {"build.gradle.kts", "settings.gradle.kts", "build.sbt"}
            for path in flat_paths
        )

    def detect(self, context: DetectionContext) -> tuple[list[StackDetection], list[EntryPoint]]:
        flat_paths = flatten_file_tree(context.file_tree)
        manifests = [
            path
            for path in flat_paths
            if path in {"build.gradle.kts", "settings.gradle.kts", "build.sbt"}
        ]
        content = "\n".join(
            "\n".join(read_text_lines(context.root / manifest))
            for manifest in manifests
        ).lower()

        stack_name = "scala" if "build.sbt" in manifests or any(path.endswith(".scala") for path in flat_paths) else "kotlin"
        frameworks: list[FrameworkDetection] = []
        if "spring-boot" in content:
            frameworks.append(FrameworkDetection(name="Spring Boot", source="manifest"))
        if "ktor" in content:
            frameworks.append(FrameworkDetection(name="Ktor", source="manifest"))
        if "play" in content and stack_name == "scala":
            frameworks.append(FrameworkDetection(name="Play", source="manifest"))

        entry_candidates = [
            "src/main/kotlin/Application.kt",
            "src/main/kotlin/Main.kt",
            "src/main/scala/Main.scala",
            "src/main/scala/Application.scala",
        ]
        entry_points: list[EntryPoint] = []
        for path in unique_strings(entry_candidates):
            if path_exists_in_tree(context.file_tree, path):
                kind = "api" if frameworks else "app"
                entry_points.append(
                    EntryPoint(path=path, stack=stack_name, kind=kind, source="manifest")
                )

        stack = StackDetection(
            stack=stack_name,
            detection_method="manifest",
            confidence="high",
            frameworks=frameworks,
            package_manager="gradle" if stack_name == "kotlin" else "sbt",
            manifests=manifests,
        )
        return [stack], entry_points

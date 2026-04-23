from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree

from sourcecode.detectors.base import (
    AbstractDetector,
    DetectionContext,
    EntryPoint,
    StackDetection,
)
from sourcecode.detectors.parsers import read_text_lines, unique_strings
from sourcecode.schema import FrameworkDetection
from sourcecode.tree_utils import flatten_file_tree


class JavaDetector(AbstractDetector):
    name = "java"
    priority = 60

    def can_detect(self, context: DetectionContext) -> bool:
        return any(manifest in context.manifests for manifest in ("pom.xml", "build.gradle"))

    def detect(self, context: DetectionContext) -> tuple[list[StackDetection], list[EntryPoint]]:
        frameworks: list[FrameworkDetection] = []
        manifests: list[str] = []

        if "pom.xml" in context.manifests:
            manifests.append("pom.xml")
            frameworks.extend(self._frameworks_from_pom(context.root / "pom.xml"))
        if "build.gradle" in context.manifests:
            manifests.append("build.gradle")
            frameworks.extend(self._frameworks_from_gradle(context.root / "build.gradle"))

        entry_points = self._collect_entry_points(context)
        stack = StackDetection(
            stack="java",
            detection_method="manifest",
            confidence="high",
            frameworks=self._dedupe_frameworks(frameworks),
            manifests=manifests,
        )
        return [stack], entry_points

    def _frameworks_from_pom(self, path: Path) -> list[FrameworkDetection]:
        try:
            tree = ElementTree.parse(path)
        except (OSError, ElementTree.ParseError):
            return []
        text = ElementTree.tostring(tree.getroot(), encoding="unicode").lower()
        frameworks: list[FrameworkDetection] = []
        if "spring-boot" in text:
            frameworks.append(FrameworkDetection(name="Spring Boot", source="pom.xml"))
        if "quarkus" in text:
            frameworks.append(FrameworkDetection(name="Quarkus", source="pom.xml"))
        return frameworks

    def _frameworks_from_gradle(self, path: Path) -> list[FrameworkDetection]:
        content = "\n".join(read_text_lines(path)).lower()
        frameworks: list[FrameworkDetection] = []
        if "com.android.application" in content or "com.android.library" in content:
            frameworks.append(FrameworkDetection(name="Android", source="build.gradle"))
        if "spring-boot" in content:
            frameworks.append(FrameworkDetection(name="Spring Boot", source="build.gradle"))
        if "quarkus" in content:
            frameworks.append(FrameworkDetection(name="Quarkus", source="build.gradle"))
        return frameworks

    def _collect_entry_points(self, context: DetectionContext) -> list[EntryPoint]:
        candidates = [
            path
            for path in flatten_file_tree(context.file_tree)
            if path.endswith(("Application.java", "Main.java"))
        ]
        return [
            EntryPoint(path=path, stack="java", kind="application", source="manifest")
            for path in unique_strings(candidates)
        ]

    def _dedupe_frameworks(self, frameworks: list[FrameworkDetection]) -> list[FrameworkDetection]:
        seen: set[str] = set()
        result: list[FrameworkDetection] = []
        for framework in frameworks:
            if framework.name not in seen:
                seen.add(framework.name)
                result.append(framework)
        return result

from __future__ import annotations

import re
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

_MAX_FILE_SIZE = 256 * 1024  # 256 KB
_MAX_JAVA_ENTRY_SCAN = 200
_MAX_ANNOTATION_ENTRY_POINTS = 20

_REST_CONTROLLER_RE = re.compile(r'@(?:Rest)?Controller\b')
_WEB_FILTER_RE = re.compile(r'@WebFilter\b')
_FILTER_BEAN_RE = re.compile(r'FilterRegistrationBean\b')


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
        all_paths = flatten_file_tree(context.file_tree)
        all_java = [p for p in all_paths if p.endswith(".java")]

        # 1. @SpringBootApplication entry: Application.java / Main.java by name
        app_candidates = [
            p for p in all_java
            if p.endswith(("Application.java", "Main.java"))
        ]
        entry_points: list[EntryPoint] = [
            EntryPoint(path=p, stack="java", kind="application", source="manifest")
            for p in unique_strings(app_candidates)
        ]

        # 2. Annotation-based scan: @RestController, @WebFilter, FilterRegistrationBean
        scan_candidates = [
            p for p in all_java
            if "/test/" not in p and "/tests/" not in p
        ][:_MAX_JAVA_ENTRY_SCAN]

        annotation_eps: list[EntryPoint] = []
        for rel_path in scan_candidates:
            if len(annotation_eps) >= _MAX_ANNOTATION_ENTRY_POINTS:
                break
            abs_path = context.root / rel_path
            annotation_eps.extend(self._scan_java_file_for_entry_points(abs_path, rel_path))
        entry_points.extend(annotation_eps)

        # 3. web.xml servlet/filter declarations
        web_xml_paths = [
            p for p in all_paths
            if p.endswith("web.xml") and "WEB-INF" in p
        ]
        for rel_path in web_xml_paths[:1]:
            abs_path = context.root / rel_path
            entry_points.extend(self._parse_web_xml(abs_path, rel_path))

        # Deduplicate by (path, kind)
        seen: set[tuple[str, str]] = set()
        unique_eps: list[EntryPoint] = []
        for ep in entry_points:
            key = (ep.path, ep.kind)
            if key not in seen:
                seen.add(key)
                unique_eps.append(ep)
        return unique_eps

    def _scan_java_file_for_entry_points(self, abs_path: Path, rel_path: str) -> list[EntryPoint]:
        try:
            if abs_path.stat().st_size > _MAX_FILE_SIZE:
                return []
            content = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []

        # Quick pre-filter before running regexes
        if "Controller" not in content and "Filter" not in content:
            return []

        if _REST_CONTROLLER_RE.search(content):
            return [EntryPoint(
                path=rel_path, stack="java", kind="http_handler",
                source="annotation", confidence="high",
            )]
        if _WEB_FILTER_RE.search(content):
            return [EntryPoint(
                path=rel_path, stack="java", kind="filter",
                source="annotation", confidence="high",
            )]
        if _FILTER_BEAN_RE.search(content):
            return [EntryPoint(
                path=rel_path, stack="java", kind="filter",
                source="annotation", confidence="medium",
            )]
        return []

    def _parse_web_xml(self, abs_path: Path, rel_path: str) -> list[EntryPoint]:
        try:
            tree = ElementTree.parse(abs_path)
        except (OSError, ElementTree.ParseError):
            return []

        root = tree.getroot()
        ns_match = re.match(r"\{[^}]+\}", root.tag)
        ns = ns_match.group(0) if ns_match else ""

        results: list[EntryPoint] = []
        if root.findall(f".//{ns}servlet-class"):
            results.append(EntryPoint(
                path=rel_path, stack="java", kind="servlet",
                source="xml_config", confidence="high",
            ))
        if root.findall(f".//{ns}filter-class"):
            results.append(EntryPoint(
                path=rel_path, stack="java", kind="filter",
                source="xml_config", confidence="high",
            ))
        return results

    def _dedupe_frameworks(self, frameworks: list[FrameworkDetection]) -> list[FrameworkDetection]:
        seen: set[str] = set()
        result: list[FrameworkDetection] = []
        for framework in frameworks:
            if framework.name not in seen:
                seen.add(framework.name)
                result.append(framework)
        return result

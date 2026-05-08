from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from sourcecode.classifier import TypeClassifier
from sourcecode.detectors.base import AbstractDetector, DetectionContext
from sourcecode.detectors.tooling import collect_tooling_signals, infer_package_manager
from sourcecode.scanner import classify_manifest
from sourcecode.schema import EntryPoint, FrameworkDetection, StackDetection

_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


class ProjectDetector:
    """Ejecuta detectores y fusiona resultados de forma determinista."""

    def __init__(
        self,
        detectors: Sequence[AbstractDetector],
        classifier: TypeClassifier | None = None,
    ) -> None:
        self.detectors = sorted(detectors, key=lambda detector: detector.priority)
        self.classifier = classifier or TypeClassifier()

    def detect(
        self,
        root: Path,
        file_tree: dict[str, Any],
        manifests: Sequence[str],
    ) -> tuple[list[StackDetection], list[EntryPoint], str | None]:
        manifest_names = [Path(manifest).name for manifest in manifests]
        manifest_types = {
            Path(m).name: classify_manifest(m, root) for m in manifests
        }
        context = DetectionContext(
            root=root,
            file_tree=file_tree,
            manifests=manifest_names,
            manifest_types=manifest_types,
        )
        merged_stacks: dict[str, StackDetection] = {}
        merged_entry_points: dict[str, EntryPoint] = {}

        for detector in self.detectors:
            if not detector.can_detect(context):
                continue

            stacks, entry_points = detector.detect(context)
            # Stamp provenance: every emitted stack and EP knows which detector produced it
            for item in stacks:
                item.produced_by = detector.name
            for item in entry_points:
                item.produced_by = detector.name

            for stack in stacks:
                existing = merged_stacks.get(stack.stack)
                if existing is None:
                    copied = self._copy_stack(stack)
                    if copied.root is None:
                        copied.root = "."
                    copied.signals = self._merge_values(
                        copied.signals,
                        collect_tooling_signals(file_tree),
                    )
                    if copied.package_manager is None:
                        copied.package_manager = infer_package_manager(copied.stack, file_tree)
                    merged_stacks[stack.stack] = copied
                    continue
                merged_stacks[stack.stack] = self._merge_stack(existing, stack)

            detected_stack_names = set(merged_stacks.keys())
            for entry_point in entry_points:
                if entry_point.stack not in detected_stack_names:
                    continue
                merged_entry_points.setdefault(entry_point.path, entry_point)

        enriched_stacks, project_type = self.classify_results(
            file_tree,
            list(merged_stacks.values()),
            list(merged_entry_points.values()),
        )
        return enriched_stacks, list(merged_entry_points.values()), project_type

    def classify_results(
        self,
        file_tree: dict[str, Any],
        stacks: Sequence[StackDetection],
        entry_points: Sequence[EntryPoint],
        *,
        project_type_override: str | None = None,
    ) -> tuple[list[StackDetection], str | None]:
        enriched_stacks, project_type = self.classifier.enrich(file_tree, stacks, entry_points)
        return enriched_stacks, project_type_override or project_type

    def _copy_stack(self, stack: StackDetection) -> StackDetection:
        return StackDetection(
            stack=stack.stack,
            detection_method=stack.detection_method,
            confidence=stack.confidence,
            frameworks=[
                FrameworkDetection(
                    name=framework.name,
                    source=framework.source,
                    confidence=framework.confidence,
                    detected_via=list(framework.detected_via),
                    version=framework.version,
                )
                for framework in stack.frameworks
            ],
            package_manager=stack.package_manager,
            manifests=list(stack.manifests),
            primary=stack.primary,
            root=stack.root,
            workspace=stack.workspace,
            signals=list(stack.signals),
            produced_by=stack.produced_by,
            # Java-specific fields
            language_version=stack.language_version,
            packaging=stack.packaging,
            app_server_hint=stack.app_server_hint,
            spring_profiles=list(stack.spring_profiles),
        )

    def _merge_stack(self, current: StackDetection, incoming: StackDetection) -> StackDetection:
        current.frameworks = self._merge_frameworks(current.frameworks, incoming.frameworks)
        current.manifests = self._merge_manifests(current.manifests, incoming.manifests)
        current.signals = self._merge_values(current.signals, incoming.signals)
        if incoming.package_manager and not current.package_manager:
            current.package_manager = incoming.package_manager
        if self._confidence_rank(incoming.confidence) > self._confidence_rank(current.confidence):
            current.confidence = incoming.confidence
            current.detection_method = incoming.detection_method
        elif self._confidence_rank(incoming.confidence) == self._confidence_rank(current.confidence):
            if current.detection_method == "heuristic" and incoming.detection_method != "heuristic":
                current.detection_method = incoming.detection_method
        # Java-specific: propagate from incoming if not already set
        if incoming.language_version and not current.language_version:
            current.language_version = incoming.language_version
        if incoming.packaging and not current.packaging:
            current.packaging = incoming.packaging
        if incoming.app_server_hint and not current.app_server_hint:
            current.app_server_hint = incoming.app_server_hint
        if incoming.spring_profiles and not current.spring_profiles:
            current.spring_profiles = list(incoming.spring_profiles)
        return current

    def _merge_frameworks(
        self,
        current: Iterable[FrameworkDetection],
        incoming: Iterable[FrameworkDetection],
    ) -> list[FrameworkDetection]:
        merged: dict[str, FrameworkDetection] = {}
        for framework in list(current) + list(incoming):
            if framework.name not in merged:
                merged[framework.name] = FrameworkDetection(
                    name=framework.name,
                    source=framework.source,
                    confidence=framework.confidence,
                    detected_via=list(framework.detected_via),
                    version=framework.version,
                )
            elif framework.version and not merged[framework.name].version:
                # Preserve version from whichever source has it
                from dataclasses import replace as _fr_replace
                merged[framework.name] = _fr_replace(merged[framework.name], version=framework.version)
        return list(merged.values())

    def _merge_manifests(self, current: Iterable[str], incoming: Iterable[str]) -> list[str]:
        return self._merge_values(current, incoming)

    def _merge_values(self, current: Iterable[str], incoming: Iterable[str]) -> list[str]:
        merged: list[str] = []
        seen: set[str] = set()
        for manifest in list(current) + list(incoming):
            if manifest not in seen:
                seen.add(manifest)
                merged.append(manifest)
        return merged

    def _confidence_rank(self, confidence: str) -> int:
        return _CONFIDENCE_RANK.get(confidence, -1)

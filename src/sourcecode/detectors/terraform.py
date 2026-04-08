"""Detector de proyectos Terraform."""
from __future__ import annotations

from sourcecode.detectors.base import (
    AbstractDetector,
    DetectionContext,
    EntryPoint,
    StackDetection,
)
from sourcecode.detectors.parsers import read_text_lines
from sourcecode.tree_utils import flatten_file_tree, path_exists_in_tree


class TerraformDetector(AbstractDetector):
    name = "terraform"
    priority = 40

    def can_detect(self, context: DetectionContext) -> bool:
        return any(path.endswith(".tf") for path in flatten_file_tree(context.file_tree))

    def detect(self, context: DetectionContext) -> tuple[list[StackDetection], list[EntryPoint]]:
        manifests = [path for path in flatten_file_tree(context.file_tree) if path.endswith(".tf")]
        signals: list[str] = []
        for manifest in manifests:
            content = "\n".join(read_text_lines(context.root / manifest)).lower()
            if 'provider "aws"' in content:
                signals.append("provider:aws")
            if 'provider "azurerm"' in content:
                signals.append("provider:azure")
            if 'provider "google"' in content:
                signals.append("provider:gcp")

        entry_points: list[EntryPoint] = []
        if path_exists_in_tree(context.file_tree, "main.tf"):
            entry_points.append(
                EntryPoint(path="main.tf", stack="terraform", kind="infra", source="manifest")
            )

        stack = StackDetection(
            stack="terraform",
            detection_method="manifest",
            confidence="medium",
            package_manager="terraform",
            manifests=manifests,
            signals=signals,
        )
        return [stack], entry_points

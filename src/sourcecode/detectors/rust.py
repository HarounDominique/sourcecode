"""Detector de proyectos Rust."""
from __future__ import annotations

from typing import Any

from sourcecode.detectors.base import (
    AbstractDetector,
    DetectionContext,
    EntryPoint,
    StackDetection,
)
from sourcecode.detectors.parsers import load_toml_file, unique_strings
from sourcecode.schema import FrameworkDetection
from sourcecode.tree_utils import path_exists_in_tree

_FRAMEWORK_MAP = {
    "axum": "Axum",
    "actix-web": "Actix Web",
    "rocket": "Rocket",
    "clap": "Clap",
}


class RustDetector(AbstractDetector):
    name = "rust"
    priority = 50

    def can_detect(self, context: DetectionContext) -> bool:
        return "Cargo.toml" in context.manifests

    def detect(self, context: DetectionContext) -> tuple[list[StackDetection], list[EntryPoint]]:
        cargo = load_toml_file(context.root / "Cargo.toml")
        if cargo is None:
            return [], []

        dependencies = cargo.get("dependencies", {})
        frameworks = [
            FrameworkDetection(name=label, source="Cargo.toml")
            for package_name, label in _FRAMEWORK_MAP.items()
            if isinstance(dependencies, dict) and package_name in dependencies
        ]
        entry_points = self._collect_entry_points(context, cargo)
        stack = StackDetection(
            stack="rust",
            detection_method="manifest",
            confidence="high",
            frameworks=frameworks,
            manifests=["Cargo.toml"],
        )
        return [stack], entry_points

    def _collect_entry_points(self, context: DetectionContext, cargo: dict[str, Any]) -> list[EntryPoint]:
        candidates: list[str] = []
        if path_exists_in_tree(context.file_tree, "src/main.rs"):
            candidates.append("src/main.rs")

        bins = cargo.get("bin", [])
        if isinstance(bins, list):
            for item in bins:
                if isinstance(item, dict):
                    path = item.get("path")
                    if isinstance(path, str) and path.strip():
                        candidates.append(path.strip())

        return [
            EntryPoint(path=path, stack="rust", kind="binary", source="Cargo.toml")
            for path in unique_strings(candidates)
            if path_exists_in_tree(context.file_tree, path)
        ]

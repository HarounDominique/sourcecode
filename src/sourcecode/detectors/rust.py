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
    "tokio": "Tokio",
    "tauri": "Tauri",
    "tonic": "tonic/gRPC",
    "warp": "Warp",
    "poem": "Poem",
    "sqlx": "sqlx",
    "diesel": "Diesel",
    "tower": "Tower",
    "serde": "Serde",
}


class RustDetector(AbstractDetector):
    name = "rust"
    priority = 50

    def can_detect(self, context: DetectionContext) -> bool:
        return "Cargo.toml" in context.manifests

    def detect(self, context: DetectionContext) -> tuple[list[StackDetection], list[EntryPoint]]:
        from sourcecode.detectors.hybrid import merge_framework_detections, scan_for_frameworks

        cargo = load_toml_file(context.root / "Cargo.toml")
        if cargo is None:
            return [], []

        all_deps: dict[str, Any] = {}
        top_deps = cargo.get("dependencies", {})
        if isinstance(top_deps, dict):
            all_deps.update(top_deps)
        ws_deps = cargo.get("workspace", {})
        if isinstance(ws_deps, dict):
            wd = ws_deps.get("dependencies", {})
            if isinstance(wd, dict):
                all_deps.update(wd)

        manifest_frameworks = [
            FrameworkDetection(name=label, source="Cargo.toml")
            for package_name, label in _FRAMEWORK_MAP.items()
            if package_name in all_deps
        ]
        entry_points = self._collect_entry_points(context, cargo)
        priority = [ep.path for ep in entry_points]
        import_frameworks = scan_for_frameworks(context.root, context.file_tree, "rust", priority_paths=priority)
        frameworks = merge_framework_detections(manifest_frameworks, import_frameworks)

        signals: list[str] = []
        ws = cargo.get("workspace", {})
        if isinstance(ws, dict):
            members = ws.get("members", [])
            if isinstance(members, list) and members:
                signals.append(f"workspace:{len(members)} crates")
                crate_names = [str(m).rstrip("/").split("/")[-1] for m in members[:6]]
                signals.append(f"crates: {', '.join(crate_names)}")

        stack = StackDetection(
            stack="rust",
            detection_method="manifest",
            confidence="high",
            frameworks=frameworks,
            manifests=["Cargo.toml"],
            signals=signals,
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

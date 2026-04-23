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


class ElixirDetector(AbstractDetector):
    name = "elixir"
    priority = 33

    def can_detect(self, context: DetectionContext) -> bool:
        return path_exists_in_tree(context.file_tree, "mix.exs")

    def detect(self, context: DetectionContext) -> tuple[list[StackDetection], list[EntryPoint]]:
        content = "\n".join(read_text_lines(context.root / "mix.exs")).lower()
        frameworks: list[FrameworkDetection] = []
        if "phoenix" in content:
            frameworks.append(FrameworkDetection(name="Phoenix", source="mix.exs"))

        candidates = [
            path
            for path in flatten_file_tree(context.file_tree)
            if path.endswith("/application.ex")
        ]
        entry_points: list[EntryPoint] = []
        for path in unique_strings(candidates + ["mix.exs"]):
            if path_exists_in_tree(context.file_tree, path):
                kind = "web" if frameworks else "app"
                entry_points.append(
                    EntryPoint(path=path, stack="elixir", kind=kind, source="mix.exs")
                )

        stack = StackDetection(
            stack="elixir",
            detection_method="manifest",
            confidence="high",
            frameworks=frameworks,
            package_manager="mix",
            manifests=["mix.exs"],
        )
        return [stack], entry_points

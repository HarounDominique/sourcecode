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


class RubyDetector(AbstractDetector):
    name = "ruby"
    priority = 80

    def can_detect(self, context: DetectionContext) -> bool:
        return "Gemfile" in context.manifests

    def detect(self, context: DetectionContext) -> tuple[list[StackDetection], list[EntryPoint]]:
        content = "\n".join(read_text_lines(context.root / "Gemfile")).lower()
        frameworks: list[FrameworkDetection] = []
        if "gem 'rails'" in content or 'gem "rails"' in content:
            frameworks.append(FrameworkDetection(name="Rails", source="Gemfile"))
        if "gem 'sinatra'" in content or 'gem "sinatra"' in content:
            frameworks.append(FrameworkDetection(name="Sinatra", source="Gemfile"))

        entry_points: list[EntryPoint] = []
        for path in ("bin/rails", "config.ru", "app.rb"):
            if path_exists_in_tree(context.file_tree, path):
                entry_points.append(
                    EntryPoint(path=path, stack="ruby", kind="application", source="Gemfile")
                )

        stack = StackDetection(
            stack="ruby",
            detection_method="manifest",
            confidence="high",
            frameworks=frameworks,
            manifests=["Gemfile"],
        )
        return [stack], entry_points

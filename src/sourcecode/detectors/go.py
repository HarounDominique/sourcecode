"""Detector de proyectos Go."""
from __future__ import annotations

from sourcecode.detectors.base import (
    AbstractDetector,
    DetectionContext,
    EntryPoint,
    StackDetection,
)
from sourcecode.detectors.parsers import read_text_lines, unique_strings
from sourcecode.schema import FrameworkDetection
from sourcecode.tree_utils import flatten_file_tree

_FRAMEWORK_MAP = {
    "github.com/gin-gonic/gin": "Gin",
    "github.com/labstack/echo": "Echo",
    "github.com/spf13/cobra": "Cobra",
    "github.com/gofiber/fiber": "Fiber",
}


class GoDetector(AbstractDetector):
    name = "go"
    priority = 40

    def can_detect(self, context: DetectionContext) -> bool:
        return "go.mod" in context.manifests

    def detect(self, context: DetectionContext) -> tuple[list[StackDetection], list[EntryPoint]]:
        lines = read_text_lines(context.root / "go.mod")
        content = "\n".join(lines)
        frameworks = [
            FrameworkDetection(name=label, source="go.mod")
            for dependency, label in _FRAMEWORK_MAP.items()
            if dependency in content
        ]
        entry_candidates = [
            path for path in flatten_file_tree(context.file_tree) if path.endswith("main.go")
        ]
        preferred = [path for path in entry_candidates if path.startswith("cmd/")] or entry_candidates
        entry_points = [
            EntryPoint(path=path, stack="go", kind="binary", source="go.mod")
            for path in unique_strings(preferred)
        ]
        stack = StackDetection(
            stack="go",
            detection_method="manifest",
            confidence="high",
            frameworks=frameworks,
            manifests=["go.mod"],
        )
        return [stack], entry_points

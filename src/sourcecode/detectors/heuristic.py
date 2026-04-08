"""Fallback heuristico por extension de fichero."""
from __future__ import annotations

from collections import Counter

from sourcecode.detectors.base import (
    AbstractDetector,
    DetectionContext,
    EntryPoint,
    StackDetection,
)
from sourcecode.tree_utils import flatten_file_tree

_EXTENSION_MAP = {
    ".py": "python",
    ".js": "nodejs",
    ".ts": "nodejs",
    ".tsx": "nodejs",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".php": "php",
    ".rb": "ruby",
    ".dart": "dart",
}

_ENTRYPOINT_NAMES = {
    "main.py": ("python", "script"),
    "app.py": ("python", "app"),
    "index.js": ("nodejs", "server"),
    "main.go": ("go", "binary"),
    "main.rs": ("rust", "binary"),
}


class HeuristicDetector(AbstractDetector):
    name = "heuristic"
    priority = 999

    def can_detect(self, context: DetectionContext) -> bool:
        return True

    def detect(self, context: DetectionContext) -> tuple[list[StackDetection], list[EntryPoint]]:
        paths = flatten_file_tree(context.file_tree)
        counts: Counter[str] = Counter()
        for path in paths:
            for extension, stack in _EXTENSION_MAP.items():
                if path.endswith(extension):
                    counts[stack] += 1
                    break

        stacks = [
            StackDetection(
                stack=stack,
                detection_method="heuristic",
                confidence="low",
                manifests=[],
            )
            for stack, _count in counts.most_common()
        ]

        entry_points: list[EntryPoint] = []
        for path in paths:
            filename = path.rsplit("/", 1)[-1]
            if filename in _ENTRYPOINT_NAMES:
                stack, kind = _ENTRYPOINT_NAMES[filename]
                entry_points.append(
                    EntryPoint(path=path, stack=stack, kind=kind, source="heuristic")
                )
        return stacks, entry_points

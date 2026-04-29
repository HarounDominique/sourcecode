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
    "github.com/go-chi/chi": "chi",
    "github.com/gorilla/mux": "gorilla/mux",
    "google.golang.org/grpc": "gRPC",
    "connectrpc.com/connect": "ConnectRPC",
    "github.com/urfave/cli": "urfave/cli",
    "github.com/grpc-ecosystem/grpc-gateway": "gRPC-Gateway",
}


class GoDetector(AbstractDetector):
    name = "go"
    priority = 40

    def can_detect(self, context: DetectionContext) -> bool:
        return "go.mod" in context.manifests

    def detect(self, context: DetectionContext) -> tuple[list[StackDetection], list[EntryPoint]]:
        from sourcecode.detectors.hybrid import merge_framework_detections, scan_for_frameworks

        lines = read_text_lines(context.root / "go.mod")
        content = "\n".join(lines)
        manifest_frameworks = [
            FrameworkDetection(name=label, source="go.mod")
            for dependency, label in _FRAMEWORK_MAP.items()
            if dependency in content
        ]
        entry_candidates = [
            path for path in flatten_file_tree(context.file_tree) if path.endswith("main.go")
        ]
        preferred = [path for path in entry_candidates if path.startswith("cmd/")] or entry_candidates
        entry_points = [
            EntryPoint(
                path=path,
                stack="go",
                kind="binary",
                source="convention",
                confidence="medium",
            )
            for path in unique_strings(preferred)
        ]
        priority = [ep.path for ep in entry_points]
        import_frameworks = scan_for_frameworks(context.root, context.file_tree, "go", priority_paths=priority)
        frameworks = merge_framework_detections(manifest_frameworks, import_frameworks)
        signals: list[str] = []
        manifests = ["go.mod"]
        if (context.root / "go.work").is_file():
            signals.append("workspace:go.work")
            manifests.append("go.work")
        if len(preferred) > 1:
            signals.append(f"multi-binary:{len(preferred)}")
        stack = StackDetection(
            stack="go",
            detection_method="manifest",
            confidence="high",
            frameworks=frameworks,
            manifests=manifests,
            signals=signals,
        )
        return [stack], entry_points

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


class DotnetDetector(AbstractDetector):
    name = "dotnet"
    priority = 32

    def can_detect(self, context: DetectionContext) -> bool:
        return any(
            path.endswith((".csproj", ".sln"))
            for path in flatten_file_tree(context.file_tree)
        )

    def detect(self, context: DetectionContext) -> tuple[list[StackDetection], list[EntryPoint]]:
        manifests = [
            path
            for path in flatten_file_tree(context.file_tree)
            if path.endswith((".csproj", ".sln"))
        ]
        content = "\n".join(
            "\n".join(read_text_lines(context.root / manifest))
            for manifest in manifests
        ).lower()

        frameworks: list[FrameworkDetection] = []
        if "microsoft.net.sdk.web" in content or "microsoft.aspnetcore" in content:
            frameworks.append(FrameworkDetection(name="ASP.NET Core", source="manifest"))

        entry_points: list[EntryPoint] = []
        for path in unique_strings(
            ["Program.cs", "src/Program.cs", "Program.fs", "src/Program.fs"]
        ):
            if path_exists_in_tree(context.file_tree, path):
                kind = "api" if frameworks else "cli"
                entry_points.append(
                    EntryPoint(path=path, stack="dotnet", kind=kind, source="manifest")
                )

        stack = StackDetection(
            stack="dotnet",
            detection_method="manifest",
            confidence="high",
            frameworks=frameworks,
            package_manager="nuget",
            manifests=manifests,
        )
        return [stack], entry_points

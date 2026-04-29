from __future__ import annotations

from collections import Counter

from sourcecode.detectors.base import (
    AbstractDetector,
    DetectionContext,
    EntryPoint,
    StackDetection,
)
from sourcecode.detectors.csproj_parser import (
    CsprojProject,
    infer_architecture_pattern,
    parse_csproj,
)
from sourcecode.schema import FrameworkDetection
from sourcecode.tree_utils import flatten_file_tree, path_exists_in_tree

_PROJECT_EXTENSIONS = (".csproj", ".fsproj", ".vbproj")

_FRAMEWORK_SIGNALS: tuple[tuple[str, str], ...] = (
    ("microsoft.net.sdk.web", "ASP.NET Core"),
    ("microsoft.net.sdk.blazor", "Blazor"),
    ("microsoft.net.sdk.worker", "Worker Service"),
    ("microsoft.aspnetcore", "ASP.NET Core"),
)

_ENTRY_CANDIDATES = (
    "Program.cs",
    "src/Program.cs",
    "Program.fs",
    "src/Program.fs",
    "Startup.cs",
    "src/Startup.cs",
)


class DotnetDetector(AbstractDetector):
    name = "dotnet"
    priority = 32

    def can_detect(self, context: DetectionContext) -> bool:
        return any(
            path.endswith((*_PROJECT_EXTENSIONS, ".sln"))
            for path in flatten_file_tree(context.file_tree)
        )

    def detect(self, context: DetectionContext) -> tuple[list[StackDetection], list[EntryPoint]]:
        all_paths = flatten_file_tree(context.file_tree)
        project_paths = [p for p in all_paths if p.endswith(_PROJECT_EXTENSIONS)]

        projects: list[CsprojProject] = []
        for rel_path in project_paths:
            project = parse_csproj(context.root / rel_path, rel_path)
            if project is not None:
                projects.append(project)

        frameworks = self._collect_frameworks(projects)
        signals = self._build_signals(projects)
        entry_points = self._detect_entry_points(context, projects, frameworks)

        manifests = project_paths + [p for p in all_paths if p.endswith(".sln")]
        stack = StackDetection(
            stack="dotnet",
            detection_method="manifest",
            confidence="high",
            frameworks=frameworks,
            package_manager="nuget",
            manifests=manifests,
            signals=signals,
        )
        return [stack], entry_points

    def _collect_frameworks(self, projects: list[CsprojProject]) -> list[FrameworkDetection]:
        seen: dict[str, FrameworkDetection] = {}
        for project in projects:
            content_lower = project.sdk.lower()
            for pkg_name, _ in project.package_references:
                content_lower += " " + pkg_name.lower()
            for signal, framework_name in _FRAMEWORK_SIGNALS:
                if signal in content_lower and framework_name not in seen:
                    seen[framework_name] = FrameworkDetection(name=framework_name, source="manifest")
        return list(seen.values())

    def _build_signals(self, projects: list[CsprojProject]) -> list[str]:
        if not projects:
            return []
        signals: list[str] = []

        count = len(projects)
        signals.append(f"{count} project{'s' if count != 1 else ''} detected")

        type_counter: Counter[str] = Counter(p.project_type for p in projects)
        type_parts = []
        for ptype, n in type_counter.most_common():
            type_parts.append(f"{ptype}×{n}" if n > 1 else ptype)
        signals.append(f"project types: {', '.join(type_parts)}")

        frameworks_seen: set[str] = set()
        all_frameworks: list[str] = []
        for project in projects:
            for fw in project.target_frameworks:
                if fw and fw not in frameworks_seen:
                    frameworks_seen.add(fw)
                    all_frameworks.append(fw)
        if all_frameworks:
            signals.append(f"target frameworks: {', '.join(all_frameworks[:4])}")

        pattern = infer_architecture_pattern(projects)
        if pattern:
            signals.append(f"architecture: {pattern}")

        return signals

    def _detect_entry_points(
        self,
        context: DetectionContext,
        projects: list[CsprojProject],
        frameworks: list[FrameworkDetection],
    ) -> list[EntryPoint]:
        entry_points: list[EntryPoint] = []
        framework_names = {f.name for f in frameworks}
        has_web = bool(framework_names & {"ASP.NET Core", "Blazor"})

        # Entry points from exe/web projects
        for project in projects:
            if project.project_type in ("webapi", "console", "worker", "blazor"):
                kind = _project_type_to_entry_kind(project.project_type, has_web)
                # Look for Program.cs/Startup.cs relative to the project dir
                for candidate in _entry_file_candidates(project):
                    if path_exists_in_tree(context.file_tree, candidate):
                        entry_points.append(
                            EntryPoint(
                                path=candidate,
                                stack="dotnet",
                                kind=kind,
                                source="manifest",
                                confidence="high",
                            )
                        )
                        break

        # Fallback: known entry file patterns
        if not entry_points:
            kind = "api" if has_web else "cli"
            for candidate in _ENTRY_CANDIDATES:
                if path_exists_in_tree(context.file_tree, candidate):
                    entry_points.append(
                        EntryPoint(
                            path=candidate,
                            stack="dotnet",
                            kind=kind,
                            source="manifest",
                            confidence="medium",
                        )
                    )
                    break

        return entry_points


def _project_type_to_entry_kind(project_type: str, has_web: bool) -> str:
    if project_type == "webapi":
        return "api"
    if project_type == "blazor":
        return "web"
    if project_type == "worker":
        return "server"
    return "cli"


def _entry_file_candidates(project: CsprojProject) -> list[str]:
    prefix = f"{project.project_dir}/" if project.project_dir else ""
    candidates = [
        f"{prefix}Program.cs",
        f"{prefix}Program.fs",
        f"{prefix}Startup.cs",
    ]
    # Also check without prefix for root-level projects
    if prefix:
        candidates += ["Program.cs", "src/Program.cs"]
    return candidates

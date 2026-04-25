from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import replace
from typing import Any, Literal

from sourcecode.schema import EntryPoint, StackDetection
from sourcecode.tree_utils import flatten_file_tree

_API_FRAMEWORKS = {
    "ASP.NET Core",
    "FastAPI",
    "Django",
    "Flask",
    "Express",
    "Gin",
    "Echo",
    "Axum",
    "Actix Web",
    "Rocket",
    "Spring Boot",
    "Quarkus",
    "Laravel",
    "Symfony",
    "Ktor",
    "Play",
}
_WEB_FRAMEWORKS = {"Next.js", "React", "Vue", "Svelte", "Vite", "Flutter", "Phoenix"}
_CLI_FRAMEWORKS = {"Typer", "Cobra", "Clap"}
_API_STACKS = {"python", "go", "java", "php", "ruby", "dotnet", "kotlin", "scala"}
ConfidenceLevel = Literal["high", "medium", "low"]


class TypeClassifier:
    """Clasifica project_type y enriquece stacks con confianza/primary."""

    def enrich(
        self,
        file_tree: dict[str, Any],
        stacks: Sequence[StackDetection],
        entry_points: Sequence[EntryPoint],
    ) -> tuple[list[StackDetection], str | None]:
        enriched = [self._enrich_stack(file_tree, stack, entry_points) for stack in stacks]
        project_type = self._classify_project_type(file_tree, enriched, entry_points)
        primary_stack = self._select_primary_stack(enriched, project_type)

        final_stacks: list[StackDetection] = []
        for stack in enriched:
            final_stacks.append(replace(stack, primary=(stack.stack == primary_stack)))
        return final_stacks, project_type

    def _enrich_stack(
        self,
        file_tree: dict[str, Any],
        stack: StackDetection,
        entry_points: Sequence[EntryPoint],
    ) -> StackDetection:
        signals = list(stack.signals)
        score = 0

        if stack.manifests:
            score += 4
            signals.extend(f"manifest:{manifest}" for manifest in stack.manifests)
        if stack.frameworks:
            score += 2
            signals.extend(f"framework:{framework.name}" for framework in stack.frameworks)
        if stack.package_manager:
            score += 2
            signals.append(f"package_manager:{stack.package_manager}")

        matching_entry_points = [entry for entry in entry_points if entry.stack == stack.stack]
        if matching_entry_points:
            score += 2
            signals.extend(f"entry:{entry.path}" for entry in matching_entry_points)

        extension_hits = self._count_extension_hits(file_tree, stack.stack)
        if extension_hits >= 2:
            score += 1
            signals.append(f"extensions:{extension_hits}")

        if stack.detection_method == "heuristic":
            score -= 2
            signals.append("method:heuristic")

        confidence = self._score_to_confidence(score)
        return replace(stack, confidence=confidence, signals=self._unique(signals))

    def _classify_project_type(
        self,
        file_tree: dict[str, Any],
        stacks: Sequence[StackDetection],
        entry_points: Sequence[EntryPoint],
    ) -> str | None:
        flat_paths = set(flatten_file_tree(file_tree))
        stack_names = {stack.stack for stack in stacks}
        framework_names = {framework.name for stack in stacks for framework in stack.frameworks}

        if len(stack_names) >= 2 and self._is_fullstack(stacks):
            return "fullstack"

        if "src/lib.rs" in flat_paths and not any(path.endswith("main.rs") for path in flat_paths):
            return "library"

        if framework_names & _WEB_FRAMEWORKS or any(
            path.startswith(("app/", "pages/", "components/")) for path in flat_paths
        ):
            return "webapp"

        if framework_names & _API_FRAMEWORKS:
            return "api"

        if framework_names & _CLI_FRAMEWORKS or any(
            entry.kind == "cli" for entry in entry_points
        ) or any(path.startswith("bin/") for path in flat_paths):
            return "cli"

        if stack_names:
            single = next(iter(stack_names))
            if single in _API_STACKS and framework_names & _API_FRAMEWORKS:
                return "api"
            if single in {"cpp", "dotnet"} and any(entry.kind == "cli" for entry in entry_points):
                return "cli"

        return "unknown" if stacks else None

    def _is_fullstack(self, stacks: Sequence[StackDetection]) -> bool:
        has_web = False
        has_api = False
        for stack in stacks:
            frameworks = {framework.name for framework in stack.frameworks}
            if frameworks & _WEB_FRAMEWORKS:
                has_web = True
            elif stack.stack == "nodejs" and stack.detection_method != "heuristic":
                has_web = True
            if frameworks & _API_FRAMEWORKS or stack.stack in _API_STACKS:
                has_api = True
        return has_web and has_api

    def _select_primary_stack(
        self, stacks: Sequence[StackDetection], project_type: str | None
    ) -> str | None:
        if not stacks:
            return None

        def sort_key(stack: StackDetection) -> tuple[int, int, int]:
            score = {"low": 0, "medium": 1, "high": 2}.get(stack.confidence, 0)
            manifest_weight = 1 if stack.manifests else 0
            priority = 0
            if project_type in {"webapp", "fullstack"} and stack.stack in {"nodejs", "elixir"} or project_type == "api" and stack.stack in _API_STACKS or project_type == "cli" and any(
                framework.name in _CLI_FRAMEWORKS for framework in stack.frameworks
            ) or project_type == "cli" and stack.stack in {"cpp", "dotnet"}:
                priority = 3
            return (priority, score, manifest_weight)

        return max(stacks, key=sort_key).stack

    def _count_extension_hits(self, file_tree: dict[str, Any], stack: str) -> int:
        extensions = {
            "python": (".py",),
            "nodejs": (".js", ".ts", ".tsx"),
            "go": (".go",),
            "rust": (".rs",),
            "java": (".java",),
            "php": (".php",),
            "ruby": (".rb",),
            "dart": (".dart",),
            "dotnet": (".cs", ".fs"),
            "elixir": (".ex", ".exs"),
            "kotlin": (".kt", ".kts"),
            "scala": (".scala",),
            "terraform": (".tf",),
            "cpp": (".c", ".cc", ".cpp", ".cxx", ".hpp", ".h"),
        }.get(stack, ())
        return sum(1 for path in flatten_file_tree(file_tree) if path.endswith(extensions))

    def _score_to_confidence(self, score: int) -> ConfidenceLevel:
        if score >= 6:
            return "high"
        if score >= 3:
            return "medium"
        return "low"

    def _unique(self, values: Iterable[str]) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for value in values:
            if value not in seen:
                seen.add(value)
                ordered.append(value)
        return ordered

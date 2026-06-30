from __future__ import annotations

import re
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
    "Spring MVC",
    "Spring WebFlux",
    "Micronaut",
    "Vert.x",
    "Quarkus",
    "Jakarta EE",   # JAX-RS / Jakarta REST — pure JAX-RS projects must not fall to "unknown"
    "Laravel",
    "Symfony",
    "Ktor",
    "Play",
}
_WEB_FRAMEWORKS = {"Next.js", "React", "Vue", "Svelte", "Vite", "Flutter", "Phoenix", "Angular"}
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
        primary_assigned = False
        for stack in enriched:
            is_primary = stack.stack == primary_stack and not primary_assigned
            if is_primary:
                primary_assigned = True
            final_stacks.append(replace(stack, primary=is_primary))
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

        # BUG #4 (JobRunr field test): a framework present only in a small optional
        # integration/adapter submodule must NOT label the whole repo as that
        # framework's app type. JobRunr is a framework-agnostic background-job
        # LIBRARY whose `core` module holds ~85% of the code; Quarkus/Micronaut/Spring
        # appear only in tiny per-framework adapter modules — yet presence-based
        # classification returned project_type="api"+Quarkus. We weight by code
        # locality: if the DOMINANT source module (the one with the most source files)
        # contains no evidence of the app-defining framework, the framework is an
        # optional adapter and the repo is a library. A monolithic Spring app is
        # unaffected — its dominant module *does* use the framework. Drop such
        # localized frameworks from the set that drives the app-type decision below.
        _app_frameworks = framework_names & (_WEB_FRAMEWORKS | _API_FRAMEWORKS)
        _localized = self._localized_adapter_frameworks(file_tree, stacks, _app_frameworks)
        framework_names = framework_names - _localized

        if len(stack_names) >= 2 and self._is_fullstack(stacks):
            return "fullstack"

        if "src/lib.rs" in flat_paths and not any(path.endswith("main.rs") for path in flat_paths):
            return "library"

        # Angular SPA: angular.json is the canonical signal; framework detection is secondary
        if (
            "angular.json" in flat_paths
            or "Angular" in framework_names
        ):
            return "angular-spa"

        if framework_names & _WEB_FRAMEWORKS or any(
            path.startswith(("app/", "pages/", "components/")) for path in flat_paths
        ):
            return "webapp"

        _SERVERSIDE_TEMPLATE_FRAMEWORKS = frozenset({"Thymeleaf", "FreeMarker"})
        if framework_names & _SERVERSIDE_TEMPLATE_FRAMEWORKS:
            return "web_mvc"

        if framework_names & _API_FRAMEWORKS:
            return "api"

        # All app-defining frameworks were localized to optional adapter submodules
        # (multi-module library with per-framework integrations) — report library,
        # never "unknown", when there is clearly source code present.
        if _localized and not (framework_names & (_WEB_FRAMEWORKS | _API_FRAMEWORKS)):
            return "library"

        # Strong CLI signals: a CLI framework or an explicit cli entry point.
        if framework_names & _CLI_FRAMEWORKS or any(
            entry.kind == "cli" for entry in entry_points
        ):
            return "cli"

        if stack_names:
            single = next(iter(stack_names))
            if single in _API_STACKS and framework_names & _API_FRAMEWORKS:
                return "api"
            if single in {"cpp", "dotnet"} and any(entry.kind == "cli" for entry in entry_points):
                return "cli"

        # BUG #4 (JobRunr field test): a multi-module JVM repo with no app-defining
        # web/API framework is a library/toolkit, not an "unknown" — never let the
        # first command of an audit emit a vacuous classification for a clearly
        # structured codebase (e.g. JobRunr: core + per-framework adapter modules).
        # This is checked BEFORE the weak `bin/`-directory CLI heuristic so a build
        # output / wrapper `bin/` dir does not mislabel a library as a CLI.
        if stack_names & {"java", "kotlin", "scala"} and self._is_multi_module(file_tree):
            return "library"

        # Weak CLI heuristic: a top-level bin/ directory (only when nothing stronger).
        if any(path.startswith("bin/") for path in flat_paths):
            return "cli"

        return "unknown" if stacks else None

    def _is_multi_module(self, file_tree: dict[str, Any]) -> bool:
        """True when the repo has >1 source module (distinct `*/src/...` roots)."""
        _CODE_EXTS = (".java", ".kt", ".kts", ".scala", ".groovy")
        modules = {
            self._module_of(p)
            for p in flatten_file_tree(file_tree)
            if p.endswith(_CODE_EXTS)
        }
        modules.discard("")
        return len(modules) >= 2

    @staticmethod
    def _module_of(path: str) -> str:
        """Group a source path into its module root.

        For Maven/Gradle layouts the module is everything before `/src/`
        (e.g. `framework-support/jobrunr-quarkus/src/main/java/...` →
        `framework-support/jobrunr-quarkus`). Otherwise the top-level directory.
        """
        norm = path.replace("\\", "/")
        idx = norm.find("/src/")
        if idx > 0:
            return norm[:idx]
        head, _, tail = norm.partition("/")
        return head if tail else ""

    _EVIDENCE_PATH_RE = re.compile(r"\(([^()]+)\)\s*$")

    def _localized_adapter_frameworks(
        self,
        file_tree: dict[str, Any],
        stacks: Sequence[StackDetection],
        candidate_frameworks: set[str],
    ) -> set[str]:
        """Frameworks confined to a minority module while a framework-agnostic
        module dominates the codebase (library + per-framework adapters).

        Returns the subset of ``candidate_frameworks`` that should NOT drive the
        project-type decision. A framework qualifies only when (a) the repo is
        multi-module, (b) the framework's evidence files are all outside the
        dominant source module, and (c) the framework has located evidence files
        (a manifest-only/root detection applies repo-wide and never localizes).
        """
        if not candidate_frameworks:
            return set()

        _CODE_EXTS = (".java", ".kt", ".kts", ".scala", ".groovy")
        module_file_counts: dict[str, int] = {}
        for p in flatten_file_tree(file_tree):
            if not p.endswith(_CODE_EXTS):
                continue
            mod = self._module_of(p)
            module_file_counts[mod] = module_file_counts.get(mod, 0) + 1

        # Need a genuine multi-module repo to reason about locality.
        if len(module_file_counts) < 2:
            return set()
        dominant_module = max(module_file_counts, key=lambda m: module_file_counts[m])

        # Collect evidence file paths per framework from detected_via.
        evidence: dict[str, set[str]] = {}
        for stack in stacks:
            for fw in stack.frameworks:
                if fw.name not in candidate_frameworks:
                    continue
                paths = evidence.setdefault(fw.name, set())
                for ev in fw.detected_via:
                    if ev.startswith("manifest:"):
                        continue
                    m = self._EVIDENCE_PATH_RE.search(ev)
                    if m:
                        paths.add(m.group(1).strip())

        localized: set[str] = set()
        for fw_name in candidate_frameworks:
            files = evidence.get(fw_name) or set()
            if not files:
                # No locatable evidence (manifest-only) → applies repo-wide.
                continue
            modules = {self._module_of(f) for f in files}
            if dominant_module not in modules:
                localized.add(fw_name)
        return localized

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
            # Backend server-side stacks with manifest evidence outrank frontend stacks
            # in fullstack projects (e.g. Java+Spring wins over nodejs+Angular).
            if (project_type in {"fullstack", "api"}
                    and stack.stack in _API_STACKS
                    and stack.manifests):
                priority = 4
            elif (project_type in {"webapp", "fullstack"} and stack.stack in {"nodejs", "elixir"}
                  or project_type == "api" and stack.stack in _API_STACKS
                  or project_type == "cli" and any(
                      framework.name in _CLI_FRAMEWORKS for framework in stack.frameworks
                  )
                  or project_type == "cli" and stack.stack in {"cpp", "dotnet"}):
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

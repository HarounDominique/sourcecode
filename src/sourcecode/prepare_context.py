"""prepare_context.py — Task-aware context compiler for AI coding agents.

Each task produces a focused context bundle:
  - goal: what the agent should accomplish
  - project_summary: value-oriented product description
  - architecture_summary: flow description (entry → processing → output)
  - relevant_files: ranked by relevance with why_these_files rationale
  - key_dependencies: runtime-first, role-tagged
  - confidence: detection quality indicator
  - gaps: what's uncertain or not analyzed
  - llm_prompt: ready-to-use prompt (optional, --llm-prompt)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


# ─────────────────────────────────────────────────────────────────────────────
# Prompt templates
# ─────────────────────────────────────────────────────────────────────────────

_EXPLAIN_PROMPT = """\
You are an expert software engineer. Your task is to explain this project clearly.

## Project Summary

{project_summary}

{architecture_section}

## Key Files

{relevant_files_section}

{dependencies_section}

## Instructions

1. Summarize what this project does and who it is for.
2. Describe the main components and how they interact.
3. Identify the primary entry point and the main execution flow.
4. Highlight any non-obvious design decisions or constraints.
"""

_FIX_BUG_PROMPT = """\
You are an expert debugger. Your task is to identify and fix a bug in this codebase.

## Project Summary

{project_summary}

## Most Relevant Files

{relevant_files_section}

{suspected_areas_section}

{code_notes_section}

## Instructions

1. Review the relevant files listed above, paying close attention to suspected areas.
2. Identify the root cause of the bug.
3. Propose a minimal, targeted fix with a concrete code patch.
4. Explain why the fix is correct and what side effects to watch for.
"""

_REFACTOR_PROMPT = """\
You are an expert software engineer focused on code quality. \
Your task is to propose refactoring improvements.

## Project Summary

{project_summary}

{architecture_section}

## Files to Refactor

{relevant_files_section}

{improvement_opportunities_section}

## Instructions

1. Identify the top 3–5 most impactful refactoring opportunities.
2. For each, describe the current problem, the proposed change, and the expected benefit.
3. Prioritize changes that reduce complexity or improve testability.
4. Do not suggest changes that break public APIs without noting the impact.
"""

_GENERATE_TESTS_PROMPT = """\
You are an expert in software testing. Your task is to write tests for \
untested or undertested areas of this codebase.

## Project Summary

{project_summary}

## Files Needing Tests

{relevant_files_section}

{test_gaps_section}

{dependencies_section}

## Instructions

1. Write unit tests for the most critical untested functions and classes.
2. Cover edge cases and error paths, not just the happy path.
3. Use the same testing framework already present in the project.
4. Each test must have a clear name that describes exactly what it verifies.
"""

_ONBOARD_PROMPT = """\
You are an expert software engineer onboarding to a new codebase. \
Your task is to understand and explain this project thoroughly.

## Project

{project_summary}

{architecture_section}

## Entry Points

{relevant_files_section}

{dependencies_section}

## Instructions

1. Describe what this project does and the problem it solves.
2. Walk through the main execution flow from entry point to output.
3. Identify the 3-5 most important files to understand first.
4. Note any non-obvious conventions, constraints, or design decisions.
5. List what you would need to know to safely modify this codebase.
"""

_REVIEW_PR_PROMPT = """\
You are an expert code reviewer. Your task is to review the changes \
in this pull request within the context of the full project.

## Project Context

{project_summary}

{architecture_section}

## Changed Files

{relevant_files_section}

{suspected_areas_section}

## Instructions

1. Review the changed files for correctness, security, and maintainability.
2. Check that changes are consistent with the project's architecture.
3. Identify any missing tests, edge cases, or error handling.
4. Flag any breaking changes to public APIs or contracts.
5. Suggest concrete improvements with specific file and line references.
"""

_DELTA_PROMPT = """\
You are an expert software engineer reviewing incremental changes to a codebase.

## Project Context

{project_summary}

## Changed Files

{relevant_files_section}

{suspected_areas_section}

## Instructions

1. Analyze the changed files and their relationship to the project architecture.
2. Identify which entry points and components are affected.
3. Assess the risk and impact of these changes.
4. Flag any consistency issues with the rest of the codebase.
"""


# ─────────────────────────────────────────────────────────────────────────────
# Task registry
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TaskSpec:
    name: str
    goal: str
    description: str
    ranking_boosts: list[str]
    ranking_penalties: list[str]
    enable_code_notes: bool
    enable_dependencies: bool
    prompt_template: str
    output_hint: str


TASKS: dict[str, TaskSpec] = {
    "explain": TaskSpec(
        name="explain",
        goal="Generate a comprehensive project summary for onboarding an LLM or developer.",
        description="Analyze project structure, entry points, and key dependencies.",
        ranking_boosts=["main", "cli", "app", "core", "index", "readme", "schema", "config"],
        ranking_penalties=["test_", "spec_", ".min.", "__pycache__", "docs/"],
        enable_code_notes=False,
        enable_dependencies=True,
        prompt_template=_EXPLAIN_PROMPT,
        output_hint="project_summary, architecture_summary, relevant_files, key_dependencies",
    ),
    "fix-bug": TaskSpec(
        name="fix-bug",
        goal="Identify the most likely files and areas where a bug may be located.",
        description="Rank files by annotation density, surface TODOs/FIXMEs/BUGs.",
        ranking_boosts=["handler", "service", "middleware", "router", "controller",
                        "processor", "parser", "validator"],
        ranking_penalties=["test_", "spec_", ".min.", "__pycache__", "docs/"],
        enable_code_notes=True,
        enable_dependencies=False,
        prompt_template=_FIX_BUG_PROMPT,
        output_hint="relevant_files (ranked by risk), suspected_areas, code_notes_summary",
    ),
    "refactor": TaskSpec(
        name="refactor",
        goal="Highlight structural issues and improvement opportunities across the codebase.",
        description="Surface large files, high-annotation areas, and architectural patterns.",
        ranking_boosts=["core", "utils", "helper", "base", "common", "shared", "lib"],
        ranking_penalties=["test_", ".min.", "__pycache__"],
        enable_code_notes=True,
        enable_dependencies=False,
        prompt_template=_REFACTOR_PROMPT,
        output_hint="relevant_files, improvement_opportunities, architecture_summary",
    ),
    "generate-tests": TaskSpec(
        name="generate-tests",
        goal="Identify untested source files and generate targeted test stubs.",
        description="Find source files without matching test files and rank by complexity.",
        ranking_boosts=["service", "handler", "controller", "router", "parser",
                        "validator", "processor"],
        ranking_penalties=[".min.", "__pycache__", "docs/"],
        enable_code_notes=False,
        enable_dependencies=True,
        prompt_template=_GENERATE_TESTS_PROMPT,
        output_hint="test_gaps, relevant_files (source without tests), key_dependencies",
    ),
    "onboard": TaskSpec(
        name="onboard",
        goal="Build complete project understanding for a new agent or developer joining the codebase.",
        description="Full structural context: entry points, architecture, key files, dependencies.",
        ranking_boosts=["main", "cli", "app", "core", "index", "schema", "config", "readme"],
        ranking_penalties=[".min.", "__pycache__"],
        enable_code_notes=False,
        enable_dependencies=True,
        prompt_template=_ONBOARD_PROMPT,
        output_hint="project_summary, architecture_summary, relevant_files, key_dependencies, confidence, gaps",
    ),
    "review-pr": TaskSpec(
        name="review-pr",
        goal="Review pull request changes in the context of the full project architecture.",
        description="Surface changed files, potential regressions, and architectural consistency.",
        ranking_boosts=["handler", "service", "middleware", "router", "controller",
                        "api", "schema", "model", "validator"],
        ranking_penalties=[".min.", "__pycache__"],
        enable_code_notes=True,
        enable_dependencies=False,
        prompt_template=_REVIEW_PR_PROMPT,
        output_hint="relevant_files, suspected_areas, architecture_summary, code_notes_summary",
    ),
    "delta": TaskSpec(
        name="delta",
        goal="Produce incremental context for changed files — avoids re-reading the full repo.",
        description="Git-aware context: changed files, affected entry points, dependency impact.",
        ranking_boosts=["handler", "service", "middleware", "router", "controller",
                        "api", "schema", "model"],
        ranking_penalties=[".min.", "__pycache__"],
        enable_code_notes=True,
        enable_dependencies=False,
        prompt_template=_DELTA_PROMPT,
        output_hint="changed_files, affected_entry_points, dependency_impact, architecture_summary",
    ),
}


# ─────────────────────────────────────────────────────────────────────────────
# Output schema
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RelevantFile:
    path: str
    role: str    # entrypoint | source | test
    score: float
    reason: str
    why: str = ""  # why this file matters for the specific task


@dataclass
class TaskOutput:
    task: str
    goal: str
    project_summary: Optional[str]
    architecture_summary: Optional[str]
    relevant_files: list[RelevantFile]
    suspected_areas: list[str]
    improvement_opportunities: list[str]
    test_gaps: list[str]
    key_dependencies: list[dict[str, Any]]
    code_notes_summary: Optional[dict[str, Any]]
    limitations: list[str]
    confidence: str = "medium"       # overall detection confidence
    gaps: list[str] = field(default_factory=list)  # analysis gaps
    why_these_files: dict[str, str] = field(default_factory=dict)  # path → why relevant
    changed_files: list[str] = field(default_factory=list)         # delta task only
    affected_entry_points: list[str] = field(default_factory=list) # delta task only
    symptom: Optional[str] = None                                  # fix-bug only
    related_notes: list[dict] = field(default_factory=list)        # fix-bug + symptom only


# ─────────────────────────────────────────────────────────────────────────────
# Builder
# ─────────────────────────────────────────────────────────────────────────────

_SOURCE_EXTENSIONS: frozenset[str] = frozenset({
    ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".kt",
    ".go", ".rs", ".rb", ".php", ".cs", ".dart",
})


def _extract_ddd_domain(path: str) -> str:
    """Extract domain name from DDD package path.

    For m3informatica.saint.ddd.{domain}.infrastructure.rest.*RestController
    the domain is the segment just before application/ domain/ or infrastructure/.
    """
    parts = path.replace("\\", "/").split("/")
    _DDD_LAYERS = {"application", "domain", "infrastructure"}
    for i, part in enumerate(parts):
        if part in _DDD_LAYERS and i >= 1:
            return parts[i - 1]
    # Fallback: penultimate directory segment
    if len(parts) >= 2:
        return parts[-2]
    return ""


def _java_why(path: str, file_class: "Optional[object]") -> str:
    """Generate why string for Java files based on stereotype classification."""
    if file_class is None:
        return ""
    from sourcecode.file_classifier import JAVA_STEREOTYPE_CATEGORIES
    category = getattr(file_class, "category", "")
    if category not in JAVA_STEREOTYPE_CATEGORIES:
        return ""
    domain = _extract_ddd_domain(path)
    class_name = Path(path).stem
    if category == "api_endpoint":
        return f"Defines HTTP endpoints for the {domain} domain" if domain else "Defines HTTP API endpoints"
    if category == "business_logic":
        return f"Orchestrates {domain} business logic" if domain else "Business logic service"
    if category == "data_access":
        return f"SQL queries for {domain} data access" if domain else "Data access layer"
    if category == "domain_model":
        return f"JPA entity for {class_name} persistence"
    if category == "configuration":
        return getattr(file_class, "reason", "Spring configuration class")
    if category == "security":
        return getattr(file_class, "reason", "Spring Security configuration")
    if category == "dto":
        return f"Lombok DTO — {class_name}"
    return getattr(file_class, "reason", "")

_ALL_EXTENSIONS: frozenset[str] = _SOURCE_EXTENSIONS | frozenset({
    ".md", ".toml", ".yaml", ".yml", ".json", ".xml",
})


class TaskContextBuilder:
    def __init__(self, root: Path) -> None:
        self.root = root

    def build(self, task_name: str, *, since: Optional[str] = None, symptom: Optional[str] = None) -> TaskOutput:
        if task_name not in TASKS:
            raise ValueError(
                f"Unknown task '{task_name}'. Available: {', '.join(TASKS)}"
            )
        spec = TASKS[task_name]

        # ── 1. Scan ────────────────────────────────────────────────────────
        from sourcecode.adaptive_scanner import AdaptiveScanner
        from sourcecode.repo_classifier import RepoClassifier
        from sourcecode.tree_utils import flatten_file_tree

        _topology = RepoClassifier().classify(self.root)
        # Shallow pre-scan to detect Java manifests before choosing depth.
        from sourcecode.scanner import FileScanner as _FileScanner
        _pre = _FileScanner(self.root, max_depth=1)
        _pre_manifests = _pre.find_manifests()
        _java_names = {"pom.xml", "build.gradle", "build.gradle.kts"}
        _is_java = any(Path(m).name in _java_names for m in _pre_manifests)
        _base_depth = 12 if _is_java else 6
        scanner = AdaptiveScanner(self.root, topology=_topology, base_depth=_base_depth)
        file_tree = scanner.scan_tree()
        manifests = scanner.find_manifests()
        all_paths = [p.replace("\\", "/") for p in flatten_file_tree(file_tree)]

        # Warn when Java project has no Mapper.xml — suggests files below scan depth.
        _mybatis_warning: dict | None = None
        if _is_java and not any(p.endswith("Mapper.xml") for p in all_paths):
            _mybatis_warning = {
                "area": "mybatis",
                "reason": "Mapper XML files may exist below scan depth. Re-run with --depth 12.",
                "impact": "high",
            }

        # ── 2. Detect stacks + entry points ───────────────────────────────
        from dataclasses import replace as _replace
        from sourcecode.detectors import ProjectDetector, build_default_detectors
        from sourcecode.workspace import WorkspaceAnalyzer

        detector = ProjectDetector(build_default_detectors())
        workspace_analysis = WorkspaceAnalyzer().analyze(self.root, manifests)

        _root_manifests = [
            m for m in manifests
            if Path(m).resolve().parent == self.root
        ]
        _detection_manifests = _root_manifests if workspace_analysis.workspaces else manifests
        if workspace_analysis.is_monorepo and not _root_manifests:
            from sourcecode.schema import EntryPoint, StackDetection
            stacks: list[StackDetection] = []
            entry_points: list[EntryPoint] = []
        else:
            stacks, entry_points, _ = detector.detect(self.root, file_tree, _detection_manifests)

        # Iterate workspaces to collect per-workspace stacks and entry points —
        # same approach as the main CLI (cli.py lines 971-1041).
        for workspace in workspace_analysis.workspaces:
            ws_root = self.root / workspace.path
            if not ws_root.exists() or not ws_root.is_dir():
                continue
            _ws_topology = RepoClassifier().classify(ws_root)
            _ws_scanner = AdaptiveScanner(ws_root, topology=_ws_topology, base_depth=6)
            _ws_tree = _ws_scanner.scan_tree()
            _ws_manifests = _ws_scanner.find_manifests()
            _ws_stacks, _ws_eps, _ = detector.detect(ws_root, _ws_tree, _ws_manifests)
            stacks.extend(
                _replace(s, root=workspace.path, workspace=workspace.path, primary=False)
                for s in _ws_stacks
            )
            entry_points.extend(
                _replace(ep, path=f"{workspace.path}/{ep.path}")
                for ep in _ws_eps
            )

        stacks, project_type = detector.classify_results(
            file_tree, stacks, entry_points,
            project_type_override="monorepo" if workspace_analysis.is_monorepo else None,
        )

        # ── 3. Summarize ───────────────────────────────────────────────────
        from sourcecode.schema import AnalysisMetadata, SourceMap
        from sourcecode.summarizer import ProjectSummarizer
        from sourcecode.architecture_summary import ArchitectureSummarizer

        sm = SourceMap(
            metadata=AnalysisMetadata(analyzed_path=str(self.root)),
            file_tree=file_tree,
            stacks=stacks,
            project_type=project_type,
            entry_points=entry_points,
        )
        sm.file_paths = all_paths

        # Classify workspace packages for structural context
        if workspace_analysis.workspaces:
            from sourcecode.runtime_classifier import RuntimeClassifier
            sm.monorepo_packages = RuntimeClassifier().classify(
                self.root,
                [ws.path for ws in workspace_analysis.workspaces],
            )

        project_summary = ProjectSummarizer(self.root).generate(sm)
        architecture_summary = ArchitectureSummarizer(self.root).generate(sm)

        from sourcecode.context_summarizer import ContextSummarizer
        sm.context_summary = ContextSummarizer(self.root).generate(sm)

        # ── 4. Dependencies ────────────────────────────────────────────────
        key_dependencies: list[dict[str, Any]] = []
        limitations: list[str] = []

        if spec.enable_dependencies:
            from dataclasses import asdict
            from sourcecode.dependency_analyzer import DependencyAnalyzer

            dep_records, dep_summary = DependencyAnalyzer().analyze(self.root)
            primary_eco = stacks[0].stack if stacks else ""
            direct = [
                d for d in dep_records
                if d.scope != "transitive" and d.source in {"manifest", "lockfile"}
                and (d.role or "unknown") in {"runtime", "parsing", "serialization", "observability", "infra"}
                and d.scope not in {"dev"}
            ]
            direct.sort(key=lambda d: (0 if d.ecosystem == primary_eco else 1, d.name.lower()))
            _SKIP_DEP_KEYS = {"parent", "workspace", "resolved_version", "manifest_path"}
            key_dependencies = [
                {k: v for k, v in asdict(d).items() if v is not None and k not in _SKIP_DEP_KEYS}
                for d in direct[:15]
            ]
            limitations.extend(dep_summary.limitations)

        # ── 5. Code notes ──────────────────────────────────────────────────
        code_notes_summary: Optional[dict[str, Any]] = None
        suspected_areas: list[str] = []
        improvement_opportunities: list[str] = []
        cn_notes_for_ranking: list = []

        if spec.enable_code_notes:
            from dataclasses import asdict
            from sourcecode.code_notes_analyzer import CodeNotesAnalyzer

            cn_notes, _cn_adrs, cn_summary = CodeNotesAnalyzer().analyze(self.root)
            code_notes_summary = asdict(cn_summary)
            cn_notes_for_ranking = cn_notes

            if task_name == "fix-bug":
                bug_kinds = {"FIXME", "BUG", "HACK", "XXX"}
                counts: dict[str, int] = {}
                for note in cn_notes:
                    if note.kind in bug_kinds:
                        counts[note.path] = counts.get(note.path, 0) + 1
                suspected_areas = [
                    f"{p} ({n} annotation{'s' if n > 1 else ''})"
                    for p, n in sorted(counts.items(), key=lambda x: -x[1])[:8]
                ]

            elif task_name == "refactor":
                ref_kinds = {"TODO", "DEPRECATED", "OPTIMIZE", "HACK"}
                counts2: dict[str, int] = {}
                for note in cn_notes:
                    if note.kind in ref_kinds:
                        counts2[note.path] = counts2.get(note.path, 0) + 1
                improvement_opportunities = [
                    f"{p}: {n} refactoring annotation{'s' if n > 1 else ''}"
                    for p, n in sorted(counts2.items(), key=lambda x: -x[1])[:8]
                ]
                # Fallback: derive from file size when no code annotations exist
                if not improvement_opportunities:
                    _large: list[tuple[int, str]] = []
                    for _p in all_paths:
                        if not self._is_source(_p) or self._is_test(_p):
                            continue
                        try:
                            _loc = len((self.root / _p).read_text(errors="replace").splitlines())
                            if _loc > 200:
                                _large.append((_loc, _p))
                        except OSError:
                            pass
                    _large.sort(reverse=True)
                    improvement_opportunities = [
                        f"{p}: {loc} lines — candidate for decomposition"
                        for loc, p in _large[:8]
                    ]

        # ── 5b. Git signals for ranking ────────────────────────────────────
        git_hotspots: dict[str, int] = {}
        uncommitted_files: set[str] = set()
        try:
            from sourcecode.git_analyzer import GitAnalyzer
            _gc = GitAnalyzer().analyze(self.root, depth=30, days=90)
            _bad = {"no_git_repo", "git_not_found", "git_timeout"}
            if _gc and not (_bad & set(_gc.limitations)):
                git_hotspots = {h.file: h.commit_count for h in _gc.change_hotspots}
                if _gc.uncommitted_changes:
                    _uc = _gc.uncommitted_changes
                    uncommitted_files = set(_uc.staged) | set(_uc.unstaged)
        except Exception:
            pass

        # ── 5c. Delta: resolve git-changed files BEFORE ranking ───────────────
        # For delta task, relevant_files must rank only files changed in the
        # specified git range, not the full repo by generic entrypoint scoring.
        _delta_files: Optional[set[str]] = None
        if task_name == "delta":
            _delta_raw = self._get_git_changed_files(since=since)
            if _delta_raw:
                _delta_files = set(_delta_raw)

        # ── 5c. review-pr suspected_areas (needs git uncommitted_files) ──────
        if task_name == "review-pr" and spec.enable_code_notes:
            pr_areas: dict[str, int] = {}
            _all_paths_set = set(all_paths)
            for path in uncommitted_files:
                # Only count uncommitted files that belong to the scanned root —
                # git status may return repo-level paths from a parent directory.
                if path in _all_paths_set:
                    pr_areas[path] = pr_areas.get(path, 0) + 10
            review_kinds = {"FIXME", "TODO", "BUG", "HACK"}
            for note in cn_notes_for_ranking:
                if note.kind in review_kinds:
                    pr_areas[note.path] = pr_areas.get(note.path, 0) + 1
            _BOOST_STEMS = ("Controller", "Service", "Repository", "Mapper", "Filter", "Security")
            for path in all_paths:
                stem = Path(path).stem
                if any(k in stem for k in _BOOST_STEMS):
                    pr_areas[path] = pr_areas.get(path, 0) + 2
            suspected_areas = [
                p for p, _ in sorted(pr_areas.items(), key=lambda x: -x[1])[:8]
                if not self._is_test(p)
            ]

        # ── 6. Rank files ──────────────────────────────────────────────────
        entry_set = {ep.path for ep in entry_points}
        test_set = {p for p in all_paths if self._is_test(p)}
        source_set = {p for p in all_paths if not self._is_test(p) and self._is_source(p)}

        relevant_files = self._rank_files(
            task_name, spec, all_paths, entry_set, test_set,
            monorepo_packages=sm.monorepo_packages if sm.monorepo_packages else None,
            git_hotspots=git_hotspots,
            uncommitted_files=uncommitted_files,
            code_notes=cn_notes_for_ranking if cn_notes_for_ranking else None,
            delta_files=_delta_files,
        )

        # ── 6b. Symptom keyword boost + related notes (fix-bug + --symptom) ──
        symptom_keywords: list[str] = []
        related_notes: list[dict] = []
        if task_name == "fix-bug" and symptom:
            import re as _re
            symptom_keywords = [
                w.lower() for w in _re.split(r"[\s\W]+", symptom)
                if len(w) > 2
            ]
            if symptom_keywords:
                # Surface code notes whose text contains any keyword
                for _n in cn_notes_for_ranking:
                    _text = (getattr(_n, "text", "") or "").lower()
                    if any(kw in _text for kw in symptom_keywords):
                        related_notes.append({
                            "kind": getattr(_n, "kind", ""),
                            "path": getattr(_n, "path", ""),
                            "line": getattr(_n, "line", None),
                            "text": getattr(_n, "text", ""),
                        })
                # Secondary pass: inject files whose path matches symptom keywords
                # but weren't in the candidate pool (no structural/git signals).
                _existing_paths = {rf.path for rf in relevant_files}
                for _p in all_paths:
                    if _p in _existing_paths:
                        continue
                    if Path(_p).suffix.lower() not in _ALL_EXTENSIONS:
                        continue
                    _p_lower = _p.lower()
                    _matching_kws = [kw for kw in symptom_keywords if kw in _p_lower]
                    if not _matching_kws:
                        continue
                    _boost = 0.2 * len(_matching_kws)
                    _injected_score = round(min(0.5 + _boost, 1.0), 2)
                    _first_kw = _matching_kws[0]
                    relevant_files.append(RelevantFile(
                        path=_p,
                        role="symptom_match",
                        score=_injected_score,
                        reason=f"path matches symptom keyword: {_first_kw}",
                        why=f"symptom injection: {', '.join(_matching_kws)}",
                    ))
                    _existing_paths.add(_p)

                # Re-rank all relevant_files: boost files whose path matches keywords
                def _symptom_score(rf: "RelevantFile") -> float:
                    path_lower = rf.path.lower()
                    return rf.score + 0.2 * sum(1.0 for kw in symptom_keywords if kw in path_lower)
                relevant_files = sorted(relevant_files, key=lambda rf: -_symptom_score(rf))

        # ── 7. Test gaps (generate-tests only) ────────────────────────────
        test_gaps: list[str] = []
        if task_name == "generate-tests":
            def _normalize_test_stem(stem: str) -> str:
                # Java: FooTest / FooTests → Foo; TestFoo → Foo
                if stem.endswith("Tests"):
                    return stem[:-5]
                if stem.endswith("Test"):
                    return stem[:-4]
                if stem.startswith("Test") and len(stem) > 4 and stem[4].isupper():
                    return stem[4:]
                # Python/JS: test_foo / foo_test
                return stem.removeprefix("test_").removesuffix("_test")

            test_stems = {_normalize_test_stem(Path(p).stem) for p in test_set}
            untested = [
                p for p in source_set
                if Path(p).stem not in test_stems
                and not any(pen in p for pen in spec.ranking_penalties)
            ]
            untested.sort(key=lambda p: (len(p.split("/")), p))
            test_gaps = untested[:15]

        # ── 8. Confidence + gaps ──────────────────────────────────────────────
        from sourcecode.confidence_analyzer import ConfidenceAnalyzer
        from dataclasses import asdict as _asdict

        sm_for_conf = SourceMap(
            metadata=AnalysisMetadata(analyzed_path=str(self.root)),
            file_tree=file_tree,
            stacks=stacks,
            project_type=project_type,
            entry_points=entry_points,
        )
        sm_for_conf.file_paths = all_paths
        if spec.enable_dependencies and key_dependencies:
            from sourcecode.schema import DependencySummary
            sm_for_conf.dependency_summary = DependencySummary(
                requested=True,
                total_count=len(key_dependencies),
            )

        conf_summary, analysis_gaps = ConfidenceAnalyzer().analyze(sm_for_conf)
        confidence = conf_summary.overall
        gaps = [g.reason for g in analysis_gaps]
        if _mybatis_warning:
            gaps.append(_mybatis_warning["reason"])

        # ── 9. why_these_files ────────────────────────────────────────────────
        why_these_files: dict[str, str] = {
            rf.path: rf.reason for rf in relevant_files
        }

        # ── 10. Delta: git changed files (reuse pre-computed set from step 5c) ──
        changed_files: list[str] = []
        affected_entry_points: list[str] = []
        if task_name == "delta":
            changed_files = sorted(_delta_files) if _delta_files else self._get_git_changed_files(since=since)
            ep_set = {ep.path for ep in entry_points}
            affected_entry_points = [f for f in changed_files if f in ep_set]

        return TaskOutput(
            task=task_name,
            goal=spec.goal,
            project_summary=project_summary,
            architecture_summary=architecture_summary,
            relevant_files=relevant_files,
            suspected_areas=suspected_areas,
            improvement_opportunities=improvement_opportunities,
            test_gaps=test_gaps,
            key_dependencies=key_dependencies,
            code_notes_summary=code_notes_summary,
            limitations=limitations,
            confidence=confidence,
            gaps=gaps,
            why_these_files=why_these_files,
            changed_files=changed_files,
            affected_entry_points=affected_entry_points,
            symptom=symptom if task_name == "fix-bug" and symptom else None,
            related_notes=related_notes,
        )

    def render_prompt(self, output: TaskOutput) -> str:
        spec = TASKS[output.task]

        def _section(title: str, body: str) -> str:
            return f"## {title}\n\n{body}" if body.strip() else ""

        project_summary = output.project_summary or "No project summary available."

        architecture_section = _section(
            "Architecture", output.architecture_summary or ""
        )

        relevant_files_section = "\n".join(
            f"- `{f.path}` [{f.role}] — {f.reason}"
            for f in output.relevant_files
        ) or "No relevant files identified."

        deps_lines = [
            f"- {d['name']} "
            f"{d.get('declared_version') or d.get('resolved_version') or '(unknown)'}"
            for d in output.key_dependencies[:10]
        ]
        dependencies_section = _section("Key Dependencies", "\n".join(deps_lines))

        suspected_areas_section = _section(
            "Suspected Problem Areas",
            "\n".join(f"- {a}" for a in output.suspected_areas),
        )

        code_notes_section = ""
        if output.code_notes_summary and output.code_notes_summary.get("total", 0) > 0:
            by_kind = output.code_notes_summary.get("by_kind", {})
            kinds_str = ", ".join(f"{k}: {v}" for k, v in by_kind.items() if v > 0)
            top_files = ", ".join(output.code_notes_summary.get("top_files", [])[:5])
            code_notes_section = _section(
                "Code Annotations",
                f"Total: {output.code_notes_summary['total']} ({kinds_str})\n"
                f"Most annotated: {top_files}",
            )

        improvement_opportunities_section = _section(
            "Improvement Opportunities",
            "\n".join(f"- {o}" for o in output.improvement_opportunities),
        )

        test_gaps_section = _section(
            "Untested Files",
            "\n".join(f"- `{p}`" for p in output.test_gaps),
        )

        format_kwargs: dict[str, str] = {
            "project_summary": project_summary,
            "architecture_section": architecture_section,
            "relevant_files_section": relevant_files_section,
            "dependencies_section": dependencies_section,
            "suspected_areas_section": suspected_areas_section,
            "code_notes_section": code_notes_section,
            "improvement_opportunities_section": improvement_opportunities_section,
            "test_gaps_section": test_gaps_section,
        }
        # Only pass keys that the template actually uses
        import re as _re
        used_keys = set(_re.findall(r"\{(\w+)\}", spec.prompt_template))
        filtered_kwargs = {k: v for k, v in format_kwargs.items() if k in used_keys}
        return spec.prompt_template.format(**filtered_kwargs).strip()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _rank_files(
        self,
        task_name: str,
        spec: TaskSpec,
        all_paths: list[str],
        entry_set: set[str],
        test_set: set[str],
        monorepo_packages: Optional[list] = None,
        git_hotspots: Optional[dict[str, int]] = None,
        uncommitted_files: Optional[set[str]] = None,
        code_notes: Optional[list] = None,
        delta_files: Optional[set[str]] = None,
    ) -> list[RelevantFile]:
        from sourcecode.ranking_engine import RankingEngine
        from sourcecode.file_classifier import FileClassifier

        engine = RankingEngine(monorepo_packages or [])
        file_classifier = FileClassifier(self.root, [], monorepo_packages or [])

        # Auxiliary entry points (benchmark, docs, examples) are not runtime
        runtime_entry_set = {ep for ep in entry_set if not engine.is_auxiliary(ep)}

        _hotspots = git_hotspots or {}
        _uncommitted = uncommitted_files or set()
        _max_churn = max(_hotspots.values(), default=1)

        # Pre-compute fix-bug signals (used only when task_name == "fix-bug")
        _annotated_files: set[str] = set()
        _dominant_stack = ""
        _recently_changed_stacks: set[str] = set()
        if task_name == "fix-bug":
            _bug_kinds = {"FIXME", "BUG"}
            for _n in (code_notes or []):
                if getattr(_n, "kind", "").upper() in _bug_kinds:
                    _annotated_files.add(getattr(_n, "path", ""))

            def _file_stack(p: str) -> str:
                ext = Path(p).suffix.lower()
                if ext == ".java": return "java"
                if ext in (".ts", ".tsx", ".js", ".jsx", ".mjs"): return "typescript"
                if ext == ".py": return "python"
                if ext == ".go": return "go"
                if ext in (".kt", ".kts"): return "kotlin"
                if ext == ".rs": return "rust"
                if ext == ".rb": return "ruby"
                return "unknown"

            from collections import Counter as _Counter
            _stk_counts: _Counter[str] = _Counter(
                _file_stack(f) for f in _uncommitted if _file_stack(f) != "unknown"
            )
            if _stk_counts:
                _dominant_stack = _stk_counts.most_common(1)[0][0]
                _recently_changed_stacks = set(_stk_counts.keys())

        scored: list[tuple[float, str, RelevantFile]] = []

        # For delta task, score only files changed in the specified git range.
        paths_to_score = [p for p in all_paths if p in delta_files] if delta_files else all_paths

        for path in paths_to_score:
            if Path(path).suffix.lower() not in _ALL_EXTENSIONS:
                continue
            if any(pen in path for pen in spec.ranking_penalties):
                continue
            if engine.is_noise(path):
                continue

            is_test = path in test_set
            if is_test and task_name != "generate-tests":
                continue

            # Structural + git signals from unified engine (task-weighted)
            fs = engine.score(
                path,
                is_entrypoint=(path in runtime_entry_set),
                git_churn=_hotspots.get(path, 0),
                max_churn=_max_churn,
                is_changed=(path in _uncommitted),
                task=task_name,
            )

            if fs.score < -50:  # hard noise
                continue

            # Content classification boost (reads file imports)
            content_boost = 0.0
            content_reasons: list[str] = []
            file_class = file_classifier.classify(path)
            if file_class is not None:
                content_boost = file_class.relevance * 2.0
                content_reasons.append(f"{file_class.category}: {file_class.reason}")

            if is_test:
                content_boost += 2.0
                content_reasons.append("existing test")
            elif self._is_source(path) and not content_reasons:
                content_boost += 0.5

            # Task-specific boosts for differentiated file weighting
            path_lower = path.lower()
            _fix_bug_why = ""
            if task_name == "fix-bug":
                _why_parts: list[str] = []
                if path in _uncommitted:
                    content_boost += 0.40
                    _why_parts.append("uncommitted change (+0.40)")
                _recency = min(0.30, _hotspots.get(path, 0) * 0.05)
                if _recency > 0:
                    content_boost += _recency
                    _why_parts.append(f"recent commits (+{_recency:.2f})")
                if path in _annotated_files:
                    content_boost += 0.20
                    _why_parts.append("FIXME/BUG annotation (+0.20)")
                _file_stk = _file_stack(path)
                if _dominant_stack and _file_stk == _dominant_stack:
                    content_boost += 0.10
                    _why_parts.append("dominant changed stack (+0.10)")
                if _recently_changed_stacks and _file_stk not in _recently_changed_stacks and _file_stk != "unknown":
                    content_boost -= 0.30
                    _why_parts.append("different stack from recent changes (-0.30)")
                if _why_parts:
                    _fix_bug_why = ", ".join(_why_parts)
            elif task_name == "generate-tests":
                stem = Path(path).stem.lower()
                has_test = any(
                    stem in Path(tp).stem.lower() or Path(tp).stem.lower() in stem
                    for tp in test_set
                )
                if not has_test and self._is_source(path):
                    content_boost += 1.0
                    content_reasons.append("no test pair found")
            elif task_name == "onboard":
                if path in runtime_entry_set:
                    content_boost += 2.0
                    content_reasons.append("runtime entry point")
                if any(x in path_lower for x in ("config", "application.yml", "application.properties", "settings", "bootstrap")):
                    content_boost += 1.0
                    content_reasons.append("configuration class")
            elif task_name == "explain":
                if "controller" in path_lower and path in runtime_entry_set:
                    content_boost += 1.5
                    content_reasons.append("DDD module controller")

            total = fs.score + content_boost
            if total <= 0:
                continue

            role = (
                "entrypoint" if path in runtime_entry_set
                else ("test" if is_test else "source")
            )
            all_reasons = [r for r in fs.reasons if r != "source file"] + content_reasons
            reason_str = ", ".join(all_reasons) if all_reasons else "source file"
            why_str = _fix_bug_why if _fix_bug_why else _java_why(path, file_class)

            scored.append((total, path, RelevantFile(
                path=path,
                role=role,
                score=round(min(total / 3.0, 1.0), 2),
                reason=reason_str,
                why=why_str,
            )))

        # Deterministic: score desc, then path asc as tiebreaker
        scored.sort(key=lambda x: (-x[0], x[1]))

        # Apply directory-diversity selection via ContextScorer.
        # Files from the same directory share the same concern; the scorer
        # applies a small redundancy penalty so the final set spans more of
        # the codebase rather than clustering inside a single directory.
        # Falls back to top-15 slice when scorer is unavailable.
        try:
            from sourcecode.context_scorer import ContextScorer, NodeScore
            _ctx = ContextScorer()
            _ns: dict[str, NodeScore] = {
                path: NodeScore(
                    path=path,
                    score=total,
                    display_score=min(total / 3.0, 1.0),
                    structural=total,
                    semantic=0.0,
                    annotation=0.0,
                    proximity=0.0,
                    reasons=[rf.reason] if rf.reason else ["source file"],
                )
                for total, path, rf in scored
            }
            _selected = _ctx.select_subgraph(_ns, contracts=[], budget=15, min_score=0.15)
            _rf_map = {path: rf for _, path, rf in scored}
            return [_rf_map[p] for p in _selected if p in _rf_map]
        except Exception:
            return [f for _, _, f in scored[:15]]

    def _is_test(self, path: str) -> bool:
        name = Path(path).name.lower()
        return (
            name.startswith("test_")
            or name.endswith("_test.py")
            or name.endswith(".test.ts")
            or name.endswith(".spec.ts")
            or "/tests/" in path
            or "/test/" in path
            or "/spec/" in path
        )

    def _is_source(self, path: str) -> bool:
        return Path(path).suffix.lower() in _SOURCE_EXTENSIONS

    def _get_git_changed_files(self, since: Optional[str] = None) -> list[str]:
        """Get files changed since a git ref (default: HEAD~1) relative to self.root.

        Uses --relative so paths are relative to cwd (self.root), not the git repo
        root. This is critical for monorepos where self.root is a subpath of the
        git root and git diff would otherwise return prefixed paths that don't match
        the scanned file tree.
        """
        import subprocess
        ref = since or "HEAD~1"
        try:
            result = subprocess.run(
                ["git", "diff", "--name-only", "--relative", ref, "HEAD"],
                cwd=str(self.root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            if result.returncode == 0:
                return [
                    line.strip() for line in (result.stdout or "").splitlines()
                    if line.strip()
                ]
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        # Fallback: uncommitted changes
        try:
            result = subprocess.run(
                ["git", "diff", "--name-only", "--relative"],
                cwd=str(self.root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            if result.returncode == 0:
                return [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        return []

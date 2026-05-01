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


# ─────────────────────────────────────────────────────────────────────────────
# Builder
# ─────────────────────────────────────────────────────────────────────────────

_SOURCE_EXTENSIONS: frozenset[str] = frozenset({
    ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".kt",
    ".go", ".rs", ".rb", ".php", ".cs", ".dart",
})

_ALL_EXTENSIONS: frozenset[str] = _SOURCE_EXTENSIONS | frozenset({
    ".md", ".toml", ".yaml", ".yml", ".json", ".xml",
})


class TaskContextBuilder:
    def __init__(self, root: Path) -> None:
        self.root = root

    def build(self, task_name: str, *, since: Optional[str] = None) -> TaskOutput:
        if task_name not in TASKS:
            raise ValueError(
                f"Unknown task '{task_name}'. Available: {', '.join(TASKS)}"
            )
        spec = TASKS[task_name]

        # ── 1. Scan ────────────────────────────────────────────────────────
        from sourcecode.scanner import FileScanner
        from sourcecode.tree_utils import flatten_file_tree

        scanner = FileScanner(self.root, max_depth=6)
        file_tree = scanner.scan_tree()
        manifests = scanner.find_manifests()
        all_paths = [p.replace("\\", "/") for p in flatten_file_tree(file_tree)]

        # ── 2. Detect stacks + entry points ───────────────────────────────
        from sourcecode.detectors import ProjectDetector, build_default_detectors
        from sourcecode.workspace import WorkspaceAnalyzer

        detector = ProjectDetector(build_default_detectors())
        workspace_analysis = WorkspaceAnalyzer().analyze(self.root, manifests)
        stacks, entry_points, _ = detector.detect(self.root, file_tree, manifests)
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
            key_dependencies = [asdict(d) for d in direct[:15]]
            limitations.extend(dep_summary.limitations)

        # ── 5. Code notes ──────────────────────────────────────────────────
        code_notes_summary: Optional[dict[str, Any]] = None
        suspected_areas: list[str] = []
        improvement_opportunities: list[str] = []

        if spec.enable_code_notes:
            from dataclasses import asdict
            from sourcecode.code_notes_analyzer import CodeNotesAnalyzer

            cn_notes, _cn_adrs, cn_summary = CodeNotesAnalyzer().analyze(self.root)
            code_notes_summary = asdict(cn_summary)

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

        # ── 6. Rank files ──────────────────────────────────────────────────
        entry_set = {ep.path for ep in entry_points}
        test_set = {p for p in all_paths if self._is_test(p)}
        source_set = {p for p in all_paths if not self._is_test(p) and self._is_source(p)}

        relevant_files = self._rank_files(
            task_name, spec, all_paths, entry_set, test_set,
            monorepo_packages=sm.monorepo_packages if sm.monorepo_packages else None,
            git_hotspots=git_hotspots,
            uncommitted_files=uncommitted_files,
        )

        # ── 7. Test gaps (generate-tests only) ────────────────────────────
        test_gaps: list[str] = []
        if task_name == "generate-tests":
            test_stems = {
                Path(p).stem.removeprefix("test_").removesuffix("_test")
                for p in test_set
            }
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

        # ── 9. why_these_files ────────────────────────────────────────────────
        why_these_files: dict[str, str] = {
            rf.path: rf.reason for rf in relevant_files
        }

        # ── 10. Delta: git changed files ──────────────────────────────────────
        changed_files: list[str] = []
        affected_entry_points: list[str] = []
        if task_name == "delta":
            changed_files = self._get_git_changed_files(since=since)
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
    ) -> list[RelevantFile]:
        from sourcecode.relevance_scorer import RelevanceScorer
        from sourcecode.file_classifier import FileClassifier
        scorer = RelevanceScorer(monorepo_packages or [])
        file_classifier = FileClassifier(self.root, [
            # _rank_files only needs production path evidence; EntryPoint objects
            # are not available here, so category evidence is best-effort below.
        ], monorepo_packages or [])

        # Auxiliary entry points (benchmark, docs, examples) must not get
        # the production entry boost — they are not runtime signals.
        runtime_entry_set = {ep for ep in entry_set if not scorer.is_auxiliary(ep)}

        _hotspots = git_hotspots or {}
        _uncommitted = uncommitted_files or set()
        _max_churn = max(_hotspots.values(), default=1)

        scored: list[tuple[float, RelevantFile]] = []

        for path in all_paths:
            if Path(path).suffix.lower() not in _ALL_EXTENSIONS:
                continue
            if any(pen in path for pen in spec.ranking_penalties):
                continue

            # Hard filter: tooling/config noise
            if scorer.is_noise(path):
                continue

            is_test = path in test_set
            if is_test and task_name != "generate-tests":
                continue

            score = 0.0
            reasons: list[str] = []

            # Only runtime entry points get the production boost
            if path in runtime_entry_set:
                score += 3.0
                reasons.append("entry point")

            file_class = file_classifier.classify(path)
            if file_class is not None:
                score += file_class.relevance * 2.0
                reasons.append(f"{file_class.category}: {file_class.reason}")

            if is_test:
                score += 2.0
                reasons.append("existing test")
            elif self._is_source(path):
                score += 0.5
                if not reasons:
                    reasons.append("source file with supported extension")

            # Operational relevance boost/penalty from package role
            rel = scorer.score(path)
            score += (rel - 0.3) * 2.0  # center around 0.3 baseline

            # Suppress auxiliary dirs (benchmarks, docs, examples, demos)
            if scorer.is_auxiliary(path):
                score -= 2.0

            # Git churn: frequently changed files are high-signal for active work
            churn = _hotspots.get(path, 0)
            if churn > 0:
                score += (churn / _max_churn) * 1.5
                reasons.append(f"git churn ({churn})")

            # Uncommitted changes: files actively being edited rank highest
            if path in _uncommitted:
                score += 1.0
                reasons.append("uncommitted changes")

            if score <= 0:
                continue

            role = (
                "entrypoint" if path in runtime_entry_set
                else ("test" if is_test else "source")
            )
            scored.append((score, RelevantFile(
                path=path,
                role=role,
                score=round(score, 1),
                reason=", ".join(reasons) if reasons else "source file",
            )))

        scored.sort(key=lambda x: -x[0])
        return [f for _, f in scored[:15]]

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
        """Get files changed since a git ref (default: HEAD~1) relative to root."""
        import subprocess
        ref = since or "HEAD~1"
        try:
            result = subprocess.run(
                ["git", "diff", "--name-only", ref, "HEAD"],
                cwd=str(self.root),
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                return [
                    line.strip() for line in result.stdout.splitlines()
                    if line.strip()
                ]
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        # Fallback: uncommitted changes
        try:
            result = subprocess.run(
                ["git", "diff", "--name-only"],
                cwd=str(self.root),
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                return [line.strip() for line in result.stdout.splitlines() if line.strip()]
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        return []

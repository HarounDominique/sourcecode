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
from enum import Enum
from pathlib import Path
from typing import Any, Optional


class DiffSourceType(str, Enum):
    """Explicit diff scope — never auto-merged, never implicit."""
    WORKTREE_UNSTAGED = "WORKTREE_UNSTAGED"   # git diff (no ref)
    WORKTREE_STAGED   = "WORKTREE_STAGED"     # git diff --cached
    GIT_SINCE_REF     = "GIT_SINCE_REF"       # git diff ref HEAD
    GIT_RANGE         = "GIT_RANGE"           # git diff refA refB


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
    symptom_note: Optional[str] = None                             # fix-bug: cross-layer synonym note
    # delta-specific impact fields
    impact_summary: Optional[str] = None
    affected_modules: list[str] = field(default_factory=list)
    risk_areas: list[dict] = field(default_factory=list)
    since: Optional[str] = None
    system_impact: dict = field(default_factory=dict)
    change_type: list[str] = field(default_factory=list)
    dependency_graph_summary: dict = field(default_factory=dict)
    impact_score_per_file: dict = field(default_factory=dict)
    # error state (git ref not found, etc.)
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    error_hints: list[str] = field(default_factory=list)
    # CI decision state machine — machine-decidable signal
    ci_decision: Optional[str] = None  # "no_changes" | "analysis_success" | "git_ref_error" | "no_git_repo"
    # git baseline resolution metadata
    resolved_since_ref: Optional[str] = None   # actual ref/hash used for the diff
    resolution_path: Optional[str] = None      # "exact_local_ref"|"remote_tracking_ref"|"symbolic_ref"|"head_minus_1_fallback"|"uncommitted_changes"|"unresolvable"
    diff_validation_status: Optional[str] = None  # "valid_non_empty"|"valid_empty"|"invalid_ref"
    # review-pr specific impact sections
    base_ref: Optional[str] = None
    security_impact: dict = field(default_factory=dict)
    transactional_impact: dict = field(default_factory=dict)
    configuration_impact: dict = field(default_factory=dict)
    test_coverage_risk: dict = field(default_factory=dict)
    review_hotspots: list[str] = field(default_factory=list)
    suggested_review_order: list[str] = field(default_factory=list)
    execution_paths: list[dict] = field(default_factory=list)
    behavioral_impact: list[dict] = field(default_factory=list)
    # git-first scope metadata (review-pr only)
    scope_source: Optional[str] = None   # "git_diff" | "staged" | "untracked" | "full_scan_fallback"
    scope_files: list[str] = field(default_factory=list)
    repo_root: Optional[str] = None
    # honest output schema (review-pr only): runtime vs build split
    runtime_changes: list[dict] = field(default_factory=list)
    build_changes: dict = field(default_factory=dict)
    # review-pr: committed vs uncommitted — never merged
    committed_changes: list[dict] = field(default_factory=list)
    uncommitted_changes: list[dict] = field(default_factory=list)
    # transparency: explicit diff scope for every command
    analysis_scope: dict = field(default_factory=dict)


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
        ev = _read_persistence_evidence(Path(path).parent.parent, path)
        if ev:
            return f"JPA entity for {class_name} persistence"
        return f"Domain model — {class_name} (no persistence annotation detected)"
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

# ── Evidence admission helpers (valid types: git_diff, annotation, import, symbol, config) ──

_CLASS_SUFFIX_EVIDENCE: list[tuple[tuple[str, ...], str, str]] = [
    (("Repository", "Dao", "DAO"),                  "repository pattern",   "medium"),
    (("Mapper",),                                   "mapper pattern",       "medium"),
    (("Service", "Manager", "Handler", "Facade"),   "service pattern",      "medium"),
    (("Controller", "Resource", "Endpoint"),        "controller pattern",   "medium"),
    (("Config", "Configuration"),                   "config class",         "medium"),
    (("Test", "Tests", "IT", "Spec"),               "test class",           "strong"),
    (("DTO", "Dto"),                                "DTO pattern",          "medium"),
    (("Entity",),                                   "entity pattern",       "medium"),
]


def _symbol_evidence_from_class(class_name: str) -> "dict | None":
    for suffixes, label, strength in _CLASS_SUFFIX_EVIDENCE:
        for suffix in suffixes:
            if class_name.endswith(suffix) and class_name != suffix:
                return {
                    "type": "symbol",
                    "strength": strength,
                    "signal": f"class name suffix '{suffix}' — {label}",
                }
    return None


_PERSISTENCE_ANNOTATION_SIGNALS = frozenset({
    "@Repository", "@Entity", "@Table", "@MappedSuperclass",
    "@NamedQuery", "@SqlResultSetMapping", "@Embeddable",
})
_PERSISTENCE_IMPORT_PREFIXES = (
    "javax.persistence", "jakarta.persistence",
    "org.hibernate", "org.springframework.data.jpa",
)
_PERSISTENCE_ORM_SYMBOLS = ("EntityManager", "JpaRepository", "CrudRepository", "HibernateTemplate", "SessionFactory")


def _read_persistence_evidence(root: Path, file_path: str) -> "list[dict]":
    evidence: list[dict] = []
    try:
        with open(root / file_path, encoding="utf-8", errors="replace") as _fp:
            content = _fp.read(8000)
    except OSError:
        return evidence
    for ann in _PERSISTENCE_ANNOTATION_SIGNALS:
        if ann in content:
            evidence.append({"type": "annotation", "strength": "strong", "signal": f"{ann} found in file content"})
            break
    for imp in _PERSISTENCE_IMPORT_PREFIXES:
        if imp in content:
            evidence.append({"type": "import", "strength": "strong", "signal": f"import {imp}.* found in file content"})
            break
    for sym in _PERSISTENCE_ORM_SYMBOLS:
        if sym in content:
            evidence.append({"type": "symbol", "strength": "medium", "signal": f"{sym} usage found in file content"})
            break
    return evidence


# Code signal rules for non-persistence classifiable types.
# Path/module/suffix are explicitly excluded — only code-grounded signals.
_CODE_SIGNAL_RULES: dict[str, list[str]] = {
    "service":        ["@Service", "@Component(", "@Transactional\n", "@Transactional("],
    "controller":     ["@RestController", "@Controller", "@RequestMapping",
                       "@GetMapping", "@PostMapping", "@PutMapping", "@DeleteMapping", "@PatchMapping"],
    "entrypoint":     ["@SpringBootApplication", "public static void main", "if __name__ == '__main__'"],
    "security":       ["@EnableWebSecurity", "SecurityFilterChain", "WebSecurityConfigurerAdapter",
                       "extends OncePerRequestFilter"],
    "spring_config":  ["@Configuration", "@Bean\n", "@Bean("],
    "spring_profile": ["@Profile(", "@ConditionalOnProperty"],
    "dto":            ["@Data\n", "@Data(", "@Value\n", "@JsonProperty", "@Schema(", "@XmlRootElement",
                       "@Builder\n", "@Builder(", "@Getter", "@Setter"],
    "test":           ["@Test\n", "@Test(", "import org.junit", "import org.testng",
                       "import org.mockito", "import org.springframework.boot.test",
                       "@BeforeEach", "@BeforeAll", "@Before\n", "@Before("],
}


def _read_code_signal_evidence(root: Path, file_path: str, artifact_type: str) -> "list[dict]":
    """Verify artifact_type with code signals from file content (first 8KB).
    Returns single-item list on first match, empty list if no signal found.
    Path segments, module names, and class suffixes are excluded — per evidence firewall rules.
    """
    signals = _CODE_SIGNAL_RULES.get(artifact_type, [])
    if not signals:
        return []
    try:
        with open(root / file_path, encoding="utf-8", errors="replace") as _fp:
            content = _fp.read(8000)
    except OSError:
        return []
    for signal in signals:
        if signal in content:
            ev_type = (
                "annotation" if signal.startswith("@")
                else "import" if signal.startswith("import ")
                else "symbol"
            )
            return [{"type": ev_type, "strength": "strong", "signal": f"{signal.strip()} found in file content"}]
    return []


_ARTIFACT_CHANGE_EFFECT: dict[str, str] = {
    "entrypoint":     "application entrypoint (framework bootstrap / CLI handler)",
    "controller":     "HTTP routing layer (request-to-handler mapping)",
    "service":        "business logic layer (@Service component)",
    "repository":     "data access layer (persistence queries / ORM)",
    "mapper":         "SQL-object mapping layer (MyBatis mapper / query template)",
    "security":       "security component (authentication / access control configuration)",
    "spring_config":  "Spring @Configuration class (bean definitions / datasource wiring)",
    "spring_profile": "Spring profile override (environment-specific configuration)",
    "config":         "configuration file (application properties / environment values)",
    "build_manifest": "build manifest (dependency and plugin configuration)",
    "db_migration":   "database schema migration (DDL change pending execution)",
    "domain_model":   "domain entity (@Entity / value object)",
    "dto":            "data transfer object (serialization contract)",
    "test":           "test file (no production code modified)",
    "documentation":  "documentation file (no runtime impact)",
    "ide_noise":      "IDE/tooling artifact (no application impact)",
    "source":         "application source (artifact role requires annotation inspection)",
}

# Maps frontend symptom keywords → backend terms likely to contain the root cause.
# Used to boost service/interceptor files when the symptom is UI-only.
_FRONTEND_SYMPTOM_MAP: dict[str, list[str]] = {
    "spinner":  ["loading", "setloading", "finalize", "httpinterceptor", "interceptor", "service"],
    "loading":  ["loading", "setloading", "finalize", "httpinterceptor", "interceptor", "service"],
    "login":    ["authcontroller", "securityconfig", "filterconfig", "jwtfilter", "auth", "authentication"],
    "logout":   ["authcontroller", "securityconfig", "jwtfilter", "auth", "session"],
    "dropdown": ["getmapping", "findall", "obtenertodos", "listall", "findby"],
    "modal":    ["controller", "getmapping", "findby", "search"],
    "popup":    ["controller", "getmapping", "findby", "search"],
    "table":    ["paginated", "findby", "search", "getmapping", "listall"],
    "grid":     ["paginated", "findby", "search", "getmapping"],
    "button":   ["postmapping", "putmapping", "deletemapping", "controller", "service"],
    "form":     ["postmapping", "putmapping", "controller", "service", "dto"],
}


class TaskContextBuilder:
    def __init__(self, root: Path) -> None:
        self.root = root

    def build(self, task_name: str, *, since: Optional[str] = None, symptom: Optional[str] = None) -> TaskOutput:
        if task_name not in TASKS:
            raise ValueError(
                f"Unknown task '{task_name}'. Available: {', '.join(TASKS)}"
            )
        spec = TASKS[task_name]

        # ── 0. review-pr: git-first scope resolution (before any filesystem scan) ─
        _pr_git_root: Optional[Path] = None
        _pr_scope_files: Optional[list[str]] = None
        _pr_scope_source: str = "full_scan_fallback"

        if task_name == "review-pr":
            _pr_git_root = self._resolve_git_root()
            if _pr_git_root is None:
                return TaskOutput(
                    task="review-pr", goal=spec.goal,
                    project_summary=None, architecture_summary=None,
                    relevant_files=[], suspected_areas=[],
                    improvement_opportunities=[], test_gaps=[],
                    key_dependencies=[], code_notes_summary=None,
                    limitations=[], confidence="low",
                    error_code="no_git_repo",
                    error_message="review-pr requires a git repository.",
                    ci_decision="no_git_repo",
                    scope_source="full_scan_fallback",
                    repo_root=str(self.root),
                )
            _raw_scope, _pr_scope_source, _pr_committed_files, _pr_uncommitted_files = \
                self._get_pr_scope_files(since=since)
            if _raw_scope is None:
                # Explicit --since ref is invalid
                _avail_pr, _sug_pr = self._get_available_refs(since or "")
                _pr_hints: list[str] = []
                if _sug_pr:
                    _pr_hints.append(f"Did you mean '{_sug_pr}'?")
                if _avail_pr:
                    _pr_hints.append(f"Available refs: {', '.join(_avail_pr[:8])}")
                return TaskOutput(
                    task="review-pr", goal=spec.goal,
                    project_summary=None, architecture_summary=None,
                    relevant_files=[], suspected_areas=[],
                    improvement_opportunities=[], test_gaps=[],
                    key_dependencies=[], code_notes_summary=None,
                    limitations=[], confidence="low",
                    since=since,
                    error_code="git_ref_not_found",
                    error_message=f"Base ref '{since}' not found in this repository.",
                    error_hints=_pr_hints,
                    gaps=[f"Cannot compute PR diff: git ref '{since}' not found."] + _pr_hints,
                    ci_decision="git_ref_error",
                    scope_source="git_diff",
                    repo_root=str(_pr_git_root),
                )
            _pr_scope_files = _raw_scope
            # _pr_scope_files == [] means no diff; handled in step 5d

        _use_git_first = task_name == "review-pr"

        # ── 1. Scan ────────────────────────────────────────────────────────
        from sourcecode.adaptive_scanner import AdaptiveScanner
        from sourcecode.repo_classifier import RepoClassifier
        from sourcecode.tree_utils import flatten_file_tree
        from sourcecode.scanner import FileScanner as _FileScanner

        _pre_manifests = _FileScanner(self.root, max_depth=1).find_manifests()
        _java_names = {"pom.xml", "build.gradle", "build.gradle.kts"}
        _is_java = any(Path(m).name in _java_names for m in _pre_manifests)
        manifests = _pre_manifests

        if _use_git_first:
            # Git-first: no full filesystem traversal — skip AdaptiveScanner.
            # all_paths = scope files + siblings in same directories (bounded context
            # for behavioral_impact reverse lookups without scanning the whole repo).
            file_tree: dict = {}
            all_paths = self._expand_scope_for_analysis(_pr_scope_files or [])
        else:
            _topology = RepoClassifier().classify(self.root)
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

        if not _use_git_first:
            # Workspace sub-scans: each runs AdaptiveScanner on a workspace root.
            # Skipped for review-pr — would re-trigger full traversal per workspace.
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
            if _delta_raw is None:
                # Explicit --since ref couldn't be resolved — hard error, no fallback
                _avail_branches, _suggested = self._get_available_refs(since or "")
                _hints: list[str] = []
                if _suggested:
                    _hints.append(f"Did you mean '{_suggested}'?")
                if _avail_branches:
                    _hints.append(f"Available refs: {', '.join(_avail_branches[:8])}")
                return TaskOutput(
                    task="delta",
                    goal="Produce incremental context for changed files — avoids re-reading the full repo.",
                    project_summary=None,
                    architecture_summary=None,
                    relevant_files=[],
                    suspected_areas=[],
                    improvement_opportunities=[],
                    test_gaps=[],
                    key_dependencies=[],
                    code_notes_summary=None,
                    limitations=[],
                    confidence="low",
                    since=since,
                    error_code="git_ref_not_found",
                    error_message=f"Git reference '{since}' does not exist in this repository.",
                    error_hints=_hints,
                    gaps=[f"Cannot compute delta: git ref '{since}' not found."] + _hints,
                    ci_decision="git_ref_error",
                )
            elif _delta_raw:
                _delta_files = set(_delta_raw)

        # ── 5d. review-pr: set _delta_files from pre-resolved git scope ──────────
        # No-git and invalid-ref cases were already handled in step 0 (early returns).
        if task_name == "review-pr":
            if not _pr_scope_files:
                _no_diff_hint = "review-pr requires changed files or --since <ref>."
                return TaskOutput(
                    task="review-pr", goal=spec.goal,
                    project_summary=None, architecture_summary=None,
                    relevant_files=[], suspected_areas=[],
                    improvement_opportunities=[], test_gaps=[],
                    key_dependencies=[], code_notes_summary=None,
                    limitations=[], confidence="low",
                    error_code="no_diff",
                    error_message=f"No PR diff detected. {_no_diff_hint}",
                    gaps=[f"No PR diff detected. {_no_diff_hint}"],
                    ci_decision="no_changes",
                    scope_source=_pr_scope_source,
                    scope_files=[],
                    repo_root=str(_pr_git_root),
                )
            _delta_files = set(_pr_scope_files)

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

        # Delta uses a dedicated impact-analysis path — never the generic ranker.
        _delta_impact_summary: Optional[str] = None
        _delta_affected_modules: list[str] = []
        _delta_risk_areas: list[dict] = []
        _delta_why: dict[str, str] = {}
        _delta_analysis_gaps: list[str] = []
        _delta_system_impact: dict = {}
        _delta_change_type: list[str] = []
        _delta_dep_graph_summary: dict = {}
        _delta_impact_score_per_file: dict = {}

        if task_name in ("delta", "review-pr"):
            _delta_changed_list: list[str] = sorted(_delta_files) if _delta_files else []
            (
                relevant_files,
                _delta_impact_summary,
                _delta_affected_modules,
                _delta_risk_areas,
                _delta_why,
                _delta_analysis_gaps,
                _delta_system_impact,
                _delta_change_type,
                _delta_dep_graph_summary,
                _delta_impact_score_per_file,
            ) = self._build_delta_impact(
                changed_files=_delta_changed_list,
                all_paths=all_paths,
                entry_points=entry_points,
                since=since,
            )
        else:
            relevant_files = self._rank_files(
                task_name, spec, all_paths, entry_set, test_set,
                monorepo_packages=sm.monorepo_packages if sm.monorepo_packages else None,
                git_hotspots=git_hotspots,
                uncommitted_files=uncommitted_files,
                code_notes=cn_notes_for_ranking if cn_notes_for_ranking else None,
                delta_files=None,
            )

        # ── 6b. review-pr: derive PR-specific impact sections from delta analysis ──
        _pr_security_impact: dict = {}
        _pr_transactional_impact: dict = {}
        _pr_configuration_impact: dict = {}
        _pr_test_coverage_risk: dict = {}
        _pr_review_hotspots: list[str] = []
        _pr_suggested_review_order: list[str] = []
        _pr_base_ref: Optional[str] = None
        _pr_runtime_changes: list[dict] = []
        _pr_build_changes: dict = {}
        _pr_committed_changes: list[dict] = []
        _pr_uncommitted_changes: list[dict] = []
        _pr_committed_files: list[str] = []
        _pr_uncommitted_files: list[str] = []

        if task_name == "review-pr":
            _pr_base_ref = since or "HEAD"
            _sys_risk_areas = _delta_system_impact.get("risk_areas", [])

            _security_files = [
                f for ra in _sys_risk_areas if ra["area"] == "security"
                for f in ra["affected_files"]
            ]
            _transaction_files = [
                f for ra in _sys_risk_areas if ra["area"] in ("transactions", "business_logic")
                for f in ra["affected_files"]
            ]
            _config_files = [
                f for ra in _sys_risk_areas if ra["area"] in ("api", "config")
                for f in ra["affected_files"]
                if any(kw in f.lower() for kw in ("config", "properties", "yml", "yaml", "xml", "spring"))
            ]

            if _security_files:
                _pr_security_impact = {
                    "affected_resources": _security_files,
                    "risk_level": "high",
                }
            if _transaction_files:
                _pr_transactional_impact = {
                    "affected_transactions": _transaction_files,
                    "risk": "possible transaction boundary change",
                }
            if _config_files:
                _pr_configuration_impact = {"changed_configs": _config_files}

            # Test coverage risk scoped to changed source files only
            _changed_src = [
                f for f in sorted(_delta_files or set())
                if not self._is_test(f) and self._is_source(f)
            ]
            _test_stems = {Path(p).stem for p in test_set}
            _untested_changed = [f for f in _changed_src if Path(f).stem not in _test_stems]
            _test_risk_level = (
                "high" if len(_untested_changed) > 3
                else "medium" if _untested_changed
                else "low"
            )
            _pr_test_coverage_risk = {
                "changed_files_without_tests": _untested_changed[:10],
                "risk_level": _test_risk_level,
            }

            # Pre-classify changed files once — reused for hotspots, order, and runtime/build split
            _pr_changed_cls: dict[str, dict] = {
                f: self._classify_changed_file(f) for f in (_delta_files or set())
            }

            # Review hotspots: top changed RUNTIME files ranked by impact score — no build artifacts
            _pr_review_hotspots = sorted(
                [f for f, cls in _pr_changed_cls.items()
                 if not cls["is_noise"] and cls["artifact_type"] != "build_manifest"],
                key=lambda f: (_delta_impact_score_per_file.get(f) or {}).get("_rank_score", 0.0),
                reverse=True,
            )[:8]

            # Suggested review order: security first, then api → service → persistence → config
            # build_manifest is intentionally absent from _ORDER_TYPES
            _ORDER_TYPES = ["security", "controller", "service", "repository", "mapper",
                            "spring_config", "config", "domain_model", "dto"]
            _seen_order: set[str] = set()
            for _otype in _ORDER_TYPES:
                for _ra in _delta_risk_areas:
                    for _f in _ra.get("affected_files", []):
                        if _f not in _seen_order:
                            _cls = _pr_changed_cls.get(_f) or self._classify_changed_file(_f)
                            if _cls["artifact_type"] == _otype:
                                _pr_suggested_review_order.append(_f)
                                _seen_order.add(_f)
            for _f in _pr_review_hotspots:
                if _f not in _seen_order:
                    _pr_suggested_review_order.append(_f)
                    _seen_order.add(_f)

            # Build runtime_changes and build_changes — honest split, no score numbers
            # Also track committed vs uncommitted — NEVER merged
            _pr_runtime_changes: list[dict] = []
            _pr_committed_changes: list[dict] = []
            _pr_uncommitted_changes: list[dict] = []
            _pr_build_changes: dict = {}
            _build_artifact_files: list[str] = []
            _committed_set: set[str] = set(_pr_committed_files) if task_name == "review-pr" else set()
            _uncommitted_set: set[str] = set(_pr_uncommitted_files) if task_name == "review-pr" else set()

            _SCORE_TO_CONFIDENCE = {
                "high":   lambda s: s >= 0.60,
                "medium": lambda s: 0.40 <= s < 0.60,
            }

            for _f in sorted(_delta_files or set()):
                _f_cls = _pr_changed_cls.get(_f) or self._classify_changed_file(_f)
                _f_atype = _f_cls["artifact_type"]
                if _f_cls["is_noise"]:
                    continue
                if _f_atype == "build_manifest":
                    _build_artifact_files.append(_f)
                    continue
                # ── Evidence: only admitted types [git_diff, annotation, import, symbol, config] ──
                _evidence: list[dict] = [
                    {"type": "git_diff", "strength": "weak", "signal": "file present in commit diff"}
                ]
                # Symbol evidence from class name (path segments are rejected — not a valid type)
                _class_name = Path(_f).stem
                _sym_ev = _symbol_evidence_from_class(_class_name)
                if _sym_ev:
                    _evidence.append(_sym_ev)

                # All classifiable types require at least one code signal from content.
                # Path segments and module names are rejected evidence — not a valid type.
                _PERSISTENCE_SENSITIVE = frozenset({"repository", "mapper", "domain_model"})
                _CONTENT_VERIFIABLE = frozenset({
                    "service", "controller", "entrypoint", "security",
                    "spring_config", "spring_profile", "dto",
                })
                if _f_atype in _PERSISTENCE_SENSITIVE:
                    _content_ev = _read_persistence_evidence(self.root, _f)
                    if _content_ev:
                        _evidence.extend(_content_ev)
                    else:
                        _f_atype = "source"
                elif _f_atype in _CONTENT_VERIFIABLE:
                    _content_ev = _read_code_signal_evidence(self.root, _f, _f_atype)
                    if _content_ev:
                        _evidence.extend(_content_ev)
                    else:
                        _f_atype = "source"
                elif _f_atype == "test":
                    # test: class name suffix is valid per gate (naming rule)
                    # additionally verify with test framework signals for stronger evidence
                    _test_ev = _read_code_signal_evidence(self.root, _f, "test")
                    if _test_ev:
                        _evidence.extend(_test_ev)
                    # no downgrade — naming is a valid gate signal for test

                # Role basis: derived from strongest admitted evidence
                _has_code_ev = any(e["type"] in ("annotation", "import") for e in _evidence)
                _has_symbol_ev = any(e["type"] == "symbol" for e in _evidence)
                _role_basis = "annotation" if _has_code_ev else ("symbol" if _has_symbol_ev else "git_diff_only")
                _role_obj = {
                    "basis": _role_basis,
                    "has_annotation_signal": _has_code_ev,
                    "has_symbol_signal": _has_symbol_ev,
                }

                _diff_source = (
                    DiffSourceType.GIT_RANGE.value if _f in _committed_set
                    else DiffSourceType.WORKTREE_UNSTAGED.value if _f in _uncommitted_set
                    else "unknown"
                )
                _impact_entry = _delta_impact_score_per_file.get(_f) or {}
                _entry = {
                    "path": _f,
                    "diff_source": _diff_source,
                    "role": _role_obj,
                    "artifact_type": _f_atype,
                    "evidence": _evidence,
                    "change_effect": {
                        "statement": _ARTIFACT_CHANGE_EFFECT.get(_f_atype, "application source (artifact role requires annotation inspection)"),
                        "classification_method": _f_cls.get("confidence", "low"),
                    },
                    "evidence_completeness": _impact_entry.get("evidence", {}),
                }
                _pr_runtime_changes.append(_entry)
                # Split committed vs uncommitted — never merged
                if _f in _committed_set:
                    _pr_committed_changes.append(_entry)
                elif _f in _uncommitted_set:
                    _pr_uncommitted_changes.append(_entry)

            if _build_artifact_files:
                _pr_build_changes = {
                    "files": _build_artifact_files,
                    "impact": "dependency/configuration only",
                }

        # ── 6d. review-pr: execution paths + behavioral impact ──────────────
        _execution_paths: list[dict] = []
        _behavioral_impact: list[dict] = []
        if task_name == "review-pr" and _delta_files:
            from sourcecode.flow_analyzer import analyze_execution_paths, analyze_behavioral_impact
            _changed_sorted = sorted(_delta_files)
            _execution_paths = analyze_execution_paths(
                changed_files=_changed_sorted,
                all_paths=all_paths,
                root=self.root,
                classify_fn=self._classify_changed_file,
            )
            _behavioral_impact = analyze_behavioral_impact(
                changed_files=_changed_sorted,
                all_paths=all_paths,
                root=self.root,
                classify_fn=self._classify_changed_file,
            )

        # ── 6c. Symptom keyword boost + related notes (fix-bug + --symptom) ──
        symptom_keywords: list[str] = []
        related_notes: list[dict] = []
        symptom_note: Optional[str] = None
        if task_name == "fix-bug" and symptom:
            import re as _re
            _camel_expanded = _re.sub(r'([a-z])([A-Z])', r'\1 \2', symptom)
            _camel_expanded = _re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', _camel_expanded)
            symptom_keywords = [
                w.lower() for w in _re.split(r"[\s\W]+", _camel_expanded)
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

                # Content scan boost: read file body for symptom keywords
                _src_exts = frozenset({".java", ".py", ".ts", ".js", ".kt", ".go"})
                _content_boosted: list[RelevantFile] = []
                for _rf in relevant_files:
                    _extra = 0.0
                    if Path(_rf.path).suffix.lower() in _src_exts:
                        try:
                            _lines = (self.root / _rf.path).read_text(
                                encoding="utf-8", errors="replace"
                            ).splitlines()[:300]
                            _body = "\n".join(_lines).lower()
                            _hits = sum(_body.count(kw) for kw in symptom_keywords)
                            _extra = min(0.30, _hits * 0.02)
                        except OSError:
                            pass
                    _content_boosted.append(RelevantFile(
                        path=_rf.path,
                        role=_rf.role,
                        score=round(min(_rf.score + _extra, 1.0), 2),
                        reason=_rf.reason + (f", content-match symptom (+{_extra:.2f})" if _extra > 0 else ""),
                        why=_rf.why,
                    ))
                relevant_files = sorted(_content_boosted, key=lambda rf: -rf.score)

                # Cross-layer synonym boost: frontend keywords → backend equivalents
                _synonym_note: Optional[str] = None
                _frontend_kws = [kw for kw in symptom_keywords if kw in _FRONTEND_SYMPTOM_MAP]
                if _frontend_kws:
                    _backend_terms: list[str] = []
                    for _fkw in _frontend_kws:
                        _backend_terms.extend(_FRONTEND_SYMPTOM_MAP[_fkw])
                    _backend_terms_set = list(dict.fromkeys(_backend_terms))  # dedup, preserve order
                    _synonym_boosted: list[RelevantFile] = []
                    for _rf in relevant_files:
                        _extra_syn = 0.0
                        if Path(_rf.path).suffix.lower() in _src_exts:
                            try:
                                _lines_syn = (self.root / _rf.path).read_text(
                                    encoding="utf-8", errors="replace"
                                ).splitlines()[:300]
                                _body_syn = "\n".join(_lines_syn).lower()
                                _hits_syn = sum(_body_syn.count(t) for t in _backend_terms_set)
                                _extra_syn = min(0.20, _hits_syn * 0.02)
                            except OSError:
                                pass
                        _synonym_boosted.append(RelevantFile(
                            path=_rf.path,
                            role=_rf.role,
                            score=round(min(_rf.score + _extra_syn, 1.0), 2),
                            reason=_rf.reason + (f", synonym-match backend (+{_extra_syn:.2f})" if _extra_syn > 0 else ""),
                            why=_rf.why,
                        ))
                    relevant_files = sorted(_synonym_boosted, key=lambda rf: -rf.score)
                    _synonym_note = (
                        f"Frontend concept detected ({', '.join(_frontend_kws)}). "
                        "Boosted backend service-layer and interceptor files as likely root cause."
                    )
                    symptom_note = _synonym_note

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
        if task_name in ("delta", "review-pr"):
            # Use delta-specific gaps; ConfidenceAnalyzer gaps are about full-repo
            # detection quality and are not meaningful for an incremental diff.
            gaps = _delta_analysis_gaps
            if _mybatis_warning:
                gaps.append(_mybatis_warning["reason"])
        else:
            gaps = [g.reason for g in analysis_gaps]
            if _mybatis_warning:
                gaps.append(_mybatis_warning["reason"])

        # ── 9. why_these_files ────────────────────────────────────────────────
        if task_name in ("delta", "review-pr"):
            why_these_files = _delta_why
        else:
            why_these_files = {rf.path: rf.reason for rf in relevant_files}

        # ── 10. Delta / review-pr: git changed files + entry points ──────────
        changed_files: list[str] = []
        affected_entry_points: list[str] = []
        if task_name in ("delta", "review-pr"):
            changed_files = sorted(_delta_files) if _delta_files else (self._get_git_changed_files(since=since) or [])
            _ep_set = {ep.path for ep in entry_points}
            # include framework-detected entry points AND files classified as
            # entrypoint/controller/security by artifact taxonomy
            # (CLI mains, Spring controllers, Spring Security filters/interceptors)
            _EP_ARTIFACT_TYPES = frozenset({"entrypoint", "controller", "security"})
            affected_entry_points = sorted({
                f for f in changed_files
                if f in _ep_set
                or self._classify_changed_file(f)["artifact_type"] in _EP_ARTIFACT_TYPES
            })

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
            symptom_note=symptom_note,
            impact_summary=_delta_impact_summary,
            affected_modules=_delta_affected_modules,
            risk_areas=_delta_risk_areas,
            since=since if task_name in ("delta", "review-pr") else None,
            system_impact=_delta_system_impact,
            change_type=_delta_change_type,
            dependency_graph_summary=_delta_dep_graph_summary,
            impact_score_per_file=_delta_impact_score_per_file,
            ci_decision=(
                "no_changes" if task_name == "delta" and not changed_files
                else "analysis_success" if task_name in ("delta", "review-pr")
                else None
            ),
            # review-pr specific
            base_ref=_pr_base_ref,
            security_impact=_pr_security_impact,
            transactional_impact=_pr_transactional_impact,
            configuration_impact=_pr_configuration_impact,
            test_coverage_risk=_pr_test_coverage_risk,
            review_hotspots=_pr_review_hotspots,
            suggested_review_order=_pr_suggested_review_order,
            execution_paths=_execution_paths,
            behavioral_impact=_behavioral_impact,
            # git-first scope metadata
            scope_source=_pr_scope_source if task_name == "review-pr" else None,
            scope_files=list(_pr_scope_files) if task_name == "review-pr" and _pr_scope_files else [],
            repo_root=str(_pr_git_root) if task_name == "review-pr" and _pr_git_root else None,
            # honest output schema: runtime vs build split (review-pr only)
            runtime_changes=_pr_runtime_changes,
            build_changes=_pr_build_changes,
            # committed vs uncommitted — never merged
            committed_changes=_pr_committed_changes,
            uncommitted_changes=_pr_uncommitted_changes,
            # transparency: explicit diff scope
            analysis_scope={
                "sources_used": _pr_scope_source.split(",") if task_name == "review-pr" and _pr_scope_source else (
                    [DiffSourceType.GIT_SINCE_REF.value] if task_name == "delta" and since else
                    [DiffSourceType.WORKTREE_UNSTAGED.value] if task_name == "delta" else
                    []
                ),
                "git_equivalent_command": (
                    f"git diff --name-only {since} HEAD" if since else
                    "git diff --name-only"
                ) if task_name in ("delta", "review-pr") else None,
                "includes_uncommitted": bool(
                    (task_name == "review-pr" and _pr_uncommitted_files) or
                    (task_name == "delta" and not since)
                ),
            } if task_name in ("delta", "review-pr") else {},
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
            _bug_kinds = {"FIXME", "BUG", "HACK", "XXX"}
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
            _repo_size = len(all_paths)
            _task_budget = {
                "fix-bug": max(20, min(40, _repo_size // 80)),
                "onboard": max(15, min(25, _repo_size // 150)),
                "explain": max(10, min(20, _repo_size // 200)),
                "generate-tests": max(20, min(35, _repo_size // 100)),
                "refactor": max(15, min(30, _repo_size // 120)),
            }
            _budget = _task_budget.get(task_name, 15)
            _selected = _ctx.select_subgraph(_ns, contracts=[], budget=_budget, min_score=0.15)
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
            or (name.endswith("test.java") and name != "test.java")
            or name.endswith("tests.java")
            or (name.startswith("test") and name.endswith(".java") and len(name) > 9)
        )

    def _is_source(self, path: str) -> bool:
        return Path(path).suffix.lower() in _SOURCE_EXTENSIONS

    def _resolve_git_root(self) -> Optional[Path]:
        """Return the absolute git repo root, or None if not in a git repo."""
        import subprocess
        try:
            r = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=str(self.root),
                capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=5,
            )
            if r.returncode == 0 and r.stdout.strip():
                return Path(r.stdout.strip())
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        return None

    def _get_pr_scope_files(
        self, since: Optional[str] = None
    ) -> "tuple[Optional[list[str]], str, list[str], list[str]]":
        """Return (all_files, scope_source, committed_files, uncommitted_files).

        Scopes are NEVER mixed — committed and uncommitted tracked separately.
        Returns (None, _, _, _) only when since is explicitly provided but invalid.
        Returns ([], _, [], []) when git is available but no changes found.

        DiffSourceType mapping:
          since given  → committed: GIT_RANGE(since, HEAD)
          no since     → committed: [] (no implicit HEAD~1 fallback)
          always       → uncommitted: WORKTREE_UNSTAGED + WORKTREE_STAGED
        """
        import subprocess

        def _run(*cmd: str) -> Optional[list[str]]:
            try:
                r = subprocess.run(
                    list(cmd), cwd=str(self.root),
                    capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=10,
                )
                return (
                    [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
                    if r.returncode == 0 else None
                )
            except (subprocess.TimeoutExpired, FileNotFoundError):
                return None

        committed_files: list[str] = []
        uncommitted_files: list[str] = []
        sources: list[str] = []

        # ── Committed scope (GIT_RANGE) — only when explicit ref given ──────
        if since is not None:
            committed = _run("git", "diff", "--name-only", "--relative", since, "HEAD")
            if committed is None:
                return None, "git_diff", [], []  # invalid ref — hard error
            committed_files = committed
            if committed_files:
                sources.append(DiffSourceType.GIT_RANGE.value)

        # ── Uncommitted scope — ALWAYS separate, never implicit ──────────────
        # WORKTREE_UNSTAGED: modified but not staged
        unstaged = _run("git", "diff", "--name-only", "--relative")
        if unstaged:
            uncommitted_files.extend(f for f in unstaged if f not in uncommitted_files)
            if DiffSourceType.WORKTREE_UNSTAGED.value not in sources:
                sources.append(DiffSourceType.WORKTREE_UNSTAGED.value)

        # WORKTREE_STAGED: staged, not yet committed
        staged = _run("git", "diff", "--name-only", "--cached", "--relative")
        if staged:
            uncommitted_files.extend(f for f in staged if f not in uncommitted_files)
            if DiffSourceType.WORKTREE_STAGED.value not in sources:
                sources.append(DiffSourceType.WORKTREE_STAGED.value)

        # ── Drop paths outside self.root ──────────────────────────────────────
        def _drop_outside(lst: list[str]) -> list[str]:
            return [f for f in lst if not f.startswith("../") and not f.startswith("..\\")]

        committed_files = _drop_outside(committed_files)
        uncommitted_files = _drop_outside(uncommitted_files)

        all_files_set: set[str] = set(committed_files) | set(uncommitted_files)
        scope_source = ",".join(sources) if sources else "no_changes"
        return sorted(all_files_set), scope_source, committed_files, uncommitted_files

    def _expand_scope_for_analysis(self, scope_files: list[str]) -> list[str]:
        """Add sibling files in the same directories as scope_files (depth=1 expansion).

        Gives behavioral_impact engine context for reverse lookups (e.g. controllers
        in the same package as changed services) without traversing the full repo.
        """
        expanded: set[str] = set(scope_files)
        seen_dirs: set[Path] = set()

        for f in scope_files:
            parent = Path(f).parent
            if parent in seen_dirs:
                continue
            seen_dirs.add(parent)
            full_parent = self.root / parent
            if not full_parent.is_dir():
                continue
            try:
                for entry in full_parent.iterdir():
                    if entry.is_file():
                        rel = str(entry.relative_to(self.root)).replace("\\", "/")
                        expanded.add(rel)
            except OSError:
                pass

        return sorted(f for f in expanded if (self.root / f).exists())

    def _is_git_repo(self) -> bool:
        import subprocess
        try:
            r = subprocess.run(
                ["git", "rev-parse", "--git-dir"],
                cwd=str(self.root),
                capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=5,
            )
            return r.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    # ── Delta impact analysis ─────────────────────────────────────────────────

    @staticmethod
    def _classify_changed_file(path: str) -> dict[str, Any]:
        """Classify a changed file by artifact type, risk areas, impact level, and confidence.

        Returns dict: artifact_type, risk_areas, impact_level, is_noise, module, confidence.
        Pure path/name heuristics — no file reads, fully deterministic.

        Closed taxonomy (no unknown_* or generic_* values ever emitted):
          entrypoint | controller | service | repository | mapper | config |
          spring_config | spring_profile | security | domain_model | dto |
          test | build_manifest | documentation | ide_noise | db_migration | source

        Fallback order for unmatched source files:
          1. Stem keyword match (controller/service/repository/…)
          2. Folder path component match (innermost directory first)
          3. source (confidence=low — extension only)
        """
        norm = path.replace("\\", "/")
        name = Path(path).name
        stem = Path(path).stem
        suffix = Path(path).suffix.lower()
        norm_lower = norm.lower()
        stem_lower = stem.lower()
        name_lower = name.lower()

        _CODE_EXTS = frozenset({
            ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".kt", ".go",
            ".rs", ".rb", ".php", ".cs", ".dart", ".mjs", ".cjs", ".scala",
        })
        _CONFIG_EXTS = frozenset({
            ".yml", ".yaml", ".json", ".xml", ".toml", ".properties",
            ".env", ".cfg", ".ini", ".conf",
        })

        # IDE/hidden-tool directories → noise, skip impact analysis
        _IDE_DIR_NAMES = frozenset({
            ".idea", ".vscode", ".eclipse", ".fleet", ".git", ".github",
            ".circleci", ".travis", ".teamcity", ".gradle", ".mvn",
        })
        path_dir_parts = norm_lower.split("/")[:-1]  # all components except filename
        if any(part in _IDE_DIR_NAMES for part in path_dir_parts):
            return {
                "artifact_type": "ide_noise",
                "risk_areas": [],
                "impact_level": "noise",
                "is_noise": True,
                "module": "",
                "confidence": "high",
            }

        module = _extract_ddd_domain(path)

        # Tests (before other checks to avoid misclassifying TestFoo as service etc.)
        _is_test = (
            (stem_lower.startswith("test") and len(stem_lower) > 4)
            or (stem_lower.endswith("test") and len(stem_lower) > 4)
            or stem_lower.endswith("tests")
            or stem_lower.endswith("spec")
            or any(t in f"/{norm_lower}/" for t in (
                "/test/", "/tests/", "/spec/", "/specs/", "/__tests__/", "/it/",
            ))
        )
        if _is_test:
            return {"artifact_type": "test", "risk_areas": ["tests"], "impact_level": "low", "is_noise": False, "module": module, "confidence": "high"}

        # Entrypoints: Spring Boot Application, CLI mains, framework entry files
        _ENTRYPOINT_NAMES = frozenset({
            "main.py", "app.py", "run.py", "server.py", "wsgi.py", "asgi.py",
            "__main__.py", "index.js", "index.ts", "server.js", "server.ts",
            "app.js", "app.ts", "main.js", "main.ts",
        })
        if (
            name_lower in _ENTRYPOINT_NAMES
            or (suffix in _CODE_EXTS and stem_lower in ("cli", "manage", "entrypoint", "startup", "launcher"))
            or (suffix in (".java", ".kt") and stem_lower.endswith("application"))
        ):
            return {"artifact_type": "entrypoint", "risk_areas": ["api", "config"], "impact_level": "critical", "is_noise": False, "module": module, "confidence": "high"}

        # Security surface (extended: interceptor, filter, cors, acl)
        _SECURITY_KW = ("security", "auth", "jwt", "token", "permission", "role",
                         "credential", "encrypt", "decrypt", "oauth", "saml", "ldap",
                         "password", "secret", "interceptor", "filter", "cors", "acl")
        if suffix in _CODE_EXTS and any(kw in stem_lower for kw in _SECURITY_KW):
            impact = "critical" if any(kw in stem_lower for kw in ("security", "auth", "jwt")) else "high"
            return {"artifact_type": "security", "risk_areas": ["security"], "impact_level": impact, "is_noise": False, "module": module, "confidence": "high"}

        # API / controller layer
        _API_KW = ("controller", "restcontroller", "resource", "handler",
                   "router", "route", "endpoint", "servlet")
        if suffix in _CODE_EXTS and any(kw in stem_lower for kw in _API_KW):
            return {"artifact_type": "controller", "risk_areas": ["api"], "impact_level": "high", "is_noise": False, "module": module, "confidence": "high"}

        # Business logic / services (extended: facade, usecase, aspect, listener, component)
        _SERVICE_KW = ("service", "serviceimpl", "servicefacade", "facade", "usecase",
                       "interactor", "aspect", "listener", "subscriber", "eventhandler", "component")
        if suffix in _CODE_EXTS and any(kw in stem_lower for kw in _SERVICE_KW):
            return {"artifact_type": "service", "risk_areas": ["transactions", "business_logic"], "impact_level": "high", "is_noise": False, "module": module, "confidence": "high"}

        # Data access / repositories
        _DAO_KW = ("repository", "repositoryimpl", "dao", "daoimpl", "store", "jparepository")
        if suffix in _CODE_EXTS and any(kw in stem_lower for kw in _DAO_KW):
            return {"artifact_type": "repository", "risk_areas": ["persistence"], "impact_level": "high", "is_noise": False, "module": module, "confidence": "high"}

        # MyBatis / ORM mappers
        if "mapper" in stem_lower:
            return {"artifact_type": "mapper", "risk_areas": ["persistence"], "impact_level": "high", "is_noise": False, "module": module, "confidence": "high"}

        # Spring / app config files (by canonical name)
        if name_lower in ("application.yml", "application.yaml", "application.properties",
                           "bootstrap.yml", "bootstrap.yaml", "bootstrap.properties"):
            return {"artifact_type": "spring_config", "risk_areas": ["config"], "impact_level": "high", "is_noise": False, "module": module, "confidence": "high"}
        if name_lower.startswith("application-") and suffix in (".yml", ".yaml", ".properties"):
            return {"artifact_type": "spring_profile", "risk_areas": ["config"], "impact_level": "medium", "is_noise": False, "module": module, "confidence": "high"}
        _BUILD_MANIFEST_NAMES = frozenset({
            "pom.xml", "build.gradle", "build.gradle.kts",
            "settings.gradle", "settings.gradle.kts",
            "pyproject.toml", "setup.py", "setup.cfg",
            "package.json", "package-lock.json", "yarn.lock",
            "cargo.toml", "go.mod", "go.sum",
            "gemfile", "gemfile.lock", "build.sbt",
            "requirements.txt", "requirements-dev.txt",
        })
        if name_lower in _BUILD_MANIFEST_NAMES:
            return {"artifact_type": "build_manifest", "risk_areas": ["config", "dependencies"], "impact_level": "medium", "is_noise": False, "module": module, "confidence": "high"}

        # Configuration classes / files
        _CONFIG_STEM_KW = ("config", "configuration", "properties", "settings")
        if suffix in _CODE_EXTS and any(kw in stem_lower for kw in _CONFIG_STEM_KW):
            return {"artifact_type": "config", "risk_areas": ["config"], "impact_level": "medium", "is_noise": False, "module": module, "confidence": "high"}

        # DB migrations / SQL
        if suffix == ".sql" or any(kw in norm_lower for kw in ("migration", "flyway", "liquibase", "changelog")):
            return {"artifact_type": "db_migration", "risk_areas": ["persistence"], "impact_level": "high", "is_noise": False, "module": module, "confidence": "high"}

        # Domain models / entities
        _ENTITY_KW = ("entity", "model", "domain", "aggregate", "valueobject")
        if suffix in _CODE_EXTS and any(kw in stem_lower for kw in _ENTITY_KW):
            return {"artifact_type": "domain_model", "risk_areas": ["persistence"], "impact_level": "medium", "is_noise": False, "module": module, "confidence": "high"}

        # DTOs / request-response objects
        _DTO_KW = ("dto", "request", "response", "payload", "command", "query", "event")
        if suffix in _CODE_EXTS and any(kw in stem_lower for kw in _DTO_KW):
            return {"artifact_type": "dto", "risk_areas": [], "impact_level": "low", "is_noise": False, "module": module, "confidence": "high"}

        # No stem hint matched — path/folder components are NOT valid evidence.
        # Fall through to unclassified source (extension-only, low confidence).
        if suffix in _CODE_EXTS:
            return {"artifact_type": "source", "risk_areas": [], "impact_level": "medium", "is_noise": False, "module": module, "confidence": "low"}

        # Generic config / data files — fold into config type
        if suffix in _CONFIG_EXTS:
            return {"artifact_type": "config", "risk_areas": ["config"], "impact_level": "low", "is_noise": False, "module": module, "confidence": "low"}

        # Docs
        if suffix in (".md", ".rst", ".txt", ".adoc"):
            return {"artifact_type": "documentation", "risk_areas": [], "impact_level": "low", "is_noise": False, "module": module, "confidence": "high"}

        # Binaries, images, lock files — treat as noise (closed taxonomy: no unknown_*)
        return {"artifact_type": "ide_noise", "risk_areas": [], "impact_level": "noise", "is_noise": True, "module": module, "confidence": "low"}

    def _classify_diff_severity(self, path: str, since: Optional[str]) -> str:
        """Classify the semantic severity of a file's diff to gate BFS expansion.

        Returns: 'trivial' | 'field_change' | 'api_change' | 'security_change' | 'unknown'

        - trivial: only comments/whitespace changed — no BFS expansion seeded
        - field_change: field/attribute declarations changed — hop-1 only, no hop-2+ frontier
        - api_change: method signatures or class structure changed — full BFS
        - security_change: auth/security keywords in changed lines — full BFS + security chain
        - unknown: diff unreadable — treated as api_change (safe default)
        """
        import subprocess as _subprocess
        import re as _re

        try:
            if since:
                cmd = ["git", "diff", since, "HEAD", "--", path]
            else:
                cmd = ["git", "diff", "HEAD", "--", path]
            result = _subprocess.run(
                cmd, capture_output=True, text=True, timeout=5,
                cwd=str(self.root), encoding="utf-8", errors="ignore",
            )
            diff_text = result.stdout
        except Exception:
            return "unknown"

        if not diff_text.strip():
            return "unknown"

        changed_lines = [
            line[1:] for line in diff_text.splitlines()
            if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
        ]
        if not changed_lines:
            return "trivial"

        suffix = Path(path).suffix.lower()
        if suffix in (".java", ".kt"):
            _TRIVIAL  = _re.compile(r'^\s*(?://|/\*|\*)')
            _FIELD    = _re.compile(r'^\s*(?:private|protected|public|final|static)\s+\w[\w<>, ]*\s+\w+\s*[;=]')
            _API      = _re.compile(r'^\s*(?:public|protected)\s+\S.*\(')
            # Exclude 'password', 'role', 'permission' — these are common field names
            # in domain models and don't indicate auth logic changes. Keep mechanism
            # keywords: jwt, auth (as class prefix), token, credential, encrypt, decrypt, oauth.
            _SECURITY = _re.compile(r'\b(?:jwt|auth|token|credential|encrypt|decrypt|oauth|saml|ldap|principal|Security)\b')
            _STRUCT   = _re.compile(r'^\s*(?:class|interface|enum|record|import|package)\s')
        elif suffix == ".py":
            _TRIVIAL  = _re.compile(r'^\s*#')
            _FIELD    = _re.compile(r'^\s*(?:self\.\w+\s*=|\w+:\s*\w)')
            _API      = _re.compile(r'^\s*def\s+\w')
            _SECURITY = _re.compile(r'\b(?:jwt|auth|token|credential|encrypt|decrypt|oauth|saml|ldap|principal|security)\b', _re.IGNORECASE)
            _STRUCT   = _re.compile(r'^\s*(?:class|import|from)\s')
        elif suffix in (".ts", ".tsx", ".js", ".jsx", ".mjs"):
            _TRIVIAL  = _re.compile(r'^\s*(?://|/\*|\*)')
            _FIELD    = _re.compile(r'^\s*(?:private|readonly|public)?\s*\w+[?!]?\s*[=:]')
            _API      = _re.compile(r'^\s*(?:(?:public|private|protected|async|export)\s+)*(?:function\s+\w|\w+\s*\()')
            _SECURITY = _re.compile(r'\b(?:jwt|auth|token|credential|encrypt|decrypt|oauth|saml|ldap|principal|security)\b', _re.IGNORECASE)
            _STRUCT   = _re.compile(r'^\s*(?:class|interface|import|export\s+(?:class|interface|type))\s')
        else:
            return "unknown"

        if any(_SECURITY.search(line) for line in changed_lines):
            return "security_change"
        if any(_API.match(line) or _STRUCT.match(line) for line in changed_lines):
            return "api_change"
        if any(_FIELD.match(line) for line in changed_lines):
            return "field_change"
        if all(_TRIVIAL.match(line) or not line.strip() for line in changed_lines):
            return "trivial"
        return "field_change"  # safe default: treat unknown non-trivial as field-level

    def _scan_import_dependents(
        self,
        changed_paths: list[str],
        candidate_paths: list[str],
        *,
        max_candidates: int = 40,
    ) -> dict[str, list[str]]:
        """Find files in candidate_paths that import/reference each changed file.

        Returns mapping: changed_path → list[dependent_paths].
        Reads file contents — bounded by max_candidates per changed file.
        Only scans source files (.py, .java, .kt, .ts, .js, .tsx, .jsx).
        """
        import re as _re

        _SCANNABLE = frozenset({".py", ".java", ".kt", ".ts", ".js", ".tsx", ".jsx", ".mjs"})
        dependents: dict[str, list[str]] = {p: [] for p in changed_paths}

        for changed_path in changed_paths:
            stem = Path(changed_path).stem
            suffix = Path(changed_path).suffix.lower()
            if suffix not in _SCANNABLE:
                continue

            # Build search patterns per language
            if suffix == ".py":
                patterns = [
                    rf"(?:from|import)\s+[^\n]*\b{_re.escape(stem)}\b",
                ]
            elif suffix in (".java", ".kt"):
                patterns = [
                    rf"\bimport\b[^;]*\b{_re.escape(stem)}\b",
                    rf"(?:@Autowired|@Inject|@Resource)[^\n]*\n[^\n]*\b{_re.escape(stem)}\b",
                    rf"\b{_re.escape(stem)}\s+\w",  # field/param declaration: FooService fooService
                ]
            elif suffix in (".ts", ".tsx", ".js", ".jsx", ".mjs"):
                patterns = [
                    rf"from\s+['\"][^'\"]*{_re.escape(stem)}['\"]",
                    rf"require\s*\(\s*['\"][^'\"]*{_re.escape(stem)}['\"]",
                ]
            else:
                continue

            combined = _re.compile("|".join(patterns), _re.MULTILINE)

            scanned = 0
            for candidate in candidate_paths:
                if candidate == changed_path:
                    continue
                if Path(candidate).suffix.lower() not in _SCANNABLE:
                    continue
                if scanned >= max_candidates:
                    break
                try:
                    text = (self.root / candidate).read_text(encoding="utf-8", errors="ignore")
                    if combined.search(text):
                        dependents[changed_path].append(candidate)
                except OSError:
                    pass
                scanned += 1

        return dependents

    def _build_delta_impact(
        self,
        changed_files: list[str],
        all_paths: list[str],
        entry_points: list,
        since: Optional[str],
    ) -> tuple[list[RelevantFile], str, list[str], list[dict[str, Any]], dict[str, str], list[str]]:
        """Build incremental impact analysis for changed files.

        Returns:
            (relevant_files, impact_summary, affected_modules, risk_areas,
             why_these_files, analysis_gaps)

        Changed files are always included in relevant_files (never dropped by score).
        Related files are expanded type-aware: controller→service→repository→mapper chain.
        Scoring is hierarchical by artifact_type, not by heuristic impact_level.
        """
        # Per-artifact deterministic scores — strictly ordered by semantic role
        _ARTIFACT_SCORE: dict[str, float] = {
            "entrypoint":     0.95,
            "security":       0.90,
            "controller":     0.85,
            "service":        0.80,
            "db_migration":   0.75,
            "repository":     0.70,
            "mapper":         0.65,
            "spring_config":  0.60,
            "config":         0.55,
            "spring_profile": 0.50,
            "domain_model":   0.50,
            "build_manifest": 0.45,
            "source":         0.45,
            "dto":            0.35,
            "test":           0.30,
            "documentation":  0.25,
            "ide_noise":      0.10,
        }

        # impact_level per artifact_type — used for risk_areas severity ordering
        _ARTIFACT_IMPACT: dict[str, str] = {
            "entrypoint": "critical", "security": "critical",
            "controller": "high", "service": "high", "repository": "high",
            "mapper": "high", "db_migration": "high", "spring_config": "high",
            "config": "medium", "spring_profile": "medium",
            "build_manifest": "medium", "domain_model": "medium",
            "source": "medium",
            "dto": "low", "test": "low", "documentation": "low", "ide_noise": "noise",
        }

        # propagation_risk per artifact_type
        _PROPAGATION_RISK: dict[str, str] = {
            "entrypoint": "high", "security": "high", "controller": "high",
            "db_migration": "high", "spring_config": "high",
            "service": "medium", "repository": "medium", "mapper": "medium",
            "config": "medium", "domain_model": "medium",
            "spring_profile": "low", "build_manifest": "low", "source": "low",
            "dto": "low", "test": "low", "documentation": "low", "ide_noise": "low",
        }

        # type-aware expansion: which artifact types a changed type should pull in
        _EXPANSION_TARGETS: dict[str, frozenset[str]] = {
            "controller":    frozenset({"service", "security", "dto"}),
            "service":       frozenset({"repository", "mapper"}),
            "repository":    frozenset({"mapper", "domain_model"}),
            "mapper":        frozenset({"repository", "domain_model"}),
            "security":      frozenset({"controller", "config", "spring_config"}),
            "spring_config": frozenset({"service", "config", "repository"}),
            "config":        frozenset({"service", "repository", "controller"}),
            "entrypoint":    frozenset({"security", "config", "spring_config"}),
            "dto":           frozenset({"controller", "service"}),
            "domain_model":  frozenset({"repository", "service"}),
            "db_migration":  frozenset({"repository", "mapper"}),
            "spring_profile": frozenset({"service", "config"}),
            "source":        frozenset({"service", "repository"}),
            "test":          frozenset(),
            "documentation": frozenset(),
            "ide_noise":     frozenset(),
            "build_manifest": frozenset(),
        }

        # use module-level constant (single source of truth)
        _CHANGE_EFFECT = _ARTIFACT_CHANGE_EFFECT

        # change_type taxonomy — evidence-gated.
        # "source" → no change_type: unclassified file has no confirmed executable role.
        # behavioral_change requires semantic diff evidence (api_change/security_change),
        # not just artifact type — gate applied in Step 3d using diff_severities.
        _ARTIFACT_CHANGE_TYPES: dict[str, list[str]] = {
            "entrypoint":      ["structural_change"],   # behavioral only if diff confirms
            "security":        ["security_change"],
            "controller":      ["structural_change"],   # behavioral only if diff confirms
            "service":         [],                      # behavioral only if @Transactional or diff confirms
            "repository":      [],                      # behavioral only if query/ORM diff confirms
            "mapper":          [],
            "spring_config":   ["configuration_change"],
            "spring_profile":  ["configuration_change"],
            "config":          ["configuration_change"],
            "build_manifest":  ["dependency_change"],
            "db_migration":    ["structural_change"],
            "domain_model":    ["structural_change"],
            "dto":             ["structural_change"],
            "source":          [],   # no claim without content evidence
            "test":            [],
            "documentation":   [],
            "ide_noise":       [],
        }

        # Semantic diff → change_type supplement (gated on actual diff content)
        # applied per-file in Step 3d alongside diff_severities
        _DIFF_SEVERITY_CHANGE_TYPES: dict[str, list[str]] = {
            "api_change":      ["behavioral_change"],
            "security_change": ["behavioral_change", "security_change"],
            "field_change":    ["structural_change"],
            "trivial":         [],
            "unknown":         [],   # no claim — unknown diff content
        }

        _SEV_ORDER = ["noise", "low", "medium", "high", "critical"]

        # impact_area taxonomy — evidence-gated.
        # "source" → "application_layer" (neutral). transaction_boundary only for
        # evidence-confirmed service/transactional artifacts.
        _ATYPE_IMPACT_AREA: dict[str, str] = {
            "entrypoint":     "api_surface",
            "controller":     "api_surface",
            "dto":            "api_surface",
            "security":       "security_layer",
            "spring_config":  "dependency_injection",
            "service":        "application_layer",     # transaction_boundary only if @Transactional evidence
            "repository":     "persistence_layer",
            "mapper":         "persistence_layer",
            "db_migration":   "persistence_layer",
            "domain_model":   "persistence_layer",
            "config":         "configuration",
            "spring_profile": "configuration",
            "build_manifest": "build_system",
            "source":         "application_layer",     # neutral — no transaction claim without evidence
            "test":           "configuration",
            "documentation":  "configuration",
            "ide_noise":      "configuration",
        }
        _UI_PATH_SEGS = frozenset({"frontend", "angular", "react", "webapp", "web-app", "ui", "client-app", "client"})
        _INTEGRATION_STEMS = frozenset({"client", "adapter", "gateway", "proxy", "stub", "feignclient"})

        def _classify_impact_area(path: str, risk_areas: list[str], atype: str) -> str:
            path_lower = path.lower()
            suffix = Path(path).suffix.lower()
            path_segs = set(path_lower.replace("\\", "/").split("/"))
            if suffix in (".tsx", ".jsx", ".vue") or (
                suffix in (".ts", ".js") and bool(path_segs & _UI_PATH_SEGS)
            ):
                return "ui_layer"
            stem_lower = Path(path).stem.lower()
            if any(kw in stem_lower for kw in _INTEGRATION_STEMS):
                return "integration_layer"
            return _ATYPE_IMPACT_AREA.get(atype, "application_layer")

        # role_in_system — evidence-gated.
        # Default is "unclassified" not "core_service": unclassified source files
        # have no confirmed architectural role.
        def _role_in_system(path: str, atype: str, in_ep_paths: bool) -> str:
            if atype == "build_manifest":
                return "build_artifact"
            if atype == "test":
                return "test"
            stem_lower = Path(path).stem.lower()
            if atype == "dto" or any(kw in stem_lower for kw in ("util", "helper", "constant", "enum", "exception")):
                return "utility"
            if in_ep_paths or atype == "entrypoint":
                return "entrypoint"
            if atype == "controller":
                return "external_interface"
            if atype == "security":
                return "external_interface"
            if atype in ("config", "spring_config", "spring_profile"):
                return "configuration"
            if atype in ("repository", "mapper", "db_migration"):
                return "data_access"
            if atype == "service":
                return "service"   # annotation-confirmed — not "core_service" (overclaim)
            return "unclassified"   # no role claim without evidence

        def _structured_why(path: str, atype: str, module: str, role: str, risk_areas: list[str], cls_confidence: str = "low") -> str:
            area = _classify_impact_area(path, risk_areas, atype)
            prop = _PROPAGATION_RISK.get(atype, "low") if atype != "source" else "unknown"
            effect = _CHANGE_EFFECT.get(atype, "application source (artifact role requires annotation inspection)")
            parts = [f"artifact_type: {atype}", f"classification_confidence: {cls_confidence}"]
            # Only emit role/area/propagation when there is confirmed evidence (not source/unclassified)
            if role not in ("unclassified",):
                parts.append(f"role_in_system: {role}")
            if area not in ("application_layer",):
                parts.append(f"impact_area: {area}")
            if atype not in ("source",) and prop != "unknown":
                parts.append(f"propagation_risk: {prop}")
            parts.append(f"change_effect: {effect}")
            if module:
                parts.append(f"module: {module}")
            return " | ".join(parts)

        if not changed_files:
            return (
                [],
                "No changes detected — verify the git ref passed to --since",
                [],
                [],
                {},
                ["No changed files found. Check that --since ref exists and the diff is non-empty."],
                {},   # system_impact
                [],   # change_type
                {"edges": [], "propagation_depth": 0},  # dependency_graph_summary
                {},   # impact_score_per_file
            )

        ep_paths = {ep.path for ep in entry_points}
        graph_edges: list[dict] = []

        # ── Step 1: classify every changed file ───────────────────────────────
        classifications: dict[str, dict[str, Any]] = {
            f: self._classify_changed_file(f) for f in changed_files
        }

        # ── Step 1b: classify diff severity to gate BFS expansion ─────────────
        # trivial   → no BFS seeding (comments/whitespace only)
        # field_change → hop-1 BFS only, deps excluded from hop-2+ frontier
        # api_change   → full BFS (method signature or class structure changed)
        # security_change → full BFS + security chain allowed cross-module
        # unknown   → treated as api_change (safe default)
        diff_severities: dict[str, str] = {
            f: self._classify_diff_severity(f, since) for f in changed_files
        }

        # ── Step 2: build relevant_files from the changed set ─────────────────
        relevant: list[RelevantFile] = []
        why: dict[str, str] = {}
        affected_modules_set: set[str] = set()
        changed_dirs: set[str] = set()
        risk_acc: dict[str, dict[str, Any]] = {}  # area → {files, severity}
        ref_label = since or "HEAD~1"

        # union of expansion targets across all changed artifact types
        wanted_expansion_types: frozenset[str] = frozenset()

        for path, cls in classifications.items():
            atype = cls["artifact_type"]
            score = _ARTIFACT_SCORE.get(atype, 0.45)
            module = cls["module"]

            if module:
                affected_modules_set.add(module)
            if not cls["is_noise"]:
                parent = str(Path(path).parent).replace("\\", "/")
                if parent and parent != ".":
                    changed_dirs.add(parent)

            impact_level = _ARTIFACT_IMPACT.get(atype, "medium")
            for area in cls["risk_areas"]:
                if area not in risk_acc:
                    risk_acc[area] = {"files": [], "severity": "noise"}
                risk_acc[area]["files"].append(path)
                cur_idx = _SEV_ORDER.index(risk_acc[area]["severity"])
                new_idx = _SEV_ORDER.index(impact_level)
                if new_idx > cur_idx:
                    risk_acc[area]["severity"] = impact_level

            wanted_expansion_types = wanted_expansion_types | _EXPANSION_TARGETS.get(atype, frozenset())

            in_ep = path in ep_paths
            role = _role_in_system(path, atype, in_ep)
            cls_conf = cls.get("confidence", "low")
            why_str = _structured_why(path, atype, module, role, cls["risk_areas"], cls_conf)
            reason = f"changed since {ref_label} | artifact: {atype}"

            relevant.append(RelevantFile(path=path, role=role, score=round(score, 2), reason=reason, why=why_str))
            why[path] = why_str

        relevant.sort(key=lambda f: (-f.score, f.path))

        # ── Step 3: type-aware expansion to related files ─────────────────────
        existing_paths = {rf.path for rf in relevant}

        related: list[tuple[float, str, RelevantFile]] = []
        for path in all_paths:
            if path in existing_paths:
                continue
            if Path(path).suffix.lower() not in _ALL_EXTENSIONS:
                continue

            rel_cls = self._classify_changed_file(path)
            if rel_cls["is_noise"]:
                continue

            rel_atype = rel_cls["artifact_type"]
            # only expand if this file's type is in the wanted expansion set
            if rel_atype not in wanted_expansion_types:
                continue

            parent = str(Path(path).parent).replace("\\", "/")
            path_module = _extract_ddd_domain(path)

            in_same_module = bool(path_module and path_module in affected_modules_set)
            in_same_dir = parent in changed_dirs

            if not (in_same_module or in_same_dir):
                continue

            rel_base = _ARTIFACT_SCORE.get(rel_atype, 0.45)
            rel_score = round(rel_base * 0.60, 2)
            ctx_type = "module" if in_same_module else "directory"
            ctx_val = path_module if in_same_module else parent

            triggers = [
                Path(f).name for f in changed_files
                if (
                    (_extract_ddd_domain(f) == path_module if in_same_module
                     else str(Path(f).parent).replace("\\", "/") == parent)
                )
            ]
            in_ep = path in ep_paths
            role = _role_in_system(path, rel_atype, in_ep)
            rel_conf = rel_cls.get("confidence", "low")
            why_str = _structured_why(path, rel_atype, _extract_ddd_domain(path), role, rel_cls["risk_areas"], rel_conf)
            why_str += f" | pulled_by: type-aware expansion from {ctx_type} '{ctx_val}'"
            why_str += f" | triggered_by: {', '.join(triggers[:3])}"
            reason = f"expansion: {ctx_type} '{ctx_val}' | artifact: {rel_atype}"
            related.append((rel_score, path, RelevantFile(
                path=path, role=role, score=rel_score, reason=reason, why=why_str
            )))
            why[path] = why_str
            # module_proximity is heuristic (not a verified import) — excluded from structural graph

        related.sort(key=lambda x: (-x[0], x[1]))
        relevant.extend(rf for _, _, rf in related[:10])

        # ── Steps 3b–3c: BFS multi-hop import propagation (repo-wide, 3 hops max) ─
        # Each hop expands from whatever was found in the previous hop into ALL
        # remaining source files — not restricted to original module/dir.
        # This enables A→B→C discovery even when B and C are in different modules.
        _BFS_SCANNABLE = frozenset({".py", ".java", ".kt", ".ts", ".js", ".tsx", ".jsx", ".mjs"})
        _bfs_all_sources = [
            p for p in all_paths
            if Path(p).suffix.lower() in _BFS_SCANNABLE
        ]

        _bfs_seen: set[str] = {rf.path for rf in relevant}
        # trivial changes (comments/whitespace only) don't seed BFS — nothing structural
        # to propagate, so excluding them prevents false expansion on cosmetic commits
        _bfs_frontier: list[str] = [
            f for f in changed_files
            if Path(f).suffix.lower() in _BFS_SCANNABLE
            and diff_severities.get(f, "unknown") != "trivial"
        ]

        # (max results added from this hop, max_candidates scanned per seed)
        _BFS_HOP_BUDGET = [
            (8, 60),   # hop 1 — broad: callers of changed files
            (6, 50),   # hop 2 — transitives: callers of hop-1 files
            (4, 30),   # hop 3 — deep transitives: callers of hop-2 files
        ]

        # (hop_num, score, path, RelevantFile) — merged across all hops then added to relevant
        _bfs_collected: list[tuple[int, float, str, RelevantFile]] = []

        for _hop_num, (_max_results, _max_cands) in enumerate(_BFS_HOP_BUDGET, start=1):
            if not _bfs_frontier:
                break

            _bfs_candidates = [p for p in _bfs_all_sources if p not in _bfs_seen]
            if not _bfs_candidates:
                break

            _hop_dep_map = self._scan_import_dependents(
                changed_paths=_bfs_frontier,
                candidate_paths=_bfs_candidates,
                max_candidates=_max_cands,
            )

            # collect (score, path) pairs for this hop to build the next frontier
            _hop_scored: list[tuple[float, str]] = []
            # per-hop staging list — capped at _max_results before merging into _bfs_collected
            _hop_bfs_staged: list[tuple[int, float, str, RelevantFile]] = []

            for _seed_path, _dep_paths in _hop_dep_map.items():
                _seed_atype = (
                    classifications[_seed_path]["artifact_type"]
                    if _seed_path in classifications
                    else self._classify_changed_file(_seed_path)["artifact_type"]
                )
                # diff severity for original changed files only (hop-1 seeds);
                # hop-2+ seeds are dep files not in diff_severities → "unknown"
                _seed_severity = diff_severities.get(_seed_path, "unknown")
                for _dep_path in _dep_paths:
                    if _dep_path in _bfs_seen:
                        continue
                    _bfs_seen.add(_dep_path)

                    _dep_cls = self._classify_changed_file(_dep_path)
                    if _dep_cls["is_noise"]:
                        continue

                    _dep_atype = _dep_cls["artifact_type"]
                    _dep_module = _dep_cls["module"]

                    # Cross-module gating: if dep lives in a different domain module,
                    # only allow it if:
                    #   hop-1 AND dep_atype is explicitly in seed's _EXPANSION_TARGETS
                    # For hop-2+, cross-module deps are always excluded — transitives
                    # must stay within the changed modules to avoid system-wide explosion.
                    _is_cross_module = bool(_dep_module) and _dep_module not in affected_modules_set
                    if _is_cross_module:
                        _seed_expansion = _EXPANSION_TARGETS.get(_seed_atype, frozenset())
                        # security_change seeds are allowed to cross into the security chain
                        # even when their base expansion targets don't include those types
                        if _seed_severity == "security_change":
                            _seed_expansion = _seed_expansion | frozenset({"security", "spring_config", "config"})
                        if _hop_num >= 2 or _dep_atype not in _seed_expansion:
                            continue

                    _dep_score_base = _ARTIFACT_SCORE.get(_dep_atype, 0.45)
                    # score decays 30% per hop so transitives rank below direct dependents
                    # cross-module deps get additional 40% penalty so same-module files
                    # always rank higher in the per-hop cap
                    _cross_module_factor = 0.60 if _is_cross_module else 1.0
                    _dep_score = round(_dep_score_base * (0.70 ** _hop_num) * _cross_module_factor, 2)
                    _dep_role = _role_in_system(_dep_path, _dep_atype, _dep_path in ep_paths)

                    _dep_conf = _dep_cls.get("confidence", "low")
                    _why_str = _structured_why(
                        _dep_path, _dep_atype, _dep_module, _dep_role,
                        _dep_cls["risk_areas"], _dep_conf
                    )
                    _why_str += f" | pulled_by: hop-{_hop_num} import from {Path(_seed_path).name}"
                    _reason = (
                        f"hop-{_hop_num} import-dependent of {Path(_seed_path).name}"
                        f" ({_seed_atype})"
                    )
                    why[_dep_path] = _why_str
                    # Tests import production code but are not structural dependencies —
                    # exclude from graph, frontier, and bfs_collected entirely.
                    _is_test = _dep_atype == "test"
                    if not _is_test:
                        graph_edges.append({
                            "from": _seed_path, "to": _dep_path,
                            "edge_type": "import_dependency", "hop": _hop_num,
                        })
                        # field_change seeds don't propagate to hop-2+ frontier:
                        # a field-level change (getter, attribute) is collected at hop-1
                        # but its callers are not recursively expanded further
                        if _seed_severity != "field_change":
                            _hop_scored.append((_dep_score, _dep_path))
                        _hop_bfs_staged.append((_hop_num, _dep_score, _dep_path, RelevantFile(
                            path=_dep_path, role=_dep_role, score=_dep_score,
                            reason=_reason, why=_why_str,
                        )))

            # Per-hop cap: keep only the top-_max_results by score before merging.
            # Prevents a single high-fanout seed (e.g. User.java imported by every
            # controller) from flooding _bfs_collected and pushing out hop-2/3 results.
            _hop_bfs_staged.sort(key=lambda x: (-x[1], x[2]))
            _bfs_collected.extend(_hop_bfs_staged[:_max_results])

            # next frontier = top-N files by score from this hop
            _hop_scored.sort(key=lambda x: -x[0])
            _bfs_frontier = [p for _, p in _hop_scored[:_max_results]]

        # merge into relevant: closer hops first, then higher score; cap total at 18
        _bfs_collected.sort(key=lambda x: (x[0], -x[1], x[2]))
        _bfs_cap = sum(budget[0] for budget in _BFS_HOP_BUDGET)  # 8+6+4 = 18
        relevant.extend(rf for _, _, _, rf in _bfs_collected[:_bfs_cap])

        # Truncation guard: flag excess expansion — gap message added in Step 6.
        _EXPANSION_HARD_LIMIT = 40
        _expansion_truncated = len(relevant) > _EXPANSION_HARD_LIMIT
        if _expansion_truncated:
            relevant = relevant[:_EXPANSION_HARD_LIMIT]

        # ── Step 3d: per-file impact scores, change_type, system_impact ─────────
        # Downstream fanout: count graph edges originating from each changed file
        _downstream_count: dict[str, int] = {f: 0 for f in changed_files}
        for _edge in graph_edges:
            if _edge["from"] in _downstream_count:
                _downstream_count[_edge["from"]] += 1

        impact_score_per_file: dict[str, Any] = {}
        all_change_types: set[str] = set()
        for _path in changed_files:
            _cls = classifications[_path]
            _atype = _cls["artifact_type"]
            # Base change types from artifact classification
            _file_ctypes: set[str] = set(_ARTIFACT_CHANGE_TYPES.get(_atype, []))
            # Supplement with semantic diff evidence — behavioral_change ONLY from diff
            _diff_sev = diff_severities.get(_path, "unknown")
            _file_ctypes.update(_DIFF_SEVERITY_CHANGE_TYPES.get(_diff_sev, []))
            all_change_types.update(_file_ctypes)

            _base = _ARTIFACT_SCORE.get(_atype, 0.45)
            _sec_w = 0.20 if (_atype == "security" or "security" in _cls["risk_areas"]) else 0.0
            _fw = (
                0.15 if _atype in {"entrypoint", "spring_config"} else
                0.08 if _atype in {"security", "controller"} else
                0.0
            )
            _fanout = min(0.15, _downstream_count.get(_path, 0) * 0.05)
            _ctw = (
                0.10 if any(ct in ("behavioral_change", "security_change") for ct in _file_ctypes) else
                0.05 if any(ct in ("structural_change", "dependency_change") for ct in _file_ctypes) else
                0.02
            )
            _raw_score = round(min(1.0, _base * 0.50 + _sec_w + _fw + _fanout + _ctw), 3)
            _caller_count = _downstream_count.get(_path, 0)
            impact_score_per_file[_path] = {
                "_rank_score": _raw_score,  # internal ranking only — not a confidence claim
                "change_types": sorted(_file_ctypes),
                "diff_severity": _diff_sev,
                "evidence": {
                    "has_reverse_edges": _caller_count > 0,
                    "reverse_edge_count": _caller_count,
                    "has_route_diff": _diff_sev == "api_change",
                    "has_security_diff": _diff_sev == "security_change",
                    "has_wiring_evidence": _atype in {"spring_config", "security"} and _cls.get("confidence") == "high",
                },
            }

        _CT_ORDER = ["security_change", "behavioral_change", "structural_change",
                     "configuration_change", "dependency_change", "ui_change"]
        aggregate_change_type = [ct for ct in _CT_ORDER if ct in all_change_types]

        # system_impact: only evidence-backed subsystems.
        # application_layer → not emitted as subsystem (no architectural claim without evidence).
        # transaction_layer → removed: transaction_boundary no longer mapped to source/service.
        _IMPACT_AREA_TO_SUBSYSTEM = {
            "api_surface":          "api_layer",
            "security_layer":       "security_layer",
            "dependency_injection": "spring_di_layer",
            "persistence_layer":    "persistence_layer",
            "configuration":        "configuration_layer",
            "build_system":         "build_system",
            "ui_layer":             "ui_layer",
            "integration_layer":    "integration_layer",
            # application_layer intentionally omitted — no subsystem claim for unclassified files
        }
        _seen_subsys: set[str] = set()
        changed_subsystems: list[str] = []
        for _p, _cls in classifications.items():
            _p_atype = _cls["artifact_type"]
            # Only emit subsystem for evidence-backed classifications (not source/unclassified)
            if not _cls["is_noise"] and _p_atype not in ("source", "ide_noise"):
                _area = _classify_impact_area(_p, _cls["risk_areas"], _p_atype)
                _subsys = _IMPACT_AREA_TO_SUBSYSTEM.get(_area)
                if _subsys and _subsys not in _seen_subsys:
                    changed_subsystems.append(_subsys)
                    _seen_subsys.add(_subsys)

        # behavioral_changes: only emitted when graph evidence exists (import edges found).
        # Without reverse dependency graph, we cannot make traceable structural claims.
        # Also gated on semantic diff evidence (api_change/security_change) per file.
        _has_graph_ev = bool(graph_edges)
        behavioral_changes: list[str] = (
            [
                f"{Path(_p).name}: {_CHANGE_EFFECT.get(_cls['artifact_type'], 'application source (artifact role requires annotation inspection)')} [diff_severity={diff_severities.get(_p)}]"
                for _p, _cls in classifications.items()
                if not _cls["is_noise"]
                and diff_severities.get(_p, "unknown") in ("api_change", "security_change")
            ]
            if _has_graph_ev else []
        )

        def _runtime_impact(tc: dict[str, int]) -> list[str]:
            _ri: list[str] = []
            # Only emit claims that are evidence-backed (annotation-confirmed roles)
            if "entrypoint" in tc:
                _ri.append("Entrypoint-classified file modified — restart required before deploying to production")
            if "spring_config" in tc:
                _ri.append("Spring @Configuration-classified file modified — bean context rebuild required on restart")
            if "security" in tc:
                _ri.append("Security-classified file modified — inspect authentication and access control wiring")
            if "db_migration" in tc:
                _ri.append("Database schema migration pending — execute before deploying application")
            # service transactional claim: only if @Service annotation confirmed (not source/unclassified)
            _svc = tc.get("service", 0)
            if _svc >= 2:
                _ri.append(f"{_svc} @Service-annotated file(s) modified — verify business logic consistency")
            _repo = tc.get("repository", 0) + tc.get("mapper", 0)
            if _repo > 0:
                _ri.append(f"{_repo} persistence-classified component(s) modified — verify data access queries")
            if "build_manifest" in tc:
                _ri.append("Build manifest modified — dependency resolution required before compile")
            return _ri

        _max_hop = max((e["hop"] for e in graph_edges), default=0)
        dependency_graph_summary: dict = {
            "edges": graph_edges[:30],
            # propagation_depth is unknown when no graph edges exist — not 0
            "propagation_depth": _max_hop if graph_edges else None,
            "has_graph_evidence": bool(graph_edges),
        }

        # ── Step 4: impact summary ─────────────────────────────────────────────
        type_counts: dict[str, int] = {}
        all_risk_areas: set[str] = set()
        noise_count = 0
        for cls in classifications.values():
            t = cls["artifact_type"]
            type_counts[t] = type_counts.get(t, 0) + 1
            all_risk_areas.update(cls["risk_areas"])
            if cls["is_noise"]:
                noise_count += 1
        meaningful = len(changed_files) - noise_count

        _SUMMARY_LABELS: dict[str, str] = {
            "entrypoint":     "entrypoint(s)",
            "security":       "security file(s)",
            "controller":     "controller(s)",
            "service":        "service(s)",
            "repository":     "repository/repositories",
            "mapper":         "MyBatis mapper(s)",
            "spring_config":  "Spring config file(s)",
            "spring_profile": "Spring profile config(s)",
            "config":         "configuration file(s)",
            "build_manifest": "build manifest(s)",
            "db_migration":   "database migration(s)",
            "domain_model":   "domain model(s)",
            "dto":            "DTO(s)",
            "test":           "test file(s)",
            "source":         "source file(s)",
            "documentation":  "documentation file(s)",
        }

        if meaningful == 0:
            impact_summary = (
                f"{noise_count} IDE/tooling file(s) changed"
                " — no semantic impact on application logic"
            )
        else:
            _sev_rank = {"critical": 4, "high": 3, "medium": 2, "low": 1, "noise": 0}
            parts = []
            for atype, count in sorted(
                type_counts.items(),
                key=lambda kv: -_sev_rank.get(_ARTIFACT_IMPACT.get(kv[0], "medium"), 0),
            ):
                if atype == "ide_noise":
                    continue
                label = _SUMMARY_LABELS.get(atype, f"source file(s) ({atype})")
                parts.append(f"{count} {label}")
            impact_summary = "; ".join(parts) if parts else f"{meaningful} source file(s) changed"
            if all_risk_areas:
                impact_summary += f" — risk areas: {', '.join(sorted(all_risk_areas))}"
            if noise_count > 0:
                impact_summary += f" ({noise_count} IDE/tooling file(s) excluded)"

        # ── Step 5: risk_areas output list ─────────────────────────────────────
        risk_areas_out: list[dict[str, Any]] = sorted(
            [
                {
                    "area": area,
                    "severity": info["severity"],
                    "affected_files": sorted(info["files"])[:5],
                }
                for area, info in risk_acc.items()
            ],
            key=lambda x: (-_SEV_ORDER.index(x["severity"]), x["area"]),
        )

        # ── Build system_impact (needs risk_areas_out + type_counts) ─────────────
        system_impact = {
            "changed_subsystems": changed_subsystems,
            "behavioral_changes": behavioral_changes,
            "risk_areas": risk_areas_out,
            "runtime_impact": _runtime_impact(type_counts),
        }

        # ── Step 6: analysis gaps ──────────────────────────────────────────────
        _bfs_note = (
            f"BFS multi-hop import propagation repo-wide (propagation_depth={_max_hop})"
            if _max_hop >= 1
            else "no import-link propagation (no scannable changed files)"
        )
        analysis_gaps: list[str] = [
            f"Related file expansion: type-aware chain expansion + {_bfs_note} + module/directory heuristics",
        ]
        if _expansion_truncated:
            analysis_gaps.insert(0,
                f"truncated_dependency_graph: expansion exceeded {_EXPANSION_HARD_LIMIT} nodes"
                " — lower-priority files omitted. Narrow scope with --since <ref> for precision."
            )
        if noise_count > 0 and meaningful > 0:
            analysis_gaps.append(
                f"{noise_count} IDE/tooling file(s) in diff excluded from impact analysis"
            )
        elif noise_count > 0 and meaningful == 0:
            analysis_gaps.append(
                "All changed files are IDE/tooling — no actionable semantic impact detected"
            )
        low_confidence = [f for f, cls in classifications.items() if cls.get("confidence") == "low" and not cls["is_noise"]]
        if low_confidence:
            analysis_gaps.append(
                f"{len(low_confidence)} file(s) classified with low confidence"
                " (artifact type inferred from extension only)"
                " — consider adding stem patterns to _classify_changed_file: "
                + ", ".join(Path(f).name for f in low_confidence[:3])
            )
        if not affected_modules_set and any(not cls["is_noise"] for cls in classifications.values()):
            analysis_gaps.append(
                "DDD module/package structure not detected in changed paths"
                " — related file expansion uses directory proximity only"
            )

        return (
            relevant,
            impact_summary,
            sorted(affected_modules_set),
            risk_areas_out,
            why,
            analysis_gaps,
            system_impact,
            aggregate_change_type,
            dependency_graph_summary,
            impact_score_per_file,
        )

    def _resolve_git_baseline(self, since: Optional[str]) -> dict[str, Any]:
        """Resolve git baseline for delta diff using a 4-stage fallback chain.

        Resolution order when `since` is provided:
          1. exact local ref (git rev-parse --verify <since>)
          2. remote-tracking ref  (origin/<since>)
          3. symbolic ref         (git symbolic-ref refs/remotes/origin/HEAD)
          4. HEAD~1 fallback

        When `since` is None:
          1. uncommitted changes  (git diff --name-only --relative)
          2. HEAD~1 fallback

        Returns dict with keys:
            files: list[str]           — changed paths (empty = confirmed no changes)
            resolved_ref: str          — ref actually used for the diff
            resolution_path: str       — which strategy resolved it
            diff_validation_status: str — "valid_non_empty"|"valid_empty"|"invalid_ref"
            error: bool                — True only when ALL strategies failed
        """
        import subprocess

        def _run(*args: str, timeout: int = 5) -> tuple[bool, str]:
            try:
                r = subprocess.run(
                    ["git", *args], cwd=str(self.root),
                    capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=timeout,
                )
                return r.returncode == 0, (r.stdout or "").strip()
            except (subprocess.TimeoutExpired, FileNotFoundError):
                return False, ""

        def _verify(ref: str) -> bool:
            ok, _ = _run("rev-parse", "--verify", ref)
            return ok

        def _diff(ref: str) -> Optional[list[str]]:
            ok, out = _run("diff", "--name-only", "--relative", ref, "HEAD", timeout=10)
            if not ok:
                return None
            return [line.strip() for line in out.splitlines() if line.strip()]

        def _make(files: list[str], ref: str, path: str) -> dict[str, Any]:
            return {
                "files": files,
                "resolved_ref": ref,
                "resolution_path": path,
                "diff_validation_status": "valid_non_empty" if files else "valid_empty",
                "error": False,
            }

        if since:
            # Stage 1: exact local ref
            if _verify(since):
                files = _diff(since)
                if files is not None:
                    return _make(files, since, "exact_local_ref")

            # Stage 2: remote-tracking ref (origin/<since>)
            remote_ref = f"origin/{since}"
            if _verify(remote_ref):
                files = _diff(remote_ref)
                if files is not None:
                    return _make(files, remote_ref, "remote_tracking_ref")

            # Stage 3: symbolic ref (origin/HEAD → e.g. origin/main)
            ok, symref = _run("symbolic-ref", "refs/remotes/origin/HEAD")
            if ok and symref:
                short = symref.removeprefix("refs/remotes/")
                if _verify(short):
                    files = _diff(short)
                    if files is not None:
                        return _make(files, short, "symbolic_ref")

            # Stage 4: HEAD~1 fallback — original ref was invalid
            if _verify("HEAD~1"):
                files = _diff("HEAD~1")
                if files is not None:
                    return {
                        "files": files,
                        "resolved_ref": "HEAD~1",
                        "resolution_path": "head_minus_1_fallback",
                        "diff_validation_status": "invalid_ref",  # original ref unresolved
                        "error": False,
                    }

            # All stages failed
            return {
                "files": [],
                "resolved_ref": since,
                "resolution_path": "unresolvable",
                "diff_validation_status": "invalid_ref",
                "error": True,
            }

        else:
            # No since: uncommitted changes first
            ok, out = _run("diff", "--name-only", "--relative", timeout=10)
            if ok:
                files = [line.strip() for line in out.splitlines() if line.strip()]
                if files:
                    return _make(files, "HEAD", "uncommitted_changes")

            # HEAD~1 fallback
            if _verify("HEAD~1"):
                files = _diff("HEAD~1")
                if files is not None:
                    return _make(files or [], "HEAD~1", "head_minus_1_fallback")

            return {
                "files": [],
                "resolved_ref": "HEAD",
                "resolution_path": "unresolvable",
                "diff_validation_status": "invalid_ref",
                "error": True,
            }

    def _get_uncommitted_changed_files(self) -> list[str]:
        """Return files with uncommitted working-tree changes (unstaged only).

        Used by review-pr when no --since ref is given, so we don't confuse
        the last *committed* diff (HEAD~1 vs HEAD) with an actual PR diff.
        """
        import subprocess
        try:
            result = subprocess.run(
                ["git", "diff", "--name-only", "--relative"],
                cwd=str(self.root), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=10,
            )
            if result.returncode == 0:
                return [l.strip() for l in (result.stdout or "").splitlines() if l.strip()]
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        return []

    def _get_git_changed_files(self, since: Optional[str] = None) -> Optional[list[str]]:
        """Get files changed since a git ref (default: HEAD~1) relative to self.root.

        Returns None when `since` is explicitly provided but cannot be resolved —
        this is an error state, not "no changes". Callers must distinguish None
        (ref invalid) from [] (ref valid, no changes).

        Uses --relative so paths are relative to cwd (self.root), not the git repo
        root. This is critical for monorepos where self.root is a subpath of the
        git root and git diff would otherwise return prefixed paths that don't match
        the scanned file tree.
        """
        import subprocess

        if since is None:
            # No implicit HEAD~1 fallback — map to WORKTREE_UNSTAGED
            try:
                result = subprocess.run(
                    ["git", "diff", "--name-only", "--relative"],
                    cwd=str(self.root),
                    capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=10,
                )
                if result.returncode == 0:
                    return [ln.strip() for ln in (result.stdout or "").splitlines() if ln.strip()]
            except (subprocess.TimeoutExpired, FileNotFoundError):
                pass
            return []

        # Explicit since: GIT_SINCE_REF — committed range only, no silent fallback
        try:
            result = subprocess.run(
                ["git", "diff", "--name-only", "--relative", since, "HEAD"],
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
            return None  # ref doesn't exist — caller must fail fast
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return None

    def _get_available_refs(self, invalid_ref: str) -> tuple[list[str], Optional[str]]:
        """Return (available_branch_names, suggested_alternative) for error hints."""
        import subprocess
        branches: list[str] = []
        suggested: Optional[str] = None
        try:
            r = subprocess.run(
                ["git", "branch", "-a", "--format=%(refname:short)"],
                cwd=str(self.root),
                capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=5,
            )
            if r.returncode == 0:
                all_refs = [b.strip() for b in (r.stdout or "").splitlines() if b.strip()]
                branches = [b for b in all_refs if "HEAD" not in b][:10]
                ref_lower = invalid_ref.lower()
                if ref_lower == "master" and any(b.rstrip("/").endswith("main") for b in all_refs):
                    suggested = "main"
                elif ref_lower == "main" and any(b.rstrip("/").endswith("master") for b in all_refs):
                    suggested = "master"
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        return branches, suggested

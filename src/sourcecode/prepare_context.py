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
    HEAD_MINUS_1      = "HEAD_MINUS_1"        # git diff HEAD~1 HEAD (auto-fallback when tree clean)


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
    why: str = ""                # why this file matters for the specific task
    tier: Optional[str] = None   # fix-bug only: high | medium | low


@dataclass
class TaskOutput:
    task: str
    goal: str
    project_summary: Optional[str]
    architecture_summary: Optional[str]
    relevant_files: list[RelevantFile]
    suspected_areas: list[str]
    improvement_opportunities: list[str]
    test_gaps: list  # list[str] for non-Java; list[dict] for Java (has path/public_method_count/has_spring_annotations)
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
    symptom_explain: Optional[dict] = None                         # fix-bug: structured evidence breakdown
    symptom_hint: Optional[str] = None                             # fix-bug: redirect hint when term not found in this module
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
    warnings: list[dict] = field(default_factory=list)             # structured warnings (REF_NOT_FOUND, etc.)
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
    # compact_base fields — enriched in all prepare-context tasks (Fix #1)
    security_surface: Optional[dict] = None
    mybatis: Optional[dict] = None
    transactional_boundaries: Optional[dict] = None
    spring_profiles_info: Optional[dict] = None
    angular_analysis: Optional[dict] = None
    deployment_risks: list[str] = field(default_factory=list)
    deployment: Optional[dict] = None
    entry_points_structured: Optional[dict] = None
    # P0-2: fast-mode truncation transparency
    truncated: bool = False
    truncated_reason: Optional[str] = None
    # generate-tests: count of existing test files found (complements untested_sources)
    existing_test_count: Optional[int] = None


@dataclass
class CanonicalAnalysisIR:
    """Shared intermediate representation produced by _build_delta_impact.

    Both delta and review-pr derive their task-specific output from this IR.
    Never recalculate logic per command — render views from this single object.
    """
    relevant_files: list[RelevantFile]
    impact_summary: str
    affected_modules: list[str]
    risk_areas: list[dict]
    why_these_files: dict[str, str]
    analysis_gaps: list[str]
    system_impact: dict
    change_type: list[str]
    dependency_graph_summary: dict
    impact_score_per_file: dict


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
    "controller":     "HTTP handler layer (Spring @RestController / JAX-RS @Path resource)",
    "service":        "business logic layer (Spring @Service / CDI @ApplicationScoped bean)",
    "repository":     "data access layer (persistence queries / ORM / CDI store)",
    "mapper":         "SQL-object mapping layer (MyBatis mapper / query template)",
    "security":       "security component (authentication / access control configuration)",
    "spring_config":  "framework configuration class (Spring @Configuration / Quarkus @QuarkusApplication)",
    "spring_profile": "environment-specific configuration override (Spring profile / Quarkus profile)",
    "config":         "configuration file (application properties / environment values)",
    "build_manifest": "build manifest (dependency and plugin configuration)",
    "db_migration":   "database schema migration (DDL change pending execution)",
    "domain_model":   "domain entity (@Entity / CDI model / value object)",
    "dto":            "data transfer object (serialization contract)",
    "test":           "test file (no production code modified)",
    "documentation":  "documentation file (no runtime impact)",
    "ide_noise":      "IDE/tooling artifact (no application impact)",
    "source":         "application source file (role could not be confirmed from code signals)",
    # Angular-specific artifact types (detected before Java/Spring heuristics)
    "ng_component":   "UI presentation layer (Angular @Component)",
    "ng_pipe":        "data transformation layer (Angular @Pipe)",
    "ng_directive":   "DOM behavior layer (Angular @Directive)",
    "ng_guard":       "navigation guard (Angular CanActivate / CanActivateFn)",
    "ng_interceptor": "HTTP middleware layer (Angular HttpInterceptor)",
    "ng_resolver":    "data pre-fetch layer (Angular Resolve)",
    "ng_service":     "Angular injectable service (@Injectable)",
    "ng_module":      "Angular feature module (@NgModule)",
}

# Maps frontend symptom keywords → backend terms likely to contain the root cause.
# Used to boost service/interceptor files when the symptom is UI-only.
_FRONTEND_SYMPTOM_MAP: dict[str, list[str]] = {
    "spinner":    ["loading", "setloading", "finalize", "httpinterceptor", "interceptor", "service"],
    "loading":    ["loading", "setloading", "finalize", "httpinterceptor", "interceptor", "service"],
    "login":      ["authcontroller", "securityconfig", "filterconfig", "jwtfilter", "auth", "authentication"],
    "logout":     ["authcontroller", "securityconfig", "jwtfilter", "auth", "session"],
    "dropdown":   ["getmapping", "findall", "obtenertodos", "listall", "findby"],
    "modal":      ["controller", "getmapping", "findby", "search"],
    "popup":      ["controller", "getmapping", "findby", "search"],
    "table":      ["paginated", "findby", "search", "getmapping", "listall"],
    "grid":       ["paginated", "findby", "search", "getmapping"],
    "button":     ["postmapping", "putmapping", "deletemapping", "controller", "service"],
    "form":       ["postmapping", "putmapping", "controller", "service", "dto"],
    # session-related symptoms (Spanish + English)
    "sesion":     ["httpsession", "sessionmanager", "sessionservice", "sessionrepository", "sessionfactory", "authentication"],
    "sesiones":   ["httpsession", "sessionmanager", "sessionservice", "sessionrepository", "sessionfactory", "authentication"],
    "session":    ["httpsession", "sessionmanager", "sessionservice", "sessionrepository", "sessionfactory", "authentication"],
    # worker/assignment domain terms (common in RRHH/HR systems)
    "trabajador": ["trabajador", "empleado", "worker", "asignacion", "trabajadordao", "trabajadorservice"],
}

# Generic words that add noise when used as symptom keywords in large repos.
# "token" and "user" are too ubiquitous in auth systems to be useful alone.
_SYMPTOM_STOP_WORDS: frozenset[str] = frozenset({
    "fails", "fail", "failed", "failure",
    "not", "for", "with", "when", "that", "the", "and", "but",
    "are", "has", "had", "have", "was", "were",
    "get", "set", "can", "does", "did", "should", "would", "could",
    "null", "none", "empty", "invalid", "incorrect", "wrong", "missing",
    "error", "issue", "problem", "bug",
    "from", "into", "via", "due", "also", "after", "before",
    "slow", "fast", "new", "old",
})

# Repo-scale threshold: above this file count, use stricter injection logic.
_LARGE_REPO_THRESHOLD = 500

MAX_FILES_FAST = 2000  # above this threshold --fast uses git-index-only mode


def _count_files_bounded(root: "Path", limit: int = MAX_FILES_FAST + 1) -> int:
    """Count files under root, stopping early once limit reached (O(n) fast exit)."""
    import os as _os
    count = 0
    for _, _, fnames in _os.walk(str(root)):
        count += len(fnames)
        if count >= limit:
            return count
    return count


def _git_changed_files_fast(root: "Path") -> list[str]:
    """Return files reported by git diff --name-only HEAD (for fast-mode scanning)."""
    import subprocess as _sp
    try:
        r = _sp.run(
            ["git", "diff", "--name-only", "HEAD"],
            capture_output=True, text=True, cwd=str(root), timeout=5,
        )
        return [f.strip() for f in r.stdout.splitlines() if f.strip()]
    except Exception:
        return []


def _build_analysis_scope(
    *,
    task_name: str,
    since: Optional[str],
    delta_baseline: dict,
    pr_scope_source: str,
    pr_uncommitted_files: list,
) -> dict:
    """Build analysis_scope metadata from actual git resolution — never hardcoded."""
    if task_name == "review-pr":
        sources = pr_scope_source.split(",") if pr_scope_source else []
        # Derive git_equivalent_command from sources
        cmds: list[str] = []
        if DiffSourceType.GIT_RANGE.value in sources or DiffSourceType.GIT_SINCE_REF.value in sources:
            cmds.append(f"git diff --name-only {since} HEAD")
        if DiffSourceType.HEAD_MINUS_1.value in sources:
            cmds.append("git diff --name-only HEAD~1 HEAD")
        if DiffSourceType.WORKTREE_UNSTAGED.value in sources:
            cmds.append("git diff --name-only")
        if DiffSourceType.WORKTREE_STAGED.value in sources:
            cmds.append("git diff --name-only --cached")
        git_cmd = " + ".join(cmds) if cmds else "git diff --name-only"
        return {
            "sources_used": sources,
            "git_equivalent_command": git_cmd,
            "includes_uncommitted": bool(pr_uncommitted_files),
            "git_resolution": {
                "source_of_truth": "RANGE" if since else (
                    "HEAD" if DiffSourceType.HEAD_MINUS_1.value in sources else "WORKTREE"
                ),
                "resolved_diff_strategy": pr_scope_source,
                "commit_count_analyzed": 1 if DiffSourceType.HEAD_MINUS_1.value in sources else (
                    0 if not since else None
                ),
            },
        }

    # delta
    rpath = delta_baseline.get("resolution_path", "")
    rref = delta_baseline.get("resolved_ref", "HEAD")

    _COMMITTED_PATHS = {"exact_local_ref", "remote_tracking_ref", "symbolic_ref"}
    if rpath in _COMMITTED_PATHS:
        sources = [DiffSourceType.GIT_SINCE_REF.value]
        git_cmd = f"git diff --name-only {rref} HEAD"
        source_of_truth = "RANGE"
        includes_uncommitted = False
    elif rpath == "head_minus_1_fallback":
        sources = [DiffSourceType.HEAD_MINUS_1.value]
        git_cmd = "git diff --name-only HEAD~1 HEAD"
        source_of_truth = "HEAD"
        includes_uncommitted = False
    elif rpath == "uncommitted_staged":
        sources = [DiffSourceType.WORKTREE_STAGED.value]
        git_cmd = "git diff --name-only --cached"
        source_of_truth = "STAGED"
        includes_uncommitted = True
    else:  # uncommitted_unstaged, uncommitted_changes, no_changes_confirmed
        sources = [DiffSourceType.WORKTREE_UNSTAGED.value]
        git_cmd = "git diff --name-only"
        source_of_truth = "WORKTREE"
        includes_uncommitted = True

    return {
        "sources_used": sources,
        "git_equivalent_command": git_cmd,
        "includes_uncommitted": includes_uncommitted,
        "git_resolution": {
            "source_of_truth": source_of_truth,
            "resolved_diff_strategy": rpath or "unknown",
            "commit_count_analyzed": 1 if rpath == "head_minus_1_fallback" else (
                0 if rpath == "no_changes_confirmed" else None
            ),
        },
    }


class TaskContextBuilder:
    def __init__(self, root: Path) -> None:
        self.root = root

    # ------------------------------------------------------------------
    # RIS fast-path: serve onboard/explain from warm snapshot (<50ms)
    # ------------------------------------------------------------------

    def _try_ris_fast_path(self, task_name: str, spec: "TaskSpec") -> Optional[TaskOutput]:
        """Return TaskOutput from a warm RIS without running the full scan.

        Only activated for onboard/explain when git HEAD matches stored RIS.
        Falls through (returns None) on any error or cache miss.
        """
        try:
            import subprocess as _sp
            from sourcecode.ris import load_ris as _lris
            _ris = _lris(self.root)
            if _ris is None or not _ris.compact_summary:
                return None
            _r = _sp.run(
                ["git", "-C", str(self.root), "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, timeout=2,
            )
            if _r.returncode != 0 or _r.stdout.strip() != _ris.git_head:
                return None

            compact = _ris.compact_summary
            struct = _ris.structural_map

            entry_points = struct.get("entrypoints") or compact.get("entry_points") or []
            controllers = struct.get("controllers") or []
            services = struct.get("services") or []

            _seen: set = set()
            relevant: list[RelevantFile] = []
            for fp in entry_points[:10]:
                if isinstance(fp, str) and fp not in _seen:
                    _seen.add(fp)
                    relevant.append(RelevantFile(path=fp, role="entrypoint", score=1.0, reason="entry_point", why="Primary entry point"))
            for fp in controllers[:8]:
                if isinstance(fp, str) and fp not in _seen:
                    _seen.add(fp)
                    relevant.append(RelevantFile(path=fp, role="source", score=0.9, reason="controller", why="Controller"))
            for fp in services[:8]:
                if isinstance(fp, str) and fp not in _seen:
                    _seen.add(fp)
                    relevant.append(RelevantFile(path=fp, role="source", score=0.8, reason="service", why="Service layer"))

            # compact_summary uses "analysis_gaps" (list[dict]) not "gaps" (list[str])
            _raw_gaps = compact.get("gaps") or compact.get("analysis_gaps") or []
            _gap_strs: list[str] = [
                g.get("reason", str(g)) if isinstance(g, dict) else str(g)
                for g in _raw_gaps
                if g
            ]

            return TaskOutput(
                task=task_name,
                goal=spec.goal,
                project_summary=compact.get("project_summary") or compact.get("summary"),
                architecture_summary=compact.get("architecture_summary"),
                relevant_files=relevant,
                suspected_areas=[],
                improvement_opportunities=[],
                test_gaps=[],
                key_dependencies=compact.get("key_dependencies") or [],
                code_notes_summary=None,
                limitations=[],
                confidence=compact.get("confidence") or compact.get("confidence_summary") or "high",
                gaps=_gap_strs,
            )
        except Exception:
            return None

    def build(self, task_name: str, *, since: Optional[str] = None, symptom: Optional[str] = None, fast: bool = False, include_config: bool = False, all_gaps: bool = False) -> TaskOutput:
        if task_name not in TASKS:
            raise ValueError(
                f"Unknown task '{task_name}'. Available: {', '.join(TASKS)}"
            )
        spec = TASKS[task_name]

        # ── RIS fast-path (onboard / explain only) ────────────────────────────
        if task_name in ("onboard", "explain") and not fast:
            _warm = self._try_ris_fast_path(task_name, spec)
            if _warm is not None:
                return _warm

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
        elif fast and task_name == "onboard":
            # Onboard fast: always use shallow scan so manifests and entry points
            # are discoverable — git-changed-only mode would return only dirty files
            # (e.g. .idea/vcs.xml) which yields no useful entry points (BUG-3).
            scanner = AdaptiveScanner(self.root, base_depth=2)
            file_tree = scanner.scan_tree()
            manifests = scanner.find_manifests()
            all_paths = [p.replace("\\", "/") for p in flatten_file_tree(file_tree)]
        elif fast and _count_files_bounded(self.root) > MAX_FILES_FAST:
            # Fast mode on large repo: git-index-only — only scan git-changed files.
            # Skips full AdaptiveScanner traversal which takes 35s+ on 7k+ file repos.
            _git_files = _git_changed_files_fast(self.root)
            _git_files = [f for f in _git_files if (self.root / f).exists()]
            if not _git_files:
                # Fallback: use a shallow scan (depth 2) to get some context
                scanner = AdaptiveScanner(self.root, base_depth=2)
                file_tree = scanner.scan_tree()
                manifests = scanner.find_manifests()
                all_paths = [p.replace("\\", "/") for p in flatten_file_tree(file_tree)]
            else:
                file_tree = {}
                all_paths = _git_files
                # Keep manifests from shallow pre-scan
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

        # ── 3b. Compact-base enrichment (Fix #1) ───────────────────────────
        # Enrich sm with Java-specific fields — same as main CLI pipeline
        _java_stack_cb = next((s for s in stacks if s.stack == "java"), None)
        if _java_stack_cb is not None:
            sm.language_version = getattr(_java_stack_cb, "language_version", None) or None
            sm.spring_profiles = getattr(_java_stack_cb, "spring_profiles", []) or []
            sm.app_server_hint = getattr(_java_stack_cb, "app_server_hint", None) or None
            sm.packaging = getattr(_java_stack_cb, "packaging", None) or None

        # Import serializer helpers — same functions used by --compact
        from sourcecode.serializer import (
            _security_surface_from_eps as _cb_sec_fn,
            _mybatis_pairing as _cb_mybatis_fn,
            _transactional_summary as _cb_trans_fn,
            _spring_profiles_context as _cb_spring_fn,
            _angular_analysis as _cb_angular_fn,
            _project_deployment_risks as _cb_deploy_risks_fn,
            _bootstrap_structured as _cb_bootstrap_fn,
            _spring_boot_version as _cb_sbver_fn,
            _jndi_datasources as _cb_jndi_fn,
        )

        _cb_security_surface = _cb_sec_fn(entry_points, root=self.root, file_paths=all_paths)
        _cb_mybatis = _cb_mybatis_fn(sm)
        _cb_transactional = _cb_trans_fn(sm)
        _cb_spring_profiles = _cb_spring_fn(sm)
        _cb_angular = _cb_angular_fn(sm)
        _cb_deploy_risks = _cb_deploy_risks_fn(sm)
        _cb_bootstrap = _cb_bootstrap_fn(entry_points)

        _cb_sb_ver = _cb_sbver_fn(sm)
        _cb_deployment: Optional[dict] = None
        _cb_packaging = getattr(sm, "packaging", None)
        _cb_app_server = getattr(sm, "app_server_hint", None)
        if _cb_sb_ver or _cb_packaging or _cb_app_server:
            _cb_deployment = {}
            if _cb_sb_ver:
                _cb_deployment["spring_boot_version"] = _cb_sb_ver
            if _cb_packaging:
                _cb_deployment["packaging"] = _cb_packaging
            if _cb_app_server:
                _cb_deployment["app_server_hint"] = _cb_app_server
        _cb_jndi = _cb_jndi_fn(sm)
        if _cb_jndi:
            _cb_deployment = _cb_deployment or {}
            _cb_deployment["jndi_datasources"] = _cb_jndi

        # ── 4. Dependencies ────────────────────────────────────────────────
        key_dependencies: list[dict[str, Any]] = []
        limitations: list[str] = []

        if spec.enable_dependencies:
            from dataclasses import asdict
            from sourcecode.dependency_analyzer import DependencyAnalyzer

            dep_records, dep_summary = DependencyAnalyzer().analyze(self.root)
            primary_eco = stacks[0].stack if stacks else ""
            _direct_raw = [
                d for d in dep_records
                if d.scope != "transitive" and d.source in {"manifest", "lockfile"}
                and (d.role or "unknown") in {"runtime", "parsing", "serialization", "observability", "infra"}
                and d.scope not in {"dev"}
            ]
            _seen_dep: set[str] = set()
            direct = []
            for _d in _direct_raw:
                if _d.name not in _seen_dep:
                    _seen_dep.add(_d.name)
                    direct.append(_d)
            # Rank by framework centrality: core infra (ORM, Spring) > serialization > other.
            # Penalise vendored tooling (closure-compiler, shaded utilities) so that
            # Hibernate/JPA/Solr appear before minor build-time dependencies.
            _HIGH_SIGNAL_FRAGMENTS = (
                "hibernate", "jpa", "spring-core", "spring-context", "spring-web",
                "spring-boot", "spring-security", "spring-data",
                "solr", "elasticsearch", "kafka", "redis",
                "jackson", "gson",
                "mybatis", "druid", "datasource",
                "tomcat", "undertow", "netty",
                "slf4j", "logback", "log4j",
            )
            _LOW_SIGNAL_FRAGMENTS = (
                "closure-compiler", "closure-library",
                "google-closure", "rhino",
                "guava-gwt",
            )

            def _dep_rank(d: Any) -> tuple:
                art = (d.name or "").lower()
                eco_match = 0 if d.ecosystem == primary_eco else 1
                is_high = any(frag in art for frag in _HIGH_SIGNAL_FRAGMENTS)
                is_low = any(frag in art for frag in _LOW_SIGNAL_FRAGMENTS)
                infra_score = 0 if is_high else (2 if is_low else 1)
                return (eco_match, infra_score, art)

            direct.sort(key=_dep_rank)
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

        if spec.enable_code_notes and not fast:
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
                            _loc = len((self.root / _p).read_text(encoding="utf-8", errors="replace").splitlines())
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
        _recent_commits_for_symptom: list = []
        try:
            from sourcecode.git_analyzer import GitAnalyzer
            _gc = GitAnalyzer().analyze(self.root, depth=30, days=90)
            _bad = {"no_git_repo", "git_not_found", "git_timeout"}
            if _gc and not (_bad & set(_gc.limitations)):
                git_hotspots = {h.file: h.commit_count for h in _gc.change_hotspots}
                _recent_commits_for_symptom = list(_gc.recent_commits)
                if _gc.uncommitted_changes:
                    _uc = _gc.uncommitted_changes
                    uncommitted_files = set(_uc.staged) | set(_uc.unstaged)
        except Exception:
            pass

        # ── 5c. Delta: resolve git-changed files BEFORE ranking ───────────────
        # For delta task, relevant_files must rank only files changed in the
        # specified git range, not the full repo by generic entrypoint scoring.
        _delta_files: Optional[set[str]] = None
        _delta_baseline: dict = {}  # resolution metadata threaded into analysis_scope
        if task_name == "delta":
            _baseline = self._resolve_git_baseline(since=since)
            _delta_baseline = _baseline
            if _baseline["error"]:
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
                    resolution_path=_baseline.get("resolution_path"),
                    diff_validation_status=_baseline.get("diff_validation_status"),
                )
            _delta_raw = _baseline["files"]
            if _delta_raw:
                _delta_files = set(_delta_raw)

        # ── 5d. review-pr: set _delta_files from pre-resolved git scope ──────────
        # No-git and invalid-ref cases were already handled in step 0 (early returns).
        if task_name == "review-pr":
            if not _pr_scope_files:
                # Distinguish: no_staged_changes (CI, no --since) vs no_diff (empty range)
                if _pr_scope_source == "no_staged_changes":
                    _no_diff_msg = (
                        "No --since ref provided and no staged/uncommitted changes found. "
                        "Provide --since <ref> to specify the base commit for the diff."
                    )
                    return TaskOutput(
                        task="review-pr", goal=spec.goal,
                        project_summary=None, architecture_summary=None,
                        relevant_files=[], suspected_areas=[],
                        improvement_opportunities=[], test_gaps=[],
                        key_dependencies=[], code_notes_summary=None,
                        limitations=[], confidence="low",
                        error_code="no_diff_source",
                        error_message=_no_diff_msg,
                        error_hints=[
                            "Add --since <ref> to specify a base commit.",
                            "Common values: --since HEAD~1 (last commit)  |  --since origin/main  |  --since main",
                            "If reviewing uncommitted changes: stage them first (git add), then run without --since.",
                        ],
                        gaps=[_no_diff_msg],
                        ci_decision="no_diff_source",
                        scope_source=_pr_scope_source,
                        scope_files=[],
                        repo_root=str(_pr_git_root),
                    )
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

        # Delta and review-pr share CanonicalAnalysisIR — computed once, rendered per task.
        _ir: Optional[CanonicalAnalysisIR] = None
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
            _ir = self._build_delta_impact(
                changed_files=_delta_changed_list,
                all_paths=all_paths,
                entry_points=entry_points,
                since=since,
            )
            relevant_files = _ir.relevant_files
            _delta_impact_summary = _ir.impact_summary
            _delta_affected_modules = _ir.affected_modules
            _delta_risk_areas = _ir.risk_areas
            _delta_why = _ir.why_these_files
            _delta_analysis_gaps = _ir.analysis_gaps
            _delta_system_impact = _ir.system_impact
            _delta_change_type = _ir.change_type
            _delta_dep_graph_summary = _ir.dependency_graph_summary
            _delta_impact_score_per_file = _ir.impact_score_per_file
        else:
            relevant_files = self._rank_files(
                task_name, spec, all_paths, entry_set, test_set,
                monorepo_packages=sm.monorepo_packages if sm.monorepo_packages else None,
                git_hotspots=git_hotspots,
                uncommitted_files=uncommitted_files,
                code_notes=cn_notes_for_ranking if cn_notes_for_ranking else None,
                delta_files=None,
                symptom=symptom if task_name == "fix-bug" else None,
            )

        # ── Fast-mode fallback: never return empty relevant_files when source files exist ──
        # When --fast is active on a large repo, all_paths may be restricted to a handful of
        # changed/noise files that all get filtered out by _rank_files. Inject fallback signals:
        # 1. detected entry points (already computed, zero I/O cost)
        # 2. recently committed files (git log -10 --name-only)
        # 3. files matching symptom keywords in path (when fix-bug + --symptom)
        if fast and not relevant_files and task_name not in ("delta", "review-pr"):
            import subprocess as _sp_fb
            _fb_seen: set[str] = set()
            _fb_candidates: list[RelevantFile] = []

            # 1. Entry points from detection
            for _ep in entry_points:
                _ep_path = _ep.path.replace("\\", "/")
                if _ep_path not in _fb_seen and (self.root / _ep_path).exists():
                    _fb_candidates.append(RelevantFile(
                        path=_ep_path,
                        role="entrypoint",
                        score=0.5,
                        reason="fast-mode fallback: detected entry point",
                        why="entry_point signal from manifest/annotation detection",
                    ))
                    _fb_seen.add(_ep_path)

            # 2. Recently committed files (git log -10 --name-only)
            try:
                _gl_r = _sp_fb.run(
                    ["git", "log", "--name-only", "--pretty=format:", "-10"],
                    capture_output=True, text=True, cwd=str(self.root), timeout=5,
                )
                for _gl_f in _gl_r.stdout.splitlines():
                    _gl_f = _gl_f.strip().replace("\\", "/")
                    if (not _gl_f or _gl_f in _fb_seen):
                        continue
                    if Path(_gl_f).suffix.lower() not in _ALL_EXTENSIONS:
                        continue
                    if not (self.root / _gl_f).exists():
                        continue
                    _fb_candidates.append(RelevantFile(
                        path=_gl_f,
                        role="source",
                        score=0.3,
                        reason="fast-mode fallback: recently committed file (git log -10)",
                        why="recent commit history signal",
                    ))
                    _fb_seen.add(_gl_f)
            except Exception:
                pass

            # 3. Symptom keyword path matches (fix-bug only)
            if task_name == "fix-bug" and symptom:
                import re as _re_fb
                _fb_kws = [w.lower() for w in _re_fb.split(r"[\s\W]+", symptom) if len(w) > 2]
                for _fb_p in all_paths:
                    if _fb_p in _fb_seen:
                        continue
                    if Path(_fb_p).suffix.lower() not in _ALL_EXTENSIONS:
                        continue
                    if any(kw in _fb_p.lower() for kw in _fb_kws):
                        _fb_candidates.append(RelevantFile(
                            path=_fb_p,
                            role="source",
                            score=0.2,
                            reason=f"fast-mode fallback: path matches symptom ({symptom!r})",
                            why="symptom keyword in file path",
                        ))
                        _fb_seen.add(_fb_p)

            relevant_files = _fb_candidates[:20]

        # ── IC-006: fix-bug suspected_areas — recompute from ranked files + bug notes ──
        # relevant_files is now ranked by RankingEngine (git churn, fan_in, centrality, notes).
        # suspected_areas should reflect that ranking, not raw comment count.
        if task_name == "fix-bug" and relevant_files:
            _bug_kinds = {"FIXME", "BUG", "HACK", "XXX"}
            _bug_counts: dict[str, int] = {}
            for _note in cn_notes_for_ranking:
                if _note.kind in _bug_kinds:
                    _bug_counts[_note.path] = _bug_counts.get(_note.path, 0) + 1

            # Primary: top-ranked RelevantFile objects (dataclass, use .path/.reason)
            _ranked_paths = [rf.path for rf in relevant_files[:30]]
            _primary: list[str] = []
            for rf in relevant_files[:30]:
                p = rf.path
                if not p:
                    continue
                n = _bug_counts.get(p, 0)
                reason_str = str(rf.reason) if rf.reason else ""
                note_str = f" ({n} bug annotation{'s' if n > 1 else ''})" if n else ""
                _primary.append(f"{p}{note_str}" + (f" — {reason_str}" if reason_str else ""))
                if len(_primary) >= 5:
                    break

            # Secondary: remaining high-note files not already in primary
            _ranked_set = set(_ranked_paths[:len(_primary)])
            _secondary = [
                f"{p} ({n} annotation{'s' if n > 1 else ''})"
                for p, n in sorted(_bug_counts.items(), key=lambda x: -x[1])
                if p not in _ranked_set
            ][:3]

            if _primary or _secondary:
                suspected_areas = _primary + _secondary

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
        # Only default these for non-review-pr tasks — review-pr already set them
        # at line 714 via _get_pr_scope_files. Re-initializing here would shadow
        # those values and make _committed_set/_uncommitted_set always empty.
        if task_name != "review-pr":
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
                    "epistemic_level": "STRUCTURAL SIGNAL",
                    "basis": "files classified as security artifact type (name/annotation pattern)",
                    "risk_level": "high",
                    "risk_epistemic_level": "INFERRED (LOW CONFIDENCE)",
                }
            if _transaction_files:
                _pr_transactional_impact = {
                    "affected_transactions": _transaction_files,
                    "epistemic_level": "STRUCTURAL SIGNAL",
                    "basis": "files classified as service/business_logic artifact type",
                    "risk": "possible transaction boundary change",
                    "risk_epistemic_level": "INFERRED (LOW CONFIDENCE)",
                }
            if _config_files:
                _pr_configuration_impact = {
                    "changed_configs": _config_files,
                    "epistemic_level": "FACT",
                    "basis": "files present in diff",
                }

            # Test coverage risk scoped to changed source files only
            _changed_src = [
                f for f in sorted(_delta_files or set())
                if not self._is_test(f) and self._is_source(f)
            ]
            # BUG-03 fix: normalize test stems by stripping Test/IT/Tests suffixes.
            # Without this, RedirectUtils.java is not matched to RedirectUtilsTest.java
            # → false positive "no test coverage" on a file that IS tested.
            _test_stems = {
                Path(p).stem
                .removesuffix("Tests").removesuffix("Test").removesuffix("IT")
                for p in test_set
            } | {Path(p).stem for p in test_set}
            _untested_changed = [f for f in _changed_src if Path(f).stem not in _test_stems]
            _test_risk_level = (
                "high" if len(_untested_changed) > 3
                else "medium" if _untested_changed
                else "low"
            )
            _pr_test_coverage_risk = {
                "changed_files_without_tests": _untested_changed[:10],
                "risk_level": _test_risk_level,
                "epistemic_level": "INFERRED (LOW CONFIDENCE)",
                "basis": f"{len(_untested_changed)} changed source files have no matching test file by stem",
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
                _ev_complete = {k: v for k, v in _impact_entry.get("evidence", {}).items() if v}
                _entry: dict = {
                    "path": _f,
                    "diff_source": _diff_source,
                    "role": _role_obj,
                    "artifact_type": _f_atype,
                    "evidence": _evidence,
                    "change_effect": {
                        "statement": _ARTIFACT_CHANGE_EFFECT.get(_f_atype, "application source file (role could not be confirmed)"),
                        "classification_method": _role_basis,
                        "epistemic_level": (
                            "STRUCTURAL SIGNAL" if _has_code_ev
                            else "INFERRED (LOW CONFIDENCE)"
                        ),
                    },
                }
                if _ev_complete:
                    _entry["evidence_completeness"] = _ev_complete
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
        symptom_explain: Optional[dict] = None
        symptom_hint: Optional[str] = None
        if task_name == "fix-bug" and symptom:
            import re as _re
            _camel_expanded = _re.sub(r'([a-z])([A-Z])', r'\1 \2', symptom)
            _camel_expanded = _re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', _camel_expanded)
            symptom_keywords = [
                w.lower() for w in _re.split(r"[\s\W]+", _camel_expanded)
                if len(w) > 2 and w.lower() not in _SYMPTOM_STOP_WORDS
            ]
            if symptom_keywords:
                # Pre-compile combined keyword pattern for fast content scanning
                _kw_re = _re.compile(
                    "|".join(_re.escape(kw) for kw in symptom_keywords),
                    _re.IGNORECASE,
                )

                # Structured evidence collectors for symptom_explain
                _sx_direct_path: list[str] = []
                _sx_content: list[str] = []
                _sx_commits: list[dict] = []
                _sx_synonyms: list[str] = []
                _sx_boosts: list[dict] = []
                _sx_graph_expanded: list[str] = []

                # Pass 1: surface code notes whose text contains any keyword
                _note_matched_paths: dict[str, int] = {}  # path → count of matching notes
                for _n in cn_notes_for_ranking:
                    _text = (getattr(_n, "text", "") or "").lower()
                    if _kw_re.search(_text):
                        _np = getattr(_n, "path", "")
                        related_notes.append({
                            "kind": getattr(_n, "kind", ""),
                            "path": _np,
                            "line": getattr(_n, "line", None),
                            "text": getattr(_n, "text", ""),
                        })
                        _note_matched_paths[_np] = _note_matched_paths.get(_np, 0) + 1

                # Pass 2: build commit message index — cap at 60 most-recent commits.
                # Files touched in commits whose message matches a symptom keyword get
                # a strong recency signal. Primary signal for domain keywords ("sesiones")
                # that appear in commit messages but not file paths.
                _commit_file_hits: dict[str, int] = {}  # path → n matching commits
                _commits_scanned = _recent_commits_for_symptom[:60]
                for _cr in _commits_scanned:
                    _msg_lower = (_cr.message or "").lower()
                    if _kw_re.search(_msg_lower):
                        for _cf in (_cr.files_changed or []):
                            _cf_norm = _cf.replace("\\", "/")
                            _commit_file_hits[_cf_norm] = _commit_file_hits.get(_cf_norm, 0) + 1
                        _sx_commits.append({
                            "message": (_cr.message or "")[:80],
                            "files": list((_cr.files_changed or [])[:5]),
                        })

                # Pass 3: inject files from commit index not yet in candidate pool
                _existing_paths = {rf.path for rf in relevant_files}
                for _cp, _nhits in _commit_file_hits.items():
                    if _cp in _existing_paths:
                        continue
                    if Path(_cp).suffix.lower() not in _ALL_EXTENSIONS:
                        continue
                    _ci_score = round(min(0.5 + 0.15 * _nhits, 0.85), 2)
                    relevant_files.append(RelevantFile(
                        path=_cp,
                        role="symptom_match",
                        score=_ci_score,
                        reason=f"commit message matches symptom ({_nhits} commit{'s' if _nhits > 1 else ''})",
                        why=f"symptom commit-index: {', '.join(symptom_keywords)}",
                    ))
                    _existing_paths.add(_cp)

                # Scale-awareness: large repos need wider scan and stricter injection.
                _is_large_repo = len(all_paths) > _LARGE_REPO_THRESHOLD

                # Pass 4: inject files whose path matches symptom keywords.
                # CamelCase-expand the filename stem so "OfflineSessionLoader" matches
                # the keyword "offline" even without an explicit directory separator.
                # Large repos: cap per-keyword injections so a common term like
                # "authentication" (50+ path matches in an IAM repo) cannot flood the
                # candidate list and push specific terms like "ldap" out of the budget.
                _p4_dirs_of_injected: set[str] = set()  # directories of high-score injects
                _P4_KW_CAP = 15  # max path-injections per keyword in large repos
                _p4_kw_counts: dict[str, int] = {}
                for _p in all_paths:
                    if _p in _existing_paths:
                        continue
                    if Path(_p).suffix.lower() not in _ALL_EXTENSIONS:
                        continue
                    _p_lower = _p.lower()
                    # CamelCase-expand the stem and append to the search string so
                    # "OfflineSessionLoader" → "offline session loader" can match
                    # individual keyword tokens beyond what substring search finds.
                    _stem_raw = Path(_p).stem
                    _stem_exp = _re.sub(r'([a-z])([A-Z])', r'\1 \2', _stem_raw)
                    _stem_exp = _re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', _stem_exp).lower()
                    _p_search = _p_lower + " " + _stem_exp
                    _matching_kws = [kw for kw in symptom_keywords if kw in _p_search]
                    if not _matching_kws:
                        continue
                    # In large repos, skip keywords already at cap; keep file only if at
                    # least one keyword still has quota (multi-kw matches exhaust each
                    # keyword's quota independently so specific terms survive longer).
                    if _is_large_repo:
                        _matching_kws = [
                            kw for kw in _matching_kws
                            if _p4_kw_counts.get(kw, 0) < _P4_KW_CAP
                        ]
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
                    _sx_direct_path.append(_p)
                    if _is_large_repo:
                        for _kw in _matching_kws:
                            _p4_kw_counts[_kw] = _p4_kw_counts.get(_kw, 0) + 1
                    if _injected_score >= 0.7:
                        _p4_dirs_of_injected.add(str(Path(_p).parent))

                # Pass 4b: grep-based injection for frontend→backend synonym terms.
                # Runs parallel grep for each backend term to find files not yet in
                # the candidate pool (e.g. AkitaBaseService containing setLoading).
                _src_exts = frozenset({".java", ".py", ".ts", ".js", ".kt", ".go"})
                _frontend_kws = [kw for kw in symptom_keywords if kw in _FRONTEND_SYMPTOM_MAP]
                # Fix 5: In large repos, skip frontend→backend synonym grep for keywords
                # that already have direct path matches — those are backend terms (e.g.
                # "login" in an IAM repo) that don't need UI→service-layer translation.
                # Prevents "authentication" grep flooding keycloak with SAML adapter files.
                if _is_large_repo and _frontend_kws:
                    _frontend_kws = [
                        kw for kw in _frontend_kws
                        if not any(kw in p.lower() for p in _sx_direct_path)
                    ]
                _backend_terms_set: list[str] = []
                if _frontend_kws:
                    _bt: list[str] = []
                    for _fkw in _frontend_kws:
                        _bt.extend(_FRONTEND_SYMPTOM_MAP[_fkw])
                    _backend_terms_set = list(dict.fromkeys(_bt))

                if _backend_terms_set and not fast:
                    import subprocess as _sp2
                    _grepped: set[str] = set()
                    for _term in _backend_terms_set[:6]:
                        try:
                            _gr = _sp2.run(
                                ["grep", "-r", "-l", "-i", _term,
                                 "--include=*.ts", "--include=*.java",
                                 str(self.root)],
                                capture_output=True, text=True, timeout=4,
                            )
                            for _line in _gr.stdout.splitlines():
                                try:
                                    _rel = str(Path(_line.strip()).relative_to(self.root)).replace("\\", "/")
                                    _grepped.add(_rel)
                                except ValueError:
                                    pass
                        except Exception:
                            pass
                    _existing_paths_now = {rf.path for rf in relevant_files}
                    for _gf in sorted(_grepped):
                        if _gf in _existing_paths_now:
                            continue
                        if Path(_gf).suffix.lower() not in _src_exts:
                            continue
                        relevant_files.append(RelevantFile(
                            path=_gf,
                            role="symptom_match",
                            score=0.45,
                            reason="content contains backend symptom term (grep)",
                            why=f"grep injection: {', '.join(_backend_terms_set[:3])}",
                        ))
                        _existing_paths_now.add(_gf)

                # Pass 4c: subsystem co-location — inject sibling files from the same
                # directories as high-score (≥0.7) path-matched files. This catches
                # architecturally adjacent classes that don't mention symptom keywords
                # in their own name (e.g. InfinispanOfflineSessionCacheEntryLifespan…
                # siblings in the same infinispan/ package).
                # Large repos: cap total co-location injections so that a keyword
                # matching many directories doesn't flood the candidate list.
                if _is_large_repo and _p4_dirs_of_injected:
                    _coloc_existing = {rf.path for rf in relevant_files}
                    _P4C_CAP = 30
                    _coloc_count = 0
                    for _cp in all_paths:
                        if _coloc_count >= _P4C_CAP:
                            break
                        if _cp in _coloc_existing:
                            continue
                        if Path(_cp).suffix.lower() not in _src_exts:
                            continue
                        if str(Path(_cp).parent) in _p4_dirs_of_injected:
                            relevant_files.append(RelevantFile(
                                path=_cp,
                                role="symptom_match",
                                score=0.55,
                                reason="subsystem co-location: same directory as symptom-matched file",
                                why="directory proximity injection",
                            ))
                            _coloc_existing.add(_cp)
                            _coloc_count += 1

                # Sort before content scan so top candidates get read first.
                # In large repos: prioritise symptom_match files within each score band
                # so that subsystem-relevant files are content-scanned before generic
                # structural files at the same score.
                if _is_large_repo:
                    relevant_files = sorted(
                        relevant_files,
                        key=lambda rf: (-rf.score, 0 if rf.role == "symptom_match" else 1),
                    )
                    _CONTENT_SCAN_LIMIT = 150
                else:
                    relevant_files = sorted(relevant_files, key=lambda rf: -rf.score)
                    _CONTENT_SCAN_LIMIT = 80
                _scan_candidates = relevant_files[:_CONTENT_SCAN_LIMIT]
                _no_scan_candidates = relevant_files[_CONTENT_SCAN_LIMIT:]

                _boosted: list[RelevantFile] = []
                _raw_signals: dict[str, float] = {}  # uncapped accumulated signal per file
                _scanned_body: dict[str, str] = {}  # cache for graph expansion (Pass 5)
                for _rf in _scan_candidates:
                    _extra = 0.0
                    _extra_syn = 0.0
                    _reasons: list[str] = []
                    _p_lower = _rf.path.lower()

                    # Commit message boost: +0.25/commit, cap +0.40
                    _c_hits = _commit_file_hits.get(_rf.path, 0)
                    if _c_hits:
                        _cb = min(0.40, _c_hits * 0.25)
                        _extra += _cb
                        _reasons.append(f"commit-msg symptom ×{_c_hits} (+{_cb:.2f})")
                        _sx_boosts.append({"type": "commit_message", "value": round(_cb, 3), "evidence": _rf.path})

                    # Code note boost: +0.20/note, cap +0.30
                    _n_hits = _note_matched_paths.get(_rf.path, 0)
                    if _n_hits:
                        _nb = min(0.30, _n_hits * 0.20)
                        _extra += _nb
                        _reasons.append(f"note-match symptom ×{_n_hits} (+{_nb:.2f})")
                        _sx_boosts.append({"type": "code_note", "value": round(_nb, 3), "evidence": _rf.path})

                    # Path keyword boost for pre-existing candidates
                    _path_kws = [kw for kw in symptom_keywords if kw in _p_lower]
                    if _path_kws and _rf.role != "symptom_match":
                        _pb = 0.20 * len(_path_kws)
                        _extra += _pb
                        _reasons.append(f"path-kw symptom ({', '.join(_path_kws)}) (+{_pb:.2f})")
                        _sx_boosts.append({"type": "path_match", "value": round(_pb, 3), "evidence": _rf.path})

                    # Single file read — covers both content scan and synonym scan
                    _body_lower = ""
                    if Path(_rf.path).suffix.lower() in _src_exts:
                        try:
                            _raw_body = (self.root / _rf.path).read_text(
                                encoding="utf-8", errors="replace"
                            )[:12000]  # ~300 lines avg
                            _scanned_body[_rf.path] = _raw_body  # cache for Pass 5
                            _body_lower = _raw_body.lower()
                        except OSError:
                            pass

                    # Content scan boost: +0.05/hit, cap +0.50
                    if _body_lower:
                        _hits = len(_kw_re.findall(_body_lower))
                        _content_b = min(0.50, _hits * 0.05)
                        if _content_b > 0:
                            _extra += _content_b
                            _reasons.append(f"content-match symptom ×{_hits} (+{_content_b:.2f})")
                            _sx_boosts.append({"type": "content_match", "value": round(_content_b, 3), "evidence": _rf.path})
                            _sx_content.append(_rf.path)

                    # Synonym scan (Pass 6): only apply when file has prior non-synonym
                    # evidence (commit hit, note, path, or content) — prevents boosting
                    # arbitrary interceptors/configs with no other signal.
                    if _backend_terms_set and _body_lower:
                        _prior_boost = _extra  # boost accumulated before synonym
                        if _prior_boost >= 0.10:  # min threshold: must have real prior signal
                            _hits_syn = sum(_body_lower.count(t) for t in _backend_terms_set)
                            _extra_syn = min(0.20, _hits_syn * 0.02)
                            if _extra_syn > 0:
                                _sx_synonyms.append(_rf.path)
                                _sx_boosts.append({"type": "synonym_match", "value": round(_extra_syn, 3), "evidence": _rf.path})

                    _total_extra = _extra + _extra_syn
                    _new_reason = _rf.reason
                    if _reasons:
                        _syn_suffix = f", synonym-match backend (+{_extra_syn:.2f})" if _extra_syn > 0 else ""
                        _new_reason = _rf.reason + ", " + ", ".join(_reasons) + _syn_suffix
                    elif _extra_syn > 0:
                        _new_reason = _rf.reason + f", synonym-match backend (+{_extra_syn:.2f})"

                    _raw_signal = _rf.score + _total_extra  # uncapped for ranking
                    _raw_signals[_rf.path] = _raw_signal
                    _final_score = round(min(_raw_signal, 1.0), 2)
                    _boosted.append(RelevantFile(
                        path=_rf.path,
                        role=_rf.role,
                        score=_final_score,
                        reason=_new_reason,
                        why=_rf.why,
                    ))

                # Sort by uncapped raw signal so files with more accumulated evidence
                # (path matches + content hits + commit matches) rank above files that
                # merely cap at the same display score of 1.0.
                # _raw_signals holds each file's full sum before the display cap.
                # Files not content-scanned (_no_scan_candidates) use their base score.
                relevant_files = sorted(
                    _boosted + _no_scan_candidates,
                    key=lambda rf: -_raw_signals.get(rf.path, rf.score),
                )

                # Pass 5: reverse graph expansion from high-score seed nodes.
                # Identifies which source files in the repo REFERENCE the seed
                # classes (imports, implements, extends, field declarations).
                # This is a reverse-import lookup: for seed class "UserProvider",
                # it finds JpaUserProvider / DefaultUserSessionProvider which import
                # UserProvider — even though those files don't contain symptom
                # keywords in their own path.
                # Seeds include any high-score file (not just symptom_match role)
                # so that files found by _rank_files class-name matching also expand.
                if not fast:
                    import re as _re_gx
                    _GX_SEED_THRESH = 0.5
                    _GX_EXPAND_CAP = 30
                    _GX_HOP_DECAY = 0.6

                    # Collect seed class names from high-score results
                    _gx_seed_stems: dict[str, float] = {}  # stem → score
                    for _gx_rf in relevant_files:
                        if _gx_rf.score < _GX_SEED_THRESH:
                            continue
                        if Path(_gx_rf.path).suffix.lower() not in _src_exts:
                            continue
                        _gx_stem = Path(_gx_rf.path).stem
                        _gx_seed_stems[_gx_stem] = max(
                            _gx_seed_stems.get(_gx_stem, 0.0), _gx_rf.score
                        )

                    if _gx_seed_stems:
                        # Compile per-stem word-boundary patterns for fast matching
                        import re as _re_gx2
                        _gx_patterns: dict[str, Any] = {
                            stem: _re_gx2.compile(rf'\b{_re_gx2.escape(stem)}\b')
                            for stem in _gx_seed_stems
                        }

                        _gx_existing = {rf.path for rf in relevant_files}
                        _gx_new: list[RelevantFile] = []
                        _gx_added: set[str] = set()

                        # Candidates: non-test source files not yet in results.
                        # Small repos: scan all; large repos: use pre-scanned content only.
                        # Test files are excluded (fix-bug focuses on production code).
                        if _is_large_repo:
                            _gx_candidates = [
                                p for p in _scanned_body
                                if p not in _gx_existing and not self._is_test(p)
                            ]
                        else:
                            _gx_candidates = [
                                p for p in all_paths
                                if p not in _gx_existing
                                and Path(p).suffix.lower() in _src_exts
                                and not self._is_test(p)
                            ]

                        for _gx_cand in _gx_candidates:
                            if len(_gx_new) >= _GX_EXPAND_CAP:
                                break
                            if _gx_cand in _gx_added:
                                continue

                            # Use cached content or read fresh (small repos only)
                            _gx_body = _scanned_body.get(_gx_cand)
                            if _gx_body is None:
                                if _is_large_repo:
                                    continue  # never do fresh reads on large repos in Pass 5
                                try:
                                    _gx_body = (self.root / _gx_cand).read_text(
                                        encoding="utf-8", errors="replace"
                                    )[:8000]
                                except OSError:
                                    continue

                            # Reverse lookup: does this file reference any seed class?
                            for _gx_stem, _gx_seed_score in _gx_seed_stems.items():
                                if _gx_patterns[_gx_stem].search(_gx_body):
                                    _hop1_score = round(
                                        min(_gx_seed_score * _GX_HOP_DECAY, 0.85), 2
                                    )
                                    _gx_new.append(RelevantFile(
                                        path=_gx_cand,
                                        role="symptom_match",
                                        score=_hop1_score,
                                        reason=(
                                            f"graph_expansion: references {_gx_stem} "
                                            f"(1-hop reverse import)"
                                        ),
                                        why=f"graph_expansion: 1 hop from {_gx_stem}",
                                    ))
                                    _gx_added.add(_gx_cand)
                                    _sx_graph_expanded.append(_gx_cand)
                                    break  # one match per candidate is enough

                        if _gx_new:
                            relevant_files = sorted(
                                relevant_files + _gx_new,
                                key=lambda rf: -_raw_signals.get(rf.path, rf.score),
                            )

                # Fix 2: Cap output for large repos to stay within agent context budgets.
                # Raw signal sort above ensures highest-signal files survive the cut.
                if _is_large_repo and len(relevant_files) > 40:
                    relevant_files = relevant_files[:40]

                # Synonym note (only when synonyms actually fired)
                if _frontend_kws and _sx_synonyms:
                    symptom_note = (
                        f"Frontend concept detected ({', '.join(_frontend_kws)}). "
                        "Backend service-layer files boosted by synonym match "
                        "[INFERRED (LOW CONFIDENCE) — pattern heuristic, not structural proof]."
                    )

                # Confidence: based on richest signal type present
                if _commit_file_hits:
                    _sx_confidence = "HIGH"
                elif _sx_direct_path or _sx_content:
                    _sx_confidence = "MEDIUM"
                else:
                    _sx_confidence = "LOW"

                symptom_explain = {
                    "keywords": symptom_keywords,
                    "confidence": _sx_confidence,
                    "direct_path_matches": _sx_direct_path[:10],
                    "content_matches": _sx_content[:10],
                    "commit_matches": _sx_commits[:10],
                    "synonym_matches": _sx_synonyms[:10],
                    "graph_expansion": _sx_graph_expanded[:10],
                    "boosts": _sx_boosts[:30],
                    "final_boost": round(
                        sum(b["value"] for b in _sx_boosts), 3
                    ),
                }

                # BUG #4: LOW confidence + 0 content matches → clear suspected_areas,
                # emit actionable redirect instead of unrelated files.
                if _sx_confidence == "LOW" and not _sx_content:
                    suspected_areas = []
                    _is_fe_term = any(kw in _FRONTEND_SYMPTOM_MAP for kw in symptom_keywords)
                    _root_name = self.root.name
                    if _is_fe_term:
                        _fe_redirect = (
                            f"Term {symptom!r} not found in sources under {_root_name!r}. "
                            f"This appears to be a frontend symptom. "
                            f"Try: prepare-context fix-bug . --symptom {symptom!r} "
                            f"(monorepo root) or target a frontend sub-project directly."
                        )
                    else:
                        _fe_redirect = (
                            f"Term {symptom!r} not found in sources under {_root_name!r}. "
                            f"Verify the spelling or try a related term. "
                            f"If this is a frontend symptom, run against the frontend sub-project."
                        )
                    symptom_hint = _fe_redirect

        # ── 7. Test gaps (generate-tests only) ────────────────────────────
        test_gaps: list = []
        if task_name == "generate-tests" and not fast:
            if _is_java:
                # Java-aware algorithm (Fix #2): find Service/RestController/Repository/Mapper
                # files with no matching test pair in src/test/**
                _JAVA_TARGET_SUFFIXES = (
                    "Service.java", "RestController.java",
                    "Repository.java", "Mapper.java",
                )
                # Build set of test stems (FooTest → Foo, FooIT → Foo, etc.)
                _java_test_stems: set[str] = set()
                for _tp in all_paths:
                    if not _tp.endswith(".java"):
                        continue
                    if not self._is_test(_tp):
                        continue
                    _ts = Path(_tp).stem
                    for _suf in ("Test", "IT", "Tests", "Spec"):
                        if _ts.endswith(_suf):
                            _ts = _ts[: -len(_suf)]
                            break
                    if _ts.startswith("Test") and len(_ts) > 4 and _ts[4].isupper():
                        _ts = _ts[4:]
                    _java_test_stems.add(_ts)

                _java_candidates: list[dict] = []
                for _p in all_paths:
                    if not any(_p.endswith(_s) for _s in _JAVA_TARGET_SUFFIXES):
                        continue
                    if self._is_test(_p):
                        continue
                    _pnorm = _p.replace("\\", "/")
                    if "src/main/resources" in _pnorm or "target/" in _pnorm:
                        continue
                    if Path(_p).stem in _java_test_stems:
                        continue
                    _pub_count = 0
                    _ann_count = 0
                    try:
                        _content = (self.root / _p).read_text(
                            encoding="utf-8", errors="replace"
                        )[:16000]
                        _pub_count = _content.count("public ")
                        _ann_count = (
                            # Spring MVC / Spring Boot
                            _content.count("@Transactional")
                            + _content.count("@RequestMapping")
                            + _content.count("@GetMapping")
                            + _content.count("@PostMapping")
                            + _content.count("@PutMapping")
                            + _content.count("@DeleteMapping")
                            # JAX-RS
                            + _content.count("@Path")
                            + _content.count("@GET")
                            + _content.count("@POST")
                            + _content.count("@PUT")
                            + _content.count("@DELETE")
                            + _content.count("@PATCH")
                            # CDI / Jakarta EE
                            + _content.count("@ApplicationScoped")
                            + _content.count("@RequestScoped")
                            + _content.count("@Singleton")
                        )
                    except OSError:
                        pass
                    _java_candidates.append({
                        "path": _p,
                        "public_method_count": _pub_count,
                        "has_framework_annotations": _ann_count > 0,
                        "_rank": _pub_count + _ann_count * 2,
                    })

                _java_candidates.sort(
                    key=lambda x: -(x["public_method_count"] * (1.5 if x["has_framework_annotations"] else 1.0))
                )
                _top = _java_candidates if all_gaps else _java_candidates[:20]
                test_gaps = [
                    {
                        "path": c["path"],
                        "public_method_count": c["public_method_count"],
                        "has_framework_annotations": c["has_framework_annotations"],
                        "rank_score": round(c["public_method_count"] * (1.5 if c["has_framework_annotations"] else 1.0), 1),
                    }
                    for c in _top
                ]
            else:
                # Non-Java algorithm (unchanged)
                def _normalize_test_stem(stem: str) -> str:
                    if stem.endswith("Tests"):
                        return stem[:-5]
                    if stem.endswith("Test"):
                        return stem[:-4]
                    if stem.startswith("Test") and len(stem) > 4 and stem[4].isupper():
                        return stem[4:]
                    return stem.removeprefix("test_").removesuffix("_test")

                _CONFIG_EXCLUDE_PATTERNS = (
                    ".eslintrc", ".prettierrc", "eslint.config",
                    "karma.conf", "jest.config", "babel.config",
                    "webpack.config", "vite.config", "rollup.config",
                    "tsconfig", "angular.json", ".claude/",
                )

                def _is_config_file(p: str) -> bool:
                    name = Path(p).name.lower()
                    norm = p.replace("\\", "/")
                    return any(pat in name or pat in norm for pat in _CONFIG_EXCLUDE_PATTERNS)

                test_stems = {_normalize_test_stem(Path(p).stem) for p in test_set}
                untested = [
                    p for p in source_set
                    if Path(p).stem not in test_stems
                    and not any(pen in p for pen in spec.ranking_penalties)
                    and (include_config or not _is_config_file(p))
                ]
                untested.sort(key=lambda p: (len(p.split("/")), p))
                test_gaps = untested[:15]

        # P0-2: fast mode truncation transparency for generate-tests.
        # When --fast is active the test-gap discovery block is skipped entirely,
        # so test_gaps stays []. Without a signal the receiver interprets [] as
        # "no gaps", which is incorrect. Emit explicit truncation metadata and
        # downgrade confidence so the agent knows the analysis is incomplete.
        _fast_truncated = fast and task_name == "generate-tests"
        _fast_truncated_reason = "fast mode skips test gap discovery" if _fast_truncated else None
        if _fast_truncated:
            import sys as _sys_warn
            _sys_warn.stderr.write(
                "[warn] prepare-context generate-tests --fast: test gap discovery skipped. "
                "Output will contain truncated=true and confidence=low.\n"
            )
            _sys_warn.stderr.flush()

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
        # P0-2: fast-mode truncation overrides confidence to signal incomplete analysis
        if _fast_truncated:
            confidence = "low"
        _has_mybatis = any(
            f.name == "MyBatis"
            for s in stacks
            for f in getattr(s, "frameworks", [])
        )
        if task_name in ("delta", "review-pr"):
            # Use delta-specific gaps; ConfidenceAnalyzer gaps are about full-repo
            # detection quality and are not meaningful for an incremental diff.
            gaps = _delta_analysis_gaps
            if _mybatis_warning and _has_mybatis:
                gaps.append(_mybatis_warning["reason"])
        else:
            gaps = [g.reason for g in analysis_gaps]
            if _mybatis_warning and _has_mybatis:
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
            # For delta: _delta_files already resolved via _resolve_git_baseline — no second git call.
            # For review-pr: _get_git_changed_files fallback still valid as last resort.
            changed_files = sorted(_delta_files) if _delta_files else (
                [] if task_name == "delta" else (self._get_git_changed_files(since=since) or [])
            )
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
            symptom_explain=symptom_explain if task_name == "fix-bug" else None,
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
            analysis_scope=_build_analysis_scope(
                task_name=task_name,
                since=since,
                delta_baseline=_delta_baseline,
                pr_scope_source=_pr_scope_source,
                pr_uncommitted_files=_pr_uncommitted_files,
            ) if task_name in ("delta", "review-pr") else {},
            resolved_since_ref=_delta_baseline.get("resolved_ref") if task_name == "delta" else None,
            resolution_path=_delta_baseline.get("resolution_path") if task_name == "delta" else None,
            diff_validation_status=_delta_baseline.get("diff_validation_status") if task_name == "delta" else None,
            warnings=_delta_baseline.get("warnings", []) if task_name == "delta" else [],
            symptom_hint=symptom_hint if task_name == "fix-bug" else None,
            # compact_base fields (Fix #1) — superset of --compact for all tasks
            security_surface=_cb_security_surface,
            mybatis=_cb_mybatis,
            transactional_boundaries=_cb_transactional,
            spring_profiles_info=_cb_spring_profiles,
            angular_analysis=_cb_angular,
            deployment_risks=_cb_deploy_risks,
            deployment=_cb_deployment,
            entry_points_structured=_cb_bootstrap,
            # P0-2: fast-mode truncation transparency
            truncated=_fast_truncated,
            truncated_reason=_fast_truncated_reason,
            # generate-tests: count of test files found alongside untested_sources
            existing_test_count=len(test_set) if task_name == "generate-tests" else None,
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
        symptom: Optional[str] = None,
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

        # Per-file note counts — feeds code_note_count into RankingEngine for all tasks.
        # RankingEngine is the sole ranking source; no ad-hoc annotation boost outside it.
        _note_counts: dict[str, int] = {}
        for _n in (code_notes or []):
            _np = getattr(_n, "path", "")
            if _np:
                _note_counts[_np] = _note_counts.get(_np, 0) + 1

        # Pre-compute fix-bug signals (used only when task_name == "fix-bug")
        _dominant_stack = ""
        _recently_changed_stacks: set[str] = set()
        # Query-aware signals extracted from symptom (class names, exception types, tokens)
        _symptom_class_names: set[str] = set()    # CamelCase class names
        _symptom_exception_types: set[str] = set() # *Exception / *Error tokens
        _symptom_tokens: set[str] = set()          # all lowercase tokens

        if task_name == "fix-bug":

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

            # Extract structured signals from symptom text for AND-weighted ranking
            if symptom:
                import re as _re_bug
                _camel_re = _re_bug.compile(r'\b([A-Z][a-zA-Z0-9]+)\b')
                for _tok in _camel_re.findall(symptom):
                    if _tok.endswith(("Exception", "Error", "Throwable")):
                        _symptom_exception_types.add(_tok)
                    else:
                        _symptom_class_names.add(_tok)
                _symptom_tokens = {
                    w.lower() for w in _re_bug.split(r'[\s\W]+', symptom)
                    if len(w) > 2 and w.lower() not in _SYMPTOM_STOP_WORDS
                }

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

            # Structural + git signals from unified engine (task-weighted).
            # code_note_count routes annotation density through RankingEngine — single source.
            fs = engine.score(
                path,
                is_entrypoint=(path in runtime_entry_set),
                git_churn=_hotspots.get(path, 0),
                max_churn=_max_churn,
                is_changed=(path in _uncommitted),
                code_note_count=_note_counts.get(path, 0),
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

                # ── Query-aware AND-weighted signals (symptom-derived) ──
                # These intentionally outweigh git-recency signals so that
                # OrderServiceImpl.java ranks top-3 regardless of churn history.
                if _symptom_class_names or _symptom_exception_types:
                    _stem = Path(path).stem
                    _stem_lower = _stem.lower()
                    _matched_class = next(
                        (c for c in _symptom_class_names if _stem_lower == c.lower()),
                        None,
                    )
                    _matched_exc = next(
                        (e for e in _symptom_exception_types if _stem_lower == e.lower()),
                        None,
                    )
                    _impl_match = next(
                        (c for c in _symptom_class_names
                         if _stem_lower in (c.lower() + "impl", c.lower() + "service",
                                            c.lower() + "serviceimpl", c.lower() + "helper")),
                        None,
                    )
                    if _matched_class:
                        content_boost += 3.0
                        _why_parts.append(f"exact class match: {_stem} (+3.0)")
                    elif _matched_exc:
                        content_boost += 2.0
                        _why_parts.append(f"exception class match: {_stem} (+2.0)")
                    elif _impl_match:
                        content_boost += 2.5
                        _why_parts.append(f"class impl match: {_stem} (+2.5)")
                    else:
                        # Symbol appears anywhere in path (package adjacency)
                        _path_class_hit = next(
                            (c for c in _symptom_class_names if c.lower() in path_lower),
                            None,
                        )
                        if _path_class_hit:
                            content_boost += 1.0
                            _why_parts.append(f"symbol in path: {_path_class_hit} (+1.0)")
                        elif any(e.lower() in path_lower for e in _symptom_exception_types):
                            content_boost += 0.8
                            _why_parts.append("exception type in path (+0.8)")

                # AND-weighted token intersection — multiple matching tokens >> single.
                # CamelCase-expand the filename stem so "OfflineSessionLoader" contributes
                # "offline", "session", "loader" as individual tokens beyond what the raw
                # path splitting yields. This lets multi-word symptoms match class names.
                if _symptom_tokens:
                    _path_parts = set(path_lower.replace("/", " ").replace(".", " ").replace("_", " ").split())
                    _stem_cc = Path(path).stem
                    _stem_cc_exp = _re_bug.sub(r'([a-z])([A-Z])', r'\1 \2', _stem_cc)
                    _stem_cc_exp = _re_bug.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', _stem_cc_exp).lower()
                    _path_parts.update(_stem_cc_exp.split())
                    _intersection = _symptom_tokens & _path_parts
                    _n_match = len(_intersection)
                    if _n_match >= 3:
                        _tok_boost = min(1.2, _n_match * 0.25)
                        content_boost += _tok_boost
                        _why_parts.append(f"token AND match ({_n_match} terms: {sorted(_intersection)[:3]}) (+{_tok_boost:.2f})")
                    elif _n_match == 2:
                        content_boost += 0.4
                        _why_parts.append(f"token AND match (2 terms: {sorted(_intersection)}) (+0.40)")
                    # Single-token match: no boost — avoids OR explosion

                # ── Git / annotation signals ──
                _note_ct = _note_counts.get(path, 0)
                if _note_ct > 0:
                    _why_parts.append(f"annotation density ({_note_ct} FIXME/BUG/HACK notes)")
                if path in _uncommitted:
                    content_boost += 0.40
                    _why_parts.append("uncommitted change (+0.40)")
                _recency = min(0.30, _hotspots.get(path, 0) * 0.05)
                if _recency > 0:
                    content_boost += _recency
                    _why_parts.append(f"recent commits (+{_recency:.2f})")
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
                "fix-bug": 30,  # hard cap — prevents token explosion on large repos
                "onboard": max(15, min(25, _repo_size // 150)),
                "explain": max(10, min(20, _repo_size // 200)),
                "generate-tests": max(20, min(35, _repo_size // 100)),
                "refactor": max(15, min(30, _repo_size // 120)),
            }
            _budget = _task_budget.get(task_name, 15)
            _selected = list(_ctx.select_subgraph(_ns, contracts=[], budget=_budget, min_score=0.15))
            _rf_map = {path: rf for _, path, rf in scored}

            # Bug #3: onboard must cover ≥3 arch layers (controllers/services/domain/repositories).
            if task_name == "onboard":
                def _arch_layer(p: str) -> str:
                    n = Path(p).name.lower()
                    is_java = n.endswith(".java")
                    # HTTP handler layer: Spring MVC controllers AND JAX-RS resources/endpoints
                    # Restrict "resource"/"endpoint" to .java files to avoid matching
                    # Maven's src/main/resources/ directory or XML resource files.
                    if "controller" in n:
                        return "controllers"
                    if is_java and ("resource" in n or "endpoint" in n or "restadapter" in n):
                        return "controllers"
                    # Data access layer: Spring repos, DAOs, JDBC, JPA stores
                    if "repository" in n or "mapper" in n or "dao" in n or "store" in n:
                        return "repositories"
                    # Business logic: Spring @Service, CDI providers, factories
                    if "service" in n:
                        return "services"
                    if is_java and ("provider" in n or "factory" in n or "manager" in n):
                        return "services"
                    pn = p.replace("\\", "/")
                    if "entity" in n or "/entity/" in pn or "/domain/" in pn or "/model/" in pn:
                        return "domain"
                    return "other"

                _REQUIRED = {"controllers", "services", "repositories", "domain"}
                _covered = {_arch_layer(p) for p in _selected} & _REQUIRED
                _missing = _REQUIRED - _covered
                if len(_covered) < 3 and _missing:
                    _sel_set = set(_selected)
                    # First pass: inject from already-scored files
                    for _, _p, _ in sorted(scored, key=lambda x: -x[0]):
                        if len(_covered) >= 3:
                            break
                        if _p in _sel_set or _p not in _rf_map:
                            continue
                        _layer = _arch_layer(_p)
                        if _layer in _missing:
                            _selected.append(_p)
                            _sel_set.add(_p)
                            _covered.add(_layer)
                            _missing.discard(_layer)
                    # Second pass: fallback scan of all_paths when scored files
                    # don't cover enough layers (e.g. all Java files scored ≤ 0
                    # due to auxiliary/example package detection).
                    if len(_covered) < 3 and _missing:
                        _NON_TEST = ("/test/", "/tests/", "/spec/")
                        for _p in all_paths:
                            if len(_covered) >= 3:
                                break
                            if _p in _sel_set:
                                continue
                            if any(s in _p.replace("\\", "/") for s in _NON_TEST):
                                continue
                            _layer = _arch_layer(_p)
                            if _layer not in _missing:
                                continue
                            _rf_map[_p] = RelevantFile(
                                path=_p,
                                role="source",
                                score=0.1,
                                reason="layer coverage (onboard)",
                            )
                            _selected.append(_p)
                            _sel_set.add(_p)
                            _covered.add(_layer)
                            _missing.discard(_layer)

            result = [_rf_map[p] for p in _selected if p in _rf_map]

            # Assign fix-bug tiers based on raw score (pre-normalised total)
            if task_name == "fix-bug":
                _score_lookup = {path: total for total, path, _ in scored}
                for _rf in result:
                    _s = _score_lookup.get(_rf.path, 0.0)
                    if _s >= 4.0:
                        _rf.tier = "high"
                    elif _s >= 1.5:
                        _rf.tier = "medium"
                    else:
                        _rf.tier = "low"

            return result
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
          no since     → uncommitted: WORKTREE_UNSTAGED + WORKTREE_STAGED
          no since + clean tree → committed: HEAD_MINUS_1 (auto-fallback)
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

        # ── FIX-P1: no --since + clean working tree → no_staged_changes signal ──
        # Previously: silently fell back to HEAD~1 diff, masking a missing --since.
        # Now: emit "no_staged_changes" scope so the caller can return no_diff_source
        # (exit 1) and prompt the user to provide --since.
        # Rationale: in CI, the tree is always clean; without --since there is no
        # meaningful diff to review. Local dev should stage changes before review-pr.
        if since is None and not committed_files and not uncommitted_files:
            # Return sentinel — step 5d converts this to no_diff_source (exit 1).
            return [], "no_staged_changes", [], []

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

        # Angular-specific artifact detection (.ts files only).
        # Must run BEFORE the Java/Spring heuristics so that *.component.ts,
        # *.pipe.ts, etc. are never misclassified as "service" or "security".
        # Detection is pure-path/stem — no file reads, fully deterministic.
        if suffix == ".ts":
            # Stem may be multi-part: "causa-denegacion-form.component" → last part is the Angular type
            _ts_last = stem_lower.rsplit(".", 1)[-1]  # "component", "pipe", etc.
            _NG_SUFFIX_MAP = {
                "component":   ("ng_component",   ["ui"],               "medium"),
                "pipe":        ("ng_pipe",         ["ui"],               "low"),
                "directive":   ("ng_directive",    ["ui"],               "medium"),
                "guard":       ("ng_guard",        ["security", "auth"], "high"),
                "interceptor": ("ng_interceptor",  ["api"],              "medium"),
                "resolver":    ("ng_resolver",     ["api"],              "low"),
                "module":      ("ng_module",       ["config"],           "medium"),
            }
            if _ts_last in _NG_SUFFIX_MAP:
                _ng_atype, _ng_risks, _ng_impact = _NG_SUFFIX_MAP[_ts_last]
                return {"artifact_type": _ng_atype, "risk_areas": _ng_risks, "impact_level": _ng_impact, "is_noise": False, "module": module, "confidence": "high"}
            # Angular service: stem ends with ".service" or equals "service"
            if _ts_last == "service":
                return {"artifact_type": "ng_service", "risk_areas": ["business_logic"], "impact_level": "medium", "is_noise": False, "module": module, "confidence": "high"}

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

        # Business logic / services (extended: facade, usecase, aspect, listener)
        # NOTE: "component" intentionally removed — Angular *.component.ts files
        # are caught above by the Angular-specific block before reaching here.
        _SERVICE_KW = ("service", "serviceimpl", "servicefacade", "facade", "usecase",
                       "interactor", "aspect", "listener", "subscriber", "eventhandler")
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
    ) -> "CanonicalAnalysisIR":
        """Build incremental impact analysis for changed files.

        Returns CanonicalAnalysisIR — the shared IR consumed by delta and review-pr views.
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
            # Name-pattern heuristics — only when atype alone gave no verdict.
            # These are INFERRED (LOW CONFIDENCE) — stem match, not annotation evidence.
            if any(kw in stem_lower for kw in ("validator", "validation")):
                return "validation_component"
            if any(kw in stem_lower for kw in ("filter", "interceptor", "aspect")):
                return "runtime_filter"
            if any(kw in stem_lower for kw in ("advice", "advise", "exceptionhandler", "errorhandler")):
                return "exception_handler"
            if any(kw in stem_lower for kw in ("controller", "resource", "endpoint", "rest")):
                return "external_interface"
            if any(kw in stem_lower for kw in ("service", "svc", "usecase", "facade")):
                return "service"
            if any(kw in stem_lower for kw in ("repository", "repo", "dao", "store")):
                return "data_access"
            if any(kw in stem_lower for kw in ("config", "configuration", "settings", "properties")):
                return "configuration"
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
            return CanonicalAnalysisIR(
                relevant_files=[],
                impact_summary="No changes detected — verify the git ref passed to --since",
                affected_modules=[],
                risk_areas=[],
                why_these_files={},
                analysis_gaps=["No changed files found. Check that --since ref exists and the diff is non-empty."],
                system_impact={},
                change_type=[],
                dependency_graph_summary={"edges": [], "propagation_depth": 0},
                impact_score_per_file={},
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
        behavioral_changes: list[dict] = (
            [
                {
                    "statement": f"{Path(_p).name}: {_CHANGE_EFFECT.get(_cls['artifact_type'], 'application source')}",
                    "diff_severity": diff_severities.get(_p),
                    "epistemic_level": "STRUCTURAL SIGNAL",
                    "basis": "diff_severity in (api_change, security_change) with import-graph evidence present",
                }
                for _p, _cls in classifications.items()
                if not _cls["is_noise"]
                and diff_severities.get(_p, "unknown") in ("api_change", "security_change")
            ]
            if _has_graph_ev else []
        )

        def _runtime_impact(tc: dict[str, int]) -> list[dict]:
            _ri: list[dict] = []
            if "entrypoint" in tc:
                _ri.append({
                    "signal": "entrypoint-classified file modified",
                    "epistemic_level": "STRUCTURAL SIGNAL",
                    "basis": "artifact_type=entrypoint confirmed by annotation or naming evidence",
                })
            if "spring_config" in tc:
                _ri.append({
                    "signal": "Spring @Configuration-classified file modified",
                    "epistemic_level": "STRUCTURAL SIGNAL",
                    "basis": "@Configuration annotation detected in file content",
                })
            if "security" in tc:
                _ri.append({
                    "signal": "security-classified file modified",
                    "epistemic_level": "STRUCTURAL SIGNAL",
                    "basis": "artifact_type=security confirmed by annotation or naming evidence",
                })
            if "db_migration" in tc:
                _ri.append({
                    "signal": "database schema migration file present in diff",
                    "epistemic_level": "FACT",
                    "basis": "artifact_type=db_migration (file extension/naming convention)",
                })
            _svc = tc.get("service", 0)
            if _svc >= 2:
                _ri.append({
                    "signal": f"{_svc} @Service-annotated file(s) modified",
                    "epistemic_level": "STRUCTURAL SIGNAL",
                    "basis": "@Service annotation detected in file content",
                })
            _repo = tc.get("repository", 0) + tc.get("mapper", 0)
            if _repo > 0:
                _ri.append({
                    "signal": f"{_repo} persistence-classified component(s) modified",
                    "epistemic_level": "STRUCTURAL SIGNAL",
                    "basis": "artifact_type in (repository, mapper) confirmed by annotation evidence",
                })
            if "build_manifest" in tc:
                _ri.append({
                    "signal": "build manifest modified",
                    "epistemic_level": "FACT",
                    "basis": "artifact_type=build_manifest (pom.xml/build.gradle/package.json naming)",
                })
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
            f"multi-hop import tracing performed (depth {_max_hop})"
            if _max_hop >= 1
            else "no downstream dependencies could be verified"
        )
        analysis_gaps: list[str] = [
            f"Related file expansion via import analysis and directory proximity ({_bfs_note})",
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
                f"{len(low_confidence)} file(s) role inferred from filename only"
                f" (no code signal found): {', '.join(Path(f).name for f in low_confidence[:3])}"
            )
        if not affected_modules_set and any(not cls["is_noise"] for cls in classifications.values()):
            analysis_gaps.append(
                "DDD module/package structure not detected in changed paths"
                " — related file expansion uses directory proximity only"
            )

        return CanonicalAnalysisIR(
            relevant_files=relevant,
            impact_summary=impact_summary,
            affected_modules=sorted(affected_modules_set),
            risk_areas=risk_areas_out,
            why_these_files=why,
            analysis_gaps=analysis_gaps,
            system_impact=system_impact,
            change_type=aggregate_change_type,
            dependency_graph_summary=dependency_graph_summary,
            impact_score_per_file=impact_score_per_file,
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
            # FIX-P0-4: delta must behave consistently with review-pr for invalid refs.
            # review-pr fails immediately if the exact ref is not found.
            # delta previously silently rewrote to origin/HEAD (e.g. origin/develop)
            # or HEAD~1, masking the invalid ref and reporting "no_changes".
            #
            # New policy (matches review-pr):
            #   Stage 1: exact local ref — succeed
            #   Stage 2: remote-tracking ref (origin/<since>) — succeed (transparent alias)
            #   STOP: no symbolic-ref rewrite, no HEAD~1 silent fallback.
            #   Failure → structured error with hints, same schema as review-pr.

            # Stage 1: exact local ref
            if _verify(since):
                files = _diff(since)
                if files is not None:
                    return _make(files, since, "exact_local_ref")

            # Stage 2: remote-tracking ref (origin/<since>) — transparent remote alias
            remote_ref = f"origin/{since}"
            if _verify(remote_ref):
                files = _diff(remote_ref)
                if files is not None:
                    return _make(files, remote_ref, "remote_tracking_ref")

            # Stages 3 & 4 removed: symbolic-ref rewrite (origin/HEAD → different branch)
            # and HEAD~1 silent fallback are both dangerous when the caller named a specific
            # ref — they produce wrong diffs without any error signal.

            # All resolution stages failed → hard error (consistent with review-pr)
            return {
                "files": [],
                "resolved_ref": since,
                "resolution_path": "unresolvable",
                "diff_validation_status": "invalid_ref",
                "error": True,
            }

        else:
            # No since: unstaged → staged → HEAD~1 — never error, clean tree is valid
            ok, out = _run("diff", "--name-only", "--relative", timeout=10)
            if ok:
                files = [line.strip() for line in out.splitlines() if line.strip()]
                if files:
                    return _make(files, "HEAD", "uncommitted_unstaged")

            # Staged (committed to index but not yet to history)
            ok, out = _run("diff", "--name-only", "--cached", "--relative", timeout=10)
            if ok:
                files = [line.strip() for line in out.splitlines() if line.strip()]
                if files:
                    return _make(files, "HEAD", "uncommitted_staged")

            # HEAD~1 fallback — working tree clean, surface last commit
            if _verify("HEAD~1"):
                files = _diff("HEAD~1")
                if files is not None:
                    return _make(files or [], "HEAD~1", "head_minus_1_fallback")
            else:
                # First commit — no HEAD~1; diff against git empty tree
                _GIT_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
                files = _diff(_GIT_EMPTY_TREE)
                if files is not None:
                    return _make(files or [], _GIT_EMPTY_TREE, "initial_commit_fallback")

            # Confirmed no changes: empty repo
            return {
                "files": [],
                "resolved_ref": "HEAD",
                "resolution_path": "no_changes_confirmed",
                "diff_validation_status": "valid_empty",
                "error": False,
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

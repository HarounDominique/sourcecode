from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Optional, cast

import typer

from sourcecode import __version__
from sourcecode.entrypoint_classifier import is_production_entry_point, normalize_entry_point


# ---------------------------------------------------------------------------
# Analyzer fingerprints — short hashes of each analyzer's key rule constants.
# A change in heuristics, filter lists, or pattern maps changes the hash,
# making it immediately visible that two runs used different rule versions
# even if the semver string is the same.
# ---------------------------------------------------------------------------

def _fingerprint(*objects: object) -> str:
    raw = json.dumps([repr(o) for o in objects], sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:8]


def _compute_analyzer_fingerprints() -> dict[str, str]:
    from sourcecode.detectors.heuristic import (
        _AUXILIARY_DIRS as _HEUR_AUX,
        _ENTRYPOINT_NAMES,
        _EXTENSION_MAP,
    )
    from sourcecode.detectors.nodejs import _FRAMEWORK_MAP, NodejsDetector
    from sourcecode.confidence_analyzer import (
        _AUXILIARY_DIR_PREFIXES,
        _HARD_SOURCES,
        _SOFT_SOURCES,
    )
    from sourcecode.architecture_analyzer import (
        _BENCHMARK_DIRS,
        _NON_SOURCE_DIRS,
        LAYER_PATTERNS,
    )

    return {
        "heuristic": _fingerprint(_EXTENSION_MAP, _ENTRYPOINT_NAMES, sorted(_HEUR_AUX)),
        "nodejs": _fingerprint(_FRAMEWORK_MAP, sorted(NodejsDetector._AUXILIARY_DIRS)),
        "confidence": _fingerprint(sorted(_AUXILIARY_DIR_PREFIXES), sorted(_HARD_SOURCES), sorted(_SOFT_SOURCES)),
        "architecture": _fingerprint(sorted(_BENCHMARK_DIRS), sorted(_NON_SOURCE_DIRS), list(LAYER_PATTERNS.keys())),
    }


# ---------------------------------------------------------------------------
# Pipeline trace collector
# ---------------------------------------------------------------------------

class _TraceCollector:
    """Lightweight collector for pipeline trace events."""

    def __init__(self, enabled: bool = False) -> None:
        self._enabled = enabled
        self._events: list[dict[str, Any]] = []

    def emit(
        self,
        stage: str,
        component: str,
        action: str,
        target: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> None:
        if not self._enabled:
            return
        self._events.append({
            "stage": stage,
            "component": component,
            "action": action,
            **({"target": target} if target else {}),
            **({"reason": reason} if reason else {}),
        })

    def build_trace(self) -> "PipelineTrace":
        from sourcecode.schema import PipelineEvent, PipelineTrace
        events = [
            PipelineEvent(
                stage=e["stage"],
                component=e["component"],
                action=e["action"],
                target=e.get("target"),
                reason=e.get("reason"),
            )
            for e in self._events
        ]
        return PipelineTrace(requested=True, events=events)


# ---------------------------------------------------------------------------
# E2E pipeline coherence check
# ---------------------------------------------------------------------------

def _check_pipeline_coherence(sm: "SourceMap") -> list[str]:  # type: ignore[name-defined]
    """Verify no contradictory states exist between analyzers.

    Returns a list of human-readable violation strings (empty when clean).
    These are emitted to stderr as [coherence] warnings — never abort a run.
    """
    issues: list[str] = []
    cs = sm.confidence_summary

    if cs is not None:
        # overall:high requires at least one manifest-detected stack
        if cs.overall == "high":
            manifest_stacks = [s for s in sm.stacks if s.detection_method != "heuristic"]
            if not manifest_stacks:
                issues.append(
                    "[coherence] overall=high but all stacks are heuristic — "
                    "downgrade not applied; check confidence_analyzer"
                )

        # overall:high requires at least one production entry point
        if cs.overall == "high":
            prod_eps = [
                ep for ep in sm.entry_points
                if is_production_entry_point(ep)
            ]
            if not prod_eps and sm.entry_points:
                issues.append(
                    "[coherence] overall=high but no production entry points exist — "
                    "all detected EPs are auxiliary (benchmark/example/dev)"
                )

        # entry_point_confidence must not be high when entry_points is empty
        if cs.entry_point_confidence == "high" and not sm.entry_points:
            issues.append(
                "[coherence] entry_point_confidence=high but entry_points is empty"
            )

    return issues

_HELP = """\
Deterministic codebase context for AI coding agents.

[bold]Usage:[/bold]
  sourcecode                   [dim]# analyze current directory[/dim]
  sourcecode /path/to/repo     [dim]# analyze specific path[/dim]
  sourcecode --agent           [dim]# structured output for AI agents[/dim]

[bold]Subcommands:[/bold]
  prepare-context TASK [PATH]  [dim]# task-specific context[/dim]
  telemetry status|enable|disable
  version
  config
"""

# Known subcommand names — tokens matching these are routed as subcommands,
# not consumed as a repository path.
_SUBCOMMANDS: frozenset[str] = frozenset(
    {"telemetry", "prepare-context", "version", "config", "analyze"}
)

# Mutable container holding the path extracted by _preprocess_argv().
# Default "." means "current directory" when no path is given.
_detected_path: list[str] = ["."]


# Options that take a value token — their next arg must not be treated as a path.
_OPTIONS_WITH_VALUE: frozenset[str] = frozenset({
    "--format", "-f",
    "--output", "-o",
    "--graph-detail",
    "--graph-edges",
    "--max-nodes",
    "--docs-depth",
    "--depth",
    "--git-depth",
    "--git-days",
    "--since",
    "--path", "-p",
    "--mode",
    "--max-symbols",
    "--dependency-depth",
    "--rank-by",
    "--symbol",
})


def _preprocess_args(args: list[str]) -> list[str]:
    """Extract a repository path token from an args list and store it in _detected_path.

    Returns the modified args list (path token removed).
    Correctly skips option values (e.g. ``yaml`` in ``--format yaml``).
    If the first non-flag, non-value positional token is a known subcommand name,
    args are returned unchanged so Click can dispatch the subcommand.
    """
    result = list(args)
    skip_next = False
    for i, arg in enumerate(result):
        if skip_next:
            skip_next = False
            continue
        if arg.startswith("-"):
            # Does this option consume the next token as its value?
            flag_name = arg.split("=")[0]
            if flag_name in _OPTIONS_WITH_VALUE and "=" not in arg:
                skip_next = True
            continue
        if arg in _SUBCOMMANDS:
            return result  # known subcommand — leave for Click to dispatch
        # First genuine positional: treat as repository path
        _detected_path[0] = arg
        result.pop(i)
        return result
    return result


def _preprocess_argv() -> None:
    """Apply _preprocess_args to sys.argv in-place (used by main_entry)."""
    import sys as _sys
    modified = _preprocess_args(_sys.argv[1:])
    _sys.argv = _sys.argv[:1] + modified


def _copy_to_clipboard(content: str) -> bool:
    """Copy text to system clipboard. Returns True on success, False otherwise (never raises)."""
    import subprocess
    import sys as _sys
    try:
        if _sys.platform == "darwin":
            subprocess.run(["pbcopy"], input=content.encode("utf-8"), check=True, timeout=10)
            return True
        elif _sys.platform == "win32":
            subprocess.run(["clip"], input=content.encode("utf-16"), check=True, timeout=10)
            return True
        else:
            for cmd in (["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]):
                try:
                    subprocess.run(cmd, input=content.encode("utf-8"), check=True, timeout=10)
                    return True
                except (FileNotFoundError, subprocess.CalledProcessError):
                    continue
            return False
    except Exception:
        return False


app = typer.Typer(
    name="sourcecode",
    help=_HELP,
    add_completion=False,
    rich_markup_mode="rich",
    no_args_is_help=False,
)

# ── Hook preprocessing into the Click command layer ───────────────────────────
# Typer's CliRunner (and app() itself) calls typer.main.get_command(app) to
# create a Click Group, then calls click_group.main(args=...).
# We patch get_command so the returned Click Group always preprocesses args —
# this covers both main_entry() (sys.argv path) and runner.invoke(app, args).
import typer.main as _typer_main_module  # noqa: E402

_orig_get_command = _typer_main_module.get_command


def _get_command_with_preprocessing(typer_instance: Any) -> Any:
    cmd = _orig_get_command(typer_instance)
    if typer_instance is not app:
        return cmd  # only wrap the root app, not telemetry_app etc.
    _orig_cmd_main = cmd.main

    def _cmd_main(args: Optional[list[str]] = None, **kwargs: Any) -> Any:
        if args is not None:
            # CliRunner / programmatic call: preprocess the explicit args list.
            _detected_path[0] = "."
            args = _preprocess_args(list(args))
        # args=None → Click reads sys.argv; _preprocess_argv() in main_entry handled it.
        return _orig_cmd_main(args=args, **kwargs)

    cmd.main = _cmd_main
    return cmd


_typer_main_module.get_command = _get_command_with_preprocessing

# typer.testing imports get_command as a private alias _get_command at module
# load time; patch that reference too so CliRunner.invoke uses our version.
try:
    import typer.testing as _typer_testing_module
    _typer_testing_module._get_command = _get_command_with_preprocessing  # type: ignore[attr-defined]
except Exception:
    pass

telemetry_app = typer.Typer(help="Manage anonymous telemetry (opt-in).", rich_markup_mode="rich")
app.add_typer(telemetry_app, name="telemetry")


def _maybe_ask_consent() -> None:
    """Show first-run consent prompt once, on interactive TTYs only."""
    try:
        from sourcecode.telemetry.config import has_been_asked, mark_asked, set_enabled
        from sourcecode.telemetry.consent import ask_for_consent
        if not has_been_asked():
            enabled = ask_for_consent()
            set_enabled(enabled)
            if enabled:
                typer.echo("Telemetry enabled. Thank you. Disable: sourcecode telemetry disable", err=True)
            else:
                typer.echo("Telemetry disabled. Enable anytime: sourcecode telemetry enable", err=True)
    except Exception:
        pass


def _active_flags(
    dependencies: bool, graph_modules: bool, docs: bool, full_metrics: bool,
    semantics: bool, architecture: bool, git_context: bool, env_map: bool,
    code_notes: bool, agent: bool, compact: bool, tree: bool, no_redact: bool,
    fmt: str,
) -> list[str]:
    flags: list[str] = []
    if agent: flags.append("--agent")
    if compact: flags.append("--compact")
    if dependencies: flags.append("--dependencies")
    if graph_modules: flags.append("--graph-modules")
    if docs: flags.append("--docs")
    if full_metrics: flags.append("--full-metrics")
    if semantics: flags.append("--semantics")
    if architecture: flags.append("--architecture")
    if git_context: flags.append("--git-context")
    if env_map: flags.append("--env-map")
    if code_notes: flags.append("--code-notes")
    if tree: flags.append("--tree")
    if no_redact: flags.append("--no-redact")
    if fmt != "json": flags.append("--format")
    return flags

FORMAT_CHOICES = ["json", "yaml"]
GRAPH_DETAIL_CHOICES = ["high", "medium", "full"]
GRAPH_EDGE_CHOICES = {"imports", "calls", "contains", "extends"}
DOCS_DEPTH_CHOICES = ["module", "symbols", "full"]


def version_callback(value: bool) -> None:
    if value:
        typer.echo(f"sourcecode {__version__}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    format: str = typer.Option(
        "json",
        "--format",
        "-f",
        help="Output format: json (default) or yaml. Both carry identical data — yaml is more human-readable, json is preferred for agent pipelines.",
        show_default=True,
    ),
    output: Optional[Path] = typer.Option(
        None,
        "--output",
        "-o",
        help="Write output to a file instead of stdout. Useful for storing analysis snapshots or piping to downstream tools.",
    ),
    compact: bool = typer.Option(
        False,
        "--compact",
        help=(
            "Compact output (~600–800 tokens): project type, stacks, production entry points, "
            "dependency summary, confidence summary, and analysis gaps. "
            "Omits file tree, raw dependency lists, docs, and module graph. "
            "Designed for agent context windows. Automatically enables --dependencies, --env-map, and --code-notes."
        ),
    ),
    dependencies: bool = typer.Option(
        False,
        "--dependencies",
        help=(
            "Analyze direct and transitive dependencies. Reads manifests (pyproject.toml, package.json, go.mod, etc.) "
            "and lockfiles when available. Adds dependency_summary and key_dependencies to output."
        ),
    ),
    graph_modules: bool = typer.Option(
        False,
        "--graph-modules",
        help=(
            "Include a structural module graph: nodes (files/symbols) and edges (imports, calls, contains). "
            "Useful for understanding coupling and call flows. Adds module_graph to output. "
            "Combine with --graph-detail and --graph-edges to control scope."
        ),
    ),
    graph_detail: str = typer.Option(
        "high",
        "--graph-detail",
        help="Detail level for --graph-modules: high (top modules by importance), medium (filtered by relevance), full (all nodes and edges). Default: high.",
        show_default=True,
    ),
    max_nodes: Optional[int] = typer.Option(
        None,
        "--max-nodes",
        help="Maximum number of nodes in --graph-modules output when using high or medium detail. Prevents oversized graphs in large codebases.",
        min=1,
    ),
    graph_edges: Optional[str] = typer.Option(
        None,
        "--graph-edges",
        help="Edge types for --graph-modules, comma-separated: imports,calls,contains,extends. Default: all available. Example: --graph-edges imports,calls",
    ),
    no_tree: bool = typer.Option(
        False,
        "--no-tree",
        hidden=True,
        help="(Removed) No-op. File tree is excluded by default. Use --tree to include it.",
    ),
    tree: bool = typer.Option(
        False,
        "--tree",
        help=(
            "Include the full file_tree and flat file_paths list in output (deep-dive layer). "
            "Adds significant size — use when the agent needs to browse the full file structure."
        ),
    ),
    no_redact: bool = typer.Option(
        False,
        "--no-redact",
        help=(
            "Disable automatic secret redaction. By default, potential secrets (API keys, tokens, passwords) "
            "are replaced with [REDACTED]. Use with caution — output may contain sensitive values."
        ),
    ),
    version: Optional[bool] = typer.Option(
        None,
        "--version",
        "-v",
        callback=version_callback,
        is_eager=True,
        help="Show version number and exit.",
    ),
    depth: int = typer.Option(
        4,
        "--depth",
        help=(
            "Maximum depth for file tree traversal (default: 4, range: 1–20). "
            "Increase for deeply nested projects — Maven/Java requires at least 8 (src/main/java/...)."
        ),
        min=1,
        max=20,
    ),
    docs: bool = typer.Option(
        False,
        "--docs",
        help="Extract documentation: docstrings, function signatures, and module-level comments. Adds doc_summary and docs to output. Combine with --docs-depth to control coverage.",
    ),
    docs_depth: str = typer.Option(
        "symbols",
        "--docs-depth",
        help="Documentation extraction depth: module (module-level only), symbols (functions and classes), full (all symbols including private). Default: symbols.",
        show_default=True,
    ),
    full_metrics: bool = typer.Option(
        False,
        "--full-metrics",
        help=(
            "Technical audit: lines of code, symbol counts, cyclomatic complexity, and test coverage per file. "
            "Produces file_metrics and metrics_summary. "
            "Not included in --agent output — designed for CI pipelines and code review tools, not as primary agent context."
        ),
    ),
    semantics: bool = typer.Option(
        False,
        "--semantics",
        help=(
            "Semantic analysis: cross-file symbol resolution, call graph with confidence levels, and import linking. "
            "Adds semantic_calls, semantic_symbols, semantic_links, semantic_summary, and hotspots (files ranked by fan-in/fan-out). "
            "Slower than default analysis — skip for quick scans. "
            "Confidence degrades on dynamic dispatch, decorators, and generated code."
        ),
    ),
    architecture: bool = typer.Option(
        False,
        "--architecture",
        help=(
            "Architectural inference: detect functional layers (MVC/layered/hexagonal), bounded contexts, "
            "and dominant structural patterns. Adds architecture to output. "
            "Confidence is low when based on directory names alone — combine with --semantics for higher accuracy."
        ),
    ),
    git_context: bool = typer.Option(
        False,
        "--git-context",
        "-g",
        help="Include git activity: recent commits, change hotspots (most frequently modified files), pending uncommitted changes, and contributors. Adds git_context to output.",
    ),
    git_depth: int = typer.Option(
        20,
        "--git-depth",
        help="Number of recent commits to include with --git-context (default: 20, max: 100).",
        min=1,
        max=100,
    ),
    git_days: int = typer.Option(
        90,
        "--git-days",
        help="Time window in days for detecting change hotspots with --git-context (default: 90). Hotspots are files with the most commits in this window.",
        min=1,
        max=3650,
    ),
    env_map: bool = typer.Option(
        False,
        "--env-map",
        help="Map environment variables: keys, types (string/int/bool/url/path), categories (database/auth/service/...), and which files reference them. Adds env_map and env_summary.",
    ),
    code_notes: bool = typer.Option(
        False,
        "--code-notes",
        help=(
            "Extract inline annotations: TODO, FIXME, HACK, NOTE, DEPRECATED, WARNING, BUG, XXX, OPTIMIZE — "
            "with file location and enclosing symbol. "
            "Also detects Architecture Decision Records (ADRs) in docs/decisions/, docs/adr/, and similar paths."
        ),
    ),
    agent: bool = typer.Option(
        False,
        "--agent",
        help=(
            "Agent-optimized output: structured, noise-free JSON for AI consumption. "
            "Automatically enables --dependencies, --env-map, and --code-notes. Suppresses file tree. "
            "Output includes: identity, entry points, architecture, runtime dependencies, "
            "operational signals, confidence summary, and analysis gaps. No empty sections."
        ),
    ),
    trace_pipeline: bool = typer.Option(
        False,
        "--trace-pipeline",
        help=(
            "Diagnostic mode: include pipeline_trace in output showing every candidate, filter decision, "
            "and data origin across all pipeline stages. "
            "Use to diagnose unexpected or contaminated results. Not intended for normal agent use."
        ),
    ),
    mode: str = typer.Option(
        "contract",
        "--mode",
        help=(
            "Output mode: contract (default) | standard | raw. "
            "contract: minimal per-file contracts — exports, signatures, deps. "
            "Smallest output, recommended for AI agents. "
            "minimal is accepted as an alias for contract. "
            "standard: full per-file detail with imports, relevance scores, extraction method. "
            "raw: project-level analysis only (stacks, entry points, dependency summary). "
            "No per-file contracts."
        ),
    ),
    max_symbols: Optional[int] = typer.Option(
        None,
        "--max-symbols",
        help="Limit total exported semantic nodes across all file contracts. Trims lowest-ranked files first.",
        min=1,
    ),
    dependency_depth: int = typer.Option(
        0,
        "--dependency-depth",
        hidden=True,
        help="(Removed) Transitive resolution is not implemented. Pass 0 or omit.",
        min=0,
        max=5,
    ),
    entrypoints_only: bool = typer.Option(
        False,
        "--entrypoints-only",
        help="Contract mode: include only files that are runtime entrypoints or have exported symbols (public API surface). Note: 'entrypoints' here includes all files with exports, not strictly detected runtime entry points.",
    ),
    changed_only: bool = typer.Option(
        False,
        "--changed-only",
        help="Contract mode: include only git-modified files (staged, unstaged, untracked).",
    ),
    rank_by: str = typer.Option(
        "relevance",
        "--rank-by",
        help="Contract ranking strategy: relevance (default) | centrality | git-churn.",
    ),
    emit_graph: bool = typer.Option(
        False,
        "--emit-graph",
        help="Contract mode: include a compact dependency graph (nodes + edges) in output.",
    ),
    compress_types: bool = typer.Option(
        False,
        "--compress-types",
        hidden=True,
        help="(Removed) No observable effect when type signatures are not extracted. Omit.",
    ),
    symbol: Optional[str] = typer.Option(
        None,
        "--symbol",
        help="Contract mode: extract localized context for a specific symbol name. Returns defining file + all importers.",
    ),
    copy: bool = typer.Option(
        False,
        "--copy",
        "-c",
        help="Copy output to system clipboard after a successful run. No-op when --output is used or clipboard is unavailable.",
    ),
) -> None:
    """Analyze a repository and produce structured context for AI coding agents.

    \b
    Examples:
      sourcecode                        analyze current directory
      sourcecode /path/to/repo          analyze specific path
      sourcecode --agent                agent-optimized output
      sourcecode --agent --git-context  include git activity signals
    """
    # First-run consent (skip for telemetry/version/config subcommands)
    if ctx.invoked_subcommand not in ("telemetry", "version", "config"):
        _maybe_ask_consent()

    # When a subcommand is invoked, skip the main analysis.
    if ctx.invoked_subcommand is not None:
        return

    _t0 = time.monotonic()

    # Validate new flag choices
    _MODE_CHOICES = ("contract", "minimal", "standard", "raw")
    _DEPRECATED_MODES: dict[str, str] = {
        "hybrid": "contract",
        "deep": "standard",
    }
    if mode in _DEPRECATED_MODES:
        fallback = _DEPRECATED_MODES[mode]
        typer.echo(
            f"[deprecated] --mode {mode} is removed: produced identical output to --mode {fallback}. "
            f"Using --mode {fallback}.",
            err=True,
        )
        mode = fallback
    elif mode not in _MODE_CHOICES:
        typer.echo(
            f"Error: invalid value '{mode}' for --mode. Valid options: {', '.join(_MODE_CHOICES)}",
            err=True,
        )
        raise typer.Exit(code=1)
    _RANK_CHOICES = ("relevance", "centrality", "git-churn")
    if rank_by not in _RANK_CHOICES:
        typer.echo(
            f"Error: invalid value '{rank_by}' for --rank-by. Valid options: {', '.join(_RANK_CHOICES)}",
            err=True,
        )
        raise typer.Exit(code=1)

    if dependency_depth > 0:
        typer.echo(
            f"[warning] --dependency-depth {dependency_depth} has no effect: "
            "transitive import resolution is not implemented for npm/yarn/pip projects. "
            "Using depth=0 (direct dependencies only).",
            err=True,
        )
        dependency_depth = 0

    if compress_types:
        typer.echo(
            "[deprecated] --compress-types is removed: type signatures are rarely extracted "
            "at default depth. Flag ignored.",
            err=True,
        )

    # Validate format choices
    if format not in FORMAT_CHOICES:
        typer.echo(
            f"Error: invalid value '{format}' for --format. Valid options: {', '.join(FORMAT_CHOICES)}",
            err=True,
        )
        raise typer.Exit(code=1)
    if graph_detail not in GRAPH_DETAIL_CHOICES:
        typer.echo(
            f"Error: invalid value '{graph_detail}' for --graph-detail. Valid options: {', '.join(GRAPH_DETAIL_CHOICES)}",
            err=True,
        )
        raise typer.Exit(code=1)
    if docs_depth not in DOCS_DEPTH_CHOICES:
        typer.echo(
            f"Error: invalid value '{docs_depth}' for --docs-depth. Valid options: {', '.join(DOCS_DEPTH_CHOICES)}",
            err=True,
        )
        raise typer.Exit(code=1)

    # Path was extracted from argv by _preprocess_argv() before Click ran.
    target = Path(_detected_path[0]).resolve()
    if not target.exists():
        typer.echo(f"Error: directory '{target}' does not exist.", err=True)
        raise typer.Exit(code=1)
    if not target.is_dir():
        typer.echo(f"Error: '{target}' is not a directory.", err=True)
        raise typer.Exit(code=1)

    # Normalize mode aliases
    _CONTRACT_MODES = frozenset({"contract", "minimal", "standard"})
    if mode == "minimal":
        mode = "contract"   # minimal is a documented alias for contract
    elif mode not in _CONTRACT_MODES and mode != "raw":
        mode = "contract"   # unknown → safe default

    # Legacy flags imply raw mode unless --mode was explicitly overridden.
    # These flags produce standard_view-only output sections not in contract_view.
    # Preserves backward compat: callers using any legacy flag get their previous format.
    # New callers opt into contract mode via --mode contract (or bare invocation).
    _legacy_flags_active = (
        compact or agent or tree or format == "yaml" or trace_pipeline
        or docs or semantics or graph_modules or full_metrics or architecture
    )
    if mode in ("contract", "standard") and _legacy_flags_active:
        mode = "raw"

    # Map mode to contract_view depth
    _CONTRACT_DEPTH = {
        "contract": "minimal",
        "standard": "standard",
    }

    # --- Import analysis modules ---
    from dataclasses import asdict, replace

    from sourcecode.dependency_analyzer import DependencyAnalyzer
    from sourcecode.detectors import ProjectDetector, build_default_detectors
    from sourcecode.doc_analyzer import DocAnalyzer
    from sourcecode.graph_analyzer import GraphAnalyzer, GraphDetail
    from sourcecode.metrics_analyzer import MetricsAnalyzer
    from sourcecode.redactor import SecretRedactor, redact_dict
    from sourcecode.scanner import FileScanner
    from sourcecode.semantic_analyzer import SemanticAnalyzer
    from sourcecode.schema import (
        AnalysisMetadata,
        DocRecord,
        DocsDepth,
        DocSummary,
        EntryPoint,
        SourceMap,
        StackDetection,
    )
    from sourcecode.serializer import agent_view, compact_view, normalize_source_map, standard_view, validate_cross_analyzer_consistency, validate_source_map, write_output
    from sourcecode.workspace import WorkspaceAnalyzer

    # 1. Scan directory (SCAN-01 to SCAN-05)
    redactor = SecretRedactor(enabled=not no_redact)

    # Classify repository topology before scanning.  This is a shallow
    # filesystem read (depth 0-1 only) and completes in milliseconds.
    # The topology drives per-directory depth budgets in AdaptiveScanner.
    from sourcecode.adaptive_scanner import AdaptiveScanner
    from sourcecode.repo_classifier import RepoClassifier
    _topology = RepoClassifier().classify(target)

    # Detect manifests before scan to adjust depth.
    # find_manifests() only looks at depth 0-1, does not need the full tree.
    _pre_scanner = FileScanner(target, max_depth=1)
    manifests = _pre_scanner.find_manifests()

    # Maven uses src/main/java/<groupId>/<artifactId>/<module>/ (depth 7+).
    # At depth=4 Java files are invisible and all analyzers fail.
    # Require at least 8: src(1)+main(2)+java(3)+com(4)+co(5)+app(6)+module(7)+file.
    _java_manifest_names = {"pom.xml", "build.gradle", "build.gradle.kts"}
    _is_java = any(Path(m).name in _java_manifest_names for m in manifests)
    _java_min_depth = 8
    effective_depth = max(depth, _java_min_depth) if _is_java and depth < _java_min_depth else depth

    # --agent: enable signal analyzers; output via agent_view (not compact)
    if agent:
        dependencies = True
        env_map = True
        code_notes = True
        no_tree = True  # agents never need the raw file tree
        typer.echo("[agent] dependencies env-map code-notes (no-tree)", err=True)

    scanner = AdaptiveScanner(target, topology=_topology, base_depth=effective_depth)
    raw_tree = scanner.scan_tree()

    # 2. Filter .env and *.secret entries from file tree (SEC-02, all levels)
    def filter_sensitive_files(tree: dict[str, Any]) -> dict[str, Any]:
        filtered: dict[str, Any] = {}
        for name, value in tree.items():
            if redactor.should_exclude_file(name):
                continue  # exclude .env, *.secret from tree
            if isinstance(value, dict):
                filtered[name] = filter_sensitive_files(value)
            else:
                filtered[name] = value
        return filtered

    def prune_workspace_paths(
        tree: dict[str, Any], workspace_paths: list[str]
    ) -> dict[str, Any]:
        pruned = dict(tree)
        for workspace_path in workspace_paths:
            parts = [part for part in workspace_path.split("/") if part]
            if not parts:
                continue
            node = pruned
            for index, part in enumerate(parts):
                if not isinstance(node, dict) or part not in node:
                    break
                if index == len(parts) - 1:
                    node.pop(part, None)
                    break
                child = node.get(part)
                if not isinstance(child, dict):
                    break
                node = child
        return pruned

    file_tree = filter_sensitive_files(raw_tree)
    detector = ProjectDetector(build_default_detectors())
    workspace_analysis = WorkspaceAnalyzer().analyze(target, manifests)

    # Adaptive traversal handles monorepo source root discovery automatically.
    # Emit a diagnostic when topology confidence is low so users know why.
    import sys as _sys
    if _topology.workspace_type == "monorepo" and _topology.confidence < 0.5:
        if _sys.stderr.isatty():
            typer.echo(
                "[traversal] monorepo detected but source root confidence is low "
                f"({_topology.confidence:.0%}). Use --depth 8 or higher if files are missing.",
                err=True,
            )

    # --compact implicitly enables lightweight analysis passes so that
    # dependency_summary, env_summary and code_notes_summary are never null.
    if compact:
        dependencies = True
        env_map = True
        code_notes = True

    dependency_analyzer = DependencyAnalyzer() if dependencies else None
    graph_analyzer = GraphAnalyzer() if graph_modules else None
    parsed_graph_edges = (
        {edge.strip() for edge in graph_edges.split(",") if edge.strip()}
        if graph_edges
        else None
    )
    if parsed_graph_edges is not None:
        invalid_edges = sorted(parsed_graph_edges - GRAPH_EDGE_CHOICES)
        if invalid_edges:
            typer.echo(
                f"Error: invalid values for --graph-edges: "
                f"{', '.join(invalid_edges)}. Valid options: {', '.join(sorted(GRAPH_EDGE_CHOICES))}",
                err=True,
            )
            raise typer.Exit(code=1)
    graph_detail_typed = cast(GraphDetail, graph_detail)
    docs_depth_typed = cast(DocsDepth, docs_depth)
    doc_analyzer = DocAnalyzer() if docs else None
    metrics_analyzer = MetricsAnalyzer() if full_metrics else None

    semantic_analyzer = SemanticAnalyzer() if semantics else None

    root_manifests = [
        manifest
        for manifest in manifests
        if Path(manifest).resolve().parent == target
    ]
    detection_manifests = root_manifests if workspace_analysis.workspaces else manifests
    if workspace_analysis.is_monorepo and not root_manifests:
        stacks: list[StackDetection] = []
        entry_points: list[EntryPoint] = []
    else:
        stacks, entry_points, _project_type = detector.detect(target, file_tree, detection_manifests)

    dependency_records = []
    dependency_summaries = []
    if dependency_analyzer is not None:
        root_dependencies, root_summary = dependency_analyzer.analyze(target)
        dependency_records.extend(root_dependencies)
        dependency_summaries.append(root_summary)
    module_graphs = []
    if graph_analyzer is not None:
        root_graph_tree = (
            prune_workspace_paths(
                file_tree,
                [workspace.path for workspace in workspace_analysis.workspaces],
            )
            if workspace_analysis.workspaces
            else file_tree
        )
        module_graphs.append(
            graph_analyzer.analyze(
                target,
                root_graph_tree,
                detail="full",
                entry_points=entry_points,
            )
        )
    doc_records: list[DocRecord] = []
    doc_summaries: list[DocSummary] = []
    if doc_analyzer is not None:
        root_doc_tree = (
            prune_workspace_paths(
                file_tree,
                [workspace.path for workspace in workspace_analysis.workspaces],
            )
            if workspace_analysis.workspaces
            else file_tree
        )
        root_doc_records, root_doc_summary = doc_analyzer.analyze(
            target,
            root_doc_tree,
            depth=docs_depth_typed,
            entry_points=[ep.path for ep in entry_points],   # LQN-03
        )
        doc_records.extend(root_doc_records)
        doc_summaries.append(root_doc_summary)

    file_metrics_records: list[Any] = []
    metrics_summaries = []
    if metrics_analyzer is not None:
        root_metrics_tree = (
            prune_workspace_paths(
                file_tree,
                [workspace.path for workspace in workspace_analysis.workspaces],
            )
            if workspace_analysis.workspaces
            else file_tree
        )
        root_file_metrics, root_metrics_summary = metrics_analyzer.analyze(
            target,
            root_metrics_tree,
        )
        file_metrics_records.extend(root_file_metrics)
        metrics_summaries.append(root_metrics_summary)

    for workspace in workspace_analysis.workspaces:
        workspace_root = target / workspace.path
        if not workspace_root.exists() or not workspace_root.is_dir():
            continue
        _ws_topology = RepoClassifier().classify(workspace_root)
        workspace_scanner = AdaptiveScanner(workspace_root, topology=_ws_topology, base_depth=depth)
        workspace_tree = filter_sensitive_files(workspace_scanner.scan_tree())
        workspace_manifests = workspace_scanner.find_manifests()
        workspace_stacks, workspace_entry_points, _ = detector.detect(
            workspace_root,
            workspace_tree,
            workspace_manifests,
        )

        stacks.extend(
            replace(stack, root=workspace.path, workspace=workspace.path, primary=False)
            for stack in workspace_stacks
        )
        entry_points.extend(
            replace(
                entry_point,
                path=f"{workspace.path}/{entry_point.path}",
            )
            for entry_point in workspace_entry_points
        )
        if dependency_analyzer is not None:
            workspace_dependencies, workspace_summary = dependency_analyzer.analyze(
                workspace_root,
                workspace=workspace.path,
            )
            dependency_records.extend(workspace_dependencies)
            dependency_summaries.append(workspace_summary)
        if graph_analyzer is not None:
            workspace_graph = graph_analyzer.analyze(
                workspace_root,
                workspace_tree,
                workspace=workspace.path,
                detail="full",
                entry_points=workspace_entry_points,
            )
            module_graphs.append(
                graph_analyzer.prefix_graph(workspace_graph, workspace.path, workspace.path)
            )
        if doc_analyzer is not None:
            workspace_doc_records, workspace_doc_summary = doc_analyzer.analyze(
                workspace_root,
                workspace_tree,
                workspace=workspace.path,
                depth=docs_depth_typed,
                entry_points=[ep.path for ep in workspace_entry_points],   # LQN-03
            )
            # Prefix paths with workspace.path so they are relative to repo root
            # (same pattern as entry_points path prefixing)
            prefixed_doc_records = [
                replace(record, path=f"{workspace.path}/{record.path}")
                for record in workspace_doc_records
            ]
            doc_records.extend(prefixed_doc_records)
            doc_summaries.append(workspace_doc_summary)
        if metrics_analyzer is not None:
            ws_file_metrics, ws_metrics_summary = metrics_analyzer.analyze(
                workspace_root,
                workspace_tree,
                workspace=workspace.path,
            )
            prefixed_file_metrics = [
                replace(m, path=f"{workspace.path}/{m.path}")
                for m in ws_file_metrics
            ]
            file_metrics_records.extend(prefixed_file_metrics)
            metrics_summaries.append(ws_metrics_summary)

    stacks, project_type = detector.classify_results(
        file_tree,
        stacks,
        entry_points,
        project_type_override="monorepo" if workspace_analysis.is_monorepo else None,
    )
    dependency_summary = (
        dependency_analyzer.merge_summaries(dependency_summaries)
        if dependency_analyzer is not None
        else None
    )
    module_graph = (
        graph_analyzer.merge_graphs(
            module_graphs,
            detail=graph_detail_typed,
            edge_kinds=parsed_graph_edges,
            max_nodes=max_nodes,
            entry_points=entry_points,
        )
        if graph_analyzer is not None
        else None
    )
    doc_summary = (
        doc_analyzer.merge_summaries(doc_summaries)
        if doc_analyzer is not None
        else None
    )
    metrics_summary = (
        metrics_analyzer.merge_summaries(metrics_summaries)
        if metrics_analyzer is not None
        else None
    )

    # 3. Build schema
    # Compute analyzer fingerprints: short hashes of each analyzer's key rule
    # constants so that a rule change is always visible in the output, regardless
    # of whether the semver was bumped.
    try:
        _fingerprints = _compute_analyzer_fingerprints()
    except Exception:
        _fingerprints = {}

    metadata = AnalysisMetadata(
        analyzed_path=str(target),
        analyzer_fingerprints=_fingerprints,
        traversal_topology=_topology.as_dict(),
    )
    sm = SourceMap(
        metadata=metadata,
        file_tree=file_tree,
        stacks=stacks,
        project_type=project_type,
        entry_points=entry_points,
        dependencies=dependency_records,
        dependency_summary=dependency_summary,
        module_graph=module_graph,
        module_graph_summary=module_graph.summary if module_graph is not None else None,
        docs=doc_records,
        doc_summary=doc_summary,
        file_metrics=file_metrics_records,
        metrics_summary=metrics_summary,
    )

    # Semantic analysis (--semantics flag)
    if semantic_analyzer is not None:
        if workspace_analysis.workspaces:
            all_sem_calls: list[Any] = []
            all_sem_symbols: list[Any] = []
            all_sem_links: list[Any] = []
            all_sem_summaries: list[Any] = []
            for ws in workspace_analysis.workspaces:
                ws_calls, ws_syms, ws_links, ws_sum = semantic_analyzer.analyze(
                    target / ws.path,
                    (
                        filter_sensitive_files(
                            AdaptiveScanner(target / ws.path, base_depth=depth).scan_tree()
                        )
                    ),
                    workspace=ws.path,
                )
                # Prefix paths to repo-root-relative (sm.file_paths uses root-relative)
                _pfx = ws.path.rstrip("/") + "/"
                all_sem_calls.extend(
                    replace(c,
                        caller_path=_pfx + c.caller_path,
                        callee_path=_pfx + c.callee_path,
                    )
                    for c in ws_calls
                )
                all_sem_symbols.extend(
                    replace(s, path=_pfx + s.path) for s in ws_syms
                )
                all_sem_links.extend(
                    replace(l,
                        importer_path=_pfx + l.importer_path,
                        source_path=(
                            _pfx + l.source_path
                            if l.source_path is not None and not l.is_external
                            else l.source_path
                        ),
                    )
                    for l in ws_links
                )
                all_sem_summaries.append(ws_sum)
            merged_sem = semantic_analyzer.merge_summaries(all_sem_summaries)
            sm = replace(
                sm,
                semantic_calls=all_sem_calls,
                semantic_symbols=all_sem_symbols,
                semantic_links=all_sem_links,
                semantic_summary=merged_sem,
            )
        else:
            sem_calls, sem_syms, sem_links, sem_sum = semantic_analyzer.analyze(
                target, file_tree
            )
            sm = replace(
                sm,
                semantic_calls=sem_calls,
                semantic_symbols=sem_syms,
                semantic_links=sem_links,
                semantic_summary=sem_sum,
            )

    # Runtime architecture — classify workspace packages for structural summaries
    if workspace_analysis.workspaces:
        from sourcecode.runtime_classifier import RuntimeClassifier
        sm.monorepo_packages = RuntimeClassifier().classify(
            target,
            [ws.path for ws in workspace_analysis.workspaces],
        )

    # Phase 9: LLM Output Quality — populate derived fields
    from sourcecode.architecture_summary import ArchitectureSummarizer
    from sourcecode.summarizer import ProjectSummarizer
    from sourcecode.tree_utils import flatten_file_tree

    # LQN-01: flat path list from file_tree with forward-slash separator
    sm.file_paths = [
        p.replace("\\", "/") for p in flatten_file_tree(sm.file_tree)
    ]

    # Semantic hotspots + coverage (needs sm.file_paths populated above)
    if semantic_analyzer is not None and sm.semantic_summary is not None:
        from collections import Counter as _Counter
        from pathlib import Path as _Path

        _SRC_EXTS = {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".java", ".kt", ".rs", ".rb"}
        _fan_in: _Counter[str] = _Counter()
        _fan_out: _Counter[str] = _Counter()
        for _call in sm.semantic_calls:
            if _call.callee_path:
                _fan_in[_call.callee_path] += 1
            if _call.caller_path:
                _fan_out[_call.caller_path] += 1

        _all_call_files = set(_fan_in) | set(_fan_out)
        _hotspots: list[dict] = []
        # Filter test, noise, and auxiliary paths — they dominate fan-in but carry no signal
        _TEST_MARKERS = {"/test", "/tests", "/spec", "/specs", "_test.", ".test.", ".spec."}
        from sourcecode.ranking_engine import RankingEngine as _RankingEngine
        _sem_engine = _RankingEngine(sm.monorepo_packages)
        for _p in _all_call_files:
            if any(_m in _p for _m in _TEST_MARKERS) or _p.startswith("test"):
                continue
            if _sem_engine.is_noise(_p) or _sem_engine.is_auxiliary(_p):
                continue
            _in = _fan_in[_p]
            _out = _fan_out[_p]
            _score = _in * 2.0 + _out * 1.0
            if _score < 2:
                continue
            if _in > _out * 2:
                _reason = "high fan-in: many callers depend on this"
            elif _out > _in * 2:
                _reason = "high fan-out: orchestrates many modules"
            else:
                _reason = "hub: balanced import/call traffic"
            # Determine method confidence from calls touching this path
            _method = next(
                (c.method for c in sm.semantic_calls if c.callee_path == _p or c.caller_path == _p),
                "heuristic",
            )
            _hotspots.append({
                "path": _p,
                "importance_score": round(_score, 1),
                "fan_in": _in,
                "fan_out": _out,
                "reason": _reason,
                "confidence": "medium" if _method == "heuristic" else "high",
            })
        _hotspots.sort(key=lambda x: -x["importance_score"])

        _total_src = sum(
            1 for _fp in sm.file_paths
            if _Path(_fp).suffix.lower() in _SRC_EXTS
        )
        _analyzed = sm.semantic_summary.files_analyzed
        _cov_pct = round(_analyzed / _total_src * 100, 1) if _total_src > 0 else 0.0
        _cov_conf = (
            "high" if _cov_pct >= 80
            else "medium" if _cov_pct >= 40
            else "low"
        )

        sm = replace(
            sm,
            semantic_summary=replace(
                sm.semantic_summary,
                hotspots=_hotspots[:10],
                coverage_pct=_cov_pct,
                coverage_confidence=_cov_conf,
            ),
        )

    # LQN-05: top-15 direct dependencies from manifest/lockfile, sorted by role
    if dependency_analyzer is not None:
        from sourcecode.dependency_analyzer import _ROLE_PRIORITY

        primary_ecosystem = sm.stacks[0].stack if sm.stacks else ""
        direct_deps = [
            d for d in sm.dependencies
            if d.scope != "transitive" and d.source in {"manifest", "lockfile"}
            and (d.role or "unknown") in {"runtime", "parsing", "serialization", "observability", "infra"}
            and d.scope not in {"dev"}
        ]

        def _dep_sort_key(d: Any) -> tuple[int, int, str]:
            role_order = _ROLE_PRIORITY.get(d.role or "runtime", 5)
            eco_order = 0 if d.ecosystem == primary_ecosystem else 1
            return (role_order, eco_order, d.name.lower())

        sm.key_dependencies = sorted(direct_deps, key=_dep_sort_key)[:15]

    # LQN-02: deterministic NL summary
    sm.project_summary = ProjectSummarizer(target).generate(sm)
    sm.architecture_summary = ArchitectureSummarizer(target).generate(sm)

    # Phase 13 Plan 04: Architectural Inference (--architecture flag)
    if architecture:
        from sourcecode.architecture_analyzer import ArchitectureAnalyzer
        arch_graph = module_graph  # None if --graph-modules was not passed
        sm.architecture = ArchitectureAnalyzer().analyze(target, sm, arch_graph)

    # Git Context (--git-context flag)
    if git_context:
        from sourcecode.git_analyzer import GitAnalyzer
        sm.git_context = GitAnalyzer().analyze(target, depth=git_depth, days=git_days)

    # Env Map (--env-map flag)
    if env_map:
        from sourcecode.env_analyzer import EnvAnalyzer
        env_records, env_summary = EnvAnalyzer().analyze(target, file_tree)
        sm = replace(sm, env_map=env_records, env_summary=env_summary)

    # Code Notes (--code-notes flag)
    if code_notes:
        from sourcecode.code_notes_analyzer import CodeNotesAnalyzer
        cn_notes, cn_adrs, cn_summary = CodeNotesAnalyzer().analyze(target)
        sm = replace(sm, code_notes=cn_notes, code_adrs=cn_adrs, code_notes_summary=cn_summary)

    # Normalize optional analyzer outputs → validate schema contracts.
    # normalize_source_map fills None fields with typed empty defaults so that
    # consumers never need to null-check architecture or module_graph.
    # validate_source_map then asserts the contracts hold; it raises here
    # (pre-serialization) rather than silently producing invalid JSON.
    sm = normalize_source_map(sm)
    validate_source_map(sm)

    # Cross-analyzer semantic consistency (non-blocking: warnings to stderr).
    # strict=False so a mismatched dependency or orphan semantic link never
    # aborts a run — findings are informational until the team decides to harden.
    for _finding in validate_cross_analyzer_consistency(sm, strict=False):
        typer.echo(f"[consistency] {_finding}", err=True)

    # Build confidence summary + analysis gaps (always runs, lightweight)
    from sourcecode.confidence_analyzer import ConfidenceAnalyzer
    from dataclasses import replace as _replace
    _conf_summary, _analysis_gaps = ConfidenceAnalyzer().analyze(sm)
    sm = _replace(sm, confidence_summary=_conf_summary, analysis_gaps=_analysis_gaps)

    # E2E pipeline coherence check — emits [coherence] warnings to stderr.
    # Catches contradictory states that can survive individual-analyzer validation.
    for _issue in _check_pipeline_coherence(sm):
        typer.echo(_issue, err=True)

    # Build pipeline trace when --trace-pipeline is set.
    if trace_pipeline:
        _trace = _TraceCollector(enabled=True)
        _trace.emit("scan", "scanner", "complete",
                    reason=f"{len(sm.file_paths)} files, {len(manifests)} manifests")
        for _s in sm.stacks:
            _trace.emit("detect", _s.produced_by or "unknown", "emit_stack",
                        target=_s.stack,
                        reason=f"method={_s.detection_method} confidence={_s.confidence}")
        for _ep in sm.entry_points:
            _trace.emit("detect", _ep.produced_by or "unknown", "emit_ep",
                        target=_ep.path,
                        reason=f"type={_ep.entrypoint_type} confidence={_ep.confidence} reason={_ep.reason}")
        # Record EPs filtered from agent_view (benchmark/example with path-auxiliary parts)
        _aux_parts = frozenset({
            "benchmark", "benchmarks", "bench", "demo", "demos",
            "example", "examples", "docs", "doc", "fixtures", "fixture",
        })
        for _ep in sm.entry_points:
            _normalized_ep = normalize_entry_point(_ep)
            _ep_type = _normalized_ep.entrypoint_type
            _path_parts = _ep.path.replace("\\", "/").lower().split("/")
            _filtered = (
                _normalized_ep.classification != "production"
                or any(p in _aux_parts for p in _path_parts)
            )
            if _filtered:
                _trace.emit("output", "agent_view", "filter_ep",
                            target=_ep.path,
                            reason=f"entrypoint_type={_ep_type} (auxiliary)")
        if sm.confidence_summary is not None:
            _cs = sm.confidence_summary
            _trace.emit("confidence", "confidence_analyzer", "computed",
                        reason=(
                            f"overall={_cs.overall} "
                            f"stack={_cs.stack_confidence} "
                            f"ep={_cs.entry_point_confidence} "
                            f"anomalies={len(_cs.anomalies)}"
                        ))
        sm = _replace(sm, pipeline_trace=_trace.build_trace())

    # Contract pipeline — runs for mode=contract|standard|deep|hybrid (skip for raw)
    _is_contract_mode = mode in ("contract", "standard")
    if _is_contract_mode:
        from sourcecode.contract_pipeline import ContractPipeline
        _cp = ContractPipeline()
        _contracts, _contract_summary = _cp.run(
            target,
            sm.file_paths,
            entry_points=sm.entry_points,
            monorepo_packages=sm.monorepo_packages,
            mode=mode,
            rank_by=rank_by,  # type: ignore[arg-type]
            max_symbols=max_symbols,
            dependency_depth=dependency_depth,
            entrypoints_only=entrypoints_only,
            changed_only=changed_only,
            symbol=symbol,
            compress_types=compress_types,
        )
        sm = _replace(sm, file_contracts=_contracts, contract_summary=_contract_summary)
        if symbol is not None and len(_contracts) == 0:
            typer.echo(
                f"[warning] --symbol '{symbol}' matched 0 files. "
                "The symbol may not exist at the current --depth, or the name may differ in case. "
                "Try --depth 8 or verify the symbol name.",
                err=True,
            )
        if agent:
            typer.echo(f"[contract] {len(_contracts)} files extracted ({_contract_summary.method_breakdown})", err=True)

    # 4. Serialize
    if _is_contract_mode:
        from sourcecode.serializer import contract_view as _contract_view
        _depth = _CONTRACT_DEPTH.get(mode, "minimal")
        data = _contract_view(sm, emit_graph=emit_graph, depth=_depth)
        if not no_redact:
            data = redact_dict(data)
        content = json.dumps(data, indent=2, ensure_ascii=False)
    elif agent:
        data = agent_view(sm)
        if not no_redact:
            data = redact_dict(data)
        content = json.dumps(data, indent=2, ensure_ascii=False)
    elif compact:
        data = compact_view(sm, no_tree=no_tree)
        if not no_redact:
            data = redact_dict(data)
        content = json.dumps(data, indent=2, ensure_ascii=False)
    else:
        raw_dict = standard_view(sm, include_tree=tree and not no_tree)
        if not no_redact:
            raw_dict = redact_dict(raw_dict)

        if format == "yaml":
            from io import StringIO

            from ruamel.yaml import YAML

            yaml = YAML()
            yaml.default_flow_style = False
            yaml.representer.add_representer(
                type(None),
                lambda dumper, data_val: dumper.represent_scalar(
                    "tag:yaml.org,2002:null", "null"
                ),
            )
            stream = StringIO()
            yaml.dump(raw_dict, stream)
            content = stream.getvalue()
        else:
            content = json.dumps(raw_dict, indent=2, ensure_ascii=False)

    # 5. Telemetry (fire-and-forget, never blocks)
    try:
        from sourcecode import telemetry as _tel
        _tel.record(
            "execution_completed",
            cmd="analyze",
            flags=_active_flags(
                dependencies, graph_modules, docs, full_metrics,
                semantics, architecture, git_context, env_map,
                code_notes, agent, compact, tree, no_redact, format,
            ),
            output_fmt=format,
            file_count=len(sm.file_paths),
            duration_s=time.monotonic() - _t0,
            success=True,
        )
    except Exception:
        pass

    # 6. Write output (CLI-04)
    write_output(content, output=output)

    # 7. Clipboard copy (--copy / -c)
    if copy and output is None:
        _trimmed = content.strip()
        if _trimmed and _trimmed not in ("{}", "[]", "null"):
            if _copy_to_clipboard(content):
                typer.echo("✓ copied to clipboard", err=True)


@app.command("prepare-context")
def prepare_context_cmd(
    task: Optional[str] = typer.Argument(
        None,
        help="Task: explain | fix-bug | refactor | generate-tests | onboard | review-pr | delta",
    ),
    path: Path = typer.Argument(
        Path("."),
        help="Repository path to analyze (default: current directory)",
    ),
    since: Optional[str] = typer.Option(
        None,
        "--since",
        help="Git ref for delta task (e.g. HEAD~3, main)",
    ),
    llm_prompt: bool = typer.Option(
        False,
        "--llm-prompt",
        help="Append a ready-to-use LLM prompt to the output",
    ),
    task_help: bool = typer.Option(
        False,
        "--task-help",
        help="List available tasks with descriptions and exit",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show what would be analyzed without running it",
    ),
    copy: bool = typer.Option(
        False,
        "--copy",
        "-c",
        help="Copy output to system clipboard after a successful run. No-op when clipboard is unavailable.",
    ),
) -> None:
    """Task-specific context for AI coding agents.

    \b
    Tasks:
      explain        Architecture, entry points, key dependencies
      fix-bug        Risk-ranked files, suspected areas, annotations
      refactor       Structural issues, improvement opportunities
      generate-tests Untested source files, test gap analysis
      onboard        Full project context for new agents/developers
      review-pr      Changed files + architectural impact
      delta          Incremental context: git-changed files only

    \b
    Examples:
      sourcecode prepare-context explain
      sourcecode prepare-context explain /path/to/repo
      sourcecode prepare-context fix-bug
      sourcecode prepare-context delta --since main
      sourcecode prepare-context onboard --llm-prompt
      sourcecode prepare-context --task-help
    """
    from sourcecode.prepare_context import TASKS, TaskContextBuilder

    if task_help:
        typer.echo("Available tasks:\n")
        for name, spec in TASKS.items():
            typer.echo(f"  {name:<20} {spec.description}")
            typer.echo(f"  {'':20} Output: {spec.output_hint}\n")
        raise typer.Exit()

    if task is None:
        typer.echo(
            f"Error: task is required. Available: {', '.join(TASKS)}\n"
            "Use --task-help for descriptions.",
            err=True,
        )
        raise typer.Exit(code=1)

    if task not in TASKS:
        typer.echo(
            f"Error: unknown task '{task}'. Available: {', '.join(TASKS)}",
            err=True,
        )
        raise typer.Exit(code=1)

    target = path.resolve()
    if not target.exists() or not target.is_dir():
        typer.echo(f"Error: '{target}' is not a valid directory.", err=True)
        raise typer.Exit(code=1)

    if dry_run:
        spec = TASKS[task]
        typer.echo(f"task:        {task}")
        typer.echo(f"goal:        {spec.goal}")
        typer.echo(f"path:        {target}")
        typer.echo(f"analyzers:   dependencies={'yes' if spec.enable_dependencies else 'no'}"
                   f", code_notes={'yes' if spec.enable_code_notes else 'no'}")
        if since:
            typer.echo(f"since:       {since}")
        typer.echo(f"output:      {spec.output_hint}")
        raise typer.Exit()

    from dataclasses import asdict

    builder = TaskContextBuilder(target)
    output = builder.build(task, since=since)

    out: dict[str, Any] = {
        "task": output.task,
        "goal": output.goal,
        "project_summary": output.project_summary,
        "architecture_summary": output.architecture_summary,
        "confidence": output.confidence,
        "relevant_files": [asdict(f) for f in output.relevant_files],
        "why_these_files": output.why_these_files,
        "key_dependencies": output.key_dependencies,
    }
    if output.gaps:
        out["gaps"] = output.gaps
    if output.suspected_areas:
        out["suspected_areas"] = output.suspected_areas
    if output.improvement_opportunities:
        out["improvement_opportunities"] = output.improvement_opportunities
    if output.test_gaps:
        out["test_gaps"] = output.test_gaps
    if output.code_notes_summary:
        out["code_notes_summary"] = output.code_notes_summary
    if output.changed_files:
        out["changed_files"] = output.changed_files
    if output.affected_entry_points:
        out["affected_entry_points"] = output.affected_entry_points
    if output.limitations:
        out["limitations"] = output.limitations
    if llm_prompt:
        out["llm_prompt"] = builder.render_prompt(output)

    _pc_content = json.dumps(out, indent=2, ensure_ascii=False)
    typer.echo(_pc_content)

    if copy:
        _trimmed = _pc_content.strip()
        if _trimmed and _trimmed not in ("{}", "[]", "null"):
            if _copy_to_clipboard(_pc_content):
                typer.echo("✓ copied to clipboard", err=True)


# ── Telemetry commands ────────────────────────────────────────────────────────

@telemetry_app.command("status")
def telemetry_status() -> None:
    """Show current telemetry setting."""
    from sourcecode.telemetry.config import config_file_path, has_been_asked, is_enabled
    enabled = is_enabled()
    asked = has_been_asked()
    status = "enabled" if enabled else "disabled"
    typer.echo(f"Telemetry: {status}")
    if not asked:
        typer.echo("  (consent not yet shown — will prompt on next run)")
    typer.echo(f"  Config: {config_file_path()}")
    typer.echo("  Disable permanently: sourcecode telemetry disable")
    typer.echo("  Or set env var:      SOURCECODE_TELEMETRY=0")


@telemetry_app.command("enable")
def telemetry_enable() -> None:
    """Opt in to anonymous telemetry."""
    from sourcecode.telemetry.config import set_enabled
    from sourcecode import telemetry as _tel
    set_enabled(True)
    typer.echo("Telemetry enabled. Thank you — this helps improve sourcecode.")
    typer.echo("What is collected: version, OS, commands, flags, duration, repo size range, errors.")
    typer.echo("What is never collected: source code, paths, secrets, or any output content.")
    typer.echo("Disable at any time: sourcecode telemetry disable")
    _tel.record("telemetry_enabled", cmd="telemetry")


@telemetry_app.command("disable")
def telemetry_disable() -> None:
    """Opt out of anonymous telemetry."""
    from sourcecode.telemetry.config import set_enabled
    set_enabled(False)
    typer.echo("Telemetry disabled. No data will be collected or sent.")
    typer.echo("Re-enable at any time: sourcecode telemetry enable")


# ── version ───────────────────────────────────────────────────────────────────

@app.command("version")
def version_cmd() -> None:
    """Show version and exit."""
    typer.echo(f"sourcecode {__version__}")


# ── config ────────────────────────────────────────────────────────────────────

@app.command("config")
def config_cmd() -> None:
    """Show current configuration."""
    from sourcecode.telemetry.config import config_file_path, is_enabled
    typer.echo(f"sourcecode {__version__}")
    typer.echo(f"Config:    {config_file_path()}")
    typer.echo(f"Telemetry: {'enabled' if is_enabled() else 'disabled'}")
    typer.echo("")
    typer.echo("Manage telemetry:")
    typer.echo("  sourcecode telemetry enable")
    typer.echo("  sourcecode telemetry disable")
    typer.echo("  sourcecode telemetry status")


# ── analyze (legacy alias) ────────────────────────────────────────────────────

@app.command("analyze", hidden=True)
def analyze_cmd(
    path: Path = typer.Argument(Path("."), help="Repository path to analyze"),
) -> None:
    """[deprecated] Use: sourcecode [PATH]"""
    typer.echo(
        "Warning: 'analyze' subcommand is deprecated.\n"
        "Use:  sourcecode .\n"
        "      sourcecode /path/to/repo",
        err=True,
    )
    raise typer.Exit(code=1)


# ── Entry point ───────────────────────────────────────────────────────────────

def main_entry() -> None:
    """CLI entry point.

    Calls _preprocess_argv() before Typer/Click parses sys.argv so that
    repository path tokens are extracted before Click's Group callback
    can consume them as positional arguments (which would prevent subcommand
    routing for tokens like 'version' or 'config').
    """
    _preprocess_argv()
    app()

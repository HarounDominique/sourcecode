from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any, Optional, cast

import typer

from sourcecode import __version__
from sourcecode.entrypoint_classifier import is_production_entry_point, normalize_entry_point
from sourcecode.progress import Progress
from sourcecode.repository_ir import extract_java_endpoints as _extract_java_endpoints


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

def _build_help_text() -> str:
    """Build --help text dynamically based on current license state."""
    try:
        from sourcecode.license import is_pro as _is_pro
    except Exception:
        _is_pro = False

    if _is_pro:
        plan_badge = "[bold green]● Pro[/bold green]"
    else:
        plan_badge = "[yellow]Free[/yellow]  ·  [dim]sourcecode activate <key>[/dim] to unlock Pro"

    text = f"""\
[bold]sourcecode[/bold]  {plan_badge}

Deterministic codebase context for AI coding agents.

[bold]Primary usage:[/bold]
  sourcecode --compact                  high-signal summary (~600-800 tokens)
  sourcecode --compact --git-context    include git hotspots and uncommitted files

[bold]Examples:[/bold]
  sourcecode saint-server --compact
  sourcecode . --compact --git-context --copy
  sourcecode . --changed-only --git-context
  sourcecode prepare-context onboard saint-server
  sourcecode prepare-context delta . --since main

[bold]Subcommands:[/bold]
  prepare-context TASK [PATH]  [dim]# task-specific context (onboard, delta, fix-bug, ...)[/dim]
  mcp init                     [dim]# setup MCP integration (Claude Desktop, Cursor)[/dim]
  mcp status                   [dim]# show MCP integration status[/dim]
  mcp remove                   [dim]# remove MCP integration safely[/dim]
  mcp serve                    [dim]# start MCP server for AI agent integration[/dim]
  telemetry status|enable|disable
  version
"""

    if not _is_pro:
        text += """\

[dim bold]Locked (Pro):[/dim bold]
  [dim]impact                              blast radius before any change[/dim]
  [dim]modernize (full)                    dead zones, tangles, full coupling[/dim]
  [dim]fix-bug (full)                      complete risk-ranked file list[/dim]
  [dim]review-pr (expanded)               CI-grade PR review[/dim]
  [dim]prepare-context delta               incremental context for CI/CD[/dim]
  [dim]prepare-context generate-tests      test gap analysis[/dim]
  [dim]--full                              removes all truncation limits[/dim]

  [dim cyan]→ sourcecode activate <key>[/dim cyan]
"""

    return text


_HELP = _build_help_text()

# Known subcommand names — tokens matching these are routed as subcommands,
# not consumed as a repository path.
_SUBCOMMANDS: frozenset[str] = frozenset(
    {
        "telemetry", "prepare-context", "version", "config", "analyze",
        "repo-ir", "mcp", "endpoints", "impact",
        # Enterprise workflow commands
        "onboard", "modernize", "fix-bug", "review-pr",
        # License
        "activate",
    }
)

# Thread-local storage for the path extracted by _preprocess_argv().
# Using threading.local() prevents concurrent MCP tool calls from clobbering
# each other's target path (the old module-level list was a shared mutable global).
_tls = threading.local()


def _get_detected_path() -> str:
    """Return the thread-local detected path, defaulting to '.'."""
    return _tls.__dict__.get("detected_path", ".")


def _set_detected_path(value: str) -> None:
    """Set the thread-local detected path."""
    _tls.detected_path = value


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
    "--max-importers",
    "--exclude",
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
    _path_index: int = -1
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
        _set_detected_path(arg)
        _path_index = i
        break
    if _path_index >= 0:
        result.pop(_path_index)
    return result


def _preprocess_argv() -> None:
    """Apply _preprocess_args to sys.argv in-place (used by main_entry)."""
    import sys as _sys
    modified = _preprocess_args(_sys.argv[1:])
    _sys.argv = _sys.argv[:1] + modified


def _emit_error_json(error: str, message: str, **context: object) -> None:
    """Write a structured JSON error envelope to stderr.

    Format: {"error": "<code>", "message": "<human text>", ...<context>}
    All CLI validation and runtime errors must go through this helper so that
    agents and tools can parse stderr reliably regardless of error type.
    """
    import json as _json
    import sys as _sys
    payload: dict[str, object] = {"error": error, "message": message}
    payload.update(context)
    _sys.stderr.write(_json.dumps(payload, ensure_ascii=False) + "\n")
    _sys.stderr.flush()


# H-06: Intercept Click-level UsageError (unknown options, bad args) and emit JSON.
# Click's default show() writes "Error: No such option: --foo" as plain text.
# Automation consumers need JSON on stderr regardless of how the error originated.
try:
    import click.exceptions as _click_exc

    def _json_click_usage_error_show(self: Any, file: Any = None) -> None:  # type: ignore[override]
        import json as _je
        import sys as _jse
        _code_map = {
            "NoSuchOption": "invalid_option",
            "BadOptionUsage": "invalid_option",
            "BadParameter": "bad_parameter",
            "MissingParameter": "missing_required",
            "BadArgumentUsage": "bad_argument",
        }
        code = _code_map.get(type(self).__name__, "invalid_option")
        payload: dict[str, object] = {"error": code, "message": self.format_message()}
        _opt = getattr(self, "option_name", None) or getattr(self, "param_hint", None)
        if _opt:
            payload["flag"] = str(_opt).strip("'\"")
        _jse.stderr.write(_je.dumps(payload, ensure_ascii=False) + "\n")
        _jse.stderr.flush()

    _click_exc.UsageError.show = _json_click_usage_error_show  # type: ignore[method-assign]
except Exception:
    pass  # click unavailable — plain-text fallback


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

    # Refresh help text at invocation time so it reflects current license state.
    cmd.help = _build_help_text()

    _orig_cmd_main = cmd.main

    def _cmd_main(args: Optional[list[str]] = None, **kwargs: Any) -> Any:
        if args is not None:
            # CliRunner / programmatic call: preprocess the explicit args list.
            _set_detected_path(".")
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

mcp_app = typer.Typer(help="MCP integration: setup, status, serve, remove.", rich_markup_mode="rich")
app.add_typer(mcp_app, name="mcp")


def _maybe_ask_consent() -> None:
    """Show first-run consent prompt once, on interactive TTYs only."""
    try:
        from sourcecode.telemetry.config import has_been_asked, set_enabled
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


def _maybe_show_mcp_hint() -> None:
    """Show MCP integration hint once after first install, on TTY only."""
    import sys as _sys
    try:
        if not _sys.stderr.isatty():
            return
        from sourcecode.telemetry.config import _CONFIG_FILE, _load, _save
        data = _load()
        if data.get("mcp", {}).get("hint_shown"):
            return
        typer.echo("", err=True)
        typer.echo("  MCP integration available:", err=True)
        typer.echo("  → sourcecode mcp init", err=True)
        typer.echo("", err=True)
        data.setdefault("mcp", {})["hint_shown"] = True
        _save(data)
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
        help="Output format: json (default) or yaml.",
        show_default=True,
    ),
    output: Optional[Path] = typer.Option(
        None,
        "--output",
        "-o",
        help="Write output to a file instead of stdout.",
    ),
    compact: bool = typer.Option(
        False,
        "--compact",
        help=(
            "High-signal summary (typically 1000–3000 tokens depending on repo size): "
            "stacks, entry points, dependency summary, confidence, and gaps. "
            "Includes security_surface (when @M3FiltroSeguridad detected), mybatis (when MyBatis framework detected), and transactional_boundaries for Java projects. "
            "Use --agent for maximum signal."
        ),
    ),
    dependencies: bool = typer.Option(
        False,
        "--dependencies",
        hidden=True,
        help="Analyze direct dependencies from manifests and lockfiles.",
    ),
    graph_modules: bool = typer.Option(
        False,
        "--graph-modules",
        hidden=True,
        help="Include a structural module graph in output.",
    ),
    graph_detail: str = typer.Option(
        "high",
        "--graph-detail",
        hidden=True,
        help="Detail level for --graph-modules: high, medium, full.",
        show_default=True,
    ),
    max_nodes: Optional[int] = typer.Option(
        None,
        "--max-nodes",
        hidden=True,
        help="Maximum nodes in --graph-modules output.",
        min=1,
    ),
    graph_edges: Optional[str] = typer.Option(
        None,
        "--graph-edges",
        hidden=True,
        help="Edge types for --graph-modules, comma-separated: imports,calls,contains,extends.",
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
        hidden=True,
        help="Include the full file_tree and flat file_paths list in output.",
    ),
    no_redact: bool = typer.Option(
        False,
        "--no-redact",
        help="Disable secret redaction of output strings. Note: env var values from the OS are never included in output regardless of this flag (security policy).",
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
        help="File tree traversal depth (default: 4). Java/Maven projects auto-adjust to a minimum of 12; values below 12 have no effect on Java projects.",
        min=1,
        max=20,
    ),
    docs: bool = typer.Option(
        False,
        "--docs",
        hidden=True,
        help="Extract documentation: docstrings, function signatures, and module-level comments.",
    ),
    docs_depth: str = typer.Option(
        "symbols",
        "--docs-depth",
        hidden=True,
        help="Documentation extraction depth: module, symbols, full.",
        show_default=True,
    ),
    full_metrics: bool = typer.Option(
        False,
        "--full-metrics",
        hidden=True,
        help="Technical audit: LOC, complexity, test coverage per file.",
    ),
    semantics: bool = typer.Option(
        False,
        "--semantics",
        hidden=True,
        help="Cross-file symbol resolution and call graph analysis.",
    ),
    architecture: bool = typer.Option(
        False,
        "--architecture",
        hidden=True,
        help="Architectural layer inference (MVC/hexagonal/layered).",
    ),
    git_context: bool = typer.Option(
        False,
        "--git-context",
        "-g",
        help="Include git activity: recent commits, change hotspots, and uncommitted changes.",
    ),
    git_depth: int = typer.Option(
        20,
        "--git-depth",
        hidden=True,
        help="Number of recent commits to include with --git-context (default: 20).",
        min=1,
        max=100,
    ),
    git_days: int = typer.Option(
        90,
        "--git-days",
        hidden=True,
        help="Time window in days for change hotspot detection (default: 90).",
        min=1,
        max=3650,
    ),
    env_map: bool = typer.Option(
        False,
        "--env-map",
        hidden=True,
        help="Map environment variables referenced across the codebase.",
    ),
    code_notes: bool = typer.Option(
        False,
        "--code-notes",
        hidden=True,
        help="Extract inline annotations: TODO, FIXME, HACK, DEPRECATED, ADRs.",
    ),
    agent: bool = typer.Option(
        False,
        "--agent",
        help="Structured noise-free JSON for AI agents: identity, entry points, dependencies, confidence, gaps.",
    ),
    full: bool = typer.Option(
        False,
        "--full",
        help="Remove truncation limits on transactional_boundaries, mybatis.dto_mappers, and other capped lists.",
    ),
    trace_pipeline: bool = typer.Option(
        False,
        "--trace-pipeline",
        hidden=True,
        help="Diagnostic: include full pipeline trace in output.",
    ),
    mode: str = typer.Option(
        "contract",
        "--mode",
        hidden=True,
        help="Output mode: contract (default) | standard | raw.",
    ),
    max_symbols: Optional[int] = typer.Option(
        None,
        "--max-symbols",
        hidden=True,
        help="Limit total exported semantic nodes across all file contracts.",
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
        hidden=True,
        help="Include only files with exported symbols or runtime entrypoints.",
    ),
    changed_only: bool = typer.Option(
        False,
        "--changed-only",
        help="Limit output to git-modified files (staged, unstaged, untracked).",
    ),
    rank_by: str = typer.Option(
        "relevance",
        "--rank-by",
        hidden=True,
        help="Contract ranking strategy: relevance (default) | centrality | git-churn.",
    ),
    emit_graph: bool = typer.Option(
        False,
        "--emit-graph",
        hidden=True,
        help="Include a compact dependency graph in contract output.",
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
        hidden=True,
        help="Extract context for a specific symbol (Python/TS/JS only — not supported for Java).",
    ),
    max_importers: int = typer.Option(
        50,
        "--max-importers",
        hidden=True,
        help="Maximum importer files returned by --symbol (default: 50).",
        min=1,
        max=10000,
    ),
    copy: bool = typer.Option(
        False,
        "--copy",
        "-c",
        help="Copy output to system clipboard after a successful run. No-op when --output is used or clipboard is unavailable.",
    ),
    exclude: Optional[str] = typer.Option(
        None,
        "--exclude",
        help="Additional directories/patterns to exclude, comma-separated (e.g. 'legacy,generated').",
    ),
    no_cache: bool = typer.Option(
        False,
        "--no-cache",
        help="Bypass the scan cache and force a fresh analysis.",
    ),
) -> None:
    """Analyze a repository and produce structured context for AI coding agents.

    \b
    Examples:
      sourcecode --compact              high-signal summary (recommended)
      sourcecode --compact --git-context  include git hotspots
      sourcecode /path/to/repo --compact  analyze specific path
      sourcecode --agent                agent-optimized output (full detail)
    """
    # First-run consent (skip for telemetry/version/config subcommands)
    if ctx.invoked_subcommand not in ("telemetry", "version", "config"):
        _maybe_ask_consent()
        _maybe_show_mcp_hint()

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
        raise typer.Exit(code=2)  # FIX-P2-7: arg validation → exit 2
    _RANK_CHOICES = ("relevance", "centrality", "git-churn")
    if rank_by not in _RANK_CHOICES:
        typer.echo(
            f"Error: invalid value '{rank_by}' for --rank-by. Valid options: {', '.join(_RANK_CHOICES)}",
            err=True,
        )
        raise typer.Exit(code=2)  # FIX-P2-7: arg validation → exit 2

    if symbol is not None and not symbol.strip():
        typer.echo("symbol query cannot be empty", err=True)
        raise typer.Exit(code=2)

    if symbol and mode not in ("contract", "standard"):
        typer.echo(
            f"Error: --symbol requires --mode contract or standard (got '{mode}'). "
            "Symbol search uses the contract pipeline which does not run in raw mode.",
            err=True,
        )
        raise typer.Exit(code=2)  # FIX-P2-7: arg validation → exit 2

    if entrypoints_only and mode not in ("contract", "standard"):
        typer.echo(
            f"Error: --entrypoints-only requires --mode contract or standard (got '{mode}').",
            err=True,
        )
        raise typer.Exit(code=2)  # FIX-P2-7: arg validation → exit 2

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

    # Pro gate for --full: removing truncation limits is enterprise-scale functionality.
    if full:
        from sourcecode.license import require_feature as _req_full
        _req_full("--full")

    # P0-2 FIX: --compact and --full are mutually exclusive.
    # compact is designed to be a bounded summary; --full removes truncation limits,
    # which contradicts compact's purpose. Use --agent --full for expanded output.
    if compact and full:
        import json as _json_flags, sys as _sys_flags
        _sys_flags.stdout.write(_json_flags.dumps({
            "error": "incompatible_flags",
            "message": "--compact and --full are mutually exclusive. "
                       "--compact produces a bounded summary; --full removes truncation limits "
                       "and is meant for --agent mode. Use --agent --full for expanded output.",
            "exit_code": 1,
        }, ensure_ascii=False) + "\n")
        _sys_flags.stdout.flush()
        raise typer.Exit(code=1)

    # P0-2 FIX: --full without --compact or --agent has no effect in contract/raw mode.
    # Warn so the user knows the flag is not doing anything.
    if full and not compact and not agent:
        typer.echo(
            "[warning] --full has no effect in contract/raw mode. "
            "It only expands mybatis.dto_mappers and transactional_boundaries in "
            "--compact or --agent mode. Add --agent to get expanded output.",
            err=True,
        )

    # P1-2 FIX: --changed-only silently implies --compact; inform only on TTY.
    # PowerShell 5.1 interprets any stderr write (even with exit 0) as NativeCommandError.
    # Gate on isatty() so pipeline consumers never see informational noise on stderr.
    import sys as _sys_tty
    if changed_only and not compact and not agent and _sys_tty.stderr.isatty():
        typer.echo(
            "[info] --changed-only implies --compact (bounding output to changed files).",
            err=True,
        )

    # Validate format choices
    if format not in FORMAT_CHOICES:
        _emit_error_json(
            "invalid_flag_value",
            f"Invalid value '{format}' for --format. Valid values: {', '.join(FORMAT_CHOICES)}.",
            flag="--format",
            value=format,
            valid_values=list(FORMAT_CHOICES),
        )
        raise typer.Exit(code=2)  # FIX-P2-7: arg validation → exit 2
    if graph_detail not in GRAPH_DETAIL_CHOICES:
        _emit_error_json(
            "invalid_flag_value",
            f"Invalid value '{graph_detail}' for --graph-detail. Valid values: {', '.join(GRAPH_DETAIL_CHOICES)}.",
            flag="--graph-detail",
            value=graph_detail,
            valid_values=list(GRAPH_DETAIL_CHOICES),
        )
        raise typer.Exit(code=2)  # FIX-P2-7: arg validation → exit 2
    if docs_depth not in DOCS_DEPTH_CHOICES:
        _emit_error_json(
            "invalid_flag_value",
            f"Invalid value '{docs_depth}' for --docs-depth. Valid values: {', '.join(DOCS_DEPTH_CHOICES)}.",
            flag="--docs-depth",
            value=docs_depth,
            valid_values=list(DOCS_DEPTH_CHOICES),
        )
        raise typer.Exit(code=2)  # FIX-P2-7: arg validation → exit 2

    # Path was extracted from argv by _preprocess_argv() before Click ran.
    # FIX-P2-8: preserve original user input in error messages (Windows Git Bash
    # rewrites "/nonexistent" → "C:\Program Files\Git\nonexistent" via Path.resolve()).
    _raw_path_input = _get_detected_path()
    target = Path(_raw_path_input).resolve()
    if not target.exists():
        _emit_error_json(
            "directory_not_found",
            f"Directory '{_raw_path_input}' does not exist.",
            path=_raw_path_input,
        )
        raise typer.Exit(code=1)
    if not target.is_dir():
        _emit_error_json(
            "not_a_directory",
            f"Path '{_raw_path_input}' is not a directory.",
            path=_raw_path_input,
        )
        raise typer.Exit(code=1)

    # Normalize mode aliases
    _CONTRACT_MODES = frozenset({"contract", "minimal", "standard"})
    _user_mode_explicit = mode not in ("contract",)  # track if user passed a non-default value
    if mode == "minimal":
        mode = "contract"   # minimal is a documented alias for contract
    elif mode not in _CONTRACT_MODES and mode != "raw":
        mode = "contract"   # unknown → safe default

    # --changed-only forces compact output to bound size; full contract/raw scan
    # of a large repo produces 100KB+ even with only 2 changed files.
    if changed_only and not compact and not agent:
        compact = True

    # Legacy flags imply raw mode unless --mode was explicitly overridden.
    # --format yaml and --graph-modules are now compatible with contract_view:
    #   yaml is a serialization format (not an output-section flag)
    #   graph-modules output is included in contract_view when available
    # Other flags that produce sections exclusive to standard_view still force raw.
    _legacy_flags_active = (
        compact or tree or trace_pipeline
        or docs or semantics or full_metrics or architecture
    )
    if mode in ("contract", "standard") and _legacy_flags_active:
        if _user_mode_explicit:
            _overriding_flags = [
                f for f, v in [
                    ("--compact", compact), ("--tree", tree),
                    ("--trace-pipeline", trace_pipeline), ("--docs", docs),
                    ("--semantics", semantics), ("--full-metrics", full_metrics),
                    ("--architecture", architecture),
                ] if v
            ]
            typer.echo(
                f"[warning] --mode {mode} was overridden to raw because legacy flags "
                f"({', '.join(_overriding_flags)}) require raw output mode.",
                err=True,
            )
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
    _java_min_depth = 12
    effective_depth = max(depth, _java_min_depth) if _is_java and depth < _java_min_depth else depth

    if symbol is not None and _is_java:
        typer.echo(
            f"Error: --symbol is not supported for Java/JVM repositories. "
            "Per-file AST extraction is unavailable for JVM — symbol search only works with Python, TypeScript, and JavaScript. "
            "Alternatives: use --agent --compact to get file relevance scores, "
            "or use --git-context to find recently changed files.",
            err=True,
        )
        raise typer.Exit(code=1)

    # --agent: enable signal analyzers; output via agent_view (not compact)
    if agent:
        dependencies = True
        env_map = True
        code_notes = True
        no_tree = True  # agents never need the raw file tree
        architecture = True  # agents need full architectural signal (M4)
        graph_modules = True  # IC-003: import graph needed for architecture confidence

    # ── Two-layer cache ────────────────────────────────────────────────────────
    # L1 (core): (repo, commit, analysis_flags) → pre-computed view data dict
    #            key  = core-<git_sha>-<analysis_hash>.json.gz
    # L2 (view): (core_hash, view_flags)        → final rendered string
    #            key  = view-<core_hash16>-<view_hash>.json.gz
    #
    # Lookup order: L2 exact hit → L1 hit + view rebuild → full analysis
    # Write order:  full analysis → write L1 core → write L2 view
    #
    # Flags split:
    #   core (analysis) — affect WHAT is analysed; same core for any view
    #   view            — affect HOW it's presented; same view for any format variant
    import hashlib as _hashlib
    import subprocess as _sub
    from sourcecode import cache as _cache_mod

    _cache_hit_content: Optional[str] = None
    _git_sha = ""
    _core_key = ""
    _view_key = ""
    _core_hash = ""
    _core_flags_str = ""
    _view_flags_str = ""

    if not no_cache:
        try:
            _sha_r = _sub.run(
                ["git", "-C", str(target), "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, timeout=3,
            )
            _git_sha = _sha_r.stdout.strip()
            # Only cache when target IS the git repo root (not a subdir of one),
            # to avoid polluting sub-project directories used in tests.
            if _git_sha and (target / ".git").exists():
                from sourcecode import __version__ as _sc_version
                _excl_key = (
                    ",".join(sorted(e.strip() for e in exclude.split(",") if e.strip()))
                    if exclude else ""
                )

                # ── Core (analysis) flags: affect which analyzers run + scan config ──
                # Use effective_depth (not raw depth) so Java auto-adjustment is captured.
                _core_flags_str = (
                    f"v={_sc_version},"
                    f"dep={dependencies},gm={graph_modules},"
                    f"docs={docs},fm={full_metrics},sem={semantics},"
                    f"arch={architecture},gc={git_context},em={env_map},"
                    f"cn={code_notes},mode={mode},"
                    f"ex={_excl_key},depth={effective_depth}"
                )
                _core_h = _hashlib.sha256(_core_flags_str.encode()).hexdigest()[:8]
                _core_key = f"{_git_sha}-{_core_h}"

                # ── View flags: output presentation only (no re-analysis needed) ──
                _view_flags_str = (
                    f"c={compact},ag={agent},fmt={format},full={full},"
                    f"co={changed_only},tree={tree},nt={no_tree},"
                    f"rb={rank_by},sym={symbol},ep={entrypoints_only},"
                    f"nr={no_redact},gd={graph_detail},dd={docs_depth},"
                    f"mn={max_nodes},ge={graph_edges},mi={max_importers},"
                    f"eg={emit_graph}"
                )
                _view_h = _hashlib.sha256(_view_flags_str.encode()).hexdigest()[:8]

                # ── Lookup ──────────────────────────────────────────────────────
                # Step 1: try L1 to obtain the core_hash needed for L2 key
                _l1_result = _cache_mod.read_core(target, _core_key)
                if _l1_result is not None:
                    _core_dict_l1, _core_hash = _l1_result
                    _view_key = f"{_core_hash}-{_view_h}"

                    # Step 2: try L2 (exact view match)
                    _cache_hit_content = _cache_mod.read_view(target, _view_key)

                    # Step 3: L1 hit but L2 miss → rebuild view from core dict
                    if _cache_hit_content is None:
                        try:
                            from sourcecode.serializer import build_view_from_core as _bvfc
                            _rebuilt = _bvfc(
                                _core_dict_l1,
                                compact=compact,
                                agent=agent,
                                full=full,
                                no_tree=no_tree,
                                tree=tree,
                            )
                            if _rebuilt is not None:
                                # Apply redaction
                                if not no_redact:
                                    from sourcecode.redactor import redact_dict as _red_l1
                                    _rebuilt = _red_l1(_rebuilt)
                                # Apply output budget
                                if agent:
                                    from sourcecode.output_budget import (
                                        trim_to_budget as _trim_l1,
                                        BUDGET_AGENT,
                                    )
                                    _rebuilt = _trim_l1(_rebuilt, BUDGET_AGENT, label="agent")
                                elif compact:
                                    from sourcecode.output_budget import (
                                        trim_to_budget as _trim_l1c,
                                        BUDGET_COMPACT,
                                    )
                                    _rebuilt = _trim_l1c(_rebuilt, BUDGET_COMPACT, label="compact")
                                # Serialize
                                if format == "yaml":
                                    from io import StringIO as _SIO_L1
                                    from ruamel.yaml import YAML as _YAML_L1
                                    _yl1 = _YAML_L1()
                                    _yl1.default_flow_style = False
                                    _yl1.representer.add_representer(
                                        type(None),
                                        lambda d, v: d.represent_scalar(
                                            "tag:yaml.org,2002:null", "null"
                                        ),
                                    )
                                    _sl1 = _SIO_L1()
                                    _yl1.dump(_rebuilt, _sl1)
                                    _cache_hit_content = _sl1.getvalue()
                                else:
                                    import json as _json_l1
                                    _cache_hit_content = _json_l1.dumps(
                                        _rebuilt, indent=2, ensure_ascii=False
                                    )
                                # Cache rebuilt view in L2
                                if _cache_hit_content:
                                    _cache_mod.write_view(
                                        target,
                                        _view_key,
                                        _cache_hit_content,
                                        fmt=format,
                                    )
                        except Exception:
                            _cache_hit_content = None  # rebuild failed → full analysis

        except Exception:
            _git_sha = ""
            _core_key = ""
            _view_key = ""
            _core_hash = ""

    if _cache_hit_content is not None:
        from sourcecode.serializer import write_output
        write_output(_cache_hit_content, output=output)
        if copy and not output:
            _copy_to_clipboard(_cache_hit_content)
        return

    _extra_excludes: Optional[frozenset[str]] = None
    if exclude:
        _extra_excludes = frozenset(e.strip() for e in exclude.split(",") if e.strip())
        # IMP-2: warn if the exclude value looks like it was swallowed as a path
        # (BUG-2 symptom in older versions: --exclude value consumed as repo path).
        import sys as _sys_warn
        if len(_extra_excludes) == 1 and Path(list(_extra_excludes)[0]).is_dir():
            _sys_warn.stderr.write(
                f"[sourcecode] Warning: --exclude value '{list(_extra_excludes)[0]}' is a directory path. "
                "If this was meant as a pattern, use --exclude=pattern or --exclude pattern (both are supported).\n"
            )
            _sys_warn.stderr.flush()

    _progress = Progress()
    _progress.start("scanning files")

    scanner = AdaptiveScanner(target, topology=_topology, base_depth=effective_depth,
                               extra_excludes=_extra_excludes)
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

    _progress.update("parsing manifests")
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
    # architecture=True is also enabled so that architecture.confidence is
    # consistent with --agent (which auto-enables architecture).  The
    # ArchitectureAnalyzer is path-based and adds negligible latency.
    if compact:
        dependencies = True
        env_map = True
        code_notes = True
        architecture = True

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
        # BUG-2: skip workspaces explicitly excluded via --exclude.
        # Without this guard, excluded frontend/backend modules still contribute
        # their stacks and entry_points, causing architecture_summary to describe
        # stacks that were intentionally filtered out.
        if _extra_excludes:
            _ws_norm = workspace.path.replace("\\", "/").strip("/")
            _ws_parts = frozenset(_ws_norm.split("/"))
            if _ws_parts & _extra_excludes:
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
        extra_excludes=sorted(_extra_excludes) if _extra_excludes else [],
    )

    # Populate Java-specific root fields from java stack detection (FIX-6, 7, 8)
    _java_stack = next((s for s in stacks if s.stack == "java"), None)
    if _java_stack is not None:
        from dataclasses import replace as _dc_replace
        sm = _dc_replace(sm,
            packaging=getattr(_java_stack, "packaging", None) or None,
            language_version=getattr(_java_stack, "language_version", None) or None,
            spring_profiles=getattr(_java_stack, "spring_profiles", []) or [],
            app_server_hint=getattr(_java_stack, "app_server_hint", None) or None,
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

        _JAVA_SEMANTIC_PRIORITY: dict[str, int] = {
            "spring-boot": 0, "spring-security": 1, "mybatis": 2,
            "poi": 3, "pdfbox": 4, "jackson": 5, "jjwt": 6,
        }

        def _java_priority(d: Any) -> int:
            if d.ecosystem != "java":
                return 99
            art = (d.name.split(":")[-1] if ":" in d.name else d.name).lower()
            for key, pri in _JAVA_SEMANTIC_PRIORITY.items():
                if key in art:
                    return pri
            return 50

        def _dep_sort_key(d: Any) -> tuple[int, int, int, str]:
            role_order = _ROLE_PRIORITY.get(d.role or "runtime", 5)
            eco_order = 0 if d.ecosystem == primary_ecosystem else 1
            return (role_order, eco_order, _java_priority(d), d.name.lower())

        _seen_dep_names: set[str] = set()
        _deduped_deps: list[Any] = []
        for d in sorted(direct_deps, key=_dep_sort_key):
            if d.name not in _seen_dep_names:
                _seen_dep_names.add(d.name)
                _deduped_deps.append(d)
        sm.key_dependencies = _deduped_deps  # no cap — all direct deps included

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
        _progress.update("git analysis")
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

    # Pre-compute uncommitted files for --changed-only.
    # The contract pipeline filter and git_context are two separate subsystems;
    # wire them here so the pipeline uses git_context data, not an independent git call.
    _allowed_changed_files: Optional[set[str]] = None
    if changed_only:
        from sourcecode.git_analyzer import GitAnalyzer as _GitAnalyzerEarly
        try:
            _gc_early = _GitAnalyzerEarly().analyze(target, depth=1, days=1)
            _bad_gc = {"no_git_repo", "git_not_found", "git_timeout"}
            if _gc_early and not (_bad_gc & set(_gc_early.limitations)):
                _uc = _gc_early.uncommitted_changes
                if _uc:
                    # WORKTREE_UNSTAGED + WORKTREE_STAGED only; untracked excluded
                    _allowed_changed_files = set(_uc.staged) | set(_uc.unstaged)
            if not _allowed_changed_files:
                typer.echo(
                    "[changed-only] git unavailable or no uncommitted changes — falling back to full scan.",
                    err=True,
                )
                changed_only = False
        except Exception:
            typer.echo("[changed-only] git error — falling back to full scan.", err=True)
            changed_only = False

    # Contract pipeline — runs for mode=contract|standard|deep|hybrid (skip for raw)
    _progress.update("extracting contracts")
    _is_contract_mode = mode in ("contract", "standard")
    _pipeline_error = False
    if _is_contract_mode:
        from sourcecode.contract_pipeline import ContractPipeline
        from sourcecode.contract_model import ContractSummary as _ContractSummary
        # FIX-1: Java projects need higher caps — many files, comprehensive coverage required
        _jvm_stacks = {"java", "kotlin", "scala", "groovy"}
        _is_jvm = any(s.stack in _jvm_stacks for s in sm.stacks)
        # FIX-1: Java projects need higher caps and no relevance threshold
        _max_files_cp = 2500 if _is_jvm else 500
        _cp = ContractPipeline(max_files=_max_files_cp)
        _java_pipeline_kwargs: dict = {}
        if _is_jvm:
            _java_pipeline_kwargs["max_contracts"] = 500
            _java_pipeline_kwargs["min_score"] = 0.0
        try:
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
                max_importers=max_importers,
                semantic_calls=sm.semantic_calls or None,
                code_notes=sm.code_notes or None,
                allowed_changed_files=_allowed_changed_files,
                **_java_pipeline_kwargs,
            )
        except Exception as _exc:
            typer.echo(f"[error] contract pipeline failed: {_exc}", err=True)
            _pipeline_error = True
            _contracts = []
            _contract_summary = _ContractSummary(
                mode=mode,
                total_files=0,
                extracted_files=0,
                filtered_files=0,
                method_breakdown={},
                ranked_by=rank_by,
                limitations=[f"pipeline_error: {type(_exc).__name__}"],
            )
        sm = _replace(sm, file_contracts=_contracts, contract_summary=_contract_summary)
        if symbol is not None and len(_contracts) == 0:
            _jvm_stacks = {"java", "kotlin", "scala", "groovy"}
            _is_jvm_repo = any(s.stack in _jvm_stacks for s in sm.stacks)
            if _is_jvm_repo:
                typer.echo(
                    f"[warning] --symbol '{symbol}' matched 0 files. "
                    "Per-file AST extraction is not available for Java/JVM repos — "
                    "symbol search works only with Python, TypeScript, and JavaScript. "
                    "Use --git-context or --code-notes for JVM navigation.",
                    err=True,
                )
            else:
                typer.echo(
                    f"[warning] --symbol '{symbol}' matched 0 files. "
                    "The symbol may not exist, the name may differ in case, "
                    "or the file may be outside the scanned depth. "
                    "Try --depth 8 or verify the symbol name.",
                    err=True,
                )

    # 4. Serialize
    _progress.update("serializing output")
    if _is_contract_mode and not agent:
        from sourcecode.serializer import contract_view as _contract_view
        _depth = _CONTRACT_DEPTH.get(mode, "minimal")
        data = _contract_view(sm, emit_graph=emit_graph, depth=_depth)
        if not no_redact:
            data = redact_dict(data)
        if format == "yaml":
            from io import StringIO
            from ruamel.yaml import YAML as _YAML
            _yaml = _YAML()
            _yaml.default_flow_style = False
            _yaml.representer.add_representer(
                type(None),
                lambda dumper, data_val: dumper.represent_scalar(
                    "tag:yaml.org,2002:null", "null"
                ),
            )
            _stream = StringIO()
            _yaml.dump(data, _stream)
            content = _stream.getvalue()
        else:
            content = json.dumps(data, indent=2, ensure_ascii=False)
    elif agent:
        data = agent_view(sm, full=full)
        if not no_redact:
            data = redact_dict(data)
        # P0-1: Apply output budget — safety net for large repos.
        from sourcecode.output_budget import trim_to_budget as _trim, BUDGET_AGENT
        data = _trim(data, BUDGET_AGENT, label="agent")
        # FIX-P0-2: agent mode must honour --format yaml (previously always emitted JSON).
        if format == "yaml":
            from io import StringIO
            from ruamel.yaml import YAML as _YAML
            _yaml_ag = _YAML()
            _yaml_ag.default_flow_style = False
            _yaml_ag.representer.add_representer(
                type(None),
                lambda dumper, data_val: dumper.represent_scalar(
                    "tag:yaml.org,2002:null", "null"
                ),
            )
            _stream_ag = StringIO()
            _yaml_ag.dump(data, _stream_ag)
            content = _stream_ag.getvalue()
        else:
            content = json.dumps(data, indent=2, ensure_ascii=False)
    elif compact:
        if changed_only and _allowed_changed_files:
            # GAP-5: preserve full entry_points for architecture context even in
            # --changed-only mode. Only filter file_paths and code_notes.
            # ALWAYS-INCLUDE: security-const files must stay in file_paths even when
            # not in the git diff — they resolve Java constant references used in
            # @M3FiltroSeguridad annotations (read-only anchors, not diff output).
            def _is_always_include_ref(p: str) -> bool:
                name = p.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
                if name.endswith("Const.java") or name.endswith("Constants.java"):
                    return True
                parts = p.replace("\\", "/").lower().split("/")
                return any(seg in ("security", "seguridad", "constantes") for seg in parts)

            sm = _replace(sm,
                file_paths=[
                    p for p in sm.file_paths
                    if p in _allowed_changed_files or _is_always_include_ref(p)
                ],
                code_notes=[n for n in sm.code_notes if n.path in _allowed_changed_files],
            )
        data = compact_view(sm, no_tree=no_tree, full=full)
        if not no_redact:
            data = redact_dict(data)
        # P0-1: Apply output budget — safety net for large repos.
        from sourcecode.output_budget import trim_to_budget as _trim_c, BUDGET_COMPACT
        data = _trim_c(data, BUDGET_COMPACT, label="compact")
        if format == "yaml":
            from io import StringIO
            from ruamel.yaml import YAML as _YAML
            _yaml = _YAML()
            _yaml.default_flow_style = False
            _yaml.representer.add_representer(
                type(None),
                lambda dumper, data_val: dumper.represent_scalar(
                    "tag:yaml.org,2002:null", "null"
                ),
            )
            _stream = StringIO()
            _yaml.dump(data, _stream)
            content = _stream.getvalue()
        else:
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
    _progress.finish()
    write_output(content, output=output)

    # Persist to two-layer cache (git SHA unchanged → re-use on next run).
    #
    # L1 (core): stores pre-computed compact+agent+standard views at max
    #   fidelity so any subsequent view can be derived without re-analysis.
    # L2 (view): stores the exact rendered string for this flag combination.
    #
    # GC runs after L2 write to evict old commits and orphaned blobs/views.
    if not no_cache and _core_key and not _pipeline_error:
        try:
            from sourcecode.serializer import core_view as _core_view_fn
            _core_dict_write = _core_view_fn(sm)
            _written_core_hash = _cache_mod.write_core(target, _core_key, _core_dict_write)

            # Compute view key using the just-written core hash
            if _written_core_hash:
                if not _view_key:
                    # _view_key not set (L1 was also a miss); compute it now
                    _wvh = _hashlib.sha256(_view_flags_str.encode()).hexdigest()[:8]
                    _view_key = f"{_written_core_hash}-{_wvh}"
                _cache_mod.write_view(
                    target,
                    _view_key,
                    content,
                    fmt=format,
                    layers=_compute_analyzer_fingerprints(),
                )
                # Trigger GC (evict old commits + orphaned views + CAS blobs)
                from sourcecode.cache import cache_dir as _cdir, _gc as _run_gc
                _run_gc(_cdir(target))
        except Exception:
            pass  # non-fatal: cache write failure

    if _pipeline_error:
        raise typer.Exit(code=2)

    # 7. Clipboard copy (--copy / -c)
    if copy and output is None:
        _trimmed = content.strip()
        if _trimmed and _trimmed not in ("{}", "[]", "null"):
            if _copy_to_clipboard(content):
                typer.echo("✓ copied to clipboard", err=True)

    # 8. One-time MCP setup nudge (stderr only — does not affect exit code or stdout)
    from sourcecode.mcp_nudge import nudge_mcp_if_needed as _nudge
    _nudge()


# ── prepare-context output helpers ────────────────────────────────────────────

def _make_explanation(reason: str, why: str) -> str:
    """Merge reason+why into one human string. Drop internal pipe-format jargon."""
    if why and not why.startswith("artifact_type:"):
        return why
    return reason


def _serialize_relevant_file(f: Any) -> dict:
    from dataclasses import asdict as _asdict
    d = {k: v for k, v in _asdict(f).items() if v != "" and v is not None}
    reason = d.pop("reason", "") or ""
    why = d.pop("why", "") or ""
    # Expose score as a rounded float so agents can rank/filter files deterministically.
    # Kept as "score" (0.0–1.0 normalized relevance) — higher = more relevant.
    raw_score = d.pop("score", None)
    if raw_score is not None:
        d["score"] = round(float(raw_score), 4)
    explanation = _make_explanation(reason, why)
    if explanation:
        d["explanation"] = explanation
    return d


def _transform_impact_scores(scores: dict) -> dict:
    """Convert internal impact_score_per_file to human-readable review signals."""
    _THRESHOLDS = [(0.60, "high"), (0.40, "medium")]
    _CT_LABELS: dict[str, str] = {
        "security_change":    "security-sensitive change",
        "behavioral_change":  "behavioral change",
        "structural_change":  "structural modification",
        "configuration_change": "configuration change",
        "dependency_change":  "dependency update",
        "ui_change":          "UI change",
    }
    result: dict = {}
    for path, entry in scores.items():
        raw = entry.get("_rank_score", 0.0)
        priority = next((p for thresh, p in _THRESHOLDS if raw >= thresh), "low")
        signals: list[str] = [
            _CT_LABELS[ct] for ct in entry.get("change_types", []) if ct in _CT_LABELS
        ]
        ev = entry.get("evidence", {})
        callers = ev.get("reverse_edge_count", 0)
        if callers:
            signals.append(f"imported by {callers} other file{'s' if callers > 1 else ''}")
        out_entry: dict = {"review_priority": priority}
        if signals:
            out_entry["signals"] = signals
        result[path] = out_entry
    return result


def _clean_system_impact(si: dict) -> dict:
    """Remove empty lists/dicts from system_impact — only emit positive signals."""
    return {k: v for k, v in si.items() if v}


def _build_human_summary(output: Any) -> dict:
    """Human-readable summary block for delta/review-pr output."""
    concerns: list[str] = []
    si = getattr(output, "system_impact", {}) or {}
    for ri in si.get("runtime_impact", []):
        sig = ri.get("signal", "")
        if sig:
            concerns.append(sig)
    for bc in si.get("behavioral_changes", []):
        stmt = bc.get("statement", "")
        if stmt:
            concerns.append(stmt)
    cov = getattr(output, "test_coverage_risk", {}) or {}
    missing = cov.get("changed_files_without_tests", [])
    if missing:
        concerns.append(f"{len(missing)} changed file(s) without test coverage detected")
    summary: dict = {}
    if getattr(output, "impact_summary", None):
        summary["description"] = output.impact_summary
    if concerns:
        summary["review_concerns"] = concerns[:6]
    summary["confidence"] = (getattr(output, "confidence", None) or "medium").upper()
    return summary


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
    output_path: Optional[Path] = typer.Option(
        None,
        "--output",
        "-o",
        help="Write output to a file instead of stdout (UTF-8, avoids PowerShell BOM on Windows).",
    ),
    symptom: Optional[str] = typer.Option(
        None,
        "--symptom",
        help="(fix-bug) Keyword hint for the bug: boosts matching files and surfaces related code notes.",
    ),
    format: Optional[str] = typer.Option(
        None,
        "--format",
        help="Output format: json (default) | github-comment (Markdown PR comment for review-pr task)",
    ),
    debug_perf: bool = typer.Option(
        False,
        "--debug-perf",
        help="Emit per-phase timing to stderr (git scan ms, symptom scoring ms, total ms)",
        hidden=True,
    ),
    fast: bool = typer.Option(
        False,
        "--fast",
        help="Skip deep analysis (content search, test gap discovery, code annotations). Uses manifest/metadata only. Target: < 6 s.",
    ),
    include_config: bool = typer.Option(
        False,
        "--include-config",
        help="(generate-tests) Include tooling config files (*.conf.js, .eslintrc*, etc.) in test_gaps. Excluded by default.",
    ),
    all_gaps: bool = typer.Option(
        False,
        "--all",
        help="(generate-tests) Return the full test_gaps list without truncating to top 20.",
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
      review-pr      PR diff: execution paths with per-step runtime signals, security/transactional impact, test gaps (requires git diff or --since)
      delta          Incremental context: git-changed files only

    \b
    Examples:
      sourcecode prepare-context explain
      sourcecode prepare-context explain /path/to/repo
      sourcecode prepare-context fix-bug
      sourcecode prepare-context delta --since main
      sourcecode prepare-context review-pr --since origin/main
      sourcecode prepare-context review-pr . --since main --output review.json
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

    # Pro gate: generate-tests and delta require an active Pro license.
    _PRO_TASKS: frozenset[str] = frozenset({"generate-tests", "delta"})
    if task in _PRO_TASKS:
        from sourcecode.license import require_pro as _require_pro
        _require_pro(task)

    # Validate --format: only "json" and "github-comment" are valid for prepare-context.
    # "yaml" is intentionally NOT supported here (use main command for yaml output).
    # Invalid values must error loudly — silently falling through to JSON is a lie.
    _PC_FORMAT_CHOICES = ("json", "github-comment")
    if format is not None and format not in _PC_FORMAT_CHOICES:
        typer.echo(
            f"Error: invalid value '{format}' for --format. "
            f"Valid options: {', '.join(_PC_FORMAT_CHOICES)}.",
            err=True,
        )
        raise typer.Exit(code=2)
    # github-comment only renders for review-pr; warn and normalize for other tasks.
    if format == "github-comment" and task != "review-pr":
        typer.echo(
            f"[warning] --format github-comment is only supported for the review-pr task. "
            f"Outputting JSON for '{task}'.",
            err=True,
        )
        format = "json"

    target = path.resolve()
    if not target.exists() or not target.is_dir():
        _emit_error_json(
            "invalid_path",
            f"'{target}' is not a valid directory.",
            path=str(target),
        )
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
    import time as _time

    builder = TaskContextBuilder(target)
    _progress = Progress()
    _phase = f"analyzing ({task})"
    if since:
        _phase += f" since {since}"
    _progress.start(_phase)
    if not fast:
        import sys as _sys
        if _sys.stderr.isatty():
            _sys.stderr.write(f"Analyzing ({task})... (deep scan may take 15–35 s for large codebases)\n")
            _sys.stderr.flush()
    _t0 = _time.perf_counter()
    try:
        # H-02: apply timeout for generate-tests — large repos can stall indefinitely.
        # Mirrors SOURCECODE_TESTS_TIMEOUT_MS used by the MCP generate_tests_context tool.
        if task == "generate-tests" and not fast:
            import concurrent.futures as _cf
            import os as _os_gt
            _timeout_ms = int(_os_gt.environ.get("SOURCECODE_TESTS_TIMEOUT_MS", "30000"))
            _timeout_s = _timeout_ms / 1000.0
            _ex = _cf.ThreadPoolExecutor(max_workers=1)
            _fut = _ex.submit(
                builder.build, task,
                since=since, symptom=symptom, fast=fast,
                include_config=include_config, all_gaps=all_gaps,
            )
            _done_set, _nd_set = _cf.wait([_fut], timeout=_timeout_s)
            _ex.shutdown(wait=False)
            if _nd_set:
                import sys as _sys_gt
                if _sys_gt.stderr.isatty():
                    _sys_gt.stderr.write(
                        f"[generate-tests] timeout after {_timeout_ms}ms — returning partial result\n"
                    )
                    _sys_gt.stderr.flush()
                from sourcecode.prepare_context import TaskOutput as _TO
                output = _TO(
                    task=task,
                    goal=TASKS[task].goal,
                    project_summary=None,
                    architecture_summary=None,
                    relevant_files=[],
                    suspected_areas=[],
                    improvement_opportunities=[],
                    test_gaps=[],
                    key_dependencies=[],
                    code_notes_summary=None,
                    limitations=[f"generate-tests timed out after {_timeout_ms}ms"],
                    truncated=True,
                    truncated_reason=f"timeout_{_timeout_ms}ms",
                    confidence="low",
                )
            else:
                output = _fut.result()
        else:
            output = builder.build(task, since=since, symptom=symptom, fast=fast, include_config=include_config, all_gaps=all_gaps)
    finally:
        _progress.finish()
    _t_total = (_time.perf_counter() - _t0) * 1000

    if debug_perf:
        _perf = getattr(output, "_perf_ms", {}) or {}
        typer.echo(
            f"[debug-perf] total={_t_total:.0f}ms"
            + (f"  git_scan={_perf.get('git_scan_ms', '?')}ms" if "git_scan_ms" in _perf else "")
            + (f"  symptom={_perf.get('symptom_ms', '?')}ms" if "symptom_ms" in _perf else ""),
            err=True,
        )

    # Task-specific content-filter: each task emphasizes different output fields.
    # Fields marked False are suppressed from this task's output to reduce noise.
    _TASK_CONTENT_MAP: dict[str, dict[str, bool]] = {
        "onboard": {
            "project_summary": True, "architecture_summary": True,
            "relevant_files": True, "key_dependencies": True,
            "gaps": True, "confidence": True,
            "suspected_areas": False, "improvement_opportunities": False,
            "test_gaps": False, "code_notes_summary": False,
            "changed_files": False, "affected_entry_points": False,
        },
        "explain": {
            "project_summary": True, "architecture_summary": True,
            "relevant_files": False, "key_dependencies": True,
            "gaps": False, "confidence": True,
            "suspected_areas": False, "improvement_opportunities": False,
            "test_gaps": False, "code_notes_summary": False,
            "changed_files": False, "affected_entry_points": False,
        },
        "fix-bug": {
            "project_summary": True, "architecture_summary": False,
            "relevant_files": True, "key_dependencies": False,
            "gaps": False, "confidence": True,
            "suspected_areas": True, "improvement_opportunities": False,
            "test_gaps": False, "code_notes_summary": True,
            "changed_files": False, "affected_entry_points": False,
        },
        "generate-tests": {
            "project_summary": True, "architecture_summary": False,
            "relevant_files": True, "key_dependencies": True,
            "gaps": True, "confidence": True,
            "suspected_areas": False, "improvement_opportunities": False,
            "test_gaps": True, "code_notes_summary": False,
            "changed_files": False, "affected_entry_points": False,
        },
        "delta": {
            "project_summary": False, "architecture_summary": False,
            "relevant_files": True, "key_dependencies": False,
            "gaps": True, "confidence": True,
            "suspected_areas": False, "improvement_opportunities": False,
            "test_gaps": False, "code_notes_summary": False,
            "changed_files": True, "affected_entry_points": True,
        },
        "review-pr": {
            "project_summary": False, "architecture_summary": False,
            "relevant_files": True, "key_dependencies": False,
            "gaps": True, "confidence": False,
            "suspected_areas": False, "improvement_opportunities": False,
            "test_gaps": False, "code_notes_summary": False,
            "changed_files": True, "affected_entry_points": True,
        },
    }
    _content_filter = _TASK_CONTENT_MAP.get(task, {})

    def _task_include(field: str) -> bool:
        return _content_filter.get(field, True)

    _run_id = hashlib.sha256(
        f"{task}:{target}:{since or ''}:{format or ''}:{symptom or ''}".encode()
    ).hexdigest()[:16]

    out: dict[str, Any] = {
        "task": output.task,
        "goal": output.goal,
        "run_id": _run_id,
    }
    if _task_include("project_summary"):
        out["project_summary"] = output.project_summary
    if _task_include("architecture_summary"):
        out["architecture_summary"] = output.architecture_summary
    if _task_include("confidence"):
        out["confidence"] = output.confidence
    if task != "review-pr" and _task_include("relevant_files"):
        out["relevant_files"] = [
            _serialize_relevant_file(f)
            for f in output.relevant_files
        ]
    if _task_include("key_dependencies") and output.key_dependencies:
        out["key_dependencies"] = output.key_dependencies
    if _task_include("gaps") and output.gaps:
        out["gaps"] = output.gaps
    if _task_include("suspected_areas") and output.suspected_areas:
        out["suspected_areas"] = output.suspected_areas
    if _task_include("improvement_opportunities") and output.improvement_opportunities:
        out["improvement_opportunities"] = output.improvement_opportunities
    if _task_include("test_gaps") and output.test_gaps:
        out["test_gaps"] = output.test_gaps
    # P0-2: fast-mode truncation transparency — always emit when truncated, even if test_gaps is []
    # Use `is True` (strict) so MagicMock objects in tests don't trigger this branch.
    if getattr(output, "truncated", False) is True:
        out["truncated"] = True
        _tr = getattr(output, "truncated_reason", None)
        if isinstance(_tr, str) and _tr:
            out["truncated_reason"] = _tr
    if _task_include("code_notes_summary") and output.code_notes_summary:
        out["code_notes_summary"] = output.code_notes_summary
    if _task_include("changed_files") and output.changed_files:
        out["changed_files"] = output.changed_files
    if _task_include("affected_entry_points") and output.affected_entry_points:
        out["affected_entry_points"] = output.affected_entry_points
    # compact_base fields — included for all non-delta/review-pr tasks (Fix #1)
    if task not in ("delta", "review-pr"):
        if output.entry_points_structured:
            out["entry_points"] = output.entry_points_structured
        if output.deployment:
            out["deployment"] = output.deployment
        if output.deployment_risks:
            out["deployment_risks"] = output.deployment_risks
        if output.security_surface:
            out["security_surface"] = output.security_surface
        if output.mybatis:
            out["mybatis"] = output.mybatis
        if output.transactional_boundaries:
            out["transactional_boundaries"] = output.transactional_boundaries
        if output.spring_profiles_info:
            out["spring_profiles"] = output.spring_profiles_info
        if output.angular_analysis and (
            output.angular_analysis.get("component_count", 0) > 0
            or output.angular_analysis.get("angular_version")
        ):
            out["angular_analysis"] = output.angular_analysis
    # Delta-specific impact fields
    if task == "delta":
        if output.error_code:
            # Hard error — emit structured error JSON and exit, skip normal delta fields
            _err_out: dict[str, Any] = {
                "task": output.task,
                "ci_decision": output.ci_decision or "git_ref_error",
                "error": output.error_code,
                "since": output.since,
                "message": output.error_message,
            }
            if output.error_hints:
                _err_out["hint"] = output.error_hints
            _err_json = json.dumps(_err_out, indent=2, ensure_ascii=False)
            if output_path is not None:
                output_path.write_text(_err_json, encoding="utf-8")
            else:
                import sys as _sys
                _sys.stdout.buffer.write(_err_json.encode("utf-8"))
                _sys.stdout.buffer.write(b"\n")
                _sys.stdout.buffer.flush()
            raise typer.Exit(code=1)
        if output.ci_decision == "no_changes":
            # Early exit: no diff — emit minimal JSON without any analysis fields.
            # Prevents no_changes from ever being serialized alongside relevant_files > 0.
            _nc_out: dict[str, Any] = {
                "task": output.task,
                "ci_decision": "no_changes",
                "summary": "No changes detected",
            }
            if output.since:
                _nc_out["since"] = output.since
            if output.analysis_scope:
                _nc_out["analysis_scope"] = output.analysis_scope
            _nc_json = json.dumps(_nc_out, indent=2, ensure_ascii=False)
            if output_path is not None:
                output_path.write_text(_nc_json, encoding="utf-8")
            else:
                import sys as _sys
                _sys.stdout.buffer.write(_nc_json.encode("utf-8"))
                _sys.stdout.buffer.write(b"\n")
                _sys.stdout.buffer.flush()
            if copy:
                if _copy_to_clipboard(_nc_json):
                    typer.echo("✓ copied to clipboard", err=True)
            raise typer.Exit()
        if output.ci_decision:
            out["ci_decision"] = output.ci_decision
        if output.since:
            out["since"] = output.since
        if output.impact_summary:
            out["impact_summary"] = output.impact_summary
        if output.affected_modules:
            out["affected_modules"] = output.affected_modules
        if output.risk_areas:
            out["risk_areas"] = output.risk_areas
        if output.change_type:
            out["change_type"] = output.change_type
        if output.system_impact:
            _si = _clean_system_impact(output.system_impact)
            if _si:
                out["system_impact"] = _si
        if output.dependency_graph_summary:
            _dgraph = {k: v for k, v in output.dependency_graph_summary.items() if v is not None}
            if _dgraph:
                out["dependency_graph_summary"] = _dgraph
        if output.impact_score_per_file:
            out["impact_score_per_file"] = _transform_impact_scores(output.impact_score_per_file)
        out["summary"] = _build_human_summary(output)
    # review-pr specific fields
    if task == "review-pr":
        if output.error_code:
            _err_out: dict[str, Any] = {
                "task": output.task,
                "ci_decision": output.ci_decision or "error",
                "error": output.error_code,
                "message": output.error_message,
            }
            if output.since:
                _err_out["since"] = output.since
            if output.error_hints:
                _err_out["hint"] = output.error_hints
            _err_json = json.dumps(_err_out, indent=2, ensure_ascii=False)
            if output_path is not None:
                output_path.write_text(_err_json, encoding="utf-8")
            else:
                import sys as _sys
                _sys.stdout.buffer.write(_err_json.encode("utf-8"))
                _sys.stdout.buffer.write(b"\n")
                _sys.stdout.buffer.flush()
            # FIX: no_diff (no PR changes) is not an error — exit 0, consistent
            # with delta's no_changes handling. Only true errors exit non-zero.
            _review_pr_exit = 0 if output.error_code == "no_diff" else 1
            raise typer.Exit(code=_review_pr_exit)
        out["review_type"] = "pull_request"
        if output.ci_decision:
            out["ci_decision"] = output.ci_decision
        if output.base_ref:
            out["base_ref"] = output.base_ref
        if output.since:
            out["since"] = output.since
        if output.affected_modules:
            out["affected_modules"] = output.affected_modules
        if output.security_impact:
            out["security_impact"] = output.security_impact
        if output.transactional_impact:
            out["transactional_impact"] = output.transactional_impact
        if output.configuration_impact:
            out["configuration_impact"] = output.configuration_impact
        if output.test_coverage_risk:
            out["test_coverage_risk"] = output.test_coverage_risk
        # honest split: runtime files vs build artifacts — no mixed ranking
        if output.runtime_changes:
            out["runtime_changes"] = output.runtime_changes
        if output.build_changes:
            out["build_changes"] = output.build_changes
        if output.committed_changes:
            out["committed_changes"] = output.committed_changes
        if output.uncommitted_changes:
            out["uncommitted_changes"] = output.uncommitted_changes
        if output.review_hotspots:
            out["review_hotspots"] = output.review_hotspots
        if output.suggested_review_order:
            out["suggested_review_order"] = output.suggested_review_order
        if output.execution_paths:
            out["execution_paths"] = output.execution_paths
        if output.behavioral_impact:
            out["behavioral_impact"] = output.behavioral_impact
        if output.impact_summary:
            out["impact_summary"] = output.impact_summary
        out["summary"] = _build_human_summary(output)
        # git-first scope metadata
        out["scope"] = {
            "source": output.scope_source or "git_diff",
            "files": output.scope_files,
            "repo_root": output.repo_root or "",
        }
        # analysis_limiter: consolidate missing graph signals into one field
        _missing_signals: list[str] = []
        _dgraph = output.dependency_graph_summary or {}
        if not _dgraph.get("has_graph_evidence"):
            _missing_signals.append("dependency_graph")
        _has_import_ev = any(
            (rc.get("role") or {}).get("has_annotation_signal") or
            (rc.get("role") or {}).get("has_symbol_signal")
            for rc in (output.runtime_changes or [])
        )
        if not _has_import_ev:
            _missing_signals.append("import_graph")
        if _missing_signals:
            out["analysis_limiter"] = {"missing_signals": _missing_signals}
        # classification_confidence: global note when all runtime files are low confidence
        _all_low = bool(output.runtime_changes) and all(
            (rc.get("change_effect") or {}).get("epistemic_level") == "INFERRED (LOW CONFIDENCE)"
            for rc in output.runtime_changes
        )
        if _all_low:
            out["classification_confidence"] = "low"
    if output.limitations:
        out["limitations"] = output.limitations
    if output.symptom:
        out["symptom"] = output.symptom
    if output.related_notes:
        out["related_notes"] = output.related_notes
    if output.symptom_note:
        out["symptom_note"] = output.symptom_note
    if output.symptom_explain:
        out["symptom_explain"] = output.symptom_explain
    if getattr(output, "symptom_hint", None):
        out["symptom_hint"] = output.symptom_hint
    if getattr(output, "warnings", None):
        out["warnings"] = output.warnings
    if llm_prompt:
        out["llm_prompt"] = builder.render_prompt(output)

    # H-01: fast-mode analysis transparency — consumer must not confuse "not analyzed"
    # with "analyzed and found nothing". Fields that were never computed are absent or null,
    # not zero. analysis_mode and skipped_analyzers make the omission explicit.
    if fast:
        out["analysis_mode"] = "fast"
        _skipped: list[str] = ["deep_content_scan"]
        _spec = TASKS.get(task)
        if _spec and _spec.enable_code_notes:
            _skipped.append("code_notes")
        if task == "generate-tests":
            _skipped.append("test_gap_discovery")
        out["skipped_analyzers"] = _skipped

    # P0-1: Apply output budget per task — safety net for large repos.
    from sourcecode.output_budget import (
        trim_to_budget as _pc_trim,
        BUDGET_FIX_BUG, BUDGET_REVIEW_PR, BUDGET_ONBOARD,
        BUDGET_EXPLAIN, BUDGET_REFACTOR, BUDGET_DELTA,
    )
    _pc_budgets: dict[str, int] = {
        "fix-bug":         BUDGET_FIX_BUG,
        "review-pr":       BUDGET_REVIEW_PR,
        "onboard":         BUDGET_ONBOARD,
        "explain":         BUDGET_EXPLAIN,
        "refactor":        BUDGET_REFACTOR,
        "delta":           BUDGET_DELTA,
        "generate-tests":  BUDGET_EXPLAIN,
    }
    _pc_budget = _pc_budgets.get(task, BUDGET_EXPLAIN)
    out = _pc_trim(out, _pc_budget, label=task)

    # Free-tier limits: fix-bug (top-5 files) and review-pr (lightweight).
    # Pro users get the full analysis; free users get enough to see the value.
    if task in ("fix-bug", "review-pr"):
        from sourcecode.license import can_use as _tier_can_use
        if not _tier_can_use(task):
            _FREE_FILE_LIMIT = 5
            if task == "fix-bug":
                _rf = out.get("relevant_files")
                if isinstance(_rf, list) and len(_rf) > _FREE_FILE_LIMIT:
                    out["relevant_files"] = _rf[:_FREE_FILE_LIMIT]
                    out["tier"] = "free"
                    out["tier_note"] = (
                        f"Showing top {_FREE_FILE_LIMIT} files. "
                        "Upgrade to Pro for complete risk-ranked analysis across all files."
                    )
            else:  # review-pr
                for _cap_field in ("runtime_changes", "execution_paths", "review_hotspots", "suggested_review_order"):
                    _fval = out.get(_cap_field)
                    if isinstance(_fval, list) and len(_fval) > _FREE_FILE_LIMIT:
                        out[_cap_field] = _fval[:_FREE_FILE_LIMIT]
                out["tier"] = "free"
                out["tier_note"] = (
                    "Lightweight review. Upgrade to Pro for full blast-radius analysis, "
                    "complete execution paths, and CI-grade risk scoring."
                )

    if format == "github-comment" and task == "review-pr":
        from sourcecode.pr_comment_renderer import render_github_comment
        _pc_content = render_github_comment(out)
    else:
        _pc_content = json.dumps(out, indent=2, ensure_ascii=False)

    if output_path is not None:
        output_path.write_text(_pc_content, encoding="utf-8")
    else:
        import sys as _sys
        _pc_bytes = _pc_content.encode("utf-8")
        _sys.stdout.buffer.write(_pc_bytes)
        if not _pc_content.endswith("\n"):
            _sys.stdout.buffer.write(b"\n")
        _sys.stdout.buffer.flush()

    if copy:
        _trimmed = _pc_content.strip()
        if _trimmed and _trimmed not in ("{}", "[]", "null"):
            if _copy_to_clipboard(_pc_content):
                typer.echo("✓ copied to clipboard", err=True)

    from sourcecode.mcp_nudge import nudge_mcp_if_needed as _nudge
    _nudge()


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


def _serialize_dict(data: dict, format: str) -> str:
    """Serialize *data* to JSON or YAML string.  BUG-06 helper — shared by repo-ir and endpoints."""
    if format == "yaml":
        from io import StringIO
        from ruamel.yaml import YAML as _YAML
        _y = _YAML()
        _y.default_flow_style = False
        _y.representer.add_representer(
            type(None),
            lambda d, v: d.represent_scalar("tag:yaml.org,2002:null", "null"),
        )
        _s = StringIO()
        _y.dump(data, _s)
        return _s.getvalue()
    return json.dumps(data, indent=2, ensure_ascii=False)


# ── repo-ir ───────────────────────────────────────────────────────────────────

@app.command("repo-ir")
def repo_ir_cmd(
    path: Path = typer.Argument(
        Path("."),
        help="Repository root to analyze (default: current directory)",
    ),
    since: Optional[str] = typer.Option(
        None,
        "--since",
        help="Git ref for symbol-level diff (e.g. HEAD~1, main)",
    ),
    files: Optional[str] = typer.Option(
        None,
        "--files",
        help="Comma-separated list of Java files (relative to path) to analyze",
    ),
    output_path: Optional[Path] = typer.Option(
        None,
        "--output",
        "-o",
        help="Write output to a file instead of stdout",
    ),
    include_tests: bool = typer.Option(
        False,
        "--include-tests",
        help="Include test files in analysis (excluded by default)",
    ),
    max_nodes: Optional[int] = typer.Option(
        None,
        "--max-nodes",
        help="Limit graph.nodes to top N by impact score (reduces output size)",
    ),
    max_edges: Optional[int] = typer.Option(
        None,
        "--max-edges",
        help="Limit graph.edges to N (priority: edges between kept nodes)",
    ),
    summary_only: bool = typer.Option(
        False,
        "--summary-only",
        help="Omit full graph.nodes/edges; keep analysis summary, impact, and change_set (<300KB typical)",
    ),
    format: str = typer.Option(
        "json",
        "--format",
        "-f",
        help="Output format: json (default) or yaml.",
        show_default=True,
    ),
    copy: bool = typer.Option(
        False,
        "--copy",
        "-c",
        help="Copy output to system clipboard after a successful run. No-op when --output is used or clipboard is unavailable.",
    ),
) -> None:
    """Deterministic symbol-level IR for Java repositories.

    \b
    Extracts symbols, relations, Spring roles, and (with --since) symbol-level diffs.
    Output is JSON or YAML: graph{nodes,edges}, analysis, impact, subsystems, change_set.

    \b
    Size control:
      --summary-only          Omit full graph; keep analysis + impact (smallest output)
      --max-nodes N           Keep top N nodes by score
      --max-edges N           Keep top N edges (priority: both endpoints kept)

    \b
    Examples:
      sourcecode repo-ir
      sourcecode repo-ir /path/to/repo --since HEAD~1
      sourcecode repo-ir --files src/main/java/UserService.java
      sourcecode repo-ir --since main --output ir.json
      sourcecode repo-ir --since HEAD~3 --summary-only --output ir-small.json
      sourcecode repo-ir --max-nodes 200 --max-edges 500
    """
    import json as _json
    import sys as _sys

    from sourcecode.repository_ir import apply_ir_size_limits, build_repo_ir, find_java_files

    root = path.resolve()
    if not root.is_dir():
        _emit_error_json(
            "invalid_path",
            f"'{root}' is not a valid directory.",
            path=str(root),
        )
        raise typer.Exit(1)

    if files:
        file_list = [f.strip() for f in files.split(",") if f.strip()]
    else:
        file_list = find_java_files(root)
        if not include_tests:
            file_list = [f for f in file_list if "/test/" not in f and "/tests/" not in f]

    if not file_list:
        typer.echo(
            _json.dumps({
                "schema_version": "final-v1",
                "graph": {"nodes": [], "edges": []},
                "analysis": {
                    "changed_entities": [],
                    "impacted_entities": [],
                    "isolated_changes": [],
                    "validated_changes": [],
                },
                "impact": {"global_score": 0, "ranked_nodes": []},
                "subsystems": [],
                "change_set": [],
                "audit": {"dropped_fields": []},
            }, indent=2)
        )
        return

    _ir_phase = f"extracting IR ({len(file_list)} files)"
    if since:
        _ir_phase += f" since {since}"
    _ir_progress = Progress()
    _ir_progress.start(_ir_phase)
    try:
        ir = build_repo_ir(file_list, root, since=since)
    finally:
        _ir_progress.finish()
    ir = apply_ir_size_limits(
        ir,
        max_nodes=max_nodes,
        max_edges=max_edges,
        summary_only=summary_only,
    )
    output = _serialize_dict(ir, format)

    if output_path:
        output_path.write_text(output, encoding="utf-8")
        size_kb = len(output.encode("utf-8")) // 1024
        if summary_only:
            typer.echo(
                f"IR written to {output_path} ({size_kb}KB, graph omitted by --summary-only)",
                err=True,
            )
        else:
            n_nodes = len((ir.get("graph") or {}).get("nodes") or [])
            n_edges = len((ir.get("graph") or {}).get("edges") or [])
            typer.echo(
                f"IR written to {output_path} "
                f"({size_kb}KB, {n_nodes} nodes, {n_edges} edges)",
                err=True,
            )
    else:
        try:
            _sys.stdout.buffer.write(output.encode("utf-8"))
            _sys.stdout.buffer.write(b"\n")
            _sys.stdout.buffer.flush()
        except UnicodeEncodeError as _ue:
            # IMP-2: emit workaround before re-raising so the user knows what to do.
            _sys.stderr.write(
                f"[sourcecode] UnicodeEncodeError on stdout ({_ue.encoding}): "
                "your console codec cannot encode this output.\n"
                "Workaround: sourcecode repo-ir --output ir.json\n"
            )
            _sys.stderr.flush()
            raise
        except AttributeError:
            # Fallback for wrapped stdout without buffer (e.g. some test harnesses)
            _sys.stdout.write(output)
            _sys.stdout.write("\n")
        if copy:
            if _copy_to_clipboard(output):
                typer.echo("✓ copied to clipboard", err=True)


# ── impact (blast-radius / change-impact analysis) ────────────────────────────

@app.command("impact")
def impact_cmd(
    target: str = typer.Argument(
        ...,
        help=(
            "Class name (simple or FQN) or file path to analyze impact for. "
            "Examples: UserService, org.example.UserService, UserService.java"
        ),
    ),
    path: Path = typer.Argument(
        Path("."),
        help="Repository root to analyze (default: current directory)",
    ),
    depth: int = typer.Option(
        4,
        "--depth",
        help="BFS depth for indirect caller traversal (default: 4).",
        min=1,
        max=8,
    ),
    output_path: Optional[Path] = typer.Option(
        None,
        "--output",
        "-o",
        help="Write output to a file instead of stdout.",
    ),
    include_tests: bool = typer.Option(
        False,
        "--include-tests",
        help="Include test files in analysis (excluded by default).",
    ),
    copy: bool = typer.Option(
        False,
        "--copy",
        "-c",
        help="Copy output to system clipboard after a successful run. No-op when --output is used or clipboard is unavailable.",
    ),
) -> None:
    """Blast-radius analysis: who calls this class and what breaks if it changes?

    \b
    Builds the repository IR and propagates impact from the target symbol
    through the reverse dependency graph. Returns:
      - direct_callers     — classes that directly call or depend on the target
      - indirect_callers   — transitive callers (BFS, bounded by --depth)
      - endpoints_affected — HTTP endpoints that transitively depend on the target
      - transactional_boundaries_touched — @Transactional classes in the call chain
      - risk_score / risk_level — quantified change risk

    \b
    Examples:
      sourcecode impact UserService
      sourcecode impact org.keycloak.services.DefaultKeycloakSession /path/to/keycloak
      sourcecode impact UserService --depth 6 --output impact.json
    """
    from sourcecode.license import require_pro as _require_pro
    _require_pro("impact")

    import json as _json
    import sys as _sys

    from sourcecode.repository_ir import (
        build_repo_ir, find_java_files, compute_blast_radius,
    )
    from sourcecode.output_budget import trim_to_budget as _trim, BUDGET_IMPACT

    root = path.resolve()
    if not root.is_dir():
        _emit_error_json(
            "invalid_path",
            f"'{root}' is not a valid directory.",
            path=str(root),
        )
        raise typer.Exit(1)

    file_list = find_java_files(root)
    if not include_tests:
        file_list = [f for f in file_list if "/test/" not in f and "/tests/" not in f]

    if not file_list:
        typer.echo(
            _json.dumps(
                {
                    "target": target,
                    "resolution": "not_found",
                    "message": "No Java files found in repository.",
                    "risk_level": "unknown",
                },
                indent=2,
            )
        )
        return

    _prog = Progress()
    _prog.start(f"building IR ({len(file_list)} files) for impact analysis")
    try:
        ir = build_repo_ir(file_list, root)
    finally:
        _prog.finish()

    result = compute_blast_radius(ir, target, max_depth=depth)
    result = _trim(result, BUDGET_IMPACT, label="impact")

    output = _json.dumps(result, indent=2, ensure_ascii=False)
    if output_path:
        output_path.write_text(output, encoding="utf-8")
        typer.echo(f"Impact analysis written to {output_path}", err=True)
    else:
        try:
            _sys.stdout.buffer.write(output.encode("utf-8"))
            _sys.stdout.buffer.write(b"\n")
            _sys.stdout.buffer.flush()
        except AttributeError:
            _sys.stdout.write(output + "\n")
        if copy:
            if _copy_to_clipboard(output):
                typer.echo("✓ copied to clipboard", err=True)

    # H-03: resolution=not_found is a valid structured answer, not an infra failure.
    # Exit 0 so pipelines can parse the JSON without treating it as an error.
    # Exit 1 is reserved for path-not-found, I/O failures, and real infra errors.
    if result.get("resolution") == "not_found":
        raise typer.Exit(code=0)

    from sourcecode.mcp_nudge import nudge_mcp_if_needed as _nudge
    _nudge()


# ── endpoints ─────────────────────────────────────────────────────────────────

# _extract_java_endpoints is imported from sourcecode.repository_ir as the
# canonical single-source-of-truth endpoint extractor.



@app.command("endpoints")
def endpoints_cmd(
    path: Path = typer.Argument(
        Path("."),
        help="Repository path to scan for REST endpoints (default: current directory)",
    ),
    output_path: Optional[Path] = typer.Option(
        None, "--output", "-o",
        help="Write output to a file instead of stdout.",
    ),
    format: str = typer.Option(
        "json",
        "--format",
        "-f",
        help="Output format: json (default) or yaml.",
        show_default=True,
    ),
    copy: bool = typer.Option(
        False,
        "--copy",
        "-c",
        help="Copy output to system clipboard after a successful run. No-op when --output is used or clipboard is unavailable.",
    ),
) -> None:
    """Extract REST API endpoint surface from Java source files.

    \b
    Scans Spring MVC (@GetMapping/@PostMapping/@PutMapping/@DeleteMapping/@PatchMapping/@RequestMapping)
    and JAX-RS (@GET/@POST/@PUT/@DELETE/@PATCH with @Path) annotations.
    Extracts HTTP method, path, controller class, and handler method.

    \b
    Examples:
      sourcecode endpoints .
      sourcecode endpoints /path/to/repo
      sourcecode endpoints . --output endpoints.json
      sourcecode endpoints . --format yaml
    """
    import sys as _sys

    target = path.resolve()
    if not target.exists() or not target.is_dir():
        _emit_error_json(
            "invalid_path",
            f"'{target}' is not a valid directory.",
            path=str(target),
        )
        raise typer.Exit(code=1)

    data = _extract_java_endpoints(target)
    output = _serialize_dict(data, format)

    if output_path is not None:
        output_path.write_text(output, encoding="utf-8")
        typer.echo(
            f"Endpoints written to {output_path} ({data['total']} endpoints)",
            err=True,
        )
    else:
        _sys.stdout.buffer.write(output.encode("utf-8"))
        _sys.stdout.buffer.write(b"\n")
        _sys.stdout.buffer.flush()
        if copy:
            if _copy_to_clipboard(output):
                typer.echo("✓ copied to clipboard", err=True)

    from sourcecode.mcp_nudge import nudge_mcp_if_needed as _nudge
    _nudge()


# ── Enterprise Workflow Commands ──────────────────────────────────────────────
#
# These are the five canonical enterprise workflows.  Each is a thin wrapper
# around prepare-context (or impact) with an intent-clear name so that users
# and agents can pick the right workflow without reading docs.
#
# Tier: OSS Core (onboard), Pro (impact/modernize/fix-bug), Pro (review-pr)
# ─────────────────────────────────────────────────────────────────────────────

@app.command("onboard")
def onboard_cmd(
    ctx: typer.Context,
    path: Path = typer.Argument(
        Path("."),
        help="Repository root to onboard (default: current directory)",
    ),
    output_path: Optional[Path] = typer.Option(
        None, "--output", "-o",
        help="Write output to a file instead of stdout.",
    ),
    llm_prompt: bool = typer.Option(
        False, "--llm-prompt",
        help="Append a ready-to-use LLM prompt to the output.",
    ),
    copy: bool = typer.Option(
        False, "--copy", "-c",
        help="Copy output to clipboard after a successful run.",
    ),
) -> None:
    """[OSS Core] Onboard an unfamiliar codebase: architecture, entry points, hotspots, risks.

    \b
    Answers: "What is this repo and where do I start?"

    Builds full project context optimised for a new developer or agent that
    has never seen this codebase.  Surfaces architecture summary, subsystems,
    key entry points, hotspots, and tech-debt signals.

    \b
    Workflow:
      sourcecode onboard .
      sourcecode onboard /path/to/repo --llm-prompt
      sourcecode onboard . --output onboard.json

    \b
    Related workflows:
      sourcecode impact <target>   — What breaks if I touch this?
      sourcecode review-pr         — Risk-rank a pending PR
      sourcecode modernize .       — Where should I refactor first?
      sourcecode fix-bug           — Where does this symptom live?
    """
    ctx.invoke(
        prepare_context_cmd,
        task="onboard",
        path=path,
        llm_prompt=llm_prompt,
        copy=copy,
        output_path=output_path,
        since=None,
        task_help=False,
        dry_run=False,
        symptom=None,
        format=None,
        debug_perf=False,
        fast=False,
    )


@app.command("review-pr")
def review_pr_cmd(
    ctx: typer.Context,
    path: Path = typer.Argument(
        Path("."),
        help="Repository root (default: current directory)",
    ),
    since: Optional[str] = typer.Option(
        None, "--since",
        help="Git ref baseline for diff (e.g. origin/main, HEAD~3).",
    ),
    output_path: Optional[Path] = typer.Option(
        None, "--output", "-o",
        help="Write output to a file instead of stdout.",
    ),
    format: Optional[str] = typer.Option(
        None, "--format",
        help="Output format: json (default) | github-comment",
    ),
    copy: bool = typer.Option(
        False, "--copy", "-c",
        help="Copy output to clipboard after a successful run.",
    ),
) -> None:
    """[Pro*] PR review: blast radius, risk ranking, execution paths, security/txn impact.

    Note: [Pro*] label is reserved for a future licensing gate. This command currently
    runs without authentication. Behavior may change in a future version.

    \b
    Answers: "What does this PR break and how risky is it?"

    Computes a diff-based change impact report: changed symbols, BFS propagation
    through the call graph, affected endpoints, transactional boundaries,
    security surface, and a per-file risk ranking.

    \b
    Requires either --since or a git diff to be present.

    \b
    Workflow:
      sourcecode review-pr --since origin/main
      sourcecode review-pr . --since main --format github-comment
      sourcecode review-pr . --since HEAD~3 --output review.json

    \b
    Related workflows:
      sourcecode impact <target>   — Single-symbol blast radius
      sourcecode onboard .         — Full architecture onboarding
    """
    ctx.invoke(
        prepare_context_cmd,
        task="review-pr",
        path=path,
        since=since,
        format=format,
        copy=copy,
        output_path=output_path,
        llm_prompt=False,
        task_help=False,
        dry_run=False,
        symptom=None,
        debug_perf=False,
        fast=False,
    )


@app.command("fix-bug")
def fix_bug_cmd(
    ctx: typer.Context,
    path: Path = typer.Argument(
        Path("."),
        help="Repository root (default: current directory)",
    ),
    symptom: Optional[str] = typer.Option(
        None, "--symptom", "-s",
        help="Keyword hint for the bug (boosts matching files and surfaces related annotations).",
    ),
    output_path: Optional[Path] = typer.Option(
        None, "--output", "-o",
        help="Write output to a file instead of stdout.",
    ),
    copy: bool = typer.Option(
        False, "--copy", "-c",
        help="Copy output to clipboard after a successful run.",
    ),
) -> None:
    """[Pro*] Bug triage: risk-ranked files, suspected areas, related annotations.

    Note: [Pro*] label is reserved for a future licensing gate. This command currently
    runs without authentication. Behavior may change in a future version.

    \b
    Answers: "Where in this codebase should I look to fix this symptom?"

    Surfaces the most likely files/classes related to a bug symptom.
    Ranks by risk score, annotation signals (@Transactional, security annotations),
    and structural coupling.  Output is bounded and LLM-ready.

    \b
    Workflow:
      sourcecode fix-bug --symptom "NullPointerException in UserService"
      sourcecode fix-bug . --symptom "401 on /api/orders"
      sourcecode fix-bug . --output bug-context.json

    \b
    Related workflows:
      sourcecode impact <target>   — Propagate impact from a specific class
      sourcecode onboard .         — Full architecture context first
    """
    ctx.invoke(
        prepare_context_cmd,
        task="fix-bug",
        path=path,
        symptom=symptom,
        copy=copy,
        output_path=output_path,
        since=None,
        llm_prompt=False,
        task_help=False,
        dry_run=False,
        format=None,
        debug_perf=False,
        fast=False,
    )


@app.command("modernize")
def modernize_cmd(
    path: Path = typer.Argument(
        Path("."),
        help="Repository root to analyze (default: current directory)",
    ),
    output_path: Optional[Path] = typer.Option(
        None, "--output", "-o",
        help="Write output to a file instead of stdout.",
    ),
    copy: bool = typer.Option(
        False, "--copy", "-c",
        help="Copy output to clipboard after a successful run.",
    ),
) -> None:
    """[Pro*] Modernization planning: coupling, dead zones, risky modules, refactor candidates.

    Note: [Pro*] label is reserved for a future licensing gate. This command currently
    runs without authentication. Behavior may change in a future version.

    \b
    Answers: "Where should I refactor first, and what's safest to touch?"

    Analyzes the repo for:
      - High-coupling modules (high in-degree + out-degree nodes)
      - Dead zones (isolated symbols with no callers)
      - Risk hotspots (high fan-in + security annotations + transaction boundaries)
      - Cross-module dependency tangles
      - Subsystem summary with member counts

    Output is bounded and structured for both human review and LLM planning.

    \b
    Workflow:
      sourcecode modernize .
      sourcecode modernize /path/to/repo --output modernize.json

    \b
    Related workflows:
      sourcecode onboard .         — Architecture overview first
      sourcecode impact <target>   — Verify impact before touching a hotspot
    """
    import json as _json
    import sys as _sys
    from sourcecode.repository_ir import build_repo_ir, find_java_files, apply_ir_size_limits
    from sourcecode.output_budget import trim_to_budget, BUDGET_ONBOARD
    from sourcecode.license import can_use as _mod_can_use

    root = path.resolve()
    if not root.is_dir():
        _emit_error_json(
            "invalid_path",
            f"'{root}' is not a valid directory.",
            path=str(root),
        )
        raise typer.Exit(1)

    file_list = find_java_files(root)
    if not file_list:
        typer.echo(_json.dumps({
            "error": "No Java files found in repository.",
            "path": str(root),
        }, indent=2))
        return

    _prog = Progress()
    _prog.start(f"building IR ({len(file_list)} files) for modernization analysis")
    try:
        ir = build_repo_ir(file_list, root)
    finally:
        _prog.finish()

    graph_nodes: list = (ir.get("graph") or {}).get("nodes") or []
    subsystems: list = ir.get("subsystems") or []
    reverse_graph: dict = ir.get("reverse_graph") or {}

    # Git churn: commit frequency per file in last 90 days → proxy for volatility
    from sourcecode.contract_pipeline import _get_git_churn
    _java_rel_paths = [
        str(Path(p).relative_to(root)).replace("\\", "/") if Path(p).is_absolute() else p.replace("\\", "/")
        for p in file_list
    ]
    _file_churn: dict[str, int] = _get_git_churn(root, _java_rel_paths)

    # Build fqn → churn mapping via source_file field on graph nodes
    _fqn_churn: dict[str, int] = {}
    for _n in graph_nodes:
        _src = (_n.get("source_file") or "").replace("\\", "/")
        if _src and _src in _file_churn:
            _fqn_churn[_n["fqn"]] = _file_churn[_src]

    # High-coupling nodes: high in_degree (many dependents = risky to change)
    coupling_nodes = sorted(
        [n for n in graph_nodes if n.get("in_degree", 0) >= 3],
        key=lambda n: (-n.get("in_degree", 0), n.get("fqn", "")),
    )[:20]

    # Dead zones: symbols with zero in-degree AND zero out-degree (isolated)
    dead_zones = sorted(
        [n for n in graph_nodes
         if n.get("in_degree", 0) == 0 and n.get("out_degree", 0) == 0
         and n.get("type") in ("class", "interface")],
        key=lambda n: n.get("fqn", ""),
    )[:20]

    # Hotspot candidates: high in-degree service/repository/controller nodes,
    # ranked by composite score (in_degree × 2 + git_churn) for volatility signal.
    _HOTSPOT_ROLES = frozenset({"service", "repository", "controller", "entity"})
    _hotspot_candidates = [
        n for n in coupling_nodes if n.get("role") in _HOTSPOT_ROLES
    ]
    # Also include high-coupling nodes with name-based role inference even if
    # they didn't appear in coupling_nodes (in_degree >= 1 is sufficient here)
    _seen_hotspot_fqns = {n["fqn"] for n in _hotspot_candidates}
    for _n in graph_nodes:
        if (_n.get("fqn") not in _seen_hotspot_fqns
                and _n.get("role") in _HOTSPOT_ROLES
                and _n.get("in_degree", 0) >= 1
                and _fqn_churn.get(_n["fqn"], 0) >= 3):
            _hotspot_candidates.append(_n)
            _seen_hotspot_fqns.add(_n["fqn"])

    _max_churn = max(_fqn_churn.values(), default=1)
    hotspots = sorted(
        [
            {
                "fqn": n["fqn"],
                "role": n.get("role", "other"),
                "in_degree": n.get("in_degree", 0),
                "out_degree": n.get("out_degree", 0),
                "git_churn_90d": _fqn_churn.get(n["fqn"], 0),
                "hotspot_score": round(
                    n.get("in_degree", 0) * 2.0
                    + (_fqn_churn.get(n["fqn"], 0) / _max_churn) * 5.0,
                    2,
                ),
            }
            for n in _hotspot_candidates
        ],
        key=lambda h: (-h["hotspot_score"], h["fqn"]),
    )[:15]

    # Cross-module tangles: subsystems with high member count
    tangle_modules = sorted(
        [s for s in subsystems if len(s.get("members") or []) >= 5],
        key=lambda s: -len(s.get("members") or []),
    )[:10]

    _summary = {
        "total_classes": len([n for n in graph_nodes if n.get("type") in ("class", "interface")]),
        "total_subsystems": len(subsystems),
        "high_coupling_nodes": len(coupling_nodes),
        "dead_zone_candidates": len(dead_zones),
    }
    _subsystem_summary = [
        {
            "label": s.get("label") or s.get("name") or "",
            "package_prefix": s.get("package_prefix") or s.get("pkg") or "",
            "member_count": len(s.get("members") or []),
        }
        for s in subsystems[:15]
    ]

    if not _mod_can_use("modernize"):
        # Free tier: structural discovery only — no dead zones, tangles, or full refactor list.
        result = {
            "workflow": "modernize",
            "path": str(root),
            "tier": "free",
            "tier_note": (
                "Upgrade to Pro for full analysis: dead zones, dependency tangles, "
                "refactor candidates ranked by git churn, and complete coupling graphs."
            ),
            "summary": _summary,
            "subsystem_summary": _subsystem_summary,
            "hotspot_candidates": hotspots[:3],
            "high_coupling_nodes": [
                {"fqn": n["fqn"], "in_degree": n.get("in_degree", 0), "role": n.get("role", "other")}
                for n in coupling_nodes[:3]
            ],
        }
    else:
        # Pro tier: full analysis.
        result = {
            "workflow": "modernize",
            "path": str(root),
            "summary": _summary,
            "hotspot_candidates": hotspots,
            "high_coupling_nodes": [
                {"fqn": n["fqn"], "in_degree": n.get("in_degree", 0), "role": n.get("role", "other")}
                for n in coupling_nodes
            ],
            "dead_zone_candidates": [
                {"fqn": n["fqn"], "type": n.get("type", ""), "role": n.get("role", "other")}
                for n in dead_zones
            ],
            "subsystem_summary": _subsystem_summary,
            "cross_module_tangles": [
                {
                    "label": s.get("label") or s.get("name") or "",
                    "member_count": len(s.get("members") or []),
                }
                for s in tangle_modules
            ],
            # BUG-05 fix: don't recommend "Start with hotspot_candidates" when the list is empty.
            "recommendation": (
                (
                    "Start with hotspot_candidates (high fan-in = highest blast radius). "
                    if hotspots else
                    "high_coupling_nodes shows the most-referenced classes — start there. "
                )
                + "Dead zones are safe to remove or refactor. "
                + "Cross-module tangles indicate coupling worth decomposing."
            ),
        }

    result = trim_to_budget(result, BUDGET_ONBOARD, label="modernize")
    output = _json.dumps(result, indent=2, ensure_ascii=False)

    if output_path:
        output_path.write_text(output, encoding="utf-8")
        typer.echo(f"Modernization analysis written to {output_path}", err=True)
    else:
        try:
            _sys.stdout.buffer.write(output.encode("utf-8"))
            _sys.stdout.buffer.write(b"\n")
            _sys.stdout.buffer.flush()
        except AttributeError:
            _sys.stdout.write(output + "\n")

    if copy:
        _copy_to_clipboard(output)


# ── version ───────────────────────────────────────────────────────────────────

@app.command("activate")
def activate_cmd(
    license_key: str = typer.Argument(..., help="Your Pro license key"),
) -> None:
    """Activate a Pro license key.

    \b
    Validates the key against the license server and writes
    ~/.sourcecode/license.json.

    \b
    Examples:
      sourcecode activate SC-XXXX-XXXX-XXXX
    """
    from sourcecode.license import activate_license as _activate
    _activate(license_key)


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


# ── MCP server ────────────────────────────────────────────────────────────────

@mcp_app.command("serve")
def mcp_serve() -> None:
    """Start the MCP server on stdio for AI agent integration.

    \b
    Configure in your MCP client (e.g. Claude Desktop):
      {
        "sourcecode": {
          "command": "sourcecode",
          "args": ["mcp", "serve"]
        }
      }
    """
    import logging
    import sys as _sys

    logging.basicConfig(
        stream=_sys.stderr,
        level=logging.INFO,
        format="[sourcecode-mcp] %(levelname)s %(message)s",
    )
    from sourcecode.mcp.server import mcp as _mcp

    log = logging.getLogger(__name__)

    # P0-1: Strip UTF-8 BOM from stdin.buffer before the MCP server reads it.
    # PowerShell 5.1 on Windows writes \xEF\xBB\xBF at the start of stdin,
    # which breaks JSON parsing at line 1 column 1.
    # peek(3) loads bytes into BufferedReader's internal buffer without consuming;
    # read(3) discards only if the prefix is the UTF-8 BOM sequence.
    # No-op on Linux/macOS/Git Bash where stdin never starts with a BOM.
    # Guard: CliRunner / test stubs replace sys.stdin with StringIO (no .buffer).
    try:
        _stdin_buf = getattr(_sys.stdin, "buffer", None)
        if _stdin_buf is not None and hasattr(_stdin_buf, "peek"):
            _bom_prefix = _stdin_buf.peek(3)[:3]
            if _bom_prefix == b"\xef\xbb\xbf":
                _stdin_buf.read(3)
                log.info("sourcecode-mcp stripped UTF-8 BOM from stdin (PowerShell 5.1 workaround)")
    except Exception:
        pass  # Never abort server startup over BOM detection

    log.info("sourcecode-mcp starting (stdio transport)")
    try:
        _mcp.run()
    except KeyboardInterrupt:
        log.info("sourcecode-mcp stopped")
    except Exception as exc:
        log.critical("sourcecode-mcp fatal error: %s", exc, exc_info=True)
        raise typer.Exit(code=1)


# ── MCP onboarding ────────────────────────────────────────────────────────────

@mcp_app.command("init")
def mcp_init(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt."),
    target: Optional[str] = typer.Option(
        None,
        "--target",
        "-t",
        help="Target client: claude-desktop | cursor. Default: auto-detect all.",
    ),
) -> None:
    """Setup MCP integration for Claude Desktop, Cursor, and other clients.

    \b
    Detects installed MCP clients, backs up their config files, and safely
    inserts the sourcecode server entry. Fully idempotent — safe to re-run.

    \b
    Examples:
      sourcecode mcp init
      sourcecode mcp init --target claude-desktop
      sourcecode mcp init --target cursor --yes
    """
    from sourcecode.mcp.onboarding.detector import detect_clients, is_client_running
    from sourcecode.mcp.onboarding.planner import build_install_plan
    from sourcecode.mcp.onboarding import backup, applier

    typer.echo("Detecting MCP clients...")
    typer.echo("")

    all_clients = detect_clients()

    if target:
        target_slug = target.lower()
        clients = [c for c in all_clients if c.slug == target_slug]
        if not clients:
            valid = ", ".join(c.slug for c in all_clients)
            typer.echo(f"Unknown target '{target}'. Valid: {valid}", err=True)
            raise typer.Exit(code=1)
    else:
        clients = all_clients

    if not clients:
        typer.echo("No MCP clients found on this system.")
        typer.echo("")
        typer.echo("Manual setup — add to your MCP client config:")
        typer.echo('  "sourcecode": {"command": "sourcecode", "args": ["mcp", "serve"]}')
        raise typer.Exit(code=0)

    # Show detection results
    for client in clients:
        mark = "✓" if client.app_installed else "○"
        note = "" if client.app_installed else "  (not found)"
        typer.echo(f"  {mark}  {client.name:<18} {client.config_path}{note}")
    typer.echo("")

    # Build plan
    plan = build_install_plan(clients)
    actionable = [a for a in plan if a.client.app_installed and not a.already_installed]
    already_done = [a for a in plan if a.client.app_installed and a.already_installed]

    if already_done and not actionable:
        typer.echo("Already configured:")
        for a in already_done:
            typer.echo(f"  ✓  {a.client.name}   {a.client.config_path}")
        typer.echo("")
        typer.echo("Nothing to do. Remove: sourcecode mcp remove")
        raise typer.Exit(code=0)

    if already_done:
        typer.echo("Already configured:")
        for a in already_done:
            typer.echo(f"  ✓  {a.client.name}   {a.client.config_path}")
        typer.echo("")

    # Show plan for actionable items
    typer.echo("This will:")
    for a in actionable:
        verb = "Create " if a.will_create_file else "Modify "
        typer.echo(f"  {verb}  {a.client.config_path}")
    typer.echo(f"  Backup → ~/.config/sourcecode/mcp-backups/")
    typer.echo("")

    if not yes:
        confirmed = typer.confirm("Proceed?", default=False)
        if not confirmed:
            typer.echo("Aborted.")
            raise typer.Exit(code=0)
        typer.echo("")

    # Apply
    errors: list[str] = []
    for a in actionable:
        try:
            config = applier.read_config(a.client.config_path)
            if a.client.config_path.exists():
                bak = backup.create(a.client.config_path)
                typer.echo(f"  ✓ Backup   {bak}")
            updated = applier.apply_entry(config)
            applier.write_config(a.client.config_path, updated)
            if not applier.validate(a.client.config_path):
                errors.append(f"{a.client.name}: JSON validation failed after write")
                continue
            typer.echo(f"  ✓ Updated  {a.client.config_path}")
        except Exception as exc:
            errors.append(f"{a.client.name}: {exc}")

    typer.echo("")

    if errors:
        for err in errors:
            typer.echo(f"  ✗ {err}", err=True)
        raise typer.Exit(code=1)

    typer.echo("MCP integration active.")
    typer.echo("  Note:    repo_path must use forward slashes: C:/Users/... or /unix/path")
    typer.echo("")

    # Post-write: validate config and warn if client not running
    for a in actionable:
        if not is_client_running(a.client):
            typer.echo(
                f"  ⚠ Config written but {a.client.name} is not running. "
                f"Start {a.client.name} and run sourcecode mcp status to verify.",
                err=False,
            )
        else:
            restart_msg = "" if a.will_create_file else f" Restart {a.client.name} to apply."
            typer.echo(f"  ✓ {a.client.name} is running.{restart_msg}")

    typer.echo("")
    typer.echo("  Remove:  sourcecode mcp remove")

    # Clear nudge flag: next run finds is_installed=True → no nudge.
    from sourcecode.mcp_nudge import clear_nudge_flag as _clear_nudge
    _clear_nudge()


@mcp_app.command("status")
def mcp_status() -> None:
    """Show MCP integration status: dependencies, config files, and connectivity."""
    import subprocess as _sp
    import sys as _sys
    from sourcecode import __version__ as _cli_version
    from sourcecode.mcp.onboarding.detector import detect_clients, is_client_running
    from sourcecode.mcp.onboarding import applier

    sep = "─" * 46

    typer.echo("MCP Status")
    typer.echo(sep)

    # FIX-P0-5/P0-6: Show CLI version explicitly so drift is immediately visible.
    typer.echo(f"CLI version   {_cli_version}   ({_sys.executable})")
    typer.echo("")

    # Stage 1: Dependencies
    try:
        import mcp as _mcp_pkg  # noqa: F401
        typer.echo("Dependencies  ✓ installed")
    except ImportError:
        typer.echo("Dependencies  ✗ missing")
        typer.echo("  Fix: pip install sourcecode[mcp]")
    typer.echo("")

    clients = detect_clients()
    if not clients:
        typer.echo("  No MCP clients detected on this system.")
        typer.echo(sep)
        typer.echo("  Setup:   sourcecode mcp init")
        raise typer.Exit(code=0)

    # Stage 2: Config files — is sourcecode registered in the client's config?
    # FIX-P0-6: "configured" and "running" are distinct, independent checks.
    # Also detect external server installs (different Python/executable than CLI).
    typer.echo("Config (sourcecode registered in client config?)")
    for client in clients:
        if not client.app_installed:
            typer.echo(f"  {client.name:<20} ✗ app not found at expected path")
            typer.echo(f"    Expected: {client.config_path}")
            typer.echo(f"    Fix:      sourcecode mcp init --target {client.slug}")
            continue
        config = applier.read_config(client.config_path)
        if applier.is_installed(config):
            typer.echo(f"  {client.name:<20} ✓ configured   {client.config_path}")
            # FIX-P0-5: inspect registered command for external-server drift.
            _registered = config.get("mcpServers", {}).get("sourcecode", {})
            _reg_cmd = _registered.get("command", "")
            _reg_args = _registered.get("args", [])
            # Built-in form: command=sourcecode args=[mcp, serve] (or just the binary)
            _is_builtin = (
                _reg_cmd == "sourcecode"
                or (not _reg_args and _reg_cmd.endswith("/sourcecode"))
                or (_reg_args and _reg_args[:2] == ["mcp", "serve"])
            )
            if _is_builtin:
                typer.echo(f"    Server:  built-in (sourcecode mcp serve)  version={_cli_version}")
            else:
                # External server — different Python or custom server.py
                typer.echo(f"    Server:  ⚠ EXTERNAL — {_reg_cmd} {' '.join(_reg_args)}")
                # Try to get the external server's sourcecode version by finding the
                # sourcecode binary relative to the registered Python executable,
                # or falling back to probing the Python for the installed package.
                _ext_ver: str = "unknown"
                try:
                    import os as _os
                    # Strategy 1: look for sourcecode binary next to registered Python
                    _reg_bin_dir = _os.path.dirname(_reg_cmd)
                    _sc_sibling = _os.path.join(_reg_bin_dir, "sourcecode")
                    if _os.path.isfile(_sc_sibling):
                        _ver_r = _sp.run(
                            [_sc_sibling, "--version"],
                            capture_output=True, text=True, timeout=5,
                        )
                        if _ver_r.returncode == 0 and _ver_r.stdout.strip():
                            # "sourcecode X.Y.Z" → extract version
                            _ext_ver = _ver_r.stdout.strip().split()[-1]
                    # Strategy 2: import via registered Python
                    if _ext_ver == "unknown":
                        _ver_r2 = _sp.run(
                            [_reg_cmd, "-c",
                             "import sourcecode; print(sourcecode.__version__)"],
                            capture_output=True, text=True, timeout=5,
                        )
                        if _ver_r2.returncode == 0 and _ver_r2.stdout.strip():
                            _ext_ver = _ver_r2.stdout.strip()
                except Exception:
                    pass
                if _ext_ver != "unknown" and _ext_ver != _cli_version:
                    typer.echo(
                        f"    ⚠ VERSION DRIFT: external server version={_ext_ver}, "
                        f"CLI version={_cli_version}"
                    )
                    typer.echo(
                        "    To fix: sourcecode mcp init  (re-register using CLI built-in server)"
                    )
                elif _ext_ver != "unknown":
                    typer.echo(f"    External server version={_ext_ver}  (matches CLI ✓)")
                else:
                    typer.echo(
                        f"    ⚠ Cannot verify external server version (CLI={_cli_version}). "
                        "Re-run: sourcecode mcp init to switch to built-in server."
                    )
        else:
            typer.echo(f"  {client.name:<20} ✗ not configured  (app found, but sourcecode entry missing)")
            typer.echo(f"    Fix: sourcecode mcp init --target {client.slug}")
    typer.echo("")

    # Stage 3: Process liveness — is the client app currently running?
    # This is independent from config: a running app may still need restart to pick up config.
    typer.echo("Runtime (client app process running?)")
    any_installed = any(c.app_installed for c in clients)
    if not any_installed:
        typer.echo("  (no client apps found — nothing to check)")
    else:
        for client in clients:
            if not client.app_installed:
                continue
            if is_client_running(client):
                typer.echo(f"  {client.name:<20} ✓ running")
            else:
                typer.echo(f"  {client.name:<20} ✗ not running")
                typer.echo(f"    Fix: open {client.name}, then run sourcecode mcp status")

    typer.echo(sep)
    typer.echo("  Note:    'configured' and 'running' are checked independently.")
    typer.echo("           A running app still needs restart after first-time config.")
    typer.echo("  Path:    repo_path must use forward slashes: C:/Users/... or /unix/path")
    typer.echo("  Setup:   sourcecode mcp init")
    typer.echo("  Remove:  sourcecode mcp remove")


@mcp_app.command("remove")
def mcp_remove(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt."),
) -> None:
    """Remove sourcecode MCP integration from all configured clients.

    \b
    Backs up config files before modifying. Restores from backup when available,
    otherwise removes the sourcecode entry while preserving all other config.
    """
    from sourcecode.mcp.onboarding.detector import detect_clients
    from sourcecode.mcp.onboarding.planner import build_remove_plan
    from sourcecode.mcp.onboarding import backup, applier

    clients = detect_clients()
    plan = build_remove_plan(clients)
    installed = [a for a in plan if a.already_installed]

    if not installed:
        typer.echo("sourcecode MCP integration not found in any client config.")
        typer.echo("  Setup: sourcecode mcp init")
        raise typer.Exit(code=0)

    typer.echo("Remove sourcecode MCP integration from:")
    typer.echo("")
    for a in installed:
        typer.echo(f"  {a.client.name}   {a.client.config_path}")
        bak = backup.latest(a.client.config_path)
        if bak:
            typer.echo(f"    Backup available: {bak}")
    typer.echo("")

    if not yes:
        confirmed = typer.confirm("Proceed?", default=False)
        if not confirmed:
            typer.echo("Aborted.")
            raise typer.Exit(code=0)
        typer.echo("")

    errors: list[str] = []
    for a in installed:
        try:
            bak = backup.create(a.client.config_path)
            typer.echo(f"  ✓ Backup   {bak}")
            config = applier.read_config(a.client.config_path)
            updated = applier.remove_entry(config)
            applier.write_config(a.client.config_path, updated)
            if not applier.validate(a.client.config_path):
                errors.append(f"{a.client.name}: JSON validation failed — restoring backup")
                backup.restore(bak, a.client.config_path)
                continue
            typer.echo(f"  ✓ Updated  {a.client.config_path}")
        except Exception as exc:
            errors.append(f"{a.client.name}: {exc}")

    typer.echo("")

    if errors:
        for err in errors:
            typer.echo(f"  ✗ {err}", err=True)
        raise typer.Exit(code=1)

    typer.echo("MCP integration removed.")
    typer.echo("  Re-add:  sourcecode mcp init")


# ── Entry point ───────────────────────────────────────────────────────────────

def main_entry() -> None:
    """CLI entry point.

    Calls _preprocess_argv() before Typer/Click parses sys.argv so that
    repository path tokens are extracted before Click's Group callback
    can consume them as positional arguments (which would prevent subcommand
    routing for tokens like 'version' or 'config').
    """
    import sys as _sys
    # Force UTF-8 on stdout so Unicode characters (arrows, etc.) survive on
    # Windows where the default console codec is cp1252 (BUG-1).
    if hasattr(_sys.stdout, "reconfigure"):
        try:
            _sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    _preprocess_argv()
    app()

from __future__ import annotations

import hashlib
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional, cast

import typer

from sourcecode import __version__
from sourcecode.error_schema import INVALID_INPUT_CODE, build_error_envelope
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

Persistent structural context and ultra-fast repeated analysis for AI coding agents.

Cache warms on first scan; every subsequent call returns pre-built context in milliseconds.
Cold scan: 2–10s depending on repo size. Warm cache: 0.3–0.6s.

[bold]Primary usage:[/bold]
  sourcecode --compact                  high-signal summary (~2,500–4,000 tokens)
  sourcecode --compact --git-context    include git hotspots and uncommitted files
  sourcecode --agent                    full structured JSON for AI agents

[bold]Auth commands:[/bold]
  auth login                   [dim]# authenticate via browser (device code)[/dim]
  auth status                  [dim]# show current plan and auth state[/dim]
  auth logout                  [dim]# remove local credentials[/dim]

[bold]Cache commands:[/bold]
  cache status                 [dim]# cache size, hit keys, last-warmed timestamp[/dim]
  cache warm                   [dim]# pre-build cache ahead of an agent session[/dim]
  cache clear                  [dim]# clear all cached results for this repo[/dim]

[bold]Examples:[/bold]
  sourcecode my-project --compact
  sourcecode . --compact --git-context --copy
  sourcecode . --changed-only --git-context
  sourcecode prepare-context onboard my-project
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

[dim bold]Free tier — every command, full power, on repos up to 500 Java files.[/dim bold]
  [dim]impact, fix-bug, review-pr, modernize, --full, git-churn ranking — all free here.[/dim]

[dim bold]Pro (€19/mo · €190/yr) unlocks:[/dim bold]
  [dim]Enterprise-scale monoliths       all commands on repos above 500 Java files[/dim]
  [dim]prepare-context delta            CI/CD automation (30 free runs/repo, then Pro)[/dim]
  [dim]prepare-context generate-tests   test gap analysis on large repos[/dim]

  [dim]Non-Java repos are free at any size. Local MCP serve is free.[/dim]

  [dim cyan]→ sourcecode activate <key>[/dim cyan]
"""

    return text


_HELP = _build_help_text()

# Known subcommand names — tokens matching these are routed as subcommands,
# not consumed as a repository path.
_SUBCOMMANDS: frozenset[str] = frozenset(
    {
        "telemetry", "prepare-context", "version", "config",
        "repo-ir", "mcp", "endpoints", "impact",
        # Enterprise workflow commands
        "onboard", "modernize", "fix-bug", "review-pr",
        # License / auth
        "activate", "auth",
        # Cache observability
        "cache",
        # RIS bootstrap
        "cold-start",
        # Spring semantic audit
        "spring-audit",
        # Spring impact chain
        "impact-chain",
        # PR blast-radius report
        "pr-impact",
        # Class architectural summary
        "explain",
        # Spring Boot 2→3 migration readiness
        "migrate-check",
        # Native file rename (BLOCKER-A)
        "rename-class",
        # Large file semantic chunking (BLOCKER-B)
        "chunk-file",
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
    "--files",
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
    modified = _preprocess_args(sys.argv[1:])
    sys.argv = sys.argv[:1] + modified


def _is_always_include_ref(p: str) -> bool:
    """Return True for security-const files that must survive --changed-only filtering."""
    name = p.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if name.endswith("Const.java") or name.endswith("Constants.java"):
        return True
    parts = p.replace("\\", "/").lower().split("/")
    return any(seg in ("security", "seguridad", "constantes") for seg in parts)


def filter_sensitive_files(tree: dict[str, Any], redactor: Any) -> dict[str, Any]:
    """Recursively filter .env and *.secret entries from the file tree (SEC-02)."""
    filtered: dict[str, Any] = {}
    for name, value in tree.items():
        if redactor.should_exclude_file(name):
            continue  # exclude .env, *.secret from tree
        if isinstance(value, dict):
            filtered[name] = filter_sensitive_files(value, redactor)
        else:
            filtered[name] = value
    return filtered


def prune_workspace_paths(
    tree: dict[str, Any], workspace_paths: list[str]
) -> dict[str, Any]:
    """Remove workspace sub-trees from a file tree by path segments."""
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


def _inject_cache_meta(raw: str, meta: dict) -> str:
    """Inject ``_cache`` provenance block into a JSON dict string.

    Parses *raw* as JSON, adds ``_cache`` key, re-serialises.  Returns *raw*
    unchanged on any parse failure or non-dict JSON (YAML pass-through, etc.).
    """
    try:
        import json as _jm
        obj = _jm.loads(raw)
        if isinstance(obj, dict):
            obj["_cache"] = meta
            # Top-level cache_source for one release — backward compat alias
            if "cache_source" in meta:
                obj["cache_source"] = meta["cache_source"]
            return _jm.dumps(obj, indent=2, ensure_ascii=False)
    except Exception:
        pass
    return raw


def _emit_error_json(error: str, message: str, **context: object) -> None:
    """Write a structured JSON error envelope to stderr.

    Format: {"error": {"code": "<code>", "message": "<human text>", ...}, ...<context>}
    All CLI validation and runtime errors must go through this helper so that
    agents and tools can parse stderr reliably regardless of error type.
    """
    import json as _json
    payload = build_error_envelope(error, message, **context)
    sys.stderr.write(_json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stderr.flush()


def _safe_write_file(path: "Path", content: str) -> None:
    """Write content to path, emitting a clean JSON error on I/O failure."""
    try:
        path.write_text(content, encoding="utf-8")
    except OSError as _exc:
        _emit_error_json(
            INVALID_INPUT_CODE,
            f"Cannot write to '{path}': {_exc.strerror}.",
            hint="Check that the output directory exists and is writable.",
            expected="A writable file path.",
        )
        raise typer.Exit(code=1) from None


def _emit_command_output(
    content: str,
    output_path: "Optional[Path]",
    copy: bool,
    *,
    success_msg: "Optional[str]" = None,
) -> None:
    """Unified output pipeline: stdout | file | clipboard.

    Single call site for every command's final output step.
    File write failures emit JSON to stderr via _safe_write_file.
    Clipboard fires only when no --output path is given.
    """
    if output_path is not None:
        _safe_write_file(output_path, content)
        if success_msg:
            typer.echo(success_msg, err=True)
    else:
        try:
            sys.stdout.buffer.write(content.encode("utf-8"))
            sys.stdout.buffer.write(b"\n")
            sys.stdout.buffer.flush()
        except UnicodeEncodeError as _ue:
            sys.stderr.write(
                f"[sourcecode] UnicodeEncodeError on stdout ({_ue.encoding}): "
                "your console codec cannot encode this output.\n"
                "Workaround: use --output FILE\n"
            )
            sys.stderr.flush()
            raise
        except AttributeError:
            sys.stdout.write(content + "\n")
        if copy and _copy_to_clipboard(content):
            typer.echo("✓ copied to clipboard", err=True)


# H-06: Intercept Click-level UsageError (unknown options, bad args) and emit JSON.
# Click's default show() writes "Error: No such option: --foo" as plain text.
# Automation consumers need JSON on stderr regardless of how the error originated.
try:
    import click.exceptions as _click_exc

    def _json_click_usage_error_show(self: Any, file: Any = None) -> None:  # type: ignore[override]
        import json as _je
        _flag = str((getattr(self, "option_name", None) or getattr(self, "param_hint", None)) or "").strip("'\"")
        _context: dict[str, object] = {}
        if _flag:
            _context["flag"] = _flag
        payload = build_error_envelope(
            INVALID_INPUT_CODE,
            self.format_message(),
            **_context,
        )
        sys.stderr.write(_je.dumps(payload, ensure_ascii=False) + "\n")
        sys.stderr.flush()

    _click_exc.UsageError.show = _json_click_usage_error_show  # type: ignore[method-assign]
except Exception:
    pass  # click unavailable — plain-text fallback


def _copy_to_clipboard(content: str) -> bool:
    """Copy text to system clipboard. Returns True on success, False otherwise (never raises)."""
    import subprocess
    try:
        if sys.platform == "darwin":
            subprocess.run(["pbcopy"], input=content.encode("utf-8"), check=True, timeout=10)
            return True
        elif sys.platform == "win32":
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

    # Add [Pro] badge to Pro-gated option help strings for free-tier users.
    try:
        from sourcecode.license import is_pro as _lp_is_pro
    except Exception:
        _lp_is_pro = False
    if not _lp_is_pro:
        _PRO_OPTS = {"full"}
        for _param in cmd.params:
            if getattr(_param, "name", None) in _PRO_OPTS and getattr(_param, "help", None):
                if "[Pro" not in _param.help:
                    _param.help = _param.help + "  [dim][Pro on large repos][/dim]"

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

cache_app = typer.Typer(help="Cache inspection and management.", rich_markup_mode="rich")
app.add_typer(cache_app, name="cache")

auth_app = typer.Typer(help="Authentication: login, status, logout.", rich_markup_mode="rich")
app.add_typer(auth_app, name="auth")


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
    try:
        if not sys.stderr.isatty():
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

# ── Module-level constants ─────────────────────────────────────────────────────
_FREE_TIER_NODE_CAP: int = 50  # graph/semantic node cap — applies only to large repos on free tier
_JAVA_MIN_SCAN_DEPTH: int = 12  # Maven src/main/java/<pkg>/<module>/File depth floor
_JVM_STACKS: frozenset[str] = frozenset({"java", "kotlin", "scala", "groovy"})
_IMPACT_PRIORITY_THRESHOLDS: list[tuple[float, str]] = [
    (0.60, "high"),
    (0.40, "medium"),
]


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
            "Includes security_surface (when custom security annotations detected), mybatis (when MyBatis framework detected), and transactional_boundaries for Java projects. "
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
    no_tree: bool = False  # set True by --agent; --no-tree flag removed

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
        _emit_error_json(
            INVALID_INPUT_CODE,
            f"Invalid value '{mode}' for --mode. Valid options: {', '.join(_MODE_CHOICES)}",
            flag="--mode",
            value=mode,
            valid_values=list(_MODE_CHOICES),
            hint="Choose one of the supported --mode values.",
            expected=f"One of: {', '.join(_MODE_CHOICES)}",
        )
        raise typer.Exit(code=2)  # FIX-P2-7: arg validation → exit 2
    _RANK_CHOICES = ("relevance", "centrality", "git-churn")
    if rank_by not in _RANK_CHOICES:
        _emit_error_json(
            INVALID_INPUT_CODE,
            f"Invalid value '{rank_by}' for --rank-by. Valid options: {', '.join(_RANK_CHOICES)}",
            flag="--rank-by",
            value=rank_by,
            valid_values=list(_RANK_CHOICES),
            hint="Choose one of the supported --rank-by values.",
            expected=f"One of: {', '.join(_RANK_CHOICES)}",
        )
        raise typer.Exit(code=2)  # FIX-P2-7: arg validation → exit 2

    # Size gate for --rank-by git-churn: free on small/mid repos, Pro on monoliths.
    if rank_by == "git-churn":
        from sourcecode.license import require_repo_or_pro as _req_git_history
        _req_git_history(_get_detected_path(), "git-history")

    if symbol is not None and not symbol.strip():
        _emit_error_json(
            INVALID_INPUT_CODE,
            "symbol query cannot be empty",
            flag="--symbol",
            hint="Pass a non-empty symbol or omit --symbol.",
            expected="A non-empty symbol query.",
        )
        raise typer.Exit(code=2)

    if symbol and mode not in ("contract", "standard"):
        _emit_error_json(
            INVALID_INPUT_CODE,
            f"--symbol requires --mode contract or standard (got '{mode}'). "
            "Symbol search uses the contract pipeline which does not run in raw mode.",
            flag="--symbol",
            mode=mode,
            hint="Switch to --mode contract or --mode standard.",
            expected="A contract or standard analysis mode.",
        )
        raise typer.Exit(code=2)  # FIX-P2-7: arg validation → exit 2

    if entrypoints_only and mode not in ("contract", "standard"):
        _emit_error_json(
            INVALID_INPUT_CODE,
            f"--entrypoints-only requires --mode contract or standard (got '{mode}').",
            flag="--entrypoints-only",
            mode=mode,
            hint="Switch to --mode contract or --mode standard.",
            expected="A contract or standard analysis mode.",
        )
        raise typer.Exit(code=2)  # FIX-P2-7: arg validation → exit 2

    # Size gate for --full: free on repos up to the size limit, Pro on monoliths.
    if full:
        from sourcecode.license import require_repo_or_pro as _req_full
        _req_full(_get_detected_path(), "--full")

    # P0-2 FIX: --compact and --full are mutually exclusive.
    # compact is designed to be a bounded summary; --full removes truncation limits,
    # which contradicts compact's purpose. Use --agent --full for expanded output.
    if compact and full:
        _emit_error_json(
            INVALID_INPUT_CODE,
            "--compact and --full are mutually exclusive. "
            "--compact produces a bounded summary; --full removes truncation limits "
            "and is meant for --agent mode. Use --agent --full for expanded output.",
            hint="Remove one of the conflicting flags.",
            expected="Exactly one of --compact or --full.",
            flag_conflict=["--compact", "--full"],
        )
        raise typer.Exit(code=1)

    # MEJORA-1: --compact is silently ignored when --agent is used.
    # Always warn (not TTY-gated): user explicitly set both flags, one is being ignored.
    if compact and agent:
        typer.echo(
            "[warning] --compact ignored when --agent is used. --agent takes precedence.",
            err=True,
        )

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
    if changed_only and not compact and not agent and sys.stderr.isatty():
        typer.echo(
            "[info] --changed-only implies --compact (bounding output to changed files).",
            err=True,
        )

    # Validate format choices
    if format not in FORMAT_CHOICES:
        _emit_error_json(
            INVALID_INPUT_CODE,
            f"Invalid value '{format}' for --format. Valid values: {', '.join(FORMAT_CHOICES)}.",
            flag="--format",
            value=format,
            valid_values=list(FORMAT_CHOICES),
            hint="Choose one of the supported --format values.",
            expected=f"One of: {', '.join(FORMAT_CHOICES)}",
        )
        raise typer.Exit(code=2)  # FIX-P2-7: arg validation → exit 2
    if graph_detail not in GRAPH_DETAIL_CHOICES:
        _emit_error_json(
            INVALID_INPUT_CODE,
            f"Invalid value '{graph_detail}' for --graph-detail. Valid values: {', '.join(GRAPH_DETAIL_CHOICES)}.",
            flag="--graph-detail",
            value=graph_detail,
            valid_values=list(GRAPH_DETAIL_CHOICES),
            hint="Choose one of the supported --graph-detail values.",
            expected=f"One of: {', '.join(GRAPH_DETAIL_CHOICES)}",
        )
        raise typer.Exit(code=2)  # FIX-P2-7: arg validation → exit 2
    if docs_depth not in DOCS_DEPTH_CHOICES:
        _emit_error_json(
            INVALID_INPUT_CODE,
            f"Invalid value '{docs_depth}' for --docs-depth. Valid values: {', '.join(DOCS_DEPTH_CHOICES)}.",
            flag="--docs-depth",
            value=docs_depth,
            valid_values=list(DOCS_DEPTH_CHOICES),
            hint="Choose one of the supported --docs-depth values.",
            expected=f"One of: {', '.join(DOCS_DEPTH_CHOICES)}",
        )
        raise typer.Exit(code=2)  # FIX-P2-7: arg validation → exit 2

    # Path was extracted from argv by _preprocess_argv() before Click ran.
    # FIX-P2-8: preserve original user input in error messages (Windows Git Bash
    # rewrites "/nonexistent" → "C:\Program Files\Git\nonexistent" via Path.resolve()).
    _raw_path_input = _get_detected_path()
    target = Path(_raw_path_input).resolve()
    if not target.exists():
        _emit_error_json(
            INVALID_INPUT_CODE,
            f"Directory '{_raw_path_input}' does not exist.",
            path=_raw_path_input,
            hint="Pass an existing repository directory.",
            expected="An existing directory path.",
        )
        raise typer.Exit(code=1)
    if not target.is_dir():
        _emit_error_json(
            INVALID_INPUT_CODE,
            f"Path '{_raw_path_input}' is not a directory.",
            path=_raw_path_input,
            hint="Pass a repository directory, not a file.",
            expected="A directory path.",
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
    from sourcecode.serializer import agent_view, compact_view, normalize_source_map, standard_view, validate_cross_analyzer_consistency, validate_source_map
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
    effective_depth = max(depth, _JAVA_MIN_SCAN_DEPTH) if _is_java and depth < _JAVA_MIN_SCAN_DEPTH else depth

    if symbol is not None and _is_java:
        _emit_error_json(
            INVALID_INPUT_CODE,
            "--symbol is not supported for Java/JVM repositories. "
            "Per-file AST extraction is unavailable for JVM — symbol search only works with Python, TypeScript, and JavaScript. "
            "Alternatives: use --agent --compact to get file relevance scores, "
            "or use --git-context to find recently changed files.",
            flag="--symbol",
            hint="Use a non-Java repository or omit --symbol.",
            expected="A repository where symbol extraction is supported.",
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

    # --compact implicitly enables lightweight analysis passes so that
    # dependency_summary, env_summary and code_notes_summary are never null.
    # architecture=True is also enabled so that architecture.confidence is
    # consistent with --agent (which auto-enables architecture).  The
    # ArchitectureAnalyzer is path-based and adds negligible latency.
    # NOTE: must happen BEFORE cache key computation so key reflects effective flags.
    if compact:
        dependencies = True
        env_map = True
        code_notes = True
        architecture = True

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
    # HEAD SHA is diagnostic metadata — compute unconditionally, not tied to cache.
    try:
        _sha_r = _sub.run(
            ["git", "-C", str(target), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=3,
        )
        _git_sha = _sha_r.stdout.strip()
    except Exception:
        pass

    _core_key = ""
    _view_key = ""
    _core_hash = ""
    _core_flags_str = ""
    _view_flags_str = ""

    if not no_cache:
        try:
            # Detect actual git root (may be an ancestor of target for monorepos,
            # multi-project repos, or SVN-migrated trees where .git is in a parent).
            # The original "(target / '.git').exists()" check broke these layouts.
            _git_root_str = ""
            if _git_sha:
                try:
                    _gr_r = _sub.run(
                        ["git", "-C", str(target), "rev-parse", "--show-toplevel"],
                        capture_output=True, text=True, timeout=3,
                    )
                    if _gr_r.returncode == 0:
                        _git_root_str = _gr_r.stdout.strip()
                except Exception:
                    pass

            _excl_key = (
                ",".join(sorted(e.strip() for e in exclude.split(",") if e.strip()))
                if exclude else ""
            )

            # ── Core (analysis) flags: affect which analyzers run + scan config ──
            # Use effective_depth (not raw depth) so Java auto-adjustment is captured.
            # acv = ANALYZER_CACHE_VERSION — bumped only when analysis logic/schema
            # changes, NOT on every package release.  Prevents patch-bump cache wipes.
            _core_flags_str = (
                f"acv={_cache_mod.ANALYZER_CACHE_VERSION},"
                f"dep={dependencies},gm={graph_modules},"
                f"docs={docs},fm={full_metrics},sem={semantics},"
                f"arch={architecture},gc={git_context},em={env_map},"
                f"cn={code_notes},"
                f"ex={_excl_key},depth={effective_depth}"
            )
            _core_h = _hashlib.sha256(_core_flags_str.encode()).hexdigest()[:8]
            if _git_sha and _git_root_str:
                _core_key = f"{_git_sha}-{_core_h}"
            else:
                # No git history (untracked/no-commit repo) — stable synthetic key
                # scoped per repo path via cache_dir(); invalidated by --no-cache or cache clear.
                _core_key = f"nogit-{_core_h}"

            # ── View flags: output presentation only (no re-analysis needed) ──
            _view_flags_str = (
                f"c={compact},ag={agent},mode={mode},fmt={format},full={full},"
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

            # P1-A: --env-map misses L1 when base (em=False) exists.
            # Try the base key so env analysis can be injected lazily (<1 s)
            # instead of triggering a 17 s full rescan.
            _l1_needs_env_inject = False
            if _l1_result is None and env_map:
                _base_flags = _core_flags_str.replace(",em=True,", ",em=False,")
                _base_h8 = _hashlib.sha256(_base_flags.encode()).hexdigest()[:8]
                _sha_prefix = _git_sha if _git_sha else "nogit"
                _base_key = f"{_sha_prefix}-{_base_h8}"
                _base_result = _cache_mod.read_core(target, _base_key)
                if _base_result is not None:
                    _l1_result = _base_result
                    _l1_needs_env_inject = True

            if _l1_result is not None:
                _core_dict_l1, _core_hash = _l1_result
                _view_key = f"{_core_hash}-{_view_h}"

                # Step 2: try L2 (exact view match).
                # Skip L2 for --changed-only: the stored view is a previous
                # diff snapshot that is stale for the current diff.
                if not changed_only:
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
                        # P1-A: inject env analysis when base L1 (em=False) was used.
                        # EnvAnalyzer walks only env/config files — typically <1 s.
                        if _rebuilt is not None and _l1_needs_env_inject and compact:
                            try:
                                from sourcecode.env_analyzer import EnvAnalyzer as _EnvA_p1a
                                _env_r_p1a, _env_s_p1a = _EnvA_p1a().analyze(target, {})
                                if _env_s_p1a and (getattr(_env_s_p1a, "total", 0) or _env_r_p1a):
                                    _es_p1a: dict = {
                                        "total": getattr(_env_s_p1a, "total", 0),
                                        "required": getattr(_env_s_p1a, "required_count", 0),
                                    }
                                    _cats = getattr(_env_s_p1a, "categories", None)
                                    if _cats:
                                        _es_p1a["categories"] = _cats
                                    _rebuilt = dict(_rebuilt)
                                    _rebuilt["env_summary"] = _es_p1a
                                    if _env_r_p1a:
                                        _sorted_er = sorted(
                                            _env_r_p1a,
                                            key=lambda e: (
                                                not getattr(e, "required", False),
                                                getattr(e, "key", ""),
                                            ),
                                        )
                                        _rebuilt["env_map"] = [
                                            {
                                                "key": getattr(e, "key", ""),
                                                **({"required": True} if getattr(e, "required", False) else {}),
                                                **({"category": getattr(e, "category", None)} if getattr(e, "category", None) else {}),
                                            }
                                            for e in _sorted_er[:15]
                                        ]
                            except Exception:
                                pass  # env inject failed — continue without env data
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
                            _cache_hit_content = _serialize_dict(_rebuilt, format)
                            # Cache rebuilt view in L2 (skip for --changed-only: stale diff)
                            if _cache_hit_content and not changed_only:
                                _cache_mod.write_view(
                                    target,
                                    _view_key,
                                    _cache_hit_content,
                                    fmt=format,
                                )
                    except Exception:
                        _cache_hit_content = None  # rebuild failed → full analysis

        except Exception:
            _core_key = ""
            _view_key = ""
            _core_hash = ""

    if _cache_hit_content is not None:
        _hit_source = "L2_view" if (_view_key and _core_hash) else "L1_core"

        # P0-A/B/C: --changed-only fast path via warm cache.
        if changed_only:
            _co_git_ok = False
            _co_uc_files: set[str] = set()
            try:
                from sourcecode.git_analyzer import GitAnalyzer as _GitAnalyzerCO
                _gc_co = _GitAnalyzerCO().analyze(target, depth=1, days=1)
                _bad_gc_co = {"no_git_repo", "git_not_found", "git_timeout"}
                if _gc_co and not (_bad_gc_co & set(_gc_co.limitations)):
                    _co_git_ok = True
                    _uc_co = _gc_co.uncommitted_changes
                    if _uc_co:
                        _uc_untracked = {p for p in _uc_co.untracked if not p.endswith("/")}
                        _co_uc_files = set(_uc_co.staged) | set(_uc_co.unstaged) | _uc_untracked
            except Exception:
                pass

            if not _co_git_ok:
                # Git unavailable — disable changed_only, fall through to normal cached output.
                changed_only = False
                typer.echo("[changed-only] git unavailable — falling back to full scan.", err=True)
            elif not _co_uc_files:
                # Clean repo — unified empty schema.
                _co_clean = json.dumps({
                    "schema_version": "1.0",
                    "changed_files_count": 0,
                    "changed_files": [],
                    "analysis_scope": "empty",
                    "context": None,
                    "_meta": {"changed_only": True, "cache_source": _hit_source},
                }, ensure_ascii=False)
                _emit_command_output(_co_clean, output, copy)
                return
            else:
                # Dirty repo — filter file_paths in cached compact, unified schema.
                try:
                    _co_base = json.loads(_cache_hit_content)
                    _fps_all = _co_base.get("file_paths", [])
                    _co_base["file_paths"] = [
                        p for p in _fps_all
                        if p in _co_uc_files or _is_always_include_ref(p)
                    ]
                    _co_base.pop("_cache", None)
                    _co_dirty = json.dumps({
                        "schema_version": "1.0",
                        "changed_files_count": len(_co_uc_files),
                        "changed_files": sorted(_co_uc_files),
                        "analysis_scope": "partial",
                        "context": _co_base,
                        "_meta": {"changed_only": True, "cache_source": _hit_source},
                    }, indent=2, ensure_ascii=False)
                    _emit_command_output(_co_dirty, output, copy)
                    return
                except Exception:
                    # Parse failed — fall through to full scan.
                    changed_only = False
                    typer.echo("[changed-only] cache parse failed — falling back to full scan.", err=True)

        if not changed_only:
            if format == "json":
                try:
                    from sourcecode.ris import _has_uncommitted_changes as _huc
                    _uncommitted = _huc(target)
                except Exception:
                    _uncommitted = False
                _data_scope = "COMPACT" if compact else ("AGENT" if agent else "FULL")
                # Recover generated_at from cached content before overwriting _cache block.
                _cached_generated_at = None
                try:
                    import json as _json_ga
                    _cached_generated_at = (
                        _json_ga.loads(_cache_hit_content)
                        .get("_cache", {})
                        .get("generated_at")
                    )
                except Exception:
                    pass
                _cache_hit_content = _inject_cache_meta(_cache_hit_content, {
                    "cache_source": _hit_source,
                    "git_head_at_generation": _git_sha,
                    "current_git_head": _git_sha,
                    "is_stale": False,
                    "has_uncommitted_changes": _uncommitted,
                    "generated_at": _cached_generated_at,
                    "data_scope": _data_scope,
                })
                # Patch git_context.uncommitted_files when there are working-tree
                # changes — the cached body has stale 0 from generation time.
                if git_context and _uncommitted:
                    try:
                        import json as _json_gc
                        import subprocess as _sub_gc
                        _patched = _json_gc.loads(_cache_hit_content)
                        if "git_context" in _patched:
                            _uc_r = _sub_gc.run(
                                ["git", "-C", str(target), "status", "--porcelain"],
                                capture_output=True, text=True, timeout=3,
                            )
                            _uc_count = len(
                                [l for l in _uc_r.stdout.splitlines() if l.strip()]
                            )
                            _patched["git_context"]["uncommitted_files"] = _uc_count
                            _patched["git_context"]["_stale_fields_refreshed"] = [
                                "uncommitted_files"
                            ]
                            _cache_hit_content = _json_gc.dumps(
                                _patched, indent=2, ensure_ascii=False
                            )
                    except Exception:
                        pass  # stale value better than crash
            _emit_command_output(_cache_hit_content, output, copy)
            return

    _extra_excludes: Optional[frozenset[str]] = None
    if exclude:
        _extra_excludes = frozenset(e.strip() for e in exclude.split(",") if e.strip())
        # IMP-2: warn if the exclude value looks like it was swallowed as a path
        # (BUG-2 symptom in older versions: --exclude value consumed as repo path).
        if len(_extra_excludes) == 1 and Path(list(_extra_excludes)[0]).is_dir():
            sys.stderr.write(
                f"[sourcecode] Warning: --exclude value '{list(_extra_excludes)[0]}' is a directory path. "
                "If this was meant as a pattern, use --exclude=pattern or --exclude pattern (both are supported).\n"
            )
            sys.stderr.flush()

    _progress = Progress()
    _progress.start("scanning files")

    scanner = AdaptiveScanner(target, topology=_topology, base_depth=effective_depth,
                               extra_excludes=_extra_excludes)
    raw_tree = scanner.scan_tree()

    _progress.update("parsing manifests")
    # 2. Filter .env and *.secret entries from file tree (SEC-02, all levels)
    file_tree = filter_sensitive_files(raw_tree, redactor)
    detector = ProjectDetector(build_default_detectors())
    workspace_analysis = WorkspaceAnalyzer().analyze(target, manifests)

    # Adaptive traversal handles monorepo source root discovery automatically.
    # Emit a diagnostic when topology confidence is low so users know why.
    if _topology.workspace_type == "monorepo" and _topology.confidence < 0.5:
        if sys.stderr.isatty():
            typer.echo(
                "[traversal] monorepo detected but source root confidence is low "
                f"({_topology.confidence:.0%}). Use --depth 8 or higher if files are missing.",
                err=True,
            )

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
            _emit_error_json(
                INVALID_INPUT_CODE,
                f"invalid values for --graph-edges: "
                f"{', '.join(invalid_edges)}. Valid options: {', '.join(sorted(GRAPH_EDGE_CHOICES))}",
                flag="--graph-edges",
                value=invalid_edges,
                valid_values=sorted(GRAPH_EDGE_CHOICES),
                hint="Choose one or more supported graph edge types.",
                expected=f"One or more of: {', '.join(sorted(GRAPH_EDGE_CHOICES))}",
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
        workspace_tree = filter_sensitive_files(workspace_scanner.scan_tree(), redactor)
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
    # Size-gated node cap: free users get the full graph on small/mid repos;
    # the cap only applies to enterprise-scale monoliths (Pro territory).
    _effective_max_nodes = max_nodes
    try:
        from sourcecode.license import is_pro as _lp_is_pro_gm, is_large_repo as _lp_large_gm
        _gm_capped = (not _lp_is_pro_gm) and _lp_large_gm(str(target))
    except Exception:
        _gm_capped = False
    if _gm_capped and graph_analyzer is not None:
        if _effective_max_nodes is None or _effective_max_nodes > _FREE_TIER_NODE_CAP:
            _effective_max_nodes = _FREE_TIER_NODE_CAP

    module_graph = (
        graph_analyzer.merge_graphs(
            module_graphs,
            detail=graph_detail_typed,
            edge_kinds=parsed_graph_edges,
            max_nodes=_effective_max_nodes,
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
                            AdaptiveScanner(target / ws.path, base_depth=depth).scan_tree(),
                            redactor,
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

    # Size-gated semantic cap: free users get all symbols on small/mid repos;
    # the cap only applies to enterprise-scale monoliths (Pro territory).
    if semantic_analyzer is not None and sm.semantic_symbols:
        try:
            from sourcecode.license import is_pro as _lp_is_pro_sem, is_large_repo as _lp_large_sem
            _sem_capped = (not _lp_is_pro_sem) and _lp_large_sem(str(target))
        except Exception:
            _sem_capped = False
        if _sem_capped and len(sm.semantic_symbols) > _FREE_TIER_NODE_CAP:
            sm = replace(sm, semantic_symbols=sm.semantic_symbols[:_FREE_TIER_NODE_CAP])

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
        _git_confirmed_clean = False
        try:
            _gc_early = _GitAnalyzerEarly().analyze(target, depth=1, days=1)
            _bad_gc = {"no_git_repo", "git_not_found", "git_timeout"}
            if _gc_early and not (_bad_gc & set(_gc_early.limitations)):
                _uc = _gc_early.uncommitted_changes
                if _uc:
                    # Include untracked (new files not yet staged) so new source files
                    # are analyzed under --changed-only, not silently treated as "clean".
                    # Exclude directory entries (trailing "/") — e.g. untracked tool
                    # cache dirs like ".sourcecode-cache/" are dirs not source files.
                    _uc_files = {p for p in _uc.untracked if not p.endswith("/")}
                    _allowed_changed_files = set(_uc.staged) | set(_uc.unstaged) | _uc_files
                if not _allowed_changed_files:
                    # Git is available and confirms no uncommitted changes.
                    # Do NOT fall back to a full scan — that would silently produce
                    # output identical to --compact, making it impossible for the
                    # caller to distinguish "no changes" from "changes found".
                    _git_confirmed_clean = True
            else:
                # Git unavailable — fall back gracefully.
                typer.echo(
                    "[changed-only] git unavailable — falling back to full scan.",
                    err=True,
                )
                changed_only = False
        except Exception:
            typer.echo("[changed-only] git error — falling back to full scan.", err=True)
            changed_only = False
        if _git_confirmed_clean:
            _nc_payload = json.dumps({
                "schema_version": "1.0",
                "changed_files_count": 0,
                "changed_files": [],
                "analysis_scope": "empty",
                "context": None,
                "_meta": {"changed_only": True, "cache_source": "none"},
            }, ensure_ascii=False)
            _emit_command_output(_nc_payload, output, False)
            raise typer.Exit()

    # Contract pipeline — runs for mode=contract|standard|deep|hybrid (skip for raw)
    _progress.update("extracting contracts")
    _is_contract_mode = mode in ("contract", "standard")
    _pipeline_error = False
    if _is_contract_mode:
        from sourcecode.contract_pipeline import ContractPipeline
        from sourcecode.contract_model import ContractSummary as _ContractSummary
        # FIX-1: Java projects need higher caps — many files, comprehensive coverage required
        _is_jvm = any(s.stack in _JVM_STACKS for s in sm.stacks)
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
                entrypoints_only=entrypoints_only,
                changed_only=changed_only,
                symbol=symbol,
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
            _is_jvm_repo = any(s.stack in _JVM_STACKS for s in sm.stacks)
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
        content = _serialize_dict(data, format)
    elif agent:
        data = agent_view(sm, full=full)
        if not no_redact:
            data = redact_dict(data)
        # P0-1: Apply output budget — safety net for large repos.
        # Skip budget when writing to a file (no size constraint); warn on stdout.
        from sourcecode.output_budget import trim_to_budget as _trim, BUDGET_AGENT
        data = _trim(data, BUDGET_AGENT, label="agent", skip=(output is not None), warn_stderr=(output is None))
        # FIX-P0-2: agent mode must honour --format yaml (previously always emitted JSON).
        content = _serialize_dict(data, format)
    elif compact:
        # P0-C: compute compact_view with full sm.file_paths so mybatis/angular
        # counts are correct; filter file_paths in the output dict, not in sm.
        data = compact_view(sm, no_tree=no_tree, full=full)
        if changed_only and _allowed_changed_files:
            # GAP-5: preserve full entry_points; filter file_paths display only.
            # ALWAYS-INCLUDE: security-const files stay for Java constant refs.
            _fps_full = data.get("file_paths", [])
            data["file_paths"] = [
                p for p in _fps_full
                if p in _allowed_changed_files or _is_always_include_ref(p)
            ]
        if not no_redact:
            data = redact_dict(data)
        # P0-1: Apply output budget — safety net for large repos.
        # Skip budget when writing to a file (no size constraint); warn on stdout.
        from sourcecode.output_budget import trim_to_budget as _trim_c, BUDGET_COMPACT
        data = _trim_c(data, BUDGET_COMPACT, label="compact", skip=(output is not None), warn_stderr=(output is None))
        content = _serialize_dict(data, format)
    else:
        raw_dict = standard_view(sm, include_tree=tree and not no_tree)
        if not no_redact:
            raw_dict = redact_dict(raw_dict)
        content = _serialize_dict(raw_dict, format)

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
    if format == "json":
        try:
            from sourcecode.ris import _has_uncommitted_changes as _huc_fresh
            _uncommitted_fresh = _huc_fresh(target)
            # _huc_fresh uses --untracked-files=no — it misses repos where all
            # files are untracked (no staged/unstaged changes to tracked files).
            # Promote to True when _allowed_changed_files contains untracked files.
            if not _uncommitted_fresh and _allowed_changed_files:
                _uncommitted_fresh = True
        except Exception:
            _uncommitted_fresh = False
        import datetime as _dt
        _data_scope_fresh = "COMPACT" if compact else ("AGENT" if agent else "FULL")
        content = _inject_cache_meta(content, {
            "cache_source": "fresh",
            "git_head_at_generation": _git_sha,
            "current_git_head": _git_sha,
            "is_stale": False,
            "has_uncommitted_changes": _uncommitted_fresh,
            "generated_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "data_scope": _data_scope_fresh,
        })
        # P0-B: wrap --changed-only cold-start dirty output in unified schema.
        if changed_only and compact:
            try:
                _co_inner = json.loads(content)
                content = json.dumps({
                    "schema_version": "1.0",
                    "changed_files_count": len(_allowed_changed_files) if _allowed_changed_files else 0,
                    "changed_files": sorted(_allowed_changed_files) if _allowed_changed_files else [],
                    "analysis_scope": "partial" if _allowed_changed_files else "empty",
                    "context": _co_inner,
                    "_meta": {"changed_only": True, "cache_source": "fresh"},
                }, indent=2, ensure_ascii=False)
            except Exception:
                pass
    _emit_command_output(content, output, copy if not _pipeline_error else False)

    # Persist to two-layer cache (git SHA unchanged → re-use on next run).
    #
    # L1 (core): stores pre-computed compact+agent+standard views at max
    #   fidelity so any subsequent view can be derived without re-analysis.
    # L2 (view): stores the exact rendered string for this flag combination.
    #
    # GC runs after L2 write to evict old commits and orphaned blobs/views.
    # Writes happen in a background daemon thread so cold-run latency is not
    # penalised by gzip encoding + disk I/O.  atexit join ensures writes
    # complete on clean exit without blocking the user-visible response.
    # Skip cache write for --changed-only: content is a filtered diff view,
    # not valid for reuse as a base compact on subsequent runs.
    if not no_cache and _core_key and not _pipeline_error and not changed_only:
        import atexit as _atexit
        import threading as _threading

        # Pre-compute core dict in the main thread — avoids the 5-second atexit
        # join race on large repos where core_view(sm) itself takes seconds.
        # Background thread only does gzip + disk I/O (fast, bounded latency).
        _bg_core_dict: dict | None = None
        try:
            from sourcecode.serializer import core_view as _core_view_fn
            _bg_core_dict = _core_view_fn(sm)
        except Exception:
            pass

        if _bg_core_dict is not None:
            _bg_target = target
            _bg_core_key = _core_key
            _bg_view_key = _view_key
            _bg_view_flags_str = _view_flags_str
            _bg_content = content
            _bg_format = format
            _bg_hashlib = _hashlib
            _bg_cache_mod = _cache_mod

            def _write_cache_async() -> None:
                try:
                    _written_core_hash = _bg_cache_mod.write_core(
                        _bg_target, _bg_core_key, _bg_core_dict
                    )
                    if _written_core_hash:
                        _vk = _bg_view_key
                        if not _vk:
                            _wvh = _bg_hashlib.sha256(_bg_view_flags_str.encode()).hexdigest()[:8]
                            _vk = f"{_written_core_hash}-{_wvh}"
                        _bg_cache_mod.write_view(
                            _bg_target,
                            _vk,
                            _bg_content,
                            fmt=_bg_format,
                            layers=_compute_analyzer_fingerprints(),
                        )
                        from sourcecode.cache import cache_dir as _cdir, _gc as _run_gc
                        _run_gc(_cdir(_bg_target))
                except Exception:
                    pass

            _cache_write_thread = _threading.Thread(target=_write_cache_async, daemon=True)
            _cache_write_thread.start()
            _atexit.register(_cache_write_thread.join, 30.0)

    # Update RIS with aggregated snapshot data (non-fatal side-effect).
    # Update RIS whenever git is available, even when L1/L2 cache is skipped
    # (e.g. target is a subdirectory of the git root — _core_key may be "").
    if not no_cache and not _pipeline_error and _git_sha:
        try:
            from sourcecode.serializer import core_view as _ris_core_view
            from sourcecode.ris import maybe_update_ris as _ris_update
            _ris_update(target, _ris_core_view(sm), _git_sha)
        except Exception:
            pass

    if _pipeline_error:
        raise typer.Exit(code=2)

    # 7. One-time MCP setup nudge (stderr only — does not affect exit code or stdout)
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
    # Emit 'file' as backward-compat alias for 'path' for one release
    if "path" in d:
        d["file"] = d["path"]
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
        priority = next((p for thresh, p in _IMPACT_PRIORITY_THRESHOLDS if raw >= thresh), "low")
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
        _emit_error_json(
            INVALID_INPUT_CODE,
            f"task is required. Available: {', '.join(TASKS)}\n"
            "Use --task-help for descriptions.",
            flag="task",
            hint="Pass one of the documented prepare-context tasks.",
            expected=f"One of: {', '.join(TASKS)}",
        )
        raise typer.Exit(code=1)

    if task not in TASKS:
        _emit_error_json(
            INVALID_INPUT_CODE,
            f"unknown task '{task}'. Available: {', '.join(TASKS)}",
            flag="task",
            value=task,
            hint="Choose one of the supported prepare-context tasks.",
            expected=f"One of: {', '.join(TASKS)}",
        )
        raise typer.Exit(code=1)

    # Hybrid gate: generate-tests free on small/mid repos, Pro on monoliths.
    # delta is the automation axis: free quota per repo, then Pro (CI/CD usage).
    if task == "generate-tests":
        from sourcecode.license import require_repo_or_pro as _require_gentests
        _require_gentests(str(path.resolve()), "generate-tests")
    elif task == "delta":
        from sourcecode.license import is_pro as _delta_is_pro
        if not _delta_is_pro:
            from sourcecode.license import check_delta_free_tier as _check_delta
            _delta_allowed, _delta_used, _delta_remaining = _check_delta(str(path.resolve()))
            if not _delta_allowed:
                from sourcecode.license import require_feature as _require_feature_delta
                _require_feature_delta(
                    "delta",
                    extra_fields={
                        "free_tier_note": (
                            f"Free quota of {30} delta runs per repository exhausted."
                        ),
                        "free_tier_alternative": "sourcecode prepare-context review-pr --since <ref>",
                    },
                )
            # Within quota: emit a header note so CI logs show remaining runs.
            elif _delta_remaining <= 5:
                import sys as _sys_delta
                _sys_delta.stderr.write(
                    f"[sourcecode] delta free tier: {_delta_remaining} run(s) remaining"
                    f" (used {_delta_used}/{30}). Upgrade to Pro for unlimited CI runs.\n"
                )
                _sys_delta.stderr.flush()

    # Validate --format: only "json" and "github-comment" are valid for prepare-context.
    # "yaml" is intentionally NOT supported here (use main command for yaml output).
    # Invalid values must error loudly — silently falling through to JSON is a lie.
    _PC_FORMAT_CHOICES = ("json", "github-comment")
    if format is not None and format not in _PC_FORMAT_CHOICES:
        _emit_error_json(
            INVALID_INPUT_CODE,
            f"invalid value '{format}' for --format. "
            f"Valid options: {', '.join(_PC_FORMAT_CHOICES)}.",
            flag="--format",
            value=format,
            valid_values=list(_PC_FORMAT_CHOICES),
            hint="Choose one of the supported prepare-context output formats.",
            expected=f"One of: {', '.join(_PC_FORMAT_CHOICES)}",
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
            INVALID_INPUT_CODE,
            f"'{target}' is not a valid directory.",
            path=str(target),
            hint="Pass an existing repository directory.",
            expected="A directory path.",
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

    # Task-level cache: keyed on (task, git_head, symptom) so warm calls complete in <1s.
    # Skip for diff-dependent tasks (delta, review-pr), fast mode, and llm_prompt
    # (those embed per-call content that must not be served from cache).
    import subprocess as _pctx_sub
    import hashlib as _pctx_hash
    from sourcecode import cache as _pctx_cache
    _pctx_git_sha = ""
    _pctx_cache_key = ""
    _pctx_cacheable = task not in ("delta", "review-pr") and not fast and not llm_prompt
    if _pctx_cacheable:
        try:
            _sha_r2 = _pctx_sub.run(
                ["git", "-C", str(target), "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, timeout=3,
            )
            _pctx_git_sha = _sha_r2.stdout.strip()
        except Exception:
            pass
        if _pctx_git_sha:
            _sym_h = _pctx_hash.sha256((symptom or "").encode()).hexdigest()[:8]
            _pctx_cache_key = f"pctx-{task}-{_pctx_git_sha}-{_sym_h}-{format or 'json'}"
            _cached_pctx = _pctx_cache.read(target, _pctx_cache_key)
            if _cached_pctx is not None:
                _emit_command_output(_cached_pctx, output_path, copy)
                return

    builder = TaskContextBuilder(target)
    _progress = Progress()
    _phase = f"analyzing ({task})"
    if since:
        _phase += f" since {since}"
    _progress.start(_phase)
    if not fast:
        if sys.stderr.isatty():
            sys.stderr.write(f"Analyzing ({task})... (deep scan may take 15–35 s for large codebases)\n")
            sys.stderr.flush()
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
                if sys.stderr.isatty():
                    sys.stderr.write(
                        f"[generate-tests] timeout after {_timeout_ms}ms — returning partial result\n"
                    )
                    sys.stderr.flush()
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
        _rfs = output.relevant_files
        if task == "generate-tests":
            # relevant_files goal: untested SOURCE files. Test files belong in test_gaps.
            # Without this filter, high-churn test files rank above untested source files.
            _rfs = [f for f in _rfs if getattr(f, "role", None) != "test"]
        _serialized_rfs = [_serialize_relevant_file(f) for f in _rfs]
        out["relevant_files"] = _serialized_rfs
        if task == "fix-bug":
            # ranked_files was the v1 name for this field — emit as backward-compat alias.
            out["ranked_files"] = _serialized_rfs
    if _task_include("key_dependencies") and output.key_dependencies:
        out["key_dependencies"] = output.key_dependencies
    if _task_include("gaps") and output.gaps:
        out["gaps"] = output.gaps
    if _task_include("suspected_areas") and output.suspected_areas:
        out["suspected_areas"] = output.suspected_areas
    if _task_include("improvement_opportunities") and output.improvement_opportunities:
        out["improvement_opportunities"] = output.improvement_opportunities
    if _task_include("test_gaps") and output.test_gaps:
        # Emit both the canonical name (untested_sources) and the compat alias (test_gaps)
        # so agents can use either. untested_sources is the correct semantic name.
        out["untested_sources"] = output.test_gaps
        out["test_gaps"] = output.test_gaps  # backward compat alias
        if task == "generate-tests":
            _et_count = getattr(output, "existing_test_count", None)
            if _et_count is not None:
                out["existing_test_count"] = _et_count
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
                _safe_write_file(output_path, _err_json)
            else:
                sys.stdout.buffer.write(_err_json.encode("utf-8"))
                sys.stdout.buffer.write(b"\n")
                sys.stdout.buffer.flush()
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
                _safe_write_file(output_path, _nc_json)
            else:
                sys.stdout.buffer.write(_nc_json.encode("utf-8"))
                sys.stdout.buffer.write(b"\n")
                sys.stdout.buffer.flush()
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
                _safe_write_file(output_path, _err_json)
            else:
                sys.stdout.buffer.write(_err_json.encode("utf-8"))
                sys.stdout.buffer.write(b"\n")
                sys.stdout.buffer.flush()
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

    # Size-gated preview: on enterprise-scale monoliths free users get a capped
    # preview (top-5 / lightweight) + upgrade note. Small/mid repos get the full
    # analysis for free — gating is by repo size, never by command.
    if task in ("fix-bug", "review-pr"):
        from sourcecode.license import is_pro as _tier_is_pro, is_large_repo as _tier_large
        if (not _tier_is_pro) and _tier_large(str(path.resolve())):
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

    if _pctx_cacheable and _pctx_cache_key and format != "github-comment":
        try:
            _pctx_cache.write(target, _pctx_cache_key, _pc_content)
        except Exception:
            pass

    _emit_command_output(_pc_content, output_path, copy)

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
    force: bool = typer.Option(
        False,
        "--force",
        help="Bypass the token-size guard and emit output even when estimated tokens exceed 50K.",
    ),
    gzip_output: bool = typer.Option(
        False,
        "--gzip",
        help="Compress output with gzip. Requires --output. Reduces large IR files by ~70-80%.",
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
      --gzip                  Compress output file (~70-80% smaller; requires --output)

    \b
    Examples:
      sourcecode repo-ir
      sourcecode repo-ir /path/to/repo --since HEAD~1
      sourcecode repo-ir --files src/main/java/UserService.java
      sourcecode repo-ir --since main --output ir.json
      sourcecode repo-ir --since HEAD~3 --summary-only --output ir-small.json
      sourcecode repo-ir --max-nodes 200 --max-edges 500
      sourcecode repo-ir --output ir.json.gz --gzip
    """
    import json as _json

    from sourcecode.repository_ir import apply_ir_size_limits, build_repo_ir, find_java_files

    if format not in ("json", "yaml"):
        _emit_error_json(
            INVALID_INPUT_CODE,
            f"Invalid format '{format}'.",
            hint="Valid values: json, yaml.",
            expected="json | yaml",
        )
        raise typer.Exit(code=1)

    root = path.resolve()
    if not root.is_dir():
        _emit_error_json(
            INVALID_INPUT_CODE,
            f"'{root}' is not a valid directory.",
            path=str(root),
            hint="Pass an existing repository directory.",
            expected="A directory path.",
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

    if since:
        import subprocess as _sp
        _ref_check = _sp.run(
            ["git", "rev-parse", "--verify", since],
            cwd=str(root),
            capture_output=True,
        )
        if _ref_check.returncode != 0:
            _emit_error_json(
                INVALID_INPUT_CODE,
                f"Git ref '{since}' could not be resolved in '{root}'.",
                hint="Pass a valid git ref (branch, tag, commit hash, HEAD~N).",
                expected="A resolvable git ref.",
            )
            raise typer.Exit(code=1)

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
        if gzip_output and not str(output_path).endswith(".gz"):
            output_path = output_path.with_suffix(output_path.suffix + ".gz")
        raw_bytes = output.encode("utf-8")
        size_bytes = len(raw_bytes)
        _SIZE_WARN_BYTES = 10 * 1024 * 1024  # 10MB
        if size_bytes > _SIZE_WARN_BYTES and not gzip_output:
            typer.echo(
                f"[repo-ir] Output is {size_bytes // (1024 * 1024)}MB — "
                "consider --summary-only, --max-nodes N --max-edges N, or --gzip to compress.",
                err=True,
            )
        if gzip_output:
            import gzip as _gzip
            with _gzip.open(output_path, "wb") as _gz:
                _gz.write(raw_bytes)
            compressed_kb = output_path.stat().st_size // 1024
            size_kb = size_bytes // 1024
            typer.echo(
                f"IR written to {output_path} ({compressed_kb}KB gzip, {size_kb}KB uncompressed)",
                err=True,
            )
        else:
            output_path.write_bytes(raw_bytes)
            size_kb = size_bytes // 1024
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
        if gzip_output:
            _emit_error_json(
                INVALID_INPUT_CODE,
                "--gzip requires --output FILE.",
                hint="Add --output ir.json.gz to write compressed output to a file.",
                expected="--output path when --gzip is used.",
            )
            raise typer.Exit(1)
        _ir_size = len(output.encode("utf-8"))
        _ir_tokens_est = _ir_size // 4
        # P1-C: abort when estimated tokens > 50K unless --force or --output is given.
        if _ir_tokens_est > 50_000 and not force:
            if summary_only:
                _hint = (
                    "Use --max-nodes N --max-edges N to cap graph size, "
                    "--output FILE to save to disk, or --force to bypass this guard."
                )
            else:
                _hint = (
                    "Use --summary-only (~5K tokens), --max-nodes N --max-edges N, "
                    "--output FILE to save to disk, or --force to bypass this guard."
                )
            _emit_error_json(
                "OUTPUT_TOO_LARGE",
                f"Estimated output is ~{_ir_tokens_est // 1000}K tokens — too large for most LLM context windows.",
                hint=_hint,
                expected="Output under 50K estimated tokens.",
            )
            raise typer.Exit(1)
        if _ir_tokens_est > 10_000:
            if summary_only:
                sys.stderr.write(
                    f"[repo-ir] ~{_ir_tokens_est // 1000}K tokens — "
                    "use --max-nodes N --max-edges N or --output FILE for smaller output.\n"
                )
            else:
                sys.stderr.write(
                    f"[repo-ir] ~{_ir_tokens_est // 1000}K tokens — "
                    "use --summary-only or --output FILE for smaller output.\n"
                )
            sys.stderr.flush()
        _emit_command_output(output, None, copy)


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
    NOTE: Free on repos up to the size limit; Pro unlocks enterprise-scale
    monoliths. Run 'sourcecode license' for details.

    \b
    Examples:
      sourcecode impact UserService
      sourcecode impact org.keycloak.services.DefaultKeycloakSession /path/to/keycloak
      sourcecode impact UserService --depth 6 --output impact.json
    """
    from sourcecode.license import require_repo_or_pro as _require_repo_or_pro
    _require_repo_or_pro(str(path.resolve()), "impact")

    if format not in ("json", "yaml"):
        _emit_error_json(
            INVALID_INPUT_CODE,
            f"Invalid format '{format}'.",
            hint="format must be: json or yaml.",
            expected="json | yaml",
        )
        raise typer.Exit(code=1)

    from sourcecode.repository_ir import (
        build_repo_ir, find_java_files, compute_blast_radius,
    )
    from sourcecode.output_budget import trim_to_budget as _trim, BUDGET_IMPACT

    # Legacy-compat: old syntax was `impact <path> <target>`.
    # Detect: target resolves to an existing directory (not a class name), and
    # the path arg is not a valid directory (looks like a class name).
    _target_as_path = Path(target)
    if _target_as_path.is_dir() and not path.resolve().is_dir():
        # Gate on isatty() — non-TTY (MCP, pipes) must not receive text mixed into JSON stdout.
        if getattr(sys.stderr, "isatty", lambda: False)():
            sys.stderr.write(
                f"[impact] Legacy argument order detected: '{target}' is a directory, not a class name.\n"
                f"[impact] Swapping: target='{path}', path='{target}'. "
                f"New syntax: sourcecode impact <target> [path]\n"
            )
            sys.stderr.flush()
        target, path = str(path), _target_as_path

    if not target.strip():
        _emit_error_json(
            INVALID_INPUT_CODE,
            "Class name must not be empty.",
            hint="Pass a class name or FQN. Example: sourcecode impact OrderService .",
            expected="A non-empty class name or FQN.",
        )
        raise typer.Exit(1)

    root = path.resolve()
    if not root.is_dir():
        _emit_error_json(
            INVALID_INPUT_CODE,
            f"'{root}' is not a valid directory.",
            path=str(root),
            hint=(
                "Pass an existing repository directory as the second argument. "
                "New syntax: sourcecode impact <target> [path] — "
                "target is the class name, path is the repo root."
            ),
            expected="A directory path.",
        )
        raise typer.Exit(1)

    file_list = find_java_files(root)
    if not include_tests:
        file_list = [f for f in file_list if "/test/" not in f and "/tests/" not in f]

    if not file_list:
        _nf_output = _serialize_dict(
            {
                "target": target,
                "resolution": "not_found",
                "message": "No Java files found in repository.",
                "risk_level": "unknown",
            },
            format,
        )
        _emit_command_output(_nf_output, output_path, copy)
        return

    _prog = Progress()
    _prog.start(f"building IR ({len(file_list)} files) for impact analysis")
    try:
        ir = build_repo_ir(file_list, root)
    finally:
        _prog.finish()

    result = compute_blast_radius(ir, target, max_depth=depth)
    result = _trim(result, BUDGET_IMPACT, label="impact")

    output = _serialize_dict(result, format)
    _emit_command_output(output, output_path, copy,
                         success_msg=f"Impact analysis written to {output_path}")

    if result.get("resolution") == "not_found":
        raise typer.Exit(code=1)

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
    path_prefix: Optional[str] = typer.Option(
        None, "--path-prefix", "-p",
        help="Filter endpoints whose URL path starts with this prefix. Example: /v1/liquidacion",
    ),
    controller: Optional[str] = typer.Option(
        None, "--controller",
        help="Filter endpoints from this controller class (substring match). Example: LiquidacionJornada",
    ),
    limit: Optional[int] = typer.Option(
        None, "--limit", "-n",
        help="Maximum number of endpoints to return.",
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
      sourcecode endpoints . --path-prefix /v1/liquidacion
      sourcecode endpoints . --controller LiquidacionJornada
      sourcecode endpoints . --limit 10
    """
    if format not in ("json", "yaml"):
        _emit_error_json(
            INVALID_INPUT_CODE,
            f"Invalid format '{format}'.",
            hint="format must be: json or yaml.",
            expected="json | yaml",
        )
        raise typer.Exit(code=1)

    target = path.resolve()
    if not target.exists() or not target.is_dir():
        _emit_error_json(
            INVALID_INPUT_CODE,
            f"'{target}' is not a valid directory.",
            path=str(target),
            hint="Pass an existing repository directory.",
            expected="A directory path.",
        )
        raise typer.Exit(code=1)

    data = _extract_java_endpoints(target)

    # Update RIS api_surface section (non-fatal side-effect).
    try:
        from sourcecode.ris import update_ris_api_surface as _ris_ep
        _ris_ep(target, data)
    except Exception:
        pass

    # Apply filters before serialization.
    _total_before = data.get("total", len(data.get("endpoints", [])))
    endpoints_list = data.get("endpoints", [])
    if path_prefix:
        endpoints_list = [e for e in endpoints_list if e.get("path", "").startswith(path_prefix)]
    if controller:
        _ctrl_lower = controller.lower()
        endpoints_list = [e for e in endpoints_list if _ctrl_lower in e.get("controller", "").lower()]
    if limit is not None and limit > 0:
        endpoints_list = endpoints_list[:limit]
    if path_prefix or controller or limit is not None:
        data["endpoints"] = endpoints_list
        data["total"] = len(endpoints_list)
        data["_filter"] = {
            "path_prefix": path_prefix,
            "controller": controller,
            "limit": limit,
            "total_before_filter": _total_before,
        }

    output = _serialize_dict(data, format)

    _emit_command_output(output, output_path, copy,
                         success_msg=f"Endpoints written to {output_path} ({data['total']} endpoints)")

    from sourcecode.mcp_nudge import nudge_mcp_if_needed as _nudge
    _nudge()


# ── Spring Semantic Audit ─────────────────────────────────────────────────────


def _render_spring_audit_github_comment(result: "SpringAuditResult", min_severity: str = "low") -> str:  # type: ignore[name-defined]
    """Render SpringAuditResult as a GitHub PR comment in Markdown."""
    from sourcecode.spring_findings import SEVERITY_ORDER

    min_order = SEVERITY_ORDER.get(min_severity, 3)
    visible = [f for f in result.findings if SEVERITY_ORDER.get(f.severity, 3) <= min_order]

    sev = result.summary.get("by_severity", {})
    total = result.summary.get("total_findings", 0)
    blocking = sev.get("critical", 0) + sev.get("high", 0)

    _ICONS = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵"}
    _LABELS = {"critical": "CRITICAL", "high": "HIGH", "medium": "MEDIUM", "low": "LOW"}

    if total == 0:
        status_line = "✅ **Spring Audit — no findings**"
    elif blocking > 0:
        status_line = f"🔴 **Spring Audit — {total} finding{'s' if total != 1 else ''} ({blocking} blocking)**"
    else:
        status_line = f"🟡 **Spring Audit — {total} finding{'s' if total != 1 else ''} (0 blocking)**"

    lines: list[str] = [status_line, ""]

    if total > 0:
        severity_counts = []
        for sev_name in ("critical", "high", "medium", "low"):
            n = sev.get(sev_name, 0)
            if n:
                severity_counts.append(f"{_ICONS[sev_name]} {n} {sev_name}")
        lines.append("**Severity:** " + "  ·  ".join(severity_counts))
        lines.append("")

    if not visible:
        lines.append(f"_No findings at or above `{min_severity}` severity._")
        return "\n".join(lines)

    lines += [
        "| Sev | Pattern | File | Symbol | Title |",
        "|-----|---------|------|--------|-------|",
    ]
    for f in sorted(visible, key=lambda x: (SEVERITY_ORDER.get(x.severity, 3), x.source_file)):
        icon = _ICONS.get(f.severity, "")
        label = _LABELS.get(f.severity, f.severity.upper())
        short_file = f.source_file.split("/")[-1] if "/" in f.source_file else f.source_file
        short_sym = f.symbol.split(".")[-1] if "." in f.symbol else f.symbol
        title_escaped = f.title.replace("|", "\\|")
        lines.append(f"| {icon} {label} | `{f.pattern_id}` | `{short_file}` | `{short_sym}` | {title_escaped} |")

    lines.append("")

    if visible:
        lines.append("<details>")
        lines.append("<summary>Finding details</summary>")
        lines.append("")
        for f in sorted(visible, key=lambda x: (SEVERITY_ORDER.get(x.severity, 3), x.source_file)):
            icon = _ICONS.get(f.severity, "")
            lines.append(f"### {icon} `{f.pattern_id}` — {f.title}")
            lines.append(f"**File:** `{f.source_file}`  **Symbol:** `{f.symbol}`")
            lines.append("")
            lines.append(f.explanation)
            lines.append("")
            lines.append(f"**Fix:** {f.fix_hint}")
            lines.append("")
        lines.append("</details>")

    lines += [
        "",
        f"_Generated by [sourcecode](https://github.com/sourcecode-ai/sourcecode) · "
        f"scope: {result.scope} · min-severity: {min_severity}_",
    ]
    return "\n".join(lines)


@app.command("spring-audit")
def spring_audit_cmd(
    path: Path = typer.Argument(
        Path("."),
        help="Repository path to audit (default: current directory)",
    ),
    output_path: Optional[Path] = typer.Option(
        None, "--output", "-o",
        help="Write output to a file instead of stdout.",
    ),
    format: str = typer.Option(
        "json",
        "--format",
        "-f",
        help="Output format: json (default), yaml, or github-comment.",
        show_default=True,
    ),
    copy: bool = typer.Option(
        False,
        "--copy",
        "-c",
        help="Copy output to system clipboard after a successful run.",
    ),
    scope: str = typer.Option(
        "all",
        "--scope",
        "-s",
        help="Audit scope: all (default), tx, or security.",
        show_default=True,
    ),
    min_severity: str = typer.Option(
        "low",
        "--min-severity",
        help="Minimum severity to include: critical, high, medium, or low (default).",
        show_default=True,
    ),
    ci: bool = typer.Option(
        False,
        "--ci/--no-ci",
        help="Exit with code 1 if any findings at or above --min-severity are found. For CI/CD gates.",
    ),
) -> None:
    """Spring semantic audit: TX anomalies (TX-001..005) + security surface (SEC-001..003).

    \b
    Detects:
      TX-001  @Transactional on private/final method (CGLIB proxy bypass)
      TX-002  REQUIRES_NEW nested in REQUIRED call chain
      TX-003  readOnly=true boundary propagating to write operation
      TX-004  NOT_SUPPORTED/NEVER within active TX chain
      TX-005  Exception swallowing inside @Transactional
      SEC-001 Unsecured endpoint in annotation_based security model
      SEC-002 CVE-2025-41248: @PreAuthorize on inherited method from generic supertype
      SEC-003 @Transactional on @Controller/@RestController (TX in wrong layer)

    \b
    CI/CD usage:
      sourcecode spring-audit . --ci                             # exit 1 on any finding
      sourcecode spring-audit . --ci --min-severity high         # exit 1 only on high/critical
      sourcecode spring-audit . --ci --format github-comment     # Markdown PR comment + exit 1

    \b
    Examples:
      sourcecode spring-audit .
      sourcecode spring-audit /path/to/repo
      sourcecode spring-audit . --scope security
      sourcecode spring-audit . --min-severity high
      sourcecode spring-audit . --output audit.json
    """
    import json as _json

    from sourcecode.repository_ir import find_java_files
    from sourcecode.canonical_ir import build_canonical_ir
    from sourcecode.spring_findings import SpringAuditResult, SpringFinding
    from sourcecode.spring_tx_analyzer import run_tx_audit
    from sourcecode.spring_security_audit import run_security_audit
    from sourcecode.spring_model import SpringSemanticModel

    target = path.resolve()
    if not target.exists() or not target.is_dir():
        _emit_error_json(
            INVALID_INPUT_CODE,
            f"'{target}' is not a valid directory.",
            path=str(target),
            hint="Pass an existing repository directory.",
            expected="A directory path.",
        )
        raise typer.Exit(code=1)

    if scope not in ("all", "tx", "security"):
        _emit_error_json(
            INVALID_INPUT_CODE,
            f"Invalid scope '{scope}'.",
            hint="scope must be one of: all, tx, security.",
            expected="all | tx | security",
        )
        raise typer.Exit(code=1)

    if min_severity not in ("critical", "high", "medium", "low"):
        _emit_error_json(
            INVALID_INPUT_CODE,
            f"Invalid min-severity '{min_severity}'.",
            hint="min-severity must be one of: critical, high, medium, low.",
            expected="critical | high | medium | low",
        )
        raise typer.Exit(code=1)

    if format not in ("json", "yaml", "github-comment"):
        _emit_error_json(
            INVALID_INPUT_CODE,
            f"Invalid format '{format}'.",
            hint="format must be one of: json, yaml, github-comment.",
            expected="json | yaml | github-comment",
        )
        raise typer.Exit(code=1)

    _file_limitations: list[str] = []
    file_list = find_java_files(target, limitations=_file_limitations)
    if not file_list:
        empty_result = SpringAuditResult(
            spring_detected=False,
            scope=scope,
            limitations=["No Java files found in repository — Spring audit requires Java source."],
            metadata={"java_files_found": 0},
        ).finalize()
        if format == "github-comment":
            output = _render_spring_audit_github_comment(empty_result, min_severity)
        else:
            output = _serialize_dict(empty_result.to_dict(), format)
        _emit_command_output(output, output_path, False,
                             success_msg=f"Spring audit written to {output_path}")
        return

    cir = build_canonical_ir(file_list, target)
    _model = SpringSemanticModel.build(cir)

    results: list[SpringAuditResult] = []
    if scope in ("all", "tx"):
        results.append(run_tx_audit(cir, root=target, min_severity=min_severity, model=_model))
    if scope in ("all", "security"):
        results.append(run_security_audit(cir, root=target, min_severity=min_severity, model=_model))

    if len(results) == 1:
        combined = results[0]
    else:
        all_findings: list[SpringFinding] = []
        all_limitations: list[str] = []
        merged_meta: dict = {}
        for r in results:
            all_findings.extend(r.findings)
            all_limitations.extend(r.limitations)
            merged_meta.update(r.metadata)
        combined = SpringAuditResult(
            repo_id=results[0].repo_id,
            spring_detected=any(r.spring_detected for r in results),
            scope="all",
            findings=all_findings,
            limitations=all_limitations,
            metadata=merged_meta,
        ).finalize()

    if _file_limitations:
        combined.limitations.extend(_file_limitations)

    # Populate git_head from repo HEAD — non-fatal.
    try:
        import subprocess as _sub_sa
        _sha_r = _sub_sa.run(
            ["git", "-C", str(target), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=3,
        )
        if _sha_r.returncode == 0:
            combined.git_head = _sha_r.stdout.strip()
    except Exception:
        pass

    data = combined.to_dict()

    # Non-fatal RIS side-effect — persist summary only (not full findings).
    try:
        from sourcecode.ris import update_ris_spring_audit as _ris_sa
        _ris_sa(target, data)
    except Exception:
        pass

    if format == "github-comment":
        output = _render_spring_audit_github_comment(combined, min_severity)
    else:
        output = _serialize_dict(data, format)

    _total = combined.summary.get("total_findings", 0)
    _emit_command_output(output, output_path, copy,
                         success_msg=f"Spring audit written to {output_path} ({_total} findings)")

    if ci and combined.findings:
        raise typer.Exit(code=1)


# ── Spring Boot Migration Check ───────────────────────────────────────────────


@app.command("migrate-check")
def migrate_check_cmd(
    path: Path = typer.Argument(
        Path("."),
        help="Repository path to scan (default: current directory)",
    ),
    output_path: Optional[Path] = typer.Option(
        None, "--output", "-o",
        help="Write output to a file instead of stdout.",
    ),
    format: str = typer.Option(
        "json",
        "--format",
        "-f",
        help="Output format: json (default) or text.",
        show_default=True,
    ),
    copy: bool = typer.Option(
        False,
        "--copy",
        "-c",
        help="Copy output to system clipboard after a successful run.",
    ),
    min_severity: str = typer.Option(
        "low",
        "--min-severity",
        help="Minimum severity to include: critical, high, medium, or low (default).",
        show_default=True,
    ),
) -> None:
    """Spring Boot 2→3 migration readiness: detect javax→jakarta namespace blockers.

    \b
    Detects:
      MIG-001  javax.persistence import (CRITICAL — JPA will not compile)
      MIG-002  javax.servlet import (HIGH — Servlet API changed)
      MIG-003  javax.validation import (HIGH — Bean Validation changed)
      MIG-004  javax.transaction import (HIGH — TX API changed)
      MIG-005  extends WebSecurityConfigurerAdapter (HIGH — removed in Spring 6)
      MIG-006  javax.annotation import (MEDIUM)
      MIG-007  javax.inject import (MEDIUM)
      MIG-008  javax.ws.rs import (MEDIUM — JAX-RS changed)

    \b
    Examples:
      sourcecode migrate-check .
      sourcecode migrate-check /path/to/repo --format text
      sourcecode migrate-check . --min-severity high
      sourcecode migrate-check . --output migration.json
    """
    from sourcecode.repository_ir import find_java_files
    from sourcecode.migrate_check import run_migrate_check

    target = path.resolve()
    if not target.exists() or not target.is_dir():
        _emit_error_json(
            INVALID_INPUT_CODE,
            f"'{target}' is not a valid directory.",
            path=str(target),
            hint="Pass an existing repository directory.",
            expected="A directory path.",
        )
        raise typer.Exit(code=1)

    if format not in ("json", "text"):
        _emit_error_json(
            INVALID_INPUT_CODE,
            f"Invalid format '{format}'.",
            hint="format must be one of: json, text.",
            expected="json | text",
        )
        raise typer.Exit(code=1)

    if min_severity not in ("critical", "high", "medium", "low"):
        _emit_error_json(
            INVALID_INPUT_CODE,
            f"Invalid min-severity '{min_severity}'.",
            hint="min-severity must be one of: critical, high, medium, low.",
            expected="critical | high | medium | low",
        )
        raise typer.Exit(code=1)

    _file_limitations: list[str] = []
    file_list = find_java_files(target, limitations=_file_limitations)
    report = run_migrate_check(file_list, target, min_severity=min_severity)
    if _file_limitations:
        report.limitations.extend(_file_limitations)

    if format == "text":
        output = report.to_text(min_severity=min_severity)
    else:
        output = _serialize_dict(report.to_dict(), "json")

    _total = report.summary.get("total_findings", 0)
    _emit_command_output(
        output, output_path, copy,
        success_msg=(
            f"Migration check written to {output_path} "
            f"(score: {report.readiness_score}/100, {_total} findings)"
        ),
    )


# ── Spring Impact Chain ───────────────────────────────────────────────────────


@app.command("impact-chain")
def impact_chain_cmd(
    symbol: str = typer.Argument(
        ...,
        help=(
            "Symbol to query: FQN, class name, or Class#method. "
            "Examples: OrderService, com.example.OrderService#placeOrder"
        ),
    ),
    path: Path = typer.Argument(
        Path("."),
        help="Repository root (default: current directory)",
    ),
    depth: int = typer.Option(
        4,
        "--depth",
        help="Indirect caller BFS depth (1–8, default: 4).",
        min=1,
        max=8,
    ),
    output_path: Optional[Path] = typer.Option(
        None, "--output", "-o",
        help="Write output to a file instead of stdout.",
    ),
    format: str = typer.Option(
        "json", "--format", "-f",
        help="Output format: json (default) or yaml.",
        show_default=True,
    ),
    copy: bool = typer.Option(
        False, "--copy", "-c",
        help="Copy output to clipboard after a successful run.",
    ),
    query_type: str = typer.Option(
        "impact", "--type", "-t",
        help="Query type: impact (default) or events.",
        show_default=True,
    ),
) -> None:
    """Spring impact-chain: systemic blast radius of a symbol with TX/SEC enrichment.

    \b
    Given a symbol (class or method), returns:
      - direct_callers      — symbols that directly call the target
      - indirect_callers    — transitive callers (BFS up to --depth hops)
      - endpoints_affected  — HTTP endpoints reachable through the call chain
      - transaction_boundary — @Transactional semantics on the target (if any)
      - security_surfaces   — per-endpoint security policy + SEC findings
      - impact_findings     — TX/SEC audit findings touching the call chain
      - risk_level          — critical | high | medium | low

    \b
    With --type events, returns event topology:
      - publishers          — FQNs that publish the event class
      - consumers           — listeners with TX phase metadata
      - event_graph         — publisher → event → consumer edges (BFS ≤ 2)
      - transaction_context — AFTER_COMMIT consumers, BEFORE_COMMIT risks
      - risk_level          — high | medium | low

    \b
    Consumes SpringSemanticModel — zero duplicate CIR traversals.
    JAVA/SPRING ONLY.

    \b
    Examples:
      sourcecode impact-chain OrderService .
      sourcecode impact-chain com.example.OrderService#placeOrder /path/to/repo
      sourcecode impact-chain PaymentService . --depth 6 --output impact.json
    """
    import json as _json

    from sourcecode.repository_ir import find_java_files
    from sourcecode.canonical_ir import build_canonical_ir
    from sourcecode.spring_model import SpringSemanticModel
    from sourcecode.spring_impact import run_impact_chain
    from sourcecode.spring_findings import SpringAuditResult

    if not symbol.strip():
        _emit_error_json(
            INVALID_INPUT_CODE,
            "Symbol name must not be empty.",
            hint="Pass a class name or FQN. Example: sourcecode impact-chain OrderService .",
            expected="A non-empty class name or FQN.",
        )
        raise typer.Exit(code=1)

    _VALID_TYPES = ("impact", "events")
    if query_type not in _VALID_TYPES:
        _emit_error_json(
            INVALID_INPUT_CODE,
            f"Invalid --type '{query_type}'. Valid values: {', '.join(_VALID_TYPES)}",
            flag="--type",
            value=query_type,
            valid_values=list(_VALID_TYPES),
            hint="Use --type impact (default) or --type events.",
            expected="impact | events",
        )
        raise typer.Exit(code=1)

    target = path.resolve()
    if not target.exists() or not target.is_dir():
        _emit_error_json(
            INVALID_INPUT_CODE,
            f"'{target}' is not a valid directory.",
            path=str(target),
            hint="Pass an existing repository directory.",
            expected="A directory path.",
        )
        raise typer.Exit(code=1)

    if format not in ("json", "yaml"):
        _emit_error_json(
            INVALID_INPUT_CODE,
            f"Invalid format '{format}'.",
            hint="format must be: json or yaml.",
            expected="json | yaml",
        )
        raise typer.Exit(code=1)

    file_list = find_java_files(target)
    if not file_list:
        data: dict = {
            "schema_version": "1.0",
            "symbol": symbol,
            "resolution": "not_found",
            "analysis_warnings": ["No Java files found in repository — Spring analysis requires Java source."],
            "risk_level": "unknown",
            "confidence": "low",
            "metadata": {},
        }
        output = _serialize_dict(data, format)
        _emit_command_output(output, output_path, False,
                             success_msg=f"Impact chain written to {output_path}")
        return

    cir = build_canonical_ir(file_list, target)
    _model = SpringSemanticModel.build(cir)

    if query_type == "events":
        from sourcecode.spring_event_topology import run_event_topology
        evt_result = run_event_topology(cir, symbol, model=_model)
        data = evt_result.to_dict()
        output = _serialize_dict(data, format)
        _emit_command_output(
            output, output_path, copy,
            success_msg=(
                f"Event topology written to {output_path} "
                f"(risk: {evt_result.risk_level}, "
                f"{evt_result.metadata.get('publisher_count', 0)} publishers, "
                f"{evt_result.metadata.get('consumer_count', 0)} consumers)"
            ),
        )
        return

    result = run_impact_chain(cir, symbol, depth=depth, root=target, model=_model)

    data = result.to_dict()
    output = _serialize_dict(data, format)
    _emit_command_output(
        output, output_path, copy,
        success_msg=(
            f"Impact chain written to {output_path} "
            f"(risk: {result.risk_level}, "
            f"{len(result.direct_callers)} direct callers, "
            f"{len(result.endpoints_affected)} endpoints)"
        ),
    )

    if result.resolution == "not_found":
        raise typer.Exit(code=1)


# ── PR Impact Report ──────────────────────────────────────────────────────────

@app.command("pr-impact")
def pr_impact_cmd(
    path: Path = typer.Argument(
        Path("."),
        help="Repository root (default: current directory)",
    ),
    files: Path = typer.Option(
        ...,
        "--files",
        help="File containing the list of changed Java files, one path per line.",
    ),
    output_path: Optional[Path] = typer.Option(
        None, "--output", "-o",
        help="Write output to a file instead of stdout.",
    ),
    format: str = typer.Option(
        "text", "--format", "-f",
        help="Output format: text (default) or json.",
        show_default=True,
    ),
    copy: bool = typer.Option(
        False, "--copy", "-c",
        help="Copy output to clipboard after a successful run.",
    ),
) -> None:
    """PR blast-radius report: what can break if this PR is merged?

    \b
    Reads a list of changed Java files and produces a consolidated report:
      - Modified classes found in the changed files
      - Affected REST endpoints reachable through the call chain
      - Direct callers of each modified class
      - Event publishers and consumers triggered by the change
      - @Transactional methods in the changed classes
      - Consolidated risk level (CRITICAL / HIGH / MEDIUM / LOW)

    \b
    Reuses existing graph and impact analysis — no new parsers.
    JAVA/SPRING ONLY.

    \b
    Examples:
      sourcecode pr-impact --files changed_files.txt
      sourcecode pr-impact /path/to/repo --files diff.txt --format json
      sourcecode pr-impact --files changes.txt --output pr_report.txt
    """
    import json as _json

    from sourcecode.repository_ir import find_java_files
    from sourcecode.canonical_ir import build_canonical_ir
    from sourcecode.spring_model import SpringSemanticModel
    from sourcecode.pr_impact import run_pr_impact

    target = path.resolve()
    if not target.exists() or not target.is_dir():
        _emit_error_json(
            INVALID_INPUT_CODE,
            f"'{target}' is not a valid directory.",
            path=str(target),
            hint="Pass an existing repository directory.",
            expected="A directory path.",
        )
        raise typer.Exit(code=1)

    if not files.exists() or files.is_dir():
        _emit_error_json(
            INVALID_INPUT_CODE,
            f"--files '{files}' does not exist or is a directory. Expected a text file listing changed file paths (one per line).",
            path=str(files),
            hint=(
                "Create a file with one changed Java file path per line, then pass it with --files. "
                "Example: git diff --name-only HEAD~1 > changed.txt && sourcecode pr-impact . --files changed.txt"
            ),
            expected="A text file containing one Java file path per line.",
        )
        raise typer.Exit(code=1)

    if format not in ("text", "json"):
        _emit_error_json(
            INVALID_INPUT_CODE,
            f"Invalid format '{format}'.",
            hint="format must be: text or json.",
            expected="text | json",
        )
        raise typer.Exit(code=1)

    # Read changed-files list
    changed_files = [
        line.strip()
        for line in files.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not changed_files:
        _emit_error_json(
            INVALID_INPUT_CODE,
            f"--files '{files}' is empty.",
            hint="File must contain at least one Java file path.",
            expected="One Java file path per line.",
        )
        raise typer.Exit(code=1)

    file_list = find_java_files(target)
    if not file_list:
        data: dict = {
            "schema_version": "1.0",
            "modified_classes": [],
            "risk_level": "UNKNOWN",
            "risk_reason": "No Java files found in repository — Spring analysis requires Java source.",
            "analysis_warnings": ["No Java files found."],
            "metadata": {"changed_files_count": len(changed_files)},
        }
        output = _serialize_dict(data, "json") if format == "json" else (
            "No Java files found in repository — Spring analysis requires Java source."
        )
        _emit_command_output(output, output_path, False,
                             success_msg=f"PR impact report written to {output_path}")
        return

    cir = build_canonical_ir(file_list, target)
    model = SpringSemanticModel.build(cir)
    report = run_pr_impact(cir, changed_files, root=target, model=model)

    output = _serialize_dict(report.to_dict(), "json") if format == "json" else report.render_text()
    _emit_command_output(
        output, output_path, copy,
        success_msg=(
            f"PR impact report written to {output_path} "
            f"(risk: {report.risk_level}, "
            f"{len(report.modified_classes)} classes, "
            f"{len(report.affected_endpoints)} endpoints)"
        ),
    )


# ── Explain Command ───────────────────────────────────────────────────────────

@app.command("explain")
def explain_cmd(
    class_name: str = typer.Argument(
        ...,
        help="Simple class name to explain (e.g. UserService, OrderController).",
    ),
    path: Path = typer.Argument(
        Path("."),
        help="Repository root (default: current directory)",
    ),
    format: str = typer.Option(
        "text", "--format", "-f",
        help="Output format: text (default) or json.",
        show_default=True,
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
    """Human-readable architectural summary for a class.

    \b
    Generates a structured explanation derived entirely from static analysis:
      - Purpose and Spring stereotype
      - Public methods
      - Incoming callers (who uses this class)
      - Outgoing dependencies (what this class calls)
      - Events published and consumed
      - @Transactional boundaries
      - Security constraints (@PreAuthorize, @Secured, etc.)
      - REST endpoints related

    \b
    JAVA/SPRING ONLY. Reads from existing CIR — no new parsers.

    \b
    Examples:
      sourcecode explain UserService
      sourcecode explain OrderController /path/to/repo
      sourcecode explain UserService --format json
    """
    import json as _json

    from sourcecode.repository_ir import find_java_files
    from sourcecode.canonical_ir import build_canonical_ir
    from sourcecode.spring_model import SpringSemanticModel
    from sourcecode.explain import explain_class

    if not class_name.strip():
        _emit_error_json(
            INVALID_INPUT_CODE,
            "Class name must not be empty.",
            hint="Pass a class name. Example: sourcecode explain UserService .",
            expected="A non-empty class name.",
        )
        raise typer.Exit(code=1)

    target = path.resolve()
    if not target.exists() or not target.is_dir():
        _emit_error_json(
            INVALID_INPUT_CODE,
            f"'{target}' is not a valid directory.",
            path=str(target),
            hint="Pass an existing repository directory.",
            expected="A directory path.",
        )
        raise typer.Exit(code=1)

    if format not in ("text", "json"):
        _emit_error_json(
            INVALID_INPUT_CODE,
            f"Invalid format '{format}'.",
            hint="format must be: text or json.",
            expected="text | json",
        )
        raise typer.Exit(code=1)

    file_list = find_java_files(target)
    if not file_list:
        _emit_error_json(
            INVALID_INPUT_CODE,
            f"No Java files found in '{target}'.",
            hint="sourcecode explain requires a Java/Spring repository.",
            expected="A directory containing .java source files.",
        )
        raise typer.Exit(code=1)

    cir = build_canonical_ir(file_list, target)
    model = SpringSemanticModel.build(cir)
    explanation = explain_class(class_name, cir, model)

    if format == "json":
        output = _json.dumps(explanation.to_dict(), indent=2, ensure_ascii=False)
    else:
        output = explanation.render_text()

    _emit_command_output(output, output_path, copy,
                         success_msg=f"Explanation written to {output_path}")

    if not explanation.found:
        raise typer.Exit(code=1)


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
    # Detect misuse: `fix-bug "symptom text" /path` — path arg looks like a symptom.
    _path_str = str(path)
    _path_looks_like_symptom = (
        not Path(_path_str).exists()
        and (" " in _path_str or any(c.isupper() for c in _path_str))
    )
    if _path_looks_like_symptom and not symptom:
        _emit_error_json(
            INVALID_INPUT_CODE,
            f"'{_path_str}' is not a valid directory. Did you mean to use --symptom?",
            hint=f"Use: sourcecode fix-bug . --symptom {_path_str!r}",
            expected="A repository directory path as first argument.",
        )
        raise typer.Exit(code=1)

    if not symptom:
        # Only emit advisory to interactive terminals — non-TTY (MCP, pipes, scripts)
        # must never receive informational text mixed into JSON stdout.
        if getattr(sys.stderr, "isatty", lambda: False)():
            typer.echo(
                "[fix-bug] Results are significantly better with --symptom. "
                "Example: --symptom 'NullPointerException in PaymentService'",
                err=True,
            )
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
    from sourcecode.repository_ir import build_repo_ir, find_java_files, apply_ir_size_limits
    from sourcecode.output_budget import trim_to_budget, BUDGET_ONBOARD
    from sourcecode.license import is_pro as _mod_is_pro, is_large_repo as _mod_large

    root = path.resolve()
    if not root.is_dir():
        _emit_error_json(
            INVALID_INPUT_CODE,
            f"'{root}' is not a valid directory.",
            path=str(root),
            hint="Pass an existing repository directory.",
            expected="A directory path.",
        )
        raise typer.Exit(1)

    file_list = find_java_files(root)
    if not file_list:
        _emit_error_json(
            INVALID_INPUT_CODE,
            "No Java files found in repository.",
            path=str(root),
            hint="Pass a repository containing Java source files.",
            expected="At least one Java file.",
        )
        raise typer.Exit(1)

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

    if (not _mod_is_pro) and _mod_large(str(root)):
        # Large monolith, free tier: structural discovery preview only — no dead
        # zones, tangles, or full refactor list. Small/mid repos get full analysis.
        result = {
            "workflow": "modernize",
            "path": str(root),
            "tier": "free",
            "tier_note": (
                "This repository exceeds the free-tier size limit. "
                "Upgrade to Pro for full analysis on enterprise-scale monoliths: "
                "dead zones, dependency tangles, refactor candidates ranked by git "
                "churn, and complete coupling graphs."
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
        _safe_write_file(output_path, output)
        typer.echo(f"Modernization analysis written to {output_path}", err=True)
    else:
        try:
            sys.stdout.buffer.write(output.encode("utf-8"))
            sys.stdout.buffer.write(b"\n")
            sys.stdout.buffer.flush()
        except AttributeError:
            sys.stdout.write(output + "\n")

    if copy:
        _copy_to_clipboard(output)


# ── rename-class ──────────────────────────────────────────────────────────────

@app.command("rename-class")
def rename_class_cmd(
    path: Path = typer.Argument(
        Path("."),
        help="Repository root to operate on (default: current directory)",
    ),
    old_name: str = typer.Option(
        ..., "--from", "-f",
        help="Current class name (PascalCase, e.g. ServiceA)",
    ),
    new_name: str = typer.Option(
        ..., "--to", "-t",
        help="New class name (PascalCase, e.g. ServiceB)",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Compute changes but do not write any files or rename on disk.",
    ),
    no_tests: bool = typer.Option(
        False, "--no-tests",
        help="Exclude test files from the rename (src/main only).",
    ),
    output_path: Optional[Path] = typer.Option(
        None, "--output", "-o",
        help="Write change audit JSON to a file instead of stdout.",
    ),
    copy: bool = typer.Option(
        False, "--copy", "-c",
        help="Copy output to clipboard after a successful run.",
    ),
    format: str = typer.Option(
        "json", "--format",
        help="Output format: json (default) or yaml.",
    ),
) -> None:
    """Rename a Java class throughout the repository.

    \b
    Renames a Java class safely:
      - Updates class/interface/enum declaration
      - Updates constructor name
      - Updates all import statements
      - Updates all type references (fields, params, return types)
      - Updates extends / implements
      - Updates generics, casts, Spring @Qualifier names
      - Renames the physical .java file
      - Emits a structured change audit trail

    \b
    Examples:
      sourcecode rename-class . --from ServiceA --to ServiceB
      sourcecode rename-class /path/to/repo --from OrderManager --to OrderService
      sourcecode rename-class . --from OldName --to NewName --dry-run
      sourcecode rename-class . --from OldName --to NewName --output rename-audit.json
    """
    import json as _json
    from sourcecode.rename_refactor import rename_class

    root = path.resolve()
    if not root.is_dir():
        _emit_error_json(
            INVALID_INPUT_CODE,
            f"'{root}' is not a valid directory.",
            path=str(root),
            hint="Pass an existing repository directory.",
            expected="A directory path.",
        )
        raise typer.Exit(1)

    result = rename_class(
        root,
        old_name,
        new_name,
        dry_run=dry_run,
        include_tests=not no_tests,
    )

    if result.errors:
        _emit_error_json(
            "RENAME_ERROR",
            result.errors[0],
            errors=result.errors,
            old_name=old_name,
            new_name=new_name,
        )
        raise typer.Exit(1)

    result_dict = result.to_dict()

    output = _serialize_dict(result_dict, format)
    _action = "dry-run simulated" if dry_run else "applied"
    _emit_command_output(
        output, output_path, copy,
        success_msg=(
            f"[rename-class] {_action}: {old_name} → {new_name} "
            f"({result.files_modified} file(s) changed). "
            f"Audit written to {output_path}"
        ),
    )

    if not dry_run and not output_path:
        typer.echo(
            f"[rename-class] Renamed: {old_name} → {new_name} "
            f"({result.files_modified} file(s) updated, file renamed to {result.new_file})",
            err=True,
        )


# ── chunk-file ────────────────────────────────────────────────────────────────

@app.command("chunk-file")
def chunk_file_cmd(
    file: Path = typer.Argument(
        ...,
        help="Java file to chunk (absolute or relative path)",
    ),
    max_lines: int = typer.Option(
        500, "--max-lines", "-n",
        help="Target max lines per chunk (default: 500). Methods > max_lines emit size_warning.",
    ),
    chunk_id: Optional[int] = typer.Option(
        None, "--chunk", "-c",
        help="Return only this chunk by ID (1-based). Omit to return all chunks.",
    ),
    metadata_only: bool = typer.Option(
        False, "--metadata-only",
        help="Return chunk boundaries and metadata without file content.",
    ),
    output_path: Optional[Path] = typer.Option(
        None, "--output", "-o",
        help="Write output to a file instead of stdout.",
    ),
    format: str = typer.Option(
        "json", "--format",
        help="Output format: json (default) or yaml.",
    ),
    copy: bool = typer.Option(
        False, "--copy",
        help="Copy output to clipboard after a successful run.",
    ),
) -> None:
    """Split a large Java file into semantic chunks for AI agent consumption.

    \b
    Splits a Java file at method/class boundaries so AI agents can read
    large files (10K–25K+ lines) in context-sized pieces without timeout
    or fragmented analysis.

    Each chunk includes:
      - chunk_id, start_line, end_line, chunk_type, symbol name
      - context_header: package + class + imports summary
      - content: source lines for that chunk
      - size_warning: True if chunk > max_lines (cannot split mid-method)

    \b
    Examples:
      sourcecode chunk-file NominasCalculoService.java
      sourcecode chunk-file BigService.java --max-lines 300
      sourcecode chunk-file BigService.java --chunk 5        # read chunk 5 only
      sourcecode chunk-file BigService.java --metadata-only  # sizes/boundaries only
    """
    import json as _json
    from sourcecode.file_chunker import chunk_java_file

    abs_file = file.resolve()
    if not abs_file.is_file():
        _emit_error_json(
            INVALID_INPUT_CODE,
            f"'{abs_file}' is not a valid file.",
            path=str(abs_file),
            hint="Pass an existing Java source file.",
            expected="A .java file path.",
        )
        raise typer.Exit(1)

    if abs_file.suffix != ".java":
        _emit_error_json(
            INVALID_INPUT_CODE,
            f"'{abs_file.name}' is not a Java file. chunk-file only supports .java files.",
            path=str(abs_file),
            hint="Pass a .java source file.",
            expected="A .java file path.",
        )
        raise typer.Exit(1)

    result = chunk_java_file(abs_file, max_lines=max_lines, include_content=not metadata_only)

    if chunk_id is not None:
        # Return single chunk
        matching = [c for c in result.chunks if c.chunk_id == chunk_id]
        if not matching:
            _emit_error_json(
                INVALID_INPUT_CODE,
                f"Chunk {chunk_id} not found. File has {result.total_chunks} chunks.",
                chunk_id=chunk_id,
                total_chunks=result.total_chunks,
            )
            raise typer.Exit(1)
        result_dict = matching[0].to_dict()
    else:
        result_dict = result.to_dict()

    output = _serialize_dict(result_dict, format)
    _emit_command_output(
        output, output_path, copy,
        success_msg=f"[chunk-file] {result.total_chunks} chunks written to {output_path}",
    )


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


# ---------------------------------------------------------------------------
# Auth commands (device-flow login / status / logout)
# ---------------------------------------------------------------------------

@auth_app.command("login")
def auth_login_cmd() -> None:
    """Authenticate via browser (device code flow).

    \b
    The CLI shows a URL. Open it in your browser, log in with your account,
    and the CLI completes authentication automatically.
    Credentials are stored in ~/.sourcecode/license.json (30-min cache; Supabase is source of truth).

    \b
    Examples:
      sourcecode auth login
    """
    from sourcecode.license import auth_login as _auth_login
    _auth_login()


@auth_app.command("status")
def auth_status_cmd() -> None:
    """Show current authentication and plan status."""
    import json as _json
    try:
        from sourcecode.license import _license_data as _ld, is_pro as _ip
    except Exception:
        _ld = None
        _ip = False

    if not _ld:
        out: dict = {"status": "unauthenticated", "pro": False}
        sys.stdout.write(_json.dumps(out, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        return

    out = {
        "status": "authenticated",
        "auth_method": _ld.get("auth_method", "license_key"),
        "email": _ld.get("email", ""),
        "plan": _ld.get("plan", "unknown"),
        "plan_status": _ld.get("status", "unknown"),
        "pro": _ip,
        "validated_at": _ld.get("validated_at") or _ld.get("activated_at") or "",
    }
    sys.stdout.write(_json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    sys.stdout.flush()


@auth_app.command("logout")
def auth_logout_cmd() -> None:
    """Remove local credentials (does not cancel your subscription)."""
    import json as _json
    _lf = Path.home() / ".sourcecode" / "license.json"
    if _lf.exists():
        try:
            _lf.unlink()
            out: dict = {"status": "logged_out", "message": "Local credentials removed."}
        except Exception as _exc:
            out = {"status": "error", "message": str(_exc)}
    else:
        out = {"status": "logged_out", "message": "No local credentials found."}
    sys.stdout.write(_json.dumps(out, ensure_ascii=False) + "\n")
    sys.stdout.flush()


@app.command("version")
def version_cmd() -> None:
    """Show version and exit.

    Outputs human-readable text on interactive terminals.
    Outputs structured JSON on non-TTY (MCP, pipes, scripts):
      {"cli_version": "1.33.11", "mcp_schema_version": "1.33.11",
       "compatibility_schema_version": "1.0"}
    """
    if getattr(sys.stdout, "isatty", lambda: False)():
        typer.echo(f"sourcecode {__version__}")
    else:
        import json as _json_ver
        typer.echo(_json_ver.dumps({
            "cli_version": __version__,
            "mcp_schema_version": __version__,
            "compatibility_schema_version": "1.0",
        }, ensure_ascii=False))


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


# ── cold-start (RIS bootstrap for external MCP and agents) ───────────────────

@app.command("cold-start")
def cold_start_cmd(
    path: Path = typer.Argument(Path("."), help="Repository path (default: current directory)"),
    compact: bool = typer.Option(
        False,
        "--compact",
        help="Emit a compact subset (~10K tokens): status, git_head, stacks, entry_points, and key_dependencies only.",
    ),
    output_path: Optional[Path] = typer.Option(
        None,
        "--output",
        "-o",
        help="Write output to file instead of stdout.",
    ),
) -> None:
    """Output Repository Intelligence Snapshot bootstrap context as JSON.

    Returns instantly from persisted RIS — zero re-analysis cost.
    status: cold_start_ready | cold_start_stale | no_ris

    \b
    Note: Full output is large (~100K–200K tokens for medium repos).
    Use --compact for a ~10K token subset safe for direct LLM injection.
    Use --output FILE to save the full snapshot for local search tools.
    """
    import json as _json
    from sourcecode.ris import get_cold_start_context as _gcs
    target = Path(path).resolve()
    result = _gcs(target)
    if compact:
        # P1-C: cap at ~10K tokens — keep only fields essential for orientation.
        # BUG-6 fix: use actual RIS key names (summary/entrypoints, not stacks/entry_points)
        _cs_keys = {"status", "git_head", "summary", "entrypoints", "endpoints",
                    "project_type", "validation", "_meta"}
        result = {k: v for k, v in result.items() if k in _cs_keys}
        # Truncate endpoints to first 30 to stay within ~10K token budget
        if isinstance(result.get("endpoints"), list):
            result["endpoints"] = result["endpoints"][:30]
        result["_meta"] = {**(result.get("_meta") or {}), "compact_mode": True,
                           "full_available": "sourcecode cold-start (without --compact)"}
    _out = _json.dumps(result, indent=2, ensure_ascii=False)
    _size = len(_out.encode("utf-8"))
    _tokens = _size // 4
    _out_with_meta = _json.loads(_out)
    _out_with_meta.setdefault("_meta", {})["estimated_tokens"] = _tokens
    _out = _json.dumps(_out_with_meta, indent=2, ensure_ascii=False)
    if not compact and _size > 400_000:
        sys.stderr.write(
            f"WARNING: Output is ~{_tokens // 1000}K tokens. This exceeds the context window of "
            "most LLMs (GPT-4o: 128K, Claude Sonnet: 200K). "
            "Use --compact for a ~10K token subset, or --output FILE to save.\n"
        )
        sys.stderr.flush()
    if output_path:
        _safe_write_file(output_path, _out)
        sys.stderr.write(f"Saved {len(_out.encode('utf-8'))} bytes to {output_path}\n")
        sys.stderr.flush()
    else:
        typer.echo(_out)


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

    logging.basicConfig(
        stream=sys.stderr,
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
        _stdin_buf = getattr(sys.stdin, "buffer", None)
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
        log.critical("sourcecode-mcp fatal error: %s: %s", type(exc).__name__, exc)
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
    from sourcecode import __version__ as _cli_version
    from sourcecode.mcp.onboarding.detector import detect_clients, is_client_running
    from sourcecode.mcp.onboarding import applier

    sep = "─" * 46

    typer.echo("MCP Status")
    typer.echo(sep)

    # FIX-P0-5/P0-6: Show CLI version explicitly so drift is immediately visible.
    typer.echo(f"CLI version   {_cli_version}   ({sys.executable})")
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

    # Build config state map for cross-check in Stage 3.
    _configured_clients: set[str] = set()
    for _c in clients:
        if _c.app_installed:
            _cfg = applier.read_config(_c.config_path)
            if applier.is_installed(_cfg):
                _configured_clients.add(_c.slug)

    # Stage 3: Process liveness — is the client app currently running?
    # This is independent from config: a running app may still need restart to pick up config.
    typer.echo("Runtime (client app process running?)")
    any_installed = any(c.app_installed for c in clients)
    _action_required: list[str] = []
    if not any_installed:
        typer.echo("  (no client apps found — nothing to check)")
    else:
        for client in clients:
            if not client.app_installed:
                continue
            if is_client_running(client):
                typer.echo(f"  {client.name:<20} ✓ running")
                if client.slug not in _configured_clients:
                    _action_required.append(client.name)
            else:
                typer.echo(f"  {client.name:<20} ✗ not running")
                typer.echo(f"    Fix: open {client.name}, then run sourcecode mcp status")

    typer.echo(sep)
    if _action_required:
        for _name in _action_required:
            typer.echo(f"  ⚠ ACTION REQUIRED: {_name} is running but sourcecode is not configured.")
        typer.echo("    Run: sourcecode mcp init")
        typer.echo("")
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


@mcp_app.command("list-tools")
def mcp_list_tools(
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """List all MCP tools exposed by the sourcecode server.

    \b
    Shows each tool name, its description, and the CLI command it maps to.
    Useful for discovering capabilities when using sourcecode as an MCP server.

    \b
    Examples:
      sourcecode mcp list-tools
      sourcecode mcp list-tools --json
    """
    import asyncio
    import json as _json

    from sourcecode.mcp.server import mcp as _mcp

    tools = asyncio.run(_mcp.list_tools())
    tools_sorted = sorted(tools, key=lambda t: t.name)

    if json_output:
        payload = [
            {"name": t.name, "description": (t.description or "").strip()}
            for t in tools_sorted
        ]
        typer.echo(_json.dumps(payload, indent=2, ensure_ascii=False))
        return

    typer.echo(f"sourcecode MCP tools ({len(tools_sorted)} available)\n")
    for t in tools_sorted:
        desc_first_line = (t.description or "").strip().splitlines()[0] if t.description else ""
        typer.echo(f"  {t.name:<35} {desc_first_line}")
    typer.echo("")
    typer.echo("Use:  sourcecode mcp serve   — start MCP server on stdio")
    typer.echo("Use:  sourcecode mcp init    — configure MCP client")


# ── Cache subcommands ─────────────────────────────────────────────────────────


def _resolve_repo_root(path: Path) -> Path:
    """Resolve *path* to a repo root by walking up to find a .git directory.

    If *path* is already a git root (has .git), returns it directly.
    If *path* is a subdirectory of a git repo, returns the git root.
    Falls back to *path* itself if no git repo found.
    """
    candidate = path.resolve()
    while True:
        if (candidate / ".git").exists():
            return candidate
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    return path.resolve()


@cache_app.command("status")
def cache_status_cmd(
    path: Path = typer.Argument(Path("."), help="Repository path (default: current directory)"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Show cache statistics for a repository."""
    from sourcecode import cache as _cm
    target = _resolve_repo_root(Path(path))
    stats = _cm.status(target)
    if json_output:
        import json as _j
        typer.echo(_j.dumps(stats, indent=2, ensure_ascii=False))
    else:
        typer.echo(f"Cache dir:   {stats['cache_dir']}")
        typer.echo(f"Cores:       {stats['cores']}")
        typer.echo(f"Views:       {stats['views']}")
        typer.echo(f"CAS blobs:   {stats['cas_blobs']}")
        typer.echo(f"Total size:  {stats['total_size_mb']} MB")
        # RIS section
        if stats.get("ris_exists"):
            _stale_tag = " [STALE]" if stats.get("ris_is_stale") else ""
            typer.echo(f"RIS:         exists  HEAD={stats.get('ris_git_head', '?')}{_stale_tag}  updated={stats.get('ris_last_updated_at', '?')}")
        else:
            typer.echo("RIS:         none  (run analysis to build)")
        if stats.get("current_git_head"):
            typer.echo(f"Current HEAD:{stats['current_git_head']}")


@cache_app.command("clear")
def cache_clear_cmd(
    path: Path = typer.Argument(Path("."), help="Repository path (default: current directory)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt."),
    include_ris: bool = typer.Option(False, "--include-ris", hidden=True, help="Alias for --all. Preserved for backward compatibility."),
    all_: bool = typer.Option(False, "--all", help="Also delete the RIS snapshot (ris.json.gz). By default, RIS is preserved across clears."),
) -> None:
    """Delete cached snapshots for a repository.

    By default, RIS (ris.json.gz) is preserved — it is the persistent structural
    index used for cold-start bootstrapping.  Use --all to also clear it.
    """
    from sourcecode import cache as _cm
    target = _resolve_repo_root(Path(path))
    _clear_ris = include_ris or all_
    if not yes:
        _ris_note = " (including RIS)" if _clear_ris else " (RIS preserved — use --all to also clear it)"
        import click as _click
        _click.confirm(f"Delete all cache files for {target}{_ris_note}?", abort=True, err=True)
    removed = _cm.clear(target, clear_ris=_clear_ris)
    typer.echo(f"Removed {removed} file(s).", err=True)


@cache_app.command("warm")
def cache_warm_cmd(
    path: Path = typer.Argument(Path("."), help="Repository path to warm (default: current directory)"),
    compact: bool = typer.Option(True, "--compact/--no-compact", help="Warm compact view (default: on)."),
    agent: bool = typer.Option(False, "--agent", help="Also warm agent view."),
) -> None:
    """Pre-populate the cache by running a fresh analysis.

    Runs a full analysis to populate L1/L2 caches and rebuild the RIS
    (Repository Intelligence Snapshot). Useful after a merge/pull in CI.
    """
    import shutil as _shutil
    import subprocess as _sub
    target = _resolve_repo_root(Path(path))
    typer.echo(f"Warming cache for {target} …", err=True)
    _sc_bin = _shutil.which("sourcecode") or sys.argv[0]
    cmd = [_sc_bin, str(target)]
    if compact:
        cmd.append("--compact")
    if agent:
        cmd.append("--agent")
    result = _sub.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        typer.echo("Cache warmed (L1/L2 + RIS rebuilt).", err=True)
    else:
        typer.echo(f"Warm failed (exit {result.returncode}).", err=True)
        if result.stderr:
            typer.echo(result.stderr.strip(), err=True)
        raise typer.Exit(code=result.returncode)


@cache_app.command("freshness")
def cache_freshness_cmd(
    path: Path = typer.Argument(Path("."), help="Repository path (default: current directory)"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Report RIS freshness relative to the current git HEAD.

    Answers: is the cached snapshot current? How many commits behind is it?

    \b
    Output fields:
      fresh              — True when RIS HEAD matches current HEAD and no uncommitted changes
      current_git_head   — Current repo HEAD (short SHA)
      ris_git_head       — HEAD stored in RIS when it was last built
      delta_commits      — Number of commits between ris_git_head and HEAD (0 = in sync)
      has_uncommitted_changes — Working tree has staged/unstaged changes
      ris_exists         — False when no RIS has been built yet
      ris_last_updated_at — ISO-8601 timestamp of last RIS write
    """
    import json as _json
    import subprocess as _sub
    from sourcecode import cache as _cm
    from sourcecode.ris import _has_uncommitted_changes as _huc
    from sourcecode.ris import load_ris as _lris

    target = Path(path).resolve()
    current_head = _cm._get_git_head(target)
    ris = _lris(target)

    if ris is None:
        result: dict = {
            "fresh": False,
            "ris_exists": False,
            "current_git_head": current_head,
            "ris_git_head": None,
            "delta_commits": None,
            "has_uncommitted_changes": _huc(target),
            "ris_last_updated_at": None,
        }
    else:
        ris_head = ris.git_head
        head_matches = bool(current_head and ris_head and current_head == ris_head)
        uncommitted = _huc(target)

        # Count commits between ris_head and current HEAD
        delta = None
        if ris_head and current_head and ris_head != current_head:
            try:
                _r = _sub.run(
                    ["git", "-C", str(target), "rev-list", "--count", f"{ris_head}..HEAD"],
                    capture_output=True, text=True, timeout=5,
                )
                if _r.returncode == 0:
                    delta = int(_r.stdout.strip())
            except Exception:
                pass
        elif head_matches:
            delta = 0

        result = {
            "fresh": head_matches and not uncommitted,
            "ris_exists": True,
            "current_git_head": current_head,
            "ris_git_head": ris_head,
            "delta_commits": delta,
            "has_uncommitted_changes": uncommitted,
            "ris_last_updated_at": ris.last_updated_at,
        }

    if json_output:
        typer.echo(_json.dumps(result, indent=2, ensure_ascii=False))
    else:
        _fresh_tag = "FRESH" if result["fresh"] else "STALE"
        typer.echo(f"Status:       {_fresh_tag}")
        typer.echo(f"Current HEAD: {result['current_git_head'] or '(unknown)'}")
        typer.echo(f"RIS HEAD:     {result.get('ris_git_head') or '(none)'}")
        if result.get("delta_commits") is not None:
            typer.echo(f"Delta:        {result['delta_commits']} commit(s) behind")
        typer.echo(f"Uncommitted:  {result['has_uncommitted_changes']}")
        typer.echo(f"RIS updated:  {result.get('ris_last_updated_at') or 'never'}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main_entry() -> None:
    """CLI entry point.

    Calls _preprocess_argv() before Typer/Click parses sys.argv so that
    repository path tokens are extracted before Click's Group callback
    can consume them as positional arguments (which would prevent subcommand
    routing for tokens like 'version' or 'config').
    """
    # Force UTF-8 on stdout so Unicode characters (arrows, etc.) survive on
    # Windows where the default console codec is cp1252 (BUG-1).
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    _preprocess_argv()
    app()

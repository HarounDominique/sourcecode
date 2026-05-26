"""MCP server for sourcecode CLI.

Exposes sourcecode capabilities as MCP tools. Each tool maps to a CLI command
and delegates execution to the in-process runner — no subprocess, no binary
lookup, same process as the CLI.

All tools return:
  {"success": bool, "data": dict | str | None, "error": {"code": str, "message": str} | None}
data is the parsed JSON object from the CLI output, not a shell string.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import re
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent

from sourcecode import __version__ as _sourcecode_version
from sourcecode.mcp.runner import run_command

# FIX-P0-5: MCP server version must match CLI version exactly.
# FastMCP does not accept version= in __init__; inject it on the underlying
# low-level Server so the MCP initialize handshake reports the correct version.
mcp = FastMCP("sourcecode")
if hasattr(mcp, "_mcp_server"):
    mcp._mcp_server.version = _sourcecode_version  # type: ignore[attr-defined]


def _ok(data: Any) -> dict:
    return {"success": True, "data": data, "error": None}


def _err(message: str, code: str = "EXECUTION_FAILED") -> CallToolResult:
    """Return an MCP tool-error result with isError=True per MCP spec §tool-result."""
    payload = {"success": False, "data": None, "error": {"code": code, "message": message}}
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload))],
        isError=True,
    )


def _execute(args: list[str]) -> dict | CallToolResult:
    try:
        result = run_command(args)
    except RuntimeError as exc:
        return _err(str(exc))
    # If CLI output itself signals failure via success:false, propagate as isError=True
    if isinstance(result, dict) and result.get("success") is False:
        payload = {
            "success": False,
            "data": None,
            "error": result.get("error") or {
                "code": "EXECUTION_FAILED",
                "message": "Command returned success=false",
            },
        }
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(payload))],
            isError=True,
        )
    return _ok(result)


_DEFAULT_TESTS_TIMEOUT_MS = 15_000

# Regex for MINGW paths: /c/some/path → C:/some/path
_MINGW_PATH_RE = re.compile(r"^/([a-zA-Z])(/.*)?$")


def _normalize_repo_path(path: str) -> str:
    """Normalize repo_path for cross-platform compatibility (P0-2).

    Handles two Windows-specific formats:
    - MINGW/Git-Bash: /c/Users/... → C:/Users/...
    - Backslash:       C:\\Users\\... → C:/Users/...
    Forward-slash paths (C:/Users/... or /unix/path) pass through unchanged.
    """
    m = _MINGW_PATH_RE.match(path)
    if m:
        drive = m.group(1).upper()
        rest = m.group(2) or "/"
        path = f"{drive}:{rest}"
    path = path.replace("\\", "/")
    return path


@mcp.tool()
def get_compact_context(repo_path: str = ".", git_context: bool = False) -> dict:
    """Compact human/LLM summary of a repository (~1000-3000 tokens). USE THIS FIRST.

    Best for: quick project orientation, first-time context, token-budget constrained tasks.
    Returns: stacks, entry points, dependency summary, architecture summary, confidence, gaps.
    Includes security_surface, mybatis, and transactional_boundaries for Java/Spring projects.
    For richer machine-oriented detail (deeper signals, more sections), use get_agent_context.

    Maps to: sourcecode <repo_path> --compact [--git-context]
    repo_path: absolute path to the repository (default: current working directory).
    git_context: include git log and branch context in the analysis.
    """
    _raw = repo_path
    try:
        if not isinstance(repo_path, str):
            return _err("repo_path must be a string", "INVALID_ARGUMENT")
        if not isinstance(git_context, bool):
            return _err("git_context must be boolean", "INVALID_ARGUMENT")
        repo_path = _normalize_repo_path(repo_path)
        args = [repo_path, "--compact"]
        if git_context:
            args.append("--git-context")
        return _execute(args)
    except Exception as exc:
        return _err(
            f"Internal error: {type(exc).__name__}: {exc} — repo_path recibido: {_raw}",
            "INTERNAL_ERROR",
        )


@mcp.tool()
def get_agent_context(repo_path: str = ".", git_context: bool = False) -> dict:
    """Full structured agent context with extended machine-oriented signals (~5000-15000 tokens).

    Best for: deep analysis, bug investigation, code review, or when get_compact_context
    lacks sufficient detail. Includes all compact fields plus: env_map, code_notes,
    architecture layers, security surface, transactional boundaries, module graph summary.
    Prefer get_compact_context for quick orientation or token-constrained workflows.

    Maps to: sourcecode <repo_path> --agent [--git-context]
    repo_path: absolute path to the repository (default: current working directory).
    git_context: include git log and branch context in the analysis.
    """
    _raw = repo_path
    try:
        if not isinstance(repo_path, str):
            return _err("repo_path must be a string", "INVALID_ARGUMENT")
        if not isinstance(git_context, bool):
            return _err("git_context must be boolean", "INVALID_ARGUMENT")
        repo_path = _normalize_repo_path(repo_path)
        args = [repo_path, "--agent"]
        if git_context:
            args.append("--git-context")
        return _execute(args)
    except Exception as exc:
        return _err(
            f"Internal error: {type(exc).__name__}: {exc} — repo_path recibido: {_raw}",
            "INTERNAL_ERROR",
        )


@mcp.tool()
def get_endpoints(repo_path: str = ".") -> dict:
    """REST API endpoint surface extraction from Java source files.

    Maps to: sourcecode endpoints <repo_path>
    Returns: endpoints list with method, path, controller, handler fields;
             security dict when authorization annotations are present
             (policy: roles_allowed|permit_all|deny_all|authenticated|...);
             total (int) and no_security_signal (int) counts.
             no_security_signal counts endpoints with no recognized auth annotation —
             repos using framework-level auth (e.g. Keycloak) may show high counts.
    Supports Spring MVC (@GetMapping etc.) and JAX-RS (@GET/@POST etc.).
    Security annotations detected: @RolesAllowed, @PermitAll, @DenyAll,
    @Authenticated, @PreAuthorize, @Secured, @SecurityRequirement, @M3FiltroSeguridad.
    repo_path: absolute path to the repository (default: current working directory).
    """
    _raw = repo_path
    try:
        if not isinstance(repo_path, str):
            return _err("repo_path must be a string", "INVALID_ARGUMENT")
        repo_path = _normalize_repo_path(repo_path)
        return _execute(["endpoints", repo_path])
    except Exception as exc:
        return _err(
            f"Internal error: {type(exc).__name__}: {exc} — repo_path recibido: {_raw}",
            "INTERNAL_ERROR",
        )


@mcp.tool()
def get_module_context(repo_path: str = ".", module: str = "") -> dict:
    """Compact analysis of a specific module or subdirectory within a repository.

    Maps to: sourcecode <repo_path>/<module> --compact
    repo_path: absolute path to the repository root.
    module: subdirectory name relative to repo_path (e.g. 'src/auth', 'api', 'core').
    """
    _raw = repo_path
    try:
        if not isinstance(repo_path, str):
            return _err("repo_path must be a string", "INVALID_ARGUMENT")
        if not isinstance(module, str) or not module.strip():
            return _err("module must be a non-empty string", "INVALID_ARGUMENT")
        repo_path = _normalize_repo_path(repo_path)
        module_path = repo_path.rstrip("/") + "/" + module.strip("/")
        return _execute([module_path, "--compact"])
    except Exception as exc:
        return _err(
            f"Internal error: {type(exc).__name__}: {exc} — repo_path recibido: {_raw}",
            "INTERNAL_ERROR",
        )


@mcp.tool()
def get_delta(repo_path: str = ".", since: str = "HEAD~1") -> dict:
    """Incremental context: git-changed files since a reference commit.

    Maps to: sourcecode prepare-context delta <repo_path> --since <since>
    repo_path: absolute path to the repository (default: current working directory).
    since: git ref to diff against (e.g. HEAD~3, main, origin/main).
    """
    _raw = repo_path
    try:
        if not isinstance(repo_path, str):
            return _err("repo_path must be a string", "INVALID_ARGUMENT")
        if not isinstance(since, str) or not since.strip():
            return _err("since must be a non-empty git ref", "INVALID_ARGUMENT")
        repo_path = _normalize_repo_path(repo_path)
        return _execute(["prepare-context", "delta", repo_path, "--since", since])
    except Exception as exc:
        return _err(
            f"Internal error: {type(exc).__name__}: {exc} — repo_path recibido: {_raw}",
            "INTERNAL_ERROR",
        )


@mcp.tool()
def get_ir_summary(repo_path: str = ".") -> dict:
    """Deterministic symbol-level IR summary for Java repositories. Java only.

    Maps to: sourcecode repo-ir <repo_path> --summary-only
    Returns: reverse_graph (top 10 hubs), route_surface (top 50 endpoints),
             subsystems (top 15), impact, analysis. Full graph nodes/edges omitted.

    Output is bounded to ~100 KB for LLM safety. For full IR (can exceed 10 MB
    on large repos), use the CLI: sourcecode repo-ir <path> --output ir.json
    Use get_compact_context or get_agent_context for non-Java repos.

    repo_path: absolute path to the Java repository (default: current working directory).
    """
    _raw = repo_path
    try:
        if not isinstance(repo_path, str):
            return _err("repo_path must be a string", "INVALID_ARGUMENT")
        repo_path = _normalize_repo_path(repo_path)
        return _execute(["repo-ir", repo_path, "--summary-only"])
    except Exception as exc:
        return _err(
            f"Internal error: {type(exc).__name__}: {exc} — repo_path recibido: {_raw}",
            "INTERNAL_ERROR",
        )


@mcp.tool()
def fix_bug_context(repo_path: str = ".", symptom: str = "") -> dict:
    """Risk-ranked files for bug investigation, optionally focused by symptom.

    Maps to: sourcecode prepare-context fix-bug <repo_path> [--symptom <symptom>]
    Includes compact_base: security_surface, transactional_boundaries, spring_profiles.
    repo_path: absolute path to the repository (default: current working directory).
    symptom: optional error message or class name to focus the file ranking
             (e.g. "NullPointerException in EstructuraRrHhRestController").
    """
    _raw = repo_path
    try:
        if not isinstance(repo_path, str):
            return _err("repo_path must be a string", "INVALID_ARGUMENT")
        repo_path = _normalize_repo_path(repo_path)
        args = ["prepare-context", "fix-bug", repo_path]
        if symptom and isinstance(symptom, str) and symptom.strip():
            args.extend(["--symptom", symptom.strip()])
        return _execute(args)
    except Exception as exc:
        return _err(
            f"Internal error: {type(exc).__name__}: {exc} — repo_path recibido: {_raw}",
            "INTERNAL_ERROR",
        )


@mcp.tool()
def review_pr_context(repo_path: str = ".", since: str = "") -> dict:
    """Execution paths and risk analysis for changed files in a pull request.

    Maps to: sourcecode prepare-context review-pr <repo_path> [--since <since>]
    Returns: compact_base + execution_paths (diff-scoped) + hotspots for changed files.
    repo_path: absolute path to the repository (default: current working directory).
    since: git ref to diff against (e.g. HEAD~3, main, origin/main).
           If omitted, diffs against uncommitted changes or HEAD~1 fallback.
    """
    _raw = repo_path
    try:
        if not isinstance(repo_path, str):
            return _err("repo_path must be a string", "INVALID_ARGUMENT")
        repo_path = _normalize_repo_path(repo_path)
        args = ["prepare-context", "review-pr", repo_path]
        if since and isinstance(since, str) and since.strip():
            args.extend(["--since", since.strip()])
        return _execute(args)
    except Exception as exc:
        return _err(
            f"Internal error: {type(exc).__name__}: {exc} — repo_path recibido: {_raw}",
            "INTERNAL_ERROR",
        )


@mcp.tool()
def onboard_context(repo_path: str = ".") -> dict:
    """Onboarding context: structured overview for new contributors.

    Maps to: sourcecode prepare-context onboard <repo_path>
    repo_path: absolute path to the repository (default: current working directory).
    """
    _raw = repo_path
    try:
        if not isinstance(repo_path, str):
            return _err("repo_path must be a string", "INVALID_ARGUMENT")
        repo_path = _normalize_repo_path(repo_path)
        return _execute(["prepare-context", "onboard", repo_path])
    except Exception as exc:
        return _err(
            f"Internal error: {type(exc).__name__}: {exc} — repo_path recibido: {_raw}",
            "INTERNAL_ERROR",
        )


@mcp.tool()
def explain_context(repo_path: str = ".") -> dict:
    """Architecture and entry-point explanation for a repository.

    Maps to: sourcecode prepare-context explain <repo_path>
    Returns: project summary, architecture, entry points, key dependencies.
    repo_path: absolute path to the repository (default: current working directory).
    """
    _raw = repo_path
    try:
        if not isinstance(repo_path, str):
            return _err("repo_path must be a string", "INVALID_ARGUMENT")
        repo_path = _normalize_repo_path(repo_path)
        return _execute(["prepare-context", "explain", repo_path])
    except Exception as exc:
        return _err(
            f"Internal error: {type(exc).__name__}: {exc} — repo_path recibido: {_raw}",
            "INTERNAL_ERROR",
        )


@mcp.tool()
def refactor_context(repo_path: str = ".") -> dict:
    """Structural issues and refactor opportunities for a repository.

    Maps to: sourcecode prepare-context refactor <repo_path>
    Returns: structural issues, coupling hotspots, improvement opportunities.
    repo_path: absolute path to the repository (default: current working directory).
    """
    _raw = repo_path
    try:
        if not isinstance(repo_path, str):
            return _err("repo_path must be a string", "INVALID_ARGUMENT")
        repo_path = _normalize_repo_path(repo_path)
        return _execute(["prepare-context", "refactor", repo_path])
    except Exception as exc:
        return _err(
            f"Internal error: {type(exc).__name__}: {exc} — repo_path recibido: {_raw}",
            "INTERNAL_ERROR",
        )


@mcp.tool()
def generate_tests_context(repo_path: str = ".", include_all: bool = False) -> dict:
    """Untested source files and test gap analysis for a repository.

    Maps to: sourcecode prepare-context generate-tests <repo_path> [--all]
    Returns: test_gaps list of untested files ranked by risk.
            On large repos (>2000 classes) analysis is bounded by SOURCECODE_TESTS_TIMEOUT_MS
            (default: 15000 ms). If timeout elapses, returns truncated=true with partial results.
    repo_path: absolute path to the repository (default: current working directory).
    include_all: return full test_gaps list without truncating to top 20.
    """
    _raw = repo_path
    try:
        if not isinstance(repo_path, str):
            return _err("repo_path must be a string", "INVALID_ARGUMENT")
        if not isinstance(include_all, bool):
            return _err("include_all must be boolean", "INVALID_ARGUMENT")
        repo_path = _normalize_repo_path(repo_path)
        args = ["prepare-context", "generate-tests", repo_path]
        if include_all:
            args.append("--all")

        # P0-3: timeout guard — large repos can stall the stdio transport indefinitely.
        timeout_ms = int(os.environ.get("SOURCECODE_TESTS_TIMEOUT_MS", str(_DEFAULT_TESTS_TIMEOUT_MS)))
        timeout_s = timeout_ms / 1000.0

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = executor.submit(_execute, args)
        done, _not_done = concurrent.futures.wait([future], timeout=timeout_s)
        if _not_done:
            executor.shutdown(wait=False)
            return _ok({
                "truncated": True,
                "truncated_reason": f"timeout_{timeout_ms // 1000}s" if timeout_ms >= 1000 else f"timeout_{timeout_ms}ms",
                "files_analyzed": 0,
                "results": [],
            })
        executor.shutdown(wait=False)
        return future.result()

    except Exception as exc:
        return _err(
            f"Internal error: {type(exc).__name__}: {exc} — repo_path recibido: {_raw}",
            "INTERNAL_ERROR",
        )


@mcp.tool()
def get_impact_context(repo_path: str = ".", target: str = "", depth: int = 4) -> dict:
    """Blast-radius analysis: who calls a class and what breaks if it changes? Java only.

    Maps to: sourcecode impact <target> <repo_path> [--depth <depth>]
    Returns: direct_callers, indirect_callers, endpoints_affected,
             transactional_boundaries_touched, risk_score, risk_level, stats.

    Use this when:
    - Planning a refactor: understand the full call chain before changing a class
    - PR review: assess blast radius of a changed service or utility class
    - Incident triage: find all paths that reach a faulty component

    target: class name (simple or FQN) or Java file path. Examples:
            "UserService", "org.example.UserService", "UserService.java"
    repo_path: absolute path to the Java repository (default: current working directory).
    depth: BFS depth for indirect caller traversal (1–8, default: 4).
    """
    _raw = repo_path
    try:
        if not isinstance(repo_path, str):
            return _err("repo_path must be a string", "INVALID_ARGUMENT")
        if not isinstance(target, str) or not target.strip():
            return _err("target must be a non-empty class name or FQN", "INVALID_ARGUMENT")
        if not isinstance(depth, int) or depth < 1 or depth > 8:
            return _err("depth must be an integer between 1 and 8", "INVALID_ARGUMENT")
        repo_path = _normalize_repo_path(repo_path)
        args = ["impact", target.strip(), repo_path, "--depth", str(depth)]
        return _execute(args)
    except Exception as exc:
        return _err(
            f"Internal error: {type(exc).__name__}: {exc} — repo_path recibido: {_raw}",
            "INTERNAL_ERROR",
        )


_TELEMETRY_ACTIONS = frozenset({"status", "enable", "disable"})


@mcp.tool()
def version() -> dict:
    """Print sourcecode CLI version.

    Maps to: sourcecode version
    """
    return _execute(["version"])


@mcp.tool()
def config() -> dict:
    """Show sourcecode CLI configuration.

    Maps to: sourcecode config
    """
    return _execute(["config"])


@mcp.tool()
def telemetry(action: str) -> dict:
    """Manage telemetry settings.

    Maps to: sourcecode telemetry <action>
    action: one of "status" (show current state), "enable" (opt in), "disable" (opt out).
    Valid values: "status" | "enable" | "disable"
    """
    # FIX-P2-10: enumerate valid actions in docstring so agents don't guess.
    if action not in _TELEMETRY_ACTIONS:
        return _err(
            f"Invalid action '{action}'. Must be one of: {', '.join(sorted(_TELEMETRY_ACTIONS))}",
            "INVALID_ARGUMENT",
        )
    return _execute(["telemetry", action])

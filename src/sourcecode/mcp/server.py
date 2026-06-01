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
from sourcecode.error_schema import (
    EXECUTION_FAILED_CODE,
    INTERNAL_ERROR_CODE,
    INVALID_INPUT_CODE,
    build_error_object,
)
from sourcecode.mcp.runner import CommandError, run_command

# FIX-P0-5: MCP server version must match CLI version exactly.
# FastMCP does not accept version= in __init__; inject it on the underlying
# low-level Server so the MCP initialize handshake reports the correct version.
mcp = FastMCP("sourcecode")
if hasattr(mcp, "_mcp_server"):
    mcp._mcp_server.version = _sourcecode_version  # type: ignore[attr-defined]


def _ok(data: Any) -> dict:
    return {"success": True, "data": data, "error": None}


def _err(
    message: str,
    code: str = EXECUTION_FAILED_CODE,
    *,
    hint: str | None = None,
    expected: str | None = None,
    **context: Any,
) -> CallToolResult:
    """Return an MCP tool-error result with isError=True per MCP spec §tool-result."""
    payload = {
        "success": False,
        "data": None,
        "error": build_error_object(code, message, hint=hint, expected=expected),
    }
    payload.update(context)
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload))],
        isError=True,
    )


def _coerce_cli_error(exc: Exception, default_message: str) -> CallToolResult:
    payload = getattr(exc, "payload", None)
    if isinstance(payload, dict):
        if "error" in payload and isinstance(payload["error"], dict):
            error = payload["error"]
            normalized = {
                "success": False,
                "data": None,
                "error": build_error_object(
                    str(error.get("code") or EXECUTION_FAILED_CODE),
                    str(error.get("message") or default_message),
                    hint=str(error.get("hint") or ""),
                    expected=str(error.get("expected") or ""),
                ),
            }
            for key, value in payload.items():
                if key != "error":
                    normalized[key] = value
            return CallToolResult(
                content=[TextContent(type="text", text=json.dumps(normalized))],
                isError=True,
            )
    message = str(exc) or default_message
    return _err(
        message,
        EXECUTION_FAILED_CODE,
        hint="Inspect the CLI stderr for the structured error payload.",
        expected="Successful CLI execution.",
    )


def _execute(args: list[str]) -> dict | CallToolResult:
    try:
        result = run_command(args)
    except CommandError as exc:
        return _coerce_cli_error(exc, f"Command failed: {' '.join(args)}")
    except RuntimeError as exc:
        return _err(
            str(exc),
            EXECUTION_FAILED_CODE,
            hint="Inspect the CLI stderr for the structured error payload.",
            expected="Successful CLI execution.",
        )
    # If CLI output itself signals failure via success:false, propagate as isError=True
    if isinstance(result, dict) and result.get("success") is False:
        cli_error = result.get("error")
        if isinstance(cli_error, dict):
            normalized_error = build_error_object(
                str(cli_error.get("code") or EXECUTION_FAILED_CODE),
                str(cli_error.get("message") or "Command returned success=false"),
                hint=str(cli_error.get("hint") or ""),
                expected=str(cli_error.get("expected") or ""),
            )
        else:
            normalized_error = build_error_object(
                EXECUTION_FAILED_CODE,
                "Command returned success=false",
            )
        payload = {
            "success": False,
            "data": None,
            "error": normalized_error,
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


def _check_repo_path(path: str) -> "CallToolResult | None":
    """H-05: Validate repo_path exists and is a directory before executing.

    Returns a structured CallToolResult(isError=True) when the path is invalid,
    or None when the path is valid. Must be called after _normalize_repo_path().
    Early validation prevents the MCP server from hanging when the CLI exits
    non-zero with an empty stdout (error went to stderr, not captured by runner).
    """
    if not os.path.exists(path):
        return _err(
            f"'{path}' does not exist.",
            INVALID_INPUT_CODE,
            hint="Pass an existing repository directory.",
            expected="An existing directory path.",
            path=path,
        )
    if not os.path.isdir(path):
        return _err(
            f"'{path}' is not a directory.",
            INVALID_INPUT_CODE,
            hint="Pass a repository directory, not a file.",
            expected="A directory path.",
            path=path,
        )
    return None


@mcp.tool()
def start_session(repo_path: str = ".", task_description: str = "") -> dict:
    """PRIMARY ENTRY POINT — call this first on every new MCP session.

    Single entry point that replaces manual tool selection. Determines session state,
    checks RIS freshness, detects repo type, and returns a ready-to-execute tool
    sequence. Agent never has to guess which tool to call next.

    With task_description: detects intent (pr_review, bug_investigation,
    feature_implementation, refactor, test_generation) and returns the exact
    flow runner to call with pre-filled args. Zero sequencing decisions for agent.

    Without task_description: returns session state + recommended_next_action
    based on RIS freshness and repo type (java_spring, java, etc.).

    Returns:
      session_state        — INIT | CONTEXT_LOADED | STALE_CONTEXT |
                             INCOMPLETE_CONTEXT | TASK_INTENT_DETECTED
      repo_type            — java_spring | java | nodejs | python | unknown
      cache_freshness      — fresh | stale | missing
      recommended_next_action — {tool, reason, args} — call this next, no guessing
      required_tools_sequence — ordered list of tools the agent should call
      risk_level           — low | medium | high | unknown
      entrypoint_candidates — top entry points from RIS
      endpoints_count      — Java endpoint count from RIS api_surface
      session_meta         — {ttfca_ms, tools_suggested, agent_decision_reduction,
                              orchestration_rules_applied}
      intent               — detected intent when task_description provided
      intent_confidence    — detection confidence (1.0 = explicit match)
      ris_summary          — lightweight RIS snapshot when context loaded
      missing_data_hint    — what to build next when critical data absent
      bootstrap_hint       — how to build RIS when missing

    Orchestration rules applied automatically:
      R1: stale cache → prepend get_delta to any tool sequence
      R2: Java + no endpoint index → prepend get_endpoints
      R3: repo > 1000 classes → RIS path flagged as preferred

    KPIs tracked in session_meta:
      ttfca_ms                — time_to_first_correct_action in milliseconds
      tools_suggested         — agent decision reduction (1-3 vs 18 free tools)
      orchestration_rules_applied — which rules fired

    repo_path: absolute path to the repository (default: current working directory).
    task_description: optional natural language task description for intent detection.
      Examples: "review the PR on the auth module",
                "NullPointerException in UserService.findById",
                "implement a new endpoint for password reset"
    """
    _raw = repo_path
    try:
        if not isinstance(repo_path, str):
            return _err("repo_path must be a string", "INVALID_ARGUMENT")
        repo_path = _normalize_repo_path(repo_path)
        _path_err = _check_repo_path(repo_path)
        if _path_err is not None:
            return _path_err
        from sourcecode.mcp.orchestrator import start_session_impl
        return _ok(start_session_impl(repo_path, task_description or ""))
    except Exception as exc:
        return _err(
            f"Internal error: {type(exc).__name__}: {exc} — repo_path: {_raw}",
            "INTERNAL_ERROR",
        )


@mcp.tool()
def analyze_task(repo_path: str = ".", task_description: str = "") -> dict:
    """Detect task intent and return targeted tool sequence. No tool guessing needed.

    Given a natural language description of what you need to do, returns:
      - Detected intent (pr_review, bug_investigation, feature_implementation, etc.)
      - Ordered tool sequence to execute
      - Recommended flow runner to call with pre-filled args
      - Extracted parameters (symptom for bugs, etc.)
      - Orchestration rules that were applied

    Prefer start_session(task_description=...) for combined bootstrap + intent detection.
    Use analyze_task standalone when session is already loaded and you have a new task.

    Quality warnings included when:
      - Bug intent detected but no error class/message found in description
        (ranking will be generic, not focused)

    repo_path: absolute path to the repository (default: current working directory).
    task_description: natural language task description (required for meaningful output).
      Examples: "fix the NPE in PaymentService",
                "review PR changes in the billing module",
                "add new REST endpoint for user preferences"
    """
    _raw = repo_path
    try:
        if not isinstance(repo_path, str):
            return _err("repo_path must be a string", "INVALID_ARGUMENT")
        if not isinstance(task_description, str) or not task_description.strip():
            return _err("task_description must be a non-empty string", "INVALID_ARGUMENT")
        repo_path = _normalize_repo_path(repo_path)
        _path_err = _check_repo_path(repo_path)
        if _path_err is not None:
            return _path_err
        from sourcecode.mcp.orchestrator import analyze_task_impl
        return _ok(analyze_task_impl(repo_path, task_description))
    except Exception as exc:
        return _err(
            f"Internal error: {type(exc).__name__}: {exc} — repo_path: {_raw}",
            "INTERNAL_ERROR",
        )


@mcp.tool()
def run_pr_review_flow(repo_path: str = ".", since: str = "") -> dict:
    """PR Review Flow — auto-chains 3 tools: delta → execution paths → blast radius.

    Auto-executes the complete PR review pipeline in one call:
      1. get_delta(since) — changed files and context since merge base
      2. review_pr_context — execution paths and hotspots for changed files
      3. get_impact_context — blast radius for up to 3 changed Java classes

    Auto-detects merge base with origin/main or origin/master when since is omitted.
    Falls back to HEAD~1 when no remote branch found.

    Returns consolidated_output with all three results merged.
    session_meta.ttfca_ms shows total wall-clock time.
    quality_warnings list any partial failures (steps still return partial output).

    Use this instead of calling get_delta + review_pr_context + get_impact_context
    separately — agent makes zero sequencing decisions.

    JAVA ONLY for blast-radius step. Delta and execution paths work on all repos.

    repo_path: absolute path to the repository (default: current working directory).
    since: git ref to diff against. Auto-detected from origin/main if omitted.
    """
    _raw = repo_path
    try:
        if not isinstance(repo_path, str):
            return _err("repo_path must be a string", "INVALID_ARGUMENT")
        repo_path = _normalize_repo_path(repo_path)
        _path_err = _check_repo_path(repo_path)
        if _path_err is not None:
            return _path_err
        from sourcecode.mcp.orchestrator import run_pr_review_flow_impl
        return _ok(run_pr_review_flow_impl(repo_path, since or ""))
    except Exception as exc:
        return _err(
            f"Internal error: {type(exc).__name__}: {exc} — repo_path: {_raw}",
            "INTERNAL_ERROR",
        )


@mcp.tool()
def run_bug_investigation_flow(repo_path: str = ".", symptom: str = "") -> dict:
    """Bug Investigation Flow — auto-chains 3 tools: risk-rank → impact → IR context.

    Auto-executes the complete bug investigation pipeline in one call:
      1. fix_bug_context(symptom) — files ranked by risk, focused on symptom
      2. get_impact_context — blast radius of the top suspect class
      3. get_ir_summary — Java IR dependency context (Java repos only)

    symptom should be: error message, exception class name, or affected class.
    Without symptom: ranking is generic (not focused). quality_warnings will note this.

    Returns consolidated_output with all results plus suspect_class (auto-extracted
    from symptom or top ranked file). session_meta.ttfca_ms shows total wall time.

    Use this instead of calling fix_bug_context + get_impact_context + get_ir_summary
    separately — agent makes zero sequencing decisions.

    repo_path: absolute path to the repository (default: current working directory).
    symptom: error message, exception class, or class name.
      Examples: "NullPointerException in UserService.findById",
                "PaymentController", "AuthenticationException"
    """
    _raw = repo_path
    try:
        if not isinstance(repo_path, str):
            return _err("repo_path must be a string", "INVALID_ARGUMENT")
        repo_path = _normalize_repo_path(repo_path)
        _path_err = _check_repo_path(repo_path)
        if _path_err is not None:
            return _path_err
        from sourcecode.mcp.orchestrator import run_bug_investigation_flow_impl
        return _ok(run_bug_investigation_flow_impl(repo_path, symptom or ""))
    except Exception as exc:
        return _err(
            f"Internal error: {type(exc).__name__}: {exc} — repo_path: {_raw}",
            "INTERNAL_ERROR",
        )


@mcp.tool()
def run_feature_flow(repo_path: str = ".", feature_description: str = "") -> dict:
    """Feature Implementation Flow — auto-chains 4 tools: context → API → delta → structure.

    Auto-executes the complete feature planning pipeline in one call:
      1. get_compact_context (or RIS fast-path if fresh) — project orientation
      2. get_endpoints — existing API surface (Java repos only)
      3. get_delta(HEAD~3) — what changed recently (active development areas)
      4. refactor_context — structural coupling and hotspot awareness

    Returns consolidated_output with all four results. Use feature_description to
    help the agent understand what you are building (stored in output for traceability).

    Use this instead of calling the four tools separately — agent makes zero
    sequencing decisions. Typical wall time: 2-10s depending on repo size and cache.

    repo_path: absolute path to the repository (default: current working directory).
    feature_description: optional description of the feature to implement.
      Example: "add password reset flow with email verification"
    """
    _raw = repo_path
    try:
        if not isinstance(repo_path, str):
            return _err("repo_path must be a string", "INVALID_ARGUMENT")
        repo_path = _normalize_repo_path(repo_path)
        _path_err = _check_repo_path(repo_path)
        if _path_err is not None:
            return _path_err
        from sourcecode.mcp.orchestrator import run_feature_flow_impl
        return _ok(run_feature_flow_impl(repo_path, feature_description or ""))
    except Exception as exc:
        return _err(
            f"Internal error: {type(exc).__name__}: {exc} — repo_path: {_raw}",
            "INTERNAL_ERROR",
        )


@mcp.tool()
def get_cold_start_context(repo_path: str = ".") -> dict:
    """Instant session bootstrap from persisted Repository Intelligence Snapshot (RIS).

    PREFER start_session over this tool — it provides orchestration guidance on top
    of the same RIS data. Use get_cold_start_context when you only need the raw
    RIS bootstrap object without tool sequencing recommendations.

    Returns cached structural context built from prior analysis runs — zero re-analysis cost.

    status values:
      "cold_start_ready"  — RIS exists and matches the current git HEAD.
      "cold_start_stale"  — RIS exists but HEAD has changed since last analysis.
                            Data is still useful; run get_compact_context to refresh.
      "no_ris"            — No RIS yet for this repo; run get_compact_context first.

    Returns: status, repo_id, git_head, stale (bool), last_updated_at,
             summary (compact snapshot), entrypoints, endpoints, hotspots.

    repo_path: absolute path to the repository (default: current working directory).
    """
    _raw = repo_path
    try:
        if not isinstance(repo_path, str):
            return _err("repo_path must be a string", "INVALID_ARGUMENT")
        repo_path = _normalize_repo_path(repo_path)
        _path_err = _check_repo_path(repo_path)
        if _path_err is not None:
            return _path_err
        from pathlib import Path as _Path
        from sourcecode.ris import get_cold_start_context as _gcs
        return _ok(_gcs(_Path(repo_path)))
    except Exception as exc:
        return _err(
            f"Internal error: {type(exc).__name__}: {exc} — repo_path: {_raw}",
            "INTERNAL_ERROR",
        )


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
        _path_err = _check_repo_path(repo_path)
        if _path_err is not None:
            return _path_err
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
        _path_err = _check_repo_path(repo_path)
        if _path_err is not None:
            return _path_err
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
    """REST API endpoint surface extraction from Java source files. JAVA ONLY.

    Do NOT call this on non-Java repositories — it will return empty results.
    Use get_compact_context or get_agent_context for non-Java repos.

    Maps to: sourcecode endpoints <repo_path>
    Returns: endpoints list with method, path, controller, handler fields;
             security dict always present (policy: roles_allowed|permit_all|deny_all|
             authenticated|...|none_detected); none_detected = no auth annotation found.
             total (int), no_security_signal (int), and security_model (str) fields.
             no_security_signal counts endpoints where security.policy == "none_detected".
             security_model values: "filter_based" (centralized Spring Security config —
             high no_security_signal is expected and does NOT mean endpoints are unprotected),
             "annotation_based" (per-endpoint annotations only), "mixed" (both),
             "unknown" (no security signals detected).
    Supports Spring MVC (@GetMapping etc.) and JAX-RS (@GET/@POST etc.).
    Security annotations detected: @RolesAllowed, @PermitAll, @DenyAll,
    @Authenticated, @PreAuthorize, @Secured, @SecurityRequirement, @M3FiltroSeguridad.
    repo_path: absolute path to the Java repository (default: current working directory).
    """
    _raw = repo_path
    try:
        if not isinstance(repo_path, str):
            return _err("repo_path must be a string", "INVALID_ARGUMENT")
        repo_path = _normalize_repo_path(repo_path)
        _path_err = _check_repo_path(repo_path)
        if _path_err is not None:
            return _path_err
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
        _path_err = _check_repo_path(repo_path)
        if _path_err is not None:
            return _path_err
        module_path = repo_path.rstrip("/") + "/" + module.strip("/")
        return _execute([module_path, "--compact"])
    except Exception as exc:
        return _err(
            f"Internal error: {type(exc).__name__}: {exc} — repo_path recibido: {_raw}",
            "INTERNAL_ERROR",
        )


def _auto_since(repo_path: str) -> str:
    """Detect best merge-base for delta: origin/main > origin/master > HEAD~1."""
    import subprocess as _sp
    for base in ("origin/main", "origin/master"):
        try:
            r = _sp.run(
                ["git", "-C", repo_path, "merge-base", "HEAD", base],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        except Exception:
            pass
    return "HEAD~1"


@mcp.tool()
def get_delta(repo_path: str = ".", since: str = "") -> dict:
    """Incremental context: git-changed files since a reference commit.

    Maps to: sourcecode prepare-context delta <repo_path> --since <since>
    repo_path: absolute path to the repository (default: current working directory).
    since: git ref to diff against (e.g. HEAD~3, main, origin/main).
           If empty or omitted, auto-detects merge-base with origin/main (or
           origin/master). Falls back to HEAD~1 if no remote branch found.
           Pass "HEAD~1" explicitly to force single-commit diff.
    """
    _raw = repo_path
    try:
        if not isinstance(repo_path, str):
            return _err("repo_path must be a string", "INVALID_ARGUMENT")
        repo_path = _normalize_repo_path(repo_path)
        _path_err = _check_repo_path(repo_path)
        if _path_err is not None:
            return _path_err
        _since = since.strip() if isinstance(since, str) and since.strip() else _auto_since(repo_path)
        return _execute(["prepare-context", "delta", repo_path, "--since", _since])
    except Exception as exc:
        return _err(
            f"Internal error: {type(exc).__name__}: {exc} — repo_path recibido: {_raw}",
            "INTERNAL_ERROR",
        )


@mcp.tool()
def check_freshness(repo_path: str = ".") -> dict:
    """Report RIS freshness relative to the current git HEAD.

    Answers instantly: is the cached snapshot current? How many commits behind?
    Use before deciding whether to call get_compact_context for a refresh.

    Returns:
      fresh (bool)               — True when RIS HEAD == current HEAD and no uncommitted changes
      current_git_head (str)     — Current repo HEAD (short SHA)
      ris_git_head (str|null)    — HEAD stored in RIS at last build
      delta_commits (int|null)   — Commits between ris_git_head and HEAD (0 = in sync)
      has_uncommitted_changes    — Working tree has staged or unstaged changes
      ris_exists (bool)          — False when no RIS built yet
      ris_last_updated_at (str)  — ISO-8601 timestamp of last RIS write

    repo_path: absolute path to the repository (default: current working directory).
    """
    _raw = repo_path
    try:
        if not isinstance(repo_path, str):
            return _err("repo_path must be a string", "INVALID_ARGUMENT")
        repo_path = _normalize_repo_path(repo_path)
        _path_err = _check_repo_path(repo_path)
        if _path_err is not None:
            return _path_err

        # Call Python functions directly — avoids CliRunner/subprocess nesting
        # that caused current_git_head to return "" on Windows parent-git repos.
        import subprocess as _sp
        from pathlib import Path as _Path
        from sourcecode.cache import _get_git_head as _cache_head
        from sourcecode.ris import load_ris as _load_ris, _has_uncommitted_changes as _huc

        target = _Path(repo_path).resolve()
        current_head = _cache_head(target)
        ris = _load_ris(target)

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
            delta = None
            if ris_head and current_head and ris_head != current_head:
                try:
                    _r = _sp.run(
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

        return _ok(result)
    except Exception as exc:
        return _err(
            f"Internal error: {type(exc).__name__}: {exc} — repo_path: {_raw}",
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

    IR access paths (for full IR via CLI):
      nodes  →  result["graph"]["nodes"]   (list of {fqn, type, ...})
      edges  →  result["graph"]["edges"]   (list of {source, target, type})
    Summary mode omits nodes/edges entirely (graph._omitted explains why).

    repo_path: absolute path to the Java repository (default: current working directory).
    """
    _raw = repo_path
    try:
        if not isinstance(repo_path, str):
            return _err("repo_path must be a string", "INVALID_ARGUMENT")
        repo_path = _normalize_repo_path(repo_path)
        _path_err = _check_repo_path(repo_path)
        if _path_err is not None:
            return _path_err
        result = _execute(["repo-ir", repo_path, "--summary-only"])
        if isinstance(result, dict) and "error" not in result:
            result["_ir_access"] = {
                "nodes_path": "result['graph']['nodes']",
                "edges_path": "result['graph']['edges']",
                "note": "nodes/edges omitted in summary mode — use CLI repo-ir --output ir.json for full graph",
            }
        return result
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
        _path_err = _check_repo_path(repo_path)
        if _path_err is not None:
            return _path_err
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
        _path_err = _check_repo_path(repo_path)
        if _path_err is not None:
            return _path_err
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
        _path_err = _check_repo_path(repo_path)
        if _path_err is not None:
            return _path_err
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
        _path_err = _check_repo_path(repo_path)
        if _path_err is not None:
            return _path_err
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
        _path_err = _check_repo_path(repo_path)
        if _path_err is not None:
            return _path_err
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
        _path_err = _check_repo_path(repo_path)
        if _path_err is not None:
            return _path_err
        args = ["prepare-context", "generate-tests", repo_path]
        if include_all:
            args.append("--all")

        # P0-3: timeout guard — large repos can stall the stdio transport indefinitely.
        timeout_ms = int(os.environ.get("SOURCECODE_TESTS_TIMEOUT_MS", str(_DEFAULT_TESTS_TIMEOUT_MS)))
        timeout_s = timeout_ms / 1000.0

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
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
            result = future.result()
        finally:
            executor.shutdown(wait=True)
        return result

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
        _path_err = _check_repo_path(repo_path)
        if _path_err is not None:
            return _path_err
        args = ["impact", target.strip(), repo_path, "--depth", str(depth)]
        return _execute(args)
    except Exception as exc:
        return _err(
            f"Internal error: {type(exc).__name__}: {exc} — repo_path recibido: {_raw}",
            "INTERNAL_ERROR",
        )


@mcp.tool()
def modernize_context(repo_path: str = ".", format: str = "json") -> dict:
    """Analyzes codebase for modernization opportunities: dead zones, hotspot scores, upgrade candidates.

    Maps to: sourcecode modernize <repo_path>
    Returns: hotspot_candidates (high fan-in + git churn), dead_zone_candidates (isolated classes),
             high_coupling_nodes, subsystem_summary, cross_module_tangles, recommendation.

    Best for: refactor planning, identifying where to start, finding safe removal candidates.
    Use get_compact_context or get_agent_context first for project orientation.

    repo_path: absolute path to the Java repository (default: current working directory).
    format: output format — "json" (default). Only json is supported; yaml is not available
            for modernize output.
    """
    _raw = repo_path
    try:
        if not isinstance(repo_path, str):
            return _err("repo_path must be a string", "INVALID_ARGUMENT")
        if not isinstance(format, str) or format != "json":
            return _err("format must be 'json' — yaml is not supported for modernize output", "INVALID_ARGUMENT")
        repo_path = _normalize_repo_path(repo_path)
        _path_err = _check_repo_path(repo_path)
        if _path_err is not None:
            return _path_err
        return _execute(["modernize", repo_path])
    except Exception as exc:
        return _err(
            f"Internal error: {type(exc).__name__}: {exc} — repo_path recibido: {_raw}",
            "INTERNAL_ERROR",
        )


_TELEMETRY_ACTIONS = frozenset({"status", "enable", "disable"})


@mcp.tool()
def version() -> dict:
    """Return sourcecode version and MCP compatibility metadata.

    Maps to: sourcecode version
    Returns structured JSON: cli_version, mcp_schema_version, compatibility_schema_version.
    cli_version and mcp_schema_version are always identical (released together).
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


def _finalize_mcp_registry() -> None:
    """Replace manual tool registration with the runtime-generated registry."""
    from sourcecode.mcp.registry import build_public_tool_specs, make_tool_callable, validate_registry

    try:
        for tool in list(mcp._tool_manager.list_tools()):  # type: ignore[attr-defined]
            mcp.remove_tool(tool.name)
    except Exception:
        pass

    for spec in build_public_tool_specs():
        tool_fn = make_tool_callable(spec)
        tool_fn.__doc__ = spec.docstring
        globals()[spec.name] = tool_fn
        mcp.add_tool(
            tool_fn,
            name=spec.name,
            description=spec.description,
            structured_output=False,
        )

    drift = validate_registry()
    if drift:
        raise RuntimeError(f"MCP registry drift detected: {drift}")


_finalize_mcp_registry()

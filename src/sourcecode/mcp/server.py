"""MCP server for sourcecode CLI.

Exposes sourcecode capabilities as MCP tools. Each tool maps to a CLI command
and delegates execution to the in-process runner — no subprocess, no binary
lookup, same process as the CLI.

All tools return the canonical contract:
  {"success": bool, "data": str | None, "error": {"code": str, "message": str} | None}
"""
from __future__ import annotations

from typing import Literal

from mcp.server.fastmcp import FastMCP

from sourcecode.mcp.runner import run_command

mcp = FastMCP("sourcecode")

_PREPARE_CONTEXT_TASKS = frozenset({
    "delta", "review-pr", "fix-bug", "onboard",
    "explain", "refactor", "generate-tests",
})
_TELEMETRY_ACTIONS = frozenset({"status", "enable", "disable"})


def _ok(data: str) -> dict:
    return {"success": True, "data": data, "error": None}


def _err(message: str, code: str = "EXECUTION_FAILED") -> dict:
    return {"success": False, "data": None, "error": {"code": code, "message": message}}


def _execute(args: list[str]) -> dict:
    try:
        return _ok(run_command(args))
    except RuntimeError as exc:
        return _err(str(exc))


@mcp.tool()
def compact(path: str, git_context: bool = False) -> dict:
    """Compact analysis of a repository (~1000-3000 tokens).

    Maps to: sourcecode <path> --compact [--git-context]
    """
    if not isinstance(path, str):
        return _err("path must be a string", "INVALID_ARGUMENT")
    if not isinstance(git_context, bool):
        return _err("git_context must be boolean", "INVALID_ARGUMENT")
    args = [path, "--compact"]
    if git_context:
        args.append("--git-context")
    return _execute(args)


@mcp.tool()
def agent(path: str, git_context: bool = False) -> dict:
    """Agent-optimised analysis: identity, entry points, dependencies, gaps.

    Maps to: sourcecode <path> --agent [--git-context]
    """
    if not isinstance(path, str):
        return _err("path must be a string", "INVALID_ARGUMENT")
    if not isinstance(git_context, bool):
        return _err("git_context must be boolean", "INVALID_ARGUMENT")
    args = [path, "--agent"]
    if git_context:
        args.append("--git-context")
    return _execute(args)


@mcp.tool()
def prepare_context(
    task: Literal[
        "delta", "review-pr", "fix-bug", "onboard",
        "explain", "refactor", "generate-tests",
    ],
    path: str,
) -> dict:
    """Task-specific context for AI coding agents.

    Maps to: sourcecode prepare-context <task> <path>

    task must be one of:
      explain        Architecture, entry points, key dependencies
      fix-bug        Risk-ranked files, suspected areas, annotations
      refactor       Structural issues, improvement opportunities
      generate-tests Untested source files, test gap analysis
      onboard        Full project context for new agents/developers
      review-pr      PR diff with runtime signals and security impact
      delta          Incremental context: git-changed files only
    """
    if task not in _PREPARE_CONTEXT_TASKS:
        return _err(
            f"task must be one of {sorted(_PREPARE_CONTEXT_TASKS)}",
            "INVALID_ARGUMENT",
        )
    if not isinstance(path, str):
        return _err("path must be a string", "INVALID_ARGUMENT")
    return _execute(["prepare-context", task, path])


@mcp.tool()
def repo_ir(path: str) -> dict:
    """Deterministic symbol-level IR for Java repositories.

    Maps to: sourcecode repo-ir <path>
    Output is JSON: graph{nodes,edges}, analysis, impact, subsystems, change_set.
    """
    if not isinstance(path, str):
        return _err("path must be a string", "INVALID_ARGUMENT")
    return _execute(["repo-ir", path])


@mcp.tool()
def version() -> dict:
    """Return the installed sourcecode version.

    Maps to: sourcecode version
    """
    return _execute(["version"])


@mcp.tool()
def config() -> dict:
    """Show sourcecode configuration (version, telemetry state, config path).

    Maps to: sourcecode config
    """
    return _execute(["config"])


@mcp.tool()
def telemetry(action: Literal["status", "enable", "disable"]) -> dict:
    """Manage telemetry settings.

    Maps to: sourcecode telemetry <status|enable|disable>
    action must be one of: status, enable, disable
    """
    if action not in _TELEMETRY_ACTIONS:
        return _err(
            f"action must be one of {sorted(_TELEMETRY_ACTIONS)}",
            "INVALID_ARGUMENT",
        )
    return _execute(["telemetry", action])

"""MCP server for sourcecode CLI.

Exposes sourcecode capabilities as MCP tools. Each tool maps to a CLI command
and delegates execution to the in-process runner — no subprocess, no binary
lookup, same process as the CLI.

All tools return:
  {"success": bool, "data": dict | str | None, "error": {"code": str, "message": str} | None}
data is the parsed JSON object from the CLI output, not a shell string.
"""
from __future__ import annotations

import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from sourcecode.mcp.runner import run_command

mcp = FastMCP("sourcecode")


def _ok(data: Any) -> dict:
    return {"success": True, "data": data, "error": None}


def _err(message: str, code: str = "EXECUTION_FAILED") -> dict:
    return {"success": False, "data": None, "error": {"code": code, "message": message}}


def _execute(args: list[str]) -> dict:
    try:
        return _ok(run_command(args))
    except RuntimeError as exc:
        return _err(str(exc))


@mcp.tool()
def get_compact_context(repo_path: str = ".") -> dict:
    """High-signal summary of a repository (~1000-3000 tokens).

    Maps to: sourcecode <repo_path> --compact
    Returns: stacks, entry points, dependency summary, confidence, gaps.
    repo_path: absolute path to the repository (default: current working directory).
    """
    if not isinstance(repo_path, str):
        return _err("repo_path must be a string", "INVALID_ARGUMENT")
    return _execute([repo_path, "--compact"])


@mcp.tool()
def get_agent_context(repo_path: str = ".") -> dict:
    """Agent-optimised analysis: identity, entry points, dependencies, gaps.

    Maps to: sourcecode <repo_path> --agent
    Returns: structured noise-free JSON for AI agents.
    repo_path: absolute path to the repository (default: current working directory).
    """
    if not isinstance(repo_path, str):
        return _err("repo_path must be a string", "INVALID_ARGUMENT")
    return _execute([repo_path, "--agent"])


@mcp.tool()
def get_endpoints(repo_path: str = ".") -> dict:
    """API endpoint surface extraction.

    Maps to: sourcecode <repo_path> --endpoints (pending CLI implementation)
    repo_path: absolute path to the repository (default: current working directory).
    """
    return _err(
        "get_endpoints requires --endpoints CLI flag (pending implementation). "
        "Use get_compact_context for now — the output includes api_endpoint-classified files.",
        "NOT_IMPLEMENTED",
    )


@mcp.tool()
def get_module_context(repo_path: str = ".", module: str = "") -> dict:
    """Compact analysis of a specific module or subdirectory within a repository.

    Maps to: sourcecode <repo_path>/<module> --compact
    repo_path: absolute path to the repository root.
    module: subdirectory name relative to repo_path (e.g. 'src/auth', 'api', 'core').
    """
    if not isinstance(repo_path, str):
        return _err("repo_path must be a string", "INVALID_ARGUMENT")
    if not isinstance(module, str) or not module.strip():
        return _err("module must be a non-empty string", "INVALID_ARGUMENT")
    module_path = os.path.join(repo_path, module)
    return _execute([module_path, "--compact"])


@mcp.tool()
def get_delta(repo_path: str = ".", since: str = "HEAD~1") -> dict:
    """Incremental context: git-changed files since a reference commit.

    Maps to: sourcecode prepare-context delta <repo_path> --since <since>
    repo_path: absolute path to the repository (default: current working directory).
    since: git ref to diff against (e.g. HEAD~3, main, origin/main).
    """
    if not isinstance(repo_path, str):
        return _err("repo_path must be a string", "INVALID_ARGUMENT")
    if not isinstance(since, str) or not since.strip():
        return _err("since must be a non-empty git ref", "INVALID_ARGUMENT")
    return _execute(["prepare-context", "delta", repo_path, "--since", since])


@mcp.tool()
def get_ir_summary(repo_path: str = ".") -> dict:
    """Deterministic symbol-level IR summary for Java repositories.

    Maps to: sourcecode repo-ir <repo_path> --summary-only
    Returns: analysis summary, impact, and change_set — omits full graph nodes/edges.
    repo_path: absolute path to the repository (default: current working directory).
    """
    if not isinstance(repo_path, str):
        return _err("repo_path must be a string", "INVALID_ARGUMENT")
    return _execute(["repo-ir", repo_path, "--summary-only"])

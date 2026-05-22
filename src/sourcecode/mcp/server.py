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
def get_compact_context(repo_path: str = ".", git_context: bool = False) -> dict:
    """High-signal summary of a repository (~1000-3000 tokens).

    Maps to: sourcecode <repo_path> --compact [--git-context]
    Returns: stacks, entry points, dependency summary, confidence, gaps.
    repo_path: absolute path to the repository (default: current working directory).
    git_context: include git log and branch context in the analysis.
    """
    if not isinstance(repo_path, str):
        return _err("repo_path must be a string", "INVALID_ARGUMENT")
    if not isinstance(git_context, bool):
        return _err("git_context must be boolean", "INVALID_ARGUMENT")
    args = [repo_path, "--compact"]
    if git_context:
        args.append("--git-context")
    return _execute(args)


@mcp.tool()
def get_agent_context(repo_path: str = ".", git_context: bool = False) -> dict:
    """Agent-optimised analysis: identity, entry points, dependencies, gaps.

    Maps to: sourcecode <repo_path> --agent [--git-context]
    Returns: structured noise-free JSON for AI agents.
    repo_path: absolute path to the repository (default: current working directory).
    git_context: include git log and branch context in the analysis.
    """
    if not isinstance(repo_path, str):
        return _err("repo_path must be a string", "INVALID_ARGUMENT")
    if not isinstance(git_context, bool):
        return _err("git_context must be boolean", "INVALID_ARGUMENT")
    args = [repo_path, "--agent"]
    if git_context:
        args.append("--git-context")
    return _execute(args)


@mcp.tool()
def get_endpoints(repo_path: str = ".") -> dict:
    """REST API endpoint surface extraction from Java source files.

    Maps to: sourcecode endpoints <repo_path>
    Returns: endpoints list with method, path, controller, handler fields;
             total (int) and undocumented (int) counts.
    Supports Spring MVC (@GetMapping etc.) and JAX-RS (@GET/@POST etc.).
    repo_path: absolute path to the repository (default: current working directory).
    """
    if not isinstance(repo_path, str):
        return _err("repo_path must be a string", "INVALID_ARGUMENT")
    return _execute(["endpoints", repo_path])


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


@mcp.tool()
def fix_bug_context(repo_path: str = ".", symptom: str = "") -> dict:
    """Risk-ranked files for bug investigation, optionally focused by symptom.

    Maps to: sourcecode prepare-context fix-bug <repo_path> [--symptom <symptom>]
    Includes compact_base: security_surface, transactional_boundaries, spring_profiles.
    repo_path: absolute path to the repository (default: current working directory).
    symptom: optional error message or class name to focus the file ranking
             (e.g. "NullPointerException in EstructuraRrHhRestController").
    """
    if not isinstance(repo_path, str):
        return _err("repo_path must be a string", "INVALID_ARGUMENT")
    args = ["prepare-context", "fix-bug", repo_path]
    if symptom and isinstance(symptom, str) and symptom.strip():
        args.extend(["--symptom", symptom.strip()])
    return _execute(args)


@mcp.tool()
def review_pr_context(repo_path: str = ".", since: str = "") -> dict:
    """Execution paths and risk analysis for changed files in a pull request.

    Maps to: sourcecode prepare-context review-pr <repo_path> [--since <since>]
    Returns: compact_base + execution_paths (diff-scoped) + hotspots for changed files.
    repo_path: absolute path to the repository (default: current working directory).
    since: git ref to diff against (e.g. HEAD~3, main, origin/main).
           If omitted, diffs against uncommitted changes or HEAD~1 fallback.
    """
    if not isinstance(repo_path, str):
        return _err("repo_path must be a string", "INVALID_ARGUMENT")
    args = ["prepare-context", "review-pr", repo_path]
    if since and isinstance(since, str) and since.strip():
        args.extend(["--since", since.strip()])
    return _execute(args)


@mcp.tool()
def onboard_context(repo_path: str = ".") -> dict:
    """Onboarding context: structured overview for new contributors.

    Maps to: sourcecode prepare-context onboard <repo_path>
    repo_path: absolute path to the repository (default: current working directory).
    """
    if not isinstance(repo_path, str):
        return _err("repo_path must be a string", "INVALID_ARGUMENT")
    return _execute(["prepare-context", "onboard", repo_path])


@mcp.tool()
def explain_context(repo_path: str = ".") -> dict:
    """Architecture and entry-point explanation for a repository.

    Maps to: sourcecode prepare-context explain <repo_path>
    Returns: project summary, architecture, entry points, key dependencies.
    repo_path: absolute path to the repository (default: current working directory).
    """
    if not isinstance(repo_path, str):
        return _err("repo_path must be a string", "INVALID_ARGUMENT")
    return _execute(["prepare-context", "explain", repo_path])


@mcp.tool()
def refactor_context(repo_path: str = ".") -> dict:
    """Structural issues and refactor opportunities for a repository.

    Maps to: sourcecode prepare-context refactor <repo_path>
    Returns: structural issues, coupling hotspots, improvement opportunities.
    repo_path: absolute path to the repository (default: current working directory).
    """
    if not isinstance(repo_path, str):
        return _err("repo_path must be a string", "INVALID_ARGUMENT")
    return _execute(["prepare-context", "refactor", repo_path])


@mcp.tool()
def generate_tests_context(repo_path: str = ".", include_all: bool = False) -> dict:
    """Untested source files and test gap analysis for a repository.

    Maps to: sourcecode prepare-context generate-tests <repo_path> [--all]
    Returns: test_gaps list of untested files ranked by risk.
    repo_path: absolute path to the repository (default: current working directory).
    include_all: return full test_gaps list without truncating to top 20.
    """
    if not isinstance(repo_path, str):
        return _err("repo_path must be a string", "INVALID_ARGUMENT")
    args = ["prepare-context", "generate-tests", repo_path]
    if include_all:
        args.append("--all")
    return _execute(args)


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

    Maps to: sourcecode telemetry <status|enable|disable>
    action must be one of: status, enable, disable
    """
    if action not in _TELEMETRY_ACTIONS:
        return _err(
            f"action must be one of {sorted(_TELEMETRY_ACTIONS)}",
            "INVALID_ARGUMENT",
        )
    return _execute(["telemetry", action])

"""In-process CLI runner for MCP tool execution.

Replaces the subprocess adapter from the standalone sourcecode-mcp project.
Calls CLI commands directly in the same process via CliRunner — no binary
lookup, no process fork, no stdout encoding issues.
"""
from __future__ import annotations

import json
from typing import Any

from typer.testing import CliRunner

_runner = CliRunner()


def run_command(args: list[str]) -> Any:
    """Invoke a sourcecode CLI command in-process and return parsed output.

    Returns parsed JSON dict when output is valid JSON, else the raw string.
    Raises RuntimeError on non-zero exit or empty output.
    """
    from sourcecode.cli import _set_detected_path, _preprocess_args, app

    _set_detected_path(".")
    processed = _preprocess_args(list(args))
    result = _runner.invoke(app, processed)

    if result.exit_code != 0:
        snippet = (result.output or "").strip()
        raise RuntimeError(
            f"sourcecode command failed (exit {result.exit_code}).\n"
            f"Args: {args}\n"
            f"Output: {snippet or '(empty)'}"
        )

    output = (result.output or "").strip()
    if not output:
        raise RuntimeError(
            f"sourcecode command produced no output.\n"
            f"Args: {args}"
        )

    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return output

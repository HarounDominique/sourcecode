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


class CommandError(RuntimeError):
    """Structured CLI failure captured by the in-process runner."""

    def __init__(
        self,
        message: str,
        *,
        exit_code: int,
        stdout: str = "",
        stderr: str = "",
        payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.payload = payload


def run_command(args: list[str]) -> Any:
    """Invoke a sourcecode CLI command in-process and return parsed output.

    Returns parsed JSON dict when output is valid JSON, else the raw string.
    Raises CommandError on non-zero exit so MCP can preserve structured payloads.
    """
    from sourcecode.cli import _preprocess_args, _set_detected_path, app

    _set_detected_path(".")
    processed = _preprocess_args(list(args))
    result = _runner.invoke(app, processed)

    if result.exit_code != 0:
        stdout_raw = getattr(result, "output", "")
        stderr_raw = getattr(result, "stderr", "")
        stdout = stdout_raw.strip() if isinstance(stdout_raw, str) else ""
        stderr = stderr_raw.strip() if isinstance(stderr_raw, str) else ""
        error_text = stderr or stdout
        payload = None
        if error_text:
            try:
                parsed = json.loads(error_text)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                payload = parsed
        raise CommandError(
            f"sourcecode command failed (exit {result.exit_code}). Args: {args}",
            exit_code=result.exit_code,
            stdout=stdout,
            stderr=stderr,
            payload=payload,
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

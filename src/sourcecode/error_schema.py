"""Unified structured error schema for CLI and MCP surfaces."""
from __future__ import annotations

from typing import Any

INVALID_INPUT_CODE = "INVALID_INPUT"
EXECUTION_FAILED_CODE = "EXECUTION_FAILED"
INTERNAL_ERROR_CODE = "INTERNAL_ERROR"


def default_error_hint(code: str) -> str:
    if code == INVALID_INPUT_CODE:
        return "Check the input value, path, or flag and try again."
    if code == EXECUTION_FAILED_CODE:
        return "Run the underlying CLI command directly to inspect stderr."
    if code == INTERNAL_ERROR_CODE:
        return "Retry the command. If it persists, capture the stack trace for debugging."
    return "Inspect the command input and retry."


def default_error_expected(code: str) -> str:
    if code == INVALID_INPUT_CODE:
        return "A supported value, path, or argument shape."
    if code == EXECUTION_FAILED_CODE:
        return "Successful CLI execution."
    if code == INTERNAL_ERROR_CODE:
        return "A successful internal operation."
    return "A valid command result."


def build_error_object(
    code: str,
    message: str,
    *,
    hint: str | None = None,
    expected: str | None = None,
) -> dict[str, str]:
    return {
        "code": code,
        "message": message,
        "hint": hint or default_error_hint(code),
        "expected": expected or default_error_expected(code),
    }


def build_error_envelope(
    code: str,
    message: str,
    *,
    hint: str | None = None,
    expected: str | None = None,
    **context: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"error": build_error_object(code, message, hint=hint, expected=expected)}
    payload.update(context)
    return payload

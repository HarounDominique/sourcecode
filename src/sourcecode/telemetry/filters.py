"""Privacy filter — last line of defense before any event leaves the process.

Every event is passed through this filter before transmission.
Any field that could carry sensitive data is stripped or replaced.

Rules enforced:
  - No string longer than 64 characters
  - No strings containing path separators (/ or \\)
  - No strings containing whitespace (could be file contents)
  - flags list: only known safe flag names (allowlist)
  - error_kind: class name only, no message text
  - All unknown/unexpected fields are dropped
"""

from __future__ import annotations

import re
from dataclasses import asdict
from typing import Any

from sourcecode.telemetry.events import TelemetryEvent

# Allowlist of known-safe flag names. Only these can appear in the flags field.
_SAFE_FLAGS: frozenset[str] = frozenset({
    "--agent",
    "--compact",
    "--dependencies",
    "--graph-modules",
    "--graph-detail",
    "--graph-edges",
    "--max-nodes",
    "--docs",
    "--docs-depth",
    "--full-metrics",
    "--semantics",
    "--architecture",
    "--git-context",
    "--git-depth",
    "--git-days",
    "--env-map",
    "--code-notes",
    "--format",
    "--output",
    "--depth",
    "--no-tree",
    "--tree",
    "--no-redact",
    "--compact",
    "--version",
    "--help",
    "--llm-prompt",
    "--since",
    "--task-help",
    "--dry-run",
})

_SAFE_OS: frozenset[str] = frozenset({"linux", "macos", "windows", "other"})
_SAFE_ARCH: frozenset[str] = frozenset({"x64", "arm64", "other"})
_SAFE_CMD: frozenset[str] = frozenset({"analyze", "prepare-context", "telemetry", "unknown"})
_SAFE_EVENTS: frozenset[str] = frozenset({
    "command_executed",
    "execution_completed",
    "execution_failed",
    "telemetry_enabled",
    "telemetry_disabled",
})
_SAFE_SIZES: frozenset[str] = frozenset({"tiny", "small", "medium", "large", "huge", "unknown"})
_SAFE_DURATIONS: frozenset[str] = frozenset({"<1s", "<5s", "<15s", "<60s", "60s+", "unknown"})
_SAFE_FMTS: frozenset[str] = frozenset({"json", "yaml"})

# Pattern that looks like a path segment
_PATH_PATTERN = re.compile(r"[/\\]|^\.|\.py$|\.js$|\.ts$|\.go$")
# Any string with spaces (could be a sentence / file content)
_SPACE_PATTERN = re.compile(r"\s")


def _safe_str(value: str, allowed: frozenset[str], fallback: str = "other") -> str:
    """Return value if it's in the allowlist, otherwise fallback."""
    return value if value in allowed else fallback


def _safe_flags(flags: list[str]) -> list[str]:
    """Return only flags in the explicit allowlist."""
    return sorted(f for f in flags if f in _SAFE_FLAGS)


def _safe_error_kind(value: str | None) -> str | None:
    """Keep only the exception class name. Drop any message text."""
    if not value:
        return None
    # Exception class names are CamelCase identifiers, no spaces, no paths
    name = value.split(":")[-1].strip()  # strip message after ':'
    name = name.split(".")[-1].strip()   # strip module prefix
    if len(name) > 64 or _SPACE_PATTERN.search(name) or _PATH_PATTERN.search(name):
        return "UnknownError"
    return name[:64] if name else None


def _safe_session(value: str) -> str:
    """Session ID must be a short hex string only."""
    if re.match(r"^[0-9a-f]{1,16}$", value):
        return value
    return ""


def sanitize(event: TelemetryEvent) -> dict[str, Any]:
    """Apply privacy filter to event and return a safe dict for transmission.

    This is the single choke point through which all data must pass.
    If any field cannot be validated as safe, it is replaced with a safe default.
    """
    safe: dict[str, Any] = {
        "event": _safe_str(event.event, _SAFE_EVENTS, "command_executed"),
        "ts": event.ts[:20] if event.ts else "",  # truncate to date+time, no sub-second
        "v": event.v[:16] if event.v else "",
        "py": event.py[:8] if event.py else "",
        "os": _safe_str(event.os, _SAFE_OS, "other"),
        "arch": _safe_str(event.arch, _SAFE_ARCH, "other"),
        "cmd": _safe_str(event.cmd, _SAFE_CMD, "unknown"),
        "flags": _safe_flags(event.flags),
        "output_fmt": _safe_str(event.output_fmt, _SAFE_FMTS, "json"),
        "repo_size": _safe_str(event.repo_size, _SAFE_SIZES, "unknown"),
        "duration": _safe_str(event.duration, _SAFE_DURATIONS, "unknown"),
        "success": bool(event.success),
    }

    if event.error_kind:
        safe["error_kind"] = _safe_error_kind(event.error_kind)

    session = _safe_session(event.session)
    if session:
        safe["session"] = session

    # Final validation: reject any field value that looks like a path or long string
    for key, val in list(safe.items()):
        if isinstance(val, str):
            if len(val) > 64 or _PATH_PATTERN.search(val):
                safe[key] = ""

    return safe

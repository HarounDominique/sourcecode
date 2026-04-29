"""Telemetry event schema.

All fields are either categorical (string enum), numeric ranges (string bucket),
or bounded scalars. No free-form text, no paths, no identifiers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def file_count_bucket(n: int) -> str:
    """Convert file count to anonymous range bucket."""
    if n < 50:
        return "tiny"
    if n < 500:
        return "small"
    if n < 2000:
        return "medium"
    if n < 10000:
        return "large"
    return "huge"


def duration_bucket(seconds: float) -> str:
    """Approximate duration without exposing exact timing."""
    if seconds < 1.0:
        return "<1s"
    if seconds < 5.0:
        return "<5s"
    if seconds < 15.0:
        return "<15s"
    if seconds < 60.0:
        return "<60s"
    return "60s+"


@dataclass
class TelemetryEvent:
    """Minimal, privacy-safe telemetry event.

    Fields:
        event       — event name (command_executed / execution_completed / execution_failed)
        ts          — ISO 8601 UTC timestamp
        v           — sourcecode version
        py          — Python major.minor (e.g. "3.11")
        os          — OS family: linux | macos | windows | other
        arch        — CPU architecture: x64 | arm64 | other
        cmd         — command: analyze | prepare-context | telemetry
        flags       — flag names only (no values)
        output_fmt  — json | yaml
        repo_size   — tiny | small | medium | large | huge
        duration    — <1s | <5s | <15s | <60s | 60s+
        success     — True/False
        error_kind  — exception class name only (no message, no traceback)
        session     — 8-char random hex, ephemeral, NOT persisted
    """

    event: str
    ts: str = field(default_factory=_now_utc)
    v: str = ""
    py: str = ""
    os: str = ""
    arch: str = ""
    cmd: str = ""
    flags: list[str] = field(default_factory=list)
    output_fmt: str = "json"
    repo_size: str = "unknown"
    duration: str = "unknown"
    success: bool = True
    error_kind: Optional[str] = None
    session: str = ""

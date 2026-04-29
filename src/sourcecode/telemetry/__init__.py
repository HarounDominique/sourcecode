"""sourcecode telemetry — opt-in anonymous usage metrics.

Public API:
    is_enabled()          → bool
    record(event, **kw)   → None  (fire-and-forget)
    session_id()          → str   (ephemeral 8-char hex, new each process)

Telemetry is strictly opt-in. It is disabled by default and can be disabled
at any time via `sourcecode telemetry disable` or SOURCECODE_TELEMETRY=0.

Nothing sensitive (code, paths, secrets, output) is ever collected.
See docs/privacy.md for full details.
"""

from __future__ import annotations

import platform
import sys
import uuid
from typing import Any, Optional

from sourcecode.telemetry.config import is_enabled
from sourcecode.telemetry.events import (
    TelemetryEvent,
    duration_bucket,
    file_count_bucket,
)
from sourcecode.telemetry.filters import sanitize
from sourcecode.telemetry.transport import send

# Ephemeral session identifier — new random value each process start.
# 8 hex chars, never persisted, used only to correlate events within one run.
_SESSION: str = uuid.uuid4().hex[:8]


def session_id() -> str:
    return _SESSION


def _platform_os() -> str:
    system = platform.system().lower()
    if system == "darwin":
        return "macos"
    if system in ("linux", "windows"):
        return system
    return "other"


def _platform_arch() -> str:
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64", "i386", "i686"):
        return "x64"
    if machine in ("arm64", "aarch64", "armv8l"):
        return "arm64"
    return "other"


def _python_version() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def _sourcecode_version() -> str:
    try:
        from sourcecode import __version__
        return __version__
    except Exception:
        return "unknown"


def record(
    event: str,
    *,
    cmd: str = "analyze",
    flags: Optional[list[str]] = None,
    output_fmt: str = "json",
    file_count: Optional[int] = None,
    duration_s: Optional[float] = None,
    success: bool = True,
    error_kind: Optional[str] = None,
) -> None:
    """Record a telemetry event. Fire-and-forget — never blocks or raises.

    All data is privacy-filtered before transmission. If telemetry is disabled,
    this function returns immediately without doing anything.
    """
    if not is_enabled():
        return

    try:
        ev = TelemetryEvent(
            event=event,
            v=_sourcecode_version(),
            py=_python_version(),
            os=_platform_os(),
            arch=_platform_arch(),
            cmd=cmd,
            flags=flags or [],
            output_fmt=output_fmt,
            repo_size=file_count_bucket(file_count) if file_count is not None else "unknown",
            duration=duration_bucket(duration_s) if duration_s is not None else "unknown",
            success=success,
            error_kind=error_kind,
            session=_SESSION,
        )
        payload = sanitize(ev)
        send(payload)
    except Exception:
        pass  # telemetry must never affect the main process


__all__ = ["is_enabled", "record", "session_id"]

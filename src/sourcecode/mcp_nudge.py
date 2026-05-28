"""MCP setup nudge — one-time stderr hint after successful analysis commands.

Fires when:
  1. At least one known MCP client (Claude Desktop, Cursor) is installed
  2. sourcecode is NOT yet registered in that client's config
  3. The nudge hasn't been shown yet (~/.sourcecode/nudge_shown flag absent)

The sentinel flag (~/.sourcecode/nudge_shown) persists globally on the
filesystem — it is NOT session-scoped. Once written it suppresses all future
nudges across all terminal sessions and process invocations until it is
deleted (which `sourcecode mcp init` does on successful installation).

Cleared by: a successful `sourcecode mcp init` (deletes the flag so the
post-init detection finds is_installed=True and never nudges again).

Side effects: writes only to stderr — stdout (JSON/YAML output) is untouched.
Exit code of the calling command is unaffected.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Stable path used as a session-level "already shown" sentinel.
_FLAG: Path = Path.home() / ".sourcecode" / "nudge_shown"

_MSG = (
    "→ Claude Desktop detected. "
    "Run `sourcecode mcp init` to enable agent integration.\n"
)

# Module-level imports so names are patchable in tests.
# Falls back to no-op stubs if onboarding package is unavailable.
try:
    from sourcecode.mcp.onboarding.detector import detect_clients  # noqa: PLC0415
    from sourcecode.mcp.onboarding.applier import is_installed, read_config  # noqa: PLC0415
    _IMPORTS_OK = True
except Exception:  # pragma: no cover
    _IMPORTS_OK = False

    def detect_clients() -> list:  # type: ignore[misc]
        return []

    def is_installed(config: dict) -> bool:  # type: ignore[misc]
        return False

    def read_config(path: Path) -> dict:  # type: ignore[misc]
        return {}


def nudge_mcp_if_needed() -> None:
    """Print MCP setup nudge to stderr at most once (until mcp init succeeds)."""
    # Fast path: already shown this session.
    if _FLAG.exists():
        return

    try:
        clients = detect_clients()
    except Exception:  # pragma: no cover
        return

    needs_nudge = any(
        c.app_installed and not is_installed(read_config(c.config_path))
        for c in clients
    )

    if not needs_nudge:
        return

    # Write nudge and persist flag.
    sys.stderr.write(_MSG)
    sys.stderr.flush()
    try:
        _FLAG.parent.mkdir(parents=True, exist_ok=True)
        _FLAG.touch()
    except OSError:
        pass  # Non-fatal: nudge will fire again next run, which is acceptable.


def clear_nudge_flag() -> None:
    """Delete the session flag so post-mcp-init runs don't re-show the nudge.

    Called by `mcp init` after a successful installation.  On the next run,
    detection finds is_installed=True → needs_nudge=False → no nudge shown.
    """
    try:
        _FLAG.unlink(missing_ok=True)
    except OSError:
        pass

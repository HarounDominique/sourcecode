"""Detect MCP-capable clients installed on the current machine."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


# Registry of known MCP clients and their config paths per platform.
# Keys: "darwin", "linux", "win32" — matching sys.platform values.
_CLIENT_REGISTRY: dict[str, dict[str, str]] = {
    "Claude Desktop": {
        "darwin": "~/Library/Application Support/Claude/claude_desktop_config.json",
        "linux": "~/.config/Claude/claude_desktop_config.json",
        "win32": "{APPDATA}/Claude/claude_desktop_config.json",
    },
    "Cursor": {
        "darwin": "~/.cursor/mcp.json",
        "linux": "~/.cursor/mcp.json",
        "win32": "{USERPROFILE}/.cursor/mcp.json",
    },
}


@dataclass(frozen=True)
class MCPClient:
    name: str
    config_path: Path
    app_installed: bool  # True if the config file (or its parent dir) exists


def _resolve(template: str) -> Path:
    """Expand env vars in Windows-style {VAR} templates, then expanduser."""
    result = template
    for var in ("APPDATA", "LOCALAPPDATA", "USERPROFILE"):
        val = os.environ.get(var, "")
        if val:
            result = result.replace(f"{{{var}}}", val)
    return Path(result).expanduser()


def detect_clients() -> list[MCPClient]:
    """Return all known MCP clients with their resolved config paths."""
    plat = sys.platform
    clients: list[MCPClient] = []
    for name, paths in _CLIENT_REGISTRY.items():
        template = paths.get(plat) or paths.get("linux")
        if not template:
            continue
        config_path = _resolve(template)
        # Consider client "installed" if its config file OR parent app dir exists.
        app_installed = config_path.exists() or config_path.parent.exists()
        clients.append(MCPClient(
            name=name,
            config_path=config_path,
            app_installed=app_installed,
        ))
    return clients

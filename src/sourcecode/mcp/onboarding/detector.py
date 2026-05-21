"""Detect MCP-capable clients installed on the current machine."""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List


_CLIENT_REGISTRY: List[Dict[str, Any]] = [
    {
        "name": "Claude Desktop",
        "slug": "claude-desktop",
        "paths": {
            "darwin": "~/Library/Application Support/Claude/claude_desktop_config.json",
            "linux": "~/.config/Claude/claude_desktop_config.json",
            "win32": "{APPDATA}/Claude/claude_desktop_config.json",
        },
        "process": {
            "darwin": "Claude",
            "linux": "claude-desktop",
            "win32": "Claude",
        },
    },
    {
        "name": "Cursor",
        "slug": "cursor",
        "paths": {
            "darwin": "~/.cursor/mcp.json",
            "linux": "~/.cursor/mcp.json",
            "win32": "{USERPROFILE}/.cursor/mcp.json",
        },
        "process": {
            "darwin": "Cursor",
            "linux": "cursor",
            "win32": "Cursor",
        },
    },
]


@dataclass(frozen=True)
class MCPClient:
    name: str
    config_path: Path
    app_installed: bool  # True if the config file (or its parent dir) exists
    process_name: str    # OS process name for connectivity check
    slug: str            # --target identifier (e.g. "claude-desktop")


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
    for entry in _CLIENT_REGISTRY:
        paths: Dict[str, str] = entry["paths"]
        processes: Dict[str, str] = entry["process"]
        template = paths.get(plat) or paths.get("linux", "")
        if not template:
            continue
        config_path = _resolve(template)
        app_installed = config_path.exists() or config_path.parent.exists()
        process_name = processes.get(plat) or processes.get("linux", "")
        clients.append(MCPClient(
            name=entry["name"],
            config_path=config_path,
            app_installed=app_installed,
            process_name=process_name,
            slug=entry["slug"],
        ))
    return clients


def is_client_running(client: MCPClient) -> bool:
    """True if the client process is currently running."""
    if not client.process_name:
        return False
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["tasklist", "/fi", f"imagename eq {client.process_name}.exe"],
                capture_output=True, text=True, timeout=5,
            )
            return client.process_name.lower() in result.stdout.lower()
        else:
            result = subprocess.run(
                ["pgrep", "-x", client.process_name],
                capture_output=True, timeout=5,
            )
            return result.returncode == 0
    except Exception:
        return False

"""Safe JSON config applier for MCP client configuration files."""
from __future__ import annotations

import json
import os
from pathlib import Path

_MCP_SERVERS_KEY = "mcpServers"
_ENTRY_NAME = "sourcecode"
_ENTRY_VALUE: dict[str, object] = {
    "command": "sourcecode",
    "args": ["mcp", "serve"],
}


def read_config(path: Path) -> dict:
    """Parse JSON config from path. Returns empty dict if missing, empty, or unreadable."""
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
        return json.loads(text) if text.strip() else {}
    except (OSError, json.JSONDecodeError):
        return {}


def is_installed(config: dict) -> bool:
    """True if sourcecode entry already present in mcpServers."""
    return _ENTRY_NAME in config.get(_MCP_SERVERS_KEY, {})


def apply_entry(config: dict) -> dict:
    """Return new config dict with sourcecode merged into mcpServers."""
    config = dict(config)
    servers: dict = dict(config.get(_MCP_SERVERS_KEY, {}))
    servers[_ENTRY_NAME] = _ENTRY_VALUE
    config[_MCP_SERVERS_KEY] = servers
    return config


def remove_entry(config: dict) -> dict:
    """Return new config dict with sourcecode removed from mcpServers."""
    config = dict(config)
    servers: dict = dict(config.get(_MCP_SERVERS_KEY, {}))
    servers.pop(_ENTRY_NAME, None)
    if servers:
        config[_MCP_SERVERS_KEY] = servers
    elif _MCP_SERVERS_KEY in config:
        del config[_MCP_SERVERS_KEY]
    return config


def write_config(path: Path, config: dict) -> None:
    """Atomically write config as formatted JSON using a temp file + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def validate(path: Path) -> bool:
    """True if path contains parseable JSON."""
    try:
        json.loads(path.read_text(encoding="utf-8"))
        return True
    except Exception:
        return False

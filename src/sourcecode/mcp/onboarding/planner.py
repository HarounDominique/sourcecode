"""Build an install/remove plan from detected MCP clients."""
from __future__ import annotations

from dataclasses import dataclass

from .applier import is_installed, read_config
from .detector import MCPClient


@dataclass(frozen=True)
class ClientAction:
    client: MCPClient
    already_installed: bool  # sourcecode entry already in config
    will_create_file: bool   # config file doesn't exist yet — will be created


def build_install_plan(clients: list[MCPClient]) -> list[ClientAction]:
    """Describe what `mcp init` would do for each detected client."""
    actions: list[ClientAction] = []
    for client in clients:
        config = read_config(client.config_path)
        actions.append(ClientAction(
            client=client,
            already_installed=is_installed(config),
            will_create_file=not client.config_path.exists(),
        ))
    return actions


def build_remove_plan(clients: list[MCPClient]) -> list[ClientAction]:
    """Describe what `mcp remove` would do for each detected client."""
    actions: list[ClientAction] = []
    for client in clients:
        config = read_config(client.config_path)
        actions.append(ClientAction(
            client=client,
            already_installed=is_installed(config),
            will_create_file=False,
        ))
    return actions

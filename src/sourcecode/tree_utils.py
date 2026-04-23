from __future__ import annotations

from typing import Any


def flatten_file_tree(file_tree: dict[str, Any], prefix: str = "") -> list[str]:
    """Aplana el file_tree jerarquico a paths relativos."""
    paths: list[str] = []
    for name, value in file_tree.items():
        current = f"{prefix}/{name}" if prefix else name
        paths.append(current)
        if isinstance(value, dict):
            paths.extend(flatten_file_tree(value, current))
    return paths


def path_exists_in_tree(file_tree: dict[str, Any], target: str) -> bool:
    """Comprueba si un path relativo existe en el file_tree."""
    normalized = target.strip("/").split("/")
    node: Any = file_tree
    for index, part in enumerate(normalized):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
        if index < len(normalized) - 1 and node is None:
            return False
    return True

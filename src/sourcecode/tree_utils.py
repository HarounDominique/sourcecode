from __future__ import annotations

from pathlib import Path
from typing import Any


def safe_read_text(path: Path) -> str:
    """Read a text file with encoding fallback to handle Latin-1 / UTF-8 ambiguity.

    Fallback chain: UTF-8 (strict) → ISO-8859-1 → UTF-8 with errors='replace'.
    This prevents double-encoding artefacts that arise when a Latin-1 file is
    read as UTF-8 with replace mode and then re-serialised as UTF-8.
    """
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return path.read_text(encoding="iso-8859-1")
    except (UnicodeDecodeError, OSError):
        pass
    return path.read_text(encoding="utf-8", errors="replace")


def flatten_file_tree(file_tree: dict[str, Any], prefix: str = "") -> list[str]:
    """Aplana el file_tree jerarquico a paths relativos."""
    paths: list[str] = []
    for name, value in file_tree.items():
        current = f"{prefix}/{name}" if prefix else name
        paths.append(current)
        if isinstance(value, dict):
            paths.extend(flatten_file_tree(value, current))
    return paths


def find_files_by_name(
    file_tree: dict[str, Any], filename: str, prefix: str = ""
) -> list[str]:
    """Return all paths in the tree whose filename matches `filename`."""
    paths: list[str] = []
    for key, value in file_tree.items():
        current = f"{prefix}/{key}" if prefix else key
        if isinstance(value, dict):
            paths.extend(find_files_by_name(value, filename, current))
        elif key == filename:
            paths.append(current)
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

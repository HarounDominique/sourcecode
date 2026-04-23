from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Optional


def load_json_file(path: Path) -> Optional[dict[str, Any]]:
    """Carga un fichero JSON sin lanzar si el contenido es invalido."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def load_toml_file(path: Path) -> Optional[dict[str, Any]]:
    """Carga un fichero TOML con fallback portable para Python 3.9+."""
    try:
        content = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return None

    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - solo en 3.9/3.10
        import tomli as tomllib

    try:
        data = tomllib.loads(content)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def read_text_lines(path: Path) -> list[str]:
    """Lee un fichero de texto y retorna lineas; si falla, retorna lista vacia."""
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def find_declared_dependencies(raw: Any) -> list[str]:
    """Normaliza dependencias declaradas desde listas o mapas simples."""
    if isinstance(raw, dict):
        return [str(key).strip() for key in raw if str(key).strip()]
    if isinstance(raw, (list, tuple, set)):
        result: list[str] = []
        for item in raw:
            if isinstance(item, str):
                name = item.strip()
                if name:
                    result.append(name)
            elif isinstance(item, dict):
                result.extend(find_declared_dependencies(item))
        return result
    if isinstance(raw, str):
        return [raw.strip()] if raw.strip() else []
    return []


def unique_strings(values: Iterable[str]) -> list[str]:
    """Deduplica preservando orden."""
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered

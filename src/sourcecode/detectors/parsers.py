from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Optional

_MAVEN_PROP_REF_RE = re.compile(r'\$\{([\w.\-]+)\}')


def substitute_maven_properties(text: str, props: "dict[str, str]") -> str:
    """Resolve Maven ${prop} references in *text* against *props*.

    Shared single source of truth for Maven property resolution across
    subcommands (BUG #1, Alfresco field test): both the --compact Java-stack
    detector and migrate-check must resolve `${java.version}` → `21` identically.
    Multi-level references (${a} where a=${b}) resolve in up to 3 passes; a
    reference with no matching property is left verbatim (honest, not blanked).
    """
    if not props or "${" not in text:
        return text
    resolved = text
    for _ in range(3):
        new = _MAVEN_PROP_REF_RE.sub(lambda m: props.get(m.group(1), m.group(0)), resolved)
        if new == resolved:
            break
        resolved = new
    return resolved


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

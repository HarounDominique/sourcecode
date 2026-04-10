"""Serializer de sourcecode — JSON canonico, YAML y modo compact.

Patrones criticos:
  - SIEMPRE pasar por dataclasses.asdict() antes de json.dumps (json no serializa dataclasses)
  - ruamel.yaml con representer para null canonico (no ~)
  - compact_view() proyecta solo los campos necesarios (~500 tokens)
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict, is_dataclass
from io import StringIO
from pathlib import Path
from typing import Any, Optional

from sourcecode.schema import SourceMap


def to_json(sm: SourceMap | dict[str, Any], indent: int = 2) -> str:
    """Serializa SourceMap o dict a JSON canonico.

    Acepta un SourceMap (dataclass) o un dict ya preparado (e.g. compact_view()).
    Usa dataclasses.asdict() para convertir dataclasses a dict antes de json.dumps.
    ensure_ascii=False para preservar UTF-8 en paths.
    """
    data = asdict(sm) if is_dataclass(sm) and not isinstance(sm, type) else sm
    return json.dumps(data, indent=indent, ensure_ascii=False)


def to_yaml(sm: SourceMap) -> str:
    """Serializa SourceMap a YAML usando ruamel.yaml.

    ruamel.yaml preserva el orden de claves y serializa None como null
    (no como ~) con la configuracion por defecto del dump de dicts.
    """
    from ruamel.yaml import YAML

    yaml = YAML()
    yaml.default_flow_style = False
    # Asegurar que None se serializa como 'null' y no como '~'
    yaml.representer.add_representer(
        type(None),
        lambda dumper, data: dumper.represent_scalar("tag:yaml.org,2002:null", "null"),
    )
    stream = StringIO()
    yaml.dump(asdict(sm), stream)
    return stream.getvalue()


def compact_view(sm: SourceMap) -> dict[str, Any]:
    """Proyeccion compacta del SourceMap (~500-700 tokens).

    Incluye: schema_version, project_type, stacks, entry_points,
    project_summary (siempre), file_paths (siempre),
    dependency_summary (cuando requested=True),
    file_tree_depth1 (backward compat).

    Excluye: dependencies (lista larga), docs, module_graph.
    """
    depth1: dict[str, Any] = {}
    for name, value in sm.file_tree.items():
        if isinstance(value, dict):
            depth1[name] = {}
        else:
            depth1[name] = None

    dep_summary_dict: Any = None
    if sm.dependency_summary is not None and sm.dependency_summary.requested:
        dep_summary_dict = asdict(sm.dependency_summary)

    return {
        "schema_version": sm.metadata.schema_version,
        "project_type": sm.project_type,
        "project_summary": sm.project_summary,
        "stacks": [asdict(stack) for stack in sm.stacks],
        "entry_points": [asdict(entry_point) for entry_point in sm.entry_points],
        "file_paths": sm.file_paths,
        "file_tree_depth1": depth1,
        "dependency_summary": dep_summary_dict,
    }


def write_output(content: str, output: Optional[Path]) -> None:
    """Escribe el contenido a stdout o a un fichero.

    Args:
        content: String serializado (JSON o YAML).
        output: Path del fichero destino. None = stdout.
    """
    if output is None:
        sys.stdout.write(content)
        if not content.endswith("\n"):
            sys.stdout.write("\n")
    else:
        output.write_text(content, encoding="utf-8")

"""Schema de output v1.0 para sourcecode.

Contrato publico estable — los detectores de Fase 2 escriben en SourceMap.stacks.
Los campos stacks, project_type y entry_points son vacios/null en Fase 1.

Convencion del arbol de ficheros (D-01, D-02):
  - None = fichero
  - dict = directorio (vacio o con hijos)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Optional


def _now_utc() -> str:
    """Retorna timestamp ISO 8601 UTC con offset +00:00."""
    return datetime.now(timezone.utc).isoformat()


def _sourcecode_version() -> str:
    from sourcecode import __version__
    return __version__


@dataclass
class AnalysisMetadata:
    """Metadatos del analisis — siempre presentes en el output."""

    schema_version: str = "1.0"
    generated_at: str = field(default_factory=_now_utc)
    sourcecode_version: str = field(default_factory=_sourcecode_version)
    analyzed_path: str = ""


@dataclass
class FrameworkDetection:
    """Framework detectado dentro de un stack."""

    name: str
    source: str = "manifest"


@dataclass
class StackDetection:
    """Deteccion de un stack tecnologico."""

    stack: str
    detection_method: Literal["manifest", "lockfile", "heuristic"] = "manifest"
    confidence: Literal["high", "medium", "low"] = "high"
    frameworks: list[FrameworkDetection] = field(default_factory=list)
    package_manager: Optional[str] = None
    manifests: list[str] = field(default_factory=list)
    primary: bool = False
    root: Optional[str] = None
    workspace: Optional[str] = None
    signals: list[str] = field(default_factory=list)


@dataclass
class EntryPoint:
    """Punto de entrada detectado del proyecto."""

    path: str
    stack: str
    kind: str = "entry"
    source: str = "manifest"


@dataclass
class DependencyRecord:
    """Dependencia detectada del proyecto."""

    name: str
    ecosystem: str
    scope: str = "direct"
    declared_version: Optional[str] = None
    resolved_version: Optional[str] = None
    source: str = "manifest"
    parent: Optional[str] = None
    manifest_path: Optional[str] = None
    workspace: Optional[str] = None


@dataclass
class DependencySummary:
    """Resumen del analisis de dependencias."""

    requested: bool = False
    total_count: int = 0
    direct_count: int = 0
    transitive_count: int = 0
    ecosystems: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)


@dataclass
class GraphNode:
    """Nodo del grafo estructural del codigo."""

    id: str
    kind: str
    language: str
    path: str
    symbol: Optional[str] = None
    display_name: Optional[str] = None
    workspace: Optional[str] = None
    importance: Optional[Literal["high", "medium", "low"]] = None


@dataclass
class GraphEdge:
    """Arista del grafo estructural del codigo."""

    source: str
    target: str
    kind: str
    confidence: Literal["high", "medium", "low"] = "medium"
    method: Literal["ast", "heuristic", "unresolved"] = "heuristic"


@dataclass
class ModuleGraphSummary:
    """Resumen del analisis estructural del grafo."""

    requested: bool = False
    node_count: int = 0
    edge_count: int = 0
    languages: list[str] = field(default_factory=list)
    methods: list[str] = field(default_factory=list)
    main_flows: list[str] = field(default_factory=list)
    layers: list[str] = field(default_factory=list)
    entry_points_count: int = 0
    truncated: bool = False
    detail: Optional[Literal["high", "medium", "full"]] = None
    max_nodes_applied: Optional[int] = None
    edge_kinds: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)


@dataclass
class ModuleGraph:
    """Grafo de modulos, simbolos y relaciones del proyecto."""

    nodes: list[GraphNode] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)
    summary: ModuleGraphSummary = field(default_factory=ModuleGraphSummary)


@dataclass
class SourceMap:
    """Schema completo del output v1.0.

    Campos de deteccion (stacks, project_type, entry_points) son vacios/null
    en Fase 1 y se rellenan a partir de Fase 2.

    file_tree sigue la convencion D-01/D-02:
      - None = fichero
      - dict = directorio
    """

    metadata: AnalysisMetadata = field(default_factory=AnalysisMetadata)
    file_tree: dict[str, Any] = field(default_factory=dict)
    stacks: list[StackDetection] = field(default_factory=list)
    project_type: Optional[str] = None                    # relleno en Fase 3
    entry_points: list[EntryPoint] = field(default_factory=list)
    dependencies: list[DependencyRecord] = field(default_factory=list)
    dependency_summary: Optional[DependencySummary] = None
    module_graph: Optional[ModuleGraph] = None
    module_graph_summary: Optional[ModuleGraphSummary] = None

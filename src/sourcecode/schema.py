from __future__ import annotations

"""Schema de output v1.0 para sourcecode.

Contrato publico estable — los detectores de Fase 2 escriben en SourceMap.stacks.
Los campos stacks, project_type y entry_points son vacios/null en Fase 1.

Convencion del arbol de ficheros (D-01, D-02):
  - None = fichero
  - dict = directorio (vacio o con hijos)
"""

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
    confidence: Literal["high", "medium", "low"] = "high"
    detected_via: list[str] = field(default_factory=list)


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
    confidence: Literal["high", "medium", "low"] = "high"
    reason: Optional[str] = None   # console_script | entry_file_pattern | main_guard | typer_app | heuristic | convention
    evidence: Optional[str] = None  # brief evidence string


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
    role: Optional[str] = None  # runtime | parsing | serialization | buildtool | observability | infra | devtool | testtool | unknown


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
    dependencies: list["DependencyRecord"] = field(default_factory=list)


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
    hubs: list[str] = field(default_factory=list)
    orphans: list[str] = field(default_factory=list)
    cycle_count: int = 0


@dataclass
class ModuleGraph:
    """Grafo de modulos, simbolos y relaciones del proyecto."""

    nodes: list[GraphNode] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)
    summary: ModuleGraphSummary = field(default_factory=ModuleGraphSummary)


DocsDepth = Literal["module", "symbols", "full"]

MetricAvailability = Literal["measured", "inferred", "unavailable"]


@dataclass
class FileMetrics:
    """Metricas de calidad de un fichero de codigo fuente."""

    path: str
    language: str
    is_test: bool = False
    production_target: Optional[str] = None
    total_lines: int = 0
    code_lines: int = 0
    blank_lines: int = 0
    comment_lines: int = 0
    loc_availability: MetricAvailability = "unavailable"
    function_count: int = 0
    class_count: int = 0
    symbol_availability: MetricAvailability = "unavailable"
    cyclomatic_complexity: Optional[float] = None
    complexity_availability: MetricAvailability = "unavailable"
    line_rate: Optional[float] = None
    branch_rate: Optional[float] = None
    coverage_source: Optional[str] = None
    coverage_availability: MetricAvailability = "unavailable"
    workspace: Optional[str] = None


@dataclass
class CoverageRecord:
    """Registro de cobertura de codigo de un fichero de cobertura."""

    source_file: str
    format: str
    line_rate: Optional[float] = None
    branch_rate: Optional[float] = None
    lines_covered: Optional[int] = None
    lines_valid: Optional[int] = None
    timestamp: Optional[str] = None
    tool_version: Optional[str] = None
    file_count: int = 0


@dataclass
class MetricsSummary:
    """Resumen del analisis de metricas de calidad de codigo."""

    requested: bool = False
    file_count: int = 0
    test_file_count: int = 0
    languages: list[str] = field(default_factory=list)
    total_loc: int = 0
    coverage_records: list[CoverageRecord] = field(default_factory=list)
    coverage_sources_found: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)


@dataclass
class DocRecord:
    """Registro de documentacion extraida de un simbolo del codigo."""

    symbol: str
    kind: str
    language: str
    path: str
    doc_text: Optional[str] = None
    signature: Optional[str] = None
    source: str = "unavailable"
    importance: Literal["high", "medium", "low"] = "medium"
    workspace: Optional[str] = None


@dataclass
class DocSummary:
    """Resumen del analisis de documentacion extraida."""

    requested: bool = False
    total_count: int = 0
    symbol_count: int = 0
    languages: list[str] = field(default_factory=list)
    depth: Optional[DocsDepth] = None
    truncated: bool = False
    limitations: list[str] = field(default_factory=list)


@dataclass
class SymbolRecord:
    """Symbol definition found in a source file."""

    symbol: str               # local name: "MyClass" or "my_func"
    kind: str                 # "function" | "class" | "constant" | "method"
    language: str
    path: str                 # relative path to defining file
    line: Optional[int] = None
    qualified_name: Optional[str] = None  # "pkg.module.MyClass"
    exported: bool = True     # False if name starts with _ and no __all__ override
    workspace: Optional[str] = None


@dataclass
class CallRecord:
    """A resolved call from one symbol to another, possibly across files."""

    caller_path: str
    caller_symbol: str
    callee_path: str
    callee_symbol: str
    call_line: Optional[int] = None
    confidence: Literal["high", "medium", "low"] = "medium"
    method: Literal["ast", "heuristic", "unresolved"] = "heuristic"
    args: list[str] = field(default_factory=list)
    kwargs: dict[str, str] = field(default_factory=dict)
    workspace: Optional[str] = None


@dataclass
class SymbolLink:
    """A symbol imported in one file, resolved to its definition in another."""

    importer_path: str
    symbol: str
    source_path: Optional[str] = None
    source_line: Optional[int] = None
    is_external: bool = False
    confidence: Literal["high", "medium", "low"] = "high"
    method: Literal["ast", "heuristic", "unresolved"] = "ast"
    workspace: Optional[str] = None


@dataclass
class SemanticSummary:
    """Summary of the --semantics analysis."""

    requested: bool = False
    call_count: int = 0
    symbol_count: int = 0
    link_count: int = 0
    languages: list[str] = field(default_factory=list)
    language_coverage: dict[str, str] = field(default_factory=dict)
    files_analyzed: int = 0
    files_skipped: int = 0
    truncated: bool = False
    limitations: list[str] = field(default_factory=list)


# --- Phase 13 Plan 04: Architectural Inference ---

@dataclass
class ArchitectureDomain:
    """Un dominio funcional inferido del codigo."""

    name: str
    files: list[str] = field(default_factory=list)
    role: str = ""
    confidence: Literal["high", "medium", "low"] = "low"


@dataclass
class ArchitectureLayer:
    """Una capa arquitectonica detectada."""

    name: str
    pattern: str
    files: list[str] = field(default_factory=list)
    confidence: Literal["high", "medium", "low"] = "low"


@dataclass
class BoundedContext:
    """Un contexto acotado aproximado inferido."""

    name: str
    modules: list[str] = field(default_factory=list)
    entry_files: list[str] = field(default_factory=list)
    confidence: Literal["high", "medium", "low"] = "low"


@dataclass
class ArchitectureAnalysis:
    """Resultado del analisis arquitectonico completo."""

    requested: bool = False
    pattern: Optional[str] = None
    domains: list[ArchitectureDomain] = field(default_factory=list)
    layers: list[ArchitectureLayer] = field(default_factory=list)
    bounded_contexts: list[BoundedContext] = field(default_factory=list)
    confidence: Literal["high", "medium", "low"] = "low"
    method: str = "heuristic"
    limitations: list[str] = field(default_factory=list)


# --- Env Map ---

@dataclass
class EnvVarRecord:
    """Variable de entorno referenciada en el codigo del proyecto."""

    key: str
    required: bool = True
    default: Optional[str] = None
    type_hint: Optional[str] = None   # string | int | bool | url | path | enum
    category: Optional[str] = None    # database | cache | storage | auth | service | observability | feature_flag | server | general
    description: Optional[str] = None
    files: list[str] = field(default_factory=list)  # "path:line"


@dataclass
class EnvSummary:
    """Resumen del analisis de variables de entorno."""

    requested: bool = False
    total: int = 0
    required_count: int = 0
    optional_count: int = 0
    categories: list[str] = field(default_factory=list)
    example_files_found: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)


# --- Code Notes ---

@dataclass
class CodeNote:
    """Nota de codigo: TODO, FIXME, HACK, NOTE, DEPRECATED, WARNING, XXX, BUG, OPTIMIZE."""

    kind: str                   # TODO | FIXME | HACK | NOTE | DEPRECATED | WARNING | XXX | BUG | OPTIMIZE
    path: str                   # ruta relativa al fichero
    line: int                   # numero de linea (1-based)
    text: str                   # texto de la nota (truncado a 200 chars)
    symbol: Optional[str] = None  # funcion o clase envolvente mas cercana


@dataclass
class AdrRecord:
    """Architecture Decision Record detectado en el repositorio."""

    path: str
    title: str
    status: Optional[str] = None    # accepted | proposed | deprecated | superseded
    summary: Optional[str] = None   # primer parrafo del ADR


@dataclass
class CodeNotesSummary:
    """Resumen del analisis de notas de codigo y ADRs."""

    requested: bool = False
    total: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)
    top_files: list[str] = field(default_factory=list)   # ficheros con mas notas
    adr_count: int = 0
    limitations: list[str] = field(default_factory=list)


# --- Context Summary for AI agents ---

@dataclass
class ContextSummary:
    """Compact, high-signal context for AI agents. Generated from all available analysis."""

    requested: bool = True
    runtime_shape: str = ""                          # "REST API (FastAPI + PostgreSQL)"
    dominant_pattern: Optional[str] = None           # "Clean Architecture", "MVC", etc.
    critical_modules: list[str] = field(default_factory=list)   # hub paths + entry paths
    layer_map: dict[str, list[str]] = field(default_factory=dict)  # {"domain": ["src/domain/"]}
    edit_hints: list[str] = field(default_factory=list)  # "auth → src/auth/, tests/"
    coupling_notes: list[str] = field(default_factory=list)  # "2 import cycles", "hub: schema.py"


# --- Confidence & Explainability ---

@dataclass
class ConfidenceSummary:
    """Resumen de confianza y calidad del analisis."""

    overall: Literal["high", "medium", "low"] = "medium"
    stack_confidence: Literal["high", "medium", "low"] = "medium"
    entry_point_confidence: Literal["high", "medium", "low"] = "medium"
    hard_signals: list[str] = field(default_factory=list)   # manifest, lockfile, real entrypoint
    soft_signals: list[str] = field(default_factory=list)   # heuristic, extension-based
    ignored_signals: list[str] = field(default_factory=list)  # tooling dirs, aux manifests
    anomalies: list[str] = field(default_factory=list)


@dataclass
class AnalysisGap:
    """Gap o incertidumbre detectada en el analisis."""

    area: str   # entry_points | dependencies | stack | architecture | env
    reason: str
    impact: Literal["high", "medium", "low"] = "medium"


# --- Git Context ---

@dataclass
class CommitRecord:
    """Un commit reciente del repositorio."""

    hash: str
    message: str
    author: str
    date: str
    files_changed: list[str] = field(default_factory=list)


@dataclass
class ChangeHotspot:
    """Fichero con mayor frecuencia de cambios en la ventana de tiempo."""

    file: str
    commit_count: int
    last_changed: str


@dataclass
class UncommittedChanges:
    """Cambios pendientes en el working tree."""

    staged: list[str] = field(default_factory=list)
    unstaged: list[str] = field(default_factory=list)
    untracked: list[str] = field(default_factory=list)


@dataclass
class GitContext:
    """Contexto temporal del repositorio git."""

    requested: bool = False
    branch: Optional[str] = None
    recent_commits: list[CommitRecord] = field(default_factory=list)
    change_hotspots: list[ChangeHotspot] = field(default_factory=list)
    uncommitted_changes: Optional[UncommittedChanges] = None
    contributors: list[str] = field(default_factory=list)
    git_summary: Optional[str] = None
    limitations: list[str] = field(default_factory=list)


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
    docs: list[DocRecord] = field(default_factory=list)
    doc_summary: Optional[DocSummary] = None
    # Phase 9: LLM Output Quality
    file_paths: list[str] = field(default_factory=list)
    project_summary: Optional[str] = None
    architecture_summary: Optional[str] = None
    key_dependencies: list[DependencyRecord] = field(default_factory=list)
    # Phase 10: Code Quality Metrics
    file_metrics: list[FileMetrics] = field(default_factory=list)
    metrics_summary: Optional[MetricsSummary] = None
    # Phase 12: Static Semantics
    semantic_calls: list[CallRecord] = field(default_factory=list)
    semantic_symbols: list[SymbolRecord] = field(default_factory=list)
    semantic_links: list[SymbolLink] = field(default_factory=list)
    semantic_summary: Optional[SemanticSummary] = None
    # Phase 13 Plan 04: Architectural Inference
    architecture: Optional[ArchitectureAnalysis] = None
    # Git Context
    git_context: Optional[GitContext] = None
    # Env Map
    env_map: list[EnvVarRecord] = field(default_factory=list)
    env_summary: Optional[EnvSummary] = None
    # Code Notes
    code_notes: list[CodeNote] = field(default_factory=list)
    code_adrs: list[AdrRecord] = field(default_factory=list)
    code_notes_summary: Optional[CodeNotesSummary] = None
    # Confidence & Explainability (v0.25.0)
    confidence_summary: Optional[ConfidenceSummary] = None
    analysis_gaps: list[AnalysisGap] = field(default_factory=list)
    # AI context summary (v0.25.0)
    context_summary: Optional[ContextSummary] = None

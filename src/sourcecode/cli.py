from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Optional, cast

import typer

from sourcecode import __version__
from sourcecode.entrypoint_classifier import is_production_entry_point, normalize_entry_point


# ---------------------------------------------------------------------------
# Analyzer fingerprints — short hashes of each analyzer's key rule constants.
# A change in heuristics, filter lists, or pattern maps changes the hash,
# making it immediately visible that two runs used different rule versions
# even if the semver string is the same.
# ---------------------------------------------------------------------------

def _fingerprint(*objects: object) -> str:
    raw = json.dumps([repr(o) for o in objects], sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:8]


def _compute_analyzer_fingerprints() -> dict[str, str]:
    from sourcecode.detectors.heuristic import (
        _AUXILIARY_DIRS as _HEUR_AUX,
        _ENTRYPOINT_NAMES,
        _EXTENSION_MAP,
    )
    from sourcecode.detectors.nodejs import _FRAMEWORK_MAP, NodejsDetector
    from sourcecode.confidence_analyzer import (
        _AUXILIARY_DIR_PREFIXES,
        _HARD_SOURCES,
        _SOFT_SOURCES,
    )
    from sourcecode.architecture_analyzer import (
        _BENCHMARK_DIRS,
        _NON_SOURCE_DIRS,
        LAYER_PATTERNS,
    )

    return {
        "heuristic": _fingerprint(_EXTENSION_MAP, _ENTRYPOINT_NAMES, sorted(_HEUR_AUX)),
        "nodejs": _fingerprint(_FRAMEWORK_MAP, sorted(NodejsDetector._AUXILIARY_DIRS)),
        "confidence": _fingerprint(sorted(_AUXILIARY_DIR_PREFIXES), sorted(_HARD_SOURCES), sorted(_SOFT_SOURCES)),
        "architecture": _fingerprint(sorted(_BENCHMARK_DIRS), sorted(_NON_SOURCE_DIRS), list(LAYER_PATTERNS.keys())),
    }


# ---------------------------------------------------------------------------
# Pipeline trace collector
# ---------------------------------------------------------------------------

class _TraceCollector:
    """Lightweight collector for pipeline trace events."""

    def __init__(self, enabled: bool = False) -> None:
        self._enabled = enabled
        self._events: list[dict[str, Any]] = []

    def emit(
        self,
        stage: str,
        component: str,
        action: str,
        target: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> None:
        if not self._enabled:
            return
        self._events.append({
            "stage": stage,
            "component": component,
            "action": action,
            **({"target": target} if target else {}),
            **({"reason": reason} if reason else {}),
        })

    def build_trace(self) -> "PipelineTrace":
        from sourcecode.schema import PipelineEvent, PipelineTrace
        events = [
            PipelineEvent(
                stage=e["stage"],
                component=e["component"],
                action=e["action"],
                target=e.get("target"),
                reason=e.get("reason"),
            )
            for e in self._events
        ]
        return PipelineTrace(requested=True, events=events)


# ---------------------------------------------------------------------------
# E2E pipeline coherence check
# ---------------------------------------------------------------------------

def _check_pipeline_coherence(sm: "SourceMap") -> list[str]:  # type: ignore[name-defined]
    """Verify no contradictory states exist between analyzers.

    Returns a list of human-readable violation strings (empty when clean).
    These are emitted to stderr as [coherence] warnings — never abort a run.
    """
    issues: list[str] = []
    cs = sm.confidence_summary

    if cs is not None:
        # overall:high requires at least one manifest-detected stack
        if cs.overall == "high":
            manifest_stacks = [s for s in sm.stacks if s.detection_method != "heuristic"]
            if not manifest_stacks:
                issues.append(
                    "[coherence] overall=high but all stacks are heuristic — "
                    "downgrade not applied; check confidence_analyzer"
                )

        # overall:high requires at least one production entry point
        if cs.overall == "high":
            prod_eps = [
                ep for ep in sm.entry_points
                if is_production_entry_point(ep)
            ]
            if not prod_eps and sm.entry_points:
                issues.append(
                    "[coherence] overall=high but no production entry points exist — "
                    "all detected EPs are auxiliary (benchmark/example/dev)"
                )

        # entry_point_confidence must not be high when entry_points is empty
        if cs.entry_point_confidence == "high" and not sm.entry_points:
            issues.append(
                "[coherence] entry_point_confidence=high but entry_points is empty"
            )

    return issues

_HELP = """\
Deterministic codebase context for AI coding agents.

[bold]Usage:[/bold]
  sourcecode                   [dim]# analyze current directory[/dim]
  sourcecode /path/to/repo     [dim]# analyze specific path[/dim]
  sourcecode --agent           [dim]# structured output for AI agents[/dim]

[bold]Subcommands:[/bold]
  prepare-context TASK [PATH]  [dim]# task-specific context[/dim]
  telemetry status|enable|disable
  version
  config
"""

# Known subcommand names — tokens matching these are routed as subcommands,
# not consumed as a repository path.
_SUBCOMMANDS: frozenset[str] = frozenset(
    {"telemetry", "prepare-context", "version", "config", "analyze"}
)

# Mutable container holding the path extracted by _preprocess_argv().
# Default "." means "current directory" when no path is given.
_detected_path: list[str] = ["."]


# Options that take a value token — their next arg must not be treated as a path.
_OPTIONS_WITH_VALUE: frozenset[str] = frozenset({
    "--format", "-f",
    "--output", "-o",
    "--graph-detail",
    "--graph-edges",
    "--max-nodes",
    "--docs-depth",
    "--depth",
    "--git-depth",
    "--git-days",
    "--since",
    "--path", "-p",
})


def _preprocess_args(args: list[str]) -> list[str]:
    """Extract a repository path token from an args list and store it in _detected_path.

    Returns the modified args list (path token removed).
    Correctly skips option values (e.g. ``yaml`` in ``--format yaml``).
    If the first non-flag, non-value positional token is a known subcommand name,
    args are returned unchanged so Click can dispatch the subcommand.
    """
    result = list(args)
    skip_next = False
    for i, arg in enumerate(result):
        if skip_next:
            skip_next = False
            continue
        if arg.startswith("-"):
            # Does this option consume the next token as its value?
            flag_name = arg.split("=")[0]
            if flag_name in _OPTIONS_WITH_VALUE and "=" not in arg:
                skip_next = True
            continue
        if arg in _SUBCOMMANDS:
            return result  # known subcommand — leave for Click to dispatch
        # First genuine positional: treat as repository path
        _detected_path[0] = arg
        result.pop(i)
        return result
    return result


def _preprocess_argv() -> None:
    """Apply _preprocess_args to sys.argv in-place (used by main_entry)."""
    import sys as _sys
    modified = _preprocess_args(_sys.argv[1:])
    _sys.argv = _sys.argv[:1] + modified


app = typer.Typer(
    name="sourcecode",
    help=_HELP,
    add_completion=False,
    rich_markup_mode="rich",
    no_args_is_help=False,
)

# ── Hook preprocessing into the Click command layer ───────────────────────────
# Typer's CliRunner (and app() itself) calls typer.main.get_command(app) to
# create a Click Group, then calls click_group.main(args=...).
# We patch get_command so the returned Click Group always preprocesses args —
# this covers both main_entry() (sys.argv path) and runner.invoke(app, args).
import typer.main as _typer_main_module  # noqa: E402

_orig_get_command = _typer_main_module.get_command


def _get_command_with_preprocessing(typer_instance: Any) -> Any:
    cmd = _orig_get_command(typer_instance)
    if typer_instance is not app:
        return cmd  # only wrap the root app, not telemetry_app etc.
    _orig_cmd_main = cmd.main

    def _cmd_main(args: Optional[list[str]] = None, **kwargs: Any) -> Any:
        if args is not None:
            # CliRunner / programmatic call: preprocess the explicit args list.
            _detected_path[0] = "."
            args = _preprocess_args(list(args))
        # args=None → Click reads sys.argv; _preprocess_argv() in main_entry handled it.
        return _orig_cmd_main(args=args, **kwargs)

    cmd.main = _cmd_main
    return cmd


_typer_main_module.get_command = _get_command_with_preprocessing

# typer.testing imports get_command as a private alias _get_command at module
# load time; patch that reference too so CliRunner.invoke uses our version.
try:
    import typer.testing as _typer_testing_module
    _typer_testing_module._get_command = _get_command_with_preprocessing  # type: ignore[attr-defined]
except Exception:
    pass

telemetry_app = typer.Typer(help="Manage anonymous telemetry (opt-in).", rich_markup_mode="rich")
app.add_typer(telemetry_app, name="telemetry")


def _maybe_ask_consent() -> None:
    """Show first-run consent prompt once, on interactive TTYs only."""
    try:
        from sourcecode.telemetry.config import has_been_asked, mark_asked, set_enabled
        from sourcecode.telemetry.consent import ask_for_consent
        if not has_been_asked():
            enabled = ask_for_consent()
            set_enabled(enabled)
            if enabled:
                typer.echo("Telemetry enabled. Thank you. Disable: sourcecode telemetry disable", err=True)
            else:
                typer.echo("Telemetry disabled. Enable anytime: sourcecode telemetry enable", err=True)
    except Exception:
        pass


def _active_flags(
    dependencies: bool, graph_modules: bool, docs: bool, full_metrics: bool,
    semantics: bool, architecture: bool, git_context: bool, env_map: bool,
    code_notes: bool, agent: bool, compact: bool, tree: bool, no_redact: bool,
    fmt: str,
) -> list[str]:
    flags: list[str] = []
    if agent: flags.append("--agent")
    if compact: flags.append("--compact")
    if dependencies: flags.append("--dependencies")
    if graph_modules: flags.append("--graph-modules")
    if docs: flags.append("--docs")
    if full_metrics: flags.append("--full-metrics")
    if semantics: flags.append("--semantics")
    if architecture: flags.append("--architecture")
    if git_context: flags.append("--git-context")
    if env_map: flags.append("--env-map")
    if code_notes: flags.append("--code-notes")
    if tree: flags.append("--tree")
    if no_redact: flags.append("--no-redact")
    if fmt != "json": flags.append("--format")
    return flags

FORMAT_CHOICES = ["json", "yaml"]
GRAPH_DETAIL_CHOICES = ["high", "medium", "full"]
GRAPH_EDGE_CHOICES = {"imports", "calls", "contains", "extends"}
DOCS_DEPTH_CHOICES = ["module", "symbols", "full"]


def version_callback(value: bool) -> None:
    if value:
        typer.echo(f"sourcecode {__version__}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    format: str = typer.Option(
        "json",
        "--format",
        "-f",
        help="Formato de salida: json|yaml",
        show_default=True,
    ),
    output: Optional[Path] = typer.Option(
        None,
        "--output",
        "-o",
        help="Fichero de salida (default: stdout)",
    ),
    compact: bool = typer.Option(
        False,
        "--compact",
        help="Output reducido (~500-700 tokens): tipo, stacks, entradas, arbol nivel 1 y summaries de flags opcionales activos",
    ),
    dependencies: bool = typer.Option(
        False,
        "--dependencies",
        help="Incluir dependencias directas, versiones exactas y transitivas cuando haya lockfiles compatibles",
    ),
    graph_modules: bool = typer.Option(
        False,
        "--graph-modules",
        help="Incluir grafo estructural de modulos, imports y relaciones simples del codigo",
    ),
    graph_detail: str = typer.Option(
        "high",
        "--graph-detail",
        help="Nivel de detalle del grafo: high|medium|full",
        show_default=True,
    ),
    max_nodes: Optional[int] = typer.Option(
        None,
        "--max-nodes",
        help="Limite de nodos para `--graph-modules` en modos high/medium",
        min=1,
    ),
    graph_edges: Optional[str] = typer.Option(
        None,
        "--graph-edges",
        help="Tipos de arista para `--graph-modules` separados por comas: imports,calls,contains,extends",
    ),
    no_tree: bool = typer.Option(
        False,
        "--no-tree",
        help="Suprimir file_tree y file_paths del output (ahora deprecado: el arbol ya no se incluye por defecto)",
    ),
    tree: bool = typer.Option(
        False,
        "--tree",
        help="Incluir file_tree completo y file_paths en el output (capa deep-dive)",
    ),
    no_redact: bool = typer.Option(
        False,
        "--no-redact",
        help="Desactivar redaccion de secretos (activa por defecto)",
    ),
    version: Optional[bool] = typer.Option(
        None,
        "--version",
        "-v",
        callback=version_callback,
        is_eager=True,
        help="Mostrar version y salir",
    ),
    depth: int = typer.Option(
        4,
        "--depth",
        help="Profundidad maxima del arbol de ficheros (default: 4)",
        min=1,
        max=20,
    ),
    docs: bool = typer.Option(
        False,
        "--docs",
        help="Incluir documentacion extraida: docstrings, firmas y comentarios de modulos y simbolos",
    ),
    docs_depth: str = typer.Option(
        "symbols",
        "--docs-depth",
        help="Profundidad de extraccion de docs: module|symbols|full",
        show_default=True,
    ),
    full_metrics: bool = typer.Option(
        False,
        "--full-metrics",
        help="Auditoria tecnica: LOC, simbolos, complejidad ciclomatica y cobertura por fichero. No incluido en --agent (uso: CI, code review, no context principal para agentes IA)",
    ),
    semantics: bool = typer.Option(
        False,
        "--semantics",
        help="Incluir call graph semantico, linking cross-file de simbolos y resolucion avanzada de imports",
    ),
    architecture: bool = typer.Option(
        False,
        "--architecture",
        help="Inferencia arquitectonica: dominios funcionales, capas (MVC/layered/hexagonal) y bounded contexts aproximados",
    ),
    git_context: bool = typer.Option(
        False,
        "--git-context",
        "-g",
        help="Incluir contexto git: commits recientes, ficheros mas activos y cambios pendientes",
    ),
    git_depth: int = typer.Option(
        20,
        "--git-depth",
        help="Numero de commits recientes a incluir con --git-context (default: 20)",
        min=1,
        max=100,
    ),
    git_days: int = typer.Option(
        90,
        "--git-days",
        help="Ventana temporal en dias para detectar ficheros mas activos con --git-context (default: 90)",
        min=1,
        max=3650,
    ),
    env_map: bool = typer.Option(
        False,
        "--env-map",
        help="Incluir mapa de variables de entorno: claves, tipos, categorias y ficheros que las referencian",
    ),
    code_notes: bool = typer.Option(
        False,
        "--code-notes",
        help="Extraer anotaciones TODO/FIXME/HACK/NOTE/DEPRECATED/WARNING/BUG/XXX/OPTIMIZE con ubicacion y simbolo envolvente, y detectar ADRs en docs/decisions/, docs/adr/ y similares",
    ),
    agent: bool = typer.Option(
        False,
        "--agent",
        help="Modo agente: output estructurado y sin ruido para consumo por IA. Incluye identidad, entrypoints, arquitectura, dependencias clave, señales operacionales y gaps. Sin arbol de ficheros ni secciones vacias.",
    ),
    trace_pipeline: bool = typer.Option(
        False,
        "--trace-pipeline",
        help="Modo trazabilidad: incluye pipeline_trace con candidatos, filtros, descartes y origen de cada dato. Para diagnóstico de contaminación de resultados.",
    ),
) -> None:
    """Analyze a repository and produce structured context for AI coding agents.

    \b
    Examples:
      sourcecode                        analyze current directory
      sourcecode /path/to/repo          analyze specific path
      sourcecode --agent                agent-optimized output
      sourcecode --agent --git-context  include git activity signals
    """
    # First-run consent (skip for telemetry/version/config subcommands)
    if ctx.invoked_subcommand not in ("telemetry", "version", "config"):
        _maybe_ask_consent()

    # When a subcommand is invoked, skip the main analysis.
    if ctx.invoked_subcommand is not None:
        return

    _t0 = time.monotonic()

    # Validar formato
    if format not in FORMAT_CHOICES:
        typer.echo(
            f"Error: valor invalido '{format}' para --format. Opciones: {', '.join(FORMAT_CHOICES)}",
            err=True,
        )
        raise typer.Exit(code=1)
    if graph_detail not in GRAPH_DETAIL_CHOICES:
        typer.echo(
            f"Error: valor invalido '{graph_detail}' para --graph-detail. Opciones: {', '.join(GRAPH_DETAIL_CHOICES)}",
            err=True,
        )
        raise typer.Exit(code=1)
    if docs_depth not in DOCS_DEPTH_CHOICES:
        typer.echo(
            f"Error: valor invalido '{docs_depth}' para --docs-depth. Opciones: {', '.join(DOCS_DEPTH_CHOICES)}",
            err=True,
        )
        raise typer.Exit(code=1)

    # Path was extracted from argv by _preprocess_argv() before Click ran.
    target = Path(_detected_path[0]).resolve()
    if not target.exists():
        typer.echo(f"Error: directory '{target}' does not exist.", err=True)
        raise typer.Exit(code=1)
    if not target.is_dir():
        typer.echo(f"Error: '{target}' is not a directory.", err=True)
        raise typer.Exit(code=1)

    # --- Importar modulos de logica ---
    from dataclasses import asdict, replace

    from sourcecode.dependency_analyzer import DependencyAnalyzer
    from sourcecode.detectors import ProjectDetector, build_default_detectors
    from sourcecode.doc_analyzer import DocAnalyzer
    from sourcecode.graph_analyzer import GraphAnalyzer, GraphDetail
    from sourcecode.metrics_analyzer import MetricsAnalyzer
    from sourcecode.redactor import SecretRedactor, redact_dict
    from sourcecode.scanner import FileScanner
    from sourcecode.semantic_analyzer import SemanticAnalyzer
    from sourcecode.schema import (
        AnalysisMetadata,
        DocRecord,
        DocsDepth,
        DocSummary,
        EntryPoint,
        SourceMap,
        StackDetection,
    )
    from sourcecode.serializer import agent_view, compact_view, normalize_source_map, standard_view, validate_cross_analyzer_consistency, validate_source_map, write_output
    from sourcecode.workspace import WorkspaceAnalyzer

    # 1. Escanear el directorio (SCAN-01 a SCAN-05)
    redactor = SecretRedactor(enabled=not no_redact)

    # Detectar manifests antes del scan para ajustar depth.
    # find_manifests() solo mira profundidad 0-1, no necesita el arbol.
    _pre_scanner = FileScanner(target, max_depth=1)
    manifests = _pre_scanner.find_manifests()

    # Maven usa src/main/java/<groupId>/<artifactId>/<module>/ (profundidad 7+).
    # Con depth=4 los ficheros .java son invisibles y todos los analizadores fallan.
    # Necesitamos al menos 8: src(1)+main(2)+java(3)+com(4)+co(5)+app(6)+module(7)+file.
    _java_manifest_names = {"pom.xml", "build.gradle", "build.gradle.kts"}
    _is_java = any(Path(m).name in _java_manifest_names for m in manifests)
    _java_min_depth = 8
    effective_depth = max(depth, _java_min_depth) if _is_java and depth < _java_min_depth else depth

    # --agent: enable signal analyzers; output via agent_view (not compact)
    if agent:
        dependencies = True
        env_map = True
        code_notes = True
        no_tree = True  # agents never need the raw file tree
        typer.echo("[agent] dependencies env-map code-notes (no-tree)", err=True)

    scanner = FileScanner(target, max_depth=effective_depth)
    raw_tree = scanner.scan_tree()

    # 2. Filtrar del arbol las entradas de .env y *.secret (SEC-02, todos los niveles)
    def filter_sensitive_files(tree: dict[str, Any]) -> dict[str, Any]:
        filtered: dict[str, Any] = {}
        for name, value in tree.items():
            if redactor.should_exclude_file(name):
                continue  # excluir .env, *.secret del arbol
            if isinstance(value, dict):
                filtered[name] = filter_sensitive_files(value)
            else:
                filtered[name] = value
        return filtered

    def prune_workspace_paths(
        tree: dict[str, Any], workspace_paths: list[str]
    ) -> dict[str, Any]:
        pruned = dict(tree)
        for workspace_path in workspace_paths:
            parts = [part for part in workspace_path.split("/") if part]
            if not parts:
                continue
            node = pruned
            for index, part in enumerate(parts):
                if not isinstance(node, dict) or part not in node:
                    break
                if index == len(parts) - 1:
                    node.pop(part, None)
                    break
                child = node.get(part)
                if not isinstance(child, dict):
                    break
                node = child
        return pruned

    file_tree = filter_sensitive_files(raw_tree)
    detector = ProjectDetector(build_default_detectors())
    workspace_analysis = WorkspaceAnalyzer().analyze(target, manifests)

    # --compact implicitly enables lightweight analysis passes so that
    # dependency_summary, env_summary and code_notes_summary are never null.
    if compact:
        dependencies = True
        env_map = True
        code_notes = True

    dependency_analyzer = DependencyAnalyzer() if dependencies else None
    graph_analyzer = GraphAnalyzer() if graph_modules else None
    parsed_graph_edges = (
        {edge.strip() for edge in graph_edges.split(",") if edge.strip()}
        if graph_edges
        else None
    )
    if parsed_graph_edges is not None:
        invalid_edges = sorted(parsed_graph_edges - GRAPH_EDGE_CHOICES)
        if invalid_edges:
            typer.echo(
                "Error: valores invalidos para --graph-edges: "
                f"{', '.join(invalid_edges)}. Opciones: {', '.join(sorted(GRAPH_EDGE_CHOICES))}",
                err=True,
            )
            raise typer.Exit(code=1)
    graph_detail_typed = cast(GraphDetail, graph_detail)
    docs_depth_typed = cast(DocsDepth, docs_depth)
    doc_analyzer = DocAnalyzer() if docs else None
    metrics_analyzer = MetricsAnalyzer() if full_metrics else None

    semantic_analyzer = SemanticAnalyzer() if semantics else None

    root_manifests = [
        manifest
        for manifest in manifests
        if Path(manifest).resolve().parent == target
    ]
    detection_manifests = root_manifests if workspace_analysis.workspaces else manifests
    if workspace_analysis.is_monorepo and not root_manifests:
        stacks: list[StackDetection] = []
        entry_points: list[EntryPoint] = []
    else:
        stacks, entry_points, _project_type = detector.detect(target, file_tree, detection_manifests)

    dependency_records = []
    dependency_summaries = []
    if dependency_analyzer is not None:
        root_dependencies, root_summary = dependency_analyzer.analyze(target)
        dependency_records.extend(root_dependencies)
        dependency_summaries.append(root_summary)
    module_graphs = []
    if graph_analyzer is not None:
        root_graph_tree = (
            prune_workspace_paths(
                file_tree,
                [workspace.path for workspace in workspace_analysis.workspaces],
            )
            if workspace_analysis.workspaces
            else file_tree
        )
        module_graphs.append(
            graph_analyzer.analyze(
                target,
                root_graph_tree,
                detail="full",
                entry_points=entry_points,
            )
        )
    doc_records: list[DocRecord] = []
    doc_summaries: list[DocSummary] = []
    if doc_analyzer is not None:
        root_doc_tree = (
            prune_workspace_paths(
                file_tree,
                [workspace.path for workspace in workspace_analysis.workspaces],
            )
            if workspace_analysis.workspaces
            else file_tree
        )
        root_doc_records, root_doc_summary = doc_analyzer.analyze(
            target,
            root_doc_tree,
            depth=docs_depth_typed,
            entry_points=[ep.path for ep in entry_points],   # LQN-03
        )
        doc_records.extend(root_doc_records)
        doc_summaries.append(root_doc_summary)

    file_metrics_records: list[Any] = []
    metrics_summaries = []
    if metrics_analyzer is not None:
        root_metrics_tree = (
            prune_workspace_paths(
                file_tree,
                [workspace.path for workspace in workspace_analysis.workspaces],
            )
            if workspace_analysis.workspaces
            else file_tree
        )
        root_file_metrics, root_metrics_summary = metrics_analyzer.analyze(
            target,
            root_metrics_tree,
        )
        file_metrics_records.extend(root_file_metrics)
        metrics_summaries.append(root_metrics_summary)

    for workspace in workspace_analysis.workspaces:
        workspace_root = target / workspace.path
        if not workspace_root.exists() or not workspace_root.is_dir():
            continue
        workspace_scanner = FileScanner(workspace_root, max_depth=depth)
        workspace_tree = filter_sensitive_files(workspace_scanner.scan_tree())
        workspace_manifests = workspace_scanner.find_manifests()
        workspace_stacks, workspace_entry_points, _ = detector.detect(
            workspace_root,
            workspace_tree,
            workspace_manifests,
        )

        stacks.extend(
            replace(stack, root=workspace.path, workspace=workspace.path, primary=False)
            for stack in workspace_stacks
        )
        entry_points.extend(
            replace(
                entry_point,
                path=f"{workspace.path}/{entry_point.path}",
            )
            for entry_point in workspace_entry_points
        )
        if dependency_analyzer is not None:
            workspace_dependencies, workspace_summary = dependency_analyzer.analyze(
                workspace_root,
                workspace=workspace.path,
            )
            dependency_records.extend(workspace_dependencies)
            dependency_summaries.append(workspace_summary)
        if graph_analyzer is not None:
            workspace_graph = graph_analyzer.analyze(
                workspace_root,
                workspace_tree,
                workspace=workspace.path,
                detail="full",
                entry_points=workspace_entry_points,
            )
            module_graphs.append(
                graph_analyzer.prefix_graph(workspace_graph, workspace.path, workspace.path)
            )
        if doc_analyzer is not None:
            workspace_doc_records, workspace_doc_summary = doc_analyzer.analyze(
                workspace_root,
                workspace_tree,
                workspace=workspace.path,
                depth=docs_depth_typed,
                entry_points=[ep.path for ep in workspace_entry_points],   # LQN-03
            )
            # Prefix paths with workspace.path so they are relative to repo root
            # (same pattern as entry_points path prefixing)
            prefixed_doc_records = [
                replace(record, path=f"{workspace.path}/{record.path}")
                for record in workspace_doc_records
            ]
            doc_records.extend(prefixed_doc_records)
            doc_summaries.append(workspace_doc_summary)
        if metrics_analyzer is not None:
            ws_file_metrics, ws_metrics_summary = metrics_analyzer.analyze(
                workspace_root,
                workspace_tree,
                workspace=workspace.path,
            )
            prefixed_file_metrics = [
                replace(m, path=f"{workspace.path}/{m.path}")
                for m in ws_file_metrics
            ]
            file_metrics_records.extend(prefixed_file_metrics)
            metrics_summaries.append(ws_metrics_summary)

    stacks, project_type = detector.classify_results(
        file_tree,
        stacks,
        entry_points,
        project_type_override="monorepo" if workspace_analysis.is_monorepo else None,
    )
    dependency_summary = (
        dependency_analyzer.merge_summaries(dependency_summaries)
        if dependency_analyzer is not None
        else None
    )
    module_graph = (
        graph_analyzer.merge_graphs(
            module_graphs,
            detail=graph_detail_typed,
            edge_kinds=parsed_graph_edges,
            max_nodes=max_nodes,
            entry_points=entry_points,
        )
        if graph_analyzer is not None
        else None
    )
    doc_summary = (
        doc_analyzer.merge_summaries(doc_summaries)
        if doc_analyzer is not None
        else None
    )
    metrics_summary = (
        metrics_analyzer.merge_summaries(metrics_summaries)
        if metrics_analyzer is not None
        else None
    )

    # 3. Construir el schema
    # Compute analyzer fingerprints: short hashes of each analyzer's key rule
    # constants so that a rule change is always visible in the output, regardless
    # of whether the semver was bumped.
    try:
        _fingerprints = _compute_analyzer_fingerprints()
    except Exception:
        _fingerprints = {}

    metadata = AnalysisMetadata(
        analyzed_path=str(target),
        analyzer_fingerprints=_fingerprints,
    )
    sm = SourceMap(
        metadata=metadata,
        file_tree=file_tree,
        stacks=stacks,
        project_type=project_type,
        entry_points=entry_points,
        dependencies=dependency_records,
        dependency_summary=dependency_summary,
        module_graph=module_graph,
        module_graph_summary=module_graph.summary if module_graph is not None else None,
        docs=doc_records,
        doc_summary=doc_summary,
        file_metrics=file_metrics_records,
        metrics_summary=metrics_summary,
    )

    # Semantic analysis (--semantics flag)
    if semantic_analyzer is not None:
        if workspace_analysis.workspaces:
            all_sem_calls: list[Any] = []
            all_sem_symbols: list[Any] = []
            all_sem_links: list[Any] = []
            all_sem_summaries: list[Any] = []
            for ws in workspace_analysis.workspaces:
                ws_calls, ws_syms, ws_links, ws_sum = semantic_analyzer.analyze(
                    target / ws.path,
                    (
                        filter_sensitive_files(
                            FileScanner(target / ws.path, max_depth=depth).scan_tree()
                        )
                    ),
                    workspace=ws.path,
                )
                all_sem_calls.extend(ws_calls)
                all_sem_symbols.extend(ws_syms)
                all_sem_links.extend(ws_links)
                all_sem_summaries.append(ws_sum)
            merged_sem = semantic_analyzer.merge_summaries(all_sem_summaries)
            sm = replace(
                sm,
                semantic_calls=all_sem_calls,
                semantic_symbols=all_sem_symbols,
                semantic_links=all_sem_links,
                semantic_summary=merged_sem,
            )
        else:
            sem_calls, sem_syms, sem_links, sem_sum = semantic_analyzer.analyze(
                target, file_tree
            )
            sm = replace(
                sm,
                semantic_calls=sem_calls,
                semantic_symbols=sem_syms,
                semantic_links=sem_links,
                semantic_summary=sem_sum,
            )

    # Runtime architecture — classify workspace packages for structural summaries
    if workspace_analysis.workspaces:
        from sourcecode.runtime_classifier import RuntimeClassifier
        sm.monorepo_packages = RuntimeClassifier().classify(
            target,
            [ws.path for ws in workspace_analysis.workspaces],
        )

    # Phase 9: LLM Output Quality — poblar campos derivados
    from sourcecode.architecture_summary import ArchitectureSummarizer
    from sourcecode.summarizer import ProjectSummarizer
    from sourcecode.tree_utils import flatten_file_tree

    # LQN-01: lista plana de paths del file_tree con separador forward-slash
    sm.file_paths = [
        p.replace("\\", "/") for p in flatten_file_tree(sm.file_tree)
    ]

    # LQN-05: top-15 dependencias directas de manifest/lockfile, ordenadas por rol
    if dependency_analyzer is not None:
        from sourcecode.dependency_analyzer import _ROLE_PRIORITY

        primary_ecosystem = sm.stacks[0].stack if sm.stacks else ""
        direct_deps = [
            d for d in sm.dependencies
            if d.scope != "transitive" and d.source in {"manifest", "lockfile"}
        ]

        def _dep_sort_key(d: Any) -> tuple[int, int, str]:
            role_order = _ROLE_PRIORITY.get(d.role or "runtime", 5)
            eco_order = 0 if d.ecosystem == primary_ecosystem else 1
            return (role_order, eco_order, d.name.lower())

        sm.key_dependencies = sorted(direct_deps, key=_dep_sort_key)[:15]

    # LQN-02: resumen NL deterministico
    sm.project_summary = ProjectSummarizer(target).generate(sm)
    sm.architecture_summary = ArchitectureSummarizer(target).generate(sm)

    # Phase 13 Plan 04: Architectural Inference (--architecture flag)
    if architecture:
        from sourcecode.architecture_analyzer import ArchitectureAnalyzer
        arch_graph = module_graph  # None si --graph-modules no fue pasado
        sm.architecture = ArchitectureAnalyzer().analyze(target, sm, arch_graph)

    # Git Context (--git-context flag)
    if git_context:
        from sourcecode.git_analyzer import GitAnalyzer
        sm.git_context = GitAnalyzer().analyze(target, depth=git_depth, days=git_days)

    # Env Map (--env-map flag)
    if env_map:
        from sourcecode.env_analyzer import EnvAnalyzer
        env_records, env_summary = EnvAnalyzer().analyze(target, file_tree)
        sm = replace(sm, env_map=env_records, env_summary=env_summary)

    # Code Notes (--code-notes flag)
    if code_notes:
        from sourcecode.code_notes_analyzer import CodeNotesAnalyzer
        cn_notes, cn_adrs, cn_summary = CodeNotesAnalyzer().analyze(target)
        sm = replace(sm, code_notes=cn_notes, code_adrs=cn_adrs, code_notes_summary=cn_summary)

    # Normalize optional analyzer outputs → validate schema contracts.
    # normalize_source_map fills None fields with typed empty defaults so that
    # consumers never need to null-check architecture or module_graph.
    # validate_source_map then asserts the contracts hold; it raises here
    # (pre-serialization) rather than silently producing invalid JSON.
    sm = normalize_source_map(sm)
    validate_source_map(sm)

    # Cross-analyzer semantic consistency (non-blocking: warnings to stderr).
    # strict=False so a mismatched dependency or orphan semantic link never
    # aborts a run — findings are informational until the team decides to harden.
    for _finding in validate_cross_analyzer_consistency(sm, strict=False):
        typer.echo(f"[consistency] {_finding}", err=True)

    # Build confidence summary + analysis gaps (always runs, lightweight)
    from sourcecode.confidence_analyzer import ConfidenceAnalyzer
    from dataclasses import replace as _replace
    _conf_summary, _analysis_gaps = ConfidenceAnalyzer().analyze(sm)
    sm = _replace(sm, confidence_summary=_conf_summary, analysis_gaps=_analysis_gaps)

    # E2E pipeline coherence check — emits [coherence] warnings to stderr.
    # Catches contradictory states that can survive individual-analyzer validation.
    for _issue in _check_pipeline_coherence(sm):
        typer.echo(_issue, err=True)

    # Build pipeline trace when --trace-pipeline is set.
    if trace_pipeline:
        _trace = _TraceCollector(enabled=True)
        _trace.emit("scan", "scanner", "complete",
                    reason=f"{len(sm.file_paths)} files, {len(manifests)} manifests")
        for _s in sm.stacks:
            _trace.emit("detect", _s.produced_by or "unknown", "emit_stack",
                        target=_s.stack,
                        reason=f"method={_s.detection_method} confidence={_s.confidence}")
        for _ep in sm.entry_points:
            _trace.emit("detect", _ep.produced_by or "unknown", "emit_ep",
                        target=_ep.path,
                        reason=f"type={_ep.entrypoint_type} confidence={_ep.confidence} reason={_ep.reason}")
        # Record EPs filtered from agent_view (benchmark/example with path-auxiliary parts)
        _aux_parts = frozenset({
            "benchmark", "benchmarks", "bench", "demo", "demos",
            "example", "examples", "docs", "doc", "fixtures", "fixture",
        })
        for _ep in sm.entry_points:
            _normalized_ep = normalize_entry_point(_ep)
            _ep_type = _normalized_ep.entrypoint_type
            _path_parts = _ep.path.replace("\\", "/").lower().split("/")
            _filtered = (
                _normalized_ep.classification != "production"
                or any(p in _aux_parts for p in _path_parts)
            )
            if _filtered:
                _trace.emit("output", "agent_view", "filter_ep",
                            target=_ep.path,
                            reason=f"entrypoint_type={_ep_type} (auxiliary)")
        if sm.confidence_summary is not None:
            _cs = sm.confidence_summary
            _trace.emit("confidence", "confidence_analyzer", "computed",
                        reason=(
                            f"overall={_cs.overall} "
                            f"stack={_cs.stack_confidence} "
                            f"ep={_cs.entry_point_confidence} "
                            f"anomalies={len(_cs.anomalies)}"
                        ))
        sm = _replace(sm, pipeline_trace=_trace.build_trace())

    # 4. Serializar
    if agent:
        data = agent_view(sm)
        if not no_redact:
            data = redact_dict(data)
        content = json.dumps(data, indent=2, ensure_ascii=False)
    elif compact:
        data = compact_view(sm, no_tree=no_tree)
        if not no_redact:
            data = redact_dict(data)
        content = json.dumps(data, indent=2, ensure_ascii=False)
    else:
        raw_dict = standard_view(sm, include_tree=tree and not no_tree)
        if not no_redact:
            raw_dict = redact_dict(raw_dict)

        if format == "yaml":
            from io import StringIO

            from ruamel.yaml import YAML

            yaml = YAML()
            yaml.default_flow_style = False
            yaml.representer.add_representer(
                type(None),
                lambda dumper, data_val: dumper.represent_scalar(
                    "tag:yaml.org,2002:null", "null"
                ),
            )
            stream = StringIO()
            yaml.dump(raw_dict, stream)
            content = stream.getvalue()
        else:
            content = json.dumps(raw_dict, indent=2, ensure_ascii=False)

    # 5. Telemetry (fire-and-forget, never blocks)
    try:
        from sourcecode import telemetry as _tel
        _tel.record(
            "execution_completed",
            cmd="analyze",
            flags=_active_flags(
                dependencies, graph_modules, docs, full_metrics,
                semantics, architecture, git_context, env_map,
                code_notes, agent, compact, tree, no_redact, format,
            ),
            output_fmt=format,
            file_count=len(sm.file_paths),
            duration_s=time.monotonic() - _t0,
            success=True,
        )
    except Exception:
        pass

    # 6. Escribir output (CLI-04)
    write_output(content, output=output)


@app.command("prepare-context")
def prepare_context_cmd(
    task: Optional[str] = typer.Argument(
        None,
        help="Task: explain | fix-bug | refactor | generate-tests | onboard | review-pr | delta",
    ),
    path: Path = typer.Argument(
        Path("."),
        help="Repository path to analyze (default: current directory)",
    ),
    since: Optional[str] = typer.Option(
        None,
        "--since",
        help="Git ref for delta task (e.g. HEAD~3, main)",
    ),
    llm_prompt: bool = typer.Option(
        False,
        "--llm-prompt",
        help="Append a ready-to-use LLM prompt to the output",
    ),
    task_help: bool = typer.Option(
        False,
        "--task-help",
        help="List available tasks with descriptions and exit",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show what would be analyzed without running it",
    ),
) -> None:
    """Task-specific context for AI coding agents.

    \b
    Tasks:
      explain        Architecture, entry points, key dependencies
      fix-bug        Risk-ranked files, suspected areas, annotations
      refactor       Structural issues, improvement opportunities
      generate-tests Untested source files, test gap analysis
      onboard        Full project context for new agents/developers
      review-pr      Changed files + architectural impact
      delta          Incremental context: git-changed files only

    \b
    Examples:
      sourcecode prepare-context explain
      sourcecode prepare-context explain /path/to/repo
      sourcecode prepare-context fix-bug
      sourcecode prepare-context delta --since main
      sourcecode prepare-context onboard --llm-prompt
      sourcecode prepare-context --task-help
    """
    from sourcecode.prepare_context import TASKS, TaskContextBuilder

    if task_help:
        typer.echo("Available tasks:\n")
        for name, spec in TASKS.items():
            typer.echo(f"  {name:<20} {spec.description}")
            typer.echo(f"  {'':20} Output: {spec.output_hint}\n")
        raise typer.Exit()

    if task is None:
        typer.echo(
            f"Error: task is required. Available: {', '.join(TASKS)}\n"
            "Use --task-help for descriptions.",
            err=True,
        )
        raise typer.Exit(code=1)

    if task not in TASKS:
        typer.echo(
            f"Error: unknown task '{task}'. Available: {', '.join(TASKS)}",
            err=True,
        )
        raise typer.Exit(code=1)

    target = path.resolve()
    if not target.exists() or not target.is_dir():
        typer.echo(f"Error: '{target}' no es un directorio válido.", err=True)
        raise typer.Exit(code=1)

    if dry_run:
        spec = TASKS[task]
        typer.echo(f"task:        {task}")
        typer.echo(f"goal:        {spec.goal}")
        typer.echo(f"path:        {target}")
        typer.echo(f"analyzers:   dependencies={'yes' if spec.enable_dependencies else 'no'}"
                   f", code_notes={'yes' if spec.enable_code_notes else 'no'}")
        if since:
            typer.echo(f"since:       {since}")
        typer.echo(f"output:      {spec.output_hint}")
        raise typer.Exit()

    from dataclasses import asdict

    builder = TaskContextBuilder(target)
    output = builder.build(task, since=since)

    out: dict[str, Any] = {
        "task": output.task,
        "goal": output.goal,
        "project_summary": output.project_summary,
        "architecture_summary": output.architecture_summary,
        "confidence": output.confidence,
        "relevant_files": [asdict(f) for f in output.relevant_files],
        "why_these_files": output.why_these_files,
        "key_dependencies": output.key_dependencies,
    }
    if output.gaps:
        out["gaps"] = output.gaps
    if output.suspected_areas:
        out["suspected_areas"] = output.suspected_areas
    if output.improvement_opportunities:
        out["improvement_opportunities"] = output.improvement_opportunities
    if output.test_gaps:
        out["test_gaps"] = output.test_gaps
    if output.code_notes_summary:
        out["code_notes_summary"] = output.code_notes_summary
    if output.changed_files:
        out["changed_files"] = output.changed_files
    if output.affected_entry_points:
        out["affected_entry_points"] = output.affected_entry_points
    if output.limitations:
        out["limitations"] = output.limitations
    if llm_prompt:
        out["llm_prompt"] = builder.render_prompt(output)

    typer.echo(json.dumps(out, indent=2, ensure_ascii=False))


# ── Telemetry commands ────────────────────────────────────────────────────────

@telemetry_app.command("status")
def telemetry_status() -> None:
    """Show current telemetry setting."""
    from sourcecode.telemetry.config import config_file_path, has_been_asked, is_enabled
    enabled = is_enabled()
    asked = has_been_asked()
    status = "enabled" if enabled else "disabled"
    typer.echo(f"Telemetry: {status}")
    if not asked:
        typer.echo("  (consent not yet shown — will prompt on next run)")
    typer.echo(f"  Config: {config_file_path()}")
    typer.echo("  Disable permanently: sourcecode telemetry disable")
    typer.echo("  Or set env var:      SOURCECODE_TELEMETRY=0")


@telemetry_app.command("enable")
def telemetry_enable() -> None:
    """Opt in to anonymous telemetry."""
    from sourcecode.telemetry.config import set_enabled
    from sourcecode import telemetry as _tel
    set_enabled(True)
    typer.echo("Telemetry enabled. Thank you — this helps improve sourcecode.")
    typer.echo("What is collected: version, OS, commands, flags, duration, repo size range, errors.")
    typer.echo("What is never collected: source code, paths, secrets, or any output content.")
    typer.echo("Disable at any time: sourcecode telemetry disable")
    _tel.record("telemetry_enabled", cmd="telemetry")


@telemetry_app.command("disable")
def telemetry_disable() -> None:
    """Opt out of anonymous telemetry."""
    from sourcecode.telemetry.config import set_enabled
    set_enabled(False)
    typer.echo("Telemetry disabled. No data will be collected or sent.")
    typer.echo("Re-enable at any time: sourcecode telemetry enable")


# ── version ───────────────────────────────────────────────────────────────────

@app.command("version")
def version_cmd() -> None:
    """Show version and exit."""
    typer.echo(f"sourcecode {__version__}")


# ── config ────────────────────────────────────────────────────────────────────

@app.command("config")
def config_cmd() -> None:
    """Show current configuration."""
    from sourcecode.telemetry.config import config_file_path, is_enabled
    typer.echo(f"sourcecode {__version__}")
    typer.echo(f"Config:    {config_file_path()}")
    typer.echo(f"Telemetry: {'enabled' if is_enabled() else 'disabled'}")
    typer.echo("")
    typer.echo("Manage telemetry:")
    typer.echo("  sourcecode telemetry enable")
    typer.echo("  sourcecode telemetry disable")
    typer.echo("  sourcecode telemetry status")


# ── analyze (legacy alias) ────────────────────────────────────────────────────

@app.command("analyze", hidden=True)
def analyze_cmd(
    path: Path = typer.Argument(Path("."), help="Repository path to analyze"),
) -> None:
    """[deprecated] Use: sourcecode [PATH]"""
    typer.echo(
        "Warning: 'analyze' subcommand is deprecated.\n"
        "Use:  sourcecode .\n"
        "      sourcecode /path/to/repo",
        err=True,
    )
    raise typer.Exit(code=1)


# ── Entry point ───────────────────────────────────────────────────────────────

def main_entry() -> None:
    """CLI entry point.

    Calls _preprocess_argv() before Typer/Click parses sys.argv so that
    repository path tokens are extracted before Click's Group callback
    can consume them as positional arguments (which would prevent subcommand
    routing for tokens like 'version' or 'config').
    """
    _preprocess_argv()
    app()

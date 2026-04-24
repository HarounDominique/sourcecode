from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional, cast

import typer

from sourcecode import __version__

app = typer.Typer(
    name="sourcecode",
    help="Genera un mapa de contexto estructurado del proyecto para agentes IA.",
    add_completion=False,
)

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
    path: Path = typer.Argument(Path("."), help="Directorio a analizar (default: directorio actual)"),
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
        help="Incluir metricas de calidad: LOC, simbolos, complejidad, tests y cobertura por fichero",
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
) -> None:
    """Genera un mapa de contexto estructurado del proyecto en formato JSON o YAML."""
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

    # Resolver y validar path
    target = path.resolve()
    if not target.exists():
        typer.echo(f"Error: el directorio '{target}' no existe.", err=True)
        raise typer.Exit(code=1)
    if not target.is_dir():
        typer.echo(f"Error: '{target}' no es un directorio.", err=True)
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
    from sourcecode.serializer import compact_view, normalize_source_map, validate_cross_analyzer_consistency, validate_source_map, write_output
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
    metadata = AnalysisMetadata(analyzed_path=str(target))
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

    # Phase 9: LLM Output Quality — poblar campos derivados
    from sourcecode.architecture_summary import ArchitectureSummarizer
    from sourcecode.summarizer import ProjectSummarizer
    from sourcecode.tree_utils import flatten_file_tree

    # LQN-01: lista plana de paths del file_tree con separador forward-slash
    sm.file_paths = [
        p.replace("\\", "/") for p in flatten_file_tree(sm.file_tree)
    ]

    # LQN-05: top-15 dependencias directas de manifest/lockfile
    if dependency_analyzer is not None:
        primary_ecosystem = sm.stacks[0].stack if sm.stacks else ""
        direct_deps = [
            d for d in sm.dependencies
            if d.scope != "transitive" and d.source in {"manifest", "lockfile"}
        ]

        def _dep_sort_key(d: Any) -> tuple[int, str]:
            return (0 if d.ecosystem == primary_ecosystem else 1, d.name.lower())

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

    # 4. Serializar (con o sin modo compact)
    if compact:
        data = compact_view(sm)
        # Aplicar redaccion sobre el dict del compact view
        if not no_redact:
            data = redact_dict(data)
        content = json.dumps(data, indent=2, ensure_ascii=False)
    else:
        # Redactar sobre el dict serializado (SEC-01, SEC-03)
        raw_dict = asdict(sm)
        if not no_redact:
            raw_dict = redact_dict(raw_dict)

        if format == "yaml":
            # Para YAML, serializar el dict directamente con ruamel.yaml
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

    # 5. Escribir output (CLI-04)
    write_output(content, output=output)


@app.command("prepare-context")
def prepare_context_cmd(
    task: str = typer.Argument(..., help="Descripción de la tarea"),
    path: Path = typer.Option(".", "--path", "-p", help="Directorio del proyecto"),
) -> None:
    """Prepara contexto mínimo optimizado para que un LLM modifique el código."""
    from dataclasses import asdict

    from sourcecode.prepare_context import ContextBuilder

    target = path.resolve()

    if not target.exists() or not target.is_dir():
        typer.echo(f"Error: '{target}' no es un directorio válido.", err=True)
        raise typer.Exit(code=1)

    builder = ContextBuilder(target)
    result = builder.prepare(task)

    out = {
        "task": result.task,
        "entry_points": result.entry_points,
        "relevant_files": [asdict(f) for f in result.relevant_files],
        "call_flow": result.call_flow,
        "snippets": [asdict(s) for s in result.snippets],
        "tests": result.tests,
        "notes": result.notes,
    }

    typer.echo(json.dumps(out, indent=2, ensure_ascii=False))

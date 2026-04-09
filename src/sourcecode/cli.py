"""CLI de sourcecode — interfaz de linea de comandos principal."""
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
        help="Output reducido (~500 tokens): tipo de proyecto, stacks, entradas y arbol nivel 1",
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
    from sourcecode.redactor import SecretRedactor, redact_dict
    from sourcecode.scanner import FileScanner
    from sourcecode.schema import AnalysisMetadata, DocsDepth, EntryPoint, SourceMap, StackDetection
    from sourcecode.serializer import compact_view, write_output
    from sourcecode.workspace import WorkspaceAnalyzer

    # 1. Escanear el directorio (SCAN-01 a SCAN-05)
    redactor = SecretRedactor(enabled=not no_redact)
    scanner = FileScanner(target, max_depth=depth)
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
    manifests = scanner.find_manifests()
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
    doc_records: list = []
    doc_summaries: list = []
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
        )
        doc_records.extend(root_doc_records)
        doc_summaries.append(root_doc_summary)

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
            )
            doc_records.extend(workspace_doc_records)
            doc_summaries.append(workspace_doc_summary)

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
    )

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

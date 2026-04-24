from __future__ import annotations

"""Serializer de sourcecode — JSON canonico, YAML y modo compact.

Patrones criticos:
  - SIEMPRE pasar por dataclasses.asdict() antes de json.dumps (json no serializa dataclasses)
  - ruamel.yaml con representer para null canonico (no ~)
  - compact_view() proyecta solo los campos necesarios (~500 tokens)
"""

import json
import sys
from dataclasses import asdict, dataclass, is_dataclass, replace
from io import StringIO
from pathlib import Path
from typing import Any, Optional

from sourcecode.schema import (
    ArchitectureAnalysis,
    ModuleGraph,
    ModuleGraphSummary,
    SourceMap,
)


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
    project_summary (siempre), architecture_summary (siempre),
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
        dep_summary_dict.pop("dependencies", None)

    env_summary_dict: Any = None
    if sm.env_summary is not None and sm.env_summary.requested:
        env_summary_dict = asdict(sm.env_summary)

    code_notes_summary_dict: Any = None
    if sm.code_notes_summary is not None and sm.code_notes_summary.requested:
        code_notes_summary_dict = asdict(sm.code_notes_summary)

    return {
        "schema_version": sm.metadata.schema_version,
        "project_type": sm.project_type,
        "project_summary": sm.project_summary,
        "architecture_summary": sm.architecture_summary,
        "stacks": [asdict(stack) for stack in sm.stacks],
        "entry_points": [asdict(entry_point) for entry_point in sm.entry_points],
        "file_tree_depth1": depth1,
        "dependency_summary": dep_summary_dict,
        "env_summary": env_summary_dict,
        "code_notes_summary": code_notes_summary_dict,
    }


def normalize_source_map(sm: SourceMap) -> SourceMap:
    """Fill in typed empty defaults for optional analyzer fields.

    Fields controlled by flags (--architecture, --graph-modules) are None when
    the flag is absent.  Downstream consumers and tests then need null-checks
    everywhere.  This layer converts None → a well-typed default so the output
    schema is always structurally complete.

    The ``requested=False`` sentinel on each default tells consumers the
    analysis was not requested, without forcing them to branch on None.
    """
    changes: dict[str, Any] = {}

    # architecture: always an ArchitectureAnalysis, never None
    if sm.architecture is None:
        changes["architecture"] = ArchitectureAnalysis(requested=False)

    # module_graph: always a ModuleGraph (possibly empty), never None.
    # module_graph_summary is kept in sync as a convenience field.
    if sm.module_graph is None:
        empty_graph = ModuleGraph(summary=ModuleGraphSummary(requested=False))
        changes["module_graph"] = empty_graph
        if sm.module_graph_summary is None:
            changes["module_graph_summary"] = empty_graph.summary
    elif sm.module_graph_summary is None:
        # graph exists but summary was never set — sync it
        changes["module_graph_summary"] = sm.module_graph.summary

    # dependencies is already list[DependencyRecord] by default_factory, but
    # guard against any future refactor that could accidentally set it to None
    if sm.dependencies is None:  # type: ignore[comparison-overlap]
        changes["dependencies"] = []

    return replace(sm, **changes) if changes else sm


def validate_source_map(sm: SourceMap) -> None:
    """Assert structural schema contracts on a (already normalised) SourceMap.

    Call this *after* normalize_source_map() so that the checks below catch
    bugs in the normaliser itself or in code that bypasses it.

    Raises:
        ValueError: listing every violated contract, never just the first.
    """
    errors: list[str] = []

    # --- architecture ---
    if sm.architecture is None:
        errors.append("architecture must not be null (call normalize_source_map first)")
    else:
        if not isinstance(sm.architecture.domains, list):
            errors.append(
                f"architecture.domains must be list, got {type(sm.architecture.domains).__name__}"
            )
        if sm.architecture.confidence not in ("high", "medium", "low"):
            errors.append(
                f"architecture.confidence must be high|medium|low, "
                f"got {sm.architecture.confidence!r}"
            )

    # --- module_graph ---
    if sm.module_graph is None:
        errors.append("module_graph must not be null (call normalize_source_map first)")
    else:
        if not isinstance(sm.module_graph.nodes, list):
            errors.append(
                f"module_graph.nodes must be list, got {type(sm.module_graph.nodes).__name__}"
            )
        if not isinstance(sm.module_graph.edges, list):
            errors.append(
                f"module_graph.edges must be list, got {type(sm.module_graph.edges).__name__}"
            )

    # --- dependencies ---
    if not isinstance(sm.dependencies, list):
        errors.append(
            f"dependencies must be list, got {type(sm.dependencies).__name__}"
        )

    if errors:
        bullet = "\n  - "
        raise ValueError(
            f"SourceMap schema violations ({len(errors)}):{bullet}"
            + bullet.join(errors)
        )


_GRAPH_CODE_EXTENSIONS: frozenset[str] = frozenset({
    ".py", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs",
    ".go", ".rs", ".java", ".kt", ".rb",
})


def _rule_dependency_graph(
    sm: SourceMap,
    known_paths: set[str],
    findings: list[str],
) -> None:
    """Rule 1 — dependency/graph consistency.

    Sub-rule 1a: every GraphNode whose path ends with a code extension must
    exist in file_tree.  A mismatch means the graph references phantom files
    the scanner never found.

    Sub-rule 1b: if the graph has *external*-looking edge targets (package
    names without path separators), every non-transitive dependency should
    appear in those targets.  This rule is intentionally skipped when the
    graph only tracks internal module-to-module edges, avoiding false
    positives for projects whose graph_analyzer does not emit external imports.
    """
    if not sm.module_graph.summary.requested:
        return

    # 1a — graph node paths must be in file_tree
    for node in sm.module_graph.nodes:
        p = node.path
        if Path(p).suffix.lower() in _GRAPH_CODE_EXTENSIONS and p not in known_paths:
            findings.append(
                f"[dependency_graph] graph node '{node.id}' path '{p}' "
                f"not found in file_tree"
            )

    # 1b — dep names should appear in external-facing edge targets
    if sm.dependency_summary is None or not sm.dependency_summary.requested:
        return
    if not sm.module_graph.edges:
        return

    # Only apply when the graph contains at least one package-like target
    # (no path separator, no code extension) — signals external import tracking.
    pkg_targets = {
        e.target.lower()
        for e in sm.module_graph.edges
        if "/" not in e.target
        and not Path(e.target).suffix.lower() in _GRAPH_CODE_EXTENSIONS
    }
    if not pkg_targets:
        return  # graph has only internal file edges; dep↔edge check not applicable

    for dep in sm.dependencies:
        if dep.scope == "transitive":
            continue
        name = dep.name.lower().replace("-", "_")
        if not any(name in t.replace("-", "_") for t in pkg_targets):
            sample = ", ".join(sorted(pkg_targets)[:3])
            findings.append(
                f"[dependency_graph] dependency '{dep.name}' declared in manifest "
                f"but absent from module_graph external edges (visible: {sample})"
            )


def _rule_semantic_file_tree(
    sm: SourceMap,
    known_paths: set[str],
    findings: list[str],
) -> None:
    """Rule 2 — semantic_links paths must exist in file_tree.

    Both the importer and the source (when not external) must be files the
    scanner actually found.  An orphan path means the semantic_analyzer
    resolved a symbol to a file that does not belong to the project.
    """
    for link in sm.semantic_links:
        if link.importer_path not in known_paths:
            findings.append(
                f"[semantic_file_tree] semantic_link importer "
                f"'{link.importer_path}' not in file_tree"
            )
        if link.source_path is not None and not link.is_external:
            if link.source_path not in known_paths:
                findings.append(
                    f"[semantic_file_tree] semantic_link source "
                    f"'{link.source_path}' not in file_tree (is_external=False)"
                )


def _rule_architecture_graph(
    sm: SourceMap,
    known_paths: set[str],
    findings: list[str],
) -> None:
    """Rule 3 — architecture domain files must be a subset of file_tree.

    The architecture_analyzer clusters files into domains.  Every file it
    assigns to a domain should be a file the scanner found.  A mismatch
    means the architecture_analyzer is referencing phantom paths, likely
    from a stale file_paths list or a mis-configured root.
    """
    if not sm.architecture.requested:
        return
    for domain in sm.architecture.domains:
        for path in domain.files:
            if path not in known_paths:
                findings.append(
                    f"[architecture_graph] domain '{domain.name}' "
                    f"references '{path}' not in file_tree"
                )


def validate_cross_analyzer_consistency(
    sm: SourceMap,
    *,
    strict: bool = False,
) -> list[str]:
    """Check semantic alignment across analyzer outputs.

    Applies three rules (see helpers above):
      Rule 1 — dependency/graph: graph node paths and external edge targets
               must be consistent with declared dependencies and file_tree.
      Rule 2 — semantic/file_tree: SymbolLink paths must exist in file_tree.
      Rule 3 — architecture/graph: domain files must exist in file_tree.

    Args:
        sm:     A SourceMap that has already been normalised and structurally
                validated (call normalize_source_map + validate_source_map first).
        strict: If True, raises ValueError listing all findings.
                If False (default), returns the findings list so the caller
                can log warnings without aborting the pipeline.

    Returns:
        List of human-readable finding strings (empty when all rules pass).
    """
    findings: list[str] = []
    known = set(sm.file_paths)

    _rule_dependency_graph(sm, known, findings)
    _rule_semantic_file_tree(sm, known, findings)
    _rule_architecture_graph(sm, known, findings)

    if strict and findings:
        bullet = "\n  - "
        raise ValueError(
            f"Cross-analyzer consistency violations ({len(findings)}):{bullet}"
            + bullet.join(findings)
        )

    return findings


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

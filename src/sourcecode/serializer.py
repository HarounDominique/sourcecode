from __future__ import annotations

"""sourcecode serializer — canonical JSON, YAML, and compact mode.

Critical patterns:
  - Always pass through dataclasses.asdict() before json.dumps (json does not serialize dataclasses)
  - ruamel.yaml with representer for canonical null (not ~)
  - compact_view() projects only required fields (~500 tokens)
"""

import json
import sys
from dataclasses import asdict, dataclass, is_dataclass, replace
from io import StringIO
from pathlib import Path
from typing import Any, Optional

from sourcecode.entrypoint_classifier import normalize_entry_point, is_production_entry_point
from sourcecode.file_classifier import FileClassifier
from sourcecode.schema import (
    ArchitectureAnalysis,
    ModuleGraph,
    ModuleGraphSummary,
    SourceMap,
)


def to_json(sm: SourceMap | dict[str, Any], indent: int = 2) -> str:
    """Serialize SourceMap or dict to canonical JSON.

    Accepts a SourceMap (dataclass) or an already-prepared dict (e.g. compact_view()).
    Uses dataclasses.asdict() to convert dataclasses before json.dumps.
    ensure_ascii=False to preserve UTF-8 in paths.
    """
    data = asdict(sm) if is_dataclass(sm) and not isinstance(sm, type) else sm
    return json.dumps(data, indent=indent, ensure_ascii=False)


def to_yaml(sm: SourceMap) -> str:
    """Serialize SourceMap to YAML using ruamel.yaml.

    ruamel.yaml preserves key order and serializes None as null
    (not as ~) with the default dict dump configuration.
    """
    from ruamel.yaml import YAML

    yaml = YAML()
    yaml.default_flow_style = False
    # Ensure None is serialized as 'null', not '~'
    yaml.representer.add_representer(
        type(None),
        lambda dumper, data: dumper.represent_scalar("tag:yaml.org,2002:null", "null"),
    )
    stream = StringIO()
    yaml.dump(asdict(sm), stream)
    return stream.getvalue()


def _clean_entry_point(ep: Any) -> dict[str, Any]:
    normalized = normalize_entry_point(ep)
    return {
        k: v
        for k, v in asdict(normalized).items()
        if v is not None and v != "" and k != "workspace"
    }


def _entry_point_groups(entry_points: list[Any]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {
        "production": [],
        "development": [],
        "auxiliary": [],
    }
    for ep in entry_points:
        normalized = normalize_entry_point(ep)
        item = _clean_entry_point(normalized)
        if is_production_entry_point(normalized):
            groups["production"].append(item)
        elif normalized.classification == "development":
            groups["development"].append(item)
        else:
            groups["auxiliary"].append(item)

    groups["production"].sort(key=lambda ep: (ep.get("runtime_relevance") != "high", ep.get("path", "")))
    groups["development"].sort(key=lambda ep: ep.get("path", ""))
    groups["auxiliary"].sort(key=lambda ep: ep.get("path", ""))
    return groups


_PRODUCTION_DEP_ROLES = {"runtime", "parsing", "serialization", "observability", "infra"}
_DEV_DEP_ROLES = {"devtool"}
_TEST_DEP_ROLES = {"testtool"}
_BUILD_DEP_ROLES = {"buildtool"}


def _dependency_groups(sm: SourceMap) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {
        "production_dependencies": [],
        "dev_tools": [],
        "test_utilities": [],
        "build_tooling": [],
        "noise_dependencies": [],
        "suspicious_dependencies": [],
    }
    if sm.dependency_summary is None or not sm.dependency_summary.requested:
        return groups

    root = Path(sm.metadata.analyzed_path) if sm.metadata.analyzed_path else Path(".")
    import_index = _dependency_import_index(root, sm.file_paths)

    for dep in sm.dependency_summary.dependencies:
        if dep.scope == "transitive":
            continue
        item = {
            k: v for k, v in asdict(dep).items()
            if v is not None and k not in {"parent"}
        }
        role = dep.role or "unknown"
        scope = dep.scope
        name_key = _dep_import_key(dep.name)

        if role in _PRODUCTION_DEP_ROLES and scope not in {"dev"}:
            groups["production_dependencies"].append(item)
            if dep.source == "manifest" and name_key not in import_index:
                suspect = dict(item)
                suspect["reason"] = "declared as production dependency but no static import observed"
                groups["suspicious_dependencies"].append(suspect)
        elif role in _TEST_DEP_ROLES:
            groups["test_utilities"].append(item)
        elif role in _BUILD_DEP_ROLES:
            groups["build_tooling"].append(item)
        elif role in _DEV_DEP_ROLES or scope in {"dev", "optional"}:
            groups["dev_tools"].append(item)
        else:
            groups["noise_dependencies"].append(item)

    for values in groups.values():
        values.sort(key=lambda d: (d.get("ecosystem", ""), d.get("name", "")))
    return groups


def _dependency_import_index(root: Path, file_paths: list[str]) -> set[str]:
    import re

    index: set[str] = set()
    import_re = re.compile(
        r"(?:from\s+([A-Za-z0-9_@./-]+)\s+import|import\s+([A-Za-z0-9_@./-]+)|"
        r"require\(['\"]([^'\"]+)['\"]\)|from\s+['\"]([^'\"]+)['\"])",
        re.MULTILINE,
    )
    for path in file_paths[:2000]:
        if Path(path).suffix.lower() not in {".py", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs"}:
            continue
        try:
            content = (root / path).read_text(encoding="utf-8", errors="replace")[:20000]
        except OSError:
            continue
        for match in import_re.findall(content):
            raw = next((part for part in match if part), "")
            if raw and not raw.startswith("."):
                index.add(_dep_import_key(raw))
    return index


def _dep_import_key(name: str) -> str:
    lowered = name.lower()
    if lowered.startswith("@"):
        parts = lowered.split("/")
        return "/".join(parts[:2])
    return lowered.split("/")[0].replace("_", "-")


def _file_relevance(sm: SourceMap, *, limit: int = 15) -> list[dict[str, Any]]:
    root = Path(sm.metadata.analyzed_path) if sm.metadata.analyzed_path else Path(".")
    classifier = FileClassifier(root, sm.entry_points, sm.monorepo_packages)
    items = classifier.classify_paths(sm.file_paths, limit=limit)
    return [asdict(item) for item in items]


def _architecture_context(sm: SourceMap) -> dict[str, Any]:
    arch = sm.architecture
    if arch is not None and arch.requested:
        pattern = arch.pattern if arch.pattern not in (None, "unknown", "flat") else "no confirmed architecture pattern; inferred partial layering"
        return {
            "summary": sm.architecture_summary,
            "pattern": pattern,
            "confidence": arch.confidence,
            "method": arch.method,
            "layers": [
                {
                    "name": layer.name,
                    "confidence": layer.confidence,
                    "file_count": len(layer.files),
                }
                for layer in arch.layers
            ],
            "limitations": arch.limitations,
        }
    return {
        "summary": sm.architecture_summary,
        "pattern": "no confirmed architecture pattern; inferred partial layering",
        "confidence": "low",
        "method": "not_requested",
        "limitations": [
            "architecture analyzer not requested; summary limited to stack, filesystem and entrypoint evidence"
        ],
    }


def _section_confidence(sm: SourceMap) -> dict[str, str]:
    cs = sm.confidence_summary
    dep_conf = "low"
    if sm.dependency_summary is not None and sm.dependency_summary.requested:
        dep_conf = "medium"
        if sm.dependency_summary.sources and sm.dependency_summary.total_count > 0:
            dep_conf = "high"
    arch_conf = "low"
    if sm.architecture is not None and sm.architecture.requested:
        arch_conf = sm.architecture.confidence
    file_conf = "medium" if sm.file_paths else "low"
    return {
        "stack": cs.stack_confidence if cs else "low",
        "entrypoints": cs.entry_point_confidence if cs else "low",
        "dependencies": dep_conf,
        "architecture": arch_conf,
        "file_relevance": file_conf,
    }


def compact_view(sm: SourceMap, *, no_tree: bool = False) -> dict[str, Any]:
    """Context package ready for prompt or handoff (~600-800 tokens).

    Answers: what it is, where it enters, what depends on what,
    what signals matter, and what uncertainty exists.

    Includes: project_type, project_summary, architecture_summary,
    stacks, entry_points, dependency_summary + key_dependencies (when analyzed),
    env_summary (when analyzed), code_notes_summary (when analyzed),
    confidence_summary, anomalies, analysis_gaps.

    Excludes: file_tree, raw dependency lists, docs, module_graph.
    Empty sections are explained when relevant.
    """
    dep_summary_dict: Any = None
    key_deps: Any = None
    if sm.dependency_summary is not None and sm.dependency_summary.requested:
        dep_summary_dict = asdict(sm.dependency_summary)
        dep_summary_dict.pop("dependencies", None)
        key_deps = [
            asdict(d) for d in sm.key_dependencies
            if (d.role or "unknown") in _PRODUCTION_DEP_ROLES and d.scope not in {"dev"}
        ]
    elif sm.dependency_summary is None or not sm.dependency_summary.requested:
        dep_summary_dict = None  # "not analyzed" — agent should add --dependencies

    env_summary_dict: Any = None
    if sm.env_summary is not None and sm.env_summary.requested:
        env_summary_dict = asdict(sm.env_summary)

    code_notes_summary_dict: Any = None
    if sm.code_notes_summary is not None and sm.code_notes_summary.requested:
        code_notes_summary_dict = asdict(sm.code_notes_summary)

    # Entry points: production runtime only. Auxiliary and development entries
    # are exposed separately so agents do not mix tooling with execution paths.
    ep_groups = _entry_point_groups(sm.entry_points)
    entry_points_compact = ep_groups["production"]
    if not entry_points_compact:
        entry_points_compact = []  # truth signal: no production runtime detected

    # Confidence summary
    conf_dict: Any = None
    anomalies: Any = None
    if sm.confidence_summary is not None:
        conf_dict = asdict(sm.confidence_summary)
        if sm.confidence_summary.anomalies:
            anomalies = sm.confidence_summary.anomalies

    # Analysis gaps
    gaps_list: Any = None
    if sm.analysis_gaps:
        gaps_list = [asdict(g) for g in sm.analysis_gaps]

    context_summary_dict: Any = None
    if sm.context_summary is not None and sm.context_summary.requested:
        context_summary_dict = asdict(sm.context_summary)

    result: dict[str, Any] = {
        "schema_version": sm.metadata.schema_version,
        "project_type": sm.project_type,
        "project_summary": sm.project_summary,
        "architecture_summary": sm.architecture_summary,
        "context_summary": context_summary_dict,
        "stacks": [asdict(stack) for stack in sm.stacks],
        "entry_points": entry_points_compact,
        "development_entry_points": ep_groups["development"] or None,
        "auxiliary_entry_points": ep_groups["auxiliary"] or None,
        "dependency_summary": dep_summary_dict,
        "key_dependencies": key_deps,
        "env_summary": env_summary_dict,
        "code_notes_summary": code_notes_summary_dict,
        "confidence_summary": conf_dict,
        "anomalies": anomalies,
        "analysis_gaps": gaps_list,
    }
    # Strip keys that are fully None and not informative
    return {k: v for k, v in result.items() if v is not None or k in (
        "project_type", "project_summary", "architecture_summary",
        "dependency_summary", "confidence_summary",
    )}


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

    normalized_eps = [normalize_entry_point(ep) for ep in sm.entry_points]
    if normalized_eps != sm.entry_points:
        changes["entry_points"] = normalized_eps

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

    # 1a — graph node paths must be in file_tree (aggregate)
    phantom_paths = [
        node.path
        for node in sm.module_graph.nodes
        if Path(node.path).suffix.lower() in _GRAPH_CODE_EXTENSIONS
        and node.path not in known_paths
    ]
    if phantom_paths:
        sample = ", ".join(phantom_paths[:3])
        findings.append(
            f"[dependency_graph] {len(phantom_paths)} graph node(s) reference paths "
            f"not in file_tree: {sample}"
            + (f" (+{len(phantom_paths) - 3} more)" if len(phantom_paths) > 3 else "")
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

    missing_deps = [
        dep.name
        for dep in sm.dependencies
        if dep.scope != "transitive"
        and not any(
            dep.name.lower().replace("-", "_") in t.replace("-", "_")
            for t in pkg_targets
        )
    ]
    if missing_deps:
        sample = ", ".join(missing_deps[:5])
        findings.append(
            f"[dependency_graph] {len(missing_deps)} manifest dep(s) absent from "
            f"graph external edges: {sample}"
            + (f" (+{len(missing_deps) - 5} more)" if len(missing_deps) > 5 else "")
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
    importer_miss_paths = [
        link.importer_path
        for link in sm.semantic_links
        if link.importer_path not in known_paths
    ]
    source_miss_paths = [
        link.source_path
        for link in sm.semantic_links
        if link.source_path is not None
        and not link.is_external
        and link.source_path not in known_paths
    ]
    total = len(importer_miss_paths) + len(source_miss_paths)
    if total > 0:
        parts: list[str] = []
        if importer_miss_paths:
            sample = ", ".join(dict.fromkeys(importer_miss_paths[:2]))
            parts.append(f"{len(importer_miss_paths)} importer(s) (e.g. {sample})")
        if source_miss_paths:
            sample = ", ".join(dict.fromkeys(source_miss_paths[:2]))
            parts.append(f"{len(source_miss_paths)} source(s) (e.g. {sample})")
        findings.append(
            f"[semantic_file_tree] {total} semantic link path(s) not in file_tree: "
            + "; ".join(parts)
            + " — may indicate workspace-relative paths"
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
    all_phantom: list[str] = []
    domain_counts: list[str] = []
    for domain in sm.architecture.domains:
        phantom_files = [p for p in domain.files if p not in known_paths]
        if phantom_files:
            all_phantom.extend(phantom_files[:2])
            domain_counts.append(f"'{domain.name}': {len(phantom_files)}")
    if domain_counts:
        sample = ", ".join(dict.fromkeys(all_phantom[:3]))
        findings.append(
            f"[architecture_graph] {len(domain_counts)} domain(s) reference phantom paths "
            f"(e.g. {sample}): "
            + ", ".join(domain_counts[:5])
            + ("..." if len(domain_counts) > 5 else "")
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


def agent_view(sm: SourceMap) -> dict[str, Any]:
    """Opinionated output for AI agents — structured, noise-free, gap-aware.

    Output order:
        1. project          → identity: type, summary, primary stack, frameworks
        2. entry_points     → where execution starts (with reason/evidence)
        3. architecture     → how it's structured (flow description)
        4. key_dependencies → runtime dependencies that matter (when analyzed)
        5. signals          → compact operational context (env, notes, tests)
        6. confidence_summary → detection quality and hard/soft signals
        7. analysis_gaps    → what's uncertain or missing

    Never includes: file_tree, file_paths, schema internals, empty sections,
    null fields, raw dependency lists, metrics, docs, or low-signal metadata.
    """
    # ── 1. Identity ──────────────────────────────────────────────────────────
    primary = next((s for s in sm.stacks if s.primary), sm.stacks[0] if sm.stacks else None)

    project: dict[str, Any] = {
        "type": sm.project_type,
        "summary": sm.project_summary,
    }
    if primary:
        project["primary_stack"] = primary.stack
        if primary.frameworks:
            project["frameworks"] = [f.name for f in primary.frameworks]
        if primary.package_manager:
            project["package_manager"] = primary.package_manager
        if primary.root and primary.root != ".":
            project["root"] = primary.root

    secondary = [s for s in sm.stacks if not s.primary and s.stack != (primary.stack if primary else "")]
    if secondary:
        project["secondary_stacks"] = sorted({s.stack for s in secondary})

    result: dict[str, Any] = {"project": project}

    # ── 2. Entry points: production/runtime only in the primary field ─────────
    # Development and auxiliary entries are explicit side channels. A missing
    # production runtime is represented as entry_points=[], never by fallback.
    ep_groups = _entry_point_groups(sm.entry_points)
    result["entry_points"] = ep_groups["production"]
    result["development_entry_points"] = ep_groups["development"]
    result["auxiliary_entry_points"] = ep_groups["auxiliary"]

    # ── 3. Architecture ───────────────────────────────────────────────────────
    result["architecture"] = _architecture_context(sm)

    # ── 3a. File relevance: evidence-backed categories, not keyword matches ──
    relevant_files = _file_relevance(sm)
    if relevant_files:
        result["file_relevance"] = relevant_files

    # ── 3b. Monorepo package roles (when available) ───────────────────────────
    if sm.monorepo_packages:
        _noise_roles = {"benchmark_layer", "tooling_layer", "docs_layer", "test_layer"}
        operational_pkgs = [
            {"path": p.path, "role": p.architectural_role, "criticality": p.criticality}
            for p in sm.monorepo_packages
            if p.architectural_role not in _noise_roles
        ]
        if operational_pkgs:
            result["runtime_packages"] = operational_pkgs

    # ── 4. Dependencies: separated by operational role ───────────────────────
    dep_groups = _dependency_groups(sm)
    if dep_groups["production_dependencies"]:
        result["production_dependencies"] = dep_groups["production_dependencies"][:15]
    for dep_key in ("dev_tools", "test_utilities", "build_tooling", "noise_dependencies", "suspicious_dependencies"):
        if dep_groups[dep_key]:
            result[dep_key] = dep_groups[dep_key][:15]

    # Backward-compatible compact list, now production-only.
    production_key_deps = [
        d for d in sm.key_dependencies
        if (d.role or "unknown") in _PRODUCTION_DEP_ROLES and d.scope not in {"dev"}
    ]
    if sm.dependency_summary and sm.dependency_summary.requested and production_key_deps:
        _dep_skip = {"parent", "manifest_path", "workspace", "source", "ecosystem"}
        result["key_dependencies"] = [
            {k: v for k, v in asdict(d).items() if v is not None and k not in _dep_skip}
            for d in production_key_deps[:15]
        ]

    # ── 5. Signals — compact operational context ─────────────────────────────
    signals: dict[str, Any] = {}

    if sm.env_summary and sm.env_summary.requested and sm.env_summary.total > 0:
        signals["env_vars"] = {
            "total": sm.env_summary.total,
            "required": sm.env_summary.required_count,
        }
        if sm.env_summary.categories:
            signals["env_vars"]["categories"] = sm.env_summary.categories

    if sm.code_notes_summary and sm.code_notes_summary.requested and sm.code_notes_summary.total > 0:
        by_kind = {k: v for k, v in sm.code_notes_summary.by_kind.items() if v > 0}
        if by_kind:
            signals["code_notes"] = {"total": sm.code_notes_summary.total, "by_kind": by_kind}
        if sm.code_notes_summary.adr_count > 0:
            signals["adrs"] = sm.code_notes_summary.adr_count

    has_tests = any(
        "/test" in p or "/tests" in p or "/spec" in p or p.startswith("test")
        for p in sm.file_paths
    )
    if has_tests:
        signals["has_tests"] = True

    # Semantic hotspots (populated when --semantics was passed)
    if sm.semantic_summary is not None and sm.semantic_summary.requested:
        sem = sm.semantic_summary
        sem_info: dict[str, Any] = {
            "files_analyzed": sem.files_analyzed,
            "symbols": sem.symbol_count,
            "calls": sem.call_count,
            "links": sem.link_count,
            "languages": sem.languages,
        }
        if sem.coverage_pct is not None:
            sem_info["coverage_pct"] = sem.coverage_pct
            sem_info["coverage_confidence"] = sem.coverage_confidence
        if sem.truncated:
            sem_info["truncated"] = True
        if sem.hotspots:
            sem_info["hotspots"] = sem.hotspots[:10]
        signals["semantic_graph"] = sem_info

    if signals:
        result["signals"] = signals

    # ── 6. Confidence summary ─────────────────────────────────────────────────
    if sm.confidence_summary is not None:
        cs = sm.confidence_summary
        conf: dict[str, Any] = {
            "overall": cs.overall,
            "stack": cs.stack_confidence,
            "entry_points": cs.entry_point_confidence,
            "sections": _section_confidence(sm),
        }
        if cs.hard_signals:
            conf["hard_signals"] = cs.hard_signals
        if cs.soft_signals:
            conf["soft_signals"] = cs.soft_signals
        if cs.ignored_signals:
            conf["ignored_signals"] = cs.ignored_signals
        if cs.anomalies:
            conf["anomalies"] = cs.anomalies
        result["confidence_summary"] = conf

    # ── 7. Analysis gaps ──────────────────────────────────────────────────────
    analysis_gaps: list[dict[str, Any]] = []

    if sm.analysis_gaps:
        analysis_gaps = [asdict(g) for g in sm.analysis_gaps]
    else:
        # Fallback gap derivation when confidence_analyzer was not run
        if not sm.entry_points:
            analysis_gaps.append({
                "area": "entry_points",
                "reason": "No entry point detected — project structure may be non-standard",
                "impact": "high",
            })
        if primary and primary.confidence == "low":
            analysis_gaps.append({
                "area": "stack",
                "reason": f"Low-confidence detection for '{primary.stack}' — no manifest found",
                "impact": "medium",
            })
        heuristic_stacks = [s for s in sm.stacks if s.detection_method == "heuristic"]
        if heuristic_stacks:
            analysis_gaps.append({
                "area": "stack",
                "reason": f"Heuristic-only detection (no manifest): {', '.join(s.stack for s in heuristic_stacks)}",
                "impact": "medium",
            })
        if not sm.dependency_summary or not sm.dependency_summary.requested:
            analysis_gaps.append({
                "area": "dependencies",
                "reason": "Dependencies not analyzed — add --dependencies for full context",
                "impact": "medium",
            })

    if analysis_gaps:
        result["analysis_gaps"] = analysis_gaps

    return result


def standard_view(sm: SourceMap, *, include_tree: bool = False) -> dict[str, Any]:
    """Default output — three signal layers.

    Layer A (always):
        metadata, project_type, project_summary, architecture_summary,
        stacks, entry_points.

    Layer B (when the corresponding flag was passed):
        dependency_summary + key_dependencies, env_summary + env_map,
        code_notes_summary + code_notes, git_context.

    Layer C (only when the flag was explicitly passed, checked via *.requested):
        module_graph, docs, semantic_*, file_metrics, architecture inference.

    file_tree / file_paths only when include_tree=True.
    Full dependencies list is never included — use key_dependencies instead.
    Empty unrequested analyzer fields are omitted entirely.
    """
    ep_groups = _entry_point_groups(sm.entry_points)

    result: dict[str, Any] = {
        "metadata": asdict(sm.metadata),
        "project_type": sm.project_type,
        "project_summary": sm.project_summary,
        "architecture_summary": sm.architecture_summary,
        "stacks": [asdict(s) for s in sm.stacks],
        "entry_points": ep_groups["production"],
        "development_entry_points": ep_groups["development"],
        "auxiliary_entry_points": ep_groups["auxiliary"],
    }

    # Layer B — signals (only when the corresponding analyzer ran)
    if sm.dependency_summary is not None and sm.dependency_summary.requested:
        dep_dict = asdict(sm.dependency_summary)
        dep_dict.pop("dependencies", None)  # avoid duplication with key_dependencies
        result["dependency_summary"] = dep_dict
        result["key_dependencies"] = [
            asdict(d) for d in sm.key_dependencies
            if (d.role or "unknown") in _PRODUCTION_DEP_ROLES and d.scope not in {"dev"}
        ]

    if sm.env_summary is not None and sm.env_summary.requested:
        result["env_summary"] = asdict(sm.env_summary)
        result["env_map"] = [asdict(e) for e in sm.env_map]

    if sm.code_notes_summary is not None and sm.code_notes_summary.requested:
        result["code_notes_summary"] = asdict(sm.code_notes_summary)
        if sm.code_notes:
            result["code_notes"] = [asdict(n) for n in sm.code_notes]
        if sm.code_adrs:
            result["code_adrs"] = [asdict(a) for a in sm.code_adrs]

    if sm.git_context is not None and sm.git_context.requested:
        result["git_context"] = asdict(sm.git_context)

    # Layer C — deep-dive (flag must have been explicitly passed)
    if sm.module_graph is not None and sm.module_graph.summary.requested:
        result["module_graph"] = asdict(sm.module_graph)
        result["module_graph_summary"] = asdict(sm.module_graph.summary)

    if sm.doc_summary is not None and sm.doc_summary.requested:
        result["doc_summary"] = asdict(sm.doc_summary)
        result["docs"] = [asdict(d) for d in sm.docs]

    if sm.semantic_summary is not None and sm.semantic_summary.requested:
        result["semantic_summary"] = asdict(sm.semantic_summary)
        result["semantic_calls"] = [asdict(c) for c in sm.semantic_calls]
        result["semantic_symbols"] = [asdict(s) for s in sm.semantic_symbols]
        result["semantic_links"] = [asdict(lnk) for lnk in sm.semantic_links]

    if sm.metrics_summary is not None and sm.metrics_summary.requested:
        result["metrics_summary"] = asdict(sm.metrics_summary)
        result["file_metrics"] = [asdict(m) for m in sm.file_metrics]

    if sm.architecture is not None and sm.architecture.requested:
        result["architecture"] = asdict(sm.architecture)

    if include_tree:
        result["file_tree"] = sm.file_tree
        result["file_paths"] = sm.file_paths

    if sm.pipeline_trace is not None and sm.pipeline_trace.requested:
        result["pipeline_trace"] = asdict(sm.pipeline_trace)

    return result


def write_output(content: str, output: Optional[Path]) -> None:
    """Write content to stdout or a file.

    Args:
        content: Serialized string (JSON or YAML).
        output: Destination file path. None = stdout.
    """
    if output is None:
        sys.stdout.write(content)
        if not content.endswith("\n"):
            sys.stdout.write("\n")
    else:
        output.write_text(content, encoding="utf-8")

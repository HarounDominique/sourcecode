"""pr_impact.py — PR Impact Report: blast radius for a list of changed Java files.

Answers: "What can I break if I merge this PR?"

Aggregates run_impact_chain() + event topology across all classes in changed files.
Produces a consolidated text report + structured dict.

Reuses: CIR, SpringSemanticModel, ImpactOrchestrator, EventGraph.
No new parsers or CIR traversals.

Usage:
    cir = build_canonical_ir(find_java_files(root), root)
    report = run_pr_impact(cir, ["src/.../UserService.java"], root=root)
    print(report.render_text())
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from sourcecode.canonical_ir import CanonicalRepositoryIR
    from sourcecode.spring_model import SpringSemanticModel

_RISK_ORDER: dict[str, int] = {
    "critical": 4, "high": 3, "medium": 2, "low": 1, "unknown": 0
}
_RISK_LABEL: dict[int, str] = {
    4: "CRITICAL", 3: "HIGH", 2: "MEDIUM", 1: "LOW", 0: "UNKNOWN"
}


# ---------------------------------------------------------------------------
# Output model
# ---------------------------------------------------------------------------

@dataclass
class PRImpactReport:
    """Consolidated impact report for a list of changed Java files."""

    modified_classes: list[str] = field(default_factory=list)
    affected_endpoints: list[dict] = field(default_factory=list)   # {method, path}
    direct_callers: list[str] = field(default_factory=list)
    event_publishers: list[str] = field(default_factory=list)      # human lines
    event_consumers: list[str] = field(default_factory=list)       # human lines
    transactional_methods: list[str] = field(default_factory=list)
    risk_level: str = "UNKNOWN"
    risk_reason: str = ""
    analysis_warnings: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "schema_version": "1.0",
            "modified_classes": self.modified_classes,
            "affected_endpoints": self.affected_endpoints,
            "direct_callers": self.direct_callers,
            "event_flow": {
                "publishers": self.event_publishers,
                "consumers": self.event_consumers,
            },
            "transactional_methods": self.transactional_methods,
            "risk_level": self.risk_level,
            "risk_reason": self.risk_reason,
            "analysis_warnings": self.analysis_warnings,
            "metadata": self.metadata,
        }

    def render_text(self) -> str:
        sep = "=" * 50
        lines = [sep, "PR IMPACT REPORT", "=" * 16, ""]

        def _short(fqn: str) -> str:
            return fqn.rsplit(".", 1)[-1] if "." in fqn else fqn

        def _short_method(fqn: str) -> str:
            if "#" in fqn:
                cls, meth = fqn.rsplit("#", 1)
                return f"{_short(cls)}.{meth}()"
            return _short(fqn)

        lines.append("Modified:")
        lines.append("")
        if self.modified_classes:
            for cls in self.modified_classes:
                lines.append(f"  * {_short(cls)}")
        else:
            lines.append("  (no Spring classes found in changed files)")
        lines.append("")

        if self.affected_endpoints:
            lines.append("Affected Endpoints:")
            lines.append("")
            for ep in self.affected_endpoints:
                lines.append(f"  * {ep.get('method', '?')} {ep.get('path', '?')}")
            lines.append("")

        if self.direct_callers:
            lines.append("Direct Callers:")
            lines.append("")
            for caller in self.direct_callers:
                lines.append(f"  * {_short(caller)}")
            lines.append("")

        event_items = self.event_publishers + self.event_consumers
        if event_items:
            lines.append("Event Flow:")
            lines.append("")
            for item in event_items:
                lines.append(f"  * {item}")
            lines.append("")

        if self.transactional_methods:
            lines.append("Transactional Impact:")
            lines.append("")
            for m in self.transactional_methods:
                lines.append(f"  * {_short_method(m)}")
            lines.append("")

        lines.append("Risk Level:")
        lines.append(self.risk_level)
        lines.append("")

        if self.risk_reason:
            lines.append("Reason:")
            lines.append(self.risk_reason)
            lines.append("")

        lines.append(sep)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# File → class mapping
# ---------------------------------------------------------------------------

def _build_file_class_index(cir: "CanonicalRepositoryIR") -> dict[str, list[str]]:
    """Return {relative_source_file: [class_fqns]} from CIR raw IR nodes.

    Only collects class-level nodes (no '#' in fqn) — method/field nodes are excluded
    because impact-chain is queried at class granularity.
    """
    index: dict[str, list[str]] = {}
    nodes: list[dict] = (cir._raw_ir.get("graph") or {}).get("nodes") or []
    for node in nodes:
        fqn: str = node.get("fqn") or ""
        sf: str = node.get("source_file") or ""
        if not fqn or not sf or "#" in fqn:
            continue
        index.setdefault(sf, []).append(fqn)
    return index


def _resolve_changed_files(
    file_list: list[str],
    file_class_index: dict[str, list[str]],
    root: Path,
) -> tuple[list[str], list[str]]:
    """Map changed file paths to class FQNs.

    Matching order:
      1. Exact key in file_class_index (path already relative to repo root)
      2. Relative path derived from absolute path via root
      3. Suffix match (e.g., "UserService.java" matches any CIR file ending with it)

    Returns (class_fqns, warnings). class_fqns is deduplicated, order-preserving.
    """
    class_fqns: list[str] = []
    warnings: list[str] = []
    seen_classes: set[str] = set()

    for raw_path in file_list:
        path_str = raw_path.strip()
        if not path_str:
            continue

        norm = path_str.replace("\\", "/")
        candidates: list[str] = []

        # 1. Exact match
        if norm in file_class_index:
            candidates = file_class_index[norm]
        else:
            # 2. Absolute path → relative to root
            try:
                abs_p = Path(path_str)
                if abs_p.is_absolute():
                    rel_str = str(abs_p.relative_to(root)).replace("\\", "/")
                    if rel_str in file_class_index:
                        candidates = file_class_index[rel_str]
            except (ValueError, Exception):
                pass

            # 3. Suffix match
            if not candidates:
                matches = [
                    k for k in file_class_index
                    if k == norm or k.endswith("/" + norm.lstrip("/"))
                ]
                if len(matches) == 1:
                    candidates = file_class_index[matches[0]]
                elif len(matches) > 1:
                    warnings.append(
                        f"Ambiguous path '{path_str}' matched {len(matches)} files; "
                        "using first match."
                    )
                    candidates = file_class_index[matches[0]]

        if not candidates:
            warnings.append(
                f"No Spring classes found for '{path_str}' — "
                "file not in CIR or has no class symbols."
            )

        for cls in candidates:
            if cls not in seen_classes:
                seen_classes.add(cls)
                class_fqns.append(cls)

    return class_fqns, warnings


# ---------------------------------------------------------------------------
# Event flow
# ---------------------------------------------------------------------------

def _collect_event_flow(
    class_fqns_set: set[str],
    model: "SpringSemanticModel",
) -> tuple[list[str], list[str]]:
    """Return (publisher_lines, consumer_lines) describing event flow for changed classes.

    publisher_lines: "Publishes <EventType>" for events published by changed classes.
    consumer_lines:  "Consumed by <Listener>" when changed class publishes an event that
                     has listeners, or "Listens to <EventType>" when a changed class
                     is itself a listener.
    """

    def _short(fqn: str) -> str:
        return fqn.rsplit(".", 1)[-1] if "." in fqn else fqn

    def _class_of(fqn: str) -> str:
        return fqn.split("#")[0] if "#" in fqn else fqn

    publisher_lines: list[str] = []
    consumer_lines: list[str] = []
    seen: set[str] = set()

    # Changed class publishes an event → report publish + downstream consumers
    for event_type, publishers in model.event_graph.publishers.items():
        for pub_fqn in publishers:
            if _class_of(pub_fqn) not in class_fqns_set:
                continue
            pub_key = f"pub:{event_type}"
            if pub_key not in seen:
                seen.add(pub_key)
                publisher_lines.append(f"Publishes {_short(event_type)}")
            for consumer_fqn in model.event_graph.listeners_of(event_type):
                con_key = f"con:{event_type}:{consumer_fqn}"
                if con_key not in seen:
                    seen.add(con_key)
                    consumer_lines.append(f"Consumed by {_short(consumer_fqn)}")

    # Changed class is a listener → report what it listens to
    for event_type, listeners in model.event_graph.listeners.items():
        for lst_fqn in listeners:
            if _class_of(lst_fqn) not in class_fqns_set:
                continue
            lst_class = _class_of(lst_fqn)
            lst_key = f"lst:{lst_class}:{event_type}"
            if lst_key not in seen:
                seen.add(lst_key)
                consumer_lines.append(f"Listens to {_short(event_type)}")

    return publisher_lines, consumer_lines


# ---------------------------------------------------------------------------
# Transactional methods
# ---------------------------------------------------------------------------

def _collect_tx_methods(
    class_fqns_set: set[str],
    model: "SpringSemanticModel",
) -> list[str]:
    """Return FQNs with @Transactional boundaries declared in changed classes."""
    tx_methods: list[str] = []
    seen: set[str] = set()

    for cls in class_fqns_set:
        # Class-level @Transactional: the class symbol itself is the boundary
        if cls in model.tx_index.class_level:
            if cls not in seen:
                seen.add(cls)
                tx_methods.append(cls)
        # Method-level @Transactional
        for boundary in model.tx_index.by_class.get(cls, []):
            sym = boundary.symbol
            if sym not in seen:
                seen.add(sym)
                tx_methods.append(sym)

    return tx_methods


# ---------------------------------------------------------------------------
# Risk consolidation
# ---------------------------------------------------------------------------

def _compute_risk(
    endpoints: list[dict],
    callers: list[str],
    event_publishers: list[str],
    event_consumers: list[str],
    tx_methods: list[str],
    individual_risks: list[str],
) -> tuple[str, str]:
    """Return (risk_level_label, reason_string).

    Base risk from individual impact chains. Boost when multiple dimensions present.
    """
    base = max((_RISK_ORDER.get(r.lower(), 0) for r in individual_risks), default=0)

    reasons: list[str] = []
    if endpoints:
        reasons.append("Public API")
    if event_publishers or event_consumers:
        reasons.append("Event Flow")
    if tx_methods:
        reasons.append("Transaction Boundary")
    if len(callers) > 5:
        reasons.append("High Call Fan-in")

    level = base
    if len(reasons) >= 3 and level < _RISK_ORDER["high"]:
        level = _RISK_ORDER["high"]
    elif len(reasons) >= 2 and level < _RISK_ORDER["medium"]:
        level = _RISK_ORDER["medium"]

    label = _RISK_LABEL.get(level, "LOW")
    reason = " + ".join(reasons) if reasons else "No high-risk signals detected"
    return label, reason


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_pr_impact(
    cir: "CanonicalRepositoryIR",
    changed_files: list[str],
    *,
    root: Path,
    model: Optional["SpringSemanticModel"] = None,
) -> PRImpactReport:
    """Run PR impact analysis for a list of changed Java file paths.

    Args:
        cir:           CanonicalRepositoryIR from build_canonical_ir().
        changed_files: Paths to changed Java files (relative, absolute, or bare name).
        root:          Repo root (used for absolute-path normalization).
        model:         Pre-built SpringSemanticModel. Built internally if None.

    Returns PRImpactReport — always serializable, never raises.
    """
    try:
        return _run_pr_impact_internal(cir, changed_files, root=root, model=model)
    except Exception as exc:
        return PRImpactReport(
            risk_level="UNKNOWN",
            risk_reason="Internal error during analysis.",
            analysis_warnings=[f"Internal error: {type(exc).__name__}: {exc}"],
            metadata={"changed_files_count": len(changed_files)},
        )


def _run_pr_impact_internal(
    cir: "CanonicalRepositoryIR",
    changed_files: list[str],
    *,
    root: Path,
    model: Optional["SpringSemanticModel"],
) -> PRImpactReport:
    from sourcecode.spring_model import SpringSemanticModel
    from sourcecode.spring_impact import run_impact_chain

    t0 = time.monotonic()
    warnings: list[str] = []

    if model is None:
        model = SpringSemanticModel.build(cir)

    # 1. Map file paths → class FQNs
    file_class_index = _build_file_class_index(cir)
    class_fqns, file_warnings = _resolve_changed_files(changed_files, file_class_index, root)
    warnings.extend(file_warnings)

    if not class_fqns:
        return PRImpactReport(
            risk_level="UNKNOWN",
            risk_reason="No Spring classes found in changed files.",
            analysis_warnings=warnings,
            metadata={
                "changed_files_count": len(changed_files),
                "classes_analyzed": 0,
            },
        )

    class_fqns_set = set(class_fqns)

    # 2. Impact chain per modified class
    all_direct_callers: list[str] = []
    all_endpoints: list[dict] = []
    individual_risks: list[str] = []
    seen_callers: set[str] = set()
    seen_ep_ids: set[str] = set()

    for cls in class_fqns:
        result = run_impact_chain(
            cir, cls,
            root=root,
            model=model,
            prebuilt_findings=[],  # skip audit findings — focus on structural impact
        )
        warnings.extend(result.analysis_warnings)
        individual_risks.append(result.risk_level)

        for caller in result.direct_callers:
            caller_class = caller.split("#")[0] if "#" in caller else caller
            if caller_class not in class_fqns_set and caller_class not in seen_callers:
                seen_callers.add(caller_class)
                all_direct_callers.append(caller_class)

        for ep in result.endpoints_affected:
            ep_id = ep.endpoint_id
            if ep_id not in seen_ep_ids:
                seen_ep_ids.add(ep_id)
                all_endpoints.append({"method": ep.method, "path": ep.path})

    # 3. Event flow
    event_publishers, event_consumers = _collect_event_flow(class_fqns_set, model)

    # 4. Transactional boundaries in changed classes
    tx_methods = _collect_tx_methods(class_fqns_set, model)

    # 5. Consolidated risk
    risk_level, risk_reason = _compute_risk(
        all_endpoints,
        all_direct_callers,
        event_publishers,
        event_consumers,
        tx_methods,
        individual_risks,
    )

    elapsed_ms = round((time.monotonic() - t0) * 1000, 2)

    return PRImpactReport(
        modified_classes=class_fqns,
        affected_endpoints=all_endpoints,
        direct_callers=all_direct_callers,
        event_publishers=event_publishers,
        event_consumers=event_consumers,
        transactional_methods=tx_methods,
        risk_level=risk_level,
        risk_reason=risk_reason,
        analysis_warnings=warnings,
        metadata={
            "changed_files_count": len(changed_files),
            "classes_analyzed": len(class_fqns),
            "analysis_time_ms": elapsed_ms,
        },
    )

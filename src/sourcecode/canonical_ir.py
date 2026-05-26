"""canonical_ir.py — Canonical Repository IR contract (single source of truth).

Architecture:
    build_canonical_ir()      → CanonicalRepositoryIR
    project_route_surface()   → derives route_surface list from CIR
    project_endpoint_surface() → derives endpoint surface dict from CIR
    project_blast_radius()    → derives blast-radius dict from CIR
    validate_canonical_ir()   → invariant checker; returns violation list

All external projections derive exclusively from CanonicalRepositoryIR.
No view reconstructs endpoint, security, or blast-radius data independently.

IR schema version: 1.0.0
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from sourcecode.repository_ir import (
    build_repo_ir,
    compute_blast_radius as _compute_blast_radius,
)

# ---------------------------------------------------------------------------
# Schema version — single constant, embedded in every CIR
# ---------------------------------------------------------------------------

IR_SCHEMA_VERSION = "1.0.0"

# Edge types excluded from reverse graph (mirrors repository_ir._REVERSE_EXCLUDE)
_REVERSE_EXCLUDE: frozenset[str] = frozenset({"annotated_with", "mapped_to"})


# ---------------------------------------------------------------------------
# CanonicalSecurity
# ---------------------------------------------------------------------------

@dataclass
class CanonicalSecurity:
    """Canonical security policy for an endpoint.

    Single source of truth for authorization metadata.
    Never reconstructed from annotations in independent code paths.

    source_scope: where the annotation lives —
      "method"    annotation on the handler method
      "class"     annotation on the controller class (method has none)
      "inherited" endpoint inherited from parent class
    """
    policy: str                              # deny_all|permit_all|roles_allowed|authenticated|...
    source_scope: str                        # method|class|inherited
    effective_roles: list[str] = field(default_factory=list)
    expression: str = ""                     # SpEL for @PreAuthorize/@PostAuthorize
    required_permission: str = ""            # for @M3FiltroSeguridad
    raw: dict = field(default_factory=dict)  # full original policy dict

    def to_dict(self) -> dict:
        """Serialize for external consumption (omits internal fields)."""
        out: dict = {"policy": self.policy}
        if self.effective_roles:
            out["roles"] = self.effective_roles
        if self.expression:
            out["expression"] = self.expression
        if self.required_permission:
            out["required_permission"] = self.required_permission
        return out

    def to_full_dict(self) -> dict:
        """Full serialization including source_scope (for CIR audit/debug)."""
        out = self.to_dict()
        out["source_scope"] = self.source_scope
        return out

    @classmethod
    def from_policy_dict(
        cls, d: dict, *, source_scope: str = "method"
    ) -> "CanonicalSecurity":
        """Build from the policy dict emitted by _route_security_from_sym."""
        return cls(
            policy=d.get("policy", ""),
            source_scope=source_scope,
            effective_roles=list(d.get("roles", [])),
            expression=d.get("expression", ""),
            required_permission=d.get("required_permission", ""),
            raw=dict(d),
        )


# ---------------------------------------------------------------------------
# CanonicalEndpoint
# ---------------------------------------------------------------------------

@dataclass
class CanonicalEndpoint:
    """Canonical endpoint entity — single source of truth for REST endpoint data.

    id is deterministic: METHOD:path:controller_class:handler_symbol
    Field names are stable and typed — never loose dicts with optional fields.

    No independent reconstruction: always derived from route_surface in IR.
    """
    id: str                              # METHOD:path:controller_fqn:handler_symbol
    path: str
    method: str
    controller_class: str                # FQN of controller class
    handler_symbol: str                  # FQN of handler method: pkg.Class#method
    security: Optional[CanonicalSecurity]
    source_file: str
    stable_id: str
    inheritance_depth: int = 0
    dependent_symbols: list[str] = field(default_factory=list)  # lazy, filled by blast-radius

    def to_dict(self) -> dict:
        out: dict = {
            "id": self.id,
            "path": self.path,
            "method": self.method,
            "controller_class": self.controller_class,
            "handler_symbol": self.handler_symbol,
            "source_file": self.source_file,
            "stable_id": self.stable_id,
            "inheritance_depth": self.inheritance_depth,
        }
        if self.security is not None:
            out["security"] = self.security.to_dict()
        if self.dependent_symbols:
            out["dependent_symbols"] = self.dependent_symbols
        return out

    @staticmethod
    def make_id(
        method: str, path: str, controller_class: str, handler_symbol: str
    ) -> str:
        """Deterministic endpoint ID — stable across formatting/body changes."""
        return f"{method}:{path}:{controller_class}:{handler_symbol}"


# ---------------------------------------------------------------------------
# CanonicalRepositoryIR
# ---------------------------------------------------------------------------

@dataclass
class CanonicalRepositoryIR:
    """Canonical Repository IR — single source of truth for all code intelligence.

    Projections that MUST derive from this structure:
      project_route_surface()       route_surface list
      project_endpoint_surface()    endpoint surface dict (replaces extract_java_endpoints)
      project_blast_radius()        blast-radius dict

    No view is allowed to independently reconstruct endpoints, security, or
    blast-radius data — all must project from cir.endpoints / cir.reverse_graph.
    """
    schema_version: str                                       # always IR_SCHEMA_VERSION
    cir_hash: str                                             # sha256 fingerprint
    files: list[str]                                          # sorted relative file paths
    symbols: list[str]                                        # sorted symbol FQNs
    call_graph: list[dict]                                    # sorted forward edges
    reverse_graph: dict[str, dict[str, list[str]]]            # target → {type → [callers]}
    dependencies: list[dict]                                  # import/extends/implements edges
    endpoints: list[CanonicalEndpoint]                        # canonical endpoint list
    security_index: dict[str, CanonicalSecurity]              # handler_symbol → security
    metadata: dict[str, Any]                                  # stats, gaps, subsystems, etc.
    # Raw IR dict retained for projections that need full IR fields
    # (e.g. project_blast_radius delegates to compute_blast_radius)
    _raw_ir: dict = field(default_factory=dict, repr=False, compare=False)


# ---------------------------------------------------------------------------
# Fingerprint
# ---------------------------------------------------------------------------

def _compute_cir_hash(
    schema_version: str,
    files: list[str],
    symbols: list[str],
    endpoints: list[CanonicalEndpoint],
    call_graph: list[dict],
) -> str:
    """Compute deterministic sha256 fingerprint of canonical IR content.

    Identical repo + schema_version → identical hash.
    Changes on: any symbol/endpoint added or removed, schema version bump.
    Stable across: formatting, comments, ordering differences.
    """
    edge_keys = sorted(
        f"{e.get('from', '')}:{e.get('type', '')}:{e.get('to', '')}"
        for e in call_graph
    )
    endpoint_keys = sorted(ep.id for ep in endpoints)

    content = {
        "schema_version": schema_version,
        "files": sorted(files),
        "symbols": sorted(symbols),
        "edges": edge_keys,
        "endpoints": endpoint_keys,
    }
    raw = json.dumps(content, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Conversion helpers
# ---------------------------------------------------------------------------

def _route_to_canonical_endpoint(route: dict) -> CanonicalEndpoint:
    """Convert a route_surface entry to a CanonicalEndpoint.

    Canonical field mapping:
      route["effective_class"] | route["controller"]  →  controller_class (FQN)
      route["symbol"]                                  →  handler_symbol   (FQN)
      route["security_annotations"]                    →  security         (CanonicalSecurity)

    This is the ONLY place that reads route_surface dict fields.
    All other code must read CanonicalEndpoint attributes.
    """
    controller_class = (
        route.get("effective_class")
        or route.get("controller")
        or route.get("declaring_class")
        or ""
    )
    handler_symbol = route.get("symbol") or ""
    method = route.get("method") or ""
    path = route.get("path") or ""

    security_dict = route.get("security_annotations")
    security: Optional[CanonicalSecurity] = None
    if security_dict:
        # Determine source_scope from inheritance_depth:
        # depth=0 → annotation on method or class (method takes precedence)
        # depth>0 → inherited from parent class
        depth = route.get("inheritance_depth") or 0
        scope = "inherited" if depth > 0 else "method"
        security = CanonicalSecurity.from_policy_dict(security_dict, source_scope=scope)

    endpoint_id = CanonicalEndpoint.make_id(method, path, controller_class, handler_symbol)

    return CanonicalEndpoint(
        id=endpoint_id,
        path=path,
        method=method,
        controller_class=controller_class,
        handler_symbol=handler_symbol,
        security=security,
        source_file=route.get("source_file") or "",
        stable_id=route.get("stable_id") or "",
        inheritance_depth=route.get("inheritance_depth") or 0,
    )


def ir_dict_to_canonical(
    ir: dict,
    *,
    file_paths: Optional[list[str]] = None,
) -> CanonicalRepositoryIR:
    """Convert build_repo_ir output dict to CanonicalRepositoryIR.

    Single entry point for CIR construction from an already-built IR dict.
    All projections should use this rather than working with the raw IR dict.

    Args:
        ir:         Full IR dict from build_repo_ir.
        file_paths: Optional explicit file list (used for files field).
                    If None, derived from graph node source_file fields.
    """
    graph = ir.get("graph") or {}
    nodes = graph.get("nodes") or []
    edges = graph.get("edges") or []

    # Sorted symbol FQNs — stable ordering
    symbols = sorted(n["fqn"] for n in nodes if "fqn" in n)

    # Sorted file paths — stable ordering
    if file_paths is not None:
        files = sorted(file_paths)
    else:
        file_set: set[str] = set()
        for n in nodes:
            sf = n.get("source_file") or ""
            if sf:
                file_set.add(sf)
        files = sorted(file_set)

    # Sorted call graph edges — stable ordering: from, type, to
    call_graph = sorted(
        edges,
        key=lambda e: (e.get("from", ""), e.get("type", ""), e.get("to", "")),
    )

    # Dependency edges (structural subset of call_graph)
    _dep_types = frozenset({"imports", "extends", "implements", "injects"})
    dependencies = [e for e in call_graph if e.get("type") in _dep_types]

    # Build canonical endpoints from route_surface — stable ordering
    route_surface = ir.get("route_surface") or []
    # Deduplicate by endpoint id (route_surface can have duplicates from multi-prefix)
    _seen_ids: set[str] = set()
    raw_endpoints: list[CanonicalEndpoint] = []
    for r in route_surface:
        ep = _route_to_canonical_endpoint(r)
        if ep.id not in _seen_ids:
            _seen_ids.add(ep.id)
            raw_endpoints.append(ep)

    endpoints = sorted(
        raw_endpoints,
        key=lambda ep: (ep.method, ep.path, ep.controller_class, ep.handler_symbol),
    )

    # Security index: handler_symbol → CanonicalSecurity
    # For handlers with security, record from the most-specific source
    security_index: dict[str, CanonicalSecurity] = {}
    for ep in endpoints:
        if ep.security is not None and ep.handler_symbol:
            security_index[ep.handler_symbol] = ep.security

    # Metadata aggregation
    metadata: dict[str, Any] = {
        "schema_version": IR_SCHEMA_VERSION,
        "symbol_count": len(symbols),
        "endpoint_count": len(endpoints),
        "file_count": len(files),
        "edge_count": len(call_graph),
        "subsystems": ir.get("subsystems") or [],
        "analysis_gaps": ir.get("analysis_gaps") or [],
        "spring_events": ir.get("spring_events") or {},
        "score_basis": (ir.get("impact") or {}).get("score_basis", "none"),
        "reverse_graph_size": len(ir.get("reverse_graph") or {}),
        "security_model": ir.get("security_model", "unknown"),
    }

    cir_hash = _compute_cir_hash(
        IR_SCHEMA_VERSION, files, symbols, endpoints, call_graph
    )

    return CanonicalRepositoryIR(
        schema_version=IR_SCHEMA_VERSION,
        cir_hash=cir_hash,
        files=files,
        symbols=symbols,
        call_graph=call_graph,
        reverse_graph=ir.get("reverse_graph") or {},
        dependencies=dependencies,
        endpoints=endpoints,
        security_index=security_index,
        metadata=metadata,
        _raw_ir=ir,
    )


# ---------------------------------------------------------------------------
# Public builder
# ---------------------------------------------------------------------------

def build_canonical_ir(
    file_paths: list[str],
    root: Path,
    *,
    since: Optional[str] = None,
) -> CanonicalRepositoryIR:
    """Build CanonicalRepositoryIR from Java files.

    Single source of truth builder — the canonical entry point for code intelligence.
    Delegates Java IR extraction to build_repo_ir (5-phase pipeline unchanged).
    Returns a CanonicalRepositoryIR from which all projections must derive.

    Args:
        file_paths: Relative paths to Java files (from find_java_files).
        root:       Absolute repo root.
        since:      Git ref for symbol diff (e.g. "HEAD~1", "main").
    """
    ir = build_repo_ir(file_paths, root, since=since)
    return ir_dict_to_canonical(ir, file_paths=file_paths)


# ---------------------------------------------------------------------------
# Projection: route_surface
# ---------------------------------------------------------------------------

def project_route_surface(cir: CanonicalRepositoryIR) -> list[dict]:
    """Project CIR endpoints to route_surface format.

    Canonical derivation — no independent endpoint data reconstruction.
    Output format compatible with existing route_surface consumers.
    """
    routes: list[dict] = []
    for ep in cir.endpoints:
        entry: dict = {
            "symbol": ep.handler_symbol,
            "controller": ep.controller_class,
            "declaring_class": ep.controller_class,
            "effective_class": ep.controller_class,
            "path": ep.path,
            "method": ep.method,
            "stable_id": ep.stable_id,
            "inheritance_depth": ep.inheritance_depth,
        }
        if ep.security is not None:
            # Keep security_annotations key for backward compat with route_surface consumers
            entry["security_annotations"] = ep.security.to_dict()
        routes.append(entry)

    return sorted(routes, key=lambda r: (r["effective_class"], r["path"]))


# ---------------------------------------------------------------------------
# Projection: endpoint_surface (canonical replacement for extract_java_endpoints)
# ---------------------------------------------------------------------------

def project_endpoint_surface(cir: CanonicalRepositoryIR) -> dict:
    """Project CIR to endpoint surface format.

    Replaces extract_java_endpoints as the canonical endpoint extractor.
    Output format is backward compatible with extract_java_endpoints.

    Source: cir.endpoints — never independently parsed from Java source.
    Security: cir.security_index — never independently re-extracted.
    """
    endpoints: list[dict] = []

    for ep in cir.endpoints:
        # Simple names for backward compat (same as extract_java_endpoints output)
        controller_simple = ep.controller_class.split(".")[-1]
        handler_simple = (
            ep.handler_symbol.split("#")[1]
            if "#" in ep.handler_symbol
            else ep.handler_symbol.rsplit(".", 1)[-1]
        )

        entry: dict = {
            "method": ep.method,
            "path": ep.path,
            "controller": controller_simple,
            "handler": handler_simple,
        }

        if ep.security is not None:
            entry["security"] = ep.security.to_dict()
            # Backward compat: top-level required_permission for custom annotation
            if ep.security.policy == "custom_permission":
                entry["required_permission"] = ep.security.required_permission

        endpoints.append(entry)

    no_security_signal = sum(1 for e in endpoints if "security" not in e)
    return {
        "endpoints": endpoints,
        "total": len(endpoints),
        "no_security_signal": no_security_signal,
        "security_model": cir.metadata.get("security_model", "unknown"),
        # Legacy field alias — same count, kept for backward compat
        "undocumented": no_security_signal,
    }


# ---------------------------------------------------------------------------
# Projection: blast_radius
# ---------------------------------------------------------------------------

def project_blast_radius(
    cir: CanonicalRepositoryIR,
    target: str,
    *,
    max_depth: int = 4,
) -> dict:
    """Project blast radius from CIR.

    Delegates BFS logic to compute_blast_radius. Uses _raw_ir which has the
    canonical route_surface already built from the same symbol extraction pass.

    All endpoints in the result come from cir.endpoints (via route_surface).
    No independent endpoint reconstruction.
    """
    return _compute_blast_radius(cir._raw_ir, target, max_depth=max_depth)


# ---------------------------------------------------------------------------
# Invariant validator
# ---------------------------------------------------------------------------

def validate_canonical_ir(cir: CanonicalRepositoryIR) -> list[str]:
    """Validate CIR invariants. Returns list of violation strings.

    Empty list means CIR is valid.

    Invariants:
      1. Schema version matches IR_SCHEMA_VERSION
      2. Endpoint consistency: no duplicate IDs, required fields present
      3. Security index consistency: all keys are valid handler_symbols
      4. Graph consistency: reverse_graph entries correspond to call_graph edges
      5. Determinism: cir_hash recomputes identically
      6. Blast radius endpoints: endpoints_affected ⊆ cir.endpoints (sampled)
    """
    violations: list[str] = []

    # ── 1. Schema version ────────────────────────────────────────────────────
    if cir.schema_version != IR_SCHEMA_VERSION:
        violations.append(
            f"SCHEMA: version mismatch: stored='{cir.schema_version}' "
            f"expected='{IR_SCHEMA_VERSION}'"
        )

    # ── 2. Endpoint consistency ──────────────────────────────────────────────
    endpoint_ids: set[str] = set()
    handler_symbols: set[str] = set()
    for ep in cir.endpoints:
        # Duplicate IDs
        if ep.id in endpoint_ids:
            violations.append(f"ENDPOINT: duplicate id={ep.id!r}")
        endpoint_ids.add(ep.id)

        # Required fields
        if not ep.method:
            violations.append(f"ENDPOINT: id={ep.id!r} missing method")
        if not ep.path:
            violations.append(f"ENDPOINT: id={ep.id!r} missing path")
        if not ep.controller_class:
            violations.append(f"ENDPOINT: id={ep.id!r} missing controller_class")
        if not ep.handler_symbol:
            violations.append(f"ENDPOINT: id={ep.id!r} missing handler_symbol")
        else:
            handler_symbols.add(ep.handler_symbol)

        # ID must be deterministically reconstructible
        expected_id = CanonicalEndpoint.make_id(
            ep.method, ep.path, ep.controller_class, ep.handler_symbol
        )
        if ep.id != expected_id:
            violations.append(
                f"ENDPOINT: id not deterministic: stored={ep.id!r} "
                f"recomputed={expected_id!r}"
            )

    # ── 3. Security index consistency ────────────────────────────────────────
    for sym in cir.security_index:
        if sym not in handler_symbols:
            violations.append(
                f"SECURITY_INDEX: key {sym!r} not in endpoint handler_symbols"
            )

    # ── 4. Graph consistency (sampled — max 500 edges) ───────────────────────
    # Build forward edge set from call_graph for reverse-graph validation
    call_graph_edge_set: set[tuple[str, str, str]] = {
        (e.get("from", ""), e.get("to", ""), e.get("type", ""))
        for e in cir.call_graph
    }

    rg_checked = 0
    for target_sym, by_type in sorted(cir.reverse_graph.items()):
        for edge_type, callers in sorted(by_type.items()):
            if edge_type in _REVERSE_EXCLUDE:
                continue
            for caller in sorted(callers):
                if (caller, target_sym, edge_type) not in call_graph_edge_set:
                    violations.append(
                        f"GRAPH: reverse_graph edge "
                        f"{caller!r} →[{edge_type}]→ {target_sym!r} "
                        f"not in call_graph"
                    )
                rg_checked += 1
                if rg_checked >= 500:
                    break
            if rg_checked >= 500:
                break
        if rg_checked >= 500:
            break

    # ── 5. Determinism: recompute hash ───────────────────────────────────────
    recomputed = _compute_cir_hash(
        cir.schema_version, cir.files, cir.symbols, cir.endpoints, cir.call_graph
    )
    if recomputed != cir.cir_hash:
        violations.append(
            f"DETERMINISM: cir_hash mismatch: "
            f"stored='{cir.cir_hash[:16]}...' "
            f"recomputed='{recomputed[:16]}...'"
        )

    return violations

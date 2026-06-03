"""spring_impact.py — Impact-chain: symbol → systemic blast radius with semantic enrichment.

Bridges cir.reverse_graph (caller BFS) with SpringSemanticModel (TX/SEC/endpoint
semantic layer). Produces a single structured query result without re-deriving any
data already present in the model.

Architecture:
  extraction     → CIR (cir.symbols, cir.reverse_graph, cir.endpoints)
  semantic index → SpringSemanticModel (tx_index, endpoint_index, call_adj)
  audit findings → TxPatternEngine + SecurityScanner (run once, filter to call chain)
  orchestration  → ImpactOrchestrator.query()
  serialization  → ImpactChainResult.to_dict()

Usage:
    model = SpringSemanticModel.build(cir)
    result = run_impact_chain(cir, "OrderService#placeOrder", model=model, root=Path("/repo"))
    output = json.dumps(result.to_dict(), indent=2)

Never raises. Returns ImpactChainResult with resolution="not_found" on unknown symbol.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from sourcecode.spring_findings import SEVERITY_ORDER, SpringFinding
from sourcecode.spring_model import SpringSemanticModel

if TYPE_CHECKING:
    from sourcecode.canonical_ir import CanonicalRepositoryIR

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SCHEMA_VERSION = "1.0"

# Edge types excluded from caller BFS.
# contained_in — structural membership (method → enclosing class), not a call.
# imports      — type reference (A imports B as a type); NOT a runtime call.
#                Only appears on class nodes, never method nodes. Including it
#                chains through DTOs and entities that merely reference the service
#                type, inflating caller counts without semantic value.
_SKIP_EDGE_TYPES: frozenset[str] = frozenset({"contained_in", "imports"})

# Max BFS depth guard — caller growth is bounded per _bfs_callers
_BFS_DEFAULT_DEPTH = 4
_BFS_HARD_LIMIT = 8
_BFS_CALLER_CAP = 500  # hub class guard — same threshold as compute_blast_radius

# Severity → numeric weight for risk scoring
_SEVERITY_WEIGHT: dict[str, float] = {
    "critical": 4.0,
    "high": 3.0,
    "medium": 2.0,
    "low": 1.0,
}


# ---------------------------------------------------------------------------
# Output model
# ---------------------------------------------------------------------------

@dataclass
class AffectedEndpoint:
    """HTTP endpoint reachable through the call chain from the queried symbol."""
    endpoint_id: str
    method: str
    path: str
    controller_class: str
    handler_symbol: str
    source_file: str
    security_policy: str  # e.g. "spring_pre_authorize", "none_detected", "unknown"

    def to_dict(self) -> dict:
        return {
            "endpoint_id": self.endpoint_id,
            "method": self.method,
            "path": self.path,
            "controller_class": self.controller_class,
            "handler_symbol": self.handler_symbol,
            "source_file": self.source_file,
            "security_policy": self.security_policy,
        }


@dataclass
class ImpactChainResult:
    """Top-level output of an impact-chain query.

    Stable contract — do not remove or rename fields.
    Add new fields with Optional defaults for backward compatibility.
    """
    schema_version: str = _SCHEMA_VERSION
    symbol: str = ""                           # resolved FQN (or original input if not_found)
    resolution: str = "not_found"              # "exact" | "class_expanded" | "partial" | "not_found"
    direct_callers: list[str] = field(default_factory=list)
    indirect_callers: list[str] = field(default_factory=list)
    endpoints_affected: list[AffectedEndpoint] = field(default_factory=list)
    transaction_boundary: Optional[dict] = None   # TransactionBoundary.to_dict() or None
    security_surfaces: list[dict] = field(default_factory=list)   # per-endpoint security info
    impact_findings: list[dict] = field(default_factory=list)     # SpringFinding.to_dict() filtered
    analysis_warnings: list[str] = field(default_factory=list)
    risk_level: str = "unknown"                # "critical" | "high" | "medium" | "low" | "unknown"
    confidence: str = "high"                   # "high" | "medium" | "low"
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d: dict = {
            "schema_version": self.schema_version,
            "symbol": self.symbol,
            "resolution": self.resolution,
            "direct_callers": self.direct_callers,
            "indirect_callers": self.indirect_callers,
            "endpoints_affected": [ep.to_dict() for ep in self.endpoints_affected],
            "transaction_boundary": self.transaction_boundary,
            "security_surfaces": self.security_surfaces,
            "impact_findings": self.impact_findings,
            "analysis_warnings": self.analysis_warnings,
            "risk_level": self.risk_level,
            "confidence": self.confidence,
            "metadata": self.metadata,
        }
        return d


# ---------------------------------------------------------------------------
# Symbol resolution
# ---------------------------------------------------------------------------

def _class_of(fqn: str) -> str:
    """Extract class FQN from a method FQN.  pkg.Class#method → pkg.Class."""
    if "#" in fqn:
        return fqn.split("#")[0]
    return fqn


def _resolve_symbol(
    symbol: str,
    cir_symbols: list[str],
) -> tuple[str, list[str], list[str]]:
    """Resolve input symbol to one or more CIR FQNs.

    Matching order:
      1. Exact FQN match in cir.symbols.
      2. If input has no '#': expand to all FQNs whose class part equals or ends
         with '.<input>' — covers both the class node and its method nodes.
      3. If input has '#': try matching class + method suffix.
      4. Not found.

    Resolution values:
      "exact"          — full FQN provided and matched exactly.
      "class_expanded" — short class name matched one class by suffix; all its
                         symbols included. Confidence stays high.
      "partial"        — ambiguous (multiple classes matched) or method not found
                         on matched class. Confidence degrades to medium.
      "not_found"      — no match.

    Returns (resolution, matched_fqns, warnings).
    Returned list is always deduplicated (preserving order).
    """
    warnings: list[str] = []

    # 1. Exact FQN
    if symbol in cir_symbols:
        return "exact", [symbol], []

    # Parse optional class/method parts
    if "#" in symbol:
        class_input, method_input = symbol.rsplit("#", 1)
    else:
        class_input, method_input = symbol, ""

    # Build a lookup set of all class FQNs (the part before '#')
    cir_classes: set[str] = {_class_of(s) for s in cir_symbols}

    # 2. Try class match — exact FQN for the class part, or suffix
    def _class_matches(fqn: str) -> bool:
        cls = _class_of(fqn)
        return cls == class_input or cls.endswith("." + class_input)

    class_matched_raw = [s for s in cir_symbols if _class_matches(s)]
    # Deduplicate preserving order (upstream CIR may contain duplicate symbols)
    class_matched = list(dict.fromkeys(class_matched_raw))

    if class_matched:
        # Determine how many distinct classes were matched
        matched_class_fqns: set[str] = {_class_of(s) for s in class_matched}
        unambiguous = len(matched_class_fqns) == 1

        if not method_input:
            # Expand to all symbols of the matched class(es)
            if class_input in cir_classes:
                resolution = "exact"
            elif unambiguous:
                resolution = "class_expanded"
            else:
                resolution = "partial"
            return resolution, class_matched, []
        else:
            # Filter by method name
            method_matched = list(dict.fromkeys(
                s for s in class_matched
                if "#" in s and s.rsplit("#", 1)[1] == method_input
            ))
            if method_matched:
                method_matched_classes = {_class_of(s) for s in method_matched}
                if f"{class_input}#{method_input}" in cir_symbols:
                    resolution = "exact"
                elif len(method_matched_classes) == 1:
                    resolution = "class_expanded"
                else:
                    resolution = "partial"
                return resolution, method_matched, []
            # Class found, method not found → fall back to class-level
            warnings.append(
                f"Method '#{method_input}' not found on matched class(es); "
                "resolved to class-level symbols."
            )
            return "partial", class_matched, warnings

    return "not_found", [], [f"Symbol '{symbol}' not found in CIR."]


# ---------------------------------------------------------------------------
# BFS through reverse graph
# ---------------------------------------------------------------------------

def _bfs_callers(
    seed_fqns: list[str],
    reverse_graph: dict[str, dict[str, list[str]]],
    max_depth: int,
) -> tuple[list[str], list[str], bool]:
    """BFS from seed FQNs through reverse_graph.

    Returns (direct_callers, indirect_callers, was_truncated).
    direct_callers: depth-1 callers (callers of the seed symbols themselves).
    indirect_callers: depth-2+ callers, up to max_depth.
    was_truncated: True when hub-class guard capped the traversal.
    """
    visited: set[str] = set(seed_fqns)
    direct: list[str] = []
    indirect: list[str] = []
    was_truncated = False

    # Hub-class guard: if seeds have > _BFS_CALLER_CAP direct callers combined,
    # cap effective depth to 1 to avoid O(n^depth) explosion.
    total_direct_count = 0
    for seed in seed_fqns:
        entry = reverse_graph.get(seed) or {}
        for etype, fqn_list in entry.items():
            if etype not in _SKIP_EDGE_TYPES:
                total_direct_count += len(fqn_list)

    effective_depth = 1 if total_direct_count > _BFS_CALLER_CAP else max_depth
    if effective_depth < max_depth:
        was_truncated = True

    # Queue: (fqn, depth)
    queue: list[tuple[str, int]] = [(s, 0) for s in seed_fqns]

    while queue:
        fqn, depth = queue.pop(0)
        if depth >= effective_depth:
            continue
        entry = reverse_graph.get(fqn) or {}
        for etype, fqn_list in entry.items():
            if etype in _SKIP_EDGE_TYPES:
                continue
            for caller in fqn_list:
                if caller in visited:
                    continue
                visited.add(caller)
                if depth == 0:
                    direct.append(caller)
                else:
                    indirect.append(caller)
                if depth + 1 < effective_depth:
                    queue.append((caller, depth + 1))

    return direct, indirect, was_truncated


# ---------------------------------------------------------------------------
# Endpoint mapping
# ---------------------------------------------------------------------------

def _collect_endpoints(
    all_callers: list[str],
    seed_fqns: list[str],
    model: SpringSemanticModel,
) -> list[AffectedEndpoint]:
    """Map callers + seeds to affected HTTP endpoints via model.endpoint_index.

    An endpoint is affected if its handler_symbol is in the call chain, OR if
    the seed is the controller class itself (class-level query — all its endpoints
    are in scope).

    Precision rule: a single-method seed only captures endpoints that method
    handles directly, not all endpoints of its controller.
    """
    all_fqns = set(seed_fqns) | set(all_callers)

    # Class-level seeds: controller class nodes (no '#') that are controllers.
    # These arise when the user queries an entire class, not a specific method.
    class_level_controllers: set[str] = {
        fqn for fqn in seed_fqns
        if "#" not in fqn and fqn in model.endpoint_index.controller_fqns
    }

    result: list[AffectedEndpoint] = []
    seen_ep_ids: set[str] = set()

    # Collect candidate controllers: those whose handler_symbol is in the chain
    # OR whose class node was a seed (class-level query).
    candidate_controllers: set[str] = set(class_level_controllers)
    for fqn in all_fqns:
        cls = _class_of(fqn)
        if cls in model.endpoint_index.controller_fqns:
            candidate_controllers.add(cls)

    for controller in sorted(candidate_controllers):
        for ep in model.endpoint_index.endpoints_for(controller):
            handler = getattr(ep, "handler_symbol", "") or ""
            ep_id = getattr(ep, "id", "") or ""

            # Include if: handler is directly in call chain OR controller is a
            # class-level seed (whole-class query, all its endpoints in scope).
            if handler not in all_fqns and controller not in class_level_controllers:
                continue

            if ep_id in seen_ep_ids:
                continue
            seen_ep_ids.add(ep_id)
            security = getattr(ep, "security", None)
            policy = getattr(security, "policy", "unknown") if security else "none_detected"
            result.append(AffectedEndpoint(
                endpoint_id=ep_id,
                method=getattr(ep, "method", ""),
                path=getattr(ep, "path", ""),
                controller_class=controller,
                handler_symbol=handler,
                source_file=model.endpoint_index.source_file(controller),
                security_policy=policy,
            ))

    return result


# ---------------------------------------------------------------------------
# Findings filter
# ---------------------------------------------------------------------------

def _filter_findings(
    all_findings: list[SpringFinding],
    seed_fqns: list[str],
    direct_callers: list[str],
    indirect_callers: list[str],
    affected_endpoints: list[AffectedEndpoint],
) -> list[SpringFinding]:
    """Return findings that are relevant to the queried call chain.

    A finding is relevant if:
    - Its symbol (FQN or class part) is in the call chain.
    - Its evidence references a symbol or endpoint in the call chain.
    - It names a related_symbol in the call chain.
    """
    all_chain_fqns: set[str] = set(seed_fqns) | set(direct_callers) | set(indirect_callers)
    all_chain_classes: set[str] = {_class_of(fqn) for fqn in all_chain_fqns}
    affected_ep_ids: set[str] = {ep.endpoint_id for ep in affected_endpoints}
    affected_controllers: set[str] = {ep.controller_class for ep in affected_endpoints}

    out: list[SpringFinding] = []
    for f in all_findings:
        # Direct symbol match
        if f.symbol in all_chain_fqns or _class_of(f.symbol) in all_chain_classes:
            out.append(f)
            continue
        # Evidence references a symbol in chain
        ev = f.evidence or {}
        if (
            ev.get("outer_symbol") in all_chain_fqns
            or ev.get("inner_symbol") in all_chain_fqns
            or ev.get("controller_class") in all_chain_classes
            or ev.get("controller_class") in affected_controllers
            or ev.get("endpoint_id") in affected_ep_ids
        ):
            out.append(f)
            continue
        # Related symbol in chain
        if any(rs in all_chain_fqns or _class_of(rs) in all_chain_classes
               for rs in (f.related_symbols or [])):
            out.append(f)

    return out


# ---------------------------------------------------------------------------
# Risk computation
# ---------------------------------------------------------------------------

def _compute_risk(
    direct_callers: int,
    indirect_callers: int,
    endpoints_affected: int,
    findings: list[SpringFinding],
) -> tuple[str, float]:
    """Return (risk_level, risk_score).

    Formula (deterministic):
      endpoint_score   = endpoints_affected × 3          (HTTP exposure matters most)
      caller_score     = min(direct × 2 + indirect, 20)  (capped to avoid hub inflation)
      findings_score   = Σ severity_weight per finding    (TX/SEC findings in chain)
    """
    finding_score = sum(_SEVERITY_WEIGHT.get(f.severity, 0.0) for f in findings)
    endpoint_score = endpoints_affected * 3.0
    caller_score = min(direct_callers * 2.0 + indirect_callers * 0.5, 20.0)
    total = finding_score + endpoint_score + caller_score

    if total >= 20.0:
        level = "critical"
    elif total >= 10.0:
        level = "high"
    elif total >= 4.0:
        level = "medium"
    elif total > 0.0:
        level = "low"
    else:
        level = "low"

    return level, round(total, 2)


# ---------------------------------------------------------------------------
# Security surface aggregation
# ---------------------------------------------------------------------------

def _build_security_surfaces(
    endpoints_affected: list[AffectedEndpoint],
    impact_findings: list[SpringFinding],
) -> list[dict]:
    """Per-endpoint security surface with associated finding IDs."""
    finding_by_ep: dict[str, list[str]] = {}
    for f in impact_findings:
        if f.category == "security":
            ep_id = (f.evidence or {}).get("endpoint_id") or ""
            if ep_id:
                finding_by_ep.setdefault(ep_id, []).append(f.id)

    surfaces = []
    for ep in endpoints_affected:
        surfaces.append({
            "endpoint_id": ep.endpoint_id,
            "method": ep.method,
            "path": ep.path,
            "security_policy": ep.security_policy,
            "security_findings": finding_by_ep.get(ep.endpoint_id, []),
        })
    return surfaces


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class ImpactOrchestrator:
    """Stateless query engine: symbol → ImpactChainResult.

    Consumes a pre-built CIR + SpringSemanticModel.  Never re-derives data
    already present in the model.  Pattern analyzers are invoked once and
    their findings filtered to the call chain — no per-symbol audit re-runs.
    """

    def query(
        self,
        cir: CanonicalRepositoryIR,
        model: SpringSemanticModel,
        symbol: str,
        *,
        depth: int = _BFS_DEFAULT_DEPTH,
        root: Optional[Path] = None,
        prebuilt_findings: Optional[list[SpringFinding]] = None,
    ) -> ImpactChainResult:
        """Execute impact-chain query for symbol.

        Args:
            cir:               CanonicalRepositoryIR (from build_canonical_ir).
            model:             Pre-built SpringSemanticModel.
            symbol:            Target FQN, class name, or Class#method.
            depth:             BFS depth for indirect caller traversal.
            root:              Repo root path (for TX-005 source reading).
            prebuilt_findings: Pre-run audit findings (avoids double audit).
                               If None, runs TX + SEC audit internally.
        """
        t0 = time.monotonic()
        depth = max(1, min(depth, _BFS_HARD_LIMIT))
        warnings: list[str] = []

        # ── 1. Resolve symbol ─────────────────────────────────────────────
        resolution, seed_fqns, sym_warnings = _resolve_symbol(symbol, cir.symbols)
        warnings.extend(sym_warnings)

        if resolution == "not_found" or not seed_fqns:
            return ImpactChainResult(
                symbol=symbol,
                resolution="not_found",
                analysis_warnings=warnings or [f"Symbol '{symbol}' not found in CIR."],
                risk_level="unknown",
                confidence="low",
                metadata={"analysis_depth": depth},
            )

        # Canonical symbol: single seed → use it; multiple seeds from one class
        # expansion → use the class FQN; truly ambiguous → keep original input.
        if len(seed_fqns) == 1:
            resolved_symbol = seed_fqns[0]
        else:
            seed_classes = {_class_of(s) for s in seed_fqns}
            resolved_symbol = next(iter(seed_classes)) if len(seed_classes) == 1 else symbol

        # ── 2. BFS through reverse graph ─────────────────────────────────
        direct_callers, indirect_callers, truncated = _bfs_callers(
            seed_fqns, cir.reverse_graph, depth
        )
        if truncated:
            warnings.append(
                "Hub-class guard active: symbol has > 500 direct callers — "
                "indirect caller traversal capped at depth=1."
            )

        # ── 3. Endpoints affected ─────────────────────────────────────────
        all_callers = direct_callers + indirect_callers
        endpoints_affected = _collect_endpoints(all_callers, seed_fqns, model)

        # ── 4. TX boundary for the target symbol ─────────────────────────
        tx_boundary = None
        try:
            boundary = model.tx_index.effective_boundary(resolved_symbol)
            if boundary is None and "#" not in resolved_symbol:
                # Class-level symbol — try class_level directly
                boundary = model.tx_index.class_level.get(resolved_symbol)
            if boundary is not None:
                tx_boundary = boundary.to_dict()
        except Exception:
            pass

        # ── 5. TX + SEC audit findings, filtered to call chain ────────────
        if prebuilt_findings is not None:
            all_findings = prebuilt_findings
        else:
            all_findings = _run_audit_for_chain(cir, model, root)

        impact_findings_raw = _filter_findings(
            all_findings, seed_fqns, direct_callers, indirect_callers, endpoints_affected
        )
        # Sort by severity, then symbol
        impact_findings_raw.sort(
            key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.symbol)
        )
        impact_findings = [f.to_dict() for f in impact_findings_raw]

        # ── 6. Security surfaces ──────────────────────────────────────────
        security_surfaces = _build_security_surfaces(endpoints_affected, impact_findings_raw)

        # ── 7. Risk ───────────────────────────────────────────────────────
        risk_level, risk_score = _compute_risk(
            len(direct_callers),
            len(indirect_callers),
            len(endpoints_affected),
            impact_findings_raw,
        )

        confidence: str
        if resolution == "not_found":
            confidence = "low"
        elif resolution == "partial" or warnings:
            confidence = "medium"
        else:
            confidence = "high"

        elapsed_ms = round((time.monotonic() - t0) * 1000, 2)

        return ImpactChainResult(
            symbol=resolved_symbol,
            resolution=resolution,
            direct_callers=direct_callers,
            indirect_callers=indirect_callers,
            endpoints_affected=endpoints_affected,
            transaction_boundary=tx_boundary,
            security_surfaces=security_surfaces,
            impact_findings=impact_findings,
            analysis_warnings=warnings,
            risk_level=risk_level,
            confidence=confidence,
            metadata={
                "analysis_depth": depth,
                "callers_total": len(direct_callers) + len(indirect_callers),
                "endpoints_total": len(endpoints_affected),
                "findings_in_chain": len(impact_findings),
                "risk_score": risk_score,
                "model_build_time_ms": model.build_time_ms,
                "query_time_ms": elapsed_ms,
            },
        )


def _run_audit_for_chain(
    cir: CanonicalRepositoryIR,
    model: SpringSemanticModel,
    root: Optional[Path],
) -> list[SpringFinding]:
    """Run TX + SEC audit and return combined findings list.

    Uses the pre-built model — no duplicate CIR traversal.
    Never raises; returns [] on any error.
    """
    from sourcecode.spring_security_audit import run_security_audit
    from sourcecode.spring_tx_analyzer import run_tx_audit

    findings: list[SpringFinding] = []
    try:
        tx_result = run_tx_audit(cir, root=root, model=model)
        findings.extend(tx_result.findings)
    except Exception:
        pass
    try:
        sec_result = run_security_audit(cir, root=root, model=model)
        findings.extend(sec_result.findings)
    except Exception:
        pass
    return findings


# ---------------------------------------------------------------------------
# Convenience entry point
# ---------------------------------------------------------------------------

def run_impact_chain(
    cir: CanonicalRepositoryIR,
    symbol: str,
    *,
    depth: int = _BFS_DEFAULT_DEPTH,
    root: Optional[Path] = None,
    model: Optional[SpringSemanticModel] = None,
    prebuilt_findings: Optional[list[SpringFinding]] = None,
) -> ImpactChainResult:
    """Run impact-chain query from a CIR.

    Args:
        cir:               CanonicalRepositoryIR from build_canonical_ir().
        symbol:            FQN, class name, or Class#method to query.
        depth:             Indirect caller BFS depth (1–8, default: 4).
        root:              Repo root Path (for TX-005 source reading).
        model:             Pre-built SpringSemanticModel. Built internally if None.
        prebuilt_findings: Pre-run SpringFinding list. Avoids double audit if
                           impact-chain runs after spring-audit in the same session.

    Returns ImpactChainResult — always JSON-serializable, never raises.
    """
    try:
        if model is None:
            model = SpringSemanticModel.build(cir)
        orchestrator = ImpactOrchestrator()
        return orchestrator.query(
            cir, model, symbol,
            depth=depth, root=root,
            prebuilt_findings=prebuilt_findings,
        )
    except Exception as exc:
        return ImpactChainResult(
            symbol=symbol,
            resolution="not_found",
            analysis_warnings=[f"Internal error: {type(exc).__name__}: {exc}"],
            risk_level="unknown",
            confidence="low",
        )

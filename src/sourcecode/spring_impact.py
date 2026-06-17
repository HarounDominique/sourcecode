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

from sourcecode.fqn_utils import normalize_owner_fqn
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
# implements /  — CH-006: structural type declarations, NOT calls. The reverse edge
# extends        on an interface/base lists its implementors/subclasses; an
#                implementor does not *call* the interface by virtue of implementing
#                it. Traversing these attributes every SIBLING implementor of a shared
#                interface as a "caller". On a high-fanout in-repo hub interface (e.g.
#                halo's CustomEndpoint, 43 implementors) this turned a leaf endpoint
#                into 42 false direct callers / risk:high. Interface→impl expansion that
#                IS wanted (CH-001a/b) flows through ImplementationGraph indices, not
#                through these reverse-graph edges, so excluding them here is loss-free.
_SKIP_EDGE_TYPES: frozenset[str] = frozenset(
    {"contained_in", "imports", "implements", "extends"}
)

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
    implementations: list[str] = field(default_factory=list)  # in-repo subtypes of queried interface/base
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
            "implementations": self.implementations,
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
# CH-003 — value/DTO type blind-spot detection
# ---------------------------------------------------------------------------
# The impact graph models call + DI/injection edges but not *type-usage* edges
# (constructor instantiation `new T()`, field/local-variable type, and method
# return type incl. @ResponseBody). For a service/repository the call+DI edges
# cover the real blast radius; for a value/DTO/response type they cover nothing,
# so its impact is invisible — and an all-zero result reported at confidence=high
# reads as "globally dead" (a dangerous false negative). Until type-usage edges
# are modelled (Fase 22 / CH-002), positively identify plain value types and
# downgrade confidence + warn instead of asserting an empty high-confidence result.
_STEREOTYPE_ANNOTATIONS = frozenset({
    "@Service", "@Repository", "@Controller", "@RestController",
    "@Component", "@Configuration", "@ControllerAdvice",
    "@RestControllerAdvice", "@Bean",
})
_VALUE_TYPE_KINDS = frozenset({"class", "enum", "record"})


# ---------------------------------------------------------------------------
# CH-005 — framework/external-interface DI blind-spot detection
# ---------------------------------------------------------------------------
# When a class implements/extends a type that is NOT an in-repo symbol (a
# framework or library supertype — e.g. Spring Security's RedirectStrategy, a
# servlet Filter, a JPA base), the class is typically invoked polymorphically
# *through that external type* and wired by framework DI/config. No in-repo call
# edge ever names the impl's own method, and ImplementationGraph.build()
# deliberately drops external supertypes (cir_graphs: `to_fqn not in
# known_symbols` → skipped), so CH-001b cannot expand to the interface. Result:
# impact-chain reports 0 callers / risk:low at confidence=high — a dangerous
# false negative, since the real blast radius flows through framework wiring the
# static call-graph never traverses. Detect the external supertype positively,
# warn, and downgrade confidence (parallel to the CH-003 value-type guard).
#
# Inert marker interfaces carry no methods → no polymorphic dispatch → no hidden
# blast radius, so they are excluded to avoid firing on plain Serializable DTOs.
_INERT_MARKER_SUPERTYPES = frozenset({
    "Serializable", "java.io.Serializable",
    "Cloneable", "java.lang.Cloneable",
    "Externalizable", "java.io.Externalizable",
})


def _external_supertypes(cir, class_fqn: str) -> list[str]:
    """Return supertypes of class_fqn that are NOT in-repo symbols.

    Reads raw implements/extends edges from cir.dependencies and keeps only those
    whose target cannot be resolved to a single in-repo class (i.e. framework /
    library types). Mirrors ImplementationGraph's resolution rules (exact FQN
    match, then unambiguous simple-name match) so the internal/external split is
    identical. Inert marker interfaces are dropped. Order-preserving, deduped.
    """
    deps = getattr(cir, "dependencies", None) or []
    known: set[str] = set(getattr(cir, "symbols", None) or [])
    simple_to_fqn: dict[str, list[str]] = {}
    for sym in known:
        if "#" not in sym and "." in sym:
            simple_to_fqn.setdefault(sym.rsplit(".", 1)[1], []).append(sym)

    external: list[str] = []
    for edge in deps:
        if edge.get("type") not in ("implements", "extends"):
            continue
        frm = normalize_owner_fqn((edge.get("from") or "").strip())
        if frm != class_fqn:
            continue
        to = (edge.get("to") or "").strip()
        if not to or ">" in to or "<" in to:
            continue
        simple = to.rsplit(".", 1)[1] if "." in to else to
        if simple in _INERT_MARKER_SUPERTYPES or to in _INERT_MARKER_SUPERTYPES:
            continue
        # Internal if it resolves to exactly one in-repo class (exact or simple-name).
        if to in known:
            continue
        if len(simple_to_fqn.get(simple, [])) == 1:
            continue
        external.append(to)
    return list(dict.fromkeys(external))


def _is_unmodeled_value_type(cir, class_fqn: str, model) -> bool:
    """True iff class_fqn is positively a plain value/DTO type whose blast radius
    flows only through type-usage edges the impact graph does not model.

    Conservative: returns False whenever the type cannot be positively confirmed
    (node metadata absent, stereotype annotation present, recognized Spring role,
    or controller) so spine symbols and incomplete-IR cases keep legacy behaviour.
    """
    graph = (getattr(cir, "_raw_ir", None) or {}).get("graph") or {}
    node = next((n for n in (graph.get("nodes") or []) if n.get("fqn") == class_fqn), None)
    if node is None:
        return False  # cannot confirm — stay conservative (preserves IC-V3)
    if (node.get("symbol_kind") or node.get("type")) not in _VALUE_TYPE_KINDS:
        return False  # interface / annotation / bean-method etc.
    anns = node.get("annotations") or []
    if any(a.split("(", 1)[0] in _STEREOTYPE_ANNOTATIONS for a in anns):
        return False  # Spring stereotype bean — spine participant
    if (node.get("role") or "other") != "other":
        return False  # recognized Spring role (repository/service/controller/mapper)
    controllers = getattr(getattr(model, "endpoint_index", None), "controller_fqns", frozenset())
    if class_fqn in controllers:
        return False
    return True


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
    impl_graph: object | None = None,
) -> tuple[list[str], list[str], bool]:
    """BFS from seed FQNs through reverse_graph.

    Returns (direct_callers, indirect_callers, was_truncated).
    direct_callers: depth-1 callers (callers of the seed symbols themselves).
    indirect_callers: depth-2+ callers, up to max_depth.
    was_truncated: True when hub-class guard capped the traversal.

    CH-002: when an injects edge leads to a field/constructor node (X#<init> or
    X#fieldName), the containing class X is also added to the caller set and BFS
    continues from X.  This resolves the DI traversal gap where contained_in edges
    (which are skipped) were the only path from a field node back to its class.

    BUG-004 fix: when BFS reaches a class-level node (no '#'), callers of that
    class live on method-level keys (e.g. 'Foo#doWork') rather than the class key
    ('Foo').  A class→method-key index is built upfront so the BFS also traverses
    method-level entries when processing a class-level FQN.

    Fase 21-03 (mid-chain impl→interface): when BFS reaches an implementation class
    node, its callers typically inject the *interface* type, so the injects reverse
    edges live on the interface node — not on the impl class.  When `impl_graph` is
    supplied, the interfaces of each class-level node are folded into its edge set so
    BFS crosses the DI boundary (e.g. VetServiceImpl → VetService → VetRestController).
    CH-001b performs the same expansion for the SEED only; this extends it to every
    impl reached during traversal, closing repo→service→controller chains.
    """
    visited: set[str] = set(seed_fqns)
    direct: list[str] = []
    indirect: list[str] = []
    was_truncated = False

    # BUG-004: index class FQN → list of method-level keys in reverse_graph.
    # Callers of Foo#doWork are stored under reverse_graph["Foo#doWork"], never
    # under reverse_graph["Foo"].  Without this index, BFS silently terminates
    # whenever a class-level node is enqueued (e.g. via CH-002 expansion).
    class_method_index: dict[str, list[str]] = {}
    for rg_key in reverse_graph:
        if "#" in rg_key:
            cls = rg_key.split("#")[0]
            class_method_index.setdefault(cls, []).append(rg_key)

    def _edges_for(fqn: str) -> list[tuple[str, list[str]]]:
        """Return all (etype, fqn_list) pairs from reverse_graph for fqn.
        For class-level FQNs also includes method-level entries (BUG-004) and,
        when impl_graph is supplied, the reverse edges of the class's interfaces
        (Fase 21-03 mid-chain impl→interface DI boundary crossing)."""
        edges: list[tuple[str, list[str]]] = list((reverse_graph.get(fqn) or {}).items())
        if "#" not in fqn:
            for mk in class_method_index.get(fqn, []):
                edges.extend((reverse_graph.get(mk) or {}).items())
            if impl_graph is not None:
                for iface in impl_graph.interfaces_of(fqn):
                    edges.extend((reverse_graph.get(iface) or {}).items())
                    for mk in class_method_index.get(iface, []):
                        edges.extend((reverse_graph.get(mk) or {}).items())
        return edges

    # Hub-class guard: cap depth to 1 when the UNIQUE direct caller set exceeds
    # _BFS_CALLER_CAP, to avoid O(n^depth) BFS explosion on high-fanout seeds.
    # Uses unique callers (not raw sum per seed) so that interface-expansion seeds
    # (which add many method-level FQNs sharing the same callers) don't trigger the
    # guard prematurely — a 36-method interface still has ~20 unique calling classes.
    unique_direct_callers: set[str] = set()
    for seed in seed_fqns:
        for etype, fqn_list in _edges_for(seed):
            if etype not in _SKIP_EDGE_TYPES:
                unique_direct_callers.update(fqn_list)

    effective_depth = 1 if len(unique_direct_callers) > _BFS_CALLER_CAP else max_depth
    if effective_depth < max_depth:
        was_truncated = True

    # Queue: (fqn, depth)
    queue: list[tuple[str, int]] = [(s, 0) for s in seed_fqns]

    def _add_caller(caller: str, depth: int) -> None:
        if caller in visited:
            return
        visited.add(caller)
        if depth == 0:
            direct.append(caller)
        else:
            indirect.append(caller)
        if depth + 1 < effective_depth:
            queue.append((caller, depth + 1))

    while queue:
        fqn, depth = queue.pop(0)
        if depth >= effective_depth:
            continue
        for etype, fqn_list in _edges_for(fqn):
            if etype in _SKIP_EDGE_TYPES:
                continue
            for caller in fqn_list:
                if etype == "injects":
                    # CH-002: field (pkg.Class.field) and constructor (pkg.Class#<init>)
                    # FQNs are injection sites, not callers.  Normalize to owning class so
                    # member FQNs never appear in direct_callers / indirect_callers.
                    _add_caller(normalize_owner_fqn(caller), depth)
                else:
                    _add_caller(caller, depth)

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

    # Class-level controller FQNs (no '#') that appear anywhere in the chain.
    # Two cases produce a class-level controller node in the chain:
    #   1. Seed is the controller class itself (user queried the whole controller).
    #   2. Caller is the controller class node — happens when a service/repository
    #      is injected into a controller: the BFS reverse edge lands on the class
    #      node (e.g. "OwnerController" with no '#'), not a specific method.
    #      All endpoints of that controller are affected because any change to the
    #      injected dependency impacts every handler that uses it.
    class_level_controllers: set[str] = {
        fqn for fqn in all_fqns
        if "#" not in fqn and fqn in model.endpoint_index.controller_fqns
    }

    result: list[AffectedEndpoint] = []
    seen_ep_ids: set[str] = set()

    # Collect candidate controllers: those whose handler_symbol is in the chain
    # OR whose class node appears in the chain (class-level).
    candidate_controllers: set[str] = set(class_level_controllers)
    for fqn in all_fqns:
        cls = _class_of(fqn)
        if cls in model.endpoint_index.controller_fqns:
            candidate_controllers.add(cls)

    for controller in sorted(candidate_controllers):
        for ep in model.endpoint_index.endpoints_for(controller):
            handler = getattr(ep, "handler_symbol", "") or ""
            ep_id = getattr(ep, "id", "") or ""

            # Include if: handler method is in call chain OR the controller's class
            # node appears at class-level in the chain (seed or DI-injected class).
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

    if total >= 25.0:
        level = "critical"
    elif total >= 12.0:
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
        # F-1: not every warning degrades confidence. The CH-001a/b interface↔impl
        # expansion notices are INFORMATIONAL (they describe normal, correct operation)
        # and previously forced every Spring interface/impl query — the common case — down
        # to confidence=medium permanently. Only genuinely degrading conditions (capped
        # traversal) set this flag; resolution=="partial" is handled separately below.
        confidence_reducing = False

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

        # CH-001: expand interface seeds to include in-repo subtypes.
        # When the queried class is an interface (or abstract base), BFS should also
        # start from every descendant symbol so TX boundaries and callers on the
        # concrete impls are found.  CH-001c: the descendant set is the transitive
        # closure of implements + extends edges — concrete impls, sub-interfaces, and
        # subclasses alike — so a base interface query reaches impls hidden behind an
        # intermediate sub-interface (e.g. SpringData repositories).
        impl_graph = getattr(cir, "implementation_graph", None)
        # Track original seed classes BEFORE CH-001a expansion so CH-001b does not
        # cascade through interfaces shared by impl classes added here (false positives).
        original_seed_classes: set[str] = {_class_of(s) for s in seed_fqns}
        # Subtype classes surfaced to the caller as the impacted implementation set.
        subtype_classes_added: set[str] = set()
        if impl_graph is not None:
            seed_classes_ch001 = {_class_of(s) for s in seed_fqns}
            impl_seeds: list[str] = []
            for seed_class in sorted(seed_classes_ch001):
                subtypes = impl_graph.all_subtypes_of(seed_class)
                if not subtypes:
                    continue
                for impl_class in subtypes:
                    if impl_class in subtype_classes_added:
                        continue
                    subtype_classes_added.add(impl_class)
                    for sym in cir.symbols:
                        if _class_of(sym) == impl_class and sym not in set(seed_fqns):
                            impl_seeds.append(sym)
            if impl_seeds:
                seed_fqns = list(dict.fromkeys(seed_fqns + impl_seeds))
                n_classes = len(subtype_classes_added)
                n_syms = len(impl_seeds)
                warnings.append(
                    f"Interface implementation expansion: "
                    f"added {n_syms} symbol(s) from {n_classes} implementation(s)."
                )

        # CH-001b: expand impl seeds to include their interfaces for BFS (BUG-IC-002).
        # Callers typically inject the interface type, so reverse-graph edges live on
        # the interface node, not on the implementation node.  Without this expansion,
        # querying 'OrderServiceImpl' finds 0 callers even though 36 classes inject it.
        # IMPORTANT: only expand ORIGINAL user-query seeds, not classes added by CH-001a.
        # Expanding CH-001a-added impls cascades through shared utility interfaces
        # (e.g. RefByUuid) and produces false-positive callers from sibling implementors.
        if impl_graph is not None:
            current_seed_classes = {_class_of(s) for s in seed_fqns}
            iface_seeds: list[str] = []
            iface_classes_added: set[str] = set()
            for seed_class in sorted(original_seed_classes):
                ifaces = impl_graph.interfaces_of(seed_class)
                for iface_class in ifaces:
                    if iface_class in iface_classes_added or iface_class in current_seed_classes:
                        continue
                    iface_classes_added.add(iface_class)
                    for sym in cir.symbols:
                        if _class_of(sym) == iface_class and sym not in set(seed_fqns):
                            iface_seeds.append(sym)
            if iface_seeds:
                seed_fqns = list(dict.fromkeys(seed_fqns + iface_seeds))
                n_classes = len(iface_classes_added)
                n_syms = len(iface_seeds)
                warnings.append(
                    f"Implementation-to-interface expansion (CH-001b): "
                    f"added {n_syms} symbol(s) from {n_classes} interface(s) for caller BFS."
                )

        # ── 2. BFS through reverse graph ─────────────────────────────────
        direct_callers, indirect_callers, truncated = _bfs_callers(
            seed_fqns, cir.reverse_graph, depth, impl_graph=impl_graph
        )
        if truncated:
            warnings.append(
                "Hub-class guard active: symbol has > 500 direct callers — "
                "indirect caller traversal capped at depth=1."
            )
            confidence_reducing = True  # capped traversal → result is incomplete

        # ── 3. Endpoints affected ─────────────────────────────────────────
        all_callers = direct_callers + indirect_callers
        endpoints_affected = _collect_endpoints(all_callers, seed_fqns, model)

        # ── 4. TX boundary for the target symbol ─────────────────────────
        tx_boundary = None
        try:
            boundary = model.tx_index.effective_boundary(resolved_symbol)
            if boundary is None and "#" not in resolved_symbol:
                # Class-level symbol — try class_level directly, then fall back
                # to first method-level boundary if class has only method-level TX.
                boundary = model.tx_index.class_level.get(resolved_symbol)
                if boundary is None:
                    method_boundaries = model.tx_index.by_class.get(resolved_symbol, [])
                    if method_boundaries:
                        boundary = method_boundaries[0]
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

        # Empty blast radius is ambiguous: genuinely-unused code OR an unmodeled-edge
        # blind spot. Two positively-detected blind spots reclassify it from a
        # high-confidence "safe to change" into a low-confidence "look further".
        empty_blast = (
            not direct_callers and not indirect_callers
            and not endpoints_affected and not subtype_classes_added
        )
        class_level_seed = "#" not in resolved_symbol and resolution != "not_found"

        # CH-005: framework/external-interface DI blind spot. Checked first because
        # its diagnosis (polymorphic invocation via an external supertype + framework
        # wiring) is more specific than the value-type fallback for the same symbol.
        external_supertypes: list[str] = []
        if empty_blast and class_level_seed:
            external_supertypes = _external_supertypes(cir, resolved_symbol)
        framework_di_blind_spot = bool(external_supertypes)
        if framework_di_blind_spot:
            warnings.append(
                "Framework/external-interface DI blind spot (CH-005): this class "
                "implements/extends external type(s) [" + ", ".join(external_supertypes)
                + "] and is likely invoked polymorphically through them and wired by "
                "framework DI/config. The static call-graph has no in-repo edge naming "
                "this class's methods, so 0 callers is NOT proof it is unused — search "
                "DI/security/config wiring for the supertype to find the real callers."
            )

        # CH-003: empty blast radius on a positively-identified value/DTO type is a
        # type-usage blind spot, not proof of dead code — warn + drop confidence.
        value_type_blind_spot = (
            empty_blast
            and class_level_seed
            and not framework_di_blind_spot
            and _is_unmodeled_value_type(cir, resolved_symbol, model)
        )
        if value_type_blind_spot:
            warnings.append(
                "Type-usage edges not modeled (CH-003): this type's blast radius flows "
                "through instantiation (new T()), field/local types, and method return "
                "types (incl. @ResponseBody) — edges impact-chain does not yet track. "
                "An empty result is NOT proof the type is unused."
            )

        # G-2 residual guard: something imports this symbol but no call/DI/instantiation
        # edge resolved to it. With static-call edges now extracted, the common static
        # utility case is covered; this catches what remains (static imports invoked
        # without a qualifier, reflection, method references) — usages the call-graph
        # cannot bind. An empty blast radius here is NOT proof of dead code, so it must
        # not be reported as a high-confidence "safe to change".
        unresolved_ref_blind_spot = False
        if (
            empty_blast
            and class_level_seed
            and not framework_di_blind_spot
            and not value_type_blind_spot
        ):
            _rev = cir.reverse_graph.get(resolved_symbol) or {}
            _importers = sorted(set(_rev.get("imports") or []))
            if _importers:
                unresolved_ref_blind_spot = True
                warnings.append(
                    f"Unresolved inbound references (G-2): {len(_importers)} in-repo "
                    "file(s) import this symbol but no call/DI/instantiation edge "
                    "resolves to it — the usage may be a static import, reflection, or "
                    "method reference the call-graph does not model. 0 callers is NOT "
                    "proof this symbol is unused."
                )

        confidence: str
        if resolution == "not_found":
            confidence = "low"
        elif framework_di_blind_spot or value_type_blind_spot or unresolved_ref_blind_spot:
            confidence = "low"
        elif resolution == "partial" or confidence_reducing:
            confidence = "medium"
        else:
            confidence = "high"

        elapsed_ms = round((time.monotonic() - t0) * 1000, 2)

        return ImpactChainResult(
            symbol=resolved_symbol,
            resolution=resolution,
            direct_callers=direct_callers,
            indirect_callers=indirect_callers,
            implementations=sorted(subtype_classes_added),
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
                "blind_spots": (
                    (["framework_di"] if framework_di_blind_spot else [])
                    + (["value_type"] if value_type_blind_spot else [])
                    + (["unresolved_refs"] if unresolved_ref_blind_spot else [])
                ),
                "external_supertypes": external_supertypes,
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

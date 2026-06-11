"""spring_model.py — Shared Spring semantic model.

Builds once per analysis run from a CanonicalRepositoryIR.
All pattern analyzers consume this rather than re-deriving shared structures.

Components:
  CallAdjacency      — forward call adjacency (caller → callees)
  InheritanceGraph   — extends/implements graph with generic-parent detection
  BeanGraph          — Spring bean registry + injection graph
  EndpointIndex      — endpoints pre-indexed by controller (replaces O(n) scans)
  EventGraph         — Spring event topology (foundation for EVT-001/002)
  SpringSemanticModel — umbrella: all sub-models built once per run

Eliminates per-pattern duplicate traversals:
  build_tx_index()           was called 2× per scope=all run → now 1×
  _build_forward_adjacency() was called 3× (TX-002/003/004) → now 1×
  _build_extends_map()       was called per-run (SEC-002)    → now 1×
  controller source_file scan was O(n) per finding (SEC-001/002) → now 1×

Usage:
    model = SpringSemanticModel.build(cir)
    # or with pre-built tx_index (avoids double-build in CLI):
    model = SpringSemanticModel.build(cir, tx_index=existing_index)
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from sourcecode.spring_semantic import TransactionBoundaryIndex, build_tx_index

if TYPE_CHECKING:
    from sourcecode.canonical_ir import CanonicalRepositoryIR

# Edge types excluded from forward adjacency (structural, not call edges)
_CALL_SKIP: frozenset[str] = frozenset({"annotated_with", "mapped_to", "contained_in"})

# Spring bean stereotype annotations
_BEAN_ANNOTATIONS: frozenset[str] = frozenset({
    "@Component", "@Service", "@Repository",
    "@Controller", "@RestController", "@Configuration", "@Bean",
    # JPA persistence annotations — not Spring beans but need stereotype recognition in explain
    "@Entity", "@MappedSuperclass", "@Embeddable",
})

# JPA stereotypes that are NOT Spring IoC beans — present in any JPA project (Quarkus, JEE, etc.)
_JPA_ONLY_STEREOTYPES: frozenset[str] = frozenset({"entity", "mappedsuperclass", "embeddable"})

_GENERIC_PARAM_RE = re.compile(r"<[A-Z][\w,\s<>?]*>")


# ---------------------------------------------------------------------------
# CallAdjacency
# ---------------------------------------------------------------------------

@dataclass
class CallAdjacency:
    """Forward call adjacency built from CIR call_graph edges.

    Shared across TX patterns (TX-002, TX-003, TX-004) to avoid one rebuild
    per pattern per analysis run.
    """
    adjacency: dict[str, list[str]] = field(default_factory=dict)  # caller → [callees]

    @classmethod
    def build(cls, cir: "CanonicalRepositoryIR") -> "CallAdjacency":
        adj: dict[str, list[str]] = {}
        for edge in cir.call_graph:
            if not isinstance(edge, dict):
                continue
            if edge.get("type") in _CALL_SKIP:
                continue
            frm = edge.get("from") or ""
            to = edge.get("to") or ""
            if frm and to:
                adj.setdefault(frm, []).append(to)
        return cls(adjacency=adj)


# ---------------------------------------------------------------------------
# InheritanceGraph
# ---------------------------------------------------------------------------

@dataclass
class InheritanceGraph:
    """Extends/implements graph derived from CIR dependency edges.

    Provides inheritance relationships for SEC-002 (@PreAuthorize on generic
    supertype) and future self-invocation detection.

    generic_parents: FQNs whose immediate parent signature has type parameters.
    Computed once; avoids per-pattern regex matching at analysis time.
    """
    parent_of: dict[str, str] = field(default_factory=dict)    # child FQN → parent signature
    generic_parents: set[str] = field(default_factory=set)      # FQNs with a generic parent

    @classmethod
    def build(cls, cir: "CanonicalRepositoryIR") -> "InheritanceGraph":
        parent_of: dict[str, str] = {}
        generic_parents: set[str] = set()
        for edge in cir.dependencies:
            if not isinstance(edge, dict):
                continue
            if edge.get("type") != "extends":
                continue
            child = edge.get("from") or ""
            parent = edge.get("to") or ""
            if child and parent:
                parent_of[child] = parent
                if _GENERIC_PARAM_RE.search(parent):
                    generic_parents.add(child)
        return cls(parent_of=parent_of, generic_parents=generic_parents)

    def immediate_parent(self, fqn: str) -> str:
        """Return the immediate parent signature for fqn, or empty string."""
        return self.parent_of.get(fqn, "")

    def has_generic_parent(self, fqn: str) -> bool:
        """True when the immediate parent of fqn has type parameters."""
        return fqn in self.generic_parents


# ---------------------------------------------------------------------------
# BeanGraph
# ---------------------------------------------------------------------------

@dataclass
class BeanNode:
    """Minimal representation of a detected Spring bean."""
    fqn: str
    stereotype: str    # component|service|repository|controller|configuration|bean
    source_file: str


@dataclass
class BeanGraph:
    """Spring bean registry derived from _raw_ir graph nodes.

    Foundation for future capabilities:
      - self-invocation detection (proxy cannot intercept this.method())
      - conditional bean analysis (@ConditionalOn* chains)
      - module impact analysis (inter-bean dependency tracing)

    injections: @Autowired / constructor injection edges (type="injects").
    """
    beans: dict[str, BeanNode] = field(default_factory=dict)        # fqn → node
    injections: dict[str, list[str]] = field(default_factory=dict)  # fqn → [injected FQNs]

    @classmethod
    def build(cls, cir: "CanonicalRepositoryIR") -> "BeanGraph":
        beans: dict[str, BeanNode] = {}
        injections: dict[str, list[str]] = {}

        raw_ir = getattr(cir, "_raw_ir", {}) or {}
        nodes = (raw_ir.get("graph") or {}).get("nodes") or []

        # Pass 1: build meta-bean-annotation map from annotation-type nodes.
        # e.g. @DomainService (annotated with @Service) maps "@DomainService" → "service"
        _meta_bean_stereotype: dict[str, str] = {}
        for node in nodes:
            if not isinstance(node, dict):
                continue
            if (node.get("symbol_kind") or node.get("type") or "") != "annotation":
                continue
            _ann_set = set(node.get("annotations") or [])
            _match = _ann_set & _BEAN_ANNOTATIONS
            if not _match:
                continue
            _fqn = node.get("fqn") or ""
            if not _fqn:
                continue
            _simple = "@" + _fqn.split(".")[-1]
            _bean_ann = next(iter(_match))
            _meta_bean_stereotype[_simple] = _bean_ann.lstrip("@").lower()

        # Pass 2: collect all bean nodes (direct or via meta-annotation).
        for node in nodes:
            if not isinstance(node, dict):
                continue
            if (node.get("symbol_kind") or node.get("type") or "") == "annotation":
                continue  # annotation-type nodes are not beans
            ann_set = set(node.get("annotations") or [])
            match = ann_set & _BEAN_ANNOTATIONS
            if not match:
                meta_match = ann_set & set(_meta_bean_stereotype)
                if not meta_match:
                    continue
                ann = next(iter(meta_match))
                stereotype = _meta_bean_stereotype[ann]
            else:
                ann = next(iter(match))
                stereotype = ann.lstrip("@").lower()
            fqn = node.get("fqn") or ""
            if not fqn:
                continue
            beans[fqn] = BeanNode(
                fqn=fqn,
                stereotype=stereotype,
                source_file=node.get("source_file") or "",
            )

        for edge in cir.call_graph:
            if not isinstance(edge, dict):
                continue
            if edge.get("type") != "injects":
                continue
            frm = edge.get("from") or ""
            to = edge.get("to") or ""
            if frm and to and frm in beans:
                injections.setdefault(frm, []).append(to)

        return cls(beans=beans, injections=injections)

    def is_bean(self, fqn: str) -> bool:
        return fqn in self.beans

    def has_spring_beans(self) -> bool:
        """True only if Spring IoC beans exist — JPA entities do not count."""
        return any(b.stereotype not in _JPA_ONLY_STEREOTYPES for b in self.beans.values())

    def get_stereotype(self, fqn: str) -> str:
        node = self.beans.get(fqn)
        return node.stereotype if node else ""


# ---------------------------------------------------------------------------
# EndpointIndex
# ---------------------------------------------------------------------------

@dataclass
class EndpointIndex:
    """Endpoints pre-indexed from CIR, built once per analysis run.

    Replaces per-pattern O(n) cir.files scans and ad-hoc controller_fqns set
    comprehensions in SEC-001/002/003. Foundation for future EVT, impact-chain,
    and test-selection patterns that need controller → endpoint chains.

    by_controller:       controller_fqn → [CanonicalEndpoint]
    source_by_controller: controller_fqn → source file path (best-effort)
    controller_fqns:     frozenset for O(1) membership tests (SEC-003)
    """
    by_controller: dict[str, list] = field(default_factory=dict)
    source_by_controller: dict[str, str] = field(default_factory=dict)
    controller_fqns: frozenset = field(default_factory=frozenset)

    @classmethod
    def build(cls, cir: "CanonicalRepositoryIR") -> "EndpointIndex":
        by_controller: dict[str, list] = {}
        source_by_controller: dict[str, str] = {}

        for ep in (getattr(cir, "endpoints", None) or []):
            fqn = getattr(ep, "controller_class", "") or ""
            if not fqn:
                continue
            by_controller.setdefault(fqn, []).append(ep)
            if fqn not in source_by_controller:
                sf = getattr(ep, "source_file", "") or ""
                if not sf:
                    simple = fqn.split(".")[-1]
                    for path in (getattr(cir, "files", None) or []):
                        if isinstance(path, str) and (
                            path.endswith(f"{simple}.java") or path.endswith(f"{simple}.kt")
                        ):
                            sf = path
                            break
                source_by_controller[fqn] = sf

        return cls(
            by_controller=by_controller,
            source_by_controller=source_by_controller,
            controller_fqns=frozenset(by_controller.keys()),
        )

    def source_file(self, controller_fqn: str) -> str:
        """Return source file for controller_fqn, or empty string."""
        return self.source_by_controller.get(controller_fqn, "")

    def endpoints_for(self, controller_fqn: str) -> list:
        """Return endpoints declared on controller_fqn."""
        return self.by_controller.get(controller_fqn, [])


# ---------------------------------------------------------------------------
# EventGraph
# ---------------------------------------------------------------------------

_EVENT_EDGE_TYPES: frozenset[str] = frozenset({"publishes_event", "listens_to_event"})


@dataclass
class EventGraph:
    """Spring event topology built from call_graph edges.

    Foundation for EVT-001 (@EventListener chains) and EVT-002 patterns.
    BeanGraph is a prerequisite (already in SpringSemanticModel).

    publishers:  event_type → [FQNs of publishing symbols]
    listeners:   event_type → [FQNs of listening symbols]
    event_types: all event types seen (union of publisher + listener keys)
    total_edges: total publishes_event + listens_to_event edge count
    """
    publishers: dict[str, list[str]] = field(default_factory=dict)
    listeners: dict[str, list[str]] = field(default_factory=dict)
    event_types: frozenset[str] = field(default_factory=frozenset)
    total_edges: int = 0

    @classmethod
    def build(cls, cir: "CanonicalRepositoryIR") -> "EventGraph":
        publishers: dict[str, list[str]] = {}
        listeners: dict[str, list[str]] = {}
        total = 0

        for edge in (getattr(cir, "call_graph", None) or []):
            if not isinstance(edge, dict):
                continue
            etype = edge.get("type") or ""
            if etype not in _EVENT_EDGE_TYPES:
                continue
            frm = edge.get("from") or ""
            to = edge.get("to") or ""
            if not frm or not to:
                continue
            if etype == "publishes_event":
                publishers.setdefault(to, []).append(frm)
            else:
                listeners.setdefault(to, []).append(frm)
            total += 1

        return cls(
            publishers=publishers,
            listeners=listeners,
            event_types=frozenset(publishers.keys()) | frozenset(listeners.keys()),
            total_edges=total,
        )

    def publishers_of(self, event_type: str) -> list[str]:
        """Return FQNs that publish event_type."""
        return self.publishers.get(event_type, [])

    def listeners_of(self, event_type: str) -> list[str]:
        """Return FQNs that listen for event_type."""
        return self.listeners.get(event_type, [])

    def has_events(self) -> bool:
        return self.total_edges > 0


# ---------------------------------------------------------------------------
# SpringSemanticModel
# ---------------------------------------------------------------------------

@dataclass
class SpringSemanticModel:
    """Shared semantic context. Built once per analysis run.

    Eliminates duplicate CIR traversals when multiple patterns run together.
    Pattern analyzers that receive a model should consume it rather than
    re-deriving from the CIR.

    Fields:
        tx_index:       @Transactional boundary index.
        call_adj:       Forward call adjacency (caller → callees).
        inheritance:    Extends/implements graph + generic-parent detection.
        bean_graph:     Spring bean registry and injection graph.
        endpoint_index: Endpoints pre-indexed by controller (SEC patterns, EVT).
        event_graph:    Spring event topology for EVT-001/002 patterns.
        build_time_ms:  Wall-clock ms to build all sub-models (not counting
                        a pre-built tx_index passed by the caller).
    """
    tx_index: TransactionBoundaryIndex
    call_adj: CallAdjacency
    inheritance: InheritanceGraph
    bean_graph: BeanGraph
    endpoint_index: EndpointIndex
    event_graph: EventGraph
    build_time_ms: float = 0.0

    @classmethod
    def build(
        cls,
        cir: "CanonicalRepositoryIR",
        *,
        tx_index: Optional[TransactionBoundaryIndex] = None,
    ) -> "SpringSemanticModel":
        """Build all sub-models from a CIR. Never raises.

        Args:
            cir:      CanonicalRepositoryIR from build_canonical_ir().
            tx_index: Pre-built index to reuse — avoids double build in CLI
                      when tx_index was already computed for another purpose.
        """
        t0 = time.monotonic()

        try:
            tx = tx_index if tx_index is not None else build_tx_index(cir)
        except Exception:
            tx = TransactionBoundaryIndex()

        try:
            adj = CallAdjacency.build(cir)
        except Exception:
            adj = CallAdjacency()

        try:
            inh = InheritanceGraph.build(cir)
        except Exception:
            inh = InheritanceGraph()

        try:
            bg = BeanGraph.build(cir)
        except Exception:
            bg = BeanGraph()

        try:
            ei = EndpointIndex.build(cir)
        except Exception:
            ei = EndpointIndex()

        try:
            eg = EventGraph.build(cir)
        except Exception:
            eg = EventGraph()

        elapsed = round((time.monotonic() - t0) * 1000, 2)
        return cls(
            tx_index=tx,
            call_adj=adj,
            inheritance=inh,
            bean_graph=bg,
            endpoint_index=ei,
            event_graph=eg,
            build_time_ms=elapsed,
        )

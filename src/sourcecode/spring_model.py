"""spring_model.py — Shared Spring semantic model.

Builds once per analysis run from a CanonicalRepositoryIR.
All pattern analyzers consume this rather than re-deriving shared structures.

Components:
  CallAdjacency      — forward call adjacency (caller → callees)
  InheritanceGraph   — extends/implements graph with generic-parent detection
  BeanGraph          — Spring bean registry + injection graph
  SpringSemanticModel — umbrella: tx_index + call_adj + inheritance + bean_graph

Eliminates per-pattern duplicate traversals:
  build_tx_index()        was called 2× per scope=all run → now 1×
  _build_forward_adjacency() was called 3× (TX-002/003/004) → now 1×
  _build_extends_map()    was called per-run (SEC-002)    → now 1×

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
})

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

        for node in nodes:
            if not isinstance(node, dict):
                continue
            ann_set = set(node.get("annotations") or [])
            match = ann_set & _BEAN_ANNOTATIONS
            if not match:
                continue
            fqn = node.get("fqn") or ""
            if not fqn:
                continue
            ann = next(iter(match))
            stereotype = ann.lstrip("@").lower()
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

    def get_stereotype(self, fqn: str) -> str:
        node = self.beans.get(fqn)
        return node.stereotype if node else ""


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
        tx_index:      @Transactional boundary index.
        call_adj:      Forward call adjacency (caller → callees).
        inheritance:   Extends/implements graph + generic-parent detection.
        bean_graph:    Spring bean registry and injection graph.
        build_time_ms: Wall-clock ms to build all sub-models (not counting
                       a pre-built tx_index passed by the caller).
    """
    tx_index: TransactionBoundaryIndex
    call_adj: CallAdjacency
    inheritance: InheritanceGraph
    bean_graph: BeanGraph
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

        elapsed = round((time.monotonic() - t0) * 1000, 2)
        return cls(
            tx_index=tx,
            call_adj=adj,
            inheritance=inh,
            bean_graph=bg,
            build_time_ms=elapsed,
        )

"""context_graph.py — The ContextGraph façade (single source of structural truth).

Purpose
-------
One stable, framework-agnostic entry point through which any component obtains
structural knowledge about a repository:

    source → IR → ContextGraph → all consumers

and never `source → private parser → rule`.

This module is a **façade**, not a second model. It owns no parsing and no data
of its own: it wraps a `CanonicalRepositoryIR` (the already-consolidated IR) and
exposes it through a query API designed to outlive the current Java/Spring
backend. The API speaks in *architectural concepts* — symbols, relations,
evidence, dependencies, calls, annotations, endpoints — not in Spring specifics.
Framework flavour (controller/repository/entity/…) is carried as *data* on the
generic `role` axis, never baked into the API shape, so a future backend for
another language/framework can populate the same graph without breaking any
consumer.

Design constraints honoured (Phase 1 of the ContextGraph migration):
  * No new parsing. Construction delegates entirely to `build_canonical_ir`.
  * No duplicated structure. Lookups reuse the CIR's own indices
    (implementation_graph, injection_graph, reverse_graph, endpoints, …).
  * No behaviour change. This module adds an access path; it removes none yet.
  * Deterministic. Every query returns results in a stable, sorted order.
  * No persistence / incremental / cross-process cache (later phases).

Escape hatch: `.cir` exposes the underlying `CanonicalRepositoryIR` for code not
yet migrated. Later phases shrink direct `.cir` use toward zero.
"""
from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Optional

from sourcecode.canonical_ir import (
    CanonicalEndpoint,
    CanonicalRepositoryIR,
    CanonicalSecurity,
    build_canonical_ir,
)
from sourcecode.repository_ir import find_java_files

# ---------------------------------------------------------------------------
# Concept taxonomy — framework-neutral vocabulary
# ---------------------------------------------------------------------------
# These are the *concept* names the façade guarantees. Backends map their native
# stereotypes onto them. A rule that asks for role="controller" does not care
# whether the backend is Spring MVC, JAX-RS, or something else entirely.

# Structural kinds a Symbol can take (generic; superset across backends).
SYMBOL_KINDS: frozenset[str] = frozenset(
    {
        "class",
        "interface",
        "enum",
        "annotation",
        "method",
        "constructor",
        "field",
        "bean",
        "endpoint",
    }
)

# Relation kinds a Relation can take (generic; matches IR edge `type` values).
RELATION_KINDS: frozenset[str] = frozenset(
    {
        "imports",
        "extends",
        "implements",
        "injects",
        "calls",
        "annotated_with",
        "mapped_to",
        "contained_in",
        "references",
        "returns",
        "instantiates",
        "listens_to_event",
        "publishes_event",
    }
)


# ---------------------------------------------------------------------------
# View objects — immutable, language-neutral projections of IR nodes/edges
# ---------------------------------------------------------------------------
# These are thin read-only views over data the IR already computed. They exist
# so consumers depend on a named, typed concept ("a Symbol has a role") instead
# of on the shape of an internal dict. They copy no logic and add no fields the
# IR did not already produce.


@dataclass(frozen=True)
class Symbol:
    """A named program element — the generic node concept.

    `role` is the framework-neutral stereotype axis (controller/service/
    repository/entity/bean/component/…/other). `kind` is the structural axis
    (class/interface/method/…). Everything else is provenance the IR attached.
    """

    fqn: str
    kind: str
    role: str
    name: str  # canonical (human-readable) name
    source_file: str
    line: Optional[int]
    signature: str
    annotations: tuple[str, ...]
    annotation_values: Mapping[str, str]
    modifiers: tuple[str, ...]
    in_degree: int
    out_degree: int

    @property
    def is_type(self) -> bool:
        return self.kind in ("class", "interface", "enum", "annotation")

    def has_annotation(self, name: str) -> bool:
        """True if annotated with `name` (accepts with/without leading '@',
        matches on the simple annotation name)."""
        want = name.lstrip("@").rsplit(".", 1)[-1]
        for a in self.annotations:
            if a.lstrip("@").rsplit(".", 1)[-1] == want:
                return True
        return False


@dataclass(frozen=True)
class Relation:
    """A directed structural relationship between two symbols — the generic edge
    concept. `kind` is a value from RELATION_KINDS."""

    source: str
    target: str
    kind: str
    confidence: str
    evidence: Mapping[str, object]


@dataclass(frozen=True)
class Evidence:
    """Structural evidence backing a symbol — the generic provenance concept.

    Phase 1 surfaces the always-derivable structural evidence: the incoming and
    outgoing relations that justify the symbol's place in the graph. (The
    richer diff-backed EvidenceBundle remains reachable via `.cir` until a later
    phase promotes it here.)"""

    fqn: str
    incoming: tuple[Relation, ...]
    outgoing: tuple[Relation, ...]

    @property
    def is_grounded(self) -> bool:
        """True when at least one relation connects this symbol to the graph."""
        return bool(self.incoming) or bool(self.outgoing)


# ---------------------------------------------------------------------------
# ContextGraph — the façade
# ---------------------------------------------------------------------------


class ContextGraph:
    """Stable, framework-agnostic query surface over a CanonicalRepositoryIR.

    Construct with `build`/`build_from_root` (delegates to the IR pipeline) or
    wrap an existing CIR with `from_cir`. All queries are pure and deterministic.
    """

    __slots__ = ("_cir", "_nodes_by_fqn", "_nodes", "_build_ms")

    def __init__(self, cir: CanonicalRepositoryIR, *, build_ms: float = 0.0) -> None:
        self._cir = cir
        self._build_ms = build_ms
        # Index the rich node dicts once (the IR already produced them). This is
        # a lookup index over existing data, not a second copy of the model.
        raw_nodes = ((cir._raw_ir.get("graph") or {}).get("nodes")) or []
        self._nodes: tuple[Symbol, ...] = tuple(
            _node_to_symbol(n) for n in raw_nodes
        )
        self._nodes_by_fqn: dict[str, Symbol] = {s.fqn: s for s in self._nodes}

    # -- construction -------------------------------------------------------

    @classmethod
    def build(
        cls,
        file_paths: list[str],
        root: Path,
        *,
        since: Optional[str] = None,
    ) -> "ContextGraph":
        """Build the graph from an explicit Java file list (same inputs the CLI
        commands already resolve). Delegates to `build_canonical_ir` — no new
        parsing path is introduced."""
        t0 = time.perf_counter()
        cir = build_canonical_ir(file_paths, root, since=since)
        build_ms = (time.perf_counter() - t0) * 1000.0
        return cls(cir, build_ms=build_ms)

    @classmethod
    def build_from_root(
        cls,
        root: Path,
        *,
        since: Optional[str] = None,
        max_files: int = 8000,
    ) -> "ContextGraph":
        """Convenience: discover Java files under `root` then build. Uses the
        same `find_java_files` discovery the engine uses."""
        files = find_java_files(root, max_files=max_files)
        return cls.build(files, root, since=since)

    @classmethod
    def from_cir(cls, cir: CanonicalRepositoryIR) -> "ContextGraph":
        """Wrap an already-built CIR (no rebuild). Use when a caller already has
        the canonical IR in hand and wants the query API over it."""
        return cls(cir)

    # -- underlying model (transitional escape hatch) -----------------------

    @property
    def cir(self) -> CanonicalRepositoryIR:
        """The underlying CanonicalRepositoryIR. Transitional: consumers not yet
        migrated read this directly. Later phases drive its use toward zero."""
        return self._cir

    # -- symbols ------------------------------------------------------------

    def symbol(self, fqn: str) -> Optional[Symbol]:
        """Look up a single symbol by fully-qualified name. O(1)."""
        return self._nodes_by_fqn.get(fqn)

    def symbols(
        self,
        *,
        kind: Optional[str] = None,
        role: Optional[str] = None,
        annotated_with: Optional[str] = None,
        name_contains: Optional[str] = None,
    ) -> list[Symbol]:
        """All symbols matching the given generic filters (AND-combined).

        Every filter is optional; with no filter this returns the full symbol
        set. Results are sorted by FQN for determinism. This is the primitive
        the role-convenience methods below are thin sugar over.
        """
        out: list[Symbol] = []
        for s in self._nodes:
            if kind is not None and s.kind != kind:
                continue
            if role is not None and s.role != role:
                continue
            if annotated_with is not None and not s.has_annotation(annotated_with):
                continue
            if name_contains is not None and name_contains not in s.fqn:
                continue
            out.append(s)
        out.sort(key=lambda s: s.fqn)
        return out

    def types(self) -> list[Symbol]:
        """All class/interface/enum/annotation symbols, sorted by FQN."""
        return sorted(
            (s for s in self._nodes if s.is_type), key=lambda s: s.fqn
        )

    def fields_of(self, type_fqn: str) -> list[Symbol]:
        """Annotated field symbols declared directly on a type (the IR emits a
        field node when the field carries at least one annotation). Excludes
        nested-class fields. Sorted by declaration order (source line), so
        consumers see fields as the source declares them."""
        prefix = type_fqn + "."
        out = [
            s
            for s in self._nodes
            if s.kind == "field"
            and s.fqn.startswith(prefix)
            and "." not in s.fqn[len(prefix):]
        ]
        out.sort(key=lambda s: (s.source_file, s.line if s.line is not None else 0, s.fqn))
        return out

    def annotation_types(self) -> list[Symbol]:
        """Annotation-type (`@interface`) declarations, sorted by FQN. Their
        own annotations/annotation_values carry meta-annotations such as
        @Target or @Constraint."""
        return self.symbols(kind="annotation")

    # -- role convenience (framework-neutral sugar over symbols(role=…)) -----
    # Each is a one-line filter on the generic `role` axis. They read naturally
    # for Java/Spring today but impose no framework coupling on the API shape.

    def with_role(self, role: str) -> list[Symbol]:
        return self.symbols(role=role)

    def controllers(self) -> list[Symbol]:
        return self.symbols(role="controller")

    def services(self) -> list[Symbol]:
        return self.symbols(role="service")

    def repositories(self) -> list[Symbol]:
        return self.symbols(role="repository")

    def components(self) -> list[Symbol]:
        return self.symbols(role="component")

    def entities(self) -> list[Symbol]:
        return self.symbols(role="entity")

    def configurations(self) -> list[Symbol]:
        return self.symbols(role="config")

    # -- relations ----------------------------------------------------------

    def relations(
        self,
        *,
        kind: Optional[str] = None,
        source: Optional[str] = None,
        target: Optional[str] = None,
    ) -> list[Relation]:
        """All relations matching the given filters (AND-combined), in the IR's
        stable edge order (already sorted from → type → to)."""
        out: list[Relation] = []
        for e in self._cir.call_graph:
            if kind is not None and e.get("type") != kind:
                continue
            if source is not None and e.get("from") != source:
                continue
            if target is not None and e.get("to") != target:
                continue
            out.append(_edge_to_relation(e))
        return out

    # -- inheritance / implementation (reuses cir.implementation_graph) ------

    def implementations_of(self, interface_fqn: str) -> list[str]:
        """Concrete in-repo implementations of an interface (implements edges
        only — excludes sub-interfaces/subclasses)."""
        return list(self._cir.implementation_graph.implementations_of(interface_fqn))

    def interfaces_of(self, class_fqn: str) -> list[str]:
        """In-repo interfaces a class implements."""
        return list(self._cir.implementation_graph.interfaces_of(class_fqn))

    def subtypes_of(self, type_fqn: str) -> list[str]:
        """Direct in-repo subtypes (implements + extends children)."""
        return list(self._cir.implementation_graph.subtypes_of(type_fqn))

    def all_subtypes_of(self, type_fqn: str) -> list[str]:
        """Transitive closure of in-repo subtypes (BFS, cycle-safe)."""
        return list(self._cir.implementation_graph.all_subtypes_of(type_fqn))

    def supertypes_of(self, type_fqn: str) -> list[str]:
        """Direct in-repo supertypes (implemented/extended)."""
        return list(self._cir.implementation_graph.supertypes_of(type_fqn))

    # -- dependency injection (reuses cir.injection_graph) -------------------

    def injected_dependencies_of(self, class_fqn: str) -> list[str]:
        """Service FQNs injected into a class (field/constructor/setter lifted
        to class level)."""
        return list(self._cir.injection_graph.dependencies_of(class_fqn))

    def dependents_of(self, service_fqn: str) -> list[str]:
        """Class FQNs that inject a given service (class-level)."""
        return list(self._cir.injection_graph.dependents_of(service_fqn))

    # -- call graph (reuses cir.reverse_graph / cir.call_graph) --------------

    def callers_of(
        self, fqn: str, *, kinds: Optional[Iterable[str]] = None
    ) -> list[str]:
        """Symbols with an edge *into* `fqn`. Optionally restrict to edge
        `kinds` (e.g. only 'calls'). Deterministically ordered."""
        by_type = self._cir.reverse_graph.get(fqn) or {}
        want = set(kinds) if kinds is not None else None
        out: set[str] = set()
        for etype, froms in by_type.items():
            if want is not None and etype not in want:
                continue
            out.update(froms)
        return sorted(out)

    def callees_of(
        self, fqn: str, *, kinds: Optional[Iterable[str]] = None
    ) -> list[str]:
        """Symbols `fqn` has an edge *out* to. Optionally restrict to edge
        `kinds`. Deterministically ordered."""
        want = set(kinds) if kinds is not None else None
        out: set[str] = set()
        for e in self._cir.call_graph:
            if e.get("from") != fqn:
                continue
            if want is not None and e.get("type") not in want:
                continue
            to = e.get("to")
            if to:
                out.add(to)
        return sorted(out)

    # -- endpoints (reuses cir.endpoints / cir.security_index) ---------------

    def endpoints(self) -> list[CanonicalEndpoint]:
        """All HTTP endpoints (already canonical + deduped + sorted in the IR)."""
        return list(self._cir.endpoints)

    def endpoints_of(self, controller_fqn: str) -> list[CanonicalEndpoint]:
        """Endpoints handled by a given controller class."""
        return [
            ep for ep in self._cir.endpoints if ep.controller_class == controller_fqn
        ]

    def security_for(self, handler_fqn: str) -> Optional[CanonicalSecurity]:
        """Security policy attached to an endpoint handler, if any."""
        return self._cir.security_index.get(handler_fqn)

    # -- evidence -----------------------------------------------------------

    def evidence_for(self, fqn: str) -> Evidence:
        """Structural evidence grounding a symbol: the relations in and out of
        it. Always derivable from the graph (empty tuples if isolated)."""
        incoming = tuple(
            _edge_to_relation(e)
            for e in self._cir.call_graph
            if e.get("to") == fqn
        )
        outgoing = tuple(
            _edge_to_relation(e)
            for e in self._cir.call_graph
            if e.get("from") == fqn
        )
        return Evidence(fqn=fqn, incoming=incoming, outgoing=outgoing)

    # -- metrics ------------------------------------------------------------

    def metrics(self) -> dict:
        """Structural size + composition of the graph. For Phase 1 measurement;
        derived entirely from already-built data."""
        role_hist = Counter(s.role for s in self._nodes)
        kind_hist = Counter(s.kind for s in self._nodes)
        edge_hist = Counter(e.get("type", "") for e in self._cir.call_graph)
        grounded = sum(
            1
            for s in self._nodes
            if s.in_degree or s.out_degree
        )
        return {
            "cir_hash": self._cir.cir_hash,
            "node_count": len(self._nodes),
            "relation_count": len(self._cir.call_graph),
            "endpoint_count": len(self._cir.endpoints),
            "grounded_node_count": grounded,
            "file_count": len(self._cir.files),
            "build_ms": round(self._build_ms, 2),
            "roles": dict(sorted(role_hist.items())),
            "kinds": dict(sorted(kind_hist.items())),
            "relation_kinds": dict(sorted(edge_hist.items())),
        }

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"ContextGraph(nodes={len(self._nodes)}, "
            f"relations={len(self._cir.call_graph)}, "
            f"endpoints={len(self._cir.endpoints)}, "
            f"hash={self._cir.cir_hash[:12]})"
        )


# ---------------------------------------------------------------------------
# Node/edge → view converters (pure)
# ---------------------------------------------------------------------------


def _node_to_symbol(n: Mapping[str, object]) -> Symbol:
    """Project a raw IR graph node dict onto the neutral Symbol view.

    The IR node carries both a structural `symbol_kind` and a legacy `type`;
    `symbol_kind` is preferred, falling back to `type`.
    """
    kind = str(n.get("symbol_kind") or n.get("type") or "")
    line = n.get("line")
    ann_values = n.get("annotation_values") or {}
    return Symbol(
        fqn=str(n.get("fqn") or ""),
        kind=kind,
        role=str(n.get("role") or "other"),
        name=str(n.get("canonical_name") or n.get("fqn") or ""),
        source_file=str(n.get("source_file") or ""),
        line=line if isinstance(line, int) else None,
        signature=str(n.get("signature") or ""),
        annotations=tuple(n.get("annotations") or ()),
        annotation_values=dict(ann_values) if isinstance(ann_values, dict) else {},
        modifiers=tuple(n.get("modifiers") or ()),
        in_degree=int(n.get("in_degree") or 0),
        out_degree=int(n.get("out_degree") or 0),
    )


def _edge_to_relation(e: Mapping[str, object]) -> Relation:
    """Project a raw IR edge dict onto the neutral Relation view."""
    return Relation(
        source=str(e.get("from") or ""),
        target=str(e.get("to") or ""),
        kind=str(e.get("type") or ""),
        confidence=str(e.get("confidence") or "high"),
        evidence=e.get("evidence") or {},
    )

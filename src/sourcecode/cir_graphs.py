"""cir_graphs.py — Derived graph indices built from CanonicalRepositoryIR.

ImplementationGraph (CH-001): interface → implementation(s) lookup.
InjectionGraph     (CH-002): DI dependency → dependents lookup, with field/constructor lifting.

Both are built from cir.dependencies (implements + injects edges) and are keyed to
known CIR symbols only.  External interfaces (java.io.Serializable, etc.) are excluded.

Architecture constraint: these classes depend only on CIR data.  They must never import
from spring_model, spring_impact, or any semantic layer.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# ImplementationGraph — CH-001
# ---------------------------------------------------------------------------

@dataclass
class ImplementationGraph:
    """Maps interface FQNs to their in-repo implementing classes, and vice-versa.

    Built from implements edges where BOTH ends are known CIR symbols (internal
    interface/class pairs).  External framework interfaces are excluded.
    """
    _impl_of: dict[str, list[str]] = field(default_factory=dict)
    _ifaces_of: dict[str, list[str]] = field(default_factory=dict)

    # ---------------------------------------------------------------------------
    # Queries
    # ---------------------------------------------------------------------------

    def implementations_of(self, interface_fqn: str) -> list[str]:
        """Return FQNs of classes that implement interface_fqn (in-repo only)."""
        return self._impl_of.get(interface_fqn, [])

    def interfaces_of(self, class_fqn: str) -> list[str]:
        """Return FQNs of in-repo interfaces implemented by class_fqn."""
        return self._ifaces_of.get(class_fqn, [])

    def primary_implementation(self, interface_fqn: str) -> str | None:
        """Return the single implementation if unambiguous, else None.

        A single implementation is unambiguous by definition.
        Multiple implementations are ambiguous — callers must decide.
        @Primary detection is not yet implemented (requires annotation data in CIR).
        """
        impls = self._impl_of.get(interface_fqn, [])
        return impls[0] if len(impls) == 1 else None

    def has_implementations(self, interface_fqn: str) -> bool:
        return bool(self._impl_of.get(interface_fqn))

    # ---------------------------------------------------------------------------
    # Builder
    # ---------------------------------------------------------------------------

    @classmethod
    def build(
        cls,
        dependencies: list[dict],
        known_symbols: set[str],
    ) -> ImplementationGraph:
        """Build from CIR dependencies list, restricting to known in-repo symbols.

        Args:
            dependencies:  cir.dependencies — list of edge dicts with 'from'/'to'/'type'
            known_symbols: set(cir.symbols) — only in-repo FQNs

        The Java parser stores 'implements' edges with the simple class name in the 'to'
        field (e.g. 'OrderService') rather than the FQN.  We resolve these via a
        precomputed simple-name → FQN map built from known_symbols.  Only unambiguous
        resolutions are accepted; external framework interfaces and ambiguous names are
        excluded.

        Includes edges where the implementing class (from_fqn) is NOT in known_symbols
        only when the interface IS known — this handles partial-parse edge cases.
        """
        # Pre-build simple-name → [FQN] lookup for class-level symbols only (no '#').
        # Used to resolve unqualified interface names (BUG-IC-001).
        _simple_to_fqn: dict[str, list[str]] = {}
        for sym in known_symbols:
            if "#" not in sym and "." in sym:
                simple = sym.rsplit(".", 1)[1]
                _simple_to_fqn.setdefault(simple, []).append(sym)

        impl_of: dict[str, list[str]] = {}
        ifaces_of: dict[str, list[str]] = {}

        for edge in dependencies:
            if edge.get("type") != "implements":
                continue
            from_fqn = (edge.get("from") or "").strip()
            to_fqn = (edge.get("to") or "").strip()
            if not from_fqn or not to_fqn:
                continue
            # Resolve to_fqn: prefer exact known-symbol match, then try simple-name lookup.
            # Rejects external interfaces (java.*, org.springframework.*) and ambiguous names.
            if to_fqn not in known_symbols:
                candidates = _simple_to_fqn.get(to_fqn, [])
                if len(candidates) != 1:
                    continue
                to_fqn = candidates[0]
            # Ignore malformed FQNs (e.g. generic type fragments like "Long>")
            if ">" in to_fqn or "<" in to_fqn:
                continue
            if ">" in from_fqn or "<" in from_fqn:
                continue

            if from_fqn not in impl_of.get(to_fqn, []):
                impl_of.setdefault(to_fqn, []).append(from_fqn)
            if to_fqn not in ifaces_of.get(from_fqn, []):
                ifaces_of.setdefault(from_fqn, []).append(to_fqn)

        return cls(_impl_of=impl_of, _ifaces_of=ifaces_of)


# ---------------------------------------------------------------------------
# InjectionGraph — CH-002
# ---------------------------------------------------------------------------

@dataclass
class InjectionGraph:
    """Maps DI injection edges to class-level dependency relationships.

    Resolves field FQN and constructor FQN injectors to their enclosing class,
    enabling BFS traversal to continue past injection boundaries.

    Injects edge forms:
      constructor: ClassName#<init> → DependencyFQN
      field:       ClassName#fieldName → DependencyFQN
      lombok:      ClassName → DependencyFQN   (already class-level)
    """
    _deps_of: dict[str, list[str]] = field(default_factory=dict)
    _dependents_of: dict[str, list[str]] = field(default_factory=dict)
    # Maps field/constructor FQN → enclosing class FQN
    _injector_to_class: dict[str, str] = field(default_factory=dict)

    # ---------------------------------------------------------------------------
    # Queries
    # ---------------------------------------------------------------------------

    def dependencies_of(self, class_fqn: str) -> list[str]:
        """Return service FQNs injected into class_fqn (de-duplicated, sorted)."""
        return self._deps_of.get(class_fqn, [])

    def dependents_of(self, service_fqn: str) -> list[str]:
        """Return class FQNs that inject service_fqn (class-level, de-duplicated)."""
        return self._dependents_of.get(service_fqn, [])

    def class_of_injector(self, injector_fqn: str) -> str | None:
        """Resolve a field/constructor FQN to its enclosing class.

        Returns None if injector_fqn is not a known injection site.
        """
        return self._injector_to_class.get(injector_fqn)

    # ---------------------------------------------------------------------------
    # Builder
    # ---------------------------------------------------------------------------

    @classmethod
    def build(cls, dependencies: list[dict]) -> InjectionGraph:
        """Build from CIR dependencies list.

        Args:
            dependencies: cir.dependencies — list of edge dicts with 'from'/'to'/'type'
        """
        deps_of: dict[str, list[str]] = {}
        dependents_of: dict[str, list[str]] = {}
        injector_to_class: dict[str, str] = {}

        for edge in dependencies:
            if edge.get("type") != "injects":
                continue
            from_fqn = (edge.get("from") or "").strip()
            to_fqn = (edge.get("to") or "").strip()
            if not from_fqn or not to_fqn:
                continue

            # Resolve injector to class level
            if "#" in from_fqn:
                class_fqn = from_fqn.rsplit("#", 1)[0]
                injector_to_class[from_fqn] = class_fqn
            else:
                class_fqn = from_fqn

            # Build class → [dep, ...] and service → [class, ...] indices
            deps = deps_of.setdefault(class_fqn, [])
            if to_fqn not in deps:
                deps.append(to_fqn)

            dependents = dependents_of.setdefault(to_fqn, [])
            if class_fqn not in dependents:
                dependents.append(class_fqn)

        return cls(
            _deps_of=deps_of,
            _dependents_of=dependents_of,
            _injector_to_class=injector_to_class,
        )

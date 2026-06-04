"""test_cir_graphs.py — Unit tests for ImplementationGraph and InjectionGraph.

Coverage:
  IG-01  ImplementationGraph.build — single implementation
  IG-02  ImplementationGraph.build — multiple implementations
  IG-03  ImplementationGraph.build — no implementation (empty)
  IG-04  ImplementationGraph.build — external interface excluded (not in known_symbols)
  IG-05  ImplementationGraph.interfaces_of reverse lookup
  IG-06  ImplementationGraph.primary_implementation — single impl → returns impl
  IG-07  ImplementationGraph.primary_implementation — multiple impls → returns None
  IG-08  ImplementationGraph.build — malformed generic fragment excluded
  IG-09  ImplementationGraph.build — simple name (unqualified) to-fqn resolved (BUG-IC-001)
  IG-10  ImplementationGraph.build — ambiguous simple name rejected (BUG-IC-001 safety)
  INJ-01 InjectionGraph.build — constructor injection lifts to class
  INJ-02 InjectionGraph.build — field injection lifts to class
  INJ-03 InjectionGraph.build — Lombok class-level injection (no # in from)
  INJ-04 InjectionGraph.dependencies_of
  INJ-05 InjectionGraph.dependents_of
  INJ-06 InjectionGraph.class_of_injector — constructor node
  INJ-07 InjectionGraph.class_of_injector — field node
  INJ-08 InjectionGraph.class_of_injector — unknown FQN returns None
  INJ-09 InjectionGraph.build — duplicate edges deduplicated
  INJ-10 InjectionGraph.build — non-injects edges ignored
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from sourcecode.cir_graphs import ImplementationGraph, InjectionGraph

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _impl_edge(from_fqn: str, to_fqn: str) -> dict:
    return {"from": from_fqn, "to": to_fqn, "type": "implements", "confidence": "high"}


def _inj_edge(from_fqn: str, to_fqn: str, evidence_type: str = "constructor_param") -> dict:
    return {
        "from": from_fqn,
        "to": to_fqn,
        "type": "injects",
        "confidence": "high",
        "evidence": {"type": evidence_type, "value": to_fqn.split(".")[-1]},
    }


def _ext_edge(from_fqn: str, to_fqn: str, etype: str) -> dict:
    return {"from": from_fqn, "to": to_fqn, "type": etype, "confidence": "high"}


# ---------------------------------------------------------------------------
# IG-01  Single implementation
# ---------------------------------------------------------------------------

class TestImplementationGraphSingleImpl:
    def test_single_impl_found(self) -> None:
        deps = [_impl_edge("com.example.OrderServiceImpl", "com.example.OrderService")]
        known = {"com.example.OrderService", "com.example.OrderServiceImpl"}
        graph = ImplementationGraph.build(deps, known)

        assert graph.implementations_of("com.example.OrderService") == [
            "com.example.OrderServiceImpl"
        ]

    def test_has_implementations_true(self) -> None:
        deps = [_impl_edge("com.example.OrderServiceImpl", "com.example.OrderService")]
        known = {"com.example.OrderService", "com.example.OrderServiceImpl"}
        graph = ImplementationGraph.build(deps, known)
        assert graph.has_implementations("com.example.OrderService") is True


# ---------------------------------------------------------------------------
# IG-02  Multiple implementations
# ---------------------------------------------------------------------------

class TestImplementationGraphMultipleImpls:
    def test_two_impls_returned(self) -> None:
        deps = [
            _impl_edge("com.example.OrderServiceImplA", "com.example.OrderService"),
            _impl_edge("com.example.OrderServiceImplB", "com.example.OrderService"),
        ]
        known = {
            "com.example.OrderService",
            "com.example.OrderServiceImplA",
            "com.example.OrderServiceImplB",
        }
        graph = ImplementationGraph.build(deps, known)
        impls = graph.implementations_of("com.example.OrderService")
        assert "com.example.OrderServiceImplA" in impls
        assert "com.example.OrderServiceImplB" in impls
        assert len(impls) == 2


# ---------------------------------------------------------------------------
# IG-03  No implementation
# ---------------------------------------------------------------------------

class TestImplementationGraphNoImpl:
    def test_empty_for_unknown_interface(self) -> None:
        graph = ImplementationGraph.build([], set())
        assert graph.implementations_of("com.example.SomeService") == []

    def test_has_implementations_false(self) -> None:
        graph = ImplementationGraph.build([], set())
        assert graph.has_implementations("com.example.SomeService") is False


# ---------------------------------------------------------------------------
# IG-04  External interface excluded
# ---------------------------------------------------------------------------

class TestImplementationGraphExternalExcluded:
    def test_java_serializable_excluded(self) -> None:
        deps = [
            _impl_edge("com.example.MyEntity", "java.io.Serializable"),
            _impl_edge("com.example.MyEntity", "com.example.InternalIface"),
        ]
        # java.io.Serializable is NOT in known_symbols (external)
        known = {"com.example.MyEntity", "com.example.InternalIface"}
        graph = ImplementationGraph.build(deps, known)

        assert graph.implementations_of("java.io.Serializable") == []
        assert graph.implementations_of("com.example.InternalIface") == [
            "com.example.MyEntity"
        ]

    def test_spring_framework_interface_excluded(self) -> None:
        deps = [_impl_edge("com.example.MyFormatter", "org.springframework.format.Formatter")]
        known = {"com.example.MyFormatter"}  # spring iface not in known_symbols
        graph = ImplementationGraph.build(deps, known)
        assert graph.implementations_of("org.springframework.format.Formatter") == []


# ---------------------------------------------------------------------------
# IG-05  interfaces_of reverse lookup
# ---------------------------------------------------------------------------

class TestImplementationGraphReverse:
    def test_interfaces_of(self) -> None:
        deps = [
            _impl_edge("com.example.Impl", "com.example.IfaceA"),
            _impl_edge("com.example.Impl", "com.example.IfaceB"),
        ]
        known = {"com.example.Impl", "com.example.IfaceA", "com.example.IfaceB"}
        graph = ImplementationGraph.build(deps, known)
        ifaces = graph.interfaces_of("com.example.Impl")
        assert "com.example.IfaceA" in ifaces
        assert "com.example.IfaceB" in ifaces
        assert len(ifaces) == 2

    def test_interfaces_of_unknown_class(self) -> None:
        graph = ImplementationGraph.build([], set())
        assert graph.interfaces_of("com.example.Unknown") == []


# ---------------------------------------------------------------------------
# IG-06  primary_implementation — single impl
# ---------------------------------------------------------------------------

class TestPrimaryImplementationUnambiguous:
    def test_single_impl_is_primary(self) -> None:
        deps = [_impl_edge("com.example.OrderServiceImpl", "com.example.OrderService")]
        known = {"com.example.OrderService", "com.example.OrderServiceImpl"}
        graph = ImplementationGraph.build(deps, known)
        assert graph.primary_implementation("com.example.OrderService") == (
            "com.example.OrderServiceImpl"
        )


# ---------------------------------------------------------------------------
# IG-07  primary_implementation — multiple impls → None
# ---------------------------------------------------------------------------

class TestPrimaryImplementationAmbiguous:
    def test_multiple_impls_returns_none(self) -> None:
        deps = [
            _impl_edge("com.example.ImplA", "com.example.Iface"),
            _impl_edge("com.example.ImplB", "com.example.Iface"),
        ]
        known = {"com.example.Iface", "com.example.ImplA", "com.example.ImplB"}
        graph = ImplementationGraph.build(deps, known)
        assert graph.primary_implementation("com.example.Iface") is None

    def test_no_impl_returns_none(self) -> None:
        graph = ImplementationGraph.build([], set())
        assert graph.primary_implementation("com.example.Iface") is None


# ---------------------------------------------------------------------------
# IG-08  Malformed generic fragment excluded
# ---------------------------------------------------------------------------

class TestImplementationGraphMalformedExcluded:
    def test_generic_fragment_to_excluded(self) -> None:
        deps = [_impl_edge("com.example.Validator", "Long>")]
        known = {"com.example.Validator"}
        graph = ImplementationGraph.build(deps, known)
        assert graph.implementations_of("Long>") == []
        assert graph.interfaces_of("com.example.Validator") == []

    def test_generic_fragment_from_excluded(self) -> None:
        deps = [_impl_edge("List<String>", "com.example.Iface")]
        known = {"com.example.Iface"}
        graph = ImplementationGraph.build(deps, known)
        assert graph.implementations_of("com.example.Iface") == []


# ---------------------------------------------------------------------------
# INJ-01  Constructor injection lifts to class
# ---------------------------------------------------------------------------

class TestInjectionGraphConstructor:
    def test_constructor_node_lifted(self) -> None:
        deps = [_inj_edge("com.example.ControllerA#<init>", "com.example.ServiceB")]
        graph = InjectionGraph.build(deps)

        assert graph.dependents_of("com.example.ServiceB") == ["com.example.ControllerA"]
        assert graph.dependencies_of("com.example.ControllerA") == ["com.example.ServiceB"]

    def test_class_of_injector_constructor(self) -> None:
        deps = [_inj_edge("com.example.ControllerA#<init>", "com.example.ServiceB")]
        graph = InjectionGraph.build(deps)
        assert graph.class_of_injector("com.example.ControllerA#<init>") == (
            "com.example.ControllerA"
        )


# ---------------------------------------------------------------------------
# INJ-02  Field injection lifts to class
# ---------------------------------------------------------------------------

class TestInjectionGraphField:
    def test_field_node_lifted(self) -> None:
        deps = [_inj_edge("com.example.ServiceImpl#orderService", "com.example.OrderService", "annotation")]
        graph = InjectionGraph.build(deps)

        assert graph.dependents_of("com.example.OrderService") == ["com.example.ServiceImpl"]
        assert graph.dependencies_of("com.example.ServiceImpl") == ["com.example.OrderService"]

    def test_class_of_injector_field(self) -> None:
        deps = [_inj_edge("com.example.ServiceImpl#orderService", "com.example.OrderService", "annotation")]
        graph = InjectionGraph.build(deps)
        assert graph.class_of_injector("com.example.ServiceImpl#orderService") == (
            "com.example.ServiceImpl"
        )


# ---------------------------------------------------------------------------
# INJ-03  Lombok class-level injection (no # in from)
# ---------------------------------------------------------------------------

class TestInjectionGraphLombok:
    def test_lombok_class_level(self) -> None:
        deps = [
            {"from": "com.example.MyService", "to": "com.example.Repo", "type": "injects",
             "confidence": "medium", "evidence": {"type": "lombok_constructor", "value": "@RequiredArgsConstructor"}}
        ]
        graph = InjectionGraph.build(deps)

        assert graph.dependents_of("com.example.Repo") == ["com.example.MyService"]
        assert graph.dependencies_of("com.example.MyService") == ["com.example.Repo"]
        # No injector_to_class entry for Lombok (already class-level)
        assert graph.class_of_injector("com.example.MyService") is None


# ---------------------------------------------------------------------------
# INJ-04  dependencies_of
# ---------------------------------------------------------------------------

class TestInjectionGraphDependenciesOf:
    def test_multiple_deps(self) -> None:
        deps = [
            _inj_edge("com.example.Controller#<init>", "com.example.ServiceA"),
            _inj_edge("com.example.Controller#<init>", "com.example.ServiceB"),
        ]
        graph = InjectionGraph.build(deps)
        result = graph.dependencies_of("com.example.Controller")
        assert "com.example.ServiceA" in result
        assert "com.example.ServiceB" in result
        assert len(result) == 2

    def test_unknown_class_empty(self) -> None:
        graph = InjectionGraph.build([])
        assert graph.dependencies_of("com.example.Unknown") == []


# ---------------------------------------------------------------------------
# INJ-05  dependents_of
# ---------------------------------------------------------------------------

class TestInjectionGraphDependentsOf:
    def test_multiple_dependents(self) -> None:
        deps = [
            _inj_edge("com.example.ControllerA#<init>", "com.example.UserService"),
            _inj_edge("com.example.ControllerB#<init>", "com.example.UserService"),
        ]
        graph = InjectionGraph.build(deps)
        result = graph.dependents_of("com.example.UserService")
        assert "com.example.ControllerA" in result
        assert "com.example.ControllerB" in result
        assert len(result) == 2

    def test_unknown_service_empty(self) -> None:
        graph = InjectionGraph.build([])
        assert graph.dependents_of("com.example.Unknown") == []


# ---------------------------------------------------------------------------
# INJ-06/07  class_of_injector
# ---------------------------------------------------------------------------

class TestClassOfInjector:
    def test_constructor_node_resolved(self) -> None:
        deps = [_inj_edge("com.example.Ctrl#<init>", "com.example.Svc")]
        graph = InjectionGraph.build(deps)
        assert graph.class_of_injector("com.example.Ctrl#<init>") == "com.example.Ctrl"

    def test_field_node_resolved(self) -> None:
        deps = [_inj_edge("com.example.Svc#repo", "com.example.Repo", "annotation")]
        graph = InjectionGraph.build(deps)
        assert graph.class_of_injector("com.example.Svc#repo") == "com.example.Svc"


# ---------------------------------------------------------------------------
# INJ-08  class_of_injector — unknown FQN returns None
# ---------------------------------------------------------------------------

class TestClassOfInjectorUnknown:
    def test_unknown_fqn_returns_none(self) -> None:
        graph = InjectionGraph.build([])
        assert graph.class_of_injector("com.example.NotAnInjector#method") is None


# ---------------------------------------------------------------------------
# INJ-09  Duplicate edges deduplicated
# ---------------------------------------------------------------------------

class TestInjectionGraphDeduplicated:
    def test_duplicate_deps_deduped(self) -> None:
        deps = [
            _inj_edge("com.example.Ctrl#<init>", "com.example.Svc"),
            _inj_edge("com.example.Ctrl#<init>", "com.example.Svc"),
        ]
        graph = InjectionGraph.build(deps)
        assert graph.dependencies_of("com.example.Ctrl") == ["com.example.Svc"]
        assert graph.dependents_of("com.example.Svc") == ["com.example.Ctrl"]


# ---------------------------------------------------------------------------
# INJ-10  Non-injects edges ignored
# ---------------------------------------------------------------------------

class TestInjectionGraphIgnoresOtherEdges:
    def test_imports_edge_ignored(self) -> None:
        deps = [
            _ext_edge("com.example.A", "com.example.B", "imports"),
            _ext_edge("com.example.A", "com.example.C", "extends"),
            _ext_edge("com.example.A", "com.example.D", "implements"),
        ]
        graph = InjectionGraph.build(deps)
        assert graph.dependencies_of("com.example.A") == []
        assert graph.dependents_of("com.example.B") == []


# ---------------------------------------------------------------------------
# IG-09  Simple name resolution (BUG-IC-001)
# ---------------------------------------------------------------------------

class TestImplementationGraphSimpleNameResolution:
    """BUG-IC-001 — implements edges stored with unqualified interface name.

    The Java parser emits 'to' as a simple name (e.g. 'OrderService') rather
    than the FQN.  Before the fix, all such edges were dropped because
    'OrderService' is not in known_symbols (which contains FQNs only).
    After the fix, a precomputed simple-name→FQN map resolves unambiguous names.
    """

    def test_simple_name_resolved_to_fqn(self) -> None:
        """Single unambiguous simple name resolved and edge accepted."""
        deps = [_impl_edge("com.example.OrderServiceImpl", "OrderService")]
        known = {
            "com.example.OrderService",
            "com.example.OrderServiceImpl",
        }
        graph = ImplementationGraph.build(deps, known)
        assert graph.implementations_of("com.example.OrderService") == [
            "com.example.OrderServiceImpl"
        ], "Simple name 'OrderService' must be resolved to FQN"
        assert graph.interfaces_of("com.example.OrderServiceImpl") == [
            "com.example.OrderService"
        ], "interfaces_of reverse lookup must work after simple-name resolution"

    def test_ambiguous_simple_name_rejected(self) -> None:
        """When two classes share a simple name, no resolution — edge dropped."""
        deps = [_impl_edge("com.example.impl.FooImpl", "Foo")]
        known = {
            "com.example.a.Foo",
            "com.example.b.Foo",
            "com.example.impl.FooImpl",
        }
        graph = ImplementationGraph.build(deps, known)
        assert graph.implementations_of("com.example.a.Foo") == []
        assert graph.implementations_of("com.example.b.Foo") == []
        assert graph.interfaces_of("com.example.impl.FooImpl") == [], (
            "Ambiguous simple name must not be resolved"
        )

    def test_external_simple_name_dropped(self) -> None:
        """Simple name with no match in known_symbols (external) is excluded."""
        deps = [_impl_edge("com.example.MyRunnable", "Runnable")]
        known = {"com.example.MyRunnable"}
        graph = ImplementationGraph.build(deps, known)
        assert graph.interfaces_of("com.example.MyRunnable") == [], (
            "External interface 'Runnable' not in known_symbols must be excluded"
        )

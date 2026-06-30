"""explain.py — Human-readable architectural summary for a Java class.

Usage:
    cir = build_canonical_ir(find_java_files(root), root)
    model = SpringSemanticModel.build(cir)
    result = explain_class("UserService", cir, model)
    print(result.render_text())

Derives all data from existing CIR + SpringSemanticModel — no new parsers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from sourcecode.fqn_utils import normalize_owner_fqn

if TYPE_CHECKING:
    from sourcecode.canonical_ir import CanonicalRepositoryIR
    from sourcecode.spring_model import SpringSemanticModel


_STEREOTYPE_DESC: dict[str, str] = {
    "service": "Spring @Service — business logic layer",
    "repository": "Spring @Repository — data access layer",
    "controller": "Spring @Controller — MVC request handler",
    "restcontroller": "Spring @RestController — REST request handler",
    "component": "Spring @Component — general-purpose bean",
    "configuration": "Spring @Configuration — bean factory / config",
    "bean": "Spring @Bean — managed component",
    "entity": "JPA @Entity — persistent domain object mapped to a database table",
    "mappedsuperclass": "JPA @MappedSuperclass — base class sharing persistent state with subclasses",
    "embeddable": "JPA @Embeddable — value object embedded in owning entity table",
}

_SECURITY_ANNOTATION_PREFIXES = (
    "@PreAuthorize", "@PostAuthorize", "@Secured", "@RolesAllowed",
    "@PermitAll", "@DenyAll",
)

_DEFAULT_PROPAGATION = "REQUIRED"


def _simple(fqn: str) -> str:
    """pkg.Foo#bar → Foo#bar; pkg.Foo → Foo."""
    if "." not in fqn and "#" not in fqn:
        return fqn
    # Strip package prefix before simple class name
    if "#" in fqn:
        cls_part, method = fqn.rsplit("#", 1)
        return f"{cls_part.rsplit('.', 1)[-1]}#{method}"
    return fqn.rsplit(".", 1)[-1]


def _method_name(fqn: str) -> str:
    """pkg.Foo#bar → bar."""
    return fqn.rsplit("#", 1)[-1] if "#" in fqn else fqn


# ---------------------------------------------------------------------------
# Output model
# ---------------------------------------------------------------------------

@dataclass
class ClassExplanation:
    """Structured architectural summary for a single class."""

    class_name: str
    class_fqn: str
    stereotype: str
    purpose: str
    public_methods: list[str] = field(default_factory=list)
    incoming_callers: list[str] = field(default_factory=list)
    outgoing_deps: list[str] = field(default_factory=list)
    events_published: list[str] = field(default_factory=list)
    events_consumed: list[str] = field(default_factory=list)
    transactions: list[str] = field(default_factory=list)
    security_constraints: list[str] = field(default_factory=list)
    rest_endpoints: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    found: bool = True

    def render_text(self) -> str:
        lines: list[str] = [f"## {self.class_name}", ""]

        if self.class_fqn != self.class_name:
            lines += [f"**FQN:** `{self.class_fqn}`", ""]

        lines += ["**Purpose:**", self.purpose, ""]

        def _section(title: str, items: list[str]) -> None:
            if not items:
                return
            lines.append(f"**{title}:**")
            for item in items:
                lines.append(f"* {item}")
            lines.append("")

        _section("Public Methods", self.public_methods)
        _section("Used By", self.incoming_callers)
        _section("Calls", self.outgoing_deps)
        _section("Publishes", self.events_published)
        _section("Consumes", self.events_consumed)
        _section("Transactions", self.transactions)
        _section("Security", self.security_constraints)
        _section("REST Endpoints", self.rest_endpoints)
        _section("Warnings", self.warnings)

        return "\n".join(lines).rstrip()

    def to_dict(self) -> dict:
        return {
            "class_name": self.class_name,
            "class_fqn": self.class_fqn,
            "stereotype": self.stereotype,
            "purpose": self.purpose,
            "public_methods": self.public_methods,
            "incoming_callers": self.incoming_callers,
            "outgoing_deps": self.outgoing_deps,
            "events_published": self.events_published,
            "events_consumed": self.events_consumed,
            "transactions": self.transactions,
            "security_constraints": self.security_constraints,
            "rest_endpoints": self.rest_endpoints,
            "warnings": self.warnings,
            "found": self.found,
        }


# ---------------------------------------------------------------------------
# FQN resolution
# ---------------------------------------------------------------------------

def _resolve_fqn(class_name: str, cir: "CanonicalRepositoryIR") -> tuple[str, list[str]]:
    """Find all class FQNs matching simple class_name in cir.symbols.

    Returns (best_fqn, all_matches).
    best_fqn is empty string when no match found.
    """
    suffix_dot = f".{class_name}"
    suffix_hash = f"{class_name}#"
    matches: list[str] = []

    for sym in (getattr(cir, "symbols", None) or []):
        if not isinstance(sym, str):
            continue
        if "#" in sym:
            continue  # method symbol — skip
        # Exact match or package-qualified match
        if sym == class_name or sym.endswith(suffix_dot):
            matches.append(sym)

    # Also scan raw_ir graph nodes for class symbols (more reliable for kind)
    raw_nodes = _get_raw_nodes(cir)
    node_fqns: set[str] = set()
    for node in raw_nodes:
        fqn = node.get("fqn") or ""
        if "#" in fqn:
            continue
        kind = node.get("symbol_kind") or node.get("type") or ""
        if kind not in ("class", "interface", "enum", "annotation", ""):
            continue
        if fqn == class_name or fqn.endswith(suffix_dot):
            node_fqns.add(fqn)

    # Merge — prefer node_fqns when available
    all_fqns = list(dict.fromkeys(list(node_fqns) + matches))

    if not all_fqns:
        return "", []
    return all_fqns[0], all_fqns


def _get_raw_nodes(cir: "CanonicalRepositoryIR") -> list[dict]:
    raw_ir = getattr(cir, "_raw_ir", None) or {}
    return (raw_ir.get("graph") or {}).get("nodes") or []


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------

def _build_purpose(
    class_fqn: str,
    raw_nodes: list[dict],
    stereotype: str,
    cir: "CanonicalRepositoryIR",
    model: "SpringSemanticModel",
) -> str:
    # Collect class-level annotations from raw_ir node
    class_anns: list[str] = []
    parent_sig = ""
    for node in raw_nodes:
        if node.get("fqn") == class_fqn:
            class_anns = node.get("annotations") or []
            break

    # Inheritance
    parent_sig = model.inheritance.immediate_parent(class_fqn)

    # Base description from stereotype
    desc = _STEREOTYPE_DESC.get(stereotype, "")

    # Enrich with class-level @Transactional
    tx_class = model.tx_index.class_level.get(class_fqn)
    if tx_class:
        desc = f"{desc}. Class-level @Transactional ({tx_class.propagation})" if desc else f"@Transactional ({tx_class.propagation})"

    # Enrich with parent
    if parent_sig:
        parent_simple = _simple(parent_sig.split("<")[0])
        if desc:
            desc = f"{desc}. Extends {parent_simple}"
        else:
            desc = f"Extends {parent_simple}"

    # Fallback: derive from annotations present
    if not desc:
        role_anns = [a for a in class_anns if a in (
            "@Service", "@Repository", "@Controller", "@RestController",
            "@Component", "@Configuration",
        )]
        if role_anns:
            desc = f"{role_anns[0]} bean"

    if desc:
        return desc

    # BUG #4 (JobRunr field test): with no recognized framework annotation, the old
    # fallback returned "No stereotype detected — may be a plain class or utility",
    # which is actively misleading for a central domain class. Libraries and clean/
    # hexagonal architectures model rich roles WITHOUT DI annotations. Infer a
    # low-confidence structural role from signals the tool already computes:
    # in-degree (coupling), lifecycle methods, and naming convention.
    structural = _structural_purpose(class_fqn, raw_nodes, cir)
    if structural:
        return structural
    return "No stereotype detected — may be a plain class or utility."


# Lifecycle method names that signal an orchestrator/component managing state.
_LIFECYCLE_METHODS = frozenset({
    "start", "stop", "init", "initialize", "shutdown", "close",
    "pause", "resume", "run", "destroy", "open", "restart",
})

# Class-name suffix → inferred role (no annotation required).
_NAME_ROLE_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("Server", "server/orchestrator"),
    ("Manager", "manager/coordinator"),
    ("Controller", "controller"),
    ("Service", "service"),
    ("Repository", "repository/data access"),
    ("Factory", "factory"),
    ("Builder", "builder"),
    ("Handler", "handler"),
    ("Listener", "listener"),
    ("Provider", "provider"),
    ("Registry", "registry"),
    ("Scheduler", "scheduler"),
    ("Dispatcher", "dispatcher"),
    ("Processor", "processor"),
    ("Filter", "filter"),
    ("Interceptor", "interceptor"),
)


def _structural_purpose(
    class_fqn: str,
    raw_nodes: list[dict],
    cir: "CanonicalRepositoryIR",
) -> str:
    """Infer a low-confidence stereotype from structural signals (no annotations)."""
    try:
        in_degree = len(_build_callers(class_fqn, cir))
    except Exception:
        in_degree = 0

    try:
        method_names = {m.split("(")[0].lower() for m in _build_public_methods(class_fqn, raw_nodes)}
    except Exception:
        method_names = set()
    lifecycle = sorted(method_names & _LIFECYCLE_METHODS)

    simple = _simple(class_fqn)
    role = ""
    for suffix, label in _NAME_ROLE_SUFFIXES:
        if simple.endswith(suffix):
            role = label
            break
    if not role and lifecycle:
        role = "orchestrator/lifecycle component"

    # Require at least one real signal — otherwise stay honestly silent.
    if not role and in_degree < 3:
        return ""

    head = f"Likely {role} (no DI annotations found)" if role else \
        "Likely a structurally significant class (no DI annotations found)"
    signals: list[str] = []
    if in_degree >= 3:
        signals.append(f"high in-degree ({in_degree})")
    elif in_degree:
        signals.append(f"in-degree {in_degree}")
    if lifecycle:
        signals.append(f"lifecycle methods detected ({'/'.join(lifecycle)})")
    suffix = f" — {', '.join(signals)}" if signals else ""
    return f"{head}{suffix} — inferred from structure, not annotations (low confidence)."


def _build_public_methods(class_fqn: str, raw_nodes: list[dict]) -> list[str]:
    """Return public method names for class_fqn from raw_ir nodes."""
    prefix = class_fqn + "#"
    methods: list[str] = []
    for node in raw_nodes:
        fqn = node.get("fqn") or ""
        if not fqn.startswith(prefix):
            continue
        kind = node.get("symbol_kind") or node.get("type") or ""
        if kind not in ("method", "endpoint", "constructor", ""):
            continue
        modifiers: list[str] = node.get("modifiers") or []
        if "public" not in modifiers and "protected" not in modifiers:
            continue
        method_name = fqn[len(prefix):]
        if method_name and not method_name.startswith("<"):
            methods.append(method_name)
    return sorted(set(methods))


def _build_callers(class_fqn: str, cir: "CanonicalRepositoryIR") -> list[str]:
    """DI dependents + reverse call graph callers, deduplicated, simple names."""
    seen: set[str] = set()
    result: list[str] = []

    # DI injection dependents
    for fqn in (getattr(cir, "injection_graph", None) and cir.injection_graph.dependents_of(class_fqn) or []):
        s = _simple(fqn)
        if s not in seen:
            seen.add(s)
            result.append(s)

    # Direct call reverse graph: reverse_graph[target] → {type → [callers]}
    rev: dict = (getattr(cir, "reverse_graph", None) or {}).get(class_fqn) or {}
    for callers in rev.values():
        for caller_fqn in (callers or []):
            # Normalize: field (pkg.Class.field) and method (pkg.Class#method) → class FQN
            cls_fqn = normalize_owner_fqn(caller_fqn)
            if cls_fqn == class_fqn:
                continue
            s = _simple(cls_fqn)
            if s not in seen:
                seen.add(s)
                result.append(s)

    return result


def _build_deps(class_fqn: str, cir: "CanonicalRepositoryIR") -> list[str]:
    """DI injected dependencies, simple names."""
    deps = (getattr(cir, "injection_graph", None) and cir.injection_graph.dependencies_of(class_fqn) or [])
    return sorted({_simple(d) for d in deps})


def _build_events_published(class_fqn: str, model: "SpringSemanticModel") -> list[str]:
    """Event types published by this class (any method of the class)."""
    prefix = class_fqn + "#"
    result: list[str] = []
    for event_type, publishers in model.event_graph.publishers.items():
        for pub in publishers:
            if pub == class_fqn or pub.startswith(prefix):
                result.append(_simple(event_type))
                break
    return sorted(set(result))


def _build_events_consumed(class_fqn: str, model: "SpringSemanticModel") -> list[str]:
    """Event types consumed/listened by this class."""
    prefix = class_fqn + "#"
    result: list[str] = []
    for event_type, listeners in model.event_graph.listeners.items():
        for lst in listeners:
            if lst == class_fqn or lst.startswith(prefix):
                result.append(_simple(event_type))
                break
    return sorted(set(result))


def _build_transactions(class_fqn: str, model: "SpringSemanticModel") -> list[str]:
    """Transaction boundaries for this class."""
    result: list[str] = []

    # Class-level
    cls_tx = model.tx_index.class_level.get(class_fqn)
    if cls_tx:
        label = f"@Transactional (class-level, {cls_tx.propagation})"
        if cls_tx.read_only:
            label += ", readOnly"
        result.append(label)

    # Method-level
    for boundary in (model.tx_index.by_class.get(class_fqn) or []):
        method = _method_name(boundary.symbol)
        label = method + "()"
        extras: list[str] = []
        if boundary.propagation != _DEFAULT_PROPAGATION:
            extras.append(boundary.propagation)
        if boundary.read_only:
            extras.append("readOnly")
        if extras:
            label += f" [{', '.join(extras)}]"
        result.append(label)

    return result


def _build_security(
    class_fqn: str,
    raw_nodes: list[dict],
    cir: "CanonicalRepositoryIR",
) -> list[str]:
    """Security annotations from method-level nodes + cir.security_index."""
    seen: set[str] = set()
    result: list[str] = []

    prefix = class_fqn + "#"

    # From cir.security_index (handler_symbol → CanonicalSecurity)
    for handler_sym, sec in (getattr(cir, "security_index", None) or {}).items():
        if not (handler_sym == class_fqn or handler_sym.startswith(prefix)):
            continue
        policy = getattr(sec, "policy", "") or ""
        roles = getattr(sec, "roles", []) or []
        method = _method_name(handler_sym)
        if roles:
            label = f"{method}(): {policy} ({', '.join(roles)})"
        else:
            label = f"{method}(): {policy}"
        if label not in seen:
            seen.add(label)
            result.append(label)

    # From raw_ir method annotations
    for node in raw_nodes:
        fqn = node.get("fqn") or ""
        if not fqn.startswith(prefix):
            continue
        anns: list[str] = node.get("annotations") or []
        for ann in anns:
            if any(ann.startswith(p) for p in _SECURITY_ANNOTATION_PREFIXES):
                method = _method_name(fqn)
                label = f"{method}(): {ann}"
                if label not in seen:
                    seen.add(label)
                    result.append(label)

    return result


def _build_endpoints(class_fqn: str, model: "SpringSemanticModel") -> list[str]:
    """REST endpoints declared on this controller class."""
    endpoints = model.endpoint_index.endpoints_for(class_fqn)
    result: list[str] = []
    for ep in endpoints:
        method = getattr(ep, "method", "") or "?"
        path = getattr(ep, "path", "") or "?"
        result.append(f"{method} {path}")
    return sorted(set(result))


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def explain_class(
    class_name: str,
    cir: "CanonicalRepositoryIR",
    model: "SpringSemanticModel",
) -> ClassExplanation:
    """Build a ClassExplanation for class_name from existing CIR + model.

    Never raises — wraps all derivation in try/except.
    """
    warnings: list[str] = []

    try:
        class_fqn, all_matches = _resolve_fqn(class_name, cir)
    except Exception:
        class_fqn, all_matches = "", []

    if not class_fqn:
        return ClassExplanation(
            class_name=class_name,
            class_fqn=class_name,
            stereotype="unknown",
            purpose="Class not found in repository symbols.",
            warnings=[f"'{class_name}' not found in CIR symbols. Is this a Java/Kotlin repo?"],
            found=False,
        )

    if len(all_matches) > 1:
        warnings.append(
            f"Ambiguous: {len(all_matches)} classes named '{class_name}'. "
            f"Showing first: {class_fqn}"
        )

    raw_nodes = _get_raw_nodes(cir)

    try:
        stereotype = model.bean_graph.get_stereotype(class_fqn) or "unknown"
    except Exception:
        stereotype = "unknown"

    try:
        purpose = _build_purpose(class_fqn, raw_nodes, stereotype, cir, model)
    except Exception:
        purpose = f"{stereotype} class"

    try:
        public_methods = _build_public_methods(class_fqn, raw_nodes)
    except Exception:
        public_methods = []

    try:
        incoming_callers = _build_callers(class_fqn, cir)
    except Exception:
        incoming_callers = []

    try:
        outgoing_deps = _build_deps(class_fqn, cir)
    except Exception:
        outgoing_deps = []

    try:
        events_published = _build_events_published(class_fqn, model)
    except Exception:
        events_published = []

    try:
        events_consumed = _build_events_consumed(class_fqn, model)
    except Exception:
        events_consumed = []

    try:
        transactions = _build_transactions(class_fqn, model)
    except Exception:
        transactions = []

    try:
        security_constraints = _build_security(class_fqn, raw_nodes, cir)
    except Exception:
        security_constraints = []

    try:
        rest_endpoints = _build_endpoints(class_fqn, model)
    except Exception:
        rest_endpoints = []

    return ClassExplanation(
        class_name=class_name,
        class_fqn=class_fqn,
        stereotype=stereotype,
        purpose=purpose,
        public_methods=public_methods,
        incoming_callers=incoming_callers,
        outgoing_deps=outgoing_deps,
        events_published=events_published,
        events_consumed=events_consumed,
        transactions=transactions,
        security_constraints=security_constraints,
        rest_endpoints=rest_endpoints,
        warnings=warnings,
    )

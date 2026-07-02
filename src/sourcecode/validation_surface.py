"""Validation surface extraction (Phase 20; ContextGraph-backed since Phase 3).

Combines the two sources of bean-validation truth in a Spring repo into one
per-endpoint view an agent can reason about before touching a request body:

  1. **Declarative constraints** carried by the OpenAPI spec DTOs (``pattern``,
     ``minLength``/``maxLength``, ``required``, ``minimum``/``maximum``, ``enum``)
     — recovered by :mod:`sourcecode.openapi_surface` (Phase 18). These map to
     ``@Pattern``/``@Size``/``@NotNull`` on the generated DTOs.
  2. **Custom constraint validators** hand-written in ``src`` — a ``@Constraint``
     meta-annotation plus its ``ConstraintValidator`` implementation (e.g.
     ``PetAgeValidator``) — linked to DTO fields through openapi-generator's
     ``x-field-extra-annotation`` vendor extension.

The output is a per-endpoint validation surface (which body fields are validated
and how), the custom-validator catalog discovered in source, and the set of
validation gaps (body endpoints with no declared constraint at all).

Structural facts come exclusively from the **ContextGraph** (annotation-type
nodes, field nodes with captured constraint arguments, validated handler
parameters) — this module performs no source parsing of its own. The string
helpers below operate on strings the graph already carries.

Design notes mirror :mod:`sourcecode.openapi_surface`: pure extraction (never a
conformance check), defensive (malformed input yields a partial surface, never an
exception), and deterministic ordering for stable JSON.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from sourcecode.path_filters import is_test_path

if TYPE_CHECKING:
    from sourcecode.context_graph import ContextGraph

# Built-in constraint keys (as emitted by FieldConstraint.to_dict) that count as
# "this field is validated".
_BUILTIN_CONSTRAINT_KEYS = (
    "pattern",
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "enum",
)

# validatedBy = {A.class, B.class} (or single entry) inside @Constraint(...) args.
_VALIDATED_BY_RE = re.compile(r"validatedBy\s*=\s*\{?([^)}]*)\}?\s*$")

# implements ... ConstraintValidator<Annotation, Type> — from the class node's
# declaration signature (the graph preserves generic arguments verbatim).
_IMPL_SIG_RE = re.compile(
    r"implements\s+(?:[\w.<>\[\], ]+,\s*)?ConstraintValidator\s*<\s*(\w+)\s*,\s*([\w.<>\[\] ]+?)\s*>"
)

# declaration `class X extends Y` — from the class node's signature.
_EXTENDS_SIG_RE = re.compile(r"\bextends\s+([\w.]+)")


@dataclass
class CustomConstraint:
    """A hand-written bean-validation constraint discovered in source."""

    name: str  # the @interface annotation, e.g. "PetAgeValidation"
    validators: "list[str]" = field(default_factory=list)  # ConstraintValidator impls
    message: Optional[str] = None  # default message template
    validated_types: "list[str]" = field(default_factory=list)  # T in <A, T>
    targets: "list[str]" = field(default_factory=list)  # @Target element types
    source_file: Optional[str] = None

    def to_dict(self) -> "dict[str, Any]":
        out: "dict[str, Any]" = {"annotation": self.name}
        if self.validators:
            out["validators"] = self.validators
        if self.message is not None:
            out["message"] = self.message
        if self.validated_types:
            out["validatedTypes"] = self.validated_types
        if self.targets:
            out["targets"] = self.targets
        if self.source_file:
            out["sourceFile"] = self.source_file
        return out


def _simple(name: str) -> str:
    return name.rsplit(".", 1)[-1].strip()


def _split_class_list(raw: str) -> "list[str]":
    """Parse ``A.class, B.class`` (or a single entry) into simple class names."""
    out: "list[str]" = []
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok:
            continue
        tok = re.sub(r"\.class\b", "", tok)
        simple = _simple(tok)
        if simple and simple not in out:
            out.append(simple)
    return out


def _is_excluded(source_file: "Optional[str]") -> bool:
    """Replicates the raw scan's exclusions on a graph node's source file."""
    norm = (source_file or "").replace("\\", "/")
    return (not norm) or is_test_path(norm) or "/target/" in norm or "/build/" in norm


def _build_graph(root: Path) -> "ContextGraph":
    from sourcecode.context_graph import ContextGraph

    return ContextGraph.build_from_root(Path(root))


def discover_custom_validators(
    root: Path, graph: "Optional[ContextGraph]" = None
) -> "dict[str, CustomConstraint]":
    """Discover custom ``@Constraint`` validators from the ContextGraph.

    Returns a map keyed by the annotation's simple name. Never raises; nodes
    with missing/partial metadata simply yield a partial catalog entry.
    """
    if graph is None:
        graph = _build_graph(root)

    catalog: "dict[str, CustomConstraint]" = {}

    # @interface declarations carrying @Constraint(validatedBy = ...).
    for ann in graph.annotation_types():
        if _is_excluded(ann.source_file):
            continue
        if "@Constraint" not in ann.annotations:
            continue
        name = _simple(ann.fqn)
        cc = catalog.setdefault(name, CustomConstraint(name=name))
        cc.source_file = cc.source_file or ann.source_file
        m = _VALIDATED_BY_RE.search(ann.annotation_values.get("@Constraint", ""))
        if m:
            for v in _split_class_list(m.group(1)):
                if v not in cc.validators:
                    cc.validators.append(v)
        tgt = ann.annotation_values.get("@Target", "")
        if tgt:
            for t in tgt.strip("{} ").split(","):
                t = _simple(t)
                if t and t not in cc.targets:
                    cc.targets.append(t)
        # Default message template — the @interface member's `default` literal.
        member = graph.symbol(f"{ann.fqn}#message")
        if member is not None and cc.message is None:
            d = member.annotation_values.get("_default", "")
            if len(d) >= 2 and d.startswith('"') and d.endswith('"'):
                cc.message = d[1:-1]

    # ConstraintValidator<Annotation, Type> implementations (may live in a
    # different file than the annotation — folded into the same catalog).
    for t in graph.types():
        if _is_excluded(t.source_file):
            continue
        m = _IMPL_SIG_RE.search(t.signature or "")
        if not m:
            continue
        ann_name, vtype = m.group(1), _simple(m.group(2))
        validator_cls = _simple(t.fqn)
        cc = catalog.setdefault(ann_name, CustomConstraint(name=ann_name))
        if vtype and vtype not in cc.validated_types:
            cc.validated_types.append(vtype)
        if validator_cls not in cc.validators:
            cc.validators.append(validator_cls)
    return catalog


# ---------------------------------------------------------------------------
# Source-derived constraints (no OpenAPI spec)
#
# When a repo ships no OpenAPI spec, declarative DTO constraints still live in
# the Java source as bean-validation annotations — and, since Phase 3, in the
# ContextGraph as annotated field nodes. We recover them from the graph: locate
# the handler's validated body parameter (@Valid/@Validated, captured on the
# handler node), resolve that DTO class in-repo, and read its field nodes'
# constraint annotations. This is pure extraction — it never fabricates
# constraints, and it is reported with source="source-derived" + a lower
# confidence than spec-carried constraints.
# ---------------------------------------------------------------------------

# jakarta/javax bean-validation built-in constraints. @Valid marks nested
# validation, which still means "this field is validated".
_BEAN_CONSTRAINTS = frozenset({
    "NotNull", "NotEmpty", "NotBlank", "Null", "AssertTrue", "AssertFalse",
    "Min", "Max", "DecimalMin", "DecimalMax", "Digits", "Positive",
    "PositiveOrZero", "Negative", "NegativeOrZero", "Size", "Pattern", "Email",
    "Past", "PastOrPresent", "Future", "FutureOrPresent", "Valid",
})
_VALIDATE_MARKERS = frozenset({"Valid", "Validated"})
# A class-typed identifier (starts uppercase) — heuristic for "a DTO type".
_CLASS_TYPE_RE = re.compile(r"^[A-Z][\w]*$")
_ANN_TOKEN_RE = re.compile(r"@(\w+)\s*(?:\(([^)]*)\))?")


def _index_repo_classes(graph: "ContextGraph") -> "dict[str, str]":
    """Map a class's simple name → its FQN (first non-test match in
    source-path order, replicating the old sorted-file-scan semantics)."""
    pairs = [
        (t.source_file, t.fqn)
        for t in graph.types()
        if not _is_excluded(t.source_file)
    ]
    pairs.sort()
    index: "dict[str, str]" = {}
    for _, fqn in pairs:
        index.setdefault(_simple(fqn), fqn)
    return index


def _split_top_level(params: str) -> "list[str]":
    """Split a parameter list on top-level commas (ignoring generics/parens)."""
    out: "list[str]" = []
    depth = 0
    buf: "list[str]" = []
    for c in params:
        if c in "<(":
            depth += 1
        elif c in ">)":
            depth -= 1
        if c == "," and depth == 0:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(c)
    if buf:
        out.append("".join(buf))
    return out


def _handler_body_dto(
    graph: "ContextGraph", controller_fqn: str, handler: str
) -> "Optional[tuple[str, str]]":
    """Find ``handler``'s validated body parameter from its graph node. Returns
    (dto_simple, binding) where binding is 'body' (@RequestBody) or 'form'
    (@ModelAttribute / implicit), or None when the handler has no
    @Valid/@Validated DTO parameter."""
    node = graph.symbol(f"{controller_fqn}#{handler}")
    if node is None:
        return None
    params = node.annotation_values.get("_validated_params", "")
    if not params:
        return None
    for raw in _split_top_level(params):
        raw = raw.strip()
        if not raw:
            continue
        anns = {a.group(1) for a in _ANN_TOKEN_RE.finditer(raw)}
        if not (anns & _VALIDATE_MARKERS):
            continue
        # Strip annotations, then read the parameter's declared type.
        without_ann = _ANN_TOKEN_RE.sub("", raw).strip()
        without_ann = re.sub(r"^final\s+", "", without_ann)
        parts = without_ann.split()
        if not parts:
            continue
        dto = re.sub(r"<.*", "", parts[0]).strip()
        if not _CLASS_TYPE_RE.match(dto):
            continue
        binding = "body" if "RequestBody" in anns else "form"
        return dto, binding
    return None


def _dto_field_constraints(
    graph: "ContextGraph",
    dto: str,
    class_index: "dict[str, str]",
    catalog: "dict[str, CustomConstraint]",
    _seen: "Optional[set[str]]" = None,
) -> "list[dict[str, Any]]":
    """Read a DTO's validated field nodes (own class + in-repo supertypes,
    depth-guarded), in declaration order."""
    if _seen is None:
        _seen = set()
    if dto in _seen or dto not in class_index:
        return []
    _seen.add(dto)
    dto_fqn = class_index[dto]

    fields: "list[dict[str, Any]]" = []
    for f in graph.fields_of(dto_fqn):
        rules: "list[dict[str, Any]]" = []
        customs: "list[dict[str, Any]]" = []
        for ann in f.annotations:
            bare = ann.lstrip("@")
            if bare in _BEAN_CONSTRAINTS:
                rule: "dict[str, Any]" = {"kind": bare}
                args = f.annotation_values.get(ann, "")
                if args:
                    rule["value"] = args
                rules.append(rule)
            elif bare in catalog:
                customs.append({"annotation": bare, "resolved": True})
        if rules or customs:
            entry: "dict[str, Any]" = {"name": _simple(f.fqn)}
            if rules:
                entry["rules"] = rules
            if customs:
                entry["customValidators"] = customs
            fields.append(entry)

    # Follow a single in-repo supertype so inherited constraints are not lost.
    node = graph.symbol(dto_fqn)
    ext = _EXTENDS_SIG_RE.search((node.signature or "") if node else "")
    if ext:
        seen_names = {f["name"] for f in fields}
        for inherited in _dto_field_constraints(
            graph, _simple(ext.group(1)), class_index, catalog, _seen
        ):
            if inherited["name"] not in seen_names:
                fields.append(inherited)
    return fields


def _recover_source_endpoints(
    graph: "ContextGraph",
    endpoints: "list[dict[str, Any]]",
    catalog: "dict[str, CustomConstraint]",
) -> "tuple[list[dict[str, Any]], int]":
    """Build validation routes for source endpoints whose handler validates a
    DTO. Returns (routes, validated_field_count). Only body-shaped verbs are
    considered, matching the OpenAPI path's scope."""
    class_index = _index_repo_classes(graph)
    routes: "list[dict[str, Any]]" = []
    total = 0
    for ep in endpoints:
        if not _is_body_endpoint(ep):
            continue
        controller = ep.get("controller")
        handler = ep.get("handler")
        if not controller or not handler or controller not in class_index:
            continue
        dto_binding = _handler_body_dto(graph, class_index[controller], str(handler))
        if dto_binding is None:
            continue
        dto, binding = dto_binding
        validated = _dto_field_constraints(graph, dto, class_index, catalog)
        if not validated:
            continue
        total += len(validated)
        routes.append({
            "method": ep.get("method"),
            "path": ep.get("path"),
            "controller": controller,
            "handler": handler,
            "schema": dto,
            "binding": binding,
            "source": "source-derived",
            "confidence": "medium",
            "validatedFields": validated,
        })
    return routes, total


def _field_rules(fieldc: "dict[str, Any]") -> "list[dict[str, Any]]":
    """Render a constraint dict's built-in rules as a list of {kind, value}."""
    rules: "list[dict[str, Any]]" = []
    if fieldc.get("required"):
        rules.append({"kind": "required"})
    for key in _BUILTIN_CONSTRAINT_KEYS:
        if key in fieldc:
            rules.append({"kind": key, "value": fieldc[key]})
    return rules


def _is_body_endpoint(ep: "dict[str, Any]") -> bool:
    method = str(ep.get("method", "")).upper()
    return method in ("POST", "PUT", "PATCH")


def build_validation_surface(
    root: Path,
    endpoints_data: "Optional[dict[str, Any]]" = None,
    graph: "Optional[ContextGraph]" = None,
) -> "dict[str, Any]":
    """Build the per-endpoint validation surface for a repo.

    ``endpoints_data`` is the result of :func:`extract_java_endpoints`; when not
    supplied it is computed (so the command can run standalone). ``graph`` is
    the repo's ContextGraph; when not supplied it is built once here and shared
    by every lookup below. Returns a JSON-ready dict: ``endpoints`` (validated
    fields per route), ``custom_validators`` (catalog), ``gaps`` (body routes
    with no declared constraint at all), and ``summary``.
    """
    root = Path(root)
    if endpoints_data is None:
        from sourcecode.repository_ir import extract_java_endpoints

        endpoints_data = extract_java_endpoints(root)
    if graph is None:
        graph = _build_graph(root)

    catalog = discover_custom_validators(root, graph)

    out_endpoints: "list[dict[str, Any]]" = []
    gaps: "list[dict[str, Any]]" = []
    custom_used: "set[str]" = set()
    total_validated_fields = 0

    for ep in endpoints_data.get("endpoints", []):
        body = ep.get("request_body")
        if not isinstance(body, dict):
            # A body-shaped verb with no recovered request body is a blind spot
            # worth flagging, but only when we resolved the route from the spec.
            if _is_body_endpoint(ep) and ep.get("source") == "openapi-spec":
                gaps.append(
                    {
                        "method": ep.get("method"),
                        "path": ep.get("path"),
                        "controller": ep.get("controller"),
                        "reason": "no_request_body_constraints",
                    }
                )
            continue

        constraints = body.get("constraints") or []
        validated_fields: "list[dict[str, Any]]" = []
        for fc in constraints:
            if not isinstance(fc, dict):
                continue
            rules = _field_rules(fc)
            customs: "list[dict[str, Any]]" = []
            for ann in fc.get("extraAnnotations", []) or []:
                cc = catalog.get(ann)
                entry: "dict[str, Any]" = {"annotation": ann}
                if cc is not None:
                    custom_used.add(ann)
                    if cc.validators:
                        entry["validators"] = cc.validators
                    if cc.message is not None:
                        entry["message"] = cc.message
                    entry["resolved"] = True
                else:
                    entry["resolved"] = False
                customs.append(entry)
            if not rules and not customs:
                continue
            field_entry: "dict[str, Any]" = {"name": fc.get("name")}
            if rules:
                field_entry["rules"] = rules
            if customs:
                field_entry["customValidators"] = customs
            validated_fields.append(field_entry)

        total_validated_fields += len(validated_fields)
        route = {
            "method": ep.get("method"),
            "path": ep.get("path"),
            "controller": ep.get("controller"),
            "handler": ep.get("handler"),
            "schema": body.get("schema"),
            "validatedFields": validated_fields,
        }
        out_endpoints.append(route)
        if _is_body_endpoint(ep) and not validated_fields:
            gaps.append(
                {
                    "method": ep.get("method"),
                    "path": ep.get("path"),
                    "controller": ep.get("controller"),
                    "schema": body.get("schema"),
                    "reason": "no_validated_fields",
                }
            )

    custom_list = [catalog[k].to_dict() for k in sorted(catalog)]
    result: "dict[str, Any]" = {
        "endpoints": out_endpoints,
        "custom_validators": custom_list,
        "gaps": gaps,
        "summary": {
            "endpoints_with_body": len(out_endpoints),
            "validated_fields": total_validated_fields,
            "custom_validators_declared": len(custom_list),
            "custom_validators_linked": len(custom_used),
            "gaps": len(gaps),
        },
    }
    spec_path = endpoints_data.get("openapi_spec")
    if spec_path:
        result["openapi_spec"] = spec_path
    else:
        # No OpenAPI spec on disk / under target/generated-sources. Recover
        # declarative constraints from the graph's DTO field nodes (bean-
        # validation annotations on @Valid/@Validated handler bodies), so a
        # repo without a spec is no longer reported as a sea of zeros.
        result["openapi_spec"] = None
        source_routes, source_fields = _recover_source_endpoints(
            graph, endpoints_data.get("endpoints", []), catalog
        )
        if source_routes:
            existing = {(r.get("method"), r.get("path")) for r in out_endpoints}
            for r in source_routes:
                if (r.get("method"), r.get("path")) not in existing:
                    out_endpoints.append(r)
            total_validated_fields += source_fields
            # Recompute the gaps that depended on the now-recovered routes.
            recovered = {(r.get("method"), r.get("path")) for r in source_routes}
            gaps = [
                g for g in gaps
                if (g.get("method"), g.get("path")) not in recovered
            ]
            result["endpoints"] = out_endpoints
            result["gaps"] = gaps
            result["summary"]["endpoints_with_body"] = len(out_endpoints)
            result["summary"]["validated_fields"] = total_validated_fields
            result["summary"]["gaps"] = len(gaps)
            result["summary"]["source_derived_routes"] = len(source_routes)
            result["note"] = (
                "No OpenAPI spec found; constraints were recovered from Java DTO "
                "source (bean-validation annotations on @Valid/@Validated handler "
                "bodies). Routes carry source=\"source-derived\" at medium "
                "confidence. Custom-validator linkage and nested generics may be "
                "partial; OpenAPI-carried constraints would be more complete."
            )
        else:
            result["note"] = (
                "No OpenAPI spec found and no source DTO constraints recovered "
                "(no handler validates an in-repo DTO via @Valid/@Validated, or "
                "the DTOs declare no bean-validation annotations). This is "
                "expected for such repos, not a missing-validation finding."
            )
    return result

"""Validation surface extraction (Phase 20).

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

Design notes mirror :mod:`sourcecode.openapi_surface`: pure extraction (never a
conformance check), defensive (malformed input yields a partial surface, never an
exception), and deterministic ordering for stable JSON.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from sourcecode.path_filters import is_test_path

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

# How many java files to scan for custom validators, to stay fast on big repos.
_SCAN_CAP = 5000

_CONSTRAINT_RE = re.compile(
    r"@Constraint\s*\(\s*validatedBy\s*=\s*\{?([^)}]*)\}?\s*\)", re.DOTALL
)
_INTERFACE_RE = re.compile(r"@interface\s+(\w+)")
_MESSAGE_RE = re.compile(
    r"String\s+message\s*\(\s*\)\s*default\s*\"([^\"]*)\"", re.DOTALL
)
_TARGET_RE = re.compile(r"@Target\s*\(\s*\{?([^)}]*)\}?\s*\)", re.DOTALL)
_VALIDATOR_IMPL_RE = re.compile(
    r"class\s+(\w+)\s+implements\s+ConstraintValidator\s*<\s*(\w+)\s*,\s*([\w.<>\[\] ]+?)\s*>",
    re.DOTALL,
)


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


def discover_custom_validators(root: Path) -> "dict[str, CustomConstraint]":
    """Scan non-test Java source for custom ``@Constraint`` validators.

    Returns a map keyed by the annotation's simple name. Never raises; an
    unreadable or malformed file is skipped.
    """
    root = Path(root)
    catalog: "dict[str, CustomConstraint]" = {}
    # annotation name -> validated types, harvested from ConstraintValidator impls.
    impl_types: "dict[str, list[str]]" = {}
    impl_validators: "dict[str, list[str]]" = {}

    scanned = 0
    for p in sorted(root.rglob("*.java")):
        if scanned >= _SCAN_CAP:
            break
        norm = str(p).replace("\\", "/")
        if is_test_path(norm) or "/target/" in norm or "/build/" in norm:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        scanned += 1

        if "@Constraint" not in text and "ConstraintValidator" not in text:
            continue
        rel = norm[len(str(root).replace("\\", "/")) :].lstrip("/") or norm

        # @interface declarations carrying @Constraint(validatedBy = ...).
        for m in _CONSTRAINT_RE.finditer(text):
            tail = text[m.end() :]
            iface = _INTERFACE_RE.search(tail)
            if not iface:
                continue
            ann_name = iface.group(1)
            cc = catalog.setdefault(ann_name, CustomConstraint(name=ann_name))
            cc.source_file = cc.source_file or rel
            for v in _split_class_list(m.group(1)):
                if v not in cc.validators:
                    cc.validators.append(v)
            # Default message + @Target sit within this @interface body.
            body = tail[iface.end() :]
            msg = _MESSAGE_RE.search(body)
            if msg and cc.message is None:
                cc.message = msg.group(1)
            tgt = _TARGET_RE.search(text[: m.start()][-400:] + tail[: iface.end()])
            if tgt:
                for t in tgt.group(1).split(","):
                    t = _simple(t)
                    if t and t not in cc.targets:
                        cc.targets.append(t)

        # ConstraintValidator<Annotation, Type> implementations.
        for m in _VALIDATOR_IMPL_RE.finditer(text):
            validator_cls, ann_name, vtype = m.group(1), m.group(2), _simple(m.group(3))
            impl_types.setdefault(ann_name, [])
            if vtype and vtype not in impl_types[ann_name]:
                impl_types[ann_name].append(vtype)
            impl_validators.setdefault(ann_name, [])
            if validator_cls not in impl_validators[ann_name]:
                impl_validators[ann_name].append(validator_cls)

    # Fold validator-impl findings back into the catalog (handles validators
    # declared in a different file than the annotation).
    for ann_name, types in impl_types.items():
        cc = catalog.setdefault(ann_name, CustomConstraint(name=ann_name))
        for t in types:
            if t not in cc.validated_types:
                cc.validated_types.append(t)
    for ann_name, vals in impl_validators.items():
        cc = catalog.setdefault(ann_name, CustomConstraint(name=ann_name))
        for v in vals:
            if v not in cc.validators:
                cc.validators.append(v)
    return catalog


# ---------------------------------------------------------------------------
# Source-derived constraints (no OpenAPI spec)
#
# When a repo ships no OpenAPI spec, declarative DTO constraints still live in
# the Java source as bean-validation annotations. We recover them directly:
# locate the handler's validated body parameter (@Valid/@Validated), resolve
# that DTO class in-repo, and read its fields' constraint annotations. This is
# pure extraction — it never fabricates constraints, and it is reported with
# source="source-derived" + a lower confidence than spec-carried constraints.
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
_FIELD_DECL_RE = re.compile(
    r"^\s*(?:private|protected|public)\s+"
    r"(?:final\s+|static\s+|transient\s+|volatile\s+)*"
    r"[\w.$<>\[\], ]+?\s+(\w+)\s*[;=]"
)
_CLASS_EXTENDS_RE = re.compile(r"\bclass\s+\w+\s+extends\s+(\w+)")


def _index_repo_classes(root: Path) -> "dict[str, Path]":
    """Map a class's simple name → its source file (first non-test match)."""
    index: "dict[str, Path]" = {}
    count = 0
    for jf in root.rglob("*.java"):
        rel = str(jf).replace("\\", "/")
        if is_test_path(rel) or "/target/" in rel:
            continue
        count += 1
        if count > _SCAN_CAP:
            break
        index.setdefault(jf.stem, jf)
    return index


def _balanced_parens(src: str, open_idx: int) -> "Optional[str]":
    """Given the index of a '(', return the inner text up to its matching ')'."""
    depth = 0
    for i in range(open_idx, len(src)):
        c = src[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return src[open_idx + 1:i]
    return None


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


def _handler_body_dto(controller_src: str, handler: str) -> "Optional[tuple[str, str]]":
    """Find ``handler``'s validated body parameter. Returns (dto_simple, binding)
    where binding is 'body' (@RequestBody), 'form' (@ModelAttribute / implicit),
    or None when the handler has no @Valid/@Validated DTO parameter."""
    for m in re.finditer(r"\b" + re.escape(handler) + r"\s*\(", controller_src):
        params = _balanced_parens(controller_src, m.end() - 1)
        if params is None:
            continue
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
    dto: str,
    class_index: "dict[str, Path]",
    catalog: "dict[str, CustomConstraint]",
    _seen: "Optional[set[str]]" = None,
) -> "list[dict[str, Any]]":
    """Read a DTO's validated fields (own file + in-repo supertypes, depth-guarded)."""
    if _seen is None:
        _seen = set()
    if dto in _seen or dto not in class_index:
        return []
    _seen.add(dto)
    try:
        src = class_index[dto].read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    fields: "list[dict[str, Any]]" = []
    pending: "list[tuple[str, str]]" = []  # (annotation, args)
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("@"):
            mt = _ANN_TOKEN_RE.match(stripped)
            if mt:
                pending.append((mt.group(1), (mt.group(2) or "").strip()))
            continue
        fm = _FIELD_DECL_RE.match(line)
        if fm:
            rules: "list[dict[str, Any]]" = []
            customs: "list[dict[str, Any]]" = []
            for ann, args in pending:
                if ann in _BEAN_CONSTRAINTS:
                    rule: "dict[str, Any]" = {"kind": ann}
                    if args:
                        rule["value"] = args
                    rules.append(rule)
                elif ann in catalog:
                    customs.append({"annotation": ann, "resolved": True})
            if rules or customs:
                entry: "dict[str, Any]" = {"name": fm.group(1)}
                if rules:
                    entry["rules"] = rules
                if customs:
                    entry["customValidators"] = customs
                fields.append(entry)
            pending = []
        elif stripped and not stripped.startswith("//") and not stripped.startswith("*"):
            # Any other meaningful line breaks the annotation→field adjacency.
            pending = []

    # Follow a single in-repo supertype so inherited constraints are not lost.
    ext = _CLASS_EXTENDS_RE.search(src)
    if ext:
        seen_names = {f["name"] for f in fields}
        for inherited in _dto_field_constraints(ext.group(1), class_index, catalog, _seen):
            if inherited["name"] not in seen_names:
                fields.append(inherited)
    return fields


def _recover_source_endpoints(
    root: Path,
    endpoints: "list[dict[str, Any]]",
    catalog: "dict[str, CustomConstraint]",
) -> "tuple[list[dict[str, Any]], int]":
    """Build validation routes for source endpoints whose handler validates a
    DTO. Returns (routes, validated_field_count). Only body-shaped verbs are
    considered, matching the OpenAPI path's scope."""
    class_index = _index_repo_classes(root)
    controller_cache: "dict[str, Optional[str]]" = {}
    routes: "list[dict[str, Any]]" = []
    total = 0
    for ep in endpoints:
        if not _is_body_endpoint(ep):
            continue
        controller = ep.get("controller")
        handler = ep.get("handler")
        if not controller or not handler or controller not in class_index:
            continue
        if controller not in controller_cache:
            try:
                controller_cache[controller] = class_index[controller].read_text(
                    encoding="utf-8", errors="replace"
                )
            except OSError:
                controller_cache[controller] = None
        csrc = controller_cache[controller]
        if csrc is None:
            continue
        dto_binding = _handler_body_dto(csrc, str(handler))
        if dto_binding is None:
            continue
        dto, binding = dto_binding
        validated = _dto_field_constraints(dto, class_index, catalog)
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
) -> "dict[str, Any]":
    """Build the per-endpoint validation surface for a repo.

    ``endpoints_data`` is the result of :func:`extract_java_endpoints`; when not
    supplied it is computed (so the command can run standalone). Returns a JSON-
    ready dict: ``endpoints`` (validated fields per route), ``custom_validators``
    (catalog), ``gaps`` (body routes with no declared constraint), and ``summary``.
    """
    root = Path(root)
    if endpoints_data is None:
        from sourcecode.repository_ir import extract_java_endpoints

        endpoints_data = extract_java_endpoints(root)

    catalog = discover_custom_validators(root)

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
        # declarative constraints directly from the Java DTOs that handlers
        # validate (@Valid/@Validated body params), so a repo without a spec is
        # no longer reported as a sea of zeros.
        result["openapi_spec"] = None
        source_routes, source_fields = _recover_source_endpoints(
            root, endpoints_data.get("endpoints", []), catalog
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

"""OpenAPI spec surface extraction (Phase 18, wave 18-01).

Many enterprise Spring repos generate their HTTP surface, DTOs and validation
constraints from an OpenAPI spec via openapi-generator: controllers
``implements XxxApi`` where the mapping annotations and DTO classes live under
``target/generated-sources`` (excluded from the source scan). The structural
scanner therefore sees no routes and no constraints for those controllers.

The spec itself, however, ships in the repo source (commonly
``src/main/resources/openapi.yml``): always present, deterministic, no build
required. This module discovers and parses that spec into a normalized surface
— operations (method/path/operationId/tags/requestBody) and schemas (fields
with validation constraints) — so downstream code can recover the endpoint and
constraint surface without touching generated sources.

Design notes:
  * Pure extraction, not validation: we never assert spec conformance.
  * Defensive: a malformed or partial spec yields a partial surface, never an
    exception. Unresolvable ``$ref``/``allOf`` are skipped, not fatal.
  * Bounded: discovery is limited to well-known locations + a capped content
    sniff so it never walks an entire large tree.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

_HTTP_METHODS = ("get", "put", "post", "delete", "patch", "options", "head", "trace")

# Filenames that are almost certainly an API spec.
_SPEC_NAME_HINTS = ("openapi", "swagger", "api-docs")

# Directories worth searching first (relative to repo root).
_SPEC_DIRS = (
    "src/main/resources",
    "src/main/resources/openapi",
    "api",
    "apis",
    "openapi",
    "spec",
    "specs",
    "docs",
    "contracts",
    ".",
)

# Cap on how many candidate files we content-sniff, to stay fast on big repos.
_SNIFF_CAP = 400
# Cap on $ref / allOf resolution depth, to stay safe on cyclic specs.
_RESOLVE_DEPTH = 8


@dataclass
class FieldConstraint:
    """A single schema property and its validation constraints."""

    name: str
    type: Optional[str] = None
    required: bool = False
    pattern: Optional[str] = None
    min_length: Optional[int] = None
    max_length: Optional[int] = None
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    fmt: Optional[str] = None
    enum: Optional[list[Any]] = None
    ref: Optional[str] = None  # schema name when the field is an object/array ref

    def to_dict(self) -> "dict[str, Any]":
        out: "dict[str, Any]" = {"name": self.name, "required": self.required}
        for key, val in (
            ("type", self.type),
            ("pattern", self.pattern),
            ("minLength", self.min_length),
            ("maxLength", self.max_length),
            ("minimum", self.minimum),
            ("maximum", self.maximum),
            ("format", self.fmt),
            ("enum", self.enum),
            ("ref", self.ref),
        ):
            if val is not None:
                out[key] = val
        return out


@dataclass
class OpenApiSchema:
    name: str
    fields: "list[FieldConstraint]" = field(default_factory=list)

    def to_dict(self) -> "dict[str, Any]":
        return {"name": self.name, "fields": [f.to_dict() for f in self.fields]}


@dataclass
class OpenApiOperation:
    method: str
    path: str
    operation_id: Optional[str] = None
    tags: "list[str]" = field(default_factory=list)
    request_body_schema: Optional[str] = None  # schema name (ref) of the body
    has_security: bool = False

    def to_dict(self) -> "dict[str, Any]":
        out: "dict[str, Any]" = {"method": self.method, "path": self.path}
        if self.operation_id:
            out["operationId"] = self.operation_id
        if self.tags:
            out["tags"] = self.tags
        if self.request_body_schema:
            out["requestBodySchema"] = self.request_body_schema
        out["hasSecurity"] = self.has_security
        return out


@dataclass
class OpenApiSurface:
    spec_path: str
    operations: "list[OpenApiOperation]" = field(default_factory=list)
    schemas: "dict[str, OpenApiSchema]" = field(default_factory=dict)

    def to_dict(self) -> "dict[str, Any]":
        return {
            "spec_path": self.spec_path,
            "operations": [op.to_dict() for op in self.operations],
            "schemas": {name: s.to_dict() for name, s in self.schemas.items()},
        }


def tag_to_interface(tag: str) -> str:
    """Map an OpenAPI tag to the openapi-generator interface name.

    openapi-generator with ``useTags: true`` derives one ``{PascalCaseTag}Api``
    interface per tag, splitting on ``-``/``_``/space. E.g. ``owners`` ->
    ``OwnersApi``, ``owner-v2`` -> ``OwnerV2Api``, ``vet_v2`` -> ``VetV2Api``.
    """
    import re as _re

    words = [w for w in _re.split(r"[-_\s]+", tag) if w]
    return "".join(w[:1].upper() + w[1:] for w in words) + "Api"


# ── Discovery ──────────────────────────────────────────────────────────────


def _looks_like_spec(data: Any) -> bool:
    return isinstance(data, dict) and ("openapi" in data or "swagger" in data)


def _load_yaml_or_json(path: Path) -> Optional[Any]:
    """Load a YAML or JSON document, returning None on any failure."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    suffix = path.suffix.lower()
    try:
        if suffix == ".json":
            return json.loads(text)
        # .yml/.yaml (and unknown) -> YAML, which is a JSON superset.
        from ruamel.yaml import YAML

        yaml = YAML(typ="safe")
        return yaml.load(text)
    except Exception:
        # Last resort: a .json-less file that is actually JSON.
        try:
            return json.loads(text)
        except Exception:
            return None


def find_openapi_specs(root: Path) -> "list[Path]":
    """Discover OpenAPI/Swagger spec files under ``root``.

    Strategy: collect candidates by filename hint within well-known dirs, then
    content-sniff a bounded set of ``.yml/.yaml/.json`` files to confirm. Result
    is sorted for determinism. Never raises.
    """
    root = Path(root)
    candidates: "list[Path]" = []
    seen: "set[Path]" = set()

    def _consider(p: Path) -> None:
        try:
            rp = p.resolve()
        except OSError:
            return
        if rp in seen or not p.is_file():
            return
        seen.add(rp)
        candidates.append(p)

    # Pass 1: filename-hinted files in well-known dirs.
    for rel in _SPEC_DIRS:
        d = root / rel
        if not d.is_dir():
            continue
        try:
            entries = sorted(d.iterdir())
        except OSError:
            continue
        for p in entries:
            if not p.is_file():
                continue
            stem = p.stem.lower()
            if p.suffix.lower() in (".yml", ".yaml", ".json") and any(
                h in stem for h in _SPEC_NAME_HINTS
            ):
                _consider(p)

    # Confirm pass-1 candidates by content; keep only real specs.
    confirmed: "list[Path]" = []
    for p in candidates:
        data = _load_yaml_or_json(p)
        if _looks_like_spec(data):
            confirmed.append(p)

    if confirmed:
        return sorted(confirmed, key=lambda p: str(p))

    # Pass 2 (fallback): bounded content sniff of resource-y yaml/json files.
    sniffed = 0
    for rel in ("src/main/resources", "."):
        d = root / rel
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*")):
            if sniffed >= _SNIFF_CAP:
                break
            if not p.is_file() or p.suffix.lower() not in (".yml", ".yaml", ".json"):
                continue
            # Skip obvious build output.
            parts = {seg.lower() for seg in p.parts}
            if "target" in parts or "node_modules" in parts or "build" in parts:
                continue
            sniffed += 1
            data = _load_yaml_or_json(p)
            if _looks_like_spec(data):
                _consider(p)
    return sorted({p.resolve(): p for p in candidates}.values(), key=lambda p: str(p))


# ── Parsing ────────────────────────────────────────────────────────────────


def _ref_name(ref: Any) -> Optional[str]:
    """Return the trailing name of a ``#/components/schemas/Xxx`` ref."""
    if isinstance(ref, str) and ref.startswith("#/"):
        return ref.rsplit("/", 1)[-1]
    return None


def _field_from_property(name: str, prop: Any, required: bool) -> FieldConstraint:
    fc = FieldConstraint(name=name, required=required)
    if not isinstance(prop, dict):
        return fc
    ref = _ref_name(prop.get("$ref"))
    if ref:
        fc.ref = ref
    fc.type = prop.get("type")
    fc.pattern = prop.get("pattern")
    fc.fmt = prop.get("format")
    for src, dst in (("minLength", "min_length"), ("maxLength", "max_length")):
        v = prop.get(src)
        if isinstance(v, int):
            setattr(fc, dst, v)
    for src, dst in (("minimum", "minimum"), ("maximum", "maximum")):
        v = prop.get(src)
        if isinstance(v, (int, float)):
            setattr(fc, dst, float(v))
    enum = prop.get("enum")
    if isinstance(enum, list):
        fc.enum = list(enum)
    if fc.type is None and prop.get("type") == "array":
        items = prop.get("items")
        if isinstance(items, dict):
            fc.ref = fc.ref or _ref_name(items.get("$ref"))
    return fc


def _resolve_schema_fields(
    node: Any,
    all_schemas: "dict[str, Any]",
    depth: int = 0,
    _seen: "Optional[set[str]]" = None,
) -> "tuple[dict[str, FieldConstraint], set[str]]":
    """Recursively flatten a schema node (handling allOf + $ref) into fields.

    Returns (ordered field map by name, required-name set). Bounded by depth.
    """
    fields: "dict[str, FieldConstraint]" = {}
    required: "set[str]" = set()
    if depth > _RESOLVE_DEPTH or not isinstance(node, dict):
        return fields, required
    seen = _seen or set()

    # $ref -> resolve the referenced schema.
    ref = _ref_name(node.get("$ref"))
    if ref:
        if ref in seen:
            return fields, required
        target = all_schemas.get(ref)
        if isinstance(target, dict):
            return _resolve_schema_fields(target, all_schemas, depth + 1, seen | {ref})
        return fields, required

    # allOf -> merge each sub-schema.
    for sub in node.get("allOf", []) or []:
        sub_fields, sub_req = _resolve_schema_fields(
            sub, all_schemas, depth + 1, seen
        )
        fields.update(sub_fields)
        required |= sub_req

    # required list at this level.
    for r in node.get("required", []) or []:
        if isinstance(r, str):
            required.add(r)

    # direct properties.
    props = node.get("properties")
    if isinstance(props, dict):
        for pname, prop in props.items():
            fields[pname] = _field_from_property(pname, prop, required=False)

    # apply required flags now that we know the union.
    for rname in required:
        if rname in fields:
            fields[rname].required = True
    return fields, required


def _parse_schemas(components: Any) -> "dict[str, OpenApiSchema]":
    schemas_raw = {}
    if isinstance(components, dict):
        schemas_raw = components.get("schemas") or {}
    if not isinstance(schemas_raw, dict):
        return {}
    out: "dict[str, OpenApiSchema]" = {}
    for name, node in schemas_raw.items():
        fields_map, _ = _resolve_schema_fields(node, schemas_raw)
        out[name] = OpenApiSchema(name=name, fields=list(fields_map.values()))
    return out


def _request_body_schema(operation: Any) -> Optional[str]:
    if not isinstance(operation, dict):
        return None
    body = operation.get("requestBody")
    if not isinstance(body, dict):
        return None
    content = body.get("content")
    if not isinstance(content, dict):
        return None
    # Prefer application/json, else first media type with a schema.
    media_types = [content.get("application/json")] + [
        v for k, v in content.items() if k != "application/json"
    ]
    for media in media_types:
        if not isinstance(media, dict):
            continue
        schema = media.get("schema")
        if isinstance(schema, dict):
            name = _ref_name(schema.get("$ref"))
            if name:
                return name
            # array of refs
            if schema.get("type") == "array":
                items = schema.get("items")
                if isinstance(items, dict):
                    return _ref_name(items.get("$ref"))
    return None


def _parse_operations(paths: Any) -> "list[OpenApiOperation]":
    if not isinstance(paths, dict):
        return []
    ops: "list[OpenApiOperation]" = []
    for path, methods in paths.items():
        if not isinstance(methods, dict):
            continue
        for method in _HTTP_METHODS:
            op = methods.get(method)
            if not isinstance(op, dict):
                continue
            tags = op.get("tags")
            ops.append(
                OpenApiOperation(
                    method=method.upper(),
                    path=str(path),
                    operation_id=op.get("operationId"),
                    tags=[str(t) for t in tags] if isinstance(tags, list) else [],
                    request_body_schema=_request_body_schema(op),
                    has_security="security" in op,
                )
            )
    return ops


def parse_openapi_spec(path: Path) -> Optional[OpenApiSurface]:
    """Parse a single spec file into an OpenApiSurface, or None if unparseable."""
    data = _load_yaml_or_json(Path(path))
    if not _looks_like_spec(data):
        return None
    surface = OpenApiSurface(spec_path=str(path))
    try:
        surface.operations = _parse_operations(data.get("paths"))
        surface.schemas = _parse_schemas(data.get("components"))
    except Exception:
        # Partial surface beats a crash; return whatever resolved.
        pass
    return surface


def build_openapi_surface(root: Path) -> Optional[OpenApiSurface]:
    """Discover and parse the primary OpenAPI spec under ``root``.

    Returns the surface of the first discovered spec (deterministic ordering),
    or None when no spec is present.
    """
    specs = find_openapi_specs(Path(root))
    for spec in specs:
        surface = parse_openapi_spec(spec)
        if surface is not None and (surface.operations or surface.schemas):
            return surface
    return None

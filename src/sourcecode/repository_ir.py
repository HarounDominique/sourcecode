"""repository_ir.py — Deterministic Repository Intermediate Representation.

5-phase Java IR pipeline:
  Phase 1: Symbol extraction (class/interface/method/field)
  Phase 2: Spring semantic tagging (annotation-gated only)
  Phase 3: Symbol relation graph (statically detectable edges)
  Phase 4: Symbol-level diff (vs git baseline)
  Phase 5: Evidence Engine — EvidenceBundle per entity, single output contract

Deterministic: identical inputs → identical output.
Graph is the sole source of structural semantics.
No inference, approximation, or heuristics.
"""

from __future__ import annotations

import re
import subprocess
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Data classes — Phases 1–4
# ---------------------------------------------------------------------------

@dataclass
class SymbolRecord:
    symbol: str          # fully qualified: pkg.Class | pkg.Class#method | pkg.Class.field
    type: str            # class | interface | method | field  (backward-compat values)
    modifiers: list[str] = field(default_factory=list)
    annotations: list[str] = field(default_factory=list)
    imports_used: list[str] = field(default_factory=list)
    declaring_file: str = ""
    confidence: str = "medium"  # high | medium | low
    # Stable identity contract — populated by _extract_symbols
    stable_id: str = ""         # deterministic across formatting/body changes
    symbol_kind: str = ""       # class|interface|enum|annotation|method|constructor|field|endpoint|bean
    canonical_name: str = ""    # pkg.Class#method(Type1,Type2) — human-readable
    source_file: str = ""       # alias for declaring_file (IR output contract)
    signature: str = ""         # (Type1,Type2)->ReturnType for methods; type for fields
    param_types: list[str] = field(default_factory=list)
    return_type: str = ""
    annotation_values: dict[str, str] = field(default_factory=dict)  # ann_name → raw args string


@dataclass
class RelationEdge:
    from_symbol: str
    to_symbol: str
    type: str            # imports | extends | implements | injects | mapped_to | annotated_with
    confidence: str = "high"
    evidence: dict = field(default_factory=dict)  # {type: ..., value: ...}


@dataclass
class ChangedSymbol:
    symbol: str
    change_type: str     # added | removed | modified
    diff_type: str       # signature_change | annotation_change | structural_change | unknown
    confidence: str = "medium"


# ---------------------------------------------------------------------------
# Data classes — Phase 5 (Evidence Engine)
# ---------------------------------------------------------------------------

@dataclass
class EvidenceBundle:
    entity: str
    type: str                   # symbol | edge
    evidence: list[dict]        # [{source: str, strength: float}, ...]
    graph_links: list[str]      # edge keys connected to this entity
    diff_links: list[str]       # diff FQNs backing this entity
    ir_links: list[str]         # IR FQNs backing this entity

    @property
    def evidence_strength(self) -> float:
        if not self.evidence:
            return 0.0
        return round(sum(e["strength"] for e in self.evidence) / len(self.evidence), 4)

    @property
    def is_complete(self) -> bool:
        """All three evidence sources present — required for validated_changes."""
        return bool(self.graph_links) and bool(self.diff_links) and bool(self.ir_links)

    def to_dict(self) -> dict:
        return {
            "entity": self.entity,
            "type": self.type,
            "evidence": self.evidence,
            "graph_links": self.graph_links,
            "diff_links": self.diff_links,
            "ir_links": self.ir_links,
        }


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

_PKG_RE = re.compile(r'^package\s+([\w.]+)\s*;', re.MULTILINE)
_IMPORT_RE = re.compile(r'^import\s+(?:static\s+)?([\w.]+(?:\.\*)?)\s*;', re.MULTILINE)
_ANN_RE = re.compile(r'^(@[\w.]+)')
_ANN_WITH_ARGS_RE = re.compile(r'^(@[\w.]+)\s*(?:\(([^)]*)\))?')

_CLASS_DECL_RE = re.compile(
    r'(?:^|(?<=\s))'
    r'(?P<kind>class|interface|enum|@interface)\s+'
    r'(?P<name>[A-Z]\w*)'
    r'(?:\s*<[^{;]*?(?=>|\{))?'
    r'(?:\s+extends\s+(?P<extends>[\w.<>?,\s]+?))?'
    r'(?:\s+implements\s+(?P<implements>[\w.<>?,\s]+?))?'
    r'(?:\s+permits\s+[\w,\s]+?)?'
    r'\s*\{',
)

_METHOD_DECL_RE = re.compile(
    r'^(?P<modifiers>(?:(?:public|private|protected|static|final|synchronized'
    r'|abstract|default|native|strictfp|override)\s+)*)'
    r'(?:<[\w,\s?]+>\s+)?'
    r'(?P<return_type>(?:void|boolean|byte|char|short|int|long|float|double|String|[\w.<>\[\]?,]+)\s+)'
    r'(?P<name>[a-z_]\w*)\s*\(',
)

_CONSTRUCTOR_DECL_RE = re.compile(
    r'^(?P<modifiers>(?:(?:public|private|protected)\s+)*)'
    r'(?P<name>[A-Z]\w*)\s*\('
    r'(?P<params>[^)]*)',
)

_FIELD_DECL_RE = re.compile(
    r'^(?P<modifiers>(?:(?:private|protected|public|static|final|volatile|transient)\s+)*)'
    r'(?P<type>[\w<>.,\[\]? ]+?)\s+'
    r'(?P<name>[a-z_]\w*)\s*[;=]',
)

_REQUEST_MAPPING_RE = re.compile(
    r'@(?:Request|Get|Post|Put|Delete|Patch)Mapping\s*\(\s*(?:value\s*=\s*)?["\']([^"\']+)["\']'
)

_ENDPOINT_ANNOTATIONS: frozenset[str] = frozenset({
    "@GetMapping", "@PostMapping", "@PutMapping", "@DeleteMapping",
    "@PatchMapping", "@RequestMapping",
})

_MODIFIER_WORDS: frozenset[str] = frozenset({
    "public", "private", "protected", "static", "final", "abstract",
    "synchronized", "native", "strictfp", "transient", "volatile", "default",
})

_JAVA_KEYWORDS: frozenset[str] = frozenset({
    "if", "else", "for", "while", "do", "switch", "case", "break", "continue",
    "return", "new", "throw", "try", "catch", "finally", "instanceof",
    "this", "super", "void", "class", "interface", "enum", "extends", "implements",
    "import", "package", "static", "final", "abstract", "synchronized", "native",
    "true", "false", "null",
})

_INJECT_ANNOTATIONS: frozenset[str] = frozenset({
    "@Autowired", "@Inject", "@Value", "@Qualifier", "@Resource",
})

_SPRING_ROLE_MAP: dict[str, str] = {
    "@RestController": "controller",
    "@Controller": "controller",
    "@Service": "service",
    "@Repository": "repository",
    "@Component": "component",
    "@Configuration": "config",
    "@Bean": "config",
}

_SPRING_OTHER: frozenset[str] = frozenset({
    "@Transactional", "@RequestMapping", "@GetMapping", "@PostMapping",
    "@PutMapping", "@DeleteMapping", "@PatchMapping", "@Autowired",
    "@Inject", "@Value", "@Qualifier", "@EnableWebSecurity",
    "@SpringBootApplication", "@EnableAutoConfiguration",
})

_HTTP_METHOD_MAP: dict[str, str] = {
    "@GetMapping": "GET",
    "@PostMapping": "POST",
    "@PutMapping": "PUT",
    "@DeleteMapping": "DELETE",
    "@PatchMapping": "PATCH",
}

# ---------------------------------------------------------------------------
# Phase 5 constants
# ---------------------------------------------------------------------------

# IR weights: fixed per Spring role (spec: controller=1.0, service=0.8, repo=0.7, other=0.3)
_IR_WEIGHTS: dict[str, float] = {
    "controller": 1.0,
    "service": 0.8,
    "repository": 0.7,
}
_IR_WEIGHT_DEFAULT: float = 0.3

# diff_intensity: method change=1.0, field/annotation=0.6, formatting=0.1
_DIFF_INTENSITY_MAP: dict[str, float] = {
    "signature_change": 1.0,
    "route_surface_change": 1.0,
    "structural_change": 0.6,
    "annotation_change": 0.6,
    "unknown": 0.1,
}

_PROPAGATION_DECAY: float = 0.5
_BFS_MAX_DEPTH: int = 3

# Regex to strip leading annotations from a single parameter (e.g. @NotNull @Valid String name)
_ANN_PREFIX_RE = re.compile(r'^(?:@\w+\s*(?:\([^)]*\))?\s*)+')


# ---------------------------------------------------------------------------
# Stable ID helpers
# ---------------------------------------------------------------------------

def _normalize_type_name(raw: str) -> str:
    """Strip annotations, final modifier, and param name; return only type.

    "(Long id)"    -> strip after parsing → "Long"
    "@NotNull User user" → "User"
    "List<String>" → "List<String>"
    """
    raw = _ANN_PREFIX_RE.sub("", raw).strip()
    raw = re.sub(r'\bfinal\s+', "", raw).strip()
    # "Type name" → extract Type (rightmost word is the param name)
    m = re.match(r'^([\w<>\[\].,? ]+?)\s+\w+$', raw)
    if m:
        return m.group(1).strip()
    return raw.strip()


def _parse_param_types(params_str: str) -> list[str]:
    """Parse "(Long id, @Valid String name)" → ["Long", "String"].

    Handles simple param lists only (no nested generic commas).
    For multi-line param lists callers receive an empty string → returns [].
    """
    if not params_str or not params_str.strip():
        return []
    result: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in params_str:
        if ch in ("<", "("):
            depth += 1
            current.append(ch)
        elif ch in (">", ")"):
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            part = "".join(current).strip()
            if part:
                t = _normalize_type_name(part)
                if t:
                    result.append(t)
            current = []
        else:
            current.append(ch)
    part = "".join(current).strip()
    if part:
        t = _normalize_type_name(part)
        if t:
            result.append(t)
    return result


def _normalize_return_type(raw: str) -> str:
    """Normalize return type string: strip whitespace, keep generics."""
    return raw.strip()


def _compute_stable_id(
    package: str,
    class_simple: str,
    kind: str,
    symbol_name: str,
    param_types: Optional[list[str]] = None,
    return_type: str = "",
) -> str:
    """Compute deterministic stable symbol identity.

    Format: {package}:{class_simple}:{kind}:{symbol_name}[:{params}[:{return_type}]]

    Survives: formatting, comments, body changes, imports, nearby movement.
    Changes on: rename, param type change, class package move, kind change.

    Never uses line numbers, byte offsets, or content hashes.
    """
    pkg = package or "_"
    cls = class_simple or "_"
    parts = [pkg, cls, kind, symbol_name]
    if param_types is not None:
        parts.append(f"({','.join(param_types)})")
    if return_type:
        parts.append(_normalize_return_type(return_type))
    return ":".join(parts)


# ---------------------------------------------------------------------------
# Helpers — Phases 1–4
# ---------------------------------------------------------------------------

def _count_net_braces(line: str) -> int:
    depth = 0
    in_str = False
    in_char = False
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == '\\' and (in_str or in_char):
            i += 2
            continue
        if ch == '"' and not in_char:
            in_str = not in_str
        elif ch == "'" and not in_str:
            in_char = not in_char
        elif not in_str and not in_char:
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
        i += 1
    return depth


def _extract_modifiers(text: str) -> list[str]:
    return sorted(w for w in text.split() if w in _MODIFIER_WORDS)


def _parse_modifier_str(s: str) -> list[str]:
    return sorted(w.strip() for w in s.split() if w.strip() in _MODIFIER_WORDS)


def _pop_closed(class_stack: list[tuple[str, int]], depth: int) -> None:
    while class_stack and depth <= class_stack[-1][1]:
        class_stack.pop()


def _resolve_type(simple: str, import_map: dict[str, str]) -> Optional[str]:
    base = re.sub(r'<.*', '', simple).strip().split('.')[-1]
    return import_map.get(base)


def _resolve_types_from_text(text: str, import_map: dict[str, str]) -> list[str]:
    resolved = []
    for token in re.findall(r'\b([A-Z]\w*)\b', text):
        fqn = import_map.get(token)
        if fqn:
            resolved.append(fqn)
    return sorted(set(resolved))


# ---------------------------------------------------------------------------
# Phase 1 — Symbol extraction
# ---------------------------------------------------------------------------

def _extract_symbols(source: str, rel_path: str) -> tuple[str, list[SymbolRecord], list[str]]:
    """Phase 1: Extract symbols from a Java source file.

    Returns (package, symbols, raw_imports).
    """
    package = ""
    pkg_m = _PKG_RE.search(source)
    if pkg_m:
        package = pkg_m.group(1)

    raw_imports: list[str] = [m.group(1) for m in _IMPORT_RE.finditer(source)]
    import_map: dict[str, str] = {}
    for fqn in raw_imports:
        parts = fqn.split(".")
        if parts[-1] != "*":
            import_map[parts[-1]] = fqn

    symbols: list[SymbolRecord] = []
    depth = 0
    class_stack: list[tuple[str, int]] = []
    pending_anns: list[str] = []
    pending_ann_values: dict[str, str] = {}
    in_block_comment = False

    for line in source.splitlines():
        stripped = line.strip()

        if in_block_comment:
            if "*/" in stripped:
                in_block_comment = False
            continue
        if "/*" in stripped:
            if "*/" not in stripped:
                in_block_comment = True
            continue
        if stripped.startswith("//") or stripped.startswith("*"):
            continue

        net = _count_net_braces(stripped)

        if stripped.startswith("@"):
            ann_m = _ANN_WITH_ARGS_RE.match(stripped)
            if ann_m:
                ann = ann_m.group(1)
                ann_args = ann_m.group(2) or ""
                if ann not in pending_anns:
                    pending_anns.append(ann)
                if ann_args and ann in _ENDPOINT_ANNOTATIONS:
                    pending_ann_values[ann] = ann_args.strip()
            depth += net
            _pop_closed(class_stack, depth)
            continue

        cls_m = _CLASS_DECL_RE.search(stripped)
        if cls_m:
            kind_kw = cls_m.group("kind")
            name = cls_m.group("name")

            if class_stack:
                fqn = f"{class_stack[-1][0]}.{name}"
            else:
                fqn = f"{package}.{name}" if package else name

            modifiers = _extract_modifiers(stripped[:cls_m.start()])
            extends_str = (cls_m.group("extends") or "").strip()
            implements_str = (cls_m.group("implements") or "").strip()

            sig_types = ([extends_str] if extends_str else []) + (
                [s.strip() for s in implements_str.split(",") if s.strip()]
                if implements_str else []
            )
            used = _resolve_types_from_text(" ".join(sig_types), import_map)

            sym_type = "interface" if kind_kw == "interface" else "class"

            # symbol_kind distinguishes enum/annotation from class/interface
            if kind_kw == "enum":
                sym_kind = "enum"
            elif kind_kw == "@interface":
                sym_kind = "annotation"
            elif kind_kw == "interface":
                sym_kind = "interface"
            else:
                sym_kind = "class"

            _stable_id = _compute_stable_id(package, name, sym_kind, name)
            _sig_parts = [kind_kw, name]
            if extends_str:
                _sig_parts.append(f"extends {extends_str}")
            if implements_str:
                _sig_parts.append(f"implements {implements_str}")

            symbols.append(SymbolRecord(
                symbol=fqn,
                type=sym_type,
                modifiers=modifiers,
                annotations=sorted(set(pending_anns)),
                imports_used=used,
                declaring_file=rel_path,
                confidence="high",
                stable_id=_stable_id,
                symbol_kind=sym_kind,
                canonical_name=fqn,
                source_file=rel_path,
                signature=" ".join(_sig_parts),
                annotation_values=dict(pending_ann_values),
            ))

            class_stack.append((fqn, depth))
            pending_anns = []
            pending_ann_values = {}
            depth += net
            _pop_closed(class_stack, depth)
            continue

        if class_stack:
            class_fqn = class_stack[-1][0]
            # simple name of enclosing class (last segment, strip inner class paths)
            _class_simple = class_fqn.split(".")[-1]

            mth_m = _METHOD_DECL_RE.match(stripped)
            if mth_m:
                mname = mth_m.group("name")
                if mname not in _JAVA_KEYWORDS:
                    fqn = f"{class_fqn}#{mname}"
                    modifiers = _parse_modifier_str(mth_m.group("modifiers") or "")
                    used = _resolve_types_from_text(stripped, import_map)
                    conf = "high" if ("public" in modifiers or pending_anns) else "medium"

                    # Extract return type and params from matched line
                    _ret_raw = (mth_m.group("return_type") or "").strip()
                    _after_paren = stripped[mth_m.end():]
                    if ")" in _after_paren:
                        _params_str = _after_paren[:_after_paren.index(")")]
                        _param_types = _parse_param_types(_params_str)
                    else:
                        _param_types = []  # multi-line param list — deterministically empty

                    # Determine symbol_kind from annotations
                    _anns = sorted(set(pending_anns))
                    if "@Bean" in _anns:
                        _sym_kind = "bean"
                    elif _anns and any(a in _ENDPOINT_ANNOTATIONS for a in _anns):
                        _sym_kind = "endpoint"
                    else:
                        _sym_kind = "method"

                    _stable_id = _compute_stable_id(
                        package, _class_simple, _sym_kind, mname, _param_types, _ret_raw
                    )
                    _param_str = ",".join(_param_types)
                    _canonical = f"{class_fqn}#{mname}({_param_str})"
                    _signature = f"({_param_str})->{_ret_raw}"

                    symbols.append(SymbolRecord(
                        symbol=fqn,
                        type="method",
                        modifiers=modifiers,
                        annotations=_anns,
                        imports_used=used,
                        declaring_file=rel_path,
                        confidence=conf,
                        stable_id=_stable_id,
                        symbol_kind=_sym_kind,
                        canonical_name=_canonical,
                        source_file=rel_path,
                        signature=_signature,
                        param_types=_param_types,
                        return_type=_ret_raw,
                        annotation_values=dict(pending_ann_values),
                    ))
                    pending_anns = []
                    pending_ann_values = {}
                    depth += net
                    _pop_closed(class_stack, depth)
                    continue

            # Constructor detection: uppercase name matching enclosing class
            ctor_m = _CONSTRUCTOR_DECL_RE.match(stripped)
            if ctor_m and ctor_m.group("name") == _class_simple:
                _ctor_params_str = ctor_m.group("params")
                _ctor_param_types = _parse_param_types(_ctor_params_str)
                _ctor_anns = sorted(set(pending_anns))
                _ctor_modifiers = _parse_modifier_str(ctor_m.group("modifiers") or "")
                _ctor_fqn = f"{class_fqn}#<init>"
                _stable_id = _compute_stable_id(
                    package, _class_simple, "constructor", _class_simple, _ctor_param_types
                )
                _param_str = ",".join(_ctor_param_types)
                symbols.append(SymbolRecord(
                    symbol=_ctor_fqn,
                    type="method",
                    modifiers=_ctor_modifiers,
                    annotations=_ctor_anns,
                    imports_used=[],
                    declaring_file=rel_path,
                    confidence="high" if ("public" in _ctor_modifiers or _ctor_anns) else "medium",
                    stable_id=_stable_id,
                    symbol_kind="constructor",
                    canonical_name=f"{class_fqn}#{_class_simple}({_param_str})",
                    source_file=rel_path,
                    signature=f"({_param_str})->void",
                    param_types=_ctor_param_types,
                    return_type="void",
                ))
                pending_anns = []
                pending_ann_values = {}
                depth += net
                _pop_closed(class_stack, depth)
                continue

            if pending_anns and any(a in _INJECT_ANNOTATIONS for a in pending_anns):
                fld_m = _FIELD_DECL_RE.match(stripped)
                if fld_m:
                    fname = fld_m.group("name")
                    ftype = fld_m.group("type").strip()
                    if fname and ftype and fname not in _JAVA_KEYWORDS:
                        fqn = f"{class_fqn}.{fname}"
                        modifiers = _parse_modifier_str(fld_m.group("modifiers") or "")
                        used = _resolve_types_from_text(ftype, import_map)
                        _stable_id = _compute_stable_id(
                            package, _class_simple, "field", fname, None, ftype
                        )

                        symbols.append(SymbolRecord(
                            symbol=fqn,
                            type="field",
                            modifiers=modifiers,
                            annotations=sorted(set(pending_anns)),
                            imports_used=used,
                            declaring_file=rel_path,
                            confidence="high",
                            stable_id=_stable_id,
                            symbol_kind="field",
                            canonical_name=fqn,
                            source_file=rel_path,
                            signature=f"{ftype} {fname}",
                        ))
                        pending_anns = []
                        pending_ann_values = {}
                        depth += net
                        _pop_closed(class_stack, depth)
                        continue

        pending_anns = []
        pending_ann_values = {}
        depth += net
        _pop_closed(class_stack, depth)

    return package, symbols, raw_imports


# ---------------------------------------------------------------------------
# Phase 2 — Spring semantic tagging
# ---------------------------------------------------------------------------

def _spring_role(annotations: list[str]) -> str:
    for ann in annotations:
        role = _SPRING_ROLE_MAP.get(ann)
        if role:
            return role
    return "unknown"


def _build_spring_summary(symbols: list[SymbolRecord]) -> dict:
    """Phase 2: Aggregate Spring-annotated symbols into a summary."""
    controllers: list[str] = []
    services: list[str] = []
    repositories: list[str] = []
    configs: list[str] = []
    transactional: list[str] = []

    for sym in symbols:
        if sym.type not in ("class", "interface"):
            if "@Transactional" in sym.annotations:
                transactional.append(sym.symbol)
            continue

        role = _spring_role(sym.annotations)
        if role == "controller":
            controllers.append(sym.symbol)
        elif role == "service":
            services.append(sym.symbol)
        elif role == "repository":
            repositories.append(sym.symbol)
        elif role == "config":
            configs.append(sym.symbol)

        if "@Transactional" in sym.annotations:
            transactional.append(sym.symbol)

    return {
        "controllers": sorted(controllers),
        "services": sorted(services),
        "repositories": sorted(repositories),
        "configs": sorted(configs),
        "transactional": sorted(transactional),
    }


def _extract_mapped_paths(source: str, class_fqn: str) -> dict[str, str]:
    paths: dict[str, str] = {}
    for m in _REQUEST_MAPPING_RE.finditer(source):
        paths[m.group(1)] = class_fqn
    return paths


# ---------------------------------------------------------------------------
# Phase 3 — Symbol relation graph
# ---------------------------------------------------------------------------

def _build_relations(
    symbols: list[SymbolRecord],
    raw_imports: list[str],
    source: str,
    package: str,
    rel_path: str,
) -> list[RelationEdge]:
    """Phase 3: Build directed relation graph for symbols in one file."""
    edges: list[RelationEdge] = []

    import_map: dict[str, str] = {}
    for fqn in raw_imports:
        parts = fqn.split(".")
        if parts[-1] != "*":
            import_map[parts[-1]] = fqn

    for sym in symbols:
        sym_fqn = sym.symbol

        for ann in sym.annotations:
            edges.append(RelationEdge(
                from_symbol=sym_fqn,
                to_symbol=ann,
                type="annotated_with",
                confidence="high",
                evidence={"type": "annotation", "value": ann},
            ))

        if sym.type in ("class", "interface"):
            for fqn in raw_imports:
                if fqn.endswith(".*"):
                    continue
                edges.append(RelationEdge(
                    from_symbol=sym_fqn,
                    to_symbol=fqn,
                    type="imports",
                    confidence="high",
                    evidence={"type": "import", "value": fqn},
                ))

        if sym.type == "field":
            for imp_fqn in sym.imports_used:
                edges.append(RelationEdge(
                    from_symbol=sym_fqn,
                    to_symbol=imp_fqn,
                    type="injects",
                    confidence="high",
                    evidence={"type": "annotation", "value": next(
                        (a for a in sym.annotations if a in _INJECT_ANNOTATIONS), "@Autowired"
                    )},
                ))

    for m in re.finditer(
        r'(?:class|interface)\s+(\w+)(?:\s+extends\s+([\w.<>?,\s]+?))?'
        r'(?:\s+implements\s+([\w.<>?,\s]+?))?\s*\{',
        source,
    ):
        name = m.group(1)
        extends_str = (m.group(2) or "").strip()
        implements_str = (m.group(3) or "").strip()
        class_fqn = f"{package}.{name}" if package else name

        if extends_str:
            base = re.sub(r'<.*', '', extends_str).strip()
            to = import_map.get(base, base)
            edges.append(RelationEdge(
                from_symbol=class_fqn,
                to_symbol=to,
                type="extends",
                confidence="high",
                evidence={"type": "signature", "value": f"extends {extends_str}"},
            ))

        if implements_str:
            for iface in implements_str.split(","):
                iface = iface.strip()
                base = re.sub(r'<.*', '', iface).strip()
                if not base:
                    continue
                to = import_map.get(base, base)
                edges.append(RelationEdge(
                    from_symbol=class_fqn,
                    to_symbol=to,
                    type="implements",
                    confidence="high",
                    evidence={"type": "signature", "value": f"implements {iface}"},
                ))

    for m_path, class_fqn in _extract_mapped_paths(source, "").items():
        for sym in symbols:
            if sym.type in ("class", "interface") and (
                "@RestController" in sym.annotations or "@Controller" in sym.annotations
            ):
                edges.append(RelationEdge(
                    from_symbol=sym.symbol,
                    to_symbol=m_path,
                    type="mapped_to",
                    confidence="high",
                    evidence={"type": "annotation", "value": f"@RequestMapping(\"{m_path}\")"},
                ))

    # contained_in edges: method/field → enclosing class (structural membership)
    _local_classes = {s.symbol for s in symbols if s.type in ("class", "interface")}
    for sym in symbols:
        if sym.type in ("method", "field"):
            enclosing = _enclosing_class(sym.symbol)
            if enclosing != sym.symbol and enclosing in _local_classes:
                edges.append(RelationEdge(
                    from_symbol=sym.symbol,
                    to_symbol=enclosing,
                    type="contained_in",
                    confidence="high",
                    evidence={"type": "structural", "value": f"member of {enclosing}"},
                ))

    seen: set[tuple[str, str, str]] = set()
    unique: list[RelationEdge] = []
    for e in edges:
        key = (e.from_symbol, e.to_symbol, e.type)
        if key not in seen:
            seen.add(key)
            unique.append(e)

    return sorted(unique, key=lambda e: (e.from_symbol, e.type, e.to_symbol))


# ---------------------------------------------------------------------------
# Phase 4 — Symbol-level diff
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Route-surface helpers
# ---------------------------------------------------------------------------

def _parse_route_path(args_str: str) -> str:
    """Extract path string from annotation args. Handles named and positional forms."""
    if not args_str:
        return ""
    for key in ("value", "path"):
        m = re.search(rf'\b{key}\s*=\s*"([^"]*)"', args_str)
        if m:
            return m.group(1)
    m = re.search(r'"([^"]*)"', args_str)
    return m.group(1) if m else ""


def _parse_route_http_method(ann_name: str, args_str: str) -> str:
    """Derive HTTP method from annotation name or explicit method= arg."""
    explicit = _HTTP_METHOD_MAP.get(ann_name)
    if explicit:
        return explicit
    m = re.search(r'method\s*=\s*(?:RequestMethod\.)?(\w+)', args_str or "")
    return m.group(1).upper() if m else ""


def _parse_route_extras(args_str: str) -> dict:
    """Extract produces/consumes/params from annotation args."""
    result: dict = {}
    for key in ("produces", "consumes", "params"):
        m = re.search(rf'\b{key}\s*=\s*(?:"([^"]*)"|{{([^}}]*)}})', args_str or "")
        if m:
            result[key] = m.group(1) or m.group(2) or ""
    return result


def _is_route_symbol(sym: SymbolRecord) -> bool:
    return bool(sym.annotation_values) and any(
        a in _ENDPOINT_ANNOTATIONS for a in sym.annotations
    )


def _route_annotation_name(sym: SymbolRecord) -> str:
    for ann in sym.annotations:
        if ann in _ENDPOINT_ANNOTATIONS:
            return ann
    return ""


def _enclosing_class(fqn: str) -> str:
    if "#" in fqn:
        return fqn.split("#")[0]
    if "." in fqn:
        return fqn.rsplit(".", 1)[0]
    return fqn


def _symbol_fingerprint(sym: SymbolRecord) -> str:
    route_val_seg = "|".join(
        f"{a}:{sym.annotation_values.get(a, '')}"
        for a in sorted(sym.annotations)
        if a in _ENDPOINT_ANNOTATIONS
    )
    return (
        f"{sym.type}|{','.join(sym.modifiers)}"
        f"|{','.join(sym.annotations)}|{','.join(sym.imports_used)}"
        f"|{route_val_seg}"
    )


def _diff_symbols(
    old_symbols: list[SymbolRecord],
    new_symbols: list[SymbolRecord],
) -> list[ChangedSymbol]:
    """Phase 4: Compare old vs new symbol sets and classify changes."""
    old_map: dict[str, SymbolRecord] = {s.symbol: s for s in old_symbols}
    new_map: dict[str, SymbolRecord] = {s.symbol: s for s in new_symbols}

    changed: list[ChangedSymbol] = []

    for fqn in sorted(new_map):
        if fqn not in old_map:
            changed.append(ChangedSymbol(
                symbol=fqn,
                change_type="added",
                diff_type="structural_change",
                confidence="high",
            ))

    for fqn in sorted(old_map):
        if fqn not in new_map:
            changed.append(ChangedSymbol(
                symbol=fqn,
                change_type="removed",
                diff_type="structural_change",
                confidence="high",
            ))

    for fqn in sorted(old_map):
        if fqn not in new_map:
            continue
        old = old_map[fqn]
        new = new_map[fqn]
        if _symbol_fingerprint(old) == _symbol_fingerprint(new):
            continue

        diff_type = "unknown"
        old_rvals = {a: old.annotation_values.get(a, "") for a in old.annotations if a in _ENDPOINT_ANNOTATIONS}
        new_rvals = {a: new.annotation_values.get(a, "") for a in new.annotations if a in _ENDPOINT_ANNOTATIONS}
        if old_rvals != new_rvals:
            diff_type = "route_surface_change"
        elif set(old.annotations) != set(new.annotations):
            diff_type = "annotation_change"
        elif set(old.modifiers) != set(new.modifiers):
            diff_type = "structural_change"
        elif set(old.imports_used) != set(new.imports_used):
            diff_type = "signature_change"

        changed.append(ChangedSymbol(
            symbol=fqn,
            change_type="modified",
            diff_type=diff_type,
            confidence="high",
        ))

    return changed


def _diff_routes(
    old_syms: list[SymbolRecord],
    new_syms: list[SymbolRecord],
) -> list[dict]:
    """Detect route-surface changes between old and new symbol sets."""
    old_map = {s.symbol: s for s in old_syms if _is_route_symbol(s)}
    new_map = {s.symbol: s for s in new_syms if _is_route_symbol(s)}

    route_diffs: list[dict] = []
    for fqn in sorted(set(old_map) & set(new_map)):
        old_sym = old_map[fqn]
        new_sym = new_map[fqn]

        old_ann = _route_annotation_name(old_sym)
        new_ann = _route_annotation_name(new_sym)
        old_args = old_sym.annotation_values.get(old_ann, "")
        new_args = new_sym.annotation_values.get(new_ann, "")

        old_path = _parse_route_path(old_args)
        new_path = _parse_route_path(new_args)
        old_http = _parse_route_http_method(old_ann, old_args)
        new_http = _parse_route_http_method(new_ann, new_args)
        old_extras = _parse_route_extras(old_args)
        new_extras = _parse_route_extras(new_args)

        if old_path == new_path and old_http == new_http and old_ann == new_ann and old_extras == new_extras:
            continue

        evidence: dict = {
            "annotation_value_changed": old_path != new_path,
            "mapping_annotation": new_ann.lstrip("@"),
            "old_value": old_path,
            "new_value": new_path,
        }
        if old_http != new_http:
            evidence["http_method_changed"] = True
            evidence["old_http_method"] = old_http
            evidence["new_http_method"] = new_http
        if old_ann != new_ann:
            evidence["annotation_changed"] = True
            evidence["old_annotation"] = old_ann
            evidence["new_annotation"] = new_ann
        for key in ("produces", "consumes", "params"):
            if old_extras.get(key) != new_extras.get(key):
                evidence[f"{key}_changed"] = True
                evidence[f"old_{key}"] = old_extras.get(key, "")
                evidence[f"new_{key}"] = new_extras.get(key, "")

        route_diffs.append({
            "symbol": fqn,
            "controller": _enclosing_class(fqn),
            "route_surface_changed": True,
            "old_route": old_path,
            "new_route": new_path,
            "stable_id": new_sym.stable_id,
            "evidence": evidence,
        })

    return sorted(route_diffs, key=lambda d: d["symbol"])


def _get_git_old_content(git_root: Path, rel_path: str, since: str) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "show", f"{since}:{rel_path}"],
            cwd=str(git_root),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
        pass
    return None


# ---------------------------------------------------------------------------
# Phase 5 — Evidence Engine
# ---------------------------------------------------------------------------

def _diff_intensity_cs(cs: ChangedSymbol) -> float:
    """Map a ChangedSymbol to diff_intensity (spec: method=1.0, field=0.6, formatting=0.1)."""
    if cs.change_type in ("added", "removed"):
        return 1.0
    return _DIFF_INTENSITY_MAP.get(cs.diff_type, 0.1)


def _bfs_reachability(start: str, adjacency: dict[str, set[str]], max_depth: int = _BFS_MAX_DEPTH) -> int:
    """Count nodes reachable from start within max_depth hops (excluding start)."""
    visited: set[str] = {start}
    frontier: list[str] = [start]
    for _ in range(max_depth):
        next_frontier: list[str] = []
        for node in frontier:
            for neighbor in adjacency.get(node, set()):
                if neighbor not in visited:
                    visited.add(neighbor)
                    next_frontier.append(neighbor)
        frontier = next_frontier
        if not frontier:
            break
    return len(visited) - 1


def _build_evidence_bundles(
    symbols: list[SymbolRecord],
    relations: list[RelationEdge],
    changed_symbols: list[ChangedSymbol],
) -> dict[str, EvidenceBundle]:
    """Phase 5: Build evidence bundles for each symbol in the current IR."""
    # Index graph links by node FQN (both ends of each edge)
    graph_links_by_node: dict[str, list[str]] = {}
    for e in relations:
        key = f"{e.from_symbol}→{e.to_symbol}[{e.type}]"
        graph_links_by_node.setdefault(e.from_symbol, []).append(key)
        graph_links_by_node.setdefault(e.to_symbol, []).append(key)

    changed_map: dict[str, ChangedSymbol] = {cs.symbol: cs for cs in changed_symbols}

    bundles: dict[str, EvidenceBundle] = {}
    for sym in symbols:
        fqn = sym.symbol
        ir_strength = {"high": 1.0, "medium": 0.7, "low": 0.3}.get(sym.confidence, 0.5)
        evidence_items: list[dict] = [{"source": "ir_phase1", "strength": ir_strength}]

        g_links = sorted(set(graph_links_by_node.get(fqn, [])))
        if g_links:
            evidence_items.append({"source": "graph_edge", "strength": 1.0})

        d_links: list[str] = []
        cs = changed_map.get(fqn)
        if cs:
            d_links = [fqn]
            evidence_items.append({"source": "git_diff", "strength": _diff_intensity_cs(cs)})

        bundles[fqn] = EvidenceBundle(
            entity=fqn,
            type="symbol",
            evidence=evidence_items,
            graph_links=g_links,
            diff_links=d_links,
            ir_links=[fqn],
        )

    # Removed symbols: in diff but not in current IR — diff evidence only
    for cs in changed_symbols:
        if cs.symbol not in bundles and cs.change_type == "removed":
            bundles[cs.symbol] = EvidenceBundle(
                entity=cs.symbol,
                type="symbol",
                evidence=[{"source": "git_diff", "strength": 1.0}],
                graph_links=[],
                diff_links=[cs.symbol],
                ir_links=[],
            )

    return bundles


def _detect_subsystems(all_fqns: list[str], relations: list[RelationEdge]) -> list[list[str]]:
    """Connected components of the relation graph (Union-Find, graph-only)."""
    fqn_set = set(all_fqns)
    parent: dict[str, str] = {fqn: fqn for fqn in all_fqns}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: str, y: str) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    for edge in relations:
        f, t = edge.from_symbol, edge.to_symbol
        if f in fqn_set and t in fqn_set:
            union(f, t)

    components: dict[str, list[str]] = {}
    for fqn in all_fqns:
        root = find(fqn)
        components.setdefault(root, []).append(fqn)

    return [sorted(v) for v in sorted(components.values())]


_EDGE_REASON_TEMPLATES: dict[str, str] = {
    "imports": "{from_sym} depends on {to_sym} (import)",
    "injects": "{from_sym} injects {to_sym}",
    "implements": "{from_sym} implements {to_sym}",
    "extends": "{from_sym} extends {to_sym}",
    "contained_in": "{from_sym} is a member of {to_sym}",
    "annotated_with": "{from_sym} is annotated with {to_sym}",
    "mapped_to": "Route {to_sym} depends on {from_sym}",
}

# Edge types to exclude from reverse impact traversal (too noisy / non-dependency semantics)
_REVERSE_EXCLUDE: frozenset[str] = frozenset({"annotated_with", "mapped_to"})


def _edge_reason(edge_type: str, from_sym: str, to_sym: str) -> str:
    tmpl = _EDGE_REASON_TEMPLATES.get(
        edge_type, "{from_sym} → {to_sym} [{edge_type}]"
    )
    return tmpl.format(from_sym=from_sym, to_sym=to_sym, edge_type=edge_type)


def _build_reverse_adjacency(
    relations: list[RelationEdge],
    all_fqns: set[str],
) -> dict[str, list[RelationEdge]]:
    """Invert the relation graph: target → edges pointing to it (known symbols only)."""
    reverse: dict[str, list[RelationEdge]] = {}
    for edge in relations:
        if edge.type in _REVERSE_EXCLUDE:
            continue
        if edge.to_symbol in all_fqns:
            reverse.setdefault(edge.to_symbol, []).append(edge)
    return reverse


def _bfs_impact_with_paths(
    changed_fqns: set[str],
    changed_scores: dict[str, float],
    reverse_adj: dict[str, list[RelationEdge]],
    all_fqns: set[str],
    max_depth: int = _BFS_MAX_DEPTH,
    enclosing_seeds: set[str] | None = None,
) -> list[dict]:
    """BFS on reverse graph: propagates impact from changed symbols to dependents.

    Each impacted entry carries included_because: explicit graph path explaining inclusion.
    No graph path → no impact (deterministic guarantee).

    enclosing_seeds: set of extra seeds that are enclosing classes (not directly changed).
    contained_in edges are skipped when traversing FROM these seeds to avoid pulling in
    sibling members of the actually-changed symbol.
    """
    _enclosing = enclosing_seeds or set()
    impacted: dict[str, dict] = {}
    # (node, via_fqn, depth, score, path, reasons)
    queue: deque[tuple[str, str, int, float, list[str], list[str]]] = deque()

    for fqn in sorted(changed_fqns):
        base = changed_scores.get(fqn, 0.0)
        skip_contained = fqn in _enclosing
        for edge in sorted(reverse_adj.get(fqn, []), key=lambda e: e.from_symbol):
            if skip_contained and edge.type == "contained_in":
                continue
            neighbor = edge.from_symbol
            if neighbor not in changed_fqns and neighbor in all_fqns:
                score = round(base * _PROPAGATION_DECAY, 4)
                if score > 0:
                    reason = _edge_reason(edge.type, neighbor, fqn)
                    queue.append((neighbor, fqn, 1, score, [fqn, neighbor], [reason]))

    while queue:
        node, via, depth, score, path, reasons = queue.popleft()
        existing = impacted.get(node)
        if existing and existing["impact_score"] >= score:
            continue
        impacted[node] = {
            "entity": node,
            "depth": depth,
            "impact_score": score,
            "via": via,
            "graph_path": path,
            "included_because": reasons,
        }
        if depth < max_depth:
            for edge in sorted(reverse_adj.get(node, []), key=lambda e: e.from_symbol):
                neighbor = edge.from_symbol
                if neighbor not in changed_fqns and neighbor in all_fqns:
                    next_score = round(score * _PROPAGATION_DECAY, 4)
                    if next_score > 0:
                        reason = _edge_reason(edge.type, neighbor, node)
                        queue.append((
                            neighbor, node, depth + 1, next_score,
                            path + [neighbor],
                            reasons + [reason],
                        ))

    return sorted(impacted.values(), key=lambda x: (-x["impact_score"], x["entity"]))


# ---------------------------------------------------------------------------
# Phase 5 — Assembly: single output contract
# ---------------------------------------------------------------------------

def _edge_to_dict(edge: RelationEdge) -> dict:
    return {
        "from": edge.from_symbol,
        "to": edge.to_symbol,
        "type": edge.type,
        "confidence": edge.confidence,
        "evidence": edge.evidence,
    }


def _assemble(
    symbols: list[SymbolRecord],
    relations: list[RelationEdge],
    changed_symbols: list[ChangedSymbol],
    spring_summary: dict,  # noqa: ARG001 — used internally via _spring_role on symbols
    route_diffs: list[dict] | None = None,
) -> dict:
    """Phase 5: Final assembly — single deterministic output contract."""
    sorted_syms = sorted(symbols, key=lambda s: s.symbol)
    sorted_rels = sorted(relations, key=lambda e: (e.from_symbol, e.type, e.to_symbol))
    sorted_changed = sorted(changed_symbols, key=lambda c: c.symbol)

    # Spring role map: fqn → role (from annotation evidence only)
    spring_role_map: dict[str, str] = {}
    for sym in sorted_syms:
        if sym.type in ("class", "interface"):
            role = _spring_role(sym.annotations)
            spring_role_map[sym.symbol] = role

    # Degree maps (graph-derived)
    in_deg: dict[str, int] = {}
    out_deg: dict[str, int] = {}
    for e in sorted_rels:
        out_deg[e.from_symbol] = out_deg.get(e.from_symbol, 0) + 1
        in_deg[e.to_symbol] = in_deg.get(e.to_symbol, 0) + 1

    # Directed adjacency list (graph-only)
    adjacency: dict[str, set[str]] = {}
    for e in sorted_rels:
        adjacency.setdefault(e.from_symbol, set()).add(e.to_symbol)

    all_fqns_set = {s.symbol for s in sorted_syms}

    # Bounded BFS reachability per node (graph-only)
    bfs_reach: dict[str, int] = {
        s.symbol: _bfs_reachability(s.symbol, adjacency)
        for s in sorted_syms
    }

    # Normalize centrality across all nodes
    max_raw = max(
        (in_deg.get(s.symbol, 0) + out_deg.get(s.symbol, 0) + bfs_reach.get(s.symbol, 0) * 0.1
         for s in sorted_syms),
        default=1.0,
    ) or 1.0

    # Build evidence bundles (Phase 5 core)
    bundles = _build_evidence_bundles(sorted_syms, sorted_rels, sorted_changed)

    # Changed map for score computation
    changed_map: dict[str, ChangedSymbol] = {cs.symbol: cs for cs in sorted_changed}

    # Score per node: ir_weight × graph_centrality × diff_intensity × evidence_strength
    # Unchanged nodes: diff_intensity=0 → score=0 (no diff signal)
    node_scores: dict[str, float] = {}
    for sym in sorted_syms:
        fqn = sym.symbol
        role = spring_role_map.get(fqn, "other")
        w = _IR_WEIGHTS.get(role, _IR_WEIGHT_DEFAULT)
        raw_c = in_deg.get(fqn, 0) + out_deg.get(fqn, 0) + bfs_reach.get(fqn, 0) * 0.1
        c = min(1.0, raw_c / max_raw)
        cs = changed_map.get(fqn)
        di = _diff_intensity_cs(cs) if cs else 0.0
        es = bundles[fqn].evidence_strength if fqn in bundles else 0.0
        node_scores[fqn] = round(w * c * di * es, 4) if di > 0 else 0.0

    # --- Analysis: classify changed symbols ---
    dropped_fields: list[dict] = []
    changed_entities_out: list[dict] = []
    isolated_changes_out: list[dict] = []
    validated_changes_out: list[dict] = []
    change_set_out: list[dict] = []

    for cs in sorted_changed:
        fqn = cs.symbol
        bundle = bundles.get(fqn)
        score = node_scores.get(fqn, 0.0)
        role = spring_role_map.get(fqn, "other")
        w = _IR_WEIGHTS.get(role, _IR_WEIGHT_DEFAULT)
        raw_c = in_deg.get(fqn, 0) + out_deg.get(fqn, 0) + bfs_reach.get(fqn, 0) * 0.1
        c = round(min(1.0, raw_c / max_raw), 4)
        di = _diff_intensity_cs(cs)
        es = bundle.evidence_strength if bundle else 0.0

        entry = {
            "entity": fqn,
            "change_type": cs.change_type,
            "diff_type": cs.diff_type,
            "score": score,
        }

        if bundle and bundle.graph_links:
            changed_entities_out.append(entry)
            if bundle.is_complete:
                validated_changes_out.append(entry)
            # is_complete requires diff_links too — already true since cs exists
        else:
            # No graph evidence → isolated (cannot propagate, cannot validate)
            isolated_changes_out.append(entry)
            dropped_fields.append({
                "field": "validated_changes",
                "entity": fqn,
                "reason": "no graph evidence",
            })

        change_set_out.append({
            "entity": fqn,
            "change_type": cs.change_type,
            "diff_type": cs.diff_type,
            "ir_weight": w,
            "graph_centrality": c,
            "diff_intensity": di,
            "evidence_strength": es,
            "score": score,
            "evidence_bundle": bundle.to_dict() if bundle else None,
        })

    # --- Reverse graph: target → dependents (for impact propagation + agent queries) ---
    reverse_adj = _build_reverse_adjacency(sorted_rels, all_fqns_set)

    # --- Impact propagation (BFS on reverse graph — finds who depends on changed symbol) ---
    changed_with_graph = {e["entity"] for e in changed_entities_out}
    changed_scores_map = {fqn: node_scores.get(fqn, 0.0) for fqn in changed_with_graph}

    # Method/field change → also propagate from enclosing class (class is effectively changed).
    # These are "enclosing seeds" — contained_in edges are skipped from them to avoid
    # pulling in sibling members of the actually-changed symbol.
    _enclosing_seeds: set[str] = set()
    _extra_seeds: dict[str, float] = {}
    for fqn, score in list(changed_scores_map.items()):
        enclosing = _enclosing_class(fqn)
        if enclosing != fqn and enclosing in all_fqns_set and enclosing not in changed_scores_map:
            _extra_seeds[enclosing] = max(_extra_seeds.get(enclosing, 0.0), score)
            _enclosing_seeds.add(enclosing)
    changed_with_graph.update(_extra_seeds)
    changed_scores_map.update(_extra_seeds)

    impacted_entities_out = _bfs_impact_with_paths(
        changed_with_graph, changed_scores_map, reverse_adj, all_fqns_set,
        enclosing_seeds=_enclosing_seeds,
    )

    # --- Subsystem detection (connected components, graph-only) ---
    subsystems = _detect_subsystems(sorted(all_fqns_set), sorted_rels)

    # --- Impact summary ---
    global_score = round(sum(node_scores.values()), 4)

    ranked_nodes = sorted(
        [
            {
                "entity": s.symbol,
                "type": s.type,
                "role": spring_role_map.get(s.symbol, "other"),
                "score": node_scores.get(s.symbol, 0.0),
            }
            for s in sorted_syms
        ],
        key=lambda n: (-n["score"], n["entity"]),
    )

    # --- Graph output ---
    graph_nodes = [
        {
            "fqn": s.symbol,
            "stable_id": s.stable_id,
            "symbol_kind": s.symbol_kind,
            "canonical_name": s.canonical_name or s.symbol,
            "source_file": s.declaring_file,
            "signature": s.signature,
            "type": s.type,
            "role": spring_role_map.get(s.symbol, "other"),
            "in_degree": in_deg.get(s.symbol, 0),
            "out_degree": out_deg.get(s.symbol, 0),
        }
        for s in sorted_syms
    ]
    graph_edges = [_edge_to_dict(e) for e in sorted_rels]

    # Reverse graph index: target_fqn → {edge_type → [from_fqn, ...]} for agent queries
    reverse_graph_out: dict[str, dict[str, list[str]]] = {}
    for target, edges_in in sorted(reverse_adj.items()):
        by_type: dict[str, list[str]] = {}
        for e in sorted(edges_in, key=lambda x: x.from_symbol):
            by_type.setdefault(e.type, []).append(e.from_symbol)
        reverse_graph_out[target] = by_type

    return {
        "schema_version": "final-v1",
        "graph": {
            "nodes": graph_nodes,
            "edges": graph_edges,
        },
        "reverse_graph": reverse_graph_out,
        "analysis": {
            "changed_entities": changed_entities_out,
            "impacted_entities": impacted_entities_out,
            "isolated_changes": isolated_changes_out,
            "validated_changes": validated_changes_out,
        },
        "impact": {
            "global_score": global_score,
            "ranked_nodes": ranked_nodes,
        },
        "subsystems": subsystems,
        "change_set": change_set_out,
        "route_surface": route_diffs or [],
        "audit": {
            "dropped_fields": dropped_fields,
        },
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_file_ir(
    source: str,
    rel_path: str,
    *,
    old_source: Optional[str] = None,
) -> dict:
    """Build IR for a single Java file.

    Args:
        source:     Current file content.
        rel_path:   Relative path within the repo (used as declaring_file).
        old_source: Optional baseline content for symbol diff (Phase 4).

    Returns single deterministic IR dict (schema_version=final-v1).
    """
    package, symbols, raw_imports = _extract_symbols(source, rel_path)
    relations = _build_relations(symbols, raw_imports, source, package, rel_path)
    spring_summary = _build_spring_summary(symbols)

    changed_symbols: list[ChangedSymbol] = []
    route_diffs: list[dict] = []
    if old_source is not None:
        _, old_symbols, _ = _extract_symbols(old_source, rel_path)
        changed_symbols = _diff_symbols(old_symbols, symbols)
        route_diffs = _diff_routes(old_symbols, symbols)

    return _assemble(symbols, relations, changed_symbols, spring_summary, route_diffs)


def build_repo_ir(
    file_paths: list[str],
    root: Path,
    *,
    since: Optional[str] = None,
) -> dict:
    """Build IR across multiple Java files in a repo.

    Args:
        file_paths: Relative paths to Java files to analyze.
        root:       Absolute repo root.
        since:      Git ref for symbol diff (e.g. "HEAD~1", "main").

    Returns aggregated deterministic IR dict (schema_version=final-v1).
    """
    all_symbols: list[SymbolRecord] = []
    all_relations: list[RelationEdge] = []
    all_changed: list[ChangedSymbol] = []
    all_route_diffs: list[dict] = []

    for rel_path in sorted(file_paths):
        abs_path = root / rel_path
        try:
            source = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        old_source: Optional[str] = None
        if since:
            old_source = _get_git_old_content(root, rel_path, since)

        package, symbols, raw_imports = _extract_symbols(source, rel_path)
        relations = _build_relations(symbols, raw_imports, source, package, rel_path)

        if old_source is not None:
            _, old_symbols, _ = _extract_symbols(old_source, rel_path)
            all_changed.extend(_diff_symbols(old_symbols, symbols))
            all_route_diffs.extend(_diff_routes(old_symbols, symbols))
        elif since:
            for sym in symbols:
                all_changed.append(ChangedSymbol(
                    symbol=sym.symbol,
                    change_type="added",
                    diff_type="structural_change",
                    confidence="high",
                ))

        all_symbols.extend(symbols)
        all_relations.extend(relations)

    spring_summary = _build_spring_summary(all_symbols)

    # Deduplicate relations
    seen: set[tuple[str, str, str]] = set()
    unique_relations: list[RelationEdge] = []
    for e in all_relations:
        key = (e.from_symbol, e.to_symbol, e.type)
        if key not in seen:
            seen.add(key)
            unique_relations.append(e)

    all_route_diffs_sorted = sorted(all_route_diffs, key=lambda d: d["symbol"])
    return _assemble(all_symbols, unique_relations, all_changed, spring_summary, all_route_diffs_sorted)


# ---------------------------------------------------------------------------
# Convenience: find Java files in a repo
# ---------------------------------------------------------------------------

def find_java_files(root: Path, *, max_files: int = 500) -> list[str]:
    """Return relative paths to Java files under root, excluding test dirs."""
    results: list[str] = []
    for p in sorted(root.rglob("*.java")):
        if len(results) >= max_files:
            break
        try:
            rel = str(p.relative_to(root)).replace("\\", "/")
        except ValueError:
            continue
        if "/test/" in rel or "/tests/" in rel or rel.startswith("test/"):
            continue
        results.append(rel)
    return results

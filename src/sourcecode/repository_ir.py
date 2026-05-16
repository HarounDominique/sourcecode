"""repository_ir.py — Deterministic Repository Intermediate Representation.

5-phase Java IR pipeline:
  Phase 1: Symbol extraction (class/interface/method/field)
  Phase 2: Spring semantic tagging (annotation-gated only)
  Phase 3: Symbol relation graph (statically detectable edges)
  Phase 4: Symbol-level diff (vs git baseline)
  Phase 5: Final IR assembly

Deterministic: identical inputs → identical output.
No architectural inference without graph evidence.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class SymbolRecord:
    symbol: str          # fully qualified: pkg.Class | pkg.Class#method | pkg.Class.field
    type: str            # class | interface | method | field
    modifiers: list[str] = field(default_factory=list)
    annotations: list[str] = field(default_factory=list)
    imports_used: list[str] = field(default_factory=list)
    declaring_file: str = ""
    confidence: str = "medium"  # high | medium | low


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
# Regex patterns
# ---------------------------------------------------------------------------

_PKG_RE = re.compile(r'^package\s+([\w.]+)\s*;', re.MULTILINE)
_IMPORT_RE = re.compile(r'^import\s+(?:static\s+)?([\w.]+(?:\.\*)?)\s*;', re.MULTILINE)
_ANN_RE = re.compile(r'^(@[\w.]+)')

_CLASS_DECL_RE = re.compile(
    r'(?:^|(?<=\s))'
    r'(?P<kind>class|interface|enum|@interface)\s+'
    r'(?P<name>[A-Z]\w*)'                         # class names start uppercase
    r'(?:\s*<[^{;]*?(?=>|\{))?'                   # optional generic params (non-greedy)
    r'(?:\s+extends\s+(?P<extends>[\w.<>?,\s]+?))?'
    r'(?:\s+implements\s+(?P<implements>[\w.<>?,\s]+?))?'
    r'(?:\s+permits\s+[\w,\s]+?)?'
    r'\s*\{',
)

# Method: modifiers + optional generic + return type + name + (
_METHOD_DECL_RE = re.compile(
    r'^(?P<modifiers>(?:(?:public|private|protected|static|final|synchronized'
    r'|abstract|default|native|strictfp|override)\s+)*)'
    r'(?:<[\w,\s?]+>\s+)?'                         # generic type params on method
    r'(?:(?:void|boolean|byte|char|short|int|long|float|double|String|[\w.<>\[\]?,]+)\s+)'
    r'(?P<name>[a-z_]\w*)\s*\(',                   # method name starts lowercase
)

# Annotated field: modifiers + type + name + ; or =
_FIELD_DECL_RE = re.compile(
    r'^(?P<modifiers>(?:(?:private|protected|public|static|final|volatile|transient)\s+)*)'
    r'(?P<type>[\w<>.,\[\]? ]+?)\s+'
    r'(?P<name>[a-z_]\w*)\s*[;=]',
)

_REQUEST_MAPPING_RE = re.compile(
    r'@(?:Request|Get|Post|Put|Delete|Patch)Mapping\s*\(\s*(?:value\s*=\s*)?["\']([^"\']+)["\']'
)

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

# Spring annotation → role (None = not a role annotation)
_SPRING_ROLE_MAP: dict[str, str] = {
    "@RestController": "controller",
    "@Controller": "controller",
    "@Service": "service",
    "@Repository": "repository",
    "@Component": "component",
    "@Configuration": "config",
    "@Bean": "config",
}

# Spring annotations that exist but don't define a role
_SPRING_OTHER: frozenset[str] = frozenset({
    "@Transactional", "@RequestMapping", "@GetMapping", "@PostMapping",
    "@PutMapping", "@DeleteMapping", "@PatchMapping", "@Autowired",
    "@Inject", "@Value", "@Qualifier", "@EnableWebSecurity",
    "@SpringBootApplication", "@EnableAutoConfiguration",
})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _count_net_braces(line: str) -> int:
    """Count { minus } on the line, skipping string and char literals."""
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
    """Resolve a simple type name to its FQN using the import map."""
    base = re.sub(r'<.*', '', simple).strip().split('.')[-1]
    return import_map.get(base)


def _resolve_types_from_text(text: str, import_map: dict[str, str]) -> list[str]:
    """Extract all type names from a signature/type string and resolve them."""
    resolved = []
    # Find all capitalized identifiers (Java types are PascalCase)
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
    # Package
    package = ""
    pkg_m = _PKG_RE.search(source)
    if pkg_m:
        package = pkg_m.group(1)

    # Imports
    raw_imports: list[str] = [m.group(1) for m in _IMPORT_RE.finditer(source)]
    import_map: dict[str, str] = {}
    for fqn in raw_imports:
        parts = fqn.split(".")
        if parts[-1] != "*":
            import_map[parts[-1]] = fqn

    symbols: list[SymbolRecord] = []
    depth = 0
    class_stack: list[tuple[str, int]] = []  # (fqn, depth_before_opening_brace)
    pending_anns: list[str] = []
    in_block_comment = False

    for line in source.splitlines():
        stripped = line.strip()

        # Block comment
        if in_block_comment:
            if "*/" in stripped:
                in_block_comment = False
            continue
        if "/*" in stripped:
            if "*/" not in stripped:
                in_block_comment = True
            # still process the line content before /*  — but simpler to skip
            continue
        if stripped.startswith("//") or stripped.startswith("*"):
            continue

        net = _count_net_braces(stripped)

        # Annotation accumulation
        if stripped.startswith("@"):
            ann_m = _ANN_RE.match(stripped)
            if ann_m:
                ann = ann_m.group(1)
                if ann not in pending_anns:
                    pending_anns.append(ann)
            depth += net
            _pop_closed(class_stack, depth)
            continue

        # --- Class / interface / enum declaration ---
        cls_m = _CLASS_DECL_RE.search(stripped)
        if cls_m:
            kind_kw = cls_m.group("kind")
            name = cls_m.group("name")

            # FQN: use outer class if nested, else package prefix
            if class_stack:
                fqn = f"{class_stack[-1][0]}.{name}"
            else:
                fqn = f"{package}.{name}" if package else name

            modifiers = _extract_modifiers(stripped[:cls_m.start()])
            extends_str = (cls_m.group("extends") or "").strip()
            implements_str = (cls_m.group("implements") or "").strip()

            # imports_used: resolve extends/implements type names
            sig_types = ([extends_str] if extends_str else []) + (
                [s.strip() for s in implements_str.split(",") if s.strip()]
                if implements_str else []
            )
            used = _resolve_types_from_text(" ".join(sig_types), import_map)

            sym_type = "interface" if kind_kw == "interface" else "class"

            symbols.append(SymbolRecord(
                symbol=fqn,
                type=sym_type,
                modifiers=modifiers,
                annotations=sorted(set(pending_anns)),
                imports_used=used,
                declaring_file=rel_path,
                confidence="high",
            ))

            class_stack.append((fqn, depth))
            pending_anns = []
            depth += net
            _pop_closed(class_stack, depth)
            continue

        # --- Method declaration (only inside a class) ---
        if class_stack:
            mth_m = _METHOD_DECL_RE.match(stripped)
            if mth_m:
                mname = mth_m.group("name")
                if mname not in _JAVA_KEYWORDS:
                    class_fqn = class_stack[-1][0]
                    fqn = f"{class_fqn}#{mname}"
                    modifiers = _parse_modifier_str(mth_m.group("modifiers") or "")
                    used = _resolve_types_from_text(stripped, import_map)
                    # High confidence for public/annotated methods, medium otherwise
                    conf = "high" if ("public" in modifiers or pending_anns) else "medium"

                    symbols.append(SymbolRecord(
                        symbol=fqn,
                        type="method",
                        modifiers=modifiers,
                        annotations=sorted(set(pending_anns)),
                        imports_used=used,
                        declaring_file=rel_path,
                        confidence=conf,
                    ))
                    pending_anns = []
                    depth += net
                    _pop_closed(class_stack, depth)
                    continue

            # --- Annotated field declaration ---
            if pending_anns and any(a in _INJECT_ANNOTATIONS for a in pending_anns):
                fld_m = _FIELD_DECL_RE.match(stripped)
                if fld_m:
                    fname = fld_m.group("name")
                    ftype = fld_m.group("type").strip()
                    if fname and ftype and fname not in _JAVA_KEYWORDS:
                        class_fqn = class_stack[-1][0]
                        fqn = f"{class_fqn}.{fname}"
                        modifiers = _parse_modifier_str(fld_m.group("modifiers") or "")
                        used = _resolve_types_from_text(ftype, import_map)

                        symbols.append(SymbolRecord(
                            symbol=fqn,
                            type="field",
                            modifiers=modifiers,
                            annotations=sorted(set(pending_anns)),
                            imports_used=used,
                            declaring_file=rel_path,
                            confidence="high",
                        ))
                        pending_anns = []
                        depth += net
                        _pop_closed(class_stack, depth)
                        continue

        # Clear pending annotations if this line is not a declaration
        pending_anns = []
        depth += net
        _pop_closed(class_stack, depth)

    return package, symbols, raw_imports


# ---------------------------------------------------------------------------
# Phase 2 — Spring semantic tagging
# ---------------------------------------------------------------------------

def _spring_role(annotations: list[str]) -> str:
    """Derive Spring role from annotation list. Returns 'unknown' if none match."""
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
    mapped_paths: dict[str, str] = {}

    for sym in symbols:
        if sym.type not in ("class", "interface"):
            # Collect transactional methods
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
        "mapped_paths": dict(sorted(mapped_paths.items())),
    }


def _attach_spring_roles(symbols: list[SymbolRecord], source_map: dict[str, str]) -> None:
    """Attach spring_role to class symbols using the raw source for @RequestMapping paths."""
    for sym in symbols:
        if sym.type not in ("class", "interface") or sym.type == "method":
            continue
        # Attach mapped_to from source (cannot be done purely from SymbolRecord)


def _extract_mapped_paths(source: str, class_fqn: str) -> dict[str, str]:
    """Extract @RequestMapping paths → class FQN from raw source."""
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

    # Build lookup: simple_name → fqn from imports
    import_map: dict[str, str] = {}
    for fqn in raw_imports:
        parts = fqn.split(".")
        if parts[-1] != "*":
            import_map[parts[-1]] = fqn

    for sym in symbols:
        sym_fqn = sym.symbol

        # annotated_with edges
        for ann in sym.annotations:
            edges.append(RelationEdge(
                from_symbol=sym_fqn,
                to_symbol=ann,
                type="annotated_with",
                confidence="high",
                evidence={"type": "annotation", "value": ann},
            ))

        # For classes: extends / implements / injects
        if sym.type in ("class", "interface"):
            # imports edges (from file-level imports)
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
            # injects: @Autowired/@Inject field → resolved type
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

    # extends / implements edges from class symbol's imports_used
    # These were already resolved during extraction; rebuild from source
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

    # mapped_to edges from @RequestMapping
    for m_path, class_fqn in _extract_mapped_paths(source, "").items():
        # Find the class FQN for this source
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

    # Deduplicate edges (same from/to/type)
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

def _symbol_fingerprint(sym: SymbolRecord) -> str:
    """Stable fingerprint of a symbol for change detection."""
    return f"{sym.type}|{','.join(sym.modifiers)}|{','.join(sym.annotations)}|{','.join(sym.imports_used)}"


def _diff_symbols(
    old_symbols: list[SymbolRecord],
    new_symbols: list[SymbolRecord],
) -> list[ChangedSymbol]:
    """Phase 4: Compare old vs new symbol sets and classify changes."""
    old_map: dict[str, SymbolRecord] = {s.symbol: s for s in old_symbols}
    new_map: dict[str, SymbolRecord] = {s.symbol: s for s in new_symbols}

    changed: list[ChangedSymbol] = []

    # Added
    for fqn in sorted(new_map):
        if fqn not in old_map:
            changed.append(ChangedSymbol(
                symbol=fqn,
                change_type="added",
                diff_type="structural_change",
                confidence="high",
            ))

    # Removed
    for fqn in sorted(old_map):
        if fqn not in new_map:
            changed.append(ChangedSymbol(
                symbol=fqn,
                change_type="removed",
                diff_type="structural_change",
                confidence="high",
            ))

    # Modified
    for fqn in sorted(old_map):
        if fqn not in new_map:
            continue
        old = old_map[fqn]
        new = new_map[fqn]
        if _symbol_fingerprint(old) == _symbol_fingerprint(new):
            continue

        # Classify the modification
        diff_type = "unknown"
        if set(old.annotations) != set(new.annotations):
            diff_type = "annotation_change"
        elif set(old.modifiers) != set(new.modifiers):
            diff_type = "structural_change"
        elif set(old.imports_used) != set(new.imports_used):
            # imports_used tracks types in signature — change means signature changed
            diff_type = "signature_change"

        changed.append(ChangedSymbol(
            symbol=fqn,
            change_type="modified",
            diff_type=diff_type,
            confidence="high",
        ))

    return changed


def _get_git_old_content(git_root: Path, rel_path: str, since: str) -> Optional[str]:
    """Fetch file content at git ref. Returns None if not available."""
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
# Phase 5 — IR assembly
# ---------------------------------------------------------------------------

def _symbol_to_dict(sym: SymbolRecord) -> dict:
    return {
        "symbol": sym.symbol,
        "type": sym.type,
        "modifiers": sym.modifiers,
        "annotations": sym.annotations,
        "imports_used": sym.imports_used,
        "declaring_file": sym.declaring_file,
        "confidence": sym.confidence,
    }


def _edge_to_dict(edge: RelationEdge) -> dict:
    return {
        "from": edge.from_symbol,
        "to": edge.to_symbol,
        "type": edge.type,
        "confidence": edge.confidence,
        "evidence": edge.evidence,
    }


def _changed_to_dict(cs: ChangedSymbol) -> dict:
    return {
        "symbol": cs.symbol,
        "change_type": cs.change_type,
        "diff_type": cs.diff_type,
        "confidence": cs.confidence,
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
        source:    Current file content.
        rel_path:  Relative path within the repo (used as declaring_file).
        old_source: Optional baseline content for symbol diff (Phase 4).

    Returns IR dict with: symbols, relations, changed_symbols, spring_summary,
    graph_metadata.
    """
    package, symbols, raw_imports = _extract_symbols(source, rel_path)
    relations = _build_relations(symbols, raw_imports, source, package, rel_path)
    spring_summary = _build_spring_summary(symbols)

    changed_symbols: list[ChangedSymbol] = []
    if old_source is not None:
        _, old_symbols, _ = _extract_symbols(old_source, rel_path)
        changed_symbols = _diff_symbols(old_symbols, symbols)

    return _assemble(symbols, relations, changed_symbols, spring_summary)


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

    Returns aggregated IR dict.
    """
    all_symbols: list[SymbolRecord] = []
    all_relations: list[RelationEdge] = []
    all_changed: list[ChangedSymbol] = []

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
        elif since:
            # File is new (not in baseline) — all symbols are added
            for sym in symbols:
                all_changed.append(ChangedSymbol(
                    symbol=sym.symbol,
                    change_type="added",
                    diff_type="structural_change",
                    confidence="high",
                ))

        all_symbols.extend(symbols)
        all_relations.extend(relations)

    # Aggregate Spring summary across all files
    spring_summary = _build_spring_summary(all_symbols)

    # Deduplicate relations
    seen: set[tuple[str, str, str]] = set()
    unique_relations: list[RelationEdge] = []
    for e in all_relations:
        key = (e.from_symbol, e.to_symbol, e.type)
        if key not in seen:
            seen.add(key)
            unique_relations.append(e)

    return _assemble(all_symbols, unique_relations, all_changed, spring_summary)


def _assemble(
    symbols: list[SymbolRecord],
    relations: list[RelationEdge],
    changed_symbols: list[ChangedSymbol],
    spring_summary: dict,
) -> dict:
    """Phase 5: Final IR assembly."""
    # Deterministic ordering
    sorted_symbols = sorted(symbols, key=lambda s: s.symbol)
    sorted_relations = sorted(relations, key=lambda e: (e.from_symbol, e.type, e.to_symbol))
    sorted_changed = sorted(changed_symbols, key=lambda c: c.symbol)

    call_edges = [e for e in sorted_relations if e.type == "calls"]

    return {
        "symbols": [_symbol_to_dict(s) for s in sorted_symbols],
        "relations": [_edge_to_dict(e) for e in sorted_relations],
        "changed_symbols": [_changed_to_dict(c) for c in sorted_changed],
        "spring_summary": spring_summary,
        "graph_metadata": {
            "node_count": len(sorted_symbols),
            "edge_count": len(sorted_relations),
            "has_call_graph": len(call_edges) > 0,
        },
    }


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
        # Exclude test directories (heuristic)
        if "/test/" in rel or "/tests/" in rel or rel.startswith("test/"):
            continue
        results.append(rel)
    return results

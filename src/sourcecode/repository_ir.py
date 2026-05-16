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
    r'(?:(?:void|boolean|byte|char|short|int|long|float|double|String|[\w.<>\[\]?,]+)\s+)'
    r'(?P<name>[a-z_]\w*)\s*\(',
)

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
    "structural_change": 0.6,
    "annotation_change": 0.6,
    "unknown": 0.1,
}

_PROPAGATION_DECAY: float = 0.5
_BFS_MAX_DEPTH: int = 3


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
            ann_m = _ANN_RE.match(stripped)
            if ann_m:
                ann = ann_m.group(1)
                if ann not in pending_anns:
                    pending_anns.append(ann)
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

        if class_stack:
            mth_m = _METHOD_DECL_RE.match(stripped)
            if mth_m:
                mname = mth_m.group("name")
                if mname not in _JAVA_KEYWORDS:
                    class_fqn = class_stack[-1][0]
                    fqn = f"{class_fqn}#{mname}"
                    modifiers = _parse_modifier_str(mth_m.group("modifiers") or "")
                    used = _resolve_types_from_text(stripped, import_map)
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

        pending_anns = []
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
    return f"{sym.type}|{','.join(sym.modifiers)}|{','.join(sym.annotations)}|{','.join(sym.imports_used)}"


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
        if set(old.annotations) != set(new.annotations):
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


def _propagate_impact(
    changed_fqns: set[str],
    changed_scores: dict[str, float],
    adjacency: dict[str, set[str]],
    all_fqns: set[str],
    max_depth: int = _BFS_MAX_DEPTH,
) -> list[dict]:
    """BFS impact propagation with decay per hop. No path → no impact."""
    impacted: dict[str, dict] = {}
    # (node, via, depth, score)
    queue: deque[tuple[str, str, int, float]] = deque()

    for fqn in sorted(changed_fqns):
        base = changed_scores.get(fqn, 0.0)
        for neighbor in sorted(adjacency.get(fqn, set())):
            if neighbor not in changed_fqns and neighbor in all_fqns:
                score = round(base * _PROPAGATION_DECAY, 4)
                if score > 0:
                    queue.append((neighbor, fqn, 1, score))

    while queue:
        node, via, depth, score = queue.popleft()
        existing = impacted.get(node)
        if existing and existing["impact_score"] >= score:
            continue
        impacted[node] = {"entity": node, "depth": depth, "impact_score": score, "via": via}
        if depth < max_depth:
            for neighbor in sorted(adjacency.get(node, set())):
                if neighbor not in changed_fqns and neighbor in all_fqns:
                    next_score = round(score * _PROPAGATION_DECAY, 4)
                    if next_score > 0:
                        queue.append((neighbor, node, depth + 1, next_score))

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

    # --- Impact propagation (BFS, graph-only) ---
    changed_with_graph = {e["entity"] for e in changed_entities_out}
    changed_scores_map = {fqn: node_scores.get(fqn, 0.0) for fqn in changed_with_graph}
    impacted_entities_out = _propagate_impact(
        changed_with_graph, changed_scores_map, adjacency, all_fqns_set
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
            "type": s.type,
            "role": spring_role_map.get(s.symbol, "other"),
            "in_degree": in_deg.get(s.symbol, 0),
            "out_degree": out_deg.get(s.symbol, 0),
        }
        for s in sorted_syms
    ]
    graph_edges = [_edge_to_dict(e) for e in sorted_rels]

    return {
        "schema_version": "final-v1",
        "graph": {
            "nodes": graph_nodes,
            "edges": graph_edges,
        },
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

    Returns aggregated deterministic IR dict (schema_version=final-v1).
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

    return _assemble(all_symbols, unique_relations, all_changed, spring_summary)


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

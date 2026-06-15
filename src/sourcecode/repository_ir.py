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

import random
import re
import subprocess
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from sourcecode.fqn_utils import normalize_owner_fqn as _normalize_owner_fqn
from sourcecode.path_filters import is_test_path as _is_test_path
from sourcecode.security_config import (
    CustomSecuritySpec,
    capture_markers as _capture_markers,
    load_custom_security as _load_custom_security,
)

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
_ANN_WITH_ARGS_RE = re.compile(
    r'^(@[\w.]+)\s*'
    r'(?:\('
    r'((?:[^()"\']*|"[^"]*"|\'[^\']*\'|\((?:[^()"\']*|"[^"]*"|\'[^\']*\')*\))*)'
    r'\))?'
)

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

# Regex to collect static final String constants for annotation constant-folding (P1 fix).
_STATIC_FINAL_STR_RE = re.compile(
    r'(?:public|protected|private)?\s*static\s+final\s+String\s+(\w+)\s*=\s*"([^"]*)"'
)

_ENDPOINT_ANNOTATIONS: frozenset[str] = frozenset({
    # Spring MVC
    "@GetMapping", "@PostMapping", "@PutMapping", "@DeleteMapping",
    "@PatchMapping", "@RequestMapping",
    # JAX-RS / Jakarta REST
    "@GET", "@POST", "@PUT", "@DELETE", "@PATCH", "@HEAD", "@OPTIONS",
})

# JAX-RS HTTP verb annotations (subset of _ENDPOINT_ANNOTATIONS; path lives in @Path, not here).
_JAXRS_HTTP_ANNOTATIONS: frozenset[str] = frozenset({
    "@GET", "@POST", "@PUT", "@DELETE", "@PATCH", "@HEAD", "@OPTIONS",
})

# Annotations whose args contain a path value and must be captured.
_PATH_ANNOTATIONS: frozenset[str] = frozenset({"@Path"})

# Security / authorization annotations whose args must be captured.
# Includes standard Jakarta EE, JAX-RS, Quarkus/MicroProfile, and custom patterns.
_PERMISSION_ANNOTATIONS: frozenset[str] = frozenset({
    # Jakarta EE / JAX-RS standard
    "@RolesAllowed",
    "@PermitAll",
    "@DenyAll",
    # Quarkus / MicroProfile
    "@Authenticated",
    "@AuthenticatedWithRoles",
    # Spring Security
    "@PreAuthorize",
    "@PostAuthorize",
    "@Secured",
    "@RequiresRoles",
    "@RequiresPermissions",
    # OpenAPI
    "@SecurityRequirement",
    # Jakarta Servlet
    "@ServletSecurity",
})

# Marker-only security annotations that carry no args but still signal access policy.
_SECURITY_MARKER_ANNOTATIONS: frozenset[str] = frozenset({
    "@PermitAll", "@DenyAll", "@Authenticated",
})

# Annotations on config classes that indicate a centralized security filter chain
# (Spring Security / Spring Boot).  When present, per-endpoint no_security_signal
# is expected and does NOT mean endpoints are unprotected.
_FILTER_SECURITY_ANNOTATIONS: frozenset[str] = frozenset({
    "@EnableWebSecurity",
    # @EnableMethodSecurity / @EnableGlobalMethodSecurity enable per-method annotation
    # security (@PreAuthorize/@Secured), NOT a filter chain — must NOT be treated as
    # filter_based or SEC-001 is suppressed for every unannotated endpoint.
})

# Programmatic security: method-call patterns that indicate runtime auth enforcement.
# Requires method-call or field-access context — bare class name mentions (imports,
# type declarations) must NOT match or IAM/auth-domain repos generate false positives.
_PROGRAMMATIC_SECURITY_RE = re.compile(
    r"\b(?:hasRole|hasAuthority|isAuthenticated|requirePermission|checkPermission"
    r"|assertAuthorized|authenticate)\s*\("
    r"|SecurityContextHolder\."
    r"|\.(?:getAuthentication|getSecurityContext|getPrincipal|isAuthorized|checkAccess)\s*\("
    r"|throw\s+new\s+(?:AccessDeniedException|UnauthorizedException|ForbiddenException|AuthenticationException)\b",
    re.MULTILINE,
)


def _has_programmatic_security(source: str) -> bool:
    return bool(_PROGRAMMATIC_SECURITY_RE.search(source))


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

# Lombok annotations that generate constructors injecting fields
_LOMBOK_CTOR_ANNOTATIONS: frozenset[str] = frozenset({
    "@RequiredArgsConstructor",  # injects private final fields
    "@AllArgsConstructor",       # injects all non-static fields
})

# Transaction annotations whose args must be captured for semantic analysis.
_TX_ANNOTATIONS: frozenset[str] = frozenset({"@Transactional", "@TransactionalEventListener"})

# Combined set used in _extract_symbols annotation-value capture.
_CAPTURE_ANN_ARGS: frozenset[str] = (
    _ENDPOINT_ANNOTATIONS | _PERMISSION_ANNOTATIONS | _PATH_ANNOTATIONS | _TX_ANNOTATIONS
)

_JAVA_ROLE_MAP: dict[str, str] = {
    # Spring MVC / Spring Boot
    "@RestController": "controller",
    "@Controller": "controller",
    "@Service": "service",
    "@Repository": "repository",
    "@Component": "component",
    "@Configuration": "config",
    "@Bean": "config",
    # JPA / Hibernate
    "@Entity": "entity",
    "@MappedSuperclass": "entity",
    "@Embeddable": "entity",
    "@Table": "entity",
    # CDI / Jakarta EE
    "@ApplicationScoped": "service",
    "@RequestScoped": "service",
    "@SessionScoped": "service",
    "@ConversationScoped": "service",
    "@Singleton": "service",
    "@Dependent": "component",
    "@Named": "component",
    "@Produces": "component",
    "@Stateless": "service",
    "@Stateful": "service",
    "@MessageDriven": "service",
    # JAX-RS
    "@Provider": "provider",
    "@Consumes": "controller",
    # Quarkus
    "@QuarkusMain": "entrypoint",
    "@QuarkusTest": "test",
    "@QuarkusIntegrationTest": "test",
    "@RegisterForReflection": "component",
    # Spring Security / AOP
    "@Aspect": "config",
    "@EnableWebSecurity": "config",
    "@EnableMethodSecurity": "config",
}

# Backward-compatible alias — external callers may reference this name.
_SPRING_ROLE_MAP = _JAVA_ROLE_MAP

# Keycloak/Quarkus SPI interface names — classes implementing these are spi_provider entry points.
_SPI_ROLE_INTERFACES: frozenset[str] = frozenset({
    "EventListenerProvider", "EventListenerProviderFactory",
    "RealmResourceProvider", "RealmResourceProviderFactory",
    "AuthenticatorFactory", "Authenticator",
    "ProtocolMapper", "ProtocolMapperFactory",
    "CredentialProvider", "CredentialProviderFactory",
    "PolicyProviderFactory", "PolicyProvider",
    "RequiredActionProvider", "RequiredActionFactory",
    "IdentityProviderMapper", "IdentityProviderFactory",
})

_SPRING_OTHER: frozenset[str] = frozenset({
    # Spring
    "@Transactional", "@RequestMapping", "@GetMapping", "@PostMapping",
    "@PutMapping", "@DeleteMapping", "@PatchMapping", "@Autowired",
    "@Inject", "@Value", "@Qualifier", "@EnableWebSecurity",
    "@SpringBootApplication", "@EnableAutoConfiguration",
    "@EventListener", "@TransactionalEventListener",
    "@KafkaListener", "@RabbitListener",
    "@Async", "@Scheduled", "@Cacheable", "@CacheEvict",
    # CDI / Jakarta EE
    "@ApplicationScoped", "@RequestScoped", "@SessionScoped", "@Dependent",
    "@Named", "@Produces", "@Consumes",
    # JAX-RS (non-HTTP-verb)
    "@Path", "@PathParam", "@QueryParam", "@FormParam", "@HeaderParam",
    "@MatrixParam", "@CookieParam", "@Context",
})

_PUBLISH_EVENT_RE = re.compile(r'\.publishEvent\s*\(\s*new\s+(\w+)\s*[(\{]')

# Two-step publish: SomeEvent var = new SomeEvent(...); publisher.publishEvent(var)
# Used when event is created before passing to publishEvent (common pattern).
_PUBLISH_EVENT_CALL_RE = re.compile(r'\.publishEvent\s*\(')
_NEW_EVENT_INSTANTIATION_RE = re.compile(r'\bnew\s+(\w+Event)\s*[\({]')

# Keycloak SPI event fire pattern: XxxEvent.fire(session, ...)
_FIRE_EVENT_RE = re.compile(r'\b(\w+Event)\.fire\s*\(')

# Class-level consumer detection from class signature (not annotations).
# Pattern 1: implements [Prefix]ApplicationListener<EventType>
#            Matches both the standard Spring interface (ApplicationListener<E>) and
#            framework-specific subinterfaces (BroadleafApplicationListener<E>,
#            SmartApplicationListener<E>, etc.).  Uses \w* prefix instead of \b so
#            that "Broadleaf" prefix does not break the word boundary. (BUG-EVT-001)
_APP_LISTENER_RE = re.compile(r'\w*ApplicationListener\s*<\s*(\w+)\s*>')
# Pattern 2: extends AbstractXxxEventListener<EventType> — abstract base class pattern
#            (Broadleaf's AbstractBroadleafApplicationEventListener and similar).
#            Matches any parent class name that contains "EventListener".
_ABSTRACT_LISTENER_RE = re.compile(r'\bextends\s+\w+EventListener\w*\s*<\s*(\w+)\s*>')

# Block comment stripper — removes /* ... */ (including Javadoc) to prevent
# _PUBLISH_EVENT_RE / _FIRE_EVENT_RE from matching example code in comments.
_BLOCK_COMMENT_RE = re.compile(r'/\*.*?\*/', re.DOTALL)
_LINE_COMMENT_RE = re.compile(r'//[^\n]*')


def _strip_java_comments(source: str) -> str:
    """Remove // line comments and /* */ block comments from Java source."""
    source = _BLOCK_COMMENT_RE.sub(' ', source)
    source = _LINE_COMMENT_RE.sub(' ', source)
    return source


def _parse_annotation_line(line: str) -> tuple[str, str]:
    """Parse annotation name and args from a line starting with '@'.

    Returns (ann_name, ann_args) where ann_args is content inside the outermost ().
    Uses O(n) character scanning instead of regex to avoid catastrophic backtracking
    on lines with deeply nested annotation arguments (e.g. @APIResponse with @Content
    containing @Schema — 3-level nesting that breaks _ANN_WITH_ARGS_RE).
    """
    if not line.startswith('@'):
        return "", ""
    i = 1
    while i < len(line) and (line[i].isalnum() or line[i] in ('_', '.')):
        i += 1
    ann_name = line[:i]
    while i < len(line) and line[i] in (' ', '\t'):
        i += 1
    if i >= len(line) or line[i] != '(':
        return ann_name, ""
    depth = 0
    in_string = False
    string_char = ''
    start = i + 1
    i += 1
    while i < len(line):
        c = line[i]
        if in_string:
            if c == '\\':
                i += 2
                continue
            if c == string_char:
                in_string = False
        elif c in ('"', "'"):
            in_string = True
            string_char = c
        elif c == '(':
            depth += 1
        elif c == ')':
            if depth == 0:
                return ann_name, line[start:i]
            depth -= 1
        i += 1
    return ann_name, line[start:]

# Edge types used for subsystem grouping — semantic hierarchy only, not imports
_SUBSYSTEM_STRUCTURAL_EDGES: frozenset[str] = frozenset({
    "extends", "implements", "injects", "contained_in",
})

_HTTP_METHOD_MAP: dict[str, str] = {
    # Spring MVC
    "@GetMapping": "GET",
    "@PostMapping": "POST",
    "@PutMapping": "PUT",
    "@DeleteMapping": "DELETE",
    "@PatchMapping": "PATCH",
    # JAX-RS
    "@GET": "GET",
    "@POST": "POST",
    "@PUT": "PUT",
    "@DELETE": "DELETE",
    "@PATCH": "PATCH",
    "@HEAD": "HEAD",
    "@OPTIONS": "OPTIONS",
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

# Used by _count_net_braces fast path: strip string/char literals before counting braces.
# Handles escape sequences (\\) so escaped quotes don't close the literal prematurely.
_STRING_LITERAL_RE = re.compile(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'')

# Module-level cache for class-keyword detection (avoids recompilation per _extract_symbols call)
_CLASS_KW_RE = re.compile(r'\b(?:class|interface|enum)\s+[A-Z]')


# ---------------------------------------------------------------------------
# Stable ID helpers
# ---------------------------------------------------------------------------

_FINAL_STRIP_RE = re.compile(r'\bfinal\s+')
_TYPE_PARAM_RE = re.compile(r'^([\w<>\[\].,? ]+?)\s+\w+$')


def _normalize_type_name(raw: str) -> str:
    """Strip annotations, final modifier, and param name; return only type."""
    raw = _ANN_PREFIX_RE.sub("", raw).strip()
    raw = _FINAL_STRIP_RE.sub("", raw).strip()
    m = _TYPE_PARAM_RE.match(raw)
    if m:
        return m.group(1).strip()
    return raw.strip()


def _split_supertype_list(raw: str) -> list[str]:
    """Split an ``extends``/``implements`` clause into individual base type names.

    Handles multiple supertypes (interfaces may extend several) and strips generic
    type arguments *before* splitting so that commas inside ``<...>`` do not corrupt
    the result.  e.g. ``"VetRepository, Repository<Vet, Integer>"`` → ``["VetRepository",
    "Repository"]``.
    """
    if not raw or not raw.strip():
        return []
    # Iteratively remove (possibly nested) generic parameters so any commas they
    # contain are gone before we split on the top-level commas.
    prev = None
    stripped = raw
    while prev != stripped:
        prev = stripped
        stripped = re.sub(r'<[^<>]*>', '', stripped)
    bases: list[str] = []
    for piece in stripped.split(","):
        base = re.sub(r'<.*', '', piece).strip()
        if base:
            bases.append(base)
    return bases


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
    # Fast exit: no braces on this line at all
    if '{' not in line and '}' not in line:
        return 0
    # Fast path: no string/char literals — count directly (C-speed)
    if '"' not in line and "'" not in line:
        return line.count('{') - line.count('}')
    # Slow path: strip string/char literals first so quoted braces don't count
    clean = _STRING_LITERAL_RE.sub('', line)
    return clean.count('{') - clean.count('}')


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

def _extract_symbols(
    source: str,
    rel_path: str,
    *,
    extra_capture: "frozenset[str]" = frozenset(),
) -> tuple[str, list[SymbolRecord], list[str]]:
    """Phase 1: Extract symbols from a Java source file.

    extra_capture: extra annotation tokens (e.g. custom security annotations like
    "@M3FiltroSeguridad") whose argument lists must be stored in annotation_values
    even though they are not in the built-in _CAPTURE_ANN_ARGS set.

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

    # P1 fix: pre-scan all static final String constants for annotation constant-folding.
    # This enables resolution of @RequestMapping("/" + SECTION_KEY) into "/category".
    _file_constants = _collect_file_constants(source)

    symbols: list[SymbolRecord] = []
    depth = 0
    class_stack: list[tuple[str, int]] = []
    pending_anns: list[str] = []
    pending_ann_values: dict[str, str] = {}
    in_block_comment = False

    # BUG-PARSER-001: normalize multi-line class declarations where the opening brace
    # appears on a continuation line (e.g. "implements A,\n  B, C {").
    # _CLASS_DECL_RE requires '{' on the same line as 'class' — joining the continuation
    # here makes the regex work without changing the per-line brace-depth counter.
    _raw_lines = source.splitlines()
    _joined: list[str] = []
    _i = 0
    while _i < len(_raw_lines):
        _line = _raw_lines[_i]
        _stripped = _line.strip()
        if (_CLASS_KW_RE.search(_stripped) and '{' not in _stripped
                and not _stripped.startswith('//')
                and not _stripped.startswith('*')):
            # Continuation: join until we hit a line containing '{'
            _buf = _line
            _i += 1
            while _i < len(_raw_lines):
                _cont = _raw_lines[_i]
                _buf = _buf.rstrip() + ' ' + _cont.strip()
                _i += 1
                if '{' in _cont:
                    break
            _joined.append(_buf)
        else:
            _joined.append(_line)
            _i += 1

    # P1 fix: normalize multiline annotations (e.g. @RequestMapping(\n  value="..."\n))
    # into single lines so the per-line regex can capture annotation args correctly.
    _normalized_lines = _normalize_multiline_annotations(_joined)

    for line in _normalized_lines:
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
            ann, ann_args = _parse_annotation_line(stripped)
            if ann:
                if ann not in pending_anns:
                    pending_anns.append(ann)
                if ann_args and (ann in _CAPTURE_ANN_ARGS or ann in extra_capture):
                    # P1 fix: attempt to resolve constant expressions before storing.
                    # Transforms '"/" + SECTION_KEY' → '"/category"' when constant
                    # is defined in this file. Falls back to original if unresolvable.
                    resolved_args = _resolve_ann_path_expr(ann_args.strip(), _file_constants)
                    pending_ann_values[ann] = resolved_args
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
# Phase 2 — Java/Jakarta/CDI/JAX-RS semantic tagging
# ---------------------------------------------------------------------------

def _java_role(annotations: list[str]) -> str:
    """Return the architecture role for a class based on its annotations.

    Covers Spring MVC, CDI/Jakarta EE, JAX-RS, and Quarkus patterns.
    Returns 'unknown' when no recognized annotation is present.
    """
    for ann in annotations:
        role = _JAVA_ROLE_MAP.get(ann)
        if role:
            return role
    return "unknown"


# Name-suffix patterns for role inference when annotations are absent.
# Ordered: more specific patterns first.
_JAVA_NAME_ROLE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(?:Controller|Resource|Endpoint|Handler|Servlet|Filter|Action)$"), "controller"),
    (re.compile(r"(?:ServiceImpl|ServiceBean|ServiceFacade|Facade)$"), "service"),
    (re.compile(r"(?:Service|Manager|Processor|Coordinator|Orchestrator|UseCase|Interactor)$"), "service"),
    (re.compile(r"(?:RepositoryImpl|DaoImpl|DAOImpl)$"), "repository"),
    (re.compile(r"(?:Repository|Dao|DAO|Store|Persistence|JpaRepository|CrudRepository)$"), "repository"),
    (re.compile(r"(?:Entity|Model|Domain|Vo|ValueObject|Record)$"), "entity"),
    (re.compile(r"(?:Config|Configuration|Configurer|AutoConfiguration|Properties|Settings)$"), "config"),
    (re.compile(r"(?:Factory|Builder|Provider|Supplier|Creator|Generator)$"), "provider"),
    (re.compile(r"(?:Listener|Observer|Handler|EventHandler|MessageListener|Consumer)$"), "component"),
    (re.compile(r"(?:Util|Utils|Helper|Helpers|Converter|Transformer|Mapper|Adapter)$"), "component"),
    (re.compile(r"(?:Exception|Error)$"), "other"),
    (re.compile(r"(?:Test|Tests|Spec|IT|IntegrationTest)$"), "test"),
]


def _java_role_from_name(simple_name: str) -> str:
    """Infer role from Java class simple name when annotations don't classify it.

    Returns 'other' (never 'unknown') — callers use 'unknown' to mean
    'not classified at all'; 'other' means 'classified but no interesting role'.
    """
    for pattern, role in _JAVA_NAME_ROLE_PATTERNS:
        if pattern.search(simple_name):
            return role
    return "other"


# Backward-compatible alias used by external callers and serializer.
_spring_role = _java_role


def _build_spring_summary(symbols: list[SymbolRecord]) -> dict:
    """Phase 2: Aggregate Java/CDI/JAX-RS/Spring annotated symbols into a summary."""
    controllers: list[str] = []
    services: list[str] = []
    repositories: list[str] = []
    configs: list[str] = []
    transactional: list[str] = []
    providers: list[str] = []
    spi_impls: list[str] = []

    for sym in symbols:
        if sym.type not in ("class", "interface"):
            if "@Transactional" in sym.annotations:
                transactional.append(sym.symbol)
            continue

        role = _java_role(sym.annotations)
        if role == "controller":
            controllers.append(sym.symbol)
        elif role == "service":
            services.append(sym.symbol)
        elif role == "repository":
            repositories.append(sym.symbol)
        elif role == "config":
            configs.append(sym.symbol)
        elif role == "provider":
            providers.append(sym.symbol)

        # JAX-RS class-level @Path → resource controller (no Spring annotation required).
        if role == "unknown" and "@Path" in sym.annotations:
            controllers.append(sym.symbol)

        # Keycloak/Quarkus SPI: class signature mentions a known SPI interface.
        sig = sym.signature or ""
        if any(iface in sig for iface in _SPI_ROLE_INTERFACES):
            spi_impls.append(sym.symbol)

        if "@Transactional" in sym.annotations:
            transactional.append(sym.symbol)

    result: dict = {
        "controllers": sorted(set(controllers)),
        "services": sorted(services),
        "repositories": sorted(repositories),
        "configs": sorted(configs),
        "transactional": sorted(transactional),
    }
    if providers:
        result["providers"] = sorted(providers)
    if spi_impls:
        result["spi_implementations"] = sorted(spi_impls)
    return result


def _extract_mapped_paths(source: str, class_fqn: str) -> dict[str, str]:
    paths: dict[str, str] = {}
    for m in _REQUEST_MAPPING_RE.finditer(source):
        paths[m.group(1)] = class_fqn
    return paths


# ---------------------------------------------------------------------------
# Phase 3 — Symbol relation graph
# ---------------------------------------------------------------------------

def _build_same_package_map(symbols: list[SymbolRecord]) -> dict[str, dict[str, str]]:
    """Build {package: {simple_name: FQN}} map from all class/interface symbols.

    Used by build_repo_ir to resolve same-package types that need no explicit import.
    In Java, classes in the same package reference each other without import statements,
    so import_map is empty for them — this map provides the fallback resolution.
    """
    result: dict[str, dict[str, str]] = {}
    for sym in symbols:
        if sym.type not in ("class", "interface") or "#" in sym.symbol:
            continue
        pkg = sym.symbol.rsplit(".", 1)[0] if "." in sym.symbol else ""
        simple = sym.symbol.split(".")[-1]
        result.setdefault(pkg, {})[simple] = sym.symbol
    return result


def _build_relations(
    symbols: list[SymbolRecord],
    raw_imports: list[str],
    source: str,
    package: str,
    rel_path: str,
    same_pkg_types: dict[str, str] | None = None,
) -> list[RelationEdge]:
    """Phase 3: Build directed relation graph for symbols in one file.

    same_pkg_types: {simple_name → FQN} for classes in the same package.
    Passed by build_repo_ir after a first pass that collects all symbols.
    Enables resolving injection targets that share a package with the caller
    and therefore need no explicit Java import statement.
    """
    edges: list[RelationEdge] = []
    _same_pkg: dict[str, str] = same_pkg_types or {}

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
            _inject_ann = next(
                (a for a in sym.annotations if a in _INJECT_ANNOTATIONS), "@Autowired"
            )
            _field_targets: set[str] = set(sym.imports_used)
            # Same-package field injection: imports_used is empty when the field type
            # shares a package with the declaring class (no import needed in Java).
            # Extract type from signature ("Type name") and resolve via same_pkg_types.
            if not _field_targets and _same_pkg:
                _sig_type = (sym.signature or "").split()[0] if sym.signature else ""
                _sig_base = re.sub(r'<.*', '', _sig_type).strip()
                if _sig_base and _sig_base[0].isupper():
                    _same_fqn = _same_pkg.get(_sig_base)
                    if _same_fqn and _same_fqn != _enclosing_class(sym_fqn):
                        _field_targets.add(_same_fqn)
            for imp_fqn in _field_targets:
                edges.append(RelationEdge(
                    from_symbol=sym_fqn,
                    to_symbol=imp_fqn,
                    type="injects",
                    confidence="high",
                    evidence={"type": "annotation", "value": _inject_ann},
                ))

    # ── Constructor injection ─────────────────────────────────────────────────
    # Spring 4.3+ omits @Autowired when there is a single constructor.
    # Both annotated and bare constructors get injects edges from ClassName#<init>
    # to each resolvable parameter type so the reverse graph can propagate impact.
    for sym in symbols:
        if sym.symbol_kind != "constructor" or not sym.param_types:
            continue
        for simple_type in sym.param_types:
            base = re.sub(r'<.*', '', simple_type).strip()
            fqn = import_map.get(base) or _same_pkg.get(base)
            if fqn:
                edges.append(RelationEdge(
                    from_symbol=sym.symbol,
                    to_symbol=fqn,
                    type="injects",
                    confidence="high",
                    evidence={"type": "constructor_param", "value": simple_type},
                ))

    # ── Lombok constructor injection ──────────────────────────────────────────
    # @RequiredArgsConstructor: injects all private final fields.
    # @AllArgsConstructor: injects all non-static fields.
    # No explicit constructor symbol exists; edges are emitted from the class FQN.
    for sym in symbols:
        if sym.type not in ("class", "interface"):
            continue
        _has_req = "@RequiredArgsConstructor" in sym.annotations
        _has_all = "@AllArgsConstructor" in sym.annotations
        if not (_has_req or _has_all):
            continue
        _lombok_ann = "@RequiredArgsConstructor" if _has_req else "@AllArgsConstructor"
        for _line in source.splitlines():
            fld = _FIELD_DECL_RE.match(_line.strip())
            if not fld:
                continue
            _mods = _parse_modifier_str(fld.group("modifiers") or "")
            if "static" in _mods:
                continue
            if _has_req and "final" not in _mods:
                continue
            _ftype = fld.group("type").strip()
            _base = re.sub(r'<.*', '', _ftype).strip()
            _fqn = import_map.get(_base) or _same_pkg.get(_base)
            if _fqn:
                edges.append(RelationEdge(
                    from_symbol=sym.symbol,
                    to_symbol=_fqn,
                    type="injects",
                    confidence="medium",
                    evidence={"type": "lombok_constructor", "value": _lombok_ann},
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
            # An interface may extend multiple interfaces (e.g.
            # `extends VetRepository, Repository<Vet, Integer>`); split on top-level
            # commas so each base produces its own edge and the reverse graph sees
            # every supertype (not a single mangled token).
            for base in _split_supertype_list(extends_str):
                to = import_map.get(base, base)
                edges.append(RelationEdge(
                    from_symbol=class_fqn,
                    to_symbol=to,
                    type="extends",
                    confidence="high",
                    evidence={"type": "signature", "value": f"extends {extends_str}"},
                ))

        if implements_str:
            for base in _split_supertype_list(implements_str):
                to = import_map.get(base, base)
                edges.append(RelationEdge(
                    from_symbol=class_fqn,
                    to_symbol=to,
                    type="implements",
                    confidence="high",
                    evidence={"type": "signature", "value": f"implements {base}"},
                ))

    # mapped_to edges: controller class → class-level @RequestMapping path prefix.
    # O(N) scan of symbols — do NOT call _extract_mapped_paths(source) here because
    # _REQUEST_MAPPING_RE also matches method-level @GetMapping/@PostMapping, producing
    # O(N_methods) paths × O(N_syms) inner loop = O(N²) on files with many endpoints.
    for sym in symbols:
        if sym.type not in ("class", "interface"):
            continue
        if "@RestController" not in sym.annotations and "@Controller" not in sym.annotations:
            continue
        if "@RequestMapping" not in sym.annotations:
            continue
        _rm_args = sym.annotation_values.get("@RequestMapping", "")
        for _m_path in _parse_route_paths(_rm_args):
            if _m_path:
                edges.append(RelationEdge(
                    from_symbol=sym.symbol,
                    to_symbol=_m_path,
                    type="mapped_to",
                    confidence="high",
                    evidence={"type": "annotation", "value": f"@RequestMapping(\"{_m_path}\")"},
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

    # Event flow edges — listens_to_event and publishes_event.
    # Spring: method with @EventListener or @TransactionalEventListener → resolved event type(s).
    _LISTENER_ANNOTATIONS: frozenset[str] = frozenset({
        "@EventListener", "@TransactionalEventListener",
    })
    for sym in symbols:
        if sym.type == "method" and (sym.annotations and
                any(a in _LISTENER_ANNOTATIONS for a in sym.annotations)):
            ann = next(a for a in sym.annotations if a in _LISTENER_ANNOTATIONS)
            for imp_fqn in sym.imports_used:
                edges.append(RelationEdge(
                    from_symbol=sym.symbol,
                    to_symbol=imp_fqn,
                    type="listens_to_event",
                    confidence="high",
                    evidence={"type": "annotation", "value": ann},
                ))

    _class_syms = [s for s in symbols if s.type in ("class", "interface") and "#" not in s.symbol]

    # Strip comments before event scanning to prevent Javadoc examples from
    # generating false publisher edges (BUG-003).
    _source_no_comments = _strip_java_comments(source)

    # Spring: class that calls publishEvent(new XxxEvent(...)) → event type FQN.
    for m in _PUBLISH_EVENT_RE.finditer(_source_no_comments):
        event_simple = m.group(1)
        # BUG-004: try import_map first, then same-package map, then keep simple name.
        event_fqn = import_map.get(event_simple) or _same_pkg.get(event_simple) or event_simple
        for cls_sym in _class_syms:
            edges.append(RelationEdge(
                from_symbol=cls_sym.symbol,
                to_symbol=event_fqn,
                type="publishes_event",
                confidence="medium",
                evidence={"type": "method_call", "value": f"publishEvent(new {event_simple})"},
            ))

    # Two-step publish: `SomeEvent var = new SomeEvent(...); publisher.publishEvent(var)`.
    # The inline regex above only catches publishEvent(new Evt(...)).  Many Spring services
    # instantiate the event first and then pass the variable.  When the source contains
    # publishEvent( (any form) we also scan for new XxxEvent instantiations that the inline
    # regex would have missed (i.e. not already emitted), using confidence=low.
    if _PUBLISH_EVENT_CALL_RE.search(_source_no_comments):
        inline_matched: set[str] = {m.group(1) for m in _PUBLISH_EVENT_RE.finditer(_source_no_comments)}
        for m in _NEW_EVENT_INSTANTIATION_RE.finditer(_source_no_comments):
            event_simple = m.group(1)
            if event_simple in inline_matched:
                continue  # already captured by inline regex at higher confidence
            event_fqn = import_map.get(event_simple) or _same_pkg.get(event_simple) or event_simple
            for cls_sym in _class_syms:
                edges.append(RelationEdge(
                    from_symbol=cls_sym.symbol,
                    to_symbol=event_fqn,
                    type="publishes_event",
                    confidence="low",
                    evidence={"type": "method_call", "value": f"publishEvent(var) + new {event_simple}"},
                ))

    # Keycloak SPI: XxxEvent.fire(...) static dispatch → publishes_event.
    for m in _FIRE_EVENT_RE.finditer(_source_no_comments):
        event_simple = m.group(1)
        event_fqn = import_map.get(event_simple) or _same_pkg.get(event_simple) or event_simple
        for cls_sym in _class_syms:
            edges.append(RelationEdge(
                from_symbol=cls_sym.symbol,
                to_symbol=event_fqn,
                type="publishes_event",
                confidence="medium",
                evidence={"type": "method_call", "value": f"{event_simple}.fire(...)"},
            ))

    # Keycloak SPI: class implementing EventListenerProvider → listens_to_event.
    _ELP_IFACE = "EventListenerProvider"
    for sym in symbols:
        if sym.type == "class" and _ELP_IFACE in (sym.signature or ""):
            event_fqn = import_map.get("Event", "org.keycloak.events.Event")
            edges.append(RelationEdge(
                from_symbol=sym.symbol,
                to_symbol=event_fqn,
                type="listens_to_event",
                confidence="high",
                evidence={"type": "signature", "value": f"implements {_ELP_IFACE}"},
            ))

    # Class-level consumer detection via class signature (EVT-003 / EVT-004).
    # Pattern A: class Foo implements ApplicationListener<XxxEvent>
    #            → standard Spring interface, event type = generic param.
    # Pattern B: class Foo extends AbstractXxxEventListener<XxxEvent>
    #            → abstract base class pattern (Broadleaf and similar frameworks),
    #              event type = generic param of the parent class.
    for sym in _class_syms:
        sig = sym.signature or ""
        for pattern, ev_label in (
            (_APP_LISTENER_RE, "implements ApplicationListener"),
            (_ABSTRACT_LISTENER_RE, "extends *EventListener"),
        ):
            m = pattern.search(sig)
            if m:
                event_simple = m.group(1)
                event_fqn = (
                    import_map.get(event_simple)
                    or _same_pkg.get(event_simple)
                    or event_simple
                )
                edges.append(RelationEdge(
                    from_symbol=sym.symbol,
                    to_symbol=event_fqn,
                    type="listens_to_event",
                    confidence="high",
                    evidence={"type": "signature", "value": f"{ev_label}<{event_simple}>"},
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

# Return types that are never JAX-RS resource classes
_LOCATOR_SKIP_RETURN_TYPES: frozenset[str] = frozenset({
    "void", "Void", "Object", "Response", "String", "List", "Map", "Set",
    "Collection", "Optional", "CompletionStage", "Future", "Mono", "Flux",
    "Builder", "UriBuilder", "Link", "URI", "URL",
    "javax.ws.rs.core.Response", "jakarta.ws.rs.core.Response",
})


def _join_path_segments(*segments: str) -> str:
    """Join JAX-RS path segments with slash normalization.

    >>> _join_path_segments("/admin", "realms", "{realm}", "attack-detection")
    '/admin/realms/{realm}/attack-detection'
    >>> _join_path_segments("/admin", "", "")
    '/admin'
    """
    parts = [s.strip("/") for s in segments if s and s.strip("/")]
    return ("/" + "/".join(parts)) if parts else "/"


def _build_jaxrs_locator_map(symbols: list["SymbolRecord"]) -> dict[str, list[tuple[str, str]]]:
    """Detect JAX-RS sub-resource locator methods and build child → [(parent, path)] map.

    A sub-resource locator is a method that:
    - Has @Path annotation
    - Has NO HTTP verb annotation (@GET, @POST, @PUT, @DELETE, @PATCH, @HEAD, @OPTIONS)
    - Returns a non-trivial resource class type

    Returns: child_class_simple → [(parent_class_simple, locator_path_segment), ...]
    """
    locator_map: dict[str, list[tuple[str, str]]] = {}
    for sym in symbols:
        if sym.type != "method" and sym.symbol_kind not in ("method",):
            continue
        if "@Path" not in sym.annotations:
            continue
        # Exclude endpoint methods (have HTTP verb)
        if any(a in sym.annotations for a in _JAXRS_HTTP_ANNOTATIONS):
            continue
        # Exclude Spring endpoints
        if any(a in sym.annotations for a in _ENDPOINT_ANNOTATIONS - _JAXRS_HTTP_ANNOTATIONS):
            continue

        locator_path = _parse_route_path(sym.annotation_values.get("@Path", ""))

        ret = sym.return_type.strip()
        if not ret:
            continue
        # Simplify: strip package prefix and generics
        ret_simple = ret.split(".")[-1].split("<")[0].strip()
        if not ret_simple or ret_simple in _LOCATOR_SKIP_RETURN_TYPES:
            continue
        if ret_simple[0].islower():  # lowercase → primitive/variable, not a class
            continue

        parent_fqn = _enclosing_class(sym.symbol)
        parent_simple = parent_fqn.split(".")[-1]

        locator_map.setdefault(ret_simple, []).append((parent_simple, locator_path))

    return locator_map


def _resolve_jaxrs_prefixes(
    cls_simple: str,
    class_info: dict[str, dict],
    locator_map: dict[str, list[tuple[str, str]]],
    visited: frozenset,
) -> list[str]:
    """Recursively compute full path prefixes by walking up the JAX-RS locator chain.

    For a class with no class-level @Path, its effective prefix(es) come entirely
    from the locator chain: parent_prefix + locator_method_path + own_class_path.

    Cycle guard: visited prevents infinite recursion in malformed hierarchies.
    """
    if cls_simple in visited:
        return class_info.get(cls_simple, {}).get("prefixes", [""])

    own_prefixes = class_info.get(cls_simple, {}).get("prefixes", [""])

    if cls_simple not in locator_map:
        return own_prefixes

    new_visited = visited | {cls_simple}
    full_prefixes: list[str] = []

    for parent_simple, locator_path in locator_map[cls_simple]:
        parent_full = _resolve_jaxrs_prefixes(parent_simple, class_info, locator_map, new_visited)
        # Skip implementation/unrooted parents: if the parent resolves to only empty
        # prefixes AND has no class-level @Path annotation, it is a concrete impl class
        # (e.g. DefaultClientsApi implements ClientsApi) that duplicates a locator method
        # from its interface. Including it would produce spurious short paths like /{id}
        # alongside the correctly-resolved full path. The interface version is already
        # in the locator_map and will produce the correct full path.
        _parent_has_path_ann = class_info.get(parent_simple, {}).get("has_path_ann", False)
        _non_empty_parent = [p for p in parent_full if p]
        if not _non_empty_parent and not _parent_has_path_ann:
            continue
        use_parent_paths = _non_empty_parent if _non_empty_parent else parent_full
        for pp in use_parent_paths:
            for op in own_prefixes:
                combined = _join_path_segments(pp, locator_path, op)
                full_prefixes.append(combined)

    return full_prefixes if full_prefixes else own_prefixes


# ---------------------------------------------------------------------------
# P1 fix: static constant folding for Spring @RequestMapping annotations
# ---------------------------------------------------------------------------

def _collect_file_constants(source: str) -> dict[str, str]:
    """Pre-scan Java source for all static final String constants.

    Returns {simple_name: value} covering all classes in the file.
    Used by _resolve_ann_path_expr to fold constant references in @RequestMapping args.
    """
    # Fast path: skip entirely when no declarations present (C-speed string scan)
    if 'static final String' not in source:
        return {}
    # Scan only candidate lines (skips full-source regex over 100KB files).
    # Running _STATIC_FINAL_STR_RE over the whole source is O(source_size) due to
    # optional modifier group backtracking; per-line match is far cheaper.
    constants: dict[str, str] = {}
    for line in source.splitlines():
        if 'static' in line and 'final' in line and 'String' in line and '=' in line and '"' in line:
            m = _STATIC_FINAL_STR_RE.search(line)
            if m:
                constants[m.group(1)] = m.group(2)
    return constants


def _resolve_const_concat(expr: str, constants: dict[str, str]) -> Optional[str]:
    """Resolve a Java string expression that may use + concatenation and constant refs.

    Handles:
      "literal"                       → "literal"
      "/" + SECTION_KEY               → "/category"  (if SECTION_KEY in constants)
      ClassName.FIELD + "/users"      → "prefix/users"  (looks up FIELD in constants)
      "a" + UNKNOWN + "b"             → None  (unresolvable → caller keeps original)

    Returns the resolved string value or None if any part cannot be resolved.
    """
    expr = expr.strip()
    # Single string literal — extract content
    lm = re.match(r'^"([^"]*)"$', expr)
    if lm:
        return lm.group(1)

    # No concatenation operator → single constant reference
    if "+" not in expr:
        field = expr.split(".")[-1].strip()
        return constants.get(field)  # None if not in file constants

    # Split on + (handles ClassName.FIELD + "literal" + ... patterns)
    parts = re.split(r'\s*\+\s*', expr)
    resolved: list[str] = []
    for part in parts:
        part = part.strip()
        lm2 = re.match(r'^"([^"]*)"$', part)
        if lm2:
            resolved.append(lm2.group(1))
        else:
            field = part.split(".")[-1].strip()
            val = constants.get(field)
            if val is None:
                return None  # Unresolvable part — abort, preserve original
            resolved.append(val)
    return "".join(resolved)


def _resolve_ann_path_expr(ann_args: str, constants: dict[str, str]) -> str:
    """Try to resolve constant references inside annotation args.

    Transforms raw annotation arg string — e.g.:
      '"/" + AdminCategoryController.SECTION_KEY'
    into a form _parse_route_paths can extract:
      '"/category"'

    If resolution fails (cross-file or unparseable), returns ann_args unchanged
    so the existing literal-extraction fallback still runs.
    """
    ann_args = ann_args.strip()
    if not constants:
        return ann_args

    # Named parameter forms: value = expr  /  path = expr
    for key in ("value", "path"):
        km = re.match(
            rf'^\s*{key}\s*=\s*(.+?)(\s*,\s*(?:method|produces|consumes|params|headers|name)\b.*)?$',
            ann_args, re.DOTALL
        )
        if km:
            expr = km.group(1).strip().rstrip(",").strip()
            resolved = _resolve_const_concat(expr, constants)
            if resolved is not None:
                tail = km.group(2) or ""
                return f'{key} = "{resolved}"{tail}'
            return ann_args  # Can't resolve, keep original

    # Bare / positional expression: "lit" + CONST or just CONST
    resolved = _resolve_const_concat(ann_args, constants)
    if resolved is not None:
        return f'"{resolved}"'
    return ann_args


def _normalize_multiline_annotations(lines: list[str]) -> list[str]:
    """Merge multiline annotation spans into a single line.

    Handles annotations split across lines because their args span multiple lines:
      @RequestMapping(              ← opens paren, doesn't close
          value = "/add",
          method = RequestMethod.GET
      )

    Merges into: '@RequestMapping(value = "/add", method = RequestMethod.GET)'
    """
    result: list[str] = []
    buf: list[str] = []
    paren_depth = 0

    for line in lines:
        stripped = line.strip()
        if buf:
            # Continuation of a multiline annotation
            buf.append(stripped)
            paren_depth += stripped.count("(") - stripped.count(")")
            if paren_depth <= 0:
                result.append(" ".join(buf))
                buf = []
                paren_depth = 0
        elif stripped.startswith("@") and "(" in stripped:
            opens = stripped.count("(")
            closes = stripped.count(")")
            if opens > closes:
                # Unbalanced — start collecting continuation lines
                buf = [stripped]
                paren_depth = opens - closes
            else:
                result.append(line)
        else:
            result.append(line)

    # Flush any dangling buffer (shouldn't happen in well-formed code)
    if buf:
        result.extend(buf)
    return result


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


def _parse_route_paths(args_str: str) -> list[str]:
    """Return all route paths from annotation args, including array syntax.

    Handles:
      @RequestMapping("/single")                    → ["/single"]
      @RequestMapping({"/v1/foo", "/v1/bar"})       → ["/v1/foo", "/v1/bar"]
      @RequestMapping(value = "/single")            → ["/single"]
    """
    if not args_str:
        return [""]
    for key in ("value", "path"):
        m = re.search(rf'\b{key}\s*=\s*"([^"]*)"', args_str)
        if m:
            return [m.group(1)]
    paths = re.findall(r'"([^"]*)"', args_str)
    return paths if paths else [""]


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
    # JAX-RS @GET has no annotation args, so annotation_values may be empty.
    return any(a in _ENDPOINT_ANNOTATIONS for a in sym.annotations)


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
        if a in _ENDPOINT_ANNOTATIONS or a in _PATH_ANNOTATIONS
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

        # JAX-RS: HTTP verb carries no path; path lives in @Path annotation.
        if old_ann in _JAXRS_HTTP_ANNOTATIONS:
            old_args = old_sym.annotation_values.get("@Path", "")
        if new_ann in _JAXRS_HTTP_ANNOTATIONS:
            new_args = new_sym.annotation_values.get("@Path", "")

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
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
        pass
    return None


def _get_git_changed_files(root: "Path", since: str) -> "Optional[frozenset[str]]":
    """H-04: Return set of paths changed between `since` and HEAD, or None on failure.

    One `git diff --name-only` call replaces O(n) `git show` calls — only files
    in the returned set need old-content fetched for symbol diff computation.
    Returns None when git is unavailable or the ref cannot be resolved; the
    caller must fall back to the original per-file fetch in that case.
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", since, "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if result.returncode == 0:
            return frozenset(p.strip() for p in result.stdout.splitlines() if p.strip())
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


def _common_package_prefix(fqns: list[str]) -> str:
    """Longest common dot-separated package prefix across a list of FQNs."""
    if not fqns:
        return ""
    # Strip class/method suffix — keep only package parts (lowercase segments)
    def pkg_parts(fqn: str) -> list[str]:
        parts = fqn.split(".")
        # Drop trailing class/method names (PascalCase or after '#')
        result = []
        for p in parts:
            if "#" in p:
                break
            if p and p[0].isupper():
                break
            result.append(p)
        return result

    segs = [pkg_parts(f) for f in fqns if pkg_parts(f)]
    if not segs:
        return fqns[0].rsplit(".", 1)[0] if "." in fqns[0] else fqns[0]
    common = segs[0]
    for s in segs[1:]:
        new_common = []
        for a, b in zip(common, s):
            if a == b:
                new_common.append(a)
            else:
                break
        common = new_common
        if not common:
            break
    return ".".join(common) if common else fqns[0].rsplit(".", 1)[0]


def _subsystem_label(package_prefix: str) -> str:
    """Derive short human label enforcing minimum meaningful depth.

    For org.keycloak.services → "keycloak.services" (not "services" alone).
    Avoids single-segment labels like "org" or "keycloak" that convey nothing.
    """
    parts = [p for p in package_prefix.split(".") if p]
    _SKIP = {"com", "org", "net", "io", "java", "javax"}
    meaningful = [p for p in parts if p not in _SKIP]
    if not meaningful:
        return parts[-1] if parts else package_prefix
    # Use last two meaningful segments for disambiguation:
    # org.keycloak.services → ["keycloak", "services"] → "keycloak.services"
    if len(meaningful) >= 2:
        return f"{meaningful[-2]}.{meaningful[-1]}"
    return meaningful[-1]


def _canonical_subsystem_pkg(fqn: str) -> str:
    """Canonical subsystem package for a FQN — minimum depth 3 for org/com/net/io packages.

    org.keycloak.services.FooResource  → org.keycloak.services
    org.keycloak.FooClass              → org.keycloak
    com.example.util.Helper            → com.example.util
    SomeTopLevelClass                  → SomeTopLevelClass

    Never returns a bare TLD ("org", "com") — that collapses all classes into one subsystem.
    When only 1 lowercase segment exists before the class boundary, grabs one raw segment
    (even if uppercase) to force at least 2-segment grouping.
    """
    _TOP_LEVEL = {"com", "org", "net", "io", "java", "javax"}
    # Well-known framework namespaces that are not application boundaries;
    # go one level deeper (depth 5) so callers get the actual app module.
    _FRAMEWORK_NS = {"springframework", "apache", "eclipse", "google", "jetbrains"}
    parts: list[str] = []
    for segment in fqn.split("."):
        if "#" in segment or (segment and segment[0].isupper()):
            break
        parts.append(segment)
    if not parts:
        return fqn.rsplit(".", 1)[0] if "." in fqn else fqn
    if parts[0] in _TOP_LEVEL and len(parts) >= 3:
        if len(parts) >= 5 and len(parts) > 3 and parts[1] in _FRAMEWORK_NS:
            return ".".join(parts[:5])
        return ".".join(parts[:3])
    # Prevent bare TLD collapse: "org" or "com" alone as subsystem key is meaningless
    # and groups ALL classes under that TLD into a single giant component.
    # When only the TLD was collected before hitting a class boundary, grab the next
    # raw FQN segment (may be uppercase) to produce a 2-segment grouping key.
    if parts[0] in _TOP_LEVEL and len(parts) == 1:
        raw = fqn.split(".")
        if len(raw) >= 2:
            return f"{raw[0]}.{raw[1].split('#')[0]}"
    return ".".join(parts)


def _detect_subsystems(all_fqns: list[str], relations: list[RelationEdge]) -> list[dict]:  # noqa: ARG001
    """Group symbols by canonical subsystem package (minimum depth org.keycloak.<module>).

    Uses package-prefix grouping instead of Union-Find on relation edges.
    Union-Find on imports produced one giant component ("org") for large monorepos.
    Package-prefix grouping at depth 3 yields meaningful module-level subsystems.
    """
    components: dict[str, list[str]] = {}
    for fqn in all_fqns:
        pkg = _canonical_subsystem_pkg(fqn)
        components.setdefault(pkg, []).append(fqn)

    result: list[dict] = []
    for pkg_prefix, members in sorted(components.items()):
        # Skip bare class names (no package) and method references — these are
        # unpackaged utility classes (e.g. CopyDependencies) or IR artefacts.
        if "." not in pkg_prefix or "#" in pkg_prefix:
            continue
        members = sorted(members)
        label = _subsystem_label(pkg_prefix)
        short_names = [m.split(".")[-1].split("#")[0] for m in members[:3]]
        summary = ", ".join(short_names)
        if len(members) > 3:
            summary += f" ... ({len(members)} total)"
        result.append({
            "label": label,
            "package_prefix": pkg_prefix,
            "member_count": len(members),
            "members": members,
            "summary": summary,
        })
    return result


_EDGE_REASON_TEMPLATES: dict[str, str] = {
    "imports": "{from_sym} depends on {to_sym} (import)",
    "injects": "{from_sym} injects {to_sym}",
    "implements": "{from_sym} implements {to_sym}",
    "extends": "{from_sym} extends {to_sym}",
    "contained_in": "{from_sym} is a member of {to_sym}",
    "annotated_with": "{from_sym} is annotated with {to_sym}",
    "mapped_to": "Route {to_sym} depends on {from_sym}",
    "publishes_event": "{from_sym} publishes event {to_sym}",
    "listens_to_event": "{from_sym} listens for event {to_sym}",
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

def _compute_analysis_gaps(
    symbols: list[SymbolRecord],
    spring_summary: dict,
    route_surface: list[dict],
    relations: list[RelationEdge],
) -> list[dict]:
    """Compute structural analysis gaps — real system failures, not cosmetic issues."""
    gaps: list[dict] = []

    if not symbols:
        gaps.append({
            "area": "symbol_extraction",
            "reason": "No Java symbols extracted — check path or file access",
            "impact": "high",
        })
        return gaps

    controllers = spring_summary.get("controllers", [])
    if controllers and not route_surface:
        gaps.append({
            "area": "route_surface",
            "reason": (
                f"{len(controllers)} controller(s) detected but route_surface is empty — "
                "JAX-RS @Path or Spring @RequestMapping annotations may be missing"
            ),
            "impact": "high",
        })

    # Detect EventListenerProvider implementations via class signature, not class name.
    # A class named "CustomProcessor" implementing EventListenerProvider would be missed
    # by a class-name check but is correctly found via its implements clause in signature.
    _ELP_IFACE = "EventListenerProvider"
    elp_impls = [
        sym.symbol for sym in symbols
        if sym.type == "class" and _ELP_IFACE in (sym.signature or "")
    ]
    event_edges = [e for e in relations if e.type in ("listens_to_event", "publishes_event")]
    if elp_impls and not event_edges:
        gaps.append({
            "area": "event_flow",
            "reason": (
                f"{len(elp_impls)} EventListenerProvider implementation(s) found but "
                "no event flow edges detected"
            ),
            "impact": "medium",
        })

    return gaps


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
    custom_security: "tuple[CustomSecuritySpec, ...]" = (),
) -> dict:
    """Phase 5: Final assembly — single deterministic output contract."""
    sorted_syms = sorted(symbols, key=lambda s: s.symbol)
    sorted_rels = sorted(relations, key=lambda e: (e.from_symbol, e.type, e.to_symbol))
    sorted_changed = sorted(changed_symbols, key=lambda c: c.symbol)

    # Java role map: fqn → role (annotation evidence + JAX-RS @Path heuristic + name fallback)
    spring_role_map: dict[str, str] = {}
    for sym in sorted_syms:
        if sym.type in ("class", "interface"):
            role = _java_role(sym.annotations)
            # JAX-RS resource: class-level @Path without a recognized annotation → controller
            if role == "unknown" and "@Path" in sym.annotations:
                role = "controller"
            # Name-based fallback: when annotations provide no signal, infer from class name
            if role == "unknown":
                simple = sym.symbol.split(".")[-1].split("#")[0]
                role = _java_role_from_name(simple)
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

    # Bounded BFS reachability per node (graph-only).
    # Skipped when symbol count exceeds threshold: O(N*(V+E)) BFS for every symbol
    # hangs on large repos (keycloak: 80K+ symbols → 180s+ with no output).
    # bfs_reach contributes only 0.1× weight vs in_deg+out_deg; skipping it on large
    # repos causes no accuracy loss for spring-audit/endpoints/security analysis.
    _BFS_SYMBOL_THRESHOLD: int = 5000
    if len(sorted_syms) <= _BFS_SYMBOL_THRESHOLD:
        bfs_reach: dict[str, int] = {
            s.symbol: _bfs_reachability(s.symbol, adjacency)
            for s in sorted_syms
        }
    else:
        bfs_reach = {}

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
    has_diff = bool(sorted_changed)
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

    # No diff signal (no --since): fall back to call-graph centrality scores.
    # Avoids emitting all-zero scores which mislead agents into thinking the tool is broken.
    score_basis: str
    impact_note: Optional[str]
    if not has_diff and sorted_syms:
        for sym in sorted_syms:
            fqn = sym.symbol
            role = spring_role_map.get(fqn, "other")
            w = _IR_WEIGHTS.get(role, _IR_WEIGHT_DEFAULT)
            raw_c = in_deg.get(fqn, 0) + out_deg.get(fqn, 0) + bfs_reach.get(fqn, 0) * 0.1
            c = min(1.0, raw_c / max_raw)
            node_scores[fqn] = round(w * c, 4)
        score_basis = "call_graph_centrality"
        impact_note = "impact scores based on call-graph centrality (no --since provided)"
    elif has_diff:
        score_basis = "diff_impact"
        impact_note = None
    else:
        # No symbols at all — omit scores entirely rather than emit zeros
        score_basis = "none"
        impact_note = None

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
            "annotations": list(s.annotations),
            "annotation_values": dict(s.annotation_values),
            "modifiers": list(s.modifiers),
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

    # IC-005: aggregate event flow edges already built in _build_relations.
    # Always emit spring_events (even when empty) so callers don't need key-presence checks.
    _listen_edges = [e for e in sorted_rels if e.type == "listens_to_event"]
    _publish_edges = [e for e in sorted_rels if e.type == "publishes_event"]
    _spring_events: dict = {
        "listeners": sorted({e.from_symbol for e in _listen_edges}),
        "publishers": sorted({e.from_symbol for e in _publish_edges}),
        "event_types": sorted({e.to_symbol for e in _listen_edges + _publish_edges}),
        "flow_count": len(_listen_edges) + len(_publish_edges),
    }

    _base = {
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
            "score_basis": score_basis,
            **({"impact_note": impact_note} if impact_note else {}),
            "ranked_nodes": ranked_nodes,
        },
        "subsystems": subsystems,
        "change_set": change_set_out,
    }

    _extends_map = {
        e.from_symbol: e.to_symbol.split(".")[-1]
        for e in sorted_rels if e.type == "extends"
    }
    _route_surface = _build_route_surface(
        sorted_syms, route_diffs, extends_map=_extends_map, custom_security=custom_security
    )
    _analysis_gaps = _compute_analysis_gaps(sorted_syms, spring_summary, _route_surface, sorted_rels)

    # Detect filter-based security model for the assembled IR.
    # Stored here so CIR projections (project_endpoint_surface) can read it without
    # re-parsing symbols.
    _class_syms_asm = [s for s in sorted_syms if s.type in ("class", "interface")]
    _filter_based_asm = (
        any(
            ann in _FILTER_SECURITY_ANNOTATIONS
            for sym in _class_syms_asm
            for ann in sym.annotations
        )
        or any(
            _extends_map.get(sym.symbol, "") == "WebSecurityConfigurerAdapter"
            for sym in _class_syms_asm
        )
    )
    # Only real annotation-based policies count (not "programmatic" fallback).
    # Programmatic security does not mean every unannotated endpoint is unsecured.
    _has_ann_sec_asm = any(
        isinstance(r.get("security_annotations"), dict)
        and r["security_annotations"].get("policy") not in (None, "programmatic", "none_detected")
        for r in _route_surface
        if isinstance(r, dict)
    )
    if _filter_based_asm and _has_ann_sec_asm:
        _security_model_asm = "mixed"
    elif _filter_based_asm:
        _security_model_asm = "filter_based"
    elif _has_ann_sec_asm:
        _security_model_asm = "annotation_based"
    else:
        _security_model_asm = "unknown"

    return {
        **_base,
        "route_surface": _route_surface,
        "spring_events": _spring_events,
        "analysis_gaps": _analysis_gaps,
        "security_model": _security_model_asm,
        "audit": {
            "dropped_fields": dropped_fields,
        },
    }


# ---------------------------------------------------------------------------
# Route surface security extraction
# ---------------------------------------------------------------------------

def _custom_ann_param(raw: str, key: str) -> str:
    """Extract `key = value` from a raw annotation argument string.

    Prefers a quoted string literal; falls back to a bare token (constant ref
    such as ``SeguridadRecursosConst.RRHH_MOVADMINISTRATIVOS``). Returns "" when
    the key is absent.
    """
    import re as _re
    if not key:
        return ""
    m = _re.search(rf'\b{_re.escape(key)}\s*=\s*"([^"]+)"', raw)
    if m:
        return m.group(1)
    m = _re.search(rf'\b{_re.escape(key)}\s*=\s*([A-Za-z_][\w.]*)', raw)
    if m:
        return m.group(1)
    return ""


def _route_security_from_sym(
    method_sym: "Optional[SymbolRecord]",
    class_sym: "Optional[SymbolRecord]",
    custom_security: "tuple[CustomSecuritySpec, ...]" = (),
) -> "Optional[dict]":
    """Extract security policy from method and/or class-level annotations.

    Canonical single-source-of-truth security extractor.
    All security extraction (route_surface, endpoint_surface, MCP) uses this function.
    No independent re-extraction elsewhere.

    Resolution order (method-level takes precedence):
      @DenyAll                → {policy: deny_all}
      @PermitAll              → {policy: permit_all}
      @RolesAllowed           → {policy: roles_allowed, roles: [...]}
      @Authenticated          → {policy: authenticated}
      @PreAuthorize           → {policy: spring_preauthorize, expression: ...}
      @PostAuthorize          → {policy: spring_postauthorize, expression: ...}
      @Secured                → {policy: secured, roles: [...]}
      @RequiresRoles          → {policy: requiresroles, roles: [...]}
      @RequiresPermissions    → {policy: requirespermissions, roles: [...]}
      @SecurityRequirement    → {policy: openapi_security, spec: ...}
      <custom>                → {policy: custom, annotation, resourceName?, requiredLevel?}

    custom_security: project-defined security annotations from sourcecode.config.json
    (BUG-3). Checked after the built-in set so standard annotations always win.

    Falls back to class-level annotations if no method-level security found.
    Returns None if no security signal detected at either level.
    """
    import re as _re

    def _extract_from(sym: "SymbolRecord") -> "Optional[dict]":
        anns = set(sym.annotations)
        vals = sym.annotation_values

        if "@DenyAll" in anns:
            return {"policy": "deny_all"}
        if "@PermitAll" in anns:
            return {"policy": "permit_all"}
        if "@RolesAllowed" in anns:
            raw = vals.get("@RolesAllowed", "")
            roles = _re.findall(r'"([^"]+)"', raw)
            return {"policy": "roles_allowed", "roles": roles or [raw.strip('{} "\'')]}
        if "@Authenticated" in anns or "@AuthenticatedWithRoles" in anns:
            return {"policy": "authenticated"}
        for spring_ann in ("@PreAuthorize", "@PostAuthorize"):
            if spring_ann in anns:
                raw = vals.get(spring_ann, "")
                return {"policy": "spring_" + spring_ann[1:].lower(), "expression": raw.strip('"')}
        if "@Secured" in anns:
            raw = vals.get("@Secured", "")
            roles = _re.findall(r'"([^"]+)"', raw)
            return {"policy": "secured", "roles": roles}
        # Apache Shiro annotations
        for shiro_ann in ("@RequiresRoles", "@RequiresPermissions"):
            if shiro_ann in anns:
                raw = vals.get(shiro_ann, "")
                roles = _re.findall(r'"([^"]+)"', raw)
                return {"policy": shiro_ann[1:].lower(), "roles": roles}
        # OpenAPI security requirement
        if "@SecurityRequirement" in anns:
            raw = vals.get("@SecurityRequirement", "")
            return {"policy": "openapi_security", "spec": raw.strip()}
        # Project-defined custom security annotations (BUG-3).
        for spec in custom_security:
            if spec.marker in anns:
                raw = vals.get(spec.marker, "")
                out: dict = {"policy": "custom", "annotation": spec.short_name}
                res = _custom_ann_param(raw, spec.resource_param)
                lvl = _custom_ann_param(raw, spec.level_param)
                if res:
                    out["resourceName"] = res
                if lvl:
                    out["requiredLevel"] = lvl
                if spec.risk_level and spec.risk_level != "custom":
                    out["riskLevel"] = spec.risk_level
                return out
        return None

    # Method-level first, then class-level fallback
    for candidate in filter(None, [method_sym, class_sym]):
        result = _extract_from(candidate)
        if result is not None:
            result["_scope"] = "class" if candidate is class_sym else "method"
            return result
    return None


# ---------------------------------------------------------------------------
# Route surface helper (Fix 4)
# ---------------------------------------------------------------------------

def _build_route_surface(
    symbols: list[SymbolRecord],
    route_diffs: Optional[list[dict]],
    extends_map: Optional[dict[str, str]] = None,
    custom_security: "tuple[CustomSecuritySpec, ...]" = (),
) -> list[dict]:
    """Return route surface with inheritance projection and JAX-RS sub-resource locator resolution.

    extends_map: child_fqn → parent_simple_name derived from RelationEdge extends edges.
    Projects inherited endpoints onto subclasses that have a class-level @RequestMapping
    prefix but zero own method-level endpoints (IC-001 fix).

    JAX-RS sub-resource locators: methods with @Path but no HTTP verb annotation that return
    a resource class are used to compose full paths across the locator chain.
    Example: AdminRoot(@Path("/admin")) → getRealmsAdmin()(@Path("realms")) →
             RealmsAdminResource → getRealmAdmin()(@Path("{realm}")) →
             RealmAdminResource → getAttackDetection()(@Path("attack-detection")) →
             AttackDetectionResource → @GET @Path("brute-force/users/{userId}")
    Resolved path: /admin/realms/{realm}/attack-detection/brute-force/users/{userId}

    route_diffs semantics:
      None  → build full route surface from symbols (used by build_repo_ir and
              extract_java_endpoints)
      []    → return empty (no baseline to diff against, e.g. extract_file_ir
              without old_source)
      [...]  → return the pre-computed diffs from _diff_routes
    """
    if route_diffs is not None:
        return route_diffs

    # Phase 0: Build JAX-RS sub-resource locator map.
    # child_class_simple → [(parent_class_simple, locator_path_segment), ...]
    # Built before class_info so it is available for the "skip client proxy" guard.
    locator_map = _build_jaxrs_locator_map(symbols)

    # Phase 1: build per-class metadata (prefixes list) and own endpoint list.
    # "prefixes" is a list to support array @RequestMapping({"/v1/foo", "/v1/bar"}).
    class_info: dict[str, dict] = {}  # simple_name → {fqn, prefixes, own_endpoints}
    # P1 FIX: class symbol lookup for security annotation inheritance
    class_sym_by_simple: dict[str, "SymbolRecord"] = {}
    for sym in symbols:
        if sym.type not in ("class", "interface"):
            continue
        simple = sym.symbol.split(".")[-1]
        class_sym_by_simple[simple] = sym
        prefixes: list[str] = [""]
        if "@RequestMapping" in sym.annotations:
            args = sym.annotation_values.get("@RequestMapping", "")
            prefixes = _parse_route_paths(args)
        elif "@Path" in sym.annotations:
            # JAX-RS: class-level @Path is the resource prefix.
            args = sym.annotation_values.get("@Path", "")
            prefixes = _parse_route_paths(args) if args else [""]
        class_info[simple] = {
            "fqn": sym.symbol,
            "prefixes": prefixes,
            "own_endpoints": [],
            "has_path_ann": "@Path" in sym.annotations or "@RequestMapping" in sym.annotations,
        }

    routes: list[dict] = []
    seen: set[tuple] = set()
    _prog_sec_cache: dict[str, Optional[bool]] = {}  # declaring_file → has_programmatic

    # Phase 2: emit own endpoint symbols and record them per class.
    # Each method emits one route per resolved effective prefix.
    # For JAX-RS: effective prefix is resolved via sub-resource locator chain.
    # For Spring: effective prefix is the class-level @RequestMapping value (unchanged).
    for sym in symbols:
        if sym.symbol_kind != "endpoint":
            continue
        ann_name = next((a for a in sym.annotations if a in _ENDPOINT_ANNOTATIONS), None)
        if not ann_name:
            continue
        cls_fqn = _enclosing_class(sym.symbol)
        cls_simple = cls_fqn.split(".")[-1]
        args = sym.annotation_values.get(ann_name, "")

        if ann_name in _JAXRS_HTTP_ANNOTATIONS:
            # JAX-RS: HTTP verb annotations carry no path; path lives in @Path on the method.
            suffix = _parse_route_path(sym.annotation_values.get("@Path", ""))
            _cls_entry = class_info.get(cls_simple, {})
            cls_prefixes = _cls_entry.get("prefixes", [""])
            cls_has_path = _cls_entry.get("has_path_ann", False)
            # Skip client proxy interfaces: no class-level @Path, no method @Path,
            # and not reachable via a locator chain. Client proxies (e.g. RESTEasy
            # @RegisterRestClient) have HTTP verb annotations but no server-side binding.
            if (not suffix and not cls_has_path
                    and all(not p for p in cls_prefixes)
                    and cls_simple not in locator_map):
                continue
            # Resolve full path prefix via sub-resource locator chain.
            # For classes with no locator parent this returns own class prefix (unchanged).
            effective_prefixes = _resolve_jaxrs_prefixes(
                cls_simple, class_info, locator_map, frozenset()
            )
        else:
            # Spring MVC: path lives in annotation args; class prefix from @RequestMapping.
            suffix = _parse_route_path(args)
            effective_prefixes = class_info.get(cls_simple, {}).get("prefixes", [""])

        method = _parse_route_http_method(ann_name, args) or "GET"

        if cls_simple in class_info:
            class_info[cls_simple]["own_endpoints"].append(
                (method, suffix, sym.symbol, sym.stable_id)
            )

        # P1 FIX: extract security annotations (method-level first, class fallback)
        _cls_sym_for_sec = class_sym_by_simple.get(cls_simple)
        _sec = _route_security_from_sym(sym, _cls_sym_for_sec, custom_security)

        # Programmatic security fallback: scan controller file when no annotation found.
        if _sec is None:
            _decl_file = sym.declaring_file or ""
            if _decl_file and _decl_file not in _prog_sec_cache:
                try:
                    _prog_sec_cache[_decl_file] = _has_programmatic_security(
                        Path(_decl_file).read_text(encoding="utf-8", errors="ignore")
                    )
                except Exception:
                    _prog_sec_cache[_decl_file] = False
            if _prog_sec_cache.get(_decl_file):
                _sec = {"policy": "programmatic"}

        for prefix in effective_prefixes:
            # P1 fix: re.sub collapses any number of consecutive slashes (///, //, etc.)
            # Single .replace("//", "/") fails for triple-slash from prefix="/" + suffix="/{id}".
            full_path = re.sub(r"/+", "/", prefix + "/" + suffix).rstrip("/") or "/"
            if not full_path.startswith("/"):
                full_path = "/" + full_path

            key = (sym.symbol, method, prefix)
            if key not in seen:
                seen.add(key)
                _route_entry: dict = {
                    "symbol": sym.symbol,
                    "controller": cls_fqn,
                    "declaring_class": cls_fqn,
                    "effective_class": cls_fqn,
                    "path": full_path,
                    "method": method,
                    "stable_id": sym.stable_id,
                    "inheritance_depth": 0,
                }
                _route_entry["security_annotations"] = _sec
                routes.append(_route_entry)

    # Phase 3: inheritance projection — subclasses with a class-level @RequestMapping
    # prefix inherit parent methods that they do not override (same HTTP verb + path suffix).
    if extends_map:
        fqn_to_simple: dict[str, str] = {d["fqn"]: s for s, d in class_info.items()}
        simple_extends: dict[str, str] = {
            fqn_to_simple.get(child_fqn, child_fqn.split(".")[-1]): parent_simple
            for child_fqn, parent_simple in extends_map.items()
        }

        # Build lookup for security_annotations from phase-2 routes
        _parent_sec_by_sym: dict[str, object] = {
            r["symbol"]: r.get("security_annotations") for r in routes
        }

        for cls_simple, data in class_info.items():
            if not any(data["prefixes"]):
                continue

            # (verb, suffix) pairs declared on this subclass — these shadow parent methods.
            own_override_set: set[tuple[str, str]] = {
                (verb, suffix) for verb, suffix, _, _ in data["own_endpoints"]
            }

            chain = simple_extends.get(cls_simple)
            visited: set[str] = {cls_simple}
            depth = 1
            while chain and chain not in visited:
                visited.add(chain)
                parent = class_info.get(chain)
                if not parent:
                    break
                if parent["own_endpoints"]:
                    for verb, suffix, declaring_sym, stable_id in parent["own_endpoints"]:
                        # Skip methods the subclass overrides (same verb + path suffix).
                        if (verb, suffix) in own_override_set:
                            continue
                        for prefix in data["prefixes"]:
                            # P1 fix: collapse any number of consecutive slashes
                            full_path = re.sub(r"/+", "/", prefix + "/" + suffix).rstrip("/") or "/"
                            if not full_path.startswith("/"):
                                full_path = "/" + full_path
                            key = (cls_simple, declaring_sym, verb, prefix)
                            if key not in seen:
                                seen.add(key)
                                routes.append({
                                    "symbol": declaring_sym,
                                    "controller": data["fqn"],
                                    "declaring_class": parent["fqn"],
                                    "effective_class": data["fqn"],
                                    "path": full_path,
                                    "method": verb,
                                    "stable_id": stable_id,
                                    "inheritance_depth": depth,
                                    "security_annotations": _parent_sec_by_sym.get(declaring_sym),
                                })
                    break
                chain = simple_extends.get(chain)
                depth += 1

    return sorted(routes, key=lambda r: (r["effective_class"], r["path"]))


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
    custom_security: "Optional[list[CustomSecuritySpec]]" = None,
) -> dict:
    """Build IR across multiple Java files in a repo.

    Args:
        file_paths:      Relative paths to Java files to analyze.
        root:            Absolute repo root.
        since:           Git ref for symbol diff (e.g. "HEAD~1", "main").
        custom_security: Custom security annotation specs (BUG-3). When None,
                         loaded from <root>/sourcecode.config.json.

    Returns aggregated deterministic IR dict (schema_version=final-v1).
    """
    if custom_security is None:
        custom_security = _load_custom_security(root)
    _custom_sec_tuple = tuple(custom_security)
    _extra_capture = _capture_markers(custom_security)

    all_symbols: list[SymbolRecord] = []
    all_relations: list[RelationEdge] = []
    all_changed: list[ChangedSymbol] = []
    all_route_diffs: list[dict] = []

    # H-04: prefetch changed-file list once; avoids O(n) `git show` calls.
    # _since_changed=None means git unavailable → fall back to per-file fetch.
    _since_changed: "Optional[frozenset[str]]" = None
    if since:
        _since_changed = _get_git_changed_files(root, since)

    # L-6: analysis_meta tracking (files_read, lines_read, symbols_analyzed, token_estimate)
    _meta_files_read = 0
    _meta_lines_read = 0
    _meta_chars_read = 0

    # Pass 1: extract symbols from all files so we can build the same-package
    # type map before building relations.  Java classes in the same package
    # reference each other without import statements, so import_map alone cannot
    # resolve them — _build_same_package_map provides the cross-file fallback.
    #
    # Pre-scan filter: skip full symbol extraction for files that have no
    # Spring/JAX-RS/CDI annotations. These files (utility classes, model beans,
    # SPI interfaces) contribute no endpoints, transactions, or security findings
    # to spring-audit. The text scan is C-speed vs O(lines) Python parse loop.
    # Non-annotated files still register their package+class via a lightweight
    # regex scan so same-package type resolution remains correct.
    _ANNOTATION_MARKERS: tuple[str, ...] = (
        '@Controller', '@RestController', '@Service', '@Repository',
        '@Component', '@Configuration', '@Bean', '@Transactional',
        '@Path', '@GET', '@POST', '@PUT', '@DELETE', '@PATCH',
        '@PreAuthorize', '@RolesAllowed', '@Secured', '@EnableWebSecurity',
        '@SpringBootApplication', '@EventListener', '@TransactionalEventListener',
        '@RequiredArgsConstructor', '@AllArgsConstructor',
        '@Inject', '@ApplicationScoped', '@RequestScoped', '@Singleton',
        '@EnableMethodSecurity', '@EnableGlobalMethodSecurity',
        # JPA / persistence (needed for stereotype detection in all commands)
        '@Entity', '@MappedSuperclass', '@Embeddable',
        # AOP / messaging / event sourcing
        '@Aspect', '@Aggregate', '@Document',
        # Spring Data
        '@Query', '@NamedQuery',
        # Profile-gated beans/interfaces (e.g. Spring Data repository specializations
        # like `@Profile("spring-data-jpa") interface FooRepo extends FooRepository`).
        # Without this marker such interfaces are pre-scan-skipped and their
        # extends/implements edges are lost — making them invisible to impact analysis.
        '@Profile',
    )
    # Pre-pass: collect custom meta-annotation names from @interface definitions
    # that compose known Spring stereotypes (e.g. @DomainService = @Service + @Transactional).
    # These names must be added to the marker set so classes using them aren't
    # filtered out by the fast pre-scan below.
    _custom_meta_markers: set[str] = set()
    for _rp in sorted(file_paths):
        try:
            _src = (root / _rp).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "@interface" not in _src:
            continue
        if not any(m in _src for m in _ANNOTATION_MARKERS):
            continue
        for _m in re.finditer(r'@interface\s+(\w+)', _src):
            _custom_meta_markers.add(f"@{_m.group(1)}")
    # Custom security annotations (BUG-3) are also pre-scan markers so files
    # whose only relevant annotation is a custom one aren't filtered out.
    _effective_markers = (
        _ANNOTATION_MARKERS + tuple(_custom_meta_markers) + tuple(_extra_capture)
    )

    _per_file: list[tuple[str, str, str, list[str], list[SymbolRecord]]] = []
    for rel_path in sorted(file_paths):
        abs_path = root / rel_path
        try:
            source = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        _meta_files_read += 1
        _meta_lines_read += source.count("\n") + (1 if source and not source.endswith("\n") else 0)
        _meta_chars_read += len(source)
        # Fast pre-scan: if file has no relevant annotations skip full extraction.
        # Still register package/class name for same-package resolution.
        if not any(marker in source for marker in _effective_markers):
            pkg_m = _PKG_RE.search(source)
            _pkg = pkg_m.group(1) if pkg_m else ""
            # Minimal class-name symbols for same-package map (no methods/fields)
            _min_syms: list[SymbolRecord] = []
            for _cm in re.finditer(r'(?:class|interface|enum)\s+(\w+)', source):
                _cls_name = _cm.group(1)
                _fqn = f"{_pkg}.{_cls_name}" if _pkg else _cls_name
                _min_syms.append(SymbolRecord(
                    symbol=_fqn, type="class", confidence="medium",
                    declaring_file=rel_path,
                ))
            all_symbols.extend(_min_syms)
            # No relations needed for non-annotated files
            continue
        package, symbols, raw_imports = _extract_symbols(
            source, rel_path, extra_capture=_extra_capture
        )
        all_symbols.extend(symbols)
        _per_file.append((rel_path, source, package, raw_imports, symbols))

    # Build {package: {simple_name: FQN}} from every class/interface found.
    _same_pkg_map: dict[str, dict[str, str]] = _build_same_package_map(all_symbols)

    # Pass 2: build relations with same-package type resolution available.
    for rel_path, source, package, raw_imports, symbols in _per_file:
        same_pkg_types = _same_pkg_map.get(package, {})
        relations = _build_relations(
            symbols, raw_imports, source, package, rel_path,
            same_pkg_types=same_pkg_types,
        )

        old_source: Optional[str] = None
        if since:
            _file_changed = _since_changed is None or rel_path in _since_changed
            if _file_changed:
                old_source = _get_git_old_content(root, rel_path, since)

        if old_source is not None:
            _, old_symbols, _ = _extract_symbols(
                old_source, rel_path, extra_capture=_extra_capture
            )
            all_changed.extend(_diff_symbols(old_symbols, symbols))
            all_route_diffs.extend(_diff_routes(old_symbols, symbols))
        elif since and (_since_changed is None or rel_path in _since_changed):
            # File is new in since..HEAD (not in old ref) — treat as added.
            for sym in symbols:
                all_changed.append(ChangedSymbol(
                    symbol=sym.symbol,
                    change_type="added",
                    diff_type="structural_change",
                    confidence="high",
                ))

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

    # When since is given, route_surface is a diff view (empty [] or list of changes).
    # When since is None, pass None so _build_route_surface builds the full route surface.
    route_diffs_arg: Optional[list[dict]] = (
        sorted(all_route_diffs, key=lambda d: d["symbol"]) if since else None
    )
    ir = _assemble(
        all_symbols, unique_relations, all_changed, spring_summary, route_diffs_arg,
        custom_security=_custom_sec_tuple,
    )

    # BUG-7: XML Spring Security detection for the canonical CIR pipeline.
    # _assemble only sees Java symbols — XML config is invisible to it.
    # Scan here (where root is available) and retag route_surface entries so
    # build_canonical_ir produces correct CanonicalEndpoint.security values.
    _xml_sec_re = re.compile(
        r'(?:xmlns(?::[a-z]+)?="http://www\.springframework\.org/schema/security"'
        r'|<security:http\b'
        r'|<http\s[^>]*use-expressions'
        r'|spring-security-[2345]'
        r'|xmlns:security="http://www\.springframework\.org/schema/security")',
        re.IGNORECASE,
    )
    _xml_sec_detected = False
    for _xml_glob in (
        "*security*.xml", "*Security*.xml",
        "*applicationContext*.xml", "*-context.xml", "*Context.xml",
        "*spring*.xml", "*Spring*.xml",
    ):
        for _xf in root.rglob(_xml_glob):
            if "target/" in str(_xf).replace("\\", "/"):
                continue
            try:
                _xt = _xf.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if _xml_sec_re.search(_xt):
                _xml_sec_detected = True
                break
        if _xml_sec_detected:
            break
    if _xml_sec_detected:
        _sec_model = ir.get("security_model", "unknown")
        if _sec_model == "unknown":
            ir["security_model"] = "xml_or_filter_chain"
        elif _sec_model in ("annotation_based", "mixed"):
            ir["security_model"] = "mixed"
        # Retag route_surface entries that have no security (would become none_detected in CIR)
        for _r in ir.get("route_surface") or []:
            _r_sec = _r.get("security_annotations")
            if _r_sec is None or (isinstance(_r_sec, dict) and _r_sec.get("policy") == "none_detected"):
                _r["security_annotations"] = {"policy": "xml_or_filter_chain"}

    # L-6: inject analysis_meta — files_read, lines_read, symbols_analyzed, token_estimate
    ir["analysis_meta"] = {
        "files_read": _meta_files_read,
        "lines_read": _meta_lines_read,
        "symbols_analyzed": len(all_symbols),
        "token_estimate": _meta_chars_read // 4,  # 4 chars ≈ 1 token (rough approximation)
    }
    return ir


# ---------------------------------------------------------------------------
# Output size limits
# ---------------------------------------------------------------------------

# Vendor/generated dirs to skip when finding Java files and in git analysis.
_VENDOR_DIRS: frozenset[str] = frozenset({
    "vendor", "node_modules", "dist", "target", "build",
    ".gradle", ".mvn", "generated", "generated-sources",
    "generated-resources",
})


def apply_ir_size_limits(
    ir: dict,
    *,
    max_nodes: Optional[int] = None,
    max_edges: Optional[int] = None,
    summary_only: bool = False,
) -> dict:
    """Apply size limits to a repo-ir output dict. Non-destructive: returns new dict.

    Node ordering: top-ranked (by impact score) nodes are kept first.
    Edge priority: edges connecting two kept nodes over cross-boundary edges.
    """
    if not max_nodes and not max_edges and not summary_only:
        return ir

    out = dict(ir)
    graph = ir.get("graph") or {}
    nodes: list[dict] = list(graph.get("nodes") or [])
    edges: list[dict] = list(graph.get("edges") or [])
    ranked: list[dict] = list((ir.get("impact") or {}).get("ranked_nodes") or [])
    analysis: dict = ir.get("analysis") or {}

    if summary_only:
        # FIX-P0-3 (revised): summary_only must be safe for LLM context windows.
        # Hard budget: 100 KB. Bound every top-level section including inner lists.
        _SUMMARY_MAX_BYTES = 100_000

        n_nodes, n_edges = len(nodes), len(edges)
        out["graph"] = {
            "nodes": [],
            "edges": [],
            "_omitted": (
                f"{n_nodes} nodes and {n_edges} edges omitted — "
                "remove --summary-only to restore full graph"
            ),
        }

        # Reverse graph: top 10 hubs by in-degree, capped inner lists to 20 callers each.
        # BUG-FIX: previously took top 30 keys but did not cap inner lists → each hub
        # could be 50-140 KB (all callers of KeycloakSession, RealmModel, etc.).
        def _cap_rg_entry(entry: dict, max_per_list: int = 20) -> dict:
            """Cap every list inside a reverse_graph entry to max_per_list items."""
            return {k: (v[:max_per_list] if isinstance(v, list) else v) for k, v in entry.items()}

        full_rg: dict = ir.get("reverse_graph") or {}
        if full_rg:
            _rg_sorted = sorted(
                full_rg.items(),
                key=lambda x: sum(len(v) for v in x[1].values() if isinstance(v, list)),
                reverse=True,
            )
            out["reverse_graph"] = {
                k: _cap_rg_entry(v) for k, v in _rg_sorted[:10]
            }
            if len(full_rg) > 10:
                out["reverse_graph_note"] = (
                    f"Showing 10/{len(full_rg)} reverse-graph hubs (top by in-degree), "
                    "20 callers each. Remove --summary-only for full graph."
                )
        else:
            out["reverse_graph"] = {}

        out["impact"] = {
            "global_score": (ir.get("impact") or {}).get("global_score", 0),
            "ranked_nodes": ranked[:20],
        }
        out["analysis"] = {
            "changed_entities": (analysis.get("changed_entities") or [])[:20],
            "impacted_entities": (analysis.get("impacted_entities") or [])[:20],
            "isolated_changes": (analysis.get("isolated_changes") or [])[:10],
            "validated_changes": (analysis.get("validated_changes") or [])[:10],
        }
        # Bound subsystems: top 15 by member count, members capped at 10 each
        raw_subsystems: list = ir.get("subsystems") or []
        if raw_subsystems:
            _ss_sorted = sorted(
                raw_subsystems,
                key=lambda s: len(s.get("members", [])),
                reverse=True,
            )
            _ss_capped = []
            for _ss in _ss_sorted[:15]:
                _ss_entry = dict(_ss)
                if isinstance(_ss_entry.get("members"), list) and len(_ss_entry["members"]) > 10:
                    _ss_entry["members"] = _ss_entry["members"][:10]
                    _ss_entry["_members_note"] = f"Showing 10/{len(_ss.get('members', []))} members"
                _ss_capped.append(_ss_entry)
            out["subsystems"] = _ss_capped
            if len(raw_subsystems) > 15:
                out["subsystems_note"] = (
                    f"Showing 15/{len(raw_subsystems)} subsystems by member count. "
                    "Remove --summary-only for the full list."
                )
        # Bound change_set: top 30 by impact
        raw_cs: list = ir.get("change_set") or []
        out["change_set"] = raw_cs[:30]
        if len(raw_cs) > 30:
            out["change_set_note"] = (
                f"Showing 30/{len(raw_cs)} changed symbols. "
                "Remove --summary-only for the full list."
            )
        # Bound route_surface: top 50 endpoints.
        # BUG-FIX: route_surface is a list (not dict) — previous isinstance(dict) check
        # never triggered, passing all 434 entries unchanged (244 KB on Keycloak).
        raw_rs = ir.get("route_surface")
        _rs_total = 0
        if isinstance(raw_rs, list):
            _rs_total = len(raw_rs)
            out["route_surface"] = raw_rs[:50]
            if _rs_total > 50:
                out["route_surface_note"] = (
                    f"Showing 50/{_rs_total} endpoints. "
                    "Remove --summary-only for full route surface."
                )
        elif isinstance(raw_rs, dict):
            # Legacy dict format with "endpoints" sub-key
            raw_eps: list = raw_rs.get("endpoints") or []
            _rs_total = len(raw_eps)
            out["route_surface"] = {
                **{k: v for k, v in raw_rs.items() if k != "endpoints"},
                "endpoints": raw_eps[:50],
            }
            if _rs_total > 50:
                out["route_surface"]["_note"] = (
                    f"Showing 50/{_rs_total} endpoints. "
                    "Remove --summary-only for full route surface."
                )
        # spring_events: cap at 50 (usually small but can grow on large Spring apps)
        raw_se = ir.get("spring_events")
        if isinstance(raw_se, list) and len(raw_se) > 50:
            out["spring_events"] = raw_se[:50]
            out["spring_events_note"] = f"Showing 50/{len(raw_se)} spring events."
        # analysis_gaps: keep as-is (always small)

        # Hard byte budget: trim progressively until under _SUMMARY_MAX_BYTES.
        # Each pass reduces a specific section; stops as soon as budget is met.
        import json as _json
        _encoded = _json.dumps(out, ensure_ascii=False)
        _over_budget = len(_encoded.encode("utf-8")) > _SUMMARY_MAX_BYTES
        if _over_budget:
            # Trim schedule: (section, new_limit, inner_list_cap_if_dict)
            _trim_schedule = [
                ("route_surface", 30, None),
                ("reverse_graph", 5, 10),
                ("route_surface", 15, None),
                ("subsystems", 8, None),
                ("change_set", 10, None),
                ("impact_ranked", 10, None),
                ("route_surface", 5, None),
                ("reverse_graph", 2, 5),
                ("reverse_graph", 0, None),
            ]
            for _trim_key, _trim_limit, _inner_cap in _trim_schedule:
                if _trim_key == "impact_ranked":
                    out["impact"] = {**out["impact"], "ranked_nodes": ranked[:_trim_limit]}
                elif _trim_key == "reverse_graph" and _trim_limit == 0:
                    out["reverse_graph"] = {}
                    out["reverse_graph_note"] = (
                        "reverse_graph omitted to meet LLM budget. "
                        "Remove --summary-only for full IR."
                    )
                elif _trim_key in out:
                    _val = out[_trim_key]
                    if isinstance(_val, dict) and _inner_cap is not None:
                        out[_trim_key] = {
                            k: _cap_rg_entry(v, _inner_cap) if isinstance(v, dict) else v
                            for k, v in list(_val.items())[:_trim_limit]
                        }
                    elif isinstance(_val, dict):
                        out[_trim_key] = dict(list(_val.items())[:_trim_limit])
                    elif isinstance(_val, list):
                        out[_trim_key] = _val[:_trim_limit]
                _encoded = _json.dumps(out, ensure_ascii=False)
                if len(_encoded.encode("utf-8")) <= _SUMMARY_MAX_BYTES:
                    break
            out["_budget_note"] = (
                f"Output trimmed to ~{len(_encoded.encode('utf-8')) // 1024}KB "
                f"(target {_SUMMARY_MAX_BYTES // 1024}KB) for LLM safety. "
                "Remove --summary-only or use --output for full IR."
            )
        return out

    # Build score map from ranked_nodes (already sorted -score, fqn)
    score_map: dict[str, float] = {rn["entity"]: rn["score"] for rn in ranked}
    kept_fqns: Optional[set[str]] = None

    if max_nodes is not None and len(nodes) > max_nodes:
        nodes_sorted = sorted(
            nodes,
            key=lambda n: (-score_map.get(n["fqn"], 0.0), n["fqn"]),
        )
        nodes = nodes_sorted[:max_nodes]
        kept_fqns = {n["fqn"] for n in nodes}
        ranked = [rn for rn in ranked if rn["entity"] in kept_fqns]

    if kept_fqns is not None or max_edges is not None:
        if kept_fqns is not None:
            # Fix 2: type-aware priority so semantic edges survive node truncation.
            # Annotation strings (@Service etc.) and field FQNs are never in kept_fqns,
            # so "both endpoints kept" drops all injects/annotated_with edges.
            _SEMANTIC_TYPES = frozenset({"extends", "implements", "injects",
                                         "publishes_event", "listens_to_event"})
            _ANNOTATION_TYPES = frozenset({"annotated_with"})
            tier1 = [e for e in edges if e["from"] in kept_fqns and e["type"] in _SEMANTIC_TYPES]
            tier2 = [e for e in edges if e["from"] in kept_fqns and e["type"] in _ANNOTATION_TYPES]
            tier3 = [e for e in edges
                     if e["from"] in kept_fqns and e["to"] in kept_fqns and e["type"] == "imports"]
            _seen_e = {(e["from"], e["to"], e["type"]) for e in tier1 + tier2 + tier3}
            tier4 = [e for e in edges if (e["from"], e["to"], e["type"]) not in _seen_e]
            edges = tier1 + tier2 + tier3 + tier4
        if max_edges is not None:
            edges = edges[:max_edges]

    out["graph"] = {"nodes": nodes, "edges": edges}
    out["impact"] = {
        "global_score": (ir.get("impact") or {}).get("global_score", 0),
        "ranked_nodes": ranked,
    }

    # ── Trim reverse_graph to match node/edge limits ──────────────────────────
    # BUG-P0-02: reverse_graph was never bounded by --max-nodes/--max-edges.
    # A 26K-node repo (Broadleaf) emits ~3MB of reverse_graph even when
    # --max-nodes 200 --max-edges 500 is requested.
    full_rg: dict = ir.get("reverse_graph") or {}
    if full_rg:
        # Inner caller-list cap: prevents individual entries from dominating budget.
        # Formula: max(20, max_nodes // 4) when max_nodes given; 50 otherwise.
        def _cap_rg_lists(entry: dict, cap: int) -> dict:
            return {k: (v[:cap] if isinstance(v, list) and len(v) > cap else v)
                    for k, v in entry.items()}

        if kept_fqns is not None:
            # max_nodes was applied — restrict reverse_graph to kept nodes only.
            # Cap inner caller lists proportionally: large max_nodes → more callers shown.
            _inner_cap = max(20, max_nodes // 4) if max_nodes else 50
            trimmed_rg: dict = {
                k: _cap_rg_lists(v, _inner_cap)
                for k, v in full_rg.items()
                if k in kept_fqns
            }
            out["reverse_graph"] = trimmed_rg
            _rg_trimmed_count = len(full_rg) - len(trimmed_rg)
            if _rg_trimmed_count:
                out["reverse_graph_note"] = (
                    f"reverse_graph trimmed: {len(trimmed_rg)}/{len(full_rg)} entries "
                    f"kept (matching --max-nodes {max_nodes} kept nodes), "
                    f"caller lists capped at {_inner_cap}. "
                    "Use --output for full reverse_graph."
                )
        elif max_edges is not None:
            # Only max_edges given (no max_nodes): cap reverse_graph keys
            # proportionally.  Target: at most max_edges keys, sorted by in-degree
            # (most-connected hubs first) so the most useful entries survive.
            _rg_limit = max(1, min(max_edges, len(full_rg)))
            _rg_sorted_keys = sorted(
                full_rg.keys(),
                key=lambda k: sum(len(v) for v in full_rg[k].values() if isinstance(v, list)),
                reverse=True,
            )
            _inner_cap = 50
            out["reverse_graph"] = {
                k: _cap_rg_lists(full_rg[k], _inner_cap)
                for k in _rg_sorted_keys[:_rg_limit]
            }
            if len(full_rg) > _rg_limit:
                out["reverse_graph_note"] = (
                    f"reverse_graph trimmed: {_rg_limit}/{len(full_rg)} entries "
                    f"kept (top by in-degree, bounded by --max-edges {max_edges}), "
                    f"caller lists capped at {_inner_cap}. "
                    "Use --output for full reverse_graph."
                )

    return out


# ---------------------------------------------------------------------------
# Convenience: find Java files in a repo
# ---------------------------------------------------------------------------

def extract_java_endpoints(root: Path) -> "dict[str, Any]":
    """Extract REST endpoint surface from Java source files.

    Canonical endpoint extractor — uses IR symbol extraction + _build_route_surface().
    Security extraction delegated to _route_security_from_sym (single source of truth);
    results stored in route["security_annotations"] by _build_route_surface.

    Returns JSON-serializable dict:
      {endpoints: [{method, path, controller, handler, security?, required_permission?}],
       total, no_security_signal, undocumented}
    """
    import re as _re
    from typing import Any as _Any
    from sourcecode.path_filters import is_test_path

    _EXTENDS_FROM_SIG = _re.compile(r'\bextends\s+(\w+)')

    # Custom security annotations (BUG-3): recognized via sourcecode.config.json.
    _custom_security = _load_custom_security(root)
    _custom_sec_tuple = tuple(_custom_security)
    _extra_capture = _capture_markers(_custom_security)

    # Exclude REST client proxy modules — they use JAX-RS annotations for client-side
    # proxy generation (RESTEasy, MicroProfile REST Client) and are NOT server resources.
    _CLIENT_PATH_FRAGMENTS = (
        "/admin-client/", "/rest-client/", "/client-api/", "/api-client/",
    )
    java_files = sorted(
        p for p in root.rglob("*.java")
        if not is_test_path(str(p).replace("\\", "/"))
        and "target/" not in str(p).replace("\\", "/")
        and not any(f in str(p).replace("\\", "/") for f in _CLIENT_PATH_FRAGMENTS)
    )

    all_symbols: list[SymbolRecord] = []
    extends_map: dict[str, str] = {}

    for jf in java_files:
        try:
            source = jf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            rel = str(jf.relative_to(root)).replace("\\", "/")
        except ValueError:
            rel = str(jf).replace("\\", "/")
        _, symbols, _ = _extract_symbols(source, rel, extra_capture=_extra_capture)
        for sym in symbols:
            all_symbols.append(sym)
            if sym.type in ("class", "interface"):
                m = _EXTENDS_FROM_SIG.search(sym.signature or "")
                if m:
                    extends_map[sym.symbol] = m.group(1)

    routes = _build_route_surface(
        all_symbols, route_diffs=None, extends_map=extends_map,
        custom_security=_custom_sec_tuple,
    )

    # Security extraction: _build_route_surface already calls _route_security_from_sym
    # and stores the result as route["security_annotations"].
    # No independent re-extraction here — _route_security_from_sym is the single
    # source of truth for security policy extraction.

    # Detect interface-based Spring MVC controllers (implements Controller).
    # These predate annotation-style and have URL mapping in XML, not annotations.
    # We emit a synthetic "(xml-mapped)" entry so they appear in the endpoint surface.
    _SPRING_CONTROLLER_IFACE = "org.springframework.web.servlet.mvc.Controller"
    _annotated_classes = {
        route.get("effective_class", "").split(".")[-1]
        for route in routes
    }
    for sym in all_symbols:
        if sym.type != "class":
            continue
        if _SPRING_CONTROLLER_IFACE not in sym.imports_used:
            continue
        cls_simple = sym.symbol.split(".")[-1]
        if cls_simple in _annotated_classes:
            continue
        routes.append({
            "symbol": f"{sym.symbol}#handleRequest",
            "effective_class": sym.symbol,
            "method": "ANY",
            "path": "(xml-mapped)",
            "security_annotations": None,
            "note": "interface-based Spring MVC controller — URL mapped via XML",
        })

    # Detect controllers whose HTTP mappings live on an IMPLEMENTED interface that is
    # not part of the scanned source surface. The dominant case is openapi-generator
    # "interface-only" output (e.g. PetV2Api, VetsApi) emitted under
    # target/generated-sources, which the scanner excludes. Such a controller carries
    # @RestController/@Controller and an `implements XxxApi` clause but contributes no
    # method-level routes, so its endpoints are invisible. Emit an explicit warning so
    # an empty/partial surface is not silently misread as "no endpoints / no security".
    _CONTROLLER_ANNS = {"@RestController", "@Controller"}
    _IMPLEMENTS_RE = _re.compile(r'\bimplements\s+(.+)$')
    _routed_fqns = {route.get("effective_class") for route in routes}
    interface_defined_controllers: list[str] = []
    endpoint_warnings: list[str] = []
    for sym in all_symbols:
        if sym.type != "class":
            continue
        if not (_CONTROLLER_ANNS & set(sym.annotations)):
            continue
        if sym.symbol in _routed_fqns:
            continue  # already contributes routes — surface is captured
        m = _IMPLEMENTS_RE.search(sym.signature or "")
        if not m:
            continue
        ifaces = _split_supertype_list(m.group(1))
        api_ifaces = [i for i in ifaces if i.endswith("Api")]
        if not api_ifaces:
            continue
        interface_defined_controllers.append(sym.symbol)
        endpoint_warnings.append(
            f"{sym.symbol.split('.')[-1]} implements {', '.join(api_ifaces)}: HTTP "
            "mappings are declared on the implemented interface (commonly generated by "
            "openapi-generator under target/generated-sources, which is not scanned). "
            "Endpoint surface for this controller is NOT captured."
        )

    endpoints: list[dict] = []
    for route in routes:
        handler = (
            route["symbol"].split("#")[1]
            if "#" in route["symbol"]
            else route["symbol"].rsplit(".", 1)[-1]
        )
        controller = route.get("effective_class", "").split(".")[-1]

        entry: dict = {
            "method": route["method"],
            "path": route["path"],
            "controller": controller,
            "handler": handler,
        }
        # Use security_annotations already extracted by _build_route_surface
        # via the canonical _route_security_from_sym extractor.
        security_info = route.get("security_annotations")
        entry["security"] = security_info if security_info is not None else {"policy": "none_detected"}
        if security_info:
            # Backward compat: keep required_permission for custom annotation
            if isinstance(security_info, dict) and security_info.get("policy") == "custom_permission":
                entry["required_permission"] = security_info["required_permission"]
        endpoints.append(entry)

    # Filter out endpoints whose path looks like a Java FQN (e.g. dynamic admin routing
    # in frameworks like Broadleaf Commerce where @AdminSection registers entity class
    # FQNs as URL segments). These are not real REST paths — they are resolved at
    # runtime by the framework. Including them pollutes the endpoint surface with 20+
    # garbage entries that confuse agents and break endpoint count accuracy.
    # Pattern: path segment that matches a Java package hierarchy (org.foo.Bar).
    import re as _re_fqn
    _FQN_PATH_RE = _re_fqn.compile(
        r"/(org|com|net|io|edu)\.[a-z][a-z0-9]*\.[a-zA-Z]",
    )
    endpoints = [e for e in endpoints if not _FQN_PATH_RE.search(e.get("path", ""))]

    # "no_security_signal" = no recognized security annotation at method OR class level.
    # Note: repos may use framework-level security (e.g. Keycloak itself) with no
    # per-endpoint annotations — this count reflects annotation-based coverage only.
    no_security_signal = sum(
        1 for e in endpoints
        if e.get("security", {}).get("policy") == "none_detected"
    )

    # Detect filter-based security: centralized Spring Security config class.
    # When present, high no_security_signal is expected — security is enforced by
    # the filter chain, not per-endpoint annotations.
    _class_syms = [s for s in all_symbols if s.type in ("class", "interface")]
    _filter_based = (
        # Config class annotated with EnableWebSecurity / EnableMethodSecurity
        any(
            ann in _FILTER_SECURITY_ANNOTATIONS
            for sym in _class_syms
            for ann in sym.annotations
        )
        # Class extends WebSecurityConfigurerAdapter (pre-Spring 5.7 style)
        or any(
            extends_map.get(sym.symbol, "") == "WebSecurityConfigurerAdapter"
            for sym in _class_syms
        )
    )
    _has_annotation_security = any(
        e.get("security", {}).get("policy") not in (None, "none_detected", "programmatic")
        for e in endpoints
    )
    if _filter_based and _has_annotation_security:
        security_model = "mixed"
    elif _filter_based:
        security_model = "filter_based"
    elif _has_annotation_security:
        security_model = "annotation_based"
    else:
        security_model = "unknown"

    # Detect XML-based Spring Security config. When present, per-endpoint
    # none_detected is expected and does NOT mean the endpoint is unsecured —
    # security is declared in XML (HttpSecurity rules, filter chains, web.xml
    # security constraints). Update security_model and re-tag affected endpoints
    # so the output cannot be misread as "unprotected".
    _XML_SECURITY_RE = re.compile(
        r'(?:xmlns(?::[a-z]+)?="http://www\.springframework\.org/schema/security"'
        r'|<security:http\b'
        r'|<http\s[^>]*use-expressions'
        r'|spring-security-[2345]'
        r'|xmlns:security="http://www\.springframework\.org/schema/security")',
        re.IGNORECASE,
    )
    _xml_security_detected = False
    _XML_GLOBS = (
        "*security*.xml", "*Security*.xml",
        "*applicationContext*.xml", "*-context.xml", "*Context.xml",
        "*spring*.xml", "*Spring*.xml",
    )
    for _glob in _XML_GLOBS:
        for _xf in root.rglob(_glob):
            if "target/" in str(_xf).replace("\\", "/"):
                continue
            try:
                _xt = _xf.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if _XML_SECURITY_RE.search(_xt):
                _xml_security_detected = True
                break
        if _xml_security_detected:
            break

    if _xml_security_detected:
        # Re-tag per-endpoint none_detected → xml_or_filter_chain regardless of security_model.
        # BUG-7 fix: previously only ran when model == "unknown", causing false-positive SEC-001
        # when annotation security (@PreAuthorize) coexisted with XML security config.
        for ep in endpoints:
            if ep.get("security", {}).get("policy") == "none_detected":
                ep["security"] = {"policy": "xml_or_filter_chain"}
        if security_model == "unknown":
            security_model = "xml_or_filter_chain"
        elif security_model in ("annotation_based", "mixed"):
            security_model = "mixed"
        # filter_based stays filter_based — XML + filter chain is still filter_based
        # Recompute no_security_signal (now counts only truly unknown endpoints)
        no_security_signal = sum(
            1 for e in endpoints
            if e.get("security", {}).get("policy") == "none_detected"
        )

    result: dict[str, Any] = {
        "endpoints": endpoints,
        "total": len(endpoints),
        "no_security_signal": no_security_signal,
        "security_model": security_model,
        # Keep legacy field name for backward compat, now means same as no_security_signal
        "undocumented": no_security_signal,
    }
    # Surface incomplete-endpoint warnings (interface-defined controllers) only when
    # present, to keep output backward-compatible for the common case.
    if endpoint_warnings:
        result["warnings"] = endpoint_warnings
        result["interface_defined_controllers"] = interface_defined_controllers
    return result


def find_java_files(root: Path, *, max_files: int = 8000, limitations: list[str] | None = None) -> list[str]:
    """Return relative paths to Java files under root, excluding test dirs and vendor."""
    results: list[str] = []
    _capped = False
    for p in sorted(root.rglob("*.java")):
        if len(results) >= max_files:
            _capped = True
            break
        try:
            rel = str(p.relative_to(root)).replace("\\", "/")
        except ValueError:
            continue
        parts = rel.split("/")
        # Skip test dirs — use centralised is_test_path (consistent with
        # extract_java_endpoints), guarded against false positives where a Java
        # *package* is named "test" inside a production src/main/ source root.
        if _is_test_path(rel):
            _skip = True
            # Prepend "/" so the check works whether or not rel has a leading slash.
            _rel_sl = "/" + rel
            if "/src/main/" in _rel_sl:
                # is_test_path may fire on a package segment (e.g. com.example.test)
                # rather than a true test module directory.  Only skip when the path
                # prefix BEFORE src/main/ is itself a test path (meaning the whole
                # module is a test module, not just a package named "test").
                _prefix = _rel_sl.split("/src/main/")[0]
                _prefix_parts = [p for p in _prefix.split("/") if p]
                # A module is a test module if is_test_path says so OR if any
                # module directory component starts with "test" (e.g. "test-framework",
                # "test-providers", "testsuite") OR contains "test"/"tests" as a
                # hyphen/underscore-separated word (e.g. "jobrunr-micronaut-tests").
                _prefix_is_test = (
                    _is_test_path(_prefix + "/x.java")
                    or any(p.lower().startswith("test") for p in _prefix_parts)
                    or any(
                        w in {"test", "tests", "spec", "specs"}
                        for p in _prefix_parts
                        for w in p.lower().replace("-", " ").replace("_", " ").split()
                    )
                )
                if not _prefix_is_test:
                    _skip = False
            if _skip:
                continue
        # Skip vendor/generated/build dirs
        if any(part in _VENDOR_DIRS for part in parts[:-1]):
            continue
        # Skip REST client proxy modules — JAX-RS annotations there are for client-side
        # proxy generation, not server resources. Including them pollutes IR roles and routes.
        if any(f in rel for f in ("/admin-client/", "/rest-client/", "/client-api/", "/api-client/")):
            continue
        results.append(rel)
    if _capped and limitations is not None:
        limitations.append(
            f"MAX_JAVA_FILES_REACHED: scanned {max_files} files — repository likely has more"
        )
    return results


# ---------------------------------------------------------------------------
# Blast-radius / change-impact analysis
# ---------------------------------------------------------------------------

def compute_blast_radius(
    ir: dict,
    target: str,
    *,
    max_depth: int = 4,
) -> dict:
    """Compute the blast radius (change impact) of a symbol or class in a repo IR.

    Given a fully built IR dict (from build_repo_ir), identifies:
    - direct callers (reverse graph depth=1)
    - indirect callers (reverse graph depth=2+, BFS)
    - endpoints in route_surface that transitively depend on the target
    - mappers / persistence paths (repositories, DAOs, @Mapper interfaces) in the blast cone
    - security surface affected (security policies on touched endpoints)
    - cross_module_impact: which subsystems are hit
    - transactional boundaries touched
    - risk_score / risk_level / confidence_score / confidence_level
    - explanation: short human-readable rationale

    Args:
        ir:        Full repo IR dict (from build_repo_ir; NOT summary_only trimmed).
        target:    FQN, simple class name, or file-stem to look up.
        max_depth: BFS depth limit (default 4).

    Returns a structured blast-radius dict ready for JSON output.
    """
    reverse_graph: dict[str, dict[str, list[str]]] = ir.get("reverse_graph") or {}
    route_surface: list[dict] = ir.get("route_surface") or []
    _graph: dict = ir.get("graph") or {}
    graph_nodes: list[dict] = _graph.get("nodes") or []
    graph_edges: list[dict] = _graph.get("edges") or []
    subsystems: list[dict] = ir.get("subsystems") or []

    # ── 1. Resolve target → one or more FQNs ─────────────────────────────────
    _path_like = "/" in target or "\\" in target or target.endswith(".java")
    resolution, matched_fqns = _resolve_target(target, reverse_graph, graph_nodes)

    # File-path input with ambiguous resolution: require the user to be specific.
    if _path_like and len(matched_fqns) > 1:
        _candidates = sorted(matched_fqns)
        return {
            "target": target,
            "resolution": "ambiguous_path",
            "message": (
                f"Path '{target}' matches {len(matched_fqns)} classes in the IR. "
                "Pass the full FQN to select one."
            ),
            "candidates": _candidates,
            "direct_callers": [],
            "indirect_callers": [],
            "endpoints_affected": [],
            "mappers_affected": [],
            "security_surface_affected": [],
            "cross_module_impact": [],
            "transactional_boundaries_touched": [],
            "risk_score": 0.0,
            "risk_level": "unknown",
            "confidence_score": 0.0,
            "confidence_level": "low",
            "explanation": f"Ambiguous path — {len(matched_fqns)} candidates found.",
        }

    if not matched_fqns:
        # Build a short candidate list to help the user
        _candidates = _blast_radius_candidates(target, reverse_graph, graph_nodes)
        result: dict = {
            "target": target,
            "resolution": "not_found",
            "message": (
                f"No symbol matching {target!r} found in IR. "
                "Verify the class name or FQN. "
                "Run `sourcecode repo-ir <repo> --output ir.json` to inspect available symbols."
            ),
            "direct_callers": [],
            "indirect_callers": [],
            "endpoints_affected": [],
            "mappers_affected": [],
            "security_surface_affected": [],
            "cross_module_impact": [],
            "transactional_boundaries_touched": [],
            "risk_score": 0.0,
            "risk_level": "unknown",
            "confidence_score": 0.0,
            "confidence_level": "low",
            "explanation": f"Target {target!r} not found in IR.",
        }
        if _candidates:
            result["candidates"] = _candidates
        return result

    # When resolution is ambiguous/partial, surface ordered candidates alongside results
    _candidates_out: list[dict] = []
    if resolution in ("ambiguous", "partial"):
        _candidates_out = _blast_radius_candidates(target, reverse_graph, graph_nodes)

    # ── 2. BFS from target(s) through reverse graph ───────────────────────────
    all_affected: dict[str, int] = {}   # fqn → depth at which first found
    direct_callers: list[str] = []
    queue: list[tuple[str, int]] = []

    # BUG-02 fix: hub-class guard.  If any seed has > 500 direct callers (e.g.
    # KeycloakSession with 2023 importers), deep BFS is O(n^depth) and collapses
    # to 70-91s at depth=4.  Cap effective depth to 1 for hub classes so the
    # direct-caller list is still accurate but we skip the catastrophic expansion.
    # Instead of omitting indirect callers entirely, we do a sampled BFS: pick
    # _SAMPLE_SIZE random direct callers, run depth-2 BFS from those, then scale
    # up to estimate total indirect reach.
    _HUB_CALLER_THRESHOLD = 500
    _HUB_SAMPLE_SIZE = 20
    _HUB_SAMPLE_DEPTH = 2
    _effective_depth = max_depth
    _hub_class_guard = False
    for seed in matched_fqns:
        _seed_callers = _all_callers_from_rg(seed, reverse_graph)
        if len(_seed_callers) > _HUB_CALLER_THRESHOLD and max_depth > 1:
            _effective_depth = 1
            _hub_class_guard = True
            break

    for seed in matched_fqns:
        callers = _all_callers_from_rg(seed, reverse_graph)
        for c in callers:
            if c not in all_affected:
                all_affected[c] = 1
                direct_callers.append(c)
                if _effective_depth > 1:
                    queue.append((c, 1))

    # ── 2a. Interface bridging: Spring DI / CDI / IoC pattern ────────────────
    # In DI frameworks (Spring, CDI, Guice), callers inject the INTERFACE, not
    # the Impl.  e.g. `impact OrderServiceImpl` → 0 direct callers, because every
    # caller wires against OrderService.
    #
    # Root cause: implements edges in graph.edges often carry unresolved short-name
    # `to` values (e.g. "OrderService" not FQN), so _build_reverse_adjacency drops
    # them (to_symbol ∉ all_fqns).  The reverse_graph["...OrderService"] therefore
    # has no "implements" key — we cannot scan it from the reverse side.
    #
    # Fix: scan FORWARD graph edges for type=implements FROM our matched classes.
    # Resolve the `to` value (short or FQN) against reverse_graph keys via suffix
    # matching.  Gather non-structural callers of those interface keys and merge
    # them into direct_callers.
    _iface_bridging: list[dict] = []  # [{interface, caller_count}] for output metadata

    _target_is_interface = any(
        n.get("symbol_kind") == "interface" or n.get("type") == "interface"
        for n in graph_nodes
        if n.get("fqn") in matched_fqns
    )

    if not _target_is_interface and graph_edges:
        # Build suffix→FQN lookup for reverse_graph keys (one-time, O(n))
        _rg_suffix_map: dict[str, list[str]] = {}
        for _rg_key in reverse_graph:
            _sfx = _simple_name(_rg_key)
            _rg_suffix_map.setdefault(_sfx, []).append(_rg_key)

        _BRIDGE_SKIP = frozenset({
            "implements", "extends", "contained_in", "annotated_with"
        })

        for _edge in graph_edges:
            if _edge.get("type") != "implements":
                continue
            _from = _edge.get("from") or ""
            if _from not in matched_fqns:
                continue
            # Resolve `to` (may be short name like "OrderService" or full FQN)
            _to_raw = _edge.get("to") or ""
            _to_simple = _simple_name(_to_raw)
            _candidate_iface_keys: list[str] = []
            if _to_raw in reverse_graph:
                _candidate_iface_keys = [_to_raw]
            else:
                _candidate_iface_keys = _rg_suffix_map.get(_to_simple, [])

            for _iface_fqn in _candidate_iface_keys:
                _rg_entry = reverse_graph[_iface_fqn]
                _iface_callers = [
                    c
                    for _etype, _clist in _rg_entry.items()
                    if _etype not in _BRIDGE_SKIP
                    for c in _clist
                    if c not in matched_fqns
                ]
                if not _iface_callers:
                    continue
                _iface_bridging.append({
                    "interface": _iface_fqn,
                    "caller_count": len(_iface_callers),
                })
                for c in _iface_callers:
                    if c not in all_affected:
                        all_affected[c] = 1
                        direct_callers.append(c)
                        if _effective_depth > 1:
                            queue.append((c, 1))

    # BFS for indirect callers
    indirect_callers: list[str] = []
    visited: set[str] = set(matched_fqns) | set(direct_callers)

    while queue:
        node, depth = queue.pop(0)
        if depth >= _effective_depth:
            continue
        for caller in _all_callers_from_rg(node, reverse_graph):
            if caller not in visited:
                visited.add(caller)
                all_affected[caller] = depth + 1
                indirect_callers.append(caller)
                queue.append((caller, depth + 1))

    # Sampled BFS for hub classes: direct BFS was capped at depth=1, so
    # indirect_callers is empty.  Sample _HUB_SAMPLE_SIZE random direct callers,
    # run depth-_HUB_SAMPLE_DEPTH BFS from those, and scale up to estimate reach.
    _indirect_sampled = False
    _indirect_estimated_count: int | None = None
    if _hub_class_guard and direct_callers:
        _n_direct = len(direct_callers)
        _k = min(_HUB_SAMPLE_SIZE, _n_direct)
        _sample_seeds = sorted(direct_callers, key=lambda x: str(x))[:_k]
        _sample_visited: set[str] = set(matched_fqns) | set(direct_callers)
        _sample_queue: list[tuple[str, int]] = [(c, 1) for c in _sample_seeds]
        _sample_indirect: list[str] = []
        while _sample_queue:
            _snode, _sdepth = _sample_queue.pop(0)
            if _sdepth >= _HUB_SAMPLE_DEPTH:
                continue
            for _scaller in _all_callers_from_rg(_snode, reverse_graph):
                if _scaller not in _sample_visited:
                    _sample_visited.add(_scaller)
                    all_affected[_scaller] = _sdepth + 1
                    _sample_indirect.append(_scaller)
                    _sample_queue.append((_scaller, _sdepth + 1))
        if _sample_indirect:
            indirect_callers = _sample_indirect
            _indirect_sampled = True
            # Scale: sample covered _k of _n_direct seeds; extrapolate linearly
            _scale = _n_direct / _k
            _indirect_estimated_count = round(len(_sample_indirect) * _scale)

    # ── 3. Identify affected endpoints from route_surface ─────────────────────
    affected_classes: set[str] = set(matched_fqns) | set(direct_callers) | set(indirect_callers)
    # Expand to enclosing classes of field/method FQNs in affected set.
    affected_with_enclosing: set[str] = affected_classes | {
        _enclosing_class(fqn) for fqn in affected_classes
    }
    # Normalize: extract simple class name from FQN for matching
    affected_simple: set[str] = {_simple_name(fqn) for fqn in affected_with_enclosing}

    endpoints_affected: list[dict] = []
    for ep in route_surface:
        ep_class = ep.get("effective_class") or ep.get("controller") or ep.get("class") or ""
        ep_symbol = ep.get("symbol") or ""
        ep_handler = (
            ep_symbol.split("#", 1)[1] if "#" in ep_symbol
            else ep.get("handler") or ""
        )
        ep_fqn = ep_symbol or (f"{ep_class}#{ep_handler}" if ep_class and ep_handler else ep_class)
        if (
            ep_class in affected_with_enclosing
            or _simple_name(ep_class) in affected_simple
            or ep_fqn in affected_with_enclosing
        ):
            _ep_entry: dict = {
                "method": ep.get("method", ""),
                "path": ep.get("path", ""),
                "class": ep_class,
                "handler": ep_handler,
            }
            if ep.get("security_annotations"):
                _ep_entry["security"] = ep["security_annotations"]
            endpoints_affected.append(_ep_entry)

    # ── 4. Mappers / persistence paths ────────────────────────────────────────
    # Identify class-level @Repository, @Mapper, DAO, mapper_interface nodes
    # in the blast cone.  Method-level symbols are excluded — only class declarations
    # are surfaced so the list stays actionable (one entry per persistence class).
    _MAPPER_ROLES = frozenset({"repository"})
    _MAPPER_NAME_PATTERNS = re.compile(
        r"(?:Repository|Mapper|Dao|DAO|Store|JdbcTemplate|JpaRepository)", re.IGNORECASE
    )
    mappers_affected: list[dict] = []
    _seen_mapper_fqns: set[str] = set()
    for node_dict in graph_nodes:
        fqn = node_dict.get("fqn") or ""
        if not fqn:
            continue
        # Restrict to class/interface-level symbols only (not methods/fields)
        _sym_kind = node_dict.get("symbol_kind") or node_dict.get("type") or ""
        _is_class_level = _sym_kind in (
            "class", "interface", "enum", "mapper_interface", "", "other"
        ) and "#" not in fqn and "." not in fqn.split(".")[-1]
        if not _is_class_level:
            continue
        node_enc = _enclosing_class(fqn)
        in_blast = fqn in affected_with_enclosing or node_enc in affected_with_enclosing
        if not in_blast:
            continue
        role = node_dict.get("role") or ""
        canonical = node_dict.get("canonical_name") or fqn
        symbol_kind = node_dict.get("symbol_kind") or ""
        # Match by role or by name pattern (mapper_interface, DAO, Repository suffixes)
        is_mapper = (
            role in _MAPPER_ROLES
            or symbol_kind == "mapper_interface"
            or bool(_MAPPER_NAME_PATTERNS.search(_simple_name(fqn)))
        )
        if is_mapper and fqn not in _seen_mapper_fqns:
            _seen_mapper_fqns.add(fqn)
            _mapper_entry: dict = {
                "fqn": fqn,
                "role": role or ("mapper" if symbol_kind == "mapper_interface" else "repository"),
                "source_file": node_dict.get("source_file") or "",
            }
            if canonical != fqn:
                _mapper_entry["canonical_name"] = canonical
            mappers_affected.append(_mapper_entry)

    mappers_affected = sorted(mappers_affected, key=lambda m: m["fqn"])[:20]

    # ── 5. Security surface affected ─────────────────────────────────────────
    # Collect distinct security policies from endpoints_affected + any affected classes
    # that carry security annotations in graph_nodes.
    security_surface_affected: list[dict] = []
    _seen_sec_keys: set[str] = set()

    for ep in endpoints_affected:
        sec = ep.get("security")
        if sec and isinstance(sec, dict):
            policy = sec.get("policy") or ""
            roles = sec.get("roles") or sec.get("spec") or ""
            _key = f"{ep.get('path','')}|{policy}|{roles}"
            if _key not in _seen_sec_keys:
                _seen_sec_keys.add(_key)
                security_surface_affected.append({
                    "endpoint": f"{ep.get('method','')} {ep.get('path','')}".strip(),
                    "policy": policy,
                    "roles": roles,
                })

    security_surface_affected = security_surface_affected[:15]

    # ── 6. Cross-module impact ────────────────────────────────────────────────
    # Map affected FQNs → subsystem, count impacted members per subsystem.
    _module_hits: dict[str, int] = {}
    _module_fqns: dict[str, list[str]] = {}
    for fqn in affected_with_enclosing:
        pkg = _canonical_subsystem_pkg(fqn)
        if not pkg:
            continue
        _module_hits[pkg] = _module_hits.get(pkg, 0) + 1
        _module_fqns.setdefault(pkg, []).append(fqn)

    # Enrich with subsystem labels from IR
    _subsys_label_map: dict[str, str] = {}
    for ss in subsystems:
        pkg_key = ss.get("package_prefix") or ss.get("pkg") or ""
        label = ss.get("label") or ss.get("name") or pkg_key
        if pkg_key:
            _subsys_label_map[pkg_key] = label

    cross_module_impact: list[dict] = []
    for pkg, count in sorted(_module_hits.items(), key=lambda x: -x[1]):
        label = _subsys_label_map.get(pkg) or _subsystem_label(pkg)
        cross_module_impact.append({
            "module": label,
            "package_prefix": pkg,
            "affected_symbol_count": count,
        })
    cross_module_impact = cross_module_impact[:10]

    # ── 7. Transactional boundaries touched ───────────────────────────────────
    txn_nodes: list[str] = []
    for node_dict in graph_nodes:
        fqn = node_dict.get("fqn") or ""
        role = node_dict.get("role") or ""
        symbol_kind = node_dict.get("symbol_kind") or ""
        if role == "transaction_boundary" or "Transactional" in (node_dict.get("canonical_name") or ""):
            if fqn in affected_classes or _enclosing_class(fqn) in affected_classes:
                txn_nodes.append(fqn)
        elif symbol_kind == "method" and fqn in affected_classes:
            enc = _enclosing_class(fqn)
            for n2 in graph_nodes:
                if n2.get("fqn") == enc and n2.get("role") == "transaction_boundary":
                    txn_nodes.append(fqn)
                    break

    txn_nodes = sorted(set(txn_nodes))

    # ── 8. Risk score ─────────────────────────────────────────────────────────
    n_direct   = len(direct_callers)
    n_indirect = len(indirect_callers)
    n_ep       = len(endpoints_affected)
    n_txn      = len(txn_nodes)
    n_mappers  = len(mappers_affected)
    n_modules  = len(cross_module_impact)
    n_sec      = len(security_surface_affected)

    raw_score = (
        n_direct * 2.0
        + n_indirect * 0.5
        + n_ep * 3.0
        + n_txn * 2.5
        + n_mappers * 1.5
        + n_modules * 1.0
        + n_sec * 2.0
    )
    risk_score = round(min(raw_score, 100.0), 2)

    if risk_score >= 30 or (n_ep >= 5 and n_txn >= 2):
        risk_level = "critical"
    elif risk_score >= 20 or n_ep >= 3 or n_txn >= 2:
        risk_level = "high"
    elif risk_score >= 5 or n_ep >= 1 or n_txn >= 1:
        risk_level = "medium"
    elif risk_score > 0:
        risk_level = "low"
    else:
        risk_level = "none"

    # ── 9. Confidence score ───────────────────────────────────────────────────
    # Reflects IR completeness and resolution quality.
    # exact match = high; suffix/ambiguous = medium; partial = low
    _res_conf = {"exact": 1.0, "ambiguous": 0.7, "partial": 0.4}.get(resolution, 0.3)
    # Penalize small graphs (< 10 nodes = sparse IR = low confidence)
    _graph_size = len(graph_nodes)
    _graph_conf = min(1.0, _graph_size / 50.0)  # saturates at 50+ nodes
    # Penalize empty reverse graph (no edges = no real traversal)
    _rg_populated = 1.0 if reverse_graph else 0.3
    confidence_score = round((_res_conf * 0.5 + _graph_conf * 0.3 + _rg_populated * 0.2), 2)

    if confidence_score >= 0.75:
        confidence_level = "high"
    elif confidence_score >= 0.45:
        confidence_level = "medium"
    else:
        confidence_level = "low"

    # ── 10. Explanation ───────────────────────────────────────────────────────
    _bfs_truncated = _effective_depth < max_depth

    _parts: list[str] = []
    if n_direct:
        _parts.append(f"{n_direct} direct caller{'s' if n_direct != 1 else ''}")
    if n_indirect:
        _parts.append(f"{n_indirect} indirect caller{'s' if n_indirect != 1 else ''}")
    if n_ep:
        _parts.append(f"{n_ep} endpoint{'s' if n_ep != 1 else ''} exposed")
    if n_txn:
        _parts.append(f"{n_txn} transactional boundary{'s' if n_txn != 1 else ''} touched")
    if n_mappers:
        _parts.append(f"{n_mappers} persistence path{'s' if n_mappers != 1 else ''} in blast cone")
    if n_sec:
        _parts.append(f"{n_sec} security-gated endpoint{'s' if n_sec != 1 else ''} affected")
    if n_modules > 1:
        _parts.append(f"impact crosses {n_modules} modules")

    if _iface_bridging:
        _iface_names = [b["interface"].split(".")[-1] for b in _iface_bridging]
        _parts.append(
            f"callers resolved via interface{'s' if len(_iface_names) > 1 else ''} "
            f"({', '.join(_iface_names)}) — Spring/CDI DI pattern"
        )

    # Transparency: hub-class BFS truncation must appear in explanation so the
    # text and JSON are semantically identical.
    if _bfs_truncated:
        if _indirect_sampled and _indirect_estimated_count is not None:
            _parts.append(
                f"indirect callers sampled ({_HUB_SAMPLE_SIZE} of {n_direct} seeds, "
                f"depth={_HUB_SAMPLE_DEPTH}): {n_indirect} found in sample, "
                f"~{_indirect_estimated_count} estimated total"
            )
        else:
            _parts.append(
                f"indirect BFS skipped (hub class: {n_direct} direct callers "
                f"exceed {_HUB_CALLER_THRESHOLD} threshold; no indirect callers reachable "
                "from sample — graph may be a terminal sink)"
            )

    if not _parts:
        explanation = f"No callers or dependents found for {target!r}. Low-risk isolated change."
    else:
        explanation = f"Risk={risk_level.upper()}: {'; '.join(_parts)}."
        if confidence_level != "high":
            explanation += f" (confidence={confidence_level}: IR may be incomplete)"

    # ── 11. Assemble output ───────────────────────────────────────────────────
    _indirect_summary = sorted(indirect_callers, key=lambda x: all_affected.get(x, 99))[:50]

    out: dict = {
        "target": target,
        "matched_fqns": list(sorted(matched_fqns)),
        "resolution": resolution,
        "risk_score": risk_score,
        "risk_level": risk_level,
        "confidence_score": confidence_score,
        "confidence_level": confidence_level,
        "explanation": explanation,
        "direct_callers": sorted(direct_callers)[:30],
        "indirect_callers": _indirect_summary,
        "endpoints_affected": endpoints_affected,
        "mappers_affected": mappers_affected,
        "security_surface_affected": security_surface_affected,
        "cross_module_impact": cross_module_impact,
        "transactional_boundaries_touched": txn_nodes,
        "depth_reached": _effective_depth,  # actual BFS depth used, not the requested max
        "bfs_truncated": _bfs_truncated,
        "stats": {
            "direct_caller_count": n_direct,
            "indirect_caller_count": n_indirect,
            "indirect_callers_computed": not _bfs_truncated or _indirect_sampled,
            "indirect_callers_sampled": _indirect_sampled,
            "endpoints_affected_count": n_ep,
            "transactional_boundaries_count": n_txn,
            "mappers_affected_count": n_mappers,
            "modules_affected_count": n_modules,
            "security_surface_count": n_sec,
        },
    }
    if _indirect_sampled and _indirect_estimated_count is not None:
        out["indirect_callers_estimated_count"] = _indirect_estimated_count
        out["indirect_callers_sample_note"] = (
            f"indirect_callers contains a sample (BFS depth={_HUB_SAMPLE_DEPTH} from "
            f"{min(_HUB_SAMPLE_SIZE, n_direct)} of {n_direct} direct callers). "
            f"Estimated total indirect reach: ~{_indirect_estimated_count}. "
            "Actual count may differ; use a lower-fan-in entry point for exact traversal."
        )
    if _candidates_out:
        out["candidates"] = _candidates_out
    if _iface_bridging:
        out["via_interface_resolution"] = _iface_bridging
        out["via_interface_note"] = (
            "Target is a concrete class injected via interface(s) in DI frameworks "
            "(Spring/CDI/Guice). direct_callers includes callers of the implemented "
            "interface(s) — these are the real production dependents."
        )
    if _bfs_truncated:
        out["bfs_truncation_reason"] = "hub_class_depth_cap"
        if _indirect_sampled:
            out["bfs_truncation_note"] = (
                f"Full BFS capped at depth=1 (hub class: {n_direct} direct callers "
                f">{_HUB_CALLER_THRESHOLD}). indirect_callers is a sampled estimate — "
                f"BFS from {min(_HUB_SAMPLE_SIZE, n_direct)} random seeds at depth={_HUB_SAMPLE_DEPTH}."
            )
        else:
            out["bfs_truncation_note"] = (
                f"Indirect BFS capped at depth=1: target has {n_direct} direct callers "
                f"(>{_HUB_CALLER_THRESHOLD} threshold). indirect_callers is empty — "
                "no indirect callers reachable from sampled seeds (terminal sink or sparse graph). "
                "Use a lower-fan-in entry point for full transitive traversal."
            )
    if len(direct_callers) > 30:
        out["direct_callers_note"] = (
            f"Showing 30/{n_direct} direct callers. Use --output to inspect full IR."
        )
    if len(indirect_callers) > 50:
        out["indirect_callers_note"] = (
            f"Showing 50/{n_indirect} indirect callers. Use --output to inspect full IR."
        )
    return out


def _blast_radius_candidates(
    target: str,
    reverse_graph: dict[str, dict[str, list[str]]],
    graph_nodes: list[dict],
) -> list[dict]:
    """Return up to 10 candidate FQNs ordered by relevance for fuzzy target matches.

    Ranking: exact suffix > partial name > in-degree (fan-in = likely important class).
    """
    t_lower = target.strip().lower()
    if t_lower.endswith(".java"):
        t_lower = t_lower[:-5]
    simple_lower = t_lower.split(".")[-1]

    all_fqns: list[str] = list({n["fqn"] for n in graph_nodes if "fqn" in n})
    # Build in-degree map for relevance ranking
    in_deg: dict[str, int] = {}
    for entry in reverse_graph.values():
        for callers in entry.values():
            for c in callers:
                in_deg[c] = in_deg.get(c, 0) + 1

    scored: list[tuple[float, str]] = []
    for fqn in all_fqns:
        fqn_lower = fqn.lower()
        simple_fqn = _simple_name(fqn).lower()
        score = 0.0
        if simple_fqn == simple_lower:
            score += 10.0   # exact simple-name match
        elif simple_lower in simple_fqn:
            score += 5.0    # prefix/suffix match
        elif simple_lower in fqn_lower:
            score += 2.0    # substring
        if score == 0.0:
            continue
        score += min(in_deg.get(fqn, 0) * 0.1, 2.0)  # fan-in bonus (capped)
        scored.append((score, fqn))

    scored.sort(key=lambda x: (-x[0], x[1]))
    return [
        {"fqn": fqn, "relevance_score": round(s, 2)}
        for s, fqn in scored[:10]
    ]


def _resolve_target(
    target: str,
    reverse_graph: dict[str, dict[str, list[str]]],
    graph_nodes: list[dict],
) -> tuple[str, set[str]]:
    """Resolve a user-provided target string to a set of matching FQNs.

    Resolution order:
      1. Exact FQN match in reverse_graph keys
      2. Graph-nodes lookup (fqn exact, then simple-name suffix)
      3. Suffix match on reverse_graph keys (. separator)
      4. File stem match (convert path to class-name candidate)

    Returns (resolution_type, set_of_fqns).
    """
    # Normalize: strip .java suffix if given a path
    t = target.strip()
    _is_path_like = "/" in t or "\\" in t or t.endswith(".java")
    if t.endswith(".java"):
        t = t[:-5]
    # When given a file path (e.g. src/main/java/org/foo/Bar), extract the class
    # name (basename) so suffix matching resolves it to the correct FQN.
    # Path-to-dot conversion (src.main.java.org.foo.Bar) never matches an IR FQN.
    if _is_path_like and ("/" in t or "\\" in t):
        import os.path as _osp
        t = _osp.basename(t.replace("\\", "/"))
    t_class = t.replace("/", ".").replace("\\", ".")

    # 1. Exact match
    if t in reverse_graph:
        return "exact", {t}
    if t_class != t and t_class in reverse_graph:
        return "exact", {t_class}

    # 2. Exact match from graph_nodes
    all_fqns: set[str] = {n["fqn"] for n in graph_nodes if "fqn" in n}
    if t in all_fqns:
        return "exact", {t}

    # 3. Suffix match — e.g. "DefaultKeycloakSession" → "org.keycloak.services.DefaultKeycloakSession"
    simple = t.split(".")[-1]  # last segment
    suffix_matches: set[str] = set()
    for fqn in reverse_graph:
        if fqn.split(".")[-1] == simple or fqn.endswith(f".{simple}"):
            suffix_matches.add(fqn)
    # Also check graph_nodes (nodes not in reverse_graph = no callers, but still valid targets)
    for fqn in all_fqns:
        if fqn.split(".")[-1] == simple or fqn.endswith(f".{simple}"):
            suffix_matches.add(fqn)
    if suffix_matches:
        resolution = "exact" if len(suffix_matches) == 1 else "ambiguous"
        return resolution, suffix_matches

    # 4. Partial substring match (last resort)
    partial: set[str] = set()
    t_lower = simple.lower()
    for fqn in all_fqns:
        if t_lower in fqn.lower():
            partial.add(fqn)
    if partial:
        return "partial", partial

    return "not_found", set()


_BLAST_SKIP_EDGE_TYPES: frozenset[str] = frozenset({"contained_in", "imports"})
# 'contained_in': structural membership (method→enclosing class), not a caller.
# 'imports':      Java import statements — any class that references the type in
#                 an import declaration, including those that only use it as a
#                 method-return type or catch-block type.  Import presence does NOT
#                 imply a runtime dependency; including it produces false-positive
#                 callers (e.g. sibling service classes that share a utility interface).
#                 Consistent with spring_impact._SKIP_EDGE_TYPES.


def _all_callers_from_rg(fqn: str, reverse_graph: dict[str, dict[str, list[str]]]) -> list[str]:
    """Return all callers of fqn from the reverse graph (all edge types).

    BUG-01 fix: skip 'contained_in' edges — those represent structural membership
    (method→enclosing class), not actual callers.  Without this, an Impl class
    with 91 own methods would show 91 "direct callers" and inflate risk to HIGH.

    CH-002 fix: for 'injects' edges, normalize field/constructor FQNs to their
    enclosing class.  e.g. pkg.ConsolidacionService.calcularField → pkg.ConsolidacionService
    so BFS can continue through DI injection chains and find controllers.

    FP-001 fix: skip 'imports' edges — import declarations are not runtime
    dependencies and produce false-positive callers (e.g. sibling classes that share
    a utility interface but don't call the target).
    """
    entry = reverse_graph.get(fqn) or {}
    callers: list[str] = []
    seen: set[str] = set()
    for edge_type, fqn_list in entry.items():
        if edge_type in _BLAST_SKIP_EDGE_TYPES:
            continue
        for c in fqn_list:
            normalized = _normalize_owner_fqn(c) if edge_type == "injects" else c
            if normalized not in seen:
                seen.add(normalized)
                callers.append(normalized)
    return callers


def _simple_name(fqn: str) -> str:
    """Extract the simple class name from a fully-qualified name."""
    return fqn.split(".")[-1].split("#")[0]

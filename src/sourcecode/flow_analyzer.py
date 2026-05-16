"""flow_analyzer.py — Evidence-based execution path extraction for PR context.

Builds Entry → Service → Repository → EndState ordered sequences using ONLY
direct code evidence: field injection, constructor params, type annotations,
method calls, explicit instantiation.

V3: execution_paths with runtime_notes — conditional branches, optional execution,
and async side-effects are surfaced when explicit code signals exist.
No inference, no naming, no invented behavior.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Optional

_ENTRY_ARTIFACT_TYPES = frozenset({"controller", "entrypoint"})
_SERVICE_ARTIFACT_TYPES = frozenset({"service"})
_REPO_ARTIFACT_TYPES = frozenset({"repository", "mapper"})

_DB_KEYWORDS = frozenset({"repository", "dao", "mapper", "store", "jpa", "jdbc", "sql"})
_EVENT_KEYWORDS = frozenset({"event", "publish", "emit", "kafka", "queue", "rabbit", "sns", "bus"})

_HTTP_ENTRY_RE = re.compile(
    r'@(?:Get|Post|Put|Delete|Patch|Request)Mapping[^)]*\)'
    r'|@(?:Get|Post|Put|Delete|Patch)\([^)]*\)'
    r'|@\w+\.(?:get|post|put|delete|patch)\([^)]*\)',
    re.IGNORECASE,
)
_METHOD_NAME_RE = re.compile(
    r'(?:public\s+|async\s+|def\s+|function\s+)*'
    r'(?:[\w<>\[\]]+\s+)?'
    r'(\w+)\s*\(',
)

# Runtime signal patterns: (compiled_regex, note_text)
# Only signals with explicit code evidence — no inference.
# Three categories: condition | branch | async
_RUNTIME_SIGNALS: list[tuple[re.Pattern, str]] = [
    # ── Conditional / auth guards ─────────────────────────────────────────────
    (re.compile(r'@PreAuthorize|@Secured|@RolesAllowed', re.IGNORECASE),
     "condition: authorization check present (@PreAuthorize / @Secured)"),
    (re.compile(r'isAuthenticated\(\)|hasRole\(|hasAuthority\(|SecurityContextHolder', re.IGNORECASE),
     "condition: reads authentication context"),
    (re.compile(r'featureFlag|FeatureToggle|\.isEnabled\s*\(|\.isActive\s*\(', re.IGNORECASE),
     "condition: feature flag gates execution"),
    # Null/empty guard with early return — matches if (...null/empty...) return/throw on same line
    (re.compile(r'if\s*\([^)]*(?:==\s*null|!=\s*null|isEmpty\s*\(\)|isBlank\s*\(\))[^)]*\)'
                r'\s*(?:\{?\s*)?(?:return|throw)\b', re.IGNORECASE),
     "condition: null/empty guard with early return"),

    # ── Optional execution / branching ────────────────────────────────────────
    (re.compile(r'@Cacheable|@CacheEvict|@CachePut', re.IGNORECASE),
     "branch: Spring cache may short-circuit downstream call"),
    (re.compile(r'\.getIfPresent\s*\(|cache\.get\s*\(|cacheManager\.', re.IGNORECASE),
     "branch: manual cache lookup may short-circuit"),
    (re.compile(r'Optional\s*<|\.orElseThrow\s*\(|\.orElseGet\s*\(|\.orElse\s*\(', re.IGNORECASE),
     "branch: result may be absent (Optional)"),

    # ── Async / side effects ──────────────────────────────────────────────────
    (re.compile(r'@Async\b'),
     "async: runs in separate thread (@Async)"),
    (re.compile(r'CompletableFuture|\.supplyAsync\s*\(|\.runAsync\s*\('),
     "async: non-blocking future-based execution"),
    (re.compile(r'\basync\s+def\b|\bawait\b', re.IGNORECASE),
     "async: non-blocking (async/await)"),
    (re.compile(r'publishEvent\s*\(|applicationEventPublisher|eventPublisher\.', re.IGNORECASE),
     "async: Spring application event emitted"),
    (re.compile(r'kafkaTemplate\.|KafkaProducer|@KafkaListener', re.IGNORECASE),
     "async: Kafka message produced"),
    (re.compile(r'rabbitTemplate\.|amqpTemplate\.|@RabbitListener', re.IGNORECASE),
     "async: RabbitMQ message sent"),
]


def _detect_lang(path: str) -> str:
    return {
        ".java": "java", ".kt": "kotlin",
        ".py": "python",
        ".ts": "typescript", ".tsx": "typescript",
        ".js": "javascript", ".jsx": "javascript",
        ".go": "go", ".cs": "csharp", ".rb": "ruby", ".php": "php",
    }.get(Path(path).suffix.lower(), "unknown")


def _strip_comments(content: str, lang: str) -> str:
    content = re.sub(r"/\*.*?\*/", " ", content, flags=re.DOTALL)
    content = re.sub(r"//[^\n]*", " ", content)
    if lang in ("python", "ruby", "go"):
        content = re.sub(r"#[^\n]*", " ", content)
    return content


def _read_safe(root: Path, rel_path: str) -> str:
    try:
        return (root / rel_path).read_text(encoding="utf-8", errors="ignore")
    except (OSError, ValueError):
        return ""


def _collect_runtime_notes(content: str, lang: str) -> list[str]:
    """Scan comment-stripped content for explicit runtime behavior signals.

    Returns only notes backed by a direct code pattern match.
    Returns [] when no signals are found.
    """
    clean = _strip_comments(content, lang)
    notes: list[str] = []
    seen: set[str] = set()
    for pattern, note in _RUNTIME_SIGNALS:
        if note not in seen and pattern.search(clean):
            notes.append(note)
            seen.add(note)
    return notes


def _find_entry_method(clean: str) -> Optional[str]:
    m = _HTTP_ENTRY_RE.search(clean)
    if not m:
        return None
    after = clean[m.end():]
    mn = _METHOD_NAME_RE.match(after.lstrip())
    if mn:
        name = mn.group(1)
        if name.lower() not in ("public", "async", "def", "function", "void", "override"):
            return name
    return None


def _build_field_map(clean: str) -> dict[str, str]:
    """Map field_name_lower → ClassName from injection patterns."""
    fmap: dict[str, str] = {}
    for m in re.finditer(r"private\s+(\w+)(?:<[^>]+>)?\s+(\w+)\s*[;=,)]", clean):
        fmap[m.group(2).lower()] = m.group(1)
    for m in re.finditer(r"(?:private|protected|readonly)\s+(\w+)\s*:\s*(\w+)", clean):
        fmap[m.group(1).lower()] = m.group(2)
    for m in re.finditer(r"self\.(\w+)\s*=\s*(\w+)\s*\(", clean):
        fmap[m.group(1).lower()] = m.group(2)
    return fmap


def _find_called_method(clean: str, class_name: str, fmap: dict[str, str]) -> Optional[str]:
    fields = [f for f, t in fmap.items() if t.lower() == class_name.lower()]
    for field in fields:
        pat = rf"\bthis\.{re.escape(field)}\.(\w+)\s*\(|\b{re.escape(field)}\.(\w+)\s*\("
        for m in re.finditer(pat, clean, re.IGNORECASE):
            name = m.group(1) or m.group(2)
            if name and name.lower() not in ("class", "new", "super", "get", "set"):
                return name
    for m in re.finditer(rf"\b{re.escape(class_name)}\.(\w+)\s*\(", clean, re.IGNORECASE):
        name = m.group(1)
        if name.lower() not in ("class", "new", "super"):
            return name
    return None


def _has_code_evidence(clean: str, class_name: str) -> bool:
    """True only when class_name has direct code evidence in pre-stripped content."""
    esc = re.escape(class_name)
    if re.search(rf"\b(?:private|protected)\s+{esc}\b", clean, re.IGNORECASE):
        return True
    if re.search(rf"[,(]\s*{esc}\s+\w+", clean, re.IGNORECASE):
        return True
    if re.search(rf":\s*{esc}\b", clean, re.IGNORECASE):
        return True
    if re.search(rf"\bnew\s+{esc}\s*\(", clean, re.IGNORECASE):
        return True
    if re.search(rf"\b{esc}\s*\(", clean):
        return True
    if re.search(rf"\b{esc}\b", clean, re.IGNORECASE):
        non_import = re.search(
            rf"^(?!\s*(?:import|require|from|//|#|\*)\b).*\b{esc}\b",
            clean, re.IGNORECASE | re.MULTILINE,
        )
        if non_import:
            return True
    return False


def _find_evidenced_ordered(
    root: Path,
    source_path: str,
    candidates: list[str],
) -> list[tuple[str, Optional[str]]]:
    """Return (class_name, method_or_None) for candidates with direct code evidence,
    ordered by their first appearance position in the source file."""
    content = _read_safe(root, source_path)
    if not content:
        return []
    lang = _detect_lang(source_path)
    clean = _strip_comments(content, lang)
    fmap = _build_field_map(clean)

    positioned: list[tuple[int, str, Optional[str]]] = []
    for cand_path in candidates:
        class_name = Path(cand_path).stem
        if not _has_code_evidence(clean, class_name):
            continue
        method = _find_called_method(clean, class_name, fmap)
        m = re.search(rf"\b{re.escape(class_name)}\b", clean, re.IGNORECASE)
        pos = m.start() if m else len(clean)
        positioned.append((pos, class_name, method))

    positioned.sort(key=lambda x: x[0])
    return [(cls, meth) for _, cls, meth in positioned]


def _detect_end_state(path: list[str]) -> str:
    for step in path:
        s = step.lower()
        if any(kw in s for kw in _DB_KEYWORDS):
            return "DB write"
        if any(kw in s for kw in _EVENT_KEYWORDS):
            return "event emitted"
    return "HTTP response"


def _step_label(class_name: str, method: Optional[str]) -> str:
    return f"{class_name}.{method}" if method else class_name


def _path_name(entry_class: str) -> str:
    domain = re.sub(
        r"(?:RestController|Controller|Resource|Handler|Api|Endpoint|Router|Servlet)$",
        "", entry_class, flags=re.IGNORECASE,
    )
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", domain).strip()


def analyze_execution_paths(
    changed_files: list[str],
    all_paths: list[str],
    root: Path,
    classify_fn: Callable[[str], dict],
    max_paths: int = 3,
) -> list[dict]:
    """Build ordered execution paths with runtime behavior signals.

    Each path:
    - One service per entry point (most evident, earliest-referenced)
    - Each step requires direct code evidence
    - runtime_notes populated from explicit code signals only (never inferred)
    - Forward-only: Controller → Service → Repository

    Returns list of: {name, entry_point, path, runtime_notes, end_state}
    Returns [] when no verifiable path exists.
    """
    entry_files = [
        f for f in changed_files
        if classify_fn(f)["artifact_type"] in _ENTRY_ARTIFACT_TYPES
    ]
    if not entry_files:
        return []

    all_services = [p for p in all_paths if classify_fn(p)["artifact_type"] in _SERVICE_ARTIFACT_TYPES]
    all_repos = [p for p in all_paths if classify_fn(p)["artifact_type"] in _REPO_ARTIFACT_TYPES]

    result: list[dict] = []

    for entry_path in entry_files[:max_paths]:
        entry_class = Path(entry_path).stem
        lang = _detect_lang(entry_path)

        entry_content = _read_safe(root, entry_path)
        entry_clean = _strip_comments(entry_content, lang) if entry_content else ""
        entry_method = _find_entry_method(entry_clean) if entry_clean else None
        entry_point_str = _step_label(entry_class, entry_method)

        evidenced_svcs = _find_evidenced_ordered(root, entry_path, all_services)
        if not evidenced_svcs:
            continue

        svc_class, svc_method = evidenced_svcs[0]
        svc_label = _step_label(svc_class, svc_method)

        svc_path = next((p for p in all_services if Path(p).stem == svc_class), None)
        svc_content = _read_safe(root, svc_path) if svc_path else ""
        svc_lang = _detect_lang(svc_path) if svc_path else "unknown"

        # Service step — notes scoped to service file only
        path_items: list[dict] = [
            {"step": svc_label,
             "notes": _collect_runtime_notes(svc_content, svc_lang) if svc_content else []},
        ]

        # Repository step — notes scoped to repo file only
        if svc_path:
            evidenced_repos = _find_evidenced_ordered(root, svc_path, all_repos)
            if evidenced_repos:
                repo_class, repo_method = evidenced_repos[0]
                repo_label = _step_label(repo_class, repo_method)
                repo_path = next((p for p in all_repos if Path(p).stem == repo_class), None)
                repo_content = _read_safe(root, repo_path) if repo_path else ""
                repo_lang = _detect_lang(repo_path) if repo_path else "unknown"
                path_items.append(
                    {"step": repo_label,
                     "notes": _collect_runtime_notes(repo_content, repo_lang) if repo_content else []},
                )

        # Entry-point notes scoped to controller file
        entry_notes = _collect_runtime_notes(entry_content, lang) if entry_content else []

        result.append({
            "name": _path_name(entry_class),
            "entry_point": {"step": entry_point_str, "notes": entry_notes},
            "path": path_items,
            "end_state": _detect_end_state([item["step"] for item in path_items]),
        })

    return result

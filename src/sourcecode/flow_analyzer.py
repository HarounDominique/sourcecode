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


_EVIDENCE_PRIORITY: dict[str, int] = {
    "none": 0, "heuristic_only": 1, "direct_call": 2, "direct_injection": 3,
}
_EVIDENCE_STRONG = frozenset({"direct_call", "direct_injection"})


def _classify_evidence_type(clean: str, class_name: str) -> str:
    """Return how class_name is referenced in pre-stripped content."""
    esc = re.escape(class_name)
    if re.search(rf"\b(?:private|protected)\s+{esc}\b", clean, re.IGNORECASE):
        return "direct_injection"
    if re.search(rf"[,(]\s*{esc}\s+\w+", clean, re.IGNORECASE):
        return "direct_injection"
    if re.search(rf":\s*{esc}\b", clean, re.IGNORECASE):
        return "direct_injection"
    if re.search(rf"\bnew\s+{esc}\s*\(", clean, re.IGNORECASE):
        return "direct_call"
    if re.search(rf"\b{esc}\s*\(", clean):
        return "direct_call"
    non_import = re.search(
        rf"^(?!\s*(?:import|require|from|//|#|\*)\b).*\b{esc}\b",
        clean, re.IGNORECASE | re.MULTILINE,
    )
    if non_import:
        return "heuristic_only"
    return "none"


def _worst_evidence(levels: list[str]) -> str:
    return min(levels, key=lambda x: _EVIDENCE_PRIORITY.get(x, 0)) if levels else "none"


def _compute_confidence(evidence_level: str, trace_len: int) -> str:
    if evidence_level not in _EVIDENCE_STRONG:
        return "low"
    return "high" if trace_len >= 2 else "medium"


def _build_trace_step(source_class: str, target_class: str, evidence_type: str) -> str:
    verb = "injects" if evidence_type == "direct_injection" else "calls"
    return f"{source_class} {verb} {target_class}"


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


# ── Behavioral impact helpers ─────────────────────────────────────────────────

def _domain_from_class(class_name: str) -> str:
    """Extract human-readable domain noun from a class name."""
    stripped = re.sub(
        r"(?i)(?:repository|repo|dao|mapper|store|service|manager|handler|helper|"
        r"impl|controller|api|resource|endpoint|facade)$",
        "", class_name,
    )
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", stripped).strip().lower()


def _impact_item(statement: str, support: str, certainty: str) -> dict:
    truth_level = "observed" if certainty == "high" else "inferred"
    return {"statement": statement, "support": support, "certainty": certainty, "truth_level": truth_level}


def _impact_descriptions(
    changed_class: str,
    changed_type: str,
    end_state: str,
    ctrl_clean: str,
    evidence_level: str,
) -> list[dict]:
    domain = _domain_from_class(changed_class)
    certainty = "medium" if evidence_level in _EVIDENCE_STRONG else "low"
    items: list[dict] = []

    if changed_type in _REPO_ARTIFACT_TYPES:
        items.append(_impact_item(
            f"{domain} persistence may be affected (inferred from path)" if domain else "persistence may be affected (inferred from path)",
            f"{changed_class} classified as repository from path",
            certainty,
        ))
    elif changed_type in _SERVICE_ARTIFACT_TYPES:
        if end_state == "DB write":
            items.append(_impact_item(
                f"{domain} persistence may be affected (repository with DB write in path)" if domain else "persistence may be affected (repository with DB write in path)",
                f"{changed_class} delegates to repository with DB write",
                certainty,
            ))
        else:
            items.append(_impact_item(
                f"{domain} behavior may change" if domain else "behavior may change",
                f"{changed_class} is a service in path",
                certainty,
            ))
    else:
        items.append(_impact_item(
            f"{domain} behavior may change" if domain else "behavior may change",
            f"{changed_class} is in path",
            certainty,
        ))

    if re.search(r"@PreAuthorize|@Secured|@RolesAllowed|hasRole\(|isAuthenticated", ctrl_clean, re.IGNORECASE):
        items.append(_impact_item(
            "authorization check present on entry point",
            "security annotation detected on controller",
            "high",
        ))

    if re.search(r"@Transactional\b", ctrl_clean):
        items.append(_impact_item(
            "transactional boundary in path",
            "@Transactional detected on entry point",
            "high",
        ))

    return items[:3]


def _impact_descriptions_for_controller(
    affected_path: list[str],
    end_state: str,
    ctrl_clean: str,
    evidence_level: str,
) -> list[dict]:
    certainty = "medium" if evidence_level in _EVIDENCE_STRONG else "low"
    items: list[dict] = []

    if end_state == "DB write":
        domain = ""
        for step in reversed(affected_path):
            base = step.split(".")[0]
            d = _domain_from_class(base)
            if d:
                domain = d
                break
        items.append(_impact_item(
            f"{domain} persistence may be affected (repository with DB write in path)" if domain else "data persistence may be affected (repository with DB write in path)",
            "repository with DB write detected in path",
            certainty,
        ))
    else:
        items.append(_impact_item(
            "request handler behavior may change",
            "controller entry point modified",
            certainty,
        ))

    if re.search(r"@PreAuthorize|@Secured|@RolesAllowed|hasRole\(|isAuthenticated", ctrl_clean, re.IGNORECASE):
        items.append(_impact_item(
            "authorization check present on entry point",
            "security annotation detected on controller",
            "high",
        ))

    if re.search(r"@Transactional\b", ctrl_clean):
        items.append(_impact_item(
            "transactional boundary in path",
            "@Transactional detected on controller",
            "high",
        ))

    return items[:3]


def analyze_behavioral_impact(
    changed_files: list[str],
    all_paths: list[str],
    root: Path,
    classify_fn: Callable[[str], dict],
    max_impacts: int = 3,
) -> list[dict]:
    """Build behavioral impact entries for PR review.

    For changed controllers: forward traversal → service → repository.
    For changed services/repos/domain: reverse lookup → find callers → build causal path.

    Each entry: {entry_point, affected_path, impact, end_state}
    All paths require direct code evidence — no naming/module inference.
    Returns [] when no verifiable causal path exists.
    """
    entry_changed = [f for f in changed_files if classify_fn(f)["artifact_type"] in _ENTRY_ARTIFACT_TYPES]
    non_entry_changed = [f for f in changed_files if classify_fn(f)["artifact_type"] not in _ENTRY_ARTIFACT_TYPES]

    all_entries = [p for p in all_paths if classify_fn(p)["artifact_type"] in _ENTRY_ARTIFACT_TYPES]
    all_services = [p for p in all_paths if classify_fn(p)["artifact_type"] in _SERVICE_ARTIFACT_TYPES]
    all_repos = [p for p in all_paths if classify_fn(p)["artifact_type"] in _REPO_ARTIFACT_TYPES]

    result: list[dict] = []
    seen_entries: set[str] = set()

    # Case 1: changed controllers — forward traversal
    for entry_path in entry_changed:
        if len(result) >= max_impacts:
            break
        entry_class = Path(entry_path).stem
        if entry_class in seen_entries:
            continue
        lang = _detect_lang(entry_path)
        ctrl_content = _read_safe(root, entry_path)
        if not ctrl_content:
            continue
        ctrl_clean = _strip_comments(ctrl_content, lang)
        entry_method = _find_entry_method(ctrl_clean)
        entry_str = _step_label(entry_class, entry_method)

        evidenced_svcs = _find_evidenced_ordered(root, entry_path, all_services)
        if not evidenced_svcs:
            continue

        svc_class, svc_method = evidenced_svcs[0]
        svc_evidence = _classify_evidence_type(ctrl_clean, svc_class)
        affected_path = [_step_label(svc_class, svc_method)]
        trace = [_build_trace_step(entry_class, svc_class, svc_evidence)]
        evidence_levels = [svc_evidence]

        svc_path = next((p for p in all_services if Path(p).stem == svc_class), None)
        if svc_path:
            svc_content_raw = _read_safe(root, svc_path)
            if svc_content_raw:
                svc_clean_raw = _strip_comments(svc_content_raw, _detect_lang(svc_path))
                evidenced_repos = _find_evidenced_ordered(root, svc_path, all_repos)
                if evidenced_repos:
                    repo_class, repo_method = evidenced_repos[0]
                    repo_evidence = _classify_evidence_type(svc_clean_raw, repo_class)
                    affected_path.append(_step_label(repo_class, repo_method))
                    trace.append(_build_trace_step(svc_class, repo_class, repo_evidence))
                    evidence_levels.append(repo_evidence)

        end_state = _detect_end_state(affected_path)
        evidence_level = _worst_evidence(evidence_levels)
        confidence = _compute_confidence(evidence_level, len(trace))
        seen_entries.add(entry_class)
        result.append({
            "entry_point": entry_str,
            "affected_path": affected_path,
            "impact": _impact_descriptions_for_controller(affected_path, end_state, ctrl_clean, evidence_level),
            "end_state": end_state,
            "confidence": confidence,
            "evidence_level": evidence_level,
            "trace": trace,
        })

    # Case 2: changed non-controllers — reverse lookup
    for changed_path in non_entry_changed:
        if len(result) >= max_impacts:
            break
        changed_class = Path(changed_path).stem
        changed_type = classify_fn(changed_path)["artifact_type"]

        for ctrl_path in all_entries:
            if len(result) >= max_impacts:
                break
            ctrl_class = Path(ctrl_path).stem
            if ctrl_class in seen_entries:
                continue
            ctrl_content = _read_safe(root, ctrl_path)
            if not ctrl_content:
                continue
            ctrl_lang = _detect_lang(ctrl_path)
            ctrl_clean = _strip_comments(ctrl_content, ctrl_lang)

            affected_path: list[str] = []
            trace: list[str] = []
            evidence_levels: list[str] = []

            if _has_code_evidence(ctrl_clean, changed_class):
                # Direct: controller → changed class
                ctrl_to_changed = _classify_evidence_type(ctrl_clean, changed_class)
                fmap = _build_field_map(ctrl_clean)
                method = _find_called_method(ctrl_clean, changed_class, fmap)
                affected_path.append(_step_label(changed_class, method))
                trace.append(_build_trace_step(ctrl_class, changed_class, ctrl_to_changed))
                evidence_levels.append(ctrl_to_changed)

                if changed_type in _SERVICE_ARTIFACT_TYPES:
                    changed_content = _read_safe(root, changed_path)
                    changed_clean = _strip_comments(changed_content, _detect_lang(changed_path)) if changed_content else ""
                    evidenced_repos = _find_evidenced_ordered(root, changed_path, all_repos)
                    if evidenced_repos:
                        rclass, rmethod = evidenced_repos[0]
                        repo_evidence = _classify_evidence_type(changed_clean, rclass)
                        affected_path.append(_step_label(rclass, rmethod))
                        trace.append(_build_trace_step(changed_class, rclass, repo_evidence))
                        evidence_levels.append(repo_evidence)
            else:
                # Indirect: controller → mediating service → changed class
                for svc_class, svc_method in _find_evidenced_ordered(root, ctrl_path, all_services):
                    svc_p = next((p for p in all_services if Path(p).stem == svc_class), None)
                    if not svc_p:
                        continue
                    svc_content = _read_safe(root, svc_p)
                    if not svc_content:
                        continue
                    svc_lang = _detect_lang(svc_p)
                    svc_clean = _strip_comments(svc_content, svc_lang)
                    if not _has_code_evidence(svc_clean, changed_class):
                        continue

                    ctrl_to_svc = _classify_evidence_type(ctrl_clean, svc_class)
                    svc_to_changed = _classify_evidence_type(svc_clean, changed_class)
                    fmap = _build_field_map(svc_clean)
                    method = _find_called_method(svc_clean, changed_class, fmap)
                    affected_path = [_step_label(svc_class, svc_method), _step_label(changed_class, method)]
                    trace = [
                        _build_trace_step(ctrl_class, svc_class, ctrl_to_svc),
                        _build_trace_step(svc_class, changed_class, svc_to_changed),
                    ]
                    evidence_levels = [ctrl_to_svc, svc_to_changed]

                    if changed_type in _SERVICE_ARTIFACT_TYPES:
                        changed_content = _read_safe(root, changed_path)
                        changed_clean = _strip_comments(changed_content, _detect_lang(changed_path)) if changed_content else ""
                        evidenced_repos = _find_evidenced_ordered(root, changed_path, all_repos)
                        if evidenced_repos:
                            rclass, rmethod = evidenced_repos[0]
                            repo_evidence = _classify_evidence_type(changed_clean, rclass)
                            affected_path.append(_step_label(rclass, rmethod))
                            trace.append(_build_trace_step(changed_class, rclass, repo_evidence))
                            evidence_levels.append(repo_evidence)
                    break

            if not affected_path:
                continue

            entry_method = _find_entry_method(ctrl_clean)
            end_state = _detect_end_state(affected_path)
            evidence_level = _worst_evidence(evidence_levels)
            confidence = _compute_confidence(evidence_level, len(trace))
            seen_entries.add(ctrl_class)
            result.append({
                "entry_point": _step_label(ctrl_class, entry_method),
                "affected_path": affected_path,
                "impact": _impact_descriptions(changed_class, changed_type, end_state, ctrl_clean, evidence_level),
                "end_state": end_state,
                "confidence": confidence,
                "evidence_level": evidence_level,
                "trace": trace,
            })

    return result

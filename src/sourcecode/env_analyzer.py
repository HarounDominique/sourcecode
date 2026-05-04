from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Optional

_MAX_FILES_PER_KEY = 10
_MAX_KEYS = 200
_MAX_FILE_SIZE = 512 * 1024  # 512 KB

_SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv",
    ".mypy_cache", "dist", "build", ".tox", ".eggs", "coverage",
    ".next", ".nuxt", ".output", "vendor",
}

_CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs",
    ".go", ".rb", ".java", ".php", ".rs", ".sh", ".bash",
}

_ENV_EXAMPLE_NAMES = {
    ".env.example", ".env.sample", ".env.template", ".env.dist",
    "example.env", "sample.env",
}

# Spring Boot application.properties / application.yml and their profile variants
_SPRING_CONF_BASE = {"application.properties", "application.yml", "application.yaml"}
_SPRING_CONF_PROFILE_RE = re.compile(r'^application-([a-z0-9_-]+)\.(properties|ya?ml)$', re.IGNORECASE)
# Matches ${ENV_VAR} or ${ENV_VAR:default} where ENV_VAR is UPPER_SNAKE_CASE.
# Group 1 = key, Group 2 = default (may be empty string, absent = no default).
_SPRING_ENV_VAR_RE = re.compile(r'\$\{([A-Z][A-Z0-9_]*)(?::([^}]*))?\}')
# Matches ${spring.dotted.key} or ${spring.dotted.key:default} — Spring property references.
# These are internal property cross-references, not OS env vars, but still config signals.
_SPRING_PROP_REF_RE = re.compile(r'\$\{([a-z][a-z0-9]*(?:\.[a-z][a-z0-9_-]*)*)(?::([^}]*))?\}')

# Patterns where absence of the variable causes a hard runtime error (not just None/null).
# py_environ_bracket → os.environ["KEY"] raises KeyError
# java_spring_value   → Spring fails to start if ${KEY} has no default
_HARD_REQUIRED_PATTERNS: frozenset[str] = frozenset({
    "py_environ_bracket",
    "java_spring_value",
})

# (pattern_id, compiled_regex)
# Grupos de captura: group(1)=key, group(2)=default si existe
_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("py_getenv", re.compile(
        r"""os\.getenv\(\s*["']([A-Z][A-Z0-9_]*)["']\s*(?:,\s*(?:["']([^"']*?)["']|([A-Za-z_]\w*|None)))?\s*\)"""
    )),
    ("py_environ_bracket", re.compile(
        r"""os\.environ\s*\[\s*["']([A-Z][A-Z0-9_]*)["']\s*\]"""
    )),
    ("py_environ_get", re.compile(
        r"""os\.environ\.get\(\s*["']([A-Z][A-Z0-9_]*)["']\s*(?:,\s*(?:["']([^"']*?)["']|([A-Za-z_]\w*|None)))?\s*\)"""
    )),
    ("js_process_dot", re.compile(
        r"""process\.env\.([A-Z][A-Z0-9_]*)(?!\s*=(?!=))"""
    )),
    ("js_process_bracket", re.compile(
        r"""process\.env\[\s*["']([A-Z][A-Z0-9_]*)["']\s*\]"""
    )),
    ("go_getenv", re.compile(
        r"""os\.(?:Getenv|LookupEnv)\(\s*["']([A-Z][A-Z0-9_]*)["']\s*\)"""
    )),
    ("ruby_env_bracket", re.compile(
        r"""ENV\s*\[\s*["']([A-Z][A-Z0-9_]*)["']\s*\]"""
    )),
    ("ruby_env_fetch", re.compile(
        r"""ENV\.fetch\(\s*["']([A-Z][A-Z0-9_]*)["']\s*(?:,\s*(?:["']([^"']*?)["']|([A-Za-z_]\w*|nil)))?\s*\)"""
    )),
    ("java_getenv", re.compile(
        r"""System\.getenv\(\s*["']([A-Z][A-Z0-9_]*)["']\s*\)"""
    )),
    ("java_spring_value", re.compile(
        r"""@Value\(\s*["']\$\{([A-Z][A-Z0-9_]*)(?::[^}]*)?\}["']\s*\)"""
    )),
    ("php_getenv", re.compile(
        r"""getenv\(\s*["']([A-Z][A-Z0-9_]*)["']\s*\)"""
    )),
    ("php_env_bracket", re.compile(
        r"""\$_(?:ENV|SERVER)\s*\[\s*["']([A-Z][A-Z0-9_]*)["']\s*\]"""
    )),
    ("rust_env_var", re.compile(
        r"""env::var\(\s*["']([A-Z][A-Z0-9_]*)["']\s*\)"""
    )),
]

# (prefixes_or_suffixes, category) — los que empiezan con "_" son sufijos
_CATEGORY_RULES: list[tuple[list[str], str]] = [
    (["DB_", "DATABASE_", "POSTGRES_", "POSTGRESQL_", "MYSQL_", "MONGO_", "SQLITE_", "MARIADB_", "MSSQL_", "COCKROACH_"], "database"),
    (["REDIS_", "CACHE_", "MEMCACHE_", "MEMCACHED_"], "cache"),
    (["S3_", "BUCKET_", "GCS_", "BLOB_", "STORAGE_", "MINIO_", "AZURE_BLOB_"], "storage"),
    (["JWT_", "SECRET_", "AUTH_", "TOKEN_", "OAUTH_", "KEYCLOAK_", "AUTH0_", "FIREBASE_", "API_KEY", "API_SECRET"],  "auth"),
    (["_SECRET", "_TOKEN", "_API_KEY", "_ACCESS_KEY", "_PRIVATE_KEY", "_SIGNING_KEY", "_PASSWORD", "_PASSWD"], "auth"),
    (["SMTP_", "EMAIL_", "MAIL_", "SENDGRID_", "MAILGUN_", "SES_", "TWILIO_", "SLACK_WEBHOOK", "TELEGRAM_"], "service"),
    (["SENTRY_", "DATADOG_", "NEWRELIC_", "LOG_", "LOGGING_", "OTEL_", "METRICS_", "TRACE_", "HONEYCOMB_"], "observability"),
    (["FEATURE_", "FF_", "ENABLE_", "DISABLE_", "FLAG_", "TOGGLE_"], "feature_flag"),
    (["PORT", "_PORT", "HOST", "_HOST", "_URL", "_URI", "_DSN", "_ADDR", "_ADDRESS", "_ENDPOINT", "_BASE_URL"], "server"),
]

_TYPE_RULES: list[tuple[list[str], str]] = [
    (["_PORT", "_TIMEOUT", "_LIMIT", "_SIZE", "_MAX", "_MIN", "_COUNT", "_NUM", "_NUMBER",
      "_TTL", "_EXPIRY", "_EXPIRATION", "_WORKERS", "_THREADS", "_CONNECTIONS", "_POOL", "_RETRY",
      "PORT"], "int"),
    (["ENABLE_", "DISABLE_", "FEATURE_", "_ENABLED", "_DISABLED", "_DEBUG", "_SSL", "_TLS",
      "_SECURE", "_VERIFY", "_INSECURE", "_VERBOSE", "_FLAG", "DEBUG", "VERBOSE"], "bool"),
    (["_URL", "_URI", "_DSN", "_ENDPOINT", "_BASE_URL", "_WEBHOOK"], "url"),
    (["_PATH", "_DIR", "_FILE", "_FOLDER", "_DIRECTORY"], "path"),
    (["LOG_LEVEL", "_ENV", "_ENVIRONMENT", "_MODE", "_LEVEL", "_STAGE",
      "NODE_ENV", "APP_ENV", "RAILS_ENV", "GO_ENV", "PYTHON_ENV", "FLASK_ENV", "DJANGO_ENV"], "enum"),
]


def _infer_category(key: str) -> str:
    upper = key.upper()
    for prefixes, category in _CATEGORY_RULES:
        for token in prefixes:
            if token.startswith("_"):
                if upper.endswith(token):
                    return category
            else:
                if upper.startswith(token) or upper == token:
                    return category
    if upper in ("DEBUG", "SECRET", "SECRET_KEY"):
        return "auth" if upper in ("SECRET", "SECRET_KEY") else "observability"
    return "general"


def _infer_type_hint(key: str) -> str:
    upper = key.upper()
    for patterns, hint in _TYPE_RULES:
        for token in patterns:
            if token.startswith("_"):
                if upper.endswith(token):
                    return hint
            else:
                if upper.startswith(token) or upper == token:
                    return hint
    return "string"


def _scan_file(
    path: Path,
    rel_path: str,
    findings: dict[str, list[tuple[str, Optional[str], bool, Optional[str]]]],
) -> None:
    """Escanea un fichero y acumula hallazgos en findings[key] = [(file_ref, default, is_hard, profile)]."""
    try:
        size = path.stat().st_size
        if size > _MAX_FILE_SIZE:
            return
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return

    for pattern_id, regex in _PATTERNS:
        is_hard = pattern_id in _HARD_REQUIRED_PATTERNS
        for m in regex.finditer(content):
            key = m.group(1)
            if not key:
                continue
            # Determine default: group(2) is string default, group(3) is identifier default
            default: Optional[str] = None
            try:
                raw_default = m.group(2) or m.group(3)
                if raw_default and raw_default not in ("None", "nil", "null", "undefined"):
                    default = raw_default
            except IndexError:
                pass

            line_num = content.count("\n", 0, m.start()) + 1
            file_ref = f"{rel_path}:{line_num}"
            findings[key].append((file_ref, default, is_hard, None))


def _parse_env_example(
    path: Path,
    rel_path: str,
) -> list[tuple[str, Optional[str], Optional[str]]]:
    """Parse fichero .env.example. Retorna [(key, default_value, description)]."""
    results = []
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return results

    pending_comment: Optional[str] = None
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            pending_comment = None
            continue
        if stripped.startswith("#"):
            text = stripped[1:].strip()
            pending_comment = text if text else None
            continue
        if "=" in stripped:
            key, _, value = stripped.partition("=")
            key = key.strip()
            value = value.strip()
            if key and re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
                results.append((key, value or None, pending_comment))
            pending_comment = None
        else:
            pending_comment = None
    return results


def _extract_spring_profile(filename: str) -> Optional[str]:
    """Extract Spring profile from filename.

    application.yml / application.properties → 'default'
    application-m3dev.yml → 'm3dev'
    """
    name_lower = filename.lower()
    if name_lower in _SPRING_CONF_BASE:
        return "default"
    m = _SPRING_CONF_PROFILE_RE.match(name_lower)
    if m:
        return m.group(1)
    return None


def _parse_spring_config(
    path: Path,
    rel_path: str,
    findings: dict,
    profile: Optional[str] = None,
) -> int:
    """Parse application.properties / application.yml for ${ENV_VAR} refs.

    Returns the total number of ${...} placeholders found (candidates).
    Captures default values from ${VAR:default} syntax.
    Marks vars without defaults as hard-required (Spring fails to start if missing).
    """
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0

    candidates = 0

    # 1. UPPER_SNAKE_CASE env var references: ${DB_HOST} or ${DB_HOST:localhost}
    for m in _SPRING_ENV_VAR_RE.finditer(content):
        key = m.group(1)
        raw_default = m.group(2)  # None if no colon, "" if colon with empty default
        # A colon means a default was specified (even if empty string)
        has_default = raw_default is not None
        default: Optional[str] = raw_default if (raw_default and raw_default.strip()) else None
        line_num = content.count("\n", 0, m.start()) + 1
        # Hard required only when no default is provided
        is_hard = not has_default
        findings[key].append((f"{rel_path}:{line_num}", default, is_hard, profile))
        candidates += 1

    # 2. lowercase.dotted Spring property refs: ${spring.datasource.url:default}
    # These are internal property cross-references; store with a special prefix so
    # callers can distinguish them from OS env vars. We do NOT mark them hard-required
    # because they reference Spring's own property resolution chain.
    for m in _SPRING_PROP_REF_RE.finditer(content):
        key = m.group(1)
        raw_default = m.group(2)
        default = raw_default if (raw_default and raw_default.strip()) else None
        line_num = content.count("\n", 0, m.start()) + 1
        findings[key].append((f"{rel_path}:{line_num}", default, False, profile))
        candidates += 1

    return candidates


class EnvAnalyzer:
    """Extrae el mapa de variables de entorno del proyecto."""

    def analyze(
        self,
        root: Path,
        file_tree: dict,
    ) -> tuple[list, object]:
        from sourcecode.schema import EnvSummary, EnvVarRecord

        # findings[key] = list of (file_ref, default_or_None, is_hard_required, profile_or_None)
        findings: dict[str, list[tuple[str, Optional[str], bool, Optional[str]]]] = defaultdict(list)
        example_entries: list[tuple[str, Optional[str], Optional[str]]] = []
        example_files_found: list[str] = []
        limitations: list[str] = []
        profiles_scanned: list[str] = []
        spring_candidates: int = 0

        spring_candidates = self._walk(
            root, root, findings, example_entries, example_files_found,
            limitations, profiles_scanned,
        )

        # Merge findings into EnvVarRecord per key
        records: dict[str, EnvVarRecord] = {}

        # 1. From source code scans
        for key, refs in findings.items():
            if len(records) >= _MAX_KEYS:
                limitations.append(f"key_limit_reached:{_MAX_KEYS}")
                break
            defaults = [d for _, d, _, _ in refs if d is not None]
            # required only when access pattern causes a hard runtime error if missing:
            # os.environ["KEY"] (KeyError) or Spring @Value/${KEY} without default.
            # os.getenv("KEY") / os.environ.get("KEY") return None — not hard required.
            has_hard_access = any(is_hard for _, _, is_hard, _ in refs)
            required = has_hard_access and not defaults
            default_val = defaults[0] if defaults else None
            unique_files: list[str] = []
            seen: set[str] = set()
            # Collect first profile seen for this key (from Spring config files)
            first_profile: Optional[str] = None
            for file_ref, _, _, prof in refs:
                if file_ref not in seen:
                    seen.add(file_ref)
                    unique_files.append(file_ref)
                if first_profile is None and prof is not None:
                    first_profile = prof
                if len(unique_files) >= _MAX_FILES_PER_KEY:
                    break
            records[key] = EnvVarRecord(
                key=key,
                required=required,
                default=default_val,
                type_hint=_infer_type_hint(key),
                category=_infer_category(key),
                files=unique_files,
                profile=first_profile,
            )

        # 2. Supplement with .env.example entries (fill description + add missing keys)
        for key, example_default, description in example_entries:
            if key in records:
                # Only add description; never override required status from code analysis
                if description and not records[key].description:
                    records[key] = _replace_description(records[key], description)
            else:
                if len(records) >= _MAX_KEYS:
                    break
                records[key] = EnvVarRecord(
                    key=key,
                    required=False,  # .env.example documents presence; hard required needs a code access pattern
                    default=example_default,
                    type_hint=_infer_type_hint(key),
                    category=_infer_category(key),
                    description=description,
                    files=[],
                )

        # Sort: by category then key
        sorted_records = sorted(
            records.values(),
            key=lambda r: (r.category or "zzz", r.key),
        )

        # Build summary
        categories = sorted({r.category for r in sorted_records if r.category})
        required_count = sum(1 for r in sorted_records if r.required)

        # Coverage note: warn if Spring config was scanned but coverage seems partial
        coverage_note: Optional[str] = None
        if profiles_scanned and spring_candidates > 0:
            spring_key_count = sum(
                1 for r in sorted_records if r.profile is not None
            )
            if spring_key_count < spring_candidates:
                coverage_note = (
                    f"{spring_candidates} Spring ${{VAR}} placeholder(s) found across "
                    f"{len(profiles_scanned)} profile(s); {spring_key_count} unique key(s) "
                    "extracted. Duplicates across profiles collapsed."
                )

        summary = EnvSummary(
            requested=True,
            total=len(sorted_records),
            required_count=required_count,
            optional_count=len(sorted_records) - required_count,
            categories=categories,
            example_files_found=example_files_found,
            limitations=limitations,
            profiles_scanned=sorted(set(profiles_scanned)),
            spring_candidates=spring_candidates,
            coverage_note=coverage_note,
        )

        return sorted_records, summary

    def _walk(
        self,
        root: Path,
        current: Path,
        findings: dict,
        example_entries: list,
        example_files_found: list,
        limitations: list,
        profiles_scanned: list,
    ) -> int:
        """Walk the directory tree accumulating env var findings. Returns spring_candidates count."""
        try:
            entries = sorted(current.iterdir())
        except PermissionError:
            return 0

        total_spring_candidates = 0

        for entry in entries:
            name = entry.name
            if name.startswith(".") and name not in _ENV_EXAMPLE_NAMES and entry.is_dir():
                continue
            if entry.is_dir():
                if name in _SKIP_DIRS:
                    continue
                total_spring_candidates += self._walk(
                    root, entry, findings, example_entries, example_files_found,
                    limitations, profiles_scanned,
                )
            elif entry.is_file():
                rel = entry.relative_to(root).as_posix()
                name_lower = name.lower()
                # .env.example and similar
                if name in _ENV_EXAMPLE_NAMES:
                    example_files_found.append(rel)
                    example_entries.extend(_parse_env_example(entry, rel))
                    continue
                # Spring Boot application.properties / application.yml (incl. profiles)
                if name_lower in _SPRING_CONF_BASE or _SPRING_CONF_PROFILE_RE.match(name_lower):
                    profile = _extract_spring_profile(name)
                    if profile and profile not in profiles_scanned:
                        profiles_scanned.append(profile)
                    count = _parse_spring_config(entry, rel, findings, profile)
                    total_spring_candidates += count
                    continue
                # Source code files
                suffix = entry.suffix.lower()
                if suffix in _CODE_EXTENSIONS:
                    _scan_file(entry, rel, findings)

        return total_spring_candidates


def _replace_description(record, description: str):
    from dataclasses import replace
    return replace(record, description=description)


def _replace_required(record, required: bool, default: Optional[str]):
    from dataclasses import replace
    return replace(record, required=required, default=default if not record.default else record.default)

from __future__ import annotations

"""sourcecode serializer — canonical JSON, YAML, and compact mode.

Critical patterns:
  - Always pass through dataclasses.asdict() before json.dumps (json does not serialize dataclasses)
  - ruamel.yaml with representer for canonical null (not ~)
  - compact_view() projects only required fields (~500 tokens)
"""

import json
import sys
from dataclasses import asdict, dataclass, is_dataclass, replace
from io import StringIO
from pathlib import Path
from typing import Any, Optional

from sourcecode.entrypoint_classifier import normalize_entry_point, is_production_entry_point
from sourcecode.file_classifier import FileClassifier
from sourcecode.schema import (
    ArchitectureAnalysis,
    ModuleGraph,
    ModuleGraphSummary,
    SourceMap,
)

# ---------------------------------------------------------------------------
# Visibility caps — public output is intentionally small.
# Internal analysis stays broad; only high-signal results reach the output.
# ---------------------------------------------------------------------------
_EP_PRODUCTION_CAP = 5       # max production entry points in default output
_EP_DEV_CAP = 3              # max development entry points in default output
_FILE_RELEVANCE_LIMIT = 10   # max files in file_relevance section
_FILE_RELEVANCE_MIN_COMBINED = 0.40  # minimum combined score — must earn inclusion
_PROD_DEPS_CAP = 10          # max production dependencies shown
_SECONDARY_DEPS_CAP = 5      # max per dev/test/build dependency group
_MONOREPO_PKGS_CAP = 8       # max workspace/runtime packages shown
_KEY_DEPS_CAP = 50           # max key dependencies shown
_CODE_NOTES_CAP = 15         # max code notes in default output
_ENV_MAP_CAP = 15            # max env var entries in default output
_MAX_DEFAULT_CONTRACTS = 20  # max contracts in default/standard contract output
_MAX_HARD_SIGNALS_DEFAULT = 20  # max hard_signals entries in default output


def _truncate_note(text: str, limit: int) -> str:
    """Truncate note text at word boundary, appending … when cut."""
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(".,;") + "…"


def _cap_contracts_for_output(
    contracts: list[Any],
    max_count: int = _MAX_DEFAULT_CONTRACTS,
) -> tuple[list[Any], dict[str, Any]]:
    """Sort contracts by relevance_score desc, cap to max_count, return (sampled, meta)."""
    total = len(contracts)
    sampled = sorted(contracts, key=lambda c: getattr(c, "relevance_score", 0.0), reverse=True)[:max_count]
    meta: dict[str, Any] = {"total": total, "shown": len(sampled), "truncated": total > max_count}
    return sampled, meta


def to_json(sm: SourceMap | dict[str, Any], indent: int = 2) -> str:
    """Serialize SourceMap or dict to canonical JSON.

    Accepts a SourceMap (dataclass) or an already-prepared dict (e.g. compact_view()).
    Uses dataclasses.asdict() to convert dataclasses before json.dumps.
    ensure_ascii=False to preserve UTF-8 in paths.
    """
    data = asdict(sm) if is_dataclass(sm) and not isinstance(sm, type) else sm
    return json.dumps(data, indent=indent, ensure_ascii=False)


def to_yaml(sm: SourceMap) -> str:
    """Serialize SourceMap to YAML using ruamel.yaml.

    ruamel.yaml preserves key order and serializes None as null
    (not as ~) with the default dict dump configuration.
    """
    from ruamel.yaml import YAML

    yaml = YAML()
    yaml.default_flow_style = False
    # Ensure None is serialized as 'null', not '~'
    yaml.representer.add_representer(
        type(None),
        lambda dumper, data: dumper.represent_scalar("tag:yaml.org,2002:null", "null"),
    )
    stream = StringIO()
    yaml.dump(asdict(sm), stream)
    return stream.getvalue()


def _clean_entry_point(ep: Any) -> dict[str, Any]:
    normalized = normalize_entry_point(ep)
    return {
        k: v
        for k, v in asdict(normalized).items()
        if v is not None and v != "" and k != "workspace"
    }


def _entry_point_groups(entry_points: list[Any]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {
        "production": [],
        "development": [],
        "auxiliary": [],
    }
    for ep in entry_points:
        normalized = normalize_entry_point(ep)
        item = _clean_entry_point(normalized)
        if is_production_entry_point(normalized):
            groups["production"].append(item)
        elif normalized.classification == "development":
            groups["development"].append(item)
        else:
            groups["auxiliary"].append(item)

    groups["production"].sort(key=lambda ep: (ep.get("runtime_relevance") != "high", ep.get("path", "")))
    groups["development"].sort(key=lambda ep: ep.get("path", ""))
    groups["auxiliary"].sort(key=lambda ep: ep.get("path", ""))
    return groups


_PRODUCTION_DEP_ROLES = {"runtime", "parsing", "serialization", "observability", "infra"}
_DEV_DEP_ROLES = {"devtool"}
_TEST_DEP_ROLES = {"testtool"}
_BUILD_DEP_ROLES = {"buildtool"}


def _dependency_groups(sm: SourceMap) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {
        "production_dependencies": [],
        "dev_tools": [],
        "test_utilities": [],
        "build_tooling": [],
        "noise_dependencies": [],
        "suspicious_dependencies": [],
    }
    if sm.dependency_summary is None or not sm.dependency_summary.requested:
        return groups

    root = Path(sm.metadata.analyzed_path) if sm.metadata.analyzed_path else Path(".")
    import_index = _dependency_import_index(root, sm.file_paths)

    for dep in sm.dependency_summary.dependencies:
        if dep.scope == "transitive":
            continue
        item = {
            k: v for k, v in asdict(dep).items()
            if v is not None and k not in {"parent"}
        }
        role = dep.role or "unknown"
        scope = dep.scope
        name_key = _dep_import_key(dep.name)

        if role in _PRODUCTION_DEP_ROLES and scope not in {"dev"}:
            groups["production_dependencies"].append(item)
            _jvm_ecosystems = {"maven", "gradle", "java", "kotlin", "scala", "groovy"}
            if dep.source == "manifest" and name_key not in import_index:
                if dep.ecosystem in _jvm_ecosystems:
                    # Static import check unsupported for JVM: import index only covers
                    # Python/JS/TS. Flagging JVM deps as suspicious produces only false positives.
                    pass
                else:
                    suspect = dict(item)
                    suspect["reason"] = "declared as production dependency but no static import observed"
                    groups["suspicious_dependencies"].append(suspect)
        elif role in _TEST_DEP_ROLES:
            groups["test_utilities"].append(item)
        elif role in _BUILD_DEP_ROLES:
            groups["build_tooling"].append(item)
        elif role in _DEV_DEP_ROLES or scope in {"dev", "optional"}:
            groups["dev_tools"].append(item)
        else:
            groups["noise_dependencies"].append(item)

    for values in groups.values():
        values.sort(key=lambda d: (d.get("ecosystem", ""), d.get("name", "")))
    return groups


def _dependency_import_index(root: Path, file_paths: list[str]) -> set[str]:
    import re

    index: set[str] = set()
    import_re = re.compile(
        r"(?:from\s+([A-Za-z0-9_@./-]+)\s+import|import\s+([A-Za-z0-9_@./-]+)|"
        r"require\(['\"]([^'\"]+)['\"]\)|from\s+['\"]([^'\"]+)['\"])",
        re.MULTILINE,
    )
    for path in file_paths[:2000]:
        if Path(path).suffix.lower() not in {".py", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs"}:
            continue
        try:
            content = (root / path).read_text(encoding="utf-8", errors="replace")[:20000]
        except OSError:
            continue
        for match in import_re.findall(content):
            raw = next((part for part in match if part), "")
            if raw and not raw.startswith("."):
                index.add(_dep_import_key(raw))
    return index


def _dep_import_key(name: str) -> str:
    lowered = name.lower()
    if lowered.startswith("@"):
        parts = lowered.split("/")
        return "/".join(parts[:2])
    return lowered.split("/")[0].replace("_", "-")


# ---------------------------------------------------------------------------
# Java/Spring compact-mode helpers (v1.10.0)
# ---------------------------------------------------------------------------

def _compact_git_context(sm: "SourceMap") -> "Optional[dict[str, Any]]":
    """Lightweight git_context for compact/agent output. Top-5 hotspots only."""
    gc = sm.git_context
    if gc is None or not gc.requested:
        return None
    _bad = {"no_git_repo", "git_not_found", "git_timeout"}
    if _bad & set(gc.limitations):
        return None
    ctx: dict[str, Any] = {}
    if gc.branch:
        ctx["branch"] = gc.branch
    if gc.uncommitted_changes is not None:
        uc = gc.uncommitted_changes
        ctx["uncommitted_files"] = len(uc.staged) + len(uc.unstaged) + len(uc.untracked)
    if gc.change_hotspots:
        ctx["top_hotspots"] = [
            {"file": h.file, "commits": h.commit_count}
            for h in gc.change_hotspots[:5]
        ]
    elif gc.recent_commits:
        # No activity in the hotspot window — derive hotspots from recent commit history.
        # Repos with old or infrequent commits still deserve file-level signal.
        from collections import Counter as _Counter
        _fc: _Counter[str] = _Counter(
            f for c in gc.recent_commits for f in (c.files_changed or [])
        )
        if _fc:
            ctx["top_hotspots"] = [
                {"file": f, "commits": n}
                for f, n in _fc.most_common(5)
            ]
            ctx["hotspots_source"] = "recent_commits"
    if gc.recent_commits:
        ctx["recent_commits"] = [
            {
                "hash": c.hash[:8],
                "message": (c.message or "")[:80],
                "date": (c.date or "")[:10],
                "author": c.author or "",
            }
            for c in gc.recent_commits[:5]
        ]
    return ctx if ctx else None


def _dep_risk_flags(name: str, version: "Optional[str]") -> list[str]:
    """Static heuristic risk flags for a single dependency. No external lookups."""
    flags: list[str] = []
    nl = name.lower()
    if "spring-boot" in nl or "spring.boot" in nl:
        if version and version.startswith("2."):
            flags.append("spring-boot-2.x-eol")
    if nl.startswith("javax.") or nl == "javax":
        flags.append("javax-to-jakarta-migration-risk")
    if "ojdbc" in nl or nl in {"com.oracle.database.jdbc", "oracle.jdbc.driver.oracledriver"}:
        flags.append("oracle-vendor-lock")
    return flags


def _project_deployment_risks(sm: "SourceMap") -> list[str]:
    """Project-level deployment risk flags derived from Java version, app server, and framework."""
    risks: list[str] = []
    lv = sm.language_version or ""
    if lv in ("1.8", "8", "1.7", "7"):
        risks.append("legacy-java-runtime")
    if getattr(sm, "app_server_hint", None) == "weblogic" and getattr(sm, "packaging", None) == "war":
        risks.append("legacy-app-server-deployment")
    sb_ver = _spring_boot_version(sm)
    if sb_ver and sb_ver.startswith("2."):
        risks.append("spring-boot-2.x-eol")
    return risks


_DTO_MAPPER_STEMS = frozenset({"DtoMapper", "GenericDtoMapper", "BaseMapper", "AbstractMapper"})

def _is_dto_mapper(path: str, root: Optional[Path] = None) -> bool:
    """True when a *Mapper.java is a bean-mapping class, not a MyBatis @Mapper interface.

    Heuristic 1: stem contains "Dto" (e.g. CotizacionDtoMapper).
    Heuristic 2: file content extends a known bean-mapper base class.
    Heuristic 3: NO @Mapper (org.apache.ibatis.annotations.Mapper) annotation found in file.
    """
    from pathlib import Path as _Path
    stem = _Path(path).stem
    if "Dto" in stem or stem in _DTO_MAPPER_STEMS:
        return True
    if root is not None:
        try:
            from sourcecode.tree_utils import safe_read_text
            content = safe_read_text(root / path)
            # Explicit @Mapper annotation → real MyBatis mapper
            if "org.apache.ibatis.annotations.Mapper" in content or "@Mapper" in content[:2000]:
                return False
            # Extends bean-mapping base → dto mapper
            if any(f"extends {base}" in content for base in _DTO_MAPPER_STEMS):
                return True
            # No @Mapper and no XML → likely dto mapper
            return True
        except OSError:
            pass
    return False


def _mybatis_pairing(sm: "SourceMap", *, full: bool = False) -> "Optional[dict[str, Any]]":
    """Lightweight MyBatis mapper interface <-> XML file pairing from file_paths.

    Separates genuine @Mapper interfaces (need XML) from DtoMapper bean-mapping
    classes (no XML needed) to eliminate false-positive missing_xml reports.
    """
    from pathlib import Path as _Path
    has_mybatis = any(
        any(f.name.lower() == "mybatis" for f in s.frameworks)
        for s in sm.stacks
    )
    if not has_mybatis:
        return None

    root = Path(sm.metadata.analyzed_path) if sm.metadata.analyzed_path else None
    non_test = [p for p in sm.file_paths if "/test/" not in p and "/tests/" not in p]
    all_mapper_java = [p for p in non_test if p.endswith("Mapper.java")]

    # Separate @Mapper interfaces from DtoMapper bean-mapping classes
    mybatis_interfaces: list[str] = []
    dto_mappers: list[str] = []
    for p in all_mapper_java:
        if _is_dto_mapper(p, root):
            dto_mappers.append(p)
        else:
            mybatis_interfaces.append(p)

    xml_files = [p for p in sm.file_paths if p.endswith("Mapper.xml")]
    interface_index = {_Path(p).stem: p for p in mybatis_interfaces}
    xml_index = {_Path(p).stem: p for p in xml_files}
    orphan_xml = [xml_index[s] for s in xml_index if s not in interface_index]
    missing_xml = [s for s in interface_index if s not in xml_index]

    result: dict[str, Any] = {
        "mapper_interfaces": len(mybatis_interfaces),
        "xml_files": len(xml_files),
    }
    if orphan_xml:
        result["orphan_xml"] = orphan_xml[:5]
    if missing_xml:
        result["missing_xml"] = missing_xml[:5]
    if dto_mappers:
        _total_dto = len(dto_mappers)
        result["dto_mappers"] = dto_mappers if full else dto_mappers[:10]
        result["dto_mappers_total"] = _total_dto
        if _total_dto > 10 and not full:
            result["dto_mappers_truncated"] = True
            result["dto_mappers_warning"] = f"Showing 10/{_total_dto} mappers. Use --full to see all."
    return result


def _spring_boot_version(sm: "SourceMap") -> "Optional[str]":
    """Extract Spring Boot version from detected frameworks."""
    for s in sm.stacks:
        for fw in s.frameworks:
            if fw.name == "Spring Boot" and fw.version:
                return fw.version
    return None


def _spring_event_signal(sm: "SourceMap") -> "Optional[dict[str, Any]]":
    """IC-005: Surface @EventListener and publishEvent from Java source files.

    Scans sm.file_paths for Java files containing Spring event annotations.
    Only runs on Java/Spring projects. Lightweight — path heuristics first,
    then targeted content scan on candidate files only.
    """
    import re as _re
    java_paths = [p for p in sm.file_paths if p.endswith(".java") and "target/" not in p]
    if not java_paths:
        return None
    _frameworks = [f.name for s in (sm.stacks or []) for f in s.frameworks]
    if not any("Spring" in fw for fw in _frameworks):
        return None

    analyzed_path = getattr(sm.metadata, "analyzed_path", None) if sm.metadata else None
    root = Path(analyzed_path) if analyzed_path else None

    listeners: list[str] = []
    publishers: list[str] = []
    event_types: set[str] = set()
    _publish_re = _re.compile(r"\.publishEvent\s*\(\s*new\s+(\w+)")

    for rel_path in java_paths:
        if root is None:
            break
        try:
            content = (root / rel_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "@EventListener" in content:
            cls_m = _re.search(r"class\s+(\w+)", content)
            cls_name = cls_m.group(1) if cls_m else Path(rel_path).stem
            if cls_name not in listeners:
                listeners.append(cls_name)
        for m in _publish_re.finditer(content):
            event_types.add(m.group(1))
            cls_m = _re.search(r"class\s+(\w+)", content)
            cls_name = cls_m.group(1) if cls_m else Path(rel_path).stem
            if cls_name not in publishers:
                publishers.append(cls_name)

    if not listeners and not publishers:
        return None

    return {
        "listeners": sorted(set(listeners))[:10],
        "publishers": sorted(set(publishers))[:10],
        "event_types": sorted(event_types)[:10],
        "flow_count": len(listeners) + len(publishers),
    }


def _spring_profiles_context(sm: "SourceMap") -> "Optional[dict[str, Any]]":
    """Build structured spring_profiles block: detected names + per-profile file variants."""
    # Only emit for Spring Boot / Spring projects. Quarkus and Jakarta EE projects
    # use application-{profile}.properties too but those are NOT Spring profiles.
    _is_spring = any(
        f.name in ("Spring Boot", "Spring")
        for stack in getattr(sm, "stacks", [])
        for f in getattr(stack, "frameworks", [])
    )
    if not _is_spring:
        return None

    # Gather profile names from env_summary (populated by env_analyzer scanning
    # application-{profile}.yml files) or the top-level spring_profiles list.
    profiles: list[str] = []
    if sm.env_summary is not None:
        _sp = sm.env_summary.spring_profiles or sm.env_summary.profiles_scanned
        if _sp:
            profiles = sorted(set(_sp))
    if not profiles:
        _top = getattr(sm, "spring_profiles", [])
        profiles = sorted(set(_top)) if _top else []
    if not profiles:
        return None

    # Per-profile variants: Java/XML files whose stem or path contains the profile name.
    # Portal profiles (e.g. "ingesa-portal") use dash in directory/resource names but
    # never in Java class names — match on full path instead of stem only.
    per_profile: dict[str, list[str]] = {}
    for profile in profiles:
        pfx = profile.lower()
        is_portal = "-" in profile
        if is_portal:
            matches = [
                p for p in sm.file_paths
                if pfx in p.lower() and (p.endswith(".java") or p.endswith(".xml"))
            ]
        else:
            matches = [
                p for p in sm.file_paths
                if (Path(p).stem.lower() == pfx
                    or Path(p).stem.lower().startswith(pfx + "-")
                    or Path(p).stem.lower().endswith("-" + pfx))
                and p.endswith(".java")
            ]
        if matches:
            per_profile[profile] = [Path(p).name for p in matches[:5]]
        elif is_portal:
            per_profile[profile] = []

    result: dict[str, Any] = {"detected": profiles}
    if per_profile:
        result["per_profile_variants"] = per_profile
        # Heuristic note when security strategy variants detected
        has_security = any(
            "security" in v.lower() or "strategy" in v.lower()
            for vlist in per_profile.values()
            for v in vlist
        )
        if has_security:
            result["note"] = (
                "Each profile activates a different SecurityStrategy implementation"
            )
    return result


def _transactional_summary(sm: "SourceMap", *, full: bool = False) -> "Optional[dict[str, Any]]":
    """Surface @Transactional class boundaries from the Java stack detection."""
    for s in sm.stacks:
        classes = getattr(s, "transactional_classes", [])
        if classes:
            total = len(classes)
            result: dict[str, Any] = {"count": total, "classes": classes}
            if total > 10 and not full:
                result["classes"] = classes[:10]
                result["truncated"] = True
                result["note"] = f"showing 10 of {total}; use --full to see all {total}"
            return result
    return None


def _resolve_java_constant(symbol: str, root: "Optional[Path]", file_paths: "Optional[list[str]]") -> str:
    """Resolve a Java constant reference like ClassName.FIELD_NAME to its string value."""
    import re as _re
    if not root or not file_paths or "." not in symbol:
        return symbol
    parts = symbol.rsplit(".", 1)
    if len(parts) != 2:
        return symbol
    class_name, field_name = parts
    if not class_name or not field_name or not field_name.isupper():
        return symbol
    target_file = f"{class_name}.java"
    candidates = [p for p in file_paths if Path(p).name == target_file]
    if not candidates:
        return symbol
    _CONST_RE = _re.compile(
        r'\b' + _re.escape(field_name) + r'\s*=\s*"([^"]+)"'
    )
    for rel_path in candidates:
        try:
            abs_path = root / rel_path
            content = abs_path.read_text(encoding="utf-8", errors="replace")
            m = _CONST_RE.search(content)
            if m:
                return m.group(1)
        except OSError:
            continue
    return symbol


def _security_surface_from_eps(
    eps: list,
    *,
    root: "Optional[Path]" = None,
    file_paths: "Optional[list[str]]" = None,
) -> "Optional[dict[str, Any]]":
    """Extract @M3FiltroSeguridad resource names from entry point evidence strings."""
    import re as _re
    _NOMBRE_RE = _re.compile(r"nombreRecurso=[\"']([^\"']+)[\"']")
    _CONST_SYMBOL_RE = _re.compile(r'^[\w]+\.[\w]+$')
    resource_names: list[str] = []
    unresolved: list[str] = []
    seen: set[str] = set()
    for ep in eps:
        evidence = getattr(ep, "evidence", None)
        if not evidence:
            continue
        for m in _NOMBRE_RE.finditer(evidence):
            nm = m.group(1)
            if not nm or nm in seen:
                continue
            seen.add(nm)
            if _CONST_SYMBOL_RE.match(nm):
                resolved = _resolve_java_constant(nm, root, file_paths)
                if resolved != nm:
                    resource_names.append(resolved)
                else:
                    resource_names.append(nm)
                    unresolved.append(nm)
            else:
                resource_names.append(nm)
    if not resource_names:
        return None
    result: dict[str, Any] = {
        "schema": (
            "Values used in @M3FiltroSeguridad(nombreRecurso=VALUE) on REST controller "
            "methods. Each value names a permission resource checked at runtime."
        ),
        "resource_names": resource_names,
    }
    if unresolved:
        result["resource_names_unresolved"] = unresolved
    return result


def _bootstrap_structured(eps: list) -> "Optional[dict[str, Any]]":
    """Separate Java entry points into bootstrap / security / controllers groups."""
    from pathlib import Path as _Path
    bootstrap: list[str] = []
    security: list[str] = []
    controllers: list[dict] = []
    seen_b: set[str] = set()
    seen_s: set[str] = set()
    seen_c: set[str] = set()

    for ep in eps:
        path = getattr(ep, "path", "")
        if "/test/" in path or "/tests/" in path:
            continue
        kind = getattr(ep, "kind", "")
        stem = _Path(path).stem

        if kind == "application" or any(k in stem for k in ("Application", "Main", "Initializer", "Bootstrap")):
            if path not in seen_b:
                seen_b.add(path)
                bootstrap.append(path)
        elif kind == "filter" or any(k in stem for k in ("Filter", "Security", "Auth", "Jwt", "WebSecurity")):
            if path not in seen_s:
                seen_s.add(path)
                security.append(path)
        elif kind in ("rest_controller", "mvc_controller"):
            if path not in seen_c:
                seen_c.add(path)
                item: dict[str, Any] = {"path": path}
                http_path = getattr(ep, "http_path", None)
                if http_path:
                    item["http_path"] = http_path
                controllers.append(item)

    if not bootstrap and not security:
        return None

    result: dict[str, Any] = {}
    if bootstrap:
        result["bootstrap"] = bootstrap
    if security:
        result["security"] = security
    if controllers:
        # Count unique files (classes) vs total entries (methods/endpoints)
        controller_classes = len({c["path"] for c in controllers})
        controller_methods = len(controllers)

        # Extract all DDD module names from controller paths and group by domain area.
        # Path pattern: .../ddd/{module}/infrastructure/rest/*Controller.java
        _DDD_LAYERS = {"application", "domain", "infrastructure"}
        module_names: list[str] = []
        seen_modules: set[str] = set()
        for c in controllers:
            parts = c["path"].replace("\\", "/").split("/")
            module = ""
            for i, part in enumerate(parts):
                if part in _DDD_LAYERS and i >= 1:
                    module = parts[i - 1]
                    break
            if not module:
                # fallback: penultimate directory
                module = parts[-2] if len(parts) >= 2 else ""
            if module and module not in seen_modules:
                seen_modules.add(module)
                module_names.append(module)

        _ctrl_note = (
            f"{controller_methods} detected entry-point methods across "
            f"{controller_classes} controller classes"
            f" (use 'sourcecode endpoints' for full surface)"
        )
        if len(module_names) > 30:
            # Group by first path segment under ddd/ (inferred domain area)
            domain_groups: dict[str, list[str]] = {}
            for c in controllers:
                parts = c["path"].replace("\\", "/").split("/")
                module = ""
                domain_prefix = ""
                for i, part in enumerate(parts):
                    if part in _DDD_LAYERS and i >= 1:
                        module = parts[i - 1]
                        # the segment before the module is the domain area
                        domain_prefix = parts[i - 2] if i >= 2 else ""
                        break
                if module:
                    domain_groups.setdefault(domain_prefix or "other", [])
                    if module not in domain_groups[domain_prefix or "other"]:
                        domain_groups[domain_prefix or "other"].append(module)
            result["controllers"] = {
                "classes": controller_classes,
                "methods": controller_methods,
                "note": _ctrl_note,
                "modules": {k: sorted(v) for k, v in sorted(domain_groups.items())},
            }
        else:
            result["controllers"] = {
                "classes": controller_classes,
                "methods": controller_methods,
                "note": _ctrl_note,
                "modules": sorted(module_names),
            }
    return result


def _lightweight_arch_pattern(sm: "SourceMap") -> "Optional[dict[str, Any]]":
    """Heuristic architecture pattern from directory names alone."""
    if not sm.file_paths:
        return None
    dir_names: set[str] = set()
    for p in sm.file_paths:
        for part in p.replace("\\", "/").split("/")[:-1]:
            dir_names.add(part.lower())

    # HTTP handler layer: Spring MVC controllers AND JAX-RS resources
    has_controller = bool(
        {"controller", "controllers", "api", "rest", "web", "handler", "handlers",
         "resource", "resources", "endpoint", "endpoints"}
        & dir_names
    )
    # Business logic layer: Spring services, CDI providers, use-cases
    has_service = bool(
        {"service", "services", "usecase", "usecases", "application",
         "provider", "providers", "manager", "managers"}
        & dir_names
    )
    # Data access layer: JPA/JDBC repos, CDI stores, DAOs
    has_repository = bool(
        {"repository", "repositories", "repo", "repos", "dao", "persistence",
         "store", "stores", "datastore", "datastores"}
        & dir_names
    )
    has_domain = bool({"domain", "domains", "core", "model", "models", "entity", "entities"} & dir_names)
    has_infra = bool({"infrastructure", "infra", "adapter", "adapters"} & dir_names)

    if has_controller and has_service and has_repository and has_domain:
        return {"pattern": "ddd-layered", "confidence": 0.72 if has_infra else 0.55}
    if bool({"ports", "port"} & dir_names) and bool({"adapter", "adapters"} & dir_names):
        return {"pattern": "hexagonal-like", "confidence": 0.65}
    if has_controller and bool({"model", "models", "entity", "entities"} & dir_names):
        return {"pattern": "mvc", "confidence": 0.55}
    if has_controller and has_service and has_repository:
        return {"pattern": "layered", "confidence": 0.70}
    if has_controller and has_service:
        return {"pattern": "layered", "confidence": 0.42}
    return None


def _jndi_datasources(sm: "SourceMap") -> "Optional[list[dict[str, Any]]]":
    """Scan application.yml and persistence.xml for JNDI datasource names."""
    import re as _re
    _JNDI_YML_RE = _re.compile(r"jndi-name\s*:\s*(.+)")
    _JNDI_XML_RE = _re.compile(r"<jta-data-source>\s*([^<]+)\s*</jta-data-source>")
    root = Path(sm.metadata.analyzed_path) if sm.metadata.analyzed_path else Path(".")
    datasources: list[dict[str, Any]] = []
    seen: set[str] = set()

    for path_str in sm.file_paths:
        fname = path_str.rsplit("/", 1)[-1]
        if fname not in {"application.yml", "application.yaml", "persistence.xml"}:
            continue
        abs_path = root / path_str
        try:
            from sourcecode.tree_utils import safe_read_text
            content = safe_read_text(abs_path)
        except OSError:
            continue
        pattern = _JNDI_YML_RE if fname.endswith((".yml", ".yaml")) else _JNDI_XML_RE
        for m in pattern.finditer(content):
            name = m.group(1).strip().strip('"\'')
            if name and name not in seen:
                seen.add(name)
                datasources.append({"name": name, "source": path_str})

    if not datasources:
        return None
    return datasources


def _tiered_display_score(
    pre_bonus_combined: float,
    file_class: Any,
    path: str,
    entry_paths: set,
    has_structural_signals: bool = False,
) -> float:
    """Evidence-tiered display score [0.0, 1.0].

    Tiers enforce: strong evidence > medium evidence > filesystem/path only.
    M3 sort bonuses must NOT be included in pre_bonus_combined — they are for
    ordering only and must not inflate the displayed score.

    Tier ceilings:
      T1  confirmed production entrypoint           0.92–1.00
      T2  entrypoint (weaker category)              0.80–0.91
      T3  annotation-confirmed stereotype           0.40–0.90  (table-calibrated)
      T4  framework import evidence                 0.55–0.79
      T5  code definitions + imports                0.38–0.54
      T6  build manifest / tooling / test           0.25–0.45
      T7  path/filesystem signal only               0.10–0.39
    """
    from sourcecode.file_classifier import JAVA_STEREOTYPE_CATEGORIES

    cat = file_class.category if file_class else None
    base_rel = file_class.relevance if file_class else 0.0

    # T1: confirmed production entrypoint
    if path in entry_paths and cat in ("runtime_core", "cli_entrypoint"):
        return round(min(1.0, max(0.92, base_rel)), 3)

    # T2: in entry_paths but weaker evidence category
    if path in entry_paths:
        return round(min(0.91, max(0.80, pre_bonus_combined / 2.0)), 3)

    # T3: annotation-confirmed stereotype — table values are already calibrated
    if file_class and cat in JAVA_STEREOTYPE_CATEGORIES:
        return round(base_rel, 3)

    # T4: framework import evidence (medium strength)
    if cat in ("api_layer", "database_layer", "infrastructure"):
        return round(min(0.79, max(0.55, pre_bonus_combined / 2.0)), 3)

    # T5: code definitions with imports (medium-low)
    if cat in ("application_logic", "domain_model"):
        return round(min(0.54, max(0.38, pre_bonus_combined / 2.0)), 3)

    # T6: build manifest / tooling / test
    if cat == "build_system":
        return round(min(0.45, base_rel), 3)
    if cat in ("tests", "tooling"):
        return round(min(0.35, base_rel), 3)

    # T7: no content classification — filesystem/structural signals only
    # has_structural_signals: fan_in, churn, export — allows up to 0.54
    # pure path/filename only — hard cap 0.39
    if has_structural_signals:
        return round(min(0.54, max(0.10, pre_bonus_combined / 2.0)), 3)
    return round(min(0.39, max(0.10, pre_bonus_combined / 2.0)), 3)


def _build_file_signals(
    file_class: Any,
    path: str,
    entry_paths: set,
    fs_reasons: list,
    sem_hub: float,
) -> list[dict]:
    """Minimal per-file signal breakdown: what contributed to this file's score."""
    from sourcecode.file_classifier import JAVA_STEREOTYPE_CATEGORIES

    signals: list[dict] = []

    if path in entry_paths:
        signals.append({"type": "runtime_entrypoint", "strength": "strong"})

    if file_class:
        cat = file_class.category
        if cat in JAVA_STEREOTYPE_CATEGORIES:
            signals.append({"type": "framework_annotation", "strength": "strong"})
        elif cat in ("api_layer", "database_layer", "infrastructure"):
            ev = [e for e in (file_class.evidence or [])[:2] if e]
            signals.append({"type": "framework_import", "strength": "medium", "evidence": ev})
        elif cat in ("application_logic",):
            signals.append({"type": "code_definitions_with_imports", "strength": "medium"})
        elif cat in ("domain_model",):
            signals.append({"type": "domain_model_definitions", "strength": "medium"})
        elif cat in ("build_system",):
            signals.append({"type": "build_manifest", "strength": "medium"})

    for r in fs_reasons:
        r_lower = r.lower()
        if "import centrality" in r_lower or "imported by" in r_lower:
            signals.append({"type": "import_centrality", "strength": "medium"})
        elif "hub module" in r_lower:
            signals.append({"type": "hub_module", "strength": "medium"})
        elif "recent churn" in r_lower:
            signals.append({"type": "git_churn", "strength": "medium"})
        elif "uncommitted" in r_lower:
            signals.append({"type": "uncommitted_changes", "strength": "medium"})

    if sem_hub >= 0.15:
        signals.append({"type": "call_graph_hub", "strength": "strong"})

    if not signals:
        signals.append({"type": "filesystem_path", "strength": "weak"})

    return signals


def _file_relevance(sm: SourceMap, *, limit: int = _FILE_RELEVANCE_LIMIT) -> list[dict[str, Any]]:
    from sourcecode.ranking_engine import RankingEngine

    root = Path(sm.metadata.analyzed_path) if sm.metadata.analyzed_path else Path(".")
    classifier = FileClassifier(root, sm.entry_points, sm.monorepo_packages)
    engine = RankingEngine(sm.monorepo_packages)

    # Incorporate git hotspots when --git-context was passed
    git_churn: dict[str, int] = {}
    gc = sm.git_context
    if (gc and gc.requested and gc.change_hotspots
            and not any(lim in gc.limitations
                        for lim in ("no_git_repo", "git_not_found", "git_timeout"))):
        git_churn = {h.file: h.commit_count for h in gc.change_hotspots}
    max_churn = max(git_churn.values(), default=1)

    # Incorporate semantic hotspots from --semantics when available.
    # Hotspots rank files by call-graph centrality (fan_in×2 + fan_out),
    # normalised across the analysed files.
    semantic_hub_scores: dict[str, float] = {}
    ss = sm.semantic_summary
    if ss and getattr(ss, "requested", False) and ss.hotspots:
        max_importance = max(
            (h.get("importance_score", 0.0) for h in ss.hotspots),
            default=1.0,
        ) or 1.0
        for h in ss.hotspots:
            p = h.get("path", "")
            if p:
                semantic_hub_scores[p] = h.get("importance_score", 0.0) / max_importance

    entry_paths = {ep.path for ep in sm.entry_points}
    scored: list[tuple[float, dict[str, Any]]] = []

    from sourcecode.ranking_engine import (
        compute_impact_score, resolve_runtime_impact, resolve_framework_signal,
        _NORM_TARGET_HI,
    )

    for path in sm.file_paths:
        file_class = classifier.classify(path)
        fs = engine.score(
            path,
            git_churn=git_churn.get(path, 0),
            max_churn=max_churn,
            is_entrypoint=path in entry_paths,
        )

        if fs.score < -50:  # hard noise
            continue

        stem = Path(path).stem
        cat = file_class.category if file_class else None

        # ── Component 1: runtime_impact — execution-path role ─────────────────
        runtime_impact = resolve_runtime_impact(cat)

        # ── Component 2: dependency_centrality — call-graph importance ─────────
        # Entry points treat external HTTP/CLI callers as high centrality.
        # Isolated files (no semantic data) score 0.0 — negative weighting by omission.
        dep_centrality = semantic_hub_scores.get(path, 0.0)
        if path in entry_paths:
            dep_centrality = max(dep_centrality, 0.8)

        # ── Component 3: framework_signal_strength — annotation quality ─────────
        fw_signal = resolve_framework_signal(cat)

        # ── Component 4: change_type_severity — git churn as structural proxy ───
        churn = git_churn.get(path, 0)
        change_sev = min(churn / max(max_churn, 1), 1.0) * 0.8

        # ── Component 5: test_risk_factor — no per-file coverage data ──────────
        test_risk = 0.2

        formula_raw = compute_impact_score(
            runtime_impact, dep_centrality, fw_signal, change_sev, test_risk
        )

        # T1 override: confirmed production entrypoints → 0.92–1.00.
        # Only runtime_core / cli_entrypoint categories justify scores ≥ 0.92.
        if path in entry_paths and cat in ("runtime_core", "cli_entrypoint"):
            score_val = round(
                min(1.0, max(0.92, file_class.relevance if file_class else 0.92)), 3
            )
        else:
            score_val = round(formula_raw, 3)

        # Visibility threshold: formula score or high-relevance content exception.
        if score_val < _FILE_RELEVANCE_MIN_COMBINED:
            if not (file_class
                    and file_class.relevance > 0.45
                    and file_class.confidence in {"high", "medium"}):
                continue

        # Suppress low-confidence auxiliary/config files
        if (file_class
                and file_class.confidence == "low"
                and file_class.category in {"config", "auxiliary"}
                and score_val < 0.45):
            continue

        # relevance: content evidence only — intentionally diverges from score
        # when dep_centrality or churn are non-zero (score ≠ relevance invariant)
        relevance_val = round(file_class.relevance, 3) if file_class else round(
            min(0.39, max(0.10, runtime_impact)), 3
        )

        # sem_hub retained for signal reporting only — not used in score formula
        sem_hub = semantic_hub_scores.get(path, 0.0) * 0.30
        ranking_reasons = [r for r in fs.reasons if r != "source file"]
        if sem_hub >= 0.15:
            ranking_reasons.append("call graph hub")

        signals = _build_file_signals(file_class, path, entry_paths, ranking_reasons, sem_hub)

        item: dict[str, Any] = {
            "path": path,
            "category": file_class.category if file_class else "source",
            "confidence": file_class.confidence if file_class else "low",
            "score": score_val,
            "relevance": relevance_val,
            "reason": file_class.reason if file_class else (fs.reasons[0] if fs.reasons else "source file"),
            "evidence": file_class.evidence if file_class else [],
            "signals": signals,
        }

        if ranking_reasons:
            item["ranking_reasons"] = ranking_reasons

        # Override: universal base controller classes score as runtime_core
        if any(k in stem for k in ("GenericRestController", "GenericCRUDRestController")):
            item["category"] = "runtime_core"
            item["score"] = 0.95
            item["relevance"] = 0.95
            item["reason"] = (
                "base class for all REST controllers — extends this to get "
                "centralized exception handling via handlerException()"
            )
            item["evidence"] = ["base_rest_controller"]
            item["ranking_reasons"] = ["universal base class", "exception handling contract"]
            item["signals"] = [{"type": "framework_annotation", "strength": "strong"}]

        # sort_key = final score (rank consistency: score order = output order)
        scored.append((item["score"], item))

    # Initial sort: score desc, path asc for deterministic tie-break
    scored.sort(key=lambda x: (-x[0], x[1]["path"]))

    # Normalization: enforce minimum spread ≥ 0.4 among non-T1 files.
    # T1 files (confirmed entrypoints, 0.92–1.0) are excluded — already correct.
    # Prevents score compression when structural signals (--semantics, git) absent.
    _nonep = [(sk, it) for sk, it in scored if it["path"] not in entry_paths]
    if len(_nonep) > 1:
        _vals = [it["score"] for _, it in _nonep]
        _lo, _hi = min(_vals), max(_vals)
        _spread = _hi - _lo
        if _spread < 0.40:
            _top_cat = max(_nonep, key=lambda x: x[1]["score"])[1].get("category", "")
            _target_hi = _NORM_TARGET_HI.get(_top_cat, 0.60)
            _target_lo = max(0.10, _target_hi - 0.50)
            if _spread > 0:
                _scale = (_target_hi - _target_lo) / _spread
                for _, it in _nonep:
                    it["score"] = round(
                        max(0.0, min(1.0, _target_lo + (it["score"] - _lo) * _scale)), 3
                    )
            else:
                _mid = round((_target_hi + _target_lo) / 2.0, 3)
                for _, it in _nonep:
                    it["score"] = _mid

    # Re-sort by final score to guarantee rank consistency after normalization
    scored.sort(key=lambda x: (-x[1]["score"], x[1]["path"]))

    # Diversity cap: at most half the budget from any single category.
    # Prevents 10/10 controllers drowning out services, repositories, domain.
    _CAT_CAP = max(1, limit // 2)
    _cat_counts: dict[str, int] = {}
    result: list[dict[str, Any]] = []
    for _, item in scored:
        cat = item.get("category", "source")
        if _cat_counts.get(cat, 0) >= _CAT_CAP:
            continue
        _cat_counts[cat] = _cat_counts.get(cat, 0) + 1
        result.append(item)
        if len(result) >= limit:
            break
    return result


def _architecture_context(sm: SourceMap) -> dict[str, Any]:
    arch = sm.architecture
    if arch is not None and arch.requested:
        pattern = arch.pattern if arch.pattern not in (None, "unknown", "flat") else None
        if not pattern:
            _hint = _lightweight_arch_pattern(sm)
            if _hint:
                ctx: dict[str, Any] = {
                    "summary": sm.architecture_summary,
                    "pattern": _hint["pattern"],
                    "confidence": arch.confidence,
                    "pattern_confidence": _hint["confidence"],
                    "method": arch.method,
                }
                if arch.limitations:
                    ctx["limitations"] = arch.limitations
                return ctx
        ctx = {
            "summary": sm.architecture_summary,
            "pattern": pattern or "insufficient_evidence",
            "confidence": arch.confidence,
            "method": arch.method,
        }
        if arch.layers:
            ctx["layers"] = [
                {
                    "name": layer.name,
                    "confidence": layer.confidence,
                    "file_count": len(layer.files),
                }
                for layer in arch.layers
            ]
        else:
            ctx["no_layers_detected"] = True
        if arch.bounded_contexts:
            # BUG-04 fix: deduplicate — same package root can be detected N times
            ctx["bounded_contexts"] = list(dict.fromkeys(bc.name for bc in arch.bounded_contexts))
        if arch.ddd_layers_detected:
            ctx["ddd_layers_detected"] = arch.ddd_layers_detected
        if arch.confidence == "low" and not pattern:
            ctx["note"] = "directory structure insufficient for reliable architectural inference; use --semantics for higher accuracy"
        if arch.limitations:
            ctx["limitations"] = arch.limitations
        return ctx
    _hint = _lightweight_arch_pattern(sm)
    if _hint:
        return {
            "summary": sm.architecture_summary,
            "pattern": _hint["pattern"],
            "pattern_confidence": _hint["confidence"],
            "confidence": "low",
            "method": "filesystem_heuristic",
            "limitations": [
                "architecture analyzer not requested; pattern inferred from directory names only"
            ],
        }
    return {
        "summary": sm.architecture_summary,
        "pattern": "insufficient_evidence",
        "confidence": "low",
        "method": "not_requested",
        "limitations": [
            "architecture analyzer not requested; summary limited to stack, filesystem and entrypoint evidence"
        ],
    }


def _serialize_file_metric(m: Any) -> dict[str, Any]:
    """Serialize FileMetrics, omitting null cyclomatic_complexity when availability is unavailable.

    Prevents 100% of JS/TS/Go/Rust files from appearing as errors due to null complexity.
    The complexity_availability field already communicates the reason — the null value adds noise.
    """
    d = asdict(m)
    if d.get("complexity_availability") == "unavailable":
        d.pop("cyclomatic_complexity", None)
    return d


def _confidence_reasons(sm: SourceMap) -> dict[str, list[str]]:
    """Actionable reasons explaining low-confidence sections.

    If a section is 'low' with no derivable reasons, the caller should
    upgrade it to 'high' (the low rating was overly conservative).
    """
    reasons: dict[str, list[str]] = {}

    arch = sm.architecture
    if arch and arch.requested and arch.confidence == "low":
        arch_reasons: list[str] = []
        for lim in (arch.limitations or []):
            if lim:
                arch_reasons.append(lim)
        if sm.analysis_gaps:
            for gap in sm.analysis_gaps:
                if gap.area in ("api_contract", "architecture", "documentation"):
                    arch_reasons.append(gap.reason)
        else:
            _has_openapi = any(
                p.endswith(("openapi.yaml", "openapi.yml", "openapi.json", "swagger.yaml", "swagger.json"))
                or "swagger" in p.lower() or "springdoc" in p.lower()
                for p in sm.file_paths
            )
            if not _has_openapi:
                arch_reasons.append("No OpenAPI/Swagger spec found — API surface unverifiable")
        if arch.bounded_contexts:
            bc_count = len(arch.bounded_contexts)
            arch_reasons.append(f"bounded_contexts detected: {bc_count}")
        if arch_reasons:
            reasons["architecture"] = arch_reasons

    return reasons


def _section_confidence(sm: SourceMap) -> dict[str, str]:
    cs = sm.confidence_summary
    dep_conf = "low"
    if sm.dependency_summary is not None and sm.dependency_summary.requested:
        dep_conf = "medium"
        if sm.dependency_summary.sources and sm.dependency_summary.total_count > 0:
            dep_conf = "high"
    arch_conf = "low"
    if sm.architecture is not None and sm.architecture.requested:
        arch_conf = sm.architecture.confidence
    file_conf = "medium" if sm.file_paths else "low"
    return {
        "stack": cs.stack_confidence if cs else "low",
        "entrypoints": cs.entry_point_confidence if cs else "low",
        "dependencies": dep_conf,
        "architecture": arch_conf,
        "file_relevance": file_conf,
    }


def compact_view(sm: SourceMap, *, no_tree: bool = False, full: bool = False) -> dict[str, Any]:
    """Context package ready for prompt or handoff (~300-500 tokens).

    Answers: what it is, where it enters, what depends on what,
    what signals matter, and what uncertainty exists.

    Includes: project_type, project_summary, architecture_summary,
    stacks (minimal), entry_points (path+kind only), key_dependencies (name+version+role),
    env_summary (when analyzed), code_notes_summary (when analyzed),
    confidence (overall only), analysis_gaps.

    Excludes: file_tree, raw dependency lists, docs, module_graph, verbose metadata.
    """
    # Key dependencies — name + version + role + risk_flags
    key_deps: Any = None
    if sm.dependency_summary is not None and sm.dependency_summary.requested:
        key_deps = []
        for d in sm.key_dependencies:
            if (d.role or "unknown") not in _PRODUCTION_DEP_ROLES or d.scope in {"dev"}:
                continue
            entry: dict[str, Any] = {"name": d.name}
            if d.declared_version:
                entry["version"] = d.declared_version
            if d.role and d.role != "runtime":
                entry["role"] = d.role
            flags = _dep_risk_flags(d.name, d.declared_version)
            if flags:
                entry["risk_flags"] = flags
            key_deps.append(entry)
        key_deps = key_deps[:_KEY_DEPS_CAP]

    # Dependency summary — requested flag + count + source only
    dep_summary_dict: Any = None
    if sm.dependency_summary is not None and sm.dependency_summary.requested:
        ds = sm.dependency_summary
        dep_summary_dict = {
            "requested": True,
            "total_count": ds.total_count,
            "direct": ds.direct_count,
            **({"sources": ds.sources} if ds.sources else {}),
        }

    # Env map — key + required + category only (drop type_hint, files list)
    env_summary_dict: Any = None
    env_map_items: Any = None
    if sm.env_summary is not None and sm.env_summary.requested:
        env_summary_dict = {
            "total": sm.env_summary.total,
            "required": sm.env_summary.required_count,
            **({"categories": sm.env_summary.categories} if sm.env_summary.categories else {}),
        }
        if sm.env_map:
            _sorted_env = sorted(
                sm.env_map,
                key=lambda e: (not getattr(e, "required", False), getattr(e, "key", "")),
            )
            env_map_items = [
                {
                    "key": getattr(e, "key", ""),
                    **({"required": True} if getattr(e, "required", False) else {}),
                    **({"category": getattr(e, "category", None)} if getattr(e, "category", None) else {}),
                }
                for e in _sorted_env[:_ENV_MAP_CAP]
            ]

    # Code notes — kind + path + line + truncated text only
    code_notes_summary_dict: Any = None
    code_notes_items: Any = None
    if sm.code_notes_summary is not None and sm.code_notes_summary.requested:
        cn = sm.code_notes_summary
        by_kind = {k: v for k, v in cn.by_kind.items() if v > 0}
        code_notes_summary_dict = {"total": cn.total, **({"by_kind": by_kind} if by_kind else {})}
        if sm.code_notes:
            _SEVERITY_ORDER = {"BUG": 0, "FIXME": 1, "DEPRECATED": 2, "TODO": 3, "HACK": 4, "WARNING": 5}
            _sorted_notes = sorted(
                sm.code_notes,
                key=lambda n: (_SEVERITY_ORDER.get(getattr(n, "kind", "").upper(), 9), getattr(n, "path", "")),
            )
            code_notes_items = [
                {
                    "kind": getattr(n, "kind", ""),
                    "path": getattr(n, "path", ""),
                    "line": getattr(n, "line", None),
                    **({"text": _truncate_note(getattr(n, "text", ""), 120)} if getattr(n, "text", "") else {}),
                }
                for n in _sorted_notes[:_CODE_NOTES_CAP]
            ]

    # Entry points — bootstrap-prioritized; structured when bootstrap classes detected
    ep_groups = _entry_point_groups(sm.entry_points)
    _bootstrap_struct = _bootstrap_structured(sm.entry_points)
    if _bootstrap_struct:
        entry_points_compact: Any = _bootstrap_struct
    else:
        entry_points_compact = [
            {
                "path": ep["path"],
                **({"kind": ep["kind"]} if ep.get("kind") else {}),
                **({"confidence": ep["confidence"]} if ep.get("confidence") else {}),
            }
            for ep in ep_groups["production"][:_EP_PRODUCTION_CAP]
        ]

    # Stacks — deduplicated: for same stack name, prefer manifest over heuristic
    _stack_best: dict[str, Any] = {}
    for _s in sm.stacks:
        _existing = _stack_best.get(_s.stack)
        if _existing is None:
            _stack_best[_s.stack] = _s
        elif _s.detection_method != "heuristic" and _existing.detection_method == "heuristic":
            _stack_best[_s.stack] = _s
        elif _s.primary and not _existing.primary and _s.detection_method == _existing.detection_method:
            _stack_best[_s.stack] = _s
    stacks_compact = [
        {
            "stack": s.stack,
            "detection_method": s.detection_method,
            "confidence": s.confidence,
            **({"primary": True} if s.primary else {}),
            **({"frameworks": list(dict.fromkeys(f.name for f in s.frameworks))} if s.frameworks else {}),
            **({"package_manager": s.package_manager} if s.package_manager else {}),
        }
        for s in _stack_best.values()
    ]

    # Confidence — overall only + anomalies + factors (P3: traceability)
    conf_dict: Any = None
    if sm.confidence_summary is not None:
        cs = sm.confidence_summary
        _sections = _section_confidence(sm)
        conf_dict = {
            "overall": cs.overall,
            "stack": cs.stack_confidence,
            "entry_points": cs.entry_point_confidence,
            "sections": _sections,
        }
        if cs.anomalies:
            conf_dict["anomalies"] = cs.anomalies
        # Traceability: expose what drove the score so the caller can understand
        # why --compact may differ from --agent (architecture analyzer not run)
        if cs.factors:
            conf_dict["factors"] = cs.factors
        # BUG-07 fix: ensure overall is consistent with sections.architecture.
        # In --compact mode the ConfidenceAnalyzer builds sm_for_conf without
        # architecture (not yet analyzed), so overall stays "high" based on
        # stack+entry_points alone.  But _section_confidence reads the real
        # sm.architecture — if that section is "low", overall cannot be "high".
        if _sections.get("architecture") == "low" and conf_dict["overall"] == "high":
            conf_dict["overall"] = "medium"
            _factors = list(conf_dict.get("factors") or [])
            _factors.append("architecture.confidence=low → overall capped at medium (consistency fix)")
            conf_dict["factors"] = _factors

    # Analysis gaps
    gaps_list: Any = None
    if sm.analysis_gaps:
        gaps_list = [
            {"area": g.area, "reason": g.reason, "impact": g.impact}
            for g in sm.analysis_gaps
        ]

    # Java/Spring operational context
    _language_version = getattr(sm, "language_version", None)
    _packaging = getattr(sm, "packaging", None)
    _app_server = getattr(sm, "app_server_hint", None)
    _sb_version = _spring_boot_version(sm)
    _deployment: Any = None
    if _packaging or _app_server or _sb_version:
        _deployment = {}
        if _sb_version:
            _deployment["spring_boot_version"] = _sb_version
        if _packaging:
            _deployment["packaging"] = _packaging
        if _app_server:
            _deployment["app_server_hint"] = _app_server
    _deploy_risks = _project_deployment_risks(sm)
    _sec_root = Path(sm.metadata.analyzed_path) if sm.metadata.analyzed_path else None
    _security_surface = _security_surface_from_eps(sm.entry_points, root=_sec_root, file_paths=sm.file_paths)
    _mybatis = _mybatis_pairing(sm, full=full)
    _transactional = _transactional_summary(sm, full=full)
    _git_ctx = _compact_git_context(sm)
    _spring_profiles = _spring_profiles_context(sm)

    # Suppress empty optional sections (no signal value)
    _effective_env_summary = env_summary_dict if (env_summary_dict and env_summary_dict.get("total", 0) > 0) else None
    _effective_env_map = env_map_items if _effective_env_summary else None
    _effective_notes_summary = code_notes_summary_dict if (code_notes_summary_dict and code_notes_summary_dict.get("total", 0) > 0) else None
    _effective_notes = code_notes_items if _effective_notes_summary else None

    result: dict[str, Any] = {
        "schema_version": sm.metadata.schema_version,
        "project_type": sm.project_type,
        "project_summary": sm.project_summary,
        "architecture_summary": sm.architecture_summary,
        "stacks": stacks_compact,
        "entry_points": entry_points_compact,
        "dependency_summary": dep_summary_dict,
        "key_dependencies": key_deps,
        "env_summary": _effective_env_summary,
        "env_map": _effective_env_map,
        "code_notes_summary": _effective_notes_summary,
        "code_notes": _effective_notes,
        "confidence_summary": conf_dict,
        "analysis_gaps": gaps_list,
    }
    _jndi = _jndi_datasources(sm)
    if _language_version:
        result["language_version"] = _language_version
    if _deployment or _jndi:
        _deployment = _deployment or {}
        if _jndi:
            _deployment["jndi_datasources"] = _jndi
            _deployment["jndi_note"] = (
                "JNDI datasources require WebLogic/WildFly binding; "
                "spring.datasource.* properties only apply to embedded Tomcat"
            )
        result["deployment"] = _deployment
    if _deploy_risks:
        result["deployment_risks"] = _deploy_risks
    if _security_surface:
        result["security_surface"] = _security_surface
    if _mybatis:
        result["mybatis"] = _mybatis
    if _transactional:
        result["transactional_boundaries"] = _transactional
    if _git_ctx:
        result["git_context"] = _git_ctx
    # Angular structural analysis (GAP-10)
    if sm.project_type in ("angular-spa", "webapp") or any(
        any(f.name == "Angular" for f in s.frameworks) for s in sm.stacks
    ):
        _ang = _angular_analysis(sm)
        if _ang and (_ang.get("component_count", 0) > 0 or _ang.get("angular_version")):
            result["angular_analysis"] = _ang
    if _spring_profiles:
        result["spring_profiles"] = _spring_profiles
    _always_include = {"project_type", "project_summary", "architecture_summary", "dependency_summary"}
    return {k: v for k, v in result.items() if v is not None or k in _always_include}


def normalize_source_map(sm: SourceMap) -> SourceMap:
    """Fill in typed empty defaults for optional analyzer fields.

    Fields controlled by flags (--architecture, --graph-modules) are None when
    the flag is absent.  Downstream consumers and tests then need null-checks
    everywhere.  This layer converts None → a well-typed default so the output
    schema is always structurally complete.

    The ``requested=False`` sentinel on each default tells consumers the
    analysis was not requested, without forcing them to branch on None.
    """
    changes: dict[str, Any] = {}

    # architecture: always an ArchitectureAnalysis, never None
    if sm.architecture is None:
        changes["architecture"] = ArchitectureAnalysis(requested=False)

    # module_graph: always a ModuleGraph (possibly empty), never None.
    # module_graph_summary is kept in sync as a convenience field.
    if sm.module_graph is None:
        empty_graph = ModuleGraph(summary=ModuleGraphSummary(requested=False))
        changes["module_graph"] = empty_graph
        if sm.module_graph_summary is None:
            changes["module_graph_summary"] = empty_graph.summary
    elif sm.module_graph_summary is None:
        # graph exists but summary was never set — sync it
        changes["module_graph_summary"] = sm.module_graph.summary

    # dependencies is already list[DependencyRecord] by default_factory, but
    # guard against any future refactor that could accidentally set it to None
    if sm.dependencies is None:  # type: ignore[comparison-overlap]
        changes["dependencies"] = []

    normalized_eps = [normalize_entry_point(ep) for ep in sm.entry_points]
    if normalized_eps != sm.entry_points:
        changes["entry_points"] = normalized_eps

    return replace(sm, **changes) if changes else sm


def validate_source_map(sm: SourceMap) -> None:
    """Assert structural schema contracts on a (already normalised) SourceMap.

    Call this *after* normalize_source_map() so that the checks below catch
    bugs in the normaliser itself or in code that bypasses it.

    Raises:
        ValueError: listing every violated contract, never just the first.
    """
    errors: list[str] = []

    # --- architecture ---
    if sm.architecture is None:
        errors.append("architecture must not be null (call normalize_source_map first)")
    else:
        if not isinstance(sm.architecture.domains, list):
            errors.append(
                f"architecture.domains must be list, got {type(sm.architecture.domains).__name__}"
            )
        if sm.architecture.confidence not in ("high", "medium", "low"):
            errors.append(
                f"architecture.confidence must be high|medium|low, "
                f"got {sm.architecture.confidence!r}"
            )

    # --- module_graph ---
    if sm.module_graph is None:
        errors.append("module_graph must not be null (call normalize_source_map first)")
    else:
        if not isinstance(sm.module_graph.nodes, list):
            errors.append(
                f"module_graph.nodes must be list, got {type(sm.module_graph.nodes).__name__}"
            )
        if not isinstance(sm.module_graph.edges, list):
            errors.append(
                f"module_graph.edges must be list, got {type(sm.module_graph.edges).__name__}"
            )

    # --- dependencies ---
    if not isinstance(sm.dependencies, list):
        errors.append(
            f"dependencies must be list, got {type(sm.dependencies).__name__}"
        )

    if errors:
        bullet = "\n  - "
        raise ValueError(
            f"SourceMap schema violations ({len(errors)}):{bullet}"
            + bullet.join(errors)
        )


_GRAPH_CODE_EXTENSIONS: frozenset[str] = frozenset({
    ".py", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs",
    ".go", ".rs", ".java", ".kt", ".rb",
})


def _rule_dependency_graph(
    sm: SourceMap,
    known_paths: set[str],
    findings: list[str],
) -> None:
    """Rule 1 — dependency/graph consistency.

    Sub-rule 1a: every GraphNode whose path ends with a code extension must
    exist in file_tree.  A mismatch means the graph references phantom files
    the scanner never found.

    Sub-rule 1b: if the graph has *external*-looking edge targets (package
    names without path separators), every non-transitive dependency should
    appear in those targets.  This rule is intentionally skipped when the
    graph only tracks internal module-to-module edges, avoiding false
    positives for projects whose graph_analyzer does not emit external imports.
    """
    if not sm.module_graph.summary.requested:
        return

    # 1a — graph node paths must be in file_tree (aggregate)
    phantom_paths = [
        node.path
        for node in sm.module_graph.nodes
        if Path(node.path).suffix.lower() in _GRAPH_CODE_EXTENSIONS
        and node.path not in known_paths
    ]
    if phantom_paths:
        sample = ", ".join(phantom_paths[:3])
        findings.append(
            f"[dependency_graph] {len(phantom_paths)} graph node(s) reference paths "
            f"not in file_tree: {sample}"
            + (f" (+{len(phantom_paths) - 3} more)" if len(phantom_paths) > 3 else "")
        )

    # 1b — dep names should appear in external-facing edge targets
    if sm.dependency_summary is None or not sm.dependency_summary.requested:
        return
    if not sm.module_graph.edges:
        return

    # Only apply when the graph contains at least one package-like target
    # (no path separator, no code extension) — signals external import tracking.
    pkg_targets = {
        e.target.lower()
        for e in sm.module_graph.edges
        if "/" not in e.target
        and not Path(e.target).suffix.lower() in _GRAPH_CODE_EXTENSIONS
    }
    if not pkg_targets:
        return  # graph has only internal file edges; dep↔edge check not applicable

    missing_deps = [
        dep.name
        for dep in sm.dependencies
        if dep.scope != "transitive"
        and not any(
            dep.name.lower().replace("-", "_") in t.replace("-", "_")
            for t in pkg_targets
        )
    ]
    if missing_deps:
        sample = ", ".join(missing_deps[:5])
        findings.append(
            f"[dependency_graph] {len(missing_deps)} manifest dep(s) absent from "
            f"graph external edges: {sample}"
            + (f" (+{len(missing_deps) - 5} more)" if len(missing_deps) > 5 else "")
        )


def _rule_semantic_file_tree(
    sm: SourceMap,
    known_paths: set[str],
    findings: list[str],
) -> None:
    """Rule 2 — semantic_links paths must exist in file_tree.

    Both the importer and the source (when not external) must be files the
    scanner actually found.  An orphan path means the semantic_analyzer
    resolved a symbol to a file that does not belong to the project.
    """
    importer_miss_paths = [
        link.importer_path
        for link in sm.semantic_links
        if link.importer_path not in known_paths
    ]
    source_miss_paths = [
        link.source_path
        for link in sm.semantic_links
        if link.source_path is not None
        and not link.is_external
        and link.source_path not in known_paths
    ]
    total = len(importer_miss_paths) + len(source_miss_paths)
    if total > 0:
        parts: list[str] = []
        if importer_miss_paths:
            sample = ", ".join(dict.fromkeys(importer_miss_paths[:2]))
            parts.append(f"{len(importer_miss_paths)} importer(s) (e.g. {sample})")
        if source_miss_paths:
            sample = ", ".join(dict.fromkeys(source_miss_paths[:2]))
            parts.append(f"{len(source_miss_paths)} source(s) (e.g. {sample})")
        findings.append(
            f"[semantic_file_tree] {total} semantic link path(s) not in file_tree: "
            + "; ".join(parts)
            + " — may indicate workspace-relative paths"
        )


def _rule_architecture_graph(
    sm: SourceMap,
    known_paths: set[str],
    findings: list[str],
) -> None:
    """Rule 3 — architecture domain files must be a subset of file_tree.

    The architecture_analyzer clusters files into domains.  Every file it
    assigns to a domain should be a file the scanner found.  A mismatch
    means the architecture_analyzer is referencing phantom paths, likely
    from a stale file_paths list or a mis-configured root.
    """
    if not sm.architecture.requested:
        return
    all_phantom: list[str] = []
    domain_counts: list[str] = []
    for domain in sm.architecture.domains:
        phantom_files = [p for p in domain.files if p not in known_paths]
        if phantom_files:
            all_phantom.extend(phantom_files[:2])
            domain_counts.append(f"'{domain.name}': {len(phantom_files)}")
    if domain_counts:
        sample = ", ".join(dict.fromkeys(all_phantom[:3]))
        findings.append(
            f"[architecture_graph] {len(domain_counts)} domain(s) reference phantom paths "
            f"(e.g. {sample}): "
            + ", ".join(domain_counts[:5])
            + ("..." if len(domain_counts) > 5 else "")
        )


def validate_cross_analyzer_consistency(
    sm: SourceMap,
    *,
    strict: bool = False,
) -> list[str]:
    """Check semantic alignment across analyzer outputs.

    Applies three rules (see helpers above):
      Rule 1 — dependency/graph: graph node paths and external edge targets
               must be consistent with declared dependencies and file_tree.
      Rule 2 — semantic/file_tree: SymbolLink paths must exist in file_tree.
      Rule 3 — architecture/graph: domain files must exist in file_tree.

    Args:
        sm:     A SourceMap that has already been normalised and structurally
                validated (call normalize_source_map + validate_source_map first).
        strict: If True, raises ValueError listing all findings.
                If False (default), returns the findings list so the caller
                can log warnings without aborting the pipeline.

    Returns:
        List of human-readable finding strings (empty when all rules pass).
    """
    findings: list[str] = []
    known = set(sm.file_paths)

    _rule_dependency_graph(sm, known, findings)
    _rule_semantic_file_tree(sm, known, findings)
    _rule_architecture_graph(sm, known, findings)

    if strict and findings:
        bullet = "\n  - "
        raise ValueError(
            f"Cross-analyzer consistency violations ({len(findings)}):{bullet}"
            + bullet.join(findings)
        )

    return findings


def _angular_analysis(sm: "SourceMap") -> "Optional[dict[str, Any]]":
    """Extract Angular structural metrics for TypeScript/Angular projects (GAP-10)."""
    import json as _json
    import re as _re

    ts_files = [p for p in sm.file_paths if p.endswith(".ts") and not p.endswith(".d.ts")]
    if not ts_files:
        return None

    root = Path(sm.metadata.analyzed_path) if sm.metadata.analyzed_path else Path(".")

    component_count = 0
    service_count = 0
    lazy_routes_count = 0
    akita_stores = 0
    standalone_components = False
    route_paths: list[str] = []

    for rel in ts_files:
        try:
            content = (root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        component_count += content.count("@Component(")
        service_count += content.count("@Injectable(")
        # Count lazy route patterns: `loadChildren:` (property syntax used in route
        # configs) and `loadComponent:` (standalone component lazy loading). The old
        # `loadChildren(` form counted zero because Angular uses property syntax, not
        # a function call (BUG-5).
        fname_lower = rel.replace("\\", "/").split("/")[-1].lower()
        _is_routing_file = (
            "routing" in fname_lower
            or fname_lower in ("app.routes.ts", "app-routing.module.ts")
            or fname_lower.endswith(".routes.ts")
        )
        lazy_routes_count += content.count("loadChildren:")
        lazy_routes_count += content.count("loadComponent:")
        if _is_routing_file:
            # Also count standalone dynamic imports that aren't already caught above
            _lc_imports = content.count("import(") - content.count("loadChildren:") - content.count("loadComponent:")
            if _lc_imports > 0:
                lazy_routes_count += _lc_imports
        akita_stores += content.count("@StoreConfig(")
        if not standalone_components and "bootstrapApplication(" in content:
            standalone_components = True
        # Route tree: parse path: '...' in routing files
        fname = rel.replace("\\", "/").split("/")[-1]
        if "routing" in fname or fname in ("app.routes.ts",):
            for m in _re.finditer(r"path\s*:\s*['\"]([^'\"]*)['\"]", content):
                val = m.group(1)
                if val and val not in route_paths:
                    route_paths.append(val)

    # Angular version from package.json — check root first, then subdirectories.
    # In monorepos (Java + Angular), the Angular package.json is in a subdirectory
    # like frontend/ and not at the repo root. We probe candidate locations.
    angular_version: Optional[str] = None

    def _read_angular_version_from_pkg(pkg_path: Path) -> Optional[str]:
        """Extract @angular/core version from a package.json file."""
        try:
            pkg = _json.loads(pkg_path.read_text(encoding="utf-8", errors="replace"))
            deps = {
                **(pkg.get("dependencies") or {}),
                **(pkg.get("devDependencies") or {}),
                **(pkg.get("peerDependencies") or {}),
            }
            av = deps.get("@angular/core")
            if av and isinstance(av, str):
                return av.lstrip("^~>=")
        except Exception:
            pass
        return None

    # 1. Try root package.json first (fastest, most common for pure Angular projects)
    _root_pkg = root / "package.json"
    if _root_pkg.exists():
        angular_version = _read_angular_version_from_pkg(_root_pkg)

    # 2. If not found at root, search subdirectory package.json files.
    # Limit to ts_files-derived subdirs to avoid scanning the whole repo.
    if angular_version is None and ts_files:
        _candidate_dirs: set[str] = set()
        for ts_rel in ts_files[:200]:  # sample first 200 ts files
            parts = ts_rel.replace("\\", "/").split("/")
            if len(parts) >= 2:
                _candidate_dirs.add(parts[0])  # top-level subdir (e.g. "frontend")
        for subdir in sorted(_candidate_dirs):
            _sub_pkg = root / subdir / "package.json"
            if _sub_pkg.exists():
                _v = _read_angular_version_from_pkg(_sub_pkg)
                if _v:
                    angular_version = _v
                    break

    # Also check angular.json for entry point
    entry_point: Optional[str] = None
    angular_json = root / "angular.json"
    if angular_json.exists():
        try:
            aj = _json.loads(angular_json.read_text(encoding="utf-8", errors="replace"))
            projects = aj.get("projects") or {}
            for proj in projects.values():
                main = (
                    (proj.get("architect") or {})
                    .get("build", {})
                    .get("options", {})
                    .get("main")
                )
                if main:
                    entry_point = main
                    break
        except Exception:
            pass

    return {
        "angular_version": angular_version,
        "standalone_components": standalone_components,
        "lazy_routes_count": lazy_routes_count,
        "akita_stores": akita_stores,
        "route_tree": route_paths[:20],
        "component_count": component_count,
        "service_count": service_count,
        **({"entry_point": entry_point} if entry_point else {}),
    }


def compute_context_limit(mode: str, normal: int = 20) -> int:
    """Return bounded file-relevance limit for agent context modes.

    Never returns 'all files' — unbounded expansion degrades signal quality
    by flooding context with low-relevance files before trim_to_budget cuts
    indiscriminately by byte count rather than relevance rank.

    Modes:
        normal: TOP_N = normal (default 20)
        full:   min(normal * 2, 50) — better recall, still bounded
        deep:   min(normal * 4, 100) — maximum context, explicit opt-in
    """
    if mode == "full":
        return min(normal * 2, 50)
    if mode == "deep":
        return min(normal * 4, 100)
    return normal  # normal mode


def expand_relevance_window(files: list, mode: str, normal: int = 20) -> list:
    """Return a bounded-relevance slice of pre-scored files.

    Assumes files is already sorted descending by score (guaranteed by
    _file_relevance). Enforces compute_context_limit(mode, normal) cap.
    Deterministic: same input → same output.
    """
    limit = compute_context_limit(mode, normal)
    return files[:limit]


def agent_view(sm: SourceMap, *, full: bool = False) -> dict[str, Any]:
    """Opinionated output for AI agents — structured, noise-free, gap-aware.

    Output order:
        1. project             → identity: type, summary, primary stack, frameworks
        2. entry_points        → where execution starts (path+kind+confidence)
        3. architecture        → pattern, layers (capped at 5)
        4. file_relevance      → top-20 scored files — primary agent value-add
        5. runtime_packages    → monorepo package roles (when available)
        6. key_dependencies    → production deps: name+version+role+risk_flags (cap 20)
        7. suspicious_dependencies → declared but never imported (cap 5)
        8. signals             → env summary, top-5 code notes, tests, Spring/JVM context
        9. git_context         → top-5 hotspots, branch, uncommitted count
        10. confidence_summary → overall quality + anomalies (no internal signals)
        11. confidence_reasons → actionable reasons for low-confidence sections
        12. analysis_gaps      → what's uncertain or missing

    Never includes: file_tree, file_paths, raw dep lists, dep_groups detail,
    hard/soft/ignored signals, env var key list, metrics, docs, agent_mode meta.
    """
    _AGENT_KEY_DEPS_CAP = 20
    _AGENT_LAYERS_CAP = 5
    _AGENT_CODE_NOTES_CAP = 5

    # ── 1. Identity ──────────────────────────────────────────────────────────
    primary = next((s for s in sm.stacks if s.primary), sm.stacks[0] if sm.stacks else None)

    project: dict[str, Any] = {
        "type": sm.project_type,
        "summary": sm.project_summary,
    }
    if primary:
        project["primary_stack"] = primary.stack
        if primary.frameworks:
            project["frameworks"] = [f.name for f in primary.frameworks]
        if primary.package_manager:
            project["package_manager"] = primary.package_manager
        if primary.root and primary.root != ".":
            project["root"] = primary.root

    secondary = [s for s in sm.stacks if not s.primary and s.stack != (primary.stack if primary else "")]
    if secondary:
        project["secondary_stacks"] = sorted({s.stack for s in secondary})

    # Java operational context in project block
    _lv = getattr(sm, "language_version", None)
    _pkg = getattr(sm, "packaging", None)
    _app_srv = getattr(sm, "app_server_hint", None)
    _sb_ver = _spring_boot_version(sm)
    if _lv:
        project["language_version"] = _lv
    if _pkg or _app_srv or _sb_ver:
        _depl: dict[str, Any] = {}
        if _sb_ver:
            _depl["spring_boot_version"] = _sb_ver
        if _pkg:
            _depl["packaging"] = _pkg
        if _app_srv:
            _depl["app_server_hint"] = _app_srv
        project["deployment"] = _depl
    _proj_risks = _project_deployment_risks(sm)
    if _proj_risks:
        project["deployment_risks"] = _proj_risks

    result: dict[str, Any] = {"project": project}

    # ── 2. Entry points: bootstrap-prioritized, then production ─────────────
    _bs = _bootstrap_structured(sm.entry_points)
    if _bs:
        result["entry_points"] = _bs
    else:
        ep_groups = _entry_point_groups(sm.entry_points)
        result["entry_points"] = [
            {
                "path": ep["path"],
                **({"kind": ep["kind"]} if ep.get("kind") else {}),
                **({"confidence": ep["confidence"]} if ep.get("confidence") else {}),
            }
            for ep in ep_groups["production"][:_EP_PRODUCTION_CAP]
        ]

    # ── 3. Architecture — pattern + layers (capped) ───────────────────────────
    _arch_ctx = _architecture_context(sm)
    if "layers" in _arch_ctx and len(_arch_ctx["layers"]) > _AGENT_LAYERS_CAP:
        _arch_ctx = dict(_arch_ctx)
        _arch_ctx["layers"] = _arch_ctx["layers"][:_AGENT_LAYERS_CAP]
    result["architecture"] = _arch_ctx

    # ── 4. File relevance: top-scored files — primary agent value-add ────────
    # P0 FIX: --full must NOT pass _total_paths as limit (unbounded expansion).
    # Unbounded expansion floods context with low-relevance files; trim_to_budget
    # then cuts indiscriminately by byte count, not by relevance rank.
    # Bounded strategy: normal=20, full=min(20*2,50)=40, deep=min(20*4,100)=80.
    _FR_AGENT_CAP = 20
    _total_paths = len(sm.file_paths)
    _fr_mode = "full" if full else "normal"
    _fr_limit = compute_context_limit(_fr_mode, _FR_AGENT_CAP)
    relevant_files = _file_relevance(sm, limit=_fr_limit)
    if relevant_files:
        result["file_relevance"] = relevant_files
        if _total_paths > _fr_limit:
            result["file_relevance_hint"] = (
                f"Showing top {_fr_limit}/{_total_paths} files by score "
                f"({'--full' if full else 'normal'} mode, bounded for signal quality). "
                f"Use --deep for up to {compute_context_limit('deep', _FR_AGENT_CAP)} files."
            )

    # ── 5. Monorepo package roles (when available), capped ───────────────────
    if sm.monorepo_packages:
        _noise_roles = {"benchmark_layer", "tooling_layer", "docs_layer", "test_layer"}
        operational_pkgs = [
            {"path": p.path, "role": p.architectural_role, "criticality": p.criticality}
            for p in sm.monorepo_packages
            if p.architectural_role not in _noise_roles
        ]
        if operational_pkgs:
            result["runtime_packages"] = operational_pkgs[:_MONOREPO_PKGS_CAP]

    # ── 6. Key dependencies: name+version+role+risk_flags only, cap 20 ───────
    # dep_groups verbose detail (production_dependencies, dev_tools, etc.) excluded —
    # fully overlaps key_dependencies with added noise from full asdict fields.
    production_key_deps = [
        d for d in sm.key_dependencies
        if (d.role or "unknown") in _PRODUCTION_DEP_ROLES and d.scope not in {"dev"}
    ]
    if sm.dependency_summary and sm.dependency_summary.requested and production_key_deps:
        _kd_list = []
        for d in production_key_deps[:_AGENT_KEY_DEPS_CAP]:
            entry: dict[str, Any] = {"name": d.name}
            if d.declared_version:
                entry["version"] = d.declared_version
            if d.role and d.role != "runtime":
                entry["role"] = d.role
            flags = _dep_risk_flags(d.name, d.declared_version)
            if flags:
                entry["risk_flags"] = flags
            _kd_list.append(entry)
        result["key_dependencies"] = _kd_list

    # ── 7. Suspicious dependencies: declared but no static import observed ────
    dep_groups = _dependency_groups(sm)
    if dep_groups["suspicious_dependencies"]:
        result["suspicious_dependencies"] = [
            {"name": d.get("name", ""), "reason": d.get("reason", "")}
            for d in dep_groups["suspicious_dependencies"][:_SECONDARY_DEPS_CAP]
        ]

    # ── 8. Signals — compact operational context ─────────────────────────────
    signals: dict[str, Any] = {}

    if sm.env_summary and sm.env_summary.requested and sm.env_summary.total > 0:
        signals["env_vars"] = {
            "total": sm.env_summary.total,
            "required": sm.env_summary.required_count,
        }
        if sm.env_summary.categories:
            signals["env_vars"]["categories"] = sm.env_summary.categories
        _sp_ctx = _spring_profiles_context(sm)
        if _sp_ctx:
            signals["spring_profiles"] = _sp_ctx
        elif (sm.env_summary.spring_profiles or sm.env_summary.profiles_scanned):
            _raw_profiles = sm.env_summary.spring_profiles or sm.env_summary.profiles_scanned
            signals["env_vars"]["spring_profiles"] = sorted(set(_raw_profiles))

    if sm.code_notes_summary and sm.code_notes_summary.requested and sm.code_notes_summary.total > 0:
        by_kind = {k: v for k, v in sm.code_notes_summary.by_kind.items() if v > 0}
        _code_notes_signal: dict[str, Any] = {}
        if by_kind:
            _code_notes_signal = {"total": sm.code_notes_summary.total, "by_kind": by_kind}
        if sm.code_notes:
            _SEVERITY_ORDER = {"BUG": 0, "FIXME": 1, "DEPRECATED": 2, "TODO": 3, "HACK": 4, "WARNING": 5}
            _sorted_notes = sorted(
                sm.code_notes,
                key=lambda n: (_SEVERITY_ORDER.get(getattr(n, "kind", "").upper(), 9), getattr(n, "path", "")),
            )
            _code_notes_signal["top"] = [
                {
                    "kind": getattr(n, "kind", ""),
                    "path": getattr(n, "path", ""),
                    "line": getattr(n, "line", None),
                    **({"text": _truncate_note(getattr(n, "text", ""), 120)} if getattr(n, "text", "") else {}),
                }
                for n in _sorted_notes[:_AGENT_CODE_NOTES_CAP]
                if getattr(n, "kind", "").upper() in _SEVERITY_ORDER
            ]
        if _code_notes_signal:
            signals["code_notes"] = _code_notes_signal
        if sm.code_notes_summary.adr_count > 0:
            signals["adrs"] = sm.code_notes_summary.adr_count

    has_tests = any(
        "/test" in p or "/tests" in p or "/spec" in p or p.startswith("test")
        for p in sm.file_paths
    )
    if has_tests:
        signals["has_tests"] = True

    # Semantic hotspots (populated when --semantics was passed)
    if sm.semantic_summary is not None and sm.semantic_summary.requested:
        sem = sm.semantic_summary
        sem_info: dict[str, Any] = {
            "files_analyzed": sem.files_analyzed,
            "symbols": sem.symbol_count,
            "calls": sem.call_count,
            "links": sem.link_count,
            "languages": sem.languages,
        }
        if sem.coverage_pct is not None:
            sem_info["coverage_pct"] = sem.coverage_pct
            sem_info["coverage_confidence"] = sem.coverage_confidence
        if sem.truncated:
            sem_info["truncated"] = True
        if sem.hotspots:
            sem_info["hotspots"] = sem.hotspots[:10]
        signals["semantic_graph"] = sem_info

    # Java/Spring: security surface, ORM structure, transactional boundaries
    _av_root = Path(sm.metadata.analyzed_path) if sm.metadata.analyzed_path else None
    _sec_surf = _security_surface_from_eps(sm.entry_points, root=_av_root, file_paths=sm.file_paths)
    if _sec_surf:
        signals["security_surface"] = _sec_surf
    _mb = _mybatis_pairing(sm, full=full)
    if _mb:
        signals["mybatis"] = _mb
    _txn = _transactional_summary(sm, full=full)
    if _txn:
        signals["transactional_boundaries"] = _txn

    # IC-005: Spring event flows (@EventListener / publishEvent)
    _evf = _spring_event_signal(sm)
    if _evf:
        signals["event_flows"] = _evf

    if signals:
        result["signals"] = signals

    # ── 8b. Angular structural analysis (GAP-10) ──────────────────────────────
    if sm.project_type in ("angular-spa", "webapp") or any(
        any(f.name == "Angular" for f in s.frameworks) for s in sm.stacks
    ):
        _ang = _angular_analysis(sm)
        if _ang and (_ang.get("component_count", 0) > 0 or _ang.get("angular_version")):
            result["angular_analysis"] = _ang

    # ── 9. Git context — lightweight (top-5 hotspots, branch, uncommitted count)
    _gc = _compact_git_context(sm)
    if _gc:
        result["git_context"] = _gc

    # ── 10. Confidence summary — overall quality + anomalies only ─────────────
    # hard_signals/soft_signals/ignored_signals are detection internals — excluded.
    if sm.confidence_summary is not None:
        cs = sm.confidence_summary
        conf: dict[str, Any] = {
            "overall": cs.overall,
            "stack": cs.stack_confidence,
            "entry_points": cs.entry_point_confidence,
            "sections": _section_confidence(sm),
        }
        if cs.anomalies:
            conf["anomalies"] = cs.anomalies
        # Traceability: expose what drove the score (P3 fix — single source of truth)
        if cs.factors:
            conf["factors"] = cs.factors
        result["confidence_summary"] = conf

    # ── 11. Confidence reasons: actionable explanation of low-confidence sections
    # NOTE: Do NOT mutate confidence_summary here — rendering must never recalculate
    # scores. The upgrade logic (low → high when no reasons found) belongs in
    # ConfidenceAnalyzer, not in the renderer. See confidence_analyzer.py factors.
    _conf_reasons = _confidence_reasons(sm)
    if _conf_reasons:
        result["confidence_reasons"] = _conf_reasons

    # ── 12. Analysis gaps ─────────────────────────────────────────────────────
    analysis_gaps: list[dict[str, Any]] = []

    if sm.analysis_gaps:
        analysis_gaps = [asdict(g) for g in sm.analysis_gaps]
    elif sm.confidence_summary is None:
        # Fallback gap derivation only when confidence_analyzer was not run at all
        # (empty sm.analysis_gaps with sm.confidence_summary set means analyzer ran → 0 gaps)
        if not sm.entry_points:
            analysis_gaps.append({
                "area": "entry_points",
                "reason": "No entry point detected — project structure may be non-standard",
                "impact": "high",
            })
        if primary and primary.confidence == "low":
            analysis_gaps.append({
                "area": "stack",
                "reason": f"Low-confidence detection for '{primary.stack}' — no manifest found",
                "impact": "medium",
            })
        heuristic_stacks = [s for s in sm.stacks if s.detection_method == "heuristic"]
        if heuristic_stacks:
            analysis_gaps.append({
                "area": "stack",
                "reason": f"Heuristic-only detection (no manifest): {', '.join(s.stack for s in heuristic_stacks)}",
                "impact": "medium",
            })
        if not sm.dependency_summary or not sm.dependency_summary.requested:
            analysis_gaps.append({
                "area": "dependencies",
                "reason": "Dependencies not analyzed — use the full analyze command with dependency flags for complete context",
                "impact": "medium",
            })

    if analysis_gaps:
        result["analysis_gaps"] = analysis_gaps

    return result


def standard_view(sm: SourceMap, *, include_tree: bool = False) -> dict[str, Any]:
    """Default output — three signal layers.

    Layer A (always):
        metadata, project_type, project_summary, architecture_summary,
        stacks, entry_points.

    Layer B (when the corresponding flag was passed):
        dependency_summary + key_dependencies, env_summary + env_map,
        code_notes_summary + code_notes, git_context.

    Layer C (only when the flag was explicitly passed, checked via *.requested):
        module_graph, docs, semantic_*, file_metrics, architecture inference.

    file_tree / file_paths only when include_tree=True.
    Full dependencies list is never included — use key_dependencies instead.
    Empty unrequested analyzer fields are omitted entirely.
    """
    ep_groups = _entry_point_groups(sm.entry_points)

    result: dict[str, Any] = {
        "metadata": asdict(sm.metadata),
        "project_type": sm.project_type,
        "project_summary": sm.project_summary,
        "architecture_summary": sm.architecture_summary,
        "stacks": [asdict(s) for s in sm.stacks],
        "entry_points": ep_groups["production"][:_EP_PRODUCTION_CAP],
    }
    if ep_groups["development"]:
        result["development_entry_points"] = ep_groups["development"][:_EP_DEV_CAP]
    if ep_groups["auxiliary"]:
        result["auxiliary_entry_points"] = ep_groups["auxiliary"][:_EP_DEV_CAP]

    # Java-specific root fields (FIX-6, FIX-7, FIX-8)
    if getattr(sm, "packaging", None):
        result["packaging"] = sm.packaging
    if getattr(sm, "language_version", None):
        result["language_version"] = sm.language_version
    if getattr(sm, "spring_profiles", None):
        result["spring_profiles"] = sm.spring_profiles
    if getattr(sm, "app_server_hint", None):
        result["app_server_hint"] = sm.app_server_hint

    # Layer B — signals (only when the corresponding analyzer ran)
    if sm.dependency_summary is not None and sm.dependency_summary.requested:
        dep_dict = asdict(sm.dependency_summary)
        dep_dict.pop("dependencies", None)  # avoid duplication with key_dependencies
        result["dependency_summary"] = dep_dict
        result["key_dependencies"] = [
            asdict(d) for d in sm.key_dependencies
            if (d.role or "unknown") in _PRODUCTION_DEP_ROLES and d.scope not in {"dev"}
        ][:_KEY_DEPS_CAP]

    if sm.env_summary is not None and sm.env_summary.requested:
        env_sum_dict = asdict(sm.env_summary)
        _sp = sm.env_summary.spring_profiles or sm.env_summary.profiles_scanned
        if _sp:
            env_sum_dict["spring_profiles"] = sorted(set(_sp))
        result["env_summary"] = env_sum_dict
        result["env_map"] = [asdict(e) for e in sm.env_map[:_ENV_MAP_CAP]]

    if sm.code_notes_summary is not None and sm.code_notes_summary.requested:
        result["code_notes_summary"] = asdict(sm.code_notes_summary)
        if sm.code_notes:
            _SEVERITY_ORDER = {"BUG": 0, "FIXME": 1, "DEPRECATED": 2, "TODO": 3, "HACK": 4, "WARNING": 5}
            _sorted_notes = sorted(
                sm.code_notes,
                key=lambda n: (_SEVERITY_ORDER.get(getattr(n, "kind", "").upper(), 9), getattr(n, "path", "")),
            )
            result["code_notes"] = [asdict(n) for n in _sorted_notes[:_CODE_NOTES_CAP]]
        if sm.code_adrs:
            result["code_adrs"] = [asdict(a) for a in sm.code_adrs]

    if sm.git_context is not None and sm.git_context.requested:
        result["git_context"] = asdict(sm.git_context)

    # Layer C — deep-dive (flag must have been explicitly passed)
    if sm.module_graph is not None and sm.module_graph.summary.requested:
        result["module_graph"] = asdict(sm.module_graph)
        result["module_graph_summary"] = asdict(sm.module_graph.summary)

    if sm.doc_summary is not None and sm.doc_summary.requested:
        result["doc_summary"] = asdict(sm.doc_summary)
        result["docs"] = [asdict(d) for d in sm.docs]

    if sm.semantic_summary is not None and sm.semantic_summary.requested:
        result["semantic_summary"] = asdict(sm.semantic_summary)
        # Backward compat: also emit hotspots at top level (moved to semantic_summary in v1.5.0).
        # Consumers reading d["hotspots"] directly still work.
        if sm.semantic_summary.hotspots:
            result["hotspots"] = sm.semantic_summary.hotspots[:10]
        # Defensive filter: never emit objects with null required fields.
        # A null entry in these arrays is worse than a shorter array — it causes
        # agents to misinterpret the analysis as valid when it is not.
        result["semantic_calls"] = [
            asdict(c) for c in sm.semantic_calls
            if c.caller_path and c.callee_path
        ]
        result["semantic_symbols"] = [
            asdict(s) for s in sm.semantic_symbols
            if s.symbol and s.kind and s.language and s.path
        ]
        result["semantic_links"] = [
            asdict(lnk) for lnk in sm.semantic_links
            if lnk.importer_path and lnk.symbol
        ]

    if sm.metrics_summary is not None and sm.metrics_summary.requested:
        result["metrics_summary"] = asdict(sm.metrics_summary)
        result["file_metrics"] = [_serialize_file_metric(m) for m in sm.file_metrics]

    if sm.architecture is not None and sm.architecture.requested:
        result["architecture"] = asdict(sm.architecture)

    if include_tree:
        result["file_tree"] = sm.file_tree
        result["file_paths"] = sm.file_paths

    if sm.pipeline_trace is not None and sm.pipeline_trace.requested:
        result["pipeline_trace"] = asdict(sm.pipeline_trace)

    return result


# ---------------------------------------------------------------------------
# Two-layer cache: core_view + build_view_from_core
# ---------------------------------------------------------------------------

#: Bump to invalidate all L1 core caches when the core format changes.
CORE_VIEW_VERSION: str = "1"

#: Fields that standard_view omits from file_tree when no_tree is active.
_TREE_FIELDS: frozenset[str] = frozenset({"file_tree", "file_paths"})

#: transactional_boundaries truncation threshold for full=False compact view.
_TXN_COMPACT_CAP = 10


def core_view(sm: SourceMap) -> dict[str, Any]:
    """Pre-compute all view variants for L1 (core) cache.

    Stores compact, agent, and standard views at **maximum fidelity**
    (full=True, include_tree=True).  View-specific flags (compact/agent,
    format, no_tree, full, redaction, budget) are applied later when
    building the L2 view from this core — they never affect core content.

    Schema::

        {
          "_cv":       "<CORE_VIEW_VERSION>",
          "_compact":  compact_view(sm, no_tree=False, full=True),
          "_agent":    agent_view(sm, full=True),
          "_standard": standard_view(sm, include_tree=True),
        }
    """
    return {
        "_cv": CORE_VIEW_VERSION,
        "_compact": compact_view(sm, no_tree=False, full=True),
        "_agent": agent_view(sm, full=True),
        "_standard": standard_view(sm, include_tree=True),
    }


def build_view_from_core(
    core: dict[str, Any],
    *,
    compact: bool = False,
    agent: bool = False,
    full: bool = False,
    no_tree: bool = False,
    tree: bool = False,
) -> Optional[dict[str, Any]]:
    """Derive a view dict from an L1 core dict (skip full re-analysis).

    Returns the view dict (before redaction / budget / serialisation) or
    ``None`` when the core format is unrecognised or data is missing —
    the caller must fall back to a full analysis run.

    Parameters
    ----------
    core:
        Dict returned by :func:`core_view` (stored in L1 cache).
    compact / agent:
        Which view mode to reconstruct (mutually exclusive; both False =
        standard view).
    full:
        When *False* and *compact* is True, truncate transactional_boundaries
        to ``_TXN_COMPACT_CAP`` entries (mirrors compact_view behaviour).
    no_tree / tree:
        Control file_tree / file_paths inclusion in standard / compact views.
    """
    if not isinstance(core, dict) or core.get("_cv") != CORE_VIEW_VERSION:
        return None  # stale or unknown core format → full re-analysis

    if agent:
        data = core.get("_agent")
        if not isinstance(data, dict):
            return None
        return data  # agent_view never includes file_tree/file_paths

    if compact:
        data = core.get("_compact")
        if not isinstance(data, dict):
            return None
        # compact_view stores max-fidelity data; apply flag filters
        if no_tree:
            data = {k: v for k, v in data.items() if k not in _TREE_FIELDS}
        if not full:
            # Truncate transactional_boundaries to _TXN_COMPACT_CAP when stored
            # with full=True (mirrors _transactional_summary(full=False) logic).
            txn = data.get("transactional_boundaries")
            if isinstance(txn, dict):
                classes = txn.get("classes") or []
                count = txn.get("count", len(classes))
                if count > _TXN_COMPACT_CAP and not txn.get("truncated"):
                    data = dict(data)
                    data["transactional_boundaries"] = {
                        **txn,
                        "classes": classes[:_TXN_COMPACT_CAP],
                        "truncated": True,
                        "note": (
                            f"showing {_TXN_COMPACT_CAP} of {count}; "
                            f"use --full to see all {count}"
                        ),
                    }
        return data

    # Standard view
    data = core.get("_standard")
    if not isinstance(data, dict):
        return None
    want_tree = tree and not no_tree
    if not want_tree:
        data = {k: v for k, v in data.items() if k not in _TREE_FIELDS}
    return data


def contract_view(
    sm: SourceMap,
    *,
    emit_graph: bool = False,
    depth: str = "minimal",
) -> dict[str, Any]:
    """Contract-mode output: project header + per-file semantic contracts.

    depth="minimal" (default): compact header, filtered imports, no ranking
      metadata, no per-file method/limitations. Smallest token footprint.
    depth="standard": full per-file detail — imports, relevance scores,
      fan metrics, extraction method. Current v0.33 behavior.
    depth="deep": standard + optional analysis sections (deps, env, git).

    Never includes: file bodies, function implementations, comments, or
    low-signal metadata regardless of depth.
    """
    contracts = sm.file_contracts or []

    if depth == "minimal":
        return _contract_view_minimal(sm, contracts, emit_graph=emit_graph)
    if depth in ("standard", "deep"):
        return _contract_view_standard(sm, contracts, emit_graph=emit_graph,
                                       include_optional=(depth == "deep"))
    return _contract_view_minimal(sm, contracts, emit_graph=emit_graph)


# ---------------------------------------------------------------------------
# Minimal contract renderer — smallest token footprint
# ---------------------------------------------------------------------------

def _contract_view_minimal(
    sm: SourceMap,
    contracts: list[Any],
    *,
    emit_graph: bool = False,
) -> dict[str, Any]:
    """Minimal contract: project header + stripped per-file contracts."""
    primary = next((s for s in sm.stacks if s.primary), sm.stacks[0] if sm.stacks else None)

    # Entry point paths only (production), capped
    ep_paths = sorted({
        ep.path.replace("\\", "/")
        for ep in sm.entry_points
        if is_production_entry_point(ep)
    })[:_EP_PRODUCTION_CAP]

    project: dict[str, Any] = {"type": sm.project_type}
    if primary:
        project["stack"] = primary.stack
        if primary.frameworks:
            project["frameworks"] = [f.name for f in primary.frameworks]
    if ep_paths:
        project["entry_points"] = ep_paths
    if sm.project_summary:
        project["summary"] = sm.project_summary

    result: dict[str, Any] = {
        "schema_version": sm.metadata.schema_version,
        "mode": "contract",
        "project": project,
    }

    # Full stacks list (needed for version checks in smoke tests)
    if sm.stacks:
        result["stacks"] = [asdict(s) for s in sm.stacks]

    # Java-specific root fields
    if getattr(sm, "packaging", None):
        result["packaging"] = sm.packaging
    if getattr(sm, "language_version", None):
        result["language_version"] = sm.language_version
    if getattr(sm, "spring_profiles", None):
        result["spring_profiles"] = sm.spring_profiles
    if getattr(sm, "app_server_hint", None):
        result["app_server_hint"] = sm.app_server_hint

    # Per-file contracts — capped to avoid token bloat on large projects
    if contracts:
        _capped, _meta = _cap_contracts_for_output(contracts)
        result["contracts"] = [_serialize_contract_minimal(c) for c in _capped]
        result["contracts_meta"] = _meta

    # Optional analysis sections — included when the analyzer explicitly ran
    # (user passed --dependencies, --env-map, --code-notes, --git-context)
    if sm.dependency_summary is not None and sm.dependency_summary.requested:
        dep_dict = asdict(sm.dependency_summary)
        dep_dict.pop("dependencies", None)
        result["dependency_summary"] = dep_dict
        result["key_dependencies"] = [
            {k: v for k, v in asdict(d).items() if v is not None and k != "parent"}
            for d in sm.key_dependencies
            if (d.role or "unknown") in _PRODUCTION_DEP_ROLES and d.scope not in {"dev"}
        ]

    if sm.env_summary is not None and sm.env_summary.requested:
        result["env_summary"] = asdict(sm.env_summary)
        if sm.env_map:
            # Include top-20 env entries sorted by required first, then name.
            # Agents read the summary count but need the actual keys to act on them.
            _sorted_env = sorted(sm.env_map, key=lambda e: (not getattr(e, "required", False), getattr(e, "name", "")))
            result["env_map"] = [
                {k: v for k, v in asdict(e).items() if v is not None and v != ""}
                for e in _sorted_env[:20]
            ]

    if sm.code_notes_summary is not None and sm.code_notes_summary.requested:
        result["code_notes_summary"] = asdict(sm.code_notes_summary)
        if sm.code_notes:
            # Include top-20 notes by severity: BUG > FIXME > DEPRECATED > TODO > others.
            _SEVERITY_ORDER = {"BUG": 0, "FIXME": 1, "DEPRECATED": 2, "TODO": 3, "HACK": 4, "WARNING": 5}
            _sorted_notes = sorted(
                sm.code_notes,
                key=lambda n: (_SEVERITY_ORDER.get(getattr(n, "kind", "").upper(), 9), getattr(n, "path", "")),
            )
            result["code_notes"] = [
                {k: v for k, v in asdict(n).items() if v is not None and v != ""}
                for n in _sorted_notes[:20]
            ]

    if sm.git_context is not None and sm.git_context.requested:
        result["git_context"] = asdict(sm.git_context)

    # Optional graph (--emit-graph)
    if emit_graph and contracts:
        from sourcecode.contract_pipeline import build_dependency_graph
        result["dependency_graph"] = build_dependency_graph(contracts)

    # Compact summary
    if sm.contract_summary is not None:
        cs = sm.contract_summary
        # degraded only when tree-sitter is actually unavailable — not when individual
        # files fall back due to parse errors or size limits.
        degraded = any("tree_sitter_unavailable" in lim for lim in cs.limitations)
        summary: dict[str, Any] = {
            "files": cs.extracted_files,
            "total": cs.total_files,
        }
        if cs.method_breakdown:
            summary["methods"] = cs.method_breakdown
        if degraded:
            summary["degraded"] = True
            summary["degraded_hint"] = "install sourcecode[ast] for full TS/JS extraction"
        result["summary"] = summary
        if cs.symbol_truncation:
            result["symbol_query"] = cs.symbol_truncation

    # Monorepo package roles — helps agents understand workspace structure
    if sm.monorepo_packages:
        _noise_roles = {"benchmark_layer", "tooling_layer", "docs_layer", "test_layer"}
        operational_pkgs = [
            {"path": p.path, "role": p.architectural_role, "criticality": p.criticality}
            for p in sm.monorepo_packages
            if p.architectural_role not in _noise_roles
        ]
        if operational_pkgs:
            result["workspace_packages"] = operational_pkgs[:_MONOREPO_PKGS_CAP]

    # Confidence summary — detection quality signal
    if sm.confidence_summary is not None:
        cs_conf = sm.confidence_summary
        conf: dict[str, Any] = {
            "overall": cs_conf.overall,
            "stack": cs_conf.stack_confidence,
            "entry_points": cs_conf.entry_point_confidence,
        }
        if cs_conf.anomalies:
            conf["anomalies"] = cs_conf.anomalies
        result["confidence"] = conf

    # Analysis gaps — explicit about what could not be analyzed
    if sm.analysis_gaps:
        result["analysis_gaps"] = [asdict(g) for g in sm.analysis_gaps]

    # Module graph — included when --graph-modules was requested
    if sm.module_graph is not None and sm.module_graph_summary is not None and sm.module_graph_summary.requested:
        result["module_graph"] = {
            "nodes": [asdict(n) for n in sm.module_graph.nodes],
            "edges": [asdict(e) for e in sm.module_graph.edges],
            "summary": asdict(sm.module_graph_summary),
        }
        result["module_graph_summary"] = asdict(sm.module_graph_summary)

    return result


def _split_params(param_str: str) -> list[str]:
    """Split parameter string at top-level commas."""
    params: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in param_str:
        if ch in "([{":
            depth += 1
            current.append(ch)
        elif ch in ")]}":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            p = "".join(current).strip()
            if p:
                params.append(p)
            current = []
        else:
            current.append(ch)
    if current:
        p = "".join(current).strip()
        if p:
            params.append(p)
    return params


def _strip_param_default(param: str) -> str:
    """Remove '= <default>' from a single parameter, keeping type annotation."""
    depth = 0
    for i, ch in enumerate(param):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "=" and depth == 0:
            return param[:i].rstrip()
    return param


def _compress_sig(name: str, sig: str, max_len: int = 100) -> str:
    """Compress a function signature — strip defaults, preserve type annotations."""
    paren_start = sig.find("(")
    if paren_start < 0:
        full = f"{name}{sig}"
        return full[:max_len - 3] + "..." if len(full) > max_len else full

    # Find matching close paren
    depth = 0
    paren_end = -1
    for i, ch in enumerate(sig[paren_start:], paren_start):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                paren_end = i
                break

    if paren_end >= 0:
        param_str = sig[paren_start + 1:paren_end]
        ret_str = sig[paren_end + 1:]
        clean_params = [_strip_param_default(p) for p in _split_params(param_str)]
        full = f"{name}({', '.join(clean_params)}){ret_str}"
    else:
        # Truncated signature (e.g. 2000-char cap hit) — best-effort strip of visible params
        visible = sig[paren_start + 1:]
        partial = _split_params(visible)
        clean_params = [_strip_param_default(p) for p in partial]
        full = f"{name}({', '.join(clean_params)}"

    if len(full) > max_len:
        full = full[:max_len - 3] + "..."
    return full


_MAX_FN_PER_CONTRACT = 5   # max function signatures per contract (token budget)
_MAX_SIG_LEN = 60          # max chars per compressed signature


def _serialize_contract_java(c: Any) -> dict[str, Any]:
    """Java-specific contract serializer with full field names and annotations."""
    item: dict[str, Any] = {"path": c.path, "language": "java"}

    exports_out: list[dict] = []
    for e in c.exports:
        entry: dict = {"kind": e.kind, "name": e.name}
        if getattr(e, "annotations", None):
            entry["annotations"] = e.annotations
        if getattr(e, "extends", None):
            entry["extends"] = e.extends
        if getattr(e, "implements", None) and e.implements:
            entry["implements"] = e.implements
        if getattr(e, "signature", None):
            entry["signature"] = e.signature
        exports_out.append(entry)
    if exports_out:
        item["exports"] = exports_out

    if c.imports:
        item["imports"] = [imp.source for imp in c.imports[:20]]

    autowired = getattr(c, "autowired_fields", [])
    if autowired:
        item["autowired_fields"] = autowired

    return item


def _serialize_contract_mybatis_xml(c: Any) -> dict[str, Any]:
    """Serialize a MyBatis *Mapper.xml contract."""
    item: dict[str, Any] = {"path": c.path, "language": "mybatis-xml"}
    # Extract namespace stored as "namespace:<fqn>" in dependencies
    for dep in (c.dependencies or []):
        if dep.startswith("namespace:"):
            item["namespace"] = dep[len("namespace:"):]
            break
    exports_out: list[dict] = []
    for e in c.exports:
        entry: dict = {"kind": e.kind, "name": e.name}
        if getattr(e, "type_ref", None):
            entry["type"] = e.type_ref
        exports_out.append(entry)
    if exports_out:
        item["exports"] = exports_out
    return item


def _serialize_contract_minimal(c: Any) -> dict[str, Any]:
    """Serialize one FileContract to minimal format."""
    if getattr(c, "language", None) == "java":
        return _serialize_contract_java(c)
    if getattr(c, "language", None) == "mybatis-xml":
        return _serialize_contract_mybatis_xml(c)
    item: dict[str, Any] = {"path": c.path, "role": c.role}

    if c.is_changed:
        item["changed"] = True

    # Exported function signatures — compressed, capped
    exported_names = {e.name for e in c.exports}
    fn_names_in_sigs: set[str] = set()
    if c.functions:
        fns = []
        for f in sorted(c.functions, key=lambda f: f.name):
            if not (f.exported or f.name in exported_names):
                continue
            fns.append(_compress_sig(f.name, f.signature, max_len=_MAX_SIG_LEN))
            fn_names_in_sigs.add(f.name)
            if len(fns) >= _MAX_FN_PER_CONTRACT:
                break
        if fns:
            item["fn"] = fns

    # Exports: omit function names already shown in fn; keep non-function exports
    if c.exports:
        exs: list[Any] = []
        non_fn_exports = [e for e in c.exports if e.kind not in ("function", "unknown")]
        fn_exports_not_in_sig = [
            e for e in c.exports
            if e.kind in ("function", "unknown") and e.name not in fn_names_in_sigs
        ]
        remaining = non_fn_exports + fn_exports_not_in_sig
        if remaining:
            kinds = {e.kind for e in remaining}
            if len(kinds) == 1 and "function" not in kinds and "unknown" not in kinds:
                only_kind = next(iter(kinds))
                exs = [{"k": only_kind, "names": sorted(e.name for e in remaining)}]
            else:
                for e in sorted(remaining, key=lambda e: e.name):
                    if e.kind in ("function", "unknown"):
                        exs.append(e.name)
                    else:
                        exs.append({"name": e.name, "k": e.kind})
            item["exports"] = exs

    # External deps (non-stdlib already filtered in extractor)
    if c.dependencies:
        item["deps"] = sorted(c.dependencies)

    # Types: skip if fully covered by exports (avoids duplication in model files)
    if c.types:
        export_names_set = {e.name for e in c.exports}
        non_redundant = [t for t in c.types if t.name not in export_names_set]
        if non_redundant:
            item["types"] = [
                {"name": t.name, "k": t.kind} if t.kind not in ("interface", "class") else t.name
                for t in sorted(non_redundant, key=lambda t: t.name)
            ]

    # Hooks (TSX/JSX — usually short list)
    if c.hooks_used:
        item["hooks"] = c.hooks_used

    return item


# ---------------------------------------------------------------------------
# Standard contract renderer — full per-file detail (v0.33 behavior)
# ---------------------------------------------------------------------------

def _contract_view_standard(
    sm: SourceMap,
    contracts: list[Any],
    *,
    emit_graph: bool = False,
    include_optional: bool = False,
) -> dict[str, Any]:
    """Standard contract: full per-file detail — mirrors v0.33 output."""
    from dataclasses import asdict as _asdict

    primary = next((s for s in sm.stacks if s.primary), sm.stacks[0] if sm.stacks else None)
    project: dict[str, Any] = {"type": sm.project_type}
    if sm.project_summary:
        project["summary"] = sm.project_summary
    if primary:
        project["primary_stack"] = primary.stack
        if primary.frameworks:
            project["frameworks"] = [f.name for f in primary.frameworks]
        if primary.package_manager:
            project["package_manager"] = primary.package_manager

    ep_groups = _entry_point_groups(sm.entry_points)

    result: dict[str, Any] = {
        "schema_version": sm.metadata.schema_version,
        "mode": "standard",
        "project": project,
        "stacks": [
            {"stack": s.stack, "primary": s.primary,
             "frameworks": [f.name for f in (s.frameworks or [])],
             "package_manager": s.package_manager}
            for s in sm.stacks
        ],
        "entry_points": ep_groups["production"][:_EP_PRODUCTION_CAP],
    }
    if sm.metadata.traversal_topology:
        result["traversal"] = sm.metadata.traversal_topology
    if ep_groups["development"]:
        result["development_entry_points"] = ep_groups["development"][:_EP_DEV_CAP]

    if sm.confidence_summary is not None:
        result["confidence"] = {
            "overall": sm.confidence_summary.overall,
            "stack": sm.confidence_summary.stack_confidence,
        }

    # Per-file contracts (full detail) — capped to avoid token bloat on large projects
    if contracts:
        _capped, _meta = _cap_contracts_for_output(contracts)
        serialized: list[dict[str, Any]] = []
        for c in _capped:
            if getattr(c, "language", None) == "mybatis-xml":
                item = _serialize_contract_mybatis_xml(c)
                item["relevance_score"] = round(c.relevance_score, 3)
                serialized.append(item)
                continue
            item: dict[str, Any] = {
                "path": c.path,
                "language": c.language,
                "role": c.role,
                "relevance_score": round(c.relevance_score, 3),
            }
            if c.fan_in or c.fan_out:
                item["fan_in"] = c.fan_in
                item["fan_out"] = c.fan_out
            if c.is_entrypoint:
                item["is_entrypoint"] = True
            if c.is_changed:
                item["is_changed"] = True
            if c.exports:
                item["exports"] = [
                    {k: v for k, v in _asdict(e).items()
                     if v is not None and v is not False and v != "unknown"}
                    for e in c.exports
                ]
            if c.imports:
                item["imports"] = [
                    {"source": i.source, "symbols": i.symbols}
                    if i.symbols else {"source": i.source}
                    for i in c.imports
                ]
            if c.functions:
                item["functions"] = [
                    {k: v for k, v in _asdict(f).items()
                     if v is not None and v is not False and v != []}
                    for f in c.functions
                ]
            if c.types:
                item["types"] = [
                    {k: v for k, v in _asdict(t).items()
                     if v is not None and v != [] and v != "unknown"}
                    for t in c.types
                ]
            if c.hooks_used:
                item["hooks_used"] = c.hooks_used
            if c.dependencies:
                item["dependencies"] = c.dependencies
            if c.limitations:
                item["limitations"] = c.limitations
            if getattr(c, "ranking_reasons", None):
                non_trivial = [r for r in c.ranking_reasons if r not in ("source file", "noise")]
                if non_trivial:
                    item["ranking_reasons"] = non_trivial
            item["method"] = c.extraction_method
            serialized.append(item)
        result["contracts"] = serialized
        result["contracts_meta"] = _meta

    # Optional analysis sections (deep mode or when analyzers ran)
    if include_optional:
        if sm.dependency_summary is not None and sm.dependency_summary.requested:
            dep_dict = asdict(sm.dependency_summary)
            dep_dict.pop("dependencies", None)
            result["dependency_summary"] = dep_dict
            result["key_dependencies"] = [
                {k: v for k, v in asdict(d).items() if v is not None and k != "parent"}
                for d in sm.key_dependencies
                if (d.role or "unknown") in _PRODUCTION_DEP_ROLES and d.scope not in {"dev"}
            ]
        if sm.env_summary is not None and sm.env_summary.requested:
            result["env_summary"] = asdict(sm.env_summary)
        if sm.code_notes_summary is not None and sm.code_notes_summary.requested:
            result["code_notes_summary"] = asdict(sm.code_notes_summary)
        if sm.git_context is not None and sm.git_context.requested:
            result["git_context"] = asdict(sm.git_context)

    if emit_graph and contracts:
        from sourcecode.contract_pipeline import build_dependency_graph
        result["dependency_graph"] = build_dependency_graph(contracts)

    if sm.contract_summary is not None:
        cs = sm.contract_summary
        result["contract_summary"] = {
            "mode": cs.mode,
            "total_files": cs.total_files,
            "extracted_files": cs.extracted_files,
            "method_breakdown": cs.method_breakdown,
            "ranked_by": cs.ranked_by,
        }
        if cs.limitations:
            result["contract_summary"]["limitations"] = cs.limitations
        if cs.symbol_truncation:
            result["symbol_query"] = cs.symbol_truncation

    return result


def write_output(content: str, output: Optional[Path]) -> None:
    """Write content to stdout or a file.

    Args:
        content: Serialized string (JSON or YAML).
        output: Destination file path. None = stdout.
    """
    if output is None:
        sys.stdout.buffer.write(content.encode("utf-8"))
        if not content.endswith("\n"):
            sys.stdout.buffer.write(b"\n")
    else:
        output.write_text(content, encoding="utf-8")

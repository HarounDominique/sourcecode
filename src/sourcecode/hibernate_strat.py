"""hibernate_strat.py — Hibernate 5.x → 6.x migration stratification model.

A Hibernate major upgrade is NOT a single dependency bump for enterprise systems
that use dynamic persistence. This module splits Hibernate exposure into four
independent migration domains and classifies each one on its own risk axis:

  1. JPA Annotation Layer        — LOW (unless deprecated annotations exist)
  2. Criteria API Layer          — HIGH, escalates to CRITICAL when built
                                    dynamically via reflection / abstraction DAOs
  3. HQL / String-based Queries  — MEDIUM, escalates to HIGH on concatenation
  4. Hibernate SPI / Internal    — CRITICAL BLOCKER (UserType, Interceptor, SPI)

Beyond the diagnostic summary it emits **actionable, machine-readable rewrite
targets** at call-site granularity (`rewrite_targets[]`) so a migration agent can
consume the output directly instead of re-parsing the repository. It also produces:
  - a module-level Hibernate exposure map,
  - per-layer manual/assisted/mechanical sub-counts (static vs dynamic Criteria),
  - honest effort ranges (low/high/confidence) + an auditable effort model,
  - a hibernate_readiness score (0-100),
  - critical call-chain detection and golden-SQL instrumentation hotspots,
  - separation of observable code risk from inferred runtime risk,
  - an UPGRADE-vs-REWRITE verdict via stop-condition logic.

Entry point: analyze_hibernate(file_paths, root) → HibernateStratification

This is purely additive and does not aggregate Hibernate into a single score.
"""
from __future__ import annotations

import bisect
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# Sub-schema version for the `hibernate` output section. Bump on shape changes.
HIBERNATE_SCHEMA_VERSION = "2.0"


# ---------------------------------------------------------------------------
# Layer identifiers
# ---------------------------------------------------------------------------

LAYER_JPA = "jpa_annotations"
LAYER_CRITERIA = "criteria_api"
LAYER_HQL = "hql_string_queries"
LAYER_SPI = "hibernate_spi_internal"

_LAYER_TITLES: dict[str, str] = {
    LAYER_JPA: "JPA Annotation Layer",
    LAYER_CRITERIA: "Criteria API Layer",
    LAYER_HQL: "HQL / String-based Queries",
    LAYER_SPI: "Hibernate SPI / Internal API",
}
_LAYER_CODE: dict[str, str] = {
    LAYER_JPA: "JPA", LAYER_CRITERIA: "CRIT", LAYER_HQL: "HQL", LAYER_SPI: "SPI",
}

# Classification verdicts
CLASS_NONE = "none"
CLASS_UPGRADE = "upgrade_zone"
CLASS_UPGRADE_CARE = "upgrade_with_care"
CLASS_REWRITE = "rewrite_zone"

_CLASS_LABELS: dict[str, str] = {
    CLASS_NONE: "NO HIBERNATE USAGE DETECTED",
    CLASS_UPGRADE: "HIBERNATE 6 MIGRATION IS UPGRADE ZONE (dependency bump + spot fixes)",
    CLASS_UPGRADE_CARE: "HIBERNATE 6 MIGRATION IS UPGRADE-WITH-CARE ZONE (static queries to revalidate)",
    CLASS_REWRITE: "HIBERNATE 6 MIGRATION IS HIGH RISK REWRITE ZONE (NOT UPGRADE ZONE)",
}

_SEVERITY_RANK: dict[str, int] = {"critical": 0, "high": 1, "medium": 2, "low": 3}

# Migration kinds (ascending automatability)
KIND_MANUAL = "manual_rewrite"
KIND_ASSISTED = "assisted"
KIND_MECHANICAL = "mechanical"
KIND_REVIEW = "review"

# Effort range multipliers by confidence band: (low_mult, high_mult).
_CONF_MULT: dict[str, tuple[float, float]] = {
    "high": (0.9, 1.2),
    "medium": (0.7, 1.5),
    "low": (0.5, 2.0),
}


# ---------------------------------------------------------------------------
# Detection patterns
# ---------------------------------------------------------------------------

# Layer 1 — standard JPA annotations (LOW baseline).
_JPA_ANNOTATION_RE = re.compile(
    r"@(Entity|Table|OneToMany|ManyToOne|OneToOne|ManyToMany|Column|"
    r"Id|GeneratedValue|MappedSuperclass|Embeddable|Embedded|JoinColumn|"
    r"JoinTable|Inheritance|DiscriminatorColumn)\b"
)
# Hibernate / JPA annotations deprecated or reworked in Hibernate 6 (escalate).
_JPA_DEPRECATED_RE = re.compile(
    r"@TypeDefs?\b|"
    r"org\.hibernate\.annotations\.Type\b|"
    r"@Type\s*\(\s*type\s*=|"
    r"@GenericGenerator\b|"
    r"@org\.hibernate\.annotations\.Entity\b"
)

# Layer 2 — JPA Criteria API + legacy Hibernate Criteria.
_CRITERIA_JPA_RE = re.compile(
    r"\bCriteriaBuilder\b|\bCriteriaQuery\b|\bCriteriaUpdate\b|"
    r"\bCriteriaDelete\b|\bRoot\s*<|\bPredicate\b|persistence\.criteria"
)
_CRITERIA_LEGACY_RE = re.compile(
    r"org\.hibernate\.Criteria\b|\.createCriteria\s*\(|\bDetachedCriteria\b|"
    r"\bRestrictions\.|\bProjections\.|\bCriterion\b|"
    r"\bConjunction\b|\bDisjunction\b"
)

# Layer 3 — HQL / native string queries. createQuery requires a string literal so
# CriteriaBuilder.createQuery(Class) (Layer 2) is not double-counted here.
_HQL_RE = re.compile(
    r"\.createQuery\s*\(\s*\"|"
    r"\.(createSQLQuery|createNativeQuery|getNamedQuery|createNamedQuery)\s*\("
)
# String concatenation inside a query call → dynamic SQL shape (HIGH).
_HQL_CONCAT_RE = re.compile(
    r"\.(?:createQuery|createSQLQuery|createNativeQuery)\s*\("
    r"[^;]{0,400}?(?:\"[^\"]*\"\s*\+|\+\s*\"[^\"]*\"|\+\s*\w+\s*\+)",
    re.DOTALL,
)

# Layer 4 — Hibernate SPI / internal API (CRITICAL blocker).
_SPI_RE = re.compile(
    r"\b(?:implements|extends)\s+\w*(?:UserType|CompositeUserType|UserCollectionType)\b|"
    r"\bimplements\s+\w*UserType\b|"
    r"\borg\.hibernate\.(?:type|engine|internal|persister|metamodel|"
    r"boot\.spi|boot\.internal|event|tuple|property\.access|loader|sql\.ast)\b|"
    r"\bEmptyInterceptor\b|"
    r"\bimplements\s+\w*Interceptor\b|"
    r"\b(?:implements|extends)\s+\w*EventListener\b|"
    r"\bSessionFactoryImpl\b|\bSessionImplementor\b|"
    r"\bSharedSessionContractImplementor\b|"
    r"\bsetPropertyAccessStrategy\b|\bPropertyAccessStrategy\b|"
    r"\bImplicitNamingStrategy\b|\bPhysicalNamingStrategy\b"
)

# Escalation markers — dynamic / reflection-based persistence construction.
_ABSTRACTION_CLASS_RE = re.compile(
    r"\b(DynamicEntityDao|DynamicEntityDaoImpl|BasicPersistenceModule|"
    r"PersistenceModule|GenericDao|GenericEntityDao|GenericEntityService|"
    r"DynamicDaoHelper|CriteriaTranslator|FieldManager|EntityMetadata)\w*\b"
)
_REFLECTION_RE = re.compile(
    r"Class\.forName\s*\(|\.getDeclaredField|\.getDeclaredMethod|"
    r"\.getDeclaredFields\s*\(|\.newInstance\s*\(|Method\.invoke|"
    r"java\.lang\.reflect|\bField\[\]|metadata\.get|buildCriteria|"
    r"\.getMetamodel\s*\("
)

# Lightweight symbol indexing.
_CLASS_DECL_RE = re.compile(r"\b(?:class|interface|enum)\s+(\w+)")
_METHOD_DECL_RE = re.compile(r"\b(\w+)\s*\([^;{)]*\)\s*\{")
_NON_METHOD_NAMES = frozenset({
    "if", "for", "while", "switch", "catch", "synchronized", "try", "return",
    "new", "do", "else", "case", "super", "this",
})


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class RewriteTarget:
    id: str
    layer: str
    source_file: str
    line_start: int
    line_end: int
    current_pattern: str
    current_snippet: str
    target_api: str
    migration_kind: str
    auto_migratable: bool
    blocking_reason: str
    symbol: str
    module: str
    dynamic: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "layer": self.layer,
            "source_file": self.source_file,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "current_pattern": self.current_pattern,
            "current_snippet": self.current_snippet,
            "target_api": self.target_api,
            "migration_kind": self.migration_kind,
            "auto_migratable": self.auto_migratable,
            "blocking_reason": self.blocking_reason,
            "symbol": self.symbol,
            "module": self.module,
            "dynamic": self.dynamic,
        }


@dataclass
class HibernateFinding:
    layer: str
    severity: str
    pattern: str
    source_file: str
    first_line: int
    module: str
    occurrences: int = 1
    dynamic: bool = False

    def to_dict(self) -> dict:
        return {
            "layer": self.layer,
            "severity": self.severity,
            "pattern": self.pattern,
            "source_file": self.source_file,
            "first_line": self.first_line,
            "module": self.module,
            "occurrences": self.occurrences,
            "dynamic": self.dynamic,
        }


@dataclass
class HibernateLayerRisk:
    layer: str
    risk: str
    reason: str
    estimated_effort: str          # legacy string "~75.8d" (compat)
    file_count: int
    occurrence_count: int
    effort_range: dict = field(default_factory=dict)   # {low, high, confidence}
    # Migration-kind breakdown (derived from rewrite_targets of this layer).
    manual_count: int = 0
    assisted_count: int = 0
    mechanical_count: int = 0
    review_count: int = 0
    # Criteria-specific.
    static_count: Optional[int] = None
    dynamic_count: Optional[int] = None
    # SPI-specific.
    userType_rewrite_count: Optional[int] = None
    userType_resolvable_count: Optional[int] = None

    def to_dict(self) -> dict:
        d: dict = {
            "layer": self.layer,
            "layer_title": _LAYER_TITLES.get(self.layer, self.layer),
            "risk": self.risk,
            "reason": self.reason,
            "estimated_effort": self.estimated_effort,
            "effort_range": self.effort_range,
            "file_count": self.file_count,
            "occurrence_count": self.occurrence_count,
            "manual_count": self.manual_count,
            "assisted_count": self.assisted_count,
            "mechanical_count": self.mechanical_count,
            "review_count": self.review_count,
        }
        if self.static_count is not None:
            d["static_count"] = self.static_count
            d["dynamic_count"] = self.dynamic_count
        if self.userType_rewrite_count is not None:
            d["userType_rewrite_count"] = self.userType_rewrite_count
            d["userType_resolvable_count"] = self.userType_resolvable_count
        return d


@dataclass
class HibernateStratification:
    detected: bool = False
    classification: str = CLASS_NONE
    classification_label: str = _CLASS_LABELS[CLASS_NONE]
    readiness: int = 100
    risk_matrix: list[HibernateLayerRisk] = field(default_factory=list)
    module_exposure: dict[str, dict] = field(default_factory=dict)
    incompatible_patterns: list[str] = field(default_factory=list)
    critical_call_chains: list[dict] = field(default_factory=list)
    rewrite_targets: list[RewriteTarget] = field(default_factory=list)
    golden_sql_hotspots: list[dict] = field(default_factory=list)
    stop_conditions_triggered: list[str] = field(default_factory=list)
    observable_risk: list[str] = field(default_factory=list)
    inferred_runtime_risk: list[str] = field(default_factory=list)
    total_effort_range_days: dict = field(default_factory=dict)
    effort_model: dict = field(default_factory=dict)
    findings: list[HibernateFinding] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "schema_version": HIBERNATE_SCHEMA_VERSION,
            "detected": self.detected,
            "classification": self.classification,
            "classification_label": self.classification_label,
            "stratified": True,
            "hibernate_readiness": self.readiness,
            "risk_matrix": [r.to_dict() for r in self.risk_matrix],
            "module_exposure_map": self.module_exposure,
            "incompatible_patterns": self.incompatible_patterns,
            "critical_call_chains": self.critical_call_chains,
            "rewrite_targets": [t.to_dict() for t in self.rewrite_targets],
            "golden_sql_hotspots": self.golden_sql_hotspots,
            "stop_conditions_triggered": self.stop_conditions_triggered,
            "total_effort_range_days": self.total_effort_range_days,
            "effort_model": self.effort_model,
            "risk_separation": {
                "observable_code_risk": self.observable_risk,
                "inferred_runtime_risk": self.inferred_runtime_risk,
            },
            "findings": [f.to_dict() for f in self.findings],
        }

    def to_text(self) -> str:
        if not self.detected:
            return "Hibernate Migration: no Hibernate/JPA persistence usage detected."

        lines: list[str] = [
            "── Hibernate 5.x → 6.x Migration Stratification ──",
            f"Classification: {self.classification_label}",
            f"Hibernate readiness: {self.readiness}/100   "
            f"Rewrite targets: {len(self.rewrite_targets)}   "
            f"Effort: {self.total_effort_range_days.get('low', '?')}–"
            f"{self.total_effort_range_days.get('high', '?')}d "
            f"({self.total_effort_range_days.get('confidence', '?')} confidence)",
            "",
            "Hibernate Migration Impact Matrix:",
            f"  {'Layer':<30} {'Risk':<9} {'Effort':<13} Breakdown",
        ]
        for r in self.risk_matrix:
            bd = f"manual={r.manual_count} assisted={r.assisted_count} mech={r.mechanical_count}"
            if r.static_count is not None:
                bd += f" (static={r.static_count}/dyn={r.dynamic_count})"
            lines.append(
                f"  {_LAYER_TITLES.get(r.layer, r.layer):<30} "
                f"{r.risk.upper():<9} {r.estimated_effort:<13} {bd}"
            )

        lines.append("")
        lines.append("Module Hibernate Exposure Map:")
        if self.module_exposure:
            for mod, info in sorted(
                self.module_exposure.items(),
                key=lambda kv: _SEVERITY_RANK.get(kv[1].get("max_risk", "low"), 3),
            ):
                tags = []
                if info.get("criteria_dynamic"):
                    tags.append("dynamic-criteria")
                if info.get("has_spi"):
                    tags.append("custom-SPI")
                if info.get("has_reflection"):
                    tags.append("reflection")
                tag_str = f"  [{', '.join(tags)}]" if tags else ""
                layers = ", ".join(sorted(info.get("layers", {}).keys()))
                lines.append(
                    f"  {mod:<40} {info.get('max_risk', 'low').upper():<9} {layers}{tag_str}"
                )
        else:
            lines.append("  (no module attribution)")

        if self.critical_call_chains:
            lines.append("")
            lines.append("Critical Call Chains (dynamic / reflection-based persistence):")
            for cc in self.critical_call_chains:
                lines.append(
                    f"  {cc['class']}  ({cc['source_file']}:{cc['first_line']}) — {cc['reason']}"
                )

        if self.golden_sql_hotspots:
            lines.append("")
            lines.append("Golden-SQL Instrumentation Hotspots (where to pin behaviour tests):")
            for h in self.golden_sql_hotspots:
                lines.append(
                    f"  {h['symbol']:<35} {h['dynamic_query_count']:>4} dyn-queries  "
                    f"({h['source_file']})"
                )

        if self.rewrite_targets:
            lines.append("")
            lines.append(f"Rewrite Targets (first 15 of {len(self.rewrite_targets)}):")
            for t in self.rewrite_targets[:15]:
                lines.append(
                    f"  [{t.migration_kind}] {t.source_file}:{t.line_start} "
                    f"{t.symbol} → {t.target_api}"
                )

        if self.incompatible_patterns:
            lines.append("")
            lines.append("Incompatible / High-risk Patterns Found:")
            for p in self.incompatible_patterns:
                lines.append(f"  - {p}")

        if self.stop_conditions_triggered:
            lines.append("")
            lines.append("Stop Conditions Triggered (force REWRITE classification):")
            for s in self.stop_conditions_triggered:
                lines.append(f"  ! {s}")

        lines.append("")
        lines.append("Risk Separation:")
        lines.append("  Observable code risk:")
        for o in (self.observable_risk or ["  (none)"]):
            lines.append(f"    - {o}")
        lines.append("  Inferred runtime risk:")
        for o in (self.inferred_runtime_risk or ["  (none)"]):
            lines.append(f"    - {o}")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _module_of(rel_path: str) -> str:
    """Derive a Maven/Gradle module name from a source path.

    Uses the directory segment immediately before '/src/' (the module root). Falls
    back to the first path segment, or 'root' for top-level files.
    """
    norm = rel_path.replace("\\", "/")
    parts = norm.split("/")
    for i, seg in enumerate(parts):
        if seg == "src" and i > 0:
            return parts[i - 1]
    if len(parts) > 1:
        return parts[0]
    return "root"


def _line_index(source: str) -> list[int]:
    """Offsets of every newline — for O(log n) offset→line lookups via bisect."""
    return [i for i, c in enumerate(source) if c == "\n"]


def _line_at(nl: list[int], pos: int) -> int:
    return bisect.bisect_right(nl, pos) + 1


def _index_symbols(source: str, nl: list[int]) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    classes = [(_line_at(nl, m.start()), m.group(1)) for m in _CLASS_DECL_RE.finditer(source)]
    methods = [
        (_line_at(nl, m.start()), m.group(1))
        for m in _METHOD_DECL_RE.finditer(source)
        if m.group(1) not in _NON_METHOD_NAMES
    ]
    return classes, methods


def _nearest(decls: list[tuple[int, str]], line: int) -> Optional[str]:
    best: Optional[str] = None
    for dl, name in decls:
        if dl <= line:
            best = name
        else:
            break
    return best


def _symbol_at(line: int, classes: list[tuple[int, str]], methods: list[tuple[int, str]]) -> str:
    cls = _nearest(classes, line)
    meth = _nearest(methods, line)
    if cls and meth:
        return f"{cls}#{meth}"
    return cls or meth or ""


def _snippet(lines: list[str], ls: int, le: int) -> str:
    seg = " ".join(l.strip() for l in lines[ls - 1: le])
    seg = re.sub(r"\s+", " ", seg).strip()
    return (seg[:157] + "...") if len(seg) > 160 else seg


def _count(pattern: re.Pattern, source: str) -> int:
    return sum(1 for _ in pattern.finditer(source))


def _make_id(layer: str, source_file: str, line: int, pattern: str) -> str:
    h = hashlib.sha256(f"{source_file}:{line}:{pattern}".encode()).hexdigest()[:10]
    return f"HB6-{_LAYER_CODE.get(layer, 'GEN')}-{h}"


# ---------------------------------------------------------------------------
# Target-mapping rules (current pattern → Hibernate 6 destination)
# ---------------------------------------------------------------------------

def _criteria_target(legacy: bool, dynamic: bool) -> tuple[str, str, bool, str]:
    if legacy:
        return ("jakarta.persistence.criteria.CriteriaBuilder",
                KIND_MANUAL, False,
                "org.hibernate.Criteria / Restrictions / Projections removed in Hibernate 6")
    if dynamic:
        return ("jakarta.persistence.criteria.CriteriaBuilder (dynamic builder rewrite)",
                KIND_MANUAL, False,
                "dynamic Criteria construction via reflection / abstraction DAO")
    return ("jakarta.persistence.criteria (validate Hibernate 6 semantics)",
            KIND_ASSISTED, False,
            "JPA Criteria metamodel/semantics changed in Hibernate 6")


def _jpa_deprecated_target(text: str) -> tuple[str, str, bool, str]:
    if "@TypeDef" in text:
        return ("@Type(value=...) (Hibernate 6 — @TypeDef removed)",
                KIND_ASSISTED, False, "@TypeDef / @TypeDefs removed in Hibernate 6")
    if "@GenericGenerator" in text:
        return ("@GenericGenerator (Hibernate 6 — strategy/parameters reworked)",
                KIND_ASSISTED, False, "@GenericGenerator reworked in Hibernate 6")
    # @Type(type="...") or org.hibernate.annotations.Type
    return ("@Type(value=...) / @JdbcTypeCode (Hibernate 6)",
            KIND_MECHANICAL, True,
            "@Type(type=) attribute renamed/replaced in Hibernate 6 (1:1 mapping)")


def _hql_target(concat: bool) -> tuple[str, str, bool, str]:
    if concat:
        return ("HQL (parameterize + validate Hibernate 6 parser)",
                KIND_ASSISTED, False, "runtime-resolved query string (concatenation)")
    return ("HQL / native query (validate against Hibernate 6 parser)",
            KIND_REVIEW, False, "")


def _spi_target(text: str) -> tuple[str, str, bool, str]:
    t = text
    if "UserCollectionType" in t:
        return ("org.hibernate.usertype.UserCollectionType (redesigned in H6)",
                KIND_MANUAL, False, "UserCollectionType interface redesigned in Hibernate 6")
    if "CompositeUserType" in t:
        return ("org.hibernate.usertype.CompositeUserType (redesigned in H6)",
                KIND_MANUAL, False, "CompositeUserType redesigned in Hibernate 6")
    if "UserType" in t:
        return ("org.hibernate.usertype.UserType (new method signatures in H6)",
                KIND_MANUAL, False, "UserType interface redesigned in Hibernate 6")
    if "EmptyInterceptor" in t:
        return ("org.hibernate.Interceptor (EmptyInterceptor removed in H6)",
                KIND_MANUAL, False, "EmptyInterceptor removed in Hibernate 6")
    if "Interceptor" in t:
        return ("org.hibernate.Interceptor (default methods in H6)",
                KIND_ASSISTED, False, "Interceptor gained default methods in Hibernate 6")
    if "EventListener" in t:
        return ("Hibernate 6 event SPI",
                KIND_MANUAL, False, "Hibernate event-listener SPI changed in Hibernate 6")
    if "PropertyAccessStrategy" in t:
        return ("org.hibernate.property.access.spi (changed in H6)",
                KIND_MANUAL, False, "PropertyAccess SPI changed in Hibernate 6")
    if "NamingStrategy" in t:
        return ("Hibernate 6 naming-strategy SPI",
                KIND_ASSISTED, False, "Naming strategy SPI adjusted in Hibernate 6")
    if "SessionFactoryImpl" in t or "SessionImplementor" in t or "SharedSessionContractImplementor" in t:
        return ("Hibernate 6 session SPI (SharedSessionContractImplementor)",
                KIND_MANUAL, False, "internal session SPI changed in Hibernate 6")
    return ("Hibernate 6 internal SPI (no stable public equivalent)",
            KIND_MANUAL, False, "internal Hibernate SPI — no direct Hibernate 6 equivalent")


# Point-estimate person-days per affected file, per layer/branch — the audit-able
# basis for effort_model. Ranges derive from these via _CONF_MULT.
_EFFORT_RATES: dict[str, float] = {
    "jpa_standard": 0.02,
    "jpa_deprecated": 0.25,
    "criteria_static": 0.4,
    "criteria_dynamic": 0.75,
    "hql_static": 0.15,
    "hql_concat": 0.3,
    "spi": 1.0,
}


def _effort_point(file_count: int, rate: float, floor: float = 0.5) -> float:
    return round(max(floor, file_count * rate), 1) if file_count else 0.0


def _effort_range(point: float, confidence: str) -> dict:
    lo, hi = _CONF_MULT[confidence]
    return {"low": round(point * lo, 1), "high": round(point * hi, 1), "confidence": confidence}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def analyze_hibernate(file_paths: list[str], root: Path) -> HibernateStratification:
    """Scan Java sources for stratified Hibernate 5→6 migration risk + rewrite targets."""
    findings: list[HibernateFinding] = []
    targets: list[RewriteTarget] = []
    modules: dict[str, dict] = {}
    call_chains: list[dict] = []
    hotspots: list[dict] = []

    layer_files: dict[str, set[str]] = {
        LAYER_JPA: set(), LAYER_CRITERIA: set(), LAYER_HQL: set(), LAYER_SPI: set()
    }
    layer_occ: dict[str, int] = {
        LAYER_JPA: 0, LAYER_CRITERIA: 0, LAYER_HQL: 0, LAYER_SPI: 0
    }
    jpa_deprecated_seen = False
    criteria_dynamic_global = False
    spi_global = False
    reflection_query_global = False
    concat_query_global = False
    incompatible: set[str] = set()

    for rel_path in sorted(file_paths):
        if not rel_path.endswith(".java"):
            continue
        abs_path = root / rel_path
        try:
            source = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        nl = _line_index(source)
        src_lines = source.split("\n")
        classes, methods = _index_symbols(source, nl)
        module = _module_of(rel_path)
        has_abstraction = bool(_ABSTRACTION_CLASS_RE.search(source))
        has_reflection = bool(_REFLECTION_RE.search(source))

        mod = modules.setdefault(
            module,
            {"layers": {}, "max_risk": "low", "criteria_dynamic": False,
             "has_spi": False, "has_reflection": False, "files": set()},
        )
        if has_reflection:
            mod["has_reflection"] = True

        def _emit_finding(layer: str, severity: str, label: str, line: int,
                          occ: int, dynamic: bool = False) -> None:
            findings.append(HibernateFinding(
                layer=layer, severity=severity, pattern=label,
                source_file=rel_path, first_line=line, module=module,
                occurrences=occ, dynamic=dynamic,
            ))
            layer_files[layer].add(rel_path)
            layer_occ[layer] += occ
            ml = mod["layers"].setdefault(layer, {"severity": severity, "occurrences": 0})
            ml["occurrences"] += occ
            if _SEVERITY_RANK[severity] < _SEVERITY_RANK[ml["severity"]]:
                ml["severity"] = severity
            if _SEVERITY_RANK[severity] < _SEVERITY_RANK[mod["max_risk"]]:
                mod["max_risk"] = severity
            mod["files"].add(rel_path)

        def _emit_target(layer: str, pattern: str, ls: int, le: int,
                         target_api: str, kind: str, auto: bool, reason: str,
                         dynamic: bool = False) -> None:
            targets.append(RewriteTarget(
                id=_make_id(layer, rel_path, ls, pattern),
                layer=layer, source_file=rel_path, line_start=ls, line_end=le,
                current_pattern=pattern, current_snippet=_snippet(src_lines, ls, le),
                target_api=target_api, migration_kind=kind, auto_migratable=auto,
                blocking_reason=reason, symbol=_symbol_at(ls, classes, methods),
                module=module, dynamic=dynamic,
            ))

        # ── Layer 1: JPA annotations ───────────────────────────────────────
        ann_n = _count(_JPA_ANNOTATION_RE, source)
        if ann_n:
            am = _JPA_ANNOTATION_RE.search(source)
            _emit_finding(LAYER_JPA, "low", "JPA mapping annotations",
                          _line_at(nl, am.start()), ann_n)
        # Deprecated annotations → escalate + emit rewrite targets (per occurrence).
        dep_matches = list(_JPA_DEPRECATED_RE.finditer(source))
        if dep_matches:
            jpa_deprecated_seen = True
            first = dep_matches[0]
            _emit_finding(LAYER_JPA, "high",
                          "Deprecated Hibernate annotation (@Type/@TypeDef/@GenericGenerator)",
                          _line_at(nl, first.start()), len(dep_matches))
            incompatible.add("Deprecated Hibernate annotations (@Type(type=)/@TypeDef) — "
                             "reworked in Hibernate 6")
            for m in dep_matches:
                ls = _line_at(nl, m.start())
                api, kind, auto, reason = _jpa_deprecated_target(m.group(0))
                _emit_target(LAYER_JPA, "deprecated-annotation", ls, ls,
                             api, kind, auto, reason)

        # ── Layer 2: Criteria API ──────────────────────────────────────────
        dynamic = has_abstraction or has_reflection
        crit_matches: list[tuple[re.Match, bool]] = (
            [(m, False) for m in _CRITERIA_JPA_RE.finditer(source)]
            + [(m, True) for m in _CRITERIA_LEGACY_RE.finditer(source)]
        )
        if crit_matches:
            severity = "critical" if dynamic else "high"
            first_line = min(_line_at(nl, m.start()) for m, _ in crit_matches)
            label = ("Dynamic Criteria construction (reflection/abstraction DAO)"
                     if dynamic else "Criteria API usage")
            _emit_finding(LAYER_CRITERIA, severity, label, first_line,
                          len(crit_matches), dynamic=dynamic)
            if dynamic:
                criteria_dynamic_global = True
                mod["criteria_dynamic"] = True
                incompatible.add("Criteria built dynamically via reflection/abstraction layer "
                                 "(Hibernate 6 incompatible patterns likely)")
            for m, legacy in crit_matches:
                ls = _line_at(nl, m.start())
                api, kind, auto, reason = _criteria_target(legacy, dynamic)
                _emit_target(LAYER_CRITERIA,
                             "legacy-criteria" if legacy else "jpa-criteria",
                             ls, ls, api, kind, auto, reason, dynamic=dynamic)
                if legacy:
                    incompatible.add("Legacy org.hibernate.Criteria / Restrictions / Projections "
                                     "(removed in Hibernate 6 — move to JPA CriteriaBuilder)")

        # ── Layer 3: HQL / string queries ──────────────────────────────────
        hql_matches = list(_HQL_RE.finditer(source))
        if hql_matches:
            file_has_concat = False
            first_line = _line_at(nl, hql_matches[0].start())
            for m in hql_matches:
                ls = _line_at(nl, m.start())
                window = source[m.start(): m.start() + 400]
                is_concat = bool(_HQL_CONCAT_RE.match("." + window) or _HQL_CONCAT_RE.search(window))
                le = _line_at(nl, m.start() + (window.find(";") if ";" in window else 0))
                le = max(ls, le)
                api, kind, auto, reason = _hql_target(is_concat)
                _emit_target(LAYER_HQL, "concat-query" if is_concat else "static-query",
                             ls, le if is_concat else ls, api, kind, auto, reason)
                if is_concat:
                    file_has_concat = True
            sev = "high" if file_has_concat else "medium"
            label = ("String-concatenated HQL/native query (dynamic SQL shape)"
                     if file_has_concat else "HQL / native string query")
            _emit_finding(LAYER_HQL, sev, label, first_line, len(hql_matches))
            if file_has_concat:
                concat_query_global = True
                incompatible.add("String-concatenated HQL/native queries — HIGH risk under "
                                 "Hibernate 6 HQL parser changes")

        # ── Layer 4: Hibernate SPI / internal ──────────────────────────────
        spi_matches = list(_SPI_RE.finditer(source))
        if spi_matches:
            spi_global = True
            mod["has_spi"] = True
            first = spi_matches[0]
            _emit_finding(LAYER_SPI, "critical",
                          "Hibernate SPI / internal API (UserType/Interceptor/SPI)",
                          _line_at(nl, first.start()), len(spi_matches))
            incompatible.add("Custom Hibernate SPI (UserType/CompositeUserType/Interceptor/"
                             "EventListener) — primary Hibernate 5→6 breakage source")
            for m in spi_matches:
                ls = _line_at(nl, m.start())
                api, kind, auto, reason = _spi_target(m.group(0))
                _emit_target(LAYER_SPI, "hibernate-spi", ls, ls, api, kind, auto, reason)

        # ── Critical call-chain detection ──────────────────────────────────
        if has_abstraction and (crit_matches or has_reflection):
            am = _ABSTRACTION_CLASS_RE.search(source)
            reason_bits = []
            if crit_matches:
                reason_bits.append("Criteria construction")
            if has_reflection:
                reason_bits.append("reflection-based entity metadata")
                reflection_query_global = True
            call_chains.append({
                "class": am.group(0),
                "source_file": rel_path,
                "first_line": _line_at(nl, am.start()),
                "reason": "dynamic query generation: " + " + ".join(reason_bits),
                "module": module,
            })
            # Golden-SQL hotspot: rank by dynamic query volume in this file.
            dyn_q = len(crit_matches) + _count(_REFLECTION_RE, source)
            hotspots.append({
                "symbol": _symbol_at(_line_at(nl, am.start()), classes, methods) or am.group(0),
                "source_file": rel_path,
                "module": module,
                "dynamic_query_count": dyn_q,
                "reflection": has_reflection,
                "reason": "concentrates runtime-generated queries — pin golden-SQL tests here",
            })

    strat = HibernateStratification()
    strat.findings = findings
    strat.rewrite_targets = sorted(
        targets, key=lambda t: (t.source_file, t.line_start, t.layer, t.id)
    )
    strat.critical_call_chains = sorted(call_chains, key=lambda c: (c["source_file"], c["first_line"]))
    strat.golden_sql_hotspots = sorted(
        hotspots, key=lambda h: (-h["dynamic_query_count"], h["source_file"])
    )[:25]

    if not findings:
        strat.detected = False
        strat.classification = CLASS_NONE
        strat.classification_label = _CLASS_LABELS[CLASS_NONE]
        return strat

    strat.detected = True
    strat.incompatible_patterns = sorted(incompatible)

    # Per-layer target kind tallies.
    def _kind_counts(layer: str) -> dict[str, int]:
        c = {KIND_MANUAL: 0, KIND_ASSISTED: 0, KIND_MECHANICAL: 0, KIND_REVIEW: 0}
        for t in targets:
            if t.layer == layer:
                c[t.migration_kind] = c.get(t.migration_kind, 0) + 1
        return c

    matrix: list[HibernateLayerRisk] = []
    point_days: dict[str, float] = {}

    # Layer 1
    if layer_files[LAYER_JPA]:
        kc = _kind_counts(LAYER_JPA)
        if jpa_deprecated_seen:
            risk, conf = "high", "medium"
            reason = ("Deprecated Hibernate annotations present "
                      "(@Type(type=)/@TypeDef/@GenericGenerator reworked in H6)")
            point = _effort_point(len(layer_files[LAYER_JPA]), _EFFORT_RATES["jpa_deprecated"])
        else:
            risk, conf = "low", "high"
            reason = ("Standard JPA mapping only — namespace handled by jakarta "
                      "migration, no Hibernate-6 annotation breakage")
            point = _effort_point(len(layer_files[LAYER_JPA]), _EFFORT_RATES["jpa_standard"], floor=0.2)
        point_days[LAYER_JPA] = point
        matrix.append(HibernateLayerRisk(
            LAYER_JPA, risk, reason, f"~{point}d", len(layer_files[LAYER_JPA]),
            layer_occ[LAYER_JPA], _effort_range(point, conf),
            manual_count=kc[KIND_MANUAL], assisted_count=kc[KIND_ASSISTED],
            mechanical_count=kc[KIND_MECHANICAL], review_count=kc[KIND_REVIEW]))

    # Layer 2
    if layer_files[LAYER_CRITERIA]:
        kc = _kind_counts(LAYER_CRITERIA)
        crit_targets = [t for t in targets if t.layer == LAYER_CRITERIA]
        dyn_n = sum(1 for t in crit_targets if t.dynamic)
        stat_n = len(crit_targets) - dyn_n
        if criteria_dynamic_global:
            risk, conf = "critical", "low"
            reason = ("Criteria constructed dynamically via reflection / abstraction DAOs "
                      "— Hibernate 6 rewrite zone")
            point = _effort_point(len(layer_files[LAYER_CRITERIA]), _EFFORT_RATES["criteria_dynamic"])
        else:
            risk, conf = "high", "medium"
            reason = ("Criteria API in use — JPA Criteria semantics changed; "
                      "legacy org.hibernate.Criteria removed in H6")
            point = _effort_point(len(layer_files[LAYER_CRITERIA]), _EFFORT_RATES["criteria_static"])
        point_days[LAYER_CRITERIA] = point
        matrix.append(HibernateLayerRisk(
            LAYER_CRITERIA, risk, reason, f"~{point}d", len(layer_files[LAYER_CRITERIA]),
            layer_occ[LAYER_CRITERIA], _effort_range(point, conf),
            manual_count=kc[KIND_MANUAL], assisted_count=kc[KIND_ASSISTED],
            mechanical_count=kc[KIND_MECHANICAL], review_count=kc[KIND_REVIEW],
            static_count=stat_n, dynamic_count=dyn_n))

    # Layer 3
    if layer_files[LAYER_HQL]:
        kc = _kind_counts(LAYER_HQL)
        if concat_query_global:
            risk, conf = "high", "medium"
            reason = ("String-concatenated / dynamic queries — HIGH risk under "
                      "Hibernate 6 HQL parser changes")
            point = _effort_point(len(layer_files[LAYER_HQL]), _EFFORT_RATES["hql_concat"])
        else:
            risk, conf = "medium", "high"
            reason = ("Static HQL / native queries — revalidate against Hibernate 6 "
                      "parser; mostly mechanical")
            point = _effort_point(len(layer_files[LAYER_HQL]), _EFFORT_RATES["hql_static"])
        point_days[LAYER_HQL] = point
        matrix.append(HibernateLayerRisk(
            LAYER_HQL, risk, reason, f"~{point}d", len(layer_files[LAYER_HQL]),
            layer_occ[LAYER_HQL], _effort_range(point, conf),
            manual_count=kc[KIND_MANUAL], assisted_count=kc[KIND_ASSISTED],
            mechanical_count=kc[KIND_MECHANICAL], review_count=kc[KIND_REVIEW]))

    # Layer 4
    if layer_files[LAYER_SPI]:
        kc = _kind_counts(LAYER_SPI)
        point = _effort_point(len(layer_files[LAYER_SPI]), _EFFORT_RATES["spi"])
        point_days[LAYER_SPI] = point
        matrix.append(HibernateLayerRisk(
            LAYER_SPI, "critical",
            "Custom Hibernate SPI/internal API (UserType, Interceptor, EventListener, "
            "engine.spi) — primary H5→H6 breakage",
            f"~{point}d", len(layer_files[LAYER_SPI]), layer_occ[LAYER_SPI],
            _effort_range(point, "low"),
            manual_count=kc[KIND_MANUAL], assisted_count=kc[KIND_ASSISTED],
            mechanical_count=kc[KIND_MECHANICAL], review_count=kc[KIND_REVIEW],
            userType_rewrite_count=kc[KIND_MANUAL],
            userType_resolvable_count=kc[KIND_ASSISTED]))

    strat.risk_matrix = matrix

    # ── Module exposure map: serialize sets ────────────────────────────────
    exposure: dict[str, dict] = {}
    for mod_name, info in modules.items():
        exposure[mod_name] = {
            "max_risk": info["max_risk"],
            "criteria_dynamic": info["criteria_dynamic"],
            "has_spi": info["has_spi"],
            "has_reflection": info["has_reflection"],
            "file_count": len(info["files"]),
            "layers": {
                layer: {"severity": d["severity"], "occurrences": d["occurrences"]}
                for layer, d in info["layers"].items()
            },
        }
    strat.module_exposure = exposure

    # ── Effort aggregation (honest: upper bound, layers may share files) ────
    confidences = [r.effort_range.get("confidence", "low") for r in matrix]
    worst_conf = "low" if "low" in confidences else ("medium" if "medium" in confidences else "high")
    strat.total_effort_range_days = {
        "low": round(sum(r.effort_range.get("low", 0) for r in matrix), 1),
        "high": round(sum(r.effort_range.get("high", 0) for r in matrix), 1),
        "confidence": worst_conf,
    }
    strat.effort_model = {
        "unit": "person-days",
        "point_days_per_file": dict(_EFFORT_RATES),
        "range_multipliers_by_confidence": {k: list(v) for k, v in _CONF_MULT.items()},
        "range_method": "low = point × low_mult, high = point × high_mult (per confidence band)",
        "caveat": ("Per-layer efforts may share files; total_effort_range_days is an upper "
                   "bound and is NOT deduplicated across layers."),
    }

    # ── hibernate_readiness (0-100) ────────────────────────────────────────
    crit_f = {f.source_file for f in findings if f.severity == "critical"}
    high_f = {f.source_file for f in findings if f.severity == "high"}
    med_f = {f.source_file for f in findings if f.severity == "medium"}
    low_f = {f.source_file for f in findings if f.severity == "low"}
    deduction = len(crit_f) * 15 + len(high_f) * 8 + len(med_f) * 3 + min(len(low_f), 15)
    strat.readiness = max(0, 100 - deduction)

    # ── Stop-condition logic → upgrade vs rewrite classification ────────────
    stops: list[str] = []
    if criteria_dynamic_global:
        stops.append("Criteria API used dynamically / via abstraction layers "
                     "(DynamicEntityDao, GenericDao, reflection)")
    if spi_global:
        stops.append("Custom Hibernate SPI present (UserType / Interceptor / EventListener / "
                     "engine.spi internals)")
    if reflection_query_global:
        stops.append("Queries generated via reflection / runtime entity metadata")
    if concat_query_global:
        stops.append("SQL/HQL shape not statically inferable (string concatenation)")
    strat.stop_conditions_triggered = stops

    if stops:
        strat.classification = CLASS_REWRITE
    elif layer_files[LAYER_CRITERIA] or concat_query_global or jpa_deprecated_seen:
        strat.classification = CLASS_UPGRADE_CARE
    else:
        strat.classification = CLASS_UPGRADE
    strat.classification_label = _CLASS_LABELS[strat.classification]

    # ── Risk separation: observable vs inferred ─────────────────────────────
    observable: list[str] = []
    inferred: list[str] = []
    if layer_files[LAYER_JPA]:
        observable.append(f"JPA mapping annotations in {len(layer_files[LAYER_JPA])} file(s)")
    if layer_files[LAYER_CRITERIA]:
        observable.append(f"Criteria API references in {len(layer_files[LAYER_CRITERIA])} file(s)")
    if layer_files[LAYER_HQL]:
        observable.append(f"HQL/native query calls in {len(layer_files[LAYER_HQL])} file(s)")
    if layer_files[LAYER_SPI]:
        observable.append(f"Hibernate SPI/internal API in {len(layer_files[LAYER_SPI])} file(s)")
    if criteria_dynamic_global or reflection_query_global:
        inferred.append("Runtime-generated query shapes (reflection/metadata) — exact SQL "
                        "cannot be statically enumerated; breakage surface is larger than "
                        "observable call sites")
    if spi_global:
        inferred.append("Custom SPI hooks fire at runtime across the persistence lifecycle — "
                        "behavioural breakage may not be visible at the call site")
    if concat_query_global:
        inferred.append("Concatenated query strings resolve at runtime — H6 HQL parser changes "
                        "may break queries not reachable by static inspection")
    strat.observable_risk = observable
    strat.inferred_runtime_risk = inferred

    return strat

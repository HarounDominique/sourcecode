"""hibernate_strat.py — Hibernate 5.x → 6.x migration stratification model.

A Hibernate major upgrade is NOT a single dependency bump for enterprise systems
that use dynamic persistence. This module splits Hibernate exposure into four
independent migration domains and classifies each one on its own risk axis:

  1. JPA Annotation Layer        — LOW (unless deprecated annotations exist)
  2. Criteria API Layer          — HIGH, escalates to CRITICAL when built
                                    dynamically via reflection / abstraction DAOs
  3. HQL / String-based Queries  — MEDIUM, escalates to HIGH on concatenation
  4. Hibernate SPI / Internal    — CRITICAL BLOCKER (UserType, Interceptor, SPI)

It also produces:
  - a module-level Hibernate exposure map,
  - critical call-chain detection (DynamicEntityDao, BasicPersistenceModule, …),
  - stop-condition logic that decides UPGRADE ZONE vs REWRITE ZONE,
  - separation of observable code risk from inferred runtime risk.

Entry point: analyze_hibernate(file_paths, root) → HibernateStratification

This is purely additive: it does not aggregate Hibernate into a single score, and
it does not assume that a Spring Boot upgrade resolves ORM risk.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


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
    r"@TypeDef\b|@TypeDefs\b|"
    r"org\.hibernate\.annotations\.Type\b|"
    r"@Type\s*\(\s*type\s*=|"
    r"@GenericGenerator\b|"
    r"@org\.hibernate\.annotations\.Entity\b"
)

# Layer 2 — JPA Criteria API + legacy Hibernate Criteria.
_CRITERIA_JPA_RE = re.compile(
    r"\bCriteriaBuilder\b|\bCriteriaQuery\b|\bCriteriaUpdate\b|"
    r"\bCriteriaDelete\b|\bRoot\s*<|\bPredicate\b|\.criteria\b|"
    r"persistence\.criteria"
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


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class HibernateFinding:
    layer: str
    severity: str
    pattern: str          # short human label of what matched
    source_file: str
    first_line: int
    module: str
    occurrences: int = 1
    dynamic: bool = False  # escalated via reflection / abstraction DAO context

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
    estimated_effort: str
    file_count: int
    occurrence_count: int

    def to_dict(self) -> dict:
        return {
            "layer": self.layer,
            "layer_title": _LAYER_TITLES.get(self.layer, self.layer),
            "risk": self.risk,
            "reason": self.reason,
            "estimated_effort": self.estimated_effort,
            "file_count": self.file_count,
            "occurrence_count": self.occurrence_count,
        }


@dataclass
class HibernateStratification:
    detected: bool = False
    classification: str = CLASS_NONE
    classification_label: str = _CLASS_LABELS[CLASS_NONE]
    risk_matrix: list[HibernateLayerRisk] = field(default_factory=list)
    module_exposure: dict[str, dict] = field(default_factory=dict)
    incompatible_patterns: list[str] = field(default_factory=list)
    critical_call_chains: list[dict] = field(default_factory=list)
    stop_conditions_triggered: list[str] = field(default_factory=list)
    observable_risk: list[str] = field(default_factory=list)
    inferred_runtime_risk: list[str] = field(default_factory=list)
    findings: list[HibernateFinding] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "detected": self.detected,
            "classification": self.classification,
            "classification_label": self.classification_label,
            "stratified": True,
            "risk_matrix": [r.to_dict() for r in self.risk_matrix],
            "module_exposure_map": self.module_exposure,
            "incompatible_patterns": self.incompatible_patterns,
            "critical_call_chains": self.critical_call_chains,
            "stop_conditions_triggered": self.stop_conditions_triggered,
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
            "",
            "Hibernate Migration Impact Matrix:",
            f"  {'Layer':<30} {'Risk':<9} Effort        Reason",
        ]
        for r in self.risk_matrix:
            lines.append(
                f"  {_LAYER_TITLES.get(r.layer, r.layer):<30} "
                f"{r.risk.upper():<9} {r.estimated_effort:<13} {r.reason}"
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
                    f"  {mod:<40} {info.get('max_risk', 'low').upper():<9} "
                    f"{layers}{tag_str}"
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


def _first_line(source: str, match: re.Match) -> int:
    return source[: match.start()].count("\n") + 1


def _count(pattern: re.Pattern, source: str) -> int:
    return sum(1 for _ in pattern.finditer(source))


# ---------------------------------------------------------------------------
# Effort heuristics
# ---------------------------------------------------------------------------

def _effort_bucket(file_count: int, base_days_per_file: float, floor: float = 0.5) -> str:
    days = round(max(floor, file_count * base_days_per_file), 1) if file_count else 0.0
    return f"~{days}d"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def analyze_hibernate(file_paths: list[str], root: Path) -> HibernateStratification:
    """Scan Java sources for stratified Hibernate 5→6 migration risk."""
    findings: list[HibernateFinding] = []
    # module -> aggregation accumulator
    modules: dict[str, dict] = {}
    call_chains: list[dict] = []

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

    for rel_path in file_paths:
        if not rel_path.endswith(".java"):
            continue
        abs_path = root / rel_path
        try:
            source = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        # Per-file escalation context.
        has_abstraction = bool(_ABSTRACTION_CLASS_RE.search(source))
        has_reflection = bool(_REFLECTION_RE.search(source))
        module = _module_of(rel_path)
        mod = modules.setdefault(
            module,
            {"layers": {}, "max_risk": "low", "criteria_dynamic": False,
             "has_spi": False, "has_reflection": False, "files": set()},
        )

        def _record(layer: str, severity: str, pattern_label: str,
                    match: re.Match, occ: int, dynamic: bool = False) -> None:
            findings.append(HibernateFinding(
                layer=layer, severity=severity, pattern=pattern_label,
                source_file=rel_path, first_line=_first_line(source, match),
                module=module, occurrences=occ, dynamic=dynamic,
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

        if has_reflection:
            mod["has_reflection"] = True

        # ── Layer 1: JPA annotations ───────────────────────────────────────
        m = _JPA_ANNOTATION_RE.search(source)
        if m:
            _record(LAYER_JPA, "low", "JPA mapping annotations",
                    m, _count(_JPA_ANNOTATION_RE, source))
        dm = _JPA_DEPRECATED_RE.search(source)
        if dm:
            jpa_deprecated_seen = True
            _record(LAYER_JPA, "high", "Deprecated Hibernate annotation (@Type/@TypeDef/@GenericGenerator)",
                    dm, _count(_JPA_DEPRECATED_RE, source))
            incompatible.add("Deprecated Hibernate annotations (@Type(type=)/@TypeDef) — reworked in Hibernate 6")

        # ── Layer 2: Criteria API ──────────────────────────────────────────
        crit_m = _CRITERIA_JPA_RE.search(source)
        legacy_m = _CRITERIA_LEGACY_RE.search(source)
        crit_hit = crit_m or legacy_m
        if crit_hit:
            dynamic = has_abstraction or has_reflection
            severity = "critical" if dynamic else "high"
            occ = _count(_CRITERIA_JPA_RE, source) + _count(_CRITERIA_LEGACY_RE, source)
            label = ("Dynamic Criteria construction (reflection/abstraction DAO)"
                     if dynamic else "Criteria API usage")
            _record(LAYER_CRITERIA, severity, label, crit_hit, occ, dynamic=dynamic)
            if dynamic:
                criteria_dynamic_global = True
                mod["criteria_dynamic"] = True
                incompatible.add("Criteria built dynamically via reflection/abstraction layer "
                                 "(Hibernate 6 incompatible patterns likely)")
            if legacy_m:
                incompatible.add("Legacy org.hibernate.Criteria / Restrictions / Projections "
                                 "(removed in Hibernate 6 — must move to JPA CriteriaBuilder)")

        # ── Layer 3: HQL / string queries ──────────────────────────────────
        hql_m = _HQL_RE.search(source)
        if hql_m:
            concat_m = _HQL_CONCAT_RE.search(source)
            if concat_m:
                concat_query_global = True
                _record(LAYER_HQL, "high",
                        "String-concatenated HQL/native query (dynamic SQL shape)",
                        concat_m, _count(_HQL_RE, source))
                incompatible.add("String-concatenated HQL/native queries — HIGH risk under "
                                 "Hibernate 6 ORM parser changes")
            else:
                _record(LAYER_HQL, "medium", "HQL / native string query",
                        hql_m, _count(_HQL_RE, source))

        # ── Layer 4: Hibernate SPI / internal ──────────────────────────────
        spi_m = _SPI_RE.search(source)
        if spi_m:
            spi_global = True
            mod["has_spi"] = True
            _record(LAYER_SPI, "critical", "Hibernate SPI / internal API (UserType/Interceptor/SPI)",
                    spi_m, _count(_SPI_RE, source))
            incompatible.add("Custom Hibernate SPI (UserType/CompositeUserType/Interceptor/"
                             "EventListener) — primary Hibernate 5→6 breakage source")

        # ── Critical call-chain detection ──────────────────────────────────
        if has_abstraction and (crit_hit or has_reflection):
            am = _ABSTRACTION_CLASS_RE.search(source)
            reason_bits = []
            if crit_hit:
                reason_bits.append("Criteria construction")
            if has_reflection:
                reason_bits.append("reflection-based entity metadata")
                reflection_query_global = True
            call_chains.append({
                "class": am.group(0),
                "source_file": rel_path,
                "first_line": _first_line(source, am),
                "reason": "dynamic query generation: " + " + ".join(reason_bits),
                "module": module,
            })

    strat = HibernateStratification()
    strat.findings = findings

    if not findings:
        strat.detected = False
        strat.classification = CLASS_NONE
        strat.classification_label = _CLASS_LABELS[CLASS_NONE]
        return strat

    strat.detected = True
    strat.critical_call_chains = call_chains
    strat.incompatible_patterns = sorted(incompatible)

    # ── Build per-layer risk matrix ────────────────────────────────────────
    matrix: list[HibernateLayerRisk] = []

    # Layer 1
    if layer_files[LAYER_JPA]:
        if jpa_deprecated_seen:
            risk, reason = "high", ("Deprecated Hibernate annotations present "
                                    "(@Type(type=)/@TypeDef/@GenericGenerator reworked in H6)")
            effort = _effort_bucket(len(layer_files[LAYER_JPA]), 0.25)
        else:
            risk, reason = "low", ("Standard JPA mapping only — namespace handled by "
                                   "jakarta migration, no Hibernate-6 annotation breakage")
            effort = _effort_bucket(len(layer_files[LAYER_JPA]), 0.02, floor=0.2)
        matrix.append(HibernateLayerRisk(
            LAYER_JPA, risk, reason, effort,
            len(layer_files[LAYER_JPA]), layer_occ[LAYER_JPA]))

    # Layer 2
    if layer_files[LAYER_CRITERIA]:
        if criteria_dynamic_global:
            risk, reason = "critical", ("Criteria constructed dynamically via reflection / "
                                        "abstraction DAOs — Hibernate 6 rewrite zone")
            effort = _effort_bucket(len(layer_files[LAYER_CRITERIA]), 0.75)
        else:
            risk, reason = "high", ("Criteria API in use — JPA Criteria semantics changed; "
                                    "legacy org.hibernate.Criteria removed in H6")
            effort = _effort_bucket(len(layer_files[LAYER_CRITERIA]), 0.4)
        matrix.append(HibernateLayerRisk(
            LAYER_CRITERIA, risk, reason, effort,
            len(layer_files[LAYER_CRITERIA]), layer_occ[LAYER_CRITERIA]))

    # Layer 3
    if layer_files[LAYER_HQL]:
        if concat_query_global:
            risk, reason = "high", ("String-concatenated / dynamic queries — HIGH risk under "
                                    "Hibernate 6 HQL parser changes")
            effort = _effort_bucket(len(layer_files[LAYER_HQL]), 0.3)
        else:
            risk, reason = "medium", ("Static HQL / native queries — revalidate against "
                                      "Hibernate 6 parser; mostly mechanical")
            effort = _effort_bucket(len(layer_files[LAYER_HQL]), 0.15)
        matrix.append(HibernateLayerRisk(
            LAYER_HQL, risk, reason, effort,
            len(layer_files[LAYER_HQL]), layer_occ[LAYER_HQL]))

    # Layer 4
    if layer_files[LAYER_SPI]:
        risk, reason = "critical", ("Custom Hibernate SPI/internal API (UserType, Interceptor, "
                                    "EventListener, engine.spi) — primary H5→H6 breakage")
        effort = _effort_bucket(len(layer_files[LAYER_SPI]), 1.0)
        matrix.append(HibernateLayerRisk(
            LAYER_SPI, risk, reason, effort,
            len(layer_files[LAYER_SPI]), layer_occ[LAYER_SPI]))

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
    elif layer_files[LAYER_HQL] or layer_files[LAYER_JPA]:
        strat.classification = CLASS_UPGRADE
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

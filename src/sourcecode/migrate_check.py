"""migrate_check.py — Spring Boot 2→3 (javax→jakarta) migration readiness checker.

Scans Java source files for import namespaces and class patterns that must be
updated when migrating from Spring Boot 2.x (javax.*) to Spring Boot 3.x (jakarta.*).

Entry point: run_migrate_check(file_paths, root) → MigrationReport
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Rule catalogue
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Rule:
    id: str
    severity: str
    title: str
    explanation: str
    fix_hint: str
    import_pattern: Optional[re.Pattern] = None   # matches the import statement
    extends_pattern: Optional[re.Pattern] = None  # matches an extends clause


_IMPORT_RULES: list[_Rule] = [
    _Rule(
        id="MIG-001",
        severity="critical",
        title="javax.persistence import — JPA namespace not migrated to jakarta",
        explanation=(
            "Spring Boot 3 uses Jakarta EE 9 which moved JPA to the jakarta.persistence "
            "namespace. Files importing javax.persistence will not compile after migration."
        ),
        fix_hint="Replace 'javax.persistence' with 'jakarta.persistence' across all affected files.",
        import_pattern=re.compile(r"^[ \t]*import\s+(javax\.persistence[^;]+);", re.MULTILINE),
    ),
    _Rule(
        id="MIG-002",
        severity="high",
        title="javax.servlet import — Servlet API not migrated to jakarta",
        explanation=(
            "Spring Boot 3 bundles Jakarta Servlet 6.0. Filters, HttpServletRequest, and "
            "HttpServletResponse referencing javax.servlet will break after migration."
        ),
        fix_hint="Replace 'javax.servlet' with 'jakarta.servlet' and update the servlet-api dependency.",
        import_pattern=re.compile(r"^[ \t]*import\s+(javax\.servlet[^;]+);", re.MULTILINE),
    ),
    _Rule(
        id="MIG-003",
        severity="high",
        title="javax.validation import — Bean Validation not migrated to jakarta",
        explanation=(
            "Spring Boot 3 uses Hibernate Validator 8.x which implements jakarta.validation. "
            "Constraint annotations (@NotNull, @Valid, etc.) under javax.validation will not be "
            "picked up by the validator after migration."
        ),
        fix_hint="Replace 'javax.validation' with 'jakarta.validation'.",
        import_pattern=re.compile(r"^[ \t]*import\s+(javax\.validation[^;]+);", re.MULTILINE),
    ),
    _Rule(
        id="MIG-004",
        severity="high",
        title="javax.transaction import — TX API not migrated to jakarta",
        explanation=(
            "Spring Boot 3 depends on Jakarta Transactions (jakarta.transaction). "
            "Direct javax.transaction imports (@Transactional from javax or UserTransaction) "
            "will resolve to the wrong class after migration."
        ),
        fix_hint="Replace 'javax.transaction' with 'jakarta.transaction'.",
        import_pattern=re.compile(r"^[ \t]*import\s+(javax\.transaction[^;]+);", re.MULTILINE),
    ),
    _Rule(
        id="MIG-006",
        severity="medium",
        title="javax.annotation import — CDI annotations not migrated to jakarta",
        explanation=(
            "jakarta.annotation replaces javax.annotation in Jakarta EE 9+. "
            "@PostConstruct, @PreDestroy, @Resource are affected."
        ),
        fix_hint="Replace 'javax.annotation' with 'jakarta.annotation'.",
        import_pattern=re.compile(r"^[ \t]*import\s+(javax\.annotation[^;]+);", re.MULTILINE),
    ),
    _Rule(
        id="MIG-007",
        severity="medium",
        title="javax.inject import — DI annotations not migrated to jakarta",
        explanation=(
            "jakarta.inject replaces javax.inject in Jakarta EE 9+. "
            "@Inject and @Named from javax.inject are affected."
        ),
        fix_hint="Replace 'javax.inject' with 'jakarta.inject'.",
        import_pattern=re.compile(r"^[ \t]*import\s+(javax\.inject[^;]+);", re.MULTILINE),
    ),
    _Rule(
        id="MIG-008",
        severity="medium",
        title="javax.ws.rs import — JAX-RS API not migrated to jakarta",
        explanation=(
            "jakarta.ws.rs replaces javax.ws.rs in Jakarta EE 9+. "
            "JAX-RS resource classes, Response, and client code are affected."
        ),
        fix_hint="Replace 'javax.ws.rs' with 'jakarta.ws.rs'.",
        import_pattern=re.compile(r"^[ \t]*import\s+(javax\.ws\.rs[^;]+);", re.MULTILINE),
    ),
]

_EXTENDS_RULES: list[_Rule] = [
    _Rule(
        id="MIG-005",
        severity="high",
        title="extends WebSecurityConfigurerAdapter — removed in Spring Security 6",
        explanation=(
            "WebSecurityConfigurerAdapter was deprecated in Spring Security 5.7 and removed in "
            "Spring Security 6 (Spring Boot 3). Classes extending it must be replaced with "
            "SecurityFilterChain @Bean methods in a @Configuration class."
        ),
        fix_hint=(
            "Remove the class extension and expose a SecurityFilterChain @Bean instead. "
            "See the Spring Security 6 migration guide."
        ),
        extends_pattern=re.compile(r"\bextends\s+WebSecurityConfigurerAdapter\b"),
    ),
]

_ALL_RULES: list[_Rule] = _IMPORT_RULES + _EXTENDS_RULES

SEVERITY_ORDER: dict[str, int] = {"critical": 0, "high": 1, "medium": 2, "low": 3}


# ---------------------------------------------------------------------------
# Finding
# ---------------------------------------------------------------------------

@dataclass
class MigrationFinding:
    id: str               # deterministic: "{rule_id}-{file_hash[:12]}"
    rule_id: str          # "MIG-001" .. "MIG-008"
    severity: str         # "critical" | "high" | "medium" | "low"
    title: str
    source_file: str      # relative path
    first_line: int       # 1-based line number of first match
    imports_found: list[str] = field(default_factory=list)  # matched import statements
    explanation: str = ""
    fix_hint: str = ""

    @staticmethod
    def make_id(rule_id: str, source_file: str) -> str:
        h = hashlib.sha256(f"{rule_id}:{source_file}".encode()).hexdigest()[:12]
        return f"{rule_id}-{h}"

    def to_dict(self) -> dict:
        d: dict = {
            "id": self.id,
            "rule_id": self.rule_id,
            "severity": self.severity,
            "title": self.title,
            "source_file": self.source_file,
            "first_line": self.first_line,
            "explanation": self.explanation,
            "fix_hint": self.fix_hint,
        }
        if self.imports_found:
            d["imports_found"] = self.imports_found
        return d


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

@dataclass
class MigrationReport:
    schema_version: str = "1.0"
    generated_at: str = ""
    repo_id: str = ""
    git_head: str = ""

    # Core metrics
    readiness_score: int = 100         # 0–100; 100 = ready to migrate
    blocking_count: int = 0            # critical + high finding count
    estimated_effort_days: float = 0.0
    spring_boot_2_detected: bool = False

    findings: list[MigrationFinding] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)

    def finalize(self) -> "MigrationReport":
        if not self.generated_at:
            self.generated_at = datetime.now(timezone.utc).isoformat()

        by_severity: dict[str, int] = {"critical": 0, "high": 0, "medium": 0, "low": 0}
        by_rule: dict[str, int] = {}
        affected_files: set[str] = set()

        for f in self.findings:
            by_severity[f.severity] = by_severity.get(f.severity, 0) + 1
            by_rule[f.rule_id] = by_rule.get(f.rule_id, 0) + 1
            affected_files.add(f.source_file)

        self.blocking_count = by_severity["critical"] + by_severity["high"]

        # Score: deduct per affected-file/severity combination (not per finding, to avoid
        # double-counting a file that imports 10 javax.persistence classes).
        critical_files: set[str] = set()
        high_files: set[str] = set()
        medium_files: set[str] = set()
        low_files: set[str] = set()
        for f in self.findings:
            if f.severity == "critical":
                critical_files.add(f.source_file)
            elif f.severity == "high":
                high_files.add(f.source_file)
            elif f.severity == "medium":
                medium_files.add(f.source_file)
            else:
                low_files.add(f.source_file)

        deduction = (
            len(critical_files) * 15
            + len(high_files) * 8
            + len(medium_files) * 3
            + len(low_files) * 1
        )
        self.readiness_score = max(0, 100 - deduction)

        # Effort: sum per distinct affected file weighted by severity
        self.estimated_effort_days = round(
            len(critical_files) * 0.5
            + len(high_files) * 0.25
            + len(medium_files) * 0.1
            + len(low_files) * 0.05,
            1,
        )

        self.summary = {
            "total_findings": len(self.findings),
            "affected_files": len(affected_files),
            "by_severity": by_severity,
            "by_rule": by_rule,
        }
        return self

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "repo_id": self.repo_id,
            "git_head": self.git_head,
            "readiness_score": self.readiness_score,
            "blocking_count": self.blocking_count,
            "estimated_effort_days": self.estimated_effort_days,
            "spring_boot_2_detected": self.spring_boot_2_detected,
            "summary": self.summary,
            "findings": [f.to_dict() for f in self.findings],
            "limitations": self.limitations,
            "metadata": self.metadata,
        }

    def to_text(self, min_severity: str = "low") -> str:
        min_order = SEVERITY_ORDER.get(min_severity, 3)
        visible = [f for f in self.findings if SEVERITY_ORDER.get(f.severity, 3) <= min_order]

        lines: list[str] = [
            f"Migration Readiness: {self.readiness_score}/100",
            f"Blocking issues: {self.blocking_count}  "
            f"(critical: {self.summary.get('by_severity', {}).get('critical', 0)}, "
            f"high: {self.summary.get('by_severity', {}).get('high', 0)})",
            f"Affected files: {self.summary.get('affected_files', 0)}",
            f"Estimated effort: {self.estimated_effort_days}d",
            "",
        ]

        if not visible:
            lines.append("No findings at or above selected severity.")
            return "\n".join(lines)

        for f in sorted(visible, key=lambda x: (SEVERITY_ORDER.get(x.severity, 3), x.source_file)):
            lines.append(
                f"{f.rule_id} [{f.severity.upper()}] {f.source_file}:{f.first_line}"
            )
            lines.append(f"  {f.title}")
            lines.append(f"  Fix: {f.fix_hint}")
            lines.append("")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

def _scan_file(
    source: str,
    rel_path: str,
    rules: list[_Rule],
) -> list[MigrationFinding]:
    findings: list[MigrationFinding] = []

    for rule in rules:
        if rule.import_pattern is not None:
            matches = list(rule.import_pattern.finditer(source))
            if not matches:
                continue
            # Compute 1-based line number of first match
            first_line = source[: matches[0].start()].count("\n") + 1
            imports_found = [m.group(1) for m in matches]
            findings.append(
                MigrationFinding(
                    id=MigrationFinding.make_id(rule.id, rel_path),
                    rule_id=rule.id,
                    severity=rule.severity,
                    title=rule.title,
                    source_file=rel_path,
                    first_line=first_line,
                    imports_found=imports_found,
                    explanation=rule.explanation,
                    fix_hint=rule.fix_hint,
                )
            )

        elif rule.extends_pattern is not None:
            m = rule.extends_pattern.search(source)
            if m is None:
                continue
            first_line = source[: m.start()].count("\n") + 1
            findings.append(
                MigrationFinding(
                    id=MigrationFinding.make_id(rule.id, rel_path),
                    rule_id=rule.id,
                    severity=rule.severity,
                    title=rule.title,
                    source_file=rel_path,
                    first_line=first_line,
                    explanation=rule.explanation,
                    fix_hint=rule.fix_hint,
                )
            )

    return findings


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_migrate_check(
    file_paths: list[str],
    root: Path,
    *,
    min_severity: str = "low",
) -> MigrationReport:
    """Scan Java files for Spring Boot 2→3 migration blockers.

    Args:
        file_paths:   Relative Java file paths (from find_java_files).
        root:         Absolute repo root.
        min_severity: Filter threshold — findings below this severity are excluded
                      from the report. Choices: critical | high | medium | low.

    Returns:
        MigrationReport with findings, readiness_score, and effort estimate.
    """
    min_order = SEVERITY_ORDER.get(min_severity, 3)
    all_findings: list[MigrationFinding] = []
    limitations: list[str] = []
    read_errors = 0

    for rel_path in file_paths:
        abs_path = root / rel_path
        try:
            source = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            read_errors += 1
            continue

        file_findings = _scan_file(source, rel_path, _ALL_RULES)
        # Apply min_severity filter
        filtered = [f for f in file_findings if SEVERITY_ORDER.get(f.severity, 3) <= min_order]
        all_findings.extend(filtered)

    if read_errors:
        limitations.append(f"{read_errors} file(s) could not be read and were skipped.")

    # Detect Spring Boot 2 pom.xml heuristic (best-effort, non-fatal)
    spring_boot_2 = _detect_spring_boot_2(root)

    report = MigrationReport(
        spring_boot_2_detected=spring_boot_2,
        findings=all_findings,
        limitations=limitations,
        metadata={
            "java_files_scanned": len(file_paths),
            "min_severity": min_severity,
            "rules_applied": [r.id for r in _ALL_RULES],
        },
    )

    # Populate git_head — non-fatal
    try:
        import subprocess as _sub
        _r = _sub.run(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=3,
        )
        if _r.returncode == 0:
            report.git_head = _r.stdout.strip()
    except Exception:
        pass

    return report.finalize()


def _detect_spring_boot_2(root: Path) -> bool:
    """Return True if any pom.xml or build.gradle declares spring-boot 2.x."""
    _SB2 = re.compile(r"spring[-.]boot[^\"'\n]*[\"']?2\.\d+", re.IGNORECASE)
    for name in ("pom.xml", "build.gradle", "build.gradle.kts"):
        candidate = root / name
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
            if _SB2.search(text):
                return True
        except OSError:
            pass
    return False

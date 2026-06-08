"""migrate_check.py — Java 8/Spring Boot 2 migration readiness checker.

Scans Java source files for patterns that must be addressed when migrating:
  - Spring Boot 2 → 3 (javax → jakarta, Spring Security 6)
  - Java 8 → 17 / 21 (SecurityManager, Nashorn, Unsafe, reflection, etc.)

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
    migration_target: str = "spring_boot_3"   # jakarta | spring_security_6 | java_11 | java_15 | java_17 | java_9_plus | java_18_plus
    openrewrite_recipe: Optional[str] = None  # Official OpenRewrite recipe ID, if one exists
    import_pattern: Optional[re.Pattern] = None   # matches the import statement
    extends_pattern: Optional[re.Pattern] = None  # matches an extends clause
    code_pattern: Optional[re.Pattern] = None     # matches arbitrary code anywhere in the file


# ---------------------------------------------------------------------------
# Jakarta namespace rules (Spring Boot 2 → 3)
# ---------------------------------------------------------------------------

_JAKARTA_RULES: list[_Rule] = [
    _Rule(
        id="MIG-001",
        severity="critical",
        title="javax.persistence import — JPA namespace not migrated to jakarta",
        explanation=(
            "Spring Boot 3 uses Jakarta EE 9 which moved JPA to the jakarta.persistence "
            "namespace. Files importing javax.persistence will not compile after migration."
        ),
        fix_hint="Replace 'javax.persistence' with 'jakarta.persistence' across all affected files.",
        migration_target="jakarta",
        openrewrite_recipe="org.openrewrite.java.migrate.jakarta.JavaxPersistenceToJakartaPersistence",
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
        migration_target="jakarta",
        openrewrite_recipe="org.openrewrite.java.migrate.jakarta.JavaxServletToJakartaServlet",
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
        migration_target="jakarta",
        openrewrite_recipe="org.openrewrite.java.migrate.jakarta.JavaxValidationToJakartaValidation",
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
        migration_target="jakarta",
        openrewrite_recipe="org.openrewrite.java.migrate.jakarta.JavaxTransactionToJakartaTransaction",
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
        migration_target="jakarta",
        openrewrite_recipe="org.openrewrite.java.migrate.jakarta.JavaxAnnotationPackageToJakarta",
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
        migration_target="jakarta",
        openrewrite_recipe="org.openrewrite.java.migrate.jakarta.JavaxInjectToJakartaInject",
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
        migration_target="jakarta",
        openrewrite_recipe="org.openrewrite.java.migrate.jakarta.JavaxWsRsToJakartaWsRs",
        import_pattern=re.compile(r"^[ \t]*import\s+(javax\.ws\.rs[^;]+);", re.MULTILINE),
    ),
    _Rule(
        id="MIG-009",
        severity="medium",
        title="javax.jms import — JMS API not migrated to jakarta",
        explanation=(
            "jakarta.jms replaces javax.jms in Jakarta EE 9+. "
            "Message listeners, ConnectionFactory, and Queue references are affected."
        ),
        fix_hint="Replace 'javax.jms' with 'jakarta.jms' and ensure messaging provider supports Jakarta EE 9.",
        migration_target="jakarta",
        openrewrite_recipe="org.openrewrite.java.migrate.jakarta.JavaxJmsToJakartaJms",
        import_pattern=re.compile(r"^[ \t]*import\s+(javax\.jms[^;]+);", re.MULTILINE),
    ),
]

# ---------------------------------------------------------------------------
# Spring Security 6 rules
# ---------------------------------------------------------------------------

_SPRING_SECURITY_RULES: list[_Rule] = [
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
        migration_target="spring_security_6",
        openrewrite_recipe="org.openrewrite.java.spring.security6.WebSecurityConfigurerAdapterToSecurityFilterChain",
        extends_pattern=re.compile(r"\bextends\s+WebSecurityConfigurerAdapter\b"),
    ),
    _Rule(
        id="MIG-020",
        severity="high",
        title="antMatchers / authorizeRequests — deprecated Spring Security 6 patterns",
        explanation=(
            "antMatchers() was replaced by requestMatchers() and authorizeRequests() was replaced "
            "by authorizeHttpRequests() in Spring Security 6. The old methods are removed. "
            "Also, AuthenticationManagerBuilder-based configuration is superseded by "
            "UserDetailsService and PasswordEncoder beans."
        ),
        fix_hint=(
            "Replace antMatchers() with requestMatchers(), authorizeRequests() with "
            "authorizeHttpRequests(). Migrate HttpSecurity config to Lambda DSL style."
        ),
        migration_target="spring_security_6",
        openrewrite_recipe="org.openrewrite.java.spring.security6.HttpSecurityLambdaDsl",
        code_pattern=re.compile(
            r"\.antMatchers\s*\(|\.authorizeRequests\s*\(\)|"
            r"\bAuthenticationManagerBuilder\b",
            re.MULTILINE,
        ),
    ),
    _Rule(
        id="MIG-019",
        severity="high",
        title="SpringFox / Swagger 2 — incompatible with Spring Boot 3 / Spring MVC 6",
        explanation=(
            "SpringFox (io.springfox) requires Spring MVC internals that were removed in "
            "Spring Framework 6. Applications using @EnableSwagger2 or springfox.documentation "
            "will fail to start after migration to Spring Boot 3."
        ),
        fix_hint=(
            "Migrate to springdoc-openapi-starter-webmvc-ui (OpenAPI 3). "
            "Replace springfox-swagger2 + springfox-swagger-ui dependencies."
        ),
        migration_target="spring_security_6",
        openrewrite_recipe=None,
        import_pattern=re.compile(
            r"^[ \t]*import\s+(springfox\.[^;]+);",
            re.MULTILINE,
        ),
        code_pattern=re.compile(r"@EnableSwagger2\b"),
    ),
]

# ---------------------------------------------------------------------------
# Java 11 — APIs removed from the JDK (must add as explicit deps)
# ---------------------------------------------------------------------------

_JAVA_11_RULES: list[_Rule] = [
    _Rule(
        id="MIG-021",
        severity="high",
        title="javax.xml.bind (JAXB) — removed from JDK in Java 11",
        explanation=(
            "JAXB was part of the JDK in Java 8 (java.xml.bind module) but removed in Java 11. "
            "Code importing javax.xml.bind will fail to compile on Java 11+ unless the "
            "jakarta.xml.bind-api and jaxb-impl artifacts are added as dependencies."
        ),
        fix_hint=(
            "Add 'jakarta.xml.bind:jakarta.xml.bind-api' and 'org.glassfish.jaxb:jaxb-runtime' "
            "as dependencies. Also migrate javax.xml.bind → jakarta.xml.bind for Spring Boot 3."
        ),
        migration_target="java_11",
        openrewrite_recipe=None,
        import_pattern=re.compile(r"^[ \t]*import\s+(javax\.xml\.bind[^;]+);", re.MULTILINE),
    ),
    _Rule(
        id="MIG-022",
        severity="high",
        title="javax.xml.ws (JAX-WS) — removed from JDK in Java 11",
        explanation=(
            "JAX-WS was bundled with the JDK in Java 8 but removed in Java 11. "
            "Applications importing javax.xml.ws require an explicit jaxws-rt dependency."
        ),
        fix_hint=(
            "Add 'com.sun.xml.ws:jaxws-rt' as a dependency. "
            "Also migrate javax.xml.ws → jakarta.xml.ws for Spring Boot 3 targets."
        ),
        migration_target="java_11",
        openrewrite_recipe=None,
        import_pattern=re.compile(r"^[ \t]*import\s+(javax\.xml\.ws[^;]+);", re.MULTILINE),
    ),
]

# ---------------------------------------------------------------------------
# Java 15 — Nashorn removed
# ---------------------------------------------------------------------------

_JAVA_15_RULES: list[_Rule] = [
    _Rule(
        id="MIG-012",
        severity="high",
        title="Nashorn ScriptEngine — removed in Java 15",
        explanation=(
            "The Nashorn JavaScript engine was deprecated in Java 11 (JEP 335) and removed "
            "in Java 15 (JEP 372). Code importing jdk.nashorn.* or obtaining the Nashorn engine "
            "via ScriptEngineManager will fail at runtime on Java 15+."
        ),
        fix_hint=(
            "Replace Nashorn with GraalVM Polyglot API (org.graalvm.sdk:polyglot) or "
            "Mozilla Rhino (org.mozilla:rhino). Remove jdk.nashorn.* imports."
        ),
        migration_target="java_15",
        openrewrite_recipe=None,
        import_pattern=re.compile(r"^[ \t]*import\s+(jdk\.nashorn[^;]+);", re.MULTILINE),
        code_pattern=re.compile(r'getEngineByName\s*\(\s*["\']nashorn["\']'),
    ),
]

# ---------------------------------------------------------------------------
# Java 17 — SecurityManager removed (JEP 411)
# ---------------------------------------------------------------------------

_JAVA_17_RULES: list[_Rule] = [
    _Rule(
        id="MIG-010",
        severity="critical",
        title="SecurityManager / AccessController — removed in Java 17 (JEP 411)",
        explanation=(
            "The Security Manager and its associated APIs (SecurityManager, AccessController, "
            "System.setSecurityManager, System.getSecurityManager, SecurityPermission, "
            "RuntimePermission) were deprecated for removal in Java 17 and are non-functional. "
            "In Java 17, setSecurityManager() throws UnsupportedOperationException unless "
            "the 'java.security.manager=allow' system property is set."
        ),
        fix_hint=(
            "Remove SecurityManager installation and AccessController.doPrivileged() calls. "
            "Replace with proper module-based access control or Jakarta Security. "
            "See JEP 411 migration guide."
        ),
        migration_target="java_17",
        openrewrite_recipe=None,
        code_pattern=re.compile(
            r"System\.(get|set)SecurityManager\s*\(|"
            r"\bSecurityManager\s+\w+\s*[=;({]|"   # variable declaration, requires code-context char to avoid Javadoc FPs
            r"\bnew\s+SecurityManager\s*\(|"
            r"\bextends\s+SecurityManager\b|"
            r"\bAccessController\.(doPrivileged|checkPermission|getContext)\s*\(",
        ),
    ),
]

# ---------------------------------------------------------------------------
# Java 9+ — Strong encapsulation (JPMS) — internal APIs
# ---------------------------------------------------------------------------

_JAVA_9_RULES: list[_Rule] = [
    _Rule(
        id="MIG-011",
        severity="high",
        title="JDK internal API imports (sun.* / com.sun.net.*) — strong encapsulation since Java 9",
        explanation=(
            "Imports from sun.* and com.sun.net.* reference JDK-internal APIs that are "
            "not part of the public specification. Since Java 9 (JPMS), these packages are "
            "strongly encapsulated and require --add-exports / --add-opens JVM flags, "
            "which are cumbersome and may be removed in future Java releases."
        ),
        fix_hint=(
            "Replace internal API usage with public equivalents. "
            "For com.sun.net.httpserver, migrate to java.net.http.HttpServer or a framework. "
            "Add '--add-exports java.base/sun.misc=ALL-UNNAMED' only as a last resort."
        ),
        migration_target="java_9_plus",
        openrewrite_recipe=None,
        import_pattern=re.compile(
            r"^[ \t]*import\s+(sun\.[^;]+|com\.sun\.(?:net|tools|jdi|source|management)[^;]+);",
            re.MULTILINE,
        ),
    ),
    _Rule(
        id="MIG-013",
        severity="high",
        title="sun.misc.Unsafe — direct access requires --add-opens since Java 9",
        explanation=(
            "sun.misc.Unsafe is a JDK-internal class not exposed by the public module system. "
            "Accessing it via reflection or direct import requires "
            "'--add-opens java.base/sun.misc=ALL-UNNAMED' on Java 9+. "
            "Many frameworks (ByteBuddy, CGLIB, ASM) use Unsafe internally."
        ),
        fix_hint=(
            "Remove direct Unsafe usage and rely on VarHandle (java.lang.invoke.VarHandle) "
            "as the public replacement. Ensure framework versions used are Java 17+ compatible."
        ),
        migration_target="java_9_plus",
        openrewrite_recipe=None,
        import_pattern=re.compile(
            r"^[ \t]*import\s+(sun\.misc\.Unsafe[^;]*);",
            re.MULTILINE,
        ),
        code_pattern=re.compile(
            r'Unsafe\.getUnsafe\s*\(|"theUnsafe"|getDeclaredField\s*\(\s*["\']theUnsafe["\']',
        ),
    ),
    _Rule(
        id="MIG-014",
        severity="medium",
        title="setAccessible(true) — may throw InaccessibleObjectException on Java 9+",
        explanation=(
            "Reflective access via setAccessible(true) to JDK-internal classes throws "
            "InaccessibleObjectException on Java 9+ unless the owning module grants access. "
            "This is an 'illegal reflective access' warning in Java 9-15 and a hard failure "
            "in Java 17+ for strongly-encapsulated modules."
        ),
        fix_hint=(
            "Ensure setAccessible() calls target application code, not JDK internal classes. "
            "Add necessary '--add-opens' flags for unavoidable cases. "
            "Prefer public APIs to avoid reflection on JDK internals entirely."
        ),
        migration_target="java_9_plus",
        openrewrite_recipe=None,
        code_pattern=re.compile(r"\.setAccessible\s*\(\s*true\s*\)"),
    ),
]

# ---------------------------------------------------------------------------
# Java 18+ — finalize() deprecated for removal
# ---------------------------------------------------------------------------

_JAVA_18_RULES: list[_Rule] = [
    _Rule(
        id="MIG-015",
        severity="medium",
        title="finalize() override — deprecated for removal since Java 9, removed in Java 18",
        explanation=(
            "Object.finalize() was deprecated in Java 9 and deprecated-for-removal in Java 18 "
            "(JEP 421). Overriding finalize() is unreliable, may delay GC, and the mechanism "
            "is being removed from the platform. Java 18+ emits warnings; future JDK versions "
            "will not call finalizers."
        ),
        fix_hint=(
            "Replace finalize() with try-with-resources (AutoCloseable/Closeable) or "
            "java.lang.ref.Cleaner for resource cleanup."
        ),
        migration_target="java_18_plus",
        openrewrite_recipe=None,
        code_pattern=re.compile(
            r"\b(?:protected|public)\s+void\s+finalize\s*\(\s*\)",
        ),
    ),
]

# ---------------------------------------------------------------------------
# Best-practice / low-severity — legacy date/time API
# ---------------------------------------------------------------------------

_LEGACY_API_RULES: list[_Rule] = [
    _Rule(
        id="MIG-016",
        severity="low",
        title="Legacy date/time API (java.util.Date / Calendar / SimpleDateFormat)",
        explanation=(
            "java.util.Date, java.util.Calendar, and java.text.SimpleDateFormat are "
            "thread-unsafe and error-prone. They are superseded by java.time (JSR-310) "
            "introduced in Java 8. While not removed, they cause issues in multi-threaded "
            "Spring applications and should be migrated before upgrading."
        ),
        fix_hint=(
            "Replace Date with LocalDate/LocalDateTime/ZonedDateTime, "
            "Calendar with java.time.Calendar, "
            "SimpleDateFormat with DateTimeFormatter (thread-safe)."
        ),
        migration_target="java_8_best_practice",
        openrewrite_recipe="org.openrewrite.java.migrate.JavaTimeAPIs",
        import_pattern=re.compile(
            r"^[ \t]*import\s+(java\.util\.(?:Date|Calendar|GregorianCalendar)"
            r"|java\.text\.(?:SimpleDateFormat|DateFormat))[^;]*;",
            re.MULTILINE,
        ),
    ),
]

# ---------------------------------------------------------------------------
# All rules list
# ---------------------------------------------------------------------------

_ALL_RULES: list[_Rule] = (
    _JAKARTA_RULES
    + _SPRING_SECURITY_RULES
    + _JAVA_11_RULES
    + _JAVA_15_RULES
    + _JAVA_17_RULES
    + _JAVA_9_RULES
    + _JAVA_18_RULES
    + _LEGACY_API_RULES
)

SEVERITY_ORDER: dict[str, int] = {"critical": 0, "high": 1, "medium": 2, "low": 3}


# ---------------------------------------------------------------------------
# Finding
# ---------------------------------------------------------------------------

@dataclass
class MigrationFinding:
    id: str               # deterministic: "{rule_id}-{file_hash[:12]}"
    rule_id: str          # "MIG-001" .. "MIG-022"
    severity: str         # "critical" | "high" | "medium" | "low"
    title: str
    source_file: str      # relative path
    first_line: int       # 1-based line number of first match
    imports_found: list[str] = field(default_factory=list)  # matched import stmts or code snippets
    explanation: str = ""
    fix_hint: str = ""
    migration_target: str = ""
    openrewrite_recipe: Optional[str] = None

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
            "migration_target": self.migration_target,
        }
        if self.imports_found:
            d["imports_found"] = self.imports_found
        if self.openrewrite_recipe:
            d["openrewrite_recipe"] = self.openrewrite_recipe
        return d


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

@dataclass
class MigrationReport:
    schema_version: str = "1.1"
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
        by_target: dict[str, int] = {}
        affected_files: set[str] = set()

        for f in self.findings:
            by_severity[f.severity] = by_severity.get(f.severity, 0) + 1
            by_rule[f.rule_id] = by_rule.get(f.rule_id, 0) + 1
            by_target[f.migration_target] = by_target.get(f.migration_target, 0) + 1
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
            "by_migration_target": by_target,
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
                f"  [{f.migration_target}]"
            )
            lines.append(f"  {f.title}")
            lines.append(f"  Fix: {f.fix_hint}")
            if f.openrewrite_recipe:
                lines.append(f"  OpenRewrite: {f.openrewrite_recipe}")
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
        # An import_pattern and code_pattern can coexist on the same rule (OR semantics).
        # A finding is created if EITHER matches; we report the earliest match position.
        matched_imports: list[str] = []
        import_first_line: Optional[int] = None
        code_first_line: Optional[int] = None
        code_snippets: list[str] = []

        if rule.import_pattern is not None:
            matches = list(rule.import_pattern.finditer(source))
            if matches:
                import_first_line = source[: matches[0].start()].count("\n") + 1
                matched_imports = [m.group(1) for m in matches]

        if rule.code_pattern is not None:
            m = rule.code_pattern.search(source)
            if m is not None:
                code_first_line = source[: m.start()].count("\n") + 1
                code_snippets = [m.group(0).strip()]

        # extends_pattern is a legacy form of code_pattern
        extends_first_line: Optional[int] = None
        if rule.extends_pattern is not None:
            m = rule.extends_pattern.search(source)
            if m is not None:
                extends_first_line = source[: m.start()].count("\n") + 1

        # Determine overall match
        candidate_lines = [
            ln for ln in (import_first_line, code_first_line, extends_first_line)
            if ln is not None
        ]
        if not candidate_lines:
            continue

        first_line = min(candidate_lines)
        all_matches = matched_imports + code_snippets

        findings.append(
            MigrationFinding(
                id=MigrationFinding.make_id(rule.id, rel_path),
                rule_id=rule.id,
                severity=rule.severity,
                title=rule.title,
                source_file=rel_path,
                first_line=first_line,
                imports_found=all_matches,
                explanation=rule.explanation,
                fix_hint=rule.fix_hint,
                migration_target=rule.migration_target,
                openrewrite_recipe=rule.openrewrite_recipe,
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
    """Scan Java files for migration blockers (Spring Boot 2→3, Java 8→17/21).

    Args:
        file_paths:   Relative Java file paths (from find_java_files).
        root:         Absolute repo root.
        min_severity: Filter threshold — findings below this severity are excluded
                      from the report. Choices: critical | high | medium | low.

    Returns:
        MigrationReport with findings, readiness_score, effort estimate, and
        migration_target breakdown.
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

    limitations.extend(_STATIC_LIMITATIONS)

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


# Items that static analysis cannot determine, always emitted as limitations
_STATIC_LIMITATIONS: list[str] = [
    "Thread.stop/suspend/resume deprecation: cannot reliably detect without type resolution "
    "(requires knowing that a variable is typed as java.lang.Thread).",
    "CORBA removal (Java 11): org.omg.* usage not scanned; add manually if project uses CORBA.",
    "Module compatibility (JPMS): --add-opens requirements cannot be determined without "
    "running the application against the target JDK.",
    "Transitive dependency compatibility: library versions (Hibernate, Jackson, etc.) must be "
    "verified separately against Spring Boot 3 BOM.",
    "XML-based Spring config (applicationContext.xml, web.xml): not scanned — bean class names "
    "and servlet filter chains in XML may reference javax.* classes.",
    "Runtime proxy behaviour (CGLIB/ByteBuddy subclass proxies): compatibility with Java 17+ "
    "strong encapsulation depends on framework version, not detectable via import scanning.",
]


def _detect_spring_boot_2(root: Path) -> bool:
    """Return True if any pom.xml or build.gradle declares spring-boot 2.x."""
    _SB2 = re.compile(
        r"(?:spring[.\-]boot[.\-]?(?:version|starter|parent)[^=\n]*[=:\s>\"']?\s*)"
        r"2\.\d+[\.\d]*|"
        r"<version>\s*2\.\d+[\.\d]*\s*</version>.*spring.boot|"
        r"spring.boot.*<version>\s*2\.\d+",
        re.IGNORECASE | re.DOTALL,
    )
    for name in ("pom.xml", "build.gradle", "build.gradle.kts"):
        candidate = root / name
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
            if _SB2.search(text):
                return True
        except OSError:
            pass
    return False

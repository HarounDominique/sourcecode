"""migrate_check.py — Java 8/Spring Boot 2 migration readiness checker.

Scans Java source files, Spring XML config, and build descriptors for patterns
that must be addressed when migrating:
  - Spring Boot 2 → 3 (javax → jakarta, Spring Security 6)
  - Java 8 → 17 / 21 (SecurityManager, Nashorn, Unsafe, reflection, etc.)
  - XML Spring config (applicationContext.xml, web.xml, security XML)
  - Dependency incompatibilities (SpringFox, Hibernate 5, ByteBuddy old)
  - Hibernate 5→6 stratified migration model (see hibernate_strat) — 4 independent
    layers, module exposure map, call-chain detection, upgrade-vs-rewrite verdict

Entry point: run_migrate_check(file_paths, root) → MigrationReport
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from sourcecode.path_filters import is_test_or_fixture_path
from sourcecode.jdk_exports import JDK_UNCONDITIONAL_EXPORTS

if TYPE_CHECKING:
    from sourcecode.hibernate_strat import HibernateStratification


# ---------------------------------------------------------------------------
# Rule catalogue — Java source rules
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Rule:
    id: str
    severity: str
    title: str
    explanation: str
    fix_hint: str
    migration_target: str = "spring_boot_3"
    openrewrite_recipe: Optional[str] = None
    import_pattern: Optional[re.Pattern] = None
    extends_pattern: Optional[re.Pattern] = None
    code_pattern: Optional[re.Pattern] = None


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
# Java 11 — APIs removed from the JDK
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
    _Rule(
        id="MIG-023",
        severity="critical",
        title="CORBA APIs (org.omg.* / javax.rmi.*) — removed from JDK in Java 11",
        explanation=(
            "The CORBA APIs (org.omg.* and javax.rmi.CORBA / javax.rmi.ssl) were deprecated "
            "in Java 9 (JEP 289) and removed from the JDK in Java 11 (JEP 320). Applications "
            "importing these packages will fail to compile or run on Java 11+ unless the "
            "'org.glassfish.corba:glassfish-corba-omgapi' artifact is added explicitly."
        ),
        fix_hint=(
            "Remove CORBA usage where possible — CORBA is effectively dead technology. "
            "If CORBA interop is unavoidable, add 'org.glassfish.corba:glassfish-corba-omgapi' "
            "as an explicit Maven/Gradle dependency."
        ),
        migration_target="java_11",
        openrewrite_recipe=None,
        import_pattern=re.compile(
            r"^[ \t]*import\s+(org\.omg\.[^;]+|javax\.rmi\.CORBA\.[^;]+|javax\.rmi\.ssl\.[^;]+);",
            re.MULTILINE,
        ),
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
# Java 17 — SecurityManager removed (JEP 411), Thread deprecated methods
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
            r"\bSecurityManager\s+\w+\s*[=;({]|"
            r"\bnew\s+SecurityManager\s*\(|"
            r"\bextends\s+SecurityManager\b|"
            r"\bAccessController\.(doPrivileged|checkPermission|getContext)\s*\(",
        ),
    ),
    _Rule(
        id="MIG-024",
        severity="medium",
        title="Thread.stop / Thread.suspend / Thread.resume — deprecated for removal (Java 17+)",
        explanation=(
            "Thread.stop(), Thread.suspend(), and Thread.resume() are deprecated since Java 1.2 "
            "and deprecated-for-removal since Java 17 (JEP 411 scope). Thread.stop() is "
            "inherently unsafe — it throws ThreadDeath which can corrupt object state. "
            "Thread.suspend/resume cause deadlocks when the suspended thread holds a monitor. "
            "Note: detection is best-effort; confirm the variable type is java.lang.Thread."
        ),
        fix_hint=(
            "Use Thread.interrupt() with InterruptedException for cooperative cancellation. "
            "Replace suspend/resume patterns with wait()/notify(), Semaphore, or a higher-level "
            "concurrency abstraction (BlockingQueue, CountDownLatch, etc.)."
        ),
        migration_target="java_17",
        openrewrite_recipe=None,
        code_pattern=re.compile(
            r"\b(?:thread|[a-zA-Z]\w*[Tt]hread)\.(stop|suspend|resume)\s*\("
            r"|\bnew\s+Thread\s*\([^)]{0,120}\)\s*\.(stop|suspend|resume)\s*\("
            r"|\bThread\.currentThread\s*\(\)\s*\.(stop|suspend|resume)\s*\(",
            re.MULTILINE,
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
            "Imports from sun.* and com.sun.* (tools/jdi/source internals) reference "
            "JDK-internal APIs that are not part of the public specification. Since Java 9 "
            "(JPMS), these packages are strongly encapsulated and require --add-exports / "
            "--add-opens JVM flags, which are cumbersome and may be removed in future Java "
            "releases. Packages the JDK exports UNCONDITIONALLY (e.g. com.sun.net.httpserver "
            "in jdk.httpserver, com.sun.management in jdk.management) are NOT flagged: they "
            "need no JVM flags on any classpath or module path."
        ),
        fix_hint=(
            "Replace internal API usage with public equivalents. "
            "For sun.misc.Unsafe migrate to java.lang.invoke.VarHandle; for com.sun.tools.* "
            "use the public javax.tools / java.compiler API. "
            "Add '--add-exports java.base/sun.misc=ALL-UNNAMED' only as a last resort. "
            "Note: unconditionally-exported packages (com.sun.net.httpserver, "
            "com.sun.management, com.sun.security.auth, ...) are auto-excluded — they are "
            "public, stable, and require no migration."
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
    _Rule(
        id="MIG-025",
        severity="medium",
        title="ReflectionFactory / MethodHandles.privateLookupIn — deep-reflection JPMS risk",
        explanation=(
            "sun.reflect.ReflectionFactory bypasses module encapsulation and is not part of "
            "the public API. MethodHandles.privateLookupIn() grants private lookup access that "
            "requires --add-opens on Java 9+. Both patterns are common in serialization "
            "frameworks and mocking libraries and may break under strict JPMS modules."
        ),
        fix_hint=(
            "Replace sun.reflect.ReflectionFactory with MethodHandles.lookup() or VarHandle. "
            "For MethodHandles.privateLookupIn, ensure the calling module has been opened "
            "via 'opens <package> to <module>' in module-info.java."
        ),
        migration_target="java_9_plus",
        openrewrite_recipe=None,
        import_pattern=re.compile(
            r"^[ \t]*import\s+(sun\.reflect\.ReflectionFactory[^;]*);",
            re.MULTILINE,
        ),
        code_pattern=re.compile(
            r"\bReflectionFactory\s*\.\s*getReflectionFactory\s*\("
            r"|\bMethodHandles\s*\.\s*privateLookupIn\s*\(",
        ),
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
        openrewrite_recipe="org.openrewrite.java.migrate.RemoveFinalizeMethod",
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
            "Calendar with LocalDate or ZonedDateTime (java.time — no Calendar equivalent), "
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
# All Java source rules
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

# BUG #2 — javax.* packages that are JDK / permanent JSR namespaces and do NOT
# migrate to jakarta. A jakarta-migration rule matching `javax.transaction` or
# `javax.annotation` must NOT fire on these FQN prefixes (e.g. javax.transaction.xa
# is the Java SE java.transaction.xa module; javax.annotation.processing is the
# annotation-processing API). The allowlist is keyed on fully-qualified import
# prefixes, never on simple class names or partial tokens.
_JAKARTA_NO_MIGRATE_PREFIXES: tuple[str, ...] = (
    "javax.transaction.xa.",
    "javax.annotation.processing.",
    "javax.xml.parsers.",
    "javax.xml.transform.",
    "javax.xml.xpath.",
    "javax.xml.stream.",
    "javax.xml.datatype.",
    "javax.xml.namespace.",
    "javax.xml.validation.",
    "javax.xml.catalog.",
    "javax.xml.crypto.",
    "javax.sql.",
    "javax.management.",
    "javax.naming.",
    "javax.crypto.",
    "javax.net.",
    "javax.security.auth.",
    "javax.security.cert.",
    "javax.security.sasl.",
    "javax.cache.",
    "javax.tools.",
    "javax.imageio.",
    "javax.sound.",
    "javax.print.",
    "javax.accessibility.",
    "javax.swing.",
    "javax.lang.model.",
)


def _is_no_migrate_javax(fqn: str) -> bool:
    """True if a javax FQN belongs to a JDK/permanent namespace (no jakarta move)."""
    return any(fqn.startswith(p) for p in _JAKARTA_NO_MIGRATE_PREFIXES)


# BUG #1 (JobRunr field test): MIG-011 flags `sun.*` / `com.sun.*` imports as
# strongly-encapsulated JDK internals on a pure PREFIX heuristic. That is wrong
# for packages the JDK exports UNCONDITIONALLY (no `to` clause) — e.g.
# `com.sun.net.httpserver` (module jdk.httpserver, public since Java 6, the basis
# of JEP 408) or `com.sun.management` (jdk.management, JMX/diagnostics). These
# need NO `--add-exports` / `--add-opens` on any classpath or module path, so they
# must not be flagged `high` / `manual_migration`. The allowlist is generated from
# the running JDK by scripts/generate_jdk_exports.py (see sourcecode/jdk_exports.py),
# never hand-maintained. Genuinely-internal packages (sun.misc.Unsafe, com.sun.tools.*,
# com.sun.jdi.*, com.sun.source.*) are NOT in the allowlist and keep `high` severity.


def _import_package(fqn: str) -> str:
    """Extract the Java package of an import FQN.

    Packages are lowercase by convention and types are Capitalized, so the
    package is the maximal prefix of non-type segments. Handles wildcard
    (`a.b.*`) and `static` imports. Conservative: an unrecognized shape yields
    the leading lowercase run, never a broader prefix — so a sub-package like
    `com.sun.management.internal` is never confused with `com.sun.management`.
    """
    fqn = fqn.strip().rstrip(";").strip()
    if fqn.startswith("static "):
        fqn = fqn[len("static "):].strip()
    if fqn.endswith(".*"):
        return fqn[:-2]
    pkg_parts: list[str] = []
    for seg in fqn.split("."):
        if seg[:1].isupper():  # first type segment — package ends here
            break
        pkg_parts.append(seg)
    return ".".join(pkg_parts)


def _is_jdk_unconditional_export(fqn: str) -> bool:
    """True if an import targets a package the JDK exports unconditionally."""
    return _import_package(fqn) in JDK_UNCONDITIONAL_EXPORTS


# BUG #8: autogenerated source markers — path fragments and the JSR-250 marker.
_GENERATED_PATH_FRAGMENTS: tuple[str, ...] = (
    "/generated-sources/", "/generated/", "/target/generated",
    "/build/generated", "/.apt_generated/",
)


def _classify_code_context(finding: "MigrationFinding") -> str:
    """Bucket a finding as main / test / generated for blocking-count segregation."""
    path = finding.source_file
    if is_test_or_fixture_path(path):
        return "test"
    norm = path.replace("\\", "/").lower()
    if any(frag in norm for frag in _GENERATED_PATH_FRAGMENTS):
        return "generated"
    # javax.annotation.Generated is the marker emitted into autogenerated code.
    if finding.rule_id == "MIG-006" and finding.imports_found and all(
        imp.rsplit(".", 1)[-1] == "Generated" for imp in finding.imports_found
    ):
        return "generated"
    return "main"


# BUG #1/#2: Spring presence must be SCOPE- and ARTIFACT-aware. The Boot 2→3 axis
# only applies to repos that use Spring AT RUNTIME — a Quarkus/Micronaut/Jakarta-pure
# repo (or one like Apache OFBiz whose ONLY Spring coordinate is spring-test, a TEST
# support library declared under a legacy `compile` block) has no Spring Boot axis.
# We therefore split Spring usage into runtime vs test-only and NEVER let a test
# artifact poison spring_present (which gates the boot3 readiness dimension).

# Test-only Spring artifacts: their presence — even when declared in a compile/
# implementation block — never implies Spring at runtime. The ARTIFACT itself is a
# test library, regardless of the declared scope.
_SPRING_TEST_ARTIFACT_RE: re.Pattern = re.compile(
    r"\bspring-(?:test|boot-test(?:-autoconfigure)?|boot-starter-test|security-test)\b",
    re.IGNORECASE,
)
# Any `spring-<artifact>` coordinate token (matches both Gradle `org.springframework:
# spring-core:…` strings and Maven `<artifactId>spring-core</artifactId>` text).
_SPRING_ARTIFACT_TOKEN_RE: re.Pattern = re.compile(
    r"\bspring-[a-z][a-z0-9]*(?:-[a-z0-9]+)*\b", re.IGNORECASE
)
# Spring Boot Gradle plugin / BOM group — an unambiguous runtime signal.
_SPRING_BOOT_PLUGIN_RE: re.Pattern = re.compile(
    r"""(?:id\s*['"]\s*org\.springframework\.boot|"""
    r"""org\.springframework\.boot\s*[:'"])""",
    re.IGNORECASE,
)
# Import-side split: a Spring import in a TEST package (org.springframework.*.test
# or org.springframework.test / .boot.test) is a test-only signal; any other
# org.springframework import in MAIN sources is a runtime signal.
_SPRING_IMPORT_RE: re.Pattern = re.compile(
    r"^[ \t]*import\s+(?:static\s+)?org\.springframework\.([\w.]+)", re.MULTILINE
)


def _build_text_spring_signals(text: str) -> tuple[bool, bool]:
    """(runtime, any_spring) for one build-file's text — artifact/scope aware.

    A `spring-*` artifact token that is not a test library, or the Spring Boot
    plugin/BOM group, counts as runtime. spring-test (and friends) alone count as
    "spring present but test-only" — never runtime.
    """
    runtime = False
    any_spring = False
    if _SPRING_BOOT_PLUGIN_RE.search(text):
        runtime = True
        any_spring = True
    for m in _SPRING_ARTIFACT_TOKEN_RE.finditer(text):
        any_spring = True
        if not _SPRING_TEST_ARTIFACT_RE.match(m.group(0)):
            runtime = True
    return runtime, any_spring


def _detect_spring_usage(
    root: Path, runtime_import_seen: bool, test_import_seen: bool
) -> tuple[bool, bool]:
    """Return (runtime_present, test_only).

    runtime_present  — Spring is on the runtime/compile path (build coordinate or a
                       MAIN-source org.springframework import that is not a test pkg).
    test_only        — Spring appears ONLY as a test dependency (e.g. spring-test);
                       the Boot 2→3 migration axis is N/A.
    """
    runtime = runtime_import_seen
    any_spring = runtime_import_seen or test_import_seen
    for abs_path, _rel in _find_build_files(root):
        try:
            text = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        r, s = _build_text_spring_signals(text)
        runtime = runtime or r
        any_spring = any_spring or s
    return runtime, (any_spring and not runtime)

# G-1: cap on total readiness deduction from low-severity (advisory, non-blocking)
# findings, so optional modernization cleanups cannot collapse the migration-readiness
# headline on a repo with zero blockers. See MigrationReport.finalize.
_LOW_SEVERITY_DEDUCTION_CAP: int = 15

# Migration targets that block a Spring Boot 2→3 upgrade (namespace + security).
# Everything else (java_8_best_practice, java_9_plus, java_11/15/17/18+) is
# orthogonal JDK modernization debt and must not sink the migration headline.
_JAKARTA_TARGETS: frozenset[str] = frozenset({"jakarta"})
_BOOT3_MIGRATION_TARGETS: frozenset[str] = frozenset(
    {"jakarta", "spring_boot_3", "spring_security_6"}
)
# BUG #6: best-practice hygiene (java.util.Date → java.time) blocks NO version
# upgrade. It is advisory only and must never sink a readiness dimension to 0 —
# reported as a separate hygiene metric, excluded from JDK-modernization scoring.
_BEST_PRACTICE_TARGETS: frozenset[str] = frozenset({"java_8_best_practice"})

# BUG #3: the migration dimensions that feed the readiness_score aggregate, in
# order. jdk_modernization is deliberately NOT here — it is orthogonal upkeep debt
# reported on its own axis, never folded into the migration headline.
_MIGRATION_DIMENSIONS: tuple[str, ...] = ("jakarta", "boot3", "hibernate")


def _parse_major(version: "Optional[str]") -> "Optional[int]":
    """Leading integer of a version string ('4.0.3' → 4); None when not parseable."""
    if not version:
        return None
    m = re.match(r"\s*(\d+)", version)
    return int(m.group(1)) if m else None
# Cap on total readiness deduction from JDK-modernization findings (medium/low),
# so reflection/date cleanups cannot collapse a jakarta-ready repo's headline.
_JDK_ADVISORY_DEDUCTION_CAP: int = 15

# Jakarta EE 9+ namespace imports — strong evidence the repo is already on
# Boot 3 / Jakarta. Used to veto a false spring_boot_2_detected verdict.
_JAKARTA_IMPORT_RE: re.Pattern = re.compile(
    r"^[ \t]*import\s+jakarta\.(?:persistence|servlet|validation|annotation|"
    r"transaction|inject|ws\.rs|jms|ejb|mail|websocket|faces|enterprise|batch|"
    r"json|el|security)\b",
    re.MULTILINE,
)


# ---------------------------------------------------------------------------
# XML config rules (applied to Spring XML config files)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _XmlRule:
    id: str
    severity: str
    title: str
    explanation: str
    fix_hint: str
    migration_target: str
    openrewrite_recipe: Optional[str] = None
    pattern: Optional[re.Pattern] = None


_XML_RULES: list[_XmlRule] = [
    _XmlRule(
        id="MIG-030",
        severity="high",
        title="javax.* class reference in Spring XML config — namespace not migrated",
        explanation=(
            "Spring XML bean definitions using class='javax.*' reference the old Java EE "
            "namespace. When the application migrates to Spring Boot 3 / Jakarta EE 9+, these "
            "bean class names must be updated to use the jakarta.* namespace equivalents. "
            "Typical occurrences: persistence providers, validators, transaction managers."
        ),
        fix_hint=(
            "Update class='javax.*' attributes in XML bean definitions to the corresponding "
            "jakarta.* class names. Run OpenRewrite or grep for 'javax.' in all XML config files."
        ),
        migration_target="jakarta",
        openrewrite_recipe=None,
        pattern=re.compile(
            r'(?:class|type|value)\s*=\s*["\'][^"\']*\bjavax\.[a-zA-Z]',
            re.MULTILINE,
        ),
    ),
    _XmlRule(
        id="MIG-031",
        severity="high",
        title="Spring Security XML — old-style <http auto-config> or versioned schema ≤5",
        explanation=(
            "XML-based Spring Security configuration using <http auto-config='true'> or "
            "pointing to a spring-security-[3-5].x.xsd schema requires significant migration "
            "for Spring Security 6 (Spring Boot 3). The auto-config shortcut and many XML "
            "namespace attributes were changed or removed in Spring Security 6."
        ),
        fix_hint=(
            "Migrate XML security config to Java-based @Configuration with SecurityFilterChain "
            "@Bean. See the Spring Security 6 XML migration guide. "
            "Update schema references to spring-security.xsd (no version) or use Spring Security 6 schemas."
        ),
        migration_target="spring_security_6",
        openrewrite_recipe="org.openrewrite.java.spring.security6.WebSecurityConfigurerAdapterToSecurityFilterChain",
        pattern=re.compile(
            r"<(?:\w+:)?http\s[^>]*auto-config\s*=\s*[\"']true[\"']"
            r"|spring-security-[2345]\.\d+\.xsd",
            re.IGNORECASE | re.MULTILINE,
        ),
    ),
    _XmlRule(
        id="MIG-032",
        severity="high",
        title="web.xml with Servlet ≤4 namespace — javax.servlet, must migrate to jakarta",
        explanation=(
            "A web.xml using the Java EE namespace (java.sun.com/xml/ns/javaee or "
            "xmlns.jcp.org/xml/ns/javaee) declares a Servlet 2.x/3.x/4.x deployment descriptor. "
            "These namespaces map to javax.servlet. Spring Boot 3 requires Jakarta Servlet 5.0+ "
            "(namespace: jakarta.ee/xml/ns/jakartaee). The deployment descriptor must be updated."
        ),
        fix_hint=(
            "Update web.xml namespace from 'http://xmlns.jcp.org/xml/ns/javaee' to "
            "'https://jakarta.ee/xml/ns/jakartaee' and set version='5.0' or '6.0'. "
            "Update all filter-class and servlet-class entries from javax.* to jakarta.* equivalents."
        ),
        migration_target="jakarta",
        openrewrite_recipe=None,
        pattern=re.compile(
            r'xmlns\s*=\s*["\']https?://(?:java\.sun\.com|xmlns\.jcp\.org)/xml/ns/javaee["\']',
            re.IGNORECASE | re.MULTILINE,
        ),
    ),
]

# XML files to scan: name-based heuristic (avoids scanning unrelated XML like Maven reports)
_XML_FILE_GLOBS: tuple[str, ...] = (
    "web.xml",
    "applicationContext.xml",
    "applicationContext-*.xml",
    "*applicationContext*.xml",
    "*-context.xml",
    "*Context.xml",
    "*-config.xml",
    "*Config.xml",
    "*security*.xml",
    "*Security*.xml",
    "*servlet*.xml",
    "*Servlet*.xml",
    "beans.xml",
    "*-beans.xml",
    "*spring*.xml",
    "*Spring*.xml",
    "*dispatcher*.xml",
    "*Dispatcher*.xml",
)

_SKIP_DIRS: frozenset[str] = frozenset([
    "target", "build", ".git", ".gradle", ".mvn",
    "node_modules", "__pycache__", ".idea", ".vscode",
    "out", "dist", "bin", "generated-sources",
])


def _is_spring_xml_candidate(fname: str) -> bool:
    return any(fnmatch.fnmatch(fname, g) for g in _XML_FILE_GLOBS)


def _find_xml_config_files(root: Path) -> list[tuple[Path, str]]:
    """Compatibility shim — calls the combined scanner."""
    xml_files, _ = _find_non_java_files(root)
    return xml_files


def _find_build_files(root: Path) -> list[tuple[Path, str]]:
    """Compatibility shim — calls the combined scanner."""
    _, build_files = _find_non_java_files(root)
    return build_files


def _find_non_java_files(
    root: Path,
) -> tuple[list[tuple[Path, str]], list[tuple[Path, str]]]:
    """Single os.walk returning (xml_config_files, build_files), excluding build dirs."""
    xml_files: list[tuple[Path, str]] = []
    build_files: list[tuple[Path, str]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        dp = Path(dirpath)
        try:
            rel_dir = dp.relative_to(root)
        except ValueError:
            continue
        rel_prefix = str(rel_dir) if str(rel_dir) != "." else ""
        for fname in filenames:
            rel = f"{rel_prefix}/{fname}" if rel_prefix else fname
            abs_path = dp / fname
            if fname.endswith(".xml"):
                if fname == "pom.xml":
                    build_files.append((abs_path, rel))
                elif _is_spring_xml_candidate(fname):
                    xml_files.append((abs_path, rel))
            elif fname in ("build.gradle", "build.gradle.kts"):
                build_files.append((abs_path, rel))
    return xml_files, build_files


def _scan_xml_file(text: str, rel_path: str) -> list["MigrationFinding"]:
    """Apply XML rules to raw XML text. Returns one finding per matched rule."""
    findings: list[MigrationFinding] = []
    for rule in _XML_RULES:
        if rule.pattern is None:
            continue
        matches = list(rule.pattern.finditer(text))
        if not matches:
            continue
        first_line = text[: matches[0].start()].count("\n") + 1
        snippets = [m.group(0)[:120].strip() for m in matches[:5]]
        findings.append(
            MigrationFinding(
                id=MigrationFinding.make_id(rule.id, rel_path),
                rule_id=rule.id,
                severity=rule.severity,
                title=rule.title,
                source_file=rel_path,
                first_line=first_line,
                imports_found=snippets,
                explanation=rule.explanation,
                fix_hint=rule.fix_hint,
                migration_target=rule.migration_target,
                openrewrite_recipe=rule.openrewrite_recipe,
            )
        )
    return findings


# ---------------------------------------------------------------------------
# Dependency rules (applied to pom.xml / build.gradle / build.gradle.kts)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _DepRule:
    id: str
    severity: str
    title: str
    explanation: str
    fix_hint: str
    migration_target: str
    openrewrite_recipe: Optional[str] = None
    # Patterns applied to raw build file text.
    # Each is tried independently; first match wins.
    maven_pattern: Optional[re.Pattern] = None
    gradle_pattern: Optional[re.Pattern] = None
    # Optional fast pre-check: skip expensive regex if this string is absent.
    quick_filter: Optional[str] = None


_DEP_RULES: list[_DepRule] = [
    _DepRule(
        id="MIG-040",
        severity="high",
        title="SpringFox (io.springfox) — incompatible with Spring Boot 3 / Spring Framework 6",
        explanation=(
            "SpringFox relies on Spring MVC internal request mapping infrastructure that was "
            "removed in Spring Framework 6. Applications declaring io.springfox:springfox-* "
            "dependencies will fail to start after migration to Spring Boot 3, even if the "
            "Java source code compiles cleanly."
        ),
        fix_hint=(
            "Replace springfox-swagger2 + springfox-swagger-ui with "
            "springdoc-openapi-starter-webmvc-ui (OpenAPI 3). "
            "Also remove @EnableSwagger2 and any SpringFox Docket configuration beans."
        ),
        migration_target="spring_boot_3",
        openrewrite_recipe=None,
        maven_pattern=re.compile(r"\bio\.springfox\b", re.IGNORECASE),
        gradle_pattern=re.compile(r"\bio\.springfox\b", re.IGNORECASE),
    ),
    _DepRule(
        id="MIG-041",
        severity="high",
        title="Hibernate 5.x explicitly pinned — Spring Boot 3 requires Hibernate 6",
        explanation=(
            "Spring Boot 3 ships with Hibernate 6.x as the JPA provider, which implements "
            "Jakarta Persistence 3.0. An explicit <version>5.*</version> for hibernate-core "
            "overrides the Spring Boot BOM and will cause runtime incompatibilities: Hibernate 5 "
            "implements javax.persistence (not jakarta.persistence)."
        ),
        fix_hint=(
            "Remove the explicit Hibernate version override and let the Spring Boot 3 BOM "
            "manage it (Hibernate 6.x). Review breaking API changes between Hibernate 5 and 6 "
            "in the Hibernate 6 migration guide."
        ),
        migration_target="jakarta",
        openrewrite_recipe=None,
        maven_pattern=re.compile(
            r"<dependency>(?:(?!</dependency>).)*?hibernate-core(?![-\w])(?:(?!</dependency>).)*?"
            r"<version>\s*5\.",
            re.DOTALL | re.IGNORECASE,
        ),
        gradle_pattern=re.compile(
            r"""['"](org\.hibernate(?:\.orm)?):hibernate-core:5\.""",
            re.IGNORECASE,
        ),
        quick_filter="hibernate-core",
    ),
    _DepRule(
        id="MIG-042",
        severity="medium",
        title="ByteBuddy < 1.12.x — may not support Java 17+ strong encapsulation",
        explanation=(
            "ByteBuddy versions before 1.12 lack stable support for Java 17+ strong JPMS "
            "encapsulation. Spring AOP, Mockito, and Hibernate proxies all depend on ByteBuddy "
            "internally. If an application pins byte-buddy at 1.0–1.11.x, proxy creation "
            "may fail with InaccessibleObjectException on Java 17+."
        ),
        fix_hint=(
            "Remove explicit ByteBuddy version overrides and let Spring Boot 3 BOM manage it "
            "(ships with 1.14.x+). If you must pin it, use >= 1.12.18."
        ),
        migration_target="java_17",
        openrewrite_recipe=None,
        maven_pattern=re.compile(
            r"<dependency>(?:(?!</dependency>).)*?byte-buddy(?:(?!</dependency>).)*?"
            r"<version>\s*1\.(?:[0-9]|1[01])\.",
            re.DOTALL | re.IGNORECASE,
        ),
        gradle_pattern=re.compile(
            r"""['"](net\.bytebuddy):byte-buddy:1\.(?:[0-9]|1[01])\.""",
            re.IGNORECASE,
        ),
        quick_filter="byte-buddy",
    ),
    _DepRule(
        id="MIG-043",
        severity="high",
        title="EhCache 2.x — incompatible with Spring Boot 3 / JCache JSR-107 migration",
        explanation=(
            "EhCache 2.x (net.sf.ehcache) uses the old JSR-107 cache API and is not compatible "
            "with the Spring Boot 3 cache abstraction. Spring Boot 3 requires EhCache 3.x "
            "(org.ehcache) which implements JCache 1.1 and uses a different configuration format."
        ),
        fix_hint=(
            "Migrate from net.sf.ehcache:ehcache to org.ehcache:ehcache:3.x. "
            "Update ehcache.xml configuration to the EhCache 3 XML format. "
            "Add the 'org.ehcache:ehcache::jakarta' classifier for Jakarta EE compatibility."
        ),
        migration_target="spring_boot_3",
        openrewrite_recipe=None,
        maven_pattern=re.compile(
            r"<groupId>\s*net\.sf\.ehcache\s*</groupId>",
            re.IGNORECASE,
        ),
        gradle_pattern=re.compile(
            r"""['"](net\.sf\.ehcache):[^'"]+""",
            re.IGNORECASE,
        ),
    ),
]

_BUILD_FILE_NAMES: tuple[str, ...] = ("pom.xml", "build.gradle", "build.gradle.kts")


def _find_build_files(root: Path) -> list[tuple[Path, str]]:
    """Return (abs_path, rel_path) for pom.xml / build.gradle files, excluding build dirs."""
    results: list[tuple[Path, str]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        dp = Path(dirpath)
        try:
            rel_dir = dp.relative_to(root)
        except ValueError:
            continue
        for fname in filenames:
            if fname in _BUILD_FILE_NAMES:
                abs_path = dp / fname
                rel = str(rel_dir / fname) if str(rel_dir) != "." else fname
                results.append((abs_path, rel))
    return results


def _resolve_maven_properties(text: str) -> str:
    """Substitute ${prop} references with values from the <properties> block.

    Handles single-level property references that appear in the same pom.xml.
    Multi-level references (${a} where a=${b}) are resolved up to 3 passes.
    """
    props: dict[str, str] = {}
    for m in re.finditer(r'<([A-Za-z][\w.\-]*)>\s*([^<${}]+?)\s*</\1>', text):
        props[m.group(1)] = m.group(2).strip()
    if not props:
        return text

    resolved = text
    for _ in range(3):
        def _sub(m: re.Match) -> str:  # noqa: E306
            return props.get(m.group(1), m.group(0))
        resolved_new = re.sub(r'\$\{([\w.\-]+)\}', _sub, resolved)
        if resolved_new == resolved:
            break
        resolved = resolved_new
    return resolved


def _scan_dep_file(text: str, rel_path: str) -> list["MigrationFinding"]:
    """Apply dependency rules to a build file. Returns one finding per matched rule."""
    is_gradle = rel_path.endswith((".gradle", ".gradle.kts"))
    if not is_gradle and rel_path.endswith(".xml"):
        text = _resolve_maven_properties(text)
    findings: list[MigrationFinding] = []
    for rule in _DEP_RULES:
        if rule.quick_filter is not None and rule.quick_filter not in text:
            continue
        pattern = rule.gradle_pattern if is_gradle else rule.maven_pattern
        if pattern is None:
            continue
        m = pattern.search(text)
        if m is None:
            continue
        first_line = text[: m.start()].count("\n") + 1
        findings.append(
            MigrationFinding(
                id=MigrationFinding.make_id(rule.id, rel_path),
                rule_id=rule.id,
                severity=rule.severity,
                title=rule.title,
                source_file=rel_path,
                first_line=first_line,
                imports_found=[m.group(0)[:120].strip()],
                explanation=rule.explanation,
                fix_hint=rule.fix_hint,
                migration_target=rule.migration_target,
                openrewrite_recipe=rule.openrewrite_recipe,
            )
        )
    return findings


# ---------------------------------------------------------------------------
# Finding
# ---------------------------------------------------------------------------

@dataclass
class MigrationFinding:
    id: str
    rule_id: str
    severity: str
    title: str
    source_file: str
    first_line: int
    imports_found: list[str] = field(default_factory=list)
    explanation: str = ""
    fix_hint: str = ""
    migration_target: str = ""
    openrewrite_recipe: Optional[str] = None
    # BUG #8: where the finding lives — "main" (product), "test" (test/fixture
    # harness), or "generated" (autogenerated source). Only "main" counts toward
    # blocking_count / readiness; test+generated are reported in a separate bucket.
    code_context: str = "main"

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
            "auto_fix_available": bool(self.openrewrite_recipe),
            "code_context": self.code_context,
        }
        if self.imports_found:
            d["imports_found"] = self.imports_found
        if self.openrewrite_recipe:
            d["openrewrite_recipe"] = self.openrewrite_recipe
        else:
            d["manual_migration"] = True
        return d


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _dimension_score(
    findings: list["MigrationFinding"],
    targets: "Optional[frozenset[str]]",
) -> int:
    """0-100 readiness for one migration dimension.

    targets=None scores the JDK-modernization dimension (every target NOT in the
    Boot 2→3 migration set). Otherwise scores only findings whose migration_target
    is in `targets`. Severity-weighted by distinct file (low capped, G-1).
    """
    crit: set[str] = set()
    high: set[str] = set()
    med: set[str] = set()
    low: set[str] = set()
    for f in findings:
        if targets is None:
            # JDK-modernization axis: everything NOT a Boot 2→3 blocker, EXCEPT
            # best-practice hygiene (BUG #6) which is advisory and scored separately.
            if f.migration_target in _BOOT3_MIGRATION_TARGETS:
                continue
            if f.migration_target in _BEST_PRACTICE_TARGETS:
                continue
        elif f.migration_target not in targets:
            continue
        if f.severity == "critical":
            crit.add(f.source_file)
        elif f.severity == "high":
            high.add(f.source_file)
        elif f.severity == "medium":
            med.add(f.source_file)
        else:
            low.add(f.source_file)
    deduction = (
        len(crit) * 15
        + len(high) * 8
        + len(med) * 3
        + min(len(low) * 1, _LOW_SEVERITY_DEDUCTION_CAP)
    )
    return max(0, 100 - deduction)


@dataclass
class MigrationReport:
    schema_version: str = "1.4"
    generated_at: str = ""
    repo_id: str = ""
    git_head: str = ""

    # Optional[int]: None == N/A (no applicable migration dimension), never a
    # manufactured 100 on a repo with nothing to migrate.
    readiness_score: Optional[int] = 100
    # Per-dimension readiness (0-100). javax→jakarta namespace, full Boot 2→3
    # migration, and orthogonal JDK modernization are scored independently so
    # JDK debt (java.util.Date, reflection) does not sink a jakarta-ready repo.
    jakarta_readiness: int = 100
    boot3_readiness: int = 100
    jdk_modernization: int = 100
    # 4th dimension: Hibernate 5→6 rewrite readiness (independent of jakarta/Boot3).
    # 100 when no Hibernate usage; sinks toward 0 in a rewrite zone.
    hibernate_readiness: int = 100
    # Names the dominant blocker class when one dimension dwarfs the headline
    # score (e.g. "hibernate_rewrite") so a reader of readiness_score is not misled.
    headline_blocker: Optional[str] = None
    # BUG #3: which readiness dimensions actually APPLY to this repo. A dimension
    # that does not apply (e.g. hibernate on a repo with no Hibernate) is N/A and
    # is excluded from the aggregate — it is never counted as 0. Maps dimension →
    # {"applicable": bool, "score": int|None, "reason": str}.
    applicable_dimensions: dict = field(default_factory=dict)
    # BUG #3: how readiness_score was derived (method + the exact applicable
    # dimension scores it aggregates) so the headline number is fully traceable.
    readiness_aggregate: dict = field(default_factory=dict)
    blocking_count: int = 0
    estimated_effort_days: float = 0.0
    # Tri-state: True = Boot 2 confirmed, False = Boot 3+ confirmed,
    # None = could not determine. Absence of evidence is never reported as True.
    spring_boot_2_detected: Optional[bool] = None
    spring_boot_version_detected: Optional[str] = None
    # BUG #2: whether Spring is used AT RUNTIME. The Boot3 dimension is N/A without it.
    spring_present: bool = True
    # BUG #1/#2: Spring appears ONLY as a test dependency (e.g. spring-test). Reported
    # so a consumer can see WHY boot3 is N/A despite an org.springframework coordinate.
    spring_test_only: bool = False
    # BUG #3/#4: the repo imports the jakarta.* namespace (already on Jakarta EE 9+).
    # Evidence that the namespace axis is RELEVANT (and complete) — distinguishes an
    # already-migrated Jakarta repo (jakarta applicable, 100) from a repo that was
    # never Java EE at all (jakarta N/A).
    jakarta_namespace_adopted: bool = False
    # BUG #6 / #8: findings that do NOT count toward blocking_count or readiness —
    # best-practice hygiene (java.util.Date…) and test/fixture/generated buckets,
    # surfaced separately so the headline reflects real product migration risk.
    hygiene_findings: int = 0
    non_blocking: dict = field(default_factory=dict)

    findings: list[MigrationFinding] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)
    # Hibernate 5→6 stratified migration model (4 independent layers, module
    # exposure map, call-chain detection, upgrade-vs-rewrite verdict). Attached
    # by run_migrate_check; None when not computed.
    hibernate: Optional["HibernateStratification"] = None

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

        # BUG #8: only product ("main") code counts toward blocking_count, the
        # readiness dimensions, and effort. Test harnesses/fixtures (deprecated
        # web.xml descriptors, test-only sun.* HTTP servers) and autogenerated
        # sources are reported in a separate non-blocking bucket — counting them
        # as product blockers inflates the verdict.
        main_findings = [f for f in self.findings if f.code_context == "main"]

        def _files_by_sev(items: list["MigrationFinding"], sev: str,
                          targets: "Optional[frozenset[str]]" = None,
                          exclude: "Optional[frozenset[str]]" = None) -> set[str]:
            out: set[str] = set()
            for f in items:
                if f.severity != sev:
                    continue
                if targets is not None and f.migration_target not in targets:
                    continue
                if exclude is not None and f.migration_target in exclude:
                    continue
                out.add(f.source_file)
            return out

        critical_files = _files_by_sev(main_findings, "critical")
        high_files = _files_by_sev(main_findings, "high")
        medium_files = _files_by_sev(main_findings, "medium")
        low_files = _files_by_sev(main_findings, "low")

        self.blocking_count = sum(
            1 for f in main_findings if f.severity in ("critical", "high")
        )

        # Per-dimension readiness — independent severity-weighted scores (MAIN only).
        self.jakarta_readiness = _dimension_score(main_findings, _JAKARTA_TARGETS)
        self.boot3_readiness = _dimension_score(main_findings, _BOOT3_MIGRATION_TARGETS)
        self.jdk_modernization = _dimension_score(main_findings, None)
        if not self.spring_present:
            self.boot3_readiness = 100

        # BUG #3/#4: the jakarta dimension is applicable ONLY with POSITIVE evidence the
        # namespace axis is relevant — either migratable javax.* code (a javax→jakarta
        # finding, axis in-progress) OR the jakarta.* namespace already adopted (axis
        # complete → 100). A repo with neither was never Java EE: jakarta is N/A, never a
        # manufactured 100 folded into the headline.
        _jakarta_applies = (
            any(f.migration_target in _JAKARTA_TARGETS for f in main_findings)
            or self.jakarta_namespace_adopted
        )

        # ── BUG #2: Hibernate applicability — version-driven, never heuristic ────
        # The 5→6 axis is applicable ONLY when the repo is actually on Hibernate < 6.
        #   resolved < 6        → applicable (rewrite score is a measurement)
        #   resolved ≥ 6        → N/A (already migrated)
        #   unresolved, Boot≥3  → N/A (Boot 3/4 BOM manages Hibernate ORM ≥6)
        #   unresolved, Boot==2 → applicable (Boot 2 BOM manages Hibernate 5.x)
        #   unresolved, no BOM  → N/A, status "unresolved" (NEVER a heuristic number
        #                          that looks like a measurement on absent data)
        hib = self.hibernate
        hib_detected = hib is not None and hib.detected
        boot_major = _parse_major(self.spring_boot_version_detected)
        _hibernate_applies = False
        _hib_score: Optional[int] = None
        hib_status = "not_detected"
        _hib_reason = "N/A — no Hibernate dependency or import detected"
        if hib_detected:
            if hib.version_confidence == "high":
                if hib.migration_applicable:              # resolved major < 6
                    _hibernate_applies, _hib_score = True, hib.readiness
                    hib_status = "resolved_h5"
                    _hib_reason = f"Hibernate 5→6 rewrite axis (resolved {hib.effective_version})"
                else:                                     # resolved major ≥ 6
                    hib_status = "managed_ge6"
                    _hib_reason = (f"N/A — resolved Hibernate {hib.effective_version} (≥6); "
                                   f"no 5→6 migration pending")
            elif boot_major is not None and boot_major >= 3:
                hib_status = "managed_ge6"
                _hib_reason = (f"N/A — Hibernate version managed by Spring Boot "
                               f"{self.spring_boot_version_detected} BOM (Hibernate ORM ≥6); "
                               f"5→6 axis inapplicable")
            elif boot_major == 2:
                _hibernate_applies, _hib_score = True, hib.readiness
                hib_status = "managed_h5"
                _hib_reason = (f"Hibernate 5→6 rewrite axis (Hibernate 5.x managed by Spring "
                               f"Boot {self.spring_boot_version_detected} BOM)")
            else:
                hib_status = "unresolved"
                _hib_reason = ("N/A — Hibernate version unresolved (not declared and no Spring "
                               "Boot BOM to infer from); not penalized on absent data")
        # Top-level scalar: the rewrite score only when applicable; else 100 (nothing
        # pending) — never a heuristic penalty on an inapplicable/unresolved axis.
        self.hibernate_readiness = _hib_score if _hibernate_applies else 100
        # Headline blocker only on a DIRECTLY-resolved Hibernate-5 rewrite zone.
        if _hibernate_applies and hib is not None and hib.classification == "rewrite_zone" \
                and hib.version_confidence == "high":
            self.headline_blocker = "hibernate_rewrite"

        # BUG #3: declare which dimensions apply, then derive readiness_score as a
        # DOCUMENTED aggregate over the applicable MIGRATION dimensions only. jakarta
        # is always applicable; boot3 only with Spring; hibernate only for a real
        # Hibernate < 6. jdk_modernization is an orthogonal upkeep axis (SecurityManager,
        # reflection, java.time) — reported but EXCLUDED from the headline so JDK debt
        # cannot sink a framework-complete repo. N/A dimensions carry score=None and
        # never enter the aggregate.
        self.applicable_dimensions = {
            "jakarta": {
                "applicable": _jakarta_applies,
                "score": self.jakarta_readiness if _jakarta_applies else None,
                "reason": ("javax→jakarta namespace migration"
                           if _jakarta_applies
                           else "N/A — no migratable javax.* imports detected"),
            },
            "boot3": {
                "applicable": self.spring_present,
                "score": self.boot3_readiness if self.spring_present else None,
                "reason": ("Spring Boot 2→3 / Security 6 migration"
                           if self.spring_present
                           else ("N/A — Spring present only as a TEST dependency "
                                 "(spring-test); no runtime Spring"
                                 if self.spring_test_only
                                 else "N/A — no Spring usage detected (non-Spring stack)")),
            },
            "jdk_modernization": {"applicable": True, "score": self.jdk_modernization,
                                  "reason": "orthogonal JDK modernization debt (excluded from "
                                            "the readiness_score aggregate)",
                                  "in_aggregate": False},
            "hibernate": {
                "applicable": _hibernate_applies,
                "score": self.hibernate_readiness if _hibernate_applies else None,
                "reason": _hib_reason,
                "status": hib_status,
            },
        }

        # ── BUG #3: aggregate invariant ─────────────────────────────────────────
        # readiness_score == min(score of every applicable migration dimension).
        # MIN (not mean): a migration is only as ready as its weakest applicable
        # axis. _MIGRATION_DIMENSIONS lists exactly which dimensions feed it; the
        # consistency invariant is asserted in finalize and covered by a unit test.
        agg_inputs = {
            name: self.applicable_dimensions[name]["score"]
            for name in _MIGRATION_DIMENSIONS
            if self.applicable_dimensions[name]["applicable"]
        }
        # BUG #4: when NO migration dimension applies (non-Spring repo, no migratable
        # javax.*, no Hibernate 5), readiness is N/A (None) — NOT a manufactured 100.
        # Absence of a migration target is "not applicable", never "100% ready".
        self.readiness_score = min(agg_inputs.values()) if agg_inputs else None
        self.readiness_aggregate = {
            "method": "min",
            "inputs": agg_inputs,
            "excluded": ["jdk_modernization"],
            "applicable": bool(agg_inputs),
            "note": ("readiness_score = min over applicable migration dimensions "
                     "(jakarta / boot3 / hibernate). jdk_modernization is an orthogonal "
                     "upkeep axis and is intentionally excluded."
                     if agg_inputs else
                     "N/A — no migration target detected (no migratable javax.*, no "
                     "runtime Spring, no Hibernate 5). JDK-modernization findings, if "
                     "any, are reported on their own axis."),
        }
        # Internal consistency guard — the headline cannot diverge from the dimensions
        # it claims to summarize (catches a future scorer change that breaks the model).
        if agg_inputs:
            assert self.readiness_score == min(agg_inputs.values()), (
                "readiness_score must equal min(applicable migration dimensions); "
                f"got {self.readiness_score} vs inputs {agg_inputs}"
            )

        # BUG #5: effort over MAIN findings only — N/A axes (Hibernate-6 phantom,
        # test fixtures) no longer pad the estimate.
        self.estimated_effort_days = round(
            len(critical_files) * 0.5
            + len(high_files) * 0.25
            + len(medium_files) * 0.1
            + len(low_files) * 0.05,
            1,
        )

        # BUG #6 / #8: hygiene + non-blocking buckets, surfaced separately.
        self.hygiene_findings = sum(
            1 for f in main_findings if f.migration_target in _BEST_PRACTICE_TARGETS
        )
        _nb_by_ctx: dict[str, int] = {}
        _nb_by_rule: dict[str, int] = {}
        for f in self.findings:
            if f.code_context == "main":
                continue
            _nb_by_ctx[f.code_context] = _nb_by_ctx.get(f.code_context, 0) + 1
            _nb_by_rule[f.rule_id] = _nb_by_rule.get(f.rule_id, 0) + 1
        self.non_blocking = {
            "count": sum(_nb_by_ctx.values()),
            "by_context": _nb_by_ctx,
            "by_rule": _nb_by_rule,
            "note": ("Findings in test/fixture harnesses or autogenerated sources. "
                     "Excluded from blocking_count, readiness, and effort."),
        }

        self.summary = {
            "total_findings": len(self.findings),
            "affected_files": len(affected_files),
            "by_severity": by_severity,
            "by_rule": by_rule,
            "by_migration_target": by_target,
            "main_findings": len(main_findings),
            "non_blocking_findings": self.non_blocking["count"],
            "hygiene_findings": self.hygiene_findings,
        }
        return self

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "repo_id": self.repo_id,
            "git_head": self.git_head,
            "readiness_score": self.readiness_score,
            "jakarta_readiness": self.jakarta_readiness,
            "boot3_readiness": self.boot3_readiness,
            "jdk_modernization": self.jdk_modernization,
            "hibernate_readiness": self.hibernate_readiness,
            "applicable_dimensions": self.applicable_dimensions,
            "readiness_aggregate": self.readiness_aggregate,
            "readiness_note": (
                "readiness_score = min over applicable MIGRATION dimensions "
                "(jakarta / boot3 / hibernate); see readiness_aggregate for the exact "
                "inputs. N/A dimensions are excluded (never counted as 0); "
                "jdk_modernization is orthogonal upkeep and is NOT in the aggregate. "
                "For decisions read the per-dimension breakdown + blocking_count."
            ),
            "headline_blocker": self.headline_blocker,
            "blocking_count": self.blocking_count,
            "estimated_effort_days": self.estimated_effort_days,
            "hygiene_findings": self.hygiene_findings,
            "non_blocking": self.non_blocking,
            "spring_present": self.spring_present,
            "spring_test_only": self.spring_test_only,
            "spring_boot_2_detected": self.spring_boot_2_detected,
            "spring_boot_version_detected": self.spring_boot_version_detected,
            "summary": self.summary,
            "findings": [f.to_dict() for f in self.findings],
            "hibernate": self.hibernate.to_dict() if self.hibernate is not None else None,
            "limitations": self.limitations,
            "metadata": self.metadata,
        }

    def to_text(self, min_severity: str = "low") -> str:
        min_order = SEVERITY_ORDER.get(min_severity, 3)
        visible = [f for f in self.findings if SEVERITY_ORDER.get(f.severity, 3) <= min_order]

        if self.spring_boot_2_detected is True:
            _boot = "Boot 2 (migration target)"
        elif self.spring_boot_2_detected is False:
            _boot = f"Boot {self.spring_boot_version_detected or '3+'} detected"
        else:
            _boot = "unknown"

        def _dim(name: str) -> str:
            d = self.applicable_dimensions.get(name, {})
            if not d.get("applicable", True):
                return f"{name}: N/A"
            return f"{name}: {d.get('score')}/100"

        # Blocking parenthetical reflects MAIN (product) findings only — matching
        # blocking_count — so the text headline cannot contradict the JSON.
        main_crit = sum(1 for f in self.findings
                        if f.code_context == "main" and f.severity == "critical")
        main_high = sum(1 for f in self.findings
                        if f.code_context == "main" and f.severity == "high")
        nb = self.non_blocking.get("count", 0)
        _headline = (f"{self.readiness_score}/100" if self.readiness_score is not None
                     else "N/A (no migration target detected)")
        lines: list[str] = [
            f"Migration Readiness: {_headline}",
            f"  {_dim('jakarta')}  {_dim('boot3')}  "
            f"{_dim('jdk_modernization')}  {_dim('hibernate')}",
            *([f"  ⚠ Headline blocker: {self.headline_blocker} "
               f"(readiness_score reflects jakarta/Boot3 only — Hibernate is a separate rewrite axis)"]
              if self.headline_blocker else []),
            f"Spring present: {self.spring_present}    Spring Boot 2 detected: {_boot}",
            f"Blocking issues (product code): {self.blocking_count}  "
            f"(critical: {main_crit}, high: {main_high})"
            + (f"   [+{nb} in test/generated, non-blocking]" if nb else ""),
            f"Affected files: {self.summary.get('affected_files', 0)}",
            f"Estimated effort: {self.estimated_effort_days}d",
            "",
        ]

        if self.hibernate is not None and self.hibernate.detected:
            lines.append(self.hibernate.to_text())
            lines.append("")

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
# Java source scanner
# ---------------------------------------------------------------------------

# BUG #7: javax.annotation.Generated is the JSR-250 marker emitted into
# AUTOGENERATED sources (SCIM, JAXB, MapStruct…). It maps mechanically to
# jakarta.annotation.Generated and carries low migration value — it must not be
# described with the generic "@PostConstruct/@PreDestroy/@Resource" blurb, nor
# rated medium alongside real CDI lifecycle annotations.
_MIG006_LIFECYCLE = ("PostConstruct", "PreDestroy", "Resource",
                     "ManagedBean", "Priority", "Resources")


def _refine_mig006(matched_imports: list[str]) -> tuple[str, str]:
    """Return (severity, explanation) tailored to the actual javax.annotation symbols.

    Explanation names the concrete symbols detected, not a fixed list. When the
    only symbol is `Generated`, severity degrades to low (mechanical, generated
    code); otherwise the CDI lifecycle annotations keep medium severity.
    """
    symbols = [imp.rsplit(".", 1)[-1] for imp in matched_imports]
    lifecycle = [s for s in symbols if s in _MIG006_LIFECYCLE]
    only_generated = bool(symbols) and all(s == "Generated" for s in symbols)
    sym_list = ", ".join(f"@{s}" for s in dict.fromkeys(symbols)) or "javax.annotation"
    if only_generated:
        return (
            "low",
            "javax.annotation.Generated (JSR-250) maps mechanically to "
            "jakarta.annotation.Generated. It is emitted into autogenerated sources "
            "and carries low migration value — re-run the generator on the Jakarta "
            "toolchain rather than hand-editing.",
        )
    affected = ", ".join(f"@{s}" for s in dict.fromkeys(lifecycle)) or sym_list
    return (
        "medium",
        f"jakarta.annotation replaces javax.annotation in Jakarta EE 9+. "
        f"Detected symbol(s): {sym_list}. {affected} are affected.",
    )


# ---------------------------------------------------------------------------
# BUG #3 (v1.68.0): framework-gated narrative.
# MIG-* explanations are written assuming a Spring Boot 2→3 upgrade because that is
# the overwhelmingly common Jakarta-migration context. On a repo where the tool has
# already determined spring_present=False (plain Jakarta EE, JAX-RS/Jersey, Quarkus,
# Guice, ...), the underlying javax→jakarta finding is still valid, but prose that
# says "Spring Boot 3 requires ..." is factually wrong for that repo and misleads any
# reader who trusts the narrative over the structured spring_present field.
#
# Treat the narrative as a pure function of the structured data: when spring_present
# is False, rewrite Spring-specific framing into framework-neutral Jakarta-EE /
# servlet-container framing. Ordered phrase map first (keeps nice sentences for the
# common phrasings), then a catch-all so no "Spring Boot" string can survive.
# ---------------------------------------------------------------------------
_NON_SPRING_EXPLANATION_SUBS: tuple[tuple[str, str], ...] = (
    ("Spring Boot 3 bundles Jakarta Servlet 6.0.",
     "Modern servlet containers (Jetty 12+, Tomcat 11+) require Jakarta Servlet 6.0."),
    ("Spring Boot 3 requires Jakarta Servlet 5.0+",
     "Jakarta EE 9+ servlet containers require Jakarta Servlet 5.0+"),
    ("Spring Boot 3 uses Jakarta EE 9 which moved",
     "Jakarta EE 9 moved"),
    ("Spring Boot 3 uses Hibernate Validator 8.x which implements jakarta.validation.",
     "Jakarta EE 9+ uses Hibernate Validator 8.x which implements jakarta.validation."),
    ("Spring Boot 3 depends on Jakarta Transactions (jakarta.transaction).",
     "Jakarta EE 9+ uses Jakarta Transactions (jakarta.transaction)."),
    ("Spring Boot 3 requires Hibernate 6",
     "Jakarta Persistence (JPA 3.x) requires Hibernate 6"),
    ("Spring Boot 3 cache abstraction. Spring Boot 3 requires EhCache 3.x",
     "Jakarta-EE cache abstraction. The modern JCache provider requires EhCache 3.x"),
    # Catch-all: any remaining Spring-Boot framing → generic Jakarta EE 9+.
    ("Spring Boot 3", "Jakarta EE 9+"),
    ("Spring Boot 2", "the pre-Jakarta (javax) baseline"),
)


def _neutralize_non_spring_explanation(text: str) -> str:
    """Rewrite Spring-specific migration prose into framework-neutral Jakarta framing.

    Applied only when the report's spring_present is False. Deterministic phrase
    substitution — no model, no guess. Guarantees the returned text contains no
    "Spring Boot" framing that the structured data does not support.
    """
    for needle, repl in _NON_SPRING_EXPLANATION_SUBS:
        text = text.replace(needle, repl)
    return text


def _scan_file(
    source: str,
    rel_path: str,
    rules: list[_Rule],
) -> list[MigrationFinding]:
    findings: list[MigrationFinding] = []

    for rule in rules:
        matched_imports: list[str] = []
        import_first_line: Optional[int] = None
        code_first_line: Optional[int] = None
        code_snippets: list[str] = []

        if rule.import_pattern is not None:
            matches = list(rule.import_pattern.finditer(source))
            # BUG #2: for jakarta-migration rules, drop imports whose FQN belongs
            # to a JDK/permanent javax namespace (javax.transaction.xa.*,
            # javax.annotation.processing.*, ...). These do NOT migrate to jakarta.
            if matches and rule.migration_target == "jakarta":
                matches = [m for m in matches if not _is_no_migrate_javax(m.group(1).strip())]
            # BUG #1: MIG-011 prefix heuristic must not flag sun.*/com.sun.* packages
            # the JDK exports unconditionally (no --add-exports/--add-opens needed).
            # Drop those imports; if none remain, the file produces no MIG-011 finding
            # (so it never inflates blocking_count / effort). Genuinely-internal
            # packages are absent from the allowlist and survive as `high`.
            if matches and rule.id == "MIG-011":
                matches = [
                    m for m in matches
                    if not _is_jdk_unconditional_export(m.group(1).strip())
                ]
            if matches:
                import_first_line = source[: matches[0].start()].count("\n") + 1
                matched_imports = [m.group(1) for m in matches]

        if rule.code_pattern is not None:
            m = rule.code_pattern.search(source)
            if m is not None:
                code_first_line = source[: m.start()].count("\n") + 1
                code_snippets = [m.group(0).strip()]

        extends_first_line: Optional[int] = None
        if rule.extends_pattern is not None:
            m = rule.extends_pattern.search(source)
            if m is not None:
                extends_first_line = source[: m.start()].count("\n") + 1

        candidate_lines = [
            ln for ln in (import_first_line, code_first_line, extends_first_line)
            if ln is not None
        ]
        if not candidate_lines:
            continue

        first_line = min(candidate_lines)
        all_matches = matched_imports + code_snippets

        severity = rule.severity
        explanation = rule.explanation
        if rule.id == "MIG-006" and matched_imports:
            severity, explanation = _refine_mig006(matched_imports)

        findings.append(
            MigrationFinding(
                id=MigrationFinding.make_id(rule.id, rel_path),
                rule_id=rule.id,
                severity=severity,
                title=rule.title,
                source_file=rel_path,
                first_line=first_line,
                imports_found=all_matches,
                explanation=explanation,
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
    """Scan a Java repository for migration blockers.

    Scans:
      - Java source files (.java) against all 24 rules (MIG-001..MIG-025)
      - Spring XML config files (applicationContext.xml, web.xml, security XML, etc.)
      - Build descriptors (pom.xml, build.gradle) for incompatible dependencies

    Args:
        file_paths:   Relative Java file paths (from find_java_files).
        root:         Absolute repo root.
        min_severity: Filter threshold. Choices: critical | high | medium | low.

    Returns:
        MigrationReport with findings, readiness_score, effort estimate, and
        migration_target breakdown.
    """
    min_order = SEVERITY_ORDER.get(min_severity, 3)
    all_findings: list[MigrationFinding] = []
    limitations: list[str] = []
    read_errors = 0
    jakarta_import_count = 0
    # BUG #1/#2: split Spring imports into runtime vs test. A test-tree file, or a
    # test-only Spring package (org.springframework.test / .boot.test), is a
    # test-only signal and must NOT mark the repo as runtime-Spring.
    spring_runtime_import_seen = False
    spring_test_import_seen = False
    from sourcecode.path_filters import is_test_path as _is_test_path

    # ── Java source scan ────────────────────────────────────────────────────
    for rel_path in file_paths:
        abs_path = root / rel_path
        try:
            source = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            read_errors += 1
            continue

        # Jakarta EE 9+ namespace adoption signal (vetoes a false Boot-2 verdict).
        jakarta_import_count += len(_JAKARTA_IMPORT_RE.findall(source))
        _in_test = _is_test_path(rel_path)
        for _m in _SPRING_IMPORT_RE.finditer(source):
            _sub = _m.group(1)
            _is_test_pkg = _sub.startswith(("test.", "boot.test.")) or _sub in ("test", "boot.test")
            if _in_test or _is_test_pkg:
                spring_test_import_seen = True
            else:
                spring_runtime_import_seen = True

        file_findings = _scan_file(source, rel_path, _ALL_RULES)
        filtered = [f for f in file_findings if SEVERITY_ORDER.get(f.severity, 3) <= min_order]
        all_findings.extend(filtered)

    if read_errors:
        limitations.append(f"{read_errors} file(s) could not be read and were skipped.")

    # ── XML + dependency scan (single tree walk) ─────────────────────────────
    xml_files, build_files = _find_non_java_files(root)
    xml_read_errors = 0
    for abs_path, rel_path in xml_files:
        try:
            text = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            xml_read_errors += 1
            continue
        xml_findings = _scan_xml_file(text, rel_path)
        filtered = [f for f in xml_findings if SEVERITY_ORDER.get(f.severity, 3) <= min_order]
        all_findings.extend(filtered)

    if xml_read_errors:
        limitations.append(f"{xml_read_errors} XML file(s) could not be read and were skipped.")

    dep_read_errors = 0
    raw_dep_findings: list[MigrationFinding] = []
    for abs_path, rel_path in build_files:
        try:
            text = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            dep_read_errors += 1
            continue
        dep_findings = _scan_dep_file(text, rel_path)
        filtered = [f for f in dep_findings if SEVERITY_ORDER.get(f.severity, 3) <= min_order]
        raw_dep_findings.extend(filtered)

    # Deduplicate dep findings by rule_id: same dependency in parent + child poms
    # is one logical finding. Keep the first occurrence (root pom sorts first).
    _seen_dep_rules: dict[str, int] = {}  # rule_id → count
    for f in raw_dep_findings:
        _seen_dep_rules[f.rule_id] = _seen_dep_rules.get(f.rule_id, 0) + 1
    _dedup_dep: list[MigrationFinding] = []
    _emitted: set[str] = set()
    for f in raw_dep_findings:
        if f.rule_id not in _emitted:
            _dedup_dep.append(f)
            _emitted.add(f.rule_id)
    all_findings.extend(_dedup_dep)

    if dep_read_errors:
        limitations.append(f"{dep_read_errors} build file(s) could not be read and were skipped.")

    limitations.extend(_STATIC_LIMITATIONS)

    spring_boot_2, spring_boot_version = _detect_spring_boot(root, jakarta_import_count)
    spring_present, spring_test_only = _detect_spring_usage(
        root, spring_runtime_import_seen, spring_test_import_seen
    )

    # BUG #8: classify each finding's code context (main / test / generated) so the
    # report can keep test fixtures and autogenerated sources out of blocking_count.
    for f in all_findings:
        f.code_context = _classify_code_context(f)

    # BUG #3 (v1.68.0): on a non-Spring repo, rewrite Spring-Boot-specific framing in
    # every finding's explanation into framework-neutral Jakarta-EE framing. The
    # finding itself (javax→jakarta) stays; only the prose is corrected so it matches
    # the structured spring_present=False signal in the same report.
    if not spring_present:
        for f in all_findings:
            if f.explanation:
                f.explanation = _neutralize_non_spring_explanation(f.explanation)

    # Hibernate 5→6 stratification (independent of min_severity — it is its own
    # risk model, not a severity-filtered finding stream).
    from sourcecode.hibernate_strat import analyze_hibernate
    hibernate_strat = analyze_hibernate(file_paths, root)

    report = MigrationReport(
        spring_boot_2_detected=spring_boot_2,
        spring_boot_version_detected=spring_boot_version,
        spring_present=spring_present,
        spring_test_only=spring_test_only,
        jakarta_namespace_adopted=jakarta_import_count > 0,
        findings=all_findings,
        hibernate=hibernate_strat,
        limitations=limitations,
        metadata={
            "java_files_scanned": len(file_paths),
            "xml_files_scanned": len(xml_files),
            "build_files_scanned": len(build_files),
            "min_severity": min_severity,
            "rules_applied": [r.id for r in _ALL_RULES],
            "xml_rules_applied": [r.id for r in _XML_RULES],
            "dep_rules_applied": [r.id for r in _DEP_RULES],
        },
    )

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


# Remaining static limitations — things that truly require runtime analysis
_STATIC_LIMITATIONS: list[str] = [
    "Thread.stop/suspend/resume detection is best-effort: variable type cannot be confirmed "
    "without compilation. Verify that flagged variables are typed as java.lang.Thread.",
    "JPMS --add-opens requirements: exact set of required flags cannot be determined without "
    "running the application against the target JDK.",
    "Transitive dependency compatibility: library versions resolved transitively (not declared "
    "directly) require 'mvn dependency:tree' or Gradle dependency insight for full analysis.",
    "Runtime proxy behaviour (CGLIB subclass proxies): compatibility with Java 17+ strong "
    "encapsulation depends on framework version at runtime, not import-level analysis.",
    "XML bean definitions referencing class names via property placeholders (${bean.class}) "
    "cannot be resolved statically.",
]


# Spring Boot version captured ONLY in an authoritative context (parent,
# managed BOM, an explicit spring-boot* dependency version, a spring.boot.version
# property, or the Gradle plugin) — NOT any stray 2.x library version elsewhere
# in the pom. These run on property-resolved text so ${spring.boot.version}
# is already substituted.
_BOOT_VERSION_PATTERNS: tuple[re.Pattern, ...] = (
    # <spring.boot.version>3.5.14</...> / <spring-boot.version> property declaration
    re.compile(r"<spring[.\-_]?boot[.\-_]?version>\s*(\d+)\.\d", re.IGNORECASE),
    # gradle.properties: springBootVersion=3.5.14 / spring.boot.version=2.7.18
    re.compile(r"spring[.\-_]?boot[.\-_]?version\s*[=:]\s*[\"']?(\d+)\.\d", re.IGNORECASE),
    # <artifactId>spring-boot-*</artifactId> immediately followed by <version>
    re.compile(
        r"<artifactId>\s*spring-boot[\w-]*\s*</artifactId>\s*<version>\s*(\d+)\.\d",
        re.IGNORECASE,
    ),
    # <version> immediately followed by <artifactId>spring-boot-*</artifactId>
    re.compile(
        r"<version>\s*(\d+)\.\d[^<]*</version>\s*<artifactId>\s*spring-boot[\w-]*\s*</artifactId>",
        re.IGNORECASE,
    ),
    # Gradle plugin: id 'org.springframework.boot' version '3.1.0'
    re.compile(
        r"org\.springframework\.boot[\"']?[^\n]*?version[^\n]*?[\"'](\d+)\.\d",
        re.IGNORECASE,
    ),
)

_BOOT_FULL_VERSION_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"<spring[.\-_]?boot[.\-_]?version>\s*(\d+\.\d[\w.\-]*)", re.IGNORECASE),
    re.compile(r"spring[.\-_]?boot[.\-_]?version\s*[=:]\s*[\"']?(\d+\.\d[\w.\-]*)", re.IGNORECASE),
    re.compile(
        r"<artifactId>\s*spring-boot[\w-]*\s*</artifactId>\s*<version>\s*(\d+\.\d[\w.\-]*)",
        re.IGNORECASE,
    ),
)


def _extract_boot_versions(text: str) -> tuple[set[int], Optional[str]]:
    """Return (set of detected Spring Boot major versions, first full version string)."""
    majors: set[int] = set()
    for pat in _BOOT_VERSION_PATTERNS:
        for m in pat.finditer(text):
            try:
                majors.add(int(m.group(1)))
            except (ValueError, IndexError):
                pass
    full: Optional[str] = None
    for pat in _BOOT_FULL_VERSION_PATTERNS:
        m = pat.search(text)
        if m:
            full = m.group(1).strip()
            break
    return majors, full


def _detect_spring_boot(root: Path, jakarta_import_count: int) -> tuple[Optional[bool], Optional[str]]:
    """Tri-state Spring Boot 2 detection. Returns (spring_boot_2_detected, version).

    - True  → Spring Boot 2.x confirmed by build evidence (migration target).
    - False → Boot 3+ confirmed, OR jakarta.* namespace already adopted en masse.
    - None  → version could not be determined. Absence of evidence is never True.

    Resolves Maven ${properties} so version-by-property (no starter-parent) works,
    and detects Boot via parent, managed BOM, spring-boot* dependency, property,
    or the Gradle plugin. Massive jakarta.* imports veto a Boot-2 verdict.
    """
    root_files = [
        root / name
        for name in ("pom.xml", "build.gradle", "build.gradle.kts", "gradle.properties")
    ]
    child_poms = list(root.glob("*/pom.xml"))
    child_gradle = list(root.glob("*/build.gradle")) + list(root.glob("*/build.gradle.kts"))
    # Limit child scan to 30 files to stay fast on large monorepos.
    candidates = root_files + (child_poms + child_gradle)[:30]

    majors: set[int] = set()
    full_version: Optional[str] = None
    for candidate in candidates:
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if candidate.name == "pom.xml":
            text = _resolve_maven_properties(text)
        file_majors, file_full = _extract_boot_versions(text)
        majors |= file_majors
        if full_version is None and file_full is not None:
            full_version = file_full

    # Boot 3+ explicitly declared → not a Boot 2 repo.
    if any(m >= 3 for m in majors):
        return False, full_version
    # Boot 2 declared. jakarta.* adoption en masse contradicts a literal Boot-2
    # verdict (already mid/post namespace migration) → report unknown, never True.
    if 2 in majors:
        if jakarta_import_count > 0:
            return None, full_version
        return True, full_version
    # No version evidence. jakarta.* present ⇒ Boot 3 (Jakarta EE 9+).
    if jakarta_import_count > 0:
        return False, full_version
    return None, full_version

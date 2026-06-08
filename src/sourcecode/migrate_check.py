"""migrate_check.py — Java 8/Spring Boot 2 migration readiness checker.

Scans Java source files, Spring XML config, and build descriptors for patterns
that must be addressed when migrating:
  - Spring Boot 2 → 3 (javax → jakarta, Spring Security 6)
  - Java 8 → 17 / 21 (SecurityManager, Nashorn, Unsafe, reflection, etc.)
  - XML Spring config (applicationContext.xml, web.xml, security XML)
  - Dependency incompatibilities (SpringFox, Hibernate 5, ByteBuddy old)

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
from typing import Optional


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


def _scan_dep_file(text: str, rel_path: str) -> list["MigrationFinding"]:
    """Apply dependency rules to a build file. Returns one finding per matched rule."""
    is_gradle = rel_path.endswith((".gradle", ".gradle.kts"))
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

@dataclass
class MigrationReport:
    schema_version: str = "1.2"
    generated_at: str = ""
    repo_id: str = ""
    git_head: str = ""

    readiness_score: int = 100
    blocking_count: int = 0
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
# Java source scanner
# ---------------------------------------------------------------------------

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

    # ── Java source scan ────────────────────────────────────────────────────
    for rel_path in file_paths:
        abs_path = root / rel_path
        try:
            source = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            read_errors += 1
            continue

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
    for abs_path, rel_path in build_files:
        try:
            text = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            dep_read_errors += 1
            continue
        dep_findings = _scan_dep_file(text, rel_path)
        filtered = [f for f in dep_findings if SEVERITY_ORDER.get(f.severity, 3) <= min_order]
        all_findings.extend(filtered)

    if dep_read_errors:
        limitations.append(f"{dep_read_errors} build file(s) could not be read and were skipped.")

    limitations.extend(_STATIC_LIMITATIONS)

    spring_boot_2 = _detect_spring_boot_2(root)

    report = MigrationReport(
        spring_boot_2_detected=spring_boot_2,
        findings=all_findings,
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

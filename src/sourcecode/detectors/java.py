from __future__ import annotations

import re
from pathlib import Path
from xml.etree import ElementTree

from sourcecode.detectors.base import (
    AbstractDetector,
    DetectionContext,
    EntryPoint,
    StackDetection,
)
from sourcecode.detectors.parsers import read_text_lines, unique_strings
from sourcecode.schema import FrameworkDetection
from sourcecode.tree_utils import flatten_file_tree

_NS_TAG_RE = re.compile(r"\{[^}]+\}")

_MAX_FILE_SIZE = 256 * 1024  # 256 KB
_MAX_JAVA_ENTRY_SCAN = 1000
_MAX_ANNOTATION_ENTRY_POINTS = 1000

_REST_CONTROLLER_RE = re.compile(r'@RestController\b')
_MVC_CONTROLLER_RE = re.compile(r'@Controller\b')
_REQUEST_MAPPING_RE = re.compile(r'@RequestMapping\b')
_CONTROLLER_ADVICE_RE = re.compile(r'@ControllerAdvice\b')
_WEB_FILTER_RE = re.compile(r'@WebFilter\b')
_FILTER_BEAN_RE = re.compile(r'FilterRegistrationBean\b')
# Extracts path from @RequestMapping("/v1/foo"), @GetMapping("/bar"), etc.
# Handles attribute order: value= may come after method= in legacy @RequestMapping style.
_HTTP_PATH_RE = re.compile(
    r'@(?:Request|Get|Post|Put|Delete|Patch)Mapping\s*\([^)]*?(?:value\s*=\s*)?["\']([^"\']+)["\']'
)
_REQUEST_METHOD_VERB_RE = re.compile(
    r'method\s*=\s*RequestMethod\.([A-Z]+)'
)
# @M3FiltroSeguridad custom security annotation
_M3_FILTRO_RE = re.compile(r'@M3FiltroSeguridad\b')
_M3_FILTRO_PARAMS_RE = re.compile(
    r'@M3FiltroSeguridad\s*\(\s*'
    r'(?:nombreRecurso\s*=\s*(?:"([^"]*)"|([\w.]+)))?'  # group 1: string literal, group 2: constant ref
    r'(?:[^)]*nivelRequerido\s*=\s*(\d+))?'  # group 3: nivel
)

# Security config detection
_WEB_SECURITY_CONFIGURER_RE = re.compile(r'WebSecurityConfigurerAdapter\b')
_SECURITY_FILTER_CHAIN_RE = re.compile(r'SecurityFilterChain\b')
_SECURITY_CONFIG_ANNOTATION_RE = re.compile(r'@EnableWebSecurity\b')
# JWT/token filter detection (files that process every request)
_ONCE_PER_REQUEST_FILTER_RE = re.compile(r'extends\s+(?:OncePerRequestFilter|GenericFilterBean)\b')
_JWT_FILTER_KEYWORDS_RE = re.compile(r'\b(?:jwt|token|bearer|authorization)\b', re.IGNORECASE)

# --- JAX-RS / Jakarta REST ---
_JAX_RS_PATH_RE = re.compile(r'@Path\s*\(\s*["\']([^"\']+)["\']')
_JAX_RS_VERB_RE = re.compile(r'@(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\b')
_JAX_RS_PROVIDER_RE = re.compile(r'@Provider\b')

# --- CDI / Jakarta EE scopes ---
_CDI_SCOPED_RE = re.compile(r'@(?:ApplicationScoped|RequestScoped|SessionScoped|Singleton|Dependent)\b')

# --- Keycloak / Quarkus SPI ---
# Matches "implements EventListenerProvider" or "implements Foo, EventListenerProvider, Bar"
_SPI_IMPL_RE = re.compile(
    r'implements\s+[\w\s,]*\b('
    r'EventListenerProvider|EventListenerProviderFactory'
    r'|RealmResourceProvider|RealmResourceProviderFactory'
    r'|AuthenticatorFactory|Authenticator'
    r'|ProtocolMapper|ProtocolMapperFactory'
    r'|CredentialProvider|CredentialProviderFactory'
    r'|PolicyProviderFactory|PolicyProvider'
    r'|RequiredActionProvider|RequiredActionFactory'
    r'|IdentityProviderMapper|IdentityProviderFactory'
    r')\b'
)

# @Transactional detection
_TRANSACTIONAL_RE = re.compile(r'@Transactional\b')
# Extracts class name: `public class Foo` or `class Foo`
_CLASS_NAME_RE = re.compile(r'\bclass\s+([A-Z][A-Za-z0-9_]*)')

# Gradle plugin Spring Boot version: id 'org.springframework.boot' version '2.6.3'
_GRADLE_SB_PLUGIN_RE = re.compile(
    r"""id\s*['"]\s*org\.springframework\.boot\s*['"]\s+version\s*['"]([\d.]+)['"']""",
    re.IGNORECASE,
)
# Gradle Java version: sourceCompatibility = '11' or sourceCompatibility = JavaVersion.VERSION_11
_GRADLE_JAVA_VERSION_RE = re.compile(
    r"""(?:sourceCompatibility|targetCompatibility|javaVersion)\s*=\s*['"]?([0-9.]+)['"]?""",
    re.IGNORECASE,
)
# JavaVersion.VERSION_11 form
_GRADLE_JAVA_ENUM_RE = re.compile(
    r"""(?:sourceCompatibility|targetCompatibility)\s*=\s*JavaVersion\.VERSION_(\d+)"""
)


class JavaDetector(AbstractDetector):
    name = "java"
    priority = 60

    def can_detect(self, context: DetectionContext) -> bool:
        return any(manifest in context.manifests for manifest in ("pom.xml", "build.gradle"))

    def detect(self, context: DetectionContext) -> tuple[list[StackDetection], list[EntryPoint]]:
        frameworks: list[FrameworkDetection] = []
        manifests: list[str] = []
        language_version: str | None = None
        packaging: str | None = None
        app_server_hint: str | None = None
        spring_profiles: list[str] = []

        if "pom.xml" in context.manifests:
            manifests.append("pom.xml")
            pom_path = context.root / "pom.xml"
            frameworks.extend(self._frameworks_from_pom(pom_path))
            meta = self._parse_pom_metadata(pom_path)
            if meta.get("language_version"):
                language_version = meta["language_version"]
            if meta.get("packaging"):
                packaging = meta["packaging"]
        if "build.gradle" in context.manifests:
            manifests.append("build.gradle")
            frameworks.extend(self._frameworks_from_gradle(context.root / "build.gradle"))
            if language_version is None:
                try:
                    gradle_content = "\n".join(read_text_lines(context.root / "build.gradle"))
                    language_version = self._extract_gradle_java_version(gradle_content)
                except OSError:
                    pass

        # Detect app server from descriptor files
        all_paths = flatten_file_tree(context.file_tree)
        if any("weblogic.xml" in p or "weblogic-ejb-jar.xml" in p for p in all_paths):
            app_server_hint = "weblogic"
        elif any("wildfly" in p.lower() or "jboss" in p.lower() for p in all_paths):
            app_server_hint = "wildfly"

        # Spring profiles — check src/main/options/, src/main/resources/
        spring_profiles = self._detect_spring_profiles(context.root, all_paths)

        entry_points = self._collect_entry_points(context)
        transactional_classes = self._collect_transactional_classes(context, all_paths)
        stack = StackDetection(
            stack="java",
            detection_method="manifest",
            confidence="high",
            frameworks=self._dedupe_frameworks(frameworks),
            manifests=manifests,
            language_version=language_version,
            packaging=packaging,
            app_server_hint=app_server_hint,
            spring_profiles=spring_profiles,
            transactional_classes=transactional_classes,
        )
        return [stack], entry_points

    def _parse_pom_metadata(self, path: Path) -> dict:
        """Extract packaging, java version from pom.xml properties/parent."""
        result: dict = {}
        try:
            tree = ElementTree.parse(path)
        except (OSError, ElementTree.ParseError):
            return result
        root = tree.getroot()
        ns_match = _NS_TAG_RE.match(root.tag)
        ns = ns_match.group(0) if ns_match else ""

        # Packaging (FIX-6)
        packaging_elem = root.find(f"{ns}packaging")
        if packaging_elem is not None and packaging_elem.text:
            result["packaging"] = packaging_elem.text.strip().lower()

        # Properties
        props_elem = root.find(f"{ns}properties")
        props: dict[str, str] = {}
        if props_elem is not None:
            for prop in props_elem:
                tag = prop.tag.replace(ns, "") if ns else prop.tag
                if prop.text:
                    props[tag] = prop.text.strip()

        # Java version (FIX-7) — check properties first, then compiler plugin
        for key in ("maven.compiler.source", "java.version", "maven.compiler.release"):
            if key in props:
                result["language_version"] = props[key]
                break
        if "language_version" not in result:
            # Check maven-compiler-plugin configuration
            for plugin in root.findall(f".//{ns}plugin"):
                artifact = (plugin.findtext(f"{ns}artifactId") or "").strip()
                if artifact == "maven-compiler-plugin":
                    config = plugin.find(f"{ns}configuration")
                    if config is not None:
                        for tag in ("source", "release"):
                            val = config.findtext(f"{ns}{tag}")
                            if val:
                                result["language_version"] = val.strip()
                                break
                    break

        return result

    def _detect_spring_profiles(self, root: Path, all_paths: list[str]) -> list[str]:
        """Detect Spring profiles from option/resource directories and application-{profile}.yml."""
        profiles: list[str] = []
        seen: set[str] = set()

        # Pattern 1: src/main/options/{profile}/ directories
        _PROFILE_DIRS = ("src/main/options/", "src/main/resources/")
        for path in all_paths:
            for prefix in _PROFILE_DIRS:
                if path.startswith(prefix):
                    remainder = path[len(prefix):]
                    parts = remainder.split("/")
                    if len(parts) >= 1 and parts[0] and not parts[0].startswith("."):
                        candidate = parts[0]
                        # Only if it's a directory (has sub-paths) with application.yml
                        if candidate not in seen and not candidate.endswith(".yml") and not candidate.endswith(".yaml") and not candidate.endswith(".properties"):
                            seen.add(candidate)
                            profiles.append(candidate)
                break

        # Pattern 2: application-{profile}.yml files
        _APP_PROFILE_RE = re.compile(r"application-([A-Za-z0-9_-]+)\.ya?ml$")
        for path in all_paths:
            m = _APP_PROFILE_RE.search(path)
            if m:
                profile = m.group(1)
                if profile not in seen:
                    seen.add(profile)
                    profiles.append(profile)

        # Filter out generic names that aren't profiles
        _SKIP = frozenset({"test", "it", "integration"})
        return [p for p in profiles if p.lower() not in _SKIP]

    def _frameworks_from_pom(self, path: Path) -> list[FrameworkDetection]:
        try:
            tree = ElementTree.parse(path)
        except (OSError, ElementTree.ParseError):
            return []
        root_elem = tree.getroot()
        ns_match = _NS_TAG_RE.match(root_elem.tag)
        ns = ns_match.group(0) if ns_match else ""

        # Extract Spring Boot version from <parent> (FIX-3)
        sb_version: str | None = None
        parent_elem = root_elem.find(f"{ns}parent")
        if parent_elem is not None:
            parent_artifact = (parent_elem.findtext(f"{ns}artifactId") or "").strip()
            if parent_artifact == "spring-boot-starter-parent":
                sb_version = (parent_elem.findtext(f"{ns}version") or "").strip() or None

        text = ElementTree.tostring(root_elem, encoding="unicode").lower()
        frameworks = self._detect_jvm_frameworks(text, "pom.xml", sb_version=sb_version)
        return frameworks

    def _frameworks_from_gradle(self, path: Path) -> list[FrameworkDetection]:
        original = "\n".join(read_text_lines(path))
        content = original.lower()
        sb_version = self._extract_gradle_sb_version(original)
        return self._detect_jvm_frameworks(content, "build.gradle", sb_version=sb_version)

    def _extract_gradle_sb_version(self, content: str) -> str | None:
        m = _GRADLE_SB_PLUGIN_RE.search(content)
        return m.group(1) if m else None

    def _extract_gradle_java_version(self, content: str) -> str | None:
        m = _GRADLE_JAVA_ENUM_RE.search(content)
        if m:
            v = m.group(1)
            return "1." + v if int(v) <= 8 else v
        m = _GRADLE_JAVA_VERSION_RE.search(content)
        if m:
            return m.group(1)
        return None

    def _detect_jvm_frameworks(self, text: str, source: str, *, sb_version: str | None = None) -> list[FrameworkDetection]:
        frameworks: list[FrameworkDetection] = []
        if "com.android.application" in text or "com.android.library" in text:
            frameworks.append(FrameworkDetection(name="Android", source=source))
        if "spring-boot" in text:
            frameworks.append(FrameworkDetection(name="Spring Boot", source=source, version=sb_version))
        if "spring-webmvc" in text or "spring-web" in text:
            frameworks.append(FrameworkDetection(name="Spring MVC", source=source))
        if "spring-webflux" in text:
            frameworks.append(FrameworkDetection(name="Spring WebFlux", source=source))
        if "quarkus" in text:
            frameworks.append(FrameworkDetection(name="Quarkus", source=source))
        if "micronaut" in text:
            frameworks.append(FrameworkDetection(name="Micronaut", source=source))
        if "io.vertx" in text or "vertx" in text:
            frameworks.append(FrameworkDetection(name="Vert.x", source=source))
        if "jakarta.ee" in text or "javax.ws.rs" in text:
            frameworks.append(FrameworkDetection(name="Jakarta EE", source=source))
        if "mybatis" in text:
            frameworks.append(FrameworkDetection(name="MyBatis", source=source))
        if "spring-boot-starter-security" in text or "spring-security-core" in text:
            frameworks.append(FrameworkDetection(name="Spring Security", source=source))
        if "spring-boot-starter-data-jpa" in text or "spring-data-jpa" in text:
            frameworks.append(FrameworkDetection(name="Spring Data JPA", source=source))
        if "spring-ldap-core" in text or "spring-security-ldap" in text:
            frameworks.append(FrameworkDetection(name="Spring LDAP", source=source))
        if "spring-aspects" in text or "spring-aop" in text:
            frameworks.append(FrameworkDetection(name="Spring AOP", source=source))
        if "spring-boot-starter-activemq" in text or "activemq-broker" in text or "activemq-client" in text:
            frameworks.append(FrameworkDetection(name="ActiveMQ", source=source))
        return frameworks

    def _collect_entry_points(self, context: DetectionContext) -> list[EntryPoint]:
        all_paths = flatten_file_tree(context.file_tree)
        all_java = [p for p in all_paths if p.endswith(".java")]

        # Augment with a direct scan of standard Java source roots for Controller-named
        # files that the depth-limited file_tree scanner may have missed.
        # DDD layouts place REST controllers at depth 10+ (e.g.
        # src/main/java/com/org/app/ddd/domain/infraestructure/rest/XxxRestController.java).
        self._augment_deep_java_controllers(context, all_java)

        # 1. @SpringBootApplication entry: Application.java / Main.java by name
        # Exclude test trees: test helpers like AdminApplication.java in
        # integration/src/test/java/ must not be treated as production entrypoints.
        from sourcecode.path_filters import is_test_path as _is_test_path
        app_candidates = [
            p for p in all_java
            if p.endswith(("Application.java", "Main.java"))
            and not _is_test_path(p)
        ]
        entry_points: list[EntryPoint] = [
            EntryPoint(path=p, stack="java", kind="application", source="manifest")
            for p in unique_strings(app_candidates)
        ]

        # 2. Annotation-based scan: @RestController, @WebFilter, FilterRegistrationBean
        # Prioritize Controller-named files so all REST controllers are detected
        # even in large codebases where total files > _MAX_JAVA_ENTRY_SCAN.
        _non_test = [p for p in all_java if not _is_test_path(p)]
        _ctrl_files = [p for p in _non_test if "Controller" in p]
        _other_files = [p for p in _non_test if "Controller" not in p]
        scan_candidates = _ctrl_files + _other_files[:max(0, _MAX_JAVA_ENTRY_SCAN - len(_ctrl_files))]

        annotation_eps: list[EntryPoint] = []
        for rel_path in scan_candidates:
            if len(annotation_eps) >= _MAX_ANNOTATION_ENTRY_POINTS:
                break
            abs_path = context.root / rel_path
            annotation_eps.extend(self._scan_java_file_for_entry_points(abs_path, rel_path))
        entry_points.extend(annotation_eps)

        # 3. web.xml servlet/filter declarations
        web_xml_paths = [
            p for p in all_paths
            if p.endswith("web.xml") and "WEB-INF" in p
        ]
        for rel_path in web_xml_paths[:1]:
            abs_path = context.root / rel_path
            entry_points.extend(self._parse_web_xml(abs_path, rel_path))

        # 4. META-INF/services SPI declarations (Keycloak/Quarkus service-loader pattern).
        # Each file name is the SPI interface FQN; content lists implementing classes.
        service_paths = [p for p in all_paths if "META-INF/services/" in p and not p.endswith("/")]
        for rel_path in service_paths[:50]:
            iface_name = Path(rel_path).name
            entry_points.append(EntryPoint(
                path=rel_path, stack="java", kind="spi_provider",
                source="service_loader", confidence="high",
                evidence=f"SPI:{iface_name}",
            ))

        # Deduplicate by (path, kind)
        seen: set[tuple[str, str]] = set()
        unique_eps: list[EntryPoint] = []
        for ep in entry_points:
            key = (ep.path, ep.kind)
            if key not in seen:
                seen.add(key)
                unique_eps.append(ep)
        return unique_eps

    def _augment_deep_java_controllers(self, context: DetectionContext, all_java: list[str]) -> None:
        """Scan standard Java source roots for *Controller*.java files not in all_java.

        The depth-limited file_tree scanner misses files at depth >= max_depth.
        DDD layouts place REST controllers deep (e.g. depth 10+), so we supplement
        with a direct filesystem walk scoped to the standard Maven/Gradle source root.
        """
        import os as _os
        existing = set(all_java)
        # Standard Java source root candidates (Maven first, then Gradle/other)
        _SRC_ROOTS = ("src/main/java", "src/main/kotlin", "src/java", "src")
        for src_root_name in _SRC_ROOTS:
            src_root = context.root / src_root_name
            if not src_root.is_dir():
                continue
            try:
                for dirpath, _dirs, filenames in _os.walk(str(src_root)):
                    for fname in filenames:
                        if not fname.endswith(".java"):
                            continue
                        # Include Spring controllers and JAX-RS resources by naming convention.
                        if "Controller" not in fname and "Resource" not in fname:
                            continue
                        full = Path(dirpath) / fname
                        if full.is_symlink():
                            continue
                        try:
                            rel = str(full.relative_to(context.root)).replace("\\", "/")
                            if rel not in existing:
                                all_java.append(rel)
                                existing.add(rel)
                        except ValueError:
                            pass
            except OSError:
                pass
            return  # use only first matching source root

    def _scan_java_file_for_entry_points(self, abs_path: Path, rel_path: str) -> list[EntryPoint]:
        try:
            if abs_path.stat().st_size > _MAX_FILE_SIZE:
                return []
            content = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []

        # Quick pre-filter before running regexes
        has_controller = "Controller" in content
        has_filter = "Filter" in content
        has_security = "WebSecurityConfigurerAdapter" in content or "SecurityFilterChain" in content or "EnableWebSecurity" in content
        has_once_filter = "OncePerRequestFilter" in content or "GenericFilterBean" in content
        has_jax_rs = "@Path" in content or "@GET" in content or "@POST" in content or "@Provider" in content
        has_cdi = "ApplicationScoped" in content or "RequestScoped" in content or "@Singleton" in content
        has_spi = "implements" in content and any(k in content for k in (
            "EventListenerProvider", "RealmResourceProvider", "AuthenticatorFactory",
            "ProtocolMapper", "CredentialProvider", "PolicyProvider",
            "RequiredActionProvider", "IdentityProviderMapper",
        ))
        if (not has_controller and not has_filter and not has_security
                and "ControllerAdvice" not in content
                and "M3FiltroSeguridad" not in content
                and not has_jax_rs and not has_cdi and not has_spi):
            return []

        if _REST_CONTROLLER_RE.search(content):
            http_path_match = _HTTP_PATH_RE.search(content)
            http_path = http_path_match.group(1) if http_path_match else None
            verb_match = _REQUEST_METHOD_VERB_RE.search(content)
            if verb_match and http_path:
                http_path = f"[{verb_match.group(1)}] {http_path}"
            elif verb_match:
                http_path = f"[{verb_match.group(1)}]"
            security_evidence = None
            m3_match = _M3_FILTRO_PARAMS_RE.search(content)
            if m3_match:
                nombre = m3_match.group(1) or m3_match.group(2) or ""
                nivel = m3_match.group(3) or ""
                security_evidence = f"@M3FiltroSeguridad(nombreRecurso={nombre!r}, nivelRequerido={nivel})"
            return [EntryPoint(
                path=rel_path, stack="java", kind="rest_controller",
                source="annotation", confidence="high",
                http_path=http_path,
                evidence=security_evidence,
            )]
        if _CONTROLLER_ADVICE_RE.search(content):
            return [EntryPoint(
                path=rel_path, stack="java", kind="exception_handler",
                source="annotation", confidence="medium",
            )]
        if _MVC_CONTROLLER_RE.search(content) and _REQUEST_MAPPING_RE.search(content):
            http_path_match = _HTTP_PATH_RE.search(content)
            http_path = http_path_match.group(1) if http_path_match else None
            verb_match = _REQUEST_METHOD_VERB_RE.search(content)
            if verb_match and http_path:
                http_path = f"[{verb_match.group(1)}] {http_path}"
            elif verb_match:
                http_path = f"[{verb_match.group(1)}]"
            security_evidence = None
            m3_match = _M3_FILTRO_PARAMS_RE.search(content)
            if m3_match:
                nombre = m3_match.group(1) or m3_match.group(2) or ""
                nivel = m3_match.group(3) or ""
                security_evidence = f"@M3FiltroSeguridad(nombreRecurso={nombre!r}, nivelRequerido={nivel})"
            return [EntryPoint(
                path=rel_path, stack="java", kind="mvc_controller",
                source="annotation", confidence="medium",
                http_path=http_path,
                evidence=security_evidence,
            )]
        if _WEB_FILTER_RE.search(content):
            return [EntryPoint(
                path=rel_path, stack="java", kind="filter",
                source="annotation", confidence="high",
            )]
        if _FILTER_BEAN_RE.search(content):
            return [EntryPoint(
                path=rel_path, stack="java", kind="filter",
                source="annotation", confidence="medium",
            )]
        if has_security and (
            _WEB_SECURITY_CONFIGURER_RE.search(content)
            or _SECURITY_CONFIG_ANNOTATION_RE.search(content)
            or _SECURITY_FILTER_CHAIN_RE.search(content)
        ):
            return [EntryPoint(
                path=rel_path, stack="java", kind="security_config",
                source="annotation", confidence="high",
                evidence="Spring Security configuration",
            )]
        if has_once_filter and _ONCE_PER_REQUEST_FILTER_RE.search(content):
            is_jwt = bool(_JWT_FILTER_KEYWORDS_RE.search(content))
            return [EntryPoint(
                path=rel_path, stack="java", kind="security_filter",
                source="annotation", confidence="high",
                evidence="jwt_filter" if is_jwt else "request_filter",
            )]

        # --- JAX-RS resource class ---
        if has_jax_rs and _JAX_RS_PATH_RE.search(content) and _JAX_RS_VERB_RE.search(content):
            path_m = _JAX_RS_PATH_RE.search(content)
            verb_m = _JAX_RS_VERB_RE.search(content)
            http_path: str | None = None
            if path_m and verb_m:
                http_path = f"[{verb_m.group(1)}] {path_m.group(1)}"
            elif path_m:
                http_path = path_m.group(1)
            return [EntryPoint(
                path=rel_path, stack="java", kind="jax_rs_controller",
                source="annotation", confidence="high",
                http_path=http_path,
            )]
        if has_jax_rs and _JAX_RS_PROVIDER_RE.search(content):
            return [EntryPoint(
                path=rel_path, stack="java", kind="jax_rs_provider",
                source="annotation", confidence="high",
                evidence="@Provider",
            )]

        # --- Keycloak / Quarkus SPI implementation ---
        if has_spi:
            spi_m = _SPI_IMPL_RE.search(content)
            if spi_m:
                return [EntryPoint(
                    path=rel_path, stack="java", kind="spi_provider",
                    source="annotation", confidence="high",
                    evidence=f"SPI:{spi_m.group(1)}",
                )]

        # --- CDI / Jakarta EE scoped bean ---
        if has_cdi and _CDI_SCOPED_RE.search(content):
            return [EntryPoint(
                path=rel_path, stack="java", kind="cdi_bean",
                source="annotation", confidence="medium",
            )]

        return []

    def _parse_web_xml(self, abs_path: Path, rel_path: str) -> list[EntryPoint]:
        try:
            tree = ElementTree.parse(abs_path)
        except (OSError, ElementTree.ParseError):
            return []

        root = tree.getroot()
        ns_match = re.match(r"\{[^}]+\}", root.tag)
        ns = ns_match.group(0) if ns_match else ""

        results: list[EntryPoint] = []
        if root.findall(f".//{ns}servlet-class"):
            results.append(EntryPoint(
                path=rel_path, stack="java", kind="servlet",
                source="xml_config", confidence="high",
            ))
        if root.findall(f".//{ns}filter-class"):
            results.append(EntryPoint(
                path=rel_path, stack="java", kind="filter",
                source="xml_config", confidence="high",
            ))
        return results

    def _dedupe_frameworks(self, frameworks: list[FrameworkDetection]) -> list[FrameworkDetection]:
        seen: set[str] = set()
        result: list[FrameworkDetection] = []
        for framework in frameworks:
            if framework.name not in seen:
                seen.add(framework.name)
                result.append(framework)
        return result

    def _collect_transactional_classes(self, context: DetectionContext, all_paths: list[str]) -> list[str]:
        """Scan Java source files for @Transactional and return unique class names."""
        classes: list[str] = []
        seen: set[str] = set()
        java_paths = [p for p in all_paths if p.endswith(".java") and "/test/" not in p and "/tests/" not in p]
        for rel_path in java_paths[:_MAX_JAVA_ENTRY_SCAN]:
            abs_path = context.root / rel_path
            try:
                if abs_path.stat().st_size > _MAX_FILE_SIZE:
                    continue
                content = abs_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if not _TRANSACTIONAL_RE.search(content):
                continue
            m = _CLASS_NAME_RE.search(content)
            if m:
                cls = m.group(1)
                if cls not in seen:
                    seen.add(cls)
                    classes.append(cls)
        return classes

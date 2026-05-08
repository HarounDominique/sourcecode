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
_MAX_ANNOTATION_ENTRY_POINTS = 500

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
    r'@M3FiltroSeguridad\s*\(\s*(?:nombreRecurso\s*=\s*"([^"]*)")?'
    r'(?:[^)]*nivelRequerido\s*=\s*(\d+))?'
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

        # Detect app server from descriptor files
        all_paths = flatten_file_tree(context.file_tree)
        if any("weblogic.xml" in p or "weblogic-ejb-jar.xml" in p for p in all_paths):
            app_server_hint = "weblogic"
        elif any("wildfly" in p.lower() or "jboss" in p.lower() for p in all_paths):
            app_server_hint = "wildfly"

        # Spring profiles — check src/main/options/, src/main/resources/
        spring_profiles = self._detect_spring_profiles(context.root, all_paths)

        entry_points = self._collect_entry_points(context)
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
        content = "\n".join(read_text_lines(path)).lower()
        return self._detect_jvm_frameworks(content, "build.gradle")

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
        return frameworks

    def _collect_entry_points(self, context: DetectionContext) -> list[EntryPoint]:
        all_paths = flatten_file_tree(context.file_tree)
        all_java = [p for p in all_paths if p.endswith(".java")]

        # 1. @SpringBootApplication entry: Application.java / Main.java by name
        app_candidates = [
            p for p in all_java
            if p.endswith(("Application.java", "Main.java"))
        ]
        entry_points: list[EntryPoint] = [
            EntryPoint(path=p, stack="java", kind="application", source="manifest")
            for p in unique_strings(app_candidates)
        ]

        # 2. Annotation-based scan: @RestController, @WebFilter, FilterRegistrationBean
        scan_candidates = [
            p for p in all_java
            if "/test/" not in p and "/tests/" not in p
        ][:_MAX_JAVA_ENTRY_SCAN]

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

        # Deduplicate by (path, kind)
        seen: set[tuple[str, str]] = set()
        unique_eps: list[EntryPoint] = []
        for ep in entry_points:
            key = (ep.path, ep.kind)
            if key not in seen:
                seen.add(key)
                unique_eps.append(ep)
        return unique_eps

    def _scan_java_file_for_entry_points(self, abs_path: Path, rel_path: str) -> list[EntryPoint]:
        try:
            if abs_path.stat().st_size > _MAX_FILE_SIZE:
                return []
            content = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []

        # Quick pre-filter before running regexes
        if ("Controller" not in content and "Filter" not in content
                and "ControllerAdvice" not in content
                and "M3FiltroSeguridad" not in content):
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
                nombre = m3_match.group(1) or ""
                nivel = m3_match.group(2) or ""
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
                nombre = m3_match.group(1) or ""
                nivel = m3_match.group(2) or ""
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

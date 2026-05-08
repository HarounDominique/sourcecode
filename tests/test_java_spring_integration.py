"""Integration tests for Java/Spring detection fixes.

Covers 6 critical failures against tests/fixtures/spring_boot_minimal/:
  FALLO 1 — @RestController detected as rest_controller with http_path
  FALLO 2 — MyBatis detected in frameworks and file_relevance
  FALLO 3 — DDD layout detected (pattern=ddd, confidence=high)
  FALLO 4 — file_relevance uses Spring stereotype table scores
  FALLO 5 — Spring profiles and custom YAML properties in env_map
  FALLO 6 — prepare-context why field populated for Java stereotypes
"""
from __future__ import annotations

from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "spring_boot_minimal"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _detect(fixture: Path):
    from sourcecode.detectors.java import JavaDetector
    from sourcecode.detectors.base import DetectionContext

    file_tree = _build_file_tree(fixture)
    ctx = DetectionContext(
        root=fixture,
        file_tree=file_tree,
        manifests=["pom.xml"],
        manifest_types={"pom.xml": "maven"},
    )
    return JavaDetector().detect(ctx)


def _entry_points(fixture: Path):
    stacks, eps = _detect(fixture)
    return eps


def _frameworks(fixture: Path):
    stacks, eps = _detect(fixture)
    return [f.name for s in stacks for f in s.frameworks]


# ---------------------------------------------------------------------------
# FALLO 1 — @RestController entry points
# ---------------------------------------------------------------------------

class TestRestControllerEntryPoints:
    def test_rest_controller_kind(self):
        eps = _entry_points(FIXTURE)
        kinds = {ep.kind for ep in eps}
        assert "rest_controller" in kinds, f"Expected rest_controller, got: {kinds}"

    def test_rest_controller_confidence_high(self):
        eps = _entry_points(FIXTURE)
        rest = [ep for ep in eps if ep.kind == "rest_controller"]
        assert rest, "No rest_controller entry points found"
        assert all(ep.confidence == "high" for ep in rest)

    def test_rest_controller_http_path_extracted(self):
        eps = _entry_points(FIXTURE)
        rest = [ep for ep in eps if ep.kind == "rest_controller"]
        paths_found = [ep.http_path for ep in rest if ep.http_path]
        assert paths_found, "No http_path extracted from @RequestMapping/@GetMapping"

    def test_multiple_rest_controllers_detected(self):
        eps = _entry_points(FIXTURE)
        rest = [ep for ep in eps if ep.kind == "rest_controller"]
        # 1 in demo/web + 5 in ddd modules = 6 total
        assert len(rest) >= 6, f"Expected ≥6 rest_controller entries, got {len(rest)}"

    def test_no_http_handler_kind(self):
        """Old 'http_handler' kind must not appear; replaced by rest_controller."""
        eps = _entry_points(FIXTURE)
        kinds = {ep.kind for ep in eps}
        assert "http_handler" not in kinds, "Deprecated http_handler kind still present"


# ---------------------------------------------------------------------------
# FALLO 2 — MyBatis detection
# ---------------------------------------------------------------------------

class TestMyBatisDetection:
    def test_mybatis_in_frameworks(self):
        fws = _frameworks(FIXTURE)
        assert "MyBatis" in fws, f"MyBatis not in frameworks: {fws}"

    def test_mapper_file_in_file_relevance(self):
        from sourcecode.file_classifier import FileClassifier
        file_tree = _build_file_tree(FIXTURE)
        all_paths = _all_paths(file_tree)
        classifier = FileClassifier(FIXTURE, [])

        mapper_path = next(
            (p for p in all_paths if "HealthMapper.java" in p), None
        )
        assert mapper_path is not None, "HealthMapper.java not found in file tree"

        fc = classifier.classify(mapper_path)
        assert fc is not None, "FileClassifier returned None for @Mapper file"
        assert fc.category == "data_access", f"Expected data_access, got {fc.category}"
        assert abs(fc.relevance - 0.65) < 0.01, f"Expected relevance 0.65, got {fc.relevance}"

    def test_mybatis_why_text(self):
        from sourcecode.file_classifier import FileClassifier
        file_tree = _build_file_tree(FIXTURE)
        all_paths = _all_paths(file_tree)
        classifier = FileClassifier(FIXTURE, [])

        mapper_path = next(p for p in all_paths if "HealthMapper.java" in p)
        fc = classifier.classify(mapper_path)
        assert "MyBatis" in fc.reason, f"Expected 'MyBatis' in reason, got: {fc.reason}"


# ---------------------------------------------------------------------------
# FALLO 3 — DDD architecture detection
# ---------------------------------------------------------------------------

class TestDDDDetection:
    def _arch(self):
        from sourcecode.architecture_analyzer import ArchitectureAnalyzer
        from sourcecode.schema import SourceMap, AnalysisMetadata
        file_tree = _build_file_tree(FIXTURE)
        all_paths = _all_paths(file_tree)
        sm = SourceMap(
            metadata=AnalysisMetadata(analyzed_path=str(FIXTURE)),
            file_tree=file_tree,
        )
        sm.file_paths = all_paths
        return ArchitectureAnalyzer().analyze(FIXTURE, sm)

    def test_pattern_is_ddd(self):
        arch = self._arch()
        assert arch.pattern == "ddd", f"Expected ddd, got {arch.pattern}"

    def test_confidence_high(self):
        arch = self._arch()
        assert arch.confidence == "high", f"Expected high, got {arch.confidence}"

    def test_bounded_contexts_present(self):
        arch = self._arch()
        bc_names = {bc.name for bc in arch.bounded_contexts}
        assert "ausente" in bc_names, f"ausente not in bounded_contexts: {bc_names}"
        assert len(bc_names) >= 5, f"Expected ≥5 bounded contexts, got {len(bc_names)}"

    def test_ddd_layers_detected(self):
        arch = self._arch()
        assert set(arch.ddd_layers_detected) == {"application", "domain", "infrastructure"}

    def test_layers_in_output(self):
        arch = self._arch()
        layer_names = {l.name for l in arch.layers}
        assert {"application", "domain", "infrastructure"} <= layer_names


# ---------------------------------------------------------------------------
# FALLO 4 — file_relevance stereotype scoring
# ---------------------------------------------------------------------------

class TestStereotypeScoring:
    def _classify(self, filename: str):
        from sourcecode.file_classifier import FileClassifier
        file_tree = _build_file_tree(FIXTURE)
        all_paths = _all_paths(file_tree)
        classifier = FileClassifier(FIXTURE, [])
        path = next((p for p in all_paths if filename in p), None)
        assert path is not None, f"{filename} not found in fixture"
        return classifier.classify(path)

    def test_rest_controller_relevance(self):
        fc = self._classify("HealthRestController.java")
        assert fc is not None
        assert fc.category == "api_endpoint"
        assert abs(fc.relevance - 0.90) < 0.01

    def test_transactional_service_relevance(self):
        fc = self._classify("HealthService.java")
        assert fc is not None
        assert fc.category == "business_logic"
        assert abs(fc.relevance - 0.75) < 0.01

    def test_repository_relevance(self):
        fc = self._classify("HealthRepository.java")
        assert fc is not None
        assert fc.category == "data_access"
        assert abs(fc.relevance - 0.65) < 0.01

    def test_entity_relevance(self):
        fc = self._classify("Health.java")
        assert fc is not None
        assert fc.category == "domain_model"
        assert abs(fc.relevance - 0.50) < 0.01

    def test_why_field_not_empty(self):
        fc = self._classify("HealthRestController.java")
        assert fc.reason, "Reason field must not be empty"
        assert "REST" in fc.reason or "HTTP" in fc.reason or "controller" in fc.reason.lower()


# ---------------------------------------------------------------------------
# FALLO 5 — Spring profiles and custom YAML properties
# ---------------------------------------------------------------------------

class TestSpringEnvMap:
    def _env(self):
        from sourcecode.env_analyzer import EnvAnalyzer
        file_tree = _build_file_tree(FIXTURE)
        return EnvAnalyzer().analyze(FIXTURE, file_tree)

    def test_spring_profiles_populated(self):
        _, summary = self._env()
        assert summary.spring_profiles, "spring_profiles must not be empty"
        assert "dev" in summary.spring_profiles

    def test_profiles_scanned_includes_default(self):
        _, summary = self._env()
        assert "default" in summary.profiles_scanned

    def test_custom_yml_properties_in_env_map(self):
        records, _ = self._env()
        keys = {r.key for r in records}
        assert any(k.startswith("saint.") for k in keys), \
            f"Expected saint.* properties, got keys: {sorted(keys)[:20]}"

    def test_custom_yml_properties_category_application(self):
        records, _ = self._env()
        saint_records = [r for r in records if r.key.startswith("saint.")]
        assert saint_records, "No saint.* records found"
        assert all(r.category == "application" for r in saint_records), \
            f"Expected category=application for saint.* keys"

    def test_custom_yml_properties_source_yml_property(self):
        records, _ = self._env()
        saint_records = [r for r in records if r.key.startswith("saint.")]
        assert all(r.source == "yml_property" for r in saint_records)

    def test_standard_env_vars_still_detected(self):
        records, _ = self._env()
        keys = {r.key for r in records}
        assert "SPRING_DATASOURCE_URL" in keys or any("DATASOURCE" in k for k in keys), \
            "Standard Spring env vars must still be detected"

    def test_ldap_password_required(self):
        records, _ = self._env()
        ldap_pwd = next((r for r in records if r.key == "LDAP_PASSWORD"), None)
        assert ldap_pwd is not None, "LDAP_PASSWORD not found in env_map"
        assert ldap_pwd.required is True, "LDAP_PASSWORD should be required (no default)"


# ---------------------------------------------------------------------------
# FALLO 6 — prepare-context why field
# ---------------------------------------------------------------------------

class TestPrepareContextWhy:
    def test_why_field_populated_for_rest_controller(self):
        from sourcecode.prepare_context import _java_why
        from sourcecode.file_classifier import FileClassifier
        file_tree = _build_file_tree(FIXTURE)
        all_paths = _all_paths(file_tree)
        classifier = FileClassifier(FIXTURE, [])

        ctrl_path = next(p for p in all_paths if "HealthRestController.java" in p)
        fc = classifier.classify(ctrl_path)
        why = _java_why(ctrl_path, fc)
        assert why, "why field must not be empty for @RestController"
        assert "endpoint" in why.lower() or "HTTP" in why or "domain" in why.lower()

    def test_why_field_populated_for_service(self):
        from sourcecode.prepare_context import _java_why
        from sourcecode.file_classifier import FileClassifier
        file_tree = _build_file_tree(FIXTURE)
        all_paths = _all_paths(file_tree)
        classifier = FileClassifier(FIXTURE, [])

        svc_path = next(p for p in all_paths if "HealthService.java" in p)
        fc = classifier.classify(svc_path)
        why = _java_why(svc_path, fc)
        assert why, "why field must not be empty for @Service"
        assert "logic" in why.lower() or "service" in why.lower() or "business" in why.lower()

    def test_why_field_empty_for_non_java(self):
        from sourcecode.prepare_context import _java_why
        why = _java_why("pom.xml", None)
        assert why == ""

    def test_ddd_why_contains_domain_name(self):
        from sourcecode.prepare_context import _java_why
        from sourcecode.file_classifier import FileClassifier
        file_tree = _build_file_tree(FIXTURE)
        all_paths = _all_paths(file_tree)
        classifier = FileClassifier(FIXTURE, [])

        # ausente DDD controller
        ctrl_path = next(
            p for p in all_paths if "ausente" in p and "RestController.java" in p
        )
        fc = classifier.classify(ctrl_path)
        why = _java_why(ctrl_path, fc)
        assert "ausente" in why, f"Expected domain 'ausente' in why, got: {why}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _all_paths(file_tree: dict) -> list[str]:
    from sourcecode.tree_utils import flatten_file_tree
    return flatten_file_tree(file_tree)


def _build_file_tree(fixture: Path) -> dict:
    from sourcecode.adaptive_scanner import AdaptiveScanner
    scanner = AdaptiveScanner(fixture, base_depth=12)
    return scanner.scan_tree()

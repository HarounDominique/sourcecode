"""Tests for migrate_check — Spring Boot 2→3 migration readiness scanner."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from sourcecode.migrate_check import (
    MigrationFinding,
    MigrationReport,
    _scan_file,
    _scan_xml_file,
    _scan_dep_file,
    _find_xml_config_files,
    _find_build_files,
    _is_spring_xml_candidate,
    _ALL_RULES,
    _XML_RULES,
    _DEP_RULES,
    run_migrate_check,
    SEVERITY_ORDER,
)

FIXTURES = Path(__file__).parent / "fixtures" / "javax_legacy"


# ---------------------------------------------------------------------------
# Unit: _scan_file
# ---------------------------------------------------------------------------

class TestScanFile:
    def test_detects_javax_persistence(self) -> None:
        source = "import javax.persistence.Entity;\nimport javax.persistence.Id;\n"
        findings = _scan_file(source, "Entity.java", _ALL_RULES)
        rule_ids = [f.rule_id for f in findings]
        assert "MIG-001" in rule_ids

    def test_detects_javax_persistence_wildcard(self) -> None:
        source = "import javax.persistence.*;\n"
        findings = _scan_file(source, "Repo.java", _ALL_RULES)
        assert any(f.rule_id == "MIG-001" for f in findings)

    def test_detects_javax_servlet(self) -> None:
        source = "import javax.servlet.http.HttpServletRequest;\n"
        findings = _scan_file(source, "Filter.java", _ALL_RULES)
        assert any(f.rule_id == "MIG-002" for f in findings)

    def test_detects_javax_validation(self) -> None:
        source = "import javax.validation.constraints.NotNull;\n"
        findings = _scan_file(source, "Dto.java", _ALL_RULES)
        assert any(f.rule_id == "MIG-003" for f in findings)

    def test_detects_javax_transaction(self) -> None:
        source = "import javax.transaction.Transactional;\n"
        findings = _scan_file(source, "Svc.java", _ALL_RULES)
        assert any(f.rule_id == "MIG-004" for f in findings)

    def test_detects_websecurityconfigureradapter(self) -> None:
        source = "public class Cfg extends WebSecurityConfigurerAdapter {\n}\n"
        findings = _scan_file(source, "Cfg.java", _ALL_RULES)
        assert any(f.rule_id == "MIG-005" for f in findings)

    def test_detects_javax_annotation(self) -> None:
        source = "import javax.annotation.PostConstruct;\n"
        findings = _scan_file(source, "Bean.java", _ALL_RULES)
        assert any(f.rule_id == "MIG-006" for f in findings)

    def test_detects_javax_inject(self) -> None:
        source = "import javax.inject.Inject;\n"
        findings = _scan_file(source, "Svc.java", _ALL_RULES)
        assert any(f.rule_id == "MIG-007" for f in findings)

    def test_detects_javax_ws_rs(self) -> None:
        source = "import javax.ws.rs.GET;\nimport javax.ws.rs.Path;\n"
        findings = _scan_file(source, "Resource.java", _ALL_RULES)
        assert any(f.rule_id == "MIG-008" for f in findings)

    def test_no_false_positive_jakarta(self) -> None:
        source = "import jakarta.persistence.Entity;\nimport jakarta.servlet.http.HttpServletRequest;\n"
        findings = _scan_file(source, "Clean.java", _ALL_RULES)
        assert findings == []

    def test_no_false_positive_extends_non_target(self) -> None:
        source = "public class Foo extends SomeOtherAdapter {\n}\n"
        findings = _scan_file(source, "Foo.java", _ALL_RULES)
        assert not any(f.rule_id == "MIG-005" for f in findings)

    def test_first_line_is_accurate(self) -> None:
        source = "package com.example;\n\nimport javax.persistence.Entity;\n"
        findings = _scan_file(source, "E.java", _ALL_RULES)
        mig001 = next(f for f in findings if f.rule_id == "MIG-001")
        assert mig001.first_line == 3

    def test_imports_found_list_populated(self) -> None:
        source = "import javax.persistence.Entity;\nimport javax.persistence.Id;\n"
        findings = _scan_file(source, "E.java", _ALL_RULES)
        mig001 = next(f for f in findings if f.rule_id == "MIG-001")
        assert len(mig001.imports_found) == 2

    def test_one_finding_per_rule_per_file(self) -> None:
        # Even with 5 javax.persistence imports, only 1 MIG-001 finding per file
        source = "\n".join(
            f"import javax.persistence.Cls{i};"
            for i in range(5)
        )
        findings = _scan_file(source, "Multi.java", _ALL_RULES)
        mig001_findings = [f for f in findings if f.rule_id == "MIG-001"]
        assert len(mig001_findings) == 1


# ---------------------------------------------------------------------------
# Unit: MigrationFinding.make_id determinism
# ---------------------------------------------------------------------------

class TestMigrationFindingId:
    def test_deterministic(self) -> None:
        id1 = MigrationFinding.make_id("MIG-001", "com/example/Foo.java")
        id2 = MigrationFinding.make_id("MIG-001", "com/example/Foo.java")
        assert id1 == id2

    def test_different_rule_different_id(self) -> None:
        id1 = MigrationFinding.make_id("MIG-001", "Foo.java")
        id2 = MigrationFinding.make_id("MIG-002", "Foo.java")
        assert id1 != id2

    def test_different_file_different_id(self) -> None:
        id1 = MigrationFinding.make_id("MIG-001", "Foo.java")
        id2 = MigrationFinding.make_id("MIG-001", "Bar.java")
        assert id1 != id2


# ---------------------------------------------------------------------------
# Unit: MigrationReport.finalize
# ---------------------------------------------------------------------------

_RULE_TARGET = {r.id: r.migration_target for r in _ALL_RULES}


class TestMigrationReportFinalize:
    def _make_finding(self, rule_id: str, severity: str, source_file: str) -> MigrationFinding:
        return MigrationFinding(
            id=MigrationFinding.make_id(rule_id, source_file),
            rule_id=rule_id,
            severity=severity,
            title="test",
            source_file=source_file,
            first_line=1,
            # Use the rule's real migration_target so framework-vs-JDK scoring
            # (BUG #4/#6) behaves as it does on real findings.
            migration_target=_RULE_TARGET.get(rule_id, "jakarta"),
        )

    def test_perfect_score_when_no_findings(self) -> None:
        report = MigrationReport().finalize()
        assert report.readiness_score == 100
        assert report.blocking_count == 0
        assert report.estimated_effort_days == 0.0

    def test_score_deducts_by_file_not_by_finding(self) -> None:
        # Same file, two critical rules → counted as 1 critical file
        f1 = self._make_finding("MIG-001", "critical", "UserEntity.java")
        f2 = self._make_finding("MIG-004", "high", "UserEntity.java")
        report = MigrationReport(findings=[f1, f2]).finalize()
        # 1 critical file (-15) + 1 high file (-8) = -23 → 77
        assert report.readiness_score == 77

    def test_score_floor_at_zero(self) -> None:
        findings = [
            self._make_finding("MIG-001", "critical", f"File{i}.java")
            for i in range(20)
        ]
        report = MigrationReport(findings=findings).finalize()
        assert report.readiness_score == 0

    def test_low_severity_deduction_is_capped(self) -> None:
        # BUG #6: best-practice hygiene (MIG-016 java.util.Date → java.time) blocks
        # no version upgrade and is EXCLUDED from readiness entirely — surfaced as a
        # separate hygiene metric. 96 Date findings must NOT dent the headline.
        findings = [
            self._make_finding("MIG-016", "low", f"File{i}.java")
            for i in range(96)
        ]
        report = MigrationReport(findings=findings).finalize()
        assert report.blocking_count == 0
        assert report.readiness_score == 100
        assert report.hygiene_findings == 96

    def test_non_best_practice_low_findings_are_capped(self) -> None:
        # G-1 cap still applies to framework low-severity findings (not hygiene):
        # 96 low jakarta files would deduct 96 uncapped; capped at 15 → 85.
        findings = [
            self._make_finding("MIG-007", "low", f"File{i}.java")  # jakarta target
            for i in range(96)
        ]
        report = MigrationReport(findings=findings).finalize()
        assert report.blocking_count == 0
        assert report.readiness_score == 85

    def test_blockers_still_floor_score_with_low_findings_present(self) -> None:
        # The low-severity cap must not shield a genuinely blocked repo.
        findings = [
            self._make_finding("MIG-001", "critical", f"Blk{i}.java")
            for i in range(20)
        ] + [
            self._make_finding("MIG-016", "low", f"Adv{i}.java")
            for i in range(50)
        ]
        report = MigrationReport(findings=findings).finalize()
        assert report.blocking_count == 20
        assert report.readiness_score == 0

    def test_blocking_count_sums_critical_and_high(self) -> None:
        findings = [
            self._make_finding("MIG-001", "critical", "A.java"),
            self._make_finding("MIG-002", "high", "B.java"),
            self._make_finding("MIG-006", "medium", "C.java"),
        ]
        report = MigrationReport(findings=findings).finalize()
        assert report.blocking_count == 2

    def test_summary_populated(self) -> None:
        f = self._make_finding("MIG-001", "critical", "E.java")
        report = MigrationReport(findings=[f]).finalize()
        assert report.summary["total_findings"] == 1
        assert report.summary["affected_files"] == 1
        assert report.summary["by_severity"]["critical"] == 1

    def test_generated_at_set(self) -> None:
        report = MigrationReport().finalize()
        assert report.generated_at != ""


# ---------------------------------------------------------------------------
# Integration: run_migrate_check on fixture files
# ---------------------------------------------------------------------------

class TestRunMigrateCheckFixture:
    def test_fixture_files_found(self) -> None:
        java_files = list(FIXTURES.glob("*.java"))
        assert len(java_files) >= 4, "Fixture directory must have at least 4 .java files"

    def test_detects_mig001_in_user_entity(self) -> None:
        file_paths = ["UserEntity.java"]
        report = run_migrate_check(file_paths, FIXTURES)
        assert any(f.rule_id == "MIG-001" for f in report.findings)

    def test_detects_mig002_in_controller(self) -> None:
        file_paths = ["UserController.java"]
        report = run_migrate_check(file_paths, FIXTURES)
        assert any(f.rule_id == "MIG-002" for f in report.findings)

    def test_detects_mig003_in_user_entity(self) -> None:
        file_paths = ["UserEntity.java"]
        report = run_migrate_check(file_paths, FIXTURES)
        assert any(f.rule_id == "MIG-003" for f in report.findings)

    def test_detects_mig004_in_transaction_service(self) -> None:
        file_paths = ["TransactionService.java"]
        report = run_migrate_check(file_paths, FIXTURES)
        assert any(f.rule_id == "MIG-004" for f in report.findings)

    def test_detects_mig005_in_security_config(self) -> None:
        file_paths = ["OldSecurityConfig.java"]
        report = run_migrate_check(file_paths, FIXTURES)
        assert any(f.rule_id == "MIG-005" for f in report.findings)

    def test_detects_mig006_and_mig007_in_inject_service(self) -> None:
        file_paths = ["InjectService.java"]
        report = run_migrate_check(file_paths, FIXTURES)
        rule_ids = {f.rule_id for f in report.findings}
        assert "MIG-006" in rule_ids
        assert "MIG-007" in rule_ids

    def test_full_fixture_scan_score_below_100(self) -> None:
        file_paths = [p.name for p in FIXTURES.glob("*.java")]
        report = run_migrate_check(file_paths, FIXTURES)
        assert report.readiness_score < 100
        assert report.blocking_count > 0

    def test_min_severity_high_excludes_medium(self) -> None:
        file_paths = [p.name for p in FIXTURES.glob("*.java")]
        report = run_migrate_check(file_paths, FIXTURES, min_severity="high")
        for f in report.findings:
            assert SEVERITY_ORDER[f.severity] <= SEVERITY_ORDER["high"]

    def test_min_severity_critical_only(self) -> None:
        file_paths = [p.name for p in FIXTURES.glob("*.java")]
        report = run_migrate_check(file_paths, FIXTURES, min_severity="critical")
        for f in report.findings:
            assert f.severity == "critical"

    def test_report_to_dict_keys(self) -> None:
        file_paths = ["UserEntity.java"]
        report = run_migrate_check(file_paths, FIXTURES)
        d = report.to_dict()
        for key in ("schema_version", "readiness_score", "blocking_count",
                    "estimated_effort_days", "findings", "summary", "metadata"):
            assert key in d

    def test_report_to_text_contains_score(self) -> None:
        file_paths = ["UserEntity.java"]
        report = run_migrate_check(file_paths, FIXTURES)
        text = report.to_text()
        assert "Migration Readiness:" in text
        assert "MIG-001" in text

    def test_empty_repo_returns_perfect_score(self) -> None:
        report = run_migrate_check([], FIXTURES)
        assert report.readiness_score == 100
        assert report.findings == []


# ---------------------------------------------------------------------------
# New rules: Jakarta MIG-009
# ---------------------------------------------------------------------------

class TestJakartaJms:
    def test_detects_javax_jms(self) -> None:
        source = "import javax.jms.MessageListener;\nimport javax.jms.Message;\n"
        findings = _scan_file(source, "Listener.java", _ALL_RULES)
        assert any(f.rule_id == "MIG-009" for f in findings)

    def test_no_false_positive_jakarta_jms(self) -> None:
        source = "import jakarta.jms.MessageListener;\nimport jakarta.jms.Message;\n"
        findings = _scan_file(source, "Listener.java", _ALL_RULES)
        assert not any(f.rule_id == "MIG-009" for f in findings)

    def test_mig009_fixture(self) -> None:
        report = run_migrate_check(["JmsListener.java"], FIXTURES)
        assert any(f.rule_id == "MIG-009" for f in report.findings)

    def test_mig009_migration_target(self) -> None:
        source = "import javax.jms.Message;\n"
        findings = _scan_file(source, "Msg.java", _ALL_RULES)
        mig = next(f for f in findings if f.rule_id == "MIG-009")
        assert mig.migration_target == "jakarta"

    def test_mig009_has_openrewrite_recipe(self) -> None:
        source = "import javax.jms.Message;\n"
        findings = _scan_file(source, "Msg.java", _ALL_RULES)
        mig = next(f for f in findings if f.rule_id == "MIG-009")
        assert mig.openrewrite_recipe is not None
        assert "Jms" in mig.openrewrite_recipe


# ---------------------------------------------------------------------------
# New rules: Spring Security 6 MIG-020 / MIG-019
# ---------------------------------------------------------------------------

class TestSpringSecuritySix:
    def test_detects_ant_matchers(self) -> None:
        source = "http.antMatchers(\"/public/**\").permitAll();\n"
        findings = _scan_file(source, "Sec.java", _ALL_RULES)
        assert any(f.rule_id == "MIG-020" for f in findings)

    def test_detects_authorize_requests(self) -> None:
        source = "http.authorizeRequests().anyRequest().authenticated();\n"
        findings = _scan_file(source, "Sec.java", _ALL_RULES)
        assert any(f.rule_id == "MIG-020" for f in findings)

    def test_detects_authentication_manager_builder(self) -> None:
        source = (
            "public void configure(AuthenticationManagerBuilder auth) throws Exception {\n"
            "    auth.userDetailsService(userDetailsService);\n"
            "}\n"
        )
        findings = _scan_file(source, "Sec.java", _ALL_RULES)
        assert any(f.rule_id == "MIG-020" for f in findings)

    def test_mig020_detected_alongside_mig005(self) -> None:
        report = run_migrate_check(["OldSecurityConfig.java"], FIXTURES)
        rule_ids = {f.rule_id for f in report.findings}
        assert "MIG-005" in rule_ids
        assert "MIG-020" in rule_ids

    def test_mig020_migration_target_spring_security_6(self) -> None:
        source = "http.antMatchers(\"/api/**\");\n"
        findings = _scan_file(source, "S.java", _ALL_RULES)
        mig = next(f for f in findings if f.rule_id == "MIG-020")
        assert mig.migration_target == "spring_security_6"

    def test_detects_springfox_import(self) -> None:
        source = "import springfox.documentation.spring.web.plugins.Docket;\n"
        findings = _scan_file(source, "Cfg.java", _ALL_RULES)
        assert any(f.rule_id == "MIG-019" for f in findings)

    def test_detects_enable_swagger2(self) -> None:
        source = "@EnableSwagger2\npublic class SwaggerConfig {}\n"
        findings = _scan_file(source, "Cfg.java", _ALL_RULES)
        assert any(f.rule_id == "MIG-019" for f in findings)

    def test_mig019_fixture(self) -> None:
        report = run_migrate_check(["SwaggerConfig.java"], FIXTURES)
        assert any(f.rule_id == "MIG-019" for f in report.findings)

    def test_no_false_positive_springdoc(self) -> None:
        source = "import org.springdoc.core.models.GroupedOpenApi;\n"
        findings = _scan_file(source, "Cfg.java", _ALL_RULES)
        assert not any(f.rule_id == "MIG-019" for f in findings)


# ---------------------------------------------------------------------------
# New rules: Java 11 MIG-021 / MIG-022
# ---------------------------------------------------------------------------

class TestJava11RemovedApis:
    def test_detects_jaxb_import(self) -> None:
        source = "import javax.xml.bind.JAXBContext;\n"
        findings = _scan_file(source, "Svc.java", _ALL_RULES)
        assert any(f.rule_id == "MIG-021" for f in findings)

    def test_detects_jaxb_wildcard(self) -> None:
        source = "import javax.xml.bind.*;\n"
        findings = _scan_file(source, "Svc.java", _ALL_RULES)
        assert any(f.rule_id == "MIG-021" for f in findings)

    def test_mig021_fixture(self) -> None:
        report = run_migrate_check(["JaxbService.java"], FIXTURES)
        assert any(f.rule_id == "MIG-021" for f in report.findings)

    def test_mig021_migration_target_java_11(self) -> None:
        source = "import javax.xml.bind.JAXBContext;\n"
        findings = _scan_file(source, "S.java", _ALL_RULES)
        mig = next(f for f in findings if f.rule_id == "MIG-021")
        assert mig.migration_target == "java_11"

    def test_detects_jaxws_import(self) -> None:
        source = "import javax.xml.ws.Service;\n"
        findings = _scan_file(source, "Svc.java", _ALL_RULES)
        assert any(f.rule_id == "MIG-022" for f in findings)

    def test_mig022_fixture(self) -> None:
        report = run_migrate_check(["JaxWsService.java"], FIXTURES)
        assert any(f.rule_id == "MIG-022" for f in report.findings)

    def test_no_false_positive_javax_xml_parsers(self) -> None:
        source = "import javax.xml.parsers.DocumentBuilder;\n"
        findings = _scan_file(source, "P.java", _ALL_RULES)
        assert not any(f.rule_id in ("MIG-021", "MIG-022") for f in findings)


# ---------------------------------------------------------------------------
# New rules: Java 15 MIG-012 (Nashorn)
# ---------------------------------------------------------------------------

class TestNashornRemoved:
    def test_detects_jdk_nashorn_import(self) -> None:
        source = "import jdk.nashorn.api.scripting.NashornScriptEngine;\n"
        findings = _scan_file(source, "Svc.java", _ALL_RULES)
        assert any(f.rule_id == "MIG-012" for f in findings)

    def test_detects_get_engine_by_name_nashorn(self) -> None:
        source = 'ScriptEngine e = new ScriptEngineManager().getEngineByName("nashorn");\n'
        findings = _scan_file(source, "Svc.java", _ALL_RULES)
        assert any(f.rule_id == "MIG-012" for f in findings)

    def test_mig012_fixture(self) -> None:
        report = run_migrate_check(["NashornScriptService.java"], FIXTURES)
        assert any(f.rule_id == "MIG-012" for f in report.findings)

    def test_mig012_migration_target_java_15(self) -> None:
        source = "import jdk.nashorn.api.scripting.NashornScriptEngine;\n"
        findings = _scan_file(source, "S.java", _ALL_RULES)
        mig = next(f for f in findings if f.rule_id == "MIG-012")
        assert mig.migration_target == "java_15"

    def test_no_false_positive_rhino(self) -> None:
        source = "import org.mozilla.javascript.Context;\n"
        findings = _scan_file(source, "S.java", _ALL_RULES)
        assert not any(f.rule_id == "MIG-012" for f in findings)


# ---------------------------------------------------------------------------
# New rules: Java 17 MIG-010 (SecurityManager)
# ---------------------------------------------------------------------------

class TestSecurityManagerRemoved:
    def test_detects_get_security_manager(self) -> None:
        source = "SecurityManager sm = System.getSecurityManager();\n"
        findings = _scan_file(source, "Svc.java", _ALL_RULES)
        assert any(f.rule_id == "MIG-010" for f in findings)

    def test_detects_set_security_manager(self) -> None:
        source = "System.setSecurityManager(new MySecurityManager());\n"
        findings = _scan_file(source, "Svc.java", _ALL_RULES)
        assert any(f.rule_id == "MIG-010" for f in findings)

    def test_detects_security_manager_variable(self) -> None:
        source = "SecurityManager sm = null;\n"
        findings = _scan_file(source, "Svc.java", _ALL_RULES)
        assert any(f.rule_id == "MIG-010" for f in findings)

    def test_detects_access_controller(self) -> None:
        source = "AccessController.doPrivileged((PrivilegedAction<Void>) () -> null);\n"
        findings = _scan_file(source, "Svc.java", _ALL_RULES)
        assert any(f.rule_id == "MIG-010" for f in findings)

    def test_mig010_fixture(self) -> None:
        report = run_migrate_check(["SecurityManagerService.java"], FIXTURES)
        assert any(f.rule_id == "MIG-010" for f in report.findings)

    def test_mig010_severity_critical(self) -> None:
        source = "SecurityManager sm = System.getSecurityManager();\n"
        findings = _scan_file(source, "S.java", _ALL_RULES)
        mig = next(f for f in findings if f.rule_id == "MIG-010")
        assert mig.severity == "critical"

    def test_mig010_migration_target_java_17(self) -> None:
        source = "System.getSecurityManager();\n"
        findings = _scan_file(source, "S.java", _ALL_RULES)
        mig = next(f for f in findings if f.rule_id == "MIG-010")
        assert mig.migration_target == "java_17"


# ---------------------------------------------------------------------------
# New rules: Java 9+ MIG-011 / MIG-013 / MIG-014
# ---------------------------------------------------------------------------

class TestJava9StrongEncapsulation:
    def test_detects_sun_misc_import(self) -> None:
        source = "import sun.misc.BASE64Encoder;\n"
        findings = _scan_file(source, "Svc.java", _ALL_RULES)
        assert any(f.rule_id == "MIG-011" for f in findings)

    def test_detects_com_sun_net_import(self) -> None:
        source = "import com.sun.net.httpserver.HttpServer;\n"
        findings = _scan_file(source, "Svc.java", _ALL_RULES)
        assert any(f.rule_id == "MIG-011" for f in findings)

    def test_mig011_fixture(self) -> None:
        report = run_migrate_check(["InternalApiService.java"], FIXTURES)
        assert any(f.rule_id == "MIG-011" for f in report.findings)

    def test_no_false_positive_com_sun_jna(self) -> None:
        source = "import com.sun.jna.Pointer;\n"
        findings = _scan_file(source, "S.java", _ALL_RULES)
        assert not any(f.rule_id == "MIG-011" for f in findings)

    def test_no_false_positive_com_sun_jersey(self) -> None:
        source = "import com.sun.jersey.api.client.WebResource;\n"
        findings = _scan_file(source, "S.java", _ALL_RULES)
        assert not any(f.rule_id == "MIG-011" for f in findings)

    def test_detects_sun_misc_unsafe_import(self) -> None:
        source = "import sun.misc.Unsafe;\n"
        findings = _scan_file(source, "Svc.java", _ALL_RULES)
        assert any(f.rule_id == "MIG-013" for f in findings)

    def test_detects_unsafe_get_unsafe_code(self) -> None:
        source = "private static final Unsafe UNSAFE = Unsafe.getUnsafe();\n"
        findings = _scan_file(source, "Svc.java", _ALL_RULES)
        assert any(f.rule_id == "MIG-013" for f in findings)

    def test_detects_the_unsafe_reflection(self) -> None:
        source = 'Field f = Unsafe.class.getDeclaredField("theUnsafe");\n'
        findings = _scan_file(source, "Svc.java", _ALL_RULES)
        assert any(f.rule_id == "MIG-013" for f in findings)

    def test_mig013_fixture(self) -> None:
        report = run_migrate_check(["UnsafeService.java"], FIXTURES)
        assert any(f.rule_id == "MIG-013" for f in report.findings)

    def test_detects_set_accessible_true(self) -> None:
        source = "field.setAccessible(true);\n"
        findings = _scan_file(source, "Svc.java", _ALL_RULES)
        assert any(f.rule_id == "MIG-014" for f in findings)

    def test_no_false_positive_set_accessible_false(self) -> None:
        source = "field.setAccessible(false);\n"
        findings = _scan_file(source, "Svc.java", _ALL_RULES)
        assert not any(f.rule_id == "MIG-014" for f in findings)

    def test_mig014_fixture(self) -> None:
        report = run_migrate_check(["ReflectionService.java"], FIXTURES)
        assert any(f.rule_id == "MIG-014" for f in report.findings)

    def test_mig014_migration_target(self) -> None:
        source = "field.setAccessible(true);\n"
        findings = _scan_file(source, "S.java", _ALL_RULES)
        mig = next(f for f in findings if f.rule_id == "MIG-014")
        assert mig.migration_target == "java_9_plus"


# ---------------------------------------------------------------------------
# New rules: Java 18+ MIG-015 (finalize)
# ---------------------------------------------------------------------------

class TestFinalizeDeprecated:
    def test_detects_protected_finalize(self) -> None:
        source = "@Override\nprotected void finalize() throws Throwable {\n    super.finalize();\n}\n"
        findings = _scan_file(source, "Bean.java", _ALL_RULES)
        assert any(f.rule_id == "MIG-015" for f in findings)

    def test_detects_public_finalize(self) -> None:
        source = "public void finalize() throws Throwable {}\n"
        findings = _scan_file(source, "Bean.java", _ALL_RULES)
        assert any(f.rule_id == "MIG-015" for f in findings)

    def test_no_false_positive_other_void_method(self) -> None:
        source = "protected void initialize() throws Throwable {}\n"
        findings = _scan_file(source, "Bean.java", _ALL_RULES)
        assert not any(f.rule_id == "MIG-015" for f in findings)

    def test_mig015_fixture(self) -> None:
        report = run_migrate_check(["LegacyResourceBean.java"], FIXTURES)
        assert any(f.rule_id == "MIG-015" for f in report.findings)

    def test_mig015_severity_medium(self) -> None:
        source = "protected void finalize() throws Throwable {}\n"
        findings = _scan_file(source, "B.java", _ALL_RULES)
        mig = next(f for f in findings if f.rule_id == "MIG-015")
        assert mig.severity == "medium"


# ---------------------------------------------------------------------------
# New rules: Legacy date/time MIG-016
# ---------------------------------------------------------------------------

class TestLegacyDateApi:
    def test_detects_util_date_import(self) -> None:
        source = "import java.util.Date;\n"
        findings = _scan_file(source, "Svc.java", _ALL_RULES)
        assert any(f.rule_id == "MIG-016" for f in findings)

    def test_detects_calendar_import(self) -> None:
        source = "import java.util.Calendar;\n"
        findings = _scan_file(source, "Svc.java", _ALL_RULES)
        assert any(f.rule_id == "MIG-016" for f in findings)

    def test_detects_simple_date_format(self) -> None:
        source = "import java.text.SimpleDateFormat;\n"
        findings = _scan_file(source, "Svc.java", _ALL_RULES)
        assert any(f.rule_id == "MIG-016" for f in findings)

    def test_detects_gregorian_calendar(self) -> None:
        source = "import java.util.GregorianCalendar;\n"
        findings = _scan_file(source, "Svc.java", _ALL_RULES)
        assert any(f.rule_id == "MIG-016" for f in findings)

    def test_no_false_positive_java_time(self) -> None:
        source = "import java.time.LocalDate;\nimport java.time.ZonedDateTime;\n"
        findings = _scan_file(source, "Svc.java", _ALL_RULES)
        assert not any(f.rule_id == "MIG-016" for f in findings)

    def test_mig016_fixture(self) -> None:
        report = run_migrate_check(["DateService.java"], FIXTURES)
        assert any(f.rule_id == "MIG-016" for f in report.findings)

    def test_mig016_severity_low(self) -> None:
        source = "import java.util.Date;\n"
        findings = _scan_file(source, "S.java", _ALL_RULES)
        mig = next(f for f in findings if f.rule_id == "MIG-016")
        assert mig.severity == "low"

    def test_mig016_has_openrewrite_recipe(self) -> None:
        source = "import java.util.Date;\n"
        findings = _scan_file(source, "S.java", _ALL_RULES)
        mig = next(f for f in findings if f.rule_id == "MIG-016")
        assert mig.openrewrite_recipe is not None


# ---------------------------------------------------------------------------
# Cross-cutting: migration_target + openrewrite_recipe in output
# ---------------------------------------------------------------------------

class TestFindingMetadata:
    def test_finding_to_dict_includes_migration_target(self) -> None:
        source = "import javax.persistence.Entity;\n"
        findings = _scan_file(source, "E.java", _ALL_RULES)
        d = findings[0].to_dict()
        assert "migration_target" in d

    def test_finding_to_dict_includes_openrewrite_recipe_when_present(self) -> None:
        source = "import javax.persistence.Entity;\n"
        findings = _scan_file(source, "E.java", _ALL_RULES)
        d = findings[0].to_dict()
        assert "openrewrite_recipe" in d
        assert d["openrewrite_recipe"] is not None

    def test_finding_to_dict_omits_openrewrite_when_none(self) -> None:
        source = "SecurityManager sm = System.getSecurityManager();\n"
        findings = _scan_file(source, "S.java", _ALL_RULES)
        mig = next(f for f in findings if f.rule_id == "MIG-010")
        d = mig.to_dict()
        assert "openrewrite_recipe" not in d

    def test_report_summary_has_by_migration_target(self) -> None:
        source = "import javax.persistence.Entity;\n"
        from pathlib import Path
        report = run_migrate_check(["UserEntity.java"], FIXTURES)
        assert "by_migration_target" in report.summary
        assert "jakarta" in report.summary["by_migration_target"]

    def test_to_text_includes_migration_target(self) -> None:
        source = "import javax.persistence.Entity;\n"
        report = run_migrate_check(["UserEntity.java"], FIXTURES)
        text = report.to_text()
        assert "jakarta" in text or "spring_boot_3" in text

    def test_report_limitations_not_empty(self) -> None:
        report = run_migrate_check([], FIXTURES)
        assert len(report.limitations) > 0

    def test_all_rules_have_migration_target(self) -> None:
        for rule in _ALL_RULES:
            assert rule.migration_target, f"{rule.id} missing migration_target"

    def test_all_rule_ids_unique(self) -> None:
        ids = [r.id for r in _ALL_RULES]
        assert len(ids) == len(set(ids)), "Duplicate rule IDs detected"


# ---------------------------------------------------------------------------
# Integration: full fixture scan with new rules
# ---------------------------------------------------------------------------

class TestFullFixtureScanExtended:
    def test_all_new_fixtures_have_findings(self) -> None:
        new_fixtures = [
            "JmsListener.java",
            "SecurityManagerService.java",
            "InternalApiService.java",
            "NashornScriptService.java",
            "UnsafeService.java",
            "ReflectionService.java",
            "LegacyResourceBean.java",
            "DateService.java",
            "SwaggerConfig.java",
            "JaxbService.java",
            "JaxWsService.java",
        ]
        for fname in new_fixtures:
            report = run_migrate_check([fname], FIXTURES)
            assert report.findings, f"{fname} produced no findings"

    def test_full_scan_has_by_target_breakdown(self) -> None:
        all_files = [p.name for p in FIXTURES.glob("*.java")]
        report = run_migrate_check(all_files, FIXTURES)
        targets = report.summary["by_migration_target"]
        assert "jakarta" in targets
        assert "java_17" in targets
        assert "java_9_plus" in targets

    def test_clean_file_no_findings(self) -> None:
        source = (
            "package com.example.modern;\n"
            "import jakarta.persistence.Entity;\n"
            "import jakarta.servlet.http.HttpServletRequest;\n"
            "import java.time.LocalDate;\n"
            "@Entity\npublic class Modern {}\n"
        )
        findings = _scan_file(source, "Modern.java", _ALL_RULES)
        assert findings == []


# ---------------------------------------------------------------------------
# New Java source rules: CORBA, Thread, ReflectionFactory
# ---------------------------------------------------------------------------

class TestCorbaRule:
    def test_detects_org_omg_import(self) -> None:
        source = "import org.omg.CORBA.Object;\nimport org.omg.PortableServer.Servant;\n"
        findings = _scan_file(source, "CORBAService.java", _ALL_RULES)
        assert any(f.rule_id == "MIG-023" for f in findings)

    def test_detects_javax_rmi_corba(self) -> None:
        source = "import javax.rmi.CORBA.Tie;\n"
        findings = _scan_file(source, "RMIService.java", _ALL_RULES)
        assert any(f.rule_id == "MIG-023" for f in findings)

    def test_no_fp_javax_annotation(self) -> None:
        source = "import javax.annotation.PostConstruct;\n"
        findings = _scan_file(source, "Bean.java", _ALL_RULES)
        assert not any(f.rule_id == "MIG-023" for f in findings)

    def test_no_fp_jakarta(self) -> None:
        source = "import jakarta.enterprise.context.ApplicationScoped;\n"
        findings = _scan_file(source, "Bean.java", _ALL_RULES)
        assert not any(f.rule_id == "MIG-023" for f in findings)

    def test_severity_is_critical(self) -> None:
        source = "import org.omg.CORBA.Object;\n"
        findings = _scan_file(source, "C.java", _ALL_RULES)
        corba = next(f for f in findings if f.rule_id == "MIG-023")
        assert corba.severity == "critical"

    def test_migration_target_java_11(self) -> None:
        source = "import org.omg.CosNaming.NamingContext;\n"
        findings = _scan_file(source, "C.java", _ALL_RULES)
        corba = next(f for f in findings if f.rule_id == "MIG-023")
        assert corba.migration_target == "java_11"


class TestThreadRule:
    def test_detects_thread_stop(self) -> None:
        source = "Thread myThread = new Thread(r);\nmyThread.stop();\n"
        findings = _scan_file(source, "X.java", _ALL_RULES)
        assert any(f.rule_id == "MIG-024" for f in findings)

    def test_detects_worker_thread_suspend(self) -> None:
        source = "workerThread.suspend();\n"
        findings = _scan_file(source, "X.java", _ALL_RULES)
        assert any(f.rule_id == "MIG-024" for f in findings)

    def test_detects_thread_currentthread_stop(self) -> None:
        source = "Thread.currentThread().stop();\n"
        findings = _scan_file(source, "X.java", _ALL_RULES)
        assert any(f.rule_id == "MIG-024" for f in findings)

    def test_no_fp_server_stop(self) -> None:
        source = "server.stop();\nservice.stop();\n"
        findings = _scan_file(source, "X.java", _ALL_RULES)
        assert not any(f.rule_id == "MIG-024" for f in findings)

    def test_no_fp_thread_pool_stop(self) -> None:
        source = "threadPool.stop();\nexecutorPool.stop();\n"
        findings = _scan_file(source, "X.java", _ALL_RULES)
        assert not any(f.rule_id == "MIG-024" for f in findings)

    def test_detects_resume_method(self) -> None:
        source = "backgroundThread.resume();\n"
        findings = _scan_file(source, "X.java", _ALL_RULES)
        assert any(f.rule_id == "MIG-024" for f in findings)


class TestReflectionFactoryRule:
    def test_detects_import(self) -> None:
        source = "import sun.reflect.ReflectionFactory;\n"
        findings = _scan_file(source, "R.java", _ALL_RULES)
        assert any(f.rule_id == "MIG-025" for f in findings)

    def test_detects_getreflectionfactory_call(self) -> None:
        source = "ReflectionFactory rf = ReflectionFactory.getReflectionFactory();\n"
        findings = _scan_file(source, "R.java", _ALL_RULES)
        assert any(f.rule_id == "MIG-025" for f in findings)

    def test_detects_private_lookup_in(self) -> None:
        source = "MethodHandles.privateLookupIn(Foo.class, MethodHandles.lookup());\n"
        findings = _scan_file(source, "R.java", _ALL_RULES)
        assert any(f.rule_id == "MIG-025" for f in findings)

    def test_severity_is_medium(self) -> None:
        source = "import sun.reflect.ReflectionFactory;\n"
        findings = _scan_file(source, "R.java", _ALL_RULES)
        r25 = next(f for f in findings if f.rule_id == "MIG-025")
        assert r25.severity == "medium"


# ---------------------------------------------------------------------------
# XML scanning rules
# ---------------------------------------------------------------------------

class TestXmlCandidateFilter:
    def test_web_xml_matches(self) -> None:
        assert _is_spring_xml_candidate("web.xml")

    def test_applicationContext_matches(self) -> None:
        assert _is_spring_xml_candidate("applicationContext.xml")

    def test_applicationContext_dash_matches(self) -> None:
        assert _is_spring_xml_candidate("applicationContext-servlet.xml")

    def test_security_xml_matches(self) -> None:
        assert _is_spring_xml_candidate("bl-applicationContext-test-security.xml")

    def test_spring_context_matches(self) -> None:
        assert _is_spring_xml_candidate("spring-context.xml")

    def test_pom_xml_not_candidate(self) -> None:
        assert not _is_spring_xml_candidate("pom.xml")

    def test_random_xml_not_candidate(self) -> None:
        assert not _is_spring_xml_candidate("report.xml")
        assert not _is_spring_xml_candidate("checkstyle.xml")


class TestXmlRule030:
    """MIG-030: javax.* class attribute in Spring XML bean definitions."""

    def test_detects_class_javax_persistence(self) -> None:
        xml = '<bean class="javax.persistence.EntityManagerFactory"/>'
        findings = _scan_xml_file(xml, "appCtx.xml")
        assert any(f.rule_id == "MIG-030" for f in findings)

    def test_detects_class_javax_validation(self) -> None:
        xml = '<bean class="javax.validation.Validator"/>'
        findings = _scan_xml_file(xml, "appCtx.xml")
        assert any(f.rule_id == "MIG-030" for f in findings)

    def test_detects_type_javax(self) -> None:
        xml = '<property type="javax.transaction.UserTransaction" />'
        findings = _scan_xml_file(xml, "appCtx.xml")
        assert any(f.rule_id == "MIG-030" for f in findings)

    def test_no_fp_jakarta_class(self) -> None:
        xml = '<bean class="jakarta.persistence.EntityManagerFactory"/>'
        findings = _scan_xml_file(xml, "appCtx.xml")
        assert not any(f.rule_id == "MIG-030" for f in findings)

    def test_no_fp_spring_class(self) -> None:
        xml = '<bean class="org.springframework.orm.jpa.LocalContainerEntityManagerFactoryBean"/>'
        findings = _scan_xml_file(xml, "appCtx.xml")
        assert not any(f.rule_id == "MIG-030" for f in findings)

    def test_severity_high(self) -> None:
        xml = '<bean class="javax.validation.Validator"/>'
        findings = _scan_xml_file(xml, "appCtx.xml")
        r = next(f for f in findings if f.rule_id == "MIG-030")
        assert r.severity == "high"


class TestXmlRule031:
    """MIG-031: Spring Security XML old auto-config or old schema."""

    def test_detects_auto_config_true(self) -> None:
        xml = '<sec:http auto-config="true"><sec:intercept-url /></sec:http>'
        findings = _scan_xml_file(xml, "security.xml")
        assert any(f.rule_id == "MIG-031" for f in findings)

    def test_detects_security_namespace_auto_config(self) -> None:
        xml = '<security:http auto-config="true">'
        findings = _scan_xml_file(xml, "security.xml")
        assert any(f.rule_id == "MIG-031" for f in findings)

    def test_detects_old_security_xsd_version3(self) -> None:
        xml = 'xsi:schemaLocation="... spring-security-3.2.xsd"'
        findings = _scan_xml_file(xml, "security.xml")
        assert any(f.rule_id == "MIG-031" for f in findings)

    def test_detects_old_security_xsd_version5(self) -> None:
        xml = 'http://www.springframework.org/schema/security/spring-security-5.8.xsd'
        findings = _scan_xml_file(xml, "security.xml")
        assert any(f.rule_id == "MIG-031" for f in findings)

    def test_no_fp_modern_security_no_version(self) -> None:
        xml = 'xsi:schemaLocation="... spring-security.xsd"'
        findings = _scan_xml_file(xml, "security.xml")
        assert not any(f.rule_id == "MIG-031" for f in findings)

    def test_openrewrite_recipe_present(self) -> None:
        xml = '<sec:http auto-config="true"/>'
        findings = _scan_xml_file(xml, "security.xml")
        r = next(f for f in findings if f.rule_id == "MIG-031")
        assert r.openrewrite_recipe is not None


class TestXmlRule032:
    """MIG-032: web.xml with old javax servlet namespace."""

    def test_detects_java_sun_namespace(self) -> None:
        xml = 'xmlns="http://java.sun.com/xml/ns/javaee"'
        findings = _scan_xml_file(xml, "web.xml")
        assert any(f.rule_id == "MIG-032" for f in findings)

    def test_detects_xmlns_jcp_namespace(self) -> None:
        xml = 'xmlns="http://xmlns.jcp.org/xml/ns/javaee"'
        findings = _scan_xml_file(xml, "web.xml")
        assert any(f.rule_id == "MIG-032" for f in findings)

    def test_no_fp_jakarta_namespace(self) -> None:
        xml = 'xmlns="https://jakarta.ee/xml/ns/jakartaee"'
        findings = _scan_xml_file(xml, "web.xml")
        assert not any(f.rule_id == "MIG-032" for f in findings)

    def test_severity_high(self) -> None:
        xml = 'xmlns="http://java.sun.com/xml/ns/javaee"'
        findings = _scan_xml_file(xml, "web.xml")
        r = next(f for f in findings if f.rule_id == "MIG-032")
        assert r.severity == "high"

    def test_first_line_accurate(self) -> None:
        xml = '<?xml version="1.0"?>\n<web-app xmlns="http://java.sun.com/xml/ns/javaee">'
        findings = _scan_xml_file(xml, "web.xml")
        r = next(f for f in findings if f.rule_id == "MIG-032")
        assert r.first_line == 2


# ---------------------------------------------------------------------------
# Dependency scanning rules
# ---------------------------------------------------------------------------

class TestDepRule040SpringFox:
    def test_maven_detects_springfox(self) -> None:
        pom = "<groupId>io.springfox</groupId>\n<artifactId>springfox-swagger2</artifactId>"
        findings = _scan_dep_file(pom, "pom.xml")
        assert any(f.rule_id == "MIG-040" for f in findings)

    def test_gradle_detects_springfox(self) -> None:
        gradle = "implementation 'io.springfox:springfox-swagger2:3.0.0'"
        findings = _scan_dep_file(gradle, "build.gradle")
        assert any(f.rule_id == "MIG-040" for f in findings)

    def test_no_fp_springdoc(self) -> None:
        pom = "<groupId>org.springdoc</groupId>\n<artifactId>springdoc-openapi</artifactId>"
        findings = _scan_dep_file(pom, "pom.xml")
        assert not any(f.rule_id == "MIG-040" for f in findings)

    def test_severity_high(self) -> None:
        pom = "<groupId>io.springfox</groupId>"
        findings = _scan_dep_file(pom, "pom.xml")
        r = next(f for f in findings if f.rule_id == "MIG-040")
        assert r.severity == "high"


class TestDepRule041Hibernate5:
    def test_maven_detects_hibernate5(self) -> None:
        pom = (
            "<dependency>\n"
            "  <groupId>org.hibernate</groupId>\n"
            "  <artifactId>hibernate-core</artifactId>\n"
            "  <version>5.6.15.Final</version>\n"
            "</dependency>"
        )
        findings = _scan_dep_file(pom, "pom.xml")
        assert any(f.rule_id == "MIG-041" for f in findings)

    def test_gradle_detects_hibernate5(self) -> None:
        gradle = "implementation 'org.hibernate:hibernate-core:5.6.15.Final'"
        findings = _scan_dep_file(gradle, "build.gradle")
        assert any(f.rule_id == "MIG-041" for f in findings)

    def test_no_fp_hibernate6(self) -> None:
        pom = (
            "<dependency>\n"
            "  <groupId>org.hibernate</groupId>\n"
            "  <artifactId>hibernate-core</artifactId>\n"
            "  <version>6.4.4.Final</version>\n"
            "</dependency>"
        )
        findings = _scan_dep_file(pom, "pom.xml")
        assert not any(f.rule_id == "MIG-041" for f in findings)

    def test_no_fp_hibernate_core_jakarta(self) -> None:
        # hibernate-core-jakarta (a transitional artifact) should not match
        pom = (
            "<dependency>\n"
            "  <artifactId>hibernate-core-jakarta</artifactId>\n"
            "  <version>5.6.15.Final</version>\n"
            "</dependency>"
        )
        findings = _scan_dep_file(pom, "pom.xml")
        assert not any(f.rule_id == "MIG-041" for f in findings)


class TestDepRule042ByteBuddy:
    def test_maven_detects_bytebuddy_111(self) -> None:
        pom = (
            "<dependency>\n"
            "  <groupId>net.bytebuddy</groupId>\n"
            "  <artifactId>byte-buddy</artifactId>\n"
            "  <version>1.11.22</version>\n"
            "</dependency>"
        )
        findings = _scan_dep_file(pom, "pom.xml")
        assert any(f.rule_id == "MIG-042" for f in findings)

    def test_maven_detects_bytebuddy_single_digit_minor(self) -> None:
        pom = (
            "<dependency>\n"
            "  <groupId>net.bytebuddy</groupId>\n"
            "  <artifactId>byte-buddy</artifactId>\n"
            "  <version>1.9.0</version>\n"
            "</dependency>"
        )
        findings = _scan_dep_file(pom, "pom.xml")
        assert any(f.rule_id == "MIG-042" for f in findings)

    def test_no_fp_bytebuddy_114(self) -> None:
        pom = (
            "<dependency>\n"
            "  <groupId>net.bytebuddy</groupId>\n"
            "  <artifactId>byte-buddy</artifactId>\n"
            "  <version>1.14.3</version>\n"
            "</dependency>"
        )
        findings = _scan_dep_file(pom, "pom.xml")
        assert not any(f.rule_id == "MIG-042" for f in findings)


class TestDepRule043EhCache2:
    def test_maven_detects_ehcache2(self) -> None:
        pom = "<groupId>net.sf.ehcache</groupId>\n<artifactId>ehcache</artifactId>"
        findings = _scan_dep_file(pom, "pom.xml")
        assert any(f.rule_id == "MIG-043" for f in findings)

    def test_no_fp_ehcache3(self) -> None:
        pom = "<groupId>org.ehcache</groupId>\n<artifactId>ehcache</artifactId>"
        findings = _scan_dep_file(pom, "pom.xml")
        assert not any(f.rule_id == "MIG-043" for f in findings)


# ---------------------------------------------------------------------------
# run_migrate_check integration with XML + dep scanning
# ---------------------------------------------------------------------------

class TestRunMigrateCheckXmlDep:
    def test_xml_and_dep_scanned_in_metadata(self, tmp_path: Path) -> None:
        # Create a minimal repo with one Java file, one web.xml, one pom.xml
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "Main.java").write_text(
            "import javax.persistence.Entity;\npublic class Main {}\n"
        )
        web_xml = (
            '<?xml version="1.0"?>\n'
            '<web-app xmlns="http://java.sun.com/xml/ns/javaee" version="3.1">'
            '</web-app>'
        )
        webapp = tmp_path / "src" / "main" / "webapp" / "WEB-INF"
        webapp.mkdir(parents=True)
        (webapp / "web.xml").write_text(web_xml)
        (tmp_path / "pom.xml").write_text(
            "<project><groupId>io.springfox</groupId></project>"
        )

        report = run_migrate_check(["src/Main.java"], tmp_path)

        assert report.metadata["xml_files_scanned"] >= 1
        assert report.metadata["build_files_scanned"] >= 1
        rule_ids = {f.rule_id for f in report.findings}
        assert "MIG-001" in rule_ids   # javax.persistence in Java file
        assert "MIG-032" in rule_ids   # old namespace in web.xml
        assert "MIG-040" in rule_ids   # springfox in pom.xml

    def test_schema_version_is_1_4(self, tmp_path: Path) -> None:
        report = run_migrate_check([], tmp_path)
        assert report.schema_version == "1.4"

    def test_to_dict_contains_schema_version(self, tmp_path: Path) -> None:
        report = run_migrate_check([], tmp_path)
        d = report.to_dict()
        assert d["schema_version"] == "1.4"

    def test_modern_repo_still_100(self, tmp_path: Path) -> None:
        # A clean Spring Boot 3 repo: Jakarta imports, modern namespace
        (tmp_path / "Main.java").write_text(
            "import jakarta.persistence.Entity;\npublic class Main {}\n"
        )
        modern_pom = (
            "<project>\n"
            "  <parent>\n"
            "    <groupId>org.springframework.boot</groupId>\n"
            "    <artifactId>spring-boot-starter-parent</artifactId>\n"
            "    <version>3.2.0</version>\n"
            "  </parent>\n"
            "</project>\n"
        )
        (tmp_path / "pom.xml").write_text(modern_pom)
        report = run_migrate_check(["Main.java"], tmp_path)
        assert report.readiness_score == 100
        assert report.findings == []


# ---------------------------------------------------------------------------
# auto_fix_available field in to_dict
# ---------------------------------------------------------------------------

class TestAutoFixAvailable:
    def test_rule_with_recipe_has_auto_fix_true(self) -> None:
        f = MigrationFinding(
            id="x", rule_id="MIG-001", severity="critical", title="t",
            source_file="F.java", first_line=1,
            openrewrite_recipe="org.openrewrite.java.migrate.jakarta.JavaxPersistenceToJakartaPersistence",
        )
        d = f.to_dict()
        assert d["auto_fix_available"] is True
        assert "manual_migration" not in d

    def test_rule_without_recipe_has_auto_fix_false(self) -> None:
        f = MigrationFinding(
            id="x", rule_id="MIG-010", severity="critical", title="t",
            source_file="F.java", first_line=1,
        )
        d = f.to_dict()
        assert d["auto_fix_available"] is False
        assert d["manual_migration"] is True

    def test_mig015_finalize_has_recipe(self) -> None:
        source = "protected void finalize() throws Throwable {}"
        findings = _scan_file(source, "X.java", _ALL_RULES)
        r = next(f for f in findings if f.rule_id == "MIG-015")
        assert r.openrewrite_recipe == "org.openrewrite.java.migrate.RemoveFinalizeMethod"
        assert r.to_dict()["auto_fix_available"] is True


class TestMavenPropertyResolution:
    """Regression tests for F-002: Maven property substitution not resolved.

    _scan_dep_file applied patterns to raw pom text, missing version references
    like ${hibernate.version} even when the property value was defined in the
    same <properties> block.
    """

    def test_mig041_detects_via_property_substitution(self) -> None:
        pom = """\
<?xml version="1.0"?>
<project>
  <properties>
    <hibernate.version>5.6.15.Final</hibernate.version>
  </properties>
  <dependencyManagement>
    <dependencies>
      <dependency>
        <groupId>org.hibernate</groupId>
        <artifactId>hibernate-core</artifactId>
        <version>${hibernate.version}</version>
      </dependency>
    </dependencies>
  </dependencyManagement>
</project>"""
        findings = _scan_dep_file(pom, "pom.xml")
        assert any(f.rule_id == "MIG-041" for f in findings), (
            "F-002 regression: MIG-041 not detected when version is a property reference"
        )

    def test_mig041_property_with_dotted_name(self) -> None:
        pom = """\
<project>
  <properties>
    <hibernate.core.version>5.4.0.Final</hibernate.core.version>
  </properties>
  <dependencies>
    <dependency>
      <groupId>org.hibernate</groupId>
      <artifactId>hibernate-core</artifactId>
      <version>${hibernate.core.version}</version>
    </dependency>
  </dependencies>
</project>"""
        findings = _scan_dep_file(pom, "pom.xml")
        assert any(f.rule_id == "MIG-041" for f in findings), (
            "F-002 regression: dotted property name not resolved"
        )

    def test_no_false_positive_hibernate6_via_property(self) -> None:
        pom = """\
<project>
  <properties>
    <hibernate.version>6.4.4.Final</hibernate.version>
  </properties>
  <dependencies>
    <dependency>
      <groupId>org.hibernate</groupId>
      <artifactId>hibernate-core</artifactId>
      <version>${hibernate.version}</version>
    </dependency>
  </dependencies>
</project>"""
        findings = _scan_dep_file(pom, "pom.xml")
        assert not any(f.rule_id == "MIG-041" for f in findings), (
            "F-002 regression: false positive on Hibernate 6 via property"
        )

    def test_springfox_detected_via_property(self) -> None:
        pom = """\
<project>
  <properties>
    <springfox.version>3.0.0</springfox.version>
  </properties>
  <dependencies>
    <dependency>
      <groupId>io.springfox</groupId>
      <artifactId>springfox-boot-starter</artifactId>
      <version>${springfox.version}</version>
    </dependency>
  </dependencies>
</project>"""
        findings = _scan_dep_file(pom, "pom.xml")
        springfox_found = any("springfox" in f.rule_id.lower() or "SpringFox" in f.title for f in findings)
        assert springfox_found, (
            "F-002 regression: SpringFox not detected when version is property reference"
        )

    def test_property_resolution_does_not_affect_gradle(self) -> None:
        """Gradle files must not be processed through Maven property resolution."""
        gradle = "implementation 'org.hibernate:hibernate-core:5.6.15.Final'"
        findings = _scan_dep_file(gradle, "build.gradle")
        assert any(f.rule_id == "MIG-041" for f in findings), (
            "Gradle detection broken after property resolution change"
        )

    def test_literal_version_still_detected(self) -> None:
        """Existing literal version detection must not regress."""
        pom = """\
<dependency>
  <groupId>org.hibernate</groupId>
  <artifactId>hibernate-core</artifactId>
  <version>5.6.15.Final</version>
</dependency>"""
        findings = _scan_dep_file(pom, "pom.xml")
        assert any(f.rule_id == "MIG-041" for f in findings), (
            "F-002 regression: literal version detection broke after property resolution"
        )


class TestMultiModuleDepDeduplication:
    """Regression tests for F-003: multi-module pom inflates dep finding counts.

    Same dependency (e.g. EhCache, SpringFox) declared in root pom + N child
    poms must produce exactly one finding, not N+1.
    """

    def _make_pom(self, tmp_path, modules):
        """Create multi-module Maven layout. modules is list of (subdir, pom_content)."""
        import tempfile
        root = tmp_path
        for subdir, content in modules:
            if subdir:
                (root / subdir).mkdir(parents=True, exist_ok=True)
                (root / subdir / "pom.xml").write_text(content)
            else:
                (root / "pom.xml").write_text(content)
        return root

    def test_ehcache_deduplicated_across_three_poms(self, tmp_path) -> None:
        ehcache_dep = """\
<dependency>
  <groupId>net.sf.ehcache</groupId>
  <artifactId>ehcache</artifactId>
  <version>2.10.9</version>
</dependency>"""
        pom = f"<project><dependencies>{ehcache_dep}</dependencies></project>"
        modules = [("", pom), ("api", pom), ("data", pom)]
        root = self._make_pom(tmp_path, modules)

        from sourcecode.migrate_check import run_migrate_check
        report = run_migrate_check([], root)
        ehcache = [f for f in report.findings if f.rule_id == "MIG-043"]
        assert len(ehcache) == 1, (
            f"F-003 regression: EhCache reported {len(ehcache)}x across 3 poms, expected 1"
        )

    def test_springfox_deduplicated_across_two_poms(self, tmp_path) -> None:
        springfox_dep = """\
<dependency>
  <groupId>io.springfox</groupId>
  <artifactId>springfox-boot-starter</artifactId>
  <version>3.0.0</version>
</dependency>"""
        pom = f"<project><dependencies>{springfox_dep}</dependencies></project>"
        modules = [("", pom), ("web", pom)]
        root = self._make_pom(tmp_path, modules)

        from sourcecode.migrate_check import run_migrate_check
        report = run_migrate_check([], root)
        springfox = [f for f in report.findings if f.rule_id == "MIG-040"]
        assert len(springfox) == 1, (
            f"F-003 regression: SpringFox reported {len(springfox)}x, expected 1"
        )

    def test_single_pom_still_detected(self, tmp_path) -> None:
        """Single pom with dependency must still produce one finding."""
        pom = """\
<project>
  <dependencies>
    <dependency>
      <groupId>net.sf.ehcache</groupId>
      <artifactId>ehcache</artifactId>
      <version>2.10.9</version>
    </dependency>
  </dependencies>
</project>"""
        (tmp_path / "pom.xml").write_text(pom)

        from sourcecode.migrate_check import run_migrate_check
        report = run_migrate_check([], tmp_path)
        ehcache = [f for f in report.findings if f.rule_id == "MIG-043"]
        assert len(ehcache) == 1, (
            f"F-003 regression: single pom now produces {len(ehcache)} findings"
        )

    def test_different_deps_each_appear_once(self, tmp_path) -> None:
        """Two different deps in the same pom must both appear exactly once."""
        pom = """\
<project>
  <dependencies>
    <dependency>
      <groupId>net.sf.ehcache</groupId>
      <artifactId>ehcache</artifactId>
      <version>2.10.9</version>
    </dependency>
    <dependency>
      <groupId>io.springfox</groupId>
      <artifactId>springfox-boot-starter</artifactId>
      <version>3.0.0</version>
    </dependency>
  </dependencies>
</project>"""
        (tmp_path / "pom.xml").write_text(pom)
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "pom.xml").write_text(pom)

        from sourcecode.migrate_check import run_migrate_check
        report = run_migrate_check([], tmp_path)
        ehcache = [f for f in report.findings if f.rule_id == "MIG-043"]
        springfox = [f for f in report.findings if f.rule_id == "MIG-040"]
        assert len(ehcache) == 1, f"EhCache: expected 1, got {len(ehcache)}"
        assert len(springfox) == 1, f"SpringFox: expected 1, got {len(springfox)}"


# ---------------------------------------------------------------------------
# BUG-1: Spring Boot version detection (tri-state, property-resolved, jakarta veto)
# ---------------------------------------------------------------------------

_BOOT3_NO_PARENT_POM = """\
<project>
  <properties>
    <spring.boot.version>3.5.14</spring.boot.version>
    <spring.version>6.2.18</spring.version>
  </properties>
  <dependencyManagement>
    <dependencies>
      <dependency>
        <groupId>org.springframework.boot</groupId>
        <artifactId>spring-boot-autoconfigure</artifactId>
        <version>${spring.boot.version}</version>
      </dependency>
    </dependencies>
  </dependencyManagement>
  <dependencies>
    <dependency>
      <groupId>javax.cache</groupId>
      <artifactId>cache-api</artifactId>
      <version>1.1.0</version>
    </dependency>
  </dependencies>
</project>"""

_BOOT2_PARENT_POM = """\
<project>
  <parent>
    <groupId>org.springframework.boot</groupId>
    <artifactId>spring-boot-starter-parent</artifactId>
    <version>2.7.18</version>
  </parent>
</project>"""


class TestSpringBootDetection:
    """BUG-1: never report spring_boot_2_detected=true without real Boot-2 evidence."""

    def test_boot3_no_starter_parent_version_by_property(self, tmp_path: Path) -> None:
        # Broadleaf shape: no <parent>, Boot version via ${spring.boot.version}.
        (tmp_path / "pom.xml").write_text(_BOOT3_NO_PARENT_POM)
        report = run_migrate_check([], tmp_path)
        assert report.spring_boot_2_detected is False
        assert report.spring_boot_version_detected == "3.5.14"

    def test_boot2_starter_parent_still_detected(self, tmp_path: Path) -> None:
        # Control: a genuine Boot 2 repo must still be flagged (no false negative).
        (tmp_path / "pom.xml").write_text(_BOOT2_PARENT_POM)
        report = run_migrate_check([], tmp_path)
        assert report.spring_boot_2_detected is True
        assert report.spring_boot_version_detected == "2.7.18"

    def test_unknown_when_no_boot_evidence(self, tmp_path: Path) -> None:
        # No Boot version anywhere, no jakarta imports → unknown, never True.
        (tmp_path / "pom.xml").write_text(
            "<project><dependencies></dependencies></project>"
        )
        report = run_migrate_check([], tmp_path)
        assert report.spring_boot_2_detected is None

    def test_stray_2x_library_version_does_not_imply_boot2(self, tmp_path: Path) -> None:
        # A non-Boot dependency pinned at 2.x must not be read as Boot 2.
        pom = """\
<project>
  <dependencies>
    <dependency>
      <groupId>commons-io</groupId>
      <artifactId>commons-io</artifactId>
      <version>2.11.0</version>
    </dependency>
  </dependencies>
</project>"""
        (tmp_path / "pom.xml").write_text(pom)
        report = run_migrate_check([], tmp_path)
        assert report.spring_boot_2_detected is not True

    def test_jakarta_imports_veto_boot2_verdict(self, tmp_path: Path) -> None:
        # Invariant 5: jakarta.persistence|jakarta.servlet imports > 0 ⇒ never True,
        # even if a Boot-2 property is present (mid-migration repo).
        (tmp_path / "pom.xml").write_text(
            "<project><properties>"
            "<spring.boot.version>2.7.18</spring.boot.version>"
            "</properties></project>"
        )
        (tmp_path / "Entity.java").write_text(
            "import jakarta.persistence.Entity;\n"
            "import jakarta.servlet.http.HttpServletRequest;\n"
        )
        report = run_migrate_check(["Entity.java"], tmp_path)
        assert report.spring_boot_2_detected is not True

    def test_jakarta_imports_imply_boot3_when_no_version(self, tmp_path: Path) -> None:
        (tmp_path / "pom.xml").write_text("<project></project>")
        (tmp_path / "Entity.java").write_text("import jakarta.persistence.Entity;\n")
        report = run_migrate_check(["Entity.java"], tmp_path)
        assert report.spring_boot_2_detected is False


# ---------------------------------------------------------------------------
# BUG-2: per-dimension readiness — JDK debt must not sink jakarta/Boot3 score
# ---------------------------------------------------------------------------

class TestDimensionalReadiness:
    @staticmethod
    def _f(rule_id: str, severity: str, target: str, src: str) -> MigrationFinding:
        return MigrationFinding(
            id=MigrationFinding.make_id(rule_id, src),
            rule_id=rule_id,
            severity=severity,
            title="t",
            source_file=src,
            first_line=1,
            migration_target=target,
        )

    def test_jdk_debt_does_not_collapse_aggregate(self) -> None:
        # Broadleaf shape: 0 jakarta findings, 1 high security blocker, plus heavy
        # JDK debt (144 low Date + 25 medium reflection). Aggregate must stay high.
        findings = [self._f("MIG-031", "high", "spring_security_6", "Sec.xml")]
        findings += [self._f("MIG-016", "low", "java_8_best_practice", f"D{i}.java")
                     for i in range(144)]
        findings += [self._f("MIG-014", "medium", "java_9_plus", f"R{i}.java")
                     for i in range(25)]
        report = MigrationReport(findings=findings).finalize()
        # readiness_score = min(applicable migration dims) = min(jakarta 100, boot3 92).
        # JDK debt is EXCLUDED from the aggregate entirely (orthogonal upkeep axis),
        # so it cannot collapse the headline — only the real Boot3 blocker counts.
        assert report.readiness_score == 92
        assert report.jakarta_readiness == 100        # namespace fully migrated
        assert report.boot3_readiness == 92            # only the one high blocker
        assert report.jdk_modernization < 100          # debt visible in its own axis
        assert report.readiness_aggregate["inputs"] == {"jakarta": 100, "boot3": 92}

    def test_real_jakarta_blockers_still_floor(self) -> None:
        findings = [self._f("MIG-001", "critical", "jakarta", f"E{i}.java")
                    for i in range(20)]
        report = MigrationReport(findings=findings).finalize()
        assert report.readiness_score == 0
        assert report.jakarta_readiness == 0
        assert report.boot3_readiness == 0

"""Tests for migrate_check — Spring Boot 2→3 migration readiness scanner."""
from __future__ import annotations

from pathlib import Path

import pytest

from sourcecode.migrate_check import (
    MigrationFinding,
    MigrationReport,
    _scan_file,
    _ALL_RULES,
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

class TestMigrationReportFinalize:
    def _make_finding(self, rule_id: str, severity: str, source_file: str) -> MigrationFinding:
        return MigrationFinding(
            id=MigrationFinding.make_id(rule_id, source_file),
            rule_id=rule_id,
            severity=severity,
            title="test",
            source_file=source_file,
            first_line=1,
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

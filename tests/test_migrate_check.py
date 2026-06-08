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

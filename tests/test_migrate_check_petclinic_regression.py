"""Golden regression: Spring Petclinic (Boot 4.0.3 / Java 17 / jakarta complete).

Pins the three false positives found on the canonical Spring Petclinic repo in
v1.65.0:
  BUG 1 — javax.cache:cache-api flagged javax-to-jakarta-migration-risk (it is a
          permanent JSR-107 namespace that never moved to jakarta).
  BUG 2 — hibernate_readiness=91 with "version unknown": a heuristic penalty on
          absent data, marked applicable despite the Boot 4 BOM managing Hibernate ≥6.
  BUG 3 — readiness_score=100 while an applicable dimension scored 91 — an aggregate
          that contradicts its own inputs.

Also guards the inverse: a real Spring Boot 2 repo using javax.persistence /
javax.servlet MUST still flag — the allowlist may never silence a legitimate
migration.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from sourcecode.migrate_check import MigrationReport, run_migrate_check, _parse_major
from sourcecode.serializer import _dep_risk_flags


def _write(root: Path, rel: str, content: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _build_petclinic_like(root: Path) -> list[str]:
    _write(root, "pom.xml",
           "<project>\n"
           "  <parent>\n"
           "    <groupId>org.springframework.boot</groupId>\n"
           "    <artifactId>spring-boot-starter-parent</artifactId>\n"
           "    <version>4.0.3</version>\n"
           "  </parent>\n"
           "  <properties><java.version>17</java.version></properties>\n"
           "  <dependencies>\n"
           "    <dependency><groupId>org.springframework.boot</groupId>\n"
           "      <artifactId>spring-boot-starter-data-jpa</artifactId></dependency>\n"
           "    <dependency><groupId>javax.cache</groupId>\n"
           "      <artifactId>cache-api</artifactId></dependency>\n"
           "  </dependencies>\n"
           "</project>\n")
    files: list[str] = []
    # Jakarta-complete JPA entity using a Hibernate annotation (Hibernate detected,
    # but version is BOM-managed, not declared).
    f = "src/main/java/org/springframework/samples/petclinic/owner/Owner.java"
    _write(root, f,
           "import jakarta.persistence.Entity;\n"
           "import jakarta.persistence.Id;\n"
           "import org.hibernate.annotations.NaturalId;\n"
           "@Entity public class Owner { @Id Long id; }\n")
    files.append(f)
    # The permanent javax.cache usage that triggered BUG 1.
    f = "src/main/java/org/springframework/samples/petclinic/system/CacheConfiguration.java"
    _write(root, f,
           "import javax.cache.configuration.MutableConfiguration;\n"
           "public class CacheConfiguration {}\n")
    files.append(f)
    return files


# ── BUG 1 ────────────────────────────────────────────────────────────────────

def test_javax_cache_coordinate_has_no_migration_flag() -> None:
    assert _dep_risk_flags("javax.cache:cache-api", None) == []
    assert _dep_risk_flags("javax.cache", None) == []


@pytest.mark.parametrize("coord", [
    "javax.sql", "javax.xml.parsers", "javax.naming", "javax.management",
    "javax.crypto", "javax.net.ssl", "javax.security.auth", "javax.tools",
    "javax.annotation.processing", "javax.lang.model",
])
def test_permanent_jdk_namespaces_never_flag(coord: str) -> None:
    assert "javax-to-jakarta-migration-risk" not in _dep_risk_flags(coord, None)


# ── BUG 1 inverse (negative test): real EE migrations MUST still flag ─────────

@pytest.mark.parametrize("coord", [
    "javax.persistence", "javax.servlet", "javax.servlet.jsp", "javax.validation",
    "javax.transaction", "javax.ejb", "javax.mail", "javax.ws.rs",
    "javax.annotation", "javax.xml.bind",
])
def test_real_ee_namespaces_still_flag(coord: str) -> None:
    # The allowlist must not silence a legitimate javax→jakarta migration.
    assert "javax-to-jakarta-migration-risk" in _dep_risk_flags(coord, None)


# ── BUG 2 ────────────────────────────────────────────────────────────────────

def test_hibernate_na_under_boot4_bom(tmp_path: Path) -> None:
    files = _build_petclinic_like(tmp_path)
    report = run_migrate_check(files, tmp_path)
    hib = report.applicable_dimensions["hibernate"]
    assert hib["applicable"] is False
    assert hib["score"] is None
    assert hib["status"] == "managed_ge6"
    assert "Spring Boot 4.0.3" in hib["reason"]
    # No phantom heuristic number leaked into the scalar.
    assert report.hibernate_readiness == 100
    assert report.headline_blocker is None


def test_hibernate_unresolved_is_not_applicable_no_bom(tmp_path: Path) -> None:
    # Hibernate used, version not declared, and NO Spring Boot BOM to infer from →
    # status "unresolved", applicable False, never a heuristic penalty.
    _write(tmp_path, "x/MoneyType.java",
           "import org.hibernate.usertype.UserType;\npublic class MoneyType implements UserType {}\n")
    report = run_migrate_check(["x/MoneyType.java"], tmp_path)
    hib = report.applicable_dimensions["hibernate"]
    assert hib["applicable"] is False
    assert hib["status"] == "unresolved"
    assert report.hibernate_readiness == 100


def test_hibernate_applicable_under_boot2_bom(tmp_path: Path) -> None:
    # Spring Boot 2 BOM manages Hibernate 5.x → the 5→6 axis IS applicable (inferred).
    _write(tmp_path, "pom.xml",
           "<project><parent>"
           "<groupId>org.springframework.boot</groupId>"
           "<artifactId>spring-boot-starter-parent</artifactId>"
           "<version>2.7.18</version></parent></project>\n")
    _write(tmp_path, "x/MoneyType.java",
           "import org.hibernate.usertype.UserType;\npublic class MoneyType implements UserType {}\n")
    report = run_migrate_check(["x/MoneyType.java"], tmp_path)
    hib = report.applicable_dimensions["hibernate"]
    assert hib["applicable"] is True
    assert hib["status"] == "managed_h5"


# ── BUG 3 ────────────────────────────────────────────────────────────────────

def test_readiness_score_consistent_with_applicable_dimensions(tmp_path: Path) -> None:
    files = _build_petclinic_like(tmp_path)
    report = run_migrate_check(files, tmp_path)
    assert report.readiness_score == 100
    assert report.blocking_count == 0
    assert report.spring_boot_version_detected == "4.0.3"
    assert report.spring_boot_2_detected is False
    # The invariant: readiness_score == min over applicable migration dimensions.
    agg = report.readiness_aggregate
    assert agg["method"] == "min"
    assert "jdk_modernization" in agg["excluded"]
    assert report.readiness_score == (min(agg["inputs"].values()) if agg["inputs"] else 100)


def test_aggregate_invariant_holds_generally() -> None:
    # Synthetic: a single high jakarta blocker + heavy excluded JDK debt. The headline
    # must equal min(applicable migration dims), independent of JDK noise.
    from sourcecode.migrate_check import MigrationFinding

    def f(rule, sev, target, src):
        return MigrationFinding(id=MigrationFinding.make_id(rule, src), rule_id=rule,
                                severity=sev, title="t", source_file=src, first_line=1,
                                migration_target=target)

    findings = [f("MIG-002", "high", "jakarta", "A.java")]
    findings += [f("MIG-016", "low", "java_8_best_practice", f"D{i}.java") for i in range(40)]
    report = MigrationReport(findings=findings).finalize()
    inputs = report.readiness_aggregate["inputs"]
    assert report.readiness_score == min(inputs.values())
    assert "jdk_modernization" not in inputs


def test_parse_major() -> None:
    assert _parse_major("4.0.3") == 4
    assert _parse_major("2.7.18") == 2
    assert _parse_major(None) is None
    assert _parse_major("") is None

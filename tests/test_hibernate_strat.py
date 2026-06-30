"""Tests for hibernate_strat — Hibernate 5→6 migration stratification model."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from sourcecode.hibernate_strat import (
    analyze_hibernate,
    HibernateStratification,
    LAYER_JPA,
    LAYER_CRITERIA,
    LAYER_HQL,
    LAYER_SPI,
    CLASS_NONE,
    CLASS_UPGRADE,
    CLASS_UPGRADE_CARE,
    CLASS_REWRITE,
    HIBERNATE_SCHEMA_VERSION,
    KIND_MANUAL,
    KIND_ASSISTED,
    KIND_MECHANICAL,
    KIND_REVIEW,
    _module_of,
)


def _write(root: Path, rel: str, body: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


_HIBERNATE_POM = (
    "<project><dependencies><dependency>"
    "<groupId>org.hibernate.orm</groupId><artifactId>hibernate-core</artifactId>"
    "<version>5.6.15.Final</version></dependency></dependencies></project>\n"
)


def _analyze(files: dict[str, str], *, with_hibernate_dep: bool = True) -> HibernateStratification:
    """Analyze an in-memory fixture repo.

    By default a pom.xml declaring org.hibernate:hibernate-core is written so the
    repo carries genuine Hibernate dependency evidence (BUG #1 gate). Tests that
    assert *non*-detection on a non-Hibernate repo pass with_hibernate_dep=False.
    """
    tmp = tempfile.mkdtemp()
    root = Path(tmp)
    if with_hibernate_dep:
        (root / "pom.xml").write_text(_HIBERNATE_POM, encoding="utf-8")
    for rel, body in files.items():
        _write(root, rel, body)
    rel_paths = list(files.keys())
    return analyze_hibernate(rel_paths, root)


# ---------------------------------------------------------------------------
# Module derivation
# ---------------------------------------------------------------------------

class TestModuleOf:
    def test_module_before_src(self) -> None:
        assert _module_of("open-admin-platform/src/main/java/x/A.java") == "open-admin-platform"

    def test_nested_module(self) -> None:
        assert _module_of("admin/core/src/main/java/x/A.java") == "core"

    def test_fallback_first_segment(self) -> None:
        assert _module_of("foo/A.java") == "foo"

    def test_root_file(self) -> None:
        assert _module_of("A.java") == "root"


# ---------------------------------------------------------------------------
# No usage
# ---------------------------------------------------------------------------

class TestNoUsage:
    def test_empty(self) -> None:
        s = analyze_hibernate([], Path("."))
        assert s.detected is False
        assert s.classification == CLASS_NONE

    def test_no_hibernate(self) -> None:
        s = _analyze({"m/src/main/java/x/Plain.java": "package x;\npublic class Plain {}\n"})
        assert s.detected is False
        assert s.classification == CLASS_NONE


# ---------------------------------------------------------------------------
# Layer 1 — JPA annotations
# ---------------------------------------------------------------------------

class TestJpaLayer:
    def test_standard_jpa_is_low_upgrade(self) -> None:
        s = _analyze({"core/src/main/java/x/E.java":
                      "import javax.persistence.Entity;\n"
                      "@Entity\npublic class E { @Id Long id; @Column String n; }\n"})
        assert s.detected
        jpa = [r for r in s.risk_matrix if r.layer == LAYER_JPA][0]
        assert jpa.risk == "low"
        assert s.classification == CLASS_UPGRADE

    def test_deprecated_annotation_escalates(self) -> None:
        s = _analyze({"core/src/main/java/x/E.java":
                      "import javax.persistence.Entity;\n"
                      "@Entity\n@TypeDef(name=\"x\")\npublic class E { @Id Long id; }\n"})
        jpa = [r for r in s.risk_matrix if r.layer == LAYER_JPA][0]
        assert jpa.risk == "high"
        assert s.classification == CLASS_UPGRADE_CARE


# ---------------------------------------------------------------------------
# Layer 2 — Criteria API
# ---------------------------------------------------------------------------

class TestCriteriaLayer:
    def test_static_criteria_is_high(self) -> None:
        s = _analyze({"core/src/main/java/x/Dao.java":
                      "import javax.persistence.criteria.CriteriaBuilder;\n"
                      "public class Dao { void f(){ CriteriaBuilder cb; CriteriaQuery cq; } }\n"})
        crit = [r for r in s.risk_matrix if r.layer == LAYER_CRITERIA][0]
        assert crit.risk == "high"
        # static criteria → upgrade-with-care, not rewrite
        assert s.classification == CLASS_UPGRADE_CARE
        assert not s.stop_conditions_triggered

    def test_dynamic_criteria_via_reflection_is_critical_rewrite(self) -> None:
        s = _analyze({"open-admin-platform/src/main/java/x/DynamicEntityDaoImpl.java":
                      "import java.lang.reflect.Field;\n"
                      "import javax.persistence.criteria.CriteriaBuilder;\n"
                      "public class DynamicEntityDaoImpl {\n"
                      "  Object q(Class<?> entityClass){\n"
                      "    Field[] f = entityClass.getDeclaredFields();\n"
                      "    CriteriaBuilder cb; CriteriaQuery cq; return cq; } }\n"})
        crit = [r for r in s.risk_matrix if r.layer == LAYER_CRITERIA][0]
        assert crit.risk == "critical"
        assert s.classification == CLASS_REWRITE
        assert any("dynamically" in x or "reflection" in x for x in s.stop_conditions_triggered)

    def test_legacy_hibernate_criteria_flagged(self) -> None:
        s = _analyze({"core/src/main/java/x/Dao.java":
                      "import org.hibernate.criterion.Restrictions;\n"
                      "public class Dao { void f(){ session.createCriteria(Foo.class).add(Restrictions.eq(\"a\",1)); } }\n"})
        crit = [r for r in s.risk_matrix if r.layer == LAYER_CRITERIA]
        assert crit
        assert any("Legacy org.hibernate.Criteria" in p for p in s.incompatible_patterns)

    def test_critical_call_chain_detected(self) -> None:
        s = _analyze({"admin/src/main/java/x/BasicPersistenceModule.java":
                      "import java.lang.reflect.Field;\n"
                      "import javax.persistence.criteria.CriteriaBuilder;\n"
                      "public class BasicPersistenceModule {\n"
                      "  void f(){ Field[] x; CriteriaBuilder cb; } }\n"})
        assert s.critical_call_chains
        assert s.critical_call_chains[0]["class"] == "BasicPersistenceModule"


# ---------------------------------------------------------------------------
# Layer 3 — HQL / string queries
# ---------------------------------------------------------------------------

class TestHqlLayer:
    def test_static_hql_is_medium(self) -> None:
        s = _analyze({"core/src/main/java/x/Repo.java":
                      "import javax.persistence.EntityManager;\n"
                      "public class Repo { void f(){ em.createQuery(\"SELECT o FROM Order o\"); } }\n"})
        hql = [r for r in s.risk_matrix if r.layer == LAYER_HQL][0]
        assert hql.risk == "medium"
        assert s.classification == CLASS_UPGRADE

    def test_concatenated_query_is_high_rewrite(self) -> None:
        s = _analyze({"core/src/main/java/x/Repo.java":
                      "import javax.persistence.EntityManager;\n"
                      "public class Repo { void f(String st){ "
                      "em.createQuery(\"SELECT o FROM Order o WHERE s='\" + st + \"'\"); } }\n"})
        hql = [r for r in s.risk_matrix if r.layer == LAYER_HQL][0]
        assert hql.risk == "high"
        assert s.classification == CLASS_REWRITE
        assert any("statically inferable" in x for x in s.stop_conditions_triggered)

    def test_criteria_createquery_not_counted_as_hql(self) -> None:
        # CriteriaBuilder.createQuery(Class) must not register as HQL.
        s = _analyze({"core/src/main/java/x/Dao.java":
                      "import javax.persistence.criteria.CriteriaBuilder;\n"
                      "public class Dao { void f(){ CriteriaBuilder cb; cb.createQuery(Foo.class); } }\n"})
        assert not any(r.layer == LAYER_HQL for r in s.risk_matrix)


# ---------------------------------------------------------------------------
# Layer 4 — SPI / internal
# ---------------------------------------------------------------------------

class TestSpiLayer:
    def test_usertype_is_critical_blocker(self) -> None:
        s = _analyze({"common/src/main/java/x/MoneyType.java":
                      "import org.hibernate.usertype.UserType;\n"
                      "public class MoneyType implements UserType {}\n"})
        spi = [r for r in s.risk_matrix if r.layer == LAYER_SPI][0]
        assert spi.risk == "critical"
        assert s.classification == CLASS_REWRITE
        assert any("Custom Hibernate SPI" in x for x in s.stop_conditions_triggered)

    def test_engine_spi_internal_flagged(self) -> None:
        s = _analyze({"core/src/main/java/x/I.java":
                      "import org.hibernate.engine.spi.SharedSessionContractImplementor;\n"
                      "public class I {}\n"})
        assert any(r.layer == LAYER_SPI for r in s.risk_matrix)


# ---------------------------------------------------------------------------
# Module exposure map + risk separation
# ---------------------------------------------------------------------------

class TestModuleExposureAndSeparation:
    def test_module_map_attributes_layers_and_max_risk(self) -> None:
        s = _analyze({
            "common/src/main/java/x/MoneyType.java":
                "import org.hibernate.usertype.UserType;\npublic class MoneyType implements UserType {}\n",
            "core/src/main/java/x/E.java":
                "@Entity\npublic class E { @Id Long id; }\n",
        })
        assert s.module_exposure["common"]["max_risk"] == "critical"
        assert s.module_exposure["common"]["has_spi"] is True
        assert s.module_exposure["core"]["max_risk"] == "low"
        assert LAYER_JPA in s.module_exposure["core"]["layers"]

    def test_inferred_runtime_risk_present_for_spi(self) -> None:
        s = _analyze({"common/src/main/java/x/MoneyType.java":
                      "import org.hibernate.usertype.UserType;\npublic class MoneyType implements UserType {}\n"})
        assert s.inferred_runtime_risk
        assert s.observable_risk

    def test_not_aggregated_into_single_score(self) -> None:
        # Contract: stratified output exposes a matrix, never one Hibernate score.
        s = _analyze({"core/src/main/java/x/E.java": "@Entity\npublic class E {}\n"})
        d = s.to_dict()
        assert d["stratified"] is True
        assert "risk_matrix" in d
        assert "hibernate_risk_score" not in d


# ---------------------------------------------------------------------------
# Integration through run_migrate_check
# ---------------------------------------------------------------------------

class TestIntegration:
    def test_migrate_check_attaches_hibernate(self) -> None:
        from sourcecode.migrate_check import run_migrate_check
        tmp = tempfile.mkdtemp()
        root = Path(tmp)
        _write(root, "common/src/main/java/x/MoneyType.java",
               "import org.hibernate.usertype.UserType;\npublic class MoneyType implements UserType {}\n")
        report = run_migrate_check(["common/src/main/java/x/MoneyType.java"], root)
        d = report.to_dict()
        assert d["hibernate"] is not None
        assert d["hibernate"]["classification"] == CLASS_REWRITE
        # text output includes the stratification block
        assert "Stratification" in report.to_text()

    def test_headline_blocker_only_when_version_confirms_h5(self) -> None:
        # BUG #1: a rewrite-zone SPI usage earns the headline_blocker ONLY when the
        # effective Hibernate version is resolved to < 6. With a pinned 5.x pom the
        # verdict is authoritative.
        from sourcecode.migrate_check import run_migrate_check
        root = Path(tempfile.mkdtemp())
        _write(root, "pom.xml",
               "<project><properties><hibernate.version>5.6.15.Final</hibernate.version>"
               "</properties></project>\n")
        _write(root, "common/src/main/java/x/MoneyType.java",
               "import org.hibernate.usertype.UserType;\npublic class MoneyType implements UserType {}\n")
        report = run_migrate_check(["common/src/main/java/x/MoneyType.java"], root)
        d = report.to_dict()
        assert d["hibernate_readiness"] < 100
        assert d["headline_blocker"] == "hibernate_rewrite"
        assert d["hibernate"]["version_major"] == 5

    def test_no_headline_blocker_when_version_unresolved(self) -> None:
        # BUG #1 principle: never assert a framework blocker without a version.
        # No build descriptor → version unknown → degrade to hypothesis, no headline.
        from sourcecode.migrate_check import run_migrate_check
        root = Path(tempfile.mkdtemp())
        _write(root, "common/src/main/java/x/MoneyType.java",
               "import org.hibernate.usertype.UserType;\npublic class MoneyType implements UserType {}\n")
        report = run_migrate_check(["common/src/main/java/x/MoneyType.java"], root)
        d = report.to_dict()
        assert d["headline_blocker"] is None
        assert d["hibernate"]["version_confidence"] == "none"

    def test_hibernate6_is_not_applicable(self) -> None:
        # BUG #1: already on Hibernate 6 → the 5→6 axis is N/A, never a blocker.
        from sourcecode.migrate_check import run_migrate_check
        root = Path(tempfile.mkdtemp())
        _write(root, "pom.xml",
               "<project><properties><hibernate.version>6.2.13.Final</hibernate.version>"
               "</properties></project>\n")
        _write(root, "common/src/main/java/x/MoneyType.java",
               "import org.hibernate.usertype.UserType;\npublic class MoneyType implements UserType {}\n")
        report = run_migrate_check(["common/src/main/java/x/MoneyType.java"], root)
        d = report.to_dict()
        assert d["headline_blocker"] is None
        assert d["hibernate"]["effective_version"] == "6.2.13.Final"
        assert d["hibernate"]["migration_applicable"] is False
        assert d["applicable_dimensions"]["hibernate"]["applicable"] is False

    def test_clean_repo_no_headline_blocker(self) -> None:
        from sourcecode.migrate_check import run_migrate_check
        report = run_migrate_check([], Path(tempfile.mkdtemp()))
        d = report.to_dict()
        assert d["headline_blocker"] is None
        assert d["hibernate_readiness"] == 100


# ---------------------------------------------------------------------------
# Rewrite targets (the core of the actionable-output upgrade)
# ---------------------------------------------------------------------------

class TestRewriteTargets:
    def test_criteria_emits_target_with_real_location_and_api(self) -> None:
        s = _analyze({"open-admin-platform/src/main/java/x/DynamicEntityDaoImpl.java":
                      "import java.lang.reflect.Field;\n"
                      "public class DynamicEntityDaoImpl {\n"
                      "  Object q(Class<?> entityClass){\n"
                      "    Field[] f = entityClass.getDeclaredFields();\n"
                      "    CriteriaBuilder cb = em.getCriteriaBuilder(); return cb; } }\n"})
        ct = [t for t in s.rewrite_targets if t.layer == LAYER_CRITERIA]
        assert ct
        t = ct[0]
        assert t.source_file.endswith("DynamicEntityDaoImpl.java")
        assert t.line_start > 0
        assert t.target_api  # populated
        assert "CriteriaBuilder" in t.target_api
        assert t.migration_kind == KIND_MANUAL
        assert t.dynamic is True
        assert t.id.startswith("HB6-CRIT-")
        assert t.symbol  # enclosing class/method captured

    def test_legacy_criteria_is_manual_rewrite(self) -> None:
        s = _analyze({"core/src/main/java/x/Dao.java":
                      "public class Dao { void f(){ session.createCriteria(Foo.class)"
                      ".add(Restrictions.eq(\"a\",1)); } }\n"})
        ct = [t for t in s.rewrite_targets if t.layer == LAYER_CRITERIA]
        assert any(t.migration_kind == KIND_MANUAL and "removed in Hibernate 6" in t.blocking_reason
                   for t in ct)

    def test_spi_targets_map_to_h6_contract(self) -> None:
        s = _analyze({"common/src/main/java/x/MoneyType.java":
                      "import org.hibernate.usertype.UserType;\n"
                      "public class MoneyType implements UserType {}\n"})
        st = [t for t in s.rewrite_targets if t.layer == LAYER_SPI]
        assert st
        assert all(t.target_api for t in st)
        assert any("UserType" in t.target_api for t in st)
        assert all(t.auto_migratable is False for t in st)

    def test_concatenated_hql_is_assisted_runtime_resolved(self) -> None:
        s = _analyze({"core/src/main/java/x/Repo.java":
                      "public class Repo { void f(String st){ "
                      "em.createQuery(\"SELECT o FROM O o WHERE s='\" + st + \"'\"); } }\n"})
        ht = [t for t in s.rewrite_targets if t.layer == LAYER_HQL]
        assert any(t.migration_kind == KIND_ASSISTED and "runtime-resolved" in t.blocking_reason
                   for t in ht)

    def test_deprecated_type_annotation_is_mechanical_automigratable(self) -> None:
        s = _analyze({"core/src/main/java/x/E.java":
                      "@Entity\npublic class E { @Type(type=\"foo\") String x; }\n"})
        jt = [t for t in s.rewrite_targets if t.layer == LAYER_JPA]
        assert any(t.migration_kind == KIND_MECHANICAL and t.auto_migratable for t in jt)

    def test_standard_jpa_emits_no_rewrite_targets(self) -> None:
        s = _analyze({"core/src/main/java/x/E.java":
                      "@Entity\npublic class E { @Id Long id; @Column String n; }\n"})
        assert not [t for t in s.rewrite_targets if t.layer == LAYER_JPA]

    def test_target_count_matches_occurrence_count_for_criteria_spi(self) -> None:
        # No regression of counts: one rewrite target per detected occurrence.
        s = _analyze({
            "core/src/main/java/x/Dao.java":
                "public class Dao { void f(){ CriteriaBuilder cb; CriteriaQuery cq; } }\n",
            "common/src/main/java/x/T.java":
                "import org.hibernate.usertype.UserType;\npublic class T implements UserType {}\n",
        })
        for layer in (LAYER_CRITERIA, LAYER_SPI):
            row = [r for r in s.risk_matrix if r.layer == layer][0]
            n_targets = len([t for t in s.rewrite_targets if t.layer == layer])
            assert n_targets == row.occurrence_count


# ---------------------------------------------------------------------------
# Sub-counts, effort honesty, hotspots
# ---------------------------------------------------------------------------

class TestSubCountsAndEffort:
    def test_criteria_static_vs_dynamic_split(self) -> None:
        s = _analyze({
            "open-admin-platform/src/main/java/x/Dyn.java":
                "import java.lang.reflect.Field;\n"
                "public class Dyn { Object q(Class<?> c){ Field[] f=c.getDeclaredFields(); "
                "CriteriaBuilder cb; return cb; } }\n",
        })
        row = [r for r in s.risk_matrix if r.layer == LAYER_CRITERIA][0]
        assert row.dynamic_count is not None and row.dynamic_count > 0
        assert row.static_count == 0

    def test_spi_rewrite_vs_resolvable_counts(self) -> None:
        s = _analyze({"common/src/main/java/x/T.java":
                      "import org.hibernate.usertype.UserType;\npublic class T implements UserType {}\n"})
        row = [r for r in s.risk_matrix if r.layer == LAYER_SPI][0]
        assert row.userType_rewrite_count is not None
        assert row.userType_rewrite_count >= 1

    def test_effort_range_and_total_present(self) -> None:
        s = _analyze({"common/src/main/java/x/T.java":
                      "import org.hibernate.usertype.UserType;\npublic class T implements UserType {}\n"})
        row = s.risk_matrix[0]
        assert {"low", "high", "confidence"} <= set(row.effort_range)
        assert row.effort_range["low"] <= row.effort_range["high"]
        d = s.to_dict()
        assert {"low", "high", "confidence"} <= set(d["total_effort_range_days"])
        assert d["effort_model"]["unit"] == "person-days"
        assert "caveat" in d["effort_model"]

    def test_readiness_below_50_when_many_critical(self) -> None:
        files = {f"m/src/main/java/x/T{i}.java":
                 "import org.hibernate.usertype.UserType;\n"
                 f"public class T{i} implements UserType {{}}\n" for i in range(4)}
        s = _analyze(files)
        assert s.readiness < 50

    def test_golden_sql_hotspots_ranked(self) -> None:
        s = _analyze({"admin/src/main/java/x/DynamicEntityDaoImpl.java":
                      "import java.lang.reflect.Field;\n"
                      "public class DynamicEntityDaoImpl { Object q(Class<?> c){ "
                      "Field[] f=c.getDeclaredFields(); CriteriaBuilder cb; CriteriaQuery cq; return cb; } }\n"})
        assert s.golden_sql_hotspots
        assert s.golden_sql_hotspots[0]["dynamic_query_count"] > 0


# ---------------------------------------------------------------------------
# Control fixtures + determinism (anti-regression guardrails)
# ---------------------------------------------------------------------------

class TestGuardrails:
    def test_schema_version_exposed(self) -> None:
        s = _analyze({"core/src/main/java/x/E.java": "@Entity\npublic class E {}\n"})
        assert s.to_dict()["schema_version"] == HIBERNATE_SCHEMA_VERSION

    def test_existing_keys_preserved(self) -> None:
        s = _analyze({"core/src/main/java/x/E.java": "@Entity\npublic class E {}\n"})
        d = s.to_dict()
        for key in ("detected", "classification", "classification_label", "stratified",
                    "risk_matrix", "module_exposure_map", "stop_conditions_triggered",
                    "risk_separation", "findings", "critical_call_chains"):
            assert key in d

    def test_already_hibernate6_repo_not_rewrite_zone(self) -> None:
        # Standard JPA + jakarta only, no Criteria/SPI/concat → upgrade, no targets.
        s = _analyze({"core/src/main/java/x/E.java":
                      "import jakarta.persistence.Entity;\nimport jakarta.persistence.Id;\n"
                      "@Entity\npublic class E { @Id Long id; }\n"})
        assert s.detected is True
        assert s.classification != CLASS_REWRITE
        assert s.rewrite_targets == []

    def test_no_hibernate_repo_not_detected(self) -> None:
        s = _analyze({"m/src/main/java/x/Plain.java": "public class Plain { int x; }\n"})
        assert s.detected is False
        assert s.rewrite_targets == []
        assert s.to_dict()["classification"] == CLASS_NONE

    def test_determinism_same_head_same_output(self) -> None:
        files = {
            "open-admin-platform/src/main/java/x/Dyn.java":
                "import java.lang.reflect.Field;\n"
                "public class Dyn { Object q(Class<?> c){ Field[] f=c.getDeclaredFields(); "
                "CriteriaBuilder cb; return cb; } }\n",
            "common/src/main/java/x/T.java":
                "import org.hibernate.usertype.UserType;\npublic class T implements UserType {}\n",
        }
        s1 = _analyze(files)
        s2 = _analyze(files)
        import json
        assert json.dumps(s1.to_dict(), sort_keys=True) == json.dumps(s2.to_dict(), sort_keys=True)

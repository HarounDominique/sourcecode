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
    _module_of,
)


def _write(root: Path, rel: str, body: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def _analyze(files: dict[str, str]) -> HibernateStratification:
    tmp = tempfile.mkdtemp()
    root = Path(tmp)
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
                      "@Entity\npublic class E { @Id Long id; @Column String n; }\n"})
        assert s.detected
        jpa = [r for r in s.risk_matrix if r.layer == LAYER_JPA][0]
        assert jpa.risk == "low"
        assert s.classification == CLASS_UPGRADE

    def test_deprecated_annotation_escalates(self) -> None:
        s = _analyze({"core/src/main/java/x/E.java":
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
                      "public class Dao { void f(){ CriteriaBuilder cb; CriteriaQuery cq; } }\n"})
        crit = [r for r in s.risk_matrix if r.layer == LAYER_CRITERIA][0]
        assert crit.risk == "high"
        # static criteria → upgrade-with-care, not rewrite
        assert s.classification == CLASS_UPGRADE_CARE
        assert not s.stop_conditions_triggered

    def test_dynamic_criteria_via_reflection_is_critical_rewrite(self) -> None:
        s = _analyze({"open-admin-platform/src/main/java/x/DynamicEntityDaoImpl.java":
                      "import java.lang.reflect.Field;\n"
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
                      "public class Dao { void f(){ session.createCriteria(Foo.class).add(Restrictions.eq(\"a\",1)); } }\n"})
        crit = [r for r in s.risk_matrix if r.layer == LAYER_CRITERIA]
        assert crit
        assert any("Legacy org.hibernate.Criteria" in p for p in s.incompatible_patterns)

    def test_critical_call_chain_detected(self) -> None:
        s = _analyze({"admin/src/main/java/x/BasicPersistenceModule.java":
                      "import java.lang.reflect.Field;\n"
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
                      "public class Repo { void f(){ em.createQuery(\"SELECT o FROM Order o\"); } }\n"})
        hql = [r for r in s.risk_matrix if r.layer == LAYER_HQL][0]
        assert hql.risk == "medium"
        assert s.classification == CLASS_UPGRADE

    def test_concatenated_query_is_high_rewrite(self) -> None:
        s = _analyze({"core/src/main/java/x/Repo.java":
                      "public class Repo { void f(String st){ "
                      "em.createQuery(\"SELECT o FROM Order o WHERE s='\" + st + \"'\"); } }\n"})
        hql = [r for r in s.risk_matrix if r.layer == LAYER_HQL][0]
        assert hql.risk == "high"
        assert s.classification == CLASS_REWRITE
        assert any("statically inferable" in x for x in s.stop_conditions_triggered)

    def test_criteria_createquery_not_counted_as_hql(self) -> None:
        # CriteriaBuilder.createQuery(Class) must not register as HQL.
        s = _analyze({"core/src/main/java/x/Dao.java":
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

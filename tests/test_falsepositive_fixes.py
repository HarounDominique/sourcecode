"""Regression tests for the Alfresco-class false-positive fixes (Bugs #1-#4).

Each bug has BOTH a negative fixture (must not fire — the false positive) and a
positive fixture (the true positive that must be preserved).
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from sourcecode.hibernate_strat import analyze_hibernate, CLASS_REWRITE
from sourcecode.migrate_check import run_migrate_check
from sourcecode.repository_ir import extract_java_endpoints


def _mkrepo(files: dict[str, str]) -> Path:
    root = Path(tempfile.mkdtemp())
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# BUG #1 — phantom Hibernate via comments / class-name tokens
# ---------------------------------------------------------------------------

class TestBug1HibernatePhantom:
    def test_fixture_A_comment_and_string_only_not_detected(self) -> None:
        # org.hibernate appears ONLY in a comment and a string literal/classpath
        # path — no real import, no dependency. Must NOT be detected as Hibernate.
        body = (
            "package x;\n"
            "// see org.hibernate.dialect.MySQLDialect for the legacy mapping\n"
            "public class DialectUtil {\n"
            '  String cfg = "config/org.hibernate.dialect.Dialect/x.xml";\n'
            "}\n"
        )
        root = _mkrepo({"repo/src/main/java/x/DialectUtil.java": body})
        s = analyze_hibernate(["repo/src/main/java/x/DialectUtil.java"], root)
        assert s.detected is False
        assert s.evidence["confidence"] == "none"
        assert s.rewrite_targets == []

    def test_fixture_B_project_owned_classes_not_detected(self) -> None:
        # Project has its OWN Dialect / SearchCriteria / Interceptor / EventListener
        # classes, NO Hibernate dependency. None of these must classify as Hibernate.
        files = {
            "repo/src/main/java/com/acme/persistence/Dialect.java":
                "package com.acme.persistence;\npublic class Dialect {}\n",
            "repo/src/main/java/com/acme/SearchCriteria.java":
                "package com.acme;\npublic class SearchCriteria { Predicate p; Conjunction c; }\n",
            "repo/src/main/java/com/acme/AuditInterceptor.java":
                "package com.acme;\npublic class AuditInterceptor implements MethodInterceptor {}\n",
            "repo/src/main/java/com/acme/NodeEventListener.java":
                "package com.acme;\npublic class NodeEventListener implements EventListener {}\n",
        }
        root = _mkrepo(files)
        s = analyze_hibernate(list(files.keys()), root)
        assert s.detected is False

    def test_fixture_C_real_import_and_dependency_detected(self) -> None:
        # Control positive: real org.hibernate import + dependency → must detect.
        files = {
            "pom.xml":
                "<project><dependencies><dependency>"
                "<groupId>org.hibernate</groupId><artifactId>hibernate-core</artifactId>"
                "<version>5.6.0.Final</version></dependency></dependencies></project>\n",
            "repo/src/main/java/x/MoneyType.java":
                "package x;\nimport org.hibernate.usertype.UserType;\n"
                "public class MoneyType implements UserType {}\n",
        }
        root = _mkrepo(files)
        s = analyze_hibernate(["repo/src/main/java/x/MoneyType.java"], root)
        assert s.detected is True
        assert s.classification == CLASS_REWRITE
        assert s.evidence["dependency_present"] is True
        assert s.evidence["hibernate_import_present"] is True
        assert s.evidence["confidence"] == "high"

    def test_migrate_check_no_hibernate_blocker_on_phantom_repo(self) -> None:
        # End-to-end: a MyBatis-style repo with only commented org.hibernate must
        # not yield hibernate_rewrite headline or a phantom-detected hibernate block.
        body = (
            "package x;\n"
            "// classpath:alfresco/db/org.hibernate.dialect.MySQLInnoDBDialect/f.xml\n"
            "public class DialectUtil {}\n"
        )
        root = _mkrepo({"repo/src/main/java/x/DialectUtil.java": body})
        report = run_migrate_check(["repo/src/main/java/x/DialectUtil.java"], root)
        d = report.to_dict()
        assert d["hibernate"]["detected"] is False
        assert d["headline_blocker"] != "hibernate_rewrite"


# ---------------------------------------------------------------------------
# BUG #2 — javax.transaction.xa.* / JDK javax allowlist
# ---------------------------------------------------------------------------

class TestBug2JavaxAllowlist:
    def _findings(self, body: str) -> list:
        root = _mkrepo({"repo/src/main/java/x/T.java": body})
        report = run_migrate_check(["repo/src/main/java/x/T.java"], root)
        return report.findings

    def test_javax_transaction_xa_not_flagged(self) -> None:
        f = self._findings("package x;\nimport javax.transaction.xa.XAResource;\n"
                           "import javax.transaction.xa.Xid;\npublic class T {}\n")
        assert not any(x.rule_id == "MIG-004" for x in f)

    def test_javax_transaction_app_level_flagged(self) -> None:
        f = self._findings("package x;\nimport javax.transaction.Transactional;\n"
                           "public class T {}\n")
        assert any(x.rule_id == "MIG-004" for x in f)

    def test_javax_annotation_processing_not_flagged(self) -> None:
        f = self._findings("package x;\nimport javax.annotation.processing.Processor;\n"
                           "public class T {}\n")
        assert not any(x.rule_id == "MIG-006" for x in f)

    def test_javax_annotation_postconstruct_flagged(self) -> None:
        f = self._findings("package x;\nimport javax.annotation.PostConstruct;\n"
                           "public class T {}\n")
        assert any(x.rule_id == "MIG-006" for x in f)

    def test_javax_sql_not_flagged_as_jakarta(self) -> None:
        f = self._findings("package x;\nimport javax.sql.DataSource;\npublic class T {}\n")
        assert not any(x.migration_target == "jakarta" for x in f)


# ---------------------------------------------------------------------------
# BUG #3 — applicable_dimensions / N/A exclusion
# ---------------------------------------------------------------------------

class TestBug3ApplicableDimensions:
    def test_applicable_dimensions_exposed_and_hibernate_na(self) -> None:
        # Non-JPA repo (servlet only) → no Hibernate dimension applies.
        root = _mkrepo({"repo/src/main/java/x/T.java":
                        "package x;\nimport javax.servlet.Filter;\npublic class T {}\n"})
        d = run_migrate_check(["repo/src/main/java/x/T.java"], root).to_dict()
        ad = d["applicable_dimensions"]
        assert ad["jakarta"]["applicable"] is True
        assert ad["hibernate"]["applicable"] is False
        assert ad["hibernate"]["score"] is None
        assert "N/A" in ad["hibernate"]["reason"]
        assert "readiness_note" in d

    def test_jakarta_complete_repo_not_dragged(self) -> None:
        # Fully-jakarta, no Boot2, no Hibernate → high readiness, hibernate excluded.
        root = _mkrepo({"repo/src/main/java/x/T.java":
                        "package x;\nimport jakarta.servlet.Filter;\npublic class T {}\n"})
        d = run_migrate_check(["repo/src/main/java/x/T.java"], root).to_dict()
        assert d["readiness_score"] == 100
        assert d["applicable_dimensions"]["hibernate"]["applicable"] is False
        assert d["hibernate_readiness"] == 100  # N/A surfaced as 100, not 0


# ---------------------------------------------------------------------------
# BUG #4 — non-Spring REST surface honesty
# ---------------------------------------------------------------------------

class TestBug4NonSpringRest:
    def test_webscripts_surface_marked_undetermined(self) -> None:
        files = {
            "repo/src/main/java/x/MyWebScript.java":
                "package x;\nimport org.springframework.extensions.webscripts.DeclarativeWebScript;\n"
                "public class MyWebScript extends DeclarativeWebScript {}\n",
            "repo/config/x/my.get.desc.xml":
                "<webscript><url>/api/my</url><authentication>user</authentication></webscript>\n",
        }
        root = _mkrepo(files)
        data = extract_java_endpoints(root)
        assert data["total"] == 0
        assert data["security_model"] == "undetermined"
        assert data["non_spring_rest_surface"]["detected"] is True
        assert data["non_spring_rest_surface"]["frameworks"]["webscripts"] >= 1
        assert any("do NOT read" in w for w in data.get("warnings", []))

    def test_spring_controller_no_false_nonspring_signal(self) -> None:
        files = {
            "src/main/java/com/example/rest/UserController.java":
                "package com.example.rest;\n"
                "import org.springframework.web.bind.annotation.*;\n"
                "@RestController\n@RequestMapping(\"/api/users\")\n"
                "public class UserController {\n"
                '  @GetMapping("/{id}")\n'
                "  public Object get(@PathVariable Long id){ return null; }\n}\n",
        }
        root = _mkrepo(files)
        data = extract_java_endpoints(root)
        assert data["total"] >= 1
        assert "non_spring_rest_surface" not in data
        assert data["security_model"] != "undetermined"

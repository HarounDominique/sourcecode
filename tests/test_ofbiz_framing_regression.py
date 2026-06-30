"""Golden regression: framing accuracy on a non-Spring, own-framework repo (Apache
OFBiz shape — release18.12, ~326k LOC, Service Engine + widgets, NO Spring).

Reproduces the seven defects that made the tool emit MISLEADING high-confidence
framings on non-Spring repositories. Every assertion maps to one defect; if any
regresses, this fails. Fixtures are synthetic (no external checkout required) but
mirror the exact shapes found on the real OFBiz repo.
"""
from __future__ import annotations

from pathlib import Path

from sourcecode.dependency_analyzer import DependencyAnalyzer
from sourcecode.migrate_check import run_migrate_check
from sourcecode.path_filters import is_vendor_path


def _write(root: Path, rel: str, content: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Defects 1–4: spring-test must NOT poison framework attribution / readiness
# ---------------------------------------------------------------------------

def _build_ofbiz_like(root: Path) -> list[str]:
    # The ONLY Spring coordinate is spring-test, declared under legacy `compile`.
    _write(root, "build.gradle",
           "dependencies {\n"
           "    // ofbiz compile libs\n"
           "    compile 'xerces:xercesImpl:2.12.2'\n"
           "    compile 'javax.servlet:javax.servlet-api:3.1.0'\n"
           "    compile 'org.springframework:spring-test:5.1.2.RELEASE'\n"
           "}\n")
    files: list[str] = []
    # Real migratable javax.* (servlet) — the Jakarta EE / Tomcat 10 axis is valid.
    f = "applications/accounting/src/main/java/org/apache/ofbiz/accounting/GlEvents.java"
    _write(root, f,
           "import javax.servlet.http.HttpServletRequest;\n"
           "public class GlEvents {\n"
           "  public static String go(HttpServletRequest request, Object response) { return null; }\n"
           "}\n")
    files.append(f)
    # An OFBiz Service-Engine service (test-tree Spring import must stay test-only).
    f = "framework/src/test/java/org/apache/ofbiz/SomeTest.java"
    _write(root, f,
           "import org.springframework.test.context.junit4.SpringRunner;\n"
           "public class SomeTest {}\n")
    files.append(f)
    return files


def test_spring_test_only_does_not_imply_runtime_spring(tmp_path: Path) -> None:
    # Defects 1 + 2: spring-test (a TEST library, even under `compile`) and a
    # test-tree org.springframework import never mark the repo as runtime-Spring.
    files = _build_ofbiz_like(tmp_path)
    report = run_migrate_check(files, tmp_path)
    assert report.spring_present is False
    assert report.spring_test_only is True


def test_boot3_dimension_na_without_runtime_spring(tmp_path: Path) -> None:
    # Defect 3: the phantom boot3=0 dimension is gone — boot3 is N/A, not 0.
    files = _build_ofbiz_like(tmp_path)
    report = run_migrate_check(files, tmp_path)
    boot3 = report.applicable_dimensions["boot3"]
    assert boot3["applicable"] is False
    assert boot3["score"] is None
    assert "test" in boot3["reason"].lower()


def test_readiness_not_collapsed_by_phantom_boot3(tmp_path: Path) -> None:
    # Defect 4: readiness is driven by the REAL applicable axis (jakarta/servlet),
    # never sunk to 0 by an inapplicable boot3 dimension. jakarta stays applicable
    # because there ARE migratable javax.* imports (Tomcat 10 / Jakarta EE).
    files = _build_ofbiz_like(tmp_path)
    report = run_migrate_check(files, tmp_path)
    assert report.applicable_dimensions["jakarta"]["applicable"] is True
    assert report.readiness_aggregate["inputs"].keys() == {"jakarta"}
    # The aggregate reflects jakarta only — boot3's phantom 0 is not an input.
    assert "boot3" not in report.readiness_aggregate["inputs"]


def test_no_migration_target_is_na_not_zero(tmp_path: Path) -> None:
    # Defect 4: a repo with no Spring, no migratable javax.*, no Hibernate has NO
    # migration axis — readiness is N/A (None), never a manufactured 0 or 100.
    _write(tmp_path, "build.gradle", "dependencies {\n    compile 'com.google.guava:guava:31.0'\n}\n")
    f = "src/main/java/com/acme/Plain.java"
    _write(tmp_path, f, "package com.acme;\npublic class Plain { int add(int a){ return a; } }\n")
    report = run_migrate_check([f], tmp_path)
    assert report.readiness_score is None
    assert report.readiness_aggregate["applicable"] is False


def test_genuine_spring_boot_repo_still_applicable(tmp_path: Path) -> None:
    # NO-REGRESSION: a real Spring Boot repo (runtime spring coordinate + main-source
    # spring import) keeps boot3 applicable and a computed readiness.
    _write(tmp_path, "build.gradle",
           "dependencies {\n"
           "    implementation 'org.springframework.boot:spring-boot-starter-web:2.7.0'\n"
           "    testImplementation 'org.springframework.boot:spring-boot-starter-test:2.7.0'\n"
           "}\n")
    f = "src/main/java/com/acme/UserController.java"
    _write(tmp_path, f,
           "import org.springframework.web.bind.annotation.RestController;\n"
           "import javax.persistence.Entity;\n"
           "@RestController public class UserController {}\n")
    report = run_migrate_check([f], tmp_path)
    assert report.spring_present is True
    assert report.spring_test_only is False
    assert report.applicable_dimensions["boot3"]["applicable"] is True
    assert report.readiness_score is not None


# ---------------------------------------------------------------------------
# Defect 6: non-standard Gradle (glob strings) must not zero out dependencies
# ---------------------------------------------------------------------------

def test_gradle_glob_strings_do_not_eat_dependencies(tmp_path: Path) -> None:
    # Real OFBiz shape: an Ant-style glob string '**/*.java' contains '/*', which a
    # naive comment stripper treated as a block-comment open — eating the whole
    # dependencies block. The string-aware stripper must preserve the deps.
    _write(tmp_path, "build.gradle",
           "task copy(type: Copy) {\n"
           "    include '**/*.java'   // glob with /* inside a string\n"
           "    exclude '**/*.class'\n"
           "}\n"
           "dependencies {\n"
           "    compile 'xerces:xercesImpl:2.12.2'\n"
           "    compile 'com.ibm.icu:icu4j:63.1'\n"
           "}\n")
    records, summary = DependencyAnalyzer().analyze(tmp_path)
    names = {r.name for r in records}
    assert "xerces:xercesImpl" in names
    assert "com.ibm.icu:icu4j" in names
    assert summary.total_count >= 2


def test_gradle_reports_gap_when_nothing_resolved(tmp_path: Path) -> None:
    # Defect 6: a dependencies block that resolves to nothing reports a GAP, not a
    # confident "0 dependencies".
    _write(tmp_path, "build.gradle",
           "dependencies {\n"
           "    runtime fileTree(dir: 'lib', include: '*.jar')\n"
           "}\n")
    _write(tmp_path, "lib/foo.jar", "x")
    records, summary = DependencyAnalyzer().analyze(tmp_path)
    assert records == []
    assert any("none resolved" in lim for lim in summary.limitations)


# ---------------------------------------------------------------------------
# Defect 7: vendored web libraries must be flagged as vendor (not project notes)
# ---------------------------------------------------------------------------

def test_vendored_jquery_is_vendor_path() -> None:
    assert is_vendor_path("themes/common/webapp/common/js/jquery/jquery-3.5.1.js")
    assert is_vendor_path("themes/common/webapp/common/js/jquery/jquery-migrate-1.2.1.js")
    assert is_vendor_path("static/js/bootstrap.bundle.js")
    # A project's own JS under a normal source dir is NOT vendor.
    assert not is_vendor_path("src/main/webapp/js/orderManager.js")


# ---------------------------------------------------------------------------
# Defect 5: framework-dispatched classes are not reported as dead zones
# ---------------------------------------------------------------------------

def test_partition_excludes_framework_dispatched(tmp_path: Path) -> None:
    from sourcecode.cli import _partition_static_unreferenced
    # OFBiz Service-Engine service: Map<String,Object> name(DispatchContext, Map<...>)
    svc = "app/src/main/java/org/apache/ofbiz/InvoiceServices.java"
    _write(tmp_path, svc,
           "import org.apache.ofbiz.service.DispatchContext;\n"
           "public class InvoiceServices {\n"
           "  public static java.util.Map<String,Object> create(DispatchContext ctx, java.util.Map<String,Object> c){return null;}\n"
           "}\n")
    # A class wired only from XML config (no static callers, no signature).
    wired = "app/src/main/java/org/apache/ofbiz/WidgetHandler.java"
    _write(tmp_path, wired, "public class WidgetHandler {}\n")
    _write(tmp_path, "framework/widget/config/handlers.xml",
           '<handlers><handler class="org.apache.ofbiz.WidgetHandler"/></handlers>\n')
    # A genuinely isolated class — no callers, no signature, not in any config.
    dead = "app/src/main/java/org/apache/ofbiz/OrphanUtil.java"
    _write(tmp_path, dead, "public class OrphanUtil {}\n")

    nodes = [
        {"fqn": "org.apache.ofbiz.InvoiceServices", "type": "class",
         "source_file": svc, "in_degree": 0, "out_degree": 0},
        {"fqn": "org.apache.ofbiz.WidgetHandler", "type": "class",
         "source_file": wired, "in_degree": 0, "out_degree": 0},
        {"fqn": "org.apache.ofbiz.OrphanUtil", "type": "class",
         "source_file": dead, "in_degree": 0, "out_degree": 0},
    ]
    unreferenced, dispatched = _partition_static_unreferenced(nodes, tmp_path)
    disp = {n["fqn"] for n in dispatched}
    unref = {n["fqn"] for n in unreferenced}
    assert "org.apache.ofbiz.InvoiceServices" in disp   # signature allowlist
    assert "org.apache.ofbiz.WidgetHandler" in disp     # XML config reference
    assert unref == {"org.apache.ofbiz.OrphanUtil"}     # only the real orphan

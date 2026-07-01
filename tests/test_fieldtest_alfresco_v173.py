"""Regression tests for the v1.73.0 Alfresco Community Repo field-test fixes.

Each test reproduces the exact pattern that produced a false positive / false
negative / inconsistency during a real architecture audit of Alfresco
(Spring, no Spring Boot; iBatis persistence; Camel/ActiveMQ messaging; WebScript
REST surface), so the defect class cannot silently reappear on future repos.

  BUG 1  --compact resolves language_version via a shared Maven property resolver.
  BUG 2  explain --output infers JSON from a .json extension (no silent Markdown).
  BUG 3  entry_points.bootstrap requires a real main()/bootstrap annotation.
  BUG 4  MyBatis framework declared only with real mapper evidence.
  BUG 5  export --integrations covers ActiveMQ factories + Camel routes.
  BUG 6  no-Hibernate repo → hibernate.detected False, readiness not penalized.
  BUG 7  endpoints/api_surface emit a zero_result_reason when 0 are recognized.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from sourcecode.detectors.java import JavaDetector
from sourcecode.detectors.parsers import substitute_maven_properties
from sourcecode.detectors import ProjectDetector
from sourcecode.integration_detector import detect_integrations
from sourcecode.repository_ir import extract_java_endpoints
from sourcecode.migrate_check import run_migrate_check


def _write(root: Path, rel: str, content: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# BUG 1 — Maven property resolution for language_version
# ---------------------------------------------------------------------------

def test_substitute_maven_properties_resolves_chained_ref():
    props = {"java.version": "21", "maven.compiler.source": "${java.version}"}
    assert substitute_maven_properties("${maven.compiler.source}", props) == "21"
    assert substitute_maven_properties("${java.version}", props) == "21"
    # Unknown reference is left verbatim (honest, not blanked).
    assert substitute_maven_properties("${missing}", props) == "${missing}"


def test_language_version_resolves_property_reference(tmp_path):
    # Alfresco shape: maven.compiler.source=${java.version}, java.version=21.
    _write(tmp_path, "pom.xml", """
<project>
  <packaging>war</packaging>
  <properties>
    <java.version>21</java.version>
    <maven.compiler.source>${java.version}</maven.compiler.source>
    <maven.compiler.target>${java.version}</maven.compiler.target>
  </properties>
</project>
""".strip())
    meta = JavaDetector()._parse_pom_metadata(tmp_path / "pom.xml")
    assert meta["language_version"] == "21", "must resolve ${java.version}, not echo it"


# ---------------------------------------------------------------------------
# BUG 2 — explain --output JSON parity
# ---------------------------------------------------------------------------

def _tiny_java_repo(root: Path) -> None:
    _write(root, "src/main/java/org/app/UserService.java",
           "package org.app;\nimport org.springframework.stereotype.Service;\n"
           "@Service\npublic class UserService { public void save() {} }\n")


def test_explain_json_output_extension_infers_json(tmp_path):
    from typer.testing import CliRunner
    from sourcecode.cli import app
    _tiny_java_repo(tmp_path)
    out = tmp_path / "explain.json"
    res = CliRunner().invoke(app, ["explain", "UserService", str(tmp_path), "-o", str(out)])
    assert res.exit_code == 0, res.output
    # BUG 2: a .json output path must contain parseable JSON, not Markdown.
    parsed = json.loads(out.read_text())
    assert isinstance(parsed, dict)


def test_explain_markdown_output_extension_stays_text(tmp_path):
    from typer.testing import CliRunner
    from sourcecode.cli import app
    _tiny_java_repo(tmp_path)
    out = tmp_path / "explain.md"
    res = CliRunner().invoke(app, ["explain", "UserService", str(tmp_path), "-o", str(out)])
    assert res.exit_code == 0, res.output
    text = out.read_text()
    try:
        json.loads(text)
        is_json = True
    except json.JSONDecodeError:
        is_json = False
    assert not is_json, "non-.json output must stay human-readable text/Markdown"


def test_explain_explicit_format_overrides_extension(tmp_path):
    from typer.testing import CliRunner
    from sourcecode.cli import app
    _tiny_java_repo(tmp_path)
    out = tmp_path / "explain.json"
    # Explicit --format text must win even with a .json path.
    res = CliRunner().invoke(
        app, ["explain", "UserService", str(tmp_path), "-o", str(out), "--format", "text"]
    )
    assert res.exit_code == 0, res.output
    try:
        json.loads(out.read_text())
        is_json = True
    except json.JSONDecodeError:
        is_json = False
    assert not is_json, "explicit --format text must override .json inference"


# ---------------------------------------------------------------------------
# BUG 3 — bootstrap entry points require verification
# ---------------------------------------------------------------------------

def test_xsd_application_class_not_bootstrap(tmp_path):
    # XSD/JAXB-generated Application.java: no main(), no bootstrap annotation.
    _write(tmp_path, "src/main/java/org/app/audit/model/Application.java",
           "package org.app.audit.model;\npublic class Application {\n"
           "    protected String name;\n    public String getName() { return name; }\n}\n")
    assert JavaDetector()._verify_bootstrap_entry(
        tmp_path / "src/main/java/org/app/audit/model/Application.java"
    ) is None


def test_real_main_and_annotation_are_bootstrap(tmp_path):
    _write(tmp_path, "Main.java",
           "public class Main { public static void main(String[] args) { } }")
    _write(tmp_path, "BootApp.java",
           "@SpringBootApplication\npublic class BootApp { }")
    d = JavaDetector()
    assert d._verify_bootstrap_entry(tmp_path / "Main.java") == "main_method"
    assert d._verify_bootstrap_entry(tmp_path / "BootApp.java") == "bootstrap_annotation"


def test_detect_excludes_name_only_application_from_entry_points(tmp_path):
    _write(tmp_path, "pom.xml", "<project><packaging>war</packaging></project>")
    _write(tmp_path, "src/main/java/org/app/model/Application.java",
           "package org.app.model;\npublic class Application { protected int id; }\n")
    stacks, entry_points, _ = ProjectDetector([JavaDetector()]).detect(
        root=tmp_path,
        file_tree={"pom.xml": None,
                   "src": {"main": {"java": {"org": {"app": {"model": {"Application.java": None}}}}}}},
        manifests=["pom.xml"],
    )
    app_eps = [e for e in entry_points if e.kind == "application"]
    assert app_eps == [], "an XSD Application.java (no main/annotation) is not bootstrap"


# ---------------------------------------------------------------------------
# BUG 4 — MyBatis only with real usage evidence
# ---------------------------------------------------------------------------

_POM_WITH_MYBATIS = """
<project>
  <packaging>war</packaging>
  <dependencies>
    <dependency><groupId>org.mybatis</groupId><artifactId>mybatis</artifactId></dependency>
  </dependencies>
</project>
""".strip()


def test_mybatis_dropped_without_usage_evidence(tmp_path):
    # org.mybatis coordinate present, but the only *Mapper.java is a bean DTO mapper
    # (no @Mapper) and there is no *Mapper.xml — MyBatis must NOT be declared.
    _write(tmp_path, "pom.xml", _POM_WITH_MYBATIS)
    _write(tmp_path, "src/main/java/org/app/PathMapper.java",
           "package org.app;\npublic class PathMapper { public String map(String p){return p;} }\n")
    stacks, _, _ = ProjectDetector([JavaDetector()]).detect(
        root=tmp_path,
        file_tree={"pom.xml": None,
                   "src": {"main": {"java": {"org": {"app": {"PathMapper.java": None}}}}}},
        manifests=["pom.xml"],
    )
    names = {f.name for s in stacks for f in s.frameworks}
    assert "MyBatis" not in names, "no @Mapper / *Mapper.xml → MyBatis is a false positive"


def test_mybatis_kept_with_annotated_mapper(tmp_path):
    _write(tmp_path, "pom.xml", _POM_WITH_MYBATIS)
    _write(tmp_path, "src/main/java/org/app/UserMapper.java",
           "package org.app;\nimport org.apache.ibatis.annotations.Mapper;\n"
           "@Mapper\npublic interface UserMapper { }\n")
    stacks, _, _ = ProjectDetector([JavaDetector()]).detect(
        root=tmp_path,
        file_tree={"pom.xml": None,
                   "src": {"main": {"java": {"org": {"app": {"UserMapper.java": None}}}}}},
        manifests=["pom.xml"],
    )
    names = {f.name for s in stacks for f in s.frameworks}
    assert "MyBatis" in names, "a real @Mapper interface is genuine MyBatis usage"


# ---------------------------------------------------------------------------
# BUG 5 — ActiveMQ connection factories + Camel routes
# ---------------------------------------------------------------------------

def test_activemq_factory_and_camel_route_detected(tmp_path):
    _write(tmp_path, "ConnectionFactoryConfiguration.java",
           "import org.apache.activemq.ActiveMQSslConnectionFactory;\n"
           "public class ConnectionFactoryConfiguration {\n"
           "  ConnectionFactory f() { return new ActiveMQSslConnectionFactory(url); }\n}\n")
    _write(tmp_path, "RepoNodeEventsRouteBuilder.java",
           "import org.apache.camel.builder.RouteBuilder;\n"
           "public class RepoNodeEventsRouteBuilder extends RouteBuilder {\n"
           "  public void configure() { from(sourceQueue).to(targetTopic); }\n}\n")
    _write(tmp_path, "LiteralRoute.java",
           "import org.apache.camel.builder.RouteBuilder;\n"
           "public class LiteralRoute extends RouteBuilder {\n"
           "  public void configure() { from(\"activemq:queue:foo\").to(\"jms:topic:bar\"); }\n}\n")
    rels = [str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*.java")]
    r = detect_integrations(rels, tmp_path)
    assert r["by_kind"].get("jms", 0) >= 2, "SSL factory + literal activemq/jms route URIs"
    clients = {rec["client"] for rec in r["integrations"]}
    assert "activemq" in clients                       # ActiveMQSslConnectionFactory
    assert any(c == "camel-route" for c in clients)    # variable-URI Camel route
    assert any(c.startswith("camel-") for c in clients)  # literal-URI Camel endpoint


# ---------------------------------------------------------------------------
# BUG 6 — no-Hibernate repo must not be penalized on the Hibernate axis
# ---------------------------------------------------------------------------

def test_no_hibernate_repo_not_penalized(tmp_path):
    # A Spring repo with zero javax/jakarta.persistence and zero Hibernate imports.
    _write(tmp_path, "pom.xml",
           "<project><packaging>war</packaging><dependencies>"
           "<dependency><groupId>org.springframework</groupId>"
           "<artifactId>spring-context</artifactId></dependency>"
           "</dependencies></project>")
    _write(tmp_path, "src/main/java/org/app/Svc.java",
           "package org.app;\nimport org.springframework.stereotype.Service;\n"
           "@Service\npublic class Svc { public int add(int a,int b){return a+b;} }\n")
    files = ["src/main/java/org/app/Svc.java"]
    report = run_migrate_check(files, tmp_path)
    assert report.hibernate is None or report.hibernate.detected is False, \
        "no Hibernate/JPA imports → hibernate must not be detected"
    assert report.headline_blocker != "hibernate_rewrite", \
        "a repo with no Hibernate must never headline a Hibernate rewrite"


# ---------------------------------------------------------------------------
# BUG 7 — zero endpoints must explain why
# ---------------------------------------------------------------------------

def test_zero_endpoints_emits_reason_with_webscripts(tmp_path):
    # WebScript-style repo: no @RestController, but *.desc.xml descriptors present.
    _write(tmp_path, "src/main/java/org/app/PlainBean.java",
           "package org.app;\npublic class PlainBean { public int x(){return 1;} }\n")
    _write(tmp_path, "src/main/resources/webscripts/upload.post.desc.xml",
           "<webscript><url>/api/upload</url></webscript>")
    data = extract_java_endpoints(tmp_path)
    assert data["total"] == 0
    reason = data.get("zero_result_reason", "")
    assert reason, "0 endpoints must carry a structured zero_result_reason"
    assert "WebScript" in reason, "reason must name the detected non-Spring surface"
    assert data.get("non_spring_rest_surface", {}).get("detected") is True


def test_zero_endpoints_reason_without_nonspring_surface(tmp_path):
    _write(tmp_path, "src/main/java/org/app/PlainBean.java",
           "package org.app;\npublic class PlainBean { public int x(){return 1;} }\n")
    data = extract_java_endpoints(tmp_path)
    assert data["total"] == 0
    assert "verify manually" in data.get("zero_result_reason", "").lower()

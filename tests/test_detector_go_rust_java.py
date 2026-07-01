from __future__ import annotations

from pathlib import Path

from sourcecode.detectors.go import GoDetector
from sourcecode.detectors.java import JavaDetector
from sourcecode.detectors.project import ProjectDetector
from sourcecode.detectors.rust import RustDetector


def test_go_detector_detects_gin_cmd_entry(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text(
        "module example.com/app\n\nrequire github.com/gin-gonic/gin v1.10.0\n"
    )
    (tmp_path / "cmd").mkdir()
    (tmp_path / "cmd" / "api").mkdir()
    (tmp_path / "cmd" / "api" / "main.go").write_text("package main")

    detector = ProjectDetector([GoDetector()])
    stacks, entry_points, project_type = detector.detect(
        root=tmp_path,
        file_tree={"go.mod": None, "cmd": {"api": {"main.go": None}}},
        manifests=["go.mod"],
    )

    assert stacks[0].frameworks[0].name == "Gin"
    assert entry_points[0].path == "cmd/api/main.go"
    assert entry_points[0].source == "convention"
    assert entry_points[0].confidence == "medium"
    assert project_type == "api"


def test_rust_detector_detects_axum_main(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text(
        """
[package]
name = "web"
version = "0.1.0"

[dependencies]
axum = "0.7"
        """.strip()
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.rs").write_text("fn main() {}")

    detector = ProjectDetector([RustDetector()])
    stacks, entry_points, project_type = detector.detect(
        root=tmp_path,
        file_tree={"Cargo.toml": None, "src": {"main.rs": None}},
        manifests=["Cargo.toml"],
    )

    assert stacks[0].frameworks[0].name == "Axum"
    assert entry_points[0].path == "src/main.rs"
    assert project_type == "api"


def test_java_detector_detects_spring_boot_application(tmp_path: Path) -> None:
    (tmp_path / "pom.xml").write_text(
        """
<project>
  <dependencies>
    <dependency>
      <artifactId>spring-boot-starter-web</artifactId>
    </dependency>
  </dependencies>
</project>
        """.strip()
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main").mkdir()
    (tmp_path / "src" / "main" / "java").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "main" / "java" / "DemoApplication.java").write_text("class DemoApplication {}")

    detector = ProjectDetector([JavaDetector()])
    stacks, entry_points, project_type = detector.detect(
        root=tmp_path,
        file_tree={
            "pom.xml": None,
            "src": {"main": {"java": {"DemoApplication.java": None}}},
        },
        manifests=["pom.xml"],
    )

    assert stacks[0].frameworks[0].name == "Spring Boot"
    assert entry_points[0].path == "src/main/java/DemoApplication.java"
    assert project_type == "api"


def test_java_detector_does_not_classify_jsr311_api_as_jakarta_ee(tmp_path: Path) -> None:
    # jsr311-api is the old JAX-RS 1.x API spec jar — just interfaces, not a Jakarta EE server.
    # Eureka uses it as transport glue. Should not produce "Jakarta EE".
    # Regression test for BUG-07.
    (tmp_path / "build.gradle").write_text(
        "dependencies {\n    api 'javax.ws.rs:jsr311-api:1.1.1'\n}\n"
    )

    detector = ProjectDetector([JavaDetector()])
    stacks, _entry_points, _project_type = detector.detect(
        root=tmp_path,
        file_tree={"build.gradle": None},
        manifests=["build.gradle"],
    )

    if stacks:
        framework_names = [f.name for f in stacks[0].frameworks]
        assert "Jakarta EE" not in framework_names


def test_java_detector_detects_jakarta_ee_from_jaxrs2_api(tmp_path: Path) -> None:
    # javax.ws.rs:javax.ws.rs-api is the JAX-RS 2.x spec — projects that pull this
    # are actively using JAX-RS as their REST layer and should classify as Jakarta EE.
    (tmp_path / "pom.xml").write_text(
        """
<project>
  <dependencies>
    <dependency>
      <groupId>javax.ws.rs</groupId>
      <artifactId>javax.ws.rs-api</artifactId>
    </dependency>
  </dependencies>
</project>
        """.strip()
    )

    detector = ProjectDetector([JavaDetector()])
    stacks, _entry_points, project_type = detector.detect(
        root=tmp_path,
        file_tree={"pom.xml": None},
        manifests=["pom.xml"],
    )

    framework_names = [f.name for f in stacks[0].frameworks]
    assert "Jakarta EE" in framework_names
    assert project_type == "api"


def test_java_detector_detects_spring_mvc_in_child_module(tmp_path: Path) -> None:
    # Root pom has <modules> but no Spring deps — Spring MVC is in a child pom.
    # Regression test for BUG-06: multi-module Maven projects classified as "unknown".
    (tmp_path / "pom.xml").write_text(
        """
<project>
  <modules>
    <module>web</module>
  </modules>
</project>
        """.strip()
    )
    web_dir = tmp_path / "web"
    web_dir.mkdir()
    (web_dir / "pom.xml").write_text(
        """
<project>
  <dependencies>
    <dependency><artifactId>spring-webmvc</artifactId></dependency>
  </dependencies>
</project>
        """.strip()
    )

    detector = ProjectDetector([JavaDetector()])
    stacks, _entry_points, project_type = detector.detect(
        root=tmp_path,
        file_tree={"pom.xml": None, "web": {"pom.xml": None}},
        manifests=["pom.xml"],
    )

    framework_names = [f.name for f in stacks[0].frameworks]
    assert "Spring MVC" in framework_names
    assert project_type == "api"


# ── v1.71.0 regression: BUG #1 pom exclusion poison (Jenkins field test) ──────

def test_pom_exclude_block_is_not_a_framework(tmp_path: Path) -> None:
    # Jenkins' war/pom.xml lists <exclude>org.springframework:spring-web</exclude>
    # and <exclude>...:spring-aop</exclude> inside an enforcer bytecode rule — those
    # are exclusions of transitive artifacts, NOT declared dependencies. Serializing
    # the whole pom made them read as "uses Spring MVC / Spring AOP". Only real
    # dependency/plugin/parent coordinates must count.
    (tmp_path / "pom.xml").write_text(
        """
<project>
  <dependencies>
    <dependency>
      <groupId>org.springframework.security</groupId>
      <artifactId>spring-security-web</artifactId>
    </dependency>
  </dependencies>
  <build>
    <plugins>
      <plugin>
        <artifactId>extra-enforcer-rules</artifactId>
        <configuration>
          <rules>
            <enforceBytecodeVersion>
              <excludes>
                <exclude>org.springframework:spring-aop</exclude>
                <exclude>org.springframework:spring-web</exclude>
              </excludes>
            </enforceBytecodeVersion>
          </rules>
        </configuration>
      </plugin>
    </plugins>
  </build>
</project>
        """.strip()
    )
    detector = ProjectDetector([JavaDetector()])
    stacks, _ep, _pt = detector.detect(
        root=tmp_path, file_tree={"pom.xml": None}, manifests=["pom.xml"],
    )
    names = [f.name for s in stacks for f in s.frameworks]
    # Real dependency → Spring Security; excluded artifacts → NOT frameworks.
    assert "Spring Security" in names, names
    assert "Spring MVC" not in names, names
    assert "Spring AOP" not in names, names


def test_pom_dependency_exclusion_coordinate_ignored(tmp_path: Path) -> None:
    # A <dependency> with a nested <exclusions><exclusion> must not read the excluded
    # coordinate as a framework — only the dependency's own coordinate counts.
    (tmp_path / "pom.xml").write_text(
        """
<project>
  <dependencies>
    <dependency>
      <groupId>org.example</groupId>
      <artifactId>example-core</artifactId>
      <exclusions>
        <exclusion>
          <groupId>org.springframework</groupId>
          <artifactId>spring-webmvc</artifactId>
        </exclusion>
      </exclusions>
    </dependency>
  </dependencies>
</project>
        """.strip()
    )
    detector = ProjectDetector([JavaDetector()])
    stacks, _ep, _pt = detector.detect(
        root=tmp_path, file_tree={"pom.xml": None}, manifests=["pom.xml"],
    )
    names = [f.name for s in stacks for f in s.frameworks]
    assert "Spring MVC" not in names, names


# ── v1.69.0 regression: BUG #4 framework framing (JobRunr field test) ─────────

def test_gradle_subproject_name_filter_is_not_a_framework(tmp_path: Path) -> None:
    # A multi-module root build.gradle that EXCLUDES a subproject by name must not
    # be read as depending on that framework. JobRunr's root build.gradle has
    # `configure(subprojects.findAll { !it.name.contains('quarkus') })`.
    (tmp_path / "build.gradle").write_text(
        "subprojects {\n"
        "  configure(subprojects.findAll { !it.name.contains('quarkus') "
        "&& it.name != 'platform' }) {\n"
        "    apply plugin: 'java-library'\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    detector = ProjectDetector([JavaDetector()])
    stacks, _ep, _pt = detector.detect(
        root=tmp_path,
        file_tree={"build.gradle": None,
                   "core": {"src": {"main": {"java": {"C.java": None}}}}},
        manifests=["build.gradle"],
    )
    names = [f.name for s in stacks for f in s.frameworks]
    assert "Quarkus" not in names, names


def test_real_gradle_dependency_still_detected(tmp_path: Path) -> None:
    # Control: a genuine Quarkus dependency coordinate is still detected.
    (tmp_path / "build.gradle").write_text(
        "dependencies {\n    implementation 'io.quarkus:quarkus-core:3.0.0'\n}\n",
        encoding="utf-8",
    )
    detector = ProjectDetector([JavaDetector()])
    stacks, _ep, _pt = detector.detect(
        root=tmp_path, file_tree={"build.gradle": None}, manifests=["build.gradle"],
    )
    names = [f.name for s in stacks for f in s.frameworks]
    assert "Quarkus" in names


def test_multi_module_jvm_library_not_unknown(tmp_path: Path) -> None:
    # A multi-module JVM repo with no web/API framework classifies as "library".
    detector = ProjectDetector([JavaDetector()])
    file_tree = {
        "build.gradle": None,
        "settings.gradle": None,
        "core": {"src": {"main": {"java": {"A.java": None, "B.java": None}}}},
        "framework-support": {"adapter": {"src": {"main": {"java": {"C.java": None}}}}},
    }
    (tmp_path / "build.gradle").write_text("subprojects { apply plugin: 'java' }\n")
    _stacks, _ep, project_type = detector.detect(
        root=tmp_path, file_tree=file_tree, manifests=["build.gradle"],
    )
    assert project_type == "library", project_type

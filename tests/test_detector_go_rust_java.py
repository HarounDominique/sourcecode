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

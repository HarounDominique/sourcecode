from __future__ import annotations

from pathlib import Path

from sourcecode.detectors.dotnet import DotnetDetector
from sourcecode.detectors.elixir import ElixirDetector
from sourcecode.detectors.jvm_ext import JvmExtDetector
from sourcecode.detectors.project import ProjectDetector


def test_dotnet_detector_detects_console_program(tmp_path: Path) -> None:
    (tmp_path / "App.csproj").write_text(
        """
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <OutputType>Exe</OutputType>
  </PropertyGroup>
</Project>
        """.strip()
    )
    (tmp_path / "Program.cs").write_text("Console.WriteLine(\"hi\");")

    detector = ProjectDetector([DotnetDetector()])
    stacks, entry_points, project_type = detector.detect(
        root=tmp_path,
        file_tree={"App.csproj": None, "Program.cs": None},
        manifests=[],
    )

    assert stacks[0].stack == "dotnet"
    assert stacks[0].package_manager == "nuget"
    assert entry_points[0].path == "Program.cs"
    assert project_type == "cli"


def test_elixir_detector_detects_phoenix_project(tmp_path: Path) -> None:
    (tmp_path / "mix.exs").write_text(
        """
defmodule Demo.MixProject do
  def project, do: [app: :demo]
  def application, do: []
  defp deps, do: [{:phoenix, "~> 1.7"}]
end
        """.strip()
    )
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "demo").mkdir()
    (tmp_path / "lib" / "demo" / "application.ex").write_text("defmodule Demo.Application do end")

    detector = ProjectDetector([ElixirDetector()])
    stacks, entry_points, project_type = detector.detect(
        root=tmp_path,
        file_tree={"mix.exs": None, "lib": {"demo": {"application.ex": None}}},
        manifests=[],
    )

    assert stacks[0].stack == "elixir"
    assert stacks[0].frameworks[0].name == "Phoenix"
    assert entry_points[0].path == "lib/demo/application.ex"
    assert project_type == "webapp"


def test_jvm_ext_detector_detects_kotlin_ktor_service(tmp_path: Path) -> None:
    (tmp_path / "build.gradle.kts").write_text(
        """
plugins { kotlin("jvm") version "2.0.0" }
dependencies { implementation("io.ktor:ktor-server-core:3.0.0") }
        """.strip()
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main").mkdir()
    (tmp_path / "src" / "main" / "kotlin").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "main" / "kotlin" / "Application.kt").write_text("fun main() {}")

    detector = ProjectDetector([JvmExtDetector()])
    stacks, entry_points, project_type = detector.detect(
        root=tmp_path,
        file_tree={
            "build.gradle.kts": None,
            "src": {"main": {"kotlin": {"Application.kt": None}}},
        },
        manifests=[],
    )

    assert stacks[0].stack == "kotlin"
    assert {item.name for item in stacks[0].frameworks} == {"Ktor"}
    assert entry_points[0].path == "src/main/kotlin/Application.kt"
    assert project_type == "api"


def test_jvm_ext_detector_detects_scala_sbt_project(tmp_path: Path) -> None:
    (tmp_path / "build.sbt").write_text('name := "demo"\nlibraryDependencies += "com.typesafe.play" %% "play" % "2.9.0"')
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main").mkdir()
    (tmp_path / "src" / "main" / "scala").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "main" / "scala" / "Main.scala").write_text("object Main extends App")

    detector = ProjectDetector([JvmExtDetector()])
    stacks, entry_points, project_type = detector.detect(
        root=tmp_path,
        file_tree={
            "build.sbt": None,
            "src": {"main": {"scala": {"Main.scala": None}}},
        },
        manifests=[],
    )

    assert stacks[0].stack == "scala"
    assert {item.name for item in stacks[0].frameworks} == {"Play"}
    assert entry_points[0].path == "src/main/scala/Main.scala"
    assert project_type == "api"

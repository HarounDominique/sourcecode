from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from sourcecode.cli import app

runner = CliRunner()


def test_cli_detects_dotnet_console_project(tmp_path: Path) -> None:
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

    result = runner.invoke(app, ["--mode", "raw", str(tmp_path)])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["stacks"][0]["stack"] == "dotnet"
    assert data["project_type"] == "cli"
    assert data["entry_points"][0]["path"] == "Program.cs"


def test_cli_detects_terraform_and_tooling_signals(tmp_path: Path) -> None:
    (tmp_path / "main.tf").write_text('provider "aws" { region = "eu-west-1" }')
    (tmp_path / "Dockerfile").write_text("FROM alpine:3.20")

    result = runner.invoke(app, ["--mode", "raw", str(tmp_path)])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    stack = data["stacks"][0]
    assert stack["stack"] == "terraform"
    assert "provider:aws" in stack["signals"]
    assert "tooling:docker" in stack["signals"]


def test_cli_detects_cpp_project_from_cmake(tmp_path: Path) -> None:
    (tmp_path / "CMakeLists.txt").write_text("project(demo)")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.cpp").write_text("int main() { return 0; }")

    result = runner.invoke(app, ["--mode", "raw", str(tmp_path)])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["stacks"][0]["stack"] == "cpp"
    assert data["project_type"] == "cli"

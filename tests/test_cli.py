"""Tests de integracion de la CLI."""
from pathlib import Path

import tomllib
from typer.testing import CliRunner

from sourcecode.cli import app

runner = CliRunner()
PROJECT_VERSION = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def test_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "sourcecode" in result.output
    assert PROJECT_VERSION in result.output


def test_help_contains_all_flags():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "--format" in result.output
    assert "--output" in result.output
    assert "--compact" in result.output
    assert "--dependencies" in result.output
    assert "--graph-modules" in result.output
    assert "--graph-detail" in result.output
    assert "--max-nodes" in result.output
    assert "--graph-edges" in result.output
    assert "--no-redact" in result.output


def test_no_args_runs_without_exception(tmp_project: Path):
    result = runner.invoke(app, [str(tmp_project)])
    assert result.exit_code == 0


def test_format_yaml(tmp_project: Path):
    result = runner.invoke(app, ["--format", "yaml", str(tmp_project)])
    assert result.exit_code == 0


def test_format_invalid(tmp_project: Path):
    result = runner.invoke(app, ["--format", "xml", str(tmp_project)])
    assert result.exit_code != 0


def test_output_file(tmp_project: Path, tmp_path: Path):
    out = tmp_path / "out.json"
    result = runner.invoke(app, ["--output", str(out), str(tmp_project)])
    assert result.exit_code == 0
    assert out.exists()


def test_compact(tmp_project: Path):
    result = runner.invoke(app, ["--compact", str(tmp_project)])
    assert result.exit_code == 0

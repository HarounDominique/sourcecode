"""Integration tests for the CLI."""
from pathlib import Path

import tomllib
from typer.testing import CliRunner

from sourcecode.cli import _detected_path, _preprocess_args, app

_runner = CliRunner()
PROJECT_VERSION = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def invoke(args: list[str]):
    """Invoke the CLI with argv preprocessing (mirrors main_entry behaviour)."""
    _detected_path[0] = "."
    processed = _preprocess_args(list(args))
    return _runner.invoke(app, processed)


def test_version():
    result = invoke(["--version"])
    assert result.exit_code == 0
    assert "sourcecode" in result.output
    assert PROJECT_VERSION in result.output


def test_version_subcommand():
    result = invoke(["version"])
    assert result.exit_code == 0
    assert PROJECT_VERSION in result.output


def test_config_subcommand():
    result = invoke(["config"])
    assert result.exit_code == 0
    assert "sourcecode" in result.output


def test_telemetry_status():
    result = invoke(["telemetry", "status"])
    assert result.exit_code == 0
    assert "Telemetry" in result.output


def test_help_contains_all_flags():
    result = invoke(["--help"])
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
    result = invoke([str(tmp_project)])
    assert result.exit_code == 0


def test_path_before_flag(tmp_project: Path):
    result = invoke([str(tmp_project), "--compact"])
    assert result.exit_code == 0


def test_flag_before_path(tmp_project: Path):
    result = invoke(["--compact", str(tmp_project)])
    assert result.exit_code == 0


def test_format_yaml(tmp_project: Path):
    result = invoke(["--format", "yaml", str(tmp_project)])
    assert result.exit_code == 0


def test_format_invalid(tmp_project: Path):
    result = invoke(["--format", "xml", str(tmp_project)])
    assert result.exit_code != 0


def test_output_file(tmp_project: Path, tmp_path: Path):
    out = tmp_path / "out.json"
    result = invoke(["--output", str(out), str(tmp_project)])
    assert result.exit_code == 0
    assert out.exists()


def test_compact(tmp_project: Path):
    result = invoke(["--compact", str(tmp_project)])
    assert result.exit_code == 0

"""Tests de integracion end-to-end del comando sourcecode."""
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sourcecode.cli import app

runner = CliRunner()

# Token de prueba valido para tests de redaccion
FAKE_TOKEN = "ghp_" + "T" * 36


@pytest.fixture
def project_with_env(tmp_path: Path) -> Path:
    """Proyecto con .env que contiene un token falso."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("# main")
    (tmp_path / ".env").write_text(f"API_TOKEN={FAKE_TOKEN}\nDEBUG=true\n")
    (tmp_path / "pyproject.toml").write_text('[project]\nname="test"\n')
    (tmp_path / ".gitignore").write_text("*.log\n")
    (tmp_path / "debug.log").write_text("debug info")
    return tmp_path


def test_full_json_output(tmp_project: Path):
    result = runner.invoke(app, [str(tmp_project)])
    assert result.exit_code == 0, f"Error: {result.output}"
    data = json.loads(result.output)
    assert data["metadata"]["schema_version"] == "1.0"
    assert "file_tree" not in data  # tree is deep-dive, not included by default
    assert data["stacks"][0]["stack"] == "python"
    assert data["project_type"] == "cli"


def test_full_json_output_with_tree(tmp_project: Path):
    result = runner.invoke(app, ["--tree", str(tmp_project)])
    assert result.exit_code == 0, f"Error: {result.output}"
    data = json.loads(result.output)
    assert isinstance(data["file_tree"], dict)
    assert len(data["file_tree"]) > 0


def test_full_yaml_output(tmp_project: Path):
    from io import StringIO

    from ruamel.yaml import YAML

    result = runner.invoke(app, ["--format", "yaml", str(tmp_project)])
    assert result.exit_code == 0, f"Error: {result.output}"
    yaml = YAML()
    data = yaml.load(StringIO(result.output))
    assert data["metadata"]["schema_version"] == "1.0"
    assert "file_tree" not in data  # tree is deep-dive, not included by default


def test_compact_output(tmp_project: Path):
    result = runner.invoke(app, ["--compact", str(tmp_project)])
    assert result.exit_code == 0, f"Error: {result.output}"
    data = json.loads(result.output)
    assert "schema_version" in data
    assert data["stacks"][0]["stack"] == "python"
    assert data["project_type"] == "cli"
    # v0.23.0: file_tree_depth1 removed from compact — noise reduction
    assert "file_tree_depth1" not in data
    assert "file_tree" not in data
    # compact now includes confidence and gaps
    assert "confidence_summary" in data or "analysis_gaps" in data


def test_output_file(tmp_project: Path, tmp_path: Path):
    out = tmp_path / "output.json"
    result = runner.invoke(app, ["--output", str(out), str(tmp_project)])
    assert result.exit_code == 0
    assert out.exists()
    data = json.loads(out.read_text())
    assert data["metadata"]["schema_version"] == "1.0"


def test_env_excluded_from_tree(project_with_env: Path):
    result = runner.invoke(app, ["--tree", str(project_with_env)])
    assert result.exit_code == 0
    data = json.loads(result.output)
    # .env no debe aparecer en el file_tree (SEC-02)
    assert ".env" not in data["file_tree"]


def test_gitignored_files_absent(project_with_env: Path):
    result = runner.invoke(app, ["--tree", str(project_with_env)])
    assert result.exit_code == 0
    data = json.loads(result.output)
    # debug.log esta en .gitignore, no debe aparecer
    assert "debug.log" not in data["file_tree"]


def test_nonexistent_path():
    result = runner.invoke(app, ["/ruta/completamente/inexistente/xyz"])
    assert result.exit_code != 0


def test_metadata_analyzed_path(tmp_project: Path):
    result = runner.invoke(app, [str(tmp_project)])
    assert result.exit_code == 0
    data = json.loads(result.output)
    # analyzed_path debe ser el path resuelto del directorio analizado
    assert str(tmp_project.resolve()) in data["metadata"]["analyzed_path"]

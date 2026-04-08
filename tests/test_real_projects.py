"""Tests de integracion sobre fixtures realistas."""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from sourcecode.cli import app

runner = CliRunner()
FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> Path:
    return FIXTURES / name


def test_nextjs_fixture_schema() -> None:
    result = runner.invoke(app, [str(load_fixture("nextjs_app"))])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["metadata"]["schema_version"] == "1.0"
    assert data["project_type"] == "webapp"
    assert data["stacks"][0]["stack"] == "nodejs"
    assert data["stacks"][0]["package_manager"] == "pnpm"
    assert {framework["name"] for framework in data["stacks"][0]["frameworks"]} == {
        "Next.js",
        "React",
    }
    assert "app" in data["file_tree"]


def test_fastapi_fixture_schema() -> None:
    result = runner.invoke(app, [str(load_fixture("fastapi_app"))])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["metadata"]["schema_version"] == "1.0"
    assert data["project_type"] == "api"
    assert data["stacks"][0]["stack"] == "python"
    assert data["stacks"][0]["frameworks"][0]["name"] == "FastAPI"
    assert data["entry_points"][0]["path"] == "src/main.py"


def test_go_fixture_schema() -> None:
    result = runner.invoke(app, [str(load_fixture("go_service"))])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["metadata"]["schema_version"] == "1.0"
    assert data["project_type"] == "api"
    assert data["stacks"][0]["stack"] == "go"
    assert data["entry_points"][0]["path"] == "cmd/api/main.go"


def test_monorepo_fixture_schema() -> None:
    result = runner.invoke(app, [str(load_fixture("pnpm_monorepo"))])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["metadata"]["schema_version"] == "1.0"
    assert data["project_type"] == "monorepo"
    assert {stack["stack"] for stack in data["stacks"]} == {"nodejs", "python"}
    assert {stack["root"] for stack in data["stacks"]} == {"apps/web", "packages/api"}
    assert any(entry["path"].startswith("apps/web/") for entry in data["entry_points"])

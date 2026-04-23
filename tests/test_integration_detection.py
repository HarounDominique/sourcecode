from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from sourcecode.cli import app

runner = CliRunner()


def test_cli_detects_nextjs_stack(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"next": "15.0.0", "react": "19.0.0"}})
    )
    (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "page.tsx").write_text("export default function Page() {}")

    result = runner.invoke(app, [str(tmp_path)])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["stacks"][0]["stack"] == "nodejs"
    assert data["stacks"][0]["package_manager"] == "pnpm"
    assert {item["name"] for item in data["stacks"][0]["frameworks"]} == {"Next.js", "React"}
    assert data["project_type"] == "webapp"


def test_cli_detects_fastapi_stack(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        """
[project]
name = "api"
dependencies = ["fastapi>=0.115", "uvicorn>=0.30"]
        """.strip()
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("app = None")

    result = runner.invoke(app, [str(tmp_path)])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["stacks"][0]["stack"] == "python"
    assert data["stacks"][0]["frameworks"][0]["name"] == "FastAPI"
    assert data["project_type"] == "api"


def test_cli_uses_heuristic_for_python_without_manifest(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('hi')")

    result = runner.invoke(app, [str(tmp_path)])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["stacks"][0]["stack"] == "python"
    assert data["stacks"][0]["detection_method"] == "heuristic"
    assert data["stacks"][0]["confidence"] == "low"
    assert data["project_type"] == "unknown"


def test_cli_detects_go_entry_point(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text(
        "module example.com/app\n\nrequire github.com/gin-gonic/gin v1.10.0\n"
    )
    (tmp_path / "cmd").mkdir()
    (tmp_path / "cmd" / "api").mkdir()
    (tmp_path / "cmd" / "api" / "main.go").write_text("package main")

    result = runner.invoke(app, [str(tmp_path)])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["entry_points"][0]["path"] == "cmd/api/main.go"
    assert data["project_type"] == "api"

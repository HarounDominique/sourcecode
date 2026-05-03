from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from sourcecode.cli import app

runner = CliRunner()


def test_cli_detects_fullstack_project(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"next": "15.0.0", "react": "19.0.0"}})
    )
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "page.tsx").write_text("export default function Page() {}")
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "pyproject.toml").write_text(
        """
[project]
name = "api"
dependencies = ["fastapi>=0.115"]
        """.strip()
    )
    (tmp_path / "backend" / "main.py").write_text("app = None")

    result = runner.invoke(app, ["--mode", "raw", str(tmp_path)])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["project_type"] == "fullstack"
    assert {stack["stack"] for stack in data["stacks"]} == {"nodejs", "python"}
    assert sum(1 for stack in data["stacks"] if stack["primary"]) == 1
    assert any(stack["root"] == "backend" for stack in data["stacks"] if stack["stack"] == "python")


def test_cli_detects_pnpm_monorepo(tmp_path: Path) -> None:
    (tmp_path / "pnpm-workspace.yaml").write_text("packages:\n  - 'apps/*'\n  - 'packages/*'\n")
    (tmp_path / "apps").mkdir()
    (tmp_path / "apps" / "web").mkdir(parents=True)
    (tmp_path / "apps" / "web" / "package.json").write_text(
        json.dumps({"dependencies": {"next": "15.0.0", "react": "19.0.0"}})
    )
    (tmp_path / "apps" / "web" / "app").mkdir()
    (tmp_path / "apps" / "web" / "app" / "page.tsx").write_text("export default function Page() {}")
    (tmp_path / "packages").mkdir()
    (tmp_path / "packages" / "api").mkdir(parents=True)
    (tmp_path / "packages" / "api" / "pyproject.toml").write_text(
        """
[project]
name = "api"
dependencies = ["fastapi>=0.115"]
        """.strip()
    )
    (tmp_path / "packages" / "api" / "main.py").write_text("app = None")

    result = runner.invoke(app, ["--mode", "raw", str(tmp_path)])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["project_type"] == "monorepo"
    assert {stack["root"] for stack in data["stacks"]} == {"apps/web", "packages/api"}
    assert {stack["stack"] for stack in data["stacks"]} == {"nodejs", "python"}

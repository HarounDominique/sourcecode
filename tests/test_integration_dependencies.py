"""Tests end-to-end de dependencias via CLI."""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from sourcecode.cli import app

runner = CliRunner()


def test_cli_dependencies_flag_enables_dependency_block(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"next": "^15.0.0"}}))
    (tmp_path / "package-lock.json").write_text(
        json.dumps(
            {
                "name": "demo",
                "lockfileVersion": 3,
                "packages": {
                    "": {"dependencies": {"next": "^15.0.0"}},
                    "node_modules/next": {"version": "15.0.0"},
                },
            }
        )
    )

    result = runner.invoke(app, ["--dependencies", str(tmp_path)])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["dependency_summary"]["requested"] is True
    assert any(dep["name"] == "next" for dep in data["dependencies"])


def test_cli_without_dependencies_flag_keeps_block_disabled(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("typer==0.16.0\n")

    result = runner.invoke(app, [str(tmp_path)])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["dependencies"] == []
    assert data["dependency_summary"] is None


def test_cli_dependencies_preserve_workspace_context_in_monorepo(tmp_path: Path) -> None:
    (tmp_path / "pnpm-workspace.yaml").write_text("packages:\n  - 'apps/*'\n  - 'packages/*'\n")
    (tmp_path / "apps" / "web").mkdir(parents=True)
    (tmp_path / "apps" / "web" / "package.json").write_text(
        json.dumps({"dependencies": {"next": "^15.0.0"}})
    )
    (tmp_path / "apps" / "web" / "package-lock.json").write_text(
        json.dumps(
            {
                "name": "web",
                "lockfileVersion": 3,
                "packages": {
                    "": {"dependencies": {"next": "^15.0.0"}},
                    "node_modules/next": {"version": "15.0.0"},
                },
            }
        )
    )
    (tmp_path / "apps" / "web" / "app").mkdir()
    (tmp_path / "apps" / "web" / "app" / "page.tsx").write_text("export default function Page() {}")
    (tmp_path / "packages" / "api").mkdir(parents=True)
    (tmp_path / "packages" / "api" / "pyproject.toml").write_text(
        """
[project]
name = "api"
dependencies = ["fastapi>=0.115"]
        """.strip()
    )
    (tmp_path / "packages" / "api" / "poetry.lock").write_text(
        """
[[package]]
name = "fastapi"
version = "0.115.2"
        """.strip()
    )
    (tmp_path / "packages" / "api" / "main.py").write_text("app = None")

    result = runner.invoke(app, ["--dependencies", str(tmp_path)])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    workspaces = {dep["workspace"] for dep in data["dependencies"] if dep["workspace"]}
    assert {"apps/web", "packages/api"} <= workspaces
    assert data["dependency_summary"]["requested"] is True

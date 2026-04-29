from __future__ import annotations

import json
from pathlib import Path

from sourcecode.dependency_analyzer import DependencyAnalyzer


def test_node_package_lock_reports_direct_and_transitive_dependencies(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"next": "^15.0.0"}, "devDependencies": {"eslint": "^9.0.0"}})
    )
    (tmp_path / "package-lock.json").write_text(
        json.dumps(
            {
                "name": "demo",
                "lockfileVersion": 3,
                "packages": {
                    "": {"dependencies": {"next": "^15.0.0"}, "devDependencies": {"eslint": "^9.0.0"}},
                    "node_modules/next": {"version": "15.0.0", "dependencies": {"react": "19.0.0"}},
                    "node_modules/eslint": {"version": "9.0.0"},
                    "node_modules/react": {"version": "19.0.0"},
                },
            }
        )
    )

    records, summary = DependencyAnalyzer().analyze(tmp_path)

    assert summary.requested is True
    assert summary.ecosystems == ["nodejs"]
    assert summary.transitive_count == 1
    next_dep = next(record for record in records if record.name == "next" and record.scope == "direct")
    react_dep = next(record for record in records if record.name == "react")
    assert next_dep.declared_version == "^15.0.0"
    assert next_dep.resolved_version == "15.0.0"
    assert react_dep.scope == "transitive"
    assert react_dep.parent == "next"


def test_node_pnpm_lock_reports_resolved_versions(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"react": "^19.0.0"}}))
    (tmp_path / "pnpm-lock.yaml").write_text(
        """
lockfileVersion: '9.0'
importers:
  .:
    dependencies:
      react:
        specifier: ^19.0.0
        version: 19.0.0
packages:
  react@19.0.0:
    resolution: {}
  scheduler@0.25.0:
    resolution: {}
    dependencies:
      react: 19.0.0
        """.strip()
    )

    records, summary = DependencyAnalyzer().analyze(tmp_path)

    react_dep = next(record for record in records if record.name == "react")
    assert react_dep.resolved_version == "19.0.0"
    assert "lockfile" in summary.sources


def test_python_poetry_lock_reports_direct_and_transitive_dependencies(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        """
[project]
name = "api"
dependencies = ["fastapi>=0.115"]
        """.strip()
    )
    (tmp_path / "poetry.lock").write_text(
        """
[[package]]
name = "fastapi"
version = "0.115.2"
dependencies = [{ name = "starlette" }]

[[package]]
name = "starlette"
version = "0.38.6"
        """.strip()
    )

    records, summary = DependencyAnalyzer().analyze(tmp_path)

    fastapi_dep = next(record for record in records if record.name == "fastapi")
    starlette_dep = next(record for record in records if record.name == "starlette")
    assert fastapi_dep.declared_version == ">=0.115"
    assert fastapi_dep.resolved_version == "0.115.2"
    assert starlette_dep.scope == "transitive"
    assert starlette_dep.parent == "fastapi"
    assert summary.transitive_count == 1


def test_python_requirements_without_lockfile_keeps_declared_versions(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("typer==0.22.0\nrich>=13.0\n")

    records, summary = DependencyAnalyzer().analyze(tmp_path)

    assert summary.direct_count == 2
    typer_dep = next(record for record in records if record.name == "typer")
    rich_dep = next(record for record in records if record.name == "rich")
    assert typer_dep.declared_version == "==0.26.0"
    assert typer_dep.resolved_version is None
    assert rich_dep.declared_version == ">=13.0"

from __future__ import annotations

from pathlib import Path

from sourcecode.workspace import WorkspaceAnalyzer


def test_workspace_analyzer_detects_pnpm_workspaces(tmp_path: Path) -> None:
    (tmp_path / "pnpm-workspace.yaml").write_text(
        "packages:\n  - 'apps/*'\n  - 'packages/*'\n"
    )
    (tmp_path / "apps").mkdir()
    (tmp_path / "apps" / "web").mkdir()
    (tmp_path / "packages").mkdir()
    (tmp_path / "packages" / "api").mkdir()

    analysis = WorkspaceAnalyzer().analyze(tmp_path, manifests=[])

    assert analysis.is_monorepo is True
    assert {workspace.path for workspace in analysis.workspaces} == {"apps/web", "packages/api"}


def test_workspace_analyzer_detects_go_work_modules(tmp_path: Path) -> None:
    (tmp_path / "go.work").write_text("use (\n    ./services/api\n    ./services/worker\n)\n")
    (tmp_path / "services").mkdir()
    (tmp_path / "services" / "api").mkdir(parents=True)
    (tmp_path / "services" / "worker").mkdir(parents=True)

    analysis = WorkspaceAnalyzer().analyze(tmp_path, manifests=[])

    assert analysis.is_monorepo is True
    assert {workspace.path for workspace in analysis.workspaces} == {
        "services/api",
        "services/worker",
    }


def test_workspace_analyzer_ignores_excluded_dirs(tmp_path: Path) -> None:
    (tmp_path / "pnpm-workspace.yaml").write_text("packages:\n  - 'apps/*'\n  - 'node_modules/*'\n")
    (tmp_path / "apps").mkdir()
    (tmp_path / "apps" / "web").mkdir()
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "bad").mkdir()

    analysis = WorkspaceAnalyzer().analyze(tmp_path, manifests=[])

    assert {workspace.path for workspace in analysis.workspaces} == {"apps/web"}

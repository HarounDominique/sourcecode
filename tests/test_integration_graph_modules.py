from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from sourcecode.cli import app

runner = CliRunner()


def test_cli_graph_modules_flag_enables_module_graph(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("")
    (tmp_path / "app" / "utils.py").write_text("def helper():\n    return 1\n")
    (tmp_path / "app" / "main.py").write_text("from .utils import helper\n\ndef run():\n    return helper()\n")

    result = runner.invoke(app, ["--graph-modules", str(tmp_path)])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["module_graph"]["summary"]["requested"] is True
    assert data["module_graph"]["summary"]["detail"] == "high"
    assert data["module_graph_summary"]["requested"] is True
    assert all(node["kind"] == "module" for node in data["module_graph"]["nodes"])
    assert all(edge["kind"] == "imports" for edge in data["module_graph"]["edges"])
    assert any(node["path"] == "app" for node in data["module_graph"]["nodes"])


def test_cli_graph_modules_full_preserves_function_level_detail(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("")
    (tmp_path / "app" / "utils.py").write_text("def helper():\n    return 1\n")
    (tmp_path / "app" / "main.py").write_text("from .utils import helper\n\ndef run():\n    return helper()\n")

    result = runner.invoke(app, ["--graph-modules", "--graph-detail", "full", str(tmp_path)])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["module_graph"]["summary"]["detail"] == "full"
    assert any(node["kind"] == "function" for node in data["module_graph"]["nodes"])
    assert any(edge["kind"] == "contains" for edge in data["module_graph"]["edges"])


def test_cli_without_graph_modules_keeps_graph_disabled(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("print('ok')\n")

    result = runner.invoke(app, [str(tmp_path)])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["module_graph"] is None
    assert data["module_graph_summary"] is None


def test_cli_graph_modules_preserves_workspace_context_in_monorepo(tmp_path: Path) -> None:
    (tmp_path / "pnpm-workspace.yaml").write_text("packages:\n  - 'apps/*'\n  - 'packages/*'\n")
    (tmp_path / "apps" / "web").mkdir(parents=True)
    (tmp_path / "apps" / "web" / "src").mkdir(parents=True)
    (tmp_path / "apps" / "web" / "src" / "utils.ts").write_text("export function helper() { return 1; }\n")
    (tmp_path / "apps" / "web" / "src" / "index.ts").write_text(
        "import { helper } from './utils';\nexport function run() { return helper(); }\n"
    )
    (tmp_path / "apps" / "web" / "package.json").write_text(json.dumps({"name": "web"}))
    (tmp_path / "packages" / "api").mkdir(parents=True)
    (tmp_path / "packages" / "api" / "__init__.py").write_text("")
    (tmp_path / "packages" / "api" / "main.py").write_text("def run():\n    return 1\n")
    (tmp_path / "packages" / "api" / "pyproject.toml").write_text("[project]\nname='api'\n")

    result = runner.invoke(app, ["--graph-modules", str(tmp_path)])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    workspaces = {node["workspace"] for node in data["module_graph"]["nodes"] if node["workspace"]}
    assert {"apps/web", "packages/api"} <= workspaces
    assert any(
        node["path"].startswith("apps/web") and node["workspace"] == "apps/web"
        for node in data["module_graph"]["nodes"]
    )
    assert not any(
        node["path"].startswith("apps/web") and node["workspace"] is None
        for node in data["module_graph"]["nodes"]
    )


def test_cli_graph_modules_respects_max_nodes_and_reports_truncation(tmp_path: Path) -> None:
    for index in range(8):
        package_dir = tmp_path / f"pkg_{index}"
        package_dir.mkdir()
        (package_dir / "__init__.py").write_text("")
        import_line = f"from pkg_{index + 1}.main import helper\n\n" if index < 7 else ""
        (package_dir / "main.py").write_text(
            f"{import_line}def helper():\n    return {index}\n"
        )

    result = runner.invoke(app, ["--graph-modules", "--max-nodes", "4", str(tmp_path)])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert len(data["module_graph"]["nodes"]) <= 4
    assert data["module_graph_summary"]["truncated"] is True


def test_cli_graph_modules_allows_edge_filter_override(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("")
    (tmp_path / "app" / "utils.py").write_text("def helper():\n    return 1\n")
    (tmp_path / "app" / "main.py").write_text("from .utils import helper\n\ndef run():\n    return helper()\n")

    result = runner.invoke(
        app,
        ["--graph-modules", "--graph-detail", "medium", "--graph-edges", "imports,calls", str(tmp_path)],
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert {"imports", "calls"} >= {edge["kind"] for edge in data["module_graph"]["edges"]}
    assert "contains" not in {edge["kind"] for edge in data["module_graph"]["edges"]}

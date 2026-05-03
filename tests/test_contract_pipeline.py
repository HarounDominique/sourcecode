from __future__ import annotations

"""Tests for ContractPipeline: extraction, ranking, filtering."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sourcecode.cli import app
from sourcecode.contract_pipeline import ContractPipeline, build_dependency_graph

runner = CliRunner()


def _make_ts_project(root: Path) -> list[str]:
    (root / "src").mkdir(exist_ok=True)
    (root / "src" / "service.ts").write_text(
        'import { db } from "./db";\n'
        'import axios from "axios";\n'
        "export interface User { id: string; name: string; }\n"
        "export async function getUser(id: string): Promise<User> { return db.get(id); }\n"
    )
    (root / "src" / "db.ts").write_text(
        "export const db = { get: async (id: string) => ({ id, name: '' }) };\n"
    )
    (root / "src" / "index.ts").write_text(
        'export { getUser } from "./service";\n'
    )
    (root / "main.py").write_text(
        "def main() -> None: pass\n"
    )
    return ["src/service.ts", "src/db.ts", "src/index.ts", "main.py"]


def test_pipeline_extracts_all_source_files(tmp_path: Path) -> None:
    file_paths = _make_ts_project(tmp_path)
    pipeline = ContractPipeline()
    contracts, summary = pipeline.run(tmp_path, file_paths)
    assert summary.extracted_files == 4
    assert summary.total_files == 4
    paths = {c.path for c in contracts}
    assert "src/service.ts" in paths
    assert "main.py" in paths


def test_pipeline_excludes_test_files_by_default(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.ts").write_text("export function run() {}\n")
    (tmp_path / "src" / "app.test.ts").write_text("test('run', () => {})\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text("def test_run(): pass\n")

    file_paths = ["src/app.ts", "src/app.test.ts", "tests/test_app.py"]
    pipeline = ContractPipeline()
    contracts, summary = pipeline.run(tmp_path, file_paths)
    result_paths = {c.path for c in contracts}
    assert "src/app.ts" in result_paths
    assert "src/app.test.ts" not in result_paths
    assert "tests/test_app.py" not in result_paths


def test_pipeline_includes_tests_with_symbol_filter(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_service.py").write_text(
        "from src.service import getUser\n"
        "def test_getUser(): pass\n"
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "service.py").write_text(
        "def getUser(id: str): return id\n"
    )
    file_paths = ["src/service.py", "tests/test_service.py"]
    pipeline = ContractPipeline()
    contracts, _ = pipeline.run(tmp_path, file_paths, symbol="getUser")
    result_paths = {c.path for c in contracts}
    assert "src/service.py" in result_paths
    # test file may be included via symbol search


def test_pipeline_entrypoint_ranked_highest(tmp_path: Path) -> None:
    from sourcecode.schema import EntryPoint
    file_paths = _make_ts_project(tmp_path)
    ep = EntryPoint(path="main.py", stack="python", kind="entry", confidence="high")
    pipeline = ContractPipeline()
    contracts, _ = pipeline.run(tmp_path, file_paths, entry_points=[ep])
    main_c = next(c for c in contracts if c.path == "main.py")
    assert main_c.is_entrypoint is True
    assert main_c.relevance_score >= 0.5
    # Entrypoint should be ranked first or near top
    assert contracts.index(main_c) <= 1


def test_pipeline_max_symbols_limits_output(tmp_path: Path) -> None:
    file_paths = _make_ts_project(tmp_path)
    pipeline = ContractPipeline()
    contracts, summary = pipeline.run(tmp_path, file_paths, max_symbols=5)
    total = sum(len(c.exports) + len(c.functions) + len(c.types) for c in contracts)
    assert total <= 5


def test_pipeline_symbol_filter_narrows_to_relevant_files(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "auth.ts").write_text(
        "export interface AuthToken { value: string; }\n"
        "export function verify(token: AuthToken): boolean { return true; }\n"
    )
    (tmp_path / "src" / "unrelated.ts").write_text(
        "export function doSomething(): void {}\n"
    )
    file_paths = ["src/auth.ts", "src/unrelated.ts"]
    pipeline = ContractPipeline()
    contracts, _ = pipeline.run(tmp_path, file_paths, symbol="AuthToken")
    result_paths = {c.path for c in contracts}
    assert "src/auth.ts" in result_paths
    # unrelated.ts doesn't define/import/use AuthToken
    assert "src/unrelated.ts" not in result_paths


def test_pipeline_rank_by_centrality(tmp_path: Path) -> None:
    file_paths = _make_ts_project(tmp_path)
    pipeline = ContractPipeline()
    contracts, summary = pipeline.run(tmp_path, file_paths, rank_by="centrality")
    assert summary.ranked_by == "centrality"
    assert len(contracts) > 0


def test_pipeline_summary_method_breakdown(tmp_path: Path) -> None:
    file_paths = _make_ts_project(tmp_path)
    pipeline = ContractPipeline()
    _, summary = pipeline.run(tmp_path, file_paths)
    assert "ast" in summary.method_breakdown
    total = sum(summary.method_breakdown.values())
    assert total == summary.extracted_files


def test_build_dependency_graph_produces_nodes(tmp_path: Path) -> None:
    file_paths = _make_ts_project(tmp_path)
    pipeline = ContractPipeline()
    contracts, _ = pipeline.run(tmp_path, file_paths)
    graph = build_dependency_graph(contracts)
    assert "nodes" in graph
    assert "edges" in graph
    assert len(graph["nodes"]) == len(contracts)
    node_paths = {n["path"] for n in graph["nodes"]}
    assert "src/service.ts" in node_paths


# ---------------------------------------------------------------------------
# CLI integration: --mode contract
# ---------------------------------------------------------------------------

def test_cli_contract_mode_default(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname="test"\n')
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("def main(): pass\n")

    result = runner.invoke(app, [str(tmp_path)])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    # Default contract mode renders as minimal
    assert data["mode"] == "minimal"
    assert "contracts" in data
    assert "project" in data
    assert "schema_version" in data


def test_cli_contract_mode_explicit(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname="test"\n')
    (tmp_path / "main.py").write_text("def main(): pass\n")

    result = runner.invoke(app, ["--mode", "contract", str(tmp_path)])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    # --mode contract is an alias for minimal
    assert data["mode"] == "minimal"
    assert isinstance(data["contracts"], list)


def test_cli_raw_mode_preserves_standard_output(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname="test"\n')
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def run(): pass\n")

    result = runner.invoke(app, ["--mode", "raw", str(tmp_path)])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert "metadata" in data
    assert data["metadata"]["schema_version"] == "1.0"
    assert "file_contracts" not in data


def test_cli_max_symbols_flag(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname="test"\n')
    for i in range(5):
        (tmp_path / f"mod{i}.py").write_text(
            f"def func{i}a(): pass\ndef func{i}b(): pass\n"
        )

    # Use --mode standard to get file_contracts with full per-symbol detail
    result = runner.invoke(app, ["--mode", "standard", "--max-symbols", "5", str(tmp_path)])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    contracts = data.get("file_contracts", [])
    total = sum(
        len(c.get("exports", [])) + len(c.get("functions", [])) + len(c.get("types", []))
        for c in contracts
    )
    assert total <= 5


def test_cli_symbol_flag(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname="test"\n')
    (tmp_path / "auth.py").write_text(
        "def verify_token(token: str) -> bool: return True\n"
    )
    (tmp_path / "unrelated.py").write_text("def other(): pass\n")

    result = runner.invoke(app, ["--mode", "contract", "--symbol", "verify_token", str(tmp_path)])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    paths = {c["path"] for c in data.get("contracts", [])}
    assert "auth.py" in paths
    assert "unrelated.py" not in paths


def test_cli_emit_graph_flag(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname="test"\n')
    (tmp_path / "app.py").write_text("def run(): pass\n")

    result = runner.invoke(app, ["--mode", "contract", "--emit-graph", str(tmp_path)])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert "dependency_graph" in data
    assert "nodes" in data["dependency_graph"]
    assert "edges" in data["dependency_graph"]


def test_cli_standard_mode_includes_detail_fields(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname="test"\n')
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("def main(): pass\n")

    result = runner.invoke(app, ["--mode", "standard", str(tmp_path)])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    # Standard mode preserves full detail fields
    assert data["mode"] == "standard"
    assert "schema_version" in data
    assert "stacks" in data
    assert "entry_points" in data
    assert "file_contracts" in data


def test_cli_invalid_mode_exits_nonzero(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--mode", "invalid", str(tmp_path)])
    assert result.exit_code != 0


def test_cli_invalid_rank_by_exits_nonzero(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--rank-by", "bogus", str(tmp_path)])
    assert result.exit_code != 0

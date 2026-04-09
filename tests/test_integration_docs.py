"""Tests de integracion CLI para el flag --docs.

Cubre los acceptance criteria DOCS-ACC-01 a DOCS-ACC-10 del Phase 8.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sourcecode.cli import app

runner = CliRunner()

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _build_docs_fixture(tmp_path: Path) -> None:
    """Crea un proyecto temporal con Python, TS y Go para tests de docs."""
    # Archivo Python con docstrings
    (tmp_path / "mymodule.py").write_text(
        '"""Module docstring."""\n\n'
        "class MyClass:\n"
        '    """Class docstring."""\n\n'
        "    def method(self, x: int) -> str:\n"
        '        """Method docstring."""\n'
        "        pass\n\n"
        'def helper(a: int, b: str = "hi") -> bool:\n'
        '    """Helper function."""\n'
        "    pass\n\n"
        "def no_doc():\n"
        "    pass\n"
    )
    # Archivo TS con JSDoc
    (tmp_path / "utils.ts").write_text(
        "/** Module description. */\n\n"
        "/** Adds two numbers. */\n"
        "export function add(a: number, b: number): number { return a + b; }\n\n"
        "/** MyTSClass docs. */\n"
        "export class MyTSClass {\n"
        "    /** Constructor. */\n"
        "    constructor(private name: string) {}\n"
        "}\n"
    )
    # Archivo Go (no soportado)
    (tmp_path / "main.go").write_text(
        "// Package main does something.\npackage main\n\nfunc main() {}\n"
    )
    # Archivo Python con docstring larga (para truncacion)
    (tmp_path / "long_docs.py").write_text(
        f'"""{" x" * 600}"""\n\ndef func():\n    pass\n'
    )


def _build_monorepo_fixture(tmp_path: Path) -> None:
    """Crea un monorepo pnpm temporal con un workspace que tiene TS."""
    (tmp_path / "pnpm-workspace.yaml").write_text("packages:\n  - 'apps/*'\n")
    (tmp_path / "package.json").write_text('{"name": "root"}\n')
    apps = tmp_path / "apps" / "web"
    apps.mkdir(parents=True)
    (apps / "package.json").write_text('{"name": "web"}\n')
    (apps / "index.ts").write_text("/** Web app entry. */\nexport function main() {}\n")


def _run(args: list[str], cwd: Path) -> tuple[int, dict]:
    """Invoca la CLI con CliRunner y retorna (exit_code, parsed_json)."""
    result = runner.invoke(app, args + [str(cwd)])
    if result.exit_code != 0:
        return result.exit_code, {}
    try:
        return result.exit_code, json.loads(result.output)
    except json.JSONDecodeError:
        return result.exit_code, {}


# ---------------------------------------------------------------------------
# Tests de integracion
# ---------------------------------------------------------------------------


def test_cli_docs_flag_enables_doc_block(tmp_path: Path) -> None:
    """DOCS-ACC-01: --docs sobre Python -> doc_summary.requested=True, docs no vacio."""
    _build_docs_fixture(tmp_path)

    code, data = _run(["--docs", "--format", "json"], tmp_path)

    assert code == 0
    assert data["doc_summary"]["requested"] is True
    assert len(data["docs"]) > 0


def test_cli_without_docs_flag_docs_disabled(tmp_path: Path) -> None:
    """DOCS-ACC-04: Sin --docs -> docs=[], doc_summary=null."""
    _build_docs_fixture(tmp_path)

    code, data = _run(["--format", "json"], tmp_path)

    assert code == 0
    assert data["docs"] == []
    assert data["doc_summary"] is None


def test_cli_docs_source_values_valid(tmp_path: Path) -> None:
    """DOCS-ACC-02: Todos los DocRecord.source in {'docstring','signature','unavailable'}."""
    _build_docs_fixture(tmp_path)
    valid_sources = {"docstring", "signature", "unavailable"}

    code, data = _run(["--docs", "--format", "json"], tmp_path)

    assert code == 0
    assert len(data["docs"]) > 0
    for record in data["docs"]:
        assert record["source"] in valid_sources, (
            f"Unexpected source '{record['source']}' in record {record['symbol']}"
        )


def test_cli_docs_truncation_at_1000_chars(tmp_path: Path) -> None:
    """DOCS-ACC-03: DocRecord.doc_text > 1000 chars termina exactamente en '...[truncated]'."""
    _build_docs_fixture(tmp_path)

    code, data = _run(["--docs", "--format", "json"], tmp_path)

    assert code == 0
    truncated_records = [
        rec for rec in data["docs"]
        if rec.get("doc_text") and rec["doc_text"].endswith("...[truncated]")
    ]
    assert len(truncated_records) > 0, (
        "Expected at least one record with doc_text ending in '...[truncated]'"
    )
    for rec in truncated_records:
        # The truncated text should be 1000 chars + suffix = 1015 chars
        assert len(rec["doc_text"]) == 1000 + len("...[truncated]"), (
            f"Truncated doc_text has unexpected length: {len(rec['doc_text'])}"
        )


def test_cli_docs_unsupported_language_unavailable(tmp_path: Path) -> None:
    """DOCS-ACC-05: .go -> source='unavailable' presente, limitation en summary."""
    _build_docs_fixture(tmp_path)

    code, data = _run(["--docs", "--format", "json"], tmp_path)

    assert code == 0
    go_records = [rec for rec in data["docs"] if rec.get("path", "").endswith(".go")]
    assert len(go_records) > 0, "Expected at least one record for .go file"
    assert all(rec["source"] == "unavailable" for rec in go_records)
    # Check limitation in summary
    limitations = data["doc_summary"]["limitations"]
    assert any("docs_unavailable" in lim for lim in limitations), (
        f"Expected 'docs_unavailable' limitation, got: {limitations}"
    )


def test_cli_docs_depth_module_one_record_per_file(tmp_path: Path) -> None:
    """DOCS-ACC-06: --docs-depth module -> solo kind='module' (un record por archivo)."""
    _build_docs_fixture(tmp_path)

    code, data = _run(["--docs", "--docs-depth", "module", "--format", "json"], tmp_path)

    assert code == 0
    assert len(data["docs"]) > 0
    # All records must have kind="module"
    for rec in data["docs"]:
        assert rec["kind"] == "module", (
            f"Expected kind='module', got '{rec['kind']}' for {rec['symbol']}"
        )


def test_cli_docs_depth_full_includes_methods(tmp_path: Path) -> None:
    """DOCS-ACC-07: --docs-depth full -> al menos un DocRecord con kind='method'."""
    _build_docs_fixture(tmp_path)

    code, data = _run(["--docs", "--docs-depth", "full", "--format", "json"], tmp_path)

    assert code == 0
    kinds = {rec["kind"] for rec in data["docs"]}
    assert "method" in kinds, (
        f"Expected at least one record with kind='method', got kinds: {kinds}"
    )


def test_cli_docs_monorepo_workspace_set(tmp_path: Path) -> None:
    """DOCS-ACC-08: Monorepo -> DocRecord.workspace != None con path correcto."""
    _build_monorepo_fixture(tmp_path)

    code, data = _run(["--docs", "--format", "json"], tmp_path)

    assert code == 0
    ws_records = [rec for rec in data["docs"] if rec.get("workspace") is not None]
    assert len(ws_records) > 0, "Expected at least one DocRecord with workspace != None"
    # Check that the path includes the workspace prefix (OS-agnostic)
    for rec in ws_records:
        ws = rec["workspace"].replace("\\", "/")
        path = rec["path"].replace("\\", "/")
        assert path.startswith(ws + "/"), (
            f"Expected path '{path}' to start with workspace '{ws}/'"
        )


def test_cli_compact_excludes_docs(tmp_path: Path) -> None:
    """DOCS-ACC-09: --compact --docs -> output JSON no contiene claves 'docs' ni 'doc_summary'."""
    _build_docs_fixture(tmp_path)

    result = runner.invoke(app, ["--compact", "--docs", "--format", "json", str(tmp_path)])

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert "docs" not in data, "Compact output should not contain 'docs' key"
    assert "doc_summary" not in data, "Compact output should not contain 'doc_summary' key"


def test_cli_docs_truncated_flag_when_limits_applied(tmp_path: Path) -> None:
    """DOCS-ACC-10: DocSummary.truncated=True cuando algun docstring >1000 chars."""
    _build_docs_fixture(tmp_path)

    code, data = _run(["--docs", "--format", "json"], tmp_path)

    assert code == 0
    assert data["doc_summary"]["truncated"] is True, (
        "Expected doc_summary.truncated=True when docstring exceeds 1000 chars"
    )

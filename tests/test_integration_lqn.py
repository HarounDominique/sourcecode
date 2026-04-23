"""Tests de integracion para LQN-01..06 — LLM Output Quality.

Cada test invoca el CLI sobre el propio directorio del proyecto via CliRunner
y verifica el contrato de los requisitos LQN.
"""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from sourcecode.cli import app

runner = CliRunner()
PROJECT_ROOT = str(Path(__file__).parent.parent)


def _invoke_json(*args: str) -> dict:
    """Invoca CLI y retorna el output como dict. Falla si exit_code != 0.

    Flags must come before the positional PATH argument (Typer/Click convention).
    """
    result = runner.invoke(app, [*args, PROJECT_ROOT])
    assert result.exit_code == 0, f"CLI fallo: {result.output}"
    return json.loads(result.output)


# ---------------------------------------------------------------------------
# LQN-01: file_paths non-empty, all use "/" separator, no backslashes
# ---------------------------------------------------------------------------


def test_lqn01_file_paths() -> None:
    """LQN-01: sourcecode . -> file_paths non-empty, all with '/' separator, no backslashes."""
    output = _invoke_json()

    assert "file_paths" in output, "file_paths key missing from output"
    fps = output["file_paths"]
    assert len(fps) > 0, "file_paths must be non-empty for a real project"
    # All multi-segment paths must use "/" not "\" (OS-normalised)
    assert not any("\\" in p for p in fps), (
        f"LQN-01: Some file_paths contain backslashes: {[p for p in fps if chr(92) in p]}"
    )
    # At least some paths must contain "/" (i.e. nested paths exist)
    assert any("/" in p for p in fps), (
        "LQN-01: Expected at least one nested path with '/' separator"
    )


# ---------------------------------------------------------------------------
# LQN-02: project_summary is a non-None non-trivial string
# ---------------------------------------------------------------------------


def test_lqn02_project_summary() -> None:
    """LQN-02: sourcecode . -> project_summary is non-None string with len > 10."""
    output = _invoke_json()

    assert "project_summary" in output, "project_summary key missing from output"
    ps = output["project_summary"]
    assert isinstance(ps, str), f"LQN-02: project_summary must be str, got {type(ps)}"
    assert len(ps) > 10, f"LQN-02: project_summary too short ({len(ps)} chars): {ps!r}"
    assert "contexto estructurado" in ps.lower(), (
        f"LQN-02: project_summary should reflect the real project description, got: {ps!r}"
    )
    assert "Stack: Python" in ps, (
        f"LQN-02: project_summary should ignore tooling-only Node.js signals, got: {ps!r}"
    )


# ---------------------------------------------------------------------------
# LQN-03: all DocRecords have importance in {high, medium, low}
# ---------------------------------------------------------------------------


def test_lqn03_doc_importance() -> None:
    """LQN-03: sourcecode . --docs -> all DocRecords have importance in {high,medium,low}."""
    output = _invoke_json("--docs")

    docs = output.get("docs", [])
    assert len(docs) > 0, "LQN-03: Expected docs[] to be non-empty with --docs flag"
    valid = {"high", "medium", "low"}
    invalid = [
        rec for rec in docs
        if rec.get("importance") not in valid
    ]
    assert len(invalid) == 0, (
        f"LQN-03: {len(invalid)} records with invalid importance: "
        f"{[{'symbol': r['symbol'], 'importance': r.get('importance')} for r in invalid[:5]]}"
    )


# ---------------------------------------------------------------------------
# LQN-04: no DocRecord has source="unavailable" in docs[]
# ---------------------------------------------------------------------------


def test_lqn04_no_unavailable() -> None:
    """LQN-04: sourcecode . --docs -> no DocRecord has source='unavailable'."""
    output = _invoke_json("--docs")

    docs = output.get("docs", [])
    unavail = [rec for rec in docs if rec.get("source") == "unavailable"]
    assert len(unavail) == 0, (
        f"LQN-04: Found {len(unavail)} source='unavailable' records in docs[]: "
        f"{[r['symbol'] for r in unavail[:5]]}"
    )


# ---------------------------------------------------------------------------
# LQN-05: key_dependencies <= 15, all scope!=transitive, source in manifest/lockfile
# ---------------------------------------------------------------------------


def test_lqn05_key_dependencies() -> None:
    """LQN-05: sourcecode . --dependencies -> key_dependencies <=15, scope!=transitive."""
    output = _invoke_json("--dependencies")

    assert "key_dependencies" in output, "key_dependencies key missing from output"
    kdeps = output["key_dependencies"]
    assert len(kdeps) <= 15, (
        f"LQN-05: key_dependencies has {len(kdeps)} entries, expected <= 15"
    )
    for dep in kdeps:
        assert dep["scope"] != "transitive", (
            f"LQN-05: key_dependency '{dep['name']}' has scope='transitive'"
        )
        assert dep["source"] in ("manifest", "lockfile"), (
            f"LQN-05: key_dependency '{dep['name']}' has unexpected source='{dep['source']}'"
        )


# ---------------------------------------------------------------------------
# LQN-06a: compact view includes project_summary (non-None)
# ---------------------------------------------------------------------------


def test_lqn06_compact_has_project_summary() -> None:
    """LQN-06a: sourcecode . --compact -> project_summary present and non-None."""
    compact = _invoke_json("--compact")

    assert "project_summary" in compact, "LQN-06a: project_summary missing from compact output"
    assert compact["project_summary"] is not None, (
        "LQN-06a: compact project_summary must be non-None for a real project"
    )


def test_lqn06_compact_has_architecture_summary_and_no_file_paths() -> None:
    """LQN-06a+: compact view includes architecture_summary and omits file_paths."""
    compact = _invoke_json("--compact")

    assert "architecture_summary" in compact, (
        "LQN-06a+: architecture_summary missing from compact output"
    )
    assert compact["architecture_summary"] is not None, (
        "LQN-06a+: compact architecture_summary must be non-None for a real project"
    )
    assert "file_paths" not in compact, (
        "LQN-06a+: compact output should omit file_paths in Phase 13"
    )


# ---------------------------------------------------------------------------
# LQN-06b: compact + --dependencies -> dependency_summary present with requested=True
# ---------------------------------------------------------------------------


def test_lqn06_compact_with_dependencies() -> None:
    """LQN-06b: sourcecode . --compact --dependencies -> dependency_summary present."""
    compact = _invoke_json("--compact", "--dependencies")

    assert "dependency_summary" in compact, (
        "LQN-06b: dependency_summary missing from compact --dependencies output"
    )
    dep_sum = compact["dependency_summary"]
    assert dep_sum is not None, (
        "LQN-06b: dependency_summary must be non-None when --dependencies is passed"
    )
    assert dep_sum["requested"] is True, (
        f"LQN-06b: dependency_summary.requested must be True, got {dep_sum['requested']}"
    )


# ---------------------------------------------------------------------------
# LQN-06c: compact without --dependencies -> dependency_summary is None
# ---------------------------------------------------------------------------


def test_lqn06_compact_no_dep_summary_without_flag() -> None:
    """LQN-06c: sourcecode . --compact (no --dependencies) -> dependency_summary is None."""
    compact = _invoke_json("--compact")

    dep_sum = compact.get("dependency_summary")
    assert dep_sum is None, (
        f"LQN-06c: compact without --dependencies should have dependency_summary=None, "
        f"got {dep_sum!r}"
    )


# ---------------------------------------------------------------------------
# ARCH-01..03: --architecture flag produces architectural analysis
# ---------------------------------------------------------------------------


def test_architecture_flag_produces_analysis() -> None:
    """ARCH-01..03: sourcecode . --architecture -> architecture field present with pattern/domains/layers/bounded_contexts."""
    output = _invoke_json("--architecture")

    assert "architecture" in output, "architecture key missing from output"
    arch = output["architecture"]
    assert arch is not None, "architecture must not be None when --architecture is passed"
    assert "pattern" in arch, "architecture.pattern missing"
    assert "domains" in arch, "architecture.domains missing"
    assert "layers" in arch, "architecture.layers missing"
    assert "bounded_contexts" in arch, "architecture.bounded_contexts missing"
    assert arch["requested"] is True, "architecture.requested must be True"


def test_no_architecture_flag_omits_key() -> None:
    """ARCH: sourcecode . (sin --architecture) -> architecture field is None."""
    output = _invoke_json()

    assert output.get("architecture") is None, (
        "architecture should be None when --architecture flag is not passed"
    )

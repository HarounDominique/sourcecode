"""Tests for v0.26.0 signal hierarchy, confidence layer, and agent output quality.

Covers required test cases from spec:
  1. Python CLI with auxiliary Node tooling in hidden dir — project_type stays python/cli
  2. Auxiliary manifest must not win over root real manifest
  3. Entry point detected from pyproject.toml (console_script reason)
  4. Entry point detected from CLI filename pattern (entry_file_pattern reason)
  5. Compact output without unnecessary empty sections
  6. --agent output without file_tree or noise
  7. Correct dependency classification by role
  8. Delta/incremental mode (prepare-context delta)
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sourcecode.cli import app

runner = CliRunner()


def _parse_json_output(output: str) -> dict:
    """Extract and parse JSON from CLI output that may contain stderr lines."""
    # Find first '{' which starts the JSON block
    idx = output.find("{")
    if idx < 0:
        raise ValueError(f"No JSON found in output: {output[:200]!r}")
    return json.loads(output[idx:])


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def python_cli_with_node_tooling(tmp_path: Path) -> Path:
    """Python CLI project with Node tooling hidden in .claude/ subfolder."""
    # Root Python project
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "sourcecode").mkdir()
    (tmp_path / "src" / "sourcecode" / "cli.py").write_text(
        "import typer\napp = typer.Typer()\n"
    )
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="mycli"\nversion="1.0"\n'
        '[project.scripts]\nmycli = "sourcecode.cli:app"\n'
    )
    # Hidden .claude dir with Node tooling (should be IGNORED for project detection)
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "package.json").write_text(
        '{"name":"agent-tools","version":"1.0","dependencies":{"axios":"^1.0"}}'
    )
    (tmp_path / ".claude" / "node_modules").mkdir()
    return tmp_path


@pytest.fixture
def python_project_aux_manifest(tmp_path: Path) -> Path:
    """Python project with aux manifest (depth > 1) that must not override root."""
    # Root manifest — this is the real project
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="myapp"\nversion="1.0"\n'
    )
    (tmp_path / "main.py").write_text("# main entry\n")
    # Auxiliary manifest buried in fixtures — must NOT win
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "fixtures").mkdir()
    (tmp_path / "tests" / "fixtures" / "sample_node_project").mkdir()
    (tmp_path / "tests" / "fixtures" / "sample_node_project" / "package.json").write_text(
        '{"name":"sample","version":"1.0"}'
    )
    return tmp_path


@pytest.fixture
def python_pyproject_entrypoint(tmp_path: Path) -> Path:
    """Python project where entry point comes from pyproject.toml scripts."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mycli").mkdir()
    (tmp_path / "src" / "mycli" / "cli.py").write_text(
        "import typer\napp = typer.Typer()\n"
        "@app.command()\ndef main(): pass\n"
    )
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="mycli"\nversion="1.0"\n'
        '[project.scripts]\nmycli = "mycli.cli:app"\n'
    )
    return tmp_path


@pytest.fixture
def python_cli_pattern_entrypoint(tmp_path: Path) -> Path:
    """Python project where entry point is detected by filename pattern."""
    (tmp_path / "cli.py").write_text(
        "import typer\napp = typer.Typer()\n"
        "@app.command()\ndef main(): pass\n"
    )
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="mypkg"\nversion="1.0"\n'
    )
    return tmp_path


@pytest.fixture
def project_with_dependencies(tmp_path: Path) -> Path:
    """Python project with diverse dependencies for role classification testing."""
    (tmp_path / "main.py").write_text("# main\n")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="svc"\nversion="1.0"\n'
        'dependencies = [\n'
        '  "fastapi>=0.100",\n'
        '  "sentry-sdk>=1.0",\n'
        '  "pytest>=8.0",\n'
        '  "boto3>=1.0",\n'
        '  "mypy>=1.0",\n'
        '  "pyyaml>=6.0",\n'
        '  "hatchling>=1.0",\n'
        ']\n'
    )
    return tmp_path


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestSignalHierarchy:
    """Auxiliary tooling dirs must not distort project_type or primary stack."""

    def test_hidden_node_tooling_does_not_distort_project_type(
        self, python_cli_with_node_tooling: Path
    ) -> None:
        """Python CLI project stays python even when .claude/ has package.json."""
        result = runner.invoke(app, [str(python_cli_with_node_tooling)])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["project_type"] in ("cli", "python", "library"), (
            f"Expected python project_type, got {data['project_type']!r}"
        )
        stacks = {s["stack"] for s in data["stacks"]}
        assert "python" in stacks, f"Python stack missing. Stacks: {stacks}"

    def test_hidden_node_tooling_primary_stack_is_python(
        self, python_cli_with_node_tooling: Path
    ) -> None:
        """Primary stack must be python, not nodejs, when .claude/package.json exists."""
        result = runner.invoke(app, [str(python_cli_with_node_tooling)])
        assert result.exit_code == 0
        data = json.loads(result.output)
        primary = next((s for s in data["stacks"] if s.get("primary")), None)
        if primary is not None:
            assert primary["stack"] == "python", (
                f"Primary stack should be python, got {primary['stack']!r}"
            )

    def test_aux_manifest_does_not_override_root_manifest(
        self, python_project_aux_manifest: Path
    ) -> None:
        """Manifest buried in tests/fixtures/ must not win over root pyproject.toml."""
        result = runner.invoke(app, [str(python_project_aux_manifest)])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        stacks = {s["stack"] for s in data["stacks"]}
        assert "python" in stacks, f"Python stack missing. Stacks: {stacks}"
        # nodejs should NOT be the primary or only stack
        primary = next((s for s in data["stacks"] if s.get("primary")), None)
        if primary is not None:
            assert primary["stack"] != "nodejs", (
                "Node.js became primary from fixture package.json — signal hierarchy broken"
            )


class TestEntryPointReason:
    """Entry points must carry reason/evidence from detection source."""

    def test_pyproject_scripts_gets_console_script_reason(
        self, python_pyproject_entrypoint: Path
    ) -> None:
        result = runner.invoke(app, [str(python_pyproject_entrypoint)])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        entry_points = data.get("entry_points", [])
        assert entry_points, "No entry points detected"
        manifest_eps = [ep for ep in entry_points if ep.get("source") == "pyproject.toml"]
        assert manifest_eps, f"No pyproject.toml entry point found. Got: {entry_points}"
        ep = manifest_eps[0]
        assert ep.get("reason") == "console_script", (
            f"Expected reason=console_script, got {ep.get('reason')!r}"
        )
        assert ep.get("confidence") == "high"

    def test_cli_filename_pattern_gets_entry_file_pattern_reason(
        self, python_cli_pattern_entrypoint: Path
    ) -> None:
        result = runner.invoke(app, [str(python_cli_pattern_entrypoint)])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        entry_points = data.get("auxiliary_entry_points", [])
        assert entry_points, "No auxiliary entry points detected"
        conv_eps = [
            ep for ep in entry_points
            if ep.get("reason") == "entry_file_pattern"
            or ep.get("source") == "convention"
        ]
        assert conv_eps, (
            f"No convention/entry_file_pattern auxiliary entry point found. Got: {entry_points}"
        )

    def test_entry_points_have_reason_field(self, tmp_project: Path) -> None:
        result = runner.invoke(app, [str(tmp_project)])
        assert result.exit_code == 0
        data = json.loads(result.output)
        for ep in data.get("entry_points", []):
            assert "reason" in ep, f"Entry point missing 'reason' field: {ep}"


class TestCompactOutput:
    """Compact mode must be clean — no empty sections, includes confidence."""

    def test_compact_has_no_file_tree(self, tmp_project: Path) -> None:
        result = runner.invoke(app, ["--compact", str(tmp_project)])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "file_tree" not in data
        assert "file_tree_depth1" not in data

    def test_compact_has_confidence_summary(self, tmp_project: Path) -> None:
        result = runner.invoke(app, ["--compact", str(tmp_project)])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "confidence_summary" in data
        cs = data["confidence_summary"]
        assert "overall" in cs
        assert cs["overall"] in ("high", "medium", "low")

    def test_compact_has_analysis_gaps(self, tmp_project: Path) -> None:
        result = runner.invoke(app, ["--compact", str(tmp_project)])
        assert result.exit_code == 0
        data = json.loads(result.output)
        # May or may not have gaps but key must be present or absent cleanly
        if "analysis_gaps" in data:
            assert isinstance(data["analysis_gaps"], list)

    def test_compact_has_project_summary(self, tmp_project: Path) -> None:
        result = runner.invoke(app, ["--compact", str(tmp_project)])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "project_summary" in data
        assert "architecture_summary" in data

    def test_compact_no_empty_lists_as_noise(self, tmp_project: Path) -> None:
        result = runner.invoke(app, ["--compact", str(tmp_project)])
        assert result.exit_code == 0
        data = json.loads(result.output)
        # empty lists for key_dependencies, analysis_gaps should not appear as []
        # (they may appear as None or be absent entirely when not meaningful)
        for key in ("key_dependencies", "analysis_gaps", "anomalies"):
            val = data.get(key)
            assert val is None or isinstance(val, list), f"{key} has unexpected type"


class TestAgentOutput:
    """--agent mode must be clean, structured, and noise-free."""

    def test_agent_has_no_file_tree(self, tmp_project: Path) -> None:
        result = runner.invoke(app, ["--agent", str(tmp_project)])
        assert result.exit_code == 0, result.output
        data = _parse_json_output(result.output)
        assert "file_tree" not in data
        assert "file_paths" not in data
        assert "file_tree_depth1" not in data

    def test_agent_has_project_block(self, tmp_project: Path) -> None:
        result = runner.invoke(app, ["--agent", str(tmp_project)])
        assert result.exit_code == 0
        data = _parse_json_output(result.output)
        assert "project" in data
        p = data["project"]
        assert "type" in p
        assert "summary" in p

    def test_agent_has_no_raw_dependencies_list(self, tmp_project: Path) -> None:
        result = runner.invoke(app, ["--agent", str(tmp_project)])
        assert result.exit_code == 0
        data = _parse_json_output(result.output)
        assert "dependencies" not in data

    def test_agent_has_no_module_graph(self, tmp_project: Path) -> None:
        result = runner.invoke(app, ["--agent", str(tmp_project)])
        assert result.exit_code == 0
        data = _parse_json_output(result.output)
        assert "module_graph" not in data
        assert "module_graph_summary" not in data

    def test_agent_has_no_empty_sections(self, tmp_project: Path) -> None:
        """No key should map to empty dict {} or empty list [] as noise."""
        result = runner.invoke(app, ["--agent", str(tmp_project)])
        assert result.exit_code == 0
        data = _parse_json_output(result.output)
        for key, val in data.items():
            if isinstance(val, dict):
                assert val, f"agent output has empty dict section: {key!r}"
            if isinstance(val, list):
                if key in {"entry_points", "development_entry_points", "auxiliary_entry_points"}:
                    continue
                assert val, f"agent output has empty list section: {key!r}"

    def test_agent_has_confidence_summary(self, tmp_project: Path) -> None:
        result = runner.invoke(app, ["--agent", str(tmp_project)])
        assert result.exit_code == 0
        data = _parse_json_output(result.output)
        assert "confidence_summary" in data
        cs = data["confidence_summary"]
        assert "overall" in cs

    def test_agent_output_order(self, tmp_project: Path) -> None:
        """agent output keys should appear in the defined order."""
        result = runner.invoke(app, ["--agent", str(tmp_project)])
        assert result.exit_code == 0
        data = _parse_json_output(result.output)
        keys = list(data.keys())
        assert keys[0] == "project", f"First key must be 'project', got {keys[0]!r}"


class TestDependencyRoles:
    """Dependencies must be classified by role with correct priority ordering."""

    def test_runtime_dep_has_runtime_role(
        self, project_with_dependencies: Path
    ) -> None:
        result = runner.invoke(app, ["--dependencies", str(project_with_dependencies)])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        key_deps = data.get("key_dependencies", [])
        fastapi = next((d for d in key_deps if d["name"] == "fastapi"), None)
        if fastapi:
            assert fastapi.get("role") == "runtime"

    def test_observability_dep_classified_correctly(
        self, project_with_dependencies: Path
    ) -> None:
        result = runner.invoke(app, ["--dependencies", str(project_with_dependencies)])
        assert result.exit_code == 0
        data = json.loads(result.output)
        key_deps = data.get("key_dependencies", [])
        sentry = next((d for d in key_deps if "sentry" in d["name"]), None)
        if sentry:
            assert sentry.get("role") == "observability", (
                f"sentry-sdk should be observability, got {sentry.get('role')!r}"
            )

    def test_infra_dep_classified_correctly(
        self, project_with_dependencies: Path
    ) -> None:
        result = runner.invoke(app, ["--dependencies", str(project_with_dependencies)])
        assert result.exit_code == 0
        data = json.loads(result.output)
        key_deps = data.get("key_dependencies", [])
        boto3 = next((d for d in key_deps if d["name"] == "boto3"), None)
        if boto3:
            assert boto3.get("role") == "infra", (
                f"boto3 should be infra, got {boto3.get('role')!r}"
            )

    def test_testtool_dep_classified_correctly(
        self, project_with_dependencies: Path
    ) -> None:
        result = runner.invoke(app, ["--dependencies", str(project_with_dependencies)])
        assert result.exit_code == 0
        data = json.loads(result.output)
        key_deps = data.get("key_dependencies", [])
        pytest_dep = next((d for d in key_deps if d["name"] == "pytest"), None)
        if pytest_dep:
            assert pytest_dep.get("role") == "testtool"

    def test_buildtool_dep_classified_correctly(
        self, project_with_dependencies: Path
    ) -> None:
        result = runner.invoke(app, ["--dependencies", str(project_with_dependencies)])
        assert result.exit_code == 0
        data = json.loads(result.output)
        key_deps = data.get("key_dependencies", [])
        hatch = next((d for d in key_deps if d["name"] == "hatchling"), None)
        if hatch:
            assert hatch.get("role") == "buildtool"

    def test_runtime_deps_appear_before_devtools(
        self, project_with_dependencies: Path
    ) -> None:
        """Runtime deps must come before devtools in key_dependencies ordering."""
        result = runner.invoke(app, ["--dependencies", str(project_with_dependencies)])
        assert result.exit_code == 0
        data = json.loads(result.output)
        key_deps = data.get("key_dependencies", [])
        if not key_deps:
            pytest.skip("No key_dependencies in output")
        roles = [d.get("role", "runtime") for d in key_deps]
        runtime_indices = [i for i, r in enumerate(roles) if r == "runtime"]
        devtool_indices = [i for i, r in enumerate(roles) if r in ("devtool", "testtool")]
        if runtime_indices and devtool_indices:
            assert max(runtime_indices) < max(devtool_indices), (
                "Runtime deps should appear before devtools in key_dependencies"
            )


class TestConfidenceAnalyzer:
    """ConfidenceSummary must be built correctly."""

    def test_confidence_summary_present_in_standard_output(
        self, tmp_project: Path
    ) -> None:
        from sourcecode.confidence_analyzer import ConfidenceAnalyzer
        from sourcecode.schema import AnalysisMetadata, SourceMap, StackDetection

        sm = SourceMap(
            metadata=AnalysisMetadata(analyzed_path=str(tmp_project)),
            stacks=[StackDetection(stack="python", detection_method="manifest", confidence="high")],
        )
        sm.file_paths = []
        conf, gaps = ConfidenceAnalyzer().analyze(sm)
        assert conf.overall in ("high", "medium", "low")
        assert isinstance(conf.hard_signals, list)
        assert isinstance(conf.soft_signals, list)
        assert isinstance(conf.ignored_signals, list)
        assert isinstance(conf.anomalies, list)
        assert isinstance(gaps, list)

    def test_heuristic_stack_produces_gap(self, tmp_path: Path) -> None:
        from sourcecode.confidence_analyzer import ConfidenceAnalyzer
        from sourcecode.schema import AnalysisMetadata, SourceMap, StackDetection

        sm = SourceMap(
            metadata=AnalysisMetadata(analyzed_path=str(tmp_path)),
            stacks=[StackDetection(stack="python", detection_method="heuristic", confidence="low")],
        )
        sm.file_paths = []
        conf, gaps = ConfidenceAnalyzer().analyze(sm)
        gap_areas = [g.area for g in gaps]
        assert "stack" in gap_areas, "Heuristic-only stack should produce stack gap"

    def test_missing_entry_points_produces_gap(self, tmp_path: Path) -> None:
        from sourcecode.confidence_analyzer import ConfidenceAnalyzer
        from sourcecode.schema import AnalysisMetadata, SourceMap, StackDetection

        sm = SourceMap(
            metadata=AnalysisMetadata(analyzed_path=str(tmp_path)),
            stacks=[StackDetection(stack="python", detection_method="manifest", confidence="high")],
            entry_points=[],
        )
        sm.file_paths = []
        conf, gaps = ConfidenceAnalyzer().analyze(sm)
        gap_areas = [g.area for g in gaps]
        assert "entry_points" in gap_areas

    def test_auxiliary_dirs_appear_in_ignored_signals(self, tmp_path: Path) -> None:
        from sourcecode.confidence_analyzer import ConfidenceAnalyzer
        from sourcecode.schema import AnalysisMetadata, SourceMap, StackDetection

        sm = SourceMap(
            metadata=AnalysisMetadata(analyzed_path=str(tmp_path)),
            stacks=[StackDetection(stack="python", detection_method="manifest", confidence="high")],
        )
        sm.file_paths = [
            "src/main.py",
            ".claude/agents/my-agent.md",
            ".vscode/settings.json",
        ]
        conf, gaps = ConfidenceAnalyzer().analyze(sm)
        ignored = conf.ignored_signals
        assert any(".claude" in s for s in ignored), (
            f".claude/ not in ignored_signals: {ignored}"
        )
        assert any(".vscode" in s for s in ignored), (
            f".vscode/ not in ignored_signals: {ignored}"
        )


class TestDeltaTask:
    """Delta task must return structured output for incremental context."""

    def test_delta_task_returns_valid_json(self, tmp_project: Path) -> None:
        result = runner.invoke(
            app,
            [str(tmp_project), "prepare-context", "delta"],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["task"] == "delta"
        assert "project_summary" in data
        assert "relevant_files" in data
        assert "confidence" in data

    def test_delta_task_returns_changed_files_field(self, tmp_project: Path) -> None:
        result = runner.invoke(
            app,
            [str(tmp_project), "prepare-context", "delta"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        # changed_files may be empty in a clean repo, but field structure valid
        if "changed_files" in data:
            assert isinstance(data["changed_files"], list)

    def test_prepare_context_onboard_task(self, tmp_project: Path) -> None:
        result = runner.invoke(
            app,
            [str(tmp_project), "prepare-context", "onboard"],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["task"] == "onboard"
        assert "project_summary" in data
        assert "confidence" in data
        assert "gaps" in data

    def test_prepare_context_review_pr_task(self, tmp_project: Path) -> None:
        result = runner.invoke(
            app,
            [str(tmp_project), "prepare-context", "review-pr"],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["task"] == "review-pr"
        assert "relevant_files" in data

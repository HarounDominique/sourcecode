"""E2E pipeline integrity tests.

Verifies that:
1. Benchmark / example entry points never contaminate agent output.
2. When only auxiliary EPs exist, confidence degrades and gaps are correct.
3. produced_by provenance is stamped on every emitted EP and stack.
4. analyzer_fingerprints are present in metadata and change when rules change.
5. --trace-pipeline emits a pipeline_trace block with correct event types.
6. _check_pipeline_coherence catches contradictory confidence / EP states.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sourcecode.cli import app, _check_pipeline_coherence, _compute_analyzer_fingerprints
from sourcecode.confidence_analyzer import ConfidenceAnalyzer
from sourcecode.schema import (
    AnalysisMetadata,
    ConfidenceSummary,
    EntryPoint,
    SourceMap,
    StackDetection,
)
from sourcecode.serializer import normalize_source_map

runner = CliRunner()


def _parse_agent(output: str) -> dict:
    idx = output.find("{")
    if idx < 0:
        raise ValueError(f"No JSON in output: {output[:300]!r}")
    return json.loads(output[idx:])


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def nocobase_like(tmp_path: Path) -> Path:
    """Repo whose scripts are docs/benchmark/example — mimics NocoBase tooling."""
    (tmp_path / "package.json").write_text(json.dumps({
        "name": "nocobase-like",
        "version": "1.0.0",
        "scripts": {
            "docs": "rspress dev",
            "benchmark": "node benchmarks/run.js",
            "example": "node examples/demo.js",
        },
        "dependencies": {"express": "^4.0.0"},
    }))
    (tmp_path / "benchmarks").mkdir()
    (tmp_path / "benchmarks" / "run.js").write_text("// bench\n")
    (tmp_path / "examples").mkdir()
    (tmp_path / "examples" / "demo.js").write_text("// demo\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "rspress.mjs").write_text("export default {};\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "index.js").write_text("// placeholder\n")
    return tmp_path


@pytest.fixture
def production_nodejs(tmp_path: Path) -> Path:
    """Repo with a real 'start' script and a benchmark script at root level.

    The benchmark script intentionally points to a non-auxiliary-dir file
    (run.js at root) so the EP reaches sm.entry_points with
    entrypoint_type='benchmark' and is then filtered at the output layer —
    allowing the trace to capture the filter_ep event.
    """
    (tmp_path / "package.json").write_text(json.dumps({
        "name": "my-server",
        "version": "1.0.0",
        "scripts": {
            "start": "node src/server.js",
            "benchmark": "node run.js",  # root-level file → reaches sm.entry_points
        },
        "dependencies": {"express": "^4.0.0"},
    }))
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "server.js").write_text("const express = require('express');\n")
    (tmp_path / "run.js").write_text("// benchmark runner\n")
    return tmp_path


@pytest.fixture
def benchmark_only_sm() -> SourceMap:
    """SourceMap where all entry points are explicitly typed as benchmark."""
    return normalize_source_map(SourceMap(
        metadata=AnalysisMetadata(analyzed_path="/fake"),
        stacks=[StackDetection(stack="nodejs", detection_method="manifest", confidence="high")],
        entry_points=[
            EntryPoint(
                path="benchmarks/run.js",
                stack="nodejs",
                entrypoint_type="benchmark",
                source="package.json#scripts",
                confidence="high",
                reason="script:benchmark",
                produced_by="nodejs",
            ),
        ],
    ))


# ---------------------------------------------------------------------------
# Test 1: Benchmark EPs must not appear in --agent output
# ---------------------------------------------------------------------------

class TestBenchmarkContamination:
    """NocoBase-like: only benchmark/example scripts in package.json."""

    def test_agent_omits_benchmark_entry_points(self, nocobase_like: Path) -> None:
        result = runner.invoke(app, ["--agent", str(nocobase_like)])
        assert result.exit_code == 0, result.output
        data = _parse_agent(result.output)
        entry_points = data.get("entry_points", [])
        benchmark_eps = [
            ep for ep in entry_points
            if ep.get("entrypoint_type") in ("benchmark", "example")
            or any(
                p in {"benchmark", "benchmarks", "example", "examples"}
                for p in ep.get("path", "").replace("\\", "/").split("/")
            )
        ]
        assert not benchmark_eps, (
            f"Benchmark/example EPs leaked into --agent output: {benchmark_eps}"
        )

    def test_agent_no_fallback_to_auxiliary_eps(self, nocobase_like: Path) -> None:
        """When no operational EP exists, agent must NOT fall back to auxiliary list."""
        result = runner.invoke(app, ["--agent", str(nocobase_like)])
        assert result.exit_code == 0, result.output
        data = _parse_agent(result.output)
        assert data.get("entry_points") == []

    def test_agent_splits_development_and_auxiliary_eps(self, nocobase_like: Path) -> None:
        result = runner.invoke(app, ["--agent", str(nocobase_like)])
        assert result.exit_code == 0, result.output
        data = _parse_agent(result.output)

        dev_eps = data.get("development_entry_points", [])
        aux_eps = data.get("auxiliary_entry_points", [])

        assert any(ep.get("path") == "docs/rspress.mjs" for ep in dev_eps), dev_eps
        assert all(ep.get("classification") == "development" for ep in dev_eps), dev_eps
        assert all(ep.get("runtime_relevance") == "low" for ep in dev_eps), dev_eps
        assert any(ep.get("path") == "benchmarks/run.js" for ep in aux_eps), aux_eps
        assert any(ep.get("path") == "examples/demo.js" for ep in aux_eps), aux_eps
        assert all(ep.get("classification") == "auxiliary" for ep in aux_eps), aux_eps

    def test_production_server_survives_benchmark_coexistence(
        self, production_nodejs: Path
    ) -> None:
        """Production 'start' EP must appear; benchmark EP must not."""
        result = runner.invoke(app, ["--agent", str(production_nodejs)])
        assert result.exit_code == 0, result.output
        data = _parse_agent(result.output)
        entry_points = data.get("entry_points", [])
        assert entry_points, "Production EP must be present"
        prod_eps = [
            ep for ep in entry_points
            if ep.get("entrypoint_type") == "production"
            or ep.get("entrypoint_type") is None
        ]
        assert prod_eps, f"No production EP found; got: {entry_points}"
        bench_eps = [
            ep for ep in entry_points
            if ep.get("entrypoint_type") == "benchmark"
        ]
        assert not bench_eps, f"Benchmark EP leaked: {bench_eps}"


# ---------------------------------------------------------------------------
# Test 2: Confidence must degrade when all EPs are auxiliary
# ---------------------------------------------------------------------------

class TestConfidenceDegradation:
    def test_benchmark_only_adds_high_impact_gap(
        self, benchmark_only_sm: SourceMap
    ) -> None:
        conf, gaps = ConfidenceAnalyzer().analyze(benchmark_only_sm)
        gap_areas = [g.area for g in gaps]
        assert "entry_points" in gap_areas
        ep_gaps = [g for g in gaps if g.area == "entry_points"]
        high_impact = [g for g in ep_gaps if g.impact == "high"]
        assert high_impact, (
            f"Expected high-impact entry_points gap for benchmark-only EPs; gaps={gaps}"
        )

    def test_benchmark_only_overall_not_high(
        self, benchmark_only_sm: SourceMap
    ) -> None:
        conf, gaps = ConfidenceAnalyzer().analyze(benchmark_only_sm)
        assert conf.overall != "high", (
            f"overall must not be 'high' when all EPs are benchmark; got {conf.overall!r}"
        )

    def test_benchmark_only_anomaly_present(
        self, benchmark_only_sm: SourceMap
    ) -> None:
        conf, gaps = ConfidenceAnalyzer().analyze(benchmark_only_sm)
        assert any("production" in a.lower() for a in conf.anomalies), (
            f"Expected 'no production entry points' anomaly; got {conf.anomalies}"
        )

    def test_nocobase_like_overall_not_high(self, nocobase_like: Path) -> None:
        result = runner.invoke(app, ["--agent", str(nocobase_like)])
        assert result.exit_code == 0, result.output
        data = _parse_agent(result.output)
        cs = data.get("confidence_summary", {})
        overall = cs.get("overall")
        assert overall != "high", (
            f"overall=high with only benchmark EPs; confidence_summary={cs}"
        )


# ---------------------------------------------------------------------------
# Test 3: produced_by provenance on EPs and stacks
# ---------------------------------------------------------------------------

class TestProvenance:
    def test_entry_points_have_produced_by(self, production_nodejs: Path) -> None:
        result = runner.invoke(app, [str(production_nodejs)])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        entry_points = data.get("entry_points", [])
        assert entry_points, "Expected at least one entry point"
        for ep in entry_points:
            assert "produced_by" in ep and ep["produced_by"], (
                f"Entry point missing produced_by: {ep}"
            )

    def test_stacks_have_produced_by(self, production_nodejs: Path) -> None:
        result = runner.invoke(app, [str(production_nodejs)])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        stacks = data.get("stacks", [])
        assert stacks, "Expected at least one stack"
        for stack in stacks:
            assert "produced_by" in stack and stack["produced_by"], (
                f"Stack missing produced_by: {stack}"
            )

    def test_nodejs_detector_produced_by_value(self, production_nodejs: Path) -> None:
        result = runner.invoke(app, [str(production_nodejs)])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        stacks = data.get("stacks", [])
        nodejs_stack = next((s for s in stacks if s["stack"] == "nodejs"), None)
        assert nodejs_stack is not None, "nodejs stack not found"
        assert nodejs_stack["produced_by"] == "nodejs", (
            f"Expected produced_by='nodejs', got {nodejs_stack['produced_by']!r}"
        )


# ---------------------------------------------------------------------------
# Test 4: analyzer_fingerprints in metadata
# ---------------------------------------------------------------------------

class TestAnalyzerFingerprints:
    def test_metadata_has_analyzer_fingerprints(self, production_nodejs: Path) -> None:
        result = runner.invoke(app, [str(production_nodejs)])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        meta = data.get("metadata", {})
        assert "analyzer_fingerprints" in meta, (
            "metadata missing analyzer_fingerprints"
        )
        fps = meta["analyzer_fingerprints"]
        assert isinstance(fps, dict) and fps, "analyzer_fingerprints must be non-empty dict"

    def test_fingerprints_include_key_analyzers(self, production_nodejs: Path) -> None:
        result = runner.invoke(app, [str(production_nodejs)])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        fps = data["metadata"]["analyzer_fingerprints"]
        assert "heuristic" in fps, "heuristic fingerprint missing"
        assert "nodejs" in fps, "nodejs fingerprint missing"
        assert "confidence" in fps, "confidence fingerprint missing"

    def test_fingerprints_are_8char_hex(self, production_nodejs: Path) -> None:
        result = runner.invoke(app, [str(production_nodejs)])
        assert result.exit_code == 0, result.output
        fps = result.output
        data = json.loads(fps)
        for name, fp in data["metadata"]["analyzer_fingerprints"].items():
            assert len(fp) == 8, f"{name} fingerprint not 8 chars: {fp!r}"
            assert all(c in "0123456789abcdef" for c in fp), (
                f"{name} fingerprint not hex: {fp!r}"
            )

    def test_compute_analyzer_fingerprints_returns_dict(self) -> None:
        fps = _compute_analyzer_fingerprints()
        assert isinstance(fps, dict)
        assert len(fps) >= 3


# ---------------------------------------------------------------------------
# Test 5: --trace-pipeline mode
# ---------------------------------------------------------------------------

class TestTracePipeline:
    def test_trace_pipeline_produces_pipeline_trace_block(
        self, production_nodejs: Path
    ) -> None:
        result = runner.invoke(app, ["--trace-pipeline", str(production_nodejs)])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert "pipeline_trace" in data, (
            "Expected pipeline_trace key in output with --trace-pipeline"
        )
        pt = data["pipeline_trace"]
        assert pt["requested"] is True
        assert isinstance(pt["events"], list) and pt["events"], (
            "pipeline_trace.events must be non-empty list"
        )

    def test_trace_pipeline_events_have_required_fields(
        self, production_nodejs: Path
    ) -> None:
        result = runner.invoke(app, ["--trace-pipeline", str(production_nodejs)])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        events = data["pipeline_trace"]["events"]
        for ev in events:
            assert "stage" in ev, f"event missing stage: {ev}"
            assert "component" in ev, f"event missing component: {ev}"
            assert "action" in ev, f"event missing action: {ev}"

    def test_trace_pipeline_includes_scan_event(
        self, production_nodejs: Path
    ) -> None:
        result = runner.invoke(app, ["--trace-pipeline", str(production_nodejs)])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        events = data["pipeline_trace"]["events"]
        scan_events = [e for e in events if e["stage"] == "scan"]
        assert scan_events, "Expected at least one scan event in pipeline_trace"

    def test_trace_pipeline_without_flag_absent(
        self, production_nodejs: Path
    ) -> None:
        result = runner.invoke(app, [str(production_nodejs)])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert "pipeline_trace" not in data, (
            "pipeline_trace must be absent when --trace-pipeline not passed"
        )

    def test_trace_records_filtered_benchmark_eps(
        self, production_nodejs: Path
    ) -> None:
        """Filter events are emitted for EPs that reach sm.entry_points but are
        excluded at the output stage (entrypoint_type=benchmark/example).
        The fixture has scripts.benchmark → run.js (root-level, non-aux-dir),
        so it reaches sm.entry_points and is then filtered by agent_view logic."""
        result = runner.invoke(app, ["--trace-pipeline", str(production_nodejs)])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        events = data["pipeline_trace"]["events"]
        filter_events = [e for e in events if e.get("action") == "filter_ep"]
        assert filter_events, (
            "Expected filter_ep event for benchmark EP in pipeline_trace"
        )
        # At least one filter event must carry 'benchmark' in its reason
        assert any("benchmark" in e.get("reason", "").lower() for e in filter_events), (
            f"Expected reason to mention 'benchmark'; filter_ep events: {filter_events}"
        )


# ---------------------------------------------------------------------------
# Test 6: _check_pipeline_coherence catches contradictions
# ---------------------------------------------------------------------------

class TestPipelineCoherence:
    def _sm_high_confidence_no_eps(self) -> SourceMap:
        sm = normalize_source_map(SourceMap(
            metadata=AnalysisMetadata(analyzed_path="/fake"),
            stacks=[StackDetection(stack="nodejs", detection_method="manifest", confidence="high")],
            entry_points=[],
            confidence_summary=ConfidenceSummary(
                overall="high",
                stack_confidence="high",
                entry_point_confidence="high",
            ),
        ))
        return sm

    def _sm_high_confidence_all_benchmark(self) -> SourceMap:
        sm = normalize_source_map(SourceMap(
            metadata=AnalysisMetadata(analyzed_path="/fake"),
            stacks=[StackDetection(stack="nodejs", detection_method="manifest", confidence="high")],
            entry_points=[
                EntryPoint(
                    path="bench/run.js",
                    stack="nodejs",
                    entrypoint_type="benchmark",
                    source="package.json#scripts",
                    confidence="high",
                    reason="script:benchmark",
                )
            ],
            confidence_summary=ConfidenceSummary(
                overall="high",
                stack_confidence="high",
                entry_point_confidence="high",
            ),
        ))
        return sm

    def test_high_confidence_with_empty_eps_flagged(self) -> None:
        issues = _check_pipeline_coherence(self._sm_high_confidence_no_eps())
        assert any("entry_point_confidence" in i for i in issues), (
            f"Expected coherence issue for high ep_confidence + no EPs; got: {issues}"
        )

    def test_high_confidence_all_benchmark_flagged(self) -> None:
        issues = _check_pipeline_coherence(self._sm_high_confidence_all_benchmark())
        assert any("benchmark" in i or "auxiliary" in i for i in issues), (
            f"Expected coherence issue for all-benchmark EPs; got: {issues}"
        )

    def test_clean_state_no_issues(self) -> None:
        sm = normalize_source_map(SourceMap(
            metadata=AnalysisMetadata(analyzed_path="/fake"),
            stacks=[StackDetection(stack="nodejs", detection_method="manifest", confidence="high")],
            entry_points=[
                EntryPoint(
                    path="src/server.js",
                    stack="nodejs",
                    entrypoint_type="production",
                    source="package.json#scripts",
                    confidence="high",
                    reason="script:start",
                )
            ],
            confidence_summary=ConfidenceSummary(
                overall="high",
                stack_confidence="high",
                entry_point_confidence="high",
            ),
        ))
        issues = _check_pipeline_coherence(sm)
        assert not issues, f"Expected no coherence issues for clean state; got: {issues}"

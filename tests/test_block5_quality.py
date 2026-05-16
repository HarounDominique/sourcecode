from __future__ import annotations

"""Tests for block 5 quality improvements:
1. --symbol truncation via max_importers
2. agent_view suppressed flags metadata
3. Empty entry point lists omitted from output
4. symbol_query in contract views
"""

from dataclasses import replace
from pathlib import Path
from typing import Any, Optional

import pytest

from sourcecode.contract_model import (
    ContractSummary,
    ExportRecord,
    FileContract,
    ImportRecord,
)
from sourcecode.contract_pipeline import _filter_by_symbol
from sourcecode.schema import (
    AnalysisMetadata,
    DocSummary,
    EntryPoint,
    MetricsSummary,
    ModuleGraph,
    ModuleGraphSummary,
    SourceMap,
    StackDetection,
)
from sourcecode.serializer import agent_view, standard_view, contract_view


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_definer(path: str, symbol: str) -> FileContract:
    c = FileContract(path=path, language="python")
    c.exports = [ExportRecord(name=symbol, kind="class")]
    return c


def _make_importer(path: str, symbol: str) -> FileContract:
    c = FileContract(path=path, language="python")
    c.imports = [ImportRecord(source="mymodule", symbols=[symbol])]
    return c


def _make_sm(**kwargs: Any) -> SourceMap:
    """Minimal SourceMap for serializer tests."""
    defaults: dict[str, Any] = {
        "metadata": AnalysisMetadata(analyzed_path="/tmp/test"),
        "stacks": [StackDetection(stack="python", primary=True)],
        "entry_points": [],
        "file_paths": [],
        "project_type": "python",
        "project_summary": "test project",
        "architecture_summary": "simple",
    }
    defaults.update(kwargs)
    return SourceMap(**defaults)


# ---------------------------------------------------------------------------
# 1. --symbol truncation: max_importers caps output
# ---------------------------------------------------------------------------

def test_symbol_truncation_limits_importers() -> None:
    """max_importers=2 with 5 importers → truncated=True, only 2 importers returned."""
    definer = _make_definer("mymodule.py", "MyClass")
    importers = [_make_importer(f"user{i}.py", "MyClass") for i in range(5)]

    result, meta = _filter_by_symbol([definer] + importers, "MyClass", max_importers=2)

    assert meta["truncated"] is True
    assert meta["importers_found"] == 5
    assert meta["importers_returned"] == 2
    assert meta["definers_found"] == 1
    # Definer + 2 importers = 3 total
    assert meta["total_returned"] == 3
    assert meta["truncation_reason"] == "max_importers_limit"
    assert "override_hint" in meta
    # Result list matches total_returned
    assert len(result) == 3


def test_symbol_no_truncation_small() -> None:
    """When importers are under the cap, truncated=False."""
    definer = _make_definer("mymodule.py", "SmallClass")
    importers = [_make_importer(f"user{i}.py", "SmallClass") for i in range(3)]

    result, meta = _filter_by_symbol([definer] + importers, "SmallClass", max_importers=50)

    assert meta["truncated"] is False
    assert meta["importers_found"] == 3
    assert meta["importers_returned"] == 3
    assert "truncation_reason" not in meta
    assert "override_hint" not in meta


def test_symbol_definers_not_truncated() -> None:
    """Defining files are always included even when importers are capped to 0."""
    definer = _make_definer("core.py", "CoreClass")
    importers = [_make_importer(f"consumer{i}.py", "CoreClass") for i in range(10)]

    result, meta = _filter_by_symbol([definer] + importers, "CoreClass", max_importers=0)

    # Even with max_importers=0, the definer must be in the result
    result_paths = {c.path for c in result}
    assert "core.py" in result_paths
    assert meta["definers_found"] == 1
    assert meta["importers_returned"] == 0
    assert meta["truncated"] is True


# ---------------------------------------------------------------------------
# 4. agent_view noise exclusions
# ---------------------------------------------------------------------------

def test_agent_view_no_agent_mode_block() -> None:
    """agent_mode metadata block removed — static content with no signal value."""
    sm = _make_sm(metrics_summary=MetricsSummary(requested=True, file_count=5))
    result = agent_view(sm)
    assert "agent_mode" not in result


def test_agent_view_no_hard_signals() -> None:
    """confidence_summary excludes hard/soft/ignored signals — detection internals."""
    from sourcecode.schema import ConfidenceSummary
    cs = ConfidenceSummary(
        overall="high",
        stack_confidence="high",
        entry_point_confidence="high",
        hard_signals=["pom.xml found", "spring-boot-starter-web in deps"],
        soft_signals=["src/main/java present"],
        ignored_signals=["README.md"],
    )
    sm = _make_sm(confidence_summary=cs)
    result = agent_view(sm)

    conf = result.get("confidence_summary", {})
    assert "hard_signals" not in conf
    assert "soft_signals" not in conf
    assert "ignored_signals" not in conf


def test_agent_view_no_env_keys_list() -> None:
    """signals.env_vars excludes the keys[] list — already present in compact env_map."""
    from sourcecode.schema import EnvSummary, EnvVarRecord
    env_map = [EnvVarRecord(key="DB_URL", required=True, category="database")]
    env_summary = EnvSummary(requested=True, total=1, required_count=1)
    sm = _make_sm(env_summary=env_summary, env_map=env_map)
    result = agent_view(sm)

    env_vars = result.get("signals", {}).get("env_vars", {})
    assert "keys" not in env_vars


def test_agent_view_no_dep_groups() -> None:
    """Verbose dep group fields excluded — overlaps key_dependencies with noise."""
    sm = _make_sm()
    result = agent_view(sm)

    assert "production_dependencies" not in result
    assert "dev_tools" not in result
    assert "test_utilities" not in result
    assert "build_tooling" not in result


# ---------------------------------------------------------------------------
# 6 & 7. Empty entry point lists omitted from output
# ---------------------------------------------------------------------------

def test_agent_view_empty_ep_lists_omitted() -> None:
    """When no dev/auxiliary entry points, those keys are absent from agent_view output."""
    sm = _make_sm(entry_points=[])
    result = agent_view(sm)

    assert "development_entry_points" not in result
    assert "auxiliary_entry_points" not in result
    # production list still present (can be empty)
    assert "entry_points" in result


def test_agent_view_dev_ep_excluded() -> None:
    """development_entry_points excluded from agent_view — noise for production-focused agents."""
    dev_ep = EntryPoint(
        path="webpack.config.js",
        stack="nodejs",
        classification="development",
        entrypoint_type="development",
        runtime_relevance="low",
    )
    sm = _make_sm(entry_points=[dev_ep])
    result = agent_view(sm)

    assert "development_entry_points" not in result


def test_standard_view_empty_ep_lists_omitted() -> None:
    """When no dev/auxiliary entry points, those keys are absent from standard_view."""
    sm = _make_sm(entry_points=[])
    result = standard_view(sm)

    assert "development_entry_points" not in result
    assert "auxiliary_entry_points" not in result
    assert "entry_points" in result


def test_standard_view_dev_ep_present_when_nonempty() -> None:
    """When a dev entry point exists, it appears in standard_view."""
    dev_ep = EntryPoint(
        path="webpack.config.js",
        stack="nodejs",
        classification="development",
        entrypoint_type="development",
        runtime_relevance="low",
    )
    sm = _make_sm(entry_points=[dev_ep])
    result = standard_view(sm)

    assert "development_entry_points" in result


# ---------------------------------------------------------------------------
# 8. symbol_query in contract output
# ---------------------------------------------------------------------------

def test_symbol_query_in_contract_minimal_view() -> None:
    """Truncation metadata appears in minimal contract output when truncated."""
    trunc = {
        "symbol": "MyClass",
        "definers_found": 1,
        "importers_found": 100,
        "importers_returned": 50,
        "references_found": 0,
        "total_returned": 51,
        "truncated": True,
        "truncation_reason": "max_importers_limit",
        "override_hint": "--symbol MyClass --max-importers 100",
    }
    cs = ContractSummary(
        mode="contract",
        total_files=200,
        extracted_files=51,
        symbol_truncation=trunc,
    )
    sm = _make_sm(contract_summary=cs, file_contracts=[])
    result = contract_view(sm, depth="minimal")

    assert "symbol_query" in result
    sq = result["symbol_query"]
    assert sq["truncated"] is True
    assert sq["symbol"] == "MyClass"
    assert sq["importers_found"] == 100


def test_symbol_query_absent_when_not_truncated() -> None:
    """symbol_query is absent when symbol_truncation is None."""
    cs = ContractSummary(
        mode="contract",
        total_files=10,
        extracted_files=3,
        symbol_truncation=None,
    )
    sm = _make_sm(contract_summary=cs, file_contracts=[])
    result = contract_view(sm, depth="minimal")

    assert "symbol_query" not in result


def test_symbol_query_in_contract_standard_view() -> None:
    """Truncation metadata appears in standard contract output when truncated."""
    trunc = {
        "symbol": "Foo",
        "definers_found": 1,
        "importers_found": 75,
        "importers_returned": 50,
        "references_found": 0,
        "total_returned": 51,
        "truncated": True,
        "truncation_reason": "max_importers_limit",
        "override_hint": "--symbol Foo --max-importers 75",
    }
    cs = ContractSummary(
        mode="standard",
        total_files=150,
        extracted_files=51,
        symbol_truncation=trunc,
    )
    sm = _make_sm(contract_summary=cs, file_contracts=[])
    result = contract_view(sm, depth="standard")

    assert "symbol_query" in result
    sq = result["symbol_query"]
    assert sq["truncated"] is True
    assert sq["symbol"] == "Foo"

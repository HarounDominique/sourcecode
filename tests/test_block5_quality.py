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
# 4. agent_view suppressed flags metadata
# ---------------------------------------------------------------------------

def test_agent_view_suppressed_flags_detected() -> None:
    """When metrics_summary.requested=True, agent_view includes it in suppressed_flags."""
    sm = _make_sm(
        metrics_summary=MetricsSummary(requested=True, file_count=5),
    )
    result = agent_view(sm)

    assert "agent_mode" in result
    am = result["agent_mode"]
    assert "--full-metrics" in am.get("suppressed_flags", [])
    assert am.get("suppressed_note") == "computed but excluded from agent_view"


def test_agent_view_suppressed_graph_modules() -> None:
    """When module_graph.summary.requested=True, --graph-modules appears in suppressed_flags."""
    mg = ModuleGraph(summary=ModuleGraphSummary(requested=True))
    sm = _make_sm(module_graph=mg)
    result = agent_view(sm)

    am = result["agent_mode"]
    assert "--graph-modules" in am.get("suppressed_flags", [])


def test_agent_view_suppressed_docs() -> None:
    """When doc_summary.requested=True, --docs appears in suppressed_flags."""
    sm = _make_sm(doc_summary=DocSummary(requested=True))
    result = agent_view(sm)

    am = result["agent_mode"]
    assert "--docs" in am.get("suppressed_flags", [])


def test_agent_view_no_suppressed_flags() -> None:
    """agent_view with no suppressed flags doesn't include suppressed_flags key."""
    sm = _make_sm()
    result = agent_view(sm)

    assert "agent_mode" in result
    am = result["agent_mode"]
    assert "suppressed_flags" not in am
    assert "--dependencies" in am.get("auto_enabled", [])


def test_agent_view_auto_enabled_always_present() -> None:
    """auto_enabled list always contains the three flags agent mode enables."""
    sm = _make_sm()
    result = agent_view(sm)

    am = result["agent_mode"]
    assert set(am["auto_enabled"]) == {"--dependencies", "--env-map", "--code-notes"}


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


def test_agent_view_dev_ep_present_when_nonempty() -> None:
    """When a dev entry point exists, development_entry_points appears in agent_view."""
    dev_ep = EntryPoint(
        path="webpack.config.js",
        stack="nodejs",
        classification="development",
        entrypoint_type="development",
        runtime_relevance="low",
    )
    sm = _make_sm(entry_points=[dev_ep])
    result = agent_view(sm)

    assert "development_entry_points" in result


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

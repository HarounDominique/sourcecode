"""Tests unitarios para ProjectSummarizer — todas las ramas del template."""
from __future__ import annotations

import pytest
from pathlib import Path

from sourcecode.schema import (
    DependencySummary,
    DependencyRecord,
    EntryPoint,
    FrameworkDetection,
    SourceMap,
    StackDetection,
)
from sourcecode.summarizer import ProjectSummarizer


def _summarizer() -> ProjectSummarizer:
    return ProjectSummarizer()


# Test 1: SourceMap vacio (sin stacks) -> fallback graceful
def test_empty_sourcemap_returns_no_stack():
    """SourceMap with no stacks returns 'Proyecto sin stack detectado.'"""
    sm = SourceMap()
    result = _summarizer().generate(sm)
    assert result == "Proyecto sin stack detectado."


# Test 2: python primary=True, sin frameworks ni deps
def test_python_primary_no_frameworks_no_deps():
    """SourceMap with python primary=True, no frameworks, dep_summary=None -> contains 'Python', no dep info."""
    sm = SourceMap(
        stacks=[StackDetection(stack="python", primary=True)],
        dependency_summary=DependencySummary(requested=True, total_count=0, ecosystems=[]),
    )
    result = _summarizer().generate(sm)
    assert "Python" in result
    assert "Sin dependencias" in result


# Test 3: python + FastAPI + dep_summary.total_count=5
def test_python_fastapi_with_deps():
    """SourceMap with python, frameworks=[FastAPI], dep_summary.total_count=5 -> contains expected parts."""
    sm = SourceMap(
        stacks=[
            StackDetection(
                stack="python",
                primary=True,
                frameworks=[FrameworkDetection(name="FastAPI", source="manifest")],
            )
        ],
        dependency_summary=DependencySummary(
            requested=True,
            total_count=5,
            direct_count=5,
            ecosystems=["python"],
        ),
    )
    result = _summarizer().generate(sm)
    assert "Python" in result
    assert "FastAPI" in result
    assert "5 dependencias" in result
    assert "python" in result


# Test 4: project_type="cli" with entry_point
def test_cli_project_with_entry_point():
    """SourceMap with project_type='cli', entry_points=[...] -> contains 'CLI' and entry path."""
    sm = SourceMap(
        stacks=[StackDetection(stack="python", primary=True)],
        project_type="cli",
        entry_points=[EntryPoint(path="src/cli.py", stack="python", kind="entry")],
    )
    result = _summarizer().generate(sm)
    assert "CLI" in result or "cli" in result.lower()
    assert "src/cli.py" in result


# Test 5: project_type="monorepo" with 2 distinct workspaces
def test_monorepo_with_two_workspaces():
    """SourceMap with project_type='monorepo', stacks with 2 workspaces -> contains 'Monorepo'."""
    sm = SourceMap(
        stacks=[
            StackDetection(stack="python", primary=False, workspace="backend"),
            StackDetection(stack="nodejs", primary=False, workspace="frontend"),
        ],
        project_type="monorepo",
    )
    result = _summarizer().generate(sm)
    assert "Monorepo" in result or "monorepo" in result.lower()


# Test 6: 2 stacks (python + nodejs), python primary=True -> python appears first
def test_primary_stack_appears_first():
    """When python is primary, the summary leads with Python info."""
    sm = SourceMap(
        stacks=[
            StackDetection(stack="nodejs", primary=False),
            StackDetection(stack="python", primary=True),
        ],
    )
    result = _summarizer().generate(sm)
    python_pos = result.lower().find("python")
    nodejs_pos = result.lower().find("nodejs")
    assert python_pos != -1, "Python should appear in summary"
    # nodejs may or may not appear, but python must come first if both present
    if nodejs_pos != -1:
        assert python_pos < nodejs_pos


# Test 7: generate() returns a string, never raises
def test_generate_never_raises():
    """generate() returns a string and never raises with any valid SourceMap."""
    test_cases = [
        SourceMap(),
        SourceMap(stacks=[StackDetection(stack="python", primary=True)]),
        SourceMap(
            stacks=[StackDetection(stack="python", primary=True)],
            project_type="api",
            dependency_summary=DependencySummary(requested=True, total_count=10, ecosystems=["python"]),
        ),
        SourceMap(project_type="unknown"),
        SourceMap(
            stacks=[StackDetection(stack="go", primary=True)],
            project_type="library",
        ),
    ]
    for sm in test_cases:
        result = _summarizer().generate(sm)
        assert isinstance(result, str), f"Expected str, got {type(result)}"
        assert len(result) > 0, "Summary must be non-empty"


# Test 8: entry_points with >3 paths -> summary lists at most 3
def test_entry_points_capped_at_three():
    """When more than 3 entry_points, the summary mentions at most 3 paths."""
    paths = [f"src/entry_{i}.py" for i in range(6)]
    sm = SourceMap(
        stacks=[StackDetection(stack="python", primary=True)],
        entry_points=[EntryPoint(path=p, stack="python", kind="entry") for p in paths],
    )
    result = _summarizer().generate(sm)
    # Count how many entry paths appear in the summary
    mentioned = sum(1 for p in paths if p in result)
    assert mentioned <= 3, f"Expected at most 3 entry paths in summary, got {mentioned}: {result}"


def test_prefers_pyproject_description(tmp_path: Path) -> None:
    """When pyproject.toml has a description, it dominates the summary."""
    (tmp_path / "pyproject.toml").write_text(
        """
[project]
name = "sourcecode"
description = "Genera contexto estructurado para agentes IA"
        """.strip()
    )
    sm = SourceMap(stacks=[StackDetection(stack="python", primary=True)])
    result = ProjectSummarizer(tmp_path).generate(sm)
    assert "Genera contexto estructurado para agentes IA" in result


def test_falls_back_to_readme_paragraph(tmp_path: Path) -> None:
    """When no manifest description exists, the first useful README paragraph is used."""
    (tmp_path / "README.md").write_text(
        "# Demo\n\nHerramienta para inspeccionar repositorios y resumir su arquitectura.\n\n## Uso\n"
    )
    sm = SourceMap(stacks=[StackDetection(stack="python", primary=True)])
    result = ProjectSummarizer(tmp_path).generate(sm)
    assert "Herramienta para inspeccionar repositorios y resumir su arquitectura." in result


def test_description_ignores_tooling_entry_points(tmp_path: Path) -> None:
    """Tooling entry points under .claude/.vscode/bin do not contaminate the summary narrative."""
    (tmp_path / "pyproject.toml").write_text(
        """
[project]
name = "sourcecode"
description = "CLI principal para analizar proyectos"
        """.strip()
    )
    sm = SourceMap(
        stacks=[StackDetection(stack="python", primary=True)],
        entry_points=[
            EntryPoint(path=".claude/hooks/main.py", stack="python", kind="cli", source="convention"),
            EntryPoint(path="src/main.py", stack="python", kind="cli", source="convention"),
        ],
    )
    result = ProjectSummarizer(tmp_path).generate(sm)
    assert "CLI principal para analizar proyectos" in result
    assert "src/main.py" in result
    assert ".claude/hooks/main.py" not in result

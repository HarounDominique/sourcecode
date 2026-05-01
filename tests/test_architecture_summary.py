from __future__ import annotations

from pathlib import Path

from sourcecode.architecture_summary import ArchitectureSummarizer
from sourcecode.schema import EntryPoint, SourceMap, StackDetection


def test_python_cli_architecture_summary_mentions_optional_analyzers(tmp_path: Path) -> None:
    package_dir = tmp_path / "src" / "sourcecode"
    package_dir.mkdir(parents=True)
    (package_dir / "cli.py").write_text(
        """
from sourcecode.scanner import FileScanner
from sourcecode.serializer import compact_view
from sourcecode.dependency_analyzer import DependencyAnalyzer

def main(dependencies: bool = False) -> None:
    dependency_analyzer = DependencyAnalyzer() if dependencies else None
    if dependency_analyzer:
        compact_view({})
        """.strip()
    )

    sm = SourceMap(
        file_tree={"src": {"sourcecode": {"cli.py": None}}},
        stacks=[StackDetection(stack="python", primary=True)],
        project_type="cli",
        entry_points=[
            EntryPoint(
                path="src/sourcecode/cli.py",
                stack="python",
                kind="cli",
                source="pyproject.toml",
                reason="console_script",
                entrypoint_type="production",
                runtime_relevance="high",
            )
        ],
    )

    result = ArchitectureSummarizer(tmp_path).generate(sm)

    assert result is not None
    assert "SourceMap" in result
    assert "dependencias" in result  # DependencyAnalyzer → human-readable label
    assert "Opcionalmente" in result


def test_no_fallback_entry_point_invented_from_src_cli_py(tmp_path: Path) -> None:
    package_dir = tmp_path / "src" / "demo"
    package_dir.mkdir(parents=True)
    (package_dir / "cli.py").write_text("print('demo')\n")

    sm = SourceMap(
        file_tree={"src": {"demo": {"cli.py": None}}},
        stacks=[StackDetection(stack="python", primary=True)],
        project_type="cli",
        entry_points=[],
    )

    result = ArchitectureSummarizer(tmp_path).generate(sm)

    assert result is not None
    assert "demo/cli.py" not in result
    assert "Python" in result or "cli" in result.lower()


def test_graceful_degradation_without_entry_evidence(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("demo\n")
    sm = SourceMap(
        file_tree={"README.md": None},
        stacks=[StackDetection(stack="python", primary=True)],
        project_type="library",
        entry_points=[],
    )

    result = ArchitectureSummarizer(tmp_path).generate(sm)

    assert result is not None
    # Rich summary generates from stacks even when source analysis is unavailable
    assert "Python" in result or "library" in result.lower()

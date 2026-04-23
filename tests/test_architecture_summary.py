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
            EntryPoint(path="src/sourcecode/cli.py", stack="python", kind="cli", source="convention")
        ],
    )

    result = ArchitectureSummarizer(tmp_path).generate(sm)

    assert result is not None
    assert "src/sourcecode/cli.py" in result
    assert "DependencyAnalyzer (--dependencies)" in result
    assert "SourceMap" in result


def test_fallback_entry_point_detects_src_cli_py(tmp_path: Path) -> None:
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
    assert "src/demo/cli.py" in result


def test_graceful_degradation_without_entry_evidence(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("demo\n")
    sm = SourceMap(
        file_tree={"README.md": None},
        stacks=[StackDetection(stack="python", primary=True)],
        project_type="library",
        entry_points=[],
    )

    result = ArchitectureSummarizer(tmp_path).generate(sm)

    assert result == "Arquitectura no inferida con suficiente evidencia estatica."

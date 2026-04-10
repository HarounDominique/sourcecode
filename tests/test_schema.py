"""Tests del schema de output y serializer."""
import json
from dataclasses import asdict
from pathlib import Path

import pytest

from sourcecode.schema import (
    AnalysisMetadata,
    EntryPoint,
    FrameworkDetection,
    SourceMap,
    StackDetection,
)
from sourcecode.serializer import compact_view, to_json, to_yaml, write_output


def test_schema_version():
    sm = SourceMap()
    assert sm.metadata.schema_version == "1.0"


def test_output_fields():
    data = asdict(SourceMap())
    for key in ("metadata", "file_tree", "stacks", "entry_points", "project_type"):
        assert key in data, f"Falta campo '{key}' en el schema"


def test_metadata_fields():
    meta = asdict(AnalysisMetadata())
    for key in ("schema_version", "generated_at", "sourcecode_version", "analyzed_path"):
        assert key in meta, f"Falta campo '{key}' en AnalysisMetadata"


def test_to_json_valid():
    result = to_json(SourceMap())
    data = json.loads(result)  # lanza si no es JSON valido
    assert data["metadata"]["schema_version"] == "1.0"


def test_yaml_json_equivalence():
    from io import StringIO

    from ruamel.yaml import YAML

    sm = SourceMap(
        file_tree={"main.py": None, "src": {"utils.py": None}},
    )
    json_data = json.loads(to_json(sm))
    yaml_str = to_yaml(sm)
    yaml_obj = YAML()
    yaml_data = dict(yaml_obj.load(StringIO(yaml_str)))
    # Comparar campos clave
    assert json_data["metadata"]["schema_version"] == yaml_data["metadata"]["schema_version"]
    assert json_data["file_tree"] == yaml_data["file_tree"]
    assert json_data["stacks"] == yaml_data["stacks"]


def test_compact_has_schema_version():
    result = compact_view(SourceMap())
    assert "schema_version" in result
    assert result["schema_version"] == "1.0"


def test_compact_has_depth1_tree():
    sm = SourceMap(
        file_tree={
            "src": {"main.py": None, "utils": {"helpers.py": None}},
            "pyproject.toml": None,
            "tests": {"test_main.py": None},
        }
    )
    result = compact_view(sm)
    assert "file_tree_depth1" in result
    # Solo el primer nivel: src={}, pyproject.toml=None, tests={}
    assert "src" in result["file_tree_depth1"]
    assert "pyproject.toml" in result["file_tree_depth1"]
    # Los hijos de src NO deben aparecer en depth1
    depth1_src = result["file_tree_depth1"]["src"]
    assert depth1_src == {} or not isinstance(depth1_src, dict) or "main.py" not in depth1_src


def test_compact_size():
    sm = SourceMap(
        file_tree={f"file_{i}.py": None for i in range(20)},
    )
    result = to_json(compact_view(sm))
    # Aproximacion: numero de tokens ~ numero de palabras en JSON
    word_count = len(result.split())
    assert word_count <= 500, f"Compact demasiado grande: {word_count} palabras"


def test_write_output_stdout(capsys: pytest.CaptureFixture):  # type: ignore[type-arg]
    write_output('{"test": 1}', output=None)
    captured = capsys.readouterr()
    assert '{"test": 1}' in captured.out


def test_write_output_file(tmp_path: Path):
    out = tmp_path / "output.json"
    write_output('{"test": 1}', output=out)
    assert out.exists()
    assert out.read_text() == '{"test": 1}'


def test_generated_at_is_utc():
    meta = AnalysisMetadata()
    assert "+00:00" in meta.generated_at or meta.generated_at.endswith("Z")


# Phase 9 Plan 03 — compact_view() new fields (TDD C1-C10)

def test_c1_compact_includes_project_summary_when_set():
    """C1: compact_view includes 'project_summary' when it is a non-None string."""
    from sourcecode.schema import SourceMap
    sm = SourceMap(project_summary="API en Python.")
    result = compact_view(sm)
    assert "project_summary" in result
    assert result["project_summary"] == "API en Python."


def test_c2_compact_includes_project_summary_none():
    """C2: compact_view includes 'project_summary': None when not set."""
    sm = SourceMap()
    result = compact_view(sm)
    assert "project_summary" in result
    assert result["project_summary"] is None


def test_c3_compact_includes_file_paths_when_set():
    """C3: compact_view includes 'file_paths' list when set."""
    sm = SourceMap(file_paths=["src/a.py", "src/b.py"])
    result = compact_view(sm)
    assert "file_paths" in result
    assert result["file_paths"] == ["src/a.py", "src/b.py"]


def test_c4_compact_includes_file_paths_empty_list():
    """C4: compact_view includes 'file_paths': [] when empty."""
    sm = SourceMap(file_paths=[])
    result = compact_view(sm)
    assert "file_paths" in result
    assert result["file_paths"] == []


def test_c5_compact_dependency_summary_dict_when_requested():
    """C5: compact_view includes dependency_summary as dict when requested=True."""
    from sourcecode.schema import DependencySummary
    sm = SourceMap(dependency_summary=DependencySummary(requested=True, total_count=5))
    result = compact_view(sm)
    assert "dependency_summary" in result
    assert isinstance(result["dependency_summary"], dict)
    assert result["dependency_summary"]["requested"] is True
    assert result["dependency_summary"]["total_count"] == 5


def test_c6_compact_dependency_summary_none_when_not_set():
    """C6: compact_view includes 'dependency_summary': None when dependency_summary=None."""
    sm = SourceMap(dependency_summary=None)
    result = compact_view(sm)
    assert "dependency_summary" in result
    assert result["dependency_summary"] is None


def test_c7_compact_dependency_summary_none_when_not_requested():
    """C7: compact_view includes 'dependency_summary': None when requested=False."""
    from sourcecode.schema import DependencySummary
    sm = SourceMap(dependency_summary=DependencySummary(requested=False))
    result = compact_view(sm)
    assert "dependency_summary" in result
    assert result["dependency_summary"] is None


def test_c8_compact_has_file_tree_depth1_backward_compat():
    """C8: compact_view still includes 'file_tree_depth1' (backward compat)."""
    sm = SourceMap()
    result = compact_view(sm)
    assert "file_tree_depth1" in result


def test_c9_compact_does_not_include_dependencies():
    """C9: compact_view does NOT include the full 'dependencies' list."""
    from sourcecode.schema import DependencyRecord
    sm = SourceMap(dependencies=[DependencyRecord(name="fastapi", ecosystem="python")])
    result = compact_view(sm)
    assert "dependencies" not in result


def test_c10_compact_does_not_include_docs_or_module_graph():
    """C10: compact_view does NOT include 'docs' or 'module_graph'."""
    sm = SourceMap()
    result = compact_view(sm)
    assert "docs" not in result
    assert "module_graph" not in result


def test_schema_serializes_typed_detection_fields():
    sm = SourceMap(
        stacks=[
            StackDetection(
                stack="python",
                frameworks=[FrameworkDetection(name="FastAPI", source="pyproject.toml")],
                manifests=["pyproject.toml"],
                primary=True,
                signals=["manifest:pyproject.toml", "entry:src/main.py"],
            )
        ],
        entry_points=[
            EntryPoint(path="src/main.py", stack="python", kind="script", source="heuristic")
        ],
    )

    data = json.loads(to_json(sm))

    assert data["stacks"][0]["stack"] == "python"
    assert data["stacks"][0]["primary"] is True
    assert data["stacks"][0]["signals"] == ["manifest:pyproject.toml", "entry:src/main.py"]
    assert data["stacks"][0]["frameworks"][0]["name"] == "FastAPI"
    assert data["entry_points"][0]["path"] == "src/main.py"


# Phase 9 — new fields on SourceMap

def test_sourcemap_new_fields_defaults():
    """SourceMap() without args has file_paths=[], project_summary=None, key_dependencies=[]."""
    from sourcecode.schema import DependencyRecord
    sm = SourceMap()
    assert sm.file_paths == []
    assert sm.project_summary is None
    assert sm.key_dependencies == []


def test_sourcemap_file_paths_persists():
    """SourceMap(file_paths=[...]) persists the list."""
    sm = SourceMap(file_paths=["src/main.py", "src/utils.py"])
    assert sm.file_paths == ["src/main.py", "src/utils.py"]


def test_sourcemap_project_summary_persists():
    """SourceMap(project_summary="...") persists the string."""
    sm = SourceMap(project_summary="API en Python (FastAPI).")
    assert sm.project_summary == "API en Python (FastAPI)."


def test_sourcemap_key_dependencies_persists():
    """SourceMap(key_dependencies=[DependencyRecord(...)]) persists the list."""
    from sourcecode.schema import DependencyRecord
    dep = DependencyRecord(name="fastapi", ecosystem="python")
    sm = SourceMap(key_dependencies=[dep])
    assert len(sm.key_dependencies) == 1
    assert sm.key_dependencies[0].name == "fastapi"


def test_sourcemap_asdict_includes_new_keys():
    """dataclasses.asdict(SourceMap()) includes 'file_paths', 'project_summary', 'key_dependencies'."""
    data = asdict(SourceMap())
    assert "file_paths" in data
    assert "project_summary" in data
    assert "key_dependencies" in data


def test_sourcemap_backward_compat():
    """Existing SourceMap without new fields still works (backward compat via defaults)."""
    sm = SourceMap(
        stacks=[StackDetection(stack="python")],
        project_type="api",
    )
    # Should not raise, and new fields have their defaults
    assert sm.file_paths == []
    assert sm.project_summary is None
    assert sm.key_dependencies == []


# Phase 9, Plan 02 — DocRecord.importance field tests

def test_docrecord_importance_default_medium() -> None:
    """DocRecord default importance is 'medium'."""
    from sourcecode.schema import DocRecord
    rec = DocRecord(symbol="f", kind="function", language="python", path="x.py")
    assert rec.importance == "medium"


def test_docrecord_importance_high_persists() -> None:
    """DocRecord(importance='high') persists 'high'."""
    from sourcecode.schema import DocRecord
    rec = DocRecord(symbol="f", kind="function", language="python", path="x.py", importance="high")
    assert rec.importance == "high"


def test_docrecord_importance_low_persists() -> None:
    """DocRecord(importance='low') persists 'low'."""
    from sourcecode.schema import DocRecord
    rec = DocRecord(symbol="f", kind="function", language="python", path="x.py", importance="low")
    assert rec.importance == "low"


def test_docrecord_asdict_includes_importance() -> None:
    """asdict(DocRecord(...)) includes 'importance' key."""
    from sourcecode.schema import DocRecord
    rec = DocRecord(symbol="f", kind="function", language="python", path="x.py")
    d = asdict(rec)
    assert "importance" in d
    assert d["importance"] == "medium"


def test_sourcemap_with_docrecord_serializes_importance() -> None:
    """SourceMap with docs=[DocRecord(...)] serializes importance in to_json()."""
    from sourcecode.schema import DocRecord
    from sourcecode.serializer import to_json
    rec = DocRecord(symbol="f", kind="function", language="python", path="x.py", importance="high")
    sm = SourceMap(docs=[rec])
    data = json.loads(to_json(sm))
    assert len(data["docs"]) == 1
    assert data["docs"][0]["importance"] == "high"

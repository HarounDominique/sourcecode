from __future__ import annotations

import json

from sourcecode.schema import DependencyRecord, DependencySummary, SourceMap
from sourcecode.serializer import to_json


def test_source_map_dependency_defaults_are_backward_compatible() -> None:
    source_map = SourceMap()

    assert source_map.dependencies == []
    assert source_map.dependency_summary is None


def test_dependency_records_serialize_direct_and_transitive() -> None:
    source_map = SourceMap(
        dependencies=[
            DependencyRecord(
                name="fastapi",
                ecosystem="python",
                declared_version=">=0.115",
                resolved_version="0.115.2",
                source="lockfile",
                manifest_path="pyproject.toml",
            ),
            DependencyRecord(
                name="starlette",
                ecosystem="python",
                scope="transitive",
                resolved_version="0.38.6",
                source="lockfile",
                parent="fastapi",
                manifest_path="poetry.lock",
            ),
        ],
        dependency_summary=DependencySummary(
            requested=True,
            total_count=2,
            direct_count=1,
            transitive_count=1,
            ecosystems=["python"],
            sources=["lockfile"],
        ),
    )

    data = json.loads(to_json(source_map))

    assert data["dependencies"][0]["name"] == "fastapi"
    assert data["dependencies"][0]["resolved_version"] == "0.115.2"
    assert data["dependencies"][1]["scope"] == "transitive"
    assert data["dependencies"][1]["parent"] == "fastapi"
    assert data["dependency_summary"]["requested"] is True
    assert data["dependency_summary"]["transitive_count"] == 1

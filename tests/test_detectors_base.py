"""Tests del contrato base y orquestador de detectores."""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from sourcecode.detectors.base import (
    AbstractDetector,
    DetectionContext,
    EntryPoint,
    FrameworkDetection,
    StackDetection,
)
from sourcecode.detectors.project import ProjectDetector


def test_stack_detection_serializes_to_json() -> None:
    stack = StackDetection(
        stack="python",
        detection_method="manifest",
        confidence="high",
        frameworks=[FrameworkDetection(name="FastAPI", source="pyproject.toml")],
        package_manager="pip",
        manifests=["pyproject.toml"],
    )

    payload = json.dumps(asdict(stack))
    data = json.loads(payload)

    assert data["stack"] == "python"
    assert data["frameworks"][0]["name"] == "FastAPI"
    assert data["manifests"] == ["pyproject.toml"]


def test_entry_point_serializes_with_expected_fields() -> None:
    entry_point = EntryPoint(
        path="src/main.py",
        stack="python",
        kind="script",
        source="heuristic",
    )

    data = asdict(entry_point)

    assert data == {
        "path": "src/main.py",
        "stack": "python",
        "kind": "script",
        "source": "heuristic",
    }


def test_abstract_detector_contract_requires_detect() -> None:
    class IncompleteDetector(AbstractDetector):
        def can_detect(self, context: DetectionContext) -> bool:
            return True

    with pytest.raises(TypeError):
        IncompleteDetector()


class ManifestNodeDetector(AbstractDetector):
    name = "node-manifest"
    priority = 10

    def can_detect(self, context: DetectionContext) -> bool:
        return "package.json" in context.manifests

    def detect(self, context: DetectionContext) -> tuple[list[StackDetection], list[EntryPoint]]:
        return (
            [
                StackDetection(
                    stack="nodejs",
                    detection_method="manifest",
                    confidence="high",
                    frameworks=[FrameworkDetection(name="Next.js", source="package.json")],
                    package_manager="pnpm",
                    manifests=["package.json"],
                )
            ],
            [EntryPoint(path="server.js", stack="nodejs", kind="server", source="package.json")],
        )


class HeuristicNodeDetector(AbstractDetector):
    name = "node-heuristic"
    priority = 90

    def can_detect(self, context: DetectionContext) -> bool:
        return True

    def detect(self, context: DetectionContext) -> tuple[list[StackDetection], list[EntryPoint]]:
        return (
            [
                StackDetection(
                    stack="nodejs",
                    detection_method="heuristic",
                    confidence="low",
                    frameworks=[FrameworkDetection(name="React", source="file_tree")],
                    manifests=[],
                )
            ],
            [EntryPoint(path="server.js", stack="nodejs", kind="server", source="file_tree")],
        )


class PythonManifestDetector(AbstractDetector):
    name = "python-manifest"
    priority = 20

    def can_detect(self, context: DetectionContext) -> bool:
        return "pyproject.toml" in context.manifests

    def detect(self, context: DetectionContext) -> tuple[list[StackDetection], list[EntryPoint]]:
        return (
            [
                StackDetection(
                    stack="python",
                    detection_method="manifest",
                    confidence="medium",
                    frameworks=[FrameworkDetection(name="FastAPI", source="pyproject.toml")],
                    manifests=["pyproject.toml"],
                )
            ],
            [EntryPoint(path="src/main.py", stack="python", kind="script", source="pyproject.toml")],
        )


def test_project_detector_merges_duplicate_stacks_and_entry_points(tmp_path: Path) -> None:
    detector = ProjectDetector(
        detectors=[HeuristicNodeDetector(), ManifestNodeDetector(), PythonManifestDetector()]
    )

    stacks, entry_points, project_type = detector.detect(
        root=tmp_path,
        file_tree={"server.js": None, "src": {"main.py": None}},
        manifests=["package.json", "pyproject.toml"],
    )

    assert [stack.stack for stack in stacks] == ["nodejs", "python"]
    node_stack = stacks[0]
    assert node_stack.confidence == "high"
    assert node_stack.detection_method == "manifest"
    assert {framework.name for framework in node_stack.frameworks} == {"Next.js", "React"}
    assert node_stack.package_manager == "pnpm"
    assert node_stack.manifests == ["package.json"]
    assert project_type == "fullstack"

    assert sorted(entry.path for entry in entry_points) == ["server.js", "src/main.py"]


def test_project_detector_skips_detectors_that_do_not_apply(tmp_path: Path) -> None:
    detector = ProjectDetector(detectors=[ManifestNodeDetector(), PythonManifestDetector()])

    stacks, entry_points, project_type = detector.detect(root=tmp_path, file_tree={}, manifests=["Gemfile"])

    assert stacks == []
    assert entry_points == []
    assert project_type is None

from __future__ import annotations

from pathlib import Path

from sourcecode.detectors.project import ProjectDetector
from sourcecode.detectors.systems import SystemsDetector
from sourcecode.detectors.terraform import TerraformDetector
from sourcecode.detectors.tooling import collect_tooling_signals


def test_terraform_detector_detects_aws_stack(tmp_path: Path) -> None:
    (tmp_path / "main.tf").write_text(
        """
provider "aws" {
  region = "eu-west-1"
}
resource "aws_s3_bucket" "logs" {}
        """.strip()
    )

    detector = ProjectDetector([TerraformDetector()])
    stacks, entry_points, project_type = detector.detect(
        root=tmp_path,
        file_tree={"main.tf": None},
        manifests=[],
    )

    assert stacks[0].stack == "terraform"
    assert "provider:aws" in stacks[0].signals
    assert entry_points[0].path == "main.tf"
    assert project_type == "unknown"


def test_systems_detector_detects_cmake_cpp_project(tmp_path: Path) -> None:
    (tmp_path / "CMakeLists.txt").write_text("project(demo)")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.cpp").write_text("int main() { return 0; }")

    detector = ProjectDetector([SystemsDetector()])
    stacks, entry_points, project_type = detector.detect(
        root=tmp_path,
        file_tree={"CMakeLists.txt": None, "src": {"main.cpp": None}},
        manifests=[],
    )

    assert stacks[0].stack == "cpp"
    assert stacks[0].package_manager == "cmake"
    assert entry_points[0].path == "src/main.cpp"
    assert project_type == "cli"


def test_collect_tooling_signals_detects_operational_files() -> None:
    file_tree = {
        "Dockerfile": None,
        "Taskfile.yml": None,
        "Procfile": None,
    }

    assert set(collect_tooling_signals(file_tree)) == {
        "tooling:docker",
        "tooling:taskfile",
        "tooling:procfile",
    }

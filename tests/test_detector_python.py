"""Tests del detector Python."""
from __future__ import annotations

from pathlib import Path

from sourcecode.detectors.project import ProjectDetector
from sourcecode.detectors.python import PythonDetector


def test_python_detector_detects_fastapi_from_pyproject(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        """
[project]
name = "api"
dependencies = ["fastapi>=0.115", "uvicorn>=0.30"]
        """.strip()
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("app = None")

    detector = ProjectDetector([PythonDetector()])
    stacks, entry_points, project_type = detector.detect(
        root=tmp_path,
        file_tree={"pyproject.toml": None, "src": {"main.py": None}},
        manifests=["pyproject.toml"],
    )

    assert stacks[0].stack == "python"
    assert stacks[0].frameworks[0].name == "FastAPI"
    assert entry_points[0].path == "src/main.py"
    assert project_type == "api"


def test_python_detector_detects_django_manage_py(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("django>=5.0\n")
    (tmp_path / "manage.py").write_text("print('manage')")

    detector = ProjectDetector([PythonDetector()])
    stacks, entry_points, project_type = detector.detect(
        root=tmp_path,
        file_tree={"requirements.txt": None, "manage.py": None},
        manifests=["requirements.txt"],
    )

    assert stacks[0].frameworks[0].name == "Django"
    assert entry_points[0].path == "manage.py"
    assert project_type == "api"


def test_python_detector_extracts_script_entry_from_pyproject(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        """
[project]
name = "cli"
dependencies = ["typer>=0.12"]

[project.scripts]
smg = "src.main:app"
        """.strip()
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("app = object()")

    detector = ProjectDetector([PythonDetector()])
    stacks, entry_points, project_type = detector.detect(
        root=tmp_path,
        file_tree={"pyproject.toml": None, "src": {"main.py": None}},
        manifests=["pyproject.toml"],
    )

    assert stacks[0].frameworks[0].name == "Typer"
    assert [entry.path for entry in entry_points] == ["src/main.py"]
    assert project_type == "cli"

"""Fixtures compartidas para tests de sourcecode."""
from pathlib import Path

import pytest


@pytest.fixture
def tmp_project(tmp_path: Path) -> Path:
    """Proyecto minimo en directorio temporal."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("# main")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "test"\n')
    (tmp_path / ".gitignore").write_text("__pycache__/\n*.pyc\n")
    return tmp_path

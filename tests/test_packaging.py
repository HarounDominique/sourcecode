from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.9
    import tomli as tomllib


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_VERSION = tomllib.loads(
    (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
)["project"]["version"]


def test_console_script_reports_version() -> None:
    command = shutil.which("sourcecode")
    if command is None:
        candidate = PROJECT_ROOT / ".venv" / "bin" / "sourcecode"
        if candidate.exists():
            command = str(candidate)
    assert command, "No se encontro el entry point 'sourcecode' en PATH"

    result = subprocess.run(
        [command, "--version"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    assert f"sourcecode {PROJECT_VERSION}" in result.stdout


def test_pyproject_declares_packaging_contract() -> None:
    data = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert data["build-system"]["build-backend"] == "hatchling.build"
    assert "hatchling" in data["build-system"]["requires"]
    assert data["project"]["name"] == "sourcecode"
    assert data["project"]["scripts"]["sourcecode"] == "sourcecode.cli:app"
    assert data["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/sourcecode"
    ]

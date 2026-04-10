"""Tests de integracion E2E para el flag --full-metrics de la CLI.

MQT-11: file_metrics y metrics_summary presentes con --full-metrics
MQT-12: backward compat sin --full-metrics (file_metrics=[], metrics_summary=null)
MQT-13: availability labels validos en todos los FileMetrics
MQT-14: deteccion de ficheros de test con is_test
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sourcecode.cli import app

runner = CliRunner()

# Directorio raiz del proyecto (atlas-cli) — el propio proyecto como fixture real
PROJECT_ROOT = Path(__file__).parent.parent


def test_full_metrics_flag_produces_file_metrics():
    """MQT-11: --full-metrics incluye file_metrics y metrics_summary en el output JSON."""
    result = runner.invoke(app, ["--full-metrics", str(PROJECT_ROOT)])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)

    assert "file_metrics" in data
    assert isinstance(data["file_metrics"], list)
    assert len(data["file_metrics"]) > 0, "file_metrics debe ser no vacio con --full-metrics"

    assert "metrics_summary" in data
    ms = data["metrics_summary"]
    assert ms is not None, "metrics_summary debe ser no-null con --full-metrics"
    assert ms["requested"] is True
    assert ms["file_count"] > 0

    # El propio proyecto tiene tests/
    assert ms["test_file_count"] > 0, "El proyecto debe tener al menos un fichero de test"

    # Al menos un FileMetrics Python con loc_availability measured
    python_files = [fm for fm in data["file_metrics"] if fm["language"] == "python"]
    assert len(python_files) > 0, "No se encontraron ficheros Python en file_metrics"
    assert all(
        fm["loc_availability"] == "measured" for fm in python_files
    ), "Los ficheros Python deben tener loc_availability='measured'"


def test_base_command_unchanged_without_flag():
    """MQT-12: sin --full-metrics, file_metrics=[] y metrics_summary=null (backward compat)."""
    result = runner.invoke(app, [str(PROJECT_ROOT)])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)

    assert data["file_metrics"] == [], (
        f"file_metrics debe ser lista vacia sin --full-metrics, got {len(data['file_metrics'])} items"
    )
    assert data["metrics_summary"] is None, (
        "metrics_summary debe ser null sin --full-metrics"
    )

    # Campos existentes siguen presentes (backward compat)
    assert "stacks" in data
    assert "file_tree" in data
    assert "file_paths" in data
    assert "project_summary" in data


def test_full_metrics_availability_labels():
    """MQT-13: todos los FileMetrics tienen availability labels validos."""
    result = runner.invoke(app, ["--full-metrics", str(PROJECT_ROOT)])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)

    valid = {"measured", "inferred", "unavailable"}
    for fm in data["file_metrics"]:
        assert fm["loc_availability"] in valid, (
            f"loc_availability invalida '{fm['loc_availability']}' en {fm['path']}"
        )
        assert fm["symbol_availability"] in valid, (
            f"symbol_availability invalida '{fm['symbol_availability']}' en {fm['path']}"
        )
        assert fm["complexity_availability"] in valid, (
            f"complexity_availability invalida '{fm['complexity_availability']}' en {fm['path']}"
        )


def test_full_metrics_with_test_files():
    """MQT-14: FileMetrics con is_test=True existen y tienen paths de test."""
    result = runner.invoke(app, ["--full-metrics", str(PROJECT_ROOT)])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)

    test_entries = [fm for fm in data["file_metrics"] if fm["is_test"]]
    assert len(test_entries) > 0, "Debe haber al menos un FileMetrics con is_test=True"

    # Los ficheros de test deben tener paths coherentes con patrones de test
    for fm in test_entries:
        path = fm["path"].replace("\\", "/")
        is_test_path = (
            "tests/" in path
            or "test_" in path
            or path.endswith("_test.py")
        )
        assert is_test_path, (
            f"FileMetrics con is_test=True tiene path inesperado: {path}"
        )

    # Los ficheros no-test no deben tener paths de tests/test_*.py
    non_test_entries = [fm for fm in data["file_metrics"] if not fm["is_test"]]
    for fm in non_test_entries:
        path = fm["path"].replace("\\", "/")
        # Un fichero que empieza con tests/test_ y termina en .py no debe estar marcado como no-test
        if path.startswith("tests/test_") and path.endswith(".py"):
            pytest.fail(
                f"Fichero {path} deberia tener is_test=True pero tiene is_test=False"
            )

from __future__ import annotations

"""Tests para CoverageParser — MQT-05..08 y tests adicionales.

Wave 0 (TDD RED): Estos tests se escriben ANTES de la implementacion.
Fallan con ImportError hasta que coverage_parser.py exista.
"""

import shutil
from pathlib import Path

import pytest

from sourcecode.coverage_parser import CoverageParser

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# MQT-05: Cobertura XML (Cobertura format)
# ---------------------------------------------------------------------------

def test_cobertura(tmp_path: Path) -> None:
    """MQT-05: _parse_cobertura_xml lee coverage.xml via stdlib ET.

    Dado fixture coverage.xml en tmpdir:
    - line_rate == approx(0.85)
    - branch_rate == approx(0.72)
    - lines_covered == 170
    - lines_valid == 200
    - file_count == 2
    - format == "cobertura_xml"
    """
    shutil.copy(FIXTURES / "coverage.xml", tmp_path / "coverage.xml")

    record = CoverageParser()._parse_cobertura_xml(tmp_path)

    assert record is not None, "_parse_cobertura_xml devolvio None inesperadamente"
    assert record.line_rate == pytest.approx(0.85)
    assert record.branch_rate == pytest.approx(0.72)
    assert record.lines_covered == 170
    assert record.lines_valid == 200
    assert record.file_count == 2
    assert record.format == "cobertura_xml"


# ---------------------------------------------------------------------------
# MQT-06: .coverage SQLite (dot_coverage format)
# ---------------------------------------------------------------------------

def test_dot_coverage(tmp_path: Path) -> None:
    """MQT-06: _parse_dot_coverage silencia DatabaseError para ficheros no-SQLite.

    - Dado fichero .coverage con contenido no-SQLite: retorna None
    - Dado que .coverage no existe: retorna None
    """
    # Caso 1: fichero .coverage con contenido invalido (no es SQLite)
    fake_coverage = tmp_path / ".coverage"
    fake_coverage.write_bytes(b"not a sqlite database")

    result = CoverageParser()._parse_dot_coverage(tmp_path)
    assert result is None, (
        f"Esperado None para fichero no-SQLite, got {result}"
    )

    # Caso 2: fichero .coverage no existe
    fake_coverage.unlink()
    result2 = CoverageParser()._parse_dot_coverage(tmp_path)
    assert result2 is None, (
        f"Esperado None cuando .coverage no existe, got {result2}"
    )


# ---------------------------------------------------------------------------
# MQT-07: LCOV format
# ---------------------------------------------------------------------------

def test_lcov(tmp_path: Path) -> None:
    """MQT-07: _parse_lcov parsea lcov.info via state machine linea a linea.

    Fixture tiene dos ficheros:
      scanner.py: LF=50, LH=42, BRF=10, BRH=7
      schema.py:  LF=30, LH=28, BRF=4,  BRH=3

    Esperado:
    - line_rate   == approx((42+28)/(50+30)) == approx(0.875)
    - branch_rate == approx((7+3)/(10+4))   == approx(0.714, abs=0.01)
    - lines_covered == 70 (42+28)
    - lines_valid   == 80 (50+30)
    - file_count == 2
    - format == "lcov"
    """
    shutil.copy(FIXTURES / "lcov.info", tmp_path / "lcov.info")

    record = CoverageParser()._parse_lcov(tmp_path)

    assert record is not None, "_parse_lcov devolvio None inesperadamente"
    assert record.line_rate == pytest.approx(0.875)
    assert record.branch_rate == pytest.approx(10 / 14, abs=0.01)
    assert record.lines_covered == 70
    assert record.lines_valid == 80
    assert record.file_count == 2
    assert record.format == "lcov"


# ---------------------------------------------------------------------------
# MQT-08: JaCoCo XML format
# ---------------------------------------------------------------------------

def test_jacoco(tmp_path: Path) -> None:
    """MQT-08: _parse_jacoco_xml lee root-level counter elements de jacoco.xml.

    Fixture tiene root-level counters:
      LINE:   missed=8,  covered=72  -> rate = 72/80 = 0.90
      BRANCH: missed=4,  covered=16  -> rate = 16/20 = 0.80

    Esperado:
    - line_rate   == approx(0.90)
    - branch_rate == approx(0.80)
    - lines_covered == 72
    - lines_valid   == 80  (8+72)
    - file_count == 2 (Scanner.java + Schema.java)
    - format == "jacoco_xml"
    """
    shutil.copy(FIXTURES / "jacoco.xml", tmp_path / "jacoco.xml")

    record = CoverageParser()._parse_jacoco_xml(tmp_path)

    assert record is not None, "_parse_jacoco_xml devolvio None inesperadamente"
    assert record.line_rate == pytest.approx(0.90)
    assert record.branch_rate == pytest.approx(0.80)
    assert record.lines_covered == 72
    assert record.lines_valid == 80
    assert record.file_count == 2
    assert record.format == "jacoco_xml"


# ---------------------------------------------------------------------------
# Additional test: parse_all en directorio vacio
# ---------------------------------------------------------------------------

def test_parse_all_empty(tmp_path: Path) -> None:
    """parse_all() sobre directorio sin artefactos retorna lista vacia."""
    records = CoverageParser().parse_all(tmp_path)
    assert records == [], f"Esperado [], got {records}"


# ---------------------------------------------------------------------------
# Additional test: build_file_coverage_map
# ---------------------------------------------------------------------------

def test_build_file_coverage_map(tmp_path: Path) -> None:
    """build_file_coverage_map retorna dict con entries para los paths del fixture.

    Dado un CoverageRecord de cobertura_xml (que tiene clases scanner.py y schema.py),
    el mapa debe contener entradas para ambos paths.
    """
    shutil.copy(FIXTURES / "coverage.xml", tmp_path / "coverage.xml")

    parser = CoverageParser()
    records = parser.parse_all(tmp_path)

    # Debe haber al menos un record de cobertura_xml
    assert any(r.format == "cobertura_xml" for r in records), (
        f"No se encontro record cobertura_xml en: {records}"
    )

    coverage_map = parser.build_file_coverage_map(tmp_path, records)

    assert isinstance(coverage_map, dict), "build_file_coverage_map debe retornar dict"
    # Los paths deben estar presentes (relativos, con / como separador)
    assert "src/scanner.py" in coverage_map, (
        f"Falta 'src/scanner.py' en coverage_map. Keys: {list(coverage_map.keys())}"
    )
    assert "src/schema.py" in coverage_map, (
        f"Falta 'src/schema.py' en coverage_map. Keys: {list(coverage_map.keys())}"
    )

    # Cada valor es (line_rate, branch_rate, source_name)
    lr, br, src = coverage_map["src/scanner.py"]
    assert lr == pytest.approx(0.90)
    assert br == pytest.approx(0.75)
    assert src == "cobertura_xml"

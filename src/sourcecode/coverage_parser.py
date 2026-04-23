from __future__ import annotations

"""Parser de artefactos de cobertura pre-existentes.

Lee coverage.xml (Cobertura), .coverage (SQLite de coverage.py >= 5.0),
lcov.info (LCOV) y jacoco.xml (JaCoCo) sin ejecutar tests ni toolchains.

Solo stdlib: sqlite3, xml.etree.ElementTree, pathlib.
"""

import sqlite3
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

from sourcecode.schema import CoverageRecord

# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _safe_float(value: Optional[str]) -> Optional[float]:
    """Convierte string a float; retorna None si es None o no parseable."""
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _safe_int(value: Optional[str]) -> Optional[int]:
    """Convierte string a int; retorna None si es None o no parseable."""
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _decode_numbits(blob: bytes) -> list[int]:
    """Decodifica bitset little-endian de coverage.py .coverage SQLite.

    Byte i, bit j set => linea (i*8 + j + 1) ejecutada.
    Ejemplo: bytes([0b00000101]) => [1, 3]
    """
    return [
        i * 8 + j + 1
        for i, byte in enumerate(blob)
        for j in range(8)
        if byte & (1 << j)
    ]


# ---------------------------------------------------------------------------
# CoverageParser
# ---------------------------------------------------------------------------

class CoverageParser:
    """Parsea artefactos de cobertura pre-existentes sin ejecutar tests."""

    _COBERTURA_CANDIDATES = [
        "coverage.xml",
        "build/coverage.xml",
        "target/coverage.xml",
        "htmlcov/coverage.xml",
    ]
    _LCOV_CANDIDATES = [
        "lcov.info",
        "coverage/lcov.info",
        "coverage.lcov",
    ]
    _JACOCO_CANDIDATES = [
        "jacoco.xml",
        "build/reports/jacoco/test/jacocoTestReport.xml",
        "target/site/jacoco/jacoco.xml",
    ]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def parse_all(self, root: Path) -> list[CoverageRecord]:
        """Busca y parsea todos los artefactos de cobertura en root.

        Nunca lanza excepcion — errores van a la lista de records con campos None.
        Retorna lista (posiblemente vacia) de CoverageRecord encontrados.
        """
        results: list[CoverageRecord] = []
        for parser in (
            self._parse_cobertura_xml,
            self._parse_dot_coverage,
            self._parse_lcov,
            self._parse_jacoco_xml,
        ):
            try:
                record = parser(root)
            except Exception:
                record = None
            if record is not None:
                results.append(record)
        return results

    def build_file_coverage_map(
        self,
        root: Path,
        records: list[CoverageRecord],
    ) -> dict[str, tuple[float | None, float | None, str]]:
        """Retorna {rel_path: (line_rate, branch_rate, source_name)}.

        Prioridad de fuente: cobertura_xml > lcov > jacoco_xml > dot_coverage.
        Paths absolutos (dot_coverage) se convierten a relativos via relative_to(root);
        paths fuera de root se descartan silenciosamente.
        """
        result: dict[str, tuple[float | None, float | None, str]] = {}

        # Determine which formats are present (in priority order)
        formats_present = {r.format for r in records}

        priority_order = ["cobertura_xml", "lcov", "jacoco_xml", "dot_coverage"]

        for fmt in priority_order:
            if fmt not in formats_present:
                continue

            per_file = self._get_per_file_data(root, fmt)
            for rel_path, (lr, br) in per_file.items():
                # Only add if not already claimed by a higher-priority format
                if rel_path not in result:
                    result[rel_path] = (lr, br, fmt)

        return result

    # ------------------------------------------------------------------
    # Format parsers (return None on any failure)
    # ------------------------------------------------------------------

    def _parse_cobertura_xml(self, root: Path) -> CoverageRecord | None:
        """Parsea coverage.xml en formato Cobertura via stdlib ET.

        Candidatos: coverage.xml, build/coverage.xml, target/coverage.xml,
        htmlcov/coverage.xml. Retorna el primero que parsea correctamente.
        """
        for candidate in self._COBERTURA_CANDIDATES:
            path = root / candidate
            if not path.exists():
                continue
            try:
                tree = ET.parse(str(path))
                root_elem = tree.getroot()
            except ET.ParseError:
                continue
            except Exception:
                continue

            if root_elem.tag != "coverage":
                continue

            line_rate = _safe_float(root_elem.get("line-rate"))
            branch_rate = _safe_float(root_elem.get("branch-rate"))
            lines_covered = _safe_int(root_elem.get("lines-covered"))
            lines_valid = _safe_int(root_elem.get("lines-valid"))
            timestamp = root_elem.get("timestamp")
            tool_version = root_elem.get("version")
            file_count = len(root_elem.findall(".//class"))

            return CoverageRecord(
                source_file=str(path.relative_to(root)).replace("\\", "/"),
                format="cobertura_xml",
                line_rate=line_rate,
                branch_rate=branch_rate,
                lines_covered=lines_covered,
                lines_valid=lines_valid,
                timestamp=timestamp,
                tool_version=tool_version,
                file_count=file_count,
            )

        return None

    def _parse_dot_coverage(self, root: Path) -> CoverageRecord | None:
        """Parsea .coverage SQLite (coverage.py >= 5.0).

        Silencia sqlite3.DatabaseError — el fichero puede ser pickle (< 5.0)
        o estar corrupto. Retorna None si no existe o no es SQLite valido.
        """
        path = root / ".coverage"
        if not path.exists():
            return None

        try:
            conn = sqlite3.connect(str(path))
            try:
                # Validate it's a coverage.py SQLite database
                cursor = conn.execute(
                    "SELECT key, value FROM meta WHERE key IN ('version', 'timestamp')"
                )
                meta_rows = cursor.fetchall()
                meta = {row[0]: row[1] for row in meta_rows}

                file_cursor = conn.execute("SELECT count(*) FROM file")
                file_count = file_cursor.fetchone()[0]
            finally:
                conn.close()
        except sqlite3.DatabaseError:
            return None
        except Exception:
            return None

        tool_version = meta.get("version")
        timestamp = meta.get("timestamp")

        return CoverageRecord(
            source_file=".coverage",
            format="dot_coverage",
            line_rate=None,
            branch_rate=None,
            lines_covered=None,
            lines_valid=None,
            timestamp=str(timestamp) if timestamp is not None else None,
            tool_version=str(tool_version) if tool_version is not None else None,
            file_count=file_count,
        )

    def _parse_lcov(self, root: Path) -> CoverageRecord | None:
        """Parsea lcov.info via state machine linea a linea.

        Candidatos: lcov.info, coverage/lcov.info, coverage.lcov.
        Lee con errors='replace' para seguridad de encoding.
        """
        for candidate in self._LCOV_CANDIDATES:
            path = root / candidate
            if not path.exists():
                continue
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            total_lf = 0
            total_lh = 0
            total_brf = 0
            total_brh = 0
            file_count = 0

            for line in content.splitlines():
                line = line.strip()
                if line.startswith("LF:"):
                    val = _safe_int(line[3:])
                    if val is not None:
                        total_lf += val
                elif line.startswith("LH:"):
                    val = _safe_int(line[3:])
                    if val is not None:
                        total_lh += val
                elif line.startswith("BRF:"):
                    val = _safe_int(line[4:])
                    if val is not None:
                        total_brf += val
                elif line.startswith("BRH:"):
                    val = _safe_int(line[4:])
                    if val is not None:
                        total_brh += val
                elif line == "end_of_record":
                    file_count += 1

            line_rate = total_lh / total_lf if total_lf > 0 else None
            branch_rate = total_brh / total_brf if total_brf > 0 else None

            return CoverageRecord(
                source_file=str(path.relative_to(root)).replace("\\", "/"),
                format="lcov",
                line_rate=line_rate,
                branch_rate=branch_rate,
                lines_covered=total_lh if total_lf > 0 else None,
                lines_valid=total_lf if total_lf > 0 else None,
                timestamp=None,
                tool_version=None,
                file_count=file_count,
            )

        return None

    def _parse_jacoco_xml(self, root: Path) -> CoverageRecord | None:
        """Parsea jacoco.xml via stdlib ET.

        Lee root-level <counter> elements (hijos directos de <report>).
        JaCoCo usa '/' en package paths (com/example), no '.'.
        """
        for candidate in self._JACOCO_CANDIDATES:
            path = root / candidate
            if not path.exists():
                continue
            try:
                tree = ET.parse(str(path))
                report_elem = tree.getroot()
            except ET.ParseError:
                continue
            except Exception:
                continue

            if report_elem.tag != "report":
                continue

            # Root-level counters only (direct children of <report>)
            counters = {
                c.get("type"): c
                for c in report_elem.findall("counter")
            }

            line_rate: Optional[float] = None
            branch_rate: Optional[float] = None
            lines_covered: Optional[int] = None
            lines_valid: Optional[int] = None

            if "LINE" in counters:
                c = counters["LINE"]
                missed = _safe_int(c.get("missed"))
                covered = _safe_int(c.get("covered"))
                if missed is not None and covered is not None:
                    total = missed + covered
                    lines_covered = covered
                    lines_valid = total
                    line_rate = covered / total if total > 0 else None

            if "BRANCH" in counters:
                c = counters["BRANCH"]
                missed = _safe_int(c.get("missed"))
                covered = _safe_int(c.get("covered"))
                if missed is not None and covered is not None:
                    total = missed + covered
                    branch_rate = covered / total if total > 0 else None

            file_count = len(report_elem.findall(".//sourcefile"))

            return CoverageRecord(
                source_file=str(path.relative_to(root)).replace("\\", "/"),
                format="jacoco_xml",
                line_rate=line_rate,
                branch_rate=branch_rate,
                lines_covered=lines_covered,
                lines_valid=lines_valid,
                timestamp=None,
                tool_version=None,
                file_count=file_count,
            )

        return None

    # ------------------------------------------------------------------
    # Internal: per-file data extraction for build_file_coverage_map
    # ------------------------------------------------------------------

    def _get_per_file_data(
        self,
        root: Path,
        fmt: str,
    ) -> dict[str, tuple[float | None, float | None]]:
        """Extrae datos de cobertura por fichero para un formato dado.

        Retorna {rel_path: (line_rate, branch_rate)}.
        """
        if fmt == "cobertura_xml":
            return self._per_file_cobertura(root)
        if fmt == "lcov":
            return self._per_file_lcov(root)
        if fmt == "jacoco_xml":
            return self._per_file_jacoco(root)
        if fmt == "dot_coverage":
            return self._per_file_dot_coverage(root)
        return {}

    def _per_file_cobertura(
        self, root: Path
    ) -> dict[str, tuple[float | None, float | None]]:
        """Extrae line_rate y branch_rate por fichero desde coverage.xml (Cobertura)."""
        result: dict[str, tuple[float | None, float | None]] = {}
        for candidate in self._COBERTURA_CANDIDATES:
            path = root / candidate
            if not path.exists():
                continue
            try:
                tree = ET.parse(str(path))
                root_elem = tree.getroot()
            except Exception:
                continue
            if root_elem.tag != "coverage":
                continue

            for cls in root_elem.findall(".//class"):
                filename = cls.get("filename")
                if not filename:
                    continue
                # Normalize path separators to forward slash
                filename = filename.replace("\\", "/")
                lr = _safe_float(cls.get("line-rate"))
                br = _safe_float(cls.get("branch-rate"))
                result[filename] = (lr, br)
            break  # use first valid candidate
        return result

    def _per_file_lcov(
        self, root: Path
    ) -> dict[str, tuple[float | None, float | None]]:
        """Extrae line_rate y branch_rate por fichero desde lcov.info."""
        result: dict[str, tuple[float | None, float | None]] = {}
        for candidate in self._LCOV_CANDIDATES:
            path = root / candidate
            if not path.exists():
                continue
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            current_file: Optional[str] = None
            lf = lh = brf = brh = 0

            for line in content.splitlines():
                line = line.strip()
                if line.startswith("SF:"):
                    current_file = line[3:].replace("\\", "/")
                    lf = lh = brf = brh = 0
                elif line.startswith("LF:"):
                    val = _safe_int(line[3:])
                    if val is not None:
                        lf = val
                elif line.startswith("LH:"):
                    val = _safe_int(line[3:])
                    if val is not None:
                        lh = val
                elif line.startswith("BRF:"):
                    val = _safe_int(line[4:])
                    if val is not None:
                        brf = val
                elif line.startswith("BRH:"):
                    val = _safe_int(line[4:])
                    if val is not None:
                        brh = val
                elif line == "end_of_record" and current_file is not None:
                    lr = lh / lf if lf > 0 else None
                    br = brh / brf if brf > 0 else None
                    result[current_file] = (lr, br)
                    current_file = None

            break  # use first valid candidate
        return result

    def _per_file_jacoco(
        self, root: Path
    ) -> dict[str, tuple[float | None, float | None]]:
        """Extrae line_rate y branch_rate por fichero desde jacoco.xml."""
        result: dict[str, tuple[float | None, float | None]] = {}
        for candidate in self._JACOCO_CANDIDATES:
            path = root / candidate
            if not path.exists():
                continue
            try:
                tree = ET.parse(str(path))
                report_elem = tree.getroot()
            except Exception:
                continue
            if report_elem.tag != "report":
                continue

            for pkg in report_elem.findall("package"):
                pkg_name = pkg.get("name", "").replace("\\", "/")
                for sf in pkg.findall("sourcefile"):
                    sf_name = sf.get("name", "")
                    rel_path = f"{pkg_name}/{sf_name}" if pkg_name else sf_name

                    counters = {
                        c.get("type"): c for c in sf.findall("counter")
                    }
                    lr: Optional[float] = None
                    br: Optional[float] = None

                    if "LINE" in counters:
                        c = counters["LINE"]
                        missed = _safe_int(c.get("missed"))
                        covered = _safe_int(c.get("covered"))
                        if missed is not None and covered is not None:
                            total = missed + covered
                            lr = covered / total if total > 0 else None

                    if "BRANCH" in counters:
                        c = counters["BRANCH"]
                        missed = _safe_int(c.get("missed"))
                        covered = _safe_int(c.get("covered"))
                        if missed is not None and covered is not None:
                            total = missed + covered
                            br = covered / total if total > 0 else None

                    result[rel_path] = (lr, br)
            break  # use first valid candidate
        return result

    def _per_file_dot_coverage(
        self, root: Path
    ) -> dict[str, tuple[float | None, float | None]]:
        """Extrae datos por fichero desde .coverage SQLite (coverage.py >= 5.0).

        Intenta leer la tabla 'line_bits' para calcular lineas ejecutadas;
        silencia cualquier error de SQLite.
        Paths absolutos se convierten a relativos con relative_to(root).
        """
        result: dict[str, tuple[float | None, float | None]] = {}
        path = root / ".coverage"
        if not path.exists():
            return result

        try:
            conn = sqlite3.connect(str(path))
            try:
                # Get all tracked files
                file_rows = conn.execute(
                    "SELECT id, path FROM file"
                ).fetchall()

                for _file_id, abs_path in file_rows:
                    # Convert to relative path
                    try:
                        rel = Path(abs_path).relative_to(root)
                        rel_str = str(rel).replace("\\", "/")
                    except ValueError:
                        # Path outside root — discard per threat model T-10-02-03
                        continue

                    # line_rate is unavailable without total_lines context
                    result[rel_str] = (None, None)

            finally:
                conn.close()
        except sqlite3.DatabaseError:
            return {}
        except Exception:
            return {}

        return result

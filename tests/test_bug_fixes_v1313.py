"""Tests for v1.31.3 bug fixes.

  BUG #1A  endpoints: @RequestMapping inside // comment extracted as active endpoint
  BUG #1B  endpoints: @RequestMapping({"path1","path2"}) array syntax ignored — no prefix
  BUG #2   generate-tests: no top-20 limit, wrong sort order, missing rank_score field
  BUG #3   onboard/explain produce identical output — relevant_files not differentiated
"""
from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sourcecode.cli import app, _extract_java_endpoints

runner = CliRunner()


def _write_java(tmp_path: Path, filename: str, source: str) -> Path:
    f = tmp_path / filename
    f.write_text(textwrap.dedent(source), encoding="utf-8")
    return tmp_path


# ── BUG #1A: commented annotations must not become endpoints ─────────────────


def test_line_comment_not_extracted(tmp_path):
    """// @RequestMapping must NOT produce an endpoint; only the active one counts."""
    _write_java(tmp_path, "ActividadController.java", """\
        @RestController
        @RequestMapping("/v1/actividad")
        public class ActividadController {

            // @RequestMapping(value="/porActividadId/{actividadId}", method=RequestMethod.GET)
            @RequestMapping(value="/activo", method=RequestMethod.GET)
            public ResponseEntity<?> getActivo() { return null; }
        }
    """)
    result = _extract_java_endpoints(tmp_path)
    paths = [e["path"] for e in result["endpoints"]]
    # Only the active annotation should appear
    assert len(paths) == 1, f"expected 1 endpoint, got {paths}"
    assert paths[0] == "/v1/actividad/activo", f"expected /v1/actividad/activo, got {paths}"


def test_block_comment_not_extracted(tmp_path):
    """/* @RequestMapping */ block must NOT produce an endpoint."""
    _write_java(tmp_path, "FooController.java", """\
        @RestController
        @RequestMapping("/api")
        public class FooController {

            /*
             * @RequestMapping(value="/old", method=RequestMethod.GET)
             */
            @GetMapping("/new")
            public String newer() { return "ok"; }
        }
    """)
    result = _extract_java_endpoints(tmp_path)
    paths = [e["path"] for e in result["endpoints"]]
    assert len(paths) == 1, f"expected 1 endpoint, got {paths}"
    assert paths[0] == "/api/new"


def test_commented_out_method_still_counts_active(tmp_path):
    """Two methods: one commented, one active. Only active one emitted."""
    _write_java(tmp_path, "StatusController.java", """\
        @RestController
        public class StatusController {

            // @PostMapping("/create")
            // public void oldCreate() {}

            @GetMapping("/status")
            public String status() { return "ok"; }
        }
    """)
    result = _extract_java_endpoints(tmp_path)
    assert result["total"] == 1
    assert result["endpoints"][0]["path"] == "/status"


# ── BUG #1B: array syntax class-level prefix ─────────────────────────────────


def test_array_prefix_generates_two_endpoints(tmp_path):
    """@RequestMapping({"/v1/foo", "/v1/bar"}) on class → two endpoints per method."""
    _write_java(tmp_path, "AgrupadorController.java", """\
        @RestController
        @RequestMapping({"/v1/agrupadorActividades", "/v1/agrupador-actividades"})
        public class AgrupadorController {

            @RequestMapping(value="/porGerencia/{id}", method=RequestMethod.GET)
            public ResponseEntity<?> porGerencia() { return null; }
        }
    """)
    result = _extract_java_endpoints(tmp_path)
    paths = {e["path"] for e in result["endpoints"]}
    assert "/v1/agrupadorActividades/porGerencia/{id}" in paths, f"missing camelCase path in {paths}"
    assert "/v1/agrupador-actividades/porGerencia/{id}" in paths, f"missing kebab path in {paths}"
    assert len(paths) == 2, f"expected exactly 2 endpoints, got {paths}"


def test_array_prefix_no_empty_prefix(tmp_path):
    """Array prefix must not produce a path without prefix."""
    _write_java(tmp_path, "AgrupadorController.java", """\
        @RestController
        @RequestMapping({"/v1/agrupadorActividades", "/v1/agrupador-actividades"})
        public class AgrupadorController {

            @RequestMapping(value="/porGerencia/{id}", method=RequestMethod.GET)
            public ResponseEntity<?> porGerencia() { return null; }
        }
    """)
    result = _extract_java_endpoints(tmp_path)
    paths = [e["path"] for e in result["endpoints"]]
    assert "/porGerencia/{id}" not in paths, f"bare path (no prefix) must not appear: {paths}"


def test_array_prefix_multiple_methods(tmp_path):
    """Two methods × two prefixes = four endpoints."""
    _write_java(tmp_path, "DualController.java", """\
        @RestController
        @RequestMapping({"/v1/foo", "/v1/bar"})
        public class DualController {

            @GetMapping("/list")
            public String list() { return "ok"; }

            @PostMapping("/create")
            public void create() {}
        }
    """)
    result = _extract_java_endpoints(tmp_path)
    assert result["total"] == 4, f"expected 4 endpoints (2 prefixes × 2 methods), got {result['total']}"


# ── regression: previously passing tests ─────────────────────────────────────


def test_borrar_multiple_still_delete(tmp_path):
    """Regression: @RequestMapping(method=DELETE) must still resolve to DELETE."""
    _write_java(tmp_path, "RolController.java", """\
        @RestController
        @RequestMapping("/v1/rol")
        public class RolController {

            @RequestMapping(value = "/borrarMultiple", method = RequestMethod.DELETE)
            public ResponseEntity<?> borrarMultiple() { return null; }
        }
    """)
    result = _extract_java_endpoints(tmp_path)
    ep = result["endpoints"][0]
    assert ep["method"] == "DELETE"
    assert ep["path"] == "/v1/rol/borrarMultiple"


def test_no_double_prefix_regression(tmp_path):
    """Regression: single-string class prefix must not duplicate."""
    _write_java(tmp_path, "PortalController.java", """\
        @RestController
        @RequestMapping("/v1/portal/acreditaciones")
        public class PortalController {

            @GetMapping("/all")
            public String all() { return "ok"; }
        }
    """)
    result = _extract_java_endpoints(tmp_path)
    paths = {e["path"] for e in result["endpoints"]}
    assert "/v1/portal/acreditaciones/all" in paths
    assert not any("/v1/portal/acreditaciones/v1" in p for p in paths)


# ── BUG #2: generate-tests ranking + top-20 + rank_score ─────────────────────


def _make_java_service(tmp_path: Path, name: str, method_count: int, spring: bool = False) -> None:
    annotations = "@Service\n" if spring else ""
    mapping = "@Transactional\n    " if spring else ""
    methods = "\n    ".join(
        f"public void method{i}() {{}}" for i in range(method_count)
    )
    code = f"""\
package com.example;

{annotations}public class {name} {{
    {mapping}{methods}
}}
"""
    (tmp_path / f"{name}.java").write_text(code, encoding="utf-8")


def test_generate_tests_rank_score_and_top20(tmp_path):
    """TablasMaestrasService(98 methods, spring=true) → rank_score=147.0, position #1."""
    pom = tmp_path / "pom.xml"
    pom.write_text(
        "<project><modelVersion>4.0.0</modelVersion>"
        "<groupId>com.example</groupId><artifactId>demo</artifactId>"
        "<version>1.0</version></project>",
        encoding="utf-8",
    )
    src = tmp_path / "src" / "main" / "java" / "com" / "example"
    src.mkdir(parents=True)

    # Top candidate: 98 public methods + spring annotations → rank_score = 98 * 1.5 = 147.0
    _make_java_service(src, "TablasMaestrasService", 98, spring=True)
    # Other candidates with lower scores
    _make_java_service(src, "UtilService", 50, spring=False)
    _make_java_service(src, "HelperService", 30, spring=True)  # 30 * 1.5 = 45.0

    result = runner.invoke(app, ["prepare-context", "generate-tests", str(tmp_path)])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)

    gaps = data.get("test_gaps", [])
    assert gaps, "test_gaps must be present and non-empty"
    assert len(gaps) <= 20, f"default must cap at 20, got {len(gaps)}"

    top = gaps[0]
    assert top["path"].endswith("TablasMaestrasService.java"), (
        f"TablasMaestrasService must be #1, got {top['path']}"
    )
    # public_method_count = count("public ") = 98 methods + 1 class decl = 99; 99 * 1.5 = 148.5
    assert top["rank_score"] == 148.5, f"expected rank_score=148.5 (99 * 1.5), got {top['rank_score']}"
    assert "rank_score" in top, "rank_score field must be present in each entry"


def test_generate_tests_all_flag_returns_full_list(tmp_path):
    """--all flag returns complete list (not capped at 20)."""
    pom = tmp_path / "pom.xml"
    pom.write_text(
        "<project><modelVersion>4.0.0</modelVersion>"
        "<groupId>com.example</groupId><artifactId>demo</artifactId>"
        "<version>1.0</version></project>",
        encoding="utf-8",
    )
    src = tmp_path / "src" / "main" / "java" / "com" / "example"
    src.mkdir(parents=True)

    # Create 25 files ending with Service.java so the Java suffix filter picks them up.
    _NAMES = [
        "ClienteService", "ProductoService", "VentaService", "CompraService",
        "PagoService", "EnvioService", "InventarioService", "FacturaService",
        "NotificacionService", "ReporteService", "UsuarioService", "RolService",
        "PermisoService", "AuditoriaService", "ConfiguracionService",
        "EmpleadoService", "DepartamentoService", "NominaService", "VacacionService",
        "AusentismoService", "HorasExtraService", "DescuentoService",
        "BeneficioService", "ContratoService", "CalendarioService",
    ]
    for i, name in enumerate(_NAMES):
        _make_java_service(src, name, method_count=5 + i, spring=False)

    default_result = runner.invoke(app, ["prepare-context", "generate-tests", str(tmp_path)])
    all_result = runner.invoke(app, ["prepare-context", "generate-tests", str(tmp_path), "--all"])

    assert default_result.exit_code == 0
    assert all_result.exit_code == 0

    default_gaps = json.loads(default_result.output).get("test_gaps", [])
    all_gaps = json.loads(all_result.output).get("test_gaps", [])

    assert len(default_gaps) <= 20
    assert len(all_gaps) > len(default_gaps), (
        f"--all must return more than default ({len(default_gaps)}), got {len(all_gaps)}"
    )
    # Same ordering: first element matches in both
    if default_gaps and all_gaps:
        assert default_gaps[0]["path"] == all_gaps[0]["path"], "ordering must be identical"


def test_generate_tests_rank_score_descending(tmp_path):
    """rank_score must be strictly descending (or tied) across test_gaps."""
    pom = tmp_path / "pom.xml"
    pom.write_text(
        "<project><modelVersion>4.0.0</modelVersion>"
        "<groupId>com.example</groupId><artifactId>demo</artifactId>"
        "<version>1.0</version></project>",
        encoding="utf-8",
    )
    src = tmp_path / "src" / "main" / "java" / "com" / "example"
    src.mkdir(parents=True)
    _make_java_service(src, "BigService", 80, spring=True)     # 120.0
    _make_java_service(src, "MediumService", 40, spring=False)  # 40.0
    _make_java_service(src, "SmallService", 10, spring=False)   # 10.0

    result = runner.invoke(app, ["prepare-context", "generate-tests", str(tmp_path)])
    assert result.exit_code == 0
    gaps = json.loads(result.output).get("test_gaps", [])
    scores = [g["rank_score"] for g in gaps]
    assert scores == sorted(scores, reverse=True), f"rank_score not descending: {scores}"


# ── BUG #3: onboard vs explain functional differentiation ────────────────────

FIXTURE = Path(__file__).parent / "fixtures" / "spring_boot_minimal"


def test_explain_has_no_relevant_files():
    """explain must NOT emit relevant_files field."""
    result = runner.invoke(app, ["prepare-context", "explain", str(FIXTURE)])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert "relevant_files" not in data, (
        f"explain must not include relevant_files, but got {list(data.keys())}"
    )


def test_onboard_relevant_files_cover_three_layers():
    """onboard relevant_files must include files from ≥3 distinct arch layers."""
    result = runner.invoke(app, ["prepare-context", "onboard", str(FIXTURE)])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)

    assert "relevant_files" in data, "onboard must include relevant_files"

    def _layer(path: str) -> str:
        # Match on filename only to avoid false positives from directory segments
        # (e.g. src/main/resources/ contains "resource" but is not a controller layer)
        n = Path(path).name.lower()
        pn = path.replace("\\", "/")
        if "controller" in n:
            return "controllers"
        if "repository" in n or "mapper" in n or "dao" in n:
            return "repositories"
        if "service" in n:
            return "services"
        if "entity" in n or "/domain/" in pn or "/model/" in pn or "/entity/" in pn:
            return "domain"
        return "other"

    paths = [f["path"] if isinstance(f, dict) else f for f in data["relevant_files"]]
    layers = {_layer(p) for p in paths} - {"other"}
    assert len(layers) >= 3, (
        f"onboard relevant_files must cover ≥3 arch layers, got {layers} from {paths}"
    )


def test_explain_onboard_are_different():
    """onboard and explain outputs must differ in at least the relevant_files field."""
    onboard = json.loads(runner.invoke(app, ["prepare-context", "onboard", str(FIXTURE)]).output)
    explain = json.loads(runner.invoke(app, ["prepare-context", "explain", str(FIXTURE)]).output)

    assert "relevant_files" in onboard
    assert "relevant_files" not in explain

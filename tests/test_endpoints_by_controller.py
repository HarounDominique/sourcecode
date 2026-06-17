"""Tests Fase 14 — API Surface por Controller (C4API-01, C4API-02).

C4API-01: cada endpoint incluye return_type.
C4API-02: agrupamiento estructurado por controller via --by-controller.

Los .java se escriben en tmp_path: una ruta bajo ``tests/`` seria excluida por
find_java_files (segmento de test), de ahi que se use un repo temporal.
"""

import json

from typer.testing import CliRunner

from sourcecode.cli import app, _group_endpoints_by_controller
from sourcecode.repository_ir import extract_java_endpoints

runner = CliRunner()

_CONTROLLER = """\
package com.example.ddd.ausente.infrastructure.rest;
import org.springframework.web.bind.annotation.*;
@RestController
@RequestMapping("/v1/ausente")
public class AusenteRestController {
    @GetMapping("/{id}")
    public AusenteDto find(@PathVariable Long id) { return null; }

    @PostMapping
    public void create(@RequestBody AusenteDto dto) { }

    @DeleteMapping("/{id}")
    public void remove(@PathVariable Long id) { }
}
"""


def _write_repo(tmp_path):
    pkg = tmp_path / "src" / "main" / "java" / "com" / "example" / "ddd" / "ausente" / "infrastructure" / "rest"
    pkg.mkdir(parents=True)
    (pkg / "AusenteRestController.java").write_text(_CONTROLLER, encoding="utf-8")
    return tmp_path


def _find(endpoints, path, method):
    for ep in endpoints:
        if ep.get("path") == path and ep.get("method") == method:
            return ep
    return None


# --- C4API-01 -----------------------------------------------------------------

def test_endpoint_includes_return_type(tmp_path):
    repo = _write_repo(tmp_path)
    eps = extract_java_endpoints(repo)["endpoints"]
    assert eps, "controller should expose endpoints"
    for ep in eps:
        assert "return_type" in ep, f"missing return_type: {ep}"
        assert isinstance(ep["return_type"], str)
        assert ep["return_type"], "return_type must not be empty"

    get = _find(eps, "/v1/ausente/{id}", "GET")
    assert get is not None
    assert get["return_type"] == "AusenteDto"

    post = _find(eps, "/v1/ausente", "POST")
    assert post is not None
    assert post["return_type"] == "void"


# --- C4API-02 -----------------------------------------------------------------

def test_group_by_controller_structure(tmp_path):
    eps = extract_java_endpoints(_write_repo(tmp_path))["endpoints"]
    grouped = _group_endpoints_by_controller(eps)
    assert isinstance(grouped["by_controller"], dict)
    assert grouped["controller_count"] == len(grouped["by_controller"])
    assert grouped["total"] == len(eps)

    key = next(k for k in grouped["by_controller"] if k.endswith("AusenteRestController"))
    routes = grouped["by_controller"][key]
    assert {"method": "GET", "path": "/v1/ausente/{id}", "return_type": "AusenteDto"} in routes
    assert {"method": "POST", "path": "/v1/ausente", "return_type": "void"} in routes


def test_group_by_controller_deterministic_order(tmp_path):
    eps = extract_java_endpoints(_write_repo(tmp_path))["endpoints"]
    g1 = _group_endpoints_by_controller(eps)
    g2 = _group_endpoints_by_controller(eps)
    assert g1 == g2
    for routes in g1["by_controller"].values():
        keys = [(r["path"], r["method"]) for r in routes]
        assert keys == sorted(keys), "routes must be sorted by (path, method)"


def test_by_controller_cli_flag(tmp_path):
    repo = _write_repo(tmp_path)
    res = runner.invoke(app, ["endpoints", str(repo), "--by-controller", "--format", "json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert "by_controller" in data
    assert "controller_count" in data
    assert data["controller_count"] == len(data["by_controller"])


def test_flat_mode_has_no_by_controller_key(tmp_path):
    repo = _write_repo(tmp_path)
    res = runner.invoke(app, ["endpoints", str(repo), "--format", "json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert "by_controller" not in data

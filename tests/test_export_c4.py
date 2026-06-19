"""Tests Fase 18 — export --c4 unified architecture + incremental manifest (C4FMT-01/02/03).

Vendor-neutral: asserts no third-party tool/company name leaks into the output.
"""

import json

from typer.testing import CliRunner

from sourcecode.cli import app

runner = CliRunner()

_SERVICE = """\
package com.x.service;
import org.springframework.stereotype.Service;
import org.springframework.web.client.RestTemplate;
@Service
public class FooService {
    private final RestTemplate rt = new RestTemplate();
    public String run() {
        return rt.getForObject("https://api.example.com/foo", String.class);
    }
}
"""

_CONTROLLER = """\
package com.x.web;
import org.springframework.web.bind.annotation.*;
import com.x.service.FooService;
@RestController
public class FooController {
    private final FooService svc;
    public FooController(FooService svc) { this.svc = svc; }
    @GetMapping("/a")
    public String get() { return svc.run(); }
}
"""

_POM = """<project><modelVersion>4.0.0</modelVersion></project>"""


def _write_repo(tmp_path, *, with_build=True):
    base = tmp_path / "src" / "main" / "java" / "com" / "x"
    (base / "service").mkdir(parents=True)
    (base / "web").mkdir(parents=True)
    (base / "service" / "FooService.java").write_text(_SERVICE, encoding="utf-8")
    (base / "web" / "FooController.java").write_text(_CONTROLLER, encoding="utf-8")
    if with_build:
        (tmp_path / "pom.xml").write_text(_POM, encoding="utf-8")
    return tmp_path


def test_c4_has_all_levels(tmp_path):
    repo = _write_repo(tmp_path)
    res = runner.invoke(app, ["export", str(repo), "--c4", "--format", "json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert data["schema_version"] == "c4-v1"
    c4 = data["c4"]
    assert set(c4) == {"context", "containers", "components", "code"}
    assert "api_surface" in data
    assert "manifest" in data
    # context external systems come from the integration detector
    assert c4["context"]["external_systems"]["count"] >= 1
    # components = module graph
    assert c4["components"]["summary"]["module_count"] >= 2
    # code = by-directory file:line map
    assert any("com/x/web" in d for d in c4["code"])


def test_c4_containers_from_build_file(tmp_path):
    repo = _write_repo(tmp_path, with_build=True)
    res = runner.invoke(app, ["export", str(repo), "--c4", "--format", "json"])
    data = json.loads(res.stdout)
    containers = data["c4"]["containers"]
    assert any(c["build_file"] == "pom.xml" for c in containers), containers


def test_c4_no_build_file_records_limitation(tmp_path):
    repo = _write_repo(tmp_path, with_build=False)
    res = runner.invoke(app, ["export", str(repo), "--c4", "--format", "json"])
    data = json.loads(res.stdout)
    assert data["c4"]["containers"] == []
    assert any("container" in lim.lower() for lim in data["limitations"]), data["limitations"]


def test_c4_manifest_hashes_are_deterministic(tmp_path):
    repo = _write_repo(tmp_path)
    r1 = json.loads(runner.invoke(app, ["export", str(repo), "--c4"]).stdout)
    r2 = json.loads(runner.invoke(app, ["export", str(repo), "--c4"]).stdout)
    h1 = r1["manifest"]["directory_hashes"]
    h2 = r2["manifest"]["directory_hashes"]
    assert h1 == h2 and h1, "hashes must be present and stable across runs"


def test_c4_manifest_hash_changes_on_edit(tmp_path):
    repo = _write_repo(tmp_path)
    before = json.loads(runner.invoke(app, ["export", str(repo), "--c4"]).stdout)
    svc_dir = "src/main/java/com/x/service"
    h_before = before["manifest"]["directory_hashes"][svc_dir]
    # Mutate a file in the service directory.
    (repo / "src/main/java/com/x/service/FooService.java").write_text(
        _SERVICE.replace("ok", "changed").replace("run", "execute"), encoding="utf-8"
    )
    after = json.loads(runner.invoke(app, ["export", str(repo), "--c4"]).stdout)
    assert after["manifest"]["directory_hashes"][svc_dir] != h_before


_DDD_ENTITY = """\
package com.x.cotizacion.domain;
public class Cotizacion {
    private Long id;
    public Long getId() { return id; }
}
"""
_DDD_SERVICE = """\
package com.x.cotizacion.application;
import com.x.cotizacion.domain.Cotizacion;
public class CotizacionService {
    public Cotizacion build() { return new Cotizacion(); }
}
"""
_DDD_REPO = """\
package com.x.cotizacion.infrastructure;
import com.x.cotizacion.domain.Cotizacion;
public class CotizacionRepo {
    public void save(Cotizacion c) {}
}
"""
_LEGACY_ENTITY = """\
package com.x.puesto;
public class Puesto {
    private Long id;
    public Long getId() { return id; }
}
"""


def _write_ddd_repo(tmp_path):
    base = tmp_path / "src" / "main" / "java" / "com" / "x"
    (base / "cotizacion" / "domain").mkdir(parents=True)
    (base / "cotizacion" / "application").mkdir(parents=True)
    (base / "cotizacion" / "infrastructure").mkdir(parents=True)
    (base / "puesto").mkdir(parents=True)
    (base / "cotizacion" / "domain" / "Cotizacion.java").write_text(_DDD_ENTITY, encoding="utf-8")
    (base / "cotizacion" / "application" / "CotizacionService.java").write_text(_DDD_SERVICE, encoding="utf-8")
    (base / "cotizacion" / "infrastructure" / "CotizacionRepo.java").write_text(_DDD_REPO, encoding="utf-8")
    (base / "puesto" / "Puesto.java").write_text(_LEGACY_ENTITY, encoding="utf-8")
    (tmp_path / "pom.xml").write_text(_POM, encoding="utf-8")
    return tmp_path


def test_c4_module_roots_rollup_layered_module(tmp_path):
    """DDD module split across domain/application/infrastructure -> one module root."""
    repo = _write_ddd_repo(tmp_path)
    data = json.loads(runner.invoke(app, ["export", str(repo), "--c4"]).stdout)
    roots = data["c4"]["components"]["module_roots"]
    cot = [m for m in roots["modules"] if m["root"].endswith("com/x/cotizacion")]
    assert len(cot) == 1, roots["modules"]
    m = cot[0]
    assert m["pattern"] == "layered", m
    assert set(m["layers"]) == {"domain", "application", "infrastructure"}, m
    assert m["leaf_dir_count"] == 3, m


def test_c4_module_roots_flat_module_classified_legacy(tmp_path):
    """Flat package (no DDD layers) -> classified flat, not layered."""
    repo = _write_ddd_repo(tmp_path)
    data = json.loads(runner.invoke(app, ["export", str(repo), "--c4"]).stdout)
    roots = data["c4"]["components"]["module_roots"]
    puesto = [m for m in roots["modules"] if m["root"].endswith("com/x/puesto")]
    assert len(puesto) == 1, roots["modules"]
    assert puesto[0]["pattern"] == "flat", puesto[0]
    assert puesto[0]["layers"] == [], puesto[0]


def test_c4_module_roots_summary_counts(tmp_path):
    repo = _write_ddd_repo(tmp_path)
    data = json.loads(runner.invoke(app, ["export", str(repo), "--c4"]).stdout)
    summary = data["c4"]["components"]["module_roots"]["summary"]
    assert summary["module_count"] == summary["layered_module_count"] + summary["flat_module_count"]
    assert summary["layered_module_count"] >= 1
    assert summary["flat_module_count"] >= 1


def test_c4_output_is_vendor_neutral(tmp_path):
    repo = _write_repo(tmp_path)
    out = runner.invoke(app, ["export", str(repo), "--c4"]).stdout.lower()
    for forbidden in ("banyan", "structurizr", "memory bank"):
        assert forbidden not in out, f"vendor leak: {forbidden}"

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


def test_c4_output_is_vendor_neutral(tmp_path):
    repo = _write_repo(tmp_path)
    out = runner.invoke(app, ["export", str(repo), "--c4"]).stdout.lower()
    for forbidden in ("banyan", "structurizr", "memory bank"):
        assert forbidden not in out, f"vendor leak: {forbidden}"

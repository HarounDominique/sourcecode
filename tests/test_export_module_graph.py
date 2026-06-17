"""Tests Fase 16 — export --module-graph (C4GRAPH-01/02)."""

import json

from typer.testing import CliRunner

from sourcecode.cli import app

runner = CliRunner()

# web package depends on service package (calls + import edges across modules).
_SERVICE = """\
package com.x.service;
import org.springframework.stereotype.Service;
@Service
public class FooService {
    public String run() { return "ok"; }
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


def _write_repo(tmp_path):
    base = tmp_path / "src" / "main" / "java" / "com" / "x"
    (base / "service").mkdir(parents=True)
    (base / "web").mkdir(parents=True)
    (base / "service" / "FooService.java").write_text(_SERVICE, encoding="utf-8")
    (base / "web" / "FooController.java").write_text(_CONTROLLER, encoding="utf-8")
    return tmp_path


def test_module_graph_has_modules_and_edges(tmp_path):
    repo = _write_repo(tmp_path)
    res = runner.invoke(app, ["export", str(repo), "--module-graph", "--format", "json"])
    assert res.exit_code == 0, res.output
    mg = json.loads(res.stdout)["module_graph"]
    mods = {n["module"] for n in mg["nodes"]}
    assert "src/main/java/com/x/web" in mods, mods
    assert "src/main/java/com/x/service" in mods, mods
    assert mg["summary"]["module_count"] == len(mg["nodes"])


def test_module_graph_edge_web_to_service(tmp_path):
    repo = _write_repo(tmp_path)
    res = runner.invoke(app, ["export", str(repo), "--module-graph", "--format", "json"])
    assert res.exit_code == 0, res.output
    edges = json.loads(res.stdout)["module_graph"]["edges"]
    web = "src/main/java/com/x/web"
    svc = "src/main/java/com/x/service"
    hit = [e for e in edges if e["from"] == web and e["to"] == svc]
    assert hit, edges
    assert hit[0]["count"] >= 1
    assert hit[0]["types"]  # at least one underlying edge type recorded


def test_module_graph_combines_with_by_directory(tmp_path):
    repo = _write_repo(tmp_path)
    res = runner.invoke(
        app, ["export", str(repo), "--module-graph", "--by-directory", "--format", "json"]
    )
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert "module_graph" in data
    assert "by_directory" in data

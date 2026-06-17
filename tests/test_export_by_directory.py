"""Tests Fase 15 — export --by-directory (C4DIR-03)."""

import json
import re

from typer.testing import CliRunner

from sourcecode.cli import app

runner = CliRunner()

_CONTROLLER = """\
package com.x;
import org.springframework.web.bind.annotation.*;
@RestController
public class AController {
    @GetMapping("/a")
    public Foo get() { return null; }
}
"""


def _write_repo(tmp_path):
    pkg = tmp_path / "src" / "main" / "java" / "com" / "x"
    pkg.mkdir(parents=True)
    (pkg / "AController.java").write_text(_CONTROLLER, encoding="utf-8")
    return tmp_path


def test_export_by_directory_groups_and_refs(tmp_path):
    repo = _write_repo(tmp_path)
    res = runner.invoke(app, ["export", str(repo), "--by-directory", "--format", "json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert "by_directory" in data
    by_dir = data["by_directory"]
    assert "src/main/java/com/x" in by_dir, list(by_dir)
    syms = by_dir["src/main/java/com/x"]
    refs = [s["ref"] for s in syms]
    assert any(re.fullmatch(r"src/main/java/com/x/AController\.java:\d+", r) for r in refs), refs


def test_export_requires_a_mode(tmp_path):
    repo = _write_repo(tmp_path)
    res = runner.invoke(app, ["export", str(repo), "--format", "json"])
    # sin --by-directory: error claro (otros formatos llegan en Fase 18), no crash silencioso
    assert res.exit_code != 0 or "by_directory" not in res.stdout

"""Regression tests for the v1.45 field-audit fixes (repo SAINT, 2026-06-16).

Covers:
  #2  --no-cache is accepted (no-op) on analysis subcommands instead of
      erroring with "No such option" (broke scripted/CI invocations).
  #3  endpoints --limit keeps the security counters coherent: total and
      no_security_signal must describe the SAME (limited) result set, with the
      repo-wide values preserved under _filter.
  #5  validation surfaces an explicit note (JSON + stderr) when no OpenAPI spec
      is found, instead of returning all-zeros silently (read as false negative).
"""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from sourcecode.cli import _preprocess_args, _set_detected_path, app

_runner = CliRunner()


def invoke(args: list[str]):
    _set_detected_path(".")
    processed = _preprocess_args(list(args))
    return _runner.invoke(app, processed)


# Three REST endpoints, no security annotations → all none_detected.
_CONTROLLER = """\
package com.example.rest;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api")
public class ThingController {

    @GetMapping("/a")
    public String a() {
        return "a";
    }

    @GetMapping("/b")
    public String b() {
        return "b";
    }

    @PostMapping("/c")
    public String c(@RequestBody String body) {
        return body;
    }
}
"""


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    src = repo / "src/main/java/com/example/rest"
    src.mkdir(parents=True)
    (src / "ThingController.java").write_text(_CONTROLLER, encoding="utf-8")
    (repo / ".git").mkdir()
    return repo


# ── #2 ───────────────────────────────────────────────────────────────────────

class TestNoCacheAcceptedOnSubcommands:
    def test_endpoints_accepts_no_cache(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        result = invoke(["endpoints", str(repo), "--no-cache"])
        assert result.exit_code == 0, result.output
        assert "no such option" not in result.output.lower()

    def test_migrate_check_accepts_no_cache(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        result = invoke(["migrate-check", str(repo), "--no-cache"])
        assert "no such option" not in result.output.lower(), result.output


# ── #3 ───────────────────────────────────────────────────────────────────────

class TestEndpointLimitCounterCoherence:
    def test_limit_recomputes_security_counters(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        full = invoke(["endpoints", str(repo), "--format", "json"])
        assert full.exit_code == 0, full.output
        full_data = json.loads(full.output)
        assert full_data["total"] == 3
        assert full_data["no_security_signal"] == 3

        limited = invoke(["endpoints", str(repo), "--limit", "2", "--format", "json"])
        assert limited.exit_code == 0, limited.output
        data = json.loads(limited.output)
        # Counters must describe the limited set, not the repo-wide one.
        assert data["total"] == 2
        assert data["no_security_signal"] == 2
        assert data["undocumented"] == 2
        assert data["no_security_signal"] <= data["total"]
        # Repo-wide values preserved for context.
        assert data["_filter"]["total_before_filter"] == 3
        assert data["_filter"]["no_security_signal_before_filter"] == 3


# ── #5 ───────────────────────────────────────────────────────────────────────

class TestValidationNoOpenApiNote:
    def test_note_present_when_no_spec(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)  # no openapi.yml on disk
        result = invoke(["validation", str(repo), "--format", "json"])
        assert result.exit_code == 0, result.output
        # The human-facing "Note:" line is emitted on stderr; CliRunner may
        # interleave it ahead of the JSON, so parse from the first brace.
        out = result.output
        data = json.loads(out[out.index("{"):])
        assert data.get("openapi_spec") is None
        assert "note" in data and "OpenAPI" in data["note"]
        # The note must also reach a human via the combined output stream.
        assert "OpenAPI" in out

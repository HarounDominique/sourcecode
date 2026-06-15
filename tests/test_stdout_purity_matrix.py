"""Wave 19-03 — stdout-purity regression matrix for the output-format contract.

For every machine-consumable command this asserts the strict contract:

  * ``-f json`` -> stdout is parseable JSON and nothing else (no Markdown,
    no log lines leaking onto stdout),
  * ``--no-cache`` does not change the shape of stdout (still pure JSON),
  * an invalid ``--format`` -> exit code 2, empty stdout, and a JSON error
    envelope on stderr carrying ``flag``/``value``.

Runs the real ``sourcecode`` entry point via subprocess so stdout and stderr
are genuinely separated (a CliRunner mixes them).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("sourcecode") is None,
    reason="sourcecode entry point not installed",
)

_CONTROLLER = """\
package com.example;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
public class GreetingController {
    @GetMapping("/hello")
    public String hello() { return "hi"; }
}
"""


@pytest.fixture
def java_repo(tmp_path: Path) -> Path:
    src = tmp_path / "src" / "main" / "java" / "com" / "example"
    src.mkdir(parents=True)
    (src / "GreetingController.java").write_text(_CONTROLLER)
    (tmp_path / "pom.xml").write_text(
        "<project><modelVersion>4.0.0</modelVersion>"
        "<groupId>com.example</groupId><artifactId>demo</artifactId>"
        "<version>0.0.1</version></project>"
    )
    return tmp_path


_TARGET = "com.example.GreetingController"

# command id -> base argv (without the repo path or format flags).
# Repo path is appended by the test. Commands that need a symbol carry it here.
_COMMANDS: "dict[str, list[str]]" = {
    "main": ["--compact"],
    "endpoints": ["endpoints"],
    "spring-audit": ["spring-audit"],
    "impact": ["impact", _TARGET],
    "repo-ir": ["repo-ir"],
    "explain": ["explain", _TARGET],
}


def _run(argv: "list[str]") -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        ["sourcecode", *argv],
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("cmd_id", list(_COMMANDS))
def test_json_stdout_is_pure(cmd_id: str, java_repo: Path):
    base = _COMMANDS[cmd_id]
    result = _run([*base, str(java_repo), "-f", "json"])
    assert result.returncode == 0, (
        f"{cmd_id} -f json should exit 0, got {result.returncode}; stderr={result.stderr!r}"
    )
    try:
        json.loads(result.stdout)
    except json.JSONDecodeError:
        pytest.fail(
            f"{cmd_id} -f json stdout must be pure JSON, got:\n{result.stdout[:400]!r}"
        )


@pytest.mark.parametrize("cmd_id", list(_COMMANDS))
def test_no_cache_keeps_json_shape(cmd_id: str, java_repo: Path):
    base = _COMMANDS[cmd_id]
    # --no-cache is a global callback flag -> precedes the subcommand.
    result = _run(["--no-cache", *base, str(java_repo), "-f", "json"])
    assert result.returncode == 0, (
        f"{cmd_id} --no-cache -f json should exit 0, got {result.returncode}; "
        f"stderr={result.stderr!r}"
    )
    try:
        json.loads(result.stdout)
    except json.JSONDecodeError:
        pytest.fail(
            f"{cmd_id} --no-cache stdout must be pure JSON, got:\n{result.stdout[:400]!r}"
        )


@pytest.mark.parametrize("cmd_id", list(_COMMANDS))
def test_invalid_format_is_uniform(cmd_id: str, java_repo: Path):
    base = _COMMANDS[cmd_id]
    result = _run([*base, str(java_repo), "--format", "xml"])
    # Argument-validation error -> exit 2 on every command.
    assert result.returncode == 2, (
        f"{cmd_id} invalid --format should exit 2, got {result.returncode}"
    )
    # Nothing leaks onto stdout on an arg-validation failure.
    assert result.stdout.strip() == "", (
        f"{cmd_id} must not write to stdout on invalid --format, got:\n{result.stdout[:200]!r}"
    )
    # stderr is the homogeneous JSON error envelope.
    payload = json.loads(result.stderr.strip())
    assert payload["error"]["code"] == "INVALID_INPUT"
    assert payload["flag"] == "--format"
    assert payload["value"] == "xml"

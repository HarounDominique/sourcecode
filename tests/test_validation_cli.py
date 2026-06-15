"""CLI tests for the `validation` command (Phase 20).

Verifies the format contract (strict -f json, exit 2 on bad format), the
--gaps-only and --path-prefix filters, and stdout JSON purity.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sourcecode.cli import _preprocess_args, _set_detected_path, app

_runner = CliRunner()


def invoke(args: list[str]):
    _set_detected_path(".")
    processed = _preprocess_args(list(args))
    return _runner.invoke(app, processed)


_CONTROLLER = """\
package com.example.rest;

import org.springframework.web.bind.annotation.RestController;

@RestController
public class PetRestController implements PetsApi {}
"""

_SPEC = """\
openapi: 3.0.1
info:
  title: Demo
  version: '1.0'
paths:
  /pets:
    post:
      tags: [pets]
      operationId: addPet
      requestBody:
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/PetFields'
      responses:
        '201':
          description: created
components:
  schemas:
    PetFields:
      type: object
      properties:
        name:
          type: string
          minLength: 1
          pattern: "^[A-Za-z].*"
      required: [name]
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    java = tmp_path / "src" / "main" / "java" / "com" / "example" / "rest"
    java.mkdir(parents=True)
    (java / "PetRestController.java").write_text(_CONTROLLER)
    res = tmp_path / "src" / "main" / "resources"
    res.mkdir(parents=True)
    (res / "openapi.yml").write_text(_SPEC)
    (tmp_path / "pom.xml").write_text(
        "<project><modelVersion>4.0.0</modelVersion>"
        "<groupId>com.example</groupId><artifactId>demo</artifactId>"
        "<version>0</version></project>"
    )
    return tmp_path


class TestValidationCli:
    def test_help(self):
        result = invoke(["validation", "--help"])
        assert result.exit_code == 0
        assert "validation" in result.stdout.lower()

    def test_json_default_is_pure(self, repo: Path):
        result = invoke(["validation", str(repo)])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert "endpoints" in data
        assert "summary" in data
        post = next(e for e in data["endpoints"] if e["handler"] == "addPet")
        assert post["schema"] == "PetFields"

    def test_yaml_format(self, repo: Path):
        result = invoke(["validation", str(repo), "-f", "yaml"])
        assert result.exit_code == 0
        assert "summary:" in result.stdout

    def test_invalid_format_exits_2(self, repo: Path):
        result = invoke(["validation", str(repo), "-f", "xml"])
        assert result.exit_code == 2

    def test_gaps_only_shape(self, repo: Path):
        result = invoke(["validation", str(repo), "--gaps-only"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert set(data.keys()) == {"gaps", "summary"}

    def test_path_prefix_filter(self, repo: Path):
        result = invoke(["validation", str(repo), "-p", "/nonexistent"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["endpoints"] == []

    def test_missing_directory(self, tmp_path: Path):
        result = invoke(["validation", str(tmp_path / "nope")])
        assert result.exit_code == 1

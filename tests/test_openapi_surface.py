"""Unit tests for openapi_surface (Phase 18, wave 18-01).

Self-contained fixtures — no dependency on any cloned repo.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sourcecode.openapi_surface import (
    build_openapi_surface,
    find_openapi_specs,
    parse_openapi_spec,
)

_SPEC = """\
openapi: 3.0.1
info:
  title: Demo
  version: '1.0'
paths:
  /owners:
    post:
      tags: [owners]
      operationId: addOwner
      requestBody:
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/OwnerFields'
      responses:
        '201':
          description: created
  /owners/{id}:
    get:
      tags: [owners]
      operationId: getOwner
      security:
        - basicAuth: []
      responses:
        '200':
          description: ok
components:
  schemas:
    OwnerFields:
      type: object
      properties:
        firstName:
          type: string
          minLength: 1
          maxLength: 30
          pattern: "^[A-Z].*"
        city:
          type: string
      required:
        - firstName
    Owner:
      allOf:
        - $ref: '#/components/schemas/OwnerFields'
        - type: object
          properties:
            id:
              type: integer
              format: int32
              minimum: 0
          required:
            - id
"""


@pytest.fixture
def spec_repo(tmp_path: Path) -> Path:
    res = tmp_path / "src" / "main" / "resources"
    res.mkdir(parents=True)
    (res / "openapi.yml").write_text(_SPEC)
    # noise: a non-spec yaml that must be ignored
    (res / "application.yml").write_text("server:\n  port: 8080\n")
    # noise: a spec-looking file under target/ that must be skipped
    gen = tmp_path / "target" / "generated-sources"
    gen.mkdir(parents=True)
    (gen / "openapi.yaml").write_text(_SPEC)
    return tmp_path


class TestDiscovery:
    def test_finds_spec_in_resources(self, spec_repo: Path):
        specs = find_openapi_specs(spec_repo)
        names = [p.name for p in specs]
        assert "openapi.yml" in names

    def test_ignores_non_spec_yaml(self, spec_repo: Path):
        specs = find_openapi_specs(spec_repo)
        assert all(p.name != "application.yml" for p in specs)

    def test_skips_target_generated(self, spec_repo: Path):
        specs = find_openapi_specs(spec_repo)
        assert all("target" not in p.parts for p in specs)

    def test_no_spec_returns_empty(self, tmp_path: Path):
        (tmp_path / "main.py").write_text("print(1)")
        assert find_openapi_specs(tmp_path) == []


class TestOperations:
    def test_operation_count_and_fields(self, spec_repo: Path):
        surface = build_openapi_surface(spec_repo)
        assert surface is not None
        ops = {o.operation_id: o for o in surface.operations}
        assert set(ops) == {"addOwner", "getOwner"}
        add = ops["addOwner"]
        assert add.method == "POST"
        assert add.path == "/owners"
        assert add.tags == ["owners"]
        assert add.request_body_schema == "OwnerFields"
        assert add.has_security is False

    def test_security_flag(self, spec_repo: Path):
        surface = build_openapi_surface(spec_repo)
        get = next(o for o in surface.operations if o.operation_id == "getOwner")
        assert get.has_security is True
        assert get.method == "GET"


class TestSchemasAndConstraints:
    def test_constraints_captured(self, spec_repo: Path):
        surface = build_openapi_surface(spec_repo)
        owner_fields = surface.schemas["OwnerFields"]
        by_name = {f.name: f for f in owner_fields.fields}
        first = by_name["firstName"]
        assert first.required is True
        assert first.pattern == "^[A-Z].*"
        assert first.min_length == 1
        assert first.max_length == 30
        assert first.type == "string"
        # city present but not required
        assert by_name["city"].required is False

    def test_allof_flattening_and_required_union(self, spec_repo: Path):
        surface = build_openapi_surface(spec_repo)
        owner = surface.schemas["Owner"]
        names = {f.name for f in owner.fields}
        # inherited from OwnerFields + own id
        assert {"firstName", "city", "id"} <= names
        by_name = {f.name: f for f in owner.fields}
        assert by_name["id"].required is True
        assert by_name["firstName"].required is True  # required carried via allOf
        assert by_name["id"].minimum == 0.0

    def test_to_dict_roundtrip_shapes(self, spec_repo: Path):
        surface = build_openapi_surface(spec_repo)
        d = surface.to_dict()
        assert "operations" in d and "schemas" in d
        assert isinstance(d["operations"], list)
        assert "OwnerFields" in d["schemas"]


class TestDefensive:
    def test_malformed_yaml_returns_none(self, tmp_path: Path):
        p = tmp_path / "openapi.yml"
        p.write_text("openapi: 3.0.1\npaths: [this is: not valid: mapping\n")
        # parse must not raise; returns None or a partial surface
        result = parse_openapi_spec(p)
        assert result is None or result.spec_path == str(p)

    def test_non_spec_file_returns_none(self, tmp_path: Path):
        p = tmp_path / "random.yml"
        p.write_text("foo: bar\n")
        assert parse_openapi_spec(p) is None

    def test_json_spec_supported(self, tmp_path: Path):
        import json

        spec = {
            "openapi": "3.0.1",
            "paths": {
                "/ping": {"get": {"operationId": "ping", "responses": {}}}
            },
            "components": {"schemas": {}},
        }
        p = tmp_path / "openapi.json"
        p.write_text(json.dumps(spec))
        surface = parse_openapi_spec(p)
        assert surface is not None
        assert surface.operations[0].operation_id == "ping"

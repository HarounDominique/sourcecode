"""Integration tests: recovering endpoint + constraint surface from an OpenAPI
spec for interface-defined (openapi-generator) controllers (Phase 18, 18-02/03).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sourcecode.repository_ir import extract_java_endpoints

_CONTROLLER = """\
package com.example.rest;

import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api")
public class ThingRestController implements ThingsApi {
    // HTTP mappings live on the generated ThingsApi interface (not scanned).
}
"""

_SPEC = """\
openapi: 3.0.1
info:
  title: Demo
  version: '1.0'
paths:
  /things:
    get:
      tags: [things]
      operationId: listThings
      responses:
        '200':
          description: ok
    post:
      tags: [things]
      operationId: addThing
      requestBody:
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/ThingFields'
      responses:
        '201':
          description: created
components:
  schemas:
    ThingFields:
      type: object
      properties:
        name:
          type: string
          minLength: 1
          maxLength: 80
          pattern: "^[A-Za-z].*"
      required:
        - name
"""


@pytest.fixture
def spec_controller_repo(tmp_path: Path) -> Path:
    src = tmp_path / "src" / "main" / "java" / "com" / "example" / "rest"
    src.mkdir(parents=True)
    (src / "ThingRestController.java").write_text(_CONTROLLER)
    res = tmp_path / "src" / "main" / "resources"
    res.mkdir(parents=True)
    (res / "openapi.yml").write_text(_SPEC)
    (tmp_path / "pom.xml").write_text(
        "<project><modelVersion>4.0.0</modelVersion>"
        "<groupId>com.example</groupId><artifactId>demo</artifactId>"
        "<version>0</version></project>"
    )
    return tmp_path


class TestSpecResolved:
    def test_endpoints_recovered_from_spec(self, spec_controller_repo: Path):
        result = extract_java_endpoints(spec_controller_repo)
        spec_eps = [e for e in result["endpoints"] if e.get("source") == "openapi-spec"]
        # tag "things" -> ThingsApi -> matched; both GET and POST recovered
        handlers = {e["handler"] for e in spec_eps}
        assert handlers == {"listThings", "addThing"}
        methods = {(e["method"], e["path"]) for e in spec_eps}
        assert ("GET", "/things") in methods
        assert ("POST", "/things") in methods

    def test_controller_marked_resolved_no_warning(self, spec_controller_repo: Path):
        result = extract_java_endpoints(spec_controller_repo)
        assert any(
            fqn.endswith("ThingRestController")
            for fqn in result.get("resolved_from_openapi_spec", [])
        )
        # resolved -> no "NOT captured" warning, no interface_defined_controllers
        assert "warnings" not in result
        assert "interface_defined_controllers" not in result
        assert result.get("openapi_spec", "").endswith("openapi.yml")

    def test_request_body_constraints_attached(self, spec_controller_repo: Path):
        result = extract_java_endpoints(spec_controller_repo)
        post = next(
            e for e in result["endpoints"]
            if e.get("handler") == "addThing"
        )
        rb = post["request_body"]
        assert rb["schema"] == "ThingFields"
        name = next(f for f in rb["constraints"] if f["name"] == "name")
        assert name["required"] is True
        assert name["pattern"] == "^[A-Za-z].*"
        assert name["minLength"] == 1
        assert name["maxLength"] == 80


class TestSpecUnresolved:
    def test_no_matching_tag_keeps_warning(self, tmp_path: Path):
        # Controller implements an interface with no matching spec tag.
        src = tmp_path / "src" / "main" / "java" / "com" / "example"
        src.mkdir(parents=True)
        (src / "OrphanController.java").write_text(
            "package com.example;\n"
            "import org.springframework.web.bind.annotation.RestController;\n"
            "@RestController\n"
            "public class OrphanController implements WidgetsApi {}\n"
        )
        res = tmp_path / "src" / "main" / "resources"
        res.mkdir(parents=True)
        # spec has only "things", not "widgets"
        (res / "openapi.yml").write_text(_SPEC)
        result = extract_java_endpoints(tmp_path)
        assert any("OrphanController" in w for w in result.get("warnings", []))
        assert any(
            fqn.endswith("OrphanController")
            for fqn in result.get("interface_defined_controllers", [])
        )

    def test_no_spec_present_keeps_warning(self, tmp_path: Path):
        src = tmp_path / "src" / "main" / "java" / "com" / "example"
        src.mkdir(parents=True)
        (src / "ThingRestController.java").write_text(_CONTROLLER)
        result = extract_java_endpoints(tmp_path)
        # no spec -> legacy behavior: warning, no resolution
        assert result.get("warnings")
        assert "resolved_from_openapi_spec" not in result

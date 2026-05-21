"""Tests for v1.31.2 bug fixes.

  BUG #1  endpoints: class-level @RequestMapping leaked into method loop
          → all methods showed class path only (or double prefix)
  BUG #2  endpoints: @RequestMapping(method=RequestMethod.DELETE) resolved as GET
"""
from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sourcecode.cli import app, _extract_java_endpoints

runner = CliRunner()


# ── helpers ───────────────────────────────────────────────────────────────────

def _write_java(tmp_path: Path, filename: str, source: str) -> Path:
    f = tmp_path / filename
    f.write_text(textwrap.dedent(source), encoding="utf-8")
    return tmp_path


# ── BUG #1: path concatenation ─────────────────────────────────────────────


def test_acceso_rol_paths_correct(tmp_path):
    """AccesoRolRestController: each method gets class_base + method_path."""
    _write_java(tmp_path, "AccesoRolRestController.java", """\
        @RestController
        @RequestMapping("/v1/accesoRol")
        public class AccesoRolRestController {

            @M3FiltroSeguridad("accesoRol")
            @GetMapping("/list")
            public ResponseEntity<?> list() { return null; }

            @PostMapping("/create")
            public ResponseEntity<?> create() { return null; }

            @DeleteMapping("/{id}")
            public ResponseEntity<?> delete() { return null; }
        }
    """)
    result = _extract_java_endpoints(tmp_path)
    paths = {e["path"] for e in result["endpoints"]}
    assert "/v1/accesoRol/list" in paths, f"expected /v1/accesoRol/list, got {paths}"
    assert "/v1/accesoRol/create" in paths
    assert "/v1/accesoRol/{id}" in paths
    # class path alone must NOT appear as a standalone endpoint
    assert "/v1/accesoRol" not in paths
    # no double-prefix
    assert not any("/v1/accesoRol/v1/accesoRol" in p for p in paths)


def test_no_double_prefix(tmp_path):
    """Class path must not be concatenated with itself."""
    _write_java(tmp_path, "PortalController.java", """\
        @RestController
        @RequestMapping("/v1/portal/acreditaciones")
        public class PortalController {

            @GetMapping("/all")
            public List<?> getAll() { return null; }
        }
    """)
    result = _extract_java_endpoints(tmp_path)
    paths = {e["path"] for e in result["endpoints"]}
    assert "/v1/portal/acreditaciones/all" in paths
    assert not any("acreditaciones/v1" in p for p in paths)


def test_method_count_one_per_handler(tmp_path):
    """Each handler must produce exactly one endpoint, not two."""
    _write_java(tmp_path, "FooController.java", """\
        @RestController
        @RequestMapping("/api")
        public class FooController {

            @GetMapping("/items")
            public List<?> items() { return null; }

            @PostMapping("/items")
            public void save() { }
        }
    """)
    result = _extract_java_endpoints(tmp_path)
    assert result["total"] == 2
    handlers = [e["handler"] for e in result["endpoints"]]
    assert len(handlers) == len(set(handlers)), "duplicate handlers found"


def test_no_class_level_path_in_method_loop(tmp_path):
    """Class-level @RequestMapping must not pollute method loop."""
    _write_java(tmp_path, "SimpleController.java", """\
        @RestController
        @RequestMapping(value = "/base")
        public class SimpleController {

            @GetMapping
            public String root() { return "ok"; }
        }
    """)
    result = _extract_java_endpoints(tmp_path)
    # root() with @GetMapping (no path) → /base
    assert result["total"] == 1
    assert result["endpoints"][0]["path"] == "/base"
    assert result["endpoints"][0]["handler"] == "root"


# ── BUG #2: HTTP method resolution ────────────────────────────────────────────


def test_request_mapping_delete_method(tmp_path):
    """@RequestMapping(method=RequestMethod.DELETE) must resolve to DELETE."""
    _write_java(tmp_path, "RolController.java", """\
        @RestController
        @RequestMapping("/v1/rol")
        public class RolController {

            @RequestMapping(value = "/borrarMultiple", method = RequestMethod.DELETE)
            public ResponseEntity<?> borrarMultiple() { return null; }
        }
    """)
    result = _extract_java_endpoints(tmp_path)
    assert result["total"] == 1
    ep = result["endpoints"][0]
    assert ep["method"] == "DELETE", f"expected DELETE, got {ep['method']}"
    assert ep["handler"] == "borrarMultiple"
    assert ep["path"] == "/v1/rol/borrarMultiple"


def test_request_mapping_post_method(tmp_path):
    """@RequestMapping(method=RequestMethod.POST) must resolve to POST."""
    _write_java(tmp_path, "ItemController.java", """\
        @RestController
        @RequestMapping("/items")
        public class ItemController {

            @RequestMapping(value = "/create", method = RequestMethod.POST)
            public ResponseEntity<?> create() { return null; }
        }
    """)
    result = _extract_java_endpoints(tmp_path)
    ep = result["endpoints"][0]
    assert ep["method"] == "POST"


def test_request_mapping_no_method_defaults_get(tmp_path):
    """@RequestMapping without method= defaults to GET."""
    _write_java(tmp_path, "InfoController.java", """\
        @RestController
        public class InfoController {

            @RequestMapping("/health")
            public String health() { return "ok"; }
        }
    """)
    result = _extract_java_endpoints(tmp_path)
    assert result["endpoints"][0]["method"] == "GET"


def test_request_mapping_method_before_value(tmp_path):
    """@RequestMapping(method=DELETE, value="/path") — method= before value=."""
    _write_java(tmp_path, "OrderController.java", """\
        @RestController
        @RequestMapping("/orders")
        public class OrderController {

            @RequestMapping(method = RequestMethod.DELETE, value = "/cancel")
            public void cancel() { }
        }
    """)
    result = _extract_java_endpoints(tmp_path)
    ep = result["endpoints"][0]
    assert ep["method"] == "DELETE"
    assert ep["path"] == "/orders/cancel"


def test_delete_mapping_annotation(tmp_path):
    """@DeleteMapping must still resolve to DELETE (regression guard)."""
    _write_java(tmp_path, "TaskController.java", """\
        @RestController
        @RequestMapping("/tasks")
        public class TaskController {

            @DeleteMapping("/{id}")
            public void remove() { }
        }
    """)
    result = _extract_java_endpoints(tmp_path)
    assert result["endpoints"][0]["method"] == "DELETE"


# ── combined: paths + methods ─────────────────────────────────────────────────


def test_mixed_controller(tmp_path):
    """Full CRUD controller: correct paths and methods for all verbs."""
    _write_java(tmp_path, "CrudController.java", """\
        @RestController
        @RequestMapping("/v1/items")
        public class CrudController {

            @GetMapping
            public List<?> getAll() { return null; }

            @GetMapping("/{id}")
            public Object getById() { return null; }

            @PostMapping
            public Object create() { return null; }

            @PutMapping("/{id}")
            public Object update() { return null; }

            @DeleteMapping("/{id}")
            public void delete() { }

            @RequestMapping(value = "/bulk", method = RequestMethod.DELETE)
            public void deleteBulk() { }
        }
    """)
    result = _extract_java_endpoints(tmp_path)
    by_handler = {e["handler"]: e for e in result["endpoints"]}

    assert by_handler["getAll"]["method"] == "GET"
    assert by_handler["getAll"]["path"] == "/v1/items"
    assert by_handler["getById"]["path"] == "/v1/items/{id}"
    assert by_handler["create"]["method"] == "POST"
    assert by_handler["update"]["method"] == "PUT"
    assert by_handler["delete"]["method"] == "DELETE"
    assert by_handler["deleteBulk"]["method"] == "DELETE"
    assert by_handler["deleteBulk"]["path"] == "/v1/items/bulk"

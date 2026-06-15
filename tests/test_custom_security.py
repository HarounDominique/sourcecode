"""Tests for custom security annotation support (BUG-3).

Covers sourcecode.config.json loading and recognition of project-defined
authorization annotations (e.g. @M3FiltroSeguridad) by the canonical security
extractor and the endpoint surface.
"""
from __future__ import annotations

import json
from pathlib import Path

from sourcecode.repository_ir import (
    SymbolRecord,
    _route_security_from_sym,
    extract_java_endpoints,
)
from sourcecode.security_config import (
    CustomSecuritySpec,
    capture_markers,
    load_custom_security,
)

_CONFIG = {
    "customSecurityAnnotations": [
        {
            "fullyQualifiedName": "com.example.security.M3FiltroSeguridad",
            "shortName": "M3FiltroSeguridad",
            "resourceParam": "nombreRecurso",
            "levelParam": "nivelRequerido",
        }
    ]
}

_CONTROLLER = '''package com.example;
import org.springframework.web.bind.annotation.*;
@RestController
@RequestMapping("/ausente")
public class AusenteRestController {
    @M3FiltroSeguridad(nombreRecurso = "RRHH_MOVADMINISTRATIVOS", nivelRequerido = "NIVEL_LECTURA")
    @RequestMapping(value = "/porProvision/{id}", method = RequestMethod.GET)
    public String getMovimientoRRHH(@PathVariable String id) { return id; }
}
'''


def _write_repo(root: Path, *, with_config: bool) -> None:
    pkg = root / "src" / "main" / "java" / "com" / "example"
    pkg.mkdir(parents=True)
    (pkg / "AusenteRestController.java").write_text(_CONTROLLER, encoding="utf-8")
    if with_config:
        (root / "sourcecode.config.json").write_text(json.dumps(_CONFIG), encoding="utf-8")


class TestLoadConfig:
    def test_parses_specs(self, tmp_path):
        (tmp_path / "sourcecode.config.json").write_text(json.dumps(_CONFIG), encoding="utf-8")
        specs = load_custom_security(tmp_path)
        assert len(specs) == 1
        s = specs[0]
        assert s.short_name == "M3FiltroSeguridad"
        assert s.resource_param == "nombreRecurso"
        assert s.level_param == "nivelRequerido"
        assert s.marker == "@M3FiltroSeguridad"
        assert capture_markers(specs) == frozenset({"@M3FiltroSeguridad"})

    def test_missing_file_is_empty(self, tmp_path):
        assert load_custom_security(tmp_path) == []

    def test_none_root_is_empty(self):
        assert load_custom_security(None) == []

    def test_malformed_json_is_empty(self, tmp_path):
        (tmp_path / "sourcecode.config.json").write_text("{not json", encoding="utf-8")
        assert load_custom_security(tmp_path) == []

    def test_shortname_derived_from_fqn(self, tmp_path):
        cfg = {"customSecurityAnnotations": [{"fullyQualifiedName": "a.b.MyGuard"}]}
        (tmp_path / "sourcecode.config.json").write_text(json.dumps(cfg), encoding="utf-8")
        specs = load_custom_security(tmp_path)
        assert specs[0].short_name == "MyGuard"


class TestRouteSecurityExtractor:
    def _method(self, ann_args: str) -> SymbolRecord:
        return SymbolRecord(
            symbol="com.example.Ctrl#h",
            type="method",
            annotations=["@M3FiltroSeguridad"],
            annotation_values={"@M3FiltroSeguridad": ann_args},
        )

    def test_detects_string_literal_params(self):
        spec = CustomSecuritySpec("M3FiltroSeguridad", resource_param="nombreRecurso", level_param="nivelRequerido")
        sym = self._method('nombreRecurso = "RRHH", nivelRequerido = "LECTURA"')
        out = _route_security_from_sym(sym, None, (spec,))
        assert out["policy"] == "custom"
        assert out["annotation"] == "M3FiltroSeguridad"
        assert out["resourceName"] == "RRHH"
        assert out["requiredLevel"] == "LECTURA"

    def test_constant_ref_falls_back_to_token(self):
        spec = CustomSecuritySpec("M3FiltroSeguridad", resource_param="nombreRecurso", level_param="nivelRequerido")
        sym = self._method("nombreRecurso = Const.RRHH, nivelRequerido = Svc.LECTURA")
        out = _route_security_from_sym(sym, None, (spec,))
        assert out["resourceName"] == "Const.RRHH"
        assert out["requiredLevel"] == "Svc.LECTURA"

    def test_builtin_annotation_wins_over_custom(self):
        spec = CustomSecuritySpec("M3FiltroSeguridad")
        sym = SymbolRecord(
            symbol="com.example.Ctrl#h",
            type="method",
            annotations=["@PreAuthorize", "@M3FiltroSeguridad"],
            annotation_values={"@PreAuthorize": '"hasRole(ADMIN)"'},
        )
        out = _route_security_from_sym(sym, None, (spec,))
        assert out["policy"] == "spring_preauthorize"

    def test_no_custom_specs_returns_none(self):
        sym = self._method('nombreRecurso = "RRHH"')
        assert _route_security_from_sym(sym, None, ()) is None


class TestEndpointSurface:
    def test_custom_annotation_detected_with_config(self, tmp_path):
        _write_repo(tmp_path, with_config=True)
        out = extract_java_endpoints(tmp_path)
        eps = out["endpoints"]
        assert len(eps) == 1
        sec = eps[0]["security"]
        assert sec["policy"] == "custom"
        assert sec["annotation"] == "M3FiltroSeguridad"
        assert sec["resourceName"] == "RRHH_MOVADMINISTRATIVOS"
        assert sec["requiredLevel"] == "NIVEL_LECTURA"
        assert out["no_security_signal"] == 0

    def test_no_config_stays_none_detected(self, tmp_path):
        _write_repo(tmp_path, with_config=False)
        out = extract_java_endpoints(tmp_path)
        assert out["endpoints"][0]["security"]["policy"] == "none_detected"
        assert out["no_security_signal"] == 1

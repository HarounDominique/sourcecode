"""Fase 21 — repo/service impact must reach HTTP routes recovered from the OpenAPI spec.

Field test (spring-petclinic-rest #11, weakness #2): `impact-chain` on a repository
or service symbol reports `endpoints_affected = 0` in openapi-generator interface-only
repos, even though the `endpoints` command lists the routes.

Root cause (diagnosed 21-01): the OpenAPI spec→controller linking lives only inside
`extract_java_endpoints` (the `endpoints` command path). `build_repo_ir` /
`_build_route_surface` — which feed `route_surface` → CanonicalRepositoryIR →
`EndpointIndex` → `impact-chain` — do NOT perform the linking. So spec-sourced
endpoints never reach the impact model: `EndpointIndex` is empty for interface-defined
controllers and `_collect_endpoints` can never resolve them.

Fase 21-02 closed the gap: the spec→controller linking is now shared
(`_recover_openapi_spec_routes`) and merged into `build_repo_ir`'s route_surface, so
spec-sourced endpoints reach the CIR → EndpointIndex → impact-chain, matching the
`endpoints` command. The tests pin both paths now carry the surface.
"""
from __future__ import annotations

from pathlib import Path

from sourcecode.canonical_ir import build_canonical_ir
from sourcecode.repository_ir import build_repo_ir, extract_java_endpoints
from sourcecode.spring_model import EndpointIndex

# openapi-generator "interface-only" shape: a @RestController that implements a
# generated *Api interface (under target/generated-sources, not scanned) and carries
# no method-level route annotations of its own. The HTTP surface lives in the spec.
_CONTROLLER = '''package com.example.web;
import org.springframework.web.bind.annotation.RestController;
@RestController
public class VetRestController implements VetsApi {
    public Object listVets() { return null; }
}
'''

_OPENAPI_SPEC = '''openapi: 3.0.1
info:
  title: petclinic
  version: "1.0"
paths:
  /api/vets:
    get:
      tags: [vets]
      operationId: listVets
      responses:
        "200":
          description: ok
'''

_CONTROLLER_FQN = "com.example.web.VetRestController"


def _write(root: Path, rel: str, content: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _make_repo(tmp_path: Path) -> list[str]:
    _write(tmp_path, "src/main/java/com/example/web/VetRestController.java", _CONTROLLER)
    _write(tmp_path, "src/main/resources/openapi.yml", _OPENAPI_SPEC)
    return [str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*.java")]


class TestEndpointsPathHasSpecSurface:
    """GREEN guard — the `endpoints` command already recovers the spec route.

    Documents the surface exists in one path; protects against a regression that
    would make the whole feature moot.
    """

    def test_extract_java_endpoints_recovers_spec_route(self, tmp_path):
        _make_repo(tmp_path)
        out = extract_java_endpoints(tmp_path)
        eps = out.get("endpoints") or []
        spec_eps = [e for e in eps if e.get("source") == "openapi-spec"]
        assert any(
            e.get("method") == "GET" and e.get("path") == "/api/vets"
            for e in spec_eps
        ), f"endpoints command must recover the spec route; got {eps}"


class TestImpactPathHasSpecSurface:
    """Fase 21-02: the CIR/impact path now carries the spec surface.

    The OpenAPI spec→controller linking is shared (`_recover_openapi_spec_routes`) and
    merged into `build_repo_ir`'s route_surface, so spec-sourced endpoints reach the
    CIR → EndpointIndex → impact-chain — not only the `endpoints` command.
    """

    def test_route_surface_includes_spec_route(self, tmp_path):
        files = _make_repo(tmp_path)
        ir = build_repo_ir(files, tmp_path)
        rs = ir.get("route_surface") or []
        assert any(
            r.get("method") == "GET" and r.get("path") == "/api/vets"
            for r in rs
        ), f"build_repo_ir route_surface must include the spec route; got {rs}"

    def test_endpoint_index_has_controller(self, tmp_path):
        files = _make_repo(tmp_path)
        cir = build_canonical_ir(files, tmp_path)
        ei = EndpointIndex.build(cir)
        assert _CONTROLLER_FQN in ei.controller_fqns, (
            f"EndpointIndex must index the iface-defined controller; "
            f"got {sorted(ei.controller_fqns)}"
        )
        routes = ei.endpoints_for(_CONTROLLER_FQN)
        assert any(
            getattr(ep, "method", "") == "GET" and getattr(ep, "path", "") == "/api/vets"
            for ep in routes
        ), f"EndpointIndex must expose the spec route for impact-chain; got {routes}"

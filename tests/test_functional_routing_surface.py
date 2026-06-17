"""test_functional_routing_surface.py — F-2 honest WebFlux/RouterFunction signal.

extract_java_endpoints models annotation-based (@RequestMapping/@GetMapping) routes
only. WebFlux functional routing (route().GET("/path", handler)) is NOT modeled — but
must be surfaced as an honest limitation so a zero/partial surface is never read as
"this app exposes no endpoints" (the halo field-benchmark finding).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from sourcecode.repository_ir import extract_java_endpoints

_FUNCTIONAL_ENDPOINT = """\
package run.app.endpoint;

import org.springframework.web.reactive.function.server.RouterFunction;
import org.springframework.web.reactive.function.server.ServerResponse;

@Component
public class ThumbnailEndpoint implements CustomEndpoint {
    @Override
    public RouterFunction<ServerResponse> endpoint() {
        return route()
            .GET("/thumbnails/-/via-uri", this::getThumbnailByUri, builder -> {})
            .POST("/thumbnails", this::create, builder -> {})
            .build();
    }
}
"""

_ANNOTATION_CONTROLLER = """\
package run.app.web;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
public class ProductController {
    @GetMapping("/api/products")
    public String list() { return "ok"; }
}
"""


def _write(root: Path, rel: str, content: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


class TestFunctionalRoutingSignal:
    def test_functional_routes_surfaced_when_annotation_surface_empty(self, tmp_path):
        _write(tmp_path, "src/main/java/run/app/endpoint/ThumbnailEndpoint.java",
               _FUNCTIONAL_ENDPOINT)
        result = extract_java_endpoints(tmp_path)
        assert result["total"] == 0, "no annotation endpoints expected"
        fr = result.get("functional_routing")
        assert fr is not None, "functional_routing block must be present"
        assert fr["modeled"] is False
        assert fr["files"] == 1
        assert fr["route_registrations"] == 2, f"expected GET+POST, got {fr}"
        warns = " ".join(result.get("warnings", []))
        assert "functional route" in warns.lower()
        # Empty-surface case must explicitly warn against misreading.
        assert "do not read it as" in warns.lower() or "do not read it" in warns.lower()

    def test_no_false_trigger_on_pure_annotation_repo(self, tmp_path):
        _write(tmp_path, "src/main/java/run/app/web/ProductController.java",
               _ANNOTATION_CONTROLLER)
        result = extract_java_endpoints(tmp_path)
        assert result["total"] == 1
        assert "functional_routing" not in result, (
            "annotation-only repo must not trigger the functional-routing signal"
        )
        assert not any("functional route" in w.lower() for w in result.get("warnings", []))

    def test_mixed_repo_keeps_annotation_endpoints_and_warns(self, tmp_path):
        _write(tmp_path, "src/main/java/run/app/web/ProductController.java",
               _ANNOTATION_CONTROLLER)
        _write(tmp_path, "src/main/java/run/app/endpoint/ThumbnailEndpoint.java",
               _FUNCTIONAL_ENDPOINT)
        result = extract_java_endpoints(tmp_path)
        assert result["total"] == 1, "annotation endpoint still modeled"
        assert result.get("functional_routing", {}).get("route_registrations") == 2
        warns = " ".join(result.get("warnings", []))
        assert "functional route" in warns.lower()
        # Non-empty surface: must NOT include the empty-surface misread clause.
        assert "do not read it" not in warns.lower()

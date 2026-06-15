"""Regression tests for two endpoint/impact accuracy bugs found in field eval
(spring-petclinic-rest, 2026-06-15):

  - Interface `extends` with multiple supertypes was not comma-split, so an
    interface like `SpringDataVetRepository extends VetRepository, Repository<...>`
    produced one mangled edge and was missing from the target's reverse graph
    (impact `direct_callers` omitted it).
  - Controllers whose HTTP mappings live on an implemented (generated) interface
    contributed no routes, leaving the endpoint surface silently empty.
"""
from __future__ import annotations

from pathlib import Path

from sourcecode.repository_ir import (
    _split_supertype_list,
    build_repo_ir,
    extract_java_endpoints,
)


class TestSplitSupertypeList:
    def test_single(self):
        assert _split_supertype_list("VetRepository") == ["VetRepository"]

    def test_multiple_with_generics(self):
        # commas inside <...> must not corrupt the split
        assert _split_supertype_list("VetRepository, Repository<Vet, Integer>") == [
            "VetRepository",
            "Repository",
        ]

    def test_nested_generics(self):
        assert _split_supertype_list("A<Map<String, Integer>>, B") == ["A", "B"]

    def test_empty(self):
        assert _split_supertype_list("") == []
        assert _split_supertype_list("   ") == []


_REPO_IFACE = '''package com.example.repo;
import org.springframework.data.repository.Repository;
public interface VetRepository {
}
'''

_SPRING_DATA = '''package com.example.repo.springdata;
import org.springframework.context.annotation.Profile;
import org.springframework.data.repository.Repository;
import com.example.repo.VetRepository;
@Profile("spring-data-jpa")
public interface SpringDataVetRepository extends VetRepository, Repository<Vet, Integer> {
}
'''


def _write(root: Path, rel: str, content: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


class TestExtendsReverseGraph:
    def test_interface_extends_appears_in_reverse_graph(self, tmp_path):
        base = "src/main/java/com/example/repo"
        _write(tmp_path, f"{base}/VetRepository.java", _REPO_IFACE)
        _write(tmp_path, f"{base}/springdata/SpringDataVetRepository.java", _SPRING_DATA)
        files = [str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*.java")]
        ir = build_repo_ir(files, tmp_path)
        rg = ir.get("reverse_graph") or {}
        target = "com.example.repo.VetRepository"
        assert target in rg, f"{target} missing from reverse_graph: {list(rg)}"
        callers = [c for by_type in rg[target].values() for c in by_type]
        assert "com.example.repo.springdata.SpringDataVetRepository" in callers


_CONTROLLER_IFACE_ONLY = '''package com.example.web;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.bind.annotation.RequestMapping;
@RestController
@RequestMapping("/api")
public class VetRestControllerV2 implements VetV2Api {
    public Object listVetsPage(Integer page, Integer size) { return null; }
}
'''


class TestInterfaceDefinedControllerWarning:
    def test_warning_emitted(self, tmp_path):
        _write(tmp_path, "src/main/java/com/example/web/VetRestControllerV2.java",
               _CONTROLLER_IFACE_ONLY)
        out = extract_java_endpoints(tmp_path)
        assert out.get("warnings"), "expected warnings for interface-defined controller"
        assert "com.example.web.VetRestControllerV2" in out.get(
            "interface_defined_controllers", [])
        assert any("VetV2Api" in w for w in out["warnings"])

    def test_no_warning_for_plain_controller(self, tmp_path):
        plain = '''package com.example.web;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.bind.annotation.GetMapping;
@RestController
public class PlainController {
    @GetMapping("/ping")
    public String ping() { return "ok"; }
}
'''
        _write(tmp_path, "src/main/java/com/example/web/PlainController.java", plain)
        out = extract_java_endpoints(tmp_path)
        assert "warnings" not in out

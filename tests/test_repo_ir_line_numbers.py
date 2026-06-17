"""Tests Fase 15 — line numbers en nodos de repo-ir (C4DIR-01, C4DIR-02).

Los .java se escriben en tmp_path (rutas bajo tests/ se excluyen por find_java_files).
"""

from sourcecode.repository_ir import build_repo_ir, find_java_files

# linea:  1 package / 2 import / 3 @RestController / 4 class / 5 @GetMapping / 6 method get / 7 }
_CONTROLLER = """\
package com.x;
import org.springframework.web.bind.annotation.*;
@RestController
public class AController {
    @GetMapping("/a")
    public Foo get() { return null; }
}
"""

# metodo handle declarado en linea 8 (tras anotacion multilinea 5-7)
_MULTILINE = """\
package com.x;
import org.springframework.web.bind.annotation.*;
@RestController
public class BController {
    @RequestMapping(
        value = "/b",
        method = RequestMethod.GET)
    public Bar handle() { return null; }
}
"""


def _build(tmp_path, fname, content):
    pkg = tmp_path / "src" / "main" / "java" / "com" / "x"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / fname).write_text(content, encoding="utf-8")
    root = tmp_path
    files = [f for f in find_java_files(root) if "/test/" not in f and "/tests/" not in f]
    return build_repo_ir(files, root)["graph"]["nodes"]


def _node(nodes, kind_suffix):
    for n in nodes:
        if n.get("fqn", "").endswith(kind_suffix):
            return n
    return None


def test_graph_nodes_have_line(tmp_path):
    nodes = _build(tmp_path, "AController.java", _CONTROLLER)
    assert nodes, "expected graph nodes"
    cls = _node(nodes, "AController")
    assert cls is not None
    assert cls.get("line") == 4, f"class line: {cls.get('line')}"

    method = _node(nodes, "AController#get")
    assert method is not None
    assert method.get("line") == 6, f"method line: {method.get('line')}"

    for n in nodes:
        if n.get("symbol_kind") in ("class", "interface", "method", "field", "constructor"):
            assert isinstance(n.get("line"), int), f"missing line: {n}"


def test_source_file_is_relative_path(tmp_path):
    nodes = _build(tmp_path, "AController.java", _CONTROLLER)
    cls = _node(nodes, "AController")
    assert cls is not None
    sf = cls.get("source_file")
    assert sf == "src/main/java/com/x/AController.java", sf
    assert "/" in sf and sf.endswith(".java")


def test_line_survives_multiline_annotation(tmp_path):
    nodes = _build(tmp_path, "BController.java", _MULTILINE)
    method = _node(nodes, "BController#handle")
    assert method is not None
    assert method.get("line") == 8, f"handle line: {method.get('line')}"

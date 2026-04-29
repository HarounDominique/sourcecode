"""Tests for Phase 1 improvements: monorepo support, workspace graphs, architecture patterns, analytics."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sourcecode.architecture_analyzer import ArchitectureAnalyzer
from sourcecode.detectors.base import DetectionContext
from sourcecode.detectors.go import GoDetector
from sourcecode.detectors.java import JavaDetector
from sourcecode.detectors.nodejs import NodejsDetector
from sourcecode.detectors.python import PythonDetector
from sourcecode.detectors.rust import RustDetector
from sourcecode.graph_analyzer import GraphAnalyzer
from sourcecode.schema import SourceMap


# ── Framework map expansion ──────────────────────────────────────────────────


def test_python_celery_detected(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("celery==5.3.0\nredis==5.0.0\n")
    ctx = DetectionContext(root=tmp_path, file_tree={"requirements.txt": None}, manifests=["requirements.txt"])
    stacks, _ = PythonDetector().detect(ctx)
    assert any(f.name == "Celery" for f in stacks[0].frameworks)


def test_python_click_detected(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("click==8.1.0\n")
    ctx = DetectionContext(root=tmp_path, file_tree={"requirements.txt": None}, manifests=["requirements.txt"])
    stacks, _ = PythonDetector().detect(ctx)
    assert any(f.name == "Click" for f in stacks[0].frameworks)


def test_node_fastify_detected(tmp_path: Path) -> None:
    pkg = {"name": "api", "dependencies": {"fastify": "^4.0.0"}}
    (tmp_path / "package.json").write_text(json.dumps(pkg))
    ctx = DetectionContext(root=tmp_path, file_tree={"package.json": None}, manifests=["package.json"])
    stacks, _ = NodejsDetector().detect(ctx)
    assert any(f.name == "Fastify" for f in stacks[0].frameworks)


def test_node_framework_deduplication(tmp_path: Path) -> None:
    pkg = {"name": "app", "dependencies": {"@remix-run/node": "2.0.0", "@remix-run/react": "2.0.0"}}
    (tmp_path / "package.json").write_text(json.dumps(pkg))
    ctx = DetectionContext(root=tmp_path, file_tree={"package.json": None}, manifests=["package.json"])
    stacks, _ = NodejsDetector().detect(ctx)
    remix_count = sum(1 for f in stacks[0].frameworks if f.name == "Remix")
    assert remix_count == 1  # deduplicated


def test_rust_tokio_detected(tmp_path: Path) -> None:
    cargo_content = '[package]\nname = "app"\n\n[dependencies]\ntokio = { version = "1", features = ["full"] }\n'
    (tmp_path / "Cargo.toml").write_text(cargo_content)
    ctx = DetectionContext(root=tmp_path, file_tree={"Cargo.toml": None}, manifests=["Cargo.toml"])
    stacks, _ = RustDetector().detect(ctx)
    assert any(f.name == "Tokio" for f in stacks[0].frameworks)


def test_go_chi_detected(tmp_path: Path) -> None:
    go_mod = 'module example.com/app\n\ngo 1.21\n\nrequire github.com/go-chi/chi v5.0.0\n'
    (tmp_path / "go.mod").write_text(go_mod)
    ctx = DetectionContext(root=tmp_path, file_tree={"go.mod": None}, manifests=["go.mod"])
    stacks, _ = GoDetector().detect(ctx)
    assert any(f.name == "chi" for f in stacks[0].frameworks)


def test_java_micronaut_detected(tmp_path: Path) -> None:
    pom = """<project><dependencies>
      <dependency><groupId>io.micronaut</groupId><artifactId>micronaut-core</artifactId></dependency>
    </dependencies></project>"""
    (tmp_path / "pom.xml").write_text(pom)
    ctx = DetectionContext(root=tmp_path, file_tree={"pom.xml": None}, manifests=["pom.xml"])
    stacks, _ = JavaDetector().detect(ctx)
    assert any(f.name == "Micronaut" for f in stacks[0].frameworks)


def test_java_vertx_detected(tmp_path: Path) -> None:
    pom = """<project><dependencies>
      <dependency><groupId>io.vertx</groupId><artifactId>vertx-core</artifactId></dependency>
    </dependencies></project>"""
    (tmp_path / "pom.xml").write_text(pom)
    ctx = DetectionContext(root=tmp_path, file_tree={"pom.xml": None}, manifests=["pom.xml"])
    stacks, _ = JavaDetector().detect(ctx)
    assert any(f.name == "Vert.x" for f in stacks[0].frameworks)


# ── Node monorepo signals ────────────────────────────────────────────────────


def test_node_turbo_signal(tmp_path: Path) -> None:
    pkg = {"name": "root", "private": True}
    (tmp_path / "package.json").write_text(json.dumps(pkg))
    (tmp_path / "turbo.json").write_text('{"pipeline": {}}')
    ctx = DetectionContext(root=tmp_path, file_tree={"package.json": None, "turbo.json": None}, manifests=["package.json"])
    stacks, _ = NodejsDetector().detect(ctx)
    assert "monorepo:turbo" in stacks[0].signals


def test_node_pnpm_workspace_signal(tmp_path: Path) -> None:
    pkg = {"name": "root", "private": True}
    (tmp_path / "package.json").write_text(json.dumps(pkg))
    (tmp_path / "pnpm-workspace.yaml").write_text("packages:\n  - 'apps/*'\n")
    ctx = DetectionContext(root=tmp_path, file_tree={"package.json": None, "pnpm-workspace.yaml": None}, manifests=["package.json"])
    stacks, _ = NodejsDetector().detect(ctx)
    assert "monorepo:pnpm" in stacks[0].signals


def test_node_npm_workspaces_signal(tmp_path: Path) -> None:
    pkg = {"name": "root", "workspaces": ["apps/*", "packages/*"]}
    (tmp_path / "package.json").write_text(json.dumps(pkg))
    ctx = DetectionContext(root=tmp_path, file_tree={"package.json": None}, manifests=["package.json"])
    stacks, _ = NodejsDetector().detect(ctx)
    assert "monorepo:npm-workspaces" in stacks[0].signals


def test_node_no_monorepo_no_signal(tmp_path: Path) -> None:
    pkg = {"name": "simple-app", "dependencies": {"express": "^4.0.0"}}
    (tmp_path / "package.json").write_text(json.dumps(pkg))
    ctx = DetectionContext(root=tmp_path, file_tree={"package.json": None}, manifests=["package.json"])
    stacks, _ = NodejsDetector().detect(ctx)
    assert not any("monorepo" in s for s in stacks[0].signals)


# ── Node workspace graph ─────────────────────────────────────────────────────


def _write_pkg(path: Path, name: str, deps: dict[str, str] | None = None) -> None:
    path.mkdir(parents=True, exist_ok=True)
    data: dict = {"name": name}
    if deps:
        data["dependencies"] = deps
    (path / "package.json").write_text(json.dumps(data))


def test_node_workspace_graph_nodes(tmp_path: Path) -> None:
    root_pkg = {"name": "root", "workspaces": ["apps/*", "packages/*"]}
    (tmp_path / "package.json").write_text(json.dumps(root_pkg))
    (tmp_path / "turbo.json").write_text("{}")
    _write_pkg(tmp_path / "apps" / "web", "@myapp/web")
    _write_pkg(tmp_path / "packages" / "ui", "@myapp/ui")

    file_tree = {
        "package.json": None,
        "turbo.json": None,
        "apps": {"web": {"package.json": None}},
        "packages": {"ui": {"package.json": None}},
    }
    analyzer = GraphAnalyzer()
    graph = analyzer.analyze(root=tmp_path, file_tree=file_tree)
    node_ids = {n.id for n in graph.nodes}
    assert "module:apps/web" in node_ids
    assert "module:packages/ui" in node_ids


def test_node_workspace_graph_edges(tmp_path: Path) -> None:
    root_pkg = {"name": "root", "workspaces": ["apps/*", "packages/*"]}
    (tmp_path / "package.json").write_text(json.dumps(root_pkg))
    _write_pkg(tmp_path / "apps" / "web", "@myapp/web", {"@myapp/ui": "workspace:*"})
    _write_pkg(tmp_path / "packages" / "ui", "@myapp/ui")

    file_tree = {
        "package.json": None,
        "apps": {"web": {"package.json": None}},
        "packages": {"ui": {"package.json": None}},
    }
    analyzer = GraphAnalyzer()
    graph = analyzer.analyze(root=tmp_path, file_tree=file_tree)
    edges = [(e.source, e.target, e.kind) for e in graph.edges]
    assert ("module:apps/web", "module:packages/ui", "imports") in edges


# ── Rust workspace ───────────────────────────────────────────────────────────


def test_rust_workspace_signal(tmp_path: Path) -> None:
    cargo = "[workspace]\nmembers = [\"crates/core\", \"crates/cli\"]\n"
    (tmp_path / "Cargo.toml").write_text(cargo)
    ctx = DetectionContext(root=tmp_path, file_tree={"Cargo.toml": None}, manifests=["Cargo.toml"])
    stacks, _ = RustDetector().detect(ctx)
    signals_joined = " ".join(stacks[0].signals)
    assert "workspace:2 crates" in signals_joined


def test_rust_workspace_graph_nodes(tmp_path: Path) -> None:
    root_cargo = "[workspace]\nmembers = [\"crates/core\", \"crates/cli\"]\n"
    (tmp_path / "Cargo.toml").write_text(root_cargo)
    core_dir = tmp_path / "crates" / "core"
    core_dir.mkdir(parents=True)
    (core_dir / "Cargo.toml").write_text('[package]\nname = "core"\nversion = "0.1.0"\n')
    (core_dir / "src").mkdir()
    (core_dir / "src" / "lib.rs").write_text("// lib")
    cli_dir = tmp_path / "crates" / "cli"
    cli_dir.mkdir(parents=True)
    (cli_dir / "Cargo.toml").write_text('[package]\nname = "cli"\nversion = "0.1.0"\n[dependencies]\ncore = { path = "../core" }\n')
    (cli_dir / "src").mkdir()
    (cli_dir / "src" / "main.rs").write_text("fn main() {}")

    file_tree = {"Cargo.toml": None, "crates": {"core": {"Cargo.toml": None}, "cli": {"Cargo.toml": None}}}
    analyzer = GraphAnalyzer()
    graph = analyzer.analyze(root=tmp_path, file_tree=file_tree)
    node_ids = {n.id for n in graph.nodes}
    assert "module:crates/core" in node_ids
    assert "module:crates/cli" in node_ids


def test_rust_workspace_crate_kind(tmp_path: Path) -> None:
    root_cargo = "[workspace]\nmembers = [\"crates/core\", \"crates/cli\"]\n"
    (tmp_path / "Cargo.toml").write_text(root_cargo)
    for name, is_bin in [("core", False), ("cli", True)]:
        d = tmp_path / "crates" / name
        d.mkdir(parents=True)
        (d / "Cargo.toml").write_text(f'[package]\nname = "{name}"\nversion = "0.1.0"\n')
        (d / "src").mkdir()
        entry = "main.rs" if is_bin else "lib.rs"
        (d / "src" / entry).write_text("// entry")

    file_tree = {"Cargo.toml": None, "crates": {"core": {"Cargo.toml": None}, "cli": {"Cargo.toml": None}}}
    analyzer = GraphAnalyzer()
    graph = analyzer.analyze(root=tmp_path, file_tree=file_tree)
    node_map = {n.id: n for n in graph.nodes}
    assert "bin" in (node_map.get("module:crates/cli") or type("", (), {"display_name": ""})()).display_name
    assert "lib" in (node_map.get("module:crates/core") or type("", (), {"display_name": ""})()).display_name


# ── Go workspace ─────────────────────────────────────────────────────────────


def test_go_workspace_signal(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module example.com/root\n\ngo 1.21\n")
    (tmp_path / "go.work").write_text("go 1.21\n\nuse ./api\nuse ./domain\n")
    ctx = DetectionContext(root=tmp_path, file_tree={"go.mod": None, "go.work": None}, manifests=["go.mod"])
    stacks, _ = GoDetector().detect(ctx)
    assert "workspace:go.work" in stacks[0].signals


def test_go_workspace_graph_nodes(tmp_path: Path) -> None:
    (tmp_path / "go.work").write_text("go 1.21\n\nuse ./api\nuse ./domain\n")
    api_dir = tmp_path / "api"
    api_dir.mkdir()
    (api_dir / "go.mod").write_text("module example.com/api\n\ngo 1.21\n")
    domain_dir = tmp_path / "domain"
    domain_dir.mkdir()
    (domain_dir / "go.mod").write_text("module example.com/domain\n\ngo 1.21\n")

    file_tree = {"go.work": None, "api": {"go.mod": None}, "domain": {"go.mod": None}}
    analyzer = GraphAnalyzer()
    graph = analyzer.analyze(root=tmp_path, file_tree=file_tree)
    node_ids = {n.id for n in graph.nodes}
    assert "module:api" in node_ids
    assert "module:domain" in node_ids


def test_go_multi_binary_signal(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module example.com/app\n\ngo 1.21\n")
    for svc in ("api", "worker", "migrator"):
        d = tmp_path / "cmd" / svc
        d.mkdir(parents=True)
        (d / "main.go").write_text("package main\n\nfunc main() {}")
    file_tree = {"go.mod": None, "cmd": {s: {"main.go": None} for s in ("api", "worker", "migrator")}}
    ctx = DetectionContext(root=tmp_path, file_tree=file_tree, manifests=["go.mod"])
    stacks, _ = GoDetector().detect(ctx)
    assert "multi-binary:3" in stacks[0].signals


# ── Architecture patterns ────────────────────────────────────────────────────


def _make_sm(file_paths: list[str]) -> SourceMap:
    sm = SourceMap()
    sm.file_paths = file_paths
    return sm


def test_clean_architecture_detected() -> None:
    paths = [
        "src/domain/user.py",
        "src/domain/order.py",
        "src/application/create_order.py",
        "src/application/get_user.py",
        "src/infrastructure/db.py",
        "src/infrastructure/email.py",
    ]
    result = ArchitectureAnalyzer().analyze(root=Path("."), sm=_make_sm(paths))
    assert result.pattern == "clean"
    assert result.confidence in ("high", "medium")


def test_cqrs_detected() -> None:
    paths = [
        "src/commands/create_user.py",
        "src/commands/delete_user.py",
        "src/queries/get_user.py",
        "src/queries/list_users.py",
    ]
    result = ArchitectureAnalyzer().analyze(root=Path("."), sm=_make_sm(paths))
    assert result.pattern == "cqrs"


def test_layered_not_confused_with_clean() -> None:
    """Classic MVC/layered should not be classified as clean."""
    paths = [
        "src/controllers/user_controller.py",
        "src/controllers/order_controller.py",
        "src/services/user_service.py",
        "src/repositories/user_repo.py",
        "src/repositories/order_repo.py",
    ]
    result = ArchitectureAnalyzer().analyze(root=Path("."), sm=_make_sm(paths))
    assert result.pattern == "layered"


def test_microservices_via_services_dir() -> None:
    paths = [
        "services/auth/main.py",
        "services/auth/router.py",
        "services/orders/main.py",
        "services/orders/handler.py",
        "services/payments/main.py",
        "services/payments/processor.py",
    ]
    result = ArchitectureAnalyzer().analyze(root=Path("."), sm=_make_sm(paths))
    assert result.pattern == "microservices"


def test_microservices_via_multiple_entrypoints() -> None:
    paths = [
        "auth-service/main.go",
        "auth-service/handler.go",
        "order-service/main.go",
        "order-service/handler.go",
        "payment-service/main.go",
        "payment-service/handler.go",
        "notification-service/main.go",
        "notification-service/handler.go",
    ]
    result = ArchitectureAnalyzer().analyze(root=Path("."), sm=_make_sm(paths))
    assert result.pattern == "microservices"


def test_monorepo_pattern_detected() -> None:
    paths = [
        "apps/web/index.ts",
        "apps/web/router.ts",
        "apps/api/index.ts",
        "packages/ui/button.tsx",
        "packages/utils/format.ts",
    ]
    result = ArchitectureAnalyzer().analyze(root=Path("."), sm=_make_sm(paths))
    assert result.pattern == "monorepo"


# ── Graph analytics ──────────────────────────────────────────────────────────


def test_graph_hubs_detected(tmp_path: Path) -> None:
    # Create Python modules where one module is imported by many others
    src = tmp_path / "src"
    src.mkdir()
    (src / "shared.py").write_text("SHARED = True")
    for i in range(4):
        (src / f"module{i}.py").write_text("from src.shared import SHARED")

    file_tree = {"src": {
        "shared.py": None,
        "module0.py": None,
        "module1.py": None,
        "module2.py": None,
        "module3.py": None,
    }}
    analyzer = GraphAnalyzer()
    graph = analyzer.analyze(root=tmp_path, file_tree=file_tree)
    assert any("shared" in hub for hub in graph.summary.hubs)


def test_graph_orphans_detected(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "connected_a.py").write_text("from src.connected_b import X")
    (src / "connected_b.py").write_text("X = 1")
    (src / "orphan.py").write_text("ALONE = True")

    file_tree = {"src": {"connected_a.py": None, "connected_b.py": None, "orphan.py": None}}
    analyzer = GraphAnalyzer()
    graph = analyzer.analyze(root=tmp_path, file_tree=file_tree)
    assert any("orphan" in o for o in graph.summary.orphans)


def test_graph_cycle_count(tmp_path: Path) -> None:
    # A → B → A (cycle), C → D (no cycle)
    (tmp_path / "a.py").write_text("from b import B")
    (tmp_path / "b.py").write_text("from a import A")
    (tmp_path / "c.py").write_text("from d import D")
    (tmp_path / "d.py").write_text("D = 1")

    file_tree = {"a.py": None, "b.py": None, "c.py": None, "d.py": None}
    analyzer = GraphAnalyzer()
    graph = analyzer.analyze(root=tmp_path, file_tree=file_tree)
    assert graph.summary.cycle_count >= 1


def test_graph_no_cycles_clean(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("from b import B")
    (tmp_path / "b.py").write_text("from c import C")
    (tmp_path / "c.py").write_text("C = 1")

    file_tree = {"a.py": None, "b.py": None, "c.py": None}
    analyzer = GraphAnalyzer()
    graph = analyzer.analyze(root=tmp_path, file_tree=file_tree)
    assert graph.summary.cycle_count == 0

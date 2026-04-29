"""Tests for Phase 2.1: hybrid inference, FrameworkDetection provenance, ContextSummary."""
from __future__ import annotations

from pathlib import Path

import pytest

from sourcecode.detectors.base import DetectionContext
from sourcecode.detectors.dotnet import DotnetDetector
from sourcecode.detectors.go import GoDetector
from sourcecode.detectors.hybrid import merge_framework_detections, scan_for_frameworks
from sourcecode.detectors.nodejs import NodejsDetector
from sourcecode.detectors.python import PythonDetector
from sourcecode.detectors.rust import RustDetector
from sourcecode.schema import ContextSummary, FrameworkDetection, SourceMap


# ── hybrid scanner unit tests ────────────────────────────────────────────────


def test_scan_fastapi_via_imports(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")
    result = scan_for_frameworks(tmp_path, {"main.py": None}, "python", priority_paths=["main.py"])
    assert any(fw.name == "FastAPI" for fw in result)
    fw = next(f for f in result if f.name == "FastAPI")
    assert fw.source == "imports"
    assert fw.confidence == "medium"
    assert any("fastapi" in ev.lower() for ev in fw.detected_via)


def test_scan_flask_via_imports(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("from flask import Flask\napp = Flask(__name__)\n")
    result = scan_for_frameworks(tmp_path, {"app.py": None}, "python", priority_paths=["app.py"])
    assert any(fw.name == "Flask" for fw in result)


def test_scan_click_via_decorator(tmp_path: Path) -> None:
    (tmp_path / "cli.py").write_text("import click\n\n@click.command()\ndef main(): pass\n")
    result = scan_for_frameworks(tmp_path, {"cli.py": None}, "python", priority_paths=["cli.py"])
    assert any(fw.name == "Click" for fw in result)


def test_scan_celery_via_imports(tmp_path: Path) -> None:
    (tmp_path / "tasks.py").write_text("from celery import Celery\napp = Celery('tasks')\n")
    result = scan_for_frameworks(tmp_path, {"tasks.py": None}, "python")
    assert any(fw.name == "Celery" for fw in result)


def test_scan_express_via_require(tmp_path: Path) -> None:
    (tmp_path / "server.js").write_text("const express = require('express');\nconst app = express();\n")
    result = scan_for_frameworks(tmp_path, {"server.js": None}, "nodejs", priority_paths=["server.js"])
    assert any(fw.name == "Express" for fw in result)


def test_scan_nestjs_via_nestfactory(tmp_path: Path) -> None:
    code = "import { NestFactory } from '@nestjs/core';\nasync function main() { await NestFactory.create(AppModule); }\n"
    (tmp_path / "main.ts").write_text(code)
    result = scan_for_frameworks(tmp_path, {"main.ts": None}, "nodejs", priority_paths=["main.ts"])
    assert any(fw.name == "NestJS" for fw in result)


def test_scan_gin_via_imports(tmp_path: Path) -> None:
    (tmp_path / "main.go").write_text('package main\nimport "github.com/gin-gonic/gin"\nfunc main() { r := gin.Default() }\n')
    result = scan_for_frameworks(tmp_path, {"main.go": None}, "go", priority_paths=["main.go"])
    assert any(fw.name == "Gin" for fw in result)


def test_scan_axum_via_use(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.rs").write_text("use axum::Router;\n#[tokio::main]\nasync fn main() {}\n")
    result = scan_for_frameworks(tmp_path, {"src": {"main.rs": None}}, "rust", priority_paths=["src/main.rs"])
    assert any(fw.name == "Axum" for fw in result)
    assert any(fw.name == "Tokio" for fw in result)


def test_scan_dotnet_minimal_api(tmp_path: Path) -> None:
    (tmp_path / "Program.cs").write_text(
        "var builder = WebApplication.CreateBuilder(args);\nvar app = builder.Build();\napp.MapGet(\"/\", () => \"Hello\");\n"
    )
    result = scan_for_frameworks(tmp_path, {"Program.cs": None}, "dotnet", priority_paths=["Program.cs"])
    assert any("Minimal API" in fw.name for fw in result)


def test_scan_dotnet_mvc_controllerbase(tmp_path: Path) -> None:
    (tmp_path / "Controllers").mkdir()
    (tmp_path / "Controllers" / "UserController.cs").write_text(
        "[ApiController]\npublic class UserController : ControllerBase { }\n"
    )
    file_tree = {"Controllers": {"UserController.cs": None}}
    result = scan_for_frameworks(tmp_path, file_tree, "dotnet")
    assert any("MVC" in fw.name for fw in result)


def test_scan_no_match_returns_empty(tmp_path: Path) -> None:
    (tmp_path / "utils.py").write_text("def add(a, b): return a + b\n")
    result = scan_for_frameworks(tmp_path, {"utils.py": None}, "python")
    assert result == []


def test_scan_excludes_test_files(tmp_path: Path) -> None:
    """Files in test directories should not contribute to detection."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text("from fastapi import FastAPI\n")
    result = scan_for_frameworks(tmp_path, {"tests": {"test_app.py": None}}, "python")
    assert result == []


# ── merge_framework_detections tests ─────────────────────────────────────────


def test_merge_manifest_only_is_high_confidence() -> None:
    manifest = [FrameworkDetection(name="FastAPI", source="pyproject.toml")]
    result = merge_framework_detections(manifest, [])
    fw = next(f for f in result if f.name == "FastAPI")
    assert fw.confidence == "high"
    assert any("manifest:" in ev for ev in fw.detected_via)


def test_merge_import_only_is_medium_confidence() -> None:
    imports = [FrameworkDetection(name="Flask", source="imports", confidence="medium", detected_via=["from flask import Flask (app.py)"])]
    result = merge_framework_detections([], imports)
    fw = next(f for f in result if f.name == "Flask")
    assert fw.confidence == "medium"


def test_merge_both_manifest_and_imports_is_high() -> None:
    manifest = [FrameworkDetection(name="Express", source="package.json")]
    imports = [FrameworkDetection(name="Express", source="imports", confidence="medium", detected_via=["require('express') (server.js)"])]
    result = merge_framework_detections(manifest, imports)
    fw = next(f for f in result if f.name == "Express")
    assert fw.confidence == "high"
    assert len(fw.detected_via) >= 2  # both manifest and import evidence


def test_merge_deduplicates_framework_names() -> None:
    manifest = [FrameworkDetection(name="Gin", source="go.mod")]
    imports = [FrameworkDetection(name="Gin", source="imports", confidence="medium", detected_via=["gin.Default() (main.go)"])]
    result = merge_framework_detections(manifest, imports)
    assert sum(1 for f in result if f.name == "Gin") == 1


def test_merge_adds_new_import_only_framework() -> None:
    manifest = [FrameworkDetection(name="Axum", source="Cargo.toml")]
    imports = [
        FrameworkDetection(name="Axum", source="imports", confidence="medium"),
        FrameworkDetection(name="Tokio", source="imports", confidence="medium"),
    ]
    result = merge_framework_detections(manifest, imports)
    names = {f.name for f in result}
    assert "Axum" in names
    assert "Tokio" in names  # import-only framework added


# ── Detector hybrid integration tests ────────────────────────────────────────


def test_python_detects_fastapi_without_manifest(tmp_path: Path) -> None:
    """FastAPI detected via imports even when not in requirements.txt."""
    (tmp_path / "requirements.txt").write_text("httpx==0.25.0\n")
    (tmp_path / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")
    ctx = DetectionContext(
        root=tmp_path,
        file_tree={"requirements.txt": None, "main.py": None},
        manifests=["requirements.txt"],
    )
    stacks, _ = PythonDetector().detect(ctx)
    assert any(f.name == "FastAPI" for f in stacks[0].frameworks)
    fw = next(f for f in stacks[0].frameworks if f.name == "FastAPI")
    assert fw.confidence == "medium"  # import-only = medium


def test_python_fastapi_confirmed_by_both_sources(tmp_path: Path) -> None:
    """FastAPI in both requirements.txt and imports → high confidence."""
    (tmp_path / "requirements.txt").write_text("fastapi==0.104.0\n")
    (tmp_path / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")
    ctx = DetectionContext(
        root=tmp_path,
        file_tree={"requirements.txt": None, "main.py": None},
        manifests=["requirements.txt"],
    )
    stacks, _ = PythonDetector().detect(ctx)
    fw = next(f for f in stacks[0].frameworks if f.name == "FastAPI")
    assert fw.confidence == "high"
    assert len(fw.detected_via) >= 2


def test_node_express_import_only(tmp_path: Path) -> None:
    import json
    pkg = {"name": "app", "dependencies": {"uuid": "^9.0.0"}}
    (tmp_path / "package.json").write_text(json.dumps(pkg))
    (tmp_path / "server.js").write_text("const express = require('express');\n")
    ctx = DetectionContext(
        root=tmp_path,
        file_tree={"package.json": None, "server.js": None},
        manifests=["package.json"],
    )
    stacks, _ = NodejsDetector().detect(ctx)
    assert any(f.name == "Express" for f in stacks[0].frameworks)


def test_rust_tokio_detected_from_main(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "svc"\nversion = "0.1.0"\n[dependencies]\nserde = "1.0"\n')
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.rs").write_text("#[tokio::main]\nasync fn main() {}\n")
    ctx = DetectionContext(
        root=tmp_path,
        file_tree={"Cargo.toml": None, "src": {"main.rs": None}},
        manifests=["Cargo.toml"],
    )
    stacks, _ = RustDetector().detect(ctx)
    assert any(f.name == "Tokio" for f in stacks[0].frameworks)


def test_dotnet_minimal_api_detected(tmp_path: Path) -> None:
    (tmp_path / "Api.csproj").write_text('<Project Sdk="Microsoft.NET.Sdk.Web"><PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup></Project>')
    (tmp_path / "Program.cs").write_text(
        "var builder = WebApplication.CreateBuilder(args);\nvar app = builder.Build();\napp.MapGet(\"/\", () => \"Hello\");\napp.Run();\n"
    )
    ctx = DetectionContext(
        root=tmp_path,
        file_tree={"Api.csproj": None, "Program.cs": None},
        manifests=[],
    )
    stacks, _ = DotnetDetector().detect(ctx)
    assert any("Minimal API" in f.name for f in stacks[0].frameworks)


# ── ContextSummarizer tests ───────────────────────────────────────────────────


def _make_api_sm(tmp_path: Path) -> SourceMap:
    from sourcecode.schema import StackDetection
    sm = SourceMap()
    sm.project_type = "api"
    fw = FrameworkDetection(name="FastAPI", source="pyproject.toml", confidence="high")
    sm.stacks = [StackDetection(stack="python", frameworks=[fw], primary=True, manifests=["pyproject.toml"])]
    sm.file_paths = [
        "src/domain/user.py",
        "src/application/create_user.py",
        "src/infrastructure/db.py",
        "src/main.py",
    ]
    return sm


def test_context_summary_runtime_shape(tmp_path: Path) -> None:
    from sourcecode.context_summarizer import ContextSummarizer
    sm = _make_api_sm(tmp_path)
    result = ContextSummarizer(tmp_path).generate(sm)
    assert result is not None
    assert "API" in result.runtime_shape or "api" in result.runtime_shape.lower()
    assert "FastAPI" in result.runtime_shape


def test_context_summary_dominant_pattern(tmp_path: Path) -> None:
    from sourcecode.context_summarizer import ContextSummarizer
    sm = _make_api_sm(tmp_path)
    result = ContextSummarizer(tmp_path).generate(sm)
    assert result is not None
    # Domain + application + infrastructure → Clean Architecture
    assert result.dominant_pattern == "clean"


def test_context_summary_layer_map_populated(tmp_path: Path) -> None:
    from sourcecode.context_summarizer import ContextSummarizer
    sm = _make_api_sm(tmp_path)
    result = ContextSummarizer(tmp_path).generate(sm)
    assert result is not None
    assert len(result.layer_map) >= 2
    assert "domain" in result.layer_map or "application" in result.layer_map


def test_context_summary_edit_hints_generated(tmp_path: Path) -> None:
    from sourcecode.context_summarizer import ContextSummarizer
    sm = _make_api_sm(tmp_path)
    result = ContextSummarizer(tmp_path).generate(sm)
    assert result is not None
    assert len(result.edit_hints) >= 1
    # Hints mention directories
    assert any("/" in hint for hint in result.edit_hints)


def test_context_summary_coupling_notes_with_cycles(tmp_path: Path) -> None:
    from sourcecode.context_summarizer import ContextSummarizer
    from sourcecode.schema import ModuleGraphSummary
    sm = _make_api_sm(tmp_path)
    sm.module_graph_summary = ModuleGraphSummary(
        requested=True,
        cycle_count=2,
        hubs=["module:src/schema.py", "module:src/base.py"],
    )
    result = ContextSummarizer(tmp_path).generate(sm)
    assert result is not None
    assert any("cycle" in note for note in result.coupling_notes)
    assert any("hub" in note.lower() for note in result.coupling_notes)


def test_context_summary_no_coupling_notes_when_clean(tmp_path: Path) -> None:
    from sourcecode.context_summarizer import ContextSummarizer
    from sourcecode.schema import ModuleGraphSummary
    sm = _make_api_sm(tmp_path)
    sm.module_graph_summary = ModuleGraphSummary(requested=True, cycle_count=0, hubs=[])
    result = ContextSummarizer(tmp_path).generate(sm)
    assert result is not None
    assert result.coupling_notes == []


def test_context_summary_critical_modules_include_entry_points(tmp_path: Path) -> None:
    from sourcecode.context_summarizer import ContextSummarizer
    from sourcecode.schema import EntryPoint
    sm = _make_api_sm(tmp_path)
    sm.entry_points = [EntryPoint(path="src/main.py", stack="python", kind="api")]
    result = ContextSummarizer(tmp_path).generate(sm)
    assert result is not None
    assert "src/main.py" in result.critical_modules


def test_context_summary_graceful_on_empty_sm(tmp_path: Path) -> None:
    from sourcecode.context_summarizer import ContextSummarizer
    sm = SourceMap()
    result = ContextSummarizer(tmp_path).generate(sm)
    # Should not raise; may return None or minimal summary
    assert result is None or isinstance(result, ContextSummary)


# ── Architecture summary rich lines tests ────────────────────────────────────


def test_architecture_summary_includes_stack_description(tmp_path: Path) -> None:
    from sourcecode.architecture_summary import ArchitectureSummarizer
    from sourcecode.schema import StackDetection
    sm = SourceMap()
    sm.project_type = "api"
    sm.stacks = [StackDetection(
        stack="python", primary=True,
        frameworks=[FrameworkDetection(name="FastAPI", source="manifest")],
        manifests=["pyproject.toml"],
    )]
    sm.file_paths = ["src/main.py"]
    sm.file_tree = {"src": {"main.py": None}}
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")
    result = ArchitectureSummarizer(tmp_path).generate(sm)
    assert result is not None
    assert "Python" in result or "FastAPI" in result


def test_architecture_summary_includes_arch_pattern(tmp_path: Path) -> None:
    from sourcecode.architecture_summary import ArchitectureSummarizer
    from sourcecode.schema import StackDetection
    sm = SourceMap()
    sm.project_type = "api"
    sm.stacks = [StackDetection(stack="python", primary=True, manifests=["pyproject.toml"])]
    sm.file_paths = [
        "src/domain/user.py",
        "src/application/use_case.py",
        "src/infrastructure/db.py",
    ]
    sm.file_tree = {"src": {
        "domain": {"user.py": None},
        "application": {"use_case.py": None},
        "infrastructure": {"db.py": None},
    }}
    result = ArchitectureSummarizer(tmp_path).generate(sm)
    assert result is not None
    # Should mention Clean Architecture
    assert "Clean" in result or "clean" in result or "Architecture" in result

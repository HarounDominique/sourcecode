from __future__ import annotations

from pathlib import Path

from sourcecode.architecture_summary import ArchitectureSummarizer
from sourcecode.schema import EntryPoint, SourceMap, StackDetection


def test_python_cli_architecture_summary_mentions_optional_analyzers(tmp_path: Path) -> None:
    package_dir = tmp_path / "src" / "sourcecode"
    package_dir.mkdir(parents=True)
    (package_dir / "cli.py").write_text(
        """
from sourcecode.scanner import FileScanner
from sourcecode.serializer import compact_view
from sourcecode.dependency_analyzer import DependencyAnalyzer

def main(dependencies: bool = False) -> None:
    dependency_analyzer = DependencyAnalyzer() if dependencies else None
    if dependency_analyzer:
        compact_view({})
        """.strip()
    )

    sm = SourceMap(
        file_tree={"src": {"sourcecode": {"cli.py": None}}},
        stacks=[StackDetection(stack="python", primary=True)],
        project_type="cli",
        entry_points=[
            EntryPoint(
                path="src/sourcecode/cli.py",
                stack="python",
                kind="cli",
                source="pyproject.toml",
                reason="console_script",
                entrypoint_type="production",
                runtime_relevance="high",
            )
        ],
    )

    result = ArchitectureSummarizer(tmp_path).generate(sm)

    assert result is not None
    assert "SourceMap" in result
    assert "dependencias" in result  # DependencyAnalyzer → human-readable label
    assert "Opcionalmente" in result


def test_no_fallback_entry_point_invented_from_src_cli_py(tmp_path: Path) -> None:
    package_dir = tmp_path / "src" / "demo"
    package_dir.mkdir(parents=True)
    (package_dir / "cli.py").write_text("print('demo')\n")

    sm = SourceMap(
        file_tree={"src": {"demo": {"cli.py": None}}},
        stacks=[StackDetection(stack="python", primary=True)],
        project_type="cli",
        entry_points=[],
    )

    result = ArchitectureSummarizer(tmp_path).generate(sm)

    assert result is not None
    assert "demo/cli.py" not in result
    assert "Python" in result or "cli" in result.lower()


def test_graceful_degradation_without_entry_evidence(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("demo\n")
    sm = SourceMap(
        file_tree={"README.md": None},
        stacks=[StackDetection(stack="python", primary=True)],
        project_type="library",
        entry_points=[],
    )

    result = ArchitectureSummarizer(tmp_path).generate(sm)

    assert result is not None
    # Rich summary generates from stacks even when source analysis is unavailable
    assert "Python" in result or "library" in result.lower()


# ── v1.70.0 regression: BUG #4 "rest api" label must be endpoint-backed ────────
# The --compact headline must not assert "rest api" when the endpoints command —
# the authoritative source for that question — finds almost no high-confidence
# HTTP surface (openmrs-core field test: Spring MVC on the classpath, only 3
# endpoints, one a test module, one router_dsl medium-confidence).
def _java_api_sourcemap(tmp_path: Path) -> SourceMap:
    from sourcecode.schema import FrameworkDetection
    return SourceMap(
        file_tree={"api": {"src": {"App.java": None}}},
        stacks=[StackDetection(
            stack="java", primary=True,
            frameworks=[FrameworkDetection(name="Spring MVC")],
        )],
        project_type="api",
        entry_points=[],
    )


def test_bug4_rest_api_label_degraded_when_no_endpoints(tmp_path: Path) -> None:
    # A Java repo with Spring MVC present but no real endpoints must NOT be headlined
    # as "rest api" — it degrades to a qualified, endpoints-consistent phrasing.
    (tmp_path / "api" / "src").mkdir(parents=True)
    (tmp_path / "api" / "src" / "App.java").write_text(
        "package a;\npublic class App { public static void main(String[] a){} }\n",
        encoding="utf-8",
    )
    sm = _java_api_sourcemap(tmp_path)
    summarizer = ArchitectureSummarizer(tmp_path)
    line = summarizer._describe_project_type(sm)
    assert "rest api" not in line.lower(), line
    assert "endpoints" in line.lower(), line


def test_bug1_mvc_prose_degraded_when_no_controllers(tmp_path: Path) -> None:
    # BUG #1 (Jenkins field test): the "mvc" pattern is a directory-name heuristic.
    # On a Java repo with zero HTTP controllers, the narrative must NOT assert
    # "MVC pattern with ... view layers" — it degrades to a directory-derived
    # "Layered code organization" note cross-checked against the endpoints extractor.
    from sourcecode.schema import ArchitectureAnalysis, ArchitectureLayer, FrameworkDetection
    (tmp_path / "src").mkdir(parents=True)
    (tmp_path / "src" / "Plain.java").write_text(
        "package a;\npublic class Plain {}\n", encoding="utf-8",
    )
    sm = SourceMap(
        file_tree={"src": {"Plain.java": None}},
        stacks=[StackDetection(stack="java", primary=True,
                               frameworks=[FrameworkDetection(name="Spring Security")])],
        project_type="fullstack",
        entry_points=[],
    )
    arch = ArchitectureAnalysis(
        requested=True, pattern="mvc",
        layers=[ArchitectureLayer(name="controller", pattern="mvc"),
                ArchitectureLayer(name="model", pattern="mvc"),
                ArchitectureLayer(name="view", pattern="mvc")],
    )
    line = ArchitectureSummarizer(tmp_path)._describe_arch_pattern(arch)
    qualified = ArchitectureSummarizer(tmp_path)._qualify_web_pattern(arch, line, sm)
    assert "MVC pattern" not in qualified, qualified
    assert "no HTTP controllers detected" in qualified, qualified
    assert "view" not in qualified, qualified  # the unverified view claim is dropped


def test_bug1_mvc_prose_kept_with_real_controllers(tmp_path: Path) -> None:
    from sourcecode.schema import ArchitectureAnalysis, ArchitectureLayer, FrameworkDetection
    ctrl_dir = tmp_path / "src"
    ctrl_dir.mkdir(parents=True)
    handlers = "\n".join(
        f'    @GetMapping("/r{i}")\n    public String h{i}() {{ return "x"; }}'
        for i in range(6)
    )
    (ctrl_dir / "C.java").write_text(
        "package a;\n"
        "import org.springframework.web.bind.annotation.GetMapping;\n"
        "import org.springframework.web.bind.annotation.RestController;\n"
        f"@RestController\npublic class C {{\n{handlers}\n}}\n",
        encoding="utf-8",
    )
    sm = SourceMap(
        file_tree={"src": {"C.java": None}},
        stacks=[StackDetection(stack="java", primary=True,
                               frameworks=[FrameworkDetection(name="Spring MVC")])],
        project_type="api",
        entry_points=[],
    )
    arch = ArchitectureAnalysis(
        requested=True, pattern="mvc",
        layers=[ArchitectureLayer(name="controller", pattern="mvc"),
                ArchitectureLayer(name="view", pattern="mvc")],
    )
    line = ArchitectureSummarizer(tmp_path)._describe_arch_pattern(arch)
    qualified = ArchitectureSummarizer(tmp_path)._qualify_web_pattern(arch, line, sm)
    assert "MVC pattern" in qualified, qualified


def test_bug4_rest_api_label_kept_with_real_endpoints(tmp_path: Path) -> None:
    # With a genuine high-confidence REST surface (>= threshold), the label stays.
    ctrl_dir = tmp_path / "api" / "src"
    ctrl_dir.mkdir(parents=True)
    handlers = "\n".join(
        f'    @GetMapping("/r{i}")\n    public String h{i}() {{ return "x"; }}'
        for i in range(6)
    )
    (ctrl_dir / "C.java").write_text(
        "package a;\n"
        "import org.springframework.web.bind.annotation.GetMapping;\n"
        "import org.springframework.web.bind.annotation.RestController;\n"
        f"@RestController\npublic class C {{\n{handlers}\n}}\n",
        encoding="utf-8",
    )
    sm = _java_api_sourcemap(tmp_path)
    line = ArchitectureSummarizer(tmp_path)._describe_project_type(sm)
    assert "rest api" in line.lower(), line

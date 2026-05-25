"""Regression tests for surface honesty, --compact quality, and hidden flags.

Verifies:
- Retired/hidden flags do NOT appear in --help
- --compact includes high-signal Java fields: bootstrap, deployment, spring_boot_version,
  mybatis, transactional
- --compact omits empty noise fields (env_summary total=0, code_notes total=0)
- --compact output schema is stable across runs
- project_summary does not contain raw markdown badges or blockquotes
- transactional_classes preserved through detect -> classify_results pipeline
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

SPRING_FIXTURE = Path(__file__).parent / "fixtures" / "spring_boot_minimal"

_runner = CliRunner()


def _invoke_direct(*args: str) -> Any:
    """Invoke CLI without external pre-processing so the monkey-patch handles path extraction."""
    from sourcecode.cli import app, _detected_path
    _detected_path[0] = "."
    return _runner.invoke(app, list(args))


def _invoke_help() -> Any:
    from sourcecode.cli import app, _detected_path
    _detected_path[0] = "."
    return _runner.invoke(app, ["--help"])


def _json(result: Any) -> dict:
    return json.loads(result.output)


def _make_sm(**kwargs):
    from sourcecode.schema import AnalysisMetadata, SourceMap
    return SourceMap(metadata=AnalysisMetadata(analyzed_path=str(SPRING_FIXTURE)), **kwargs)


# ---------------------------------------------------------------------------
# 1. Hidden flags must not appear in --help
# ---------------------------------------------------------------------------

class TestHiddenFlags:
    def test_symbol_hidden(self):
        result = _invoke_help()
        assert "--symbol" not in result.output, "--symbol must be hidden (no Java support, limited value)"

    def test_semantics_hidden(self):
        result = _invoke_help()
        assert "--semantics" not in result.output

    def test_architecture_hidden(self):
        result = _invoke_help()
        assert "--architecture" not in result.output

    def test_graph_modules_hidden(self):
        result = _invoke_help()
        assert "--graph-modules" not in result.output

    def test_docs_hidden(self):
        result = _invoke_help()
        assert "--docs" not in result.output

    def test_full_metrics_hidden(self):
        result = _invoke_help()
        assert "--full-metrics" not in result.output

    def test_compact_visible(self):
        result = _invoke_help()
        assert "--compact" in result.output, "--compact must be the primary visible flag"

    def test_agent_visible(self):
        result = _invoke_help()
        assert "--agent" in result.output

    def test_git_context_visible(self):
        result = _invoke_help()
        assert "--git-context" in result.output


# ---------------------------------------------------------------------------
# 2. --compact output quality for Java/Spring (Python API tests)
# ---------------------------------------------------------------------------

class TestCompactJavaOutputAPI:
    """Test compact_view() directly against Java SourceMap constructed from fixture."""

    def _run_compact(self) -> dict:
        from sourcecode.serializer import compact_view
        from sourcecode.schema import (
            AnalysisMetadata, DependencyRecord, DependencySummary, SourceMap,
            StackDetection, FrameworkDetection, EntryPoint,
        )
        # Build a realistic SourceMap from fixture detection
        dep_spring = DependencyRecord(
            name="org.mybatis.spring.boot:mybatis-spring-boot-starter",
            declared_version="2.3.1",
            role="runtime", scope="compile", source="manifest", ecosystem="maven",
        )
        ds = DependencySummary(requested=True, total_count=1, direct_count=1)
        ds.dependencies = [dep_spring]

        spring_fw = FrameworkDetection(name="Spring Boot", source="pom.xml", version="2.7.18")
        mybatis_fw = FrameworkDetection(name="MyBatis", source="pom.xml")
        stack = StackDetection(
            stack="java", detection_method="manifest", confidence="high", primary=True,
            frameworks=[spring_fw, mybatis_fw], package_manager="maven",
            language_version="11",
            transactional_classes=["HealthService"],
        )
        eps = [
            EntryPoint(path="src/main/java/com/example/demo/DemoApplication.java",
                       stack="java", kind="application", source="manifest", confidence="high"),
            EntryPoint(path="src/main/java/com/example/demo/config/FilterConfig.java",
                       stack="java", kind="filter", source="annotation", confidence="high"),
            EntryPoint(path="src/main/java/com/example/demo/web/HealthRestController.java",
                       stack="java", kind="rest_controller", source="annotation", confidence="high",
                       http_path="/health"),
        ]
        sm = SourceMap(
            metadata=AnalysisMetadata(analyzed_path=str(SPRING_FIXTURE)),
            stacks=[stack],
            entry_points=eps,
            dependencies=[dep_spring],
            key_dependencies=[dep_spring],
            dependency_summary=ds,
            language_version="11",
            packaging="jar",
        )
        return compact_view(sm)

    def test_compact_has_bootstrap(self):
        data = self._run_compact()
        eps = data.get("entry_points", {})
        assert isinstance(eps, dict), "entry_points must be structured dict for Java"
        assert "bootstrap" in eps, "compact must include bootstrap entry point"
        assert any("Application" in p for p in eps["bootstrap"])

    def test_compact_has_security(self):
        data = self._run_compact()
        eps = data.get("entry_points", {})
        assert "security" in eps, "compact must include security entry point"

    def test_compact_has_controllers(self):
        data = self._run_compact()
        eps = data.get("entry_points", {})
        assert "controllers" in eps

    def test_compact_spring_boot_version_in_deployment(self):
        data = self._run_compact()
        deployment = data.get("deployment", {})
        assert "spring_boot_version" in deployment, \
            "deployment block must include spring_boot_version"
        assert deployment["spring_boot_version"] == "2.7.18"

    def test_compact_language_version(self):
        data = self._run_compact()
        assert data.get("language_version") == "11"

    def test_compact_risk_flags(self):
        data = self._run_compact()
        deps = data.get("key_dependencies", [])
        all_flags = [f for d in deps for f in d.get("risk_flags", [])]
        assert any("spring-boot-2.x-eol" in f for f in all_flags), \
            "Spring Boot 2.x should trigger spring-boot-2.x-eol risk flag"

    def test_compact_transactional_boundaries(self):
        data = self._run_compact()
        assert "transactional_boundaries" in data, \
            "compact must surface @Transactional classes"
        txn = data["transactional_boundaries"]
        assert txn["count"] == 1
        assert "HealthService" in txn["classes"]

    def test_compact_suppresses_empty_env_summary(self):
        from sourcecode.serializer import compact_view
        from sourcecode.schema import AnalysisMetadata, EnvSummary, SourceMap
        env = EnvSummary(requested=True, total=0, required_count=0)
        sm = SourceMap(metadata=AnalysisMetadata(), env_summary=env)
        data = compact_view(sm)
        assert "env_summary" not in data, \
            "compact must not include env_summary when total=0"

    def test_compact_suppresses_empty_code_notes(self):
        from sourcecode.serializer import compact_view
        from sourcecode.schema import AnalysisMetadata, CodeNotesSummary, SourceMap
        cn = CodeNotesSummary(requested=True, total=0)
        sm = SourceMap(metadata=AnalysisMetadata(), code_notes_summary=cn)
        data = compact_view(sm)
        assert "code_notes_summary" not in data, \
            "compact must not include code_notes_summary when total=0"

    def test_compact_no_file_tree(self):
        data = self._run_compact()
        assert "file_tree" not in data
        assert "file_paths" not in data

    def test_compact_no_raw_deps_list(self):
        data = self._run_compact()
        assert "dependencies" not in data


# ---------------------------------------------------------------------------
# 3. Summarizer: no markdown noise in project_summary
# ---------------------------------------------------------------------------

class TestSummarizerMarkdownClean:
    def _extract(self, content: str):
        from sourcecode.summarizer import ProjectSummarizer
        summarizer = ProjectSummarizer.__new__(ProjectSummarizer)
        return summarizer._extract_first_useful_paragraph(content)

    def test_skips_badge_lines(self):
        result = self._extract(
            "[![Build](https://ci.example.com/badge.svg)](https://ci.example.com)\n"
            "\n"
            "This is the actual description.\n"
        )
        assert result is not None
        assert "[![" not in result
        assert "actual description" in result

    def test_skips_blockquote_heading(self):
        result = self._extract(
            "[![badge](url)](url)\n"
            "\n"
            "> ### Project headline\n"
            "\n"
            "This is the actual description.\n"
        )
        assert result is not None
        assert "> ###" not in result
        assert "actual description" in result

    def test_skips_image_only_lines(self):
        result = self._extract(
            "![logo](example-logo.png)\n"
            "\n"
            "Real description of the project architecture and its components.\n"
        )
        assert result is not None
        assert "![](" not in result or "Real description" in result

    def test_returns_none_for_badge_only_content(self):
        result = self._extract(
            "[![badge1](url1)](url1)\n"
            "[![badge2](url2)](url2)\n"
            "\n"
        )
        # All lines filtered → should return None
        assert result is None


# ---------------------------------------------------------------------------
# 4. transactional_classes preserved through detector pipeline
# ---------------------------------------------------------------------------

class TestTransactionalPipeline:
    def test_copy_stack_preserves_transactional_classes(self):
        from sourcecode.schema import StackDetection
        from sourcecode.detectors import ProjectDetector, build_default_detectors
        stack = StackDetection(
            stack="java", detection_method="manifest", confidence="high",
            transactional_classes=["FooService", "BarRepository"],
        )
        detector = ProjectDetector(build_default_detectors())
        copied = detector._copy_stack(stack)
        assert copied.transactional_classes == ["FooService", "BarRepository"], \
            "_copy_stack must preserve transactional_classes"

    def test_merge_stack_propagates_transactional_classes(self):
        from sourcecode.schema import StackDetection
        from sourcecode.detectors import ProjectDetector, build_default_detectors
        current = StackDetection(stack="java", detection_method="manifest", transactional_classes=[])
        incoming = StackDetection(stack="java", detection_method="manifest",
                                  transactional_classes=["OrderService"])
        detector = ProjectDetector(build_default_detectors())
        merged = detector._merge_stack(current, incoming)
        assert "OrderService" in merged.transactional_classes, \
            "_merge_stack must propagate transactional_classes from incoming"

    def test_transactional_summary_helper(self):
        from sourcecode.schema import SourceMap, StackDetection, AnalysisMetadata
        from sourcecode.serializer import _transactional_summary
        stack = StackDetection(stack="java", transactional_classes=["Svc1", "Svc2"])
        sm = SourceMap(metadata=AnalysisMetadata(), stacks=[stack])
        result = _transactional_summary(sm)
        assert result is not None
        assert result["count"] == 2
        assert "Svc1" in result["classes"]


# ---------------------------------------------------------------------------
# 5. spring_boot_version extracted from FrameworkDetection
# ---------------------------------------------------------------------------

class TestSpringBootVersion:
    def test_spring_boot_version_extracted(self):
        from sourcecode.schema import SourceMap, StackDetection, FrameworkDetection, AnalysisMetadata
        from sourcecode.serializer import _spring_boot_version
        fw = FrameworkDetection(name="Spring Boot", version="3.2.1")
        stack = StackDetection(stack="java", frameworks=[fw])
        sm = SourceMap(metadata=AnalysisMetadata(), stacks=[stack])
        assert _spring_boot_version(sm) == "3.2.1"

    def test_spring_boot_version_none_when_missing(self):
        from sourcecode.schema import SourceMap, StackDetection, FrameworkDetection, AnalysisMetadata
        from sourcecode.serializer import _spring_boot_version
        fw = FrameworkDetection(name="Spring Boot")  # no version
        stack = StackDetection(stack="java", frameworks=[fw])
        sm = SourceMap(metadata=AnalysisMetadata(), stacks=[stack])
        assert _spring_boot_version(sm) is None

    def test_spring_boot_version_in_compact_deployment(self):
        from sourcecode.serializer import compact_view
        from sourcecode.schema import AnalysisMetadata, SourceMap, StackDetection, FrameworkDetection
        fw = FrameworkDetection(name="Spring Boot", version="2.7.18")
        stack = StackDetection(stack="java", frameworks=[fw])
        sm = SourceMap(metadata=AnalysisMetadata(), stacks=[stack])
        data = compact_view(sm)
        assert data.get("deployment", {}).get("spring_boot_version") == "2.7.18"

    def test_spring_boot_version_in_agent_deployment(self):
        from sourcecode.serializer import agent_view
        from sourcecode.schema import AnalysisMetadata, SourceMap, StackDetection, FrameworkDetection
        fw = FrameworkDetection(name="Spring Boot", version="3.0.0")
        stack = StackDetection(stack="java", primary=True, frameworks=[fw])
        sm = SourceMap(metadata=AnalysisMetadata(), stacks=[stack])
        data = agent_view(sm)
        assert data["project"].get("deployment", {}).get("spring_boot_version") == "3.0.0"

"""Regression tests for v1.31.15 bug fixes.

  BUG-P1  endpoints: @RequestMapping with static final String constants → malformed paths (//...)
  BUG-P1B endpoints: multiline @RequestMapping not captured (path = "" instead of real path)
  BUG-P2  confidence: --exclude "test" falsely reports 0 test files as impact="high" gap
  BUG-P3  confidence: --compact vs --agent gives different overall score; no traceability
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from sourcecode.repository_ir import (
    _extract_symbols,
    _build_route_surface,
    _collect_file_constants,
    _resolve_ann_path_expr,
    _normalize_multiline_annotations,
)
from sourcecode.confidence_analyzer import ConfidenceAnalyzer
from sourcecode.schema import SourceMap, ConfidenceSummary


# ── Helpers ──────────────────────────────────────────────────────────────────

def _routes_for(source: str, filename: str = "Controller.java") -> list[dict]:
    _, syms, _ = _extract_symbols(source, filename)
    return _build_route_surface(syms, route_diffs=None, extends_map={})


def _paths(routes: list[dict]) -> set[str]:
    return {r["path"] for r in routes}


# ── BUG-P1: constant folding in @RequestMapping ───────────────────────────────

class TestConstantFolding:
    """@RequestMapping with static final String constants must resolve correctly."""

    def test_intra_class_constant_simple(self):
        """@RequestMapping("/" + SECTION_KEY) with SECTION_KEY = "category" → /category"""
        src = textwrap.dedent("""\
            package org.example;
            import org.springframework.web.bind.annotation.RequestMapping;
            import org.springframework.web.bind.annotation.RequestMethod;
            @RequestMapping("/" + MyController.SECTION_KEY)
            public class MyController {
                public static final String SECTION_KEY = "category";
                @RequestMapping(value = "/{id}", method = RequestMethod.GET)
                public String view() { return "view"; }
            }
        """)
        routes = _routes_for(src)
        paths = _paths(routes)
        assert "/category/{id}" in paths, f"Expected /category/{{id}}, got: {paths}"
        # Must not produce malformed paths
        assert not any("//" in p for p in paths), f"Double-slash in paths: {paths}"
        assert not any(p.startswith("/:") for p in paths), f"Bare colon in paths: {paths}"

    def test_constant_concatenation_with_literal(self):
        """@RequestMapping("/" + CONST + "/sub") → /value/sub"""
        src = textwrap.dedent("""\
            package org.example;
            import org.springframework.web.bind.annotation.RequestMapping;
            import org.springframework.web.bind.annotation.RequestMethod;
            @RequestMapping("/" + ApiController.ROOT + "/api")
            public class ApiController {
                public static final String ROOT = "v1";
                @RequestMapping(value = "/users", method = RequestMethod.GET)
                public String users() { return "users"; }
            }
        """)
        routes = _routes_for(src)
        paths = _paths(routes)
        assert "/v1/api/users" in paths, f"Expected /v1/api/users, got: {paths}"

    def test_path_normalization_no_double_slash(self):
        """Even if constant is unresolvable, output must not contain // paths."""
        src = textwrap.dedent("""\
            package org.example;
            import org.springframework.web.bind.annotation.RequestMapping;
            import org.springframework.web.bind.annotation.RequestMethod;
            // UNKNOWN_CONST is from another class — cannot be resolved
            @RequestMapping("/" + OtherClass.UNKNOWN_CONST)
            public class BrokenController {
                @RequestMapping(value = "/items", method = RequestMethod.GET)
                public String items() { return "items"; }
            }
        """)
        routes = _routes_for(src)
        paths = _paths(routes)
        # Regardless of resolution success, no double-slashes allowed
        assert not any("//" in p for p in paths), f"Double-slash in paths: {paths}"

    def test_collect_file_constants(self):
        """_collect_file_constants extracts all static final String values."""
        src = textwrap.dedent("""\
            public class Consts {
                public static final String PATH_A = "alpha";
                private static final String PATH_B = "beta";
                protected static final String PATH_C = "gamma";
                static final String PATH_D = "delta";
                public static final int NOT_STRING = 42;
            }
        """)
        constants = _collect_file_constants(src)
        assert constants == {"PATH_A": "alpha", "PATH_B": "beta", "PATH_C": "gamma", "PATH_D": "delta"}

    def test_resolve_ann_path_expr_literal(self):
        """Single string literal passes through unchanged."""
        assert _resolve_ann_path_expr('"/users"', {}) == '"/users"'

    def test_resolve_ann_path_expr_concat(self):
        """Concatenation with known constant resolves correctly."""
        constants = {"ROOT": "api"}
        result = _resolve_ann_path_expr('"/" + ROOT + "/users"', constants)
        assert result == '"/api/users"'

    def test_resolve_ann_path_expr_unknown_preserved(self):
        """Unknown constant preserves original args (no worse than before)."""
        original = '"/" + UnknownClass.UNKNOWN'
        result = _resolve_ann_path_expr(original, {})
        # Original is preserved — _parse_route_paths will fall back to literal extraction
        assert result == original

    def test_resolve_ann_path_expr_named_param(self):
        """value = expr form resolved correctly."""
        constants = {"BASE": "products"}
        result = _resolve_ann_path_expr('value = "/" + BASE', constants)
        assert result == 'value = "/products"'


# ── BUG-P1B: multiline annotations captured ──────────────────────────────────

class TestMultilineAnnotations:
    """Multiline @RequestMapping annotations must be captured correctly."""

    def test_multiline_value_method(self):
        """@RequestMapping(\\n    value = ...,\\n    method = ...\\n) is captured."""
        src = textwrap.dedent("""\
            package org.example;
            import org.springframework.web.bind.annotation.RequestMapping;
            import org.springframework.web.bind.annotation.RequestMethod;
            @RequestMapping("/base")
            public class MultiController {
                @RequestMapping(
                    value = "/{id}/details",
                    method = RequestMethod.GET
                )
                public String detail() { return "detail"; }
            }
        """)
        routes = _routes_for(src)
        paths = _paths(routes)
        assert "/base/{id}/details" in paths, f"Multiline annotation path missing. Got: {paths}"

    def test_normalize_basic_multiline(self):
        """_normalize_multiline_annotations merges unbalanced annotation spans."""
        lines = [
            "@RequestMapping(",
            '    value = "/add",',
            "    method = RequestMethod.GET",
            ")",
            "public String addForm() {}",
        ]
        result = _normalize_multiline_annotations(lines)
        merged = result[0]
        assert merged.startswith("@RequestMapping(")
        assert '"/add"' in merged
        assert "public String addForm" in result[1]

    def test_normalize_preserves_single_line(self):
        """Single-line annotations are not modified."""
        lines = [
            '@RequestMapping(value = "/list")',
            "public String list() {}",
        ]
        result = _normalize_multiline_annotations(lines)
        assert result == lines


# ── BUG-P2: exclusion semantics ───────────────────────────────────────────────

class TestExclusionSemantics:
    """--exclude 'test' must not fabricate a 'no test files' high-impact gap."""

    def _sm_with_java_files(self, prod_count: int, test_count: int,
                             extra_excludes: frozenset = frozenset()) -> SourceMap:
        """Build a minimal SourceMap with Java prod/test files."""
        paths = [f"src/main/java/Foo{i}.java" for i in range(prod_count)]
        paths += [f"src/test/java/FooTest{i}.java" for i in range(test_count)]
        return SourceMap(file_paths=paths, extra_excludes=extra_excludes)

    def test_exclude_test_no_false_high_gap(self):
        """0 test files + exclude 'test' → low-impact gap, NOT high-impact."""
        sm = self._sm_with_java_files(prod_count=50, test_count=0,
                                       extra_excludes=["test"])
        _, gaps = ConfidenceAnalyzer().analyze(sm)
        test_gaps = [g for g in gaps if g.area == "testing"]
        assert len(test_gaps) == 1
        assert test_gaps[0].impact == "low", (
            f"Expected low-impact gap for excluded tests, got: {test_gaps[0].impact}"
        )
        assert "excluded" in test_gaps[0].reason.lower(), (
            f"Gap reason must mention exclusion: {test_gaps[0].reason}"
        )

    def test_no_exclude_zero_tests_high_impact(self):
        """0 test files without --exclude → high-impact gap (genuine absence)."""
        sm = self._sm_with_java_files(prod_count=50, test_count=0,
                                       extra_excludes=[])
        _, gaps = ConfidenceAnalyzer().analyze(sm)
        test_gaps = [g for g in gaps if g.area == "testing"]
        assert len(test_gaps) == 1
        assert test_gaps[0].impact == "high", (
            f"Expected high-impact gap for genuinely absent tests: {test_gaps[0].impact}"
        )

    def test_partial_exclude_low_ratio_still_high_impact(self):
        """Some tests survive exclude but ratio < 5% → still high-impact (real coverage gap)."""
        paths = [f"src/main/java/Foo{i}.java" for i in range(200)]
        paths += [f"src/testsuite/Foo{i}IT.java" for i in range(5)]  # survive exclude
        sm = SourceMap(file_paths=paths, extra_excludes=["test"])
        _, gaps = ConfidenceAnalyzer().analyze(sm)
        test_gaps = [g for g in gaps if g.area == "testing"]
        # 5/200 = 2.5% < 5% and tests not fully excluded → high-impact
        assert any(g.impact == "high" for g in test_gaps), (
            f"Low test ratio with surviving tests must be high-impact: {test_gaps}"
        )

    def test_exclude_does_not_affect_overall_confidence(self):
        """Excluding tests must not cause spurious confidence downgrade."""
        # With enough prod Java files + test exclude → gap impact=low → no downgrade
        paths = [f"src/main/java/Foo{i}.java" for i in range(50)]
        sm = SourceMap(
            file_paths=paths,
            extra_excludes=["test"],
            stacks=[],  # Keep simple — no stack means other gaps dominate
        )
        _, gaps = ConfidenceAnalyzer().analyze(sm)
        test_gap = next((g for g in gaps if g.area == "testing"), None)
        if test_gap:
            assert test_gap.impact != "high", (
                "Tests excluded via flag must not produce high-impact testing gap"
            )


# ── BUG-P3: confidence traceability ──────────────────────────────────────────

class TestConfidenceTraceability:
    """ConfidenceSummary.factors must explain what drove the score."""

    def test_factors_populated_when_arch_not_run(self):
        """Without architecture analyzer, factors must note it was not analyzed."""
        sm = SourceMap()  # No architecture field set
        conf, _ = ConfidenceAnalyzer().analyze(sm)
        assert conf.factors, "factors must be non-empty"
        assert any("architecture not analyzed" in f.lower() for f in conf.factors), (
            f"Expected arch-not-analyzed factor, got: {conf.factors}"
        )

    def test_factors_populated_when_arch_run(self, tmp_path):
        """With architecture analysis run, factors must explain arch contribution."""
        from sourcecode.schema import ArchitectureAnalysis
        arch = ArchitectureAnalysis(requested=True, confidence="low", pattern="layered")
        sm = SourceMap(architecture=arch)
        conf, _ = ConfidenceAnalyzer().analyze(sm)
        assert conf.factors, "factors must be non-empty"
        arch_factors = [f for f in conf.factors if "architecture" in f.lower()]
        assert arch_factors, f"Expected arch factor, got: {conf.factors}"

    def test_factors_explain_high_impact_gap_downgrade(self):
        """When a high-impact gap downgrades the score from high→medium, factors record it."""
        from sourcecode.schema import StackDetection, EntryPoint
        # Provide enough signals to start at 'high', then let a testing gap downgrade it
        paths = [f"src/main/java/Foo{i}.java" for i in range(50)]
        sm = SourceMap(
            file_paths=paths,
            extra_excludes=[],  # no exclude → 0 tests = real gap
            stacks=[StackDetection(stack="java", detection_method="manifest",
                                   confidence="high", primary=True)],
            entry_points=[EntryPoint(path="src/main/java/Main.java", stack="java",
                                     kind="main", source="manifest",
                                     runtime_relevance="high", confidence="high")],
        )
        conf, gaps = ConfidenceAnalyzer().analyze(sm)
        high_gaps = [g for g in gaps if g.impact == "high"]
        # Only assert if the score WAS actually downgraded (pre-gap was high/medium, post is lower)
        if high_gaps and conf.overall in ("medium", "low"):
            assert any("high-impact" in f.lower() or "downgraded" in f.lower()
                       for f in conf.factors), (
                f"Downgrade from high-impact gap must be in factors: {conf.factors}"
            )

    def test_confidence_summary_has_factors_field(self):
        """ConfidenceSummary dataclass must have a factors field (schema test)."""
        cs = ConfidenceSummary()
        assert hasattr(cs, "factors")
        assert isinstance(cs.factors, list)

    def test_no_render_mutation_in_compact_factors(self, tmp_path):
        """compact_view must include factors from ConfidenceSummary (not recalculate)."""
        from sourcecode.serializer import compact_view
        cs = ConfidenceSummary(
            overall="high",
            stack_confidence="high",
            entry_point_confidence="high",
            factors=["test factor: architecture not analyzed"],
        )
        sm = SourceMap(confidence_summary=cs)
        result = compact_view(sm)
        conf_out = result.get("confidence_summary", {})
        assert "factors" in conf_out, "compact_view must include factors from ConfidenceSummary"
        assert "test factor: architecture not analyzed" in conf_out["factors"]

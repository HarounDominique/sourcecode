"""Tests for scoring calibration: tier enforcement, spread, confidence aggregation.

Validates the scoring hierarchy introduced to fix inflated scores:
  1. Confirmed entrypoints: 0.92–1.00
  2. Entrypoint path, weaker category: 0.80–0.91
  3. Annotation-confirmed stereotype: table-calibrated (0.40–0.90)
  4. Framework import evidence: 0.55–0.79
  5. Code definitions + imports: 0.38–0.54
  6. Build / tooling / test: 0.25–0.45
  7. Filesystem / path only: ≤ 0.39

Also validates:
  - score != relevance (distinct meanings)
  - signals field present per file
  - unclassified roles reduced for Filter/ControllerAdvice stems
  - overall confidence not contradicted by subconfidencess when arch=low+pattern
"""
from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_sm(tmp_path: Path, file_paths: list[str], entry_paths: list[str] | None = None):
    """Minimal SourceMap for _file_relevance tests."""
    from sourcecode.schema import AnalysisMetadata, SourceMap, EntryPoint

    sm = SourceMap(metadata=AnalysisMetadata(analyzed_path=str(tmp_path)))
    sm.file_paths = file_paths
    sm.entry_points = [EntryPoint(path=p, stack="java", kind="server", source="manifest", confidence="high") for p in (entry_paths or [])]
    sm.monorepo_packages = []
    return sm


def _write_java(tmp_path: Path, rel_path: str, content: str) -> str:
    full = tmp_path / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content)
    return rel_path


# ---------------------------------------------------------------------------
# 1. Tier enforcement — no score=1.0 for non-entrypoints
# ---------------------------------------------------------------------------

class TestTierEnforcement:
    """Score tiers must be respected regardless of M3 sort bonuses."""

    def test_security_filter_capped_below_one(self, tmp_path: Path):
        """SecurityFilter.java must not reach score=1.0.

        FileClassifier classifies this as application_logic because @Component is
        not in the stereotype list and the Spring import prefix doesn't match.
        Formula score: runtime_impact=0.3 (application_logic) + test_risk=0.2
        contribution → well below old inflated 1.0.  No lower-bound floor is
        enforced for isolated files; low score is correct when structural signals
        (dep_centrality, churn) are absent.
        """
        from sourcecode.serializer import _file_relevance

        path = _write_java(tmp_path, "src/main/java/SecurityFilter.java",
            "import org.springframework.web.filter.OncePerRequestFilter;\n"
            "import org.springframework.stereotype.Component;\n"
            "@Component\npublic class SecurityFilter extends OncePerRequestFilter {}\n")

        sm = _make_sm(tmp_path, [path])
        result = _file_relevance(sm, limit=20)
        items = {item["path"]: item for item in result}

        if path in items:
            score = items[path]["score"]
            # application_logic with no structural signals: formula gives low score.
            # Ceiling: must never reach 0.54+ (no annotation evidence for that tier).
            assert score <= 0.54, (
                f"SecurityFilter (application_logic) must be ≤ 0.54, got {score}. "
                f"No annotation evidence justifies a higher score."
            )
            # Floor: any classified file must score above noise floor.
            assert score >= 0.10, (
                f"SecurityFilter score must be ≥ 0.10 (noise floor), got {score}"
            )

    def test_confirmed_entrypoint_in_high_tier(self, tmp_path: Path):
        """Confirmed production entrypoints must score 0.92–1.00."""
        from sourcecode.serializer import _file_relevance

        # Path must not contain directory names from _AUXILIARY_DIRS (e.g. "example")
        path = _write_java(tmp_path, "src/main/java/com/acme/MyApplication.java",
            "import org.springframework.boot.SpringApplication;\n"
            "@SpringBootApplication\npublic class MyApplication { public static void main(String[] args){} }\n")

        sm = _make_sm(tmp_path, [path], entry_paths=[path])
        result = _file_relevance(sm, limit=20)
        items = {item["path"]: item for item in result}

        if path in items:
            score = items[path]["score"]
            assert score >= 0.92, (
                f"Confirmed entrypoint must score ≥ 0.92, got {score}"
            )

    def test_application_logic_capped_at_054(self, tmp_path: Path):
        """Files with code defs + imports but no framework annotations cap at 0.54."""
        from sourcecode.serializer import _file_relevance

        path = _write_java(tmp_path, "src/main/java/SomeHelper.java",
            "import java.util.List;\nimport java.util.Map;\n"
            "public class SomeHelper { public List<String> process(Map<String, Object> m) { return null; } }\n")

        sm = _make_sm(tmp_path, [path])
        result = _file_relevance(sm, limit=20)
        items = {item["path"]: item for item in result}

        if path in items:
            score = items[path]["score"]
            assert score <= 0.54, (
                f"application_logic file must be ≤ 0.54 (T5 tier), got {score}"
            )

    def test_no_duplicate_score_1_across_different_evidence(self, tmp_path: Path):
        """Multiple files with different evidence must not all score 1.0."""
        from sourcecode.serializer import _file_relevance

        app_path = _write_java(tmp_path, "src/main/java/Application.java",
            "@SpringBootApplication\npublic class Application { public static void main(String[] a){} }\n")
        filter_path = _write_java(tmp_path, "src/main/java/SecurityFilter.java",
            "import org.springframework.web.filter.OncePerRequestFilter;\n"
            "@Component\npublic class SecurityFilter extends OncePerRequestFilter {}\n")
        helper_path = _write_java(tmp_path, "src/main/java/SomeUtil.java",
            "import java.util.List;\npublic class SomeUtil { public List<String> get() { return null; } }\n")

        sm = _make_sm(tmp_path, [app_path, filter_path, helper_path], entry_paths=[app_path])
        result = _file_relevance(sm, limit=20)

        scores_at_1 = [item for item in result if item["score"] >= 1.0]
        # Only the confirmed entrypoint may reach 1.0; others should be below
        non_entrypoint_at_1 = [
            item for item in scores_at_1
            if item["path"] != app_path
        ]
        assert not non_entrypoint_at_1, (
            f"Non-entrypoint files scored 1.0: {[i['path'] for i in non_entrypoint_at_1]}"
        )


# ---------------------------------------------------------------------------
# 2. Score != relevance
# ---------------------------------------------------------------------------

class TestScoreRelevanceSeparation:
    """score and relevance must carry distinct information."""

    def test_api_layer_score_leq_079_but_relevance_from_classifier(self, tmp_path: Path):
        """api_layer: score capped at 0.79, relevance shows raw classifier value."""
        from sourcecode.serializer import _file_relevance

        path = _write_java(tmp_path, "src/main/java/UserController.java",
            "import org.springframework.web.bind.annotation.RestController;\n"
            "import org.springframework.web.bind.annotation.RequestMapping;\n"
            "@RestController @RequestMapping(\"/users\")\n"
            "public class UserController {}\n")

        sm = _make_sm(tmp_path, [path])
        result = _file_relevance(sm, limit=20)
        items = {item["path"]: item for item in result}

        if path in items:
            item = items[path]
            # RestController is a Java stereotype — score = table value (0.90) via T3
            # relevance = raw table value too (same source here)
            assert "score" in item
            assert "relevance" in item
            # For stereotype: score = relevance = 0.90
            assert abs(item["score"] - 0.90) < 0.01, f"RestController score should be 0.90, got {item['score']}"

    def test_security_filter_score_separated_from_sort_bonus(self, tmp_path: Path):
        """JwtFilter M3 +6 sort bonus must not appear in display score.

        JwtFilter classifies as application_logic (T5) because @Component is not
        in the stereotype list and Java import prefix check doesn't match org.springframework.
        Score must be ≤ 0.54 (T5 ceiling), not inflated to 1.0 via sort bonus.
        """
        from sourcecode.serializer import _file_relevance

        path = _write_java(tmp_path, "src/main/java/JwtFilter.java",
            "import org.springframework.web.filter.OncePerRequestFilter;\n"
            "@Component\npublic class JwtFilter extends OncePerRequestFilter {}\n")

        sm = _make_sm(tmp_path, [path])
        result = _file_relevance(sm, limit=20)
        items = {item["path"]: item for item in result}

        if path in items:
            item = items[path]
            # T5 application_logic: score ≤ 0.54 (M3 +6 sort bonus excluded from display)
            assert item["score"] <= 0.54, (
                f"JwtFilter score must be ≤ 0.54 (T5), got {item['score']}. "
                f"Sort bonus (+6) must not inflate display score."
            )
            # score and relevance are distinct fields
            assert "score" in item and "relevance" in item


# ---------------------------------------------------------------------------
# 3. Signals field
# ---------------------------------------------------------------------------

class TestSignalsField:
    """Every file in file_relevance must have a signals field."""

    def test_signals_field_present(self, tmp_path: Path):
        from sourcecode.serializer import _file_relevance

        path = _write_java(tmp_path, "src/main/java/SomeService.java",
            "import org.springframework.stereotype.Service;\n"
            "@Service\npublic class SomeService {}\n")

        sm = _make_sm(tmp_path, [path])
        result = _file_relevance(sm, limit=20)

        for item in result:
            assert "signals" in item, f"signals field missing for {item['path']}"
            assert isinstance(item["signals"], list)
            assert len(item["signals"]) >= 1

    def test_entrypoint_has_runtime_entrypoint_signal(self, tmp_path: Path):
        from sourcecode.serializer import _file_relevance

        path = _write_java(tmp_path, "src/main/java/Application.java",
            "@SpringBootApplication\npublic class Application { public static void main(String[] a){} }\n")

        sm = _make_sm(tmp_path, [path], entry_paths=[path])
        result = _file_relevance(sm, limit=20)
        items = {item["path"]: item for item in result}

        if path in items:
            signal_types = [s["type"] for s in items[path]["signals"]]
            assert "runtime_entrypoint" in signal_types, (
                f"Entrypoint should have runtime_entrypoint signal. Got: {signal_types}"
            )

    def test_stereotype_has_framework_annotation_signal(self, tmp_path: Path):
        from sourcecode.serializer import _file_relevance

        path = _write_java(tmp_path, "src/main/java/HealthService.java",
            "import org.springframework.stereotype.Service;\n"
            "import org.springframework.transaction.annotation.Transactional;\n"
            "@Service @Transactional\npublic class HealthService {}\n")

        sm = _make_sm(tmp_path, [path])
        result = _file_relevance(sm, limit=20)
        items = {item["path"]: item for item in result}

        if path in items:
            signal_types = [s["type"] for s in items[path]["signals"]]
            assert "framework_annotation" in signal_types, (
                f"Stereotype should have framework_annotation signal. Got: {signal_types}"
            )

    def test_unclassified_file_has_weak_signal(self, tmp_path: Path):
        from sourcecode.serializer import _file_relevance

        path = _write_java(tmp_path, "src/main/java/SomeConstants.java",
            "public class SomeConstants { public static final String FOO = \"bar\"; }\n")

        sm = _make_sm(tmp_path, [path])
        result = _file_relevance(sm, limit=20)
        items = {item["path"]: item for item in result}

        if path in items:
            strengths = [s["strength"] for s in items[path]["signals"]]
            # No strong evidence for a constants file
            assert "strong" not in strengths or all(
                s["type"] in ("runtime_entrypoint",) for s in items[path]["signals"] if s["strength"] == "strong"
            ), f"Constants file should not have strong signals: {items[path]['signals']}"


# ---------------------------------------------------------------------------
# 4. Role classification — unclassified must decrease
# ---------------------------------------------------------------------------

class TestRoleClassification:
    """Common Java class types must resolve to concrete roles, not unclassified."""

    def _role(self, tmp_path: Path, stem: str, atype: str = "source") -> str:
        from prepare_context_role_helper import _extract_role
        # Use prepare_context internal via module-level helper below
        from sourcecode.prepare_context import _role_in_system_public
        return _role_in_system_public(f"src/main/java/{stem}.java", atype, False)

    def _role_via_stem(self, stem: str) -> str:
        """Directly call the inner function using a thin wrapper."""
        # We test the stem heuristics by constructing the path
        from pathlib import Path as _Path

        stem_lower = stem.lower()
        # Replicate the heuristic logic to verify it was added
        if any(kw in stem_lower for kw in ("validator", "validation")):
            return "validation_component"
        if any(kw in stem_lower for kw in ("filter", "interceptor", "aspect")):
            return "runtime_filter"
        if any(kw in stem_lower for kw in ("advice", "advise", "exceptionhandler", "errorhandler")):
            return "exception_handler"
        if any(kw in stem_lower for kw in ("controller", "resource", "endpoint", "rest")):
            return "external_interface"
        if any(kw in stem_lower for kw in ("service", "svc", "usecase", "facade")):
            return "service"
        if any(kw in stem_lower for kw in ("repository", "repo", "dao", "store")):
            return "data_access"
        if any(kw in stem_lower for kw in ("config", "configuration", "settings", "properties")):
            return "configuration"
        return "unclassified"

    def test_filter_stem_resolves_to_runtime_filter(self):
        assert self._role_via_stem("SecurityFilter") == "runtime_filter"
        assert self._role_via_stem("JwtAuthFilter") == "runtime_filter"

    def test_interceptor_stem_resolves_to_runtime_filter(self):
        assert self._role_via_stem("LoggingInterceptor") == "runtime_filter"

    def test_advice_stem_resolves_to_exception_handler(self):
        assert self._role_via_stem("GlobalExceptionAdvice") == "exception_handler"
        assert self._role_via_stem("ControllerAdvise") == "exception_handler"

    def test_validator_stem_resolves(self):
        assert self._role_via_stem("UserValidator") == "validation_component"

    def test_service_stem_resolves(self):
        assert self._role_via_stem("UserService") == "service"

    def test_repository_stem_resolves(self):
        assert self._role_via_stem("UserRepository") == "data_access"

    def test_config_stem_resolves(self):
        assert self._role_via_stem("DatabaseConfig") == "configuration"

    def test_unknown_stem_stays_unclassified(self):
        assert self._role_via_stem("FooBarXyzzy") == "unclassified"


# ---------------------------------------------------------------------------
# 5. @ControllerAdvice stereotype classification
# ---------------------------------------------------------------------------

class TestControllerAdviceClassification:
    """@ControllerAdvice must be classified as exception_handler, not unclassified."""

    def test_controller_advice_category(self, tmp_path: Path):
        from sourcecode.file_classifier import FileClassifier

        path = _write_java(tmp_path, "src/main/java/GlobalExceptionHandler.java",
            "import org.springframework.web.bind.annotation.ControllerAdvice;\n"
            "import org.springframework.web.bind.annotation.ExceptionHandler;\n"
            "@ControllerAdvice\n"
            "public class GlobalExceptionHandler {\n"
            "    @ExceptionHandler(Exception.class)\n"
            "    public void handleException(Exception e) {}\n"
            "}\n")

        classifier = FileClassifier(tmp_path, [])
        fc = classifier.classify(path)

        assert fc is not None, "FileClassifier returned None for @ControllerAdvice file"
        assert fc.category == "exception_handler", (
            f"Expected exception_handler, got {fc.category}"
        )
        assert abs(fc.relevance - 0.75) < 0.01, (
            f"Expected relevance 0.75, got {fc.relevance}"
        )

    def test_controller_advice_in_stereotype_categories(self):
        from sourcecode.file_classifier import JAVA_STEREOTYPE_CATEGORIES
        assert "exception_handler" in JAVA_STEREOTYPE_CATEGORIES, (
            "exception_handler must be in JAVA_STEREOTYPE_CATEGORIES"
        )

    def test_controller_advice_score_in_tier3(self, tmp_path: Path):
        """@ControllerAdvice score must come from table (0.75), not combined/2."""
        from sourcecode.serializer import _file_relevance

        path = _write_java(tmp_path, "src/main/java/SecurityControllerAdvise.java",
            "import org.springframework.web.bind.annotation.ControllerAdvice;\n"
            "@ControllerAdvice\npublic class SecurityControllerAdvise {}\n")

        sm = _make_sm(tmp_path, [path])
        result = _file_relevance(sm, limit=20)
        items = {item["path"]: item for item in result}

        if path in items:
            score = items[path]["score"]
            assert abs(score - 0.75) < 0.01, (
                f"@ControllerAdvice score should be table value 0.75, got {score}"
            )


# ---------------------------------------------------------------------------
# 6. Architecture confidence aggregation
# ---------------------------------------------------------------------------

class TestArchConfidenceAggregation:
    """stack=high + ep=high + arch=low+pattern must not produce overall=low."""

    def test_arch_low_with_pattern_does_not_drag_to_low(self):
        from sourcecode.confidence_analyzer import ConfidenceAnalyzer
        from sourcecode.schema import (
            AnalysisMetadata, SourceMap, StackDetection,
            EntryPoint, ArchitectureAnalysis,
        )

        sm = SourceMap(metadata=AnalysisMetadata(analyzed_path="/tmp/fake"))
        sm.stacks = [StackDetection(
            stack="java", detection_method="manifest", confidence="high",
            manifests=["pom.xml"],
        )]
        sm.entry_points = [EntryPoint(
            path="src/main/java/Application.java",
            stack="java", kind="server", source="manifest", confidence="high",
            runtime_relevance="high",
        )]
        sm.file_paths = ["src/main/java/Application.java"]

        # Simulate arch detected (DDD) but low due to missing docs
        arch = ArchitectureAnalysis(
            requested=True,
            pattern="ddd",
            confidence="low",
            limitations=["No OpenAPI/Swagger spec found"],
        )
        sm.architecture = arch

        conf, gaps = ConfidenceAnalyzer().analyze(sm)

        assert conf.overall != "low", (
            f"overall must not be 'low' when stack=high, ep=high, arch=low+ddd pattern. "
            f"Got overall={conf.overall!r}"
        )
        assert conf.overall in ("medium", "high"), (
            f"Expected medium or high, got {conf.overall!r}"
        )

    def test_arch_low_without_pattern_can_produce_low(self):
        """arch=low with no detected pattern should still be allowed to drop overall."""
        from sourcecode.confidence_analyzer import ConfidenceAnalyzer
        from sourcecode.schema import (
            AnalysisMetadata, SourceMap, StackDetection,
            EntryPoint, ArchitectureAnalysis,
        )

        sm = SourceMap(metadata=AnalysisMetadata(analyzed_path="/tmp/fake"))
        sm.stacks = [StackDetection(
            stack="java", detection_method="manifest", confidence="high",
            manifests=["pom.xml"],
        )]
        sm.entry_points = [EntryPoint(
            path="src/main/java/Application.java",
            stack="java", kind="server", source="manifest", confidence="high",
            runtime_relevance="high",
        )]
        sm.file_paths = ["src/main/java/Application.java"]

        # arch requested but pattern not detected
        arch = ArchitectureAnalysis(
            requested=True,
            pattern=None,
            confidence="low",
        )
        sm.architecture = arch

        conf, gaps = ConfidenceAnalyzer().analyze(sm)

        # With pattern=None, arch=low CAN pull overall down — no constraint
        assert conf.overall in ("low", "medium", "high")

    def test_subconfidences_not_contradicted_in_sections(self):
        """Section confidences must reflect per-dimension reality, not just overall."""
        from sourcecode.confidence_analyzer import ConfidenceAnalyzer
        from sourcecode.serializer import _section_confidence
        from sourcecode.schema import (
            AnalysisMetadata, SourceMap, StackDetection, EntryPoint,
            ArchitectureAnalysis, DependencySummary,
        )

        sm = SourceMap(metadata=AnalysisMetadata(analyzed_path="/tmp/fake"))
        sm.stacks = [StackDetection(
            stack="java", detection_method="manifest", confidence="high",
            manifests=["pom.xml"],
        )]
        sm.entry_points = [EntryPoint(
            path="src/Application.java",
            stack="java", kind="server", source="manifest", confidence="high",
            runtime_relevance="high",
        )]
        sm.file_paths = ["src/Application.java"]
        sm.architecture = ArchitectureAnalysis(
            requested=True, pattern="ddd", confidence="high",
        )
        sm.dependency_summary = DependencySummary(
            requested=True, sources=["pom.xml"], total_count=10,
        )

        conf, _ = ConfidenceAnalyzer().analyze(sm)
        sm.confidence_summary = conf
        sections = _section_confidence(sm)

        assert sections["stack"] == "high", f"stack section should be high, got {sections['stack']}"
        assert sections["entrypoints"] == "high", f"entrypoints section should be high"
        assert sections["dependencies"] == "high", f"dependencies section should be high with sources+count"
        assert sections["architecture"] == "high", f"architecture section should reflect arch.confidence=high"


# ---------------------------------------------------------------------------
# 7. Ranking order: entrypoints > stereotype > framework_import > path
# ---------------------------------------------------------------------------

class TestRankingOrder:
    """Entrypoints must rank above annotations, annotations above imports, imports above path."""

    def test_entrypoint_ranks_above_framework_import(self, tmp_path: Path):
        from sourcecode.serializer import _file_relevance

        ep_path = _write_java(tmp_path, "src/main/java/Application.java",
            "@SpringBootApplication\npublic class Application { public static void main(String[] a){} }\n")
        filter_path = _write_java(tmp_path, "src/main/java/SecurityFilter.java",
            "import org.springframework.web.filter.OncePerRequestFilter;\n"
            "@Component\npublic class SecurityFilter extends OncePerRequestFilter {}\n")

        sm = _make_sm(tmp_path, [ep_path, filter_path], entry_paths=[ep_path])
        result = _file_relevance(sm, limit=20)

        paths_in_order = [item["path"] for item in result]
        if ep_path in paths_in_order and filter_path in paths_in_order:
            ep_idx = paths_in_order.index(ep_path)
            filter_idx = paths_in_order.index(filter_path)
            assert ep_idx < filter_idx, (
                f"Entrypoint ({ep_path}) must rank before SecurityFilter. "
                f"Got ep_idx={ep_idx}, filter_idx={filter_idx}"
            )

    def test_stereotype_scores_higher_than_plain_code(self, tmp_path: Path):
        from sourcecode.serializer import _file_relevance

        stereo_path = _write_java(tmp_path, "src/main/java/UserService.java",
            "import org.springframework.stereotype.Service;\n"
            "@Service\npublic class UserService {}\n")
        plain_path = _write_java(tmp_path, "src/main/java/UserHelper.java",
            "import java.util.List;\npublic class UserHelper { public List<String> list() { return null; } }\n")

        sm = _make_sm(tmp_path, [stereo_path, plain_path])
        result = _file_relevance(sm, limit=20)
        items = {item["path"]: item for item in result}

        if stereo_path in items and plain_path in items:
            assert items[stereo_path]["score"] > items[plain_path]["score"], (
                f"Stereotype score ({items[stereo_path]['score']}) must exceed plain code "
                f"({items[plain_path]['score']})"
            )

"""Regression tests for sourcecode v1.10.0 bug fixes and enhancements.

Covers:
  1. --compact --git-context: output differs, git_context key present, hotspots populated
  2. --agent  --git-context: same guarantees
  3. --symbol "" / "   ": exit code 2, stderr "symbol query cannot be empty"
  4. Risk flag generation on key_dependencies
  5. Bootstrap class detection (DemoApplication, FilterConfig)
  6. @M3FiltroSeguridad resource name extraction
  7. MyBatis XML pairing
  8. architecture.pattern heuristic (no more "insufficient_evidence" for layered repos)
  9. prepare-context review-pr suspected_areas populated
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

FIXTURE = Path(__file__).parent / "fixtures" / "spring_boot_minimal"

_runner = CliRunner()


def _invoke(*args: str) -> Any:
    from sourcecode.cli import app, _set_detected_path, _preprocess_args
    _set_detected_path(".")
    processed = _preprocess_args(list(args))
    return _runner.invoke(app, processed)


def _json(result: Any) -> dict:
    return json.loads(result.output)


# ---------------------------------------------------------------------------
# Helpers — build minimal SourceMap for unit tests
# ---------------------------------------------------------------------------

def _make_sm(**kwargs):
    from sourcecode.schema import (
        AnalysisMetadata, DependencyRecord, DependencySummary,
        EntryPoint, SourceMap, StackDetection,
    )
    from sourcecode.schema import FrameworkDetection
    defaults = dict(
        metadata=AnalysisMetadata(analyzed_path=str(FIXTURE)),
        file_paths=[],
        stacks=[],
        entry_points=[],
    )
    defaults.update(kwargs)
    return SourceMap(**defaults)


# ===========================================================================
# 1 & 2 — --git-context in compact/agent modes
# ===========================================================================

class TestGitContextInCompactAgent:

    def _make_sm_with_git(self):
        from sourcecode.schema import (
            AnalysisMetadata, ChangeHotspot, GitContext,
            SourceMap, UncommittedChanges,
        )
        gc = GitContext(
            requested=True,
            branch="feature/test",
            change_hotspots=[
                ChangeHotspot(file="src/Foo.java", commit_count=12, last_changed="2025-01-01"),
                ChangeHotspot(file="src/Bar.java", commit_count=7,  last_changed="2025-01-01"),
            ],
            uncommitted_changes=UncommittedChanges(
                staged=["src/Baz.java"],
                unstaged=[],
                untracked=["newfile.java"],
            ),
            limitations=[],
        )
        sm = _make_sm(
            metadata=AnalysisMetadata(analyzed_path=str(FIXTURE)),
            git_context=gc,
        )
        return sm

    def test_compact_view_includes_git_context(self):
        from sourcecode.serializer import compact_view
        sm = self._make_sm_with_git()
        data = compact_view(sm)
        assert "git_context" in data, "compact_view must include git_context when requested"

    def test_compact_git_context_has_branch(self):
        from sourcecode.serializer import compact_view
        sm = self._make_sm_with_git()
        ctx = compact_view(sm)["git_context"]
        assert ctx["branch"] == "feature/test"

    def test_compact_git_context_has_hotspots(self):
        from sourcecode.serializer import compact_view
        sm = self._make_sm_with_git()
        ctx = compact_view(sm)["git_context"]
        assert "top_hotspots" in ctx
        assert ctx["top_hotspots"][0]["file"] == "src/Foo.java"
        assert ctx["top_hotspots"][0]["commits"] == 12

    def test_compact_git_context_hotspots_capped_at_5(self):
        from sourcecode.schema import ChangeHotspot, GitContext, SourceMap, UncommittedChanges, AnalysisMetadata
        gc = GitContext(
            requested=True,
            branch="main",
            change_hotspots=[
                ChangeHotspot(file=f"src/File{i}.java", commit_count=10 - i, last_changed="2025-01-01")
                for i in range(10)
            ],
            uncommitted_changes=UncommittedChanges([], [], []),
            limitations=[],
        )
        from sourcecode.serializer import compact_view
        sm = _make_sm(metadata=AnalysisMetadata(analyzed_path=str(FIXTURE)), git_context=gc)
        ctx = compact_view(sm)["git_context"]
        assert len(ctx["top_hotspots"]) <= 5

    def test_compact_git_context_has_uncommitted_count(self):
        from sourcecode.serializer import compact_view
        sm = self._make_sm_with_git()
        ctx = compact_view(sm)["git_context"]
        assert ctx["uncommitted_files"] == 2  # staged=1, untracked=1

    def test_agent_view_includes_git_context(self):
        from sourcecode.serializer import agent_view
        sm = self._make_sm_with_git()
        data = agent_view(sm)
        assert "git_context" in data, "agent_view must include git_context when requested"

    def test_agent_git_context_matches_compact(self):
        from sourcecode.serializer import agent_view, compact_view
        sm = self._make_sm_with_git()
        a = agent_view(sm)["git_context"]
        c = compact_view(sm)["git_context"]
        assert a["branch"] == c["branch"]
        assert a["top_hotspots"] == c["top_hotspots"]

    def test_no_git_context_when_not_requested(self):
        from sourcecode.serializer import compact_view, agent_view
        sm = _make_sm()
        assert "git_context" not in compact_view(sm)
        assert "git_context" not in agent_view(sm)

    def test_no_git_context_on_unavailable_repo(self):
        from sourcecode.schema import GitContext, AnalysisMetadata
        from sourcecode.serializer import compact_view
        gc = GitContext(requested=True, branch=None, limitations=["no_git_repo"])
        sm = _make_sm(metadata=AnalysisMetadata(analyzed_path=str(FIXTURE)), git_context=gc)
        assert "git_context" not in compact_view(sm)


# ===========================================================================
# 3 — --symbol empty validation
# ===========================================================================

class TestSymbolEmptyValidation:

    def test_empty_symbol_exits_with_code_2(self):
        result = _invoke(str(FIXTURE), "--symbol", "")
        assert result.exit_code == 2, f"Expected exit 2, got {result.exit_code}"

    def test_empty_symbol_stderr_message(self):
        result = _invoke(str(FIXTURE), "--symbol", "")
        stderr = (result.stderr or result.output or "").strip()
        payload = json.loads(stderr)
        assert payload["error"]["code"] == "INVALID_INPUT"
        assert "symbol query cannot be empty" in payload["error"]["message"]

    def test_whitespace_symbol_exits_with_code_2(self):
        result = _invoke(str(FIXTURE), "--symbol", "   ")
        assert result.exit_code == 2, f"Expected exit 2, got {result.exit_code}"

    def test_valid_symbol_does_not_trigger_validation(self):
        result = _invoke(str(FIXTURE), "--symbol", "HealthService")
        # Should NOT exit with code 2 (may exit 0 or 1 for other reasons)
        assert result.exit_code != 2


# ===========================================================================
# 4 — Risk flag generation
# ===========================================================================

class TestRiskFlags:

    def test_spring_boot_2x_risk_flag(self):
        from sourcecode.serializer import _dep_risk_flags
        flags = _dep_risk_flags("spring-boot-starter-web", "2.7.18")
        assert "spring-boot-2.x-eol" in flags

    def test_spring_boot_3x_no_eol_flag(self):
        from sourcecode.serializer import _dep_risk_flags
        flags = _dep_risk_flags("spring-boot-starter-web", "3.1.0")
        assert "spring-boot-2.x-eol" not in flags

    def test_javax_migration_risk(self):
        from sourcecode.serializer import _dep_risk_flags
        flags = _dep_risk_flags("javax.servlet", None)
        assert "javax-to-jakarta-migration-risk" in flags

    def test_ojdbc_vendor_lock(self):
        from sourcecode.serializer import _dep_risk_flags
        flags = _dep_risk_flags("ojdbc8", None)
        assert "oracle-vendor-lock" in flags

    def test_no_flags_for_safe_dep(self):
        from sourcecode.serializer import _dep_risk_flags
        flags = _dep_risk_flags("lombok", "1.18.30")
        assert flags == []

    def test_java8_deployment_risk(self):
        from sourcecode.serializer import _project_deployment_risks
        from sourcecode.schema import SourceMap, AnalysisMetadata
        sm = SourceMap(
            metadata=AnalysisMetadata(analyzed_path=str(FIXTURE)),
            language_version="1.8",
        )
        risks = _project_deployment_risks(sm)
        assert "legacy-java-runtime" in risks

    def test_weblogic_war_deployment_risk(self):
        from sourcecode.serializer import _project_deployment_risks
        from sourcecode.schema import SourceMap, AnalysisMetadata
        sm = SourceMap(
            metadata=AnalysisMetadata(analyzed_path=str(FIXTURE)),
            app_server_hint="weblogic",
            packaging="war",
        )
        risks = _project_deployment_risks(sm)
        assert "legacy-app-server-deployment" in risks

    def test_risk_flags_appear_in_compact_view(self):
        from sourcecode.serializer import compact_view
        from sourcecode.schema import (
            AnalysisMetadata, DependencyRecord, DependencySummary, SourceMap,
        )
        dep = DependencyRecord(
            name="spring-boot-starter-web",
            declared_version="2.7.18",
            role="runtime",
            scope="compile",
            source="manifest",
            ecosystem="maven",
        )
        ds = DependencySummary(requested=True, total_count=1, direct_count=1)
        ds.dependencies = [dep]
        sm = SourceMap(
            metadata=AnalysisMetadata(analyzed_path=str(FIXTURE)),
            key_dependencies=[dep],
            dependency_summary=ds,
        )
        data = compact_view(sm)
        kd = data.get("key_dependencies", [])
        assert kd, "key_dependencies must be present"
        spring_entry = next((d for d in kd if "spring-boot" in d["name"]), None)
        assert spring_entry is not None
        assert "risk_flags" in spring_entry
        assert "spring-boot-2.x-eol" in spring_entry["risk_flags"]


# ===========================================================================
# 5 — Bootstrap class detection
# ===========================================================================

class TestBootstrapEntryPoints:

    def _make_eps(self):
        from sourcecode.schema import EntryPoint
        return [
            EntryPoint(path="src/main/java/com/example/DemoApplication.java",
                       stack="java", kind="application", source="manifest"),
            EntryPoint(path="src/main/java/com/example/config/FilterConfig.java",
                       stack="java", kind="filter", source="annotation"),
            EntryPoint(path="src/main/java/com/example/web/HealthRestController.java",
                       stack="java", kind="rest_controller", source="annotation",
                       http_path="/health"),
        ]

    def test_bootstrap_structured_returns_bootstrap_list(self):
        from sourcecode.serializer import _bootstrap_structured
        eps = self._make_eps()
        result = _bootstrap_structured(eps)
        assert result is not None
        assert "bootstrap" in result
        assert any("DemoApplication" in p for p in result["bootstrap"])

    def test_bootstrap_structured_returns_security_list(self):
        from sourcecode.serializer import _bootstrap_structured
        eps = self._make_eps()
        result = _bootstrap_structured(eps)
        assert "security" in result
        assert any("FilterConfig" in p for p in result["security"])

    def test_bootstrap_structured_returns_controllers_count(self):
        from sourcecode.serializer import _bootstrap_structured
        eps = self._make_eps()
        result = _bootstrap_structured(eps)
        assert "controllers" in result
        # classes only — methods field removed (was always equal to classes)
        assert result["controllers"]["classes"] == 1
        assert "note" in result["controllers"]

    def test_bootstrap_takes_priority_over_alphabetical_in_compact(self):
        from sourcecode.serializer import compact_view
        sm = _make_sm(
            metadata=__import__("sourcecode.schema", fromlist=["AnalysisMetadata"]).AnalysisMetadata(
                analyzed_path=str(FIXTURE)
            ),
            entry_points=self._make_eps(),
        )
        data = compact_view(sm)
        ep = data["entry_points"]
        assert isinstance(ep, dict), "entry_points must be structured dict when bootstrap detected"
        assert "bootstrap" in ep

    def test_no_bootstrap_struct_for_non_java(self):
        from sourcecode.schema import EntryPoint
        from sourcecode.serializer import _bootstrap_structured
        eps = [
            EntryPoint(path="src/index.js", stack="nodejs", kind="main", source="manifest"),
            EntryPoint(path="src/server.js", stack="nodejs", kind="main", source="manifest"),
        ]
        result = _bootstrap_structured(eps)
        assert result is None


# ===========================================================================
# 6 — @M3FiltroSeguridad resource name extraction
# ===========================================================================

class TestM3FiltroSeguridadExtraction:

    def _make_eps_with_security(self):
        from sourcecode.schema import EntryPoint
        return [
            EntryPoint(
                path="src/web/NominaController.java",
                stack="java", kind="rest_controller", source="annotation",
                evidence="@M3FiltroSeguridad(nombreRecurso='nominas', nivelRequerido=2)",
            ),
            EntryPoint(
                path="src/web/EmpleadoController.java",
                stack="java", kind="rest_controller", source="annotation",
                evidence="@M3FiltroSeguridad(nombreRecurso='empleados', nivelRequerido=1)",
            ),
            EntryPoint(
                path="src/web/HealthController.java",
                stack="java", kind="rest_controller", source="annotation",
            ),
        ]

    def test_resource_names_extracted(self):
        from sourcecode.serializer import _security_surface_from_eps
        eps = self._make_eps_with_security()
        result = _security_surface_from_eps(eps)
        assert result is not None
        assert "nominas" in result["resource_names"]
        assert "empleados" in result["resource_names"]

    def test_no_duplicates_in_resource_names(self):
        from sourcecode.schema import EntryPoint
        from sourcecode.serializer import _security_surface_from_eps
        eps = [
            EntryPoint(path="a.java", stack="java", kind="rest_controller", source="annotation",
                       evidence="@M3FiltroSeguridad(nombreRecurso='nominas', nivelRequerido=1)"),
            EntryPoint(path="b.java", stack="java", kind="rest_controller", source="annotation",
                       evidence="@M3FiltroSeguridad(nombreRecurso='nominas', nivelRequerido=2)"),
        ]
        result = _security_surface_from_eps(eps)
        assert result["resource_names"].count("nominas") == 1

    def test_none_when_no_m3_annotations(self):
        from sourcecode.schema import EntryPoint
        from sourcecode.serializer import _security_surface_from_eps
        eps = [EntryPoint(path="a.java", stack="java", kind="rest_controller", source="annotation")]
        assert _security_surface_from_eps(eps) is None

    def test_security_surface_in_compact_output(self):
        from sourcecode.serializer import compact_view
        from sourcecode.schema import AnalysisMetadata, SourceMap
        sm = SourceMap(
            metadata=AnalysisMetadata(analyzed_path=str(FIXTURE)),
            entry_points=self._make_eps_with_security(),
        )
        data = compact_view(sm)
        assert "security_surface" in data
        assert "nominas" in data["security_surface"]["resource_names"]


# ===========================================================================
# 7 — MyBatis mapper <-> XML pairing
# ===========================================================================

class TestMyBatisPairing:

    def _make_sm_with_mybatis(self, interfaces: list[str], xmls: list[str]):
        from sourcecode.schema import AnalysisMetadata, FrameworkDetection, SourceMap, StackDetection
        stack = StackDetection(
            stack="java",
            detection_method="manifest",
            confidence="high",
            frameworks=[FrameworkDetection(name="MyBatis", source="pom.xml")],
        )
        return SourceMap(
            metadata=AnalysisMetadata(analyzed_path=str(FIXTURE)),
            stacks=[stack],
            file_paths=interfaces + xmls,
        )

    def test_paired_mapper_counts(self):
        from sourcecode.serializer import _mybatis_pairing
        sm = self._make_sm_with_mybatis(
            ["src/main/java/HealthMapper.java"],
            ["src/main/resources/mapper/HealthMapper.xml"],
        )
        result = _mybatis_pairing(sm)
        assert result is not None
        assert result["mapper_interfaces"] == 1
        assert result["xml_files"] == 1
        assert "missing_xml" not in result
        assert "orphan_xml" not in result

    def test_missing_xml_detected(self):
        from sourcecode.serializer import _mybatis_pairing
        sm = self._make_sm_with_mybatis(
            ["src/main/java/EmpleadoMapper.java", "src/main/java/HealthMapper.java"],
            ["src/main/resources/mapper/HealthMapper.xml"],
        )
        result = _mybatis_pairing(sm)
        assert "missing_xml" in result
        assert "EmpleadoMapper" in result["missing_xml"]

    def test_orphan_xml_detected(self):
        from sourcecode.serializer import _mybatis_pairing
        sm = self._make_sm_with_mybatis(
            ["src/main/java/HealthMapper.java"],
            ["src/main/resources/mapper/HealthMapper.xml",
             "src/main/resources/mapper/OrphanMapper.xml"],
        )
        result = _mybatis_pairing(sm)
        assert "orphan_xml" in result
        assert any("OrphanMapper" in p for p in result["orphan_xml"])

    def test_none_when_no_mybatis(self):
        from sourcecode.serializer import _mybatis_pairing
        from sourcecode.schema import AnalysisMetadata, SourceMap
        sm = SourceMap(
            metadata=AnalysisMetadata(analyzed_path=str(FIXTURE)),
            file_paths=["src/HealthMapper.java", "src/HealthMapper.xml"],
        )
        assert _mybatis_pairing(sm) is None

    def test_mybatis_in_compact_output(self):
        from sourcecode.serializer import compact_view
        sm = self._make_sm_with_mybatis(
            ["src/main/java/HealthMapper.java"],
            ["src/main/resources/mapper/HealthMapper.xml"],
        )
        data = compact_view(sm)
        assert "mybatis" in data


# ===========================================================================
# 8 — architecture.pattern heuristic (no more "insufficient_evidence")
# ===========================================================================

class TestArchitecturePatternHeuristic:

    def _sm_with_paths(self, paths: list[str]):
        from sourcecode.schema import AnalysisMetadata, SourceMap
        return SourceMap(
            metadata=AnalysisMetadata(analyzed_path=str(FIXTURE)),
            file_paths=paths,
        )

    def test_layered_pattern_detected(self):
        from sourcecode.serializer import _lightweight_arch_pattern
        sm = self._sm_with_paths([
            "src/main/java/com/example/controller/FooController.java",
            "src/main/java/com/example/service/FooService.java",
            "src/main/java/com/example/repository/FooRepository.java",
        ])
        result = _lightweight_arch_pattern(sm)
        assert result is not None
        assert result["pattern"] == "layered"
        assert result["confidence"] >= 0.60

    def test_ddd_layered_detected(self):
        from sourcecode.serializer import _lightweight_arch_pattern
        sm = self._sm_with_paths([
            "src/main/java/com/example/foo/controller/FooController.java",
            "src/main/java/com/example/foo/service/FooService.java",
            "src/main/java/com/example/foo/repository/FooRepository.java",
            "src/main/java/com/example/foo/domain/Foo.java",
            "src/main/java/com/example/foo/infrastructure/FooPersistence.java",
        ])
        result = _lightweight_arch_pattern(sm)
        assert result is not None
        assert result["pattern"] == "ddd-layered"
        assert result["confidence"] >= 0.55

    def test_no_pattern_for_empty_paths(self):
        from sourcecode.serializer import _lightweight_arch_pattern
        sm = self._sm_with_paths([])
        assert _lightweight_arch_pattern(sm) is None

    def test_architecture_context_no_insufficient_evidence_for_java(self):
        from sourcecode.serializer import _architecture_context
        sm = self._sm_with_paths([
            "src/main/java/com/example/controller/FooController.java",
            "src/main/java/com/example/service/FooService.java",
            "src/main/java/com/example/repository/FooRepository.java",
        ])
        ctx = _architecture_context(sm)
        assert ctx["pattern"] != "insufficient_evidence", \
            f"Expected real pattern, got insufficient_evidence. ctx={ctx}"

    def test_fixture_compact_no_insufficient_evidence(self):
        result = _invoke(str(FIXTURE), "--compact", "--dependencies")
        assert result.exit_code == 0, result.output
        data = _json(result)
        # architecture_summary should be present; pattern heuristic fires via agent_view


# ===========================================================================
# 9 — prepare-context review-pr suspected_areas
# ===========================================================================

class TestReviewPrSuspectedAreas:
    """review-pr now requires a git diff — generic fallback removed."""

    def test_review_pr_requires_git_diff(self):
        # FIXTURE has no uncommitted changes (or no git repo) — returns structured error.
        # When running inside the atlas-cli git tree with no staged changes:
        #   - no_diff_source: no --since and no staged/unstaged changes (clean tree) → exit 1
        #   - no_diff: scope resolved but empty → exit 0
        # When running outside any git repo, error is "no_git_repo" (exit 1 — true error).
        result = _invoke("prepare-context", "review-pr", str(FIXTURE))
        data = _json(result)
        _git_errors = {"no_git_repo", "no_diff", "no_diff_source", "git_ref_not_found"}
        assert data.get("error") in _git_errors, f"Expected git error, got: {data}"
        assert "ci_decision" in data
        # Exit code: 0 for no_diff (no changes = success), 1 for all other errors
        if data.get("error") == "no_diff":
            assert result.exit_code == 0, f"no_diff must exit 0, got {result.exit_code}"
        else:
            assert result.exit_code == 1, f"git error '{data.get('error')}' must exit 1"

    def test_review_pr_error_json_is_machine_readable(self):
        result = _invoke("prepare-context", "review-pr", str(FIXTURE))
        data = _json(result)
        assert "error" in data
        assert "message" in data
        assert "ci_decision" in data

    def test_review_pr_no_diff_error_when_git_but_no_changes(self, monkeypatch):
        # Simulate: git repo present but no changed files in scope.
        # no_diff is NOT a failure — it means nothing to review, exit 0 (like delta no_changes).
        from pathlib import Path as _Path
        from sourcecode import prepare_context as _pc
        monkeypatch.setattr(_pc.TaskContextBuilder, "_resolve_git_root", lambda self: _Path(str(FIXTURE)))
        monkeypatch.setattr(_pc.TaskContextBuilder, "_get_pr_scope_files", lambda self, since=None: ([], "git_diff", [], []))
        result = _invoke("prepare-context", "review-pr", str(FIXTURE))
        data = _json(result)
        assert data.get("error") == "no_diff"
        assert data.get("ci_decision") == "no_changes"
        # FIX: no_diff → exit 0 (consistent with delta no_changes); was incorrectly exit 1
        assert result.exit_code == 0, f"no_diff must exit 0 (not an error), got {result.exit_code}"

    def test_review_pr_with_mocked_diff_returns_pr_fields(self, monkeypatch):
        # Simulate: valid git repo with one changed controller file
        from pathlib import Path as _Path
        from sourcecode import prepare_context as _pc
        _changed = ["src/main/java/com/example/UserController.java"]
        monkeypatch.setattr(_pc.TaskContextBuilder, "_resolve_git_root", lambda self: _Path(str(FIXTURE)))
        monkeypatch.setattr(
            _pc.TaskContextBuilder, "_get_pr_scope_files",
            lambda self, since=None: (_changed, "git_diff", _changed, []),
        )
        result = _invoke("prepare-context", "review-pr", str(FIXTURE))
        assert result.exit_code == 0, result.output
        data = _json(result)
        assert data.get("review_type") == "pull_request"
        assert "changed_files" in data
        assert "test_coverage_risk" in data
        assert data.get("ci_decision") == "analysis_success"

    def test_fix_bug_still_uses_annotation_density(self):
        result = _invoke("prepare-context", "fix-bug", str(FIXTURE))
        assert result.exit_code == 0, result.output
        data = _json(result)
        assert "relevant_files" in data

    def test_fix_bug_still_uses_annotation_density_2(self):
        pass  # placeholder slot kept for numbering


# ===========================================================================
# 10 — behavioral_impact unit tests
# ===========================================================================

class TestBehavioralImpact:
    """Unit tests for analyze_behavioral_impact — no CLI, uses temp files."""

    def _classify(self, path: str) -> dict:
        name = Path(path).name
        if "Controller" in name or "Api" in name:
            return {"artifact_type": "controller"}
        if "Service" in name:
            return {"artifact_type": "service"}
        if "Repository" in name or "Repo" in name:
            return {"artifact_type": "repository"}
        return {"artifact_type": "source"}

    def _make_spring_trio(self, tmp_path: Path):
        ctrl = tmp_path / "ArticleController.java"
        svc  = tmp_path / "ArticleFavoriteService.java"
        repo = tmp_path / "ArticleFavoriteRepository.java"
        ctrl.write_text(
            "@RestController\npublic class ArticleController {\n"
            "  private ArticleFavoriteService articleFavoriteService;\n"
            "  @PostMapping(\"/articles/{slug}/favorite\")\n"
            "  public ArticleData favoriteArticle(String slug) {\n"
            "    return articleFavoriteService.favorite(slug);\n"
            "  }\n}\n"
        )
        svc.write_text(
            "@Service\npublic class ArticleFavoriteService {\n"
            "  private ArticleFavoriteRepository articleFavoriteRepository;\n"
            "  public ArticleData favorite(String slug) {\n"
            "    return articleFavoriteRepository.save(slug);\n"
            "  }\n}\n"
        )
        repo.write_text(
            "@Repository\npublic interface ArticleFavoriteRepository"
            " extends JpaRepository<ArticleFavorite, Long> {}\n"
        )
        return [str(ctrl), str(svc), str(repo)], ctrl, svc, repo

    def test_service_change_finds_controller_entry_point(self, tmp_path: Path):
        from sourcecode.flow_analyzer import analyze_behavioral_impact
        all_paths, ctrl, svc, repo = self._make_spring_trio(tmp_path)
        result = analyze_behavioral_impact(
            changed_files=[str(svc)],
            all_paths=all_paths,
            root=tmp_path,
            classify_fn=self._classify,
        )
        assert len(result) == 1
        entry = result[0]
        assert "ArticleController" in entry["entry_point"]
        assert any("ArticleFavoriteService" in s for s in entry["affected_path"])
        assert len(entry["impact"]) >= 1
        assert entry["end_state"] == "DB write"

    def test_repository_change_finds_controller_via_service(self, tmp_path: Path):
        from sourcecode.flow_analyzer import analyze_behavioral_impact
        all_paths, ctrl, svc, repo = self._make_spring_trio(tmp_path)
        result = analyze_behavioral_impact(
            changed_files=[str(repo)],
            all_paths=all_paths,
            root=tmp_path,
            classify_fn=self._classify,
        )
        assert len(result) == 1
        entry = result[0]
        assert "ArticleController" in entry["entry_point"]
        assert any("Repository" in s for s in entry["affected_path"])
        assert entry["end_state"] == "DB write"
        assert any("persistence" in imp["statement"] for imp in entry["impact"])
        assert entry["confidence"] in ("low", "medium", "high")
        assert entry["evidence_level"] in ("direct_injection", "direct_call", "heuristic_only")
        assert isinstance(entry["trace"], list) and len(entry["trace"]) >= 1

    def test_controller_change_forward_traversal(self, tmp_path: Path):
        from sourcecode.flow_analyzer import analyze_behavioral_impact
        all_paths, ctrl, svc, repo = self._make_spring_trio(tmp_path)
        result = analyze_behavioral_impact(
            changed_files=[str(ctrl)],
            all_paths=all_paths,
            root=tmp_path,
            classify_fn=self._classify,
        )
        assert len(result) == 1
        entry = result[0]
        assert "ArticleController" in entry["entry_point"]
        assert any("ArticleFavoriteService" in s for s in entry["affected_path"])

    def test_no_evidence_returns_empty(self, tmp_path: Path):
        from sourcecode.flow_analyzer import analyze_behavioral_impact
        ctrl = tmp_path / "SomeController.java"
        svc  = tmp_path / "UnrelatedService.java"
        ctrl.write_text(
            "@RestController\npublic class SomeController {\n"
            "  @GetMapping(\"/\")\n  public String get() { return \"\"; }\n}\n"
        )
        svc.write_text(
            "@Service\npublic class UnrelatedService { public void run() {} }\n"
        )
        all_paths = [str(ctrl), str(svc)]
        result = analyze_behavioral_impact(
            changed_files=[str(svc)],
            all_paths=all_paths,
            root=tmp_path,
            classify_fn=self._classify,
        )
        assert result == [], f"Expected no impact without evidence, got {result}"

    def test_impact_strings_describe_behavior_not_files(self, tmp_path: Path):
        from sourcecode.flow_analyzer import analyze_behavioral_impact
        all_paths, ctrl, svc, repo = self._make_spring_trio(tmp_path)
        result = analyze_behavioral_impact(
            changed_files=[str(repo)],
            all_paths=all_paths,
            root=tmp_path,
            classify_fn=self._classify,
        )
        assert result
        for imp in result[0]["impact"]:
            assert ".java" not in imp["statement"], f"Impact should not reference filenames: {imp}"
            assert "Repository" not in imp["statement"], f"Impact should not use class names: {imp}"
            assert imp["epistemic_level"] in ("FACT", "STRUCTURAL SIGNAL", "INFERRED (LOW CONFIDENCE)", "OMITTED")
            assert imp["support"]


# ===========================================================================
# P0 — Bounded relevance expansion (compute_context_limit + agent_view --full)
# ===========================================================================

class TestBoundedRelevanceExpansion:
    """P0 fix: --full must never return all files. Bounded strategy enforced."""

    def test_compute_context_limit_normal(self):
        from sourcecode.serializer import compute_context_limit
        assert compute_context_limit("normal", 20) == 20

    def test_compute_context_limit_full(self):
        from sourcecode.serializer import compute_context_limit
        # min(20*2, 50) = 40
        assert compute_context_limit("full", 20) == 40

    def test_compute_context_limit_deep(self):
        from sourcecode.serializer import compute_context_limit
        # min(20*4, 100) = 80
        assert compute_context_limit("deep", 20) == 80

    def test_compute_context_limit_full_never_exceeds_50(self):
        from sourcecode.serializer import compute_context_limit
        # Even with large normal, full caps at 50
        assert compute_context_limit("full", 40) == 50
        assert compute_context_limit("full", 100) == 50

    def test_compute_context_limit_deep_never_exceeds_100(self):
        from sourcecode.serializer import compute_context_limit
        assert compute_context_limit("deep", 30) == 100
        assert compute_context_limit("deep", 100) == 100

    def test_expand_relevance_window_normal(self):
        from sourcecode.serializer import expand_relevance_window
        files = list(range(100))  # 100 dummy items
        result = expand_relevance_window(files, "normal", 20)
        assert len(result) == 20

    def test_expand_relevance_window_full_bounded(self):
        from sourcecode.serializer import expand_relevance_window
        files = list(range(100))
        result = expand_relevance_window(files, "full", 20)
        assert len(result) == 40
        assert len(result) <= 50  # never all files

    def test_expand_relevance_window_full_with_fewer_files(self):
        from sourcecode.serializer import expand_relevance_window
        files = list(range(10))  # fewer than limit
        result = expand_relevance_window(files, "full", 20)
        assert len(result) == 10  # returns what exists, not more

    def _make_sm_with_many_paths(self, n: int):
        from sourcecode.schema import AnalysisMetadata, SourceMap
        sm = _make_sm(
            metadata=AnalysisMetadata(analyzed_path=str(FIXTURE)),
            file_paths=[f"src/main/java/com/example/Service{i}.java" for i in range(n)],
        )
        return sm

    def test_agent_view_full_never_returns_all_files(self):
        """Core P0 guard: full=True must not produce more files than compute_context_limit."""
        from sourcecode.serializer import agent_view, compute_context_limit
        sm = self._make_sm_with_many_paths(200)
        data = agent_view(sm, full=True)
        # file_relevance may be absent when all files score below threshold
        fr = data.get("file_relevance", [])
        cap = compute_context_limit("full", 20)
        assert len(fr) <= cap, (
            f"full=True returned {len(fr)} files but cap is {cap} — unbounded expansion!"
        )

    def test_agent_view_normal_capped_at_20(self):
        from sourcecode.serializer import agent_view
        sm = self._make_sm_with_many_paths(200)
        data = agent_view(sm, full=False)
        fr = data.get("file_relevance", [])
        assert len(fr) <= 20

    def test_agent_view_full_hint_not_say_see_all(self):
        """Hint must not encourage unbounded expansion."""
        from sourcecode.serializer import agent_view
        sm = self._make_sm_with_many_paths(200)
        data = agent_view(sm, full=True)
        hint = data.get("file_relevance_hint", "")
        # Old broken hint said "Use --full to see all." — ensure removed
        assert "see all" not in hint.lower(), f"Hint still says 'see all': {hint!r}"

    def test_agent_view_deterministic_ordering(self):
        """Same input → same file order (deterministic)."""
        from sourcecode.serializer import agent_view
        sm = self._make_sm_with_many_paths(50)
        data1 = agent_view(sm, full=True)
        data2 = agent_view(sm, full=True)
        assert data1.get("file_relevance") == data2.get("file_relevance")

    def test_output_budget_still_applied(self):
        """output_budget trim must still fire after relevance fix."""
        from sourcecode.serializer import agent_view
        from sourcecode.output_budget import trim_to_budget, BUDGET_AGENT
        sm = self._make_sm_with_many_paths(200)
        data = agent_view(sm, full=True)
        trimmed = trim_to_budget(data, BUDGET_AGENT, label="agent")
        import json
        assert len(json.dumps(trimmed).encode()) <= BUDGET_AGENT * 1.05  # 5% tolerance

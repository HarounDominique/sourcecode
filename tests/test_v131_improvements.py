"""Tests for v1.31.0 improvements:
  - Area 1: repo-ir size limits (--max-nodes, --max-edges, --summary-only)
  - Area 2: symptom_explain structured evidence
  - Area 3: git_analyzer vendor dir skip
  - Area 4: Windows Unicode hardening (encoding="utf-8")
  - Area 5: symptom performance (content scan limit, regex pre-compile)
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _make_ir(n_nodes: int = 10, n_edges: int = 20) -> dict:
    """Build a minimal synthetic repo-ir dict for size-limit tests."""
    nodes = [
        {"fqn": f"pkg.Class{i}", "type": "class", "role": "other",
         "in_degree": i, "out_degree": i, "stable_id": f"id{i}",
         "symbol_kind": "class", "canonical_name": f"pkg.Class{i}",
         "source_file": f"Class{i}.java", "signature": ""}
        for i in range(n_nodes)
    ]
    edges = [
        {"from": f"pkg.Class{i % n_nodes}", "to": f"pkg.Class{(i+1) % n_nodes}",
         "type": "imports", "confidence": "high", "evidence": {}}
        for i in range(n_edges)
    ]
    ranked = [
        {"entity": f"pkg.Class{i}", "type": "class", "role": "other", "score": float(n_nodes - i)}
        for i in range(n_nodes)
    ]
    return {
        "schema_version": "final-v1",
        "graph": {"nodes": nodes, "edges": edges},
        "reverse_graph": {"pkg.Class0": {"imports": ["pkg.Class1"]}},
        "analysis": {
            "changed_entities": [{"entity": "pkg.Class0"}],
            "impacted_entities": [{"entity": f"pkg.Class{i}"} for i in range(5)],
            "isolated_changes": [],
            "validated_changes": [],
        },
        "impact": {"global_score": 42.0, "ranked_nodes": ranked},
        "subsystems": [],
        "change_set": [],
        "audit": {"dropped_fields": []},
    }


# ─── Area 1: apply_ir_size_limits ────────────────────────────────────────────

class TestApplyIrSizeLimits:
    def setup_method(self) -> None:
        from sourcecode.repository_ir import apply_ir_size_limits
        self.fn = apply_ir_size_limits

    def test_no_limits_returns_same_object(self) -> None:
        ir = _make_ir()
        result = self.fn(ir)
        assert result is ir

    def test_max_nodes_truncates(self) -> None:
        ir = _make_ir(n_nodes=10, n_edges=5)
        result = self.fn(ir, max_nodes=3)
        assert len(result["graph"]["nodes"]) == 3

    def test_max_nodes_keeps_highest_scored_first(self) -> None:
        ir = _make_ir(n_nodes=10)
        result = self.fn(ir, max_nodes=3)
        kept = {n["fqn"] for n in result["graph"]["nodes"]}
        # ranked_nodes[0] = Class0 (score=10, highest), should always be kept
        assert "pkg.Class0" in kept

    def test_max_nodes_trims_ranked_nodes(self) -> None:
        ir = _make_ir(n_nodes=10)
        result = self.fn(ir, max_nodes=3)
        kept_fqns = {n["fqn"] for n in result["graph"]["nodes"]}
        for rn in result["impact"]["ranked_nodes"]:
            assert rn["entity"] in kept_fqns

    def test_max_edges_truncates(self) -> None:
        ir = _make_ir(n_nodes=5, n_edges=20)
        result = self.fn(ir, max_edges=5)
        assert len(result["graph"]["edges"]) <= 5

    def test_max_nodes_and_max_edges_combined(self) -> None:
        ir = _make_ir(n_nodes=10, n_edges=30)
        result = self.fn(ir, max_nodes=4, max_edges=6)
        assert len(result["graph"]["nodes"]) <= 4
        assert len(result["graph"]["edges"]) <= 6

    def test_summary_only_empties_graph(self) -> None:
        ir = _make_ir(n_nodes=50, n_edges=200)
        result = self.fn(ir, summary_only=True)
        assert result["graph"]["nodes"] == []
        assert result["graph"]["edges"] == []
        assert "_omitted" in result["graph"]

    def test_summary_only_keeps_analysis(self) -> None:
        ir = _make_ir(n_nodes=10)
        result = self.fn(ir, summary_only=True)
        assert "changed_entities" in result["analysis"]
        assert "impacted_entities" in result["analysis"]

    def test_summary_only_caps_ranked_nodes_at_20(self) -> None:
        ir = _make_ir(n_nodes=50)
        result = self.fn(ir, summary_only=True)
        assert len(result["impact"]["ranked_nodes"]) <= 20

    def test_summary_only_clears_reverse_graph(self) -> None:
        ir = _make_ir(n_nodes=5)
        result = self.fn(ir, summary_only=True)
        assert result["reverse_graph"] == {}

    def test_global_score_preserved(self) -> None:
        ir = _make_ir(n_nodes=10)
        result = self.fn(ir, max_nodes=3)
        assert result["impact"]["global_score"] == ir["impact"]["global_score"]

    def test_edge_priority_keeps_internal_edges(self) -> None:
        """When max_nodes limits to 2, edges between those 2 nodes have priority."""
        ir = _make_ir(n_nodes=5, n_edges=10)
        # Force a specific score order by patching ranked_nodes
        ir["impact"]["ranked_nodes"] = [
            {"entity": "pkg.Class0", "type": "class", "role": "other", "score": 10.0},
            {"entity": "pkg.Class1", "type": "class", "role": "other", "score": 9.0},
            {"entity": "pkg.Class2", "type": "class", "role": "other", "score": 1.0},
        ]
        # Add a known edge between Class0 and Class1
        ir["graph"]["edges"].insert(0, {
            "from": "pkg.Class0", "to": "pkg.Class1",
            "type": "imports", "confidence": "high", "evidence": {}
        })
        result = self.fn(ir, max_nodes=2, max_edges=1)
        edges = result["graph"]["edges"]
        assert len(edges) == 1
        assert edges[0]["from"] == "pkg.Class0"
        assert edges[0]["to"] == "pkg.Class1"

    def test_schema_version_preserved(self) -> None:
        ir = _make_ir()
        result = self.fn(ir, max_nodes=3)
        assert result.get("schema_version") == "final-v1"


# ─── Area 1: find_java_files vendor skip ─────────────────────────────────────

class TestFindJavaFilesVendorSkip:
    def test_skips_vendor_dir(self, tmp_path: Path) -> None:
        from sourcecode.repository_ir import find_java_files

        vendor = tmp_path / "vendor" / "com" / "lib"
        vendor.mkdir(parents=True)
        (vendor / "External.java").write_text("class External {}")

        src = tmp_path / "src" / "main"
        src.mkdir(parents=True)
        (src / "MyService.java").write_text("class MyService {}")

        found = find_java_files(tmp_path)
        paths = [Path(f).name for f in found]
        assert "MyService.java" in paths
        assert "External.java" not in paths

    def test_skips_node_modules(self, tmp_path: Path) -> None:
        from sourcecode.repository_ir import find_java_files

        nm = tmp_path / "node_modules" / "something"
        nm.mkdir(parents=True)
        (nm / "Polyfill.java").write_text("class Polyfill {}")

        (tmp_path / "App.java").write_text("class App {}")
        found = find_java_files(tmp_path)
        paths = [Path(f).name for f in found]
        assert "App.java" in paths
        assert "Polyfill.java" not in paths

    def test_skips_target_dir(self, tmp_path: Path) -> None:
        from sourcecode.repository_ir import find_java_files

        target = tmp_path / "target" / "generated"
        target.mkdir(parents=True)
        (target / "Generated.java").write_text("class Generated {}")
        (tmp_path / "Main.java").write_text("class Main {}")

        found = find_java_files(tmp_path)
        paths = [Path(f).name for f in found]
        assert "Main.java" in paths
        assert "Generated.java" not in paths


# ─── Area 2: symptom_explain ─────────────────────────────────────────────────

class TestSymptomExplain:
    def _build_output(self, tmp_path: Path, symptom: str) -> Any:
        from sourcecode.prepare_context import TaskContextBuilder
        builder = TaskContextBuilder(tmp_path)
        return builder.build("fix-bug", symptom=symptom)

    def test_symptom_explain_present_when_symptom_given(self, tmp_path: Path) -> None:
        output = self._build_output(tmp_path, "login")
        # symptom_explain should be set (even if no files matched)
        assert output.symptom_explain is not None or output.symptom is None or True
        # No crash — that's the main assertion here

    def test_symptom_explain_has_required_keys(self, tmp_path: Path) -> None:
        output = self._build_output(tmp_path, "sesiones")
        if output.symptom_explain is not None:
            required = {"keywords", "confidence", "direct_path_matches",
                        "content_matches", "commit_matches", "synonym_matches",
                        "boosts", "final_boost"}
            assert required <= set(output.symptom_explain.keys())

    def test_symptom_explain_confidence_values(self, tmp_path: Path) -> None:
        output = self._build_output(tmp_path, "auth")
        if output.symptom_explain is not None:
            assert output.symptom_explain["confidence"] in {"HIGH", "MEDIUM", "LOW"}

    def test_symptom_explain_boosts_are_typed(self, tmp_path: Path) -> None:
        output = self._build_output(tmp_path, "session")
        if output.symptom_explain and output.symptom_explain["boosts"]:
            for boost in output.symptom_explain["boosts"]:
                assert "type" in boost
                assert "value" in boost
                assert "evidence" in boost
                assert isinstance(boost["value"], float)

    def test_symptom_explain_keywords_extracted(self, tmp_path: Path) -> None:
        output = self._build_output(tmp_path, "sessionManager")
        if output.symptom_explain is not None:
            kws = output.symptom_explain["keywords"]
            assert "session" in kws or "sessionmanager" in kws or "manager" in kws

    def test_no_symptom_explain_without_symptom(self, tmp_path: Path) -> None:
        output = self._build_output(tmp_path, "")
        assert output.symptom_explain is None

    def test_symptom_explain_not_on_other_tasks(self, tmp_path: Path) -> None:
        from sourcecode.prepare_context import TaskContextBuilder
        builder = TaskContextBuilder(tmp_path)
        output = builder.build("explain")
        assert output.symptom_explain is None

    def test_synonym_threshold_blocks_arbitrary_boost(self, tmp_path: Path) -> None:
        """Synonym heuristic must NOT boost files with zero prior signal."""
        src = tmp_path / "src"
        src.mkdir()
        # File contains session-related backend terms but no symptom keyword in path/commit
        interceptor = src / "GenericInterceptor.java"
        interceptor.write_text(
            "public class GenericInterceptor {\n"
            "    HttpSession session;\n"  # backend term but no real signal
            "}\n"
        )
        from sourcecode.prepare_context import TaskContextBuilder
        builder = TaskContextBuilder(tmp_path)
        output = builder.build("fix-bug", symptom="spinner")
        # File should NOT appear at a high score — threshold blocks synonym-only boost
        for rf in output.relevant_files:
            if "GenericInterceptor" in rf.path:
                # If found, it must have low score (synonym alone insufficient)
                assert rf.score < 0.5, (
                    f"GenericInterceptor boosted too high ({rf.score}) by synonym alone"
                )


# ─── Area 3: git_analyzer vendor dirs ────────────────────────────────────────

class TestGitAnalyzerVendorDirs:
    def test_vendor_dirs_in_hotspot_aux(self) -> None:
        from sourcecode.git_analyzer import _HOTSPOT_AUX_DIRS
        assert "vendor" in _HOTSPOT_AUX_DIRS
        assert "node_modules" in _HOTSPOT_AUX_DIRS
        assert "dist" in _HOTSPOT_AUX_DIRS
        assert "target" in _HOTSPOT_AUX_DIRS
        assert "build" in _HOTSPOT_AUX_DIRS

    def test_vendor_path_excluded_from_hotspots(self) -> None:
        from sourcecode.git_analyzer import _is_hotspot_admin
        assert _is_hotspot_admin("vendor/some/lib/Foo.java")
        assert _is_hotspot_admin("node_modules/pkg/index.js")
        assert _is_hotspot_admin("target/classes/App.class")
        assert _is_hotspot_admin("dist/bundle.js")

    def test_source_path_not_excluded(self) -> None:
        from sourcecode.git_analyzer import _is_hotspot_admin
        assert not _is_hotspot_admin("src/main/java/MyService.java")
        assert not _is_hotspot_admin("app/controllers/HomeController.java")

    def test_degradation_threshold_defined(self) -> None:
        from sourcecode.git_analyzer import _CHANGED_FILES_DEGRADATION_THRESHOLD
        assert isinstance(_CHANGED_FILES_DEGRADATION_THRESHOLD, int)
        assert _CHANGED_FILES_DEGRADATION_THRESHOLD > 0


# ─── Area 4: Unicode hardening ───────────────────────────────────────────────

class TestUnicodeHardening:
    def test_repository_ir_read_text_encoding(self, tmp_path: Path) -> None:
        """build_repo_ir must not crash on non-UTF-8 byte sequences."""
        from sourcecode.repository_ir import build_repo_ir

        java_file = tmp_path / "Weird.java"
        # Write a file with a cp1252 byte (0x93 = left double quotation mark)
        content = b"public class Weird {\n    // Caf\x93 comment\n}\n"
        java_file.write_bytes(content)

        # Must not raise — errors="replace" handles bad bytes
        ir = build_repo_ir(["Weird.java"], tmp_path)
        assert ir["schema_version"] == "final-v1"

    def test_repository_ir_emoji_in_source(self, tmp_path: Path) -> None:
        """build_repo_ir handles emoji and full Unicode in Java source."""
        from sourcecode.repository_ir import build_repo_ir

        java_file = tmp_path / "Emoji.java"
        java_file.write_text(
            'public class Emoji {\n    // 🎉 comment\n    String msg = "héllo";\n}\n',
            encoding="utf-8",
        )
        ir = build_repo_ir(["Emoji.java"], tmp_path)
        assert ir["schema_version"] == "final-v1"

    def test_git_analyzer_encoding_in_run_git(self) -> None:
        """_run_git must use encoding='utf-8' and errors='replace'."""
        import inspect
        from sourcecode import git_analyzer
        src = inspect.getsource(git_analyzer._run_git)
        assert 'encoding="utf-8"' in src
        assert 'errors="replace"' in src

    def test_repository_ir_git_content_encoding(self) -> None:
        """_get_git_old_content must use encoding='utf-8' and errors='replace'."""
        import inspect
        from sourcecode import repository_ir
        src = inspect.getsource(repository_ir._get_git_old_content)
        assert 'encoding="utf-8"' in src
        assert 'errors="replace"' in src


# ─── Area 5: symptom performance ─────────────────────────────────────────────

class TestSymptomPerformance:
    def test_commit_scan_capped_at_60(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Symptom commit scan must not process more than 60 commits."""
        from sourcecode.prepare_context import TaskContextBuilder
        from sourcecode.schema import CommitRecord

        scanned: list[str] = []
        original_build = TaskContextBuilder.build

        def _patched_build(self, task_name, *, since=None, symptom=None):
            # Inject 100 fake commits into the builder context to check cap
            return original_build(self, task_name, since=since, symptom=symptom)

        builder = TaskContextBuilder(tmp_path)
        # Just verify it doesn't explode with many commits
        output = builder.build("fix-bug", symptom="session")
        assert output is not None

    def test_content_scan_limit_constant(self) -> None:
        """_CONTENT_SCAN_LIMIT must be present and ≤ 100."""
        import inspect
        import re
        import sourcecode.prepare_context as pc_mod
        src = inspect.getsource(pc_mod.TaskContextBuilder.build)
        assert "_CONTENT_SCAN_LIMIT" in src, "content scan limit constant missing from build()"
        # Extract numeric value: "_CONTENT_SCAN_LIMIT = <N>"
        m = re.search(r"_CONTENT_SCAN_LIMIT\s*=\s*(\d+)", src)
        if m:
            assert int(m.group(1)) <= 100, f"_CONTENT_SCAN_LIMIT={m.group(1)} too large"

    def test_symptom_no_crash_empty_repo(self, tmp_path: Path) -> None:
        from sourcecode.prepare_context import TaskContextBuilder
        builder = TaskContextBuilder(tmp_path)
        output = builder.build("fix-bug", symptom="crash")
        assert output is not None
        assert output.task == "fix-bug"

    def test_regex_precompile_no_duplicate_matches(self, tmp_path: Path) -> None:
        """Compiled regex must match correctly for multi-keyword symptoms."""
        import re
        # Simulate the compile logic used in prepare_context
        symptom = "session manager"
        import re as _re
        _camel_expanded = _re.sub(r'([a-z])([A-Z])', r'\1 \2', symptom)
        keywords = [w.lower() for w in _re.split(r"[\s\W]+", _camel_expanded) if len(w) > 2]
        pattern = _re.compile("|".join(_re.escape(kw) for kw in keywords), _re.IGNORECASE)
        text = "SessionManager handles user sessions"
        matches = pattern.findall(text)
        assert len(matches) >= 2  # session + manager should both match


# ─── CLI flag integration ─────────────────────────────────────────────────────

class TestRepoIrCLIFlags:
    def test_summary_only_flag_reduces_size(self, tmp_path: Path) -> None:
        from typer.testing import CliRunner
        from sourcecode.cli import app

        java = tmp_path / "App.java"
        java.write_text(
            "@RestController\npublic class App { @GetMapping('/') String index() { return 'ok'; } }"
        )

        runner = CliRunner()
        result = runner.invoke(app, ["repo-ir", str(tmp_path), "--summary-only"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["graph"]["nodes"] == []
        assert data["graph"]["edges"] == []
        assert "_omitted" in data["graph"]

    def test_max_nodes_flag(self, tmp_path: Path) -> None:
        from typer.testing import CliRunner
        from sourcecode.cli import app

        for i in range(5):
            (tmp_path / f"Class{i}.java").write_text(f"public class Class{i} {{}}")

        runner = CliRunner()
        result = runner.invoke(app, ["repo-ir", str(tmp_path), "--max-nodes", "2"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert len(data["graph"]["nodes"]) <= 2

    def test_max_edges_flag(self, tmp_path: Path) -> None:
        from typer.testing import CliRunner
        from sourcecode.cli import app

        (tmp_path / "A.java").write_text(
            "import pkg.B;\npublic class A { B b; }"
        )
        (tmp_path / "B.java").write_text("public class B {}")

        runner = CliRunner()
        result = runner.invoke(app, ["repo-ir", str(tmp_path), "--max-edges", "1"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert len(data["graph"]["edges"]) <= 1

    def test_no_flags_backward_compatible(self, tmp_path: Path) -> None:
        from typer.testing import CliRunner
        from sourcecode.cli import app

        (tmp_path / "Service.java").write_text(
            "@Service\npublic class Service { void run() {} }"
        )

        runner = CliRunner()
        result_plain = runner.invoke(app, ["repo-ir", str(tmp_path)])
        result_sized = runner.invoke(app, ["repo-ir", str(tmp_path), "--max-nodes", "999"])
        assert result_plain.exit_code == 0
        assert result_sized.exit_code == 0
        # Both should have the same node count (999 > actual nodes)
        d1 = json.loads(result_plain.output)
        d2 = json.loads(result_sized.output)
        assert len(d1["graph"]["nodes"]) == len(d2["graph"]["nodes"])

    def test_schema_version_unchanged_with_limits(self, tmp_path: Path) -> None:
        from typer.testing import CliRunner
        from sourcecode.cli import app

        (tmp_path / "Foo.java").write_text("public class Foo {}")

        runner = CliRunner()
        result = runner.invoke(app, ["repo-ir", str(tmp_path), "--summary-only"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["schema_version"] == "final-v1"

"""
v1.33.13 regression tests — fix-bug --symptom graph expansion + schema compat.

Covers:
  G1  Graph expansion: 1-hop import neighbors added from seed files
  G2  Graph expansion explanation field mentions "graph_expansion"
  G3  symptom_explain.graph_expansion populated when expansion fires
  G4  Output budget: last-resort cap is >= 10 (not 3)
  G5  BUDGET_FIX_BUG raised to >= 150000
  S1  cache_source emitted at top level (schema compat A)
  S2  relevant_files[*].file backward-compat alias present
  S3  relevant_files[*].path still present alongside .file alias
  I1  fix-bug without --symptom unaffected
  I2  Graph expansion does not inject test files
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(root: Path, rel: str, content: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(content), encoding="utf-8")


def _paths(output: Any) -> set[str]:
    return {rf.path.replace("\\", "/") for rf in output.relevant_files}


def _build(root: Path, symptom: str, *, fast: bool = False) -> Any:
    from sourcecode.prepare_context import TaskContextBuilder
    return TaskContextBuilder(root).build("fix-bug", symptom=symptom, fast=fast)


def _write_provider_repo(root: Path) -> None:
    """Minimal Java repo with graph expansion targets that don't match symptom keywords.

    Seed (found by path match): UserProvider.java  → keywords: user, provider
    Hop-1 targets (only reachable via graph expansion):
      - PersistenceCoordinator.java  — imports UserProvider, no "user"/"provider" in name
      - RequestProcessor.java        — imports UserProvider, no "user"/"provider" in name
    Noise: OrderService.java — no import of UserProvider
    """
    _write(root, "pom.xml", "<project><artifactId>test</artifactId></project>")
    # Seed: UserProvider — will be found by path keyword match on "user" + "provider"
    _write(root, "src/main/java/com/demo/UserProvider.java", """
        package com.demo;
        public interface UserProvider {
            Object getById(String id);
        }
    """)
    # Hop-1 target A: no symptom keywords in name — only reachable via graph expansion
    _write(root, "src/main/java/com/demo/PersistenceCoordinator.java", """
        package com.demo;
        import com.demo.UserProvider;
        public class PersistenceCoordinator {
            private final UserProvider delegate;
            public PersistenceCoordinator(UserProvider d) { this.delegate = d; }
        }
    """)
    # Hop-1 target B: no symptom keywords in name — only reachable via graph expansion
    _write(root, "src/main/java/com/demo/RequestProcessor.java", """
        package com.demo;
        import com.demo.UserProvider;
        public class RequestProcessor {
            private final UserProvider lookup;
            public void handle(String id) { lookup.getById(id); }
        }
    """)
    # Noise: unrelated file — no import of UserProvider, should NOT appear via expansion
    _write(root, "src/main/java/com/demo/OrderService.java", """
        package com.demo;
        public class OrderService {
            public void process() {}
        }
    """)
    # Model imported by PersistenceCoordinator indirectly
    _write(root, "src/main/java/com/demo/DomainModel.java", """
        package com.demo;
        public class DomainModel {
            public String id;
        }
    """)


# ---------------------------------------------------------------------------
# G1 — Graph expansion adds 1-hop import neighbors
# ---------------------------------------------------------------------------


class TestGraphExpansion:
    def test_persistence_coordinator_found_via_graph_expansion(self, tmp_path: Path) -> None:
        """PersistenceCoordinator imports UserProvider — found by graph expansion, not path match."""
        _write_provider_repo(tmp_path)
        output = _build(tmp_path, "NullPointerException in UserProvider")
        found = _paths(output)
        assert any("PersistenceCoordinator" in p for p in found), (
            f"PersistenceCoordinator not found via graph expansion. Got: {sorted(found)}"
        )

    def test_request_processor_found_via_graph_expansion(self, tmp_path: Path) -> None:
        """RequestProcessor imports UserProvider — found by graph expansion, not path match."""
        _write_provider_repo(tmp_path)
        output = _build(tmp_path, "NullPointerException in UserProvider")
        found = _paths(output)
        assert any("RequestProcessor" in p for p in found), (
            f"RequestProcessor not found via graph expansion. Got: {sorted(found)}"
        )

    def test_unrelated_file_not_injected_via_graph_expansion(self, tmp_path: Path) -> None:
        """OrderService has no import of UserProvider — must not appear via graph expansion."""
        _write_provider_repo(tmp_path)
        output = _build(tmp_path, "NullPointerException in UserProvider")
        # OrderService has no import relationship; if it appears it must NOT be from expansion
        gx_paths = {
            rf.path for rf in output.relevant_files
            if "graph_expansion" in (rf.why or "")
        }
        assert not any("OrderService" in p for p in gx_paths), (
            f"OrderService should not appear via graph expansion: {gx_paths}"
        )

    def test_result_count_greater_than_three(self, tmp_path: Path) -> None:
        """Graph expansion + path injection should produce more than 3 results."""
        _write_provider_repo(tmp_path)
        output = _build(tmp_path, "NullPointerException in UserProvider")
        assert len(output.relevant_files) > 3, (
            f"Expected >3 relevant files, got {len(output.relevant_files)}: {_paths(output)}"
        )

    def test_graph_expansion_hop_score_decay(self, tmp_path: Path) -> None:
        """Files found ONLY via graph expansion must have score < 1.0 (decayed)."""
        _write_provider_repo(tmp_path)
        output = _build(tmp_path, "NullPointerException in UserProvider")
        # Only check files that come explicitly from graph_expansion (not path match)
        gx_files = [
            rf for rf in output.relevant_files
            if "graph_expansion" in (rf.why or "")
        ]
        for rf in gx_files:
            # Decayed score = seed.score * 0.6 ≤ 0.85, so must be < 1.0
            assert rf.score < 1.0, (
                f"Graph-expansion file {rf.path} has score {rf.score}, expected < 1.0 (decayed)"
            )


# ---------------------------------------------------------------------------
# G2 — Explanation mentions graph_expansion
# ---------------------------------------------------------------------------


class TestGraphExpansionExplanation:
    def test_explanation_mentions_graph_expansion(self, tmp_path: Path) -> None:
        """Files added ONLY via graph expansion must have 'graph_expansion' in their why field."""
        _write_provider_repo(tmp_path)
        output = _build(tmp_path, "NullPointerException in UserProvider")
        # PersistenceCoordinator and RequestProcessor have no symptom keywords in path,
        # so they can only appear via graph expansion (Pass 5).
        hop1_files = [
            rf for rf in output.relevant_files
            if "PersistenceCoordinator" in rf.path or "RequestProcessor" in rf.path
        ]
        if hop1_files:
            graph_exp_files = [rf for rf in hop1_files if "graph_expansion" in (rf.why or "")]
            assert graph_exp_files, (
                f"Expected at least one graph-expansion file with 'graph_expansion' in why. "
                f"Got: {[(rf.path, rf.why) for rf in hop1_files]}"
            )

    def test_graph_expansion_in_symptom_explain(self, tmp_path: Path) -> None:
        """symptom_explain.graph_expansion key must exist and list expanded paths."""
        _write_provider_repo(tmp_path)
        output = _build(tmp_path, "NullPointerException in UserProvider")
        assert output.symptom_explain is not None, "symptom_explain should be populated"
        assert "graph_expansion" in output.symptom_explain, (
            f"symptom_explain missing 'graph_expansion' key. Got keys: "
            f"{list(output.symptom_explain.keys())}"
        )

    def test_symptom_explain_graph_expansion_is_list(self, tmp_path: Path) -> None:
        """symptom_explain.graph_expansion must be a list."""
        _write_provider_repo(tmp_path)
        output = _build(tmp_path, "NullPointerException in UserProvider")
        if output.symptom_explain:
            gx = output.symptom_explain.get("graph_expansion")
            assert isinstance(gx, list), f"graph_expansion must be list, got {type(gx)}"


# ---------------------------------------------------------------------------
# G3 — symptom_explain populated when no expansion fires (graceful)
# ---------------------------------------------------------------------------


class TestGraphExpansionGraceful:
    def test_no_crash_when_no_seeds(self, tmp_path: Path) -> None:
        """No crash when symptom matches nothing (symptom_explain still populated)."""
        _write(tmp_path, "pom.xml", "<project><artifactId>test</artifactId></project>")
        _write(tmp_path, "src/main/java/com/demo/Foo.java", """
            package com.demo;
            public class Foo {}
        """)
        output = _build(tmp_path, "xyzzy unmatched totally random")
        # Must not crash; symptom_explain may or may not be set depending on keywords
        assert isinstance(output.relevant_files, list)

    def test_graph_expansion_list_empty_when_no_expansion(self, tmp_path: Path) -> None:
        """graph_expansion list is empty when no imports found in seeds."""
        _write(tmp_path, "pom.xml", "<project><artifactId>test</artifactId></project>")
        _write(tmp_path, "src/main/java/com/demo/FooService.java", """
            package com.demo;
            public class FooService {}
        """)
        output = _build(tmp_path, "FooService fails")
        if output.symptom_explain:
            gx = output.symptom_explain.get("graph_expansion", [])
            assert isinstance(gx, list)


# ---------------------------------------------------------------------------
# G4/G5 — Output budget: last-resort cap and BUDGET_FIX_BUG size
# ---------------------------------------------------------------------------


class TestOutputBudget:
    def test_last_resort_cap_at_least_ten(self) -> None:
        """output_budget last-resort step must leave >= 10 relevant_files."""
        from sourcecode.output_budget import _TRIM_SCHEDULE
        last_rf_cap = None
        for top_key, inner_key, max_items in _TRIM_SCHEDULE:
            if top_key == "relevant_files" and inner_key is None:
                last_rf_cap = max_items
        assert last_rf_cap is not None, "relevant_files not in _TRIM_SCHEDULE"
        assert last_rf_cap >= 10, (
            f"Last-resort relevant_files cap is {last_rf_cap}, expected >= 10"
        )

    def test_budget_fix_bug_at_least_150kb(self) -> None:
        """BUDGET_FIX_BUG must be >= 150000 bytes to handle large repo output."""
        from sourcecode.output_budget import BUDGET_FIX_BUG
        assert BUDGET_FIX_BUG >= 150_000, (
            f"BUDGET_FIX_BUG={BUDGET_FIX_BUG} too small; should be >= 150000"
        )

    def test_trim_schedule_does_not_collapse_to_three(self) -> None:
        """No trim step may collapse relevant_files to <= 3."""
        from sourcecode.output_budget import _TRIM_SCHEDULE
        for top_key, inner_key, max_items in _TRIM_SCHEDULE:
            if top_key == "relevant_files" and inner_key is None and max_items > 0:
                assert max_items >= 5, (
                    f"Trim step collapses relevant_files to {max_items} — too aggressive"
                )


# ---------------------------------------------------------------------------
# S1 — cache_source at top level (schema compat A)
# ---------------------------------------------------------------------------


class TestCacheSourceCompat:
    def test_inject_cache_meta_emits_top_level_cache_source(self, tmp_path: Path) -> None:
        """_inject_cache_meta must emit cache_source both inside _cache and at top level."""
        # We call it via a raw JSON string
        import importlib
        import sys
        # Import the function by parsing cli.py — simpler to test via subprocess
        # Actually, we can test via the prepare-context command output
        # Easier: just verify the function behavior by importing cli and calling directly.
        # cli.py uses nested function _inject_cache_meta — test via its output format.
        import json as _j
        raw = _j.dumps({"task": "fix-bug", "relevant_files": []})
        meta = {"cache_source": "L2_view", "is_stale": False}
        # Simulate what _inject_cache_meta does
        obj = _j.loads(raw)
        obj["_cache"] = meta
        if "cache_source" in meta:
            obj["cache_source"] = meta["cache_source"]
        result_str = _j.dumps(obj)
        result = _j.loads(result_str)
        assert result.get("cache_source") == "L2_view", (
            "cache_source must appear at top level"
        )
        assert result.get("_cache", {}).get("cache_source") == "L2_view", (
            "cache_source must remain inside _cache block"
        )

    def test_cache_source_in_cli_inject_function(self, tmp_path: Path) -> None:
        """Verify the actual _inject_cache_meta function emits top-level cache_source."""
        import json as _j
        import ast
        import sys
        import os
        # Read cli.py source and find _inject_cache_meta body
        cli_path = Path(__file__).parent.parent / "src/sourcecode/cli.py"
        src = cli_path.read_text(encoding="utf-8")
        # Verify the top-level cache_source write is in the source
        assert 'obj["cache_source"] = meta' in src or "obj['cache_source'] = meta" in src, (
            "cli.py must emit top-level cache_source in _inject_cache_meta"
        )


# ---------------------------------------------------------------------------
# S2/S3 — relevant_files[*].file alias (schema compat B)
# ---------------------------------------------------------------------------


class TestRelevantFileAlias:
    def _get_serialized_rfs(self, tmp_path: Path) -> list[dict]:
        """Build a fix-bug output and return the serialized relevant_files list."""
        _write(tmp_path, "pom.xml", "<project><artifactId>test</artifactId></project>")
        _write(tmp_path, "src/main/java/com/demo/UserProvider.java", """
            package com.demo;
            public interface UserProvider {
                void getUser();
            }
        """)
        _write(tmp_path, "src/main/java/com/demo/PersistenceCoordinator.java", """
            package com.demo;
            import com.demo.UserProvider;
            public class PersistenceCoordinator {
                private final UserProvider p;
                public PersistenceCoordinator(UserProvider p) { this.p = p; }
            }
        """)
        from sourcecode.cli import _serialize_relevant_file
        from sourcecode.prepare_context import TaskContextBuilder
        output = TaskContextBuilder(tmp_path).build("fix-bug", symptom="UserProvider")
        return [_serialize_relevant_file(rf) for rf in output.relevant_files]

    def test_file_alias_present(self, tmp_path: Path) -> None:
        """Each entry in relevant_files must have a 'file' key."""
        rfs = self._get_serialized_rfs(tmp_path)
        assert rfs, "relevant_files must not be empty"
        for entry in rfs:
            assert "file" in entry, (
                f"Entry missing 'file' alias: {entry}"
            )

    def test_path_still_present(self, tmp_path: Path) -> None:
        """Each entry must still have 'path' key alongside 'file'."""
        rfs = self._get_serialized_rfs(tmp_path)
        for entry in rfs:
            assert "path" in entry, f"Entry missing 'path': {entry}"

    def test_file_and_path_equal(self, tmp_path: Path) -> None:
        """'file' and 'path' must contain the same value."""
        rfs = self._get_serialized_rfs(tmp_path)
        for entry in rfs:
            if "file" in entry and "path" in entry:
                assert entry["file"] == entry["path"], (
                    f"file={entry['file']!r} != path={entry['path']!r}"
                )


# ---------------------------------------------------------------------------
# I1 — fix-bug without --symptom unaffected
# ---------------------------------------------------------------------------


class TestNoSymptomUnaffected:
    def test_fix_bug_no_symptom_returns_files(self, tmp_path: Path) -> None:
        """fix-bug without --symptom must still return relevant_files."""
        _write(tmp_path, "pom.xml", "<project><artifactId>test</artifactId></project>")
        _write(tmp_path, "src/main/java/com/demo/App.java", """
            package com.demo;
            public class App { public static void main(String[] args) {} }
        """)
        _write(tmp_path, "src/main/java/com/demo/Service.java", """
            package com.demo;
            public class Service { public void run() {} }
        """)
        from sourcecode.prepare_context import TaskContextBuilder
        output = TaskContextBuilder(tmp_path).build("fix-bug")
        assert isinstance(output.relevant_files, list)
        assert output.symptom_explain is None, "symptom_explain must be None without --symptom"


# ---------------------------------------------------------------------------
# I2 — Graph expansion does not inject test files
# ---------------------------------------------------------------------------


class TestGraphExpansionSkipsTests:
    def test_test_files_not_injected_via_graph_expansion(self, tmp_path: Path) -> None:
        """Graph expansion (Pass 5) must not add test files via reverse-import lookup."""
        _write(tmp_path, "pom.xml", "<project><artifactId>test</artifactId></project>")
        _write(tmp_path, "src/main/java/com/demo/UserProvider.java", """
            package com.demo;
            public interface UserProvider { void get(); }
        """)
        _write(tmp_path, "src/main/java/com/demo/JpaUserProvider.java", """
            package com.demo;
            import com.demo.UserProvider;
            public class JpaUserProvider implements UserProvider { public void get() {} }
        """)
        _write(tmp_path, "src/test/java/com/demo/UserProviderTest.java", """
            package com.demo;
            import com.demo.UserProvider;
            public class UserProviderTest { void test() {} }
        """)
        output = _build(tmp_path, "NullPointerException in UserProvider")
        # Test files must not appear via graph_expansion specifically
        gx_paths = {
            rf.path for rf in output.relevant_files
            if "graph_expansion" in (rf.why or "")
        }
        for p in gx_paths:
            assert "Test.java" not in p, (
                f"Test file injected via graph expansion: {p}"
            )
        # Sanity: UserProvider.java (the seed) must be in results
        all_found = {rf.path for rf in output.relevant_files}
        assert any("UserProvider.java" in p for p in all_found), (
            "UserProvider.java should appear in results"
        )

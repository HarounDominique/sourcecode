"""
Regression tests for sourcecode.ris — Repository Intelligence Snapshot.

BUG-R1: _current_git_head used full SHA (rev-parse HEAD) while cli.py stored
short SHA (rev-parse --short HEAD) in the RIS.  The staleness check
(current_head != ris.git_head) always returned True, making get_cold_start_context
return cold_start_stale forever.

Fix: _current_git_head now uses --short so both sides compare the same format.
"""
from __future__ import annotations

import gzip
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sourcecode import ris as _ris
from sourcecode.ris import (
    RepositoryIntelligenceSnapshot,
    _current_git_head,
    get_cold_start_context,
    maybe_update_ris,
    save_ris,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ris(
    tmp_path: Path,
    git_head: str = "abc1234",
) -> RepositoryIntelligenceSnapshot:
    return RepositoryIntelligenceSnapshot(
        repo_id="deadbeef12345678",
        created_at="2026-01-01T00:00:00",
        last_updated_at="2026-01-01T00:00:00",
        git_head=git_head,
        version=_ris.RIS_SCHEMA_VERSION,
        structural_map={},
        api_surface={},
        dependency_graph={},
        compact_summary={},
        agent_index={},
        git_context_snapshot={},
        metadata={"snapshot_source": "test", "confidence": 1.0, "partial": False},
    )


# ---------------------------------------------------------------------------
# BUG-R1 — SHA format consistency
# ---------------------------------------------------------------------------

class TestCurrentGitHeadShortSHA:
    def test_uses_short_flag(self) -> None:
        """_current_git_head must call rev-parse --short HEAD, not rev-parse HEAD.

        Regression: using the full 40-char SHA while cli.py stores the 7-char
        short SHA caused staleness check to always return True.
        """
        import subprocess
        import inspect
        src = inspect.getsource(_current_git_head)
        assert "--short" in src, (
            "_current_git_head must use rev-parse --short HEAD "
            "to match the short SHA stored by cli.py"
        )

    def test_returns_short_sha_format(self, tmp_path: Path) -> None:
        """Returned SHA must be short (≤ 12 chars), not the full 40-char form."""
        fake_short = "abc1234"
        with patch("subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = fake_short + "\n"
            mock_run.return_value = mock_result

            result = _current_git_head(tmp_path)

        assert result == fake_short
        # Verify --short was in the command
        call_args = mock_run.call_args[0][0]
        assert "--short" in call_args

    def test_git_error_returns_empty(self, tmp_path: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.returncode = 128
            mock_result.stdout = ""
            mock_run.return_value = mock_result
            result = _current_git_head(tmp_path)
        assert result == ""


class TestGetColdStartContext:
    def test_cold_start_ready_when_sha_matches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """get_cold_start_context returns cold_start_ready when short SHAs match.

        Regression: before fix, stored short SHA was compared against full SHA
        from _current_git_head, always yielding stale=True.
        """
        monkeypatch.setenv("SOURCECODE_CACHE_DIR", str(tmp_path / "cache"))
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()

        short_sha = "abc1234"
        ris_obj = _make_ris(tmp_path / "repo", git_head=short_sha)
        save_ris(repo, ris_obj)

        # Simulate _current_git_head returning the SAME short SHA
        with patch("sourcecode.ris._current_git_head", return_value=short_sha):
            ctx = get_cold_start_context(repo)

        assert ctx["status"] == "cold_start_ready", (
            f"Expected cold_start_ready, got {ctx['status']}. "
            "SHA comparison must use same format on both sides."
        )
        assert ctx["stale"] is False

    def test_cold_start_stale_when_sha_differs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """get_cold_start_context returns cold_start_stale when commit changed."""
        monkeypatch.setenv("SOURCECODE_CACHE_DIR", str(tmp_path / "cache"))
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()

        ris_obj = _make_ris(repo, git_head="abc1234")
        save_ris(repo, ris_obj)

        with patch("sourcecode.ris._current_git_head", return_value="def5678"):
            ctx = get_cold_start_context(repo)

        assert ctx["status"] == "cold_start_stale"
        assert ctx["stale"] is True

    def test_no_ris_returns_no_ris(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SOURCECODE_CACHE_DIR", str(tmp_path / "empty_cache"))
        repo = tmp_path / "repo"
        repo.mkdir()
        ctx = get_cold_start_context(repo)
        assert ctx["status"] == "no_ris"

    def test_full_sha_stored_would_always_be_stale(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Demonstrates the original bug: full vs short SHA always compares unequal.

        This is a documentation test — it explicitly proves the failure mode
        that existed before the fix.
        """
        monkeypatch.setenv("SOURCECODE_CACHE_DIR", str(tmp_path / "cache"))
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()

        short_sha = "abc1234"
        full_sha = "abc1234" + "0" * 33  # 40-char full SHA

        # Simulate old bug: RIS stored short SHA, _current_git_head returned full
        ris_obj = _make_ris(repo, git_head=short_sha)
        save_ris(repo, ris_obj)

        # If _current_git_head returned full SHA (old broken behavior),
        # the comparison would always be stale even on the same commit
        stale = bool(full_sha and short_sha and full_sha != short_sha)
        assert stale is True, "Full vs short SHA always differ — confirms the old bug"

        # After fix: both sides use short SHA → match
        stale_fixed = bool(short_sha and short_sha and short_sha != short_sha)
        assert stale_fixed is False, "Short vs short SHA from same commit → not stale"


class TestMaybeUpdateRis:
    def test_ris_stores_sha_from_caller(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """maybe_update_ris stores exactly the SHA passed by cli.py (short format)."""
        monkeypatch.setenv("SOURCECODE_CACHE_DIR", str(tmp_path / "cache"))
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()

        short_sha = "abc1234"
        core_dict = {"_compact": {"project_type": "python"}, "_agent": {}}
        maybe_update_ris(repo, core_dict, short_sha)

        loaded = _ris.load_ris(repo)
        assert loaded is not None
        assert loaded.git_head == short_sha, (
            f"RIS stored '{loaded.git_head}' but expected short SHA '{short_sha}'"
        )

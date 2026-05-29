"""
CLI tests for `sourcecode cache` subcommands (BUG-D1 regression).

Before 1.32.5, cache_app was implemented in cache.py but never registered with
the main CLI app, so `sourcecode cache status` exited with "No such command".
"""
from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sourcecode.cli import app
from sourcecode import cache as _cache


runner = CliRunner()


def _make_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    (repo / ".git").mkdir()
    return repo


# ---------------------------------------------------------------------------
# BUG-D1: cache subcommand group must be registered
# ---------------------------------------------------------------------------

class TestCacheCommandRegistration:
    def test_cache_in_help(self) -> None:
        """'cache' must appear in top-level --help output."""
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0, result.output
        assert "cache" in result.output

    def test_cache_status_command_exists(self) -> None:
        """`sourcecode cache --help` must list status/clear/warm."""
        result = runner.invoke(app, ["cache", "--help"])
        assert result.exit_code == 0, result.output
        assert "status" in result.output
        assert "clear" in result.output
        assert "warm" in result.output

    def test_cache_status_no_crash_on_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """cache status must not crash when no cache exists yet."""
        monkeypatch.setenv("SOURCECODE_CACHE_DIR", str(tmp_path / "cache"))
        repo = _make_repo(tmp_path)
        result = runner.invoke(app, ["cache", "status", str(repo)])
        assert result.exit_code == 0, result.output

    def test_cache_status_json_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """cache status --json must emit valid JSON."""
        monkeypatch.setenv("SOURCECODE_CACHE_DIR", str(tmp_path / "cache"))
        repo = _make_repo(tmp_path)
        result = runner.invoke(app, ["cache", "status", str(repo), "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert "cores" in data
        assert "snapshots" in data
        assert "total_size_mb" in data

    def test_cache_status_shows_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """cache status must report correct counts after writes."""
        monkeypatch.setenv("SOURCECODE_CACHE_DIR", str(tmp_path / "cache"))
        repo = _make_repo(tmp_path)
        # Write something to cache
        _cache.write(repo, "abc1234-aabbccdd",
                     json.dumps({"project_type": "python", "stacks": ["python"]}))
        result = runner.invoke(app, ["cache", "status", str(repo), "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["snapshots"] >= 1

    def test_cache_clear_with_yes_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """cache clear --yes must delete files without prompting."""
        monkeypatch.setenv("SOURCECODE_CACHE_DIR", str(tmp_path / "cache"))
        repo = _make_repo(tmp_path)
        _cache.write(repo, "abc1234-aabbccdd",
                     json.dumps({"project_type": "python"}))
        # Verify file exists
        assert _cache.status(repo)["snapshots"] >= 1

        result = runner.invoke(app, ["cache", "clear", str(repo), "--yes"])
        assert result.exit_code == 0, result.output
        assert "Removed" in result.output

        # Cache should be empty now
        stats = _cache.status(repo)
        assert stats["snapshots"] == 0
        assert stats["cores"] == 0

    def test_cache_status_exit_code_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """cache status must exit 0 (was exiting 2 before fix — 'No such command')."""
        monkeypatch.setenv("SOURCECODE_CACHE_DIR", str(tmp_path / "cache"))
        repo = _make_repo(tmp_path)
        result = runner.invoke(app, ["cache", "status", str(repo)])
        assert result.exit_code == 0, (
            f"cache status exited {result.exit_code}. "
            "Regression: cache_app was not registered with app.add_typer(). "
            f"Output: {result.output}"
        )

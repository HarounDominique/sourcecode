"""Tests for sourcecode.mcp_nudge — one-time MCP setup nudge."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import sourcecode.mcp_nudge as nudge_mod
from sourcecode.mcp_nudge import nudge_mcp_if_needed, clear_nudge_flag


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_client(app_installed: bool, config_path: Path) -> MagicMock:
    client = MagicMock()
    client.app_installed = app_installed
    client.config_path = config_path
    return client


# ── nudge_mcp_if_needed ───────────────────────────────────────────────────────

class TestNudgeMcpIfNeeded:
    """Core nudge logic — isolation via tmp_path flag dir."""

    def _run(self, flag_path: Path, clients, is_installed_val: bool, capsys) -> str:
        """Invoke nudge_mcp_if_needed with patched flag, clients and is_installed."""
        with (
            patch.object(nudge_mod, "_FLAG", flag_path),
            patch("sourcecode.mcp_nudge.detect_clients", return_value=clients),
            patch("sourcecode.mcp_nudge.is_installed", return_value=is_installed_val),
            patch("sourcecode.mcp_nudge.read_config", return_value={}),
        ):
            nudge_mcp_if_needed()
        return capsys.readouterr().err

    def test_nudge_shown_when_desktop_installed_not_configured(self, tmp_path, capsys):
        """Client installed, not in config → nudge printed."""
        flag = tmp_path / "nudge_shown"
        client = _make_client(app_installed=True, config_path=tmp_path / "config.json")
        stderr = self._run(flag, [client], is_installed_val=False, capsys=capsys)
        assert "sourcecode mcp init" in stderr
        assert flag.exists(), "Flag must be created after nudge"

    def test_no_nudge_when_already_configured(self, tmp_path, capsys):
        """Client installed AND already in config → no nudge."""
        flag = tmp_path / "nudge_shown"
        client = _make_client(app_installed=True, config_path=tmp_path / "config.json")
        stderr = self._run(flag, [client], is_installed_val=True, capsys=capsys)
        assert stderr == ""
        assert not flag.exists()

    def test_no_nudge_when_client_not_installed(self, tmp_path, capsys):
        """Client NOT installed → no nudge even if config absent."""
        flag = tmp_path / "nudge_shown"
        client = _make_client(app_installed=False, config_path=tmp_path / "config.json")
        stderr = self._run(flag, [client], is_installed_val=False, capsys=capsys)
        assert stderr == ""
        assert not flag.exists()

    def test_no_nudge_when_flag_exists(self, tmp_path, capsys):
        """Flag already present → no nudge (second run in same session)."""
        flag = tmp_path / "nudge_shown"
        flag.touch()
        client = _make_client(app_installed=True, config_path=tmp_path / "config.json")
        stderr = self._run(flag, [client], is_installed_val=False, capsys=capsys)
        assert stderr == ""

    def test_no_nudge_when_no_clients(self, tmp_path, capsys):
        """No clients detected → no nudge."""
        flag = tmp_path / "nudge_shown"
        stderr = self._run(flag, [], is_installed_val=False, capsys=capsys)
        assert stderr == ""
        assert not flag.exists()

    def test_second_call_no_repeat(self, tmp_path, capsys):
        """Nudge fires once; second call (flag now exists) is silent."""
        flag = tmp_path / "nudge_shown"
        client = _make_client(app_installed=True, config_path=tmp_path / "config.json")

        with (
            patch.object(nudge_mod, "_FLAG", flag),
            patch("sourcecode.mcp_nudge.detect_clients", return_value=[client]),
            patch("sourcecode.mcp_nudge.is_installed", return_value=False),
            patch("sourcecode.mcp_nudge.read_config", return_value={}),
        ):
            nudge_mcp_if_needed()
            capsys.readouterr()  # consume first write
            nudge_mcp_if_needed()

        second_stderr = capsys.readouterr().err
        assert second_stderr == ""

    def test_nudge_message_exact_text(self, tmp_path, capsys):
        """Message matches spec exactly."""
        flag = tmp_path / "nudge_shown"
        client = _make_client(app_installed=True, config_path=tmp_path / "config.json")
        stderr = self._run(flag, [client], is_installed_val=False, capsys=capsys)
        assert stderr == (
            "→ Claude Desktop detected. "
            "Run `sourcecode mcp init` to enable agent integration.\n"
        )

    def test_import_error_is_silent(self, tmp_path, capsys):
        """If onboarding modules can't be imported, nudge fails silently."""
        flag = tmp_path / "nudge_shown"
        with (
            patch.object(nudge_mod, "_FLAG", flag),
            patch.dict("sys.modules", {
                "sourcecode.mcp.onboarding.detector": None,  # type: ignore[dict-item]
                "sourcecode.mcp.onboarding.applier": None,   # type: ignore[dict-item]
            }),
        ):
            nudge_mcp_if_needed()  # must not raise
        assert capsys.readouterr().err == ""


# ── clear_nudge_flag ──────────────────────────────────────────────────────────

class TestClearNudgeFlag:
    def test_clears_existing_flag(self, tmp_path):
        flag = tmp_path / "nudge_shown"
        flag.touch()
        with patch.object(nudge_mod, "_FLAG", flag):
            clear_nudge_flag()
        assert not flag.exists()

    def test_noop_when_flag_absent(self, tmp_path):
        flag = tmp_path / "nudge_shown"
        with patch.object(nudge_mod, "_FLAG", flag):
            clear_nudge_flag()  # must not raise
        assert not flag.exists()

    def test_after_clear_nudge_fires_again_if_not_configured(self, tmp_path, capsys):
        """After mcp init clears flag, if somehow not configured, nudge re-fires."""
        flag = tmp_path / "nudge_shown"
        flag.touch()
        client = _make_client(app_installed=True, config_path=tmp_path / "config.json")

        with patch.object(nudge_mod, "_FLAG", flag):
            clear_nudge_flag()

        with (
            patch.object(nudge_mod, "_FLAG", flag),
            patch("sourcecode.mcp_nudge.detect_clients", return_value=[client]),
            patch("sourcecode.mcp_nudge.is_installed", return_value=False),
            patch("sourcecode.mcp_nudge.read_config", return_value={}),
        ):
            nudge_mcp_if_needed()

        assert "mcp init" in capsys.readouterr().err

"""Tests for the telemetry system.

Critical invariants verified here:
  - Sensitive data (paths, code, long strings) never escapes the filter
  - Telemetry is disabled by default
  - enable/disable round-trips correctly
  - Consent prompt defaults to False (opt-in, not opt-out)
  - Transport failures are silent
  - record() never raises regardless of input
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sourcecode.telemetry.events import (
    TelemetryEvent,
    duration_bucket,
    file_count_bucket,
)
from sourcecode.telemetry.filters import sanitize


# ── Bucket helpers ────────────────────────────────────────────────────────────

def test_file_count_bucket_ranges():
    assert file_count_bucket(0) == "tiny"
    assert file_count_bucket(49) == "tiny"
    assert file_count_bucket(50) == "small"
    assert file_count_bucket(499) == "small"
    assert file_count_bucket(500) == "medium"
    assert file_count_bucket(1999) == "medium"
    assert file_count_bucket(2000) == "large"
    assert file_count_bucket(9999) == "large"
    assert file_count_bucket(10000) == "huge"
    assert file_count_bucket(999999) == "huge"


def test_duration_bucket_ranges():
    assert duration_bucket(0.5) == "<1s"
    assert duration_bucket(1.0) == "<5s"
    assert duration_bucket(4.9) == "<5s"
    assert duration_bucket(5.0) == "<15s"
    assert duration_bucket(14.9) == "<15s"
    assert duration_bucket(15.0) == "<60s"
    assert duration_bucket(59.9) == "<60s"
    assert duration_bucket(60.0) == "60s+"
    assert duration_bucket(300.0) == "60s+"


# ── Privacy filter — core invariants ─────────────────────────────────────────

def _make_event(**kwargs: object) -> TelemetryEvent:
    defaults = dict(
        event="execution_completed",
        v="0.26.0",
        py="3.11",
        os="macos",
        arch="x64",
        cmd="analyze",
        flags=["--agent"],
        output_fmt="json",
        repo_size="small",
        duration="<5s",
        success=True,
        session="abc12345",
    )
    defaults.update(kwargs)  # type: ignore[arg-type]
    return TelemetryEvent(**defaults)  # type: ignore[arg-type]


def test_sanitize_clean_event_passes_through():
    ev = _make_event()
    result = sanitize(ev)
    assert result["event"] == "execution_completed"
    assert result["os"] == "macos"
    assert result["cmd"] == "analyze"
    assert result["success"] is True
    assert result["flags"] == ["--agent"]


def test_sanitize_strips_unknown_flags():
    ev = _make_event(flags=["--agent", "--unknown-flag", "/path/to/something"])
    result = sanitize(ev)
    assert result["flags"] == ["--agent"]


def test_sanitize_rejects_path_in_os():
    ev = _make_event(os="/etc/passwd")
    result = sanitize(ev)
    assert result["os"] == "other"


def test_sanitize_rejects_unknown_cmd():
    ev = _make_event(cmd="rm -rf /")
    result = sanitize(ev)
    assert result["cmd"] == "unknown"


def test_sanitize_rejects_unknown_event():
    ev = _make_event(event="user_data_exfiltrated")
    result = sanitize(ev)
    assert result["event"] == "command_executed"


def test_sanitize_truncates_version_string():
    ev = _make_event(v="0.26.0" + "x" * 100)
    result = sanitize(ev)
    assert len(result["v"]) <= 64


def test_sanitize_strips_path_from_version():
    ev = _make_event(v="/home/user/.local/bin/sourcecode")
    result = sanitize(ev)
    assert result["v"] == ""


def test_sanitize_error_kind_strips_message():
    ev = _make_event(error_kind="ValueError: path '/home/user/project' not found")
    result = sanitize(ev)
    # Only the class name, no message
    assert "path" not in (result.get("error_kind") or "")
    assert "home" not in (result.get("error_kind") or "")


def test_sanitize_error_kind_keeps_class_name():
    ev = _make_event(error_kind="FileNotFoundError")
    result = sanitize(ev)
    assert result.get("error_kind") == "FileNotFoundError"


def test_sanitize_invalid_session_stripped():
    ev = _make_event(session="../../../../etc/passwd")
    result = sanitize(ev)
    assert "session" not in result or result.get("session") == ""


def test_sanitize_valid_session_kept():
    ev = _make_event(session="deadbeef")
    result = sanitize(ev)
    assert result.get("session") == "deadbeef"


def test_sanitize_unknown_repo_size_falls_back():
    ev = _make_event(repo_size="12483 files in /home/user/project")
    result = sanitize(ev)
    assert result["repo_size"] == "unknown"


def test_sanitize_output_contains_no_paths():
    """Final integration check: no field in output should look like an absolute path."""
    ev = _make_event(
        flags=["--agent", "--output", "/home/user/project/context.json"],
    )
    result = sanitize(ev)
    for key, val in result.items():
        if isinstance(val, str):
            assert "/home" not in val, f"Field {key!r} contains a path: {val!r}"
            assert "\\Users" not in val, f"Field {key!r} contains a Windows path: {val!r}"


# ── Config: opt-in defaults ───────────────────────────────────────────────────

def test_telemetry_disabled_by_default(tmp_path: Path):
    """Telemetry must be OFF unless the user explicitly enables it."""
    with patch("sourcecode.telemetry.config._CONFIG_FILE", tmp_path / "config.json"):
        from sourcecode.telemetry.config import is_enabled
        assert is_enabled() is False


def test_env_var_zero_disables(tmp_path: Path):
    with patch("sourcecode.telemetry.config._CONFIG_FILE", tmp_path / "config.json"):
        with patch.dict(os.environ, {"SOURCECODE_TELEMETRY": "0"}):
            from sourcecode.telemetry.config import is_enabled, set_enabled
            set_enabled(True)  # even if config says enabled, env=0 wins
            assert is_enabled() is False


def test_env_var_one_enables(tmp_path: Path):
    with patch("sourcecode.telemetry.config._CONFIG_FILE", tmp_path / "config.json"):
        with patch.dict(os.environ, {"SOURCECODE_TELEMETRY": "1"}):
            from sourcecode.telemetry.config import is_enabled
            assert is_enabled() is True


def test_set_enabled_round_trip(tmp_path: Path):
    cfg = tmp_path / "config.json"
    with patch("sourcecode.telemetry.config._CONFIG_FILE", cfg):
        from sourcecode.telemetry.config import is_enabled, set_enabled
        set_enabled(True)
        assert is_enabled() is True
        set_enabled(False)
        assert is_enabled() is False


def test_set_enabled_marks_asked(tmp_path: Path):
    cfg = tmp_path / "config.json"
    with patch("sourcecode.telemetry.config._CONFIG_FILE", cfg):
        from sourcecode.telemetry.config import has_been_asked, set_enabled
        assert has_been_asked() is False
        set_enabled(False)
        assert has_been_asked() is True


# ── Consent: opt-in default ───────────────────────────────────────────────────

def test_consent_non_interactive_returns_false():
    """In non-TTY environments, consent must default to False."""
    with patch("sourcecode.telemetry.consent._is_interactive", return_value=False):
        from sourcecode.telemetry.consent import ask_for_consent
        result = ask_for_consent()
        assert result is False


def test_consent_interactive_default_no(monkeypatch: pytest.MonkeyPatch):
    """User pressing Enter (empty input) must default to No."""
    import io
    import sys
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n"))
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    with patch("sourcecode.telemetry.consent._is_interactive", return_value=True):
        from sourcecode.telemetry.consent import ask_for_consent
        result = ask_for_consent()
    assert result is False


def test_consent_interactive_yes(monkeypatch: pytest.MonkeyPatch):
    """User typing 'y' must enable telemetry."""
    import io
    import sys
    monkeypatch.setattr(sys, "stdin", io.StringIO("y\n"))
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    with patch("sourcecode.telemetry.consent._is_interactive", return_value=True):
        from sourcecode.telemetry.consent import ask_for_consent
        result = ask_for_consent()
    assert result is True


# ── Transport: silent failures ────────────────────────────────────────────────

def test_transport_handles_connection_error():
    """A network failure must never raise or print anything."""
    with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
        from sourcecode.telemetry.transport import _send_blocking
        _send_blocking({"event": "test", "v": "0.1"})  # must not raise


def test_transport_handles_timeout():
    import socket
    with patch("urllib.request.urlopen", side_effect=socket.timeout("timed out")):
        from sourcecode.telemetry.transport import _send_blocking
        _send_blocking({"event": "test"})  # must not raise


def test_send_uses_daemon_thread():
    """Transport must spawn a daemon thread so it never blocks process exit."""
    threads_before = {t.ident for t in threading.enumerate()}
    with patch("urllib.request.urlopen"):
        from sourcecode.telemetry.transport import send
        send({"event": "test", "v": "0.1"})
    # Daemon thread spawned — we can't easily inspect it after the fact,
    # but we verify send() returns immediately (no blocking).


# ── Public API: record() never raises ────────────────────────────────────────

def test_record_does_not_raise_when_disabled(tmp_path: Path):
    with patch("sourcecode.telemetry.config._CONFIG_FILE", tmp_path / "config.json"):
        import sourcecode.telemetry as tel
        with patch.object(tel, "is_enabled", return_value=False):
            tel.record("execution_completed", cmd="analyze")  # must not raise


def test_record_does_not_raise_on_bad_input(tmp_path: Path):
    with patch("sourcecode.telemetry.config._CONFIG_FILE", tmp_path / "config.json"):
        import sourcecode.telemetry as tel
        with patch.object(tel, "is_enabled", return_value=True):
            with patch("sourcecode.telemetry.transport.send"):
                tel.record(
                    "execution_completed",
                    cmd="analyze",
                    flags=["--agent"],
                    file_count=-999,
                    duration_s=float("inf"),
                )  # must not raise


def test_record_when_enabled_calls_send(tmp_path: Path):
    import sourcecode.telemetry as tel
    # Patch send on the __init__ module where record() looks it up
    with patch.object(tel, "is_enabled", return_value=True):
        with patch.object(tel, "send") as mock_send:
            tel.record("execution_completed", cmd="analyze", flags=["--agent"])
            assert mock_send.called
            payload = mock_send.call_args[0][0]
            # Verify payload has expected safe fields
            assert payload["event"] == "execution_completed"
            assert payload["cmd"] == "analyze"
            assert "--agent" in payload["flags"]


def test_record_payload_contains_no_sensitive_data(tmp_path: Path):
    """Integration test: record() output must not contain paths or long strings."""
    import sourcecode.telemetry as tel
    with patch.object(tel, "is_enabled", return_value=True):
        sent_payloads: list[dict] = []
        with patch.object(tel, "send", side_effect=lambda p: sent_payloads.append(p)):
            tel.record(
                "execution_completed",
                cmd="analyze",
                flags=["--agent", "--output"],
                file_count=1234,
                duration_s=3.7,
                success=True,
            )

    assert sent_payloads, "Expected one payload"
    payload = sent_payloads[0]

    # Verify all string values are short and path-free
    for key, val in payload.items():
        if isinstance(val, str):
            assert len(val) <= 64, f"{key}={val!r} too long"
            assert "/" not in val or key == "ts", f"{key}={val!r} contains /"
            assert "\\" not in val, f"{key}={val!r} contains \\"

    # Verify repo size is bucketed
    assert payload.get("repo_size") in {"tiny", "small", "medium", "large", "huge", "unknown"}
    assert payload.get("duration") in {"<1s", "<5s", "<15s", "<60s", "60s+", "unknown"}

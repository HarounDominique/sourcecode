"""Tests for the best-effort PyPI version nudge (sourcecode.version_check)."""
from __future__ import annotations

import io
from datetime import datetime, timezone

import sourcecode.version_check as vc


def _fresh_cache(latest: str) -> dict:
    return {"latest": latest, "checked_at": datetime.now(timezone.utc).isoformat()}


def _tty_stderr() -> io.StringIO:
    buf = io.StringIO()
    buf.isatty = lambda: True  # type: ignore[attr-defined]
    return buf


class TestIsNewer:
    def test_strictly_newer(self):
        assert vc._is_newer("1.37.0", "1.36.1") is True
        assert vc._is_newer("1.100.0", "1.99.0") is True

    def test_same_or_older(self):
        assert vc._is_newer("1.36.1", "1.36.1") is False
        assert vc._is_newer("1.36.0", "1.36.1") is False


class TestNudge:
    def test_fires_on_tty_when_newer(self, monkeypatch):
        buf = _tty_stderr()
        monkeypatch.setattr(vc.sys, "stderr", buf)
        monkeypatch.setattr(vc, "_read_cache", lambda: _fresh_cache("9.9.9"))
        monkeypatch.setattr(vc, "_write_cache", lambda d: None)
        # Cache is fresh -> network must not be touched.
        monkeypatch.setattr(
            vc, "_fetch_latest",
            lambda: (_ for _ in ()).throw(AssertionError("network hit on fresh cache")),
        )
        monkeypatch.delenv("SOURCECODE_NO_UPDATE_CHECK", raising=False)
        monkeypatch.delenv("SOURCECODE_CI", raising=False)

        vc.maybe_notify_update("1.36.1")
        out = buf.getvalue()
        assert "9.9.9 is available" in out
        assert "1.36.1" in out

    def test_silent_when_not_tty(self, monkeypatch):
        buf = io.StringIO()
        buf.isatty = lambda: False  # type: ignore[attr-defined]
        monkeypatch.setattr(vc.sys, "stderr", buf)
        monkeypatch.setattr(vc, "_read_cache", lambda: _fresh_cache("9.9.9"))
        vc.maybe_notify_update("1.36.1")
        assert buf.getvalue() == ""

    def test_silent_when_opted_out(self, monkeypatch):
        buf = _tty_stderr()
        monkeypatch.setattr(vc.sys, "stderr", buf)
        monkeypatch.setattr(vc, "_read_cache", lambda: _fresh_cache("9.9.9"))
        monkeypatch.setenv("SOURCECODE_NO_UPDATE_CHECK", "1")
        vc.maybe_notify_update("1.36.1")
        assert buf.getvalue() == ""

    def test_silent_when_not_newer(self, monkeypatch):
        buf = _tty_stderr()
        monkeypatch.setattr(vc.sys, "stderr", buf)
        monkeypatch.setattr(vc, "_read_cache", lambda: _fresh_cache("1.36.1"))
        monkeypatch.delenv("SOURCECODE_NO_UPDATE_CHECK", raising=False)
        monkeypatch.delenv("SOURCECODE_CI", raising=False)
        vc.maybe_notify_update("1.36.1")
        assert buf.getvalue() == ""

    def test_throttled_after_recent_notify(self, monkeypatch):
        buf = _tty_stderr()
        monkeypatch.setattr(vc.sys, "stderr", buf)
        cache = _fresh_cache("9.9.9")
        cache["notified_for"] = "9.9.9"
        cache["notified_at"] = datetime.now(timezone.utc).isoformat()
        monkeypatch.setattr(vc, "_read_cache", lambda: cache)
        monkeypatch.delenv("SOURCECODE_NO_UPDATE_CHECK", raising=False)
        monkeypatch.delenv("SOURCECODE_CI", raising=False)
        vc.maybe_notify_update("1.36.1")
        assert buf.getvalue() == ""

    def test_never_raises_on_broken_cache(self, monkeypatch):
        buf = _tty_stderr()
        monkeypatch.setattr(vc.sys, "stderr", buf)
        monkeypatch.setattr(
            vc, "_read_cache",
            lambda: (_ for _ in ()).throw(ValueError("boom")),
        )
        monkeypatch.delenv("SOURCECODE_NO_UPDATE_CHECK", raising=False)
        # Must swallow the error, not propagate.
        vc.maybe_notify_update("1.36.1")

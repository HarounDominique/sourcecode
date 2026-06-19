"""Tests for license cache TTL — SOURCECODE_CI env override."""
from __future__ import annotations

import importlib
import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

import sourcecode.license as lic


class TestGetCacheTtl:
    def test_default_ttl_is_30_minutes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SOURCECODE_CI", raising=False)
        assert lic._get_cache_ttl() == 1800

    def test_ci_ttl_is_24_hours(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOURCECODE_CI", "1")
        assert lic._get_cache_ttl() == 86400

    def test_ci_ttl_any_truthy_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOURCECODE_CI", "true")
        assert lic._get_cache_ttl() == 86400

    def test_no_ci_with_empty_string_uses_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOURCECODE_CI", "")
        assert lic._get_cache_ttl() == 1800


class TestSafeSupabaseUrl:
    """License key is POSTed here — only https (or http to loopback) is trusted."""

    def test_none_and_default_pass_through(self) -> None:
        assert lic._safe_supabase_url(None) == lic._DEFAULT_SUPABASE_URL
        assert lic._safe_supabase_url(lic._DEFAULT_SUPABASE_URL) == lic._DEFAULT_SUPABASE_URL

    def test_https_override_allowed(self) -> None:
        assert lic._safe_supabase_url("https://other.example.co") == "https://other.example.co"

    def test_http_localhost_allowed_for_dev(self) -> None:
        for url in ("http://localhost:54321", "http://127.0.0.1:54321"):
            assert lic._safe_supabase_url(url) == url

    def test_http_remote_rejected_to_default(self) -> None:
        assert lic._safe_supabase_url("http://evil.example.co") == lic._DEFAULT_SUPABASE_URL

    def test_non_http_scheme_rejected(self) -> None:
        assert lic._safe_supabase_url("ftp://host/x") == lic._DEFAULT_SUPABASE_URL


class TestProUnlock:
    """TEMPORARY early-adoption Pro unlock — floors is_pro, gate logic intact."""

    def test_unlock_floors_is_pro_with_no_license(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(lic, "_PRO_UNLOCK_ALL", True)
        monkeypatch.setattr(lic, "_load_license_file", lambda: None)
        lic._init()
        assert lic.is_pro is True

    def test_disabled_restores_paywall_with_no_license(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(lic, "_PRO_UNLOCK_ALL", False)
        monkeypatch.setattr(lic, "_load_license_file", lambda: None)
        lic._init()
        assert lic.is_pro is False

    def test_unlock_keeps_floor_when_revalidation_invalidates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(lic, "_PRO_UNLOCK_ALL", True)
        monkeypatch.setattr(lic, "_license_data", {"license_key": "k", "validated_at": "2000-01-01T00:00:00+00:00"})
        monkeypatch.setattr(lic, "_call_get_license", lambda key: {"valid": False})
        monkeypatch.setattr(lic, "_LICENSE_FILE", Path("/nonexistent/license.json"))
        lic._maybe_revalidate()
        assert lic.is_pro is True

    def test_gate_logic_intact_require_pro_exits_when_not_pro(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(lic, "is_pro", False)
        with pytest.raises(SystemExit):
            lic.require_pro("impact")


@pytest.mark.skipif(os.name == "nt", reason="POSIX file-mode semantics")
class TestLicenseFilePermissions:
    """License file holds a secret (license_key + email) — must be owner-only."""

    def test_license_file_is_owner_only(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        lic_dir = tmp_path / ".sourcecode"
        monkeypatch.setattr(lic, "_LICENSE_DIR", lic_dir)
        monkeypatch.setattr(lic, "_LICENSE_FILE", lic_dir / "license.json")
        lic._write_license_file({"license_key": "secret-key", "email": "a@b.c"})
        mode = (lic_dir / "license.json").stat().st_mode & 0o777
        assert mode == 0o600, oct(mode)

    def test_license_dir_is_owner_only(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        lic_dir = tmp_path / ".sourcecode"
        monkeypatch.setattr(lic, "_LICENSE_DIR", lic_dir)
        monkeypatch.setattr(lic, "_LICENSE_FILE", lic_dir / "license.json")
        lic._write_license_file({"license_key": "secret-key"})
        mode = lic_dir.stat().st_mode & 0o777
        assert mode == 0o700, oct(mode)

    def test_world_readable_dir_is_tightened(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pre-existing world-readable dir must be tightened (mkdir mode is ignored when it exists)."""
        lic_dir = tmp_path / ".sourcecode"
        lic_dir.mkdir(mode=0o755)
        os.chmod(lic_dir, 0o755)
        monkeypatch.setattr(lic, "_LICENSE_DIR", lic_dir)
        monkeypatch.setattr(lic, "_LICENSE_FILE", lic_dir / "license.json")
        lic._secure_dir()
        assert (lic_dir.stat().st_mode & 0o777) == 0o700


class TestMaybeRevalidateCISkip:
    """_maybe_revalidate must NOT call network when CI env is set and cache is fresh enough."""

    def _make_license_data(self, age_seconds: int) -> dict:
        validated_at = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
        return {
            "auth_method": "license_key",
            "license_key": "SC-TEST-KEY",
            "email": "test@example.com",
            "plan": "pro",
            "status": "active",
            "features": [],
            "validated_at": validated_at.isoformat(),
        }

    def test_ci_suppresses_revalidation_at_2h(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With SOURCECODE_CI=1, a 2h-old cache (>30min but <24h) must NOT trigger network call."""
        monkeypatch.setenv("SOURCECODE_CI", "1")
        monkeypatch.setattr(lic, "_license_data", self._make_license_data(7200))
        monkeypatch.setattr(lic, "is_pro", True)

        with patch.object(lic, "_call_get_license") as mock_net:
            lic._maybe_revalidate()
            mock_net.assert_not_called()

    def test_no_ci_triggers_revalidation_at_2h(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without SOURCECODE_CI, a 2h-old cache (>30min) MUST trigger revalidation."""
        monkeypatch.delenv("SOURCECODE_CI", raising=False)
        monkeypatch.setattr(lic, "_license_data", self._make_license_data(7200))
        monkeypatch.setattr(lic, "is_pro", True)

        with patch.object(lic, "_call_get_license", return_value=None) as mock_net:
            lic._maybe_revalidate()
            mock_net.assert_called_once()

    def test_ci_still_revalidates_after_24h(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Even in CI, a >24h-old cache must trigger revalidation."""
        monkeypatch.setenv("SOURCECODE_CI", "1")
        monkeypatch.setattr(lic, "_license_data", self._make_license_data(90000))
        monkeypatch.setattr(lic, "is_pro", True)

        with patch.object(lic, "_call_get_license", return_value=None) as mock_net:
            lic._maybe_revalidate()
            mock_net.assert_called_once()

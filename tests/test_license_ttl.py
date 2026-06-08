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


class TestMaybeRevalidateCISkip:
    """_maybe_revalidate must NOT call network when CI env is set and cache is fresh enough."""

    def _make_license_data(self, age_seconds: int) -> dict:
        validated_at = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
        return {
            "auth_method": "device_flow",
            "device_token": "tok_test",
            "email": "test@example.com",
            "plan": "pro",
            "status": "active",
            "features": [],
            "validated_at": validated_at.isoformat(),
        }

    def test_ci_suppresses_revalidation_at_2h(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With SOURCECODE_CI=1, a 2h-old cache (>30min but <24h) must NOT trigger network call."""
        monkeypatch.setenv("SOURCECODE_CI", "1")
        lic._license_data = self._make_license_data(7200)  # 2 hours old
        lic.is_pro = True

        with patch.object(lic, "_call_get_user_plan") as mock_net:
            lic._maybe_revalidate()
            mock_net.assert_not_called()

        lic._license_data = None
        lic.is_pro = False

    def test_no_ci_triggers_revalidation_at_2h(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without SOURCECODE_CI, a 2h-old cache (>30min) MUST trigger revalidation."""
        monkeypatch.delenv("SOURCECODE_CI", raising=False)
        lic._license_data = self._make_license_data(7200)  # 2 hours old
        lic.is_pro = True

        with patch.object(lic, "_call_get_user_plan", return_value=None) as mock_net:
            lic._maybe_revalidate()
            mock_net.assert_called_once()

        lic._license_data = None
        lic.is_pro = False

    def test_ci_still_revalidates_after_24h(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Even in CI, a >24h-old cache must trigger revalidation."""
        monkeypatch.setenv("SOURCECODE_CI", "1")
        lic._license_data = self._make_license_data(90000)  # 25 hours old
        lic.is_pro = True

        with patch.object(lic, "_call_get_user_plan", return_value=None) as mock_net:
            lic._maybe_revalidate()
            mock_net.assert_called_once()

        lic._license_data = None
        lic.is_pro = False

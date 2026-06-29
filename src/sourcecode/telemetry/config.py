"""Persistent telemetry configuration.

Telemetry is enabled by default (opt-out). It stays anonymous and never
collects source code, paths, secrets or repository content.

Config file: ~/.config/sourcecode/config.json
Disable: `sourcecode telemetry disable`, SOURCECODE_TELEMETRY=0, or DO_NOT_TRACK=1
Env override: SOURCECODE_TELEMETRY=0 (disable) or =1 (enable)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_ENV_VAR = "SOURCECODE_TELEMETRY"
_CONFIG_FILE = Path.home() / ".config" / "sourcecode" / "config.json"

# CI markers — when no explicit choice has been made, telemetry defaults OFF
# in CI (no human to see the first-run notice), ON otherwise.
_CI_VARS = (
    "CI", "CONTINUOUS_INTEGRATION", "GITHUB_ACTIONS", "CIRCLECI",
    "TRAVIS", "JENKINS_URL", "BUILDKITE", "GITLAB_CI", "TF_BUILD",
    "TEAMCITY_VERSION", "DRONE", "SEMAPHORE",
)


def _in_ci() -> bool:
    return any(os.environ.get(v) for v in _CI_VARS)


def _load() -> dict[str, Any]:
    try:
        return json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))  # type: ignore[no-any-return]
    except Exception:
        return {}


def _save(data: dict[str, Any]) -> None:
    try:
        _CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass  # config write failure is non-fatal


def is_enabled() -> bool:
    """True unless telemetry has been explicitly disabled.

    Telemetry is enabled by default (opt-out). Precedence, highest first:
      1. SOURCECODE_TELEMETRY env var (0 = off, 1 = on)
      2. DO_NOT_TRACK env var (any value other than ""/"0" turns it off)
      3. config file 'enabled' flag, if the user has made an explicit choice
      4. default: True — except in CI, where it defaults to False
    """
    env = os.environ.get(_ENV_VAR, "").strip()
    if env == "0":
        return False
    if env == "1":
        return True
    dnt = os.environ.get("DO_NOT_TRACK", "").strip()
    if dnt not in ("", "0"):
        return False
    stored = _load().get("telemetry", {}).get("enabled")
    if stored is not None:
        return bool(stored)
    # No explicit choice yet: on by default, off under CI.
    return not _in_ci()


def has_been_asked() -> bool:
    """True if the consent prompt has already been shown."""
    return bool(_load().get("telemetry", {}).get("asked", False))


def set_enabled(value: bool) -> None:
    data = _load()
    data.setdefault("telemetry", {})["enabled"] = value
    data["telemetry"]["asked"] = True
    _save(data)


def mark_asked() -> None:
    """Record that the consent prompt was shown (regardless of answer)."""
    data = _load()
    data.setdefault("telemetry", {})["asked"] = True
    _save(data)


def get_install_id() -> str:
    """Stable anonymous install id — a random UUID v4.

    Created lazily on first opted-in event. NOT derived from hardware, email,
    hostname or any identifier — it only says "the same install across runs",
    which is what enables unique-user, conversion and retention metrics.
    Returns "" if it cannot be persisted (telemetry then degrades to events
    without a stable id, never an error).
    """
    data = _load()
    tel = data.setdefault("telemetry", {})
    iid = tel.get("install_id")
    if not iid:
        import uuid
        iid = str(uuid.uuid4())
        tel["install_id"] = iid
        _save(data)
        # If the write failed, re-read to avoid handing out a non-persisted id
        if not _load().get("telemetry", {}).get("install_id"):
            return ""
    return str(iid)


def config_file_path() -> Path:
    return _CONFIG_FILE

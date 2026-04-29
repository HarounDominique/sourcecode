"""Persistent telemetry configuration.

Config file: ~/.config/sourcecode/config.json
Env override: SOURCECODE_TELEMETRY=0 (disable) or =1 (enable)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_ENV_VAR = "SOURCECODE_TELEMETRY"
_CONFIG_FILE = Path.home() / ".config" / "sourcecode" / "config.json"


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
    """True only when telemetry is explicitly opted in.

    Env var takes absolute precedence over config file.
    Default is always disabled — telemetry is strictly opt-in.
    """
    env = os.environ.get(_ENV_VAR, "").strip()
    if env == "0":
        return False
    if env == "1":
        return True
    return bool(_load().get("telemetry", {}).get("enabled", False))


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


def config_file_path() -> Path:
    return _CONFIG_FILE

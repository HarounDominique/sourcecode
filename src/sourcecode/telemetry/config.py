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

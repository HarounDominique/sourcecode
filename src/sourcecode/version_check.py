"""Best-effort "new version available" nudge.

Prints a single stderr line when a newer release exists on PyPI. Designed to be
invisible unless it has something useful to say:

  * Only runs in an interactive terminal (stderr.isatty()) — never pollutes
    piped output, MCP stdio, CI logs, or test runners.
  * Hits the network at most once per 24h (cached in
    ~/.sourcecode/version_check.json); warm runs read the cache and are instant.
  * Re-shows the same nudge at most ~once per 20h so it informs without nagging.
  * Swallows every error and never blocks meaningfully (1.5s network timeout).

Disable entirely with SOURCECODE_NO_UPDATE_CHECK=1 (also off under SOURCECODE_CI).
The check reads PyPI only; it never touches the license in ~/.sourcecode/license.json.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_CACHE_DIR = Path.home() / ".sourcecode"
_CACHE_FILE = _CACHE_DIR / "version_check.json"
_PYPI_URL = "https://pypi.org/pypi/sourcecode/json"
_CHECK_TTL_SECONDS = 86_400   # refresh the PyPI lookup at most once per 24h
_NOTIFY_TTL_SECONDS = 72_000  # re-show the nudge at most every ~20h
_FETCH_TIMEOUT = 1.5


def _disabled() -> bool:
    """True when the nudge must stay silent (opt-out, CI, or non-interactive)."""
    if os.environ.get("SOURCECODE_NO_UPDATE_CHECK"):
        return True
    if os.environ.get("SOURCECODE_CI"):
        return True
    try:
        return not sys.stderr.isatty()
    except Exception:
        return True  # no usable stderr -> stay silent


def _read_cache() -> dict:
    try:
        if _CACHE_FILE.exists():
            return json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _write_cache(data: dict) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_CACHE_FILE)
    except Exception:
        pass


def _age_seconds(iso: Optional[str]) -> float:
    if not iso:
        return float("inf")
    try:
        ts = datetime.fromisoformat(iso)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds()
    except Exception:
        return float("inf")


def _fetch_latest() -> Optional[str]:
    import urllib.request
    try:
        req = urllib.request.Request(_PYPI_URL, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return ((data.get("info") or {}).get("version")) or None
    except Exception:
        return None


def _parse(v: str) -> tuple:
    """Lenient dotted-numeric parse for the fallback path (no packaging dep)."""
    parts = []
    for chunk in str(v).split("."):
        num = ""
        for ch in chunk:
            if ch.isdigit():
                num += ch
            else:
                break
        parts.append(int(num) if num else 0)
    return tuple(parts)


def _is_newer(latest: str, current: str) -> bool:
    try:
        from packaging.version import parse as _vparse  # type: ignore
        return _vparse(latest) > _vparse(current)
    except Exception:
        return _parse(latest) > _parse(current)


def maybe_notify_update(current_version: str) -> None:
    """Print an upgrade nudge to stderr if PyPI has a newer release.

    Best-effort and fully guarded: any failure is silently ignored. Safe to call
    unconditionally from the CLI entry point.
    """
    if _disabled():
        return
    try:
        cache = _read_cache()

        # Refresh the cached "latest" at most once per TTL (the only network hit).
        if _age_seconds(cache.get("checked_at")) >= _CHECK_TTL_SECONDS:
            latest = _fetch_latest()
            if latest:
                cache["latest"] = latest
                cache["checked_at"] = datetime.now(timezone.utc).isoformat()
                _write_cache(cache)

        latest = cache.get("latest")
        if not latest or not _is_newer(latest, current_version):
            return

        # Throttle: don't nag for the same version more than once per ~20h.
        if (
            cache.get("notified_for") == latest
            and _age_seconds(cache.get("notified_at")) < _NOTIFY_TTL_SECONDS
        ):
            return

        sys.stderr.write(
            f"\n[sourcecode] v{latest} is available (you have {current_version}). "
            "Upgrade: pipx upgrade sourcecode  (pip: pip install -U sourcecode)\n"
        )
        sys.stderr.flush()

        cache["notified_for"] = latest
        cache["notified_at"] = datetime.now(timezone.utc).isoformat()
        _write_cache(cache)
    except Exception:
        pass

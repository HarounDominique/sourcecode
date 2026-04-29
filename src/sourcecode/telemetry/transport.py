"""Fire-and-forget HTTP transport for telemetry events.

Design principles:
  - Never blocks the main thread (daemon thread)
  - Never raises or prints errors to the user
  - Short timeout (3s) — drop and move on
  - No retries — a missed event is fine
  - Endpoint configurable via SOURCECODE_TELEMETRY_ENDPOINT env var
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any

_DEFAULT_ENDPOINT = "https://t.sourcecode.dev/v1/event"
_TIMEOUT_S = 3


def _endpoint() -> str:
    return os.environ.get("SOURCECODE_TELEMETRY_ENDPOINT", _DEFAULT_ENDPOINT)


def _send_blocking(payload: dict[str, Any]) -> None:
    """Blocking send — runs inside a daemon thread only."""
    try:
        import urllib.request

        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        req = urllib.request.Request(
            _endpoint(),
            data=data,
            headers={
                "Content-Type": "application/json",
                "User-Agent": f"sourcecode/{payload.get('v', 'unknown')}",
            },
            method="POST",
        )
        urllib.request.urlopen(req, timeout=_TIMEOUT_S)
    except Exception:
        pass  # always silent — a missed event is not an error


def send(payload: dict[str, Any]) -> None:
    """Dispatch payload to the telemetry endpoint in a background daemon thread.

    Returns immediately. The main process can exit without waiting.
    """
    t = threading.Thread(target=_send_blocking, args=(payload,), daemon=True)
    t.start()

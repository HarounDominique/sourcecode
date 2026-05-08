"""Lightweight terminal progress indicator for long-running commands.

Writes only to stderr. Zero-cost when stderr is not a TTY or CI is detected.
Thread-safe; stop() is idempotent.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from typing import Optional

_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")


def _terminal_capable() -> bool:
    if not sys.stderr.isatty():
        return False
    if os.environ.get("TERM") in ("dumb", ""):
        return False
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("CI"):
        return False
    return True


class Progress:
    """In-place spinner on stderr.

    Usage::

        p = Progress()
        p.start("scanning files")
        ...
        p.update("extracting contracts")
        ...
        p.finish()          # clears line, prints "✓ done (3.2s)"

    Always call stop() or finish() — both are idempotent.
    """

    def __init__(self) -> None:
        self._enabled = _terminal_capable()
        self._phase = ""
        self._t0 = time.monotonic()
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._stopped = False

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._t0

    def start(self, phase: str = "initializing") -> "Progress":
        self._t0 = time.monotonic()
        with self._lock:
            self._phase = phase
        self._stopped = False
        if not self._enabled:
            return self
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def update(self, phase: str) -> None:
        with self._lock:
            self._phase = phase

    def stop(self) -> float:
        """Stop and clear spinner. Returns elapsed seconds. Idempotent."""
        elapsed = self.elapsed
        if self._stopped:
            return elapsed
        self._stopped = True
        if not self._enabled:
            return elapsed
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        sys.stderr.write("\r\033[K")
        sys.stderr.flush()
        return elapsed

    def finish(self) -> None:
        """Stop spinner and print a completion line to stderr."""
        elapsed = self.stop()
        if not self._enabled:
            return
        t = f"{elapsed:.1f}s" if elapsed < 60 else f"{elapsed / 60:.1f}m"
        sys.stderr.write(f"✓ done ({t})\n")
        sys.stderr.flush()

    def _loop(self) -> None:
        idx = 0
        while not self._stop_event.wait(timeout=0.08):
            frame = _FRAMES[idx % len(_FRAMES)]
            elapsed = time.monotonic() - self._t0
            with self._lock:
                phase = self._phase
            line = f"\r{frame} {phase} ({elapsed:.1f}s)"
            try:
                sys.stderr.write(line)
                sys.stderr.flush()
            except Exception:
                break
            idx += 1

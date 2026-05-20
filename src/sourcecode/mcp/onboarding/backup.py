"""Timestamped backup management for MCP config files."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

_BACKUP_DIR = Path.home() / ".config" / "sourcecode" / "mcp-backups"


def _backup_stem(config_path: Path) -> str:
    """Stable prefix derived from the config path, safe for filenames."""
    parts = config_path.parts
    # Use last two path components to keep names readable but unique enough.
    label = "_".join(p for p in parts[-2:] if p).replace(".", "_")
    return label


def create(config_path: Path) -> Path:
    """Copy config_path to a timestamped backup file. Returns backup path."""
    _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    stem = _backup_stem(config_path)
    backup_path = _BACKUP_DIR / f"{stem}.{ts}.bak"
    backup_path.write_bytes(config_path.read_bytes())
    return backup_path


def restore(backup_path: Path, target_path: Path) -> None:
    """Overwrite target_path with contents of backup_path."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_bytes(backup_path.read_bytes())


def latest(config_path: Path) -> Path | None:
    """Find the most recent backup for config_path, or None."""
    if not _BACKUP_DIR.exists():
        return None
    stem = _backup_stem(config_path)
    matches = sorted(_BACKUP_DIR.glob(f"{stem}.*.bak"))
    return matches[-1] if matches else None

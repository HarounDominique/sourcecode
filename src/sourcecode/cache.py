"""
Snapshot cache manager for sourcecode — v2.

Cache layout
------------
    ~/.sourcecode/cache/<repo_id>/
        snapshot-<git_sha>-<flags_hash>.json.gz   ← versioned envelope
        cas/
            <blob_hash16>.gz                       ← content-addressed blobs

Schema
------
Every snapshot file is a gzip-compressed JSON *envelope*:

    {
      "sv":     "2",                       // schema version — bump to invalidate all
      "key":    "abc1234-aabbccdd",        // cache key (git_sha + flags_hash)
      "ts":     "2026-05-24T22:00:00Z",   // write timestamp (ISO-8601 UTC)
      "fmt":    "json",                    // output format: "json" | "yaml"
      "layers": {"heuristic": "...", ...}, // analyzer fingerprints at write time
      // ── content (one of two forms) ──────────────────────────────────────
      "snap":   {...},                     // inline fields (small) — JSON mode
      "cas":    {"file_paths": "<h16>",…}  // large fields deduped into CAS store
      // — OR —
      "raw":    "<content string>"         // YAML or unparseable JSON stored as-is
    }

Content-addressed store (CAS)
-----------------------------
Large top-level JSON fields (> _CAS_THRESHOLD bytes) are extracted into the
``cas/`` directory as individual gzip-compressed blobs identified by a 16-char
SHA-256 hash of their uncompressed bytes.  Two snapshots that share an
identical ``file_paths`` array reference the *same* blob — zero duplication.

Eviction / GC
-------------
After each write, ``_gc()`` keeps snapshots from the last
``SOURCECODE_CACHE_KEEP_COMMITS`` distinct git commits (default 5, override via
env var).  A CAS sweep runs concurrently: blobs unreferenced by any surviving
snapshot are deleted.

Backward compatibility
----------------------
v1 files (raw gzip'd content, no envelope) are detected by the absence of an
``sv`` key in the decompressed JSON, and served transparently.  Legacy files
in ``<repo>/.sourcecode-cache/`` are also checked as a final fallback.

Env vars
--------
  SOURCECODE_CACHE_DIR          Override global cache base (default: ~/.sourcecode/cache)
  SOURCECODE_CACHE_KEEP_COMMITS How many git commits to retain (default: 5; 0 = unlimited)
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Version / constants
# ---------------------------------------------------------------------------

#: Bump this string to invalidate *all* existing cached snapshots.
SCHEMA_VERSION: str = "2"

#: Bump to invalidate all L1 core caches (independent of snapshot version).
CORE_SCHEMA_VERSION: str = "1"

#: Fields eligible for CAS deduplication (applied to top-level JSON dict keys).
_CAS_FIELDS: frozenset[str] = frozenset([
    "file_paths",
    "entry_points",
    "docs",
    "dependencies",
    "graph",
    "semantic_calls",
    "semantic_symbols",
    "architecture",
    "metrics",
    "git_history",
    "env_map",
    "code_notes",
])

#: Serialised size threshold (bytes) above which a field is moved to CAS.
_CAS_THRESHOLD: int = 4096

_DEFAULT_KEEP_COMMITS: int = 5
_DEFAULT_MAX_CORES: int = 20
_DEFAULT_MAX_SIZE_MB: int = 50

# Matches "snapshot-<hex_commit>-<hex_flags>.json.gz"
_SNAPSHOT_RE = re.compile(r"^snapshot-([0-9a-f]+)-[0-9a-f]+\.json\.gz$")

# Matches "core-<hex_commit>-<hex_analysis>.json.gz"
_CORE_RE = re.compile(r"^core-([0-9a-f]+)-[0-9a-f]+\.json\.gz$")

# Matches "view-<hex_core_hash16>-<hex_view_flags>.json.gz"
_VIEW_RE = re.compile(r"^view-([0-9a-f]{16})-[0-9a-f]+\.json\.gz$")


# ---------------------------------------------------------------------------
# Public API — location helpers
# ---------------------------------------------------------------------------


def repo_id(repo_root: Path) -> str:
    """Stable 16-char hex identifier derived from the canonical repo path."""
    return hashlib.sha256(str(repo_root.resolve()).encode()).hexdigest()[:16]


def cache_dir(repo_root: Path) -> Path:
    """
    Return the per-repo cache directory (``~/.sourcecode/cache/<repo_id>/``).

    Override the base via ``SOURCECODE_CACHE_DIR``.
    """
    env_base = os.environ.get("SOURCECODE_CACHE_DIR", "")
    base: Path = Path(env_base) if env_base else Path.home() / ".sourcecode" / "cache"
    return base / repo_id(repo_root)


# ---------------------------------------------------------------------------
# Public API — observability
# ---------------------------------------------------------------------------

def status(repo_root: Path) -> dict[str, Any]:
    """Return a stats dict describing the current cache state for *repo_root*.

    Keys: ``cache_dir``, ``cores``, ``snapshots``, ``views``, ``cas_blobs``,
    ``total_size_bytes``, ``total_size_mb``.
    """
    cache_d = cache_dir(repo_root)
    if not cache_d.exists():
        return {
            "cache_dir": str(cache_d),
            "cores": 0, "snapshots": 0, "views": 0, "cas_blobs": 0,
            "total_size_bytes": 0, "total_size_mb": 0.0,
        }
    cores = list(cache_d.glob("core-*.json.gz"))
    snapshots = list(cache_d.glob("snapshot-*.json.gz"))
    views = list(cache_d.glob("view-*.json.gz"))
    cas_blobs = list((_cas_dir(cache_d)).glob("*.gz")) if _cas_dir(cache_d).exists() else []
    all_files = cores + snapshots + views + cas_blobs
    total_bytes = sum(f.stat().st_size for f in all_files if f.exists())
    return {
        "cache_dir": str(cache_d),
        "cores": len(cores),
        "snapshots": len(snapshots),
        "views": len(views),
        "cas_blobs": len(cas_blobs),
        "total_size_bytes": total_bytes,
        "total_size_mb": round(total_bytes / (1024 * 1024), 2),
    }


def clear(repo_root: Path) -> int:
    """Delete all cache files for *repo_root*.  Returns the number of files removed."""
    cache_d = cache_dir(repo_root)
    if not cache_d.exists():
        return 0
    removed = 0
    for pattern in ("core-*.json.gz", "snapshot-*.json.gz", "view-*.json.gz"):
        for f in cache_d.glob(pattern):
            _safe_unlink(f)
            removed += 1
    cas_d = _cas_dir(cache_d)
    if cas_d.exists():
        for f in cas_d.glob("*.gz"):
            _safe_unlink(f)
            removed += 1
    return removed


# ---------------------------------------------------------------------------
# Public API — read / write
# ---------------------------------------------------------------------------

def read(repo_root: Path, cache_key: str) -> Optional[str]:
    """
    Return the cached snapshot string for *cache_key*, or ``None`` on miss.

    Lookup order:
    1. ``<cache_dir>/snapshot-<cache_key>.json.gz``  — v2 envelope (new)
    2. ``<repo_root>/.sourcecode-cache/snapshot-<cache_key>.json``  — legacy
    """
    cache_d = cache_dir(repo_root)

    # ── 1. Global location (.json.gz, v2 envelope or v1 raw) ───────────────
    gz_path = cache_d / f"snapshot-{cache_key}.json.gz"
    if gz_path.exists():
        try:
            result = _parse_envelope(gz_path.read_bytes(), cache_d)
            if result is not None:
                return result
        except Exception:
            pass
        _safe_unlink(gz_path)  # corrupted or version mismatch — evict
        return None

    # ── 2. Legacy location (<repo>/.sourcecode-cache/*.json) ───────────────
    legacy = repo_root / ".sourcecode-cache" / f"snapshot-{cache_key}.json"
    if legacy.exists():
        try:
            return legacy.read_text(encoding="utf-8")
        except Exception:
            return None

    return None


def write(
    repo_root: Path,
    cache_key: str,
    content: str,
    *,
    fmt: str = "json",
    layers: Optional[dict[str, str]] = None,
) -> None:
    """
    Persist *content* as a versioned, optionally CAS-deduped snapshot.

    Parameters
    ----------
    repo_root : Path
        Root directory of the analysed repository.
    cache_key : str
        ``"{git_sha}-{flags_hash}"`` identifying this analysis.
    content : str
        Final rendered output (JSON or YAML string).
    fmt : str
        ``"json"`` or ``"yaml"`` — determines whether CAS extraction applies.
    layers : dict[str, str], optional
        Analyzer fingerprints (from ``_compute_analyzer_fingerprints()``).
        Stored in the envelope for future layer-aware reuse.

    Writes are always best-effort: any failure is silently swallowed.
    """
    cache_d = cache_dir(repo_root)
    dest = cache_d / f"snapshot-{cache_key}.json.gz"
    try:
        cache_d.mkdir(parents=True, exist_ok=True)
        payload = _build_envelope(cache_key, content, fmt, layers or {}, cache_d)
        _atomic_write(dest, payload)
    except Exception:
        return  # non-fatal

    _gc(cache_d)


# ---------------------------------------------------------------------------
# Layer 1 — Core Analysis cache
# ---------------------------------------------------------------------------

def read_core(repo_root: Path, core_key: str) -> Optional[tuple[dict[str, Any], str]]:
    """Read core analysis artifacts from L1 cache.

    Returns ``(core_dict, core_hash)`` on hit, or ``None`` on miss.
    ``core_hash`` is the 16-char SHA-256 of the stored core JSON, used as
    the L2 view-key prefix so that different views of the same core share
    a common ancestry without a full re-analysis.
    """
    cache_d = cache_dir(repo_root)
    gz_path = cache_d / f"core-{core_key}.json.gz"
    if not gz_path.exists():
        return None
    try:
        raw_bytes = gzip.decompress(gz_path.read_bytes())
        envelope = json.loads(raw_bytes.decode("utf-8"))
    except Exception:
        _safe_unlink(gz_path)
        return None

    if not isinstance(envelope, dict):
        _safe_unlink(gz_path)
        return None
    if envelope.get("csv") != CORE_SCHEMA_VERSION:
        _safe_unlink(gz_path)  # schema mismatch — evict
        return None

    core_data = envelope.get("data")
    core_hash = envelope.get("hash", "")
    if not isinstance(core_data, dict) or not core_hash:
        _safe_unlink(gz_path)
        return None

    return core_data, core_hash


def write_core(repo_root: Path, core_key: str, core_data: dict[str, Any]) -> str:
    """Persist core analysis dict to L1 cache.

    Returns the 16-char SHA-256 hash of the core JSON (the L2 key prefix).
    Writes are always best-effort; failures are silently swallowed.

    File layout::

        ~/.sourcecode/cache/<repo_id>/core-<core_key>.json.gz

    Envelope schema::

        { "csv": "1",       // CORE_SCHEMA_VERSION
          "key": "...",     // core_key passed in
          "hash": "<h16>",  // SHA-256[:16] of core JSON — used as L2 prefix
          "ts":  "...",     // ISO-8601 UTC write time
          "data": {...} }   // core_view(sm) dict
    """
    core_json = json.dumps(core_data, ensure_ascii=False)
    core_hash = hashlib.sha256(core_json.encode()).hexdigest()[:16]

    cache_d = cache_dir(repo_root)
    dest = cache_d / f"core-{core_key}.json.gz"
    try:
        cache_d.mkdir(parents=True, exist_ok=True)
        envelope: dict[str, Any] = {
            "csv": CORE_SCHEMA_VERSION,
            "key": core_key,
            "hash": core_hash,
            "ts": _now_iso(),
            "data": core_data,
        }
        payload = gzip.compress(
            json.dumps(envelope, ensure_ascii=False).encode("utf-8"),
            compresslevel=6,
        )
        _atomic_write(dest, payload)
    except Exception:
        pass

    return core_hash


# ---------------------------------------------------------------------------
# Layer 2 — Derived View cache
# ---------------------------------------------------------------------------

def read_view(repo_root: Path, view_key: str) -> Optional[str]:
    """Read a rendered view string from L2 cache.

    Views are stored as ``view-{view_key}.json.gz`` using the same
    envelope+CAS format as snapshot files.  Returns the content string
    (JSON or YAML) or ``None`` on miss.
    """
    cache_d = cache_dir(repo_root)
    gz_path = cache_d / f"view-{view_key}.json.gz"
    if not gz_path.exists():
        return None
    try:
        result = _parse_envelope(gz_path.read_bytes(), cache_d)
        if result is not None:
            return result
    except Exception:
        pass
    _safe_unlink(gz_path)
    return None


def write_view(
    repo_root: Path,
    view_key: str,
    content: str,
    *,
    fmt: str = "json",
    layers: Optional[dict[str, str]] = None,
) -> None:
    """Persist a rendered view string to L2 cache as ``view-{view_key}.json.gz``.

    Reuses the envelope+CAS infrastructure so large fields (file_paths,
    graph, docs …) are automatically deduplicated with other snapshots/views.
    Writes are always best-effort; GC is **not** triggered here — callers
    that want eviction should invoke ``_gc(cache_dir(repo_root))`` explicitly.
    """
    cache_d = cache_dir(repo_root)
    dest = cache_d / f"view-{view_key}.json.gz"
    try:
        cache_d.mkdir(parents=True, exist_ok=True)
        payload = _build_envelope(view_key, content, fmt, layers or {}, cache_d)
        _atomic_write(dest, payload)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Envelope (de)serialisation
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_envelope(
    cache_key: str,
    content: str,
    fmt: str,
    layers: dict[str, str],
    cache_d: Path,
) -> bytes:
    """Build a versioned envelope and return gzip-compressed bytes."""
    envelope: dict[str, Any] = {
        "sv": SCHEMA_VERSION,
        "key": cache_key,
        "ts": _now_iso(),
        "fmt": fmt,
        "layers": layers,
    }

    if fmt == "json":
        # Try to parse and extract large fields into CAS
        try:
            snap_dict = json.loads(content)
            if isinstance(snap_dict, dict):
                inline, cas_refs = _cas_extract(snap_dict, cache_d)
                envelope["snap"] = inline
                if cas_refs:
                    envelope["cas"] = cas_refs
            else:
                # JSON array or primitive — store as-is
                envelope["raw"] = content
        except Exception:
            envelope["raw"] = content
    else:
        # YAML or unknown format — store raw string
        envelope["raw"] = content

    return gzip.compress(
        json.dumps(envelope, ensure_ascii=False).encode("utf-8"),
        compresslevel=6,
    )


def _parse_envelope(data: bytes, cache_d: Path) -> Optional[str]:
    """
    Decompress *data*, parse envelope, resolve CAS refs, return content string.

    Returns ``None`` on schema version mismatch, CAS miss, or parse failure.
    v1 files (no envelope wrapper) are detected and served transparently.
    """
    try:
        raw_bytes = gzip.decompress(data)
    except Exception:
        return None

    # ── v1 detection ────────────────────────────────────────────────────────
    # v1 stored the content string directly (gzip'd UTF-8), not an envelope.
    # Heuristic: if decompressed bytes are not a JSON object with an "sv" key,
    # treat as v1 and return the raw bytes as the content string.
    try:
        envelope = json.loads(raw_bytes.decode("utf-8"))
    except Exception:
        # Not JSON at all (e.g. YAML v1) — return as-is
        try:
            return raw_bytes.decode("utf-8")
        except Exception:
            return None

    if not isinstance(envelope, dict) or envelope.get("sv") != SCHEMA_VERSION:
        # dict without "sv" → v1 JSON snapshot; non-matching sv → old envelope
        # Serve v1 transparently; reject mismatched schema versions as a miss.
        if isinstance(envelope, dict) and "sv" in envelope:
            return None  # schema version mismatch
        # No "sv" at all → v1 format, raw content
        return raw_bytes.decode("utf-8")

    # ── v2 envelope ─────────────────────────────────────────────────────────
    if "raw" in envelope:
        return envelope["raw"]

    if "snap" in envelope:
        inline: dict[str, Any] = envelope["snap"]
        cas_refs: dict[str, str] = envelope.get("cas", {})
        if cas_refs:
            restored = _cas_restore(inline, cas_refs, cache_d)
            if restored is None:
                return None  # CAS miss (blob evicted or corrupted)
        else:
            restored = dict(inline)
        # Re-serialise with the same parameters used by the pipeline.
        # json.loads → json.dumps round-trips correctly: Python 3.7+ preserves
        # dict insertion order and the pipeline uses indent=2, ensure_ascii=False.
        return json.dumps(restored, indent=2, ensure_ascii=False)

    return None  # malformed envelope


# ---------------------------------------------------------------------------
# CAS store
# ---------------------------------------------------------------------------

def _cas_dir(cache_d: Path) -> Path:
    return cache_d / "cas"


def _cas_path(cache_d: Path, blob_hash: str) -> Path:
    return _cas_dir(cache_d) / f"{blob_hash}.gz"


def _cas_store_blob(cache_d: Path, serialised: str) -> str:
    """
    Store *serialised* (a JSON string) in the CAS.  Idempotent.

    Returns the 16-char SHA-256 hex hash that identifies the blob.
    """
    raw = serialised.encode("utf-8")
    blob_hash = hashlib.sha256(raw).hexdigest()[:16]
    path = _cas_path(cache_d, blob_hash)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(gzip.compress(raw, compresslevel=6))
    return blob_hash


def _cas_load_blob(cache_d: Path, blob_hash: str) -> Optional[str]:
    """Return the stored JSON string for *blob_hash*, or ``None`` if absent."""
    path = _cas_path(cache_d, blob_hash)
    if not path.exists():
        return None
    try:
        return gzip.decompress(path.read_bytes()).decode("utf-8")
    except Exception:
        _safe_unlink(path)  # evict corrupted blob so it doesn't block future reads
        return None


def _cas_extract(
    snap_dict: dict[str, Any],
    cache_d: Path,
) -> tuple[dict[str, Any], dict[str, str]]:
    """
    Walk *snap_dict* top-level fields.  Fields that:
      - are in ``_CAS_FIELDS``
      - serialise to more than ``_CAS_THRESHOLD`` bytes

    … are stored as CAS blobs and replaced with their hash in the returned
    ``cas_refs`` mapping.  Other fields remain inline.
    """
    inline: dict[str, Any] = {}
    cas_refs: dict[str, str] = {}

    for key, value in snap_dict.items():
        if key in _CAS_FIELDS and value is not None:
            serialised = json.dumps(value, ensure_ascii=False)
            if len(serialised.encode("utf-8")) > _CAS_THRESHOLD:
                blob_hash = _cas_store_blob(cache_d, serialised)
                cas_refs[key] = blob_hash
                continue
        inline[key] = value

    return inline, cas_refs


def _cas_restore(
    inline: dict[str, Any],
    cas_refs: dict[str, str],
    cache_d: Path,
) -> Optional[dict[str, Any]]:
    """
    Reconstruct a full snapshot dict by loading CAS blobs for *cas_refs*.

    Returns ``None`` if any blob is missing (treat as cache miss).
    """
    result: dict[str, Any] = dict(inline)
    for field, blob_hash in cas_refs.items():
        blob_str = _cas_load_blob(cache_d, blob_hash)
        if blob_str is None:
            return None  # blob evicted or corrupted → full miss
        try:
            result[field] = json.loads(blob_str)
        except Exception:
            return None
    return result


# ---------------------------------------------------------------------------
# Eviction / GC
# ---------------------------------------------------------------------------

def _gc(cache_d: Path) -> None:
    """Evict old snapshots/cores/views and sweep orphaned CAS blobs.

    Three eviction passes (all non-fatal):
    1. Commit-based: keep only last SOURCECODE_CACHE_KEEP_COMMITS distinct SHAs.
    2. Core-count: keep at most SOURCECODE_CACHE_MAX_CORES core files (LRU).
    3. Size-based: if total cache exceeds SOURCECODE_CACHE_MAX_SIZE_MB, evict
       oldest core+snapshot files until under budget.
    Views and CAS blobs are swept after each pass.
    """
    keep = int(os.environ.get("SOURCECODE_CACHE_KEEP_COMMITS", _DEFAULT_KEEP_COMMITS))
    max_cores = int(os.environ.get("SOURCECODE_CACHE_MAX_CORES", _DEFAULT_MAX_CORES))
    max_size_bytes = int(os.environ.get("SOURCECODE_CACHE_MAX_SIZE_MB", _DEFAULT_MAX_SIZE_MB)) * 1024 * 1024

    try:
        all_snapshots = list(cache_d.glob("snapshot-*.json.gz"))
        all_cores = list(cache_d.glob("core-*.json.gz"))
        all_views = list(cache_d.glob("view-*.json.gz"))

        if not all_snapshots and not all_cores and not all_views:
            return

        # ── Pass 1: commit-based eviction ──────────────────────────────────
        groups: dict[str, list[Path]] = {}
        for f in all_snapshots:
            m = _SNAPSHOT_RE.match(f.name)
            if m:
                groups.setdefault(m.group(1), []).append(f)
        for f in all_cores:
            m = _CORE_RE.match(f.name)
            if m:
                groups.setdefault(m.group(1), []).append(f)

        surviving: list[Path]

        if keep <= 0 or len(groups) <= keep:
            surviving = all_snapshots + all_cores
        else:
            def _newest_mtime(commit: str) -> float:
                return max(p.stat().st_mtime for p in groups[commit])

            sorted_commits = sorted(groups, key=_newest_mtime, reverse=True)
            surviving = []
            for i, commit in enumerate(sorted_commits):
                if i < keep:
                    surviving.extend(groups[commit])
                else:
                    for f in groups[commit]:
                        _safe_unlink(f)

        # ── Pass 2: per-repo core count cap ────────────────────────────────
        if max_cores > 0:
            surviving_cores = [p for p in surviving if p.name.startswith("core-") and p.exists()]
            if len(surviving_cores) > max_cores:
                surviving_cores.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                for evict in surviving_cores[max_cores:]:
                    _safe_unlink(evict)
                    surviving = [p for p in surviving if p != evict]

        # ── Pass 3: total size cap ──────────────────────────────────────────
        if max_size_bytes > 0:
            size_candidates = [p for p in surviving if p.exists()]
            total = sum(p.stat().st_size for p in size_candidates if not p.name.startswith("view-"))
            if total > max_size_bytes:
                # Sort oldest-first; evict core+snapshot files until under budget
                size_candidates.sort(key=lambda p: p.stat().st_mtime)
                for evict in size_candidates:
                    if evict.name.startswith("view-"):
                        continue
                    total -= evict.stat().st_size if evict.exists() else 0
                    _safe_unlink(evict)
                    surviving = [p for p in surviving if p != evict]
                    if total <= max_size_bytes:
                        break

        # Prune view files whose core hash is no longer in the surviving set
        all_views = list(cache_d.glob("view-*.json.gz"))
        _gc_views(cache_d, surviving, all_views)

        # Sweep orphaned CAS blobs (surviving snapshots + view files may ref them)
        surviving_with_views = surviving + [v for v in all_views if v.exists()]
        _gc_cas(cache_d, surviving_with_views)

    except Exception:
        pass  # GC failure is non-fatal


def _gc_views(cache_d: Path, surviving: list[Path], all_views: list[Path]) -> None:
    """Delete view files not traceable to a surviving core.

    Collects the ``hash`` field from every surviving core envelope, then
    deletes view files whose filename core-hash prefix is absent from that
    set.  View files with unrecognisable names are left untouched.
    """
    if not all_views:
        return

    # Collect live core hashes from surviving core-*.json.gz files
    live_hashes: set[str] = set()
    for path in surviving:
        if not path.name.startswith("core-"):
            continue
        try:
            env = json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
            h = env.get("hash", "")
            if h:
                live_hashes.add(h)
        except Exception:
            pass  # unreadable core — conservatively keep its views unknown

    for vp in all_views:
        m = _VIEW_RE.match(vp.name)
        if m and m.group(1) not in live_hashes:
            _safe_unlink(vp)


def _gc_cas(cache_d: Path, surviving_snapshots: list[Path]) -> None:
    """
    Delete CAS blobs not referenced by any snapshot in *surviving_snapshots*.

    Walks each snapshot's ``cas`` dict to collect live hashes; deletes the rest.
    """
    cas_d = _cas_dir(cache_d)
    if not cas_d.exists():
        return

    try:
        # Collect all hashes referenced by surviving snapshots
        referenced: set[str] = set()
        for snap_path in surviving_snapshots:
            try:
                raw = gzip.decompress(snap_path.read_bytes())
                env = json.loads(raw.decode("utf-8"))
                if isinstance(env, dict) and "cas" in env:
                    referenced.update(env["cas"].values())
            except Exception:
                pass  # unreadable snapshot — conservatively keep its blobs unknown

        # Delete blobs not referenced by any surviving snapshot
        for blob in cas_d.glob("*.gz"):
            if blob.stem not in referenced:
                _safe_unlink(blob)

    except Exception:
        pass  # CAS sweep failure is non-fatal


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _atomic_write(dest: Path, data: bytes) -> None:
    """Write *data* to *dest* atomically via a sibling .tmp file + rename.

    On POSIX, ``Path.replace()`` is a single ``rename(2)`` syscall — the
    destination either has the old content or the new content, never a partial
    write.  The .tmp suffix keeps the partial file out of glob patterns used
    by the cache reader and GC.
    """
    tmp = dest.with_suffix(".tmp")
    try:
        tmp.write_bytes(data)
        tmp.replace(dest)
    except Exception:
        _safe_unlink(tmp)
        raise


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass

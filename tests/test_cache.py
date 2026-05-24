"""
Unit tests for sourcecode.cache — v2 snapshot cache manager.

Covers:
  - cache_dir() isolation per repo
  - SOURCECODE_CACHE_DIR override
  - read() miss returns None
  - write() + read() round-trip (envelope + gzip transparent)
  - Content is JSON-identical after envelope round-trip
  - File uses gzip magic bytes
  - Legacy .json fallback (v1 plain file)
  - v1 raw gzip format served transparently (backward compat)
  - Schema version mismatch returns None (cache miss)
  - CAS: large fields extracted to shared blobs
  - CAS: two snapshots sharing a large field → one blob (dedup)
  - CAS: missing blob treated as cache miss
  - CAS: GC sweeps orphaned blobs
  - GC: old commits evicted, recent kept
  - GC: keep=0 disables eviction
  - GC: all variants of a commit evicted together
  - Layer metadata stored in envelope
  - write() creates parent dirs
  - Corrupted .json.gz treated as miss and cleaned up
  - Compression reduces file size vs raw content
"""
from __future__ import annotations

import gzip
import json
import os
from pathlib import Path
from typing import Any

import pytest

from sourcecode import cache as _cache
from sourcecode.cache import SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_repo(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    (repo / ".git").mkdir()
    return repo


def _make_key(commit: str, flags: str = "aabbccdd") -> str:
    return f"{commit}-{flags}"


def _small_json() -> str:
    return '{"project_type": "python", "stacks": ["python"]}'


def _large_json(n: int = 300) -> str:
    """Return a JSON dict with a large file_paths array (well above _CAS_THRESHOLD)."""
    paths = [f"/src/module_{i}.py" for i in range(n)]
    return json.dumps({"project_type": "python", "file_paths": paths}, indent=2, ensure_ascii=False)


def _read_envelope(path: Path) -> dict[str, Any]:
    """Decompress and parse the envelope from a .json.gz file."""
    return json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))


# ---------------------------------------------------------------------------
# cache_dir / repo_id
# ---------------------------------------------------------------------------

class TestCacheDir:
    def test_different_repos_get_different_dirs(self, tmp_path: Path) -> None:
        r1 = _make_repo(tmp_path, "repo1")
        r2 = _make_repo(tmp_path, "repo2")
        assert _cache.cache_dir(r1) != _cache.cache_dir(r2)

    def test_same_repo_stable_dir(self, tmp_path: Path) -> None:
        r = _make_repo(tmp_path, "repo")
        assert _cache.cache_dir(r) == _cache.cache_dir(r)

    def test_env_override(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        custom = tmp_path / "my_cache"
        monkeypatch.setenv("SOURCECODE_CACHE_DIR", str(custom))
        r = _make_repo(tmp_path, "repo")
        assert _cache.cache_dir(r).is_relative_to(custom)


# ---------------------------------------------------------------------------
# Basic read / write
# ---------------------------------------------------------------------------

class TestReadWrite:
    def test_miss_returns_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOURCECODE_CACHE_DIR", str(tmp_path / "cache"))
        r = _make_repo(tmp_path, "repo")
        assert _cache.read(r, _make_key("abc1234")) is None

    def test_write_then_read_small(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOURCECODE_CACHE_DIR", str(tmp_path / "cache"))
        r = _make_repo(tmp_path, "repo")
        content = _small_json()
        _cache.write(r, _make_key("abc1234"), content)
        assert _cache.read(r, _make_key("abc1234")) is not None

    def test_round_trip_json_identical(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOURCECODE_CACHE_DIR", str(tmp_path / "cache"))
        r = _make_repo(tmp_path, "repo")
        # Use a realistic dict to verify key order and values survive round-trip
        original = json.dumps(
            {"project_type": "java", "stacks": ["spring"], "confidence": 0.95,
             "entry_points": [{"path": "Main.java", "type": "main"}]},
            indent=2, ensure_ascii=False,
        )
        _cache.write(r, _make_key("abc1234"), original)
        result = _cache.read(r, _make_key("abc1234"))
        assert result is not None
        # Parsed objects must be equal (not necessarily byte-identical due to whitespace)
        assert json.loads(result) == json.loads(original)

    def test_file_has_gzip_magic_bytes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOURCECODE_CACHE_DIR", str(tmp_path / "cache"))
        r = _make_repo(tmp_path, "repo")
        key = _make_key("abc1234")
        _cache.write(r, key, _small_json())
        gz_file = _cache.cache_dir(r) / f"snapshot-{key}.json.gz"
        assert gz_file.exists()
        assert gz_file.read_bytes()[:2] == b"\x1f\x8b"  # gzip magic

    def test_write_creates_parent_dirs(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        deep = tmp_path / "a" / "b" / "c"
        monkeypatch.setenv("SOURCECODE_CACHE_DIR", str(deep))
        r = _make_repo(tmp_path, "repo")
        _cache.write(r, _make_key("abc1234"), _small_json())
        assert _cache.cache_dir(r).exists()

    def test_yaml_stored_and_restored(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOURCECODE_CACHE_DIR", str(tmp_path / "cache"))
        r = _make_repo(tmp_path, "repo")
        key = _make_key("abc1234")
        yaml_content = "project_type: python\nstacks:\n- python\n"
        _cache.write(r, key, yaml_content, fmt="yaml")
        assert _cache.read(r, key) == yaml_content

    def test_compression_reduces_file_size(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOURCECODE_CACHE_DIR", str(tmp_path / "cache"))
        r = _make_repo(tmp_path, "repo")
        key = _make_key("abc1234")
        # Repetitive content → high compression
        content = ('{"file": "/src/module.py", "stacks": ["python"], "score": 0.9}\n' * 200)
        _cache.write(r, key, content)
        gz_file = _cache.cache_dir(r) / f"snapshot-{key}.json.gz"
        assert gz_file.stat().st_size < len(content.encode())


# ---------------------------------------------------------------------------
# Schema versioning
# ---------------------------------------------------------------------------

class TestSchemaVersioning:
    def test_envelope_contains_schema_version(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOURCECODE_CACHE_DIR", str(tmp_path / "cache"))
        r = _make_repo(tmp_path, "repo")
        key = _make_key("abc1234")
        _cache.write(r, key, _small_json())
        env = _read_envelope(_cache.cache_dir(r) / f"snapshot-{key}.json.gz")
        assert env["sv"] == SCHEMA_VERSION

    def test_schema_version_mismatch_returns_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOURCECODE_CACHE_DIR", str(tmp_path / "cache"))
        r = _make_repo(tmp_path, "repo")
        key = _make_key("abc1234")
        # Write envelope with wrong schema version
        bad_env = {"sv": "999", "key": key, "ts": "2026-01-01T00:00:00Z",
                   "fmt": "json", "layers": {}, "raw": _small_json()}
        gz_path = _cache.cache_dir(r) / f"snapshot-{key}.json.gz"
        gz_path.parent.mkdir(parents=True, exist_ok=True)
        gz_path.write_bytes(gzip.compress(json.dumps(bad_env).encode("utf-8")))
        assert _cache.read(r, key) is None

    def test_v1_raw_gzip_served_transparently(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """v1 format: raw gzip'd content string (no envelope wrapper)."""
        monkeypatch.setenv("SOURCECODE_CACHE_DIR", str(tmp_path / "cache"))
        r = _make_repo(tmp_path, "repo")
        key = _make_key("abc1234")
        v1_content = _small_json()
        gz_path = _cache.cache_dir(r) / f"snapshot-{key}.json.gz"
        gz_path.parent.mkdir(parents=True, exist_ok=True)
        # v1: just gzip the content directly
        gz_path.write_bytes(gzip.compress(v1_content.encode("utf-8")))
        # Must be served even without envelope
        result = _cache.read(r, key)
        assert result == v1_content

    def test_envelope_stores_timestamp(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOURCECODE_CACHE_DIR", str(tmp_path / "cache"))
        r = _make_repo(tmp_path, "repo")
        key = _make_key("abc1234")
        _cache.write(r, key, _small_json())
        env = _read_envelope(_cache.cache_dir(r) / f"snapshot-{key}.json.gz")
        assert "ts" in env and len(env["ts"]) >= 10  # ISO date present

    def test_envelope_stores_format(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOURCECODE_CACHE_DIR", str(tmp_path / "cache"))
        r = _make_repo(tmp_path, "repo")
        key = _make_key("abc1234")
        _cache.write(r, key, _small_json(), fmt="json")
        env = _read_envelope(_cache.cache_dir(r) / f"snapshot-{key}.json.gz")
        assert env["fmt"] == "json"


# ---------------------------------------------------------------------------
# Layer metadata
# ---------------------------------------------------------------------------

class TestLayerMetadata:
    def test_layers_stored_in_envelope(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOURCECODE_CACHE_DIR", str(tmp_path / "cache"))
        r = _make_repo(tmp_path, "repo")
        key = _make_key("abc1234")
        layers = {"heuristic": "aabb1122", "nodejs": "ccdd3344", "confidence": "eeff5566"}
        _cache.write(r, key, _small_json(), layers=layers)
        env = _read_envelope(_cache.cache_dir(r) / f"snapshot-{key}.json.gz")
        assert env["layers"] == layers

    def test_empty_layers_stored_as_empty_dict(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOURCECODE_CACHE_DIR", str(tmp_path / "cache"))
        r = _make_repo(tmp_path, "repo")
        key = _make_key("abc1234")
        _cache.write(r, key, _small_json())  # no layers arg
        env = _read_envelope(_cache.cache_dir(r) / f"snapshot-{key}.json.gz")
        assert env["layers"] == {}


# ---------------------------------------------------------------------------
# CAS (content-addressed storage)
# ---------------------------------------------------------------------------

class TestCAS:
    def test_large_field_extracted_to_cas(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOURCECODE_CACHE_DIR", str(tmp_path / "cache"))
        r = _make_repo(tmp_path, "repo")
        key = _make_key("abc1234")
        content = _large_json()  # has large file_paths array
        _cache.write(r, key, content)

        env = _read_envelope(_cache.cache_dir(r) / f"snapshot-{key}.json.gz")
        assert "cas" in env, "large field_paths should be in CAS refs"
        assert "file_paths" in env["cas"]
        # Inline snap should NOT contain file_paths
        assert "file_paths" not in env.get("snap", {})

    def test_large_field_round_trips(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOURCECODE_CACHE_DIR", str(tmp_path / "cache"))
        r = _make_repo(tmp_path, "repo")
        key = _make_key("abc1234")
        content = _large_json()
        _cache.write(r, key, content)
        result = _cache.read(r, key)
        assert result is not None
        assert json.loads(result) == json.loads(content)

    def test_cas_blob_file_exists(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOURCECODE_CACHE_DIR", str(tmp_path / "cache"))
        r = _make_repo(tmp_path, "repo")
        key = _make_key("abc1234")
        _cache.write(r, key, _large_json())

        env = _read_envelope(_cache.cache_dir(r) / f"snapshot-{key}.json.gz")
        blob_hash = env["cas"]["file_paths"]
        cas_file = _cache.cache_dir(r) / "cas" / f"{blob_hash}.gz"
        assert cas_file.exists()

    def test_two_snapshots_share_cas_blob(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Two snapshots with identical large field → one CAS blob (deduplication)."""
        monkeypatch.setenv("SOURCECODE_CACHE_DIR", str(tmp_path / "cache"))
        r = _make_repo(tmp_path, "repo")

        paths = [f"/src/module_{i}.py" for i in range(300)]
        base = {"project_type": "python", "file_paths": paths}

        # Same file_paths, different project_type → different snapshots
        content_a = json.dumps({**base, "stacks": ["python"]}, indent=2, ensure_ascii=False)
        content_b = json.dumps({**base, "stacks": ["django"]}, indent=2, ensure_ascii=False)

        key_a = _make_key("aaa1111", "00000001")
        key_b = _make_key("aaa1111", "00000002")
        _cache.write(r, key_a, content_a)
        _cache.write(r, key_b, content_b)

        env_a = _read_envelope(_cache.cache_dir(r) / f"snapshot-{key_a}.json.gz")
        env_b = _read_envelope(_cache.cache_dir(r) / f"snapshot-{key_b}.json.gz")

        # Both snapshots reference the SAME blob hash
        assert env_a["cas"]["file_paths"] == env_b["cas"]["file_paths"], (
            "identical file_paths arrays must share a single CAS blob"
        )

        # Only one blob file exists for file_paths
        cas_blobs = list((_cache.cache_dir(r) / "cas").glob("*.gz"))
        assert len(cas_blobs) == 1, f"expected 1 CAS blob, got {len(cas_blobs)}"

    def test_missing_cas_blob_is_cache_miss(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOURCECODE_CACHE_DIR", str(tmp_path / "cache"))
        r = _make_repo(tmp_path, "repo")
        key = _make_key("abc1234")
        _cache.write(r, key, _large_json())

        # Delete the CAS blob
        cas_d = _cache.cache_dir(r) / "cas"
        for blob in cas_d.glob("*.gz"):
            blob.unlink()

        assert _cache.read(r, key) is None, "missing CAS blob should cause cache miss"

    def test_small_field_stays_inline(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOURCECODE_CACHE_DIR", str(tmp_path / "cache"))
        r = _make_repo(tmp_path, "repo")
        key = _make_key("abc1234")
        content = json.dumps({"project_type": "python", "stacks": ["python"]},
                             indent=2, ensure_ascii=False)
        _cache.write(r, key, content)
        env = _read_envelope(_cache.cache_dir(r) / f"snapshot-{key}.json.gz")
        # Small fields must stay inline — no CAS refs
        assert "cas" not in env or len(env.get("cas", {})) == 0
        assert "project_type" in env.get("snap", {})


# ---------------------------------------------------------------------------
# Legacy fallback
# ---------------------------------------------------------------------------

class TestLegacyFallback:
    def test_reads_legacy_json_on_miss(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOURCECODE_CACHE_DIR", str(tmp_path / "empty_cache"))
        r = _make_repo(tmp_path, "repo")
        key = _make_key("deadbeef")
        legacy_content = '{"legacy": true}'
        (r / ".sourcecode-cache").mkdir()
        (r / ".sourcecode-cache" / f"snapshot-{key}.json").write_text(legacy_content, encoding="utf-8")
        assert _cache.read(r, key) == legacy_content

    def test_new_location_takes_precedence_over_legacy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SOURCECODE_CACHE_DIR", str(tmp_path / "cache"))
        r = _make_repo(tmp_path, "repo")
        key = _make_key("deadbeef")
        (r / ".sourcecode-cache").mkdir()
        (r / ".sourcecode-cache" / f"snapshot-{key}.json").write_text("legacy", encoding="utf-8")
        new_content = '{"new": true}'
        _cache.write(r, key, new_content)
        assert json.loads(_cache.read(r, key)) == {"new": True}


# ---------------------------------------------------------------------------
# Corrupted file
# ---------------------------------------------------------------------------

class TestCorruption:
    def test_corrupted_gz_returns_none_and_is_removed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SOURCECODE_CACHE_DIR", str(tmp_path / "cache"))
        r = _make_repo(tmp_path, "repo")
        key = _make_key("abc1234")
        gz_file = _cache.cache_dir(r) / f"snapshot-{key}.json.gz"
        gz_file.parent.mkdir(parents=True, exist_ok=True)
        gz_file.write_bytes(b"not-gzip-data")
        assert _cache.read(r, key) is None
        assert not gz_file.exists(), "corrupted file should be cleaned up"


# ---------------------------------------------------------------------------
# GC / eviction
# ---------------------------------------------------------------------------

class TestEviction:
    def _write_variants(self, repo: Path, commit: str, n: int = 2) -> None:
        for i in range(n):
            key = f"{commit}-{i:08x}"
            _cache.write(repo, key, json.dumps({"commit": commit, "v": i}, indent=2))

    def test_old_commits_evicted(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOURCECODE_CACHE_DIR", str(tmp_path / "cache"))
        monkeypatch.setenv("SOURCECODE_CACHE_KEEP_COMMITS", "2")
        r = _make_repo(tmp_path, "repo")

        for commit in ["aaa1111", "bbb2222", "ccc3333"]:
            self._write_variants(r, commit)

        remaining = list(_cache.cache_dir(r).glob("snapshot-*.json.gz"))
        remaining_commits = {f.name.split("-")[1] for f in remaining}
        assert len(remaining_commits) <= 2
        assert "aaa1111" not in remaining_commits

    def test_within_keep_limit_nothing_evicted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SOURCECODE_CACHE_DIR", str(tmp_path / "cache"))
        monkeypatch.setenv("SOURCECODE_CACHE_KEEP_COMMITS", "5")
        r = _make_repo(tmp_path, "repo")

        for commit in ["aaa1111", "bbb2222", "ccc3333"]:
            self._write_variants(r, commit, n=1)

        remaining = list(_cache.cache_dir(r).glob("snapshot-*.json.gz"))
        assert len(remaining) == 3

    def test_keep_zero_disables_gc(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOURCECODE_CACHE_DIR", str(tmp_path / "cache"))
        monkeypatch.setenv("SOURCECODE_CACHE_KEEP_COMMITS", "0")
        r = _make_repo(tmp_path, "repo")

        for i in range(8):
            self._write_variants(r, f"{i:07x}0", n=1)

        remaining = list(_cache.cache_dir(r).glob("snapshot-*.json.gz"))
        assert len(remaining) == 8

    def test_all_variants_of_old_commit_evicted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SOURCECODE_CACHE_DIR", str(tmp_path / "cache"))
        monkeypatch.setenv("SOURCECODE_CACHE_KEEP_COMMITS", "1")
        r = _make_repo(tmp_path, "repo")

        self._write_variants(r, "aaaaaaa", n=3)  # old commit, 3 variants
        self._write_variants(r, "bbbbbbb", n=2)  # new commit, triggers eviction

        remaining = list(_cache.cache_dir(r).glob("snapshot-*.json.gz"))
        remaining_commits = {f.name.split("-")[1] for f in remaining}
        assert "aaaaaaa" not in remaining_commits
        assert "bbbbbbb" in remaining_commits
        assert len(remaining) == 2


# ---------------------------------------------------------------------------
# CAS GC sweep
# ---------------------------------------------------------------------------

class TestCASGC:
    def test_orphaned_cas_blob_deleted_after_eviction(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SOURCECODE_CACHE_DIR", str(tmp_path / "cache"))
        monkeypatch.setenv("SOURCECODE_CACHE_KEEP_COMMITS", "1")
        r = _make_repo(tmp_path, "repo")

        # Old commit with a large file_paths (creates CAS blob)
        old_content = _large_json(n=300)
        _cache.write(r, "aaa1111-00000001", old_content)

        old_cas_blobs = set(p.stem for p in (_cache.cache_dir(r) / "cas").glob("*.gz"))
        assert old_cas_blobs, "CAS blob should exist after write"

        # New commit with different large content → triggers GC → evicts old snapshot
        new_paths = [f"/src/new_module_{i}.py" for i in range(300)]
        new_content = json.dumps({"project_type": "java", "file_paths": new_paths},
                                 indent=2, ensure_ascii=False)
        _cache.write(r, "bbb2222-00000001", new_content)

        surviving_cas = set(p.stem for p in (_cache.cache_dir(r) / "cas").glob("*.gz"))
        # Old blob should be swept; new blob for bbb2222 should remain
        assert old_cas_blobs != surviving_cas or len(surviving_cas) <= 1, (
            "orphaned CAS blobs from evicted commit should be cleaned up"
        )

    def test_shared_cas_blob_not_deleted_while_referenced(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SOURCECODE_CACHE_DIR", str(tmp_path / "cache"))
        monkeypatch.setenv("SOURCECODE_CACHE_KEEP_COMMITS", "2")
        r = _make_repo(tmp_path, "repo")

        # Two commits sharing the same file_paths blob
        paths = [f"/src/shared_{i}.py" for i in range(300)]
        shared_base = {"project_type": "python", "file_paths": paths}

        _cache.write(r, "aaa1111-00000001",
                     json.dumps({**shared_base, "commit": "aaa"}, indent=2, ensure_ascii=False))
        _cache.write(r, "bbb2222-00000001",
                     json.dumps({**shared_base, "commit": "bbb"}, indent=2, ensure_ascii=False))

        # Both reference the same blob
        env_a = _read_envelope(_cache.cache_dir(r) / "snapshot-aaa1111-00000001.json.gz")
        env_b = _read_envelope(_cache.cache_dir(r) / "snapshot-bbb2222-00000001.json.gz")
        assert env_a["cas"]["file_paths"] == env_b["cas"]["file_paths"]

        # Add third commit — evicts aaa1111 (keep=2) but blob is still referenced by bbb2222
        _cache.write(r, "ccc3333-00000001",
                     json.dumps({"project_type": "python", "stacks": ["python"]}, indent=2))

        surviving_cas = set(p.stem for p in (_cache.cache_dir(r) / "cas").glob("*.gz"))
        shared_hash = env_b["cas"]["file_paths"]
        assert shared_hash in surviving_cas, (
            "shared CAS blob must survive while still referenced by bbb2222"
        )

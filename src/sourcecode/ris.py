"""Repository Intelligence Snapshot (RIS) — persistent repo-level semantic state.

RIS aggregates outputs from per-commit L1/L2 snapshots into a single artifact
that survives across git commits.  It enables cold-start bootstrapping for MCP
sessions: on first tool call, agents receive structural context immediately
without triggering re-analysis.

Storage layout:
    ~/.sourcecode/cache/<repo_id>/ris.json.gz

Build lifecycle:
    1. After every successful analysis run, ``maybe_update_ris`` is called with
       the L1 core_dict (already in scope in cli.py after ``write_core``).
    2. If the git HEAD matches the stored RIS → only compact/agent/git sections
       are refreshed (the API surface from ``endpoints`` is preserved).
    3. If the git HEAD changed → all sections are rebuilt from the new core_dict
       (api_surface preserved if the caller does not provide updated endpoints).
    4. ``get_cold_start_context`` reads the RIS and returns a lightweight
       bootstrap object.  It is safe to call from MCP on every session init.

RIS is NOT a command cache.  It stores a semantic model of the repository
derived from existing snapshot outputs, not raw command results.
"""
from __future__ import annotations

import gzip
import json
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

RIS_SCHEMA_VERSION: str = "1.0"
_RIS_FILENAME: str = "ris.json.gz"


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

@dataclass
class RepositoryIntelligenceSnapshot:
    repo_id: str
    created_at: str
    last_updated_at: str
    git_head: str
    version: str
    structural_map: dict
    api_surface: dict
    dependency_graph: dict
    compact_summary: dict
    agent_index: dict
    git_context_snapshot: dict
    metadata: dict


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------

def _ris_path(repo_root: Path) -> Path:
    from sourcecode.cache import cache_dir as _cache_dir
    return _cache_dir(repo_root) / _RIS_FILENAME


def load_ris(repo_root: Path) -> Optional[RepositoryIntelligenceSnapshot]:
    """Load RIS from disk.  Returns None on any error (missing, corrupt, stale schema)."""
    try:
        p = _ris_path(repo_root)
        if not p.exists():
            return None
        raw = gzip.decompress(p.read_bytes())
        d = json.loads(raw)
        if not isinstance(d, dict) or d.get("version") != RIS_SCHEMA_VERSION:
            return None
        return RepositoryIntelligenceSnapshot(
            repo_id=d.get("repo_id", ""),
            created_at=d.get("created_at", ""),
            last_updated_at=d.get("last_updated_at", ""),
            git_head=d.get("git_head", ""),
            version=d.get("version", ""),
            structural_map=d.get("structural_map", {}),
            api_surface=d.get("api_surface", {}),
            dependency_graph=d.get("dependency_graph", {}),
            compact_summary=d.get("compact_summary", {}),
            agent_index=d.get("agent_index", {}),
            git_context_snapshot=d.get("git_context_snapshot", {}),
            metadata=d.get("metadata", {}),
        )
    except Exception:
        return None


def save_ris(repo_root: Path, ris: RepositoryIntelligenceSnapshot) -> None:
    """Write RIS to disk as gzip-compressed JSON.  Never raises."""
    try:
        p = _ris_path(repo_root)
        p.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(asdict(ris), ensure_ascii=False, separators=(",", ":"))
        p.write_bytes(gzip.compress(raw.encode("utf-8"), compresslevel=6))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Data extraction from L1 core_dict
# ---------------------------------------------------------------------------

def _extract_structural_map(agent_data: dict) -> dict:
    """Extract structural map from agent_view output."""
    entry_points = agent_data.get("entry_points", [])

    layers = []
    domains = []
    arch = agent_data.get("architecture")
    if isinstance(arch, dict):
        layers = arch.get("layers") or []
        domains = arch.get("domains") or []

    controllers: list = []
    services: list = []
    for layer in layers:
        if not isinstance(layer, dict):
            continue
        name = (layer.get("name") or "").lower()
        files = layer.get("files") or []
        if any(kw in name for kw in ("controller", "rest", "api", "endpoint")):
            controllers.extend(files)
        elif any(kw in name for kw in ("service", "business", "domain", "use_case")):
            services.extend(files)

    return {
        "entrypoints": entry_points,
        "controllers": controllers,
        "services": services,
        "modules": domains,
    }


def _extract_api_surface(agent_data: dict) -> dict:
    """Extract API surface from agent_view output (Java security surface if present)."""
    endpoints: list = []
    controllers_index: list = []

    signals = agent_data.get("signals") or {}
    sec = signals.get("security_surface") or {}
    if isinstance(sec, dict):
        raw_endpoints = sec.get("endpoints") or []
        if isinstance(raw_endpoints, list):
            endpoints = raw_endpoints
        controllers_index = sec.get("controllers") or []

    return {
        "endpoints": endpoints,
        "controllers_index": controllers_index,
    }


def _extract_dependency_graph(compact_data: dict, agent_data: dict) -> dict:
    """Extract dependency graph summary from view outputs."""
    nodes = agent_data.get("key_dependencies") or compact_data.get("key_dependencies") or []
    summary = compact_data.get("dependency_summary") or {}

    return {
        "nodes": nodes,
        "edges": [],
        "summary": summary,
    }


def _extract_git_context_snapshot(agent_data: dict) -> dict:
    """Extract git context from agent_view output."""
    gc = agent_data.get("git_context") or {}
    if not isinstance(gc, dict):
        return {"last_commits": [], "hotspots": []}
    return {
        "last_commits": gc.get("recent_commits") or [],
        "hotspots": gc.get("top_hotspots") or [],
    }


# ---------------------------------------------------------------------------
# Build / update
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_from_core(
    repo_root: Path,
    core_dict: dict,
    git_head: str,
    existing_api_surface: Optional[dict] = None,
) -> RepositoryIntelligenceSnapshot:
    from sourcecode.cache import repo_id as _repo_id_fn

    compact_data = core_dict.get("_compact") or {}
    agent_data = core_dict.get("_agent") or {}

    # Strip file_relevance — large and not needed for cold-start
    agent_index = {k: v for k, v in agent_data.items() if k != "file_relevance"}

    now = _now_iso()
    return RepositoryIntelligenceSnapshot(
        repo_id=_repo_id_fn(repo_root),
        created_at=now,
        last_updated_at=now,
        git_head=git_head,
        version=RIS_SCHEMA_VERSION,
        structural_map=_extract_structural_map(agent_data),
        api_surface=existing_api_surface or _extract_api_surface(agent_data),
        dependency_graph=_extract_dependency_graph(compact_data, agent_data),
        compact_summary=compact_data,
        agent_index=agent_index,
        git_context_snapshot=_extract_git_context_snapshot(agent_data),
        metadata={
            "snapshot_source": "existing_snapshot_system",
            "confidence": float(
                1.0 if agent_data else (0.5 if compact_data else 0.0)
            ),
            "partial": not bool(agent_data),
        },
    )


def maybe_update_ris(repo_root: Path, core_dict: dict, git_head: str) -> None:
    """Update (or create) the RIS artifact from an L1 core_dict.

    Called from cli.py after every successful write_core.  Never raises.
    """
    try:
        if not isinstance(core_dict, dict):
            return

        existing = load_ris(repo_root)

        if existing is not None and existing.git_head == git_head and git_head:
            # Same commit — refresh analysis sections, preserve api_surface
            compact_data = core_dict.get("_compact") or {}
            agent_data = core_dict.get("_agent") or {}
            agent_index = {k: v for k, v in agent_data.items() if k != "file_relevance"}

            updated = RepositoryIntelligenceSnapshot(
                repo_id=existing.repo_id,
                created_at=existing.created_at,
                last_updated_at=_now_iso(),
                git_head=git_head,
                version=RIS_SCHEMA_VERSION,
                structural_map=_extract_structural_map(agent_data) if agent_data else existing.structural_map,
                api_surface=existing.api_surface,  # preserve explicit endpoint data
                dependency_graph=_extract_dependency_graph(compact_data, agent_data) if compact_data else existing.dependency_graph,
                compact_summary=compact_data if compact_data else existing.compact_summary,
                agent_index=agent_index if agent_index else existing.agent_index,
                git_context_snapshot=_extract_git_context_snapshot(agent_data) if agent_data else existing.git_context_snapshot,
                metadata={
                    "snapshot_source": "existing_snapshot_system",
                    "confidence": float(1.0 if agent_data else (0.5 if compact_data else 0.0)),
                    "partial": not bool(agent_data),
                },
            )
            save_ris(repo_root, updated)
        else:
            # New commit or first build — rebuild all sections (preserve api_surface if available)
            existing_api = existing.api_surface if existing is not None else None
            ris = _build_from_core(repo_root, core_dict, git_head, existing_api_surface=existing_api)
            if existing is not None:
                # Preserve creation timestamp
                ris = RepositoryIntelligenceSnapshot(
                    **{**asdict(ris), "created_at": existing.created_at}
                )
            save_ris(repo_root, ris)
    except Exception:
        pass


def update_ris_api_surface(repo_root: Path, endpoints_data: dict) -> None:
    """Update the api_surface section from an ``endpoints`` command output.

    Called from endpoints_cmd after _extract_java_endpoints().  Never raises.
    """
    try:
        if not isinstance(endpoints_data, dict):
            return
        endpoints = endpoints_data.get("endpoints") or []
        existing = load_ris(repo_root)
        if existing is None:
            # No RIS yet — create a minimal stub so api_surface is persisted
            from sourcecode.cache import repo_id as _repo_id_fn
            now = _now_iso()
            existing = RepositoryIntelligenceSnapshot(
                repo_id=_repo_id_fn(repo_root),
                created_at=now,
                last_updated_at=now,
                git_head="",
                version=RIS_SCHEMA_VERSION,
                structural_map={},
                api_surface={},
                dependency_graph={},
                compact_summary={},
                agent_index={},
                git_context_snapshot={},
                metadata={"snapshot_source": "existing_snapshot_system", "confidence": 0.0, "partial": True},
            )

        # Build controllers_index from endpoint controller fields
        ctrl_set: set[str] = set()
        for ep in endpoints:
            if isinstance(ep, dict):
                ctrl = ep.get("controller") or ep.get("controller_class") or ""
                if ctrl:
                    ctrl_set.add(ctrl)

        updated_api = {
            "endpoints": endpoints,
            "controllers_index": sorted(ctrl_set),
        }
        updated = RepositoryIntelligenceSnapshot(
            **{**asdict(existing), "api_surface": updated_api, "last_updated_at": _now_iso()}
        )
        save_ris(repo_root, updated)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Cold-start context
# ---------------------------------------------------------------------------

def _current_git_head(repo_root: Path) -> str:
    """Return current HEAD short SHA.  Returns '' on any error or non-git directory.

    Uses --short to match the format stored in the RIS and used by cli.py
    cache key computation — both sides must use the same format or staleness
    checks will always return True.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def _has_uncommitted_changes(repo_root: Path) -> bool:
    """Return True if working tree has staged or unstaged changes to tracked files.

    Uses ``git status --porcelain --untracked-files=no`` so that untracked
    files (e.g. legacy .sourcecode-cache/ directories) do not produce false
    positives.  Returns False on any error (non-git dirs, etc.).
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "status", "--porcelain", "--untracked-files=no"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0:
            return bool(result.stdout.strip())
    except Exception:
        pass
    return False


def get_cold_start_context(repo_root: Path) -> dict:
    """Return a lightweight bootstrap object from the persisted RIS.

    Never raises.  Returns ``{"status": "no_ris"}`` when no RIS exists.
    """
    try:
        ris = load_ris(repo_root)
        if ris is None:
            return {"status": "no_ris"}

        current_head = _current_git_head(repo_root)
        stale = bool(current_head and ris.git_head and current_head != ris.git_head)
        uncommitted = _has_uncommitted_changes(repo_root)

        endpoints = ris.api_surface.get("endpoints", [])
        _is_java = (
            (repo_root / "pom.xml").exists()
            or (repo_root / "build.gradle").exists()
            or (repo_root / "build.gradle.kts").exists()
        )
        # api_surface_complete: False when this is a Java repo but endpoints are absent.
        # An empty list does NOT mean "no endpoints exist" — it means the endpoint
        # index has not been built yet.  Agents must call get_endpoints to populate.
        _api_complete = not _is_java or bool(endpoints)

        # Build structural validation for Java/Spring repos.
        # Detects when the RIS snapshot is structurally incomplete (controllers found
        # but endpoint index was never built), so agents can decide whether to rebuild.
        _controllers_in_map = ris.structural_map.get("controllers", [])
        _controllers_in_api = ris.api_surface.get("controllers_index", [])
        _controllers_found = len(_controllers_in_map) or len(_controllers_in_api)
        _endpoints_found = len(endpoints)
        # Spring is detected when controllers exist in structural map or api surface.
        _spring_detected = bool(_controllers_found) or bool(_controllers_in_api)
        _validation_status = (
            "incomplete_snapshot"
            if _is_java and _spring_detected and _endpoints_found == 0
            else "valid"
        )
        _validation: dict = {
            "spring_detected": _spring_detected,
            "controllers_found": _controllers_found,
            "endpoints_found": _endpoints_found,
            "status": _validation_status,
        }

        # When the snapshot is structurally incomplete, downgrade status so agents
        # don't assume cold_start_ready when critical sections are missing.
        _status_base = "cold_start_stale" if stale else "cold_start_ready"
        if _validation_status == "incomplete_snapshot" and not stale:
            _status_base = "cold_start_incomplete"

        result: dict = {
            "status": _status_base,
            "repo_id": ris.repo_id,
            "git_head": ris.git_head,
            "current_git_head": current_head,
            "stale": stale or (_validation_status == "incomplete_snapshot"),
            "has_uncommitted_changes": uncommitted,
            "last_updated_at": ris.last_updated_at,
            "cache_source": "RIS",
            "data_scope": "RIS_BOOTSTRAP",
            "api_surface_complete": _api_complete,
            "summary": ris.compact_summary,
            "entrypoints": ris.structural_map.get("entrypoints", []),
            "endpoints": endpoints,
            "hotspots": ris.git_context_snapshot.get("hotspots", []),
            "validation": _validation,
            # Fix 3: _cache wrapper for backward compat with CLI schema consumers.
            # CLI outputs inject _cache via _inject_cache_meta; MCP cold-start path
            # skips that step, leaving agents that read _cache.cache_source with None.
            "_cache": {
                "cache_source": "RIS",
                "git_head_at_generation": ris.git_head or "",
                "current_git_head": current_head or "",
                "is_stale": stale,
                "has_uncommitted_changes": uncommitted,
                "generated_at": ris.last_updated_at,
                "data_scope": "RIS_BOOTSTRAP",
            },
        }
        if not endpoints and _is_java:
            result["endpoints_hint"] = (
                "Java repo detected but no endpoint index found. "
                "Call get_endpoints (or: sourcecode endpoints <path>) to populate."
            )
        return result
    except Exception:
        return {"status": "no_ris"}

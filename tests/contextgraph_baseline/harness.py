"""ContextGraph Phase 0 — baseline harness / regression oracle.

Drives the sourcecode CLI as a black box (subprocess) across the field-test repos,
canonicalizes each command's JSON output, and records a light, git-committable
oracle (`index.json`: sha256 of canonical bytes + metrics + repo HEAD per cell).

Subcommands:
    capture      Build/refresh the baseline oracle (run before a phase begins).
    compare      Re-run the matrix and byte-diff canonical output vs the oracle.
    convergence  Static architecture-convergence metrics (IR consumers vs private parsers).

Guarantees for Phase 0:
    - No engine import → cannot change functional behavior.
    - Deterministic: canonicalization sorts keys and strips wall-clock volatiles only.
    - Portable oracle: absolute repo paths normalized to <REPO>.

Usage:
    python -m tests.contextgraph_baseline.harness capture
    python -m tests.contextgraph_baseline.harness compare
    python -m tests.contextgraph_baseline.harness convergence
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]           # atlas-cli/
RUN_CLI = REPO_ROOT / "run_cli.py"
PY = sys.executable                                        # the interpreter running us
TESTING_ROOT = Path("/Users/user/Documents/workspace/testing")

BASELINE_DIR = Path(__file__).resolve().parent / "baseline"
INDEX_PATH = BASELINE_DIR / "index.json"                  # committed oracle (light)
ARTIFACTS_DIR = BASELINE_DIR / "artifacts"                # gitignored full outputs

# ---------------------------------------------------------------------------
# Field-test repos (external — referenced by absolute path, HEAD recorded per run)
# ---------------------------------------------------------------------------

REPOS: tuple[str, ...] = (
    "spaghetti-api",
    "spring-petclinic",
    "examples",
    "eureka",
    "jobrunr",
    "ofbiz-framework",
    "openmrs-core",
    "jenkins",
    "BroadleafCommerce",
    "alfresco-community-repo",
    "keycloak",
)

# ---------------------------------------------------------------------------
# Command matrix — every cell is a deterministic, path-only, JSON-emitting engine
# command. `--no-cache` is a global flag and MUST precede the subcommand.
# Each entry: (cell_id, [args after the global flag; {REPO} is substituted]).
# ---------------------------------------------------------------------------

MATRIX: tuple[tuple[str, tuple[str, ...]], ...] = (
    # --summary-only: the full graph reaches 110 MB on monoliths (alfresco) and is
    # what repo-ir's OUTPUT_TOO_LARGE guard steers users away from. --summary-only
    # keeps analysis+impact+subsystems+change_set — all derived from the complete
    # graph, so any structural change ripples into these aggregates — at ~80 KB,
    # bounded regardless of repo size. Per-node/edge drift is covered by the 8
    # projection commands below (endpoints, spring-audit, exports, …).
    ("repo-ir",             ("repo-ir", "{REPO}", "--summary-only")),
    ("endpoints",           ("endpoints", "{REPO}")),
    ("spring-audit",        ("spring-audit", "{REPO}")),
    ("migrate-check",       ("migrate-check", "{REPO}")),
    ("validation",          ("validation", "{REPO}", "--format", "json")),
    ("export-c4",           ("export", "{REPO}", "--c4")),
    ("export-module-graph", ("export", "{REPO}", "--module-graph")),
    ("export-integrations", ("export", "{REPO}", "--integrations")),
    ("export-by-directory", ("export", "{REPO}", "--by-directory")),
)

# ---------------------------------------------------------------------------
# Volatile-field normalization — the ONLY values stripped before hashing.
# Wall-clock timestamps must not cause false drift; nothing structural is touched.
# ---------------------------------------------------------------------------

VOLATILE_KEYS: frozenset[str] = frozenset({"generated_at", "analysis_time_ms"})
_ISO_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[.\d]*(?:[+-]\d{2}:\d{2}|Z)?")


def _scrub_volatiles(obj: Any, repo_abs: str) -> Any:
    """Recursively null out wall-clock volatiles and normalize the repo path.

    Structural data is untouched; only known non-deterministic values are masked
    so the canonical hash reflects analysis output, not the moment it ran.
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in VOLATILE_KEYS:
                out[k] = "<VOLATILE>"
            else:
                out[k] = _scrub_volatiles(v, repo_abs)
        return out
    if isinstance(obj, list):
        return [_scrub_volatiles(v, repo_abs) for v in obj]
    if isinstance(obj, str):
        s = obj.replace(repo_abs, "<REPO>")
        if _ISO_TS_RE.fullmatch(s):
            return "<VOLATILE>"
        return s
    return obj


def _canonicalize(raw: str, repo_abs: str) -> tuple[Optional[str], Optional[dict]]:
    """Return (canonical_json_text, structural_stats) or (None, None) if not JSON.

    Canonical form: sorted keys, 2-space indent — the byte-diff oracle basis.
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None, None
    scrubbed = _scrub_volatiles(data, repo_abs)
    canonical = json.dumps(scrubbed, sort_keys=True, indent=2, ensure_ascii=False)
    return canonical, _structural_stats(scrubbed)


def _structural_stats(data: Any) -> dict:
    """Best-effort node/edge/evidence counts for metrics (never affects the hash)."""
    stats: dict[str, int] = {}
    if isinstance(data, dict):
        graph = data.get("graph")
        if isinstance(graph, dict):
            if isinstance(graph.get("nodes"), list):
                stats["nodes"] = len(graph["nodes"])
            if isinstance(graph.get("edges"), list):
                stats["edges"] = len(graph["edges"])
        for key in ("endpoints", "findings", "evidence", "modules"):
            v = data.get(key)
            if isinstance(v, list):
                stats[key] = len(v)
    return stats


# ---------------------------------------------------------------------------
# Repo helpers
# ---------------------------------------------------------------------------

def _repo_path(name: str) -> Path:
    return TESTING_ROOT / name


def _repo_head(name: str) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(_repo_path(name)), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=15,
        )
        return out.stdout.strip() if out.returncode == 0 else "<no-git>"
    except Exception:
        return "<no-git>"


def _present_repos() -> list[str]:
    return [r for r in REPOS if _repo_path(r).is_dir()]


# ---------------------------------------------------------------------------
# Cell execution
# ---------------------------------------------------------------------------

def _run_cell(repo: str, args: tuple[str, ...], timeout: float) -> dict:
    """Run one CLI command; return a record with canonical hash + metrics."""
    repo_abs = str(_repo_path(repo))
    cmd = [PY, str(RUN_CLI), "--no-cache", *[a.replace("{REPO}", repo_abs) for a in args]]
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        wall_ms = round((time.perf_counter() - t0) * 1000, 1)
        stdout, exit_code = proc.stdout, proc.returncode
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "wall_ms": round(timeout * 1000, 1)}

    canonical, stats = _canonicalize(stdout, repo_abs)
    if canonical is None:
        # Non-JSON output is itself a stable fact (e.g. an error contract). Hash raw
        # (path-normalized) so drift is still detectable.
        norm = stdout.replace(repo_abs, "<REPO>")
        return {
            "status": "nonjson",
            "exit_code": exit_code,
            "sha256": hashlib.sha256(norm.encode()).hexdigest(),
            "bytes": len(norm.encode()),
            "wall_ms": wall_ms,
        }
    return {
        "status": "ok",
        "exit_code": exit_code,
        "sha256": hashlib.sha256(canonical.encode()).hexdigest(),
        "bytes": len(canonical.encode()),
        "wall_ms": wall_ms,
        "stats": stats or {},
        "_canonical": canonical,  # popped before indexing; written to artifacts
    }


# ---------------------------------------------------------------------------
# capture / compare
# ---------------------------------------------------------------------------

def capture(timeout: float = 600.0) -> int:
    repos = _present_repos()
    if not repos:
        print(f"ERROR: no field-test repos under {TESTING_ROOT}", file=sys.stderr)
        return 2
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    index: dict[str, Any] = {"repos": {}, "cells": {}}
    for repo in repos:
        index["repos"][repo] = {"head": _repo_head(repo)}
    for repo in repos:
        for cell_id, args in MATRIX:
            key = f"{repo}::{cell_id}"
            rec = _run_cell(repo, args, timeout)
            canonical = rec.pop("_canonical", None)
            if canonical is not None:
                art = ARTIFACTS_DIR / repo / f"{cell_id}.json"
                art.parent.mkdir(parents=True, exist_ok=True)
                art.write_text(canonical)
            index["cells"][key] = rec
            print(f"  [{rec['status']:>7}] {key}  {rec.get('wall_ms', '?')}ms "
                  f"sha={rec.get('sha256', '-')[:12]}")
    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(json.dumps(index, indent=2, sort_keys=True))
    print(f"\nBaseline oracle written: {INDEX_PATH}  ({len(index['cells'])} cells)")
    return 0


def compare(timeout: float = 600.0, write_diffs: bool = True) -> int:
    if not INDEX_PATH.exists():
        print(f"ERROR: no baseline oracle at {INDEX_PATH}; run `capture` first.", file=sys.stderr)
        return 2
    index = json.loads(INDEX_PATH.read_text())
    base_cells: dict[str, Any] = index.get("cells", {})
    drift: list[str] = []
    checked = 0
    for repo in _present_repos():
        for cell_id, args in MATRIX:
            key = f"{repo}::{cell_id}"
            if key not in base_cells:
                continue
            checked += 1
            new = _run_cell(repo, args, timeout)
            canonical = new.pop("_canonical", None)
            old = base_cells[key]
            if new.get("sha256") != old.get("sha256") or new.get("status") != old.get("status"):
                drift.append(key)
                print(f"  DRIFT  {key}: status {old.get('status')}→{new.get('status')} "
                      f"sha {str(old.get('sha256'))[:12]}→{str(new.get('sha256'))[:12]} "
                      f"bytes {old.get('bytes')}→{new.get('bytes')}")
                if write_diffs and canonical is not None:
                    new_art = ARTIFACTS_DIR / repo / f"{cell_id}.NEW.json"
                    new_art.parent.mkdir(parents=True, exist_ok=True)
                    new_art.write_text(canonical)
            else:
                print(f"  ok     {key}")
    print(f"\nCompared {checked} cells — {len(drift)} drift.")
    if drift:
        print("Drifted cells (explain each before advancing a phase):")
        for k in drift:
            print(f"  - {k}")
        return 1
    return 0


# ---------------------------------------------------------------------------
# convergence — architecture dedup metrics (static, no engine import)
# ---------------------------------------------------------------------------

def convergence() -> int:
    src = REPO_ROOT / "src" / "sourcecode"
    consumer_re = re.compile(r"\b(repository_ir|canonical_ir|cir_graphs)\b")
    # Signals that a module runs its own Java parse rather than consuming the IR.
    parser_re = re.compile(r"\.java\b|_CLASS_DECL_RE|re\.compile\([^)]*(class|interface|@[A-Z])")
    entry_re = re.compile(r"\b(build_repo_ir|build_canonical_ir)\s*\(")

    consumers, direct_parsers, both = [], [], []
    entry_points = 0
    modules = 0
    for f in sorted(src.glob("*.py")):
        if f.name == "__init__.py":
            continue
        modules += 1
        text = f.read_text(errors="ignore")
        is_consumer = bool(consumer_re.search(text)) and f.name not in {
            "repository_ir.py", "canonical_ir.py", "cir_graphs.py"}
        is_parser = bool(parser_re.search(text))
        entry_points += len(entry_re.findall(text))
        if is_consumer:
            consumers.append(f.name)
        if is_parser:
            direct_parsers.append(f.name)
        if is_consumer and is_parser:
            both.append(f.name)

    report = {
        "modules_scanned": modules,
        "ir_consumers": len(consumers),
        "direct_java_parsers": len(direct_parsers),
        "consume_and_parse": len(both),
        "parse_entry_points": entry_points,
        "pct_engine_on_contextgraph": round(100 * len(consumers) / max(modules, 1), 1),
        "_consumers": consumers,
        "_direct_parsers": direct_parsers,
    }
    print(json.dumps(report, indent=2))
    conv_path = BASELINE_DIR / "convergence.json"
    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    conv_path.write_text(json.dumps(report, indent=2, sort_keys=True))
    print(f"\nConvergence metrics written: {conv_path}")
    return 0


# ---------------------------------------------------------------------------
# reindex — rebuild the oracle from existing artifacts (no CLI re-run)
# ---------------------------------------------------------------------------

def reindex() -> int:
    """Rebuild index.json from the canonical artifacts already on disk.

    Robust recovery path: `capture` writes per-cell artifacts as it goes but the
    index last. If a long run is interrupted, the artifacts are still valid canonical
    outputs — this recomputes each cell's sha256/bytes from them (the only fields the
    drift oracle needs). wall_ms is dropped (a metric, not correctness) and marked
    absent so it is clear the index was reindexed, not freshly measured.
    """
    if not ARTIFACTS_DIR.is_dir():
        print(f"ERROR: no artifacts at {ARTIFACTS_DIR}; run `capture` first.", file=sys.stderr)
        return 2
    index: dict[str, Any] = {"repos": {}, "cells": {}, "_reindexed": True}
    cell_ids = {cid for cid, _ in MATRIX}
    for repo_dir in sorted(ARTIFACTS_DIR.iterdir()):
        if not repo_dir.is_dir():
            continue
        repo = repo_dir.name
        index["repos"][repo] = {"head": _repo_head(repo)}
        for art in sorted(repo_dir.glob("*.json")):
            if art.name.endswith(".NEW.json"):
                continue
            cell_id = art.stem
            if cell_id not in cell_ids:
                continue
            text = art.read_text()
            index["cells"][f"{repo}::{cell_id}"] = {
                "status": "ok",
                "exit_code": 0,
                "sha256": hashlib.sha256(text.encode()).hexdigest(),
                "bytes": len(text.encode()),
                "wall_ms": None,
            }
    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(json.dumps(index, indent=2, sort_keys=True))
    print(f"Reindexed {len(index['cells'])} cells from artifacts → {INDEX_PATH}")
    return 0


# ---------------------------------------------------------------------------
# entrypoint
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print(__doc__)
        return 2
    cmd = argv[0]
    if cmd == "capture":
        return capture()
    if cmd == "compare":
        return compare()
    if cmd == "reindex":
        return reindex()
    if cmd == "convergence":
        return convergence()
    print(f"unknown subcommand: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

"""ContextGraph Phase 1 — structural metrics collector.

Builds the ContextGraph façade over each present field-test repo and records the
structural size, composition, build time, and peak memory of the graph. This is
the Phase 1 "measure, do not optimize" deliverable: it produces the numbers that
later phases (incremental invalidation, persistence) must improve, and confirms
the façade builds successfully across the full repo range.

In-process only. No caching, no persistence — one cold build per repo.

Usage:
    python -m tests.contextgraph_baseline.phase1_metrics          # all present repos
    python -m tests.contextgraph_baseline.phase1_metrics petclinic  # substring filter
"""
from __future__ import annotations

import json
import sys
import time
import tracemalloc
from pathlib import Path

# Make the engine importable (src/ layout) without installing.
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from sourcecode.context_graph import ContextGraph  # noqa: E402
from sourcecode.repository_ir import find_java_files  # noqa: E402

from tests.contextgraph_baseline.harness import (  # noqa: E402
    REPOS,
    _present_repos,
    _repo_head,
    _repo_path,
)

METRICS_PATH = Path(__file__).resolve().parent / "baseline" / "phase1_metrics.json"


def _flush(results: list[dict], totals: dict | None = None) -> None:
    """Write the metrics file atomically after each repo, so an interrupted run
    (Phase 0 showed background jobs can be killed ~480s) never loses progress."""
    payload = {
        "repos_expected": list(REPOS),
        "results": results,
        "totals": totals if totals is not None else {"in_progress": True},
    }
    METRICS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = METRICS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(METRICS_PATH)


def _measure_repo(name: str) -> dict:
    root = _repo_path(name)
    t0 = time.perf_counter()
    files = find_java_files(root)
    discover_ms = (time.perf_counter() - t0) * 1000.0

    tracemalloc.start()
    t1 = time.perf_counter()
    cg = ContextGraph.build(files, root)
    build_ms = (time.perf_counter() - t1) * 1000.0
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    m = cg.metrics()
    m.update(
        {
            "repo": name,
            "head": _repo_head(name),
            "java_files": len(files),
            "discover_ms": round(discover_ms, 2),
            "build_ms": round(build_ms, 2),  # authoritative build timing
            "peak_mem_mb": round(peak / (1024 * 1024), 2),
        }
    )
    return m


def main(argv: list[str]) -> int:
    substr = argv[1] if len(argv) > 1 else None
    repos = _present_repos()
    if substr:
        repos = [r for r in repos if substr.lower() in r.lower()]
    if not repos:
        print("no present field-test repos matched", file=sys.stderr)
        return 1

    results: list[dict] = []
    header = (f"{'repo':<26} {'files':>6} {'nodes':>7} {'rels':>7} "
              f"{'eps':>5} {'grnd':>6} {'build_ms':>9} {'mem_mb':>7}")
    print(header, flush=True)
    print("-" * 84, flush=True)
    for name in repos:
        try:
            m = _measure_repo(name)
        except Exception as exc:  # noqa: BLE001 - record and continue
            print(f"{name:<26} ERROR {type(exc).__name__}: {exc}",
                  file=sys.stderr, flush=True)
            results.append({"repo": name, "error": f"{type(exc).__name__}: {exc}"})
            _flush(results)  # durable after every repo, even on error
            continue
        results.append(m)
        print(f"{name:<26} {m['java_files']:>6} {m['node_count']:>7} "
              f"{m['relation_count']:>7} {m['endpoint_count']:>5} "
              f"{m['grounded_node_count']:>6} {m['build_ms']:>9.1f} "
              f"{m['peak_mem_mb']:>7.1f}", flush=True)
        # Persist incrementally: a kill mid-run keeps everything measured so far.
        _flush(results)

    totals = {
        "repos_measured": len([r for r in results if "error" not in r]),
        "total_nodes": sum(r.get("node_count", 0) for r in results),
        "total_relations": sum(r.get("relation_count", 0) for r in results),
        "total_endpoints": sum(r.get("endpoint_count", 0) for r in results),
        "total_build_ms": round(sum(r.get("build_ms", 0.0) for r in results), 2),
        "max_peak_mem_mb": max((r.get("peak_mem_mb", 0.0) for r in results), default=0.0),
    }

    _flush(results, totals)
    print("-" * 84, flush=True)
    print(f"totals: {json.dumps(totals)}", flush=True)
    print(f"written: {METRICS_PATH}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

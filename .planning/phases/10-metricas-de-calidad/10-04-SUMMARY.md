---
phase: 10-metricas-de-calidad
plan: "04"
subsystem: metrics-cli
tags: [metrics, cli, e2e-tests, quality-gate]
dependency_graph:
  requires: [10-01-PLAN.md, 10-02-PLAN.md, 10-03-PLAN.md]
  provides: [full-metrics-flag, E2E-MQT-11-14]
  affects: [src/sourcecode/cli.py, src/sourcecode/metrics_analyzer.py, tests/test_integration_metrics.py]
tech_stack:
  added: []
  patterns: [conditional-instantiation, workspace-path-prefixing, lazy-import, merge-summaries]
key_files:
  created:
    - tests/test_integration_metrics.py
  modified:
    - src/sourcecode/cli.py
    - src/sourcecode/metrics_analyzer.py
decisions:
  - "--full-metrics options must come before the positional PATH argument in CliRunner invocations due to Click/Typer behavior (options after positional args are treated as subcommands)"
  - "Pre-existing re.Pattern type-arg mypy errors in metrics_analyzer.py fixed inline (re.Pattern -> re.Pattern[str]) as they were in a file already modified"
  - "Pre-existing Windows path separator failures in test_integration_dependencies, test_integration_graph_modules, test_integration_multistack, test_workspace_analyzer, test_packaging, test_real_projects are out of scope and pre-date this plan"
metrics:
  duration: ~20min
  completed: "2026-04-10"
  tasks_completed: 2
  files_modified: 3
---

# Phase 10 Plan 04: CLI Wiring + E2E Tests + Quality Gate Summary

Wire MetricsAnalyzer to the CLI via `--full-metrics` flag, activate the 4 E2E integration tests (MQT-11..14), and pass the ruff+mypy quality gate — closing Phase 10 with the full public contract.

## What Was Built

### Task 1 — 6 Surgical Modifications to cli.py

**Modification 1 — Flag parameter (lines ~110-114)**

Added `full_metrics: bool = typer.Option(False, "--full-metrics", ...)` after the `docs_depth` parameter in `main()` signature.

**Modification 2 — Lazy import + conditional instantiation (lines ~153, ~221)**

Inside the lazy import block: `from sourcecode.metrics_analyzer import MetricsAnalyzer`

After `doc_analyzer` instantiation: `metrics_analyzer = MetricsAnalyzer() if full_metrics else None`

**Modification 3 — Root metrics analysis (lines ~279-295)**

After the `doc_analyzer` root block, initialized `file_metrics_records: list = []` and `metrics_summaries = []`, then conditionally called `metrics_analyzer.analyze(target, root_metrics_tree)` using the same `prune_workspace_paths` pattern as doc and graph analyzers.

**Modification 4 — Workspace loop metrics (lines ~355-366)**

Inside the `for workspace in workspace_analysis.workspaces:` loop, after the `doc_analyzer` block: calls `metrics_analyzer.analyze(workspace_root, workspace_tree, workspace=workspace.path)` and prefixes paths using `replace(m, path=f"{workspace.path}/{m.path}")` — exact same pattern as `prefixed_doc_records`.

**Modification 5 — Merge summaries (lines ~395-399)**

After `doc_summary` merge: `metrics_summary = metrics_analyzer.merge_summaries(metrics_summaries) if metrics_analyzer is not None else None`

**Modification 6 — SourceMap constructor (lines ~403-417)**

Added `file_metrics=file_metrics_records` and `metrics_summary=metrics_summary` to the `SourceMap(...)` call.

### Task 2 — E2E Tests (MQT-11..14) in test_integration_metrics.py

Replaced the `@pytest.mark.skip` stub with 4 fully-implemented integration tests:

| Test | ID | What it verifies |
|------|----|-----------------|
| `test_full_metrics_flag_produces_file_metrics` | MQT-11 | file_metrics non-empty, metrics_summary non-null, Python LOC=measured, test_file_count>0 |
| `test_base_command_unchanged_without_flag` | MQT-12 | file_metrics=[], metrics_summary=null, stacks/file_paths/project_summary still present |
| `test_full_metrics_availability_labels` | MQT-13 | All *_availability fields in {measured, inferred, unavailable} |
| `test_full_metrics_with_test_files` | MQT-14 | is_test=True entries exist; their paths match test patterns |

All 4 tests invoke via `CliRunner` using `['--full-metrics', str(PROJECT_ROOT)]` (options before positional arg per Click semantics).

## Quality Gate Results

### ruff check

```
src/sourcecode/metrics_analyzer.py  All checks passed
src/sourcecode/coverage_parser.py   All checks passed
src/sourcecode/cli.py               All checks passed (1 import sort fixed via --fix)
tests/test_integration_metrics.py   All checks passed
src/sourcecode/ (full)              All checks passed
```

Auto-fixed: import block sort in cli.py (I001).
Manual-fixed: SIM102 nested `if` in metrics_analyzer.py `_count_loc()` — combined into single `elif "/*" in stripped and "*/" not in stripped[...]:`.

### mypy --ignore-missing-imports

```
src/sourcecode/metrics_analyzer.py  Success: no issues found
src/sourcecode/coverage_parser.py   Success: no issues found
```

Fixed: `re.Pattern` -> `re.Pattern[str]` at two module-level annotations (lines 29 and 52). These were pre-existing type-arg errors from plan 10-01.

### pytest test suite

```
Before this plan:  7 failed, 224 passed, 2 skipped
After this plan:   7 failed, 228 passed, 1 skipped
```

Net change: +4 passing tests (MQT-11..14), 0 regressions introduced. The 7 pre-existing failures are all Windows path separator issues (`apps\\web` vs `apps/web`) in workspace/monorepo tests that pre-date this plan.

### Integration test results

```
tests/test_integration_metrics.py::test_full_metrics_flag_produces_file_metrics  PASSED
tests/test_integration_metrics.py::test_base_command_unchanged_without_flag      PASSED
tests/test_integration_metrics.py::test_full_metrics_availability_labels         PASSED
tests/test_integration_metrics.py::test_full_metrics_with_test_files             PASSED
4 passed in 0.76s
```

Sample output metrics from `sourcecode . --full-metrics` on atlas-cli itself:
- 362 file_metrics records
- test_file_count > 0 (test suite files detected as is_test=True)
- Python files: loc_availability=measured, symbol_availability=measured
- All availability labels validated as {measured, inferred, unavailable}

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Import sort in cli.py broken by MetricsAnalyzer insertion**
- **Found during:** Task 1 ruff gate
- **Issue:** Adding `from sourcecode.metrics_analyzer import MetricsAnalyzer` between `DocAnalyzer` and `GraphAnalyzer` imports broke ruff I001 import sort rule
- **Fix:** `python -m ruff check src/sourcecode/cli.py --fix` — auto-reformatted import block
- **Files modified:** src/sourcecode/cli.py
- **Commit:** 661540c

**2. [Rule 1 - Bug] SIM102 nested if in metrics_analyzer.py _count_loc()**
- **Found during:** Task 2 ruff gate
- **Issue:** `elif "/*" in stripped: if "*/" not in ...: in_block = True` flagged as SIM102 (nested if should be combined)
- **Fix:** Combined into `elif "/*" in stripped and "*/" not in stripped[stripped.index("/*") + 2:]: in_block = True`
- **Files modified:** src/sourcecode/metrics_analyzer.py
- **Commit:** 661540c

**3. [Rule 2 - Missing] re.Pattern type arguments missing (mypy type-arg)**
- **Found during:** Task 2 mypy gate
- **Issue:** `list[re.Pattern]` and `list[tuple[re.Pattern, str]]` missing generic type args — pre-existing from 10-01 but surfaced in mypy run
- **Fix:** Changed to `re.Pattern[str]` at both annotations
- **Files modified:** src/sourcecode/metrics_analyzer.py
- **Commit:** 661540c

## Self-Check: PASSED

| Item | Result |
|------|--------|
| src/sourcecode/cli.py exists | FOUND |
| tests/test_integration_metrics.py exists | FOUND |
| 10-04-SUMMARY.md exists | FOUND |
| commit 661540c exists | FOUND |

---
phase: 10-metricas-de-calidad
plan: "01"
subsystem: metrics
tags: [schema, dataclasses, metrics, LOC, AST, complexity, TDD]
dependency_graph:
  requires: []
  provides:
    - FileMetrics dataclass (schema.py)
    - CoverageRecord dataclass (schema.py)
    - MetricsSummary dataclass (schema.py)
    - MetricAvailability type alias (schema.py)
    - MetricsAnalyzer class (metrics_analyzer.py)
  affects:
    - src/sourcecode/schema.py
    - src/sourcecode/metrics_analyzer.py
tech_stack:
  added:
    - ast.parse for Python symbol + McCabe complexity analysis
    - re (regex) for JS/TS/Go/Rust/Java inferred symbol counting
  patterns:
    - DocAnalyzer pattern: analyze() + merge_summaries() returning typed tuples
    - TDD Wave 0: tests written before implementation (RED -> GREEN)
    - Language tier system: measured/inferred/unavailable availability flags
key_files:
  created:
    - src/sourcecode/metrics_analyzer.py
    - tests/test_metrics_analyzer.py
    - tests/test_integration_metrics.py
  modified:
    - src/sourcecode/schema.py
decisions:
  - "_LANG_MAP imported from doc_analyzer (not redefined) — single source of truth"
  - "LOC for JS/TS uses block-comment state machine to avoid counting /* ... */ as code"
  - "McCabe CC computed as average across all functions in file (float) when functions > 0"
  - "Pre-existing Windows path separator failures in test_integration_* are out of scope"
metrics:
  duration_minutes: 35
  completed_date: "2026-04-10"
  tasks_completed: 3
  files_created: 3
  files_modified: 1
  tests_passing: 11
  tests_skipped: 3
  tests_failed: 0
requirements_satisfied:
  - METRICS-01
  - OUT-10
---

# Phase 10 Plan 01: Schema FileMetrics/CoverageRecord/MetricsSummary + MetricsAnalyzer LOC Counters Summary

Schema contracts and LOC/symbol analysis engine for Phase 10 code quality metrics, following the DocAnalyzer pattern with Python AST (measured), regex (inferred), and text-scan (LOC-only) tiers.

## What Was Built

### Dataclasses Added to `src/sourcecode/schema.py`

**`MetricAvailability`** — type alias:
```python
MetricAvailability = Literal["measured", "inferred", "unavailable"]
```

**`FileMetrics`** — 20 fields covering:
- `path`, `language`, `is_test`, `production_target`
- LOC fields: `total_lines`, `code_lines`, `blank_lines`, `comment_lines`, `loc_availability`
- Symbol fields: `function_count`, `class_count`, `symbol_availability`
- Complexity: `cyclomatic_complexity`, `complexity_availability`
- Coverage (for plan 10-02): `line_rate`, `branch_rate`, `coverage_source`, `coverage_availability`
- `workspace` for monorepo support

**`CoverageRecord`** — 9 fields for parsed coverage report data (XML/JSON format source).

**`MetricsSummary`** — 8 fields: `requested`, `file_count`, `test_file_count`, `languages`, `total_loc`, `coverage_records`, `coverage_sources_found`, `limitations`.

**`SourceMap` extensions** (backward-compatible, appended at end):
```python
file_metrics: list[FileMetrics] = field(default_factory=list)
metrics_summary: Optional[MetricsSummary] = None
```

### LOC/Symbol Tier Table

| Language | LOC | Symbols | Complexity |
|----------|-----|---------|------------|
| Python | measured (text scan) | measured (ast.parse) | measured (McCabe avg per fn) |
| JavaScript / TypeScript | measured (block-comment state machine) | inferred (regex) | unavailable |
| Go | measured (text scan) | inferred (regex: `^func\s`, `^type \w+ struct`) | unavailable |
| Rust | measured (text scan) | inferred (regex: `fn \w+`, `struct \w+`) | unavailable |
| Java | measured (text scan) | inferred (regex: method + class patterns) | unavailable |
| All others | measured (text scan) | unavailable | unavailable |

### `MetricsAnalyzer` in `src/sourcecode/metrics_analyzer.py`

Public API:
- `analyze(root, file_tree, *, workspace=None) -> tuple[list[FileMetrics], MetricsSummary]`
- `merge_summaries(summaries: Iterable[MetricsSummary]) -> MetricsSummary`

Private helpers:
- `_analyze_file(abs_path, rel_path, workspace) -> FileMetrics`
- `_count_loc(content, language) -> dict`
- `_count_python_symbols(content, rel_path) -> dict` (ast.parse + McCabe)
- `_count_js_symbols(content) -> dict`
- `_count_go_symbols(content) -> dict`
- `_count_rust_symbols(content) -> dict`
- `_count_java_symbols(content) -> dict`

Guards implemented:
- `_MAX_FILES = 500` — truncates and adds `limitations["max_files_reached:{N}"]`
- `_MAX_FILE_SIZE = 500_000` bytes — skips oversized files with `limitations["file_too_large:{path}"]`
- Read errors: `limitations["read_error:{path}"]`
- SyntaxError in Python: `loc_availability="measured"`, `symbol_availability="unavailable"`

### Tests

**`tests/test_metrics_analyzer.py`** — 11 passing, 2 skipped:
- MQT-01: `test_python_loc` — text scan counts 10 lines (3 blank, 2 comment, 5 code)
- MQT-02: `test_python_symbols` — ast.parse detects 2 functions, 1 class; CC >= 1.0
- MQT-03: `test_js_loc`, `test_js_analyze_file_availability` — JS LOC + inferred symbols
- MQT-04: `test_go_rust_loc`, `test_go/rust_analyze_file_availability` — Go/Rust inferred
- MQT-13: `test_graceful_degradation_*` — unknown ext, SyntaxError, nonexistent file, mixed
- Skips: `test_is_test_file` (plan 10-03), `test_infer_production` (plan 10-03)

**`tests/test_integration_metrics.py`** — 1 skipped:
- MQT-11: `test_full_metrics_flag` (plan 10-04)

## Deviations from Plan

**None — plan executed exactly as written.**

Note: Pre-existing failures in `test_integration_dependencies.py`, `test_integration_graph_modules.py`, and `test_integration_multistack.py` are Windows path separator (`\` vs `/`) issues present before this plan. They are out of scope and logged to deferred-items.

## Known Stubs

None that affect plan goals. The `is_test` field defaults to `False` for all `FileMetrics` — the `is_test_file()` detection and `infer_production_target()` are intentionally deferred to plan 10-03, as documented by the `@pytest.mark.skip` stubs in the test file.

## Threat Mitigations Applied

All four threats from the plan's threat model are implemented:
- **T-10-01-01** (DoS via ast.parse): `_MAX_FILE_SIZE = 500_000` guard before reading any file
- **T-10-01-02** (DoS via unlimited files): `_MAX_FILES = 500` cap in `analyze()` with limitations entry
- **T-10-01-03** (info disclosure via paths): accepted — paths are already in `file_tree`
- **T-10-01-04** (catastrophic regex): all patterns use simple anchors, no nested alternation or backreferences

## Self-Check: PASSED

Files verified:
- `src/sourcecode/metrics_analyzer.py`: FOUND
- `src/sourcecode/schema.py`: FOUND (FileMetrics, CoverageRecord, MetricsSummary)
- `tests/test_metrics_analyzer.py`: FOUND
- `tests/test_integration_metrics.py`: FOUND

Commits verified:
- `0e11f98`: test(10-01) Wave 0 test stubs
- `c5598d0`: feat(10-01) schema dataclasses
- `bd49978`: feat(10-01) MetricsAnalyzer implementation

Test results: 11 passed, 3 skipped, 0 failed (metrics + schema suites)

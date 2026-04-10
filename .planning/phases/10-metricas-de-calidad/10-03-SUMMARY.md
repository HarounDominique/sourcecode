---
phase: 10-metricas-de-calidad
plan: "03"
subsystem: metrics
tags: [metrics, test-detection, coverage, analyzer]
dependency_graph:
  requires: [10-01-PLAN.md, 10-02-PLAN.md]
  provides: [MetricsAnalyzer-complete, is_test_file, infer_production_target]
  affects: [src/sourcecode/metrics_analyzer.py, tests/test_metrics_analyzer.py]
tech_stack:
  added: []
  patterns: [regex-test-detection, stem-pattern-inference, coverage-wiring, production-target-resolution]
key_files:
  created: []
  modified:
    - src/sourcecode/metrics_analyzer.py
    - tests/test_metrics_analyzer.py
decisions:
  - "production_target stores full relative path (e.g. src/scanner.py) resolved by searching file_paths; infer_production_target() returns bare name only"
  - "CoverageParser imported at module level (not lazy) — same package, no circular dependency risk"
  - "Pre-existing test_integration_dependencies failure (Windows path separator) is out of scope and pre-dates this plan"
metrics:
  duration: ~15min
  completed: "2026-04-10"
  tasks_completed: 2
  files_modified: 2
---

# Phase 10 Plan 03: Test Association + CoverageParser Integration Summary

Complete MetricsAnalyzer with test-file detection, production-target inference, and CoverageParser wiring so analyze() returns fully-populated FileMetrics for all fields.

## What Was Built

### Task 1 — is_test_file() and infer_production_target()

**is_test_file(path: str) -> bool**

Module-level function using 16 anchored regex patterns covering:

| Ecosystem | Patterns matched |
|-----------|-----------------|
| Python | `tests?/` directory, `test_` prefix, `_test.py` suffix |
| Go | `_test.go` suffix |
| JS/TS | `.spec.{js,jsx,ts,tsx,mjs,cjs}`, `.test.*`, `__tests__/` directory |
| Java/Kotlin/Scala | `Test`, `Tests`, `Spec`, `IT` class-name suffix |
| Ruby | `spec/` directory, `_spec.rb`, `_test.rb` |
| Rust | `tests/` directory |
| Dart | `_test.dart` suffix, `test/` directory |
| C/C++ | `test`/`Test`/`spec` in filename, `tests?/` directory |
| PHP | `Test.php` suffix, `tests?/` directory |

Pitfalls correctly rejected:
- `testdata/fixtures/sample.py` — `testdata/` is not `tests/`
- `src/utils/context_helpers_tested.py` — `tested` word is not a test pattern
- `src/schema.go` — Go production file without `_test.go` suffix

**infer_production_target(test_path: str) -> str | None**

Operates on the basename only; returns bare filename or None. Five stem patterns:

| Pattern | Example | Result |
|---------|---------|--------|
| `test_<name>` | `test_scanner.py` | `scanner.py` |
| `<name>_test.<ext>` | `scanner_test.go` | `scanner.go` |
| `<name>(Test\|Tests\|Spec\|IT).<ext>` | `ScannerTest.java` | `Scanner.java` |
| `<name>.(spec\|test).<ext>` | `scanner.spec.ts` | `scanner.ts` |
| `<name>_spec.<ext>` | `scanner_spec.rb` | `scanner.rb` |

### Task 2 — CoverageParser Integration in analyze()

Three wiring additions to `MetricsAnalyzer.analyze()`:

1. **Coverage parsing** (before file loop):
   ```python
   coverage_parser = CoverageParser()
   coverage_records = coverage_parser.parse_all(root)
   file_cov_map = coverage_parser.build_file_coverage_map(root, coverage_records)
   ```

2. **Per-file population** (inside file loop after `_analyze_file()`):
   - `fm.is_test = is_test_file(norm_rel)`
   - If `is_test`: resolve `production_target` to full relative path by scanning `file_paths` for a non-test file whose `Path(p).name == inferred_name`
   - Coverage fields: `fm.line_rate`, `fm.branch_rate`, `fm.coverage_source`, `fm.coverage_availability = "measured"` when entry found in `file_cov_map`

3. **Summary population**:
   - `coverage_records=coverage_records`
   - `coverage_sources_found=sorted({r.format for r in coverage_records})`

## Test Counts

| Test ID | Name | Status |
|---------|------|--------|
| MQT-01 | test_python_loc | PASS |
| MQT-02 | test_python_symbols | PASS |
| MQT-03 | test_js_loc | PASS |
| MQT-03b | test_js_analyze_file_availability | PASS |
| MQT-04 | test_go_rust_loc | PASS |
| MQT-04b | test_go_analyze_file_availability | PASS |
| MQT-04c | test_rust_analyze_file_availability | PASS |
| MQT-09 | test_is_test_file | PASS (was skipped) |
| MQT-10 | test_infer_production | PASS (was skipped) |
| MQT-13a | test_graceful_degradation_unknown_extension | PASS |
| MQT-13b | test_graceful_degradation_python_syntax_error | PASS |
| MQT-13c | test_graceful_degradation_nonexistent_file | PASS |
| MQT-13d | test_graceful_degradation_analyze_no_exception | PASS |
| new | test_analyze_returns_records | PASS |
| new | test_analyze_populates_is_test | PASS |
| new | test_analyze_populates_production_target | PASS |
| new | test_analyze_coverage_integration | PASS |
| new | test_metrics_summary_totals | PASS |
| new | test_merge_summaries | PASS |

**Total: 19 tests, 19 passing, 0 skipped, 0 failures**

## Deviations from Plan

### Auto-noted: production_target path resolution

The plan prompt noted a warning: `production_target` should store the **full relative path** (e.g., `"src/scanner.py"`), not just the bare filename that `infer_production_target()` returns. This was implemented exactly as specified:

- `infer_production_target()` returns the bare name (`"scanner.py"`)
- `analyze()` searches `file_paths` for a non-test file matching that bare name and stores the full relative path in `fm.production_target`
- `test_analyze_populates_production_target` asserts `production_target == "src/scanner.py"` (full path)

### Pre-existing test failure (out of scope)

`tests/test_integration_dependencies.py::test_cli_dependencies_preserve_workspace_context_in_monorepo` fails due to a Windows path-separator issue (`\\` vs `/`) introduced in a prior phase. Verified to pre-exist before this plan's changes. Logged to deferred items — not caused by plan 10-03.

## Known Stubs

None — all FileMetrics fields are wired. `production_target` may be `None` when no matching production file exists in the tree, which is the correct behavior (not a stub).

## Self-Check

### Created/modified files exist:

- `src/sourcecode/metrics_analyzer.py` — FOUND (modified)
- `tests/test_metrics_analyzer.py` — FOUND (modified)

### Commits:

- `f68ea63` — Task 1: is_test_file + infer_production_target + activated MQT-09/MQT-10
- `6ad98d5` — Task 2: CoverageParser wiring + integration tests

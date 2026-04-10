---
phase: 10-metricas-de-calidad
plan: "02"
subsystem: metrics
tags: [coverage, cobertura-xml, lcov, jacoco, sqlite, stdlib, xml-etree]

# Dependency graph
requires:
  - phase: 10-01
    provides: CoverageRecord, FileMetrics, MetricsSummary dataclasses in schema.py
provides:
  - CoverageParser class with parse_all() and build_file_coverage_map()
  - _decode_numbits() helper for .coverage SQLite bitset decoding
  - Synthetic fixtures: tests/fixtures/coverage.xml, lcov.info, jacoco.xml
  - 6 passing tests MQT-05..08 plus test_parse_all_empty and test_build_file_coverage_map
affects:
  - 10-03 (integrates CoverageParser into MetricsAnalyzer.analyze())

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Coverage parsing via stdlib only (xml.etree.ElementTree, sqlite3, pathlib) — no external deps"
    - "Candidate-list iteration: try each path in order, return first valid parse, continue on error"
    - "sqlite3.DatabaseError silenced for .coverage files that are pickle (coverage.py < 5.0)"
    - "LCOV state machine: SF/LF/LH/BRF/BRH/end_of_record parsed line-by-line, totals summed"
    - "JaCoCo root-level counter elements only (direct children of <report>), not per-package counters"
    - "build_file_coverage_map priority: cobertura_xml > lcov > jacoco_xml > dot_coverage"

key-files:
  created:
    - src/sourcecode/coverage_parser.py
    - tests/test_coverage_parser.py
    - tests/fixtures/coverage.xml
    - tests/fixtures/lcov.info
    - tests/fixtures/jacoco.xml
  modified: []

key-decisions:
  - "stdlib-only: no lxml, no xml.dom — ET handles Cobertura and JaCoCo without DOCTYPE processing issues"
  - "JaCoCo DOCTYPE (<!DOCTYPE report ...>) omitted from fixture — ET.parse ignores DOCTYPE by default, no workaround needed"
  - "dot_coverage returns line_rate=None: total_lines context required by MetricsAnalyzer, not available in parser"
  - "build_file_coverage_map re-parses the artifact (not the CoverageRecord) to get per-class/per-file data"
  - "Paths outside root in dot_coverage discarded via relative_to() + ValueError catch (threat T-10-02-03)"
  - "lines_covered/lines_valid on lcov set to None when total_lf==0 (defensive against empty files)"

patterns-established:
  - "Parser returns None on any failure — never raises to caller (parse_all catches any remaining exceptions)"
  - "_safe_float/_safe_int helpers centralize attribute parsing with None fallback"
  - "_decode_numbits: little-endian bitset i*8+j+1 pattern for .coverage SQLite line_bits blob"

requirements-completed: [METRICS-03, OUT-10]

# Metrics
duration: 4min
completed: 2026-04-10
---

# Phase 10 Plan 02: CoverageParser Summary

**CoverageParser with 4 format parsers (Cobertura XML, .coverage SQLite, LCOV, JaCoCo XML) using stdlib only — no external dependencies**

## Performance

- **Duration:** 4 min
- **Started:** 2026-04-10T10:23:20Z
- **Completed:** 2026-04-10T10:27:24Z
- **Tasks:** 2 (Wave 0 TDD + Task 1 implementation)
- **Files modified:** 5 created, 0 modified

## Accomplishments

- CoverageParser class with `parse_all()` (collects all found formats, never raises) and `build_file_coverage_map()` (per-file rates with priority ordering)
- 4 format parsers: `_parse_cobertura_xml`, `_parse_dot_coverage`, `_parse_lcov`, `_parse_jacoco_xml` — all stdlib, all silent on failure
- `_decode_numbits()` for .coverage SQLite bitset decoding (little-endian, line i*8+j+1)
- 6 tests MQT-05..08 + 2 additional — all green; no regressions in 203-test suite

## Parser Details

| Parser | Format | Candidates | Format string |
|--------|--------|------------|---------------|
| `_parse_cobertura_xml` | Cobertura XML (coverage.py) | `coverage.xml`, `build/coverage.xml`, `target/coverage.xml`, `htmlcov/coverage.xml` | `"cobertura_xml"` |
| `_parse_dot_coverage` | SQLite .coverage (coverage.py >= 5.0) | `.coverage` (root only) | `"dot_coverage"` |
| `_parse_lcov` | LCOV text | `lcov.info`, `coverage/lcov.info`, `coverage.lcov` | `"lcov"` |
| `_parse_jacoco_xml` | JaCoCo XML | `jacoco.xml`, `build/reports/jacoco/test/jacocoTestReport.xml`, `target/site/jacoco/jacoco.xml` | `"jacoco_xml"` |

## build_file_coverage_map Strategy

Priority order: `cobertura_xml > lcov > jacoco_xml > dot_coverage`

For each format present in the records list, the method calls `_get_per_file_data()` which re-parses the artifact to extract per-class/per-file line_rate and branch_rate. Only the highest-priority format wins for any given file path. Paths outside the project root (possible with dot_coverage absolute paths) are discarded via `Path.relative_to()` + `ValueError` catch.

## Task Commits

Each task was committed atomically:

1. **Wave 0 TDD (RED):** `42945bd` — test(10-02): add failing tests MQT-05..08 and synthetic fixtures
2. **Task 1 (GREEN):** `5fc2186` — feat(10-02): CoverageParser with 4 format parsers (stdlib only)
3. **Quality gate (ruff):** `fd87b60` — chore(10-02): quality gate — ruff fixes (import sort, unused loop var)

## Files Created/Modified

- `src/sourcecode/coverage_parser.py` — CoverageParser class, 4 parsers, helpers, per-file extraction
- `tests/test_coverage_parser.py` — 6 tests: MQT-05 (cobertura), MQT-06 (dot_coverage), MQT-07 (lcov), MQT-08 (jacoco), test_parse_all_empty, test_build_file_coverage_map
- `tests/fixtures/coverage.xml` — Cobertura XML synthetic fixture (line-rate=0.85, branch-rate=0.72, 2 classes)
- `tests/fixtures/lcov.info` — LCOV synthetic fixture (2 records: scanner.py + schema.py)
- `tests/fixtures/jacoco.xml` — JaCoCo XML synthetic fixture (root-level LINE/BRANCH counters, 2 sourcefiles)

## Decisions Made

- **stdlib-only:** ET handles Cobertura and JaCoCo without external parsers. DOCTYPE in JaCoCo fixture was omitted to avoid potential parser issues — ET.parse ignores DOCTYPE by default anyway.
- **dot_coverage returns line_rate=None:** The parser cannot calculate line rate without `total_lines` context, which comes from MetricsAnalyzer. Plan 10-03 will wire these together.
- **Re-parsing in build_file_coverage_map:** The `CoverageRecord` only stores aggregate data; per-class rates require re-reading the XML/text artifact. This trades a small I/O cost for a clean data model.
- **lines_covered/lines_valid for LCOV:** Set to `total_lh`/`total_lf` when positive, None when zero — consistent with the nullable schema contract.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Ruff: unsorted import block and unused loop variable**
- **Found during:** Task 1 quality gate (post-implementation)
- **Issue:** ruff reported `I001` (import block un-sorted) and `B007` (loop variable `file_id` unused in `_per_file_dot_coverage`)
- **Fix:** `ruff check --fix` sorted the import block; renamed `file_id` to `_file_id` in the for-tuple
- **Files modified:** `src/sourcecode/coverage_parser.py`
- **Verification:** `ruff check` reports "All checks passed!", `mypy` reports no issues, all 6 tests still pass
- **Committed in:** `fd87b60`

---

**Total deviations:** 1 auto-fixed (Rule 1 — style/linting)
**Impact on plan:** Minor style fixes only. No behavioral or structural changes.

## Issues Encountered

- Pre-existing test failures (Windows path separator `\` vs `/`) in `test_integration_dependencies.py`, `test_integration_graph_modules.py`, `test_integration_multistack.py`, `test_workspace_analyzer.py`, `test_real_projects.py`, `test_packaging.py`. All confirmed pre-existing before this plan (present on prior commit `42945bd`). Not caused by this plan — logged as out-of-scope per deviation boundary rules.

## Known Stubs

None — all parsers return real data from fixtures; no hardcoded empty values flow to UI.

## Threat Flags

None — all new network/file-access surface was already in the plan's threat model (T-10-02-01 through T-10-02-04). All mitigations implemented (ET.ParseError caught, DatabaseError caught, relative_to() path normalization, errors="replace" for LCOV).

## Next Phase Readiness

- Plan 10-03 can import `CoverageParser` directly: `from sourcecode.coverage_parser import CoverageParser`
- `parse_all(root)` returns `list[CoverageRecord]` ready to populate `MetricsSummary.coverage_records`
- `build_file_coverage_map(root, records)` returns `{rel_path: (line_rate, branch_rate, source_name)}` for populating `FileMetrics.line_rate`, `.branch_rate`, `.coverage_source`
- No blockers.

---
*Phase: 10-metricas-de-calidad*
*Completed: 2026-04-10*

## Self-Check: PASSED

All files present, all commits verified.

| Item | Status |
|------|--------|
| src/sourcecode/coverage_parser.py | FOUND |
| tests/test_coverage_parser.py | FOUND |
| tests/fixtures/coverage.xml | FOUND |
| tests/fixtures/lcov.info | FOUND |
| tests/fixtures/jacoco.xml | FOUND |
| commit 42945bd (RED) | FOUND |
| commit 5fc2186 (GREEN) | FOUND |
| commit fd87b60 (quality gate) | FOUND |

---
phase: 09-llm-output-quality
plan: "02"
subsystem: doc-analyzer
tags: [importance, lqn-03, lqn-04, doc-record, filtering]
dependency_graph:
  requires: [09-01]
  provides: [DocRecord.importance, entry_points-param, unavailable-filter]
  affects: [src/sourcecode/schema.py, src/sourcecode/doc_analyzer.py, src/sourcecode/cli.py]
tech_stack:
  added: []
  patterns: [structural-importance-inference, unavailable-record-filtering, tdd]
key_files:
  created: []
  modified:
    - src/sourcecode/schema.py
    - src/sourcecode/doc_analyzer.py
    - src/sourcecode/cli.py
    - tests/test_schema.py
    - tests/test_doc_analyzer_python.py
    - tests/test_doc_analyzer_jsdom.py
    - tests/test_integration_docs.py
decisions:
  - "Filter unavailable records from docs[] entirely rather than marking them (LQN-04): limitations[] retains the information, output is cleaner"
  - "importance uses path.count('/') on posix paths for depth: root=high, 1-level=high, 2-level=medium, 3+=low (overridden by entry_points match)"
  - "Updated existing test_unsupported_language_emits_unavailable to test_unsupported_language_no_records_emitted to match new LQN-04 semantics"
metrics:
  duration: "~6 minutes"
  completed: "2026-04-10T05:20:24Z"
  tasks_completed: 3
  files_modified: 7
---

# Phase 9 Plan 02: DocRecord.importance + filter source=unavailable + entry_points param Summary

DocRecord importance field with structural inference (high/medium/low) + elimination of source=unavailable records from docs[] output + entry_points forwarding from cli.py to DocAnalyzer.

## What Was Built

### Fields Added to DocRecord (schema.py)

```python
importance: Literal["high", "medium", "low"] = "medium"
```

Placed before `workspace` field. `Literal` was already imported from `typing` — no new import needed.

### Importance Inference Rules (_infer_importance in doc_analyzer.py)

```python
@staticmethod
def _infer_importance(path, kind, entry_points) -> Literal["high","medium","low"]:
    if entry_points and path in entry_points:  return "high"
    depth = path.count("/")
    if depth <= 1:                              return "high"   # root or 1-level deep
    if depth == 2 or kind in {"class","function"}: return "medium"
    return "low"
```

Applied to every DocRecord emitted by `_analyze_python_file` (both module-level and symbol-level records) and `_analyze_node_file`.

### Change in Unsupported Language else-block

**Before (D-06):** emitted a DocRecord with `source="unavailable"` for .go/.java/.rs/etc. files.

**After (LQN-04):** the `records.append()` is removed; only `limitations.append()` and `languages.add()` remain. The limitation `docs_unavailable:{path}:language={lang}` is still recorded in DocSummary.

### cli.py Updates

Two calls to `doc_analyzer.analyze()` updated to forward entry_points:

1. Root call: `entry_points=[ep.path for ep in entry_points]`
2. Workspace call: `entry_points=[ep.path for ep in workspace_entry_points]`

(`workspace_entry_points` is the correct variable name from the workspace detection loop at line 279.)

### Modified Tests

**tests/test_schema.py** — 5 new tests added:
- `test_docrecord_importance_default_medium`
- `test_docrecord_importance_high_persists`
- `test_docrecord_importance_low_persists`
- `test_docrecord_asdict_includes_importance`
- `test_sourcemap_with_docrecord_serializes_importance`

**tests/test_doc_analyzer_python.py** — 8 new tests added (A1-A8):
- A1: entry_points match → importance="high"
- A2: depth=2 + kind="function" → "medium"
- A3: depth=0 (root) → "high"
- A4: depth=1 → "high"
- A5: depth=2 by depth → "medium"
- A6: depth=3 + kind="method" → "low"
- A7: .go file → no source="unavailable" in records
- A8: .go file → limitation with "language=go"

**tests/test_doc_analyzer_jsdom.py** — 3 new tests added (B1-B3) + 1 updated:
- B1: .ts entry point → "high"
- B2: src/utils/helper.ts (depth=2) kind="function" → "medium"
- B3: .go unsupported → records empty for that file
- `test_unsupported_language_emits_unavailable` renamed/updated to `test_unsupported_language_no_records_emitted` (LQN-04 semantics)

**tests/test_integration_docs.py** — 2 tests updated + 3 new:
- DOCS-ACC-02: added LQN-04 negative assertion (zero unavailable in docs[])
- DOCS-ACC-05: renamed `test_cli_docs_unsupported_language_filtered_with_limitation` (negative assertion for .go records)
- `test_lqn03_all_docrecords_have_valid_importance`
- `test_lqn04_no_docrecord_source_unavailable`
- `test_lqn04_multilang_project_has_unavailable_limitation`

## End-to-End Verification

```
OK: 436 docs, importances=['high', 'low', 'medium'], unavailable=0
```

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Updated test_unsupported_language_emits_unavailable to match new LQN-04 behavior**
- **Found during:** Task 2 GREEN phase
- **Issue:** The existing Phase 8 test `test_unsupported_language_emits_unavailable` in test_doc_analyzer_jsdom.py asserted `len(records) == 1` and `rec.source == "unavailable"` for a .go file. After implementing the else-block filter (PASO 6), this test correctly failed.
- **Fix:** Renamed test to `test_unsupported_language_no_records_emitted`, changed assertions to `len(records) == 0` and verified limitation is still present.
- **Files modified:** tests/test_doc_analyzer_jsdom.py
- **Commit:** cd815b9

## Pre-existing Failures (Out of Scope)

The following test failures existed before this plan and are Windows path separator issues (backslash vs forward-slash):
- `test_integration_dependencies.py::test_cli_dependencies_preserve_workspace_context_in_monorepo`
- `test_integration_graph_modules.py::test_cli_graph_modules_preserves_workspace_context_in_monorepo`
- `test_integration_multistack.py::test_cli_detects_pnpm_monorepo`
- `test_packaging.py::test_console_script_reports_version`
- `test_real_projects.py::test_monorepo_fixture_schema`
- `test_workspace_analyzer.py::test_workspace_analyzer_detects_pnpm_workspaces`

These are logged to deferred items and are not caused by this plan.

## Known Stubs

None — all DocRecord.importance values are computed from real structural signals (entry_points, path depth, kind).

## Threat Flags

None — no new network endpoints, auth paths, file access patterns, or schema changes at trust boundaries beyond what was planned.

## Self-Check: PASSED

- src/sourcecode/schema.py: FOUND
- src/sourcecode/doc_analyzer.py: FOUND
- src/sourcecode/cli.py: FOUND
- tests/test_schema.py: FOUND
- tests/test_doc_analyzer_python.py: FOUND
- tests/test_doc_analyzer_jsdom.py: FOUND
- tests/test_integration_docs.py: FOUND
- Commit c5fc285 (Task 1): FOUND
- Commit cd815b9 (Task 2): FOUND
- Commit fe60a24 (Task 3): FOUND

---
plan: 08-01
phase: 08-documentacion-extraida
status: completed
---

# Plan 01 Summary: Schema Base + CLI Scaffold + Test Stubs

## What was done

Added the foundational types and scaffold for Phase 8 (documentation extraction) following
the same lazy-flag pattern established by Phases 6 (dependencies) and 7 (graph-modules).

**Task 1 — Schema (DocRecord, DocSummary, DocsDepth) + Wave 0 stubs:**
- Added `DocsDepth = Literal["module", "symbols", "full"]` type alias to schema.py
- Added `DocRecord` dataclass (symbol, kind, language, path, doc_text, signature, source, workspace)
  matching the field specification from RESEARCH.md D-10
- Added `DocSummary` dataclass (requested, total_count, symbol_count, languages, depth, truncated, limitations)
  following the ModuleGraphSummary pattern
- Extended `SourceMap` with `docs: list[DocRecord]` and `doc_summary: Optional[DocSummary]`
  (both default to empty/None for backward compatibility)
- Created 10 Wave 0 stub tests in `test_doc_analyzer_python.py`
- Created 7 Wave 0 stub tests in `test_doc_analyzer_jsdom.py`
- Created 8 Wave 0 stub tests in `test_integration_docs.py`

**Task 2 — DocAnalyzer scaffold + CLI flags:**
- Created `src/sourcecode/doc_analyzer.py` with `DocAnalyzer` class:
  - Constants: `_MAX_FILES=200`, `_MAX_SYMBOLS_PER_MODULE=50`, `_DOCSTRING_MAX_CHARS=1000`,
    `_TRUNCATION_SUFFIX`, `_PYTHON_EXTENSIONS`, `_NODE_EXTENSIONS`
  - `analyze()` returns `([], DocSummary(requested=True, depth=depth))` — scaffold for Plan 02
  - `merge_summaries()` aggregates DocSummary list following DependencyAnalyzer pattern
- Added `DOCS_DEPTH_CHOICES = ["module", "symbols", "full"]` constant to cli.py
- Added `--docs` (bool, default False) and `--docs-depth` (str, default "symbols") parameters
- Added `docs_depth` validation with same pattern as `graph_detail` (error + exit 1)
- Added lazy imports: `DocAnalyzer` and `DocsDepth` inside `main()`
- Added `docs_depth_typed = cast(DocsDepth, docs_depth)` and `doc_analyzer = DocAnalyzer() if docs else None`
- Added `doc_records`/`doc_summaries` accumulators with root doc tree (pruning workspace paths)
  and per-workspace analysis inside the workspace loop
- Added `doc_summary = doc_analyzer.merge_summaries(doc_summaries) if doc_analyzer else None`
- Wired `docs=doc_records, doc_summary=doc_summary` into `SourceMap` constructor

**Verified:** `compact_view()` in serializer.py builds its dict manually and does NOT include
`docs` or `doc_summary` — no changes required.

## Files modified

- `src/sourcecode/schema.py` — added DocsDepth, DocRecord, DocSummary; extended SourceMap
- `src/sourcecode/cli.py` — added DOCS_DEPTH_CHOICES, --docs/--docs-depth flags, validation, wiring
- `src/sourcecode/doc_analyzer.py` (new) — DocAnalyzer scaffold
- `tests/test_doc_analyzer_python.py` (new) — 10 Wave 0 stubs
- `tests/test_doc_analyzer_jsdom.py` (new) — 7 Wave 0 stubs
- `tests/test_integration_docs.py` (new) — 8 Wave 0 stubs

## Verification results

```
tests/test_schema.py         12 passed
tests/test_cli.py             7 passed
tests/test_doc_analyzer_python.py  10 skipped (Wave 0 stubs)
tests/test_doc_analyzer_jsdom.py    7 skipped (Wave 0 stubs)
tests/test_integration_docs.py      8 skipped (Wave 0 stubs)

Total: 19 passed, 25 skipped
```

Acceptance criteria verified:
- `grep -c "class DocRecord" src/sourcecode/schema.py` → 1
- `grep -c "class DocSummary" src/sourcecode/schema.py` → 1
- `from sourcecode.schema import DocRecord, DocSummary, DocsDepth` → OK
- `SourceMap().docs == [] and SourceMap().doc_summary is None` → OK
- `from sourcecode.doc_analyzer import DocAnalyzer` → OK
- `grep "--docs" src/sourcecode/cli.py` → flag registered
- `pytest tests/test_cli.py -q` → 7 passed
- `sourcecode . --format json` without --docs → `docs=[]` and `doc_summary=null`

## Commits

- `f95d5af` feat(08-01): add DocRecord, DocSummary, DocsDepth to schema + Wave 0 test stubs
- `e010fac` feat(08-01): add DocAnalyzer scaffold and --docs/--docs-depth CLI flags

## Notes

- Followed DependencyAnalyzer pattern (not GraphAnalyzer prefix pattern) for DocAnalyzer,
  as DocRecord.workspace field identifies the workspace context without needing path prefixing
- `merge_summaries()` scaffold is functional (correctly aggregates totals, languages, truncated flag)
  even though `analyze()` returns empty records — Plan 02 only needs to fill in the parsers
- No deviations from plan — all acceptance criteria met as specified

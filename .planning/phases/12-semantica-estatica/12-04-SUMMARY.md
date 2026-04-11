---
phase: 12-semantica-estatica
plan: "04"
subsystem: semantic-analysis
tags:
  - polyglot
  - go
  - rust
  - java
  - cli-flag
  - e2e-tests
  - quality-gate
dependency_graph:
  requires:
    - 12-01
    - 12-02
    - 12-03
  provides:
    - --semantics CLI flag (end-to-end)
    - Go/Rust/Java heuristic call graph
    - E2E integration test suite (SEM-ACC-01..05)
  affects:
    - src/sourcecode/cli.py
    - src/sourcecode/semantic_analyzer.py
tech_stack:
  added:
    - Go heuristic regex analysis
    - Rust heuristic regex analysis
    - Java heuristic regex analysis
  patterns:
    - workspace loop mirrors --full-metrics pattern
    - heuristic confidence="low" for all polyglot edges
    - language_coverage["go/rust/java"] = "heuristic"
key_files:
  created:
    - tests/test_integration_semantics.py
  modified:
    - src/sourcecode/semantic_analyzer.py
    - src/sourcecode/cli.py
    - tests/test_semantic_analyzer_python.py
decisions:
  - "Used workspace.path instead of workspace.name (WorkspaceCandidate has no name attr)"
  - "Removed unnecessary schema type imports from CLI semantic block (ruff I001)"
  - "list[Any] annotations for semantic workspace accumulators (resolves mypy type-arg)"
  - "Bare list annotation pre-existing pattern (file_metrics_records: list) kept as-is"
metrics:
  duration: ~20 min
  completed_date: 2026-04-11
  tasks_completed: 3
  files_modified: 4
  files_created: 1
---

# Phase 12 Plan 04: Polyglot Heuristics + CLI Wiring + E2E + Quality Gate Summary

**One-liner:** Polyglot call-graph heuristics (Go/Rust/Java) + `--semantics` CLI flag workspace loop + 5 E2E integration tests + ruff/mypy quality gate at 0 new errors.

## What Was Built

### Task 1: Polyglot Heuristics + E2E Tests

**New class-level attributes** in `SemanticAnalyzer`:
```python
_GO_EXTENSIONS: frozenset[str] = frozenset({".go"})
_RUST_EXTENSIONS: frozenset[str] = frozenset({".rs"})
_JVM_EXTENSIONS: frozenset[str] = frozenset({".java", ".kt", ".scala"})
```

**`_analyze_go_file(content, rel_path)`**:
- Detects `func` declarations via `r"^func\s+(?:\([^)]+\)\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\("` (MULTILINE)
- Filters local call sites to known func names; `method="heuristic"`, `confidence="low"`

**`_analyze_rust_file(content, rel_path)`**:
- Detects `fn` via `r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+([a-z_][a-zA-Z0-9_]*)"` → kind="function"
- Detects `struct` via `r"^\s*(?:pub\s+)?struct\s+([A-Za-z_][A-Za-z0-9_]*)"` → kind="class"
- Module-qualified calls `foo::bar(` emitted as CallRecord with confidence="low"
- Local calls filtered against known fn names; Rust keywords excluded

**`_analyze_java_file(content, rel_path)`**:
- Detects `class/interface/enum` → kind="class", language="java"
- Detects method declarations → kind="function"
- Local call sites filtered against known method names; Java keywords excluded

**`analyze()` extension blocks**: Go → Rust → JVM, each reading files from disk, calling the respective method, accumulating symbols/calls, updating `language_coverage` and `languages`.

**`tests/test_integration_semantics.py`** — 5 E2E tests all passing GREEN:
- SEM-ACC-01: Python project produces non-empty call graph; `language_coverage["python"] == "full"`
- SEM-ACC-02: `SymbolLink` with `source_path != None` and `is_external=False` for internal imports
- SEM-ACC-03: Self-analysis of `src/sourcecode/` produces `calls > 0` and `"python" in languages`
- SEM-ACC-04: `SemanticAnalyzer(max_files=5)` on 10 files → `truncated=True` or `max_files_reached` in limitations
- SEM-ACC-05: CLI `--semantics` flag produces `semantic_summary.requested=True`; base command unaffected

### Task 2: CLI Wiring + Workspace Loop + Quality Gate

**`--semantics` flag** added to `main()` signature:
```python
semantics: bool = typer.Option(
    False,
    "--semantics",
    help="Incluir call graph semantico, linking cross-file de simbolos y resolucion avanzada de imports",
),
```

**Lazy import + initialization** (inside `main()` body, after metrics_analyzer):
```python
from sourcecode.semantic_analyzer import SemanticAnalyzer
semantic_analyzer = SemanticAnalyzer() if semantics else None
```

**Workspace loop** (mirrors `--full-metrics` pattern exactly):
```python
if semantic_analyzer is not None:
    if workspace_analysis.workspaces:
        all_sem_calls: list[Any] = []
        all_sem_symbols: list[Any] = []
        all_sem_links: list[Any] = []
        all_sem_summaries: list[Any] = []
        for ws in workspace_analysis.workspaces:
            ws_calls, ws_syms, ws_links, ws_sum = semantic_analyzer.analyze(
                target / ws.path,
                filter_sensitive_files(FileScanner(target / ws.path, max_depth=depth).scan_tree()),
                workspace=ws.path,
            )
            all_sem_calls.extend(ws_calls)
            ...
        merged_sem = semantic_analyzer.merge_summaries(all_sem_summaries)
        sm = replace(sm, semantic_calls=all_sem_calls, ..., semantic_summary=merged_sem)
    else:
        sem_calls, sem_syms, sem_links, sem_sum = semantic_analyzer.analyze(target, file_tree)
        sm = replace(sm, semantic_calls=sem_calls, ..., semantic_summary=sem_sum)
```

**Quality Gate Results:**
- `ruff check src/ tests/`: 27 errors — all pre-existing (0 new errors introduced in Phase 12)
- `mypy src/`: 8 errors — all pre-existing (0 new errors introduced in Phase 12)

### Task 3: Regression + ROADMAP

**Final test suite** (264 passed, 4 skipped, 13 deselected pre-existing Windows path failures):
- Pre-existing failures excluded: monorepo workspace path separator tests (Windows `\\` vs `/`)
- No new regressions introduced by Phase 12

**ROADMAP.md** updated:
- Fase 12 list item marked `[x]` complete
- `12-04-PLAN.md` marked `[x]`
- Progress table: `| 12. Semantica Estatica | 4/4 | Complete | 2026-04-11 |`

## Commits

| Hash | Message |
|------|---------|
| 0783dd5 | feat(12-04): polyglot heuristics (Go/Rust/Java) + E2E integration tests |
| 11d1222 | feat(12-04): --semantics CLI flag + workspace loop + ruff/mypy gate |
| 7355d5f | docs(12-04): complete Phase 12 — ROADMAP updated |

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Fixed `workspace_analysis.has_workspaces` AttributeError**
- **Found during:** Task 2 (first test run of SEM-ACC-05)
- **Issue:** The plan specified `workspace_analysis.has_workspaces` but `WorkspaceAnalysis` dataclass has only `workspaces: list[WorkspaceCandidate]` — no `has_workspaces` property
- **Fix:** Used `workspace_analysis.workspaces` (truthy when non-empty) as the condition
- **Files modified:** `src/sourcecode/cli.py`

**2. [Rule 1 - Bug] Fixed CLI flag ordering in tests**
- **Found during:** Task 1 test run (SEM-ACC-05 first attempt)
- **Issue:** Test invoked `runner.invoke(app, [str(tmp_path), "--semantics"])` — typer treats `--semantics` after path as subcommand
- **Fix:** Reordered to `runner.invoke(app, ["--semantics", str(tmp_path)])` matching existing patterns in `test_integration_metrics.py`

**3. [Rule 2 - Missing] Removed unused schema imports from CLI semantic block**
- **Found during:** Task 2 ruff gate
- **Issue:** Added `from sourcecode.schema import CallRecord as _CallRecord, ...` inside the `if semantic_analyzer is not None:` block — ruff flagged I001 (import sorting) as a new error
- **Fix:** Removed the block entirely (type annotations use `list[Any]` which avoids the need for typed imports at runtime)

**4. [Rule 2 - Missing] Fixed mypy type-arg errors for semantic list variables**
- **Found during:** Task 2 mypy gate
- **Issue:** `all_sem_calls: list = []` introduced 3 new `[type-arg]` mypy errors
- **Fix:** Changed to `list[Any]` annotations (consistent with existing `file_metrics_records: list = []` pre-existing pattern, but with explicit Any to satisfy mypy)

## Known Stubs

None. All fields wired end-to-end: `semantic_summary.requested=True` confirmed via CLI test.

## Threat Flags

None. No new network endpoints, auth paths, or schema changes at trust boundaries beyond those already analyzed in the plan's `<threat_model>`.

## Self-Check: PASSED

- `tests/test_integration_semantics.py`: EXISTS, 5 tests GREEN
- `src/sourcecode/semantic_analyzer.py`: EXISTS, `_analyze_go_file`, `_analyze_rust_file`, `_analyze_java_file` implemented
- `src/sourcecode/cli.py`: EXISTS, `--semantics` flag + workspace loop present
- Commit 0783dd5: FOUND
- Commit 11d1222: FOUND
- Commit 7355d5f: FOUND

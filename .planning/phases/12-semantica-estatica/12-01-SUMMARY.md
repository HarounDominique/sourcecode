---
phase: 12-semantica-estatica
plan: "01"
subsystem: semantic-analysis
tags: [schema, semantic, call-graph, python, ast, tdd]
dependency_graph:
  requires: []
  provides:
    - sourcecode.schema.SymbolRecord
    - sourcecode.schema.CallRecord
    - sourcecode.schema.SymbolLink
    - sourcecode.schema.SemanticSummary
    - sourcecode.schema.SourceMap (Phase 12 fields)
    - sourcecode.semantic_analyzer.SemanticAnalyzer
  affects:
    - src/sourcecode/schema.py
    - src/sourcecode/semantic_analyzer.py
tech_stack:
  added: []
  patterns:
    - TDD Wave 0 (RED -> GREEN)
    - Two-pass AST analysis (symbol index + call resolution)
    - ImportBindings dict for import resolution
    - Budget guards with limitations[] degradation
key_files:
  created:
    - src/sourcecode/semantic_analyzer.py
    - tests/test_semantic_schema.py
    - tests/test_semantic_analyzer_python.py
  modified:
    - src/sourcecode/schema.py
decisions:
  - "CallRecord.args and CallRecord.kwargs use field(default_factory=...) to avoid shared mutable defaults"
  - "SemanticAnalyzer does NOT import GraphAnalyzer — _build_python_module_map is reimplemented locally"
  - "Dynamic calls (ast.Subscript, ast.Call as func) emit dynamic_call_skipped limitation; unresolved ast.Name calls are silently ignored per spec"
  - "Pass 1 _build_symbol_index also handles file-size and syntax guards to avoid double-processing in Pass 2"
metrics:
  duration: "~25 minutes"
  completed: "2026-04-11"
  tasks_completed: 3
  files_created: 3
  files_modified: 1
---

# Phase 12 Plan 01: Schema Semantico + SemanticAnalyzer Python two-pass call graph Summary

**One-liner:** Four semantic dataclasses added to schema.py + SemanticAnalyzer with Python AST two-pass call graph (symbol index + ImportBindings-based resolution) with budget guards and graceful degradation.

## Objective

Establish semantic schema contracts (SymbolRecord, CallRecord, SymbolLink, SemanticSummary) and create the SemanticAnalyzer with Python core (two-pass index + resolution) so plans 12-02 and 12-03 have the types and engine they need.

## Dataclasses Added to schema.py

### SymbolRecord
- `symbol: str` — local name ("MyClass", "my_func")
- `kind: str` — "function" | "class" | "constant" | "method"
- `language: str`
- `path: str` — relative path to defining file
- `line: Optional[int] = None`
- `qualified_name: Optional[str] = None` — "pkg.module.MyClass"
- `exported: bool = True` — False if name starts with _
- `workspace: Optional[str] = None`

### CallRecord
- `caller_path: str`, `caller_symbol: str`
- `callee_path: str`, `callee_symbol: str`
- `call_line: Optional[int] = None`
- `confidence: Literal["high", "medium", "low"] = "medium"`
- `method: Literal["ast", "heuristic", "unresolved"] = "heuristic"`
- `args: list[str] = field(default_factory=list)`
- `kwargs: dict[str, str] = field(default_factory=dict)` — uses field() to avoid mutable default
- `workspace: Optional[str] = None`

### SymbolLink
- `importer_path: str`, `symbol: str`
- `source_path: Optional[str] = None`
- `source_line: Optional[int] = None`
- `is_external: bool = False`
- `confidence: Literal["high", "medium", "low"] = "high"`
- `method: Literal["ast", "heuristic", "unresolved"] = "ast"`
- `workspace: Optional[str] = None`

### SemanticSummary
- `requested: bool = False`
- `call_count: int = 0`, `symbol_count: int = 0`, `link_count: int = 0`
- `languages: list[str] = field(default_factory=list)`
- `language_coverage: dict[str, str] = field(default_factory=dict)`
- `files_analyzed: int = 0`, `files_skipped: int = 0`
- `truncated: bool = False`
- `limitations: list[str] = field(default_factory=list)`

### SourceMap Phase 12 extensions (at end of class)
```python
semantic_calls: list[CallRecord] = field(default_factory=list)
semantic_symbols: list[SymbolRecord] = field(default_factory=list)
semantic_links: list[SymbolLink] = field(default_factory=list)
semantic_summary: Optional[SemanticSummary] = None
```

## Two-Pass Architecture (SemanticAnalyzer)

### Pass 1: Symbol Index (_build_symbol_index)
- Walks `tree.body` for each Python file
- Indexes `FunctionDef`, `AsyncFunctionDef` (kind="function") and `ClassDef` (kind="class") at top-level
- Records `line=node.lineno`, `exported = not name.startswith("_")`
- Returns `dict[rel_path -> list[SymbolRecord]]`
- Guard: max_symbols=10_000 (appends limitation if reached)
- Handles SyntaxError, OSError, file_too_large inline

### Pass 2: Call Resolution (_resolve_calls in analyze())
- For each file: builds ImportBindings, emits SymbolLinks, walks FunctionDef/AsyncFunctionDef bodies
- For `ast.Name` callees: resolves via ImportBindings -> reverse_module_map -> symbol_index (cross-file, confidence="high") or same-file symbol_index (confidence="high")
- For `ast.Attribute(value=ast.Name)` callees: resolves module alias via ImportBindings -> symbol lookup (confidence="medium")
- Dynamic calls (ast.Subscript/ast.Call as func): emits `dynamic_call_skipped` limitation
- Unresolved ast.Name calls: silently skipped (per spec)
- Guard: max_calls=5_000 -> SemanticSummary.truncated=True + "call_budget_reached" limitation

## ImportBindings Implementation

`_build_import_bindings(tree, rel_path, module_map, limitations)` returns `dict[local_name -> (source_module_dotted, original_symbol)]`:

- `import foo.bar as fb` -> `bindings["fb"] = ("foo.bar", "foo.bar")`
- `import foo.bar` -> `bindings["foo"] = ("foo.bar", "foo.bar")`
- `from pkg.mod import Foo` -> `bindings["Foo"] = ("pkg.mod", "Foo")`
- `from pkg.mod import Foo as F` -> `bindings["F"] = ("pkg.mod", "Foo")`
- Relative imports (`level > 0`): resolved via `_resolve_relative_import(rel_path, level, module)`
- Star imports (`*`): silently skipped (deferred to plan 12-02)
- Name shadowing detection: ast.Assign at module level rebinding an imported name removes binding and appends `name_shadowed:{path}:{name}` to limitations

## Guards Applied (All Threat Mitigations Active)

| Guard | Value | Trigger | Effect |
|-------|-------|---------|--------|
| max_files | 200 | >200 Python files | Truncate + `max_files_reached:N>M` |
| max_file_size | 200_000 bytes | File > 200KB | Skip + `file_too_large:{path}` |
| max_calls | 5_000 | >5000 calls | truncated=True + `call_budget_reached` |
| max_symbols | 10_000 | >10000 symbols | Truncate + `max_symbols_reached` |
| arg length | 80 chars | ast.unparse > 80 | Replace with `<expr>` |

## Test Metrics

**Schema tests (tests/test_semantic_schema.py):** 6 passing
- SEM-SCHEMA-01: SymbolRecord defaults
- SEM-SCHEMA-02: CallRecord defaults (+ mutable default independence)
- SEM-SCHEMA-03: SymbolLink defaults
- SEM-SCHEMA-04: SemanticSummary defaults
- SEM-SCHEMA-05: SourceMap backward compatibility

**Python core tests (tests/test_semantic_analyzer_python.py):** 5 passing, 5 skipped
- SEM-PY-01: Symbol index builds (foo + Bar with correct kind/line)
- SEM-PY-02: Direct cross-file call resolution (from target import greet; greet())
- SEM-PY-03: Same-file call resolution (helper called from main)
- SEM-PY-04: Budget guards (max_files + max_calls)
- SEM-PY-05: Graceful degradation (SyntaxError, missing file, dynamic call)
- 5 stubs skip: test_reexport_resolution, test_star_import_expansion, test_js_call_resolution, test_go_heuristic_calls, test_semantics_cli_flag

**Total new tests:** 11 passing, 5 skipped
**Regression:** 0 new failures (2 pre-existing integration test failures unrelated to this plan)

## Deviations from Plan

None — plan executed exactly as written.

The only deviation from plan language: the dynamic call detection in Pass 2 only adds `dynamic_call_skipped` for clearly dynamic patterns (`ast.Subscript`, `ast.Call` as func). Per spec, `ast.Name` unresolved calls are silently skipped (no limitation), which is the correct behavior — the plan states limitations for dynamic calls but not for simply unresolved names.

## Pre-existing Test Failures (Not Caused by This Plan)

Two integration tests fail on this branch before and after this plan's changes — confirmed via `git stash` check:
- `tests/test_integration_dependencies.py::test_cli_dependencies_preserve_workspace_context_in_monorepo`
- `tests/test_integration_graph_modules.py::test_cli_graph_modules_preserves_workspace_context_in_monorepo`

Both fail with a monorepo workspace context assertion (`'apps/web'`/`'packages/api'` set mismatch) unrelated to semantic analysis.

## Known Stubs

None — all implemented functionality is wired to tests.

## Threat Flags

None — no new network endpoints, auth paths, file access patterns, or schema changes at trust boundaries beyond what the plan's threat model already covers.

## Commits

| Hash | Type | Description |
|------|------|-------------|
| e0bd4fc | test | Wave 0 RED tests for semantic schema and SemanticAnalyzer |
| 79f537f | feat | Extend schema.py with 4 dataclasses + SourceMap Phase 12 fields |
| 2234d1c | feat | Create semantic_analyzer.py with two-pass Python call graph |

## Self-Check: PASSED

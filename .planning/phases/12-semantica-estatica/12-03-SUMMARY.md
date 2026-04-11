---
phase: 12-semantica-estatica
plan: "03"
subsystem: semantic-analysis
tags: [semantic, js, ts, javascript, typescript, call-graph, heuristic, tdd]
dependency_graph:
  requires:
    - 12-01 (SemanticAnalyzer base, SymbolRecord, CallRecord, SymbolLink, SemanticSummary)
    - 12-02 (Python import resolution, _link_symbols, reexport_map)
  provides:
    - sourcecode.semantic_analyzer._JS_KEYWORD_EXCLUSIONS
    - sourcecode.semantic_analyzer.SemanticAnalyzer._extract_js_imports
    - sourcecode.semantic_analyzer.SemanticAnalyzer._resolve_js_module_path
    - sourcecode.semantic_analyzer.SemanticAnalyzer._analyze_js_file
    - sourcecode.semantic_analyzer.SemanticAnalyzer._detect_js_calls
    - JS/TS support in SemanticAnalyzer.analyze()
    - language_coverage["nodejs"] = "heuristic"
  affects:
    - src/sourcecode/semantic_analyzer.py
tech_stack:
  added: []
  patterns:
    - TDD Wave 0 (RED -> GREEN)
    - Regex-based heuristic JS/TS analysis (no AST)
    - Import binding map for cross-file call resolution
    - Keyword exclusion frozenset for false-positive suppression
    - String literal redaction in arg capture (security)
key_files:
  created:
    - tests/test_semantic_analyzer_node.py
  modified:
    - src/sourcecode/semantic_analyzer.py
decisions:
  - "_JS_KEYWORD_EXCLUSIONS is module-level frozenset (not class-level) so it can be imported directly by tests"
  - "export default function name() pattern required extending _analyze_js_file regex to include 'default' keyword"
  - "_detect_js_calls only emits CallRecord if identifier is in js_bindings — no speculative calls for untraced identifiers"
  - "Default import binding uses local_name as callee_symbol since the importer chooses the name"
  - "String literal args replaced with '<string_literal>' regardless of content (T-12-03-03 mitigation)"
  - "_resolve_js_module_path uses Path.resolve() + relative_to(root) to prevent path traversal (T-12-03-05)"
metrics:
  duration: "~35 minutes"
  completed: "2026-04-11"
  tasks_completed: 2
  files_created: 1
  files_modified: 1
---

# Phase 12 Plan 03: JS/TS Semantic Layer + Basic Dataflow Summary

**One-liner:** JS/TS heuristic semantic layer added to SemanticAnalyzer via regex import binding extraction, _resolve_js_module_path extension probing, and _detect_js_calls filtered by _JS_KEYWORD_EXCLUSIONS frozenset, with string literal redaction in captured args.

## Objective

Extend `SemanticAnalyzer.analyze()` to process JS/TS files alongside Python, producing `CallRecord` (method="heuristic"), `SymbolRecord`, and `SymbolLink` entries for projects using React, Node.js APIs, or mixed monorepos. Set `language_coverage["nodejs"] = "heuristic"` when JS/TS files are present.

## New Module-Level Constant

### `_JS_KEYWORD_EXCLUSIONS: frozenset[str]`

Defined at module level (importable directly). Contains:
- **JS reserved words:** `if`, `else`, `for`, `while`, `do`, `switch`, `case`, `break`, `continue`, `return`, `throw`, `try`, `catch`, `finally`, `new`, `delete`, `typeof`, `instanceof`, `void`, `in`, `of`, `async`, `await`, `yield`, `import`, `export`, `default`, `class`, `extends`, `super`, `this`, `static`, `get`, `set`, `let`, `const`, `var`, `function`, `debugger`, `with`
- **Browser/Node builtins:** `console`, `Math`, `Object`, `Array`, `String`, `Number`, `Boolean`, `Promise`, `Error`, `TypeError`, `RangeError`, `Symbol`, `Map`, `Set`, `WeakMap`, `WeakSet`, `Proxy`, `Reflect`, `JSON`, `RegExp`, `Date`, `setTimeout`, `clearTimeout`, `setInterval`, `clearInterval`, `queueMicrotask`, `require`, `module`, `exports`, `process`, `global`, `window`, `document`, `navigator`, `location`, `fetch`, `URL`, `URLSearchParams`, `FormData`
- **TypeScript keywords:** `type`, `interface`, `namespace`, `declare`, `abstract`, `enum`, `as`, `from`, `keyof`, `typeof`, `infer`, `never`, `unknown`, `any`

## New Class Attribute

```python
_NODE_EXTENSIONS: frozenset[str] = frozenset({".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"})
```

## Methods Added to SemanticAnalyzer

### `_extract_js_imports(content, rel_path) -> dict[str, tuple[str, str]]`

Extracts import bindings in priority order:

| Pattern | Input | Output |
|---------|-------|--------|
| Namespace | `import * as ns from './foo'` | `{"ns": ("./foo", "*")}` |
| Named | `import { Foo, Bar as B } from './foo'` | `{"Foo": ("./foo","Foo"), "B": ("./foo","Bar")}` |
| Default | `import format from './utils'` | `{"format": ("./utils", "default")}` |
| CJS destructure | `const { parse } = require('./lib')` | `{"parse": ("./lib", "parse")}` |
| CJS plain | `const foo = require('./mod')` | `{"foo": ("./mod", "default")}` |

All patterns use `re.MULTILINE` and support both single and double quotes.

### `_resolve_js_module_path(specifier, caller_path, internal_module_paths, *, root) -> str | None`

- Returns `None` for specifiers not starting with `.` or `/` (external npm packages)
- Resolves relative path from caller directory
- Probes extensions in order: as-is, `.js`, `.ts`, `.jsx`, `.tsx`, `/index.js`, `/index.ts`
- Uses `Path.resolve()` + `relative_to(root)` to prevent path traversal (T-12-03-05 mitigation)
- Returns posix rel_path if found in `internal_module_paths`, else `None`

### `_analyze_js_file(content, rel_path) -> list[SymbolRecord]`

Detects top-level symbols using three regex patterns (re.MULTILINE):
- `export default function name()` / `export function name()` / `function name()` → kind="function"
- `export class Name` / `class Name` → kind="class"
- `export const name = function(` / `export const name = (` → kind="function"

Language: `"typescript"` for `.ts`/`.tsx`, `"javascript"` for all others.
Line numbers computed via `content[:match.start()].count('\n') + 1`.

### `_detect_js_calls(content, rel_path, caller_symbol, js_bindings, js_symbol_index, internal_module_paths, *, root, workspace) -> list[CallRecord]`

Two detection patterns:

**Pattern 1 — Namespace member call** (`obj.method(`):
- Object must be in `js_bindings` with binding `("specifier", "*")`
- Method must exist in the resolved module's symbol index
- Emits `CallRecord(confidence="medium", method="heuristic")`

**Pattern 2 — Direct identifier call** (`name(`):
- FILTERS `_JS_KEYWORD_EXCLUSIONS` — if name in exclusions, skip immediately
- Identifier must be in `js_bindings` — no speculative calls for untraced identifiers
- For default imports: local binding name IS the callee_symbol
- For named imports: original export name is the callee_symbol
- Emits `CallRecord(confidence="medium", method="heuristic")`

### `_capture_js_call_args(content, paren_pos) -> list[str]` (static)

Captures up to 5 arguments with balanced parenthesis tracking:
- Simple identifiers (`myData`, `config.timeout`) → captured textually
- Numeric literals → captured textually
- String literals (`'...'`, `"..."`, `` `...` ``) → replaced with `"<string_literal>"` (T-12-03-03 mitigation — do not expose content)
- Complex expressions (contain `(`, `?`, `=>`, `{`, etc.) → `"<expr>"`

## analyze() Integration

JS/TS block runs after Python analysis:
1. Collects all files matching `_NODE_EXTENSIONS` → `js_source_files`
2. Builds `internal_module_paths = set(js_source_files)` for cross-file resolution
3. **Pass 1:** `_analyze_js_file` + `_extract_js_imports` for all JS/TS files
4. **Pass 2:** `_detect_js_calls` per function symbol in each file; max_calls guard is shared with Python (not reset)
5. **SymbolLinks:** Internal bindings (`resolved != None`) → `is_external=False, method="heuristic"`; External (npm packages) → `is_external=True`
6. Extends `languages` list and sets `lang_coverage["nodejs"] = "heuristic"`

## Key Behaviors Confirmed by Tests

| Behavior | Test |
|----------|------|
| `_JS_KEYWORD_EXCLUSIONS` importable, is frozenset | `test_keyword_exclusion_constants` |
| Named import binding | `test_named_imports_extracted`, `test_multiple_named_imports_extracted` |
| Default import binding | `test_default_import_binding` |
| Default import → CallRecord with resolved callee_path | `test_default_import` |
| Namespace import binding | `test_namespace_import_binding` |
| Namespace member call → CallRecord | `test_namespace_call_produces_record` |
| CJS destructure binding | `test_cjs_require_binding` |
| CJS plain binding | `test_cjs_require_plain_binding` |
| CJS require → CallRecord | `test_cjs_require_produces_call` |
| if/for/console → zero CallRecords | `test_keyword_exclusion_no_call_records` |
| Simple identifier arg captured textually | `test_basic_dataflow_args` |
| String literal arg → `"<string_literal>"` | `test_string_literal_in_args_redacted` |
| TypeScript class → SymbolRecord(kind="class", language="typescript") | `test_ts_class_detection` |
| `language_coverage["nodejs"] == "heuristic"` | `test_js_language_coverage` |
| External npm import → `SymbolLink(is_external=True)` | `test_external_import_produces_external_link` |
| Internal relative import → `SymbolLink(is_external=False)` | `test_internal_import_produces_internal_link` |
| Cross-file JS call resolution | `test_js_call_resolution` |

## Test Metrics

**JS/TS tests (tests/test_semantic_analyzer_node.py):** 18 passing, 0 failing
- SEM-NODE-01: Named imports extracted (2 tests)
- SEM-NODE-02: Default import + call resolution (2 tests)
- SEM-NODE-03: Namespace import + call detection (2 tests)
- SEM-NODE-04: CommonJS require (3 tests)
- SEM-NODE-05: Keyword exclusion (2 tests)
- SEM-NODE-06: Basic dataflow args capture + string literal redaction (2 tests)
- TypeScript class detection (1 test)
- language_coverage nodejs (1 test)
- External/internal SymbolLink (2 tests)
- Cross-file JS call resolution (1 test)

**Python tests (no regressions):** 35 passing, 5 skipped (stubs for plans 12-04+)

**Broad regression check (92 unit tests):** 92 passed, 6 skipped — no new failures.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] `export default function` not detected by _analyze_js_file regex**
- **Found during:** Task 2 (test_default_import failing)
- **Issue:** The plan's specified regex `(?:export\s+)?(?:async\s+)?function` does not match `export default function name()` because `default` is a separate keyword between `export` and `function`
- **Fix:** Extended the pattern to `(?:export\s+(?:default\s+)?)?(?:async\s+)?function` — same for class patterns
- **Files modified:** `src/sourcecode/semantic_analyzer.py` (_analyze_js_file)
- **Commit:** f370dd0 (included in main feat commit)

## Pre-existing Test Failures (Not Caused by This Plan)

Multiple integration tests fail on this branch before and after this plan — confirmed via `git stash` check. All are Windows path separator issues (`'apps/web'` vs `'apps\\web'`):
- `test_integration_dependencies.py::test_cli_dependencies_preserve_workspace_context_in_monorepo`
- `test_integration_graph_modules.py::test_cli_graph_modules_preserves_workspace_context_in_monorepo`
- `test_integration_multistack.py::test_cli_detects_pnpm_monorepo`
- `test_workspace_analyzer.py::test_workspace_analyzer_detects_pnpm_workspaces`
- `test_packaging.py::test_console_script_reports_version` (WinError 193)

None of these are caused by this plan's changes.

## Known Stubs

None — all implemented functionality is wired to real tests and produces observable output.

## Threat Flags

None — no new network endpoints, auth paths, file access patterns, or schema changes beyond what the plan's threat model already covers. All threat mitigations (T-12-03-01 through T-12-03-05) are implemented as specified.

## Commits

| Hash | Type | Description |
|------|------|-------------|
| f370dd0 | feat | JS/TS semantic layer + basic dataflow (18 tests passing) |

## Self-Check: PASSED

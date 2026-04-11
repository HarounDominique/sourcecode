---
phase: 12-semantica-estatica
plan: "02"
subsystem: semantic-analysis
tags: [semantic, import-resolution, reexport, star-import, namespace-packages, symbol-link, python, ast, tdd]
dependency_graph:
  requires:
    - sourcecode.schema.SymbolRecord
    - sourcecode.schema.SymbolLink
    - sourcecode.schema.SemanticSummary
    - sourcecode.semantic_analyzer.SemanticAnalyzer (plan 12-01 base)
  provides:
    - SemanticAnalyzer._build_reexport_map
    - SemanticAnalyzer._resolve_star_imports
    - SemanticAnalyzer._link_symbols
    - SemanticAnalyzer._resolve_via_reexport
    - SemanticAnalyzer._find_symbol_line
    - SemanticAnalyzer._build_python_module_map (extended: namespace packages + limitations param)
    - SemanticAnalyzer._build_import_bindings (extended: star import expansion)
    - SemanticSummary.language_coverage["python"] == "full"
  affects:
    - src/sourcecode/semantic_analyzer.py
tech_stack:
  added: []
  patterns:
    - Reexport chain traversal (depth=2 limit with visited guard)
    - Star import expansion via __all__ AST or public name fallback
    - SymbolLink consolidation over all import bindings
    - Namespace package detection (dirs without __init__.py)
    - Reexport-first resolution priority in _link_symbols
key_files:
  created:
    - tests/test_semantic_import_resolution.py
  modified:
    - src/sourcecode/semantic_analyzer.py
decisions:
  - "_link_symbols checks reexport_map BEFORE direct module_map resolution — ensures 'from pkg import Symbol' resolves to the defining submodule, not pkg/__init__.py"
  - "_resolve_star_imports builds its own reverse_module_map locally (no side effects)"
  - "_build_reexport_map second pass follows one additional chain level by detecting __init__.py targets and resolving them through the already-built reexport_map"
  - "Namespace packages reported via limitations['namespace_package:{dir}'] — files still fully indexed with confidence unaffected at SymbolRecord level"
  - "Star import symbols added to bindings as (star_module, name) tuples — reuse existing resolution pipeline in Pass 2 and _link_symbols"
metrics:
  duration: "~30 minutes"
  completed: "2026-04-11"
  tasks_completed: 2
  files_created: 1
  files_modified: 1
---

# Phase 12 Plan 02: Python Import Resolution Avanzada + Symbol Linker + Namespace Packages Summary

**One-liner:** SemanticAnalyzer extended with four new methods covering reexport chains via __init__.py (depth=2), star import expansion via __all__ or public names, SymbolLink consolidation for all internal Python imports, and namespace package detection — setting language_coverage["python"]="full".

## Objective

Extend SemanticAnalyzer with advanced Python import resolution so that the 30-40% of imports in real projects (using __init__.py facades, star imports, and namespace packages) are correctly resolved to their defining source files.

## Methods Added to semantic_analyzer.py

### `_build_reexport_map(root, source_files, module_map, limitations) -> dict[str, dict[str, str]]`

Scans all `__init__.py` files in `source_files` and builds:
```
{ module_dotted -> { symbol_name -> source_posix_path } }
```

**Algorithm:**
1. First pass: for each `__init__.py`, parse AST, collect all `ImportFrom` nodes (relative and absolute). For each alias, register `reexport_map[module][symbol] = source_rel_path`.
2. Second pass (depth=2 chaining): for any entry where `source_posix_path` is itself an `__init__.py`, look up `reexport_map[source_module][symbol]` and update the entry to the resolved path. If the lookup fails: `limitations.append("reexport_chain_limit:{symbol}")`.

**Threat mitigations (T-12-02-01, T-12-02-05):**
- Chain depth limit = 2 (two passes only, no recursion)
- Only parses `__init__.py` files already in `source_files` (bounded by max_files guard from plan 12-01)

### `_resolve_via_reexport(source_module, original_symbol, reexport_map) -> str | None`

Simple helper: `return reexport_map.get(source_module, {}).get(original_symbol)`. Returns `source_path` (posix rel path) or `None`.

### `_resolve_star_imports(root, star_module, module_map, symbol_index, limitations) -> list[str]`

Expands `from foo import *` to a list of exported symbol names.

**Strategy:**
1. Reverse-lookup `star_module` in `module_map` to find the file path. If not found: external module — `limitations.append("star_import_external:{star_module}")` and return `[]`.
2. Parse the module's AST. Look for `__all__ = [...]` at module level (must be `ast.List` of `ast.Constant` strings).
3. If `__all__` found and static: return those names (up to 200).
4. Fallback: collect all public names at module level (`FunctionDef`, `AsyncFunctionDef`, `ClassDef`, simple `Assign` targets) whose names don't start with `_`.
5. Limit to 200 symbols per expansion. If exceeded: `limitations.append("star_import_too_large:{star_module}")`.

**Threat mitigations (T-12-02-02):** `_MAX_STAR_SYMBOLS = 200`.

### `_link_symbols(source_files, root, module_map, reexport_map, symbol_index, all_bindings, workspace) -> list[SymbolLink]`

Produces a `SymbolLink` for every import binding in every file.

**Resolution priority (critical design decision):**
1. **Reexport-first:** Try `_resolve_via_reexport(source_module, original_symbol, reexport_map)`. If found: `SymbolLink(confidence="medium", is_external=False)`.
2. **Direct:** Try `reverse_module_map.get(source_module)`. If found: `SymbolLink(confidence="high", is_external=False)`.
3. **External:** `SymbolLink(is_external=True, source_path=None, confidence="high")`.

`source_line` is fetched from `symbol_index` via `_find_symbol_line`.

### `_find_symbol_line(symbol_index, rel_path, symbol_name) -> int | None`

Static helper: iterates `symbol_index.get(rel_path, [])` and returns `sr.line` when `sr.symbol == symbol_name`.

## Extensions to Existing Methods

### `_build_python_module_map` — namespace package support + `limitations` param

Added `limitations: list[str] | None = None` parameter (fulfills the `analyze()` call with `limitations=limitations`).

**Namespace package detection:**
- Collects all directories that contain `.py` files (`dirs_with_py`)
- Collects all directories that have an `__init__.py` (`dirs_with_init`)
- For each dir in `dirs_with_py` but not in `dirs_with_init`: `limitations.append("namespace_package:{dir}")`
- Files from namespace package directories are still added to `module_map` normally (same dotted-path logic)

**Threat mitigation (T-12-02-03):** Namespace packages are flagged in limitations; consumers can filter by this signal.

### `_build_import_bindings` — star import expansion

New keyword-only parameters: `root: Path | None = None`, `symbol_index: dict | None = None`.

When `from foo import *` is encountered and both `root` and `symbol_index` are provided:
- Resolve the source module (relative or absolute)
- Call `_resolve_star_imports(root, star_module, module_map, symbol_index, limitations)`
- Add each expanded name to `bindings` as `bindings[name] = (star_module, name)`

## language_coverage["python"] = "full"

Confirmed present in `analyze()`:
```python
if source_files:
    lang_coverage["python"] = "full"
```

This is set unconditionally when Python files are analyzed, reflecting that the full advanced resolution pipeline (reexport chains, star imports, namespace packages, SymbolLink consolidation) is active.

## Reexport Resolution Design (Key Decision)

The critical insight: `_link_symbols` must check `reexport_map` **before** `reverse_module_map`.

Without this priority, `from pkg import User` (where `pkg` is `pkg/__init__.py`) resolves to `pkg/__init__.py` (direct, high confidence) instead of following the reexport chain to `pkg/models.py` (the actual defining file). The reexport-first approach correctly delivers `source_path="pkg/models.py"` with `confidence="medium"`.

## Star Import Expansion Strategy

Star imports are expanded at `_build_import_bindings` time and stored as regular bindings. This means:
- The existing Pass 2 call resolution loop works unchanged for star-imported names
- `_link_symbols` produces `SymbolLink` entries for each expanded name
- The `__all__` check is strict: requires `ast.List` of `ast.Constant` strings at module level; dynamic `__all__` falls back to public name scan

## Namespace Package Support

Namespace packages (directories without `__init__.py`) have their files included in `module_map` using the same dotted-path derivation as regular packages. They are flagged in `limitations` but not otherwise penalized — their `SymbolRecord` entries are indexed normally in Pass 1.

## Test Metrics

**tests/test_semantic_import_resolution.py (SEM-IR-01..06):** 6 passing

| Test | Scenario |
|------|----------|
| SEM-IR-01 | `from pkg import User` resolved to `pkg/models.py` via reexport chain |
| SEM-IR-02 | `from utils import *` with `__all__` — only listed names, not `_private` |
| SEM-IR-03 | `from utils import *` without `__all__` — only public names, not `_priv` |
| SEM-IR-04 | Namespace package (`namespace_pkg/` without `__init__.py`) — `func` indexed |
| SEM-IR-05 | `import utils; utils.process(data)` — `CallRecord` with `confidence="medium"` |
| SEM-IR-06 | 3-level reexport chain — completes without crash; chain limit handled |

**Regression — tests/test_semantic_analyzer_python.py (SEM-PY-01..05):** 5 passing, 0 regressions

**tests/test_semantic_schema.py:** 6 passing, 0 regressions

**Total:** 17 semantic tests passing, 0 new failures

## Deviations from Plan

### Auto-fix: Resolution priority in _link_symbols

**Rule 1 (Bug fix).** Found during SEM-IR-01 debugging: the initial implementation tried direct module_map resolution first, resulting in `source_path="pkg/__init__.py"` instead of `"pkg/models.py"`.

**Fix:** Swapped resolution order — reexport_map is checked first, direct module_map second. This is the correct behavior per the plan's stated truth: "SemanticAnalyzer resuelve 'from pkg import Symbol' cuando pkg/__init__.py re-exporta Symbol desde un submodulo."

**Files modified:** `src/sourcecode/semantic_analyzer.py` — `_link_symbols` method
**Commit:** 1434198

### _resolve_star_imports accepts `limitations` as explicit parameter

The plan's interface signature did not include `limitations` as a parameter to `_resolve_star_imports`. Added it as a regular parameter (not keyword-only) since the method needs to append to limitations for external modules and oversized expansions. The `analyze()` call chain passes `limitations` down correctly.

## Pre-existing Test Failures (Not Caused by This Plan)

Multiple integration and workspace tests fail on this Windows environment due to path separator mismatches (`'apps/web'` vs `'apps\\web'`). Confirmed pre-existing via `git stash` verification. Not caused by plan 12-02 changes.

## Known Stubs

None — all implemented functionality is wired to tests and produces correct output.

## Threat Flags

None — no new network endpoints, auth paths, file access patterns, or schema changes at trust boundaries beyond what the plan's threat model already covers.

## Commits

| Hash | Type | Description |
|------|------|-------------|
| 1434198 | feat | Python import resolution avanzada + symbol linker + namespace packages |

## Self-Check: PASSED

- `src/sourcecode/semantic_analyzer.py` exists and contains `_build_reexport_map`, `_resolve_star_imports`, `_link_symbols`, `_resolve_via_reexport`
- Commit `1434198` exists: confirmed via `git rev-parse --short HEAD`
- 17 semantic tests passing: `tests/test_semantic_import_resolution.py` (6), `tests/test_semantic_analyzer_python.py` (5), `tests/test_semantic_schema.py` (6)

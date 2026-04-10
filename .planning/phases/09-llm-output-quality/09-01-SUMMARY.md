---
phase: 09-llm-output-quality
plan: "01"
subsystem: schema+summarizer+cli
tags: [schema, summarizer, llm-output, file-paths, project-summary, key-dependencies]
dependency_graph:
  requires: []
  provides: [SourceMap.file_paths, SourceMap.project_summary, SourceMap.key_dependencies, ProjectSummarizer]
  affects: [src/sourcecode/schema.py, src/sourcecode/cli.py]
tech_stack:
  added: [src/sourcecode/summarizer.py]
  patterns: [deterministic-template, lazy-import-in-main, tdd-red-green]
key_files:
  created:
    - src/sourcecode/summarizer.py
    - tests/test_summarizer.py
  modified:
    - src/sourcecode/schema.py
    - src/sourcecode/cli.py
    - tests/test_schema.py
decisions:
  - "Lazy imports for flatten_file_tree and ProjectSummarizer placed inside main() body following existing cli.py pattern"
  - "file_paths uses replace('\\\\', '/') as defense-in-depth even though flatten_file_tree already uses forward-slashes"
  - "key_dependencies only populated when dependency_analyzer is not None (i.e., --dependencies flag active)"
  - "_dep_sort_key defined as nested function inside the if-block to stay close to its usage"
metrics:
  duration_minutes: 15
  completed_date: "2026-04-10"
  tasks_completed: 3
  files_changed: 5
---

# Phase 9 Plan 01: Schema base + ProjectSummarizer + file_paths/key_dependencies wiring Summary

**One-liner:** Three new backward-compatible SourceMap fields (file_paths, project_summary, key_dependencies) with deterministic NL template engine and cli.py wiring.

## What Was Built

### Fields Added to SourceMap (src/sourcecode/schema.py)

Three fields appended after `doc_summary`, preserving historical field order:

```python
# Phase 9: LLM Output Quality
file_paths: list[str] = field(default_factory=list)
project_summary: Optional[str] = None
key_dependencies: list[DependencyRecord] = field(default_factory=list)
```

All three use backward-compatible defaults — existing SourceMap instantiations without the new fields continue to work unchanged.

### ProjectSummarizer Templates (src/sourcecode/summarizer.py)

`ProjectSummarizer.generate(sm: SourceMap) -> str` applies the following template branches:

| project_type | Template |
|---|---|
| (no stacks) | `"Proyecto sin stack detectado."` |
| monorepo | `"Monorepo con N workspaces en Stack1, Stack2. Entry points: .... N dependencias (eco)."` |
| api | `"API en Python (FastAPI). Entry points: .... N dependencias (python)."` |
| cli | `"CLI en Python. Entry points: src/cli.py."` |
| library | `"Libreria en Python."` |
| webapp | `"Aplicacion web en Python."` |
| unknown/other | `"Proyecto en Python."` or `"StackName en Stack."` |

- Frameworks capped at 3 in the parenthetical
- Entry points capped at 3 in the Entry points clause
- `dependency_summary=None` produces no dep clause; `total_count=0` produces "Sin dependencias detectadas."
- Outer `try/except` in `generate()` ensures no exception ever propagates

### Wiring in cli.py — Exact Insertion Point

Inserted between the `sm = SourceMap(...)` constructor (line 371) and `# 4. Serializar` (line 398), at lines 373-396:

```python
# Phase 9: LLM Output Quality — poblar campos derivados
from sourcecode.tree_utils import flatten_file_tree
from sourcecode.summarizer import ProjectSummarizer

# LQN-01: lista plana de paths del file_tree con separador forward-slash
sm.file_paths = [
    p.replace("\\", "/") for p in flatten_file_tree(sm.file_tree)
]

# LQN-05: top-15 dependencias directas de manifest/lockfile
if dependency_analyzer is not None:
    primary_ecosystem = sm.stacks[0].stack if sm.stacks else ""
    direct_deps = [
        d for d in sm.dependencies
        if d.scope != "transitive" and d.source in {"manifest", "lockfile"}
    ]

    def _dep_sort_key(d: Any) -> tuple[int, str]:
        return (0 if d.ecosystem == primary_ecosystem else 1, d.name.lower())

    sm.key_dependencies = sorted(direct_deps, key=_dep_sort_key)[:15]

# LQN-02: resumen NL deterministico
sm.project_summary = ProjectSummarizer().generate(sm)
```

## Test Metrics

| Test file | Tests added | Tests total | Status |
|---|---|---|---|
| tests/test_schema.py | 6 new | 18 total | GREEN |
| tests/test_summarizer.py | 8 new | 8 total | GREEN |
| tests/test_cli.py | 0 new | 21 total | GREEN |
| Full suite (excluding pre-existing failures) | — | 86 passed | GREEN |

**Pre-existing failures (not introduced by this plan):**
- `tests/test_integration_dependencies.py::test_cli_dependencies_preserve_workspace_context_in_monorepo` — Windows backslash vs forward-slash in workspace paths. Confirmed pre-existing on git stash verification.

## Commits

| Task | Hash | Message |
|---|---|---|
| Task 1 | 1c34445 | feat(09-01): extend SourceMap with file_paths, project_summary, key_dependencies |
| Task 2 | 724efd2 | feat(09-01): add ProjectSummarizer with deterministic NL template engine |
| Task 3 | dd0ce46 | feat(09-01): wire cli.py to populate file_paths, key_dependencies, project_summary |

## Deviations from Plan

None — plan executed exactly as written.

## Known Stubs

None — all three fields are fully wired with real data:
- `file_paths` derives from `flatten_file_tree(sm.file_tree)` (always populated after scan)
- `project_summary` is generated by `ProjectSummarizer().generate(sm)` (always a non-empty string)
- `key_dependencies` is populated from `sm.dependencies` when `--dependencies` is active; empty list otherwise (by design, not a stub)

## Threat Surface Scan

No new security surface introduced beyond the threat model in the plan:
- `file_paths` derives from the same `file_tree` already filtered by `filter_sensitive_files()` and `SecretRedactor` — no additional path disclosure.
- `project_summary` template uses only structural metadata (type labels, stack names, entry point paths, dep counts) — no file content or secret values can appear.
- `key_dependencies` reads from `sm.dependencies` already parsed and sanitized by `DependencyAnalyzer`.

## Self-Check: PASSED

- [x] `src/sourcecode/summarizer.py` exists
- [x] `tests/test_summarizer.py` exists
- [x] `src/sourcecode/schema.py` has `file_paths`, `project_summary`, `key_dependencies`
- [x] `src/sourcecode/cli.py` contains `ProjectSummarizer().generate(sm)`
- [x] Commits 1c34445, 724efd2, dd0ce46 all exist
- [x] 86 tests pass (1 pre-existing failure unrelated to this plan)

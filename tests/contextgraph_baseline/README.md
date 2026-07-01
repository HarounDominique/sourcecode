# ContextGraph Phase 0 — baseline validation harness

Pure validation infrastructure for the ContextGraph migration
(`.planning/DESIGN-context-graph.md`). **Zero functional change** to the engine —
it drives the CLI as a black box via subprocess and imports nothing from
`sourcecode`.

## What it does

- **Regression oracle** — for every (repo × command) cell, canonicalizes the JSON
  output (sorted keys, wall-clock volatiles scrubbed, repo paths normalized) and
  records `sha256 + bytes + wall_ms + structural stats` in a light, git-committed
  `baseline/index.json`. Full canonical outputs go to `baseline/artifacts/`
  (gitignored — multi-MB, sourced from external repos).
- **Byte-diff comparison** — re-runs the matrix and flags any cell whose canonical
  hash changed. On drift it writes a `*.NEW.json` artifact next to the baseline for
  human diffing.
- **Convergence metrics** — static count of IR consumers vs modules that still parse
  Java directly, plus remaining parse entry points. Dedup reduction is tracked as a
  first-class phase metric alongside performance.

## Command matrix

Path-only, deterministic, JSON-emitting engine commands (global `--no-cache`):
`repo-ir`, `endpoints`, `spring-audit`, `migrate-check`, `validation --format json`,
`export --c4 / --module-graph / --integrations / --by-directory`.

## Field-test repos

External, under `/Users/user/Documents/workspace/testing/`. Each repo's `git HEAD`
is recorded in `index.json` so the baseline is reproducible. Absent repos are
skipped, not failed.

## Usage

```bash
PY=/opt/homebrew/bin/python3          # system python (.venv SIGKILLs — see memory)
$PY -m tests.contextgraph_baseline.harness convergence   # architecture metrics
$PY -m tests.contextgraph_baseline.harness capture       # build/refresh oracle
$PY -m tests.contextgraph_baseline.harness compare        # byte-diff vs oracle
```

`tests/test_contextgraph_baseline.py` runs `compare` as a pytest gate — it skips
when the oracle or repos are absent, so it never breaks CI.

## Migration protocol

1. Before a phase: `capture` (oracle reflects current, known-good output).
2. Implement the phase.
3. After: `compare` — **every drifted cell must be explained** (deliberate
   improvement) or fixed (regression) before the phase is accepted.
4. Record perf + convergence deltas in the phase report.

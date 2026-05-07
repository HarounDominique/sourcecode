# sourcecode

**Deterministic codebase context for AI coding agents.**

![Version](https://img.shields.io/badge/version-1.0.0-blue)
![Status](https://img.shields.io/badge/status-MVP-orange)
![Python](https://img.shields.io/badge/python-3.10%2B-green)

---

## What is it?

`sourcecode` analyzes a repository and produces a structured context map (JSON or YAML) designed to be consumed by AI agents or language models. It solves the "stuff the whole repo into the prompt" problem by instead producing a deterministic extract: entry points, dependencies, stacks, inline annotations, environment variables, and git activity. It is an MVP tool under active evolution — the semantic analysis and module graph features work but have known limitations that are explicitly documented below.

---

## Installation

**Prerequisites:** Python 3.10+

```bash
pip install sourcecode
# or with pipx for isolation:
pipx install sourcecode
```

Verify installation:

```bash
sourcecode version
# sourcecode 1.0.0
```

---

## Quickstart

The most useful command for integrating `sourcecode` into an AI agent:

```bash
sourcecode --agent
```

It produces a structured JSON with the essential sections (no noise, no file tree), ready to paste into an LLM context:

```json
{
  "project": {
    "type": "fullstack",
    "summary": "Full-stack project in Nodejs, mvc, 4075 source files. Domains: saint-client, saint-server, saint-portal, saint-ng-recibo. 3300 dependencies (java, nodejs).",
    "primary_stack": "nodejs",
    "secondary_stacks": ["java"]
  },
  "entry_points": [
    {
      "path": "saint-server/src/main/java/m3informatica/saint/SaintServerApplication.java",
      "stack": "java",
      "kind": "application",
      "confidence": "high"
    },
    {
      "path": "saint-client/src/main.ts",
      "stack": "nodejs",
      "kind": "entrypoint",
      "confidence": "high"
    }
  ],
  "runtime_packages": [ ... ],
  "dependencies": { ... },
  "env_map": { ... },
  "code_notes": [ ... ]
}
```

For large repositories where context matters, use `--compact` to reduce to ~600-800 tokens:

```bash
sourcecode --compact --copy
# Copies the summary to the clipboard. Ready to paste.
```

---

## Flags reference

### Global options

| Flag | Alias | Type | Default | Description | Status |
|------|-------|------|---------|-------------|--------|
| `--format` | `-f` | `json\|yaml` | `json` | Output format. YAML is more readable, JSON preferred in pipelines. | ✅ CORE |
| `--output` | `-o` | `PATH` | stdout | Writes output to a file instead of stdout. | ✅ CORE |
| `--compact` | | flag | off | ~600-800 token output: stacks, entry points, deps, gaps. No file tree. | ✅ CORE |
| `--agent` | | flag | off | JSON optimized for agents. Automatically enables `--dependencies`, `--env-map`, `--code-notes`. | ✅ CORE |
| `--dependencies` | | flag | off | Analyzes direct and transitive deps from manifests and lockfiles. | ✅ CORE |
| `--git-context` | `-g` | flag | off | Includes recent commits, change hotspots, uncommitted changes, contributors. | ✅ CORE |
| `--git-depth` | | `INT [1–100]` | `20` | Number of recent commits with `--git-context`. | ✅ CORE |
| `--git-days` | | `INT [1–3650]` | `90` | Window in days to detect hotspots with `--git-context`. | ✅ CORE |
| `--env-map` | | flag | off | Maps environment variables: key, type, category, files that reference them. | ✅ CORE |
| `--code-notes` | | flag | off | Extracts inline annotations: TODO, FIXME, HACK, BUG, DEPRECATED, NOTE, etc. | ✅ CORE |
| `--copy` | `-c` | flag | off | Copies output to the clipboard after successful execution. | ✅ CORE |
| `--depth` | | `INT [1–20]` | `4` | Maximum file tree traversal depth. Java/Maven requires ≥8. | ✅ CORE |
| `--mode` | | `contract\|standard\|raw` | `contract` | `contract`: minimal contracts per file. `standard`: full detail. `raw`: project level only. | ✅ CORE |
| `--tree` | | flag | off | Includes full `file_tree` and `file_paths` in the output. Increases size significantly. | ✅ CORE |
| `--changed-only` | | flag | off | Contract mode: only files modified in git (staged, unstaged, untracked). | ✅ CORE |
| `--rank-by` | | `relevance\|centrality\|git-churn` | `relevance` | File ranking strategy in contract mode. | ✅ CORE |
| `--semantics` | | flag | off | Cross-file symbol resolution, call graph with confidence levels, fan-in/fan-out hotspots. Slower. | 🧪 EXP |
| `--architecture` | | flag | off | Architectural layer inference (MVC/hexagonal/bounded contexts). Low confidence without `--semantics`. | 🧪 EXP |
| `--graph-modules` | | flag | off | Structural module graph: nodes (files/symbols) and edges (imports, calls, contains). | 🧪 EXP |
| `--graph-detail` | | `high\|medium\|full` | `high` | Module graph detail level. | 🧪 EXP |
| `--max-nodes` | | `INT [≥1]` | — | Maximum nodes in `--graph-modules`. Prevents huge graphs in large repos. | 🧪 EXP |
| `--graph-edges` | | `TEXT` | all | Edge types for `--graph-modules`, comma-separated: `imports,calls,contains`. | 🧪 EXP |
| `--docs` | | flag | off | Extracts docstrings, function signatures, and module comments. | 🧪 EXP |
| `--docs-depth` | | `module\|symbols\|full` | `symbols` | Docs extraction depth. `full` includes private symbols. | 🧪 EXP |
| `--symbol` | | `TEXT` | — | Contract mode: localized context for a specific symbol. Python, TS, JS only. **Does not support Java.** | 🧪 EXP |
| `--max-importers` | | `INT [1–10000]` | `50` | Limit on importer files returned by `--symbol`. | 🧪 EXP |
| `--full-metrics` | | flag | off | Per-file technical metrics: LOC, cyclomatic complexity, coverage. Aimed at CI, not at agents. | 🧪 EXP |
| `--emit-graph` | | flag | off | Contract mode: includes a compact dependency graph (nodes + edges) in the output. | 🚧 WIP |
| `--entrypoints-only` | | flag | off | Contract mode: only files with exports or entry points. Note: includes *all* files with exports. | 🚧 WIP |
| `--max-symbols` | | `INT [≥1]` | — | Limits total exported symbols in contract mode. Discards lower-ranked files. | 🚧 WIP |
| `--no-redact` | | flag | off | Disables automatic secret redaction. Output may contain sensitive values. | 🚧 WIP |
| `--trace-pipeline` | | flag | off | Diagnostic mode: includes a trace of each candidate and filtering decision. Debugging only. | 🚧 WIP |
| `--version` | `-v` | flag | — | Shows version and exits. | ✅ CORE |

---

## Subcommands

### `prepare-context TASK [PATH]`

Generates task-specific context for AI agents.

| Task | Description | Status |
|------|-------------|--------|
| `explain` | Architecture, entry points, key dependencies | ✅ CORE |
| `fix-bug` | Files prioritized by risk, inline annotations | ✅ CORE |
| `onboard` | Full context for new agents or developers | ✅ CORE |
| `delta` | Incremental context: only files changed in git | ✅ CORE |
| `refactor` | Structural problems, improvement opportunities | 🧪 EXP |
| `generate-tests` | Files without tests, coverage gap analysis | 🧪 EXP |
| `review-pr` | Changed files + architectural impact | 🧪 EXP |

| `prepare-context` flag | Type | Default | Description | Status |
|------------------------|------|---------|-------------|--------|
| `--since` | `TEXT` | — | Git ref for the `delta` task (e.g. `HEAD~3`, `main`). | ✅ CORE |
| `--llm-prompt` | flag | off | Appends a ready-to-use prompt at the end of the output. | ✅ CORE |
| `--dry-run` | flag | off | Shows what would be analyzed without running the analysis. | ✅ CORE |
| `--copy` / `-c` | flag | off | Copies output to the clipboard after execution. | ✅ CORE |
| `--task-help` | flag | off | Lists available tasks with descriptions and exits. | ✅ CORE |

### `config`

Shows current configuration (config path, telemetry status).

```bash
sourcecode config
# sourcecode 1.0.0
# Config:    ~/.config/sourcecode/config.json
# Telemetry: disabled
```

### `telemetry status|enable|disable`

Manages anonymous telemetry (opt-in by default).

```bash
sourcecode telemetry status    # View current status
sourcecode telemetry enable    # Enable
sourcecode telemetry disable   # Disable
```

---

## Advanced usage — CORE flags in detail

### `--agent`

Produces structured, noise-free JSON designed to be injected directly into an AI agent's context. Automatically enables `--dependencies`, `--env-map`, and `--code-notes`. Suppresses the file tree.

**Minimal example:**
```bash
sourcecode --agent
```

**Advanced example — specific project with git context:**
```bash
sourcecode /path/to/repo --agent --git-context --git-depth 10 --format json --output context.json
```

**Sample output:**
```json
{
  "project": {
    "type": "fullstack",
    "summary": "Full-stack project in Nodejs, mvc, 4075 source files.",
    "primary_stack": "nodejs",
    "secondary_stacks": ["java"]
  },
  "entry_points": [
    {
      "path": "src/main/java/com/example/Application.java",  // Spring Boot entrypoint
      "stack": "java",
      "kind": "application",
      "confidence": "high"
    }
  ],
  "dependencies": {
    "total_count": 3300,
    "direct": 241,
    "sources": ["lockfile", "manifest"]
  },
  "env_map": { ... },   // Detected environment variables with types and categories
  "code_notes": [ ... ] // TODOs, FIXMEs, HACKs with location and containing symbol
}
```

> 💡 **Tip for users:** Combine `--agent --copy` to copy the context directly to the clipboard and paste it into any LLM chat without intermediate steps.
>
> 🤖 **Tip for LLMs:** `--agent` intentionally omits the file tree. If the agent needs to navigate the full structure, add `--tree`. If it only needs a lightweight summary, use `--compact` instead.

---

### `--compact`

Condensed version of ~600-800 tokens: project type, stacks, production entry points, dependency summary, confidence summary, and analysis gaps. Omits file tree, raw dependency lists, docs, and module graph. Automatically enables `--dependencies`, `--env-map`, and `--code-notes`.

**Minimal example:**
```bash
sourcecode --compact
```

**Advanced example:**
```bash
sourcecode --compact --format yaml --copy
```

**Sample output (YAML, ~700 tokens):**
```yaml
project_type: fullstack
project_summary: >-
  Full-stack project in Nodejs, mvc, 4075 source files. Domains: saint-client,
  saint-server, saint-portal. 3300 dependencies (java, nodejs).
stacks:
  - stack: nodejs
    detection_method: manifest
    confidence: high
    frameworks: [Angular]
  - stack: java
    detection_method: manifest
    confidence: high
    frameworks: [Spring Boot]
entry_points:
  - path: src/main/java/m3informatica/saint/SaintServerApplication.java
    kind: application
    confidence: high
dependency_summary:
  total_count: 3300
  direct: 241
  sources: [lockfile, manifest]
confidence_summary:
  overall: medium
  limitations: [...]
```

> 💡 **Tip for users:** `--compact` is the most economical entry point for large projects. Use it for a first read before deciding whether you need `--agent` with the full context.
>
> 🤖 **Tip for LLMs:** When the agent receives `--compact`, the `confidence_summary.limitations` and `analysis_gaps` fields indicate exactly what information is missing — use them to decide whether to request a deeper analysis with additional flags.

---

### `--git-context`

Includes real git activity: recent commits, hotspots (files with the most commits in the day window), uncommitted changes, and contributors.

**Minimal example:**
```bash
sourcecode --git-context
```

**Advanced example — last 50 commits, 30-day window:**
```bash
sourcecode --agent --git-context --git-depth 50 --git-days 30
```

> 💡 **Tip for users:** `--git-context` is especially useful for `fix-bug` — hotspots indicate files with a high change rate that have historically had bugs.
>
> 🤖 **Tip for LLMs:** The `change_hotspots` field lists files ordered by commit frequency. Correlate with `code_notes` (TODOs/FIXMEs) to prioritize which files to review in debugging tasks.

---

### `--mode`

Controls the per-file detail level in the output.

| Mode | What it includes | When to use |
|------|------------------|-------------|
| `contract` (default) | Exports, signatures, deps per file. Minimal output. | Whenever the agent needs to navigate the API surface |
| `standard` | Full detail: imports, relevance scores, extraction method | In-depth analysis of a specific module |
| `raw` | Project-level analysis only: stacks, entry points, deps summary | When per-file detail is not needed |

**Example:**
```bash
sourcecode --mode raw --format yaml    # Project context only, no per-file contracts
sourcecode --mode standard             # Full detail, more verbose
```

---

### `prepare-context` — CORE tasks

#### `explain`

Generates a comprehensive summary of architecture, entry points, and key dependencies. Aimed at onboarding new agents or developers.

```bash
sourcecode prepare-context explain
sourcecode prepare-context explain /path/to/repo
```

**Sample output:**
```json
{
  "task": "explain",
  "goal": "Generate a comprehensive project summary for onboarding an LLM or developer.",
  "project_summary": "Full-stack project in Nodejs, mvc, 352 source files.",
  "confidence": "high",
  "relevant_files": [
    {
      "path": "saint-client/src/main.ts",
      "role": "entrypoint",
      "score": 1.0,
      "reason": "runtime entrypoint, workspace source root"
    }
  ]
}
```

#### `fix-bug`

Returns the files most likely to contain a bug, prioritized by risk (change frequency, uncommitted changes, inline annotations).

```bash
sourcecode prepare-context fix-bug
sourcecode prepare-context fix-bug . --llm-prompt
```

> 🤖 **Tip for LLMs:** The `reason` field of each `relevant_file` explains *why* it was selected (e.g., `"recent churn (29 commits), uncommitted changes"`). Use it to justify in the response to the user which files to inspect first.

#### `delta`

Incremental context: only files modified relative to a git ref. Much faster and lighter than a full analysis.

```bash
sourcecode prepare-context delta --since main
sourcecode prepare-context delta --since HEAD~5
sourcecode prepare-context delta --since origin/develop --copy
```

#### `onboard`

Full context for an agent or developer who is new to the project.

```bash
sourcecode prepare-context onboard --llm-prompt
```

> 🤖 **Tip for LLMs:** `--llm-prompt` appends a pre-built prompt at the end of the JSON that summarizes the project and suggests how to continue. Useful as the first message in a new Claude Code session or similar.

---

## Recommended combinations

| Goal | Flags | Example |
|------|-------|---------|
| AI agent context (general) | `--agent --copy` | `sourcecode --agent --copy` |
| Minimal, token-efficient context | `--compact --format yaml` | `sourcecode --compact --format yaml` |
| Debugging: which files to touch? | `prepare-context fix-bug --llm-prompt` | `sourcecode prepare-context fix-bug --llm-prompt` |
| PR review: only changed files | `prepare-context delta --since main` | `sourcecode prepare-context delta --since main --copy` |
| Repo snapshot for CI | `--agent --output context.json` | `sourcecode --agent --output context.json` |
| Analysis with git history | `--agent --git-context --git-depth 50` | `sourcecode --agent --git-context --git-depth 50` |
| Deep Java/Maven repo | `--agent --depth 10` | `sourcecode --agent --depth 10` |
| Onboarding a new developer | `prepare-context onboard --llm-prompt` | `sourcecode prepare-context onboard --llm-prompt` |
| Human-readable YAML | `--compact --format yaml --output summary.yml` | `sourcecode --compact --format yaml --output summary.yml` |
| Project public API only | `--entrypoints-only --mode contract` | `sourcecode --entrypoints-only --mode contract` |

---

## Experimental features 🧪

These features are available and produce output in nominal cases, but have documented limitations.

### `--semantics`

Cross-file symbol resolution, call graph with confidence levels, fan-in/fan-out hotspots. Slower than the default analysis.

**Known limitation:** Confidence degrades in code with dynamic dispatch, decorators, and generated code. Do not use as a source of truth in projects with heavy metaprogramming.

```bash
sourcecode --semantics --architecture  # Combine for greater architectural accuracy
```

### `--architecture`

Functional layer inference (MVC, hexagonal, bounded contexts). When based solely on directory names, confidence is low.

**Known limitation:** `"confidence: low"` is the most frequent result in repos without canonical directory structure. Combine with `--semantics` for more reliable results.

### `--graph-modules`

Module graph with nodes and edges (`imports`, `calls`, `contains`). Useful for understanding coupling.

```bash
sourcecode --graph-modules --graph-detail medium --max-nodes 50
sourcecode --graph-modules --graph-edges imports,calls
```

**Known limitation:** In large repos, `--graph-detail full` can produce very voluminous outputs. Use `--max-nodes` to bound it.

### `--symbol`

Extracts localized context for a specific symbol: file where it is defined + all importers.

```bash
sourcecode --symbol MyComponent
sourcecode --symbol UserService --max-importers 100
```

> ⚠️ **Important limitation:** Only works with Python, TypeScript, and JavaScript. In Java/JVM repos it produces a warning and empty output:
> ```
> [warning] --symbol 'ClassName' matched 0 files. Per-file AST extraction is not available for Java/JVM repos
> ```

### `--full-metrics`

Per-file technical metrics: LOC, symbol counts, cyclomatic complexity, test coverage.

**Note:** Not included in `--agent` by design — aimed at CI pipelines and code review tools, not as primary agent context.

```bash
sourcecode --full-metrics --output metrics.json
```

### `prepare-context refactor / generate-tests / review-pr`

They work in happy paths but file ranking may be less precise than `fix-bug` or `explain`.

```bash
sourcecode prepare-context generate-tests
sourcecode prepare-context review-pr --llm-prompt
```

---

## Roadmap / Don't use yet 🚧

> ⚠️ The following features exist in the interface but are not ready for production use or agent pipelines.

- **`--trace-pipeline`** — Internal diagnostic mode. Includes the full trace of each filtering decision. Only for debugging `sourcecode` itself. Do not use in normal pipelines.
- **`--no-redact`** — Disables automatic redaction of secrets (API keys, tokens, passwords). The output may contain sensitive values. Do not use if the output goes to an external service.
- **`--emit-graph`** — Compact dependency graph in contract mode. Unstable output.
- **`--entrypoints-only`** — Filters by entry points, but the current definition includes all files with exports (not just runtime entry points). Behavior may change.
- **`--max-symbols`** — Limits total exported symbols. The discard criterion (lower-ranked files) may change.

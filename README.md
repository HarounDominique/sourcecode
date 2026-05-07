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
    "summary": "Full-stack project in Nodejs, mvc, 4075 source files. Domains: atlas-client, atlas-server, atlas-hub, atlas-reports. 3300 dependencies (java, nodejs).",
    "primary_stack": "nodejs",
    "secondary_stacks": ["java"]
  },
  "entry_points": [
    {
      "path": "atlas-server/src/main/java/com/example/atlas/AtlasServerApplication.java",
      "stack": "java",
      "kind": "application",
      "confidence": "high"
    },
    {
      "path": "atlas-client/src/main.ts",
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

...
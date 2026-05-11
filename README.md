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

The primary command — a high-signal, low-noise summary for Java/Spring codebases:

```bash
sourcecode --compact
# ~600-800 tokens: stack, entry points, dependencies, risk flags, confidence.

sourcecode --compact --git-context
# Adds git hotspots and uncommitted file count.

sourcecode --compact --copy
# Copies the result to clipboard.
```

Example output for a Spring Boot project:

```json
{
  "project_type": "api",
  "stacks": [{ "stack": "java", "frameworks": ["Spring Boot", "MyBatis"] }],
  "entry_points": {
    "bootstrap": ["src/main/java/io/spring/RealWorldApplication.java"],
    "security": ["src/main/java/io/spring/api/security/WebSecurityConfig.java"],
    "controllers": { "count": 8, "sample": [...] }
  },
  "key_dependencies": [
    { "name": "org.mybatis.spring.boot:mybatis-spring-boot-starter",
      "version": "2.2.2", "risk_flags": ["spring-boot-2.x-eol"] }
  ],
  "language_version": "11",
  "deployment": { "spring_boot_version": "2.6.3" },
  "mybatis": { "mapper_interfaces": 4, "xml_files": 4 },
  "confidence_summary": { "overall": "high" }
}
```

For full structured output with per-file contracts and signals, use `--agent`:

```bash
sourcecode --agent
```

---

## Flags reference

### Global options

| Flag | Alias | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--compact` | | flag | off | **Recommended.** ~600-800 token summary: stack, entry points, deps, risk flags, confidence. |
| `--git-context` | `-g` | flag | off | Adds git hotspots (top changed files), branch, uncommitted file count. Use with `--compact`. |
| `--agent` | | flag | off | Full structured JSON for AI agents. Auto-enables dependency, env-var, and code-notes analysis. |
| `--changed-only` | | flag | off | Limit output to git-modified files (staged, unstaged, untracked). |
| `--depth` | | `INT [1–20]` | `4` | File tree traversal depth. Java projects auto-adjust to 12. |
| `--format` | `-f` | `json\|yaml` | `json` | Output format. JSON preferred in pipelines. |
| `--output` | `-o` | `PATH` | stdout | Write output to a file instead of stdout. |
| `--copy` | `-c` | flag | off | Copy output to clipboard after a successful run. |
| `--no-redact` | | flag | off | Disable automatic secret redaction. Output may contain sensitive values. |
| `--version` | `-v` | flag | — | Show version and exit. |

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

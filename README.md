# sourcecode

**Compressed AI-ready context for Java/Spring enterprise codebases.**

![Version](https://img.shields.io/badge/version-1.20.0-blue)
![Python](https://img.shields.io/badge/python-3.10%2B-green)

---

## What is it?

`sourcecode` analyzes a repository and produces structured JSON or YAML designed to be fed directly to AI agents or language models. It solves the "stuff the whole repo into the prompt" problem by extracting a deterministic, high-signal summary: stack detection, entry points, dependencies, git hotspots, inline annotations, and confidence metadata.

Optimized for Java/Spring Boot monorepos. Works on any codebase.

---

## Installation

### Homebrew (macOS / Linux)

```bash
brew tap haroundominique/sourcecode
brew install sourcecode
```

### pip / pipx

```bash
pip install sourcecode
# or with isolation:
pipx install sourcecode
```

### Verify

```bash
sourcecode version
# sourcecode 1.20.0
```

---

## Quickstart

```bash
# High-signal summary (~600-800 tokens) — recommended starting point
sourcecode --compact

# Add git hotspots and uncommitted file count
sourcecode --compact --git-context

# Analyze a specific path
sourcecode /path/to/repo --compact

# Copy result to clipboard
sourcecode --compact --copy

# Structured output for AI agents (identity, entry points, dependencies, confidence)
sourcecode --agent

# Only process git-modified files (forces compact output)
sourcecode --changed-only
```

Example output for a Spring Boot project (`--compact`):

```json
{
  "project_type": "api",
  "stacks": [{ "stack": "java", "detection_method": "manifest", "confidence": "high",
               "primary": true, "frameworks": ["Spring Boot", "MyBatis"] }],
  "entry_points": {
    "bootstrap": ["src/main/java/io/spring/RealWorldApplication.java"],
    "security":  ["src/main/java/io/spring/api/security/WebSecurityConfig.java"],
    "controllers": { "count": 8, "sample": ["src/main/java/io/spring/api/ArticleApi.java"] }
  },
  "key_dependencies": [
    { "name": "org.mybatis.spring.boot:mybatis-spring-boot-starter",
      "version": "2.2.2", "risk_flags": ["spring-boot-2.x-eol"] }
  ],
  "language_version": "11",
  "deployment": { "spring_boot_version": "2.6.3", "packaging": "jar" },
  "mybatis": { "mapper_interfaces": 4, "xml_files": 4 },
  "confidence_summary": { "overall": "high", "stack": "high", "entry_points": "high" }
}
```

---

## Flags reference

| Flag | Alias | Default | Description |
|------|-------|---------|-------------|
| `--compact` | | off | **Recommended.** ~600-800 token summary: stack, entry points, dependencies, risk flags, confidence, gaps. Optimized for agent context windows. |
| `--agent` | | off | Structured noise-free JSON for AI agents: identity, entry points, dependencies, confidence, gaps. Auto-enables dependency, env-var, and code-notes analysis. |
| `--git-context` | `-g` | off | Include git activity: recent commits, change hotspots, and uncommitted changes. |
| `--changed-only` | | off | Limit output to git-modified files (staged, unstaged, untracked). Forces compact output. |
| `--depth` | | `4` | File tree traversal depth (1–20). Java/Maven projects auto-adjust to 12. |
| `--format` | `-f` | `json` | Output format: `json` or `yaml`. |
| `--output` | `-o` | stdout | Write output to a file instead of stdout. |
| `--copy` | `-c` | off | Copy output to clipboard after a successful run. No-op when `--output` is set or clipboard is unavailable. |
| `--no-redact` | | off | Disable automatic secret redaction. Output may contain sensitive values. |
| `--version` | `-v` | — | Show version and exit. |

---

## `prepare-context` — task-specific context

Generates a focused context bundle for a specific AI coding task. More targeted than `--compact`: each task re-ranks files according to its own signal priorities.

```bash
sourcecode prepare-context TASK [PATH] [OPTIONS]
```

### Tasks

| Task | What it surfaces | Primary use |
|------|-----------------|-------------|
| `explain` | Architecture, entry points, key dependencies | Onboarding an LLM to a new project |
| `onboard` | Full structural context: entry points, architecture, key files, dependencies | New developer or agent joining the codebase |
| `fix-bug` | Files ranked by risk (annotations, churn, uncommitted changes), suspected areas | Debugging session |
| `refactor` | Structural problems, improvement opportunities, high-annotation files | Code quality review |
| `generate-tests` | Source files without test pairs, coverage gap analysis | Writing missing tests |
| `review-pr` | Uncommitted/changed files + architectural impact | Pre-merge review |
| `delta` | Only files changed in a git range (`--since`), affected entry points | Incremental CI context |

### Options

| Option | Description |
|--------|-------------|
| `--since REF` | Git ref for `delta` task (e.g. `HEAD~3`, `main`, `v1.2.0`). Required for `delta`; ignored for other tasks. |
| `--llm-prompt` | Append a ready-to-use LLM prompt to the output. |
| `--dry-run` | Show what would be analyzed without running it. |
| `--copy` / `-c` | Copy output to clipboard after a successful run. |
| `--task-help` | List all tasks with descriptions and exit. |

### Examples

```bash
# Explain the current repo
sourcecode prepare-context explain

# Analyze a specific repo path
sourcecode prepare-context explain /path/to/repo

# Focus on bug-prone files
sourcecode prepare-context fix-bug

# Incremental context: files changed since branch diverged from main
sourcecode prepare-context delta . --since main

# Onboard with a ready-to-paste LLM prompt
sourcecode prepare-context onboard --llm-prompt

# List all tasks
sourcecode prepare-context --task-help
```

---

## Output schema

All outputs include a `confidence_summary` block with `overall`, `stack`, and `entry_points` confidence levels (`high` / `medium` / `low`), plus an `analysis_gaps` list describing what could not be analyzed and why.

### Java/Spring-specific fields

When a Java manifest (`pom.xml` or `build.gradle`) is detected, the output includes additional fields:

| Field | Description |
|-------|-------------|
| `language_version` | Java version from `maven.compiler.source` or equivalent |
| `deployment.spring_boot_version` | Spring Boot version |
| `deployment.packaging` | `jar` or `war` |
| `deployment.app_server_hint` | `weblogic`, `wildfly`, etc. (when detectable) |
| `security_surface.resource_names` | Values of `@M3FiltroSeguridad(nombreRecurso=...)` annotations across all controllers |
| `mybatis` | Mapper interface / XML file pairing summary |
| `transactional_boundaries` | Classes annotated with `@Transactional` |
| `deployment_risks` | Static risk flags: `spring-boot-2.x-eol`, `legacy-java-runtime`, `legacy-app-server-deployment` |

---

## Telemetry

Anonymous, opt-in telemetry collects: version, OS, commands used, flags, duration, repo size range, and errors. No source code, paths, secrets, or output content is ever collected.

```bash
sourcecode telemetry status    # current setting
sourcecode telemetry enable    # opt in
sourcecode telemetry disable   # opt out (permanent)
```

Alternatively, set the environment variable:

```bash
export SOURCECODE_TELEMETRY=0
```

---

## Configuration

```bash
sourcecode config    # show version, config file path, telemetry status
```

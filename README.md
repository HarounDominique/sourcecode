<!-- generated-by: gsd-doc-writer -->
# sourcecode

`sourcecode` generates a structured project context map so an agent can quickly understand a repository's stack, entry points, and overall shape. Designed for injection into AI development agents as initial session context.

## Installation

```bash
pip install sourcecode
```

Requires Python `3.9+`.

## Quick Start

Analyze the current directory as JSON:

```bash
sourcecode .
```

Generate a compact view for prompts or handoff (~500-700 tokens):

```bash
sourcecode --compact .
```

Analyze another directory and write YAML to a file:

```bash
sourcecode --format yaml --output sourcecode.yaml /path/to/project
```

Include direct dependencies, exact versions, and transitive dependencies when compatible lockfiles are available:

```bash
sourcecode . --dependencies
```

Include an internal module graph with imports and structural relations:

```bash
sourcecode . --graph-modules
```

Extract docstrings, signatures, and comments from Python and JS/TS modules:

```bash
sourcecode . --docs
```

Control how deep the documentation extraction goes:

```bash
sourcecode . --docs --docs-depth module    # module-level docs only
sourcecode . --docs --docs-depth symbols   # modules + functions/classes (default)
sourcecode . --docs --docs-depth full      # all symbols including methods
```

Show the version:

```bash
sourcecode --version
```

## What It Detects

- Stacks: Node.js, Python, Go, Rust, Java (Maven `pom.xml` and Gradle `build.gradle`/`build.gradle.kts`), PHP, Ruby, and Dart.
- Frameworks associated with each stack when enough signals are present.
- `project_type`: `webapp`, `api`, `library`, `cli`, `fullstack`, `monorepo`, or `unknown`.
- Relevant `entry_points`, such as `main.py`, `cmd/api/main.go`, or `app/page.tsx`.
- Workspace roots in multi-stack or monorepo repositories.

## CLI Options

| Option | Default | Description |
|--------|---------|-------------|
| `PATH` | `.` | Directory to analyze. |
| `--format json\|yaml` | `json` | Output format. |
| `--output PATH` | stdout | Write to a file instead of stdout. |
| `--compact` | off | Reduced output (~500-700 tokens): `schema_version`, `project_type`, `project_summary`, `architecture_summary`, `stacks`, `entry_points`, `file_tree_depth1`, and `dependency_summary` when available. |
| `--dependencies` | off | Include direct dependencies, resolved versions, and transitive relationships when lockfiles make that possible. Also populates `key_dependencies`. |
| `--graph-modules` | off | Include a structural module graph with imports and simple relations. |
| `--graph-detail high\|medium\|full` | `high` | Graph detail level: summarized (high), balanced (medium), or full-fidelity (full). |
| `--max-nodes INTEGER` | none | Cap graph size in `high` and `medium` modes. Min: 1. |
| `--graph-edges imports,calls,contains,extends` | none | Override the default edge kinds for the selected detail level. |
| `--docs` | off | Include extracted documentation: docstrings, signatures, and comments from Python and JS/TS modules and symbols. |
| `--docs-depth module\|symbols\|full` | `symbols` | Documentation extraction depth: module-level only, modules and top-level symbols (functions/classes), or all symbols including methods. |
| `--full-metrics` | off | Include code quality metrics: LOC, symbols, complexity, tests, and coverage per file. |
| `--semantics` | off | Include semantic call graph, cross-file symbol linking, and advanced import resolution. |
| `--architecture` | off | Architectural inference: groups files into functional domains, detects layer patterns (MVC, layered, hexagonal, fullstack), and infers approximate bounded contexts. Optionally uses `--graph-modules` for more precise bounded context inference. |
| `--git-context` / `-g` | off | Include git context: recent commits, most-changed files (hotspots), uncommitted changes, contributors, and a natural-language summary. |
| `--git-depth INTEGER` | `20` | Number of recent commits to include with `--git-context`. Range: 1–100. |
| `--git-days INTEGER` | `90` | Time window in days for hotspot detection with `--git-context`. Range: 1–3650. |
| `--env-map` | off | Map all environment variables referenced in source code: key name, required/optional status, inferred type (`string`, `int`, `bool`, `url`, `path`, `enum`), functional category (`database`, `auth`, `cache`, `storage`, `service`, `observability`, `feature_flag`, `server`, `general`), default value when present, and source file locations. Supplements with descriptions from `.env.example`, `.env.sample`, and similar reference files. |
| `--code-notes` | off | Extract inline code annotations — `TODO`, `FIXME`, `HACK`, `NOTE`, `DEPRECATED`, `WARNING`, `XXX`, `BUG`, `OPTIMIZE` — with file path, line number, annotation text, and the nearest enclosing function or class. Also detects Architecture Decision Records (ADRs) in `docs/decisions/`, `docs/adr/`, `adr/`, and similar directories, extracting title, status, and summary. |
| `--depth INTEGER` | `4` | Maximum file tree depth. Range: 1–20. |
| `--no-tree` | off | Suppress `file_tree`, `file_paths` (and `file_tree_depth1` in `--compact`) from the output. Ideal with `--dependencies` on large projects to get dependency data without a multi-thousand-line file tree. |
| `--agent` | off | Agent mode: auto-selects `--compact --dependencies --env-map --code-notes` and adds `--no-tree` automatically for Java/Gradle projects or large repositories. Prints selected flags to stderr. Can be combined with other flags (e.g. `--agent --git-context`). |
| `--no-redact` | off | Disable secret redaction (enabled by default). |
| `--version` | — | Show version and exit. |

## Output Fields

The full schema (`SourceMap`) includes the following fields:

### Always present

| Field | Type | Description |
|-------|------|-------------|
| `metadata` | object | Schema version, timestamp, `sourcecode` version, and analyzed path. |
| `file_tree` | object | Repository tree where `null` represents a file and `{}` represents a directory. Suppressed when `--no-tree` is active. |
| `file_paths` | array | Flat list of all project paths derived from `file_tree`, with forward-slash separators. Respects `--depth`. Suppressed when `--no-tree` is active. |
| `project_summary` | string\|null | Deterministic natural-language description of the project. Includes: manifest/README description when available, detected architecture pattern (`layered`, `mvc`, `hexagonal`, `fullstack`), business domain names inferred from directory structure, entry points (when no domains are detected), and dependency count. Present when stacks are detected. |
| `architecture_summary` | string\|null | Static summary of the main execution flow, orchestrated modules, and output produced by the project. Present when enough structural evidence is available. |
| `stacks` | array | Stack detections with confidence, frameworks, manifests, `primary`, `root`, `workspace`, and `signals`. |
| `project_type` | string\|null | Overall project classification. |
| `entry_points` | array | Detected entry points by stack. |

### With `--dependencies`

| Field | Type | Description |
|-------|------|-------------|
| `dependencies` | array | Dependency records with declared and resolved versions, scope, and manifest path. |
| `dependency_summary` | object | Summary with ecosystem coverage, counts (total, direct, transitive), sources, and known limitations. |
| `key_dependencies` | array | Top-15 direct dependencies from `manifest` or `lockfile` sources, sorted by primary ecosystem first then alphabetically. Only populated when `--dependencies` is active. |

### With `--graph-modules`

| Field | Type | Description |
|-------|------|-------------|
| `module_graph` | object | Structural graph with nodes, edges, and analysis summary. |
| `module_graph_summary` | object | Compact graph summary (node/edge counts, layers, main flows, truncation status). |

### With `--docs`

| Field | Type | Description |
|-------|------|-------------|
| `docs` | array | Extracted `DocRecord` objects for each documented symbol. |
| `doc_summary` | object | Summary with total count, languages, depth used, truncation status, and limitations. |

### With `--env-map`

| Field | Type | Description |
|-------|------|-------------|
| `env_map` | array | One `EnvVarRecord` per detected environment variable. |
| `env_summary` | object | Summary: total count, required vs optional counts, categories present, and example files found. Also included in `--compact` output when the flag is active. |

Each `EnvVarRecord`:

| Field | Description |
|-------|-------------|
| `key` | Environment variable name (e.g. `DATABASE_URL`). |
| `required` | `true` if the code reads the variable without a fallback default (`os.environ["KEY"]`, `process.env.KEY`). `false` if a default was found (`os.getenv("KEY", "default")`). |
| `default` | Default value detected in code, or `null` if none. |
| `type_hint` | Inferred type: `string`, `int`, `bool`, `url`, `path`, or `enum`. Derived from the key name (e.g. `_PORT` → `int`, `_URL` → `url`, `ENABLE_*` → `bool`). |
| `category` | Functional group inferred from the key name: `database`, `cache`, `storage`, `auth`, `service`, `observability`, `feature_flag`, `server`, or `general`. |
| `description` | Description extracted from a comment above the variable in `.env.example`, or `null`. |
| `files` | Up to 10 `"path:line"` references where the variable is read in source code. Empty for variables found only in `.env.example`. |

Supported languages: Python (`os.getenv`, `os.environ`), JavaScript/TypeScript (`process.env`), Go (`os.Getenv`, `os.LookupEnv`), Ruby (`ENV[]`, `ENV.fetch`), Java (`System.getenv`), PHP (`getenv`, `$_ENV`), Rust (`env::var`).

### With `--code-notes`

| Field | Type | Description |
|-------|------|-------------|
| `code_notes` | array | One `CodeNote` per annotation found in source files. |
| `code_adrs` | array | One `AdrRecord` per Architecture Decision Record detected. |
| `code_notes_summary` | object | Summary: total count, counts by kind, top files with most annotations, and ADR count. Also included in `--compact` output when the flag is active. |

Each `CodeNote`:

| Field | Description |
|-------|-------------|
| `kind` | Annotation type: `TODO`, `FIXME`, `HACK`, `NOTE`, `DEPRECATED`, `WARNING`, `XXX`, `BUG`, or `OPTIMIZE`. |
| `path` | File path relative to project root. |
| `line` | 1-based line number. |
| `text` | Annotation text (truncated to 200 characters). |
| `symbol` | Name of the nearest enclosing function or class found by scanning backward up to 25 lines. `null` at module level. |

Each `AdrRecord`:

| Field | Description |
|-------|-------------|
| `path` | File path relative to project root. |
| `title` | Title extracted from the first heading (`# ...`) in the file. |
| `status` | Normalized status: `accepted`, `proposed`, `deprecated`, or `superseded`. `null` if no status line was found. |
| `summary` | First paragraph of body text after the title. `null` if not parseable. |

ADR detection looks for Markdown files in `docs/decisions/`, `docs/adr/`, `adr/`, `decisions/`, and `architecture/decisions/`, and for files with names matching `ADR-*.md`, `0001-*.md`, or `DECISION-*.md` patterns.

### With `--git-context`

| Field | Type | Description |
|-------|------|-------------|
| `git_context` | object | Git context with recent commits, change hotspots, uncommitted changes, contributors, and a natural-language summary. |

`git_context` fields:

| Field | Description |
|-------|-------------|
| `branch` | Current branch name. |
| `recent_commits` | Up to `--git-depth` commits (default 20). Each includes `hash`, `message`, `author`, `date`, and `files_changed` (capped at 10 per commit). |
| `change_hotspots` | Up to 20 files sorted by commit frequency within the `--git-days` window (default 90 days). Each includes `file`, `commit_count`, and `last_changed`. |
| `uncommitted_changes` | Object with `staged`, `unstaged`, and `untracked` file lists from `git status`. |
| `contributors` | Unique author names active within the `--git-days` window. |
| `git_summary` | Deterministic natural-language summary: branch, pending changes, top hotspots, and last commit. |
| `limitations` | List of errors or degraded analysis signals (e.g. `no_git_repo`, `git_not_found`). |

## Compact Mode

`--compact` returns a reduced JSON view optimized for LLM prompts. It excludes the full `dependencies` list, `docs`, and `module_graph`, while retaining the fields most useful for project orientation. When optional flags are also active, their summaries are included: `dependency_summary` + `key_dependencies` (with `--dependencies`), `env_summary` (with `--env-map`), and `code_notes_summary` (with `--code-notes`). Adding `--no-tree` further removes `file_tree_depth1`, bringing the output to ~500-800 tokens depending on how many flags are active.

Real output from a Python FastAPI project:

```json
{
  "schema_version": "1.0",
  "project_type": "api",
  "project_summary": "API en Python (FastAPI). Entry points: src/main.py. 12 dependencias (python).",
  "architecture_summary": null,
  "stacks": [
    {
      "stack": "python",
      "detection_method": "manifest",
      "confidence": "high",
      "frameworks": [
        { "name": "FastAPI", "source": "package.json" }
      ],
      "package_manager": null,
      "manifests": ["pyproject.toml"],
      "primary": true,
      "root": ".",
      "workspace": null,
      "signals": ["manifest:pyproject.toml", "framework:FastAPI", "entry:src/main.py"]
    }
  ],
  "entry_points": [
    {
      "path": "src/main.py",
      "stack": "python",
      "kind": "cli",
      "source": "manifest"
    }
  ],
  "file_tree_depth1": {
    "pyproject.toml": null,
    "src": {},
    "tests": {}
  },
  "dependency_summary": null
}
```

When `--compact --dependencies` is used, `dependency_summary` is populated and `key_dependencies` lists the top-15 direct dependencies with resolved versions. Add `--no-tree` to drop `file_tree_depth1` for the smallest possible context footprint.

## Docs Mode

`--docs` extracts docstrings, function signatures, and comments from Python and JS/TS source files. Each extracted record is a `DocRecord`:

```json
{
  "symbol": "create_user",
  "kind": "function",
  "language": "python",
  "path": "src/users.py",
  "doc_text": "Create a new user in the database.\n\nReturns the created user ID.",
  "signature": "def create_user(name: str, email: str) -> int",
  "source": "docstring",
  "importance": "high",
  "workspace": null
}
```

### DocRecord fields

| Field | Description |
|-------|-------------|
| `symbol` | Symbol name (module name, function name, class name, etc.). |
| `kind` | Kind of symbol: `module`, `function`, `class`, `method`, or similar. |
| `language` | Source language: `python`, `javascript`, `typescript`. |
| `path` | File path relative to the project root, forward-slash separated. |
| `doc_text` | Extracted docstring or comment text. `null` if unavailable. |
| `signature` | Function or class signature as found in source. `null` for modules. |
| `source` | How the doc was obtained: `docstring`, `comment`, or `unavailable`. Records with `source="unavailable"` are not emitted in `docs[]` — they appear only in `doc_summary.limitations`. |
| `importance` | Inferred priority: `high`, `medium`, or `low`. |
| `workspace` | Workspace path for monorepo packages, `null` for single-workspace projects. |

### Importance inference rules

- `high`: the file path matches a project entry point, or the module is at depth 1 in the file tree (e.g., `src/main.py`).
- `medium`: the file is at depth 2, or the symbol kind is `class` or `function` (not a method).
- `low`: methods and utilities in deeper subdirectories.

### `--docs-depth` levels

- `module`: extracts module-level docstrings only. One record per file.
- `symbols` (default): module-level plus top-level functions and classes.
- `full`: all of the above plus methods inside classes.

## Output — Full Schema Examples

### Dependencies

```json
{
  "dependencies": [
    {
      "name": "fastapi",
      "ecosystem": "python",
      "scope": "direct",
      "declared_version": ">=0.115",
      "resolved_version": "0.115.2",
      "source": "lockfile",
      "parent": null,
      "manifest_path": "poetry.lock",
      "workspace": null
    },
    {
      "name": "starlette",
      "ecosystem": "python",
      "scope": "transitive",
      "declared_version": null,
      "resolved_version": "0.38.6",
      "source": "lockfile",
      "parent": "fastapi",
      "manifest_path": "poetry.lock",
      "workspace": null
    }
  ],
  "dependency_summary": {
    "requested": true,
    "total_count": 2,
    "direct_count": 1,
    "transitive_count": 1,
    "ecosystems": ["python"],
    "sources": ["lockfile"],
    "limitations": []
  }
}
```

Dependency analysis is offline and conservative: if a lockfile does not expose a reliable transitive graph, `sourcecode` reports direct dependencies and records the limitation instead of guessing.

### Module Graph

```json
{
  "module_graph": {
    "nodes": [
      {
        "id": "module:app",
        "kind": "module",
        "language": "python",
        "path": "app",
        "symbol": null,
        "display_name": "app",
        "workspace": null,
        "importance": "high"
      }
    ],
    "edges": [],
    "summary": {
      "requested": true,
      "node_count": 1,
      "edge_count": 0,
      "languages": ["python"],
      "methods": ["ast"],
      "main_flows": [],
      "layers": ["app"],
      "entry_points_count": 1,
      "truncated": false,
      "detail": "high",
      "max_nodes_applied": 80,
      "edge_kinds": ["imports"],
      "limitations": []
    }
  },
  "module_graph_summary": {
    "requested": true,
    "node_count": 1,
    "edge_count": 0,
    "main_flows": [],
    "layers": ["app"],
    "entry_points_count": 1,
    "truncated": false,
    "limitations": []
  }
}
```

`--graph-modules` is tiered for LLM workflows:

- `high`: summarized graph, modules only, imports only, directory collapsing when useful. Default.
- `medium`: balanced graph with key functions and selected call edges.
- `full`: full-fidelity graph, equivalent to exhaustive AST analysis.

Graph analysis is offline and conservative. `sourcecode` prefers partial but defensible edges over pretending to build a perfect semantic call graph, and records parse failures, unresolved imports, or analysis budgets in `module_graph.summary.limitations`.

## Monorepo Support

In a monorepo, each stack includes its own `root` and `workspace`, and one of them is marked as `primary`. Entry point and doc record paths are prefixed with the workspace path so they are relative to the repository root.

```json
{
  "project_type": "monorepo",
  "project_summary": "Monorepo con 2 workspaces en Node.js, Python.",
  "stacks": [
    {
      "stack": "nodejs",
      "primary": true,
      "root": "apps/web",
      "workspace": "apps/web"
    },
    {
      "stack": "python",
      "primary": false,
      "root": "packages/api",
      "workspace": "packages/api"
    }
  ],
  "entry_points": [
    { "path": "apps/web/app/page.tsx", "stack": "nodejs", "kind": "web", "source": "manifest" },
    { "path": "packages/api/main.py", "stack": "python", "kind": "cli", "source": "manifest" }
  ]
}
```

## LLM Usage Tips

Different modes optimize for different tradeoffs between context size and depth of information.

**`--agent` — zero-config mode for AI pipelines**

Best for: running `sourcecode` from inside an agent without knowing the project in advance. Auto-selects the right flag combination based on detected stack and project size.

```bash
sourcecode --agent .
sourcecode --agent --git-context .   # add git context on top of auto-selected flags
```

On a Java/Gradle project the output is: `--compact --dependencies --env-map --code-notes --no-tree` (~800 tokens). On a Node/Python project: same without `--no-tree` (~900 tokens including depth-1 tree). Selected flags are always printed to stderr so the agent can log them.

**`--compact` — minimal context**

Best for: initial orientation, deciding what to explore next, fast handoffs between agents.

Includes: `project_summary`, `architecture_summary`, `stacks`, `entry_points`, `file_tree_depth1`, and — when the respective flags are active — `dependency_summary` + `key_dependencies`, `env_summary`, `code_notes_summary`.

```bash
sourcecode --compact .
sourcecode --compact --dependencies .           # + key dependencies with versions
sourcecode --compact --dependencies --no-tree . # smallest footprint: no file tree
```

**Full output — deep analysis**

Best for: thorough codebase understanding, architecture analysis, onboarding a new agent to an unfamiliar project.

```bash
sourcecode .
sourcecode --dependencies --graph-modules .
```

**`--docs --docs-depth symbols` — API contracts**

Best for: understanding what a module exports and how to call it, without reading source files. The default depth (`symbols`) covers top-level functions and classes — the most useful level for most agentic tasks.

```bash
sourcecode --docs .
sourcecode --docs --docs-depth full .   # include methods
```

**`project_summary` field**

`project_summary` is always generated when stacks are detected. It provides instant project context without requiring the LLM to parse the full structure. The content adapts to what is detectable:

- If `pyproject.toml`, `package.json`, or the README provides a description, it leads the summary; stack, architecture pattern, and domains are appended as context.
- If the directory structure reveals a known architecture pattern (`layered`, `mvc`, `hexagonal`, `fullstack`), it is included.
- Business domain names (directories that are not architectural layers or generic utilities) replace entry points when two or more distinct domains are detected.

Example values:

- `"API en Python (FastAPI) con arquitectura layered. Dominios: auth, users, billing. 12 dependencias (python)."` (structured project)
- `"API en Python (FastAPI). Entry points: src/main.py. 12 dependencias (python)."` (flat project, no domains detected)
- `"Aplicacion web en Node.js (Next.js) con arquitectura mvc. Dominios: products, orders. 24 dependencias (nodejs)."`
- `"Monorepo con 2 workspaces en Node.js, Python."`

**`architecture_summary` field**

`architecture_summary` is a static 3-5 line summary oriented to execution flow. It answers what the main entry point does, which modules it orchestrates, and what the project produces. In compact mode it replaces the low-signal value that `file_paths` used to occupy.

**`key_dependencies` field**

When `--dependencies` is active, `key_dependencies` contains the top-15 direct dependencies sorted by primary ecosystem first. Use it to understand core library choices without scanning hundreds of transitive records.

**`--git-context` — temporal project context**

Best for: understanding recent activity before touching code, debugging regressions, onboarding to an active repository.

Answers questions a static analysis cannot: what changed recently, which files are actively maintained, whether there is uncommitted work in progress, and who is driving the project.

```bash
sourcecode --git-context .
sourcecode --git-context --git-depth 10 --git-days 30 .
```

`git_summary` condenses the key signals into a single line:

```
"Rama main. 3 cambios pendientes (staged: 1, unstaged: 2, untracked: 0). Archivos más activos: src/cli.py (18 commits), src/schema.py (14 commits). Último commit: 2026-04-22 — docs(13): add gap closure plan."
```

Combine with `--compact` for fast handoffs that include both static structure and recent activity:

```bash
sourcecode --compact --dependencies --git-context .
```

Note: `git_context` is excluded from the `--compact` token budget but is always present in full output when the flag is active.

**`--env-map` — configuration surface**

Best for: onboarding a new agent to an unfamiliar project, understanding what environment a service requires to run, reviewing configuration completeness before deployment.

Answers: what variables does this project expect, which ones are required vs optional, where are they read, and what type and category are they?

```bash
sourcecode --env-map .
sourcecode --compact --env-map .   # configuration surface in ~700 tokens
```

Example output (excerpt):

```json
{
  "env_map": [
    {
      "key": "DATABASE_URL",
      "required": true,
      "default": null,
      "type_hint": "url",
      "category": "database",
      "description": "PostgreSQL connection string",
      "files": ["src/db.py:12", "src/config.py:5"]
    },
    {
      "key": "LOG_LEVEL",
      "required": false,
      "default": "INFO",
      "type_hint": "enum",
      "category": "observability",
      "description": null,
      "files": ["src/logger.py:3"]
    }
  ],
  "env_summary": {
    "requested": true,
    "total": 14,
    "required_count": 6,
    "optional_count": 8,
    "categories": ["auth", "database", "observability", "server"],
    "example_files_found": [".env.example"]
  }
}
```

**`--code-notes` — technical debt and intent**

Best for: understanding known issues before modifying code, identifying deprecated APIs, discovering design decisions embedded in comments, and locating ADRs when they exist.

Answers: what do the authors know is broken or suboptimal, what is explicitly marked for removal, what architectural decisions were recorded, and which areas carry the most annotation debt?

```bash
sourcecode --code-notes .
sourcecode --compact --code-notes .   # debt overview in compact form
```

Example output (excerpt):

```json
{
  "code_notes": [
    {
      "kind": "FIXME",
      "path": "src/payments.py",
      "line": 42,
      "text": "currency conversion is broken for EUR",
      "symbol": "process_payment"
    },
    {
      "kind": "DEPRECATED",
      "path": "src/auth.py",
      "line": 18,
      "text": "use AuthService instead",
      "symbol": "UserService"
    }
  ],
  "code_adrs": [
    {
      "path": "docs/decisions/0001-use-postgresql.md",
      "title": "ADR-0001: Use PostgreSQL as primary database",
      "status": "accepted",
      "summary": "PostgreSQL was chosen for its JSONB support and strong ACID guarantees."
    }
  ],
  "code_notes_summary": {
    "requested": true,
    "total": 23,
    "by_kind": {"TODO": 10, "FIXME": 7, "HACK": 3, "DEPRECATED": 2, "WARNING": 1},
    "top_files": ["src/payments.py", "src/legacy.py"],
    "adr_count": 3
  }
}
```

Combine flags for a comprehensive project handoff:

```bash
sourcecode --compact --env-map --code-notes --git-context .
```

## `prepare-context` — Task-aware context for LLMs

`prepare-context` is a subcommand that builds a focused, task-specific context optimized for LLM reasoning. Instead of a full project dump, it returns only the data an LLM needs for a specific goal.

> **Note:** Because `sourcecode` has a positional `PATH` argument, you must provide it explicitly before the subcommand:
> ```bash
> sourcecode . prepare-context <task>         # current directory
> sourcecode /my/project prepare-context <task>
> ```

### Available tasks

| Task | Goal | Output |
|------|------|--------|
| `explain` | Onboard an LLM to the project | `project_summary`, `architecture_summary`, `relevant_files`, `key_dependencies` |
| `fix-bug` | Identify likely bug locations | `relevant_files` (ranked by risk), `suspected_areas`, `code_notes_summary` |
| `refactor` | Surface improvement opportunities | `relevant_files`, `improvement_opportunities`, `architecture_summary` |
| `generate-tests` | Find untested areas | `test_gaps`, `relevant_files` (source without tests), `key_dependencies` |

### Usage

```bash
# List tasks with descriptions
sourcecode . prepare-context --task-help

# Explain the project
sourcecode . prepare-context explain

# Find bug areas, with a ready-to-paste LLM prompt
sourcecode . prepare-context fix-bug --llm-prompt

# Find untested files in a specific project
sourcecode . prepare-context generate-tests --path /my/project

# Preview what will be analyzed (no analysis run)
sourcecode . prepare-context refactor --dry-run
```

### Output format

```json
{
  "task": "fix-bug",
  "goal": "Identify the most likely files and areas where a bug may be located.",
  "project_summary": "CLI en Python (Typer). Entry points: src/cli.py. 4 dependencias.",
  "architecture_summary": null,
  "relevant_files": [
    { "path": "src/handler.py", "role": "source", "score": 2.0, "reason": "matches 'handler'" },
    { "path": "src/cli.py",     "role": "entrypoint", "score": 3.0, "reason": "entry point" }
  ],
  "suspected_areas": ["src/handler.py (2 annotations)", "src/parser.py (1 annotation)"],
  "code_notes_summary": { "total": 5, "by_kind": { "FIXME": 3, "BUG": 2 }, "top_files": ["src/handler.py"] }
}
```

### `--llm-prompt`

Add `--llm-prompt` to include a ready-to-use prompt that you can paste directly into any LLM:

```bash
sourcecode . prepare-context explain --llm-prompt | jq -r '.llm_prompt'
```

The prompt is task-specific and includes the project context, relevant files, and concrete instructions for the LLM.

### Task auto-selection with `--agent`

For zero-config usage, `--agent` on the main command automatically selects the right flags for the project type. For task-aware context, use `prepare-context` explicitly with the task that matches your goal.

## Development

Editable install with development dependencies:

```bash
pip install -e ".[dev]"
```

Local validation:

```bash
ruff check src tests
mypy src
pytest -q
```

Detailed schema reference: [docs/schema.md](docs/schema.md).

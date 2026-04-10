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

- Stacks: Node.js, Python, Go, Rust, Java, PHP, Ruby, and Dart.
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
| `--compact` | off | Reduced output (~500-700 tokens): `schema_version`, `project_type`, `project_summary`, `stacks`, `entry_points`, `file_paths`, `file_tree_depth1`, and `dependency_summary` when available. |
| `--dependencies` | off | Include direct dependencies, resolved versions, and transitive relationships when lockfiles make that possible. Also populates `key_dependencies`. |
| `--graph-modules` | off | Include a structural module graph with imports and simple relations. |
| `--graph-detail high\|medium\|full` | `high` | Graph detail level: summarized (high), balanced (medium), or full-fidelity (full). |
| `--max-nodes INTEGER` | none | Cap graph size in `high` and `medium` modes. Min: 1. |
| `--graph-edges imports,calls,contains,extends` | none | Override the default edge kinds for the selected detail level. |
| `--docs` | off | Include extracted documentation: docstrings, signatures, and comments from Python and JS/TS modules and symbols. |
| `--docs-depth module\|symbols\|full` | `symbols` | Documentation extraction depth: module-level only, modules and top-level symbols (functions/classes), or all symbols including methods. |
| `--depth INTEGER` | `4` | Maximum file tree depth. Range: 1–20. |
| `--no-redact` | off | Disable secret redaction (enabled by default). |
| `--version` | — | Show version and exit. |

## Output Fields

The full schema (`SourceMap`) includes the following fields:

### Always present

| Field | Type | Description |
|-------|------|-------------|
| `metadata` | object | Schema version, timestamp, `sourcecode` version, and analyzed path. |
| `file_tree` | object | Repository tree where `null` represents a file and `{}` represents a directory. |
| `file_paths` | array | Flat list of all project paths derived from `file_tree`, with forward-slash separators. Always present; respects `--depth`. |
| `project_summary` | string\|null | Deterministic natural-language description of the project generated from detected stacks, entry points, and dependencies. Present when stacks are detected. |
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

## Compact Mode

`--compact` returns a reduced JSON view optimized for LLM prompts (~500-700 tokens). It excludes the full `dependencies` list, `docs`, and `module_graph`, while retaining the fields most useful for project orientation.

Real output from a Python FastAPI project:

```json
{
  "schema_version": "1.0",
  "project_type": "api",
  "project_summary": "API en Python (FastAPI). Entry points: src/main.py. 12 dependencias (python).",
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
  "file_paths": ["pyproject.toml", "src/main.py", "src/routes.py", "tests/test_main.py"],
  "file_tree_depth1": {
    "pyproject.toml": null,
    "src": {},
    "tests": {}
  },
  "dependency_summary": null
}
```

When `--compact --dependencies` is used, `dependency_summary` is populated instead of `null`.

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

**`--compact` — minimal context (~500-700 tokens)**

Best for: initial orientation, deciding what to explore next, fast handoffs between agents.

Includes: `project_summary` (instant project description), `stacks`, `entry_points`, `file_paths` (flat path list for easy grep/reasoning), `file_tree_depth1`, and `dependency_summary` when `--dependencies` was also requested.

```bash
sourcecode --compact .
sourcecode --compact --dependencies .
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

`project_summary` is always generated when stacks are detected. It provides instant project context without requiring the LLM to parse the full structure. Example values:

- `"API en Python (FastAPI). Entry points: src/main.py. 12 dependencias (python)."`
- `"Aplicacion web en Node.js (Next.js, React). Entry points: app/page.tsx."`
- `"Monorepo con 2 workspaces en Node.js, Python."`

**`file_paths` field**

`file_paths` is a flat list of all project paths (forward-slash separated). It is easier for LLMs to reason about than the nested `file_tree` dict — grep it, count by extension, or identify modules by path pattern without recursive traversal.

**`key_dependencies` field**

When `--dependencies` is active, `key_dependencies` contains the top-15 direct dependencies sorted by primary ecosystem first. Use it to understand core library choices without scanning hundreds of transitive records.

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

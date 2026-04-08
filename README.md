# sourcecode

`sourcecode` generates a structured project context map so an agent can quickly understand a repository's stack, entry points, and overall shape.

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

Generate a compact view for prompts or handoff:

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

Include an internal module graph with imports and simple structural relations:

```bash
sourcecode . --graph-modules
```

Choose how much graph detail you want:

```bash
sourcecode . --graph-modules --graph-detail high
sourcecode . --graph-modules --graph-detail medium
sourcecode . --graph-modules --graph-detail full
```

Show the version:

```bash
sourcecode --version
```

Main options:

- `--format json|yaml`: output format.
- `--output PATH`: write to a file instead of `stdout`.
- `--compact`: return a reduced view with `schema_version`, `project_type`, `stacks`, `entry_points`, and `file_tree_depth1`.
- `--dependencies`: include direct dependencies, resolved versions, and transitive relationships when lockfiles make that possible.
- `--graph-modules`: include a structural graph optimized for repository reasoning.
- `--graph-detail high|medium|full`: choose a summarized, balanced, or full-fidelity graph. Default: `high`.
- `--max-nodes INTEGER`: cap graph size in `high` and `medium` modes.
- `--graph-edges imports,calls,contains,extends`: override the default edge kinds for the selected detail level.
- `--depth INTEGER`: maximum file tree depth.
- `--no-redact`: disable secret redaction.

## What It Detects

- Stacks: Node.js, Python, Go, Rust, Java, PHP, Ruby, and Dart.
- Frameworks associated with each stack when enough signals are present.
- `project_type`: `webapp`, `api`, `library`, `cli`, `fullstack`, `monorepo`, or `unknown`.
- Relevant `entry_points`, such as `main.py`, `cmd/api/main.go`, or `app/page.tsx`.
- Workspace roots in multi-stack or monorepo repositories.

## Compact Example

Real output from a Next.js fixture:

```json
{
  "schema_version": "1.0",
  "project_type": "webapp",
  "stacks": [
    {
      "stack": "nodejs",
      "detection_method": "manifest",
      "confidence": "high",
      "frameworks": [
        { "name": "Next.js", "source": "package.json" },
        { "name": "React", "source": "package.json" }
      ],
      "package_manager": "pnpm",
      "manifests": ["package.json"],
      "primary": true,
      "root": ".",
      "workspace": null,
      "signals": [
        "manifest:package.json",
        "framework:Next.js",
        "framework:React",
        "package_manager:pnpm",
        "entry:app/page.tsx"
      ]
    }
  ],
  "entry_points": [
    {
      "path": "app/page.tsx",
      "stack": "nodejs",
      "kind": "web",
      "source": "package.json"
    }
  ],
  "file_tree_depth1": {
    "pnpm-lock.yaml": null,
    "package.json": null,
    "app": {}
  }
}
```

## Monorepo Example

In a monorepo, each stack includes its own `root` and `workspace`, and one of them is marked as `primary`.

```json
{
  "project_type": "monorepo",
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
    { "path": "apps/web/app/page.tsx", "stack": "nodejs", "kind": "web" },
    { "path": "packages/api/main.py", "stack": "python", "kind": "cli" }
  ]
}
```

## Output

The full schema includes:

- `metadata`: schema version, timestamp, `sourcecode` version, and analyzed path.
- `file_tree`: repository tree where `null` represents a file and an object represents a directory.
- `stacks`: stack detections with confidence, frameworks, manifests, `primary`, `root`, `workspace`, and `signals`.
- `project_type`: overall project classification.
- `entry_points`: detected entry points by stack.
- `dependencies`: optional dependency records with declared and resolved versions.
- `dependency_summary`: optional summary with ecosystem coverage, counts, and known limitations.
- `module_graph`: optional structural graph with nodes, edges, and analysis limits.
- `module_graph_summary`: compact graph summary optimized for downstream LLM consumption.

Example dependency block:

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
      "workspace": "packages/api"
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
      "workspace": "packages/api"
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

Dependency analysis is still offline and conservative: if a lockfile does not expose a reliable transitive graph, `sourcecode` reports direct dependencies and records the limitation instead of guessing.

Example module graph block:

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

`--graph-modules` is now tiered for LLM workflows:

- `high`: summarized graph, modules only, imports only, directory collapsing when useful.
- `medium`: balanced graph with key functions and selected call edges.
- `full`: full-fidelity graph, equivalent to the previous exhaustive behavior.

Graph analysis is also offline and conservative. `sourcecode` prefers partial but defensible edges over pretending to build a perfect semantic call graph, and it records parse failures, unresolved imports, or analysis budgets in `module_graph.summary.limitations`.

Detailed reference: [docs/schema.md](/Users/user/Documents/workspace/atlas/atlas-cli/docs/schema.md).

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

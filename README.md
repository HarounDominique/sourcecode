# sourcecode

**Deterministic codebase context for AI coding agents.**

[![PyPI](https://img.shields.io/pypi/v/sourcecode)](https://pypi.org/project/sourcecode/)
[![Python](https://img.shields.io/pypi/pyversions/sourcecode)](https://pypi.org/project/sourcecode/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue)](LICENSE)
[![CI](https://img.shields.io/github/actions/workflow/status/sourcecode-ai/sourcecode/ci.yml)](https://github.com/sourcecode-ai/sourcecode/actions)

Turn any repository into structured, reproducible context optimized for AI coding agents — in one command.

```bash
pip install sourcecode
sourcecode . --agent
```

```json
{
  "project": {
    "type": "api",
    "summary": "Python REST API built with FastAPI and SQLAlchemy. Layered architecture with domain, service, and infrastructure layers.",
    "primary_stack": "python",
    "frameworks": ["FastAPI", "SQLAlchemy"]
  },
  "entry_points": [
    { "path": "src/app/main.py", "kind": "server", "confidence": "high" }
  ],
  "architecture": "FastAPI application. Clean Architecture with domain, application, and infrastructure layers. Hub modules: schema.py, models.py.",
  "key_dependencies": [
    { "name": "fastapi", "declared_version": ">=0.100", "role": "runtime" },
    { "name": "sqlalchemy", "declared_version": "^2.0", "role": "runtime" },
    { "name": "pydantic", "declared_version": "^2.0", "role": "runtime" }
  ],
  "confidence_summary": { "overall": "high" }
}
```

---

## The problem

AI coding agents are only as good as the context they receive. In large, real-world repositories, that context is almost always wrong.

- **Agents start blind.** Without repo structure, they hallucinate imports, file paths, and architecture decisions.
- **Context is noisy.** Raw file trees contain benchmark dirs, generated files, tooling configs, and docs that consume tokens without helping.
- **Architecture is invisible.** LLMs see files, not systems. They miss layers, plugin systems, entry points, and runtime topology.
- **Context decays.** What you paste today is stale tomorrow. There's no reproducible baseline.
- **Manual context doesn't scale.** Handcrafting prompts per project is engineering debt that grows with every new agent, team, and task.

---

## The solution

`sourcecode` analyzes your repository and produces a structured, reproducible context package — ready to inject into any AI coding agent.

**What it does:**
- Detects stacks, frameworks, entry points, and project type across 10+ languages
- Infers runtime topology: which packages are core, which are plugins, which are noise
- Ranks files by operational relevance for agents: git churn + runtime proximity + bootstrap signal
- Suppresses non-runtime noise: benchmarks, docs, tooling, generated files
- Produces structured JSON/YAML that agents can reason over, not raw file trees
- Runs deterministically — same repo, same output, every time

**What it outputs:**
- `project_summary` — one-sentence natural language description
- `architecture_summary` — runtime topology: layers, plugin systems, entry flows
- `entry_points` — where execution actually starts (production, not benchmarks)
- `key_dependencies` — runtime dependencies with role classification
- `relevant_files` — ranked by usefulness for coding tasks, not folder position
- `confidence_summary` — detection quality and analysis gaps

All fields are stable, machine-readable, and designed for LLM consumption.

---

## Install

```bash
pip install sourcecode
```

Requires Python 3.9+. No API keys. No network calls. Runs locally.

---

## Quickstart

**Basic analysis:**
```bash
sourcecode .
```

**Agent-optimized output** (structured, noise-free, gap-aware):
```bash
sourcecode . --agent
```

**Task-specific context for coding agents:**
```bash
# Explain the project architecture
sourcecode . prepare-context explain

# Find likely bug locations
sourcecode . prepare-context fix-bug

# Onboard a new agent to the codebase
sourcecode . prepare-context onboard

# Ranked context for a specific task
sourcecode . prepare-context refactor
```

**Pipe directly into Claude Code or any agent:**
```bash
sourcecode . --agent | claude -p "Review the architecture and suggest improvements"
```

**Write to file for session injection:**
```bash
sourcecode . --agent --output context.json
```

**Include git activity signals:**
```bash
sourcecode . --agent --git-context
```

---

## Use cases

### Claude Code
```bash
# Start every session with full context
sourcecode . --agent > .claude/context.json

# Use with CLAUDE.md for persistent context
echo "$(sourcecode . --agent --compact)" >> CLAUDE.md
```

### Cursor / Windsurf / Copilot
```bash
# Generate context snapshot before starting a feature
sourcecode . --agent --git-context --output .cursor/context.json
```

### OpenAI / Anthropic API
```python
import json, subprocess

context = json.loads(
    subprocess.check_output(["sourcecode", ".", "--agent"])
)

system_prompt = f"""
You are working on: {context['project']['summary']}
Architecture: {context['architecture']}
Entry points: {[ep['path'] for ep in context['entry_points']]}
"""
```

### CI / CD pipelines
```yaml
# .github/workflows/context.yml
- name: Generate codebase context
  run: sourcecode . --agent --output context.json

- name: AI-assisted code review
  run: |
    CONTEXT=$(cat context.json)
    # Inject into your preferred AI review step
```

### Onboarding new engineers
```bash
# Generate human-readable architecture summary
sourcecode . prepare-context onboard --llm-prompt
```

### Architecture audits
```bash
sourcecode . --agent --architecture --graph-modules --dependencies
```

---

## How it works

`sourcecode` runs a local, static analysis pipeline on your repository:

```
Repository
    │
    ├── Scanner          # File tree, manifests, workspace detection
    ├── Stack Detectors  # Language, framework, package manager detection
    ├── Entry Points     # Production entry points (not benchmarks/docs)
    ├── Git Analyzer     # Churn hotspots, uncommitted changes
    ├── Relevance Scorer # Runtime proximity × git churn × bootstrap signal
    └── Serializer       # Structured JSON/YAML output
```

No LLM calls. No network requests. No sampling. Fully deterministic.

The same repository produces the same output on every run — which means agents can cache it, diff it, and rely on it.

---

## Output modes

| Mode | Use case | Size |
|------|----------|------|
| `sourcecode .` | Full analysis | Full |
| `sourcecode . --agent` | AI agent injection | ~600–1000 tokens |
| `sourcecode . --compact` | Prompts, handoffs | ~500–700 tokens |
| `sourcecode . prepare-context <task>` | Task-specific context | ~800–1200 tokens |

### Available flags

| Flag | Description |
|------|-------------|
| `--agent` | Structured, noise-free output for AI agents. Auto-enables `--dependencies`, `--env-map`, `--code-notes`. |
| `--dependencies` | Direct dependencies with versions and role classification. |
| `--git-context` | Recent commits, change hotspots, uncommitted files. |
| `--architecture` | Layer inference: MVC, layered, hexagonal, domain-based. |
| `--graph-modules` | Module import graph and call relationships. |
| `--semantics` | Cross-file symbol resolution and call graph. |
| `--env-map` | All environment variables referenced in source. |
| `--code-notes` | TODOs, FIXMEs, HACKs, and Architecture Decision Records. |
| `--compact` | Minimal output for token-constrained prompts. |
| `--format yaml` | YAML instead of JSON. |
| `--output PATH` | Write to file instead of stdout. |

Full reference: `sourcecode --help`

### Prepare-context tasks

| Task | What it produces |
|------|-----------------|
| `explain` | Architecture + entry points + key dependencies |
| `fix-bug` | Risk-ranked files + suspected areas + code annotations |
| `refactor` | Structural issues + improvement opportunities |
| `generate-tests` | Untested source files + test gap analysis |
| `onboard` | Full project understanding for new agents/developers |
| `review-pr` | Changed files + architectural impact |
| `delta` | Git-changed files only — incremental context |

---

## Philosophy

**Determinism over approximation.** Every run on the same repository produces the same output. Agents, pipelines, and teams can depend on that.

**Runtime topology over file trees.** What matters is where execution starts, what calls what, and which modules are actually critical — not alphabetical file lists.

**Noise suppression by default.** Benchmark dirs, generated files, tooling configs, and docs are suppressed unless explicitly requested. Agents get signal, not inventory.

**Local-first, privacy-respecting.** No code leaves your machine. No API keys required. Analysis is fully offline.

**Composable, not monolithic.** Output is structured data. Pipe it, transform it, inject it, cache it. It's infrastructure, not a magic black box.

**Confidence-aware.** Every analysis includes a confidence summary and gap list. Agents know what they don't know.

---

## Supported languages and stacks

| Language | Package detection | Entry points | Frameworks |
|----------|-------------------|--------------|------------|
| Python | `pyproject.toml`, `requirements.txt`, `setup.py` | CLI, scripts, `__main__` | FastAPI, Django, Flask, Typer, Click |
| Node.js | `package.json`, lock files | `main`, `bin`, scripts | Express, Next.js, Fastify, NestJS, React, Vue |
| Go | `go.mod` | `main.go`, `cmd/` | Standard library, Gin, Echo |
| Rust | `Cargo.toml` | `main.rs`, `lib.rs` | Tokio, Actix, Axum |
| Java | `pom.xml`, `build.gradle` | Spring Boot, Quarkus, Micronaut | Spring, Quarkus |
| Kotlin | `build.gradle.kts` | Spring Boot, Ktor | Spring, Ktor |
| .NET / C# | `.csproj`, `.sln` | `Program.cs` | ASP.NET, Blazor |
| PHP | `composer.json` | `index.php` | Laravel, Symfony |
| Ruby | `Gemfile` | `config.ru` | Rails, Sinatra |
| Dart | `pubspec.yaml` | `main.dart` | Flutter |

Monorepos with mixed stacks are fully supported.

---

## Roadmap

**Now — Core stability**
- Ranking improvements (git churn, runtime proximity)
- Better architecture inference
- Broader language coverage

**Next — Agent integrations**
- MCP server for native Claude Code integration
- VS Code extension
- Context diffing (compare before/after changes)
- Incremental updates (delta mode improvements)

**Later — Team features**
- Shared context snapshots
- Architecture drift detection
- CI integration templates
- Governance and compliance context

> Focus is on adoption and utility. No monetization until the core is genuinely useful to the community.

---

## Contributing

We welcome contributions. See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, testing, and guidelines.

**Quick start for contributors:**
```bash
git clone https://github.com/sourcecode-ai/sourcecode
cd sourcecode
pip install -e ".[dev]"
pytest tests/
```

---

## Security

`sourcecode` analyzes local repositories. It does not transmit code, paths, or analysis results to any external service. See [SECURITY.md](SECURITY.md) for our security policy and responsible disclosure process.

---

## Privacy

Telemetry is **opt-in only** and disabled by default. If you choose to enable it, only anonymous usage metadata is collected — never code, paths, or content. See [docs/privacy.md](docs/privacy.md) for full details.

```bash
sourcecode telemetry status   # check current setting
sourcecode telemetry enable   # opt in
sourcecode telemetry disable  # opt out
```

---

## License

Apache License 2.0. See [LICENSE](LICENSE) for details.

---

<p align="center">
  Built for the age of AI coding agents.<br>
  <a href="https://github.com/sourcecode-ai/sourcecode">GitHub</a> ·
  <a href="https://pypi.org/project/sourcecode/">PyPI</a> ·
  <a href="docs/getting-started.md">Documentation</a>
</p>

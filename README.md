# sourcecode

**Deterministic, behavior-aware codebase context for AI agents and PR review.**

![Version](https://img.shields.io/badge/version-1.30.8-blue)
![Python](https://img.shields.io/badge/python-3.10%2B-green)

---

## What is it?

`sourcecode` analyzes a repository and produces structured JSON or YAML designed to be fed directly to AI agents or language models. It solves the "stuff the whole repo into the prompt" problem by extracting a deterministic, high-signal summary: stack detection, entry points, dependencies, git hotspots, inline annotations, and confidence metadata.

For PR review specifically, `sourcecode` extracts **execution paths**: ordered chains from entry point through service to data access, with runtime signals (auth guards, cache short-circuits, async execution) anchored to the specific step where they affect behavior. A reviewer sees _what the system does_ under this change, not just which files changed.

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
# sourcecode 1.30.8
```

---

## Quickstart

```bash
# High-signal summary (1000–3000 tokens depending on repo size) — recommended starting point
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
| `--compact` | | off | High-signal summary (1000–3000 tokens): stacks, entry points, dependencies, risk flags, confidence, gaps. Includes `security_surface`, `mybatis`, and `transactional_boundaries` for Java projects. |
| `--agent` | | off | Structured noise-free JSON for AI agents: identity, entry points, dependencies, confidence, gaps. Auto-enables dependency, env-var, and code-notes analysis. |
| `--full` | | off | Remove truncation limits on `transactional_boundaries`, `mybatis.dto_mappers`, and other capped lists. |
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
| `review-pr` | Execution paths with per-step runtime signals, security/transactional impact, test coverage gaps | Pre-merge behavior review |
| `delta` | Changed files with multi-hop impact analysis, structural import graph, system-level impact summary | Incremental CI/review context |

### Options

| Option | Description |
|--------|-------------|
| `--since REF` | Git ref for `delta` task (e.g. `HEAD~3`, `main`, `v1.2.0`). Required for `delta`; ignored for other tasks. |
| `--symptom TEXT` | *(fix-bug only)* Keyword hint for the bug — boosts matching files and surfaces related code notes. |
| `--llm-prompt` | Append a ready-to-use LLM prompt to the output. |
| `--dry-run` | Show what would be analyzed without running it. |
| `--copy` / `-c` | Copy output to clipboard after a successful run. |
| `--output` / `-o` | Write output to a file. |
| `--task-help` | List all tasks with descriptions and exit. |

### Examples

```bash
# Explain the current repo
sourcecode prepare-context explain

# Focus on bug-prone files, with a symptom hint
sourcecode prepare-context fix-bug --symptom "NullPointerException in OrderService"

# Incremental context: files changed since branch diverged from main
sourcecode prepare-context delta . --since main

# Onboard with a ready-to-paste LLM prompt
sourcecode prepare-context onboard --llm-prompt

# List all tasks
sourcecode prepare-context --task-help
```

---

## `delta` — incremental impact analysis

The `delta` task is the recommended mode for CI pipelines and PR reviews. It goes beyond listing changed files: it builds a structural import graph and propagates impact transitively up to 3 hops.

```bash
sourcecode prepare-context delta [PATH] --since REF
```

**Output fields:**

| Field | Description |
|-------|-------------|
| `changed_files` | Files modified in the git range |
| `relevant_files` | Changed files + files pulled in by the import graph (scored by artifact type and hop distance) |
| `impact_summary` | Human-readable summary: artifact types changed and active risk areas |
| `affected_modules` | DDD domain modules touched by the change |
| `risk_areas` | Per-area severity breakdown (`security`, `api`, `persistence`, etc.) |
| `change_type` | Closed taxonomy: `behavioral_change`, `structural_change`, `configuration_change`, `dependency_change`, `security_change` |
| `system_impact` | Subsystems affected, behavioral changes, runtime impact notes |
| `dependency_graph_summary` | Verified structural import edges (hop 1–3) and `propagation_depth`. **Only real imports — no heuristics, no test files.** |
| `impact_score_per_file` | Per-file numeric impact score (0–1) |
| `since` | The git ref used |
| `gaps` | What the analysis could not determine |

**How the import graph works:**

1. Each changed file is classified by artifact type (`controller`, `service`, `repository`, `security`, `spring_config`, etc.).
2. A BFS traversal walks the import graph **repo-wide** (not restricted to the same module), up to 3 hops deep.
3. `dependency_graph_summary.edges` only contains verified `import` / `@Autowired` / constructor-injection relationships. Test files and heuristic proximity matches are excluded from edges (they appear in `relevant_files` only if they have real imports of changed files).
4. Score decays 30% per hop: a directly-changed `SecurityConfig.java` scores 0.90; its direct importer scores 0.63; a transitive importer scores 0.44.

```bash
# Changed service → controller → facade (3 hops)
sourcecode prepare-context delta . --since main

# Output includes:
# dependency_graph_summary.edges:
#   hop-1: OrderService.java → OrderRepository.java
#   hop-2: OrderRepository.java → OrderController.java
#   hop-3: OrderController.java → OrderFacade.java
# propagation_depth: 3
```

---

## `review-pr` — behavior-aware PR analysis

Extracts **execution paths**: ordered chains from entry point through service to data access layer, with runtime signals anchored to the specific step where they affect behavior.

```bash
sourcecode prepare-context review-pr [PATH] --since REF
# or against uncommitted working-tree changes:
sourcecode prepare-context review-pr
```

**`execution_paths` schema:**

```json
{
  "execution_paths": [
    {
      "name": "Order",
      "entry_point": {
        "step": "OrderController.createOrder",
        "notes": ["condition: authorization check present (@PreAuthorize / @Secured)"]
      },
      "path": [
        {
          "step": "ShippingService.process",
          "notes": [
            "branch: Spring cache may short-circuit downstream call",
            "async: runs in separate thread (@Async)"
          ]
        },
        {
          "step": "OrderRepository.save",
          "notes": []
        }
      ],
      "end_state": "DB write"
    }
  ]
}
```

**Path rules:**

- One path per changed entry point — most-evident downstream call, not all branches
- Each step requires direct code evidence: field injection, constructor param, method call, or type annotation
- `notes` are scanned from that step's own source file — no cross-contamination between steps
- Path terminates where evidence ends; no gap-filling by naming convention or module similarity

**Runtime signals detected per step:**

| Signal | Example code | Note emitted |
|--------|-------------|--------------|
| Auth guard | `@PreAuthorize`, `@Secured`, `isAuthenticated()` | `condition: authorization check present` |
| Feature flag | `featureFlag.isEnabled()`, `FeatureToggle` | `condition: feature flag gates execution` |
| Null/empty guard | `if (x == null) return` | `condition: null/empty guard with early return` |
| Spring cache | `@Cacheable`, `@CacheEvict` | `branch: Spring cache may short-circuit downstream call` |
| Optional absence | `Optional<>`, `.orElseThrow()` | `branch: result may be absent (Optional)` |
| Async thread | `@Async`, `CompletableFuture` | `async: runs in separate thread (@Async)` |
| Event publishing | `publishEvent()`, `applicationEventPublisher` | `async: Spring application event emitted` |
| Kafka | `kafkaTemplate.send()` | `async: Kafka message produced` |
| RabbitMQ | `rabbitTemplate.send()` | `async: RabbitMQ message sent` |

**Other `review-pr` output fields:**

| Field | Description |
|-------|-------------|
| `review_hotspots` | Top changed files ranked by impact score |
| `suggested_review_order` | Security → API → Service → Persistence → Config |
| `security_impact` | Changed files touching the security surface |
| `transactional_impact` | Files crossing transaction boundaries |
| `test_coverage_risk` | Changed source files with no corresponding test |
| `affected_modules` | DDD domain modules touched by the change |

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

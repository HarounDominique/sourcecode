# sourcecode

**AI-ready change intelligence for Java/Spring enterprise monoliths.**

![Version](https://img.shields.io/badge/version-1.31.16-blue)
![Python](https://img.shields.io/badge/python-3.10%2B-green)

---

## What is it?

`sourcecode` analyzes a Java/Spring repository and produces structured JSON designed to be fed directly to AI agents or used in CI/CD pipelines. It solves two hard problems:

**1. "What breaks if I change X?"** — `sourcecode impact ClassName /repo` traverses the reverse dependency graph and returns every HTTP endpoint, transactional boundary, and downstream module affected by a change. In seconds, not hours.

**2. "What does this codebase do before I touch it?"** — `sourcecode onboard /repo` produces a bounded, AI-ready context bundle: entry points, architecture, key files, confidence, and gaps. Feed it directly to Claude/GPT-4 as a system prompt.

**Optimized for:** Spring Boot / Spring MVC monoliths. Works with JAX-RS (Quarkus, Jersey) at ~65% endpoint recall. Works on any codebase for stack detection and onboarding context.

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
# sourcecode 1.31.16
```

---

## Quickstart

```bash
# High-signal summary (typically 2000–4000 tokens depending on repo size)
sourcecode --compact

# Add git hotspots and uncommitted file count
sourcecode --compact --git-context

# Structured output for AI agents — bounded, noise-free, ready to inject
sourcecode --agent

# Blast radius: what breaks if this class changes?
sourcecode impact OrderService /path/to/repo

# REST endpoint surface
sourcecode endpoints /path/to/repo

# Onboard to an unfamiliar codebase
sourcecode onboard /path/to/repo

# PR review: risk, test gaps, changed modules
sourcecode review-pr /path/to/repo --since main

# Bug triage: risk-ranked files by symptom
sourcecode fix-bug /path/to/repo --symptom "NullPointerException in checkout"
```

---

## Real-world benchmarks

Measured against open-source enterprise Java repos:

| Repo | Classes | Cold scan (`--compact`) | Cache hit | Endpoints found |
|------|---------|------------------------|-----------|----------------|
| BroadleafCommerce | ~2970 | 2.6s | 0.26s | 130 |
| Keycloak | ~6363 | 8.4s | 0.27s | 693 |

Cache speedup: **30x**. The cache is keyed on file content hashes — invalidated only when source changes.

**`impact` on a high-fan-in class:**  
For hub interfaces (2000+ direct dependents), use `--depth 1` — it gives you the direct endpoints in 12s. Default depth=4 can take 90+ seconds on very large repos.

---

## Flags reference

| Flag | Alias | Default | Description |
|------|-------|---------|-------------|
| `--compact` | | off | High-signal summary (typically 2000–4000 tokens): stacks, entry points, dependencies, confidence, gaps. Includes `mybatis` and `transactional_boundaries` for Java projects. |
| `--agent` | | off | Structured JSON for AI agents: project identity, entry points, architecture, dependencies, confidence. More detail than `--compact`. ~4500–5500 tokens. |
| `--full` | | off | Remove truncation limits on `transactional_boundaries`, `mybatis.dto_mappers`, and other capped lists. |
| `--git-context` | `-g` | off | Include git activity: recent commits, change hotspots, and uncommitted file count. |
| `--changed-only` | | off | Limit output to git-modified files (staged, unstaged, untracked). |
| `--depth` | | `4` | File tree traversal depth (1–20). Java/Maven projects auto-adjust to 12. |
| `--format` | `-f` | `json` | Output format: `json` or `yaml`. |
| `--output` | `-o` | stdout | Write output to a file instead of stdout. |
| `--no-cache` | | off | Bypass scan cache and force a fresh analysis. |
| `--copy` | `-c` | off | Copy output to clipboard after a successful run. |
| `--no-redact` | | off | Disable automatic secret redaction. |
| `--version` | `-v` | — | Show version and exit. |

---

## `impact` — Blast-radius analysis

**Who calls this class, and what breaks if it changes?**

```bash
sourcecode impact ClassName /path/to/repo
sourcecode impact org.example.OrderService /path/to/repo   # FQN also accepted
sourcecode impact OrderService . --depth 2                 # limit BFS depth
```

**Output fields:**

| Field | Description |
|-------|-------------|
| `direct_callers` | Classes that directly import or inject the target |
| `indirect_callers` | Transitive callers up to `--depth` (default: 4) |
| `endpoints_affected` | HTTP endpoints whose call chain includes the target |
| `transactional_boundaries_touched` | `@Transactional` classes in the blast cone |
| `mappers_affected` | `@Repository` / `@Mapper` / DAO classes in the blast cone |
| `security_surface_affected` | Security policies on affected endpoints |
| `cross_module_impact` | Subsystems touched, ordered by affected symbol count |
| `risk_score` | 0–100 quantified change risk |
| `confidence_score` | 0–1 confidence in the analysis |
| `explanation` | Human-readable risk summary |
| `candidates` | On partial match: up to 10 FQNs ranked by relevance |

**Best practices:**
- Target **interfaces**, not implementations: `impact OrderService` > `impact OrderServiceImpl`. Callers depend on the interface contract, not the Impl.
- Use `--depth 1` when the target has 200+ callers — direct endpoints are already the most actionable signal.
- The cache applies to the underlying IR scan — second `impact` run on the same repo is significantly faster.

**Supported targets:**
- Simple class name: `OrderService`
- Fully-qualified name: `org.broadleafcommerce.core.order.service.OrderService`
- File path: `src/main/java/.../OrderService.java`

---

## `endpoints` — REST API surface

```bash
sourcecode endpoints /path/to/repo
sourcecode endpoints /path/to/repo --output endpoints.json
```

Extracts all Spring MVC (`@GetMapping`, `@PostMapping`, `@RequestMapping`, etc.) and JAX-RS (`@GET`, `@POST`, `@Path`) endpoint methods. Returns HTTP method, path, controller class, and handler method.

**Scope limitations:**
- JAX-RS subresource locators (endpoints mounted dynamically without class-level `@Path`) are not counted as standalone endpoints — they appear in `impact` output when transitively reached.
- Security context on endpoints reflects method-level annotations (`@PreAuthorize`, `@Secured`). Class-level or programmatic security shows as `no_security_signal`.

---

## `repo-ir` — Symbol-level IR

```bash
sourcecode repo-ir /path/to/repo
sourcecode repo-ir /path/to/repo --summary-only          # compact: analysis + impact, no full graph
sourcecode repo-ir /path/to/repo --since HEAD~1           # symbol-level diff
sourcecode repo-ir /path/to/repo --max-nodes 200 --max-edges 500
```

Builds a deterministic symbol graph: classes, methods, import/injection edges, Spring roles, subsystems. Output is JSON with `graph`, `reverse_graph`, `impact`, `subsystems`, and `route_surface`.

---

## `onboard` — [OSS Core] Codebase orientation

```bash
sourcecode onboard /path/to/repo
```

Entry points, architecture summary, key files, confidence level, and gaps. Designed to be injected as AI agent context at the start of a session.

---

## `review-pr` — [Pro] PR review context

```bash
sourcecode review-pr /path/to/repo --since main
sourcecode review-pr /path/to/repo --since HEAD~3
```

Changed files, risk ranking, test coverage gaps, affected modules, and blast radius of changed classes. Returns a structured `ci_decision` field for CI/CD integration.

**Test coverage note:** Coverage gaps are detected by stem matching (e.g. `OrderService.java` ↔ `OrderServiceTest.java`). Tests in the same diff are counted.

---

## `fix-bug` — [Pro] Bug triage context

```bash
sourcecode fix-bug /path/to/repo --symptom "NullPointerException in checkout"
```

Risk-ranked file list correlated to the symptom: keyword extraction, path matching, content matching, and git commit correlation. Output includes `symptom_explain` with the full evidence chain.

---

## `modernize` — [Pro] Modernization planning

```bash
sourcecode modernize /path/to/repo
```

Identifies high-coupling nodes (high fan-in = risky to change), dead zone candidates (isolated symbols), and subsystem tangles. 

**Interpreting output:** `hotspot_candidates` is a subset of `high_coupling_nodes` filtered to service/repository/controller roles. In annotation-heavy codebases, the highest-coupled nodes are often annotation types or JPA entities — check `high_coupling_nodes` directly for the full coupling picture.

---

## `prepare-context` — Task-specific context

Low-level access to all tasks with full options:

```bash
sourcecode prepare-context TASK [PATH] [OPTIONS]
```

| Task | What it surfaces |
|------|-----------------|
| `explain` | Architecture, entry points, key dependencies |
| `onboard` | Full structural context for new agents/developers |
| `fix-bug` | Files ranked by symptom correlation, risk, annotations |
| `refactor` | Structural issues, improvement opportunities |
| `generate-tests` | Source files without test pairs, coverage gap analysis |
| `review-pr` | PR diff with risk ranking, test gaps, module impact |
| `delta` | Incremental context: git-changed files + transitive import graph |

```bash
sourcecode prepare-context fix-bug --symptom "NullPointerException in OrderService"
sourcecode prepare-context review-pr --since main --format github-comment
sourcecode prepare-context onboard --llm-prompt
sourcecode prepare-context --task-help    # list all tasks
```

Note: `sourcecode onboard`, `sourcecode fix-bug`, `sourcecode review-pr`, and `sourcecode modernize` are shorthand aliases for the corresponding `prepare-context` tasks — output is identical.

---

## How to use sourcecode effectively

### With AI agents (Claude, GPT-4, etc.)

```bash
# Inject bounded context at session start:
sourcecode /repo --agent | paste-to-agent

# For a change task:
sourcecode impact PaymentService /repo --depth 1 | ask-agent "What are the risks?"

# For PR review:
sourcecode review-pr /repo --since main | ask-agent "Summarize architectural risks"
```

### In CI/CD pipelines

```bash
# Always-fresh bounded JSON — deterministic, cacheable by content hash
sourcecode /repo --compact --no-cache --format json --output context.json

# PR gate — parse ci_decision field
sourcecode review-pr /repo --since $BASE_REF --output review.json
jq '.ci_decision' review.json    # "analysis_success" | "git_ref_error" | etc.
```

### For debugging production issues

```bash
# Correlate symptom to files
sourcecode fix-bug /repo --symptom "NullPointerException in PaymentProcessor when cart is empty"
# → ranked list of files, suspected areas, git commits touching relevant code
```

### For understanding blast radius

```bash
# Interface targets give the most complete signal
sourcecode impact OrderService /repo --depth 2
# → shows all endpoints, transaction boundaries, and modules affected

# On very large repos or hub classes, depth=1 is faster and still actionable
sourcecode impact KeycloakSession /repo --depth 1
```

---

## What sourcecode does NOT do

- No runtime analysis — all signals are static (annotation, import graph, file structure)
- No semantic code understanding — it reads structure, not logic
- Architecture pattern detection works best for Spring MVC layered apps; SPI/plugin architectures (e.g. Quarkus extension model) are classified as "layered" which may be inaccurate
- Endpoint recall for JAX-RS subresource locator pattern is ~65% — endpoints mounted dynamically via factory methods are not individually counted
- `impact` on implementation classes (e.g. `OrderServiceImpl`) reflects callers of the implementation specifically, which is often zero if callers use the interface — prefer targeting the interface

---

## Output schema

All outputs include:
- `schema_version`: output format version
- `confidence_summary`: `overall`, `stack`, `entry_points` confidence levels (`high`/`medium`/`low`)
- `analysis_gaps`: list of what could not be analyzed and why

### Java/Spring-specific fields (when detected)

| Field | Description |
|-------|-------------|
| `language_version` | Java version from `maven.compiler.source` or equivalent |
| `deployment.spring_boot_version` | Spring Boot version |
| `deployment.packaging` | `jar` or `war` |
| `mybatis` | Mapper interface / XML file pairing summary |
| `transactional_boundaries` | Classes annotated with `@Transactional` |
| `deployment_risks` | Static risk flags: `spring-boot-2.x-eol`, `legacy-java-runtime` |

---

## Telemetry

Anonymous, opt-in. Collects: version, OS, commands, flags, duration, repo size range, errors. No source code, paths, secrets, or output content.

```bash
sourcecode telemetry status
sourcecode telemetry enable
sourcecode telemetry disable
```

Or: `export SOURCECODE_TELEMETRY=0`

---

## Configuration

```bash
sourcecode config    # show version, config file path, telemetry status
```

# sourcecode

**AI-ready change intelligence for Java/Spring enterprise monoliths.**

![Version](https://img.shields.io/badge/version-1.31.17-blue)
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
# sourcecode 1.31.17
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

| Repo | Java files | Cold scan (`--compact`) | Cache hit | Cache speedup | Endpoints found |
|------|-----------|------------------------|-----------|---------------|----------------|
| BroadleafCommerce | 2,985 | 2.9s | 0.20s | ~13x | 130 |
| Keycloak | 7,885 | 9.0s | 0.27s | ~33x | 693 |

The cache is keyed on file content hashes — invalidated only when source changes. Speedup varies by repo size and OS I/O.

**Token sizes (measured):**

| Mode | BroadleafCommerce | Keycloak |
|------|------------------|---------|
| `--compact` | ~2,900 | ~4,000 |
| `--agent` | ~4,800 | ~5,500 |
| `onboard` | ~2,600 | n/a |
| `fix-bug` (trimmed) | ~27,000 | ~4,600 |

**`impact` on high-fan-in classes:**  
For hub interfaces (1000+ direct dependents), use `--depth 1` — direct endpoints are already the most actionable signal. Depth=4 on very large repos may take 90+ seconds.

---

## Flags reference

| Flag | Alias | Default | Description |
|------|-------|---------|-------------|
| `--compact` | | off | High-signal summary (typically 2,500–4,000 tokens for mid-to-large Java repos): stacks, entry points, dependencies, confidence, gaps. Includes `transactional_boundaries` for Spring projects. |
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
- Target **interfaces**, not implementations: `impact OrderService` > `impact OrderServiceImpl`. In Spring projects, callers inject the interface via `@Autowired` — the impl has zero direct callers in the graph even though it runs all the code. Querying the impl returns `direct_callers: []` with no error; querying the interface returns the real blast radius.
- Use `--depth 1` when the target has 200+ callers — direct endpoints are already the most actionable signal.
- The cache applies to the underlying IR scan — second `impact` run on the same repo is significantly faster.
- When you get `direct_callers: 0` for a `@Service` or `@Repository` class, that is almost certainly the interface-injection pattern. Re-run with the interface name.

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
sourcecode repo-ir /path/to/repo --summary-only          # recommended: analysis + impact, no full graph (~20K tokens)
sourcecode repo-ir /path/to/repo --since HEAD~1           # symbol-level diff
sourcecode repo-ir /path/to/repo --files src/.../OrderService.java   # single-file IR
sourcecode repo-ir /path/to/repo --max-nodes 200 --max-edges 500     # limits forward graph only — see note below
```

Builds a deterministic symbol graph: classes, methods, import/injection edges, Spring roles, subsystems. Output is JSON with `graph`, `reverse_graph`, `impact`, `subsystems`, and `route_surface`.

**Size warning:** Without `--summary-only`, output can exceed 1MB for mid-size repos. `--max-nodes`/`--max-edges` limit the forward `graph` section only — the `reverse_graph` section is not bounded by these flags and is the largest component. Always use `--summary-only` unless you need the full graph for downstream tooling.

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

### Onboarding — new repo, new agent session

```bash
# Bounded context at session start (~2,500–5,500 tokens)
sourcecode /repo --compact              # fast overview
sourcecode /repo --agent               # more detail: file relevance, architecture, event flows
sourcecode onboard /repo               # task-structured: entry points, key files, gaps
```

Use `--compact` or `--agent` as first-prompt injection for AI coding agents. Both are bounded and deterministic.

### Impact analysis — before touching a class

```bash
# Always target the INTERFACE in Spring projects:
sourcecode impact OrderService /repo           # ✓ correct: 30 callers, 11 endpoints
sourcecode impact OrderServiceImpl /repo       # ✗ wrong: 0 callers (Spring DI blindness)

# Large hub interfaces — depth=1 is faster and still actionable:
sourcecode impact KeycloakSession /repo --depth 1

# If you get direct_callers:[] for a @Service class, re-query the interface.
```

### Bug triage — symptom-driven

```bash
# Specific symptoms produce the best signal:
sourcecode fix-bug /repo --symptom "OIDC token refresh fails after realm update"
sourcecode fix-bug /repo --symptom "NullPointerException in OrderService during checkout"

# Generic symptoms produce noisy output (100s of files) — be specific.
# Use --output to capture full output without budget truncation.
sourcecode fix-bug /repo --symptom "payment timeout" --output triage.json
```

### PR review

```bash
# JSON for programmatic use:
sourcecode review-pr /repo --since main --output review.json
jq '.ci_decision' review.json    # "analysis_success" | "git_ref_error"

# Markdown for GitHub comment:
sourcecode review-pr /repo --since main --format github-comment

# CI/CD gate — parse risk and test coverage fields:
jq '{ci_decision, test_coverage_risk, impact_summary}' review.json
```

### Modernization planning

```bash
sourcecode modernize /repo
# high_coupling_nodes: classes most risky to change (by fan-in degree)
# dead_zone_candidates: classes with zero callers — safe to remove or refactor
# Note: hotspot_candidates may be empty in annotation-heavy codebases —
#       check high_coupling_nodes directly for coupling signal.
```

### Symbol IR for downstream tooling

```bash
# Always use --summary-only unless you need the full graph:
sourcecode repo-ir /repo --summary-only --output ir.json   # ~20K tokens
sourcecode repo-ir /repo --since HEAD~3 --summary-only     # changed symbols only

# Full graph warning: output can exceed 1MB for mid-size repos.
# --max-nodes/--max-edges only limit the forward graph, not reverse_graph.
```

### With AI agents (Claude, GPT-4, etc.)

```bash
# Start agent session with bounded context:
sourcecode /repo --agent --output context.json && cat context.json | agent-cli

# For a specific change task, combine context + impact:
sourcecode /repo --compact > context.json
sourcecode impact PaymentService /repo --depth 1 >> impact.json
# Feed both to agent: "Given this context and impact, what are the risks of changing PaymentService?"

# For PR review:
sourcecode review-pr /repo --since main --format github-comment
# Paste directly into GitHub PR description or feed to agent
```

### In CI/CD pipelines

```bash
# Deterministic, content-hash cached — safe to run on every commit
sourcecode /repo --compact --no-cache --output context.json

# PR gate
sourcecode review-pr /repo --since $BASE_REF --output review.json
DECISION=$(jq -r '.ci_decision' review.json)
if [ "$DECISION" != "analysis_success" ]; then echo "Review failed: $DECISION"; fi
```

---

## What sourcecode does NOT do

- No runtime analysis — all signals are static (annotation, import graph, file structure)
- No semantic code understanding — it reads structure, not logic
- Architecture pattern detection works best for Spring MVC layered apps; SPI/plugin architectures (e.g. Quarkus extension model) are classified as "layered" which may be inaccurate
- Endpoint recall for JAX-RS subresource locator pattern is ~65% — endpoints mounted dynamically via factory methods are not individually counted. JAX-RS sub-resource paths (method-level `@Path` inside a `@Path`-annotated class) are extracted as relative paths, not the fully composed URL.
- `impact` on implementation classes (e.g. `OrderServiceImpl`) reflects callers of the implementation specifically — **in Spring Boot projects this is almost always zero**, because callers inject the interface via `@Autowired`. Always target the interface (`OrderService`) to get the real blast radius. The tool does not auto-resolve impl → interface. When `direct_callers: []` is returned with `confidence_level: high` for a `@Service` class, treat it as a prompt to re-query the interface.
- `no_security_signal` on endpoints means no method-level security annotations (`@PreAuthorize`, `@Secured`) were found — it does **not** mean the endpoint is unsecured. Projects using Spring Security filter chains, XML security config, or custom filters will show 100% `no_security_signal` even when fully secured.
- `hotspot_candidates` in `modernize` output reflects graph coupling, not git churn — in annotation-heavy codebases it is often empty even though real hotspots exist. Check `high_coupling_nodes` directly for the coupling picture.
- `project_summary` is extracted from the repository README — it may reflect marketing language rather than architectural description

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

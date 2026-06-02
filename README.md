# sourcecode

**Persistent structural context and ultra-fast repeated analysis for AI coding agents.**

![Version](https://img.shields.io/badge/version-1.33.25-blue)
![Python](https://img.shields.io/badge/python-3.10%2B-green)

---

## The problem

Every time an AI coding agent starts a new session, it has to re-parse the repository from scratch. For a large Java or TypeScript monolith, that means 5–15 seconds per invocation. Multiply by dozens of agent turns per hour, and repo context acquisition becomes a real bottleneck — not just latency, but tokens, compute, and iteration velocity.

`sourcecode` solves this with a persistent structural cache keyed on file content hashes. After the first scan, every subsequent invocation returns pre-built context in milliseconds. The repo doesn't change? The cache doesn't expire.

**The cache is not a performance optimization. It is what makes sourcecode usable as infrastructure rather than a one-off tool.**

---

## Cache performance — measured on real repos

| Repo | Size | Cold scan | Cache hit | Speedup |
|------|------|-----------|-----------|---------|
| Keycloak | 7,885 Java files | 10.5s | 0.6s | **~17x** |
| BroadleafCommerce | 2,985 Java files | 2.7s | 0.3s | **~9x** |

Cache keyed on content hashes — invalidated only when source changes. On repeated agent sessions against the same codebase, nearly every invocation is a cache hit.

**Token output (measured):**

| Mode | BroadleafCommerce | Keycloak |
|------|------------------|---------|
| `--compact` | ~2,900 | ~4,000 |
| `--agent` | ~4,800 | ~5,500 |
| `onboard` | ~2,600 | n/a |
| `fix-bug` (trimmed) | ~27,000 | ~4,600 |

---

## What changes at 0.3s vs 2.7s

At 2.7s per call, you use sourcecode to occasionally inspect a repo.

At 0.3s per call, you use sourcecode as **constant infrastructure** inside agent loops:

```
agent loop iteration:
  1. sourcecode . --compact          # 0.3s — instant structural context
  2. sourcecode impact PaymentService . --depth 1   # 0.4s — blast radius check
  3. agent makes targeted change
  4. repeat
```

Sub-second context retrieval changes the cost model for agent workflows. You can call sourcecode before every edit, before every PR review, before every test run — without batching or caching calls manually.

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
# sourcecode 1.33.4
```

---

## Quickstart

```bash
# High-signal summary — warm cache: ~0.3s, cold: 2–10s depending on repo size
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

## Cache system

sourcecode maintains a persistent cache at `.sourcecode-cache/` inside each repository. Two layers:

- **L1 (core):** analysis result keyed by `(git_sha, analysis_flags)`. Survives format changes — you can regenerate `--compact` vs `--agent` views from the same core.
- **L2 (view):** rendered output keyed by `(core_hash, view_flags)`. Exact output match — no recomputation.

**Lookup order:** L2 exact hit → L1 hit + view rebuild → full cold scan

**Cache invalidation:** Keyed on git commit SHA. Any commit invalidates the core cache for that repo. Uncommitted changes are not cached.

```bash
# Inspect cache state
sourcecode cache status

# Warm the cache ahead of an agent session
sourcecode cache warm

# Clear cache
sourcecode cache clear
```

**`--no-cache`** bypasses both layers and forces a fresh scan. Use in CI or when you need to verify a fresh result.

**Visibility:** Cache hits are silent. Use `sourcecode cache status` to see cache size, hit keys, and last-warmed timestamp.

---

## Agent workflow patterns

### Start of session — structural grounding

```bash
# Inject as first message to agent (bounded, deterministic)
sourcecode /repo --compact              # ~2,500–4,000 tokens
sourcecode /repo --agent               # ~4,500–5,500 tokens — more detail
sourcecode onboard /repo               # task-structured: entry points, key files, gaps
```

### Before every change — blast radius check

```bash
# Always target the INTERFACE in Spring projects, not the implementation:
sourcecode impact OrderService /repo           # ✓ 30 callers, 11 endpoints
sourcecode impact OrderServiceImpl /repo       # ✗ 0 callers (Spring DI blindness)

# Large hub interfaces — depth=1 is faster and still the most actionable signal:
sourcecode impact KeycloakSession /repo --depth 1
```

### Continuous agent loop — delta context

```bash
# Only changed files + their transitive importers — minimal token cost:
sourcecode prepare-context delta /repo --since HEAD~1
sourcecode . --changed-only --git-context
```

### PR review — structured risk signal

```bash
# JSON for programmatic use:
sourcecode review-pr /repo --since main --output review.json
jq '.ci_decision' review.json    # "analysis_success" | "git_ref_error"

# Markdown for GitHub comment:
sourcecode review-pr /repo --since main --format github-comment
```

### Bug triage — symptom-driven

```bash
# Specific symptoms produce the best signal:
sourcecode fix-bug /repo --symptom "OIDC token refresh fails after realm update"
sourcecode fix-bug /repo --symptom "NullPointerException in OrderService during checkout"

# Generic symptoms produce noisy output — be specific.
sourcecode fix-bug /repo --symptom "payment timeout" --output triage.json
```

### In CI — cached, deterministic, fast

```bash
# Content-hash cached — safe to run on every commit; cold only when code changes
sourcecode /repo --compact --output context.json

# PR gate
sourcecode review-pr /repo --since $BASE_REF --output review.json
DECISION=$(jq -r '.ci_decision' review.json)
if [ "$DECISION" != "analysis_success" ]; then echo "Review failed: $DECISION"; fi
```

---

## What sourcecode does (and doesn't)

**sourcecode reduces exploration cost.** It accelerates context acquisition and minimizes repeated repo parsing. It does not replace reading code — it reduces how often an agent needs to.

Specifically:

- Extracts structural signals: entry points, Spring roles, REST surfaces, dependency graphs, transactional boundaries
- Builds and caches these on first scan; serves from cache on subsequent calls
- Produces bounded, noise-free JSON designed for direct injection into agent context windows
- Computes blast radius (impact graph) from a class or interface, traversing reverse dependencies

**What it does NOT do:**

- No runtime analysis — all signals are static (annotation, import graph, file structure)
- No semantic code understanding — reads structure, not logic
- No replacement for reading code — reduces how often that's needed, not whether
- Architecture pattern detection best for Spring MVC layered apps; SPI/plugin architectures (e.g. Quarkus extension model) may be misclassified
- Endpoint recall for JAX-RS subresource locator pattern is ~65%
- `impact` on implementation classes (e.g. `OrderServiceImpl`) returns 0 callers in Spring Boot — callers inject the interface via `@Autowired`. Always target the interface. When `direct_callers: []` with `confidence_level: high` for a `@Service` class, re-query the interface.
- `no_security_signal` on endpoints means no method-level annotations found — does **not** mean the endpoint is unsecured. Projects using Spring Security filter chains show 100% `no_security_signal` even when fully secured.

---

## Command reference

### `--compact` and `--agent`

Core flags. Feed directly to AI agents as first-message context.

| Flag | Output | Tokens |
|------|--------|--------|
| `--compact` | High-signal summary: stacks, entry points, dependencies, confidence, gaps | ~2,500–4,000 |
| `--agent` | Structured JSON: identity, entry points, architecture, event flows | ~4,500–5,500 |

### `impact` — blast-radius analysis

```bash
sourcecode impact ClassName /path/to/repo
sourcecode impact org.example.OrderService /path/to/repo   # FQN also accepted
sourcecode impact OrderService . --depth 2                 # limit BFS depth
```

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
- Target **interfaces**, not implementations: `impact OrderService` > `impact OrderServiceImpl`
- Use `--depth 1` when target has 200+ callers — direct endpoints are already the most actionable signal
- Second `impact` run on the same repo is significantly faster (cache applies to underlying IR scan)

### `endpoints` — REST API surface

```bash
sourcecode endpoints /path/to/repo
sourcecode endpoints /path/to/repo --output endpoints.json
```

Extracts all Spring MVC (`@GetMapping`, `@PostMapping`, `@RequestMapping`, etc.) and JAX-RS (`@GET`, `@POST`, `@Path`) endpoint methods. Returns HTTP method, path, controller class, and handler method.

### `repo-ir` — symbol-level IR

```bash
sourcecode repo-ir /path/to/repo --summary-only          # ~20K tokens
sourcecode repo-ir /path/to/repo --since HEAD~1           # symbol-level diff
sourcecode repo-ir /path/to/repo --files src/.../OrderService.java
```

Builds a deterministic symbol graph: classes, methods, import/injection edges, Spring roles, subsystems.

**Size warning:** Without `--summary-only`, output can exceed 1MB for mid-size repos. Always use `--summary-only` unless you need the full graph for downstream tooling.

### `onboard` — codebase orientation

```bash
sourcecode onboard /path/to/repo
```

Entry points, architecture summary, key files, confidence level, and gaps. Designed to be injected as agent context at the start of a session.

### `review-pr` — [Pro] PR review context

```bash
sourcecode review-pr /path/to/repo --since main
sourcecode review-pr /path/to/repo --since HEAD~3
```

Changed files, risk ranking, test coverage gaps, affected modules, and blast radius of changed classes. Returns a `ci_decision` field for CI/CD integration.

### `fix-bug` — [Pro] Bug triage context

```bash
sourcecode fix-bug /path/to/repo --symptom "NullPointerException in checkout"
```

Risk-ranked file list correlated to the symptom: keyword extraction, path matching, content matching, git commit correlation.

### `modernize` — [Pro] Modernization planning

```bash
sourcecode modernize /path/to/repo
```

High-coupling nodes (high fan-in = risky to change), dead zone candidates (isolated symbols), subsystem tangles.

### `prepare-context` — task-specific context

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

---

## Flags reference

| Flag | Alias | Default | Description |
|------|-------|---------|-------------|
| `--compact` | | off | High-signal summary (typically 2,500–4,000 tokens for mid-to-large Java repos): stacks, entry points, dependencies, confidence, gaps. |
| `--agent` | | off | Structured JSON for AI agents: project identity, entry points, architecture, dependencies, confidence. ~4,500–5,500 tokens. |
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

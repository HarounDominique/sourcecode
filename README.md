# sourcecode

**Persistent structural context and ultra-fast repeated analysis for AI coding agents.**

![Version](https://img.shields.io/badge/version-1.35.33-blue)
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
# sourcecode 1.35.34
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

# Spring Boot 2→3 migration readiness: javax→jakarta blockers, removed APIs
sourcecode migrate-check /path/to/repo

# Spring semantic audit: TX anomalies + security surface (free)
sourcecode spring-audit /path/to/repo

# Impact chain: systemic blast radius with TX/SEC enrichment (free)
sourcecode impact-chain OrderService /path/to/repo

# Event topology: publisher → event → consumer graph (free)
sourcecode impact-chain OrderPlacedEvent /path/to/repo --type events

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

# Check RIS freshness relative to current git HEAD
sourcecode cache freshness
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

### Before every change — blast radius + TX/SEC check

```bash
# Always target the INTERFACE in Spring projects, not the implementation:
sourcecode impact OrderService /repo           # ✓ 30 callers, 11 endpoints
sourcecode impact OrderServiceImpl /repo       # ✗ 0 callers (Spring DI blindness)

# Impact chain: blast radius enriched with TX boundary and security surfaces
sourcecode impact-chain OrderService /repo

# Event topology: who publishes/consumes this event, and in what TX phase?
sourcecode impact-chain OrderPlacedEvent /repo --type events

# Spring audit: catch TX anomalies before they hit production
sourcecode spring-audit /repo --scope tx
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
- `spring-audit` and `impact-chain` are **Java/Spring only** — non-Java repos return `spring_detected: false`
- Event topology via `--type events` does not resolve Kafka/RabbitMQ/Redis message routes — only Spring ApplicationEvent and `@EventListener` chains
- Self-invocation TX bypass (calling `@Transactional` method from the same class without going through the proxy) is not detected

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

### `spring-audit` — Spring semantic audit [free]

```bash
sourcecode spring-audit /path/to/repo
sourcecode spring-audit /path/to/repo --scope tx           # TX anomalies only
sourcecode spring-audit /path/to/repo --scope security     # security surface only
sourcecode spring-audit /path/to/repo --min-severity high

# CI/CD gate: exit 1 on any finding
sourcecode spring-audit . --ci
sourcecode spring-audit . --ci --min-severity high         # exit 1 only on high/critical
sourcecode spring-audit . --ci --format github-comment     # Markdown output + exit 1
```

Detects structural Spring anomalies that survive code review and tests, but cause production failures:

| Pattern | Description |
|---------|-------------|
| `TX-001` | `@Transactional` on private/final method — CGLIB proxy bypass, TX silently ignored |
| `TX-002` | `REQUIRES_NEW` nested inside `REQUIRED` call chain — unexpected transaction nesting |
| `TX-003` | `readOnly=true` boundary propagating to write operation |
| `TX-004` | `NOT_SUPPORTED`/`NEVER` called within active TX chain |
| `TX-005` | Exception swallowing inside `@Transactional` — silent TX rollback suppression |
| `SEC-001` | Unsecured endpoint in annotation-based security model |
| `SEC-002` | CVE-2025-41248: `@PreAuthorize` on inherited method from generic supertype |
| `SEC-003` | `@Transactional` on `@Controller`/`@RestController` — TX in wrong layer |

Returns structured findings with `severity`, `confidence`, `symbol`, `source_file`, `evidence`, `explanation`, and `fix_hint`. JAVA/SPRING ONLY.

### `impact-chain` — systemic blast radius with TX/SEC enrichment [free]

```bash
sourcecode impact-chain OrderService /path/to/repo
sourcecode impact-chain com.example.OrderService#placeOrder /path/to/repo
sourcecode impact-chain PaymentService . --depth 6
```

Unlike `impact` (which traces the caller graph), `impact-chain` builds on the SpringSemanticModel to enrich every step of the blast cone with transaction and security context:

| Field | Description |
|-------|-------------|
| `direct_callers` | Symbols that directly call the target |
| `indirect_callers` | Transitive callers (BFS up to `--depth` hops, default: 4) |
| `endpoints_affected` | HTTP endpoints reachable through the call chain |
| `transaction_boundary` | `@Transactional` semantics on the target: propagation, isolation, readOnly |
| `security_surfaces` | Per-endpoint security policy + SEC finding IDs |
| `impact_findings` | TX-001..005 and SEC-001..003 findings that touch the call chain |
| `risk_level` | `critical` \| `high` \| `medium` \| `low` |

**Event topology** — query the publisher/consumer graph for a Spring event class:

```bash
sourcecode impact-chain OrderPlacedEvent /path/to/repo --type events
```

| Field | Description |
|-------|-------------|
| `publishers` | FQNs that publish this event class |
| `consumers` | Listeners with TX phase metadata (`AFTER_COMMIT`, `BEFORE_COMMIT`, etc.) |
| `event_graph` | Publisher → event → consumer edges (BFS ≤ 2 hops) |
| `transaction_context` | `AFTER_COMMIT` consumers, `BEFORE_COMMIT` risks |
| `risk_level` | Derived from TX phase and consumer count |

**Limitations of event topology:**
- Resolves Spring `ApplicationEvent` / `@EventListener` chains only
- Does not trace Kafka, RabbitMQ, Redis, or other message brokers
- Does not detect self-invocation proxy bypass
- Conditional beans (`@ConditionalOnProperty`) are not evaluated at analysis time

### `cold-start` — RIS bootstrap context

```bash
sourcecode cold-start /path/to/repo
sourcecode cold-start /path/to/repo --compact   # ~10K token subset
```

Returns the Repository Intelligence Snapshot (RIS) instantly — zero re-analysis. The RIS is built by a prior warm cache pass and includes stacks, entry points, endpoint surface, and Spring semantic signals. Status field: `cold_start_ready` | `cold_start_stale` | `no_ris`.

Use `--compact` to get a ~10K token subset safe for direct LLM injection. Full snapshot ranges from ~100K–200K tokens on medium repos — use `--output FILE` for local search tooling.

### `repo-ir` — symbol-level IR

```bash
sourcecode repo-ir /path/to/repo --summary-only                  # ~20K tokens
sourcecode repo-ir /path/to/repo --since HEAD~1                   # symbol-level diff
sourcecode repo-ir /path/to/repo --files src/.../OrderService.java
sourcecode repo-ir /path/to/repo --max-nodes 200 --max-edges 500  # limit graph size
sourcecode repo-ir /path/to/repo --output ir.json.gz --gzip       # compressed output (~70-80% smaller)
sourcecode repo-ir /path/to/repo --include-tests                   # include test files
```

Builds a deterministic symbol graph: classes, methods, import/injection edges, Spring roles, subsystems.

**Size control flags:**

| Flag | Description |
|------|-------------|
| `--summary-only` | Omit full graph nodes/edges; keep analysis summary, impact, and change_set (<300KB typical) |
| `--max-nodes N` | Keep top N nodes by impact score |
| `--max-edges N` | Keep top N edges (priority: edges between kept nodes) |
| `--gzip` | Compress output with gzip. Requires `--output`. ~70–80% smaller. |
| `--force` | Bypass the 50K-token size guard and emit output anyway |
| `--include-tests` | Include test source files (excluded by default) |

**Size warning:** Without `--summary-only`, output can exceed 1MB for mid-size repos. Always use `--summary-only` unless you need the full graph for downstream tooling.

### `explain` — architectural summary for a class

```bash
sourcecode explain UserService
sourcecode explain OrderController /path/to/repo
sourcecode explain UserService --format json
```

Human-readable architectural summary derived entirely from static analysis: Spring stereotype, public methods, incoming callers, outgoing dependencies, events published/consumed, `@Transactional` boundaries, security constraints, and related REST endpoints. JAVA/SPRING ONLY.

### `pr-impact` — PR blast-radius report

```bash
sourcecode pr-impact --files changed_files.txt
sourcecode pr-impact /path/to/repo --files diff.txt --format json
```

Takes a file listing changed Java files (one path per line) and produces a consolidated report: modified classes, affected REST endpoints reachable through the call chain, direct callers of each changed class, event publishers/consumers triggered, `@Transactional` methods in changed classes, and a consolidated risk level (`CRITICAL` / `HIGH` / `MEDIUM` / `LOW`). JAVA/SPRING ONLY.

```bash
# Typical CI usage: pipe git diff to a file, then run
git diff --name-only main | grep '\.java$' > changed.txt
sourcecode pr-impact . --files changed.txt --format json
```

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

### `migrate-check` — Spring Boot 2→3 migration readiness

```bash
sourcecode migrate-check /path/to/repo
sourcecode migrate-check . --min-severity high
sourcecode migrate-check . --format text
sourcecode migrate-check . --output migration.json
```

Detects migration blockers across Java source files, Spring XML config files, and Maven/Gradle build files. 27 rules organized by target:

**Jakarta namespace (MIG-001..009) — javax→jakarta**

| Rule | Severity | Pattern |
|------|----------|---------|
| `MIG-001` | critical | `javax.persistence` import — JPA will not compile |
| `MIG-002` | high | `javax.servlet` import — Servlet API changed |
| `MIG-003` | high | `javax.validation` import — Bean Validation changed |
| `MIG-004` | high | `javax.transaction` import — TX API changed |
| `MIG-006` | medium | `javax.annotation` import — CDI annotations changed |
| `MIG-007` | medium | `javax.inject` import — DI annotations changed |
| `MIG-008` | medium | `javax.ws.rs` import — JAX-RS changed |
| `MIG-009` | medium | `javax.jms` import — JMS API changed |

**Spring Security 6 (MIG-005, MIG-019, MIG-020)**

| Rule | Severity | Pattern |
|------|----------|---------|
| `MIG-005` | high | `extends WebSecurityConfigurerAdapter` — removed in Spring Security 6 |
| `MIG-019` | high | SpringFox / `@EnableSwagger2` — incompatible with Spring Boot 3 |
| `MIG-020` | high | `antMatchers()` / `authorizeRequests()` — replaced in Spring Security 6 |

**Java version compatibility (MIG-010..025)**

| Rule | Severity | Pattern |
|------|----------|---------|
| `MIG-010` | critical | `SecurityManager` / `AccessController` — removed in Java 17 (JEP 411) |
| `MIG-011` | high | `sun.*` / `com.sun.net.*` internal API imports — strong encapsulation since Java 9 |
| `MIG-012` | high | Nashorn `ScriptEngine` — removed in Java 15 |
| `MIG-013` | high | `sun.misc.Unsafe` — requires `--add-opens` on Java 9+ |
| `MIG-014` | medium | `setAccessible(true)` — may throw `InaccessibleObjectException` on Java 17+ |
| `MIG-015` | medium | `finalize()` override — deprecated for removal since Java 18 |
| `MIG-016` | low | `java.util.Date` / `Calendar` / `SimpleDateFormat` — use `java.time` |
| `MIG-021` | high | `javax.xml.bind` (JAXB) — removed from JDK in Java 11 |
| `MIG-022` | high | `javax.xml.ws` (JAX-WS) — removed from JDK in Java 11 |
| `MIG-023` | critical | `org.omg.*` / CORBA APIs — removed from JDK in Java 11 |
| `MIG-024` | medium | `Thread.stop()` / `Thread.suspend()` / `Thread.resume()` — deprecated for removal |
| `MIG-025` | medium | `ReflectionFactory` / `MethodHandles.privateLookupIn` — JPMS deep-reflection risk |

**Spring XML config (MIG-030..032)**

| Rule | Severity | Pattern |
|------|----------|---------|
| `MIG-030` | high | `javax.*` class reference in Spring XML bean definitions |
| `MIG-031` | high | `<http auto-config>` or versioned spring-security ≤5 schema in XML |
| `MIG-032` | high | `web.xml` with Servlet ≤4 namespace — must migrate to `jakarta.ee` |

**Build file dependencies (MIG-040..043)**

| Rule | Severity | Pattern |
|------|----------|---------|
| `MIG-040` | high | `io.springfox` dependency — incompatible with Spring Boot 3 |
| `MIG-041` | high | Hibernate 5.x explicitly pinned — Spring Boot 3 requires Hibernate 6 |
| `MIG-042` | medium | ByteBuddy < 1.12.x — may not support Java 17+ strong encapsulation |
| `MIG-043` | high | EhCache 2.x (`net.sf.ehcache`) — incompatible with Spring Boot 3 |

Each finding includes `severity`, `title`, `source_file`, `first_line`, `explanation`, `fix_hint`, `migration_target`, and `openrewrite_recipe` (when an automated recipe exists).

### `rename-class` — Java class rename

```bash
sourcecode rename-class . --from ServiceA --to ServiceB
sourcecode rename-class /path/to/repo --from OrderManager --to OrderService
sourcecode rename-class . --from OldName --to NewName --dry-run
sourcecode rename-class . --from OldName --to NewName --no-tests   # src/main only
```

Renames a Java class safely throughout the repository: declaration, constructor, all import statements, type references (fields, params, return types), `extends`/`implements`, generics, casts, and Spring `@Qualifier` names. Renames the physical `.java` file. Emits a structured change audit trail (`file`, `before_lines`, `after_lines`, `intent`, `diff`).

Use `--dry-run` to preview changes without writing to disk.

### `chunk-file` — split large Java files for agent consumption

```bash
sourcecode chunk-file BigService.java
sourcecode chunk-file BigService.java --max-lines 300
sourcecode chunk-file BigService.java --chunk 5          # read chunk 5 only
sourcecode chunk-file BigService.java --metadata-only    # boundaries only, no content
```

Splits a large Java file at method/class boundaries so AI agents can read files with 10K–25K+ lines in context-sized pieces. Each chunk includes `chunk_id`, `start_line`, `end_line`, `chunk_type`, symbol name, a `context_header` (package + class + imports summary), and `content`. A `size_warning` flag marks methods that exceed `--max-lines` and cannot be split further.

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

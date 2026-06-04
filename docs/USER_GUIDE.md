# sourcecode — User Guide

**sourcecode** analyzes Java/Spring/Maven repositories and produces structured JSON for AI coding agents and CI/CD pipelines. It answers: *what breaks if I change X*, *what TX and security anomalies exist today*, and *what does this codebase do*.

---

## What is it

A local CLI. No API keys. No network calls. No account required.

It builds a deterministic symbol graph (classes, annotations, injection edges, HTTP routes) from your repository's source files and answers structural questions about that graph. All analysis is static — it reads code, not runtime behavior.

**Optimized for:** Spring Boot / Spring MVC monoliths. JAX-RS (Quarkus, Jersey) endpoint extraction at ~65% recall. Stack detection works on any codebase.

---

## Installation

```bash
# macOS / Linux via Homebrew (recommended)
brew tap haroundominique/sourcecode
brew install sourcecode

# pip / pipx
pip install sourcecode
pipx install sourcecode   # isolated install, no venv needed

# Verify
sourcecode version
# sourcecode 1.35.4
```

Requires Python 3.10+.

---

## License activation (Pro features)

Free features work immediately after install. Pro commands (`impact` full output, `review-pr`, `fix-bug`, `modernize`) require an active license. `spring-audit`, `impact-chain`, `endpoints`, `onboard`, `repo-ir`, and `cold-start` are free.

```bash
sourcecode activate SC-XXXX-XXXX-XXXX
```

On success:

```json
{"status": "activated", "plan": "pro", "features": ["impact", "review-pr", "fix-bug", "modernize", "generate-tests"]}
```

License is cached in `~/.sourcecode/license.json` and re-validated every 24 hours. Works offline after first activation — network errors keep the cached state.

---

## Core commands

### `sourcecode --compact`

High-signal structural summary. Use at the start of any AI agent session.

```bash
sourcecode /path/to/repo --compact
sourcecode . --compact --git-context   # includes commit hotspots
sourcecode . --compact --copy          # copy to clipboard
```

Output (~2,500–4,000 tokens): detected stacks, entry points, dependencies, transactional boundaries (Spring), env vars, confidence level, analysis gaps.

### `sourcecode --agent`

More structured version of `--compact`. Designed for AI agent system prompt injection.

```bash
sourcecode /repo --agent --output context.json
cat context.json | claude -p "Explain the architecture"
```

Output (~4,500–5,500 tokens): project identity, entry points, file relevance ranking, architecture classification, confidence.

### `sourcecode onboard`

Full structural context for an unfamiliar codebase. Answers: "What is this repo and where do I start?"

```bash
sourcecode onboard /path/to/repo
sourcecode onboard . --llm-prompt     # appends a ready-to-use prompt
sourcecode onboard . --output onboard.json
```

Output: architecture summary, subsystems, key entry points, hotspots, tech-debt signals, analysis gaps.

### `sourcecode endpoints`

Extract all REST endpoints from Spring MVC and JAX-RS annotations.

```bash
sourcecode endpoints /path/to/repo
sourcecode endpoints . --format yaml
```

Output: list of `{method, path, controller, handler}`. Covers `@GetMapping`, `@PostMapping`, `@PutMapping`, `@DeleteMapping`, `@PatchMapping`, `@RequestMapping`, `@GET`, `@POST`, `@PUT`, `@DELETE`, `@PATCH` + `@Path`.

**Note:** JAX-RS sub-resource locator pattern (endpoints mounted dynamically via factory methods) is not individually counted — ~65% recall for JAX-RS.

### `sourcecode spring-audit` [free]

Detects Spring-specific structural anomalies: TX propagation bugs, security annotation gaps, and architectural anti-patterns that tests won't catch.

```bash
sourcecode spring-audit /path/to/repo
sourcecode spring-audit . --scope tx          # TX anomalies only
sourcecode spring-audit . --scope security    # security surface only
sourcecode spring-audit . --min-severity high
sourcecode spring-audit . --output audit.json
```

**TX patterns (TX-001..TX-005):** proxy bypass, nested transactions, readOnly propagation, NOT_SUPPORTED in active TX, exception swallowing.
**SEC patterns (SEC-001..SEC-003):** unsecured endpoints, CVE-2025-41248 `@PreAuthorize` inheritance bypass, `@Transactional` on controllers.

Each finding includes `severity`, `confidence`, `symbol`, `source_file`, `evidence`, `explanation`, and `fix_hint`. JAVA/SPRING ONLY.

### `sourcecode impact-chain` [free]

Systemic blast-radius analysis enriched with Spring TX and security context at every hop.

```bash
sourcecode impact-chain OrderService /repo
sourcecode impact-chain com.example.OrderService#placeOrder /repo
sourcecode impact-chain PaymentService . --depth 6
```

Returns `direct_callers`, `indirect_callers`, `endpoints_affected`, `transaction_boundary` (propagation/isolation/readOnly on the target), `security_surfaces` (per-endpoint policy + SEC finding IDs), `impact_findings` (TX/SEC findings touching the call chain), and `risk_level`.

**Event topology** — maps the publisher/consumer graph for a Spring event:

```bash
sourcecode impact-chain OrderPlacedEvent . --type events
```

Returns `publishers`, `consumers` with TX phase metadata (`AFTER_COMMIT`, `BEFORE_COMMIT`), `event_graph` edges, `transaction_context`, and `risk_level`.

**Limitations:** resolves Spring `ApplicationEvent`/`@EventListener` only; does not trace Kafka/RabbitMQ; self-invocation proxy bypass not detected.

### `sourcecode impact` [Pro]

Blast-radius analysis: who depends on a class and what breaks if it changes.

```bash
sourcecode impact OrderService /repo
sourcecode impact OrderService . --depth 6
sourcecode impact org.example.UserService /repo   # FQN form
sourcecode impact UserService.java /repo          # file form
```

Output: `direct_callers`, `indirect_callers` (BFS to `--depth`), `endpoints_affected`, `transactional_boundaries_touched`, `risk_score` (0–100), `risk_level` (low/medium/high).

**Critical:** In Spring Boot, always target the **interface**, not the implementation.

```bash
sourcecode impact OrderService /repo      # ✓ correct — returns callers via @Autowired
sourcecode impact OrderServiceImpl /repo  # ✗ wrong — returns 0 callers (DI blindness)
```

If you get `direct_callers: []` with `resolution: exact` for a `@Service` class, re-run with the interface name.

### `sourcecode fix-bug` [Pro]

Risk-ranked file list correlated to a bug symptom.

```bash
sourcecode fix-bug /repo --symptom "NullPointerException in checkout"
sourcecode fix-bug . --symptom "payment timeout"
sourcecode fix-bug . --symptom "OIDC token refresh fails after realm update"
```

Output: `relevant_files` (ranked by symptom match evidence), `suspected_areas` with evidence chain (keyword → path → content → git correlation).

**Best results with specific symptoms.** Generic symptoms ("error", "bug") return noisy file lists.

### `sourcecode review-pr` [Pro]

PR diff analysis: changed files, test coverage gaps, blast radius of changed classes.

```bash
sourcecode review-pr . --since main
sourcecode review-pr . --since origin/main --format github-comment
sourcecode review-pr . --since HEAD~3 --output review.json
```

Output: `changed_files`, `review_hotspots`, `suggested_review_order`, `test_coverage_risk`, `impact_summary`, `ci_decision`.

Parse `ci_decision` and `test_coverage_risk` for automated CI gates:

```bash
DECISION=$(jq -r '.ci_decision' review.json)
RISK=$(jq -r '.test_coverage_risk.risk_level' review.json)
```

### `sourcecode modernize` [Pro]

Identifies coupling hotspots, dead zones, and cross-module tangles.

```bash
sourcecode modernize /path/to/repo
```

Output:
- `high_coupling_nodes` — classes with highest fan-in degree (risky to change)
- `hotspot_candidates` — services/controllers with high coupling (subset of above)
- `dead_zone_candidates` — classes with zero callers (safe to remove)
- `cross_module_tangles` — packages with bidirectional coupling

**Note:** In annotation-heavy codebases, `hotspot_candidates` may be empty — annotation types and JPA entities dominate the coupling graph but don't qualify as hotspots. Check `high_coupling_nodes` directly.

---

## Typical workflows

### Onboarding a new codebase (you or an AI agent)

```bash
# Step 1: get bounded context
sourcecode onboard /repo

# Step 2: understand the entry points + architecture
sourcecode /repo --compact

# Step 3: pipe to an AI agent
sourcecode /repo --agent | claude -p "Explain the main request flow through this service"
```

### Pre-refactor safety check

```bash
# Always target the interface in Spring Boot repos:
sourcecode impact OrderService /repo

# Parse the risk level programmatically:
sourcecode impact OrderService /repo | jq '{risk_level, risk_score, direct_callers_count: (.direct_callers | length), endpoints_count: (.endpoints_affected | length)}'

# If touching a very large hub interface, use depth=1 for speed:
sourcecode impact KeycloakSession /repo --depth 1
```

### Bug triage

```bash
# Specific symptoms → best signal:
sourcecode fix-bug /repo --symptom "NullPointerException OrderService.findOrderById line 234"

# Save full output for complex bugs:
sourcecode fix-bug /repo --symptom "payment gateway timeout" --output triage.json

# Top files will include:
# - symptom_match: direct keyword/path match
# - suspected_areas: annotations + evidence chain
# - relevant_files: risk-ranked by composite score
```

### PR review

```bash
# JSON output for automated gates:
sourcecode review-pr . --since origin/main --output review.json
jq '{ci_decision, test_coverage_risk, impact_summary}' review.json

# Markdown comment for GitHub:
sourcecode review-pr . --since main --format github-comment

# Specific range:
sourcecode review-pr . --since HEAD~5
```

### Understand the REST API surface

```bash
sourcecode endpoints /repo
sourcecode endpoints /repo | jq '.endpoints | map(select(.method == "POST"))' # POST endpoints only
sourcecode endpoints /repo --output endpoints.json
```

### MCP server (AI agent integration)

```bash
# One-time setup (Claude Desktop / Cursor):
sourcecode mcp init

# Verify setup:
sourcecode mcp status

# Start server manually:
sourcecode mcp serve

# Remove:
sourcecode mcp remove
```

The MCP server exposes structural analysis tools to AI agents without requiring the agent to call the CLI directly. Claude Desktop and Cursor can query impact, endpoints, and context through the MCP protocol.

---

## Output schema

All commands output structured JSON to stdout unless `--output` is specified.

Common fields:
- `schema_version` — format version
- `confidence_summary.overall` — `high` / `medium` / `low`
- `analysis_gaps` — list of what could not be analyzed and why

Java/Spring-specific fields (when detected):
- `transactional_boundaries` — classes annotated with `@Transactional`
- `language_version` — from `maven.compiler.source`
- `deployment.spring_boot_version`
- `deployment.packaging` — `jar` or `war`
- `mybatis` — mapper interface/XML pairing summary

### Output modes

| Mode | When to use | Token size |
|------|-------------|-----------|
| `--compact` | AI session start, high-level overview | 2,500–4,000 |
| `--agent` | AI agent system prompt injection | 4,500–5,500 |
| `onboard` | Task-structured agent/developer onboarding | ~2,600 |
| `fix-bug` (trimmed) | Bug triage with token budget | ~4,600 |
| `--full` | Remove truncation limits | unlimited |

---

## Caching

Analysis is cached by content hash. Unchanged files do not re-scan.

- Cache hit speedup: 13–33× depending on repo size
- BroadleafCommerce (2,985 files): 2.9s cold → 0.20s cached
- Keycloak (7,885 files): 9.0s cold → 0.27s cached
- Bypass: `sourcecode --no-cache`

The cache is stored locally. No content leaves your machine.

---

## Flags reference

| Flag | Short | Description |
|------|-------|-------------|
| `--compact` | | High-signal summary + transactional boundaries |
| `--agent` | | Structured JSON for AI agent injection |
| `--full` | | Remove truncation limits |
| `--git-context` | `-g` | Add commit hotspots and uncommitted file count |
| `--changed-only` | | Limit to git-modified files |
| `--depth N` | | Traversal depth (default: 4, max: 20) |
| `--format` | `-f` | `json` (default) or `yaml` |
| `--output` | `-o` | Write to file instead of stdout |
| `--no-cache` | | Force fresh analysis |
| `--copy` | `-c` | Copy output to clipboard |
| `--no-redact` | | Disable automatic secret redaction |
| `--exclude PATTERN` | | Skip directories matching pattern |
| `--version` | `-v` | Show version and exit |

---

## Troubleshooting

### `impact` returns 0 callers for a @Service class

You're targeting the implementation. Spring Boot uses interface injection.

```bash
# Wrong:
sourcecode impact UserServiceImpl /repo   # 0 callers

# Correct:
sourcecode impact UserService /repo       # real blast radius
```

### `hotspot_candidates` is empty after `modernize`

Check `high_coupling_nodes` instead. In annotation-heavy codebases, the highest-coupled symbols are annotation types and JPA entities, not services. These are excluded from `hotspot_candidates` by role filter.

### `review-pr` shows `confidence: LOW`

Expected for small commits or commits touching files without annotation signals. The output is still accurate — low confidence means the role classification fell back to filename/git-diff only. Check `analysis_limiter.missing_signals` for details.

### `endpoints` shows 0 for a Spring project

Most common causes:
1. Endpoints use non-standard annotation composition (meta-annotations). Direct `@GetMapping` detection only.
2. JAX-RS sub-resource locator pattern — endpoints mounted dynamically.
3. Kotlin Spring controllers (Java-only analysis).

### Cold scan is slow (>30s)

For very large repos (15K+ Java files), consider:
- `--fast` flag (skips deep content search): `sourcecode fix-bug --fast`
- `--exclude legacy,generated` to skip non-production code
- After first run, cache hits will be <1s

---

## Limitations

- Static analysis only — no runtime behavior, no bytecode analysis
- No semantic code understanding — reads structure, not logic
- Spring MVC layered apps: high accuracy. Quarkus SPI/plugin model: architecture classification may be inaccurate
- JAX-RS subresource locator pattern: ~65% endpoint recall
- `impact` on implementation classes returns implementation-specific callers only — always target the interface in Spring Boot
- `no_security_signal` on endpoints = no method-level annotations found. Does not mean the endpoint is unsecured (Spring Security filter chains are not analyzed)
- `spring-audit` and `impact-chain`: Java/Spring only — non-Java repos return `spring_detected: false`
- Event topology (`--type events`): resolves Spring `ApplicationEvent`/`@EventListener` only. Kafka, RabbitMQ, Redis message routes not traced
- Self-invocation TX bypass (calling `@Transactional` method from same class without proxy) not detected
- Conditional beans (`@ConditionalOnProperty`) not evaluated at analysis time
- `project_summary` is extracted from README — may reflect marketing copy rather than architectural description
- Works offline; no AI inference — outputs structural facts, not quality judgments

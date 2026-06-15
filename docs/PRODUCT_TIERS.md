# sourcecode — Product Tiers

> AI-ready change intelligence for ugly enterprise Java monoliths.

This document defines the Free / Pro split.

**The gating axis is size + automation + team — never capability.** Every
command runs at full power on the Free tier for small and mid-size repos. You
pay when the work gets *bigger* (enterprise-scale monolith), *automated*
(CI/CD), or *shared* (a team / an org) — i.e. exactly when the tool is creating
the most value and replacing the most engineering time.

This is deliberate. The data on developer-tool monetisation is clear: gating
*core capability* makes free users churn; gating *scale and collaboration*
roughly doubles conversion without raising churn. So nothing is crippled on
Free — a solo dev on a normal repo gets `impact`, `fix-bug`, `review-pr`,
`modernize`, and `--full`, all of it.

The size threshold is **500 Java source files** (`_FREE_REPO_JAVA_FILE_LIMIT`).
At or below that, everything is free. Above it, the heavy-analysis commands
gate to Pro (exit code 2 with a size-framed upgrade prompt).

**Java-only by design.** The size axis counts *Java* source files only.
sourcecode monetises enterprise Java monoliths; non-Java repos (Python, Go,
TypeScript, …) never gate to Pro and are free at any size. This is a deliberate
product decision, not an oversight — see `is_large_repo()` in `license.py`.

---

## Free (Apache 2.0)

**Every command, full power, on repos up to 500 Java source files. Local,
single-repo, offline.**

| Capability | Command | Notes |
|---|---|---|
| Repo shape + tech-stack detection | `sourcecode .` | Java, Spring, Quarkus, MyBatis, Go, Python, … |
| Compact / agent LLM output | `--compact` / `--agent` | Bounded summary or structured JSON |
| Endpoint surface extraction | `sourcecode endpoints .` | Spring MVC + JAX-RS, security annotations |
| Request-body validation surface | `sourcecode validation .` | OpenAPI constraints + custom validators per endpoint; JAVA/SPRING only |
| Canonical IR (CIR) | `sourcecode repo-ir .` | Stable, deterministic, fingerprinted |
| **Full blast-radius intelligence** | `sourcecode impact <target>` | Mappers, security surface, cross-module, confidence, explanation |
| **PR risk scoring** | `sourcecode review-pr --since <ref>` | Per-symbol change risk, BFS propagation, transactional surface |
| **Bug triage context** | `sourcecode fix-bug --symptom "<s>"` | Symptom-boosted ranking, annotation signals, persistence paths |
| **Modernization planning** | `sourcecode modernize .` | Hotspots, dead zones, coupling tangles, refactor roadmap |
| **Git-churn ranking** | `--rank-by git-churn` | File volatility via git history |
| **No truncation limits** | `--full` | Full transactional boundaries, DTO mappers, large result sets |
| **Spring semantic audit** | `sourcecode spring-audit .` | TX-001..005 anomalies + SEC-001..003 security surface; JAVA/SPRING only |
| **Impact chain + TX/SEC enrichment** | `sourcecode impact-chain <symbol>` | Systemic blast radius with transaction boundary and security surfaces per hop |
| **Event topology** | `sourcecode impact-chain <event> --type events` | Publisher → event → consumer graph; AFTER_COMMIT/BEFORE_COMMIT TX phase |
| RIS bootstrap context | `sourcecode cold-start .` | Repository Intelligence Snapshot; instant return from persisted cache |
| Onboarding context | `sourcecode onboard .` | Architecture, entry points, subsystems |
| MCP local server | `sourcecode mcp serve` | Works with Claude Code, Cursor, Copilot |
| Delta context (trial) | `prepare-context delta` | 30 free runs/repo, then Pro (automation axis) |
| Offline, no data egress | all commands | IR never leaves the machine |

---

## Pro (paid, per-repo or per-LOC)

Same engine, same commands — **unlocked at scale and in automation.**

| Trigger | What unlocks | Notes |
|---|---|---|
| **Repo > 500 Java files** | All heavy-analysis commands on enterprise-scale monoliths | `impact`, `fix-bug` (full list), `review-pr` (full), `modernize` (full), `--full`, git-churn ranking, uncapped graph/semantic nodes |
| **CI/CD automation** | `prepare-context delta` beyond the 30-run free trial | Designed to run on every PR — the automation axis |
| **Test gap analysis at scale** | `prepare-context generate-tests` on large repos | Untested-file detection with coverage recommendations |
| CI gating helpers | `sourcecode review-pr --format github-comment` | Posts risk report as inline PR comment |
| Historical architecture drift | `sourcecode repo-ir --since <ref>` | Symbol-level diff vs baseline |

On a Pro-eligible repo, a Free user still gets a **capped preview** (top-5
files for `fix-bug`, lightweight `review-pr`, structural-only `modernize`) plus
an upgrade prompt — enough to see the value before paying.

**Pricing:** flat **per-developer** subscription — **€19/mo** or **€190/yr**
(annual ≈ 2 months free). Self-serve, no sales motion, no metering. The price
sits at the floor of the closest comp (CodeScene $20–30/dev/mo, a full platform)
and matches dev-tool norms (Cursor/Copilot $10–20/mo). sourcecode is a focused
CLI, not a platform, so pricing at — not above — the comp floor maximises
adoption, which is the goal. Per-repo / per-LOC (the SonarQube enterprise
motion) is deliberately *not* used here: it needs metering + a sales team we
don't have, and it belongs to an org tier we don't yet sell.

**Why Pro pays for itself:**
- One avoided production incident from a missed transactional boundary = weeks of engineering time.
- PR reviewers get a risk score and blast-radius map in under 60 seconds on repos with 10k+ classes.
- Bug triage narrows a 10k-class monolith to 5–10 ranked suspects before the first `grep`.

---

## Not offered yet (roadmap, not vaporware)

These are derived from primitives that already exist in the engine, but they are
**not sold today** and must not appear as purchasable. When an org tier exists,
this is where it goes (likely per-repo / per-LOC, the SonarQube motion):

- Hosted MCP server — single shared server for a whole org (local `mcp serve` is free today)
- Team dashboards — architecture drift, per-repo health, hotspot trends
- Audit exports — endpoint surface, security-gate coverage, TX boundary map
- SSO / org policy — SAML / OIDC, central redaction/output policy
- Fleet usage — run across a GitHub Org / GitLab Group; aggregate reporting

---

## Tier ladder summary

```
Free                          Pro  (€19/mo · €190/yr, per dev)
────────────────────────      ──────────────────────────────────
Small/mid repo, solo      →    Same commands, unlocked at scale
all commands, full power       repos > 500 Java files
single repo, local, offline    + CI/CD delta automation
local MCP serve                + uncapped graph / semantic output
```

Gating axis: **size → automation.** Not capability.

---

## Engine reality check

All tiers use **the same CIR engine.** Nothing is re-built at a higher tier,
and nothing is removed at a lower one. The primitives are:

- `SymbolRecord` / `RelationEdge` — typed IR graph nodes
- `compute_blast_radius()` — BFS over reverse dependency graph
- `_build_route_surface()` / `_route_security_from_sym()` — endpoint + security extraction
- `_detect_subsystems()` — package-prefix subsystem grouping
- `apply_ir_size_limits()` / `trim_to_budget()` — output budget enforcement

Free and Pro run identical code on identical commands. The only difference is
**how much** you can point it at: Free is bounded by repo size and the delta
trial quota; Pro removes those bounds.

Enforcement lives in `src/sourcecode/license.py`:
- `is_large_repo(path)` / `_FREE_REPO_JAVA_FILE_LIMIT` — the size axis
- `require_repo_or_pro(path, feature)` — free below the limit, Pro above it
- `check_delta_free_tier(path)` / `_DELTA_FREE_LIMIT` — the automation axis

---

## Benchmark evidence

The [benchmark suite](../tests/test_enterprise_benchmarks.py) covers five
representative enterprise Java repo types:

| Repo type | Files | Endpoints | Blast-radius tested | Notes |
|---|---|---|---|---|
| Keycloak-like (JAX-RS / Quarkus) | 3+ | 5+ | ✓ | Jakarta EE security annotations |
| Spring Boot + MyBatis | 4+ | 3+ | ✓ | @Mapper interfaces, @Transactional |
| Legacy Spring MVC monolith | 4+ | 2+ | ✓ | @PreAuthorize, layered architecture |
| Endpoint-heavy (50+ endpoints) | 10+ | 50+ | ✓ | Scale + boundedness |
| MyBatis-heavy CRM (3 entities) | 9+ | 9+ | ✓ | Multi-mapper, cross-module impact |

All repos produce valid IR in < 10s.  `compute_blast_radius` completes in < 5s.
Summary IR stays under 100 KB.  Output bounds enforced across all commands.

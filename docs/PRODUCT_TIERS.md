# sourcecode — Product Tiers

> AI-ready change intelligence for ugly enterprise Java monoliths.

This document defines the OSS Core / Pro / Enterprise capability split.
Every Pro and Enterprise feature is derived from primitives that already
exist in the open-source engine — no vaporware.

---

## OSS Core (free, Apache 2.0)

Enough to understand an unfamiliar codebase and feed an LLM.

| Capability | Command | Notes |
|---|---|---|
| Repo shape + tech-stack detection | `sourcecode .` | Java, Spring, Quarkus, MyBatis, Go, Python, … |
| Compact LLM-ready output | `--compact` | Bounded to fit a single context window |
| Agent-optimised output | `--agent` | Structured JSON for tool-use workflows |
| Endpoint surface extraction | `sourcecode endpoints .` | Spring MVC + JAX-RS, security annotations |
| Canonical IR (CIR) | `sourcecode repo-ir .` | Stable, deterministic, fingerprinted |
| Basic blast radius | `sourcecode impact <class>` | Direct + indirect callers, endpoints affected |
| **Spring semantic audit** | `sourcecode spring-audit .` | TX-001..005 anomalies + SEC-001..003 security surface; JAVA/SPRING only |
| **Impact chain + TX/SEC enrichment** | `sourcecode impact-chain <symbol>` | Systemic blast radius with transaction boundary and security surfaces per hop |
| **Event topology** | `sourcecode impact-chain <event> --type events` | Publisher → event → consumer graph; AFTER_COMMIT/BEFORE_COMMIT TX phase |
| RIS bootstrap context | `sourcecode cold-start .` | Repository Intelligence Snapshot; instant return from persisted cache |
| Onboarding context | `sourcecode onboard .` | Architecture, entry points, subsystems |
| MCP local server | `sourcecode mcp serve` | Works with Claude Code, Cursor, Copilot |
| Offline, no data egress | all commands | IR never leaves the machine |

---

## Pro (paid, per-seat or per-repo)

Decision-grade change intelligence for engineering teams.

| Capability | Command | Notes |
|---|---|---|
| **Full blast-radius intelligence** | `sourcecode impact <target>` | + mappers, security surface, cross-module, confidence score, explanation |
| **PR risk scoring** | `sourcecode review-pr --since <ref>` | Diff-based; per-symbol change risk, BFS propagation, transactional surface |
| **Bug triage context** | `sourcecode fix-bug --symptom "<s>"` | Symptom-boosted file ranking, annotation signals, persistence paths |
| **Modernization planning** | `sourcecode modernize .` | Hotspot candidates, dead zones, coupling tangles, refactor roadmap |
| Better ranking heuristics | all commands | Import-overlap, security-gate bonus, txn boundary weight |
| Bounded LLM workflows | all commands | Output contracts enforced across all Pro commands |
| CI gating helpers | `sourcecode review-pr --format github-comment` | Posts risk report as inline PR comment |
| Historical architecture drift | `sourcecode repo-ir --since <ref>` | Symbol-level diff vs baseline |

**Why Pro pays for itself:**
- One avoided production incident from a missed transactional boundary = weeks of engineering time.
- PR reviewers get a risk score and blast-radius map in under 60 seconds on repos with 10k+ classes.
- Bug triage narrows a 500-file monolith to 5–10 ranked suspects before the first `grep`.

---

## Enterprise (site licence / hosted)

Org-wide governance and fleet visibility.

| Capability | Notes |
|---|---|
| Team dashboards | Architecture drift over time, per-repo health scores, hotspot trends |
| Audit exports | SBOM-adjacent: endpoint surface, security-gate coverage, transactional boundary map |
| SSO / org policy | SAML / OIDC, central policy enforcement for redaction and output filtering |
| Hosted MCP server | Single shared server for the whole engineering org; no per-machine setup |
| Fleet usage | Run sourcecode across all repos in a GitHub Org / GitLab Group; aggregate reporting |
| Advanced security surface | Cross-repo security annotation coverage, unauthenticated endpoint tracking |
| Org-wide reporting | "How many repos have no security annotations on public endpoints?" |

---

## Capability ladder summary

```
OSS Core            Pro                   Enterprise
────────────        ──────────────────    ──────────────────────
Understand it   →   Change it safely  →   Govern it org-wide
onboard             impact (full)         fleet dashboards
endpoints           review-pr             audit exports
repo-ir             fix-bug               hosted MCP
basic impact        modernize             SSO + policy
```

---

## Engine reality check

All Pro and Enterprise features use **the same CIR engine** as OSS Core.
Nothing is re-built at a higher tier.  The primitives are:

- `SymbolRecord` / `RelationEdge` — typed IR graph nodes
- `compute_blast_radius()` — BFS over reverse dependency graph
- `_build_route_surface()` / `_route_security_from_sym()` — endpoint + security extraction
- `_detect_subsystems()` — package-prefix subsystem grouping
- `apply_ir_size_limits()` / `trim_to_budget()` — output budget enforcement

Pro unlocks richer output from these functions (mappers, cross-module, confidence,
security surface, explanation).  Enterprise adds fleet orchestration and policy
layers on top.

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

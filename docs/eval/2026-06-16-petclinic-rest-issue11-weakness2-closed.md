# Field test — petclinic-rest #11, weakness #2 closed (Fase 21 + parser fixes)

**Date:** 2026-06-16
**Repo under test:** `spring-petclinic-rest` (fresh clone, no `mvn`/generated sources)
**Version:** 1.41.0
**Command:** `sourcecode impact-chain VetRepository <repo>`

## Background

The 2026-06-15 field test ([issue11 vets pagination](2026-06-15-petclinic-rest-issue11-vets-pagination.md))
found two weaknesses:

1. **Weakness #1** — `impact-chain` over an interface symbol didn't model implementors.
   Closed by **CH-001c** (v1.40.0): interface impact resolves the full `implements` +
   `extends` descendant set. Verified live: `implementations` now lists all 3 vet backends
   (Jpa/Jdbc/SpringData).
2. **Weakness #2** — `endpoints_affected` always 0 for a repository/service seed in this
   openapi-generator interface-only repo. This document records its closure.

## Root cause — four compounding breaks

Weakness #2 was not a single bug. The repo→endpoint chain was severed at four points:

| # | Break | Layer | Fix |
|---|-------|-------|-----|
| A | spec→controller linking lived only in the `endpoints` command, never reaching `route_surface`/CIR | model wiring | Fase 21-02 (`_recover_openapi_spec_routes` shared + merged into `build_repo_ir`) |
| B | BFS reached the service impl but its callers inject the *interface* — `injects` edges sit on the interface node | impact BFS | Fase 21-03 (`_bfs_callers` folds impl→interface mid-chain) |
| C | `ClinicServiceImpl`'s constructor spans multiple lines → params lost → no `injects` edge at all | parser | BUG-PARSER-002 (paren-balancing pre-join) |
| D | the repo type is imported via `import ...repository.*` → never resolved to an FQN | parser | BUG-PARSER-003 (wildcard import resolution) |

Breaks C and D were the live-repo blockers: the **first hop** repo→service never
existed, so A and B (verified earlier with synthetic fixtures) could not fire on the
real codebase. The synthetic tests passed because they injected the `#<init>` edges
directly, bypassing the parser.

## Result

`impact-chain VetRepository` on the live repo:

```
direct_callers:   [ClinicServiceImpl]
indirect_callers: [OwnerRestControllerV1, PetRestControllerV1, PetTypeRestControllerV1,
                   SpecialtyRestControllerV1, VetRestControllerV1, VisitRestControllerV1,
                   OwnerRestControllerV2, PetRestControllerV2]
endpoints_total:  35   (was 0)
implementations:  [JdbcVetRepositoryImpl, JpaVetRepositoryImpl, SpringDataVetRepository]
```

The 35 endpoints include the spec-recovered v2 routes (`GET /v2/owners`, `GET /v2/pets`)
— confirming Fase 21-02 (spec→CIR), 21-03 (interface DI crossing), and the two parser
fixes all compose end to end.

The fan-out is large because petclinic-rest routes every controller through a single
god-service (`ClinicServiceImpl`); changing any repository legitimately touches every
handler that service backs. The result is conservative and correct, not noisy.

## Verification

- Live E2E ran without OOM (impact-chain build is light; the full repo-ir build still
  OOMs in this environment — unchanged).
- Synthetic regression tests pin each fix:
  - `TestInterfaceChainReachesController` (21-03 BFS, `_FakeCIR`)
  - `TestMultilineAndWildcardConstructorInjection` (parser, `build_repo_ir`)
  - `TestImpactPathHasSpecSurface` (21-02 route_surface→EndpointIndex)
- Full suite: **2634 passed, 3 skipped**, zero xfail.

## Commits

- `b6d701d` Fase 21-02 — spec routes reach CIR/impact-chain
- `9ab7986` Fase 21-03 — BFS crosses interface DI boundary mid-chain
- `8be7cf1` parser — multi-line + wildcard-import constructor injection edges

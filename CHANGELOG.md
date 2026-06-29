# Changelog

## [1.61.0] — 2026-06-29

### Fixed — `migrate-check` false positives on already-migrated Spring Boot 3 repos

Three defects made `migrate-check` report a Boot 3 / Jakarta repo as an
un-migrated Boot 2 project (verified on Broadleaf Commerce CE 7.0.x —
Boot 3.5.14, Java 17, `jakarta.*` fully adopted). The verdict contradicted the
code; this corrects all three at the root and adds regression guardrails.
Report `schema_version` bumped **1.2 → 1.3**.

- **`spring_boot_2_detected` no longer defaults to `true`.** The detector now
  resolves Maven `${...}` properties before reading versions and recognises
  Boot in every form: `spring-boot-starter-parent`, a managed
  `spring-boot-dependencies` BOM, a `spring-boot*` dependency versioned by
  property, and the Gradle plugin. The old heuristic used a `DOTALL` regex that
  matched any stray `2.x` library version anywhere in the pom. The field is now
  **tri-state** — `true` (Boot 2 confirmed) / `false` (Boot 3+ confirmed) /
  `null` (undetermined). **Absence of evidence is never reported as `true`.**
  A new `spring_boot_version_detected` exposes the resolved version. Massive
  `jakarta.*` import adoption vetoes any Boot-2 verdict.
- **`readiness_score` no longer collapses to 0 on JDK-modernization volume.**
  Blockers (critical/high) still floor a genuinely blocked repo, but orthogonal
  JDK debt (`java.util.Date`, reflection) is capped so it can no longer sink a
  jakarta-ready repo. The report now separates dimensions:
  `jakarta_readiness`, `boot3_readiness`, and `jdk_modernization` (each 0–100).
- **`javax.*` JDK/JSR namespaces are no longer flagged with
  `javax-to-jakarta-migration-risk`.** `javax.cache` (JSR-107), `javax.sql`,
  `javax.xml` (JAXP), `javax.naming`, `javax.management`, `javax.crypto`, etc.
  keep the `javax` prefix forever and are now allowlisted. Only the Jakarta EE 9
  renamed namespaces (`javax.servlet`, `javax.persistence`, `javax.validation`,
  `javax.xml.bind`, …) still carry the flag.

## [1.60.0] — 2026-06-29

### Changed
- **Anonymous telemetry is now on by default (opt-out).** Previously telemetry
  was strictly opt-in and disabled until the user accepted a y/N consent prompt.
  It now defaults to **on** so usage metrics flow without an extra step, while
  staying fully anonymous — no source code, paths, file names, secrets, or
  repository content are ever collected. The first interactive run shows a
  one-time **notice** (not a prompt) explaining what is collected and how to
  disable. Disable any time with `sourcecode telemetry disable`,
  `SOURCECODE_TELEMETRY=0`, or the `DO_NOT_TRACK=1` convention. With no explicit
  choice, telemetry still defaults to **off in CI** (no human to see the notice);
  an explicit opt-in is honored everywhere. README, `--help`, and
  `docs/privacy.md` updated accordingly.

## [1.59.0] — 2026-06-19

### Fixed
- **Event topology now recognizes generic-wrapper event publishers.**
  `publishEvent(new SaveServiceEvent<>(obj))` and explicit forms like
  `new SaveServiceEvent<Order>(obj)` were silently dropped: the publisher-edge
  regex required the constructor `(` to immediately follow the class name, so any
  diamond `<>` or type argument broke the match. `repo-ir` / event-topology then
  reported no producers for the whole generic `*ServiceEvent` family (e.g. OpenMRS
  `SaveServiceEvent`/`VoidServiceEvent`/`RetireServiceEvent`). The inline and
  two-step publish scans now skip an optional (incl. nested) generic argument list.
- **`impact-chain` now resolves intra-class method callers.** A query on a
  private/helper method (e.g. `OrderServiceImpl#stopOrder`) found zero direct
  callers and degraded to a wrong class-level expansion, because method-to-method
  calls inside a single class produced no graph edge — only class-level `calls`
  edges existed. The relation builder now emits method-level `calls` edges for
  bare `m(...)` and `this.m(...)` invocations whose target is a sibling method of
  the same class (string/comment-aware, overload-safe, no self-loops). The two
  defects were found dogfooding the tool on OpenMRS issue #6197.

## [1.58.0] — 2026-06-19

### Added
- **`validation` recovers DTO constraints from source when no OpenAPI spec is
  present.** Previously `validated_fields` read `0` for any repo without a spec
  on disk, even when DTOs carried bean-validation annotations. The command now
  locates each body-shaped handler's `@Valid`/`@Validated` parameter, resolves
  the DTO in-repo, and reads its field constraints (following one in-repo
  supertype so inherited constraints are kept). Recovered routes are tagged
  `source="source-derived"` at medium confidence with a `binding` hint
  (body vs form). The core symbol/endpoint extractor is untouched, so the
  OpenAPI-driven path is unchanged. Verified: spring-petclinic 0 → 13 validated
  fields; repos with no `@Valid` usage correctly stay at 0 (no false positives).

### Changed
- **`impact-chain` output now matches the `impact` command's risk schema.**
  `risk_score`, `confidence_score`, `confidence_level`, and `explanation` are
  emitted at the top level (previously `risk_score` lived only in `metadata`
  and there was no `explanation`). The legacy `confidence` string is retained
  and equals `confidence_level`. Risk/confidence formulas are unchanged — only
  the output contract was aligned so agents parse both commands with one shape.

## [1.57.0] — 2026-06-19

### Changed
- **TEMPORARY: Pro unlocked for everyone (early-adoption phase).** A new
  `_PRO_UNLOCK_ALL` switch in `license.py` (env `SOURCECODE_PRO_UNLOCK`, default on)
  floors `is_pro` to `True` at init, so anyone who installs gets Pro from the start —
  removing onboarding friction to maximize adoption. The gate *logic*
  (`require_feature` / `require_repo_or_pro` / `require_pro`, size limits, upgrade
  prompts, telemetry) is left fully intact; this only raises the entitlement floor.
  Real Pro license activation still works. To resume the paywall: set
  `_PRO_UNLOCK_ALL = False` (or `SOURCECODE_PRO_UNLOCK=0`) — no other change needed.

## [1.56.0] — 2026-06-19

### Added
- **CH-007 — `impact-chain` recovers callers wired through an external interface.**
  The flagship blast-radius gap (BroadleafCommerce #3124): a class wired by DI through
  an external framework interface (e.g. a Spring Security `UserDetailsService` /
  `RedirectStrategy` impl) reported `0 callers` because consumers inject the *interface*,
  never the impl — so no reverse-graph edge names it and CH-001b (in-repo interface
  expansion) cannot bridge. CH-005 only warned; it did not recover the callers.

  `_recover_external_iface_callers()` now reads the raw dependency edges (which record
  `<consumer> -[injects|calls|instantiates|returns]-> <external interface>` even though
  those edges never reach `reverse_graph`) and attributes the in-repo wiring/consumer
  classes as callers, then recomputes the endpoints/findings/surfaces/risk that depend
  on them. In-repo implementors of the interface are counted: exactly one → unambiguous
  binding (`confidence:medium`, `framework_di` dropped from `metadata.blind_spots`);
  several → `metadata.external_iface_binding_ambiguous:true`, confidence stays `low`
  with an ambiguity warning. No in-repo wiring → the honest CH-005 blind-spot path is
  unchanged. New metadata: `external_iface_callers_recovered`, `external_iface_binding_ambiguous`.

- **Precision: concrete JDK base classes excluded from DI detection.** `class Foo extends
  ArrayList`/`Stack`/`InputStream` is implementation reuse, not DI dispatch, and a consumer
  holding an `ArrayList` field is not a caller of `Foo`. Added `_NON_DI_SUPERTYPES`
  (java.util containers, java.io streams, java.lang bases) to the external-supertype
  filter — found via real-repo validation, where it cut false recoveries from 12 to 7.

  Validated on BroadleafCommerce (2762 files, 22 247 symbols): CH-007 recovers wiring
  callers for 7 classes, all genuine DI interfaces; querying a `UserDetailsService`
  implementation recovers the security-config + login-service classes that wire it.
  Report: `.planning/benchmark-broadleaf-3124.md`. Tests: `TestExternalInterfaceDIBridge`
  (recovery / ambiguity / no-wiring fallback).

## [1.55.0] — 2026-06-19

### Security
- **License secret no longer written world-readable.** `~/.sourcecode/license.json`
  stores the Pro `license_key` and account email but was written with the default
  umask (typically `0644`, dir `0755`), so any other local user could read the
  credential on a shared host. Now the directory is created/tightened to `0700` and
  the file is `chmod 0600` on the tmp file *before* the atomic rename (no
  world-readable window at the final path). `_secure_dir()` is applied at every
  write site (activation, license file, delta-run counter).

- **License-endpoint override now scheme-validated.** `SOURCECODE_SUPABASE_URL` was
  trusted verbatim; an `http://` value sent the license key over plaintext. The
  override is now accepted only when `https://` (any host) or `http://` to loopback
  (preserves Supabase local dev on `http://127.0.0.1:54321`); anything else is
  rejected back to the default endpoint with a warning.

- **Hardened the symbol grep in the contract pipeline.** The deep-scan symbol
  search ran `grep <symbol> .` with the symbol in pattern position and as a regex,
  while the Python fallback matched literally — inconsistent, and a leading-dash
  symbol could be parsed as a flag. Switched to `grep -F -e <symbol> -- .`: literal
  match (matches the fallback, removes the regex/ReDoS surface) and `-e`/`--` guard
  option parsing. No `shell=True` anywhere; this is defense-in-depth on argv.

Dependency audit (`pip-audit`): no known vulnerabilities in runtime dependencies.

## [1.54.0] — 2026-06-19

### Added
- **`export --c4` now emits `components.module_roots` — architectural module
  enumeration + DDD/legacy classification.** Field test (saint-server C4 doc pass)
  surfaced that the C4 export keyed modules by *leaf source directory*
  (`dirname(source_file)`), so a DDD module split across
  `domain/` / `application/` / `infrastructure/` subdirs fragmented into several
  unrelated "modules". A downstream consumer had to infer module boundaries from
  directory names, which produced a module **undercount** and **DDD-vs-legacy
  misclassification** in the generated docs.

  Fix: `_detect_module_roots()` rolls leaf dirs up to their architectural module
  root (the directory above the shallowest recognized layer dir) and classifies
  each `layered` (≥2 of `domain`/`application`/`infrastructure`) vs `flat`
  (legacy/flat package). `c4.components.module_roots` carries the per-module
  `{root, pattern, layers, symbol_count, leaf_dir_count}` plus a summary with a
  verifiable `module_count` / `layered_module_count` / `flat_module_count`, so a
  consumer enumerates real modules instead of guessing.

  Pure-structural (no extra file reads). Non-breaking: top-level `c4` keys and the
  leaf-level `--module-graph` dependency view are unchanged. 3 regression tests
  (layered rollup, flat classification, summary counts).

## [1.50.0] — 2026-06-17

### Fixed
- **F-1 — `impact-chain` confidence no longer capped at `medium` by informational
  warnings.** Confidence was computed as `medium` whenever *any* warning was present, but
  the CH-001a/b interface↔implementation expansion notices ("Interface implementation
  expansion: added N symbols…") are appended on every Spring interface/impl query — the
  overwhelmingly common case — so a clean exact/`class_expanded` resolution could never
  report `high`. The signal was meaningless: `medium` meant "normal", not "degraded".

  Fix: track a `confidence_reducing` flag set only by genuinely degrading conditions
  (hub-guard capped traversal); informational expansion notices no longer affect
  confidence. `partial` resolution and blind-spot guards (CH-003/CH-005) still degrade as
  before. Verified on shopizer: `impact-chain ProductService` now reports `confidence:high`
  (was `medium`) while still surfacing the expansion notice. 2 regression tests
  (`TestConfidenceNotCappedByInfoWarnings`): expansion warning keeps `high`, hub-guard
  truncation still caps to `medium`.

  Closes the v1.47.0 field-benchmark backlog (F-3 → CH-006 v1.48.0, F-2 → v1.49.0,
  F-1 → this release).

## [1.49.0] — 2026-06-17

### Added
- **F-2 — honest WebFlux / functional-routing signal in `endpoints`.** The endpoint
  surface models annotation-based routing (`@RequestMapping`/`@GetMapping`, JAX-RS) only.
  Routes registered via the functional DSL (`route().GET("/path", handler)` /
  `RouterFunction` / `CustomEndpoint`) were silently invisible — the v1.47.0 field
  benchmark found `endpoints` returned **0** for all of halo (a reactive app with 168
  functional registrations across 51 files), which an agent could misread as "this app
  exposes no endpoints". `endpoints` now detects functional routing and reports a
  `functional_routing` block (`files`, `route_registrations`, `modeled: false`) plus a
  warning; when the annotation surface is empty but functional routes exist, the warning
  explicitly says not to read it as "no endpoints".

  Deliberately does **not** synthesize endpoint entries: the literal DSL paths are
  relative (real paths depend on `nest()`/group-version prefixes unresolvable statically),
  and emitting partial paths would mislead more than an empty surface (same false-positive
  hazard CH-006 just removed). Full functional-route modeling is a separate effort.
  Validated: halo → 168 registrations surfaced; shopizer (annotation MVC) → no false
  trigger, 286 endpoints unchanged. 3 regression tests
  (`test_functional_routing_surface.py`).

## [1.48.0] — 2026-06-17

### Fixed
- **CH-006 — hub-interface caller over-expansion (false-positive callers) in
  `impact-chain`.** `implements` and `extends` are structural type declarations, not
  calls, but they were being traversed in the caller BFS. The reverse edge on an
  interface/base lists its implementors/subclasses, so querying a class that implements a
  high-fanout in-repo interface attributed **every sibling implementor** as a "direct
  caller". Found in the v1.47.0 field benchmark: `impact-chain ThumbnailEndpoint` on halo
  reported 42 direct callers — the other 42 `CustomEndpoint` implementors, none of which
  call it — inflating a leaf endpoint to `risk:high`. The shopizer monolith had the same
  pattern via its shared `Mapper<E,D>` base (45 phantom mapper callers on `ProductService`).

  Fix: add `implements`/`extends` to the BFS edge-skip set. This is loss-free — the wanted
  interface→implementation expansion (CH-001a/b) flows through `ImplementationGraph`
  indices, not these reverse-graph edges, and real callers travel `injects`/`calls` edges.
  Verified on both repos: halo `ThumbnailEndpoint` 42→0 false callers (`risk:high`→`low`);
  shopizer `ProductService` real callers preserved (32 direct unchanged) while 45 phantom
  `Mapper` callers — confirmed to have zero references to the queried symbol — were dropped.
  2 regression tests (`TestHubInterfaceOverExpansion`): sibling implementors excluded, real
  `injects` caller through a shared interface preserved.

  Complements CH-005: that guard handles *external* supertypes (under-reporting); CH-006
  handles *in-repo high-fanout* supertypes (over-reporting). False positives are worse than
  an empty result — they actively misdirect a change — so this is the higher-leverage half.

## [1.47.0] — 2026-06-17

### Added
- **CH-005 — framework/external-interface DI blind-spot detection in `impact-chain`.**
  When a queried class has an empty blast radius *and* implements/extends an external
  framework supertype (one the in-repo `ImplementationGraph` deliberately drops — e.g.
  Spring Security's `RedirectStrategy`, a servlet `Filter`), `impact-chain` now positively
  detects it instead of silently reporting `0 callers / risk:low` at `confidence=high`.
  Such classes are wired by framework DI/config and invoked polymorphically through the
  external type, so no in-repo edge names their methods — the empty result is an
  unmodeled-edge blind spot, not proof of dead code. The query now:
  - drops `confidence` to `low`,
  - exposes `metadata.blind_spots` (`framework_di`) and `metadata.external_supertypes`,
  - emits a `CH-005` warning pointing the agent to search DI/security/config wiring for
    the supertype to recover the real callers.

  Inert marker interfaces (`Serializable`, `Cloneable`, `Externalizable`) are excluded —
  they carry no methods, so no polymorphic dispatch and no hidden blast radius.

  This converts a dangerous false negative ("looks safe to change") into an honest "look
  further" signal. It does **not** recover the real callers (they flow through framework
  wiring the static call-graph never traverses); that is a separate, larger effort.
  Mirrors the existing CH-003 value-type guard. Validated end-to-end on BroadleafCommerce
  (`LocalRedirectStrategy` → flagged; `FieldDaoImpl` with 16 real callers → not flagged).

## [1.46.0] — 2026-06-16

### Added
- **Two Java/Spring agent flow presets in the MCP orchestrator.** These wrap existing
  tools into one-call, intent-routed flows so an agent can describe a task in natural
  language and get the right sequence without reading docs:
  - `run_migrate_flow` — wraps `migrate-check` for Spring Boot 2→3 planning and lifts a
    top-level `headline` (`readiness_score`, `blocking_count`, `estimated_effort_days`,
    `by_severity`, `by_target`) so the agent need not parse the full report.
  - `run_security_audit_flow` — wraps `spring-audit` + `endpoints`. **Config-less
    safeguard:** when no `sourcecode.config.json` is present and every endpoint reads
    `none_detected`, the flow flags a likely custom-annotation blind spot
    (`quality_warnings`) and returns a ready-to-paste `security_config_hint`, instead of
    letting a misleading 100%-unsecured surface stand (prevents a false negative).
- **Intent routing for migration and security audit.** New `INTENT_MIGRATION` /
  `INTENT_SECURITY_AUDIT` detection plus orchestration rules R5 (migration → lead with
  `get_migration_readiness`) and R6 (security audit → `get_endpoints` first, mirroring R2)
  route `start_session` / `analyze_task` to the new presets.

  Note: these presets add no new analysis — they are orchestration ergonomics over tools
  that were already callable directly. The standout value is the config-less safeguard.

## [1.45.0] — 2026-06-16

### Fixed
- **P1 — `cache clear` hung indefinitely in non-interactive contexts (CI, MCP, pipes).**
  Without `--yes`, the command called `click.confirm()`, which blocks reading stdin
  forever when stdin is an open pipe that never sends EOF (exactly the CI/agent case) —
  breaking pipelines and leaving the cache uncleared. The confirm prompt is now gated
  behind `sys.stdin.isatty()`: interactive terminals still prompt, while non-interactive
  runs proceed immediately (clear is idempotent; RIS stays preserved unless `--all`) and
  print a one-line notice. Verified: old path times out on a never-EOF pipe, new path
  returns in ~0.2s with exit 0.
- **`--no-cache` rejected by analysis subcommands ("No such option").** The flag existed
  only on the root analysis command, so `sourcecode endpoints --no-cache` (and `validation`,
  `spring-audit`, `migrate-check`, `impact`, `impact-chain`) aborted with exit 2, breaking
  scripted invocations that pass a uniform flag. These subcommands always read fresh source,
  so `--no-cache` is now accepted as a documented no-op on each.
- **`endpoints --limit` reported incoherent security counters.** `total` was recomputed to
  the limited set while `no_security_signal` / `undocumented` kept their repo-wide values
  (e.g. `total:2` next to `no_security_signal:3996`). The counters are now recomputed over
  the filtered set; the repo-wide originals are preserved under `_filter.*_before_filter`.
- **`validation` returned all-zeros silently when no OpenAPI spec was present.** A repo with
  no spec (no `target/generated-sources`/spec on disk) produced an empty result with no
  explanation, easily misread as "no validation anywhere". The result now carries an explicit
  `openapi_spec: null` + `note` field (also echoed to stderr) clarifying that declarative DTO
  constraints can't be recovered without a spec and that zero counts are expected, not a finding.

### Notes
- MCP orchestrator (`mcp/orchestrator.py`) gained a scoped TODO documenting three planned
  high-value Java/Spring flow presets (`run_migrate_flow`, `run_security_audit_flow`, and an
  R2-rule extension) — documented only, not implemented.

## [1.44.0] — 2026-06-16

### Fixed
- **CH-004c — annotation-free structural classes dropped their inheritance edges.**
  `build_repo_ir`'s fast pre-scan skips files with no recognized annotation marker,
  emitting only minimal class-name symbols (for same-package resolution) and **no
  relations**. A class with no annotation and no injected field of its own that
  participates purely structurally — `class X extends Base implements I {}` — therefore
  lost its `extends`/`implements` edges, so `implementation_graph` could not link
  sub→supertype and impact analysis could not traverse the hierarchy. The pre-scan now
  also keeps a file when a type declaration carries an `extends`/`implements` clause
  (`_INHERIT_PRESCAN_RE`), routing it through full extraction so its inheritance edges
  are built and resolved in pass 2 with the same-package map. Annotation-free leaf
  classes with no inheritance still skip, preserving the pre-scan optimization. Closes
  the last open layer of CH-004 (a/b shipped in 1.43.0).

## [1.43.0] — 2026-06-16

### Fixed
- **CH-004a — impact graph dropped field-injection-only classes.** `build_repo_ir`'s fast
  pre-scan skips files with no recognized annotation marker; the marker set included
  `@Inject` but not `@Autowired`, `@Resource`, `@Qualifier`, or `@Value`. A class wired
  purely by field injection with no class-level stereotype (e.g. an abstract base controller
  that holds the services its concrete subclasses inherit) was skipped entirely, so its
  `injects` edges never existed and `impact-chain` could not traverse through it. Added the
  field/setter-injection annotations (`@Autowired`, `@Resource`, `@Qualifier`, `@Value`,
  `@PersistenceContext`, `@PersistenceUnit`) to the pre-scan marker set.
- **CH-004b — same-package supertypes were not FQN-resolved.** The `extends`/`implements`
  edge builder resolved supertype names only via `import_map`, so a same-package
  `extends Base` (which needs no Java import) produced a **bare-name** edge target. The
  `implementation_graph` could then not link sub→supertype, making same-package class
  hierarchies invisible to impact analysis. Supertypes are now resolved via
  `_resolve_dep_type` (import + same-package + wildcard), matching `injects`/constructor edges.

Both fixes were surfaced by a field test on BroadleafCommerce (2985-file monolith); they
under-reported blast radius on any large repo, not just that one. See
`docs/eval/2026-06-16-broadleaf-checkout-impact-fieldtest.md`.

## [1.42.0] — 2026-06-16

### Added
- **Fase 22 — type-usage edges in `impact-chain` (CH-003).** Value/DTO/response
  types previously had an invisible blast radius: the impact graph modelled call and
  DI/injection edges but not how a type is wired *by type*. Two new edges close this:
  - **`returns`** — `method → returnTypeFQN` (method-level). For a `@ResponseBody`
    handler returning a domain type this is the only link from the type back to its
    endpoint, so `_collect_endpoints` now surfaces that route precisely.
  - **`instantiates`** — `class → T` for `new T(...)`, giving build-only value types
    (commands, receipts) a visible blast radius. Controller-like classes are excluded
    (already covered precisely by `returns`) to avoid broadening a DTO's impact to
    every route on the controller.

### Fixed
- **Inline-annotation method parsing.** `_METHOD_DECL_RE` could not match a
  modifier-position annotation (`public @ResponseBody Vets foo()`); the whole
  declaration failed and the method — its endpoint *and* return type — was silently
  dropped. Inline annotations are now consumed and folded into the method's annotation
  set. Recovers, e.g., the `GET /vets` handler in spring-petclinic `VetController`.

### Changed
- **Safety guard for type-usage blind spots (CH-003 Part 1).** A fully empty blast
  radius on a positively-identified plain value type (node present, `symbol_kind` in
  class/enum/record, no stereotype annotation, role `other`, not a controller) is no
  longer reported at `confidence: high` — it drops to `low` with a warning that an
  empty result is not proof the type is unused. Spine symbols and incomplete-IR cases
  keep prior behaviour. The guard now stays dormant when real type-usage edges exist.

### Field test (spring-petclinic #2333)
- `impact-chain Vets`: was `confidence: high` with 0 callers / 0 endpoints (a dangerous
  false zero) → now `high`, caller `showResourcesVetList`, endpoint `GET /vets`.
- `impact-chain VetController`: was 1 of 2 endpoints → both (`GET /vets` + `/vets.html`).

## [1.41.0] — 2026-06-16

### Added
- **Fase 21 — spec-recovered HTTP routes reach `impact-chain`.** In
  openapi-generator "interface-only" repos (`@RestController implements XxxApi`,
  mappings on the generated interface under `target/generated-sources`), the HTTP
  surface is recovered from the OpenAPI spec. That recovery previously lived only in
  the `endpoints` command path, so `impact-chain` on a repository/service symbol
  reported `endpoints_affected = 0` even though the route existed. The spec→controller
  linking is now shared and wired through the impact model end to end.
- **21-02** — `_recover_openapi_spec_routes` (repository_ir): single shared helper that
  links spec operations to interface-defined controllers and emits both the
  `endpoints`-command shape and the `route_surface` shape. `build_repo_ir` merges the
  spec-sourced `route_surface` entries, so they flow `route_surface → CanonicalRepositoryIR
  → EndpointIndex → impact-chain`. The `endpoints` command output stays byte-identical.
- **21-03** — `impact-chain` BFS now crosses the interface DI boundary mid-chain.
  `_bfs_callers` takes the `ImplementationGraph` and, for each implementation class it
  reaches, folds in the reverse edges of that class's interfaces (callers inject the
  interface type, so the `injects` edges sit on the interface node). Closes
  `repo → serviceImpl → (service interface) → controller` so the spec-recovered endpoint
  surfaces. CH-001b already did this for the seed; 21-03 extends it to every impl in the
  traversal.

### Fixed
- **BUG-PARSER-002 — multi-line constructor/method signatures.** A signature whose
  parameter list spans several physical lines (the canonical Spring constructor-injection
  idiom, one param per line) lost its parameters: the per-line decl regex captured `[^)]*`
  up to end-of-line only, so `param_types` was empty and no `injects` edges were emitted.
  The pre-join pass now balances parentheses for declaration openers, mirroring the
  existing multi-line class-declaration join.
- **BUG-PARSER-003 — wildcard-import dependency resolution.** A dependency type pulled in
  via `import pkg.*` was never resolved to an FQN (`import_map` skips `.*`), so even a
  single-line constructor produced no `injects` edge. `_build_relations` now receives the
  global `{package → {simple → FQN}}` map and resolves wildcard-imported types against it.

### Why
Field test of spring-petclinic-rest issue #11 (weakness #2): `impact-chain` on a repo or
service symbol surfaced no affected HTTP endpoints in interface-only openapi-generator
repos, even though the `endpoints` command listed the routes. The break was structural at
several layers — the spec→controller linking never reached `route_surface`/the CIR, the DI
chain dead-ended at the service impl, and (the live-repo blocker) the very first hop
repo→service was missing because petclinic's `ClinicServiceImpl` uses a multi-line
constructor with a wildcard repository import. With all four fixes, the live E2E
`impact-chain VetRepository` on petclinic-rest goes from **0** affected endpoints to the
full transitive set (**35**, reaching every controller incl. the spec-recovered v2 routes).

## [1.40.0] — 2026-06-16

### Added
- **CH-001c — interface impact models implementors.** `impact-chain` over an
  interface or abstract base now resolves its full in-repo descendant set:
  concrete `implements` classes, `extends` sub-interfaces, and subclasses, traversed
  transitively. A base interface query reaches impls hidden behind an intermediate
  sub-interface (e.g. Spring Data repositories: `SpringDataVetRepository extends
  VetRepository` alongside the JPA/JDBC impls).
- `ImplementationGraph` gains `subtypes_of()`, `supertypes_of()`, and
  `all_subtypes_of()` (transitive, cycle-safe). `extends` edges are now captured;
  `implements`-only indices (`implementations_of`/`primary_implementation`) keep their
  strict DI-resolution semantics — sub-interfaces are not counted as bean implementations.
- `ImpactChainResult.implementations`: new output field listing the in-repo subtypes
  of the queried type, making the implementation blast radius visible (previously the
  impls were silent BFS seeds).

### Why
Field test of spring-petclinic-rest issue #11 surfaced the gap: `impact-chain
VetRepository` returned only the SpringData sub-interface, missing the JPA/JDBC impls —
exactly the "3 impls" graph the maintainer cared about. Interface impact did not model
implementors.

## [1.33.0] — 2026-05-29

### Changed
- **Repositioned product identity** around persistent structural cache and ultra-fast repeated analysis for AI coding agents. Cache is now the central product story, not a performance feature.
- README rewritten: new intro emphasizing persistent context engine, cache performance benchmarks promoted above quickstart, agent workflow patterns section added, "Java/Spring analysis CLI" framing moved down.
- `pyproject.toml` description updated: "Persistent structural context and ultra-fast repeated analysis for AI coding agents".
- CLI `--help` updated: tagline, cold/warm latency numbers, cache commands section added prominently.

## [Unreleased]

### Added
- `prepare-context generate-tests --include-config`: opt-in flag to include tooling
  config files (`.eslintrc*`, `karma.conf.js`, `jest.config.js`, etc.) in `test_gaps`.
  By default these are now excluded (IMP-1).

### Fixed
- **BUG-1** `repo-ir` stdout: JSON is now written via `stdout.buffer` (UTF-8) so Unicode
  characters (e.g. `→`) survive on Windows consoles with non-UTF-8 codecs.
  `main_entry` also calls `stdout.reconfigure(encoding='utf-8')` on startup.
- **BUG-2** `--exclude` with a space-separated value (`--exclude "a,b"`) was silently
  consumed as the repository path. Added `--exclude` to the options-with-value registry
  so its argument is parsed correctly.
- **BUG-3** `prepare-context onboard --fast` returned only the git-changed file
  (e.g. `.idea/vcs.xml`). Fast mode for `onboard` now always uses a shallow depth-2
  scan so manifests and entry points are reliably discovered.
- **BUG-4** `angular_version: null` when `package.json` has `"dependencies": null`.
  The merge now uses `or {}` so an explicit `null` key doesn't raise TypeError.
  Also checks `peerDependencies` as a fallback source.
- **BUG-5** `lazy_routes_count: 0` in Angular projects. Counting now uses
  `loadChildren:` and `loadComponent:` (property syntax) instead of the defunct
  `loadChildren(` call syntax.
- **BUG-6** Angular `*.component.ts` files classified as Spring `@Service` in
  `review-pr` and `prepare-context` output on fullstack Java+Angular repos.
  Root cause: `"component"` was in `_SERVICE_KW` inside `_classify_changed_file`.
  Fix: Angular detection block (by `.ts` stem suffix) now runs **before** the
  Java/Spring heuristics. `"component"` removed from `_SERVICE_KW`. Added
  `ng_component`, `ng_pipe`, `ng_directive`, `ng_guard`, `ng_interceptor`,
  `ng_resolver`, `ng_service`, `ng_module` to `_ARTIFACT_CHANGE_EFFECT`.
  `ast_extractor._detect_role` updated with the same Angular stem-suffix map.
- **BUG-7** `--compact` help text referenced `--slim (when available)` which is
  not implemented and does not exist as a CLI option, causing user confusion
  (`Error: No such option '--slim'`). Removed the reference (Option A: remove
  mention rather than implement the flag this sprint).

### Regression tests added (`tests/test_bug_fixes_v13122.py`)
- 13 exit-code tests covering all commands reported as EXIT 255 — all verified
  to return EXIT 0 (BUG-1 through BUG-7 of this audit cycle).
- 8 Angular classification tests locking `ng_component` / `ng_service` / `ng_*`
  artifact types and `_ARTIFACT_CHANGE_EFFECT` entries.
- 3 `--slim` tests verifying the option is absent from help and CLI surface.
- 6 `angular_version` parsing tests covering `dependencies`, `devDependencies`,
  `peerDependencies`, `null` JSON values, and version prefix stripping.

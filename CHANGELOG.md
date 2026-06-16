# Changelog

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

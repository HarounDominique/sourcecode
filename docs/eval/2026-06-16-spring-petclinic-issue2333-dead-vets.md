# Field test — spring-petclinic #2333 (dead `Vets` object), real agentic workflow

**Date:** 2026-06-16
**Repo under test:** `spring-projects/spring-petclinic` (canonical Spring Boot MVC sample,
fresh `--depth 1` clone, Spring Boot 4.0.3, 109 Java files). NOT petclinic-**rest** — a
different, larger, Thymeleaf+JPA codebase, chosen to test cold-start value on an unseen repo.
**sourcecode version:** 1.41.0
**Issue:** [#2333](https://github.com/spring-projects/spring-petclinic/issues/2333) —
"[BUG] Vets object created but never added to model in `VetController#showVetList`".

## Why this issue

The remaining open petclinic-**rest** issues were weak for a fix demo (#19 empty body;
#125 a sprawling, subjective aggregate redesign). #2333 is the opposite: a concrete,
reproducible dead-code bug with a verifiable fix and an existing test pinning the
controller→template contract. It also exercises a code shape the prior field tests never
hit — a **value/DTO type** (`Vets`) used by instantiation and as a `@ResponseBody` return,
rather than an injected interface.

## The bug

`VetController.showVetList` (the paginated HTML view) builds a `Vets` wrapper, fills it,
then never uses it — `addPaginationModel` only reads `paginated.getContent()` and exposes
it as the `listVets` model attribute:

```java
Vets vets = new Vets();                      // dead
Page<Vet> paginated = findPaginated(page);
vets.getVetList().addAll(paginated.toList()); // dead
return addPaginationModel(page, paginated, model);
```

The sibling endpoint `GET /vets` (`showResourcesVetList`, `@ResponseBody`) uses `Vets`
correctly for JSON/XML mapping — so the type is not globally dead, only the local in
`showVetList` is.

## The fix

Remove the 4 dead lines (object + misleading copy-paste comment). `paginated` is retained
(consumed by `addPaginationModel`):

```diff
 	@GetMapping("/vets.html")
 	public String showVetList(@RequestParam(defaultValue = "1") int page, Model model) {
-		// Here we are returning an object of type 'Vets' rather than a collection of Vet
-		// objects so it is simpler for Object-Xml mapping
-		Vets vets = new Vets();
 		Page<Vet> paginated = findPaginated(page);
-		vets.getVetList().addAll(paginated.toList());
 		return addPaginationModel(page, paginated, model);
 	}
```

**Verification:** `./mvnw test -Dtest=VetControllerTests` → `Tests run: 2, Failures: 0,
Errors: 0`, BUILD SUCCESS. The existing `showVetListHtml` test asserts
`model().attributeExists("listVets")` — never `vets` — so the contract is provably
unaffected. The Thymeleaf template `vets/vetList.html` iterates `${listVets}` (no `vets`
reference), independently confirming the removed object reached no consumer.

## What sourcecode accelerated

- **Cold start was instant.** `impact-chain` on a never-seen 109-file repo: total wall
  time **0.28 s** (`model_build 0.43 ms`, `query 2.1 ms`), no `mvn`, no generated sources.
  This is the product's core promise and it held on an unfamiliar repo.
- **The DI/endpoint chain resolved correctly out of the box.** `impact-chain VetRepository`
  → `callers: 1` (VetController), `endpoints: 1`. The repo→controller→endpoint wiring the
  petclinic-**rest** Fase 21 work fixed generalizes cleanly to this different repo — single-
  line constructor injection (`VetController(VetRepository)`) resolved, no regression.

## What did NOT work — weakness surfaced (the valuable part)

`impact-chain Vets` returned an **all-zero blast radius** despite `Vets` being live:

```json
{ "symbol": "...vet.Vets", "resolution": "class_expanded",
  "direct_callers": [], "indirect_callers": [], "endpoints_affected": [],
  "risk_level": "low", "confidence": "high" }
```

`Vets` is instantiated twice in `VetController` and is the declared return type of the
`GET /vets` `@ResponseBody` endpoint, yet impact-chain sees **0 callers, 0 endpoints** —
reported with `confidence: high`. Corroborating: `impact-chain VetController` reports
`endpoints: 1`, but the controller has **two** `@GetMapping`s (`/vets.html` + `/vets`); the
`@ResponseBody` route that returns a domain type is the one missing.

**Diagnosis (hypothesis):** the impact graph models *call* and *DI/injection* edges but not
**type-usage edges** — constructor instantiation (`new Vets()`), local-variable type, and
method **return type**. For a service/repository the call+DI edges cover the real blast
radius; for a **value/DTO/response type** they cover nothing, so its impact is invisible.
This is a new failure class, distinct from the petclinic-rest interface-DI gaps
(weaknesses #1/#2). Candidate roadmap item: **CH-002 — model type-usage edges
(instantiation + return-type), especially `@ResponseBody` return types as endpoint links.**

**Honest impact on this task:** for *this specific bug*, sourcecode did **not** help reach
the answer. An all-zero result on a type that is in fact used is worse than no signal — read
literally it suggests `Vets` is globally dead, which would make "just delete the class" look
safe (it isn't — `/vets` depends on it). The confirmation that came instead from reading the
controller, grepping the Thymeleaf template for `${listVets}`, and the existing
`attributeExists("listVets")` assertion. Classic manual triangulation.

## Lessons / meta

- Repeats the Fase 21 lesson from the other direction: the tool is strong on the
  **call/DI spine** (repo→service→controller) and blind off it. Value types, DTOs, response
  bodies, and anything wired by *type* rather than *call* fall through.
- A **false zero reported at `confidence: high`** is the dangerous shape. If type-usage
  edges aren't modeled, a value-type query should at minimum drop confidence or warn
  ("no usage edges modeled for this symbol kind"), not assert an empty, high-confidence
  blast radius.
- Cold-start latency and the call/DI chain are genuinely production-grade on an unseen
  canonical repo. The gap is edge *coverage*, not performance or the core graph.

## Artifacts

- Fix: `spring-petclinic-fix` working tree, `VetController.java` (-4 lines), tests green.
- Issue: spring-projects/spring-petclinic#2333.
- Follow-up candidate: CH-002 (type-usage / return-type edges in the impact graph).

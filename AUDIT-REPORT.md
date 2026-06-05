# Sourcecode `explain` + `pr-impact` — Audit Report

**Date:** 2026-06-05  
**Auditor:** Independent QA  
**Tool version:** sourcecode 1.35.14  
**Scope:** `sourcecode explain <ClassName> [PATH]` and `sourcecode pr-impact [PATH] --files <file>`  
**Repos tested:** spring-petclinic (47 Java), spaghetti-api (5 Java), openmrs-core (1281 Java), BroadleafCommerce (2985 Java), keycloak (7885 Java)

---

## 1. Executive Summary

**Verdict: PASS WITH ISSUES**

Both commands are functional and useful. Determinism is perfect (100%). Performance is acceptable across all repo sizes. The command structure, endpoint detection, and transaction reporting are largely correct for repos using **constructor injection**.

However, two **HIGH-severity systemic bugs** affect repos that use **`@Autowired` field injection** or **`@Resource` injection** (the dominant pattern in openmrs-core, BroadleafCommerce, and other real-world Spring apps):

1. **Phantom callers bug**: `explain` reports field names (e.g., `dao`, `identifierValidators`) as incoming callers instead of real injecting classes. `pr-impact` similarly includes field-qualified FQNs (e.g., `AllergyValidator.patientService`) in `direct_callers`.
2. **Missing outgoing deps**: `explain` reports empty `Calls`/`outgoing_deps` for classes using `@Autowired` field injection, even when dependencies are clearly present.
3. **Field FQN pollution in `modified_classes`**: `pr-impact` includes field FQNs (e.g., `PatientServiceImpl.dao`) in the `modified_classes` list, making the report misleading.

These three bugs share a single root cause (see Section 10). They do not affect repos using pure constructor injection (spring-petclinic, spaghetti-api).

**Key numbers:**
- 6 controllers tested in petclinic: 5/6 fully correct, 1 with missing method + endpoint
- 5 services tested across openmrs-core and BroadleafCommerce: all affected by phantom callers / missing deps bug
- 3 repositories tested: all correct for callers/deps
- Determinism: 5/5 identical runs for both commands on all tested classes
- False positive rate (incoming_callers): ~100% for @Autowired field injection repos
- False negative rate (outgoing_deps): ~100% for @Autowired field injection repos

---

## 2. `explain` Command Assessment

### What works correctly

**Spring Petclinic (constructor injection — fully correct):**

All 6 controllers validated. FQNs, stereotypes, endpoints correct on 5 of 6. Outgoing deps correct (constructor injection only). Example:

```
sourcecode explain OwnerController /path/to/spring-petclinic
```
Output (JSON excerpt):
```json
{
  "class_fqn": "org.springframework.samples.petclinic.owner.OwnerController",
  "stereotype": "controller",
  "public_methods": ["findOwner", "initCreationForm", "initFindForm", "initUpdateOwnerForm",
                     "processCreationForm", "processFindForm", "processUpdateOwnerForm",
                     "setAllowedFields", "showOwner"],
  "incoming_callers": [],
  "outgoing_deps": ["OwnerRepository"],
  "rest_endpoints": ["GET /owners", "GET /owners/find", "GET /owners/new",
                     "GET /owners/{ownerId}", "GET /owners/{ownerId}/edit",
                     "POST /owners/new", "POST /owners/{ownerId}/edit"]
}
```
Source match: all 9 public methods present, all 7 endpoints present. Correct.

**Event detection** works correctly. `ResourceBundlingServiceImpl` in BroadleafCommerce:
```json
{"events_consumed": ["ContextRefreshedEvent"], "events_published": []}
```
Source confirms: `@EventListener public void initializeResources(ContextRefreshedEvent event)`. Correct.

**Non-existent class** handled gracefully:
```
sourcecode explain NonExistentClass /path/to/repo
→ Purpose: "Class not found in repository symbols."
→ Warnings: ["'NonExistentClass' not found in CIR symbols."]
```

**Ambiguous class name** reports warning and picks the first match:
```
sourcecode explain ConceptDatatype /path/to/openmrs-core
→ Warnings: ["Ambiguous: 2 classes named 'ConceptDatatype'. Showing first: org.openmrs.customdatatype.datatype.ConceptDatatype"]
```
Disambiguates from `org.openmrs.ConceptDatatype` (the core model). The picked class may not be what the user expects, but the warning is shown.

**Repository interface** (`OwnerRepository`) correctly shows `Used By: OwnerController, PetController, VisitController`. Correct.

### What is broken

**Phantom incoming callers for @Autowired field injection:**

```
sourcecode explain PatientServiceImpl /path/to/openmrs-core
→ Used By: dao, identifierValidators
```

Source: `PatientServiceImpl` has `@Autowired private PatientDAO dao` and `@Autowired @Qualifier("identifierValidators") private Map<...> identifierValidators`. These are FIELDS, not classes that use PatientServiceImpl.

Actual callers of PatientService include `ObsServiceImpl`, `EncounterServiceImpl`, `AllergyValidator`, etc. — none reported.

Same bug confirmed for `EncounterServiceImpl` (`Used By: dao`), `ObsServiceImpl` (`Used By: dao, handlers`), `ConceptServiceImpl` (`Used By: dao`), `BroadleafApplicationEventPublisherImpl` (`Used By: applicationContext`).

**Empty outgoing_deps for @Autowired field injection:**

```
sourcecode explain PatientServiceImpl /path/to/openmrs-core --format json
→ "outgoing_deps": []
```

Source: `PatientServiceImpl` has `@Autowired private PatientDAO dao`. The dependency on `PatientDAO` is not reported.

Same for `EncounterServiceImpl` (has `@Autowired private EncounterDAO dao` → `outgoing_deps: []`), `ConceptServiceImpl`, `ObsServiceImpl`.

**Root cause traced to `InjectionGraph.build()` in `cir_graphs.py`:**

The CIR stores `@Autowired` field injection edges with a dot-notation field FQN:
```json
{"from": "org.openmrs.api.impl.PatientServiceImpl.dao", "to": "org.openmrs.api.db.PatientDAO", "type": "injects"}
```

`InjectionGraph.build()` (cir_graphs.py line 183) handles `#` in FQN (constructor/method) but NOT `.` dot-notation field FQNs:
```python
if "#" in from_fqn:
    class_fqn = from_fqn.rsplit("#", 1)[0]
else:
    class_fqn = from_fqn  # ← treats "PatientServiceImpl.dao" as class_fqn
```

Result: `_deps_of["PatientServiceImpl.dao"] = ["PatientDAO"]` instead of `_deps_of["PatientServiceImpl"] = ["PatientDAO"]`.

When `explain._build_deps("PatientServiceImpl", cir)` is called, `injection_graph.dependencies_of("PatientServiceImpl")` returns `[]`.

The phantom callers come from `reverse_graph[PatientServiceImpl]['contained_in']` which includes field nodes `PatientServiceImpl.dao` and `PatientServiceImpl.identifierValidators`. In `_build_callers` (explain.py line 259):
```python
cls_fqn = caller_fqn.rsplit("#", 1)[0]  # no "#" → no stripping of ".fieldName"
if cls_fqn == class_fqn: continue  # "PatientServiceImpl.dao" != "PatientServiceImpl" → NOT skipped
s = _simple(cls_fqn)  # → "dao"
result.append("dao")  # phantom caller!
```

---

## 3. `pr-impact` Assessment

### What works correctly

**Constructor-injection repos (spring-petclinic):**

```
sourcecode pr-impact /path/to/spring-petclinic --files /tmp/ownerrepo_changed.txt
→ modified_classes: ["org.springframework.samples.petclinic.owner.OwnerRepository"]
→ direct_callers: ["OwnerController", "PetController", "VisitController"]
→ affected_endpoints: [all 13 endpoints across 3 controllers]
→ risk_level: CRITICAL
```

This is correct: OwnerRepository is used by all three controllers, all endpoints propagate through.

**Transactional methods** are correctly identified when present.

**Event flow** is correctly detected (tested with BroadleafCommerce event publishers).

**Risk level** is reasonable and follows documented logic (endpoints → CRITICAL, transaction boundary → boost).

### What is broken

**Field FQN contamination in `modified_classes`:**

```
sourcecode pr-impact /path/to/openmrs-core --files /tmp/patient_changed.txt --format json
→ "modified_classes": [
    "org.openmrs.api.impl.PatientServiceImpl",
    "org.openmrs.api.impl.PatientServiceImpl.dao",
    "org.openmrs.api.impl.PatientServiceImpl.identifierValidators"
  ]
```

`PatientServiceImpl.dao` and `PatientServiceImpl.identifierValidators` are field nodes, not classes. The tool runs `run_impact_chain()` on them as if they were modified classes.

Root cause: `_build_file_class_index()` (pr_impact.py line 150) filters by `"#" in fqn` to exclude method nodes but does NOT filter `symbol_kind == "field"`:
```python
if not fqn or not sf or "#" in fqn:
    continue  # ← field FQNs like "Class.fieldName" have no "#" → pass through
```
Field nodes share `source_file` with their enclosing class, so they get indexed alongside the class.

**Field FQN contamination in `direct_callers`:**

```
sourcecode pr-impact /path/to/openmrs-core --files /tmp/patient_changed.txt --format json
→ "direct_callers": [
    "org.openmrs.validator.AllergyValidator.patientService",
    "org.openmrs.validator.AllergyValidator",
    ...
  ]
```

`AllergyValidator.patientService` is a field FQN (AllergyValidator has `@Autowired private PatientService patientService`), not a class. The real class `AllergyValidator` is ALSO present — so there is a phantom duplicate.

Same pattern in BroadleafCommerce `pr-impact` for `CustomerServiceImpl`:
```
→ "modified_classes": [
    "CustomerServiceImpl",
    "CustomerServiceImpl.customerAddressDao",
    "CustomerServiceImpl.customerDao",
    "CustomerServiceImpl.customerForgotPasswordSecurityTokenDao",
    "CustomerServiceImpl.eventPublisher"
  ]
→ "direct_callers" includes "CustomerCustomPersistenceHandler.customerService",
    "CustomerPasswordCustomPersistenceHandler.customerService"
```
Both duplicate and phantom entries. 48 listed direct_callers — the real count after deduplication/filtering would be lower.

**Missing endpoint propagation:**

When `VetRepository` changes, only `GET /vets.html` appears in `affected_endpoints`. The `GET /vets` endpoint (which VetController also exposes) is missing due to the underlying CIR parsing bug (see Bug-5).

---

## 4. Regression Analysis

All three pre-existing commands still work:

| Command | Result |
|---------|--------|
| `sourcecode spring-audit /path/to/petclinic` | Runs successfully, 0 findings |
| `sourcecode spring-audit /path/to/openmrs-core` | Runs successfully, 1 finding |
| `sourcecode impact-chain OwnerController /path/to/petclinic` | Runs successfully, returns correct endpoints |
| `sourcecode explain OwnerController --format json` | Runs successfully, output correct |

No crashes, no missing output sections, no broken JSON. The impact-chain command also exhibits the field FQN issue in `direct_callers` (same root cause), confirmed: `direct_callers` for PatientServiceImpl includes `AllergyValidator.patientService`.

---

## 5. Determinism Analysis

**Result: Fully deterministic.** Zero variance across 5 runs for all tested classes.

```bash
# 5 runs of explain - all identical MD5:
ff6322e5ab7ba6c1f0e80c0c0d068c03 (×5) — OwnerController text format
cfa150bc9557262e35316a25c84dfb6e (×5) — OwnerController JSON format

# 5 runs of pr-impact - all identical MD5:
c9cb9a99dbc2a74cf25ff2b9b30067ea (×5) — OwnerController scenario
```

No ordering issues, no timestamp drift, no non-deterministic set ordering.

---

## 6. Performance Analysis

All measurements via `time` command (wall-clock):

| Command | Repo | Size | Time |
|---------|------|------|------|
| `explain OwnerController` | spring-petclinic | 47 files | **0.17s** |
| `explain PatientServiceImpl` | openmrs-core | 1281 files | **1.45s** |
| `explain RealmAdminResource` | keycloak | 7885 files | **7.89s** |
| `pr-impact OwnerController.java` | spring-petclinic | 47 files | **0.14s** |
| `pr-impact PatientServiceImpl.java` | openmrs-core | 1281 files | **1.46s** |
| `pr-impact RealmAdminResource.java` | keycloak | 7885 files | **7.43s** |

Performance is linear with repo size (warm cache). keycloak at ~8s is slow but within acceptable bounds for a 7885-file repo. No timeouts or runaway processes observed.

---

## 7. Edge Case Analysis

| Edge Case | Class Tested | Result | Pass? |
|-----------|-------------|--------|-------|
| No callers (leaf node) | `VetController` | Returns empty `incoming_callers`, no crash | PASS |
| No dependencies | `WelcomeController` | Returns empty `Calls`, no crash | PASS |
| POJO / model class | `Owner` | Returns FQN, public methods, no crash | PASS |
| JPA interface | `OwnerRepository` | Shows `Extends JpaRepository`, callers correct | PASS |
| Abstract class | `AbstractSnapshotTuner` | Shows methods, no crash | PASS |
| Non-existent class | `NonExistentClass` | `"Class not found"` warning, no crash | PASS |
| Ambiguous simple name | `ConceptDatatype` (2 in openmrs) | Picks first, warns about ambiguity | PASS with note |
| Large service (2279 LOC) | `ConceptServiceImpl` | Returns 138 unique methods, no crash | PASS |
| Utility class (static methods) | `OpenmrsUtil` | Returns methods, no crash | PASS |

All edge cases handled gracefully — no crashes or unhandled exceptions observed.

---

## 8. False Positive Analysis

**Context:** For repos using constructor injection (petclinic, spaghetti-api), precision is high.

**For repos using @Autowired field injection (openmrs-core, BroadleafCommerce):**

**incoming_callers / "Used By":** precision is approximately **0%** for service classes.

Evidence:
- `PatientServiceImpl`: reports `["dao", "identifierValidators"]` — both are field names, neither is a calling class.
- `EncounterServiceImpl`: reports `["dao"]` — field name, not a caller.
- `BroadleafApplicationEventPublisherImpl`: reports `["applicationContext"]` — field name.

**pr-impact `direct_callers`:** contains both real callers AND phantom field FQNs.

Evidence — `CustomerServiceImpl` in BroadleafCommerce reports 48 callers including:
- `CustomerCustomPersistenceHandler.customerService` (phantom — field FQN)
- `CustomerCustomPersistenceHandler` (real — correct)
- `CustomerPasswordCustomPersistenceHandler.customerService` (phantom)
- `CustomerPasswordCustomPersistenceHandler` (real — correct)

Estimated false positive rate in direct_callers for field-injected repos: **30–50%** of entries are phantom field FQNs.

**outgoing_deps:** false positive rate is effectively **0%** (when a dep is shown, it's real). But false negative rate is very high (see Section 9).

---

## 9. False Negative Analysis

**incoming_callers** for @Autowired field injection repos: **~100% miss rate** for real callers.

Evidence: `PatientServiceImpl` has real callers including `ObsServiceImpl`, `EncounterServiceImpl`, `AllergyValidator`, `HL7ServiceImpl` — none appear in the `incoming_callers` output (confirmed by grepping source). Only phantom field names appear.

**outgoing_deps** for @Autowired field injection: **~100% miss rate**.

Evidence:
- `PatientServiceImpl` has `@Autowired private PatientDAO dao` — `outgoing_deps: []`.
- `EncounterServiceImpl` has `@Autowired private EncounterDAO dao` — `outgoing_deps: []`.
- `ConceptServiceImpl` has `@Autowired private ConceptDAO dao` — `outgoing_deps: []`.

The CIR stores the injection edge correctly (`from: PatientServiceImpl.dao → to: PatientDAO`) but `InjectionGraph.build()` uses the field FQN as the class key, so `dependencies_of("PatientServiceImpl")` returns empty.

**endpoint detection:**

`GET /vets` endpoint for `VetController` is entirely missing from the CIR due to the array syntax parsing bug (`@GetMapping({ "/vets" })`). This is a parser-level miss, not an explain-level miss.

The VetController method `showResourcesVetList()` also does not appear in the CIR node list at all — confirmed by inspecting raw IR nodes.

---

## 10. Bugs Found

### BUG-1 — Phantom incoming callers for @Autowired field injection (HIGH)

**Severity:** HIGH  
**Reproducible:** 100% on openmrs-core, BroadleafCommerce, any repo using @Autowired field injection  
**Command affected:** `explain` (incoming_callers / "Used By")

**Evidence:**
```bash
sourcecode explain PatientServiceImpl /path/to/openmrs-core --format json
→ "incoming_callers": ["dao", "identifierValidators"]
```
Source: `@Autowired private PatientDAO dao` and `@Autowired private Map<...> identifierValidators` are fields, not callers.

**Root cause:** In `explain.py` `_build_callers()` (line 255–265), `reverse_graph[class_fqn]["contained_in"]` contains field nodes with dot-notation FQNs (e.g., `PatientServiceImpl.dao`). The check `cls_fqn = caller_fqn.rsplit("#", 1)[0]` does not strip the `.fieldName` suffix. `PatientServiceImpl.dao != PatientServiceImpl`, so the field passes the self-exclusion check and appears as a caller. `_simple("PatientServiceImpl.dao")` returns `"dao"`.

**Recommended fix:** In `_build_callers()`, after extracting `cls_fqn`, check if it's a known class symbol (or simply: if it doesn't appear as a key in CIR class symbols, skip it). Alternatively, detect the dot-notation field pattern: if `cls_fqn` is not in the known class FQN set, strip the last `.segment` to get the real class.

---

### BUG-2 — Empty outgoing_deps for @Autowired field injection (HIGH)

**Severity:** HIGH  
**Reproducible:** 100% on repos using @Autowired field injection  
**Command affected:** `explain` (outgoing_deps / "Calls")

**Evidence:**
```bash
sourcecode explain PatientServiceImpl /path/to/openmrs-core --format json
→ "outgoing_deps": []
```
Source confirms `@Autowired private PatientDAO dao`.

**Root cause:** `InjectionGraph.build()` (cir_graphs.py, line 183–187):
```python
if "#" in from_fqn:
    class_fqn = from_fqn.rsplit("#", 1)[0]  # handles constructor injection
else:
    class_fqn = from_fqn  # ← field FQN "PatientServiceImpl.dao" treated as class
```
Field injection edges have `from: "PatientServiceImpl.dao"` (dot notation, no `#`). The code maps `_deps_of["PatientServiceImpl.dao"] = ["PatientDAO"]` instead of `_deps_of["PatientServiceImpl"] = ["PatientDAO"]`. `dependencies_of("PatientServiceImpl")` returns `[]`.

**Recommended fix:** In `InjectionGraph.build()`, handle field FQN dot notation. After the `#` check, add: check if `from_fqn` is not a known class FQN (based on available class symbols), and if so, strip the last `.segment` to get the enclosing class. Or: the CIR builder should normalize field injection edges to use `Class#fieldName` consistently (like constructor injection uses `Class#<init>`).

---

### BUG-3 — Field FQN pollution in `pr-impact` modified_classes (HIGH)

**Severity:** HIGH  
**Reproducible:** 100% on repos using @Autowired / @Resource field injection  
**Command affected:** `pr-impact` (modified_classes)

**Evidence:**
```bash
sourcecode pr-impact /path/to/openmrs-core --files /tmp/patient_changed.txt --format json
→ "modified_classes": [
    "org.openmrs.api.impl.PatientServiceImpl",
    "org.openmrs.api.impl.PatientServiceImpl.dao",
    "org.openmrs.api.impl.PatientServiceImpl.identifierValidators"
  ],
"metadata": {"classes_analyzed": 3}
```
Only 1 class was actually changed, not 3.

**Root cause:** `_build_file_class_index()` (pr_impact.py line 150) filters method nodes via `"#" in fqn` but does NOT filter `symbol_kind == "field"` nodes. Field nodes have FQNs like `PatientServiceImpl.dao` (no `#`). openmrs-core has 'field' as a distinct `symbol_kind` in its CIR (confirmed: petclinic does not have field nodes → unaffected).

**Recommended fix:** Add symbol_kind filter in `_build_file_class_index()`:
```python
kind = node.get("symbol_kind") or node.get("type") or ""
if kind == "field":
    continue
```
Or alternatively: filter to only known class-level kinds: `if kind not in ("class", "interface", "enum", "annotation", ""):`.

---

### BUG-4 — Field FQN entries in `pr-impact` direct_callers (HIGH)

**Severity:** HIGH  
**Reproducible:** 100% on repos using @Autowired / @Resource field injection  
**Command affected:** `pr-impact` (direct_callers)

**Evidence:**
```bash
→ "direct_callers": [
    "org.openmrs.validator.AllergyValidator.patientService",  ← phantom field FQN
    "org.openmrs.validator.AllergyValidator",                 ← real caller (duplicate)
    ...
  ]
```
Source confirms: `AllergyValidator` has `@Autowired private PatientService patientService`. The field FQN appears alongside the real class, creating a duplicate phantom entry.

**Root cause:** In `spring_impact.py` (the `run_impact_chain` function), the BFS uses `reverse_graph[class_fqn]["injects"]` which contains entries like `AllergyValidator.patientService` (field FQN) for @Autowired field injections. These propagate into the callers list without filtering.

**Recommended fix:** After BFS caller resolution, filter out any FQN that is not a known class symbol (or strip dot-notation to resolve to enclosing class before deduplication).

---

### BUG-5 — @GetMapping array syntax not parsed (MEDIUM)

**Severity:** MEDIUM  
**Reproducible:** Specific to `@GetMapping({"/vets"})` array syntax with `@ResponseBody` return annotation  
**Command affected:** `explain` (public_methods, rest_endpoints), `pr-impact` (affected_endpoints)

**Evidence:**
```bash
sourcecode explain VetController /path/to/spring-petclinic
→ "Public Methods": ["showVetList"]           ← only 1 of 2 public methods
→ "REST Endpoints": ["GET /vets.html"]        ← only 1 of 2 endpoints
```

Source `VetController.java`:
```java
@GetMapping({ "/vets" })
public @ResponseBody Vets showResourcesVetList() { ... }
```
Method `showResourcesVetList` and endpoint `GET /vets` are **completely absent from the CIR** — the node does not exist in `raw_ir.graph.nodes` for VetController.

The likely cause: the parser doesn't handle the `public @ResponseBody Vets` pattern where `@ResponseBody` is inlined on the return type (unusual placement), OR it doesn't parse `@GetMapping({...})` with explicit array braces.

**Recommended fix:** Check the CIR parser (repository_ir.py or Java parser) for `@ResponseBody` on return type position and `@GetMapping({...})` array value syntax.

---

### BUG-6 — Overloaded transaction methods shown as duplicates (LOW)

**Severity:** LOW  
**Reproducible:** Any interface with overloaded @Transactional methods  
**Command affected:** `explain` (transactions)

**Evidence:**
```bash
sourcecode explain VetRepository /path/to/spring-petclinic
→ "Transactions": ["findAll() [readOnly]", "findAll() [readOnly]"]
```
Source has two overloads: `Collection<Vet> findAll()` and `Page<Vet> findAll(Pageable)`, both `@Transactional(readOnly=true)`. Both appear as `findAll() [readOnly]` without parameter differentiation.

**Recommended fix:** In `_build_transactions()`, include parameter type info from the method signature to distinguish overloads.

---

### BUG-7 — Protected methods shown in "Public Methods" section (LOW)

**Severity:** LOW  
**Reproducible:** Any class with protected methods  
**Command affected:** `explain` (public_methods)

**Evidence:**
```bash
sourcecode explain Vet /path/to/spring-petclinic
→ "Public Methods": ["addSpecialty", "getNrOfSpecialties", "getSpecialties", "getSpecialtiesInternal"]
```
Source `Vet.java`: `protected Set<Specialty> getSpecialtiesInternal()` — this is protected, not public.

`_build_public_methods()` (explain.py line 233–234) explicitly includes protected:
```python
if "public" not in modifiers and "protected" not in modifiers:
    continue
```

**Recommended fix:** Either rename the section to "Public/Protected Methods" in output, or change the filter to `"public" not in modifiers` only. The current behavior may be intentional for API visibility, but the label is misleading.

---

## Final Verdict

**PASS WITH ISSUES**

The `explain` and `pr-impact` commands are structurally correct and performant. For repos using **constructor injection only** (spring-petclinic, spaghetti-api), both commands produce accurate, reliable output.

For repos using **`@Autowired` / `@Resource` field injection** (the dominant pattern in openmrs-core, BroadleafCommerce, and most production Spring applications), Bugs 1–4 cause the `incoming_callers`/`Used By`, `outgoing_deps`/`Calls`, `modified_classes`, and `direct_callers` fields to be corrupted with phantom field-name entries and/or be empty when data exists.

These four bugs share a single root: **field FQN dot notation (`Class.fieldName`) is not normalized to class FQN (`Class`) in `InjectionGraph.build()`, `_build_file_class_index()`, and the BFS caller traversal in `run_impact_chain`.**

Fixing `InjectionGraph.build()` to strip `.fieldName` suffixes (treating them like `#fieldName` constructor injection) would resolve Bugs 1, 2, 3, and 4 simultaneously.

Bugs 5–7 are independent of the injection style and should be fixed separately.

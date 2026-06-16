# Field test — BroadleafCommerce checkout impact (cold-start on a 2985-file monolith)

**Date:** 2026-06-16
**Repo under test:** `BroadleafCommerce/BroadleafCommerce` (enterprise eCommerce Spring
monolith, fresh `--depth 1` clone, **2985 Java files / 38 MB**, multi-module Maven:
`common`, `core/broadleaf-framework`, `core/broadleaf-framework-web`, …).
**sourcecode version:** 1.42.0
**Anchor issue:** [#2554](https://github.com/BroadleafCommerce/BroadleafCommerce/issues/2554)
— "Order lock for performCheckout may not be released" (lock acquired before a validation
that can throw outside try-finally).

## Honest headline: mixed. Cold-start is great; endpoint blast radius is a false zero here.

This is a benchmark, not a showcase. The numbers below are reported as measured.

## What was tested

`impact-chain CheckoutServiceImpl#performCheckout` on the unbuilt repo (no `mvn`, no
generated sources), then `impact-chain DefaultPaymentGatewayCheckoutService#initiateCheckout`
to isolate a miss.

## The anchor bug is obsolete in HEAD

`performCheckout` in current `CheckoutServiceImpl` acquires **no lock** — the method was
refactored; the order-lock concern moved out. #2554 (2017) is effectively stale, same lesson
as the petclinic issues: mature repos' old issues are frequently already resolved upstream.
Also, the bug is intra-method control-flow (lock release on an exception path) — outside
sourcecode's structural model regardless.

## Numbers (measured)

- **Cold-start: 3.24 s wall** on 2985 Java files (`model_build 42.8 ms`, `query 79.2 ms`).
  Full structural IR of an enterprise monolith in ~3 s, zero config/build. The ~3 s is file
  I/O + parsing 2985 files; the graph build + query are sub-100 ms. Genuinely good.
- `impact-chain CheckoutServiceImpl#performCheckout`: resolution `class_expanded`,
  confidence `medium`, **1 direct + 8 indirect callers, 0 endpoints, no TX boundary**.

## What was correct

- **Direct caller exact.** `DefaultPaymentGatewayCheckoutService` is the only real caller of
  `performCheckout` (`initiateCheckout` → `checkoutService.performCheckout(order)`). ✓
- **Indirect callers real, no false positives.** The 8 indirect (workflow activities +
  rollback handlers, `ValidateAndConfirmPaymentActivity`, `ConfirmPaymentsRollbackHandler`,
  …) genuinely depend on `DefaultPaymentGatewayCheckoutService` — verified by grep. ✓

## `endpoints_affected = 0` — first read "false negative", then corrected to mostly TRUE negative

Initial diagnosis was that the checkout HTTP surface reaches `performCheckout` and the tool
missed it. **Investigation corrected this**: Broadleaf's `BroadleafCheckoutController` /
`BroadleafPaymentInfoController` carry **no `@Controller` and no `@RequestMapping`** — a grep
of the whole `…/controller/checkout/` dir finds zero mapping annotations. They are *framework
base classes*; the actual Spring MVC routes are defined in the **downstream demo
application** (a separate repo). So within this repo there is no checkout endpoint to find:
`endpoints = 0` is essentially correct here. The earlier "≥1 endpoint" claim was wrong — the
endpoints live out of repo. (Honesty note kept on purpose, per the project's no-trap-promo rule.)

## What the investigation DID surface — two real graph-completeness bugs (now fixed)

Chasing the chain exposed two genuine defects that were silently dropping nodes/edges from
the impact graph on any large codebase — independent of the (out-of-repo) endpoints:

- **CH-004a — pre-scan dropped field-injection-only classes.** `build_repo_ir`'s fast
  pre-scan skips files with no recognized annotation marker. The marker set had `@Inject`
  but **not `@Autowired`, `@Resource`, `@Qualifier`, `@Value`**. An abstract base wired
  purely by field injection with no class-level stereotype — exactly Broadleaf's
  `AbstractCheckoutController` — was skipped, so its `injects` edges never existed and
  impact-chain could not traverse through it. Fixed by adding the field/setter-injection
  annotations to the marker set.
- **CH-004b — same-package supertypes weren't FQN-resolved.** The `extends`/`implements`
  edge builder used only `import_map`, so a same-package `extends AbstractCheckoutController`
  (no import needed in Java) resolved to the **bare name**, never the FQN — so the
  `implementation_graph` could not link sub→supertype and same-package class hierarchies were
  invisible to impact analysis. Fixed by resolving supertypes via `_resolve_dep_type`
  (import + same-package + wildcard), matching how `injects`/constructor edges already resolve.

**Effect (measured, same query after the fixes):** `impact-chain performCheckout` direct
callers 1 → 3, indirect 8 → 11, and the abstract/base controllers now correctly enter the
blast radius (`AbstractCheckoutController`, `PaymentGatewayAbstractController`,
`BroadleafPaymentInfoController`) where before they were absent. Endpoints stay 0 — correctly,
because they are out of repo.

### Known remaining layer (CH-004c, not fixed here)

Classes with **no annotation at all** that participate only structurally (e.g. a concrete
`class X extends Base {}` with no stereotype and no injected field of its own) are still
pre-scan-skipped, so their `extends`/`implements` edges are lost. The fast pre-scan should
additionally emit inheritance edges for such files. Deferred — low marginal value for this
field test since the endpoints are out of repo regardless.

## Bottom line

- **Cold-start: production-grade** on an unseen 2985-file monolith (3.24 s, model build 43 ms).
- **Within-module direct/indirect precision: correct**, no false positives.
- **Two real graph-completeness bugs found and fixed** (CH-004a/b): field-injection-only
  classes and same-package supertypes were being dropped — a class of error that would under-
  report blast radius on *any* large repo, not just this one. That is the real, honest value
  of this field test — not a showcase number, a tool correctness fix backed by tests.
- The original "missed checkout endpoints" was an out-of-repo true negative, not a tool miss.

## Artifacts

- Repo: `broadleaf-fieldtest` working tree (clone only, unmodified).
- Issue: BroadleafCommerce/BroadleafCommerce#2554 (stale in HEAD — `performCheckout` no
  longer uses a lock).
- Fixes: CH-004a/b in `repository_ir.py` (v1.43.0), tests in `TestCH004GraphCompleteness`.
- Ground truth: `DefaultPaymentGatewayCheckoutService:250`, `BroadleafCheckoutController:272`.

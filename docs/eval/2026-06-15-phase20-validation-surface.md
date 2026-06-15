# Fase 20 — Validation Surface: comando `validation`

**Fecha:** 2026-06-15
**Versión:** sourcecode 1.39.0
**Origen:** backlog derivado de la prueba de campo #147 sobre `spring-petclinic-rest` (ver [eval #147](2026-06-15-petclinic-rest-issue147-validation.md)). La Fase 18 expuso los constraints declarativos en `endpoints`; faltaba un comando que **agregara** esa superficie por endpoint y la cruzara con los **validadores custom** del repo.

## Qué se construyó

Comando `sourcecode validation .` que une las dos fuentes de verdad de bean-validation que un agente necesita antes de tocar un request body:

1. **Constraints declarativos** de los DTOs de la spec OpenAPI (`@Pattern`/`@Size`/`@NotNull`, `minimum`/`maximum`, `enum`) — recuperados por la Fase 18 incluso cuando los DTOs son generados bajo `target/generated-sources` (no escaneado).
2. **Validadores custom** escritos a mano — anotación `@Constraint` + su `ConstraintValidator` (p.ej. `PetAgeValidator`) — descubiertos en `src/` y **enlazados** a los campos vía la extensión `x-field-extra-annotation` de openapi-generator.

- **`src/sourcecode/openapi_surface.py`** (extendido): `FieldConstraint.extra_annotations` + parseo de `x-field-extra-annotation` (string o lista → nombres simples). Expuesto en `to_dict()` como `extraAnnotations`.
- **`src/sourcecode/validation_surface.py`** (nuevo): `discover_custom_validators(root)` escanea `@Constraint(validatedBy=…)` sobre `@interface`, captura validador(es), `message()` default, `@Target`, y el tipo `T` de `ConstraintValidator<A,T>` (aunque el impl viva en otro archivo). `build_validation_surface(root)` produce, por endpoint con body: `validatedFields[{name, rules[], customValidators[]}]`, el catálogo `custom_validators`, los `gaps` (POST/PUT/PATCH sin validación declarada) y un `summary`. Defensivo: spec/clase malformada → superficie parcial, nunca excepción.
- **`src/sourcecode/cli.py`**: comando `validation` (json default, yaml), flags `--gaps-only`, `--path-prefix`. Registrado en `FORMAT_REGISTRY` y en `_SUBCOMMANDS` (sin esto, el preprocesado de path tragaba el nombre del subcomando).
- **`src/sourcecode/mcp/registry.py`**: alias curado `get_validation` (`repo_path` + `gaps_only`, docstring rico) siguiendo el patrón de `get_endpoints`; la tool cruda `validation` (6 params CLI) se oculta vía `_MCP_HIDDEN_CANONICAL_TOOLS`. `validate_registry()` → 0 drift.

## Verificación E2E (spring-petclinic-rest, checkout limpio, sin `mvn`)

```
sourcecode validation . → summary:
  endpoints_with_body: 15
  validated_fields: 44
  custom_validators_declared: 1   (PetAgeValidation → PetAgeValidator, LocalDate)
  custom_validators_linked: 1
  gaps: 0
```

Ejemplo recuperado — `POST /owners/{ownerId}/pets` (schema `PetFields`):
- `name` → `required`, `pattern ^[\p{L}]+…`, `minLength 1`, `maxLength 30`.
- `birthDate` → `required` + **custom** `PetAgeValidation` (`resolved: true`, validator `PetAgeValidator`, mensaje "Birth date must not be in the future or older than 50 years").

Exactamente lo que la issue #147 dejó implícito y antes era invisible: la regla de negocio (`PetAgeValidator`) y los constraints declarativos en una sola vista por endpoint.

## Cobertura de tests

- `tests/test_validation_surface.py` (12): discovery (incl. validador en archivo separado, skip de tests), parseo de `x-field-extra-annotation`, validatedFields, summary, gap por body sin constraints, anotación custom no resuelta (`resolved: false`), sin spec → superficie vacía.
- `tests/test_validation_cli.py` (7): json puro default, yaml, formato inválido → exit 2, `--gaps-only` shape, `--path-prefix`, directorio inexistente → exit 1.
- Suite completa: **2619 passed, 3 skipped** (sin regresiones).

## Valor para el agente

Antes: para saber qué debe cumplir un body, el agente leía la spec a mano y rezaba por que no hubiera un validador custom oculto. Ahora: una llamada determinista da reglas declarativas + validadores de negocio por endpoint, con detección de huecos (`--gaps-only`) — base directa para generar tests de validación o razonar sobre un 400.

## Backlog restante
- Validadores a nivel de **clase/método** (`@ScriptAssert`, validadores cross-field) — hoy solo se enlaza lo declarado por campo vía `x-field-extra-annotation`.
- Constraints en **query/path params** (`@RequestParam @Min`) — fuera del request body.

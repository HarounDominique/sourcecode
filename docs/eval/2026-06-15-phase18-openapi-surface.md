# Fase 18 — OpenAPI Surface: cierre y verificación

**Fecha:** 2026-06-15
**Versión:** sourcecode 1.38.0
**Roadmap:** [.planning/ROADMAP-openapi-surface-and-json-contract.md](../../.planning/ROADMAP-openapi-surface-and-json-contract.md)

## Qué se construyó

El desbloqueo del punto ciego openapi-generator / interface-defined, detectado en las pruebas #11 y #147 sobre `spring-petclinic-rest`. Antes: `endpoints` reportaba **1** de ~32 rutas y los constraints de validación (`@Pattern`/`@Size`) eran invisibles, porque mappings + DTOs viven en `target/generated-sources` (no escaneado).

**Vía elegida: parser de la spec OpenAPI del repo** (opción (b) del roadmap). La spec vive en `src/main/resources/openapi.yml`: siempre presente, determinista, sin build.

- **`src/sourcecode/openapi_surface.py`** (nuevo): discovery (filename hints en dirs conocidos + content-sniff acotado, salta `target/`/`build/`/`node_modules`), parser a modelo normalizado `OpenApiSurface{operations, schemas}` con resolución de `$ref` y `allOf` (límite de profundidad), constraints por campo (`pattern`, `minLength`, `maxLength`, `minimum`/`maximum`, `format`, `enum`, `required`, `ref`). Defensivo: spec malformada → superficie parcial, nunca excepción. Helper `tag_to_interface` (`owner-v2` → `OwnerV2Api`).
- **`extract_java_endpoints`** (modificado): recolecta controladores `implements *Api` sin rutas, los enlaza a operaciones de la spec por `tag → {Tag}Api`, y emite endpoints `source: "openapi-spec"` con `request_body.{schema, constraints}`. Controlador resuelto → sin warning; sin match → warning explícito conservado. Nuevos campos de resultado: `resolved_from_openapi_spec`, `spec_sourced_endpoints`, `openapi_spec`. Las heurísticas de `security_model`/`no_security_signal` se computan solo sobre endpoints nativos (los spec-sourced no las sesgan).

## Verificación E2E (spring-petclinic-rest, checkout, sin `mvn`)

| Métrica | Antes (1.36.5) | Después (1.38.0) |
|---------|----------------|-------------------|
| `endpoints` total | 1 | **37** |
| spec-sourced | 0 | 36 |
| controladores resueltos | 0 | 9 |
| warnings "NOT captured" | 9 | **0** |
| constraints de validación expuestos | no | sí (`request_body.constraints`) |

Ejemplo recuperado: `POST /owners` → `OwnerRestControllerV1#addOwner`, `request_body.schema=OwnerFields` con `firstName{pattern, minLength:1, maxLength:30, required}` — exactamente la superficie que la issue #147 necesitaba y que antes era invisible.

**Criterios de éxito (5):** 1 ✅ (~32→37, no 1) · 2 ✅ (DTO + constraints) · 3 ✅ (warning degradado a 0 cuando resuelto) · 4 ✅ (sin `target/`, checkout limpio) · 5 ✅ (JSON determinista; degrada a warning si no hay spec).

## Cobertura de tests

- `tests/test_openapi_surface.py` (12): discovery, operations, schemas/constraints, allOf+required union, JSON spec, defensivo.
- `tests/test_endpoints_openapi_link.py` (5): recuperación resuelta, marca resolved + sin warning, constraints en request_body, no-match conserva warning, sin-spec conserva warning legacy.
- Suite completa: **2602 passed, 3 skipped** (sin regresiones pese al refactor de `extract_java_endpoints`).

## Desviación del roadmap (justificada)

El roadmap listaba en 18-04 dos extras, **diferidos a backlog** tras reevaluación:

- **(a) `--scan-generated` (scan opt-in de `target/generated-sources`):** un repo openapi-generator **siempre** tiene la spec (es el input que genera esas fuentes), así que el parser (b) ya lo cubre de forma determinista. Un scanner de build output sería no determinista (requiere build, puede estar stale) y aportaría valor marginal nulo sobre (b). No se construye peso muerto.
- **(c) herencia de interfaz hand-written:** caso poco común (interfaces `*Api` escritas a mano en `src/` con anotaciones de mapping); `_build_route_surface` ya maneja parte de la herencia. Bajo ROI frente al caso generado, que es el dominante y ya está resuelto.

Ambos quedan como backlog con disparador claro: implementar solo si aparece un repo real donde (b) no baste.

## Backlog derivado
- Comando `sourcecode validation .`: agregar los constraints ahora expuestos (`request_body.constraints`) + validadores custom (`PetAgeValidator`) por endpoint.
- `--scan-generated` y herencia hand-written, solo bajo demanda real.

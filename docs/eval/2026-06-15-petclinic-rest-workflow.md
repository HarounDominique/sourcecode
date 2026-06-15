# Prueba de flujo agéntico real — `sourcecode` sobre spring-petclinic-rest

**Fecha:** 2026-06-15
**Herramienta:** sourcecode 1.36.4 (correcciones en 1.36.5)
**Repo objetivo:** [spring-petclinic/spring-petclinic-rest](https://github.com/spring-petclinic/spring-petclinic-rest) (Java 21, Spring Boot, Spring Security, Spring Data JPA, 109 clases Java, 3 backends de persistencia: jdbc / jpa / spring-data-jpa)
**Issue abordada:** [#11 — "No Paging and Sorting functionality"](https://github.com/spring-petclinic/spring-petclinic-rest/issues/11)

---

## 1. Resumen ejecutivo

Se clonó un repo Spring real y complejo y se resolvió una issue abierta de extremo a extremo usando `sourcecode` como capa de contexto estructural. La feature implementada —**endpoint v2 paginado para Vets** (`GET /api/v2/vets`)— es una extensión transversal que toca 9 ficheros + 1 controlador nuevo, atravesando OpenAPI → DTO → mapper → controller → service → **3 implementaciones de repositorio**.

**Resultado:** suite completa **238 tests, 0 fallos**, en los 4 conjuntos de tests de servicio (jdbc, h2-jdbc, jpa, spring-data-jpa) + nuevo test de paginación de Vets. Baseline verde antes y después.

**Veredicto sobre la herramienta:** acelera de forma real el mapeo arquitectónico y el análisis de blast-radius en segundos, pero tenía un **punto ciego grave** para el patrón concreto de este repo (controladores que implementan interfaces generadas por openapi-generator): la superficie de endpoints era prácticamente invisible. Ambos fallos sustantivos se corrigieron en 1.36.5 (ver §5b).

---

## 2. La issue y por qué es un buen caso

`HEAD` del repo era el merge `pets-v2-pagination` (#329): existía un patrón v2 paginado para **Pet** y **Owner**, pero no para el resto de agregados. La issue #11 ("No Paging and Sorting") quedaba parcialmente resuelta. El trabajo natural y verificable: **replicar el patrón v2 establecido sobre Vets**, siguiendo exactamente la plantilla in-repo.

Buen caso porque:
- Feature transversal (8 capas) → ideal para probar `impact` / `explain`.
- Hay un patrón de referencia exacto en el propio repo (bajo riesgo de diseño).
- Tiene tests reales y multi-backend → verificación objetiva.

---

## 3. El flujo, paso a paso

| Paso | Comando / acción | Tiempo | Valor |
|------|------------------|--------|-------|
| Orientación | `sourcecode . --compact` | **0.55s (cold)** | Stack, frameworks, capas, entrypoints, módulos `v1`/`v2`, conteo de controladores. Acertó "API sin UI, arquitectura por capas controller/service/repository". |
| Superficie HTTP | `sourcecode endpoints .` | <0.4s | ⚠️ **Solo detectó 1 de ~32 endpoints** (ver §5). |
| Blast radius | `sourcecode impact …VetRepository .` | instantáneo | Risk=CRITICAL, "2 direct callers; 20 persistence paths". Localizó las implementaciones a editar. |
| Auditoría | `sourcecode spring-audit .` | 0.3ms | 38 boundaries `@Transactional` (19 readOnly), 0 anomalías TX/SEC. |
| Comprensión de clase | `sourcecode explain …ClinicServiceImpl .` | <0.5s | Resumen legible del facade de servicio y sus métodos públicos. |
| Implementación | edición manual de 10 ficheros | — | Plantilla derivada de Pet/Owner v2. |
| Verificación | `./mvnw test` | ~1–2 min | **238 tests, 0 fallos.** |

---

## 4. Qué aportó valor / aceleró

1. **Orientación instantánea (`--compact`).** En medio segundo en frío, identificación correcta de stack (Spring Boot + Security + Data JPA), arquitectura por capas, ausencia de UI y existencia de módulos v1/v2. Para un repo desconocido de 109 clases esto ahorra varios minutos de exploración manual.

2. **`impact` sobre `VetRepository` enumeró los sitios de edición.** Marcó la interfaz como CRITICAL y listó implementadores directos. Útil para no olvidar que un cambio en la interfaz obliga a tocar **todos** los backends — el riesgo principal de compilación en este repo. (Inicialmente listó solo 2 de 3 → ver 🟠.)

3. **`spring-audit` dio una foto de TX/seguridad fiable y barata** (0.3ms): 38 `@Transactional`, 19 readOnly. Confirmó dónde encaja el nuevo método paginado (readOnly) sin abrir ficheros.

4. **Determinismo y velocidad.** Salida JSON estable, caché caliente sub-segundo. Encaja bien como capa de contexto para un agente: barato de invocar repetidamente.

5. **`explain` en Markdown legible** para entender el facade `ClinicService` sin leer 300 líneas.

---

## 5. Lo mejorable / lo que NO funcionó

### 🔴 Punto ciego crítico: endpoints sobre el patrón openapi-generator
`sourcecode endpoints .` reportó **1 endpoint** (`GET /` → `redirectToSwagger`) de ~32 reales. Causa raíz: en este repo los controladores implementan **interfaces generadas por openapi-generator** (`PetV2Api`, `VetsApi`, …) y las anotaciones de mapping (`@RequestMapping`, `@GetMapping`) viven en esas interfaces, bajo `target/generated-sources/openapi/…`, que el scanner excluye. El controlador solo lleva `@RestController @RequestMapping("/api")` a nivel de clase, sin mappings de método.

Comprobado:
- `target/generated-sources/openapi/.../VetsApi.java` contiene `@RequestMapping(... operationId="listVets")` en métodos `default`.
- `endpoints` y `spring-audit` reportan `endpoints_analyzed: 1`, `security_model: "unknown"`.

**Impacto:** este patrón (interface-only + openapi-generator) es muy común en Spring empresarial. Para esos repos, las features estrella (superficie de endpoints, SEC-001..003) quedan vacías y, peor, **daban una falsa sensación de "no hay endpoints/seguridad"** en vez de avisar.

→ **Corregido en 1.36.5** (warning explícito). Ver §5b.

### 🟠 `impact` omitió un implementador
`direct_callers` listó `JdbcVetRepositoryImpl` y `JpaVetRepositoryImpl`, pero **no** `SpringDataVetRepository` (que hace `extends VetRepository, Repository<Vet,Integer>`). Ground-truth: hay **3** implementadores. Doble causa: (1) la interfaz solo lleva `@Profile`, que el pre-scan no consideraba marcador → no se construían sus relaciones; (2) la cláusula `extends` con varios supertipos no se separaba por comas → edge corrupto. Riesgo: un agente que confíe en `direct_callers` para "¿qué tengo que editar?" se dejaría un backend → fallo de compilación.

→ **Corregido en 1.36.5.** Ver §5b.

### 🟡 Inconsistencias menores de CLI/formato
- ~~`explain` ignora `-f json`~~ **— diagnóstico corregido:** no es un bug. `explain` tiene su propia opción `--format/-f` con default `text` (legible) y `-f json` funciona. El fallo de parseo del informe original venía de no pasar `-f json`. Sin cambio de código.
- `sourcecode build .` no existe (es `sourcecode . --compact`); la primera intuición de "build/index" falla. Un alias o mensaje de ayuda ayudaría. (Pendiente, UX menor.)
- `endpoints . --no-cache`: `--no-cache` es flag del callback global, no del subcomando. Uso correcto: `sourcecode --no-cache endpoints .`. (No es bug del comando.)

---

## 5b. Correcciones aplicadas (v1.36.5)

Los dos fallos sustantivos se corrigieron en `sourcecode` 1.36.5 y se verificaron contra este mismo repo:

| Fallo | Causa raíz | Fix | Verificación |
|-------|-----------|-----|--------------|
| 🔴 Endpoints invisibles + silencio | Controladores `implements *Api` (mappings en interfaz generada) no aportan rutas y no se avisaba | `endpoints` emite ahora `warnings[]` + `interface_defined_controllers[]` cuando un `@RestController` implementa una interfaz `*Api` sin rutas propias | 10 controladores de petclinic-rest ahora avisados explícitamente (antes: silencio) |
| 🟠 `impact` omite implementador | (1) interfaz `@Profile`-only filtrada en pre-scan → sin relaciones; (2) `extends A, B<...>` no separado por comas → edge corrupto | (1) `@Profile` añadido a marcadores del pre-scan; (2) nuevo `_split_supertype_list` separa supertipos respetando genéricos, aplicado a `extends` e `implements` | `direct_callers` de `VetRepository` ahora incluye los **3** implementadores (jdbc, jpa, **spring-data**) |

Cobertura: `tests/test_supertype_edges.py` (7 tests). Suite completa: **2547 passed, 3 skipped**.

> Nota: el 🔴 ahora **avisa** de la superficie incompleta (recomendación mínima del informe); resolver los mappings desde la interfaz generada (scan opcional de `target/generated-sources` o herencia de `implements *Api`) queda como mejora mayor pendiente.

---

## 6. Cambios implementados en petclinic-rest (resumen)

```
 src/main/resources/openapi.yml                     | +101   (tag vet-v2, path /v2/vets, schema VetPage)
 .../service/ClinicService.java                     |  +1    (Page<Vet> findVets(Pageable))
 .../service/ClinicServiceImpl.java                 |  +6    (impl @Transactional readOnly)
 .../repository/VetRepository.java                  | +12    (findAll(Pageable))
 .../repository/jpa/JpaVetRepositoryImpl.java       | +18    (paginado vía JPQL + PageImpl)
 .../repository/jdbc/JdbcVetRepositoryImpl.java     | +39    (LIMIT/OFFSET + count + specialties)
 .../springdatajpa/SpringDataVetRepository.java     |  +9    (@Query Page<Vet> findAll)
 .../mapper/VetMapper.java                          | +13    (toVetPageDto)
 .../rest/controller/v2/VetRestControllerV2.java    | NUEVO  (implements VetV2Api)
 .../rest/controller/V2RestControllersTests.java    | +42    (testGetVetsPageSuccess)
```

**Verificación:** `./mvnw test` → `Tests run: 238, Failures: 0, Errors: 0, Skipped: 0` · `BUILD SUCCESS`.

---

## 7. Conclusión

Para **onboarding y razonamiento de impacto** en un repo Spring grande, `sourcecode` aportó valor real y rápido: el `--compact` y el `impact` ahorraron exploración manual y orientaron las ediciones correctas en una feature de 8 capas. El determinismo y la latencia sub-segundo lo hacen apto como capa de contexto de agente.

El punto débil estaba precisamente en su feature más vendible (superficie de endpoints/seguridad) cuando el repo usa openapi-generator interface-only, un patrón muy extendido; y en un grafo de impacto que omitía implementadores vía herencia de interfaz. Ambos eran riesgos de *falsa confianza* para un agente y se han corregido en 1.36.5: el primero pasando de silencio a aviso explícito, el segundo cerrando el grafo sobre `extends` multi-supertipo e interfaces `@Profile`-only.

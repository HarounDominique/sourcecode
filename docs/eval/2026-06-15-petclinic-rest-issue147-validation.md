# Prueba de flujo agéntico real (2) — `sourcecode` sobre spring-petclinic-rest, issue #147

**Fecha:** 2026-06-15
**Herramienta:** sourcecode 1.36.5
**Repo objetivo:** [spring-petclinic/spring-petclinic-rest](https://github.com/spring-petclinic/spring-petclinic-rest) (Java 21, Spring Boot, Spring Security, Spring Data JPA, 109 clases, 3 backends de persistencia, openapi-generator interface-only)
**Issue abordada:** [#147 — "Add pattern definition rules"](https://github.com/spring-petclinic/spring-petclinic-rest/issues/147) (labels: enhancement, help wanted)

> Segunda prueba sobre el mismo repo. La primera (#11, paginación de Vets) está en `2026-06-15-petclinic-rest-workflow.md`. Esta issue es de naturaleza **distinta a propósito**: cambio dirigido por configuración (OpenAPI spec) en vez de feature Java multicapa — para ver dónde `sourcecode` ayuda y dónde no aplica.

---

## 1. Resumen ejecutivo

Issue #147: la spec OpenAPI define campos con `minLength` pero sin `pattern`, así que la API acepta nombres que empiezan por dígito/símbolo o son todo espacios (p.ej. `"123"`, `"   "`). El PR de referencia en el cliente Angular (#142) ya impuso que un campo *name* requerido empiece por letra. La tarea: replicar esa regla en el backend.

**Cambio:** `pattern: "^[\\p{L}]+([ '-][\\p{L}]+){0,2}$"` añadido a los 3 campos *name* de agregado que carecían de él (`Specialty.name`, `PetType.name`, `Pet.name`; este último además sin `minLength`). Owner/Vet `firstName`/`lastName` **ya** lo tenían — se reutilizó exactamente su patrón para consistencia.

**Resultado:** suite completa **240 tests, 0 fallos** (baseline 237 + 3 nuevos de validación). `@Pattern(regexp=...)` confirmado en los DTOs generados (`SpecialtyDto`, `PetTypeDto`, `PetFieldsDto`).

**Veredicto sobre la herramienta:** valor **parcial y honesto**. El cambio sustantivo vive en `openapi.yml` (config), territorio que `sourcecode` no analiza para reglas de validación → no aceleró el núcleo. Donde sí aportó: orientación inicial (`--compact`) y, sobre todo, el **warning de controladores interface-defined** (añadido en 1.36.5 a raíz de la prueba #11) que apuntó exactamente a las 9 clases controladoras donde aterriza la validación. Confirma que el fix de 1.36.5 era acertado: el mismo aviso resulta útil en una issue independiente.

---

## 2. La issue y por qué es un buen caso (distinto)

`#147` pide listar e implementar reglas `pattern` ausentes. Buen segundo caso porque:
- **Tipo opuesto al #11:** no es código Java multicapa sino spec declarativa → DTOs generados. Pone a prueba los límites de un analizador estructural de Java.
- **Verificable:** la validación se compila en `@Pattern` sobre los DTOs y Spring devuelve 400 con `@Valid`. Test objetivo: nombre inválido → `isBadRequest`.
- **Bajo riesgo de diseño:** patrón ya establecido en el propio repo (Owner/Vet) y en el cliente Angular.

Restricción detectada antes de tocar nada: varios tests usan nombres con espacio y mayúscula intercalada — `"surgery I"`, `"dog I"`, `"Rosy I"`, `"McFarland"`. El patrón debía aceptarlos. `^[\p{L}]+([ '-][\p{L}]+){0,2}$` los acepta (hasta 3 palabras, separadores espacio/`'`/`-`) y rechaza `"123..."` y `""`.

---

## 3. El flujo, paso a paso

| Paso | Comando / acción | Tiempo | Valor |
|------|------------------|--------|-------|
| Orientación | `sourcecode . --compact` | **0.31s (warm)** | Stack, capas, 10 controladores, módulos v1/v2. Igual que en #11: correcto y rápido. |
| Superficie HTTP | `sourcecode endpoints .` | <0.4s | `endpoints_analyzed: null` + **9 `warnings[]`**: lista los 9 `@RestController` que implementan `*Api` (Owner/Pet/PetType/Specialty/User/Vet/Visit V1 + Owner/Pet V2). Apuntó las clases donde añadir tests. |
| Localización de campos | `grep minLength openapi.yml` + lectura schemas | — | 13 campos `minLength:1`; cruzado con "¿cuáles ya tienen `pattern`?" → 3 *name* sin patrón. **No** vía sourcecode (spec YAML). |
| Convención de validación | lectura `rest/validation/PetAgeValidation.java` | — | Confirmó estilo de validación del repo (anotación + validador). Hallado por estructura de carpetas, no por sourcecode. |
| Implementación | 3 ediciones en `openapi.yml` | — | Reutilizado patrón in-repo de Owner/Vet. |
| Tests | +3 `...InvalidNamePattern` (Specialty/PetType/Pet) | — | Mirror de los `...Error` existentes. |
| Verificación | `./mvnw test` | ~30s | **240 tests, 0 fallos.** `@Pattern` confirmado en DTOs generados. |

---

## 4. Qué aportó valor / aceleró

1. **`endpoints` (con el warning de 1.36.5) fue lo más útil aquí.** En un repo donde toda la superficie HTTP y la validación cuelgan de interfaces generadas, el aviso enumeró las 9 clases controladoras exactas — el conjunto donde había que añadir cobertura de test. Sin ese aviso, `endpoints` habría dado silencio (el bug original de #11). **Validación cruzada:** un fix nacido de una prueba ayuda en otra issue no relacionada.

2. **`--compact` para re-orientación instantánea** (0.31s warm). Confirma stack y capas sin reabrir el repo mentalmente.

3. **Determinismo/latencia** intactos: apto como capa de contexto barata de invocar.

---

## 5. Lo mejorable / lo que NO aplicó

### 🔵 El cambio núcleo quedó fuera del alcance de la herramienta (esperado)
La issue se resuelve en `openapi.yml`. `sourcecode` analiza estructura **Java**, no reglas de validación de specs OpenAPI. No hubo (ni se esperaba) ayuda para: encontrar qué campos tienen `minLength` sin `pattern`, ni para verificar que el patrón se propaga a los DTOs. Todo eso fue `grep` + lectura + `./mvnw`. **No es un defecto** — es el dominio de la herramienta. Pero marca el techo de su utilidad en issues *config-driven*.

### 🟠 `impact` inútil sobre DTOs generados (mismo blind spot que #11)
Intuición natural: `impact SpecialtyDto` para ver qué endpoints validan ese cuerpo. Pero `SpecialtyDto` vive en `target/generated-sources` (excluido) → no está en el grafo. El analizador no puede trazar `openapi.yml → DTO → controller → test`. Es la misma raíz que el 🔴 de #11: el patrón openapi-generator interface-only deja el núcleo del repo invisible. El warning mitiga (avisa), pero no resuelve el trazado.

> Mejora mayor pendiente (ya anotada en #11): escaneo opcional de `target/generated-sources` o herencia de mappings/anotaciones desde `implements *Api`, para que `endpoints`/`impact` cubran DTOs y rutas generadas. Es lo que convertiría a `sourcecode` de "avisa que no ve" a "ve".

### 🟡 Sin comando para superficie de validación
No existe forma de preguntar "¿qué constraints (`@Pattern`, `@Size`, `@NotNull`, validadores custom) aplican a cada endpoint/DTO?". Para issues de validación sería el comando estrella. Hoy hay que leer specs y código a mano. Candidato a feature: `sourcecode validation .` (agregaría constraints declarados + validadores custom como `PetAgeValidator`).

---

## 6. Cambios implementados en petclinic-rest (resumen)

```
 src/main/resources/openapi.yml                                    | +4   (pattern en Specialty.name, PetType.name, Pet.name; +minLength Pet.name)
 .../rest/controller/SpecialtyRestControllerV1Tests.java           | +13  (testCreateSpecialtyInvalidNamePattern)
 .../rest/controller/PetTypeRestControllerV1Tests.java             | +13  (testCreatePetTypeInvalidNamePattern)
 .../rest/controller/PetRestControllerV1Tests.java                 | +15  (testUpdatePetInvalidNamePattern)
```

**Verificación:** `./mvnw test` → `Tests run: 240, Failures: 0, Errors: 0, Skipped: 0` · `BUILD SUCCESS`. `@Pattern(regexp = "^[\\p{L}]+([ '-][\\p{L}]+){0,2}$")` presente en `SpecialtyDto`, `PetTypeDto`, `PetFieldsDto` generados.

> Nota de alcance: la issue pide "listar" todas las reglas. Se implementó el subconjunto seguro y defendible (campos *name* de agregado, análogos directos a `firstName`/`lastName` ya tratados). `city`, `address`, `description`, `username`/`password` y `Role.name` quedan deliberadamente fuera: no son "name fields" en el sentido de la issue/Angular y conllevan más riesgo (p.ej. ciudades como "St. Louis", direcciones con dígitos). Listarlos es decisión de mantenedores.

---

## 7. Conclusión

Segunda prueba, dominio opuesto a la primera. `sourcecode` **no aceleró el núcleo** de una issue dirigida por config — y es correcto que así sea, es un analizador estructural de Java. Su valor aquí fue indirecto pero real: re-orientación instantánea y, sobre todo, el **warning de controladores interface-defined** (introducido tras la prueba #11) que señaló con precisión las clases donde añadir tests. Que un fix de una prueba demuestre utilidad en otra issue independiente es la mejor señal de que iba en la dirección correcta.

Las dos pruebas convergen en la misma mejora mayor pendiente: **mientras `sourcecode` no vea `target/generated-sources` (o no herede mappings/anotaciones de `implements *Api`), el núcleo de los repos openapi-generator le queda invisible** — endpoints, DTOs, y constraints de validación incluidos. El warning evita la *falsa confianza*; cerrar ese hueco lo convertiría en herramienta de primera línea para este patrón, muy común en Spring empresarial. Candidato concreto derivado de esta issue: un comando `validation` que agregue constraints por endpoint.

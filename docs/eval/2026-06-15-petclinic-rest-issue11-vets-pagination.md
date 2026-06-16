# Field test — spring-petclinic-rest #11 (paginación v2 de vets)

**Fecha:** 2026-06-15
**Herramienta:** sourcecode 1.39.0
**Repo objetivo:** `spring-petclinic/spring-petclinic-rest` (clon limpio `spring-petclinic-rest-fieldtest`, cambios locales, sin pushear)
**Issue:** [#11 — No Paging and Sorting functionality](https://github.com/spring-petclinic/spring-petclinic-rest/issues/11) (open, `enhancement`)
**Resultado:** ✅ fix implementado y verde — `./mvnw test` → **246 passed, 0 failures**.

## Contexto y elección

Tercer flujo agéntico sobre este repo (previos: #147 patrón-validación, #11 owners-pagination en stash). De las 4 issues abiertas, dos ya tocadas, #125 es un refactor multi-repo (cambia OpenAPI + cliente Angular) y #19 es SQL casi-stale. #11 sigue abierta y es la de mayor *leverage* para el tool: app REST Spring, patrón openapi-generator, y —según el maintainer en los comentarios— "la dificultad está en la capa de repositorio: tres implementaciones y tres bases de datos".

Estado real descubierto: la paginación **v2 ya está mergeada para owners (#328) y pets (#329)** pero no para el resto de agregados. Fix elegido: **añadir `/v2/vets` paginado** replicando el patrón v2 establecido, con lo que se cierra otro agregado de #11 de forma consistente. Vets es el caso con la complejidad que el maintainer marcó (3 repo-impls + many-to-many con specialties).

## Qué construí

`GET /api/v2/vets?page&size` → `VetRestControllerV2#listVetsPage` → `ClinicService.findVets(Pageable)` → `VetRepository.findAll(Pageable)` en las **3 implementaciones** (JPA, JDBC, Spring Data JPA), con `VetMapper.toVetPageDto` y schema `VetPage` en la spec.

Footprint: **11 ficheros, +257 líneas** (10 modificados + `VetRestControllerV2.java` nuevo). Tests: `shouldFindVetsPage` + `shouldFindVetsSecondPage` (corren en los 4 perfiles `ClinicService{H2Jdbc,HsqlJdbc,Jpa,SpringDataJpa}Tests` → cubren los 3 impls × DBs) y `testGetVetsPageSuccess` (MockMvc sobre el controller V2).

## Dónde aceleró sourcecode (valor real)

1. **Localizar el hueco en segundos.** `sourcecode endpoints .` enumeró las 37 rutas y dejó ver de un vistazo que solo `owners` y `pets` tenían variante `/v2/...` paginada; vets/specialties/pettypes/visits no. Sin la herramienta: grep manual cruzando 12 controllers + spec. **Ahorro: el reconocimiento inicial.**

2. **Validar el wiring ANTES de compilar (el momento "wow").** Tras escribir la spec OpenAPI y el controller, `sourcecode --no-cache endpoints .` mostró al instante —**sin `mvn`, sin generar `target/`**— la ruta nueva ya resuelta:
   ```
   GET /v2/vets  VetRestControllerV2#listVetsPage  src=openapi-spec
   ```
   Es decir: confirmó que el tag `vet-v2` → interfaz `VetV2Api` → mi controller enganchaban correctamente, antes de pagar el build de ~2 min de openapi-generator + mapstruct. La capacidad Phase 18 (que esta misma sesión construyó) se pagó sola sobre código real.

3. **Mapa de impacto a nivel de servicio.** `sourcecode impact-chain ClinicService .` devolvió 9 `direct_callers` incluyendo los 6 controllers V1 y los 3 V2 (mi `VetRestControllerV2` entre ellos) — confirmó que no rompía el contrato del servicio compartido y dónde vivía cada consumidor.

4. **Sin falsos positivos de seguridad.** `sourcecode spring-audit .` → 0 findings; no marcó el nuevo controller (lleva `@PreAuthorize(hasRole(@roles.VET_ADMIN))`, igual que V1). Confirma que el fix no introduce regresión de superficie de seguridad.

## Qué es mejorable / no funcionó bien

1. **`impact-chain` sobre una interfaz de repositorio rinde poco.** `impact-chain VetRepository` devolvió como único `direct_callers` el sub-interface `SpringDataVetRepository` — **no** listó los implementadores `JpaVetRepositoryImpl`/`JdbcVetRepositoryImpl`, ni el caller real `ClinicServiceImpl`. Justo el grafo que el maintainer pedía ("las 3 implementaciones") es el que la herramienta NO dio para el símbolo interfaz. La relación *clase implementa interfaz* no se modela como impacto.
   → **Mejora propuesta:** `impact` sobre una interfaz debería enumerar sus implementadores (y su módulo/profile), y resolver callers método-a-método a través de la interfaz. Contrasta con `impact-chain ClinicService` (clase concreta), que sí funcionó bien.

2. **`endpoints_affected` siempre 0** en impact-chain para símbolos de repo/servicio. El call-graph no puentea repo→service→controller→ruta HTTP en este repo, porque los controllers `implements *Api` generados rompen la cadena estática (la ruta vive en la spec, no en una anotación escaneada). Coherente con el patrón openapi-generator, pero significa que el "blast radius hasta el endpoint" no está disponible justo en los repos que usan ese patrón.
   → **Mejora propuesta:** reusar el linking spec↔controller de Phase 18 para poblar `endpoints_affected` cuando el controller terminal implementa una `*Api` resuelta por la spec.

3. **`validation` no aplica a este fix** (la ruta nueva es GET, sin body) — esperado, pero deja claro que la superficie de paginación (query params `page`/`size` con `minimum`/`maximum` en la spec) no la expone ningún comando hoy; `validation` solo cubre request-body. Candidato de backlog: constraints de query/path params.

## Veredicto

Para la **fase de orientación** (entender la superficie, encontrar el hueco, validar el wiring sin build) sourcecode aceleró de forma tangible y el descubrimiento sin-build de la ruta nueva fue el punto más fuerte. Para el **análisis de impacto profundo** sobre la capa de repositorio —el núcleo de dificultad de esta issue— la herramienta se quedó corta en el símbolo interfaz, aunque acertó al nivel de clase de servicio. Dos mejoras concretas y accionables salen del ejercicio (impacto sobre interfaces; `endpoints_affected` vía spec-linking).

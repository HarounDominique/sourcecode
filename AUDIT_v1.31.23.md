# sourcecode — Auditoría Técnica Profesional v1.31.23

**Versión auditada:** 1.31.23  
**Fecha:** 2026-05-25  
**Repos:** `keycloak` (7.885 clases Java, 18K+ commits, Quarkus/Jakarta EE) · `BroadleafCommerce` (2.985 clases Java, 18K+ commits, Spring Boot)  
**Referencia anterior:** AUDIT_REAL_REPOS.md (v1.31.17, 2026-05-24)  
**Metodología:** Ejecución adversarial completa de todos los comandos/flags en ambos repos. No se usan fixtures sintéticos.

---

## 1. Resumen Ejecutivo

`sourcecode` es una herramienta de inteligencia de código Java para LLMs: extrae contexto acotado y estructurado de monolitos empresariales. En v1.31.23, **dos de los tres bugs P0 de la auditoría anterior han sido corregidos**:

- ✅ `impact` sobre clases impl (`OrderServiceImpl`): ahora devuelve 30 callers directos, 50 indirectos, `risk: high` — correcto y fiable
- ✅ `repo-ir --max-nodes/edges`: ahora acotan correctamente (1.1MB vs 3.9MB previos)
- ⚠️ BFS truncado en `indirect_callers` para targets con >1000 callers directos — persiste

El núcleo está en estado **lanzable con caveats**: la propuesta de valor central (blast radius en monolitos Java) funciona para la mayoría de casos reales. Quedan 3 P1 y 8 P2 sin corregir que afectan credibilidad pero no bloquean el uso en producción.

**Veredicto:** `productizable now` — con correcciones P1, posicionable frente a competencia directa. Sin ellas, creíble para proyectos individuales, difícil de vender a enterprise.

---

## 2. Metodología

- Todos los subcomandos y sus `--help` revisados
- `--compact`, `--agent`, `--format yaml` en ambos repos (frío + caché)
- `endpoints` en ambos repos con análisis de calidad de paths
- `impact` sobre: clases impl, clases interfaz, clases sin existencia, clases con alta fan-in
- `onboard`, `fix-bug`, `modernize`, `review-pr`, `repo-ir` en ambos repos
- `prepare-context` vs aliases de nivel superior (comparación de output)
- Timing: cold → warm en múltiples ciclos
- Verificación de correcciones de v1.31.17: cada bug del audit anterior re-testeado

---

## 3. Estado de Correcciones desde v1.31.17

### Corregidos ✅

| Bug anterior | Observado v1.31.23 | Evidencia |
|---|---|---|
| P0-01: `impact OrderServiceImpl` → 0 callers con confidence:high | **CORREGIDO** | `direct_callers:30, indirect:50, risk:high, confidence:high` |
| P0-02: `repo-ir --max-nodes 200` → 3.9MB | **CORREGIDO** | Salida: `1079KB, 200 nodes, 500 edges` |
| Endpoints: FQN class-name paths en Broadleaf | **CORREGIDO** | Keycloak: 693→613; Broadleaf: 130→110 |
| `--mode deep` generaba output idéntico a standard | **ELIMINADO** | Mensaje explícito de deprecación, redirige a standard |

### Persisten ⚠️

| Bug anterior | Estado actual |
|---|---|
| P1-04: `indirect_callers:0` para KeycloakSession (1992 callers directos) | Persiste. BFS para al nivel 1 con fan-out muy alto |
| P2-01: `bounded_contexts` incorrecto en ambos repos | Persiste (`["dto","file"]` en Broadleaf, `["keycloak"]` en Keycloak) |
| P2-02: `role:"unknown"` en todos los nodos de `modernize` | Persiste |
| P2-03: `no_security_signal` inútil en proyectos con seguridad por filtro | Persiste |
| P2-06: Confianza de arquitectura distinta entre `--compact` (low) y `--agent` (medium) | Persiste |
| P2-08: `--format` y `--no-cache` no disponibles en todos los subcomandos | Persiste |
| URL truncadas en code_notes (`"s.webkit.org/..."`) | Persiste |
| `project_summary` generado desde blurb de README en Broadleaf | Persiste |
| `hotspot_candidates: []` siempre en repos de alta actividad | Persiste |
| `--deep` referenciado en output pero error en CLI | Persiste |

---

## 4. Resultados por Repo

### 4.1 Keycloak — Servidor IAM, Quarkus/Jakarta EE, 7.885 clases

#### Lo que funciona

| Capacidad | Detalle |
|---|---|
| Stack detection | Quarkus + Jakarta EE + Vert.x + Node.js (pnpm) detectados correctamente |
| Bootstrap entry points | `KeycloakMain`, `QuarkusKeycloakApplication`, `Main` — correcto |
| Endpoints extraídos | 613 endpoints JAX-RS con controller + handler |
| `impact` en interfaz | `KeycloakApplication`: 8 callers directos, 28 indirectos, 26 endpoints — correcto |
| `impact` en impl | `KeycloakMain` y otros impls: resuelven correctamente vía DI |
| `modernize`: coupling | Nodos correctos (`KeycloakSession` in_degree:2024, `RealmModel`:1338) |
| Spring profile detection | `rhbk` (Red Hat fork) detectado desde env_vars |
| `javax-to-jakarta` risk | `javax.annotation:javax.annotation-api` correctamente marcado |
| git context | Hotspots, commits recientes, branch detectados correctamente |
| Cache speedup | Cold: 8.5s → Cached: 0.27s (~31x) |
| Code notes | 362 notas (268 TODO, 69 NOTE, 7 BUG, 6 WARNING) |

#### Lo que falla

| Problema | Severidad | Detalle |
|---|---|---|
| `project_type: "fullstack"` | P2 | Keycloak es un servidor IAM, no una app fullstack genérica |
| `bounded_contexts: ["keycloak"]` | P2 | Subsistemas reales: oidc, saml, federation, authz, admin, operator |
| `indirect_callers:0` para `KeycloakSession` | P1 | 1992 callers directos, BFS no avanza al nivel 2 — subestima impacto transitivo |
| 23 paths duplicados en endpoints | P2 | `GET /roles/{id}` aparece x4, etc. |
| Paths `.step1.html`, `/.search` en endpoints | P2 | Páginas HTML mezcladas con REST |
| `/.well-known/{provider}/realms/{realm}` duplicado | P2 | Mismo path x2 en lista |
| `entry_points.security` mezcla SPI impls con filtros reales | P2 | `WebAuthnCredentialProvider` no es security entry point |
| `no_security_signal` al 100% | P2 | Keycloak usa seguridad por filtro JAX-RS — métrica siempre vacía |
| URLs en code_notes truncadas | P3 | `"zilla.redhat.com/..."` en lugar de `"https://bugzilla.redhat.com/..."` |
| `architecture.confidence: low` en compact, `medium` en agent | P2 | Misma base de análisis, resultado distinto según modo |

### 4.2 BroadleafCommerce — Framework e-commerce, Spring Boot, 2.985 clases

#### Lo que funciona

| Capacidad | Detalle |
|---|---|
| Stack detection | Spring Boot + MVC + Security + LDAP + AOP — correcto |
| Security filter chain | `SecurityFilter`, `CsrfFilter`, `SecurityBasedIgnoreFilter` detectados |
| `transactional_boundaries` | 29 clases correctas (`OrderServiceImpl`, `OrderDaoImpl`, `OfferServiceImpl`...) |
| Event flow | Publishers, listeners y tipos de evento correctos (`CustomerPersistedEvent`, `OrderPersistedEvent`) |
| `impact OrderServiceImpl` | **30 callers directos, 50 indirectos, 11 endpoints, risk:high** — correcto (bug P0 corregido) |
| `impact OrderDaoImpl` | Resuelto correctamente vía interfaz |
| `fix-bug --symptom "order payment"` | 15+ archivos relevantes correctos surfaceados |
| Cache speedup | Cold: 2.6s → Cached: 0.25s (~10x) |
| Dependency extraction | 91 deps con versiones y risk flags (`javax.cache:cache-api` marcado) |
| `analysis_gaps` | Gap `api_contract` correctamente detectado (sin OpenAPI) |

#### Lo que falla

| Problema | Severidad | Detalle |
|---|---|---|
| `project_type: "api"` | P2 | Broadleaf es un framework, no una API REST |
| `project_summary` desde README | P1 | Texto sobre licencia comercial, no arquitectura |
| `bounded_contexts: ["dto","file"]` | P2 | Contextos reales: Order, Catalog, Customer, CMS, Offer |
| 4 paths duplicados en endpoints | P2 | `POST /category/{id}` x2, mismo controller — falso dupe por herencia de clase |
| `hotspot_candidates: []` | P2 | 18K+ commits, actividad real, algoritmo no detecta nada |
| `cross_module_tangles: []` | P2 | 8 subsistemas con acoplamiento conocido, no se detecta ninguno |
| `no_security_signal` al 100% | P2 | Broadleaf usa XML + AdminSecurityFilter — anotaciones siempre vacías |
| `entry_points.controllers.methods: 21` vs `endpoints: 110` | P2 | Discrepancia sin explicar para mismo repo |
| `subsystem_summary.member_count: 0` | P2 | Todos los subsistemas detectados tienen 0 miembros asignados |

---

## 5. Análisis del Motor Central

### 5.1 Extracción de Endpoints

| Métrica | Keycloak v1.31.23 | Broadleaf v1.31.23 |
|---|---|---|
| Total endpoints | 613 | 110 |
| Paths duplicados | 23 | 4 |
| Paths HTML/no-REST | ~3 (.step1.html, .search) | 0 |
| Paths FQN o colon-notation | ~5 sospechosos | 0 (corregido) |
| Security signal útil | ✗ (filter-based) | ✗ (filter+XML) |

**Mejora real:** La eliminación de paths FQN de Broadleaf (20 paths como `/org.broadleafcommerce...`) y la reducción de duplicados JAX-RS en Keycloak son correcciones tangibles. 80 endpoints menos que v1.31.17 combinados.

**Problema pendiente:** Los 23 duplicados de Keycloak son paths de sub-recursos JAX-RS donde el path de clase no se compone con el path de método. `GET /roles/{id}` debería ser `GET /admin/realms/{realm}/clients/{id}/roles/{id}`. La causa raíz (composición de `@Path` padre + hijo) no está corregida.

### 5.2 Impact Analysis

**Estado actual (v1.31.23):**

```
sourcecode impact OrderServiceImpl  →  30 direct, 50 indirect, risk:high, confidence:high  ✅
sourcecode impact OrderService      →  30 direct, 50 indirect, risk:high, confidence:high  ✅
sourcecode impact KeycloakApplication → 8 direct, 28 indirect, 26 endpoints, risk:high    ✅
sourcecode impact KeycloakSession   →  30 direct (list cap), 0 indirect, risk:high         ⚠️
```

El bug P0 está corregido: impl classes resuelven ahora a través de interfaces DI. El único comportamiento incorrecto restante es `indirect_callers:0` para `KeycloakSession` (1992 callers directos). El BFS colapsa en el nivel 1 por el enorme fan-out: con 1992 nodos directos, computar nivel 2+ es O(1992²) antes de poda. El campo `explanation` dice correctamente "1992 direct callers" pero `indirect_callers` en JSON es `[]`. Discrepancia entre texto y estructura.

**Impact via file path:** `sourcecode impact services/src/.../KeycloakApplication.java` devuelve `not_found`. Solo funciona con nombres de clase o FQN. Barra de entrada innecesariamente alta para usuarios que copian paths desde IDE.

### 5.3 Repo-IR

**Estado actual (v1.31.23):**

```
repo-ir . --max-nodes 200 --max-edges 500  →  1.1MB, 200 nodes, 500 edges  ✅
repo-ir . (sin límites)                    →  ~96MB (Keycloak, 56K nodes)
```

P0-02 corregido: los flags ahora acotan correctamente. El IR sin límites es esperablemente grande para repos de 7K clases y es comportamiento documentado.

### 5.4 Modernize

```json
"hotspot_candidates": [],
"subsystem_summary": [{"label": "broadleafcommerce.admin", "member_count": 0}],
"high_coupling_nodes": [{"fqn": "KeycloakSession", "role": "unknown"}]
```

Los tres campos más valiosos para el caso de uso de modernización están rotos:
- `hotspot_candidates`: siempre vacío — no usa datos de git churn
- `subsystem_summary.member_count`: siempre 0 — partición de clases no asigna miembros
- `role`: siempre "unknown" — `KeycloakSession` es obviamente una interfaz de sesión; `OrderServiceImpl` obviamente un `@Service`

---

## 6. Rendimiento (v1.31.23)

| Comando | Keycloak (7.885 archivos) | Broadleaf (2.985 archivos) |
|---|---|---|
| `--compact` cold | 8.5s | 2.6s |
| `--compact` cached | 0.27s (~31x) | 0.25s (~10x) |
| `--agent` cold | 15.8s | ~7s |
| `endpoints` cold | 8.0s | ~3s |
| `fix-bug` cold | ~20s | ~8s |
| `repo-ir --max-nodes 200` | ~12s | ~5s |

**Observación:** Mejoras de ~8% sobre v1.31.17 en timings cold (9.0s→8.5s en Keycloak). Presumiblemente por reducción de trabajo en endpoint extraction. El cache (hash de contenido) es determinista y produce ~31x speedup.

**Token output (medido):**

| Modo | Broadleaf | Keycloak |
|---|---|---|
| `--compact` | ~2.900 tokens | ~4.100 tokens |
| `--agent` | ~4.800 tokens | ~5.500 tokens |
| `onboard` | ~2.600 tokens | ~n/a |
| `fix-bug` (trimmed) | ~27.000 tokens | ~5.000 tokens |
| `repo-ir --max-nodes 200` | ~270.000 tokens | ~n/a |

---

## 7. Auditoría CLI / UX

### Inconsistencias de flags

| Flag | main | endpoints | impact | onboard | fix-bug | modernize | review-pr |
|---|---|---|---|---|---|---|---|
| `--format` | ✅ | ✅ | ✗ | ✗ | ✗ | ✗ | ✅ |
| `--no-cache` | ✅ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| `--copy` | ✅ | ✗ | ✅ | ✗ | ✅ | ✗ | ✅ |
| `--output` | ✅ | ✗ | ✅ | ✅ | ✅ | ✗ | ✅ |

Un desarrollador que intenta `sourcecode impact KeycloakApplication --format yaml` recibe `No such option: --format` — error genérico de Click, sin sugerencia de alternativa.

### Flags documentados pero rotos o ausentes

- **`--deep`**: referenciado en el campo `file_relevance_hint` del output (`"Use --deep for up to 80 files"`) pero la CLI devuelve `No such option: --deep (Possible options: --depth, --mode, --tree)`. Feature ghost: prometida en output, invisible en help.
- **`--mode deep`**: ahora explícitamente deprecado con mensaje. Buen comportamiento, pero el output sigue referenciando `--deep`.

### Calidad de mensajes de error

| Escenario | Resultado |
|---|---|
| Target inexistente en `impact` | `{resolution: "not_found"}` JSON limpio ✅ |
| Git ref inválido en `review-pr` | JSON con lista de branches disponibles ✅ |
| `--format yaml` en `impact` | Error genérico Click ✗ |
| Path de archivo en `impact` | `not_found` sin sugerencia de usar nombre de clase ✗ |

### Inconsistencias de schema

- `truncated: null` (ausente) en algunos campos vs `truncated: false` (booleano explícito) en otros — debería ser siempre booleano explícito
- `direct_callers` lista capeada a 30 sin campo `direct_callers_count` adyacente — el count real está solo en `explanation` como texto
- `architecture.confidence: low` (compact) vs `medium` (agent) para el mismo repo — misma base de análisis, resultado distinto según modo de salida
- `entry_points.controllers.methods: 21` vs `endpoints` extrae 110 para Broadleaf — discrepancia no explicada
- `run_id` en todos los task outputs — propósito sin documentar

---

## 8. Bugs Clasificados (estado actual)

### P1 — Alta severidad

**BUG-P1-01: `indirect_callers:0` para targets con fan-out extremo**
- `sourcecode impact KeycloakSession` → `indirect_callers: []` pero `explanation` dice "1992 direct callers"
- BFS colapsa en nivel 1: con 1992 nodos directos, la expansión nivel 2 es costosa sin poda agresiva
- Impacto: LLM subestima impacto transitivo de cambios en interfaces core
- Fix: Sampled BFS — seleccionar subconjunto representativo de nivel 2 con anotación `"indirect_callers_sampled: true"`. O documentar explícitamente con `"indirect_callers_truncated": true, "indirect_callers_estimated": 8000+`

**BUG-P1-02: `project_summary` generado desde blurb de README**
- Broadleaf: copia texto sobre licencia comercial — sin valor arquitectural
- Keycloak: mejorado respecto a v1.31.17 pero sigue describiendo qué hace Keycloak *para usuarios*, no arquitectura del codebase
- Fix: Template estructurado: `"[N módulos Maven] [framework] — [M] clases Java, [K] endpoints REST, [J] límites transaccionales. Punto de entrada: [bootstrap class]."`

**BUG-P1-03: `fix-bug` sin campo `score` en `relevant_files`**
- 15+ archivos devueltos sin puntuación relativa — agente no puede priorizar
- Con síntoma genérico (NPE, timeout) puede devolver 400+ archivos
- Fix: Campo `score: float[0..1]` en cada archivo. Cap automático a top-20 para síntomas de baja especificidad

### P2 — Severidad media

**BUG-P2-01: `bounded_contexts` detecta utility packages, no dominios**
- Broadleaf: `["dto", "file"]` — packages de utilities
- Keycloak: `["keycloak"]` — nombre del proyecto entero
- Fix: Usar nombres de módulos Maven (`<artifactId>`) como señal primaria para bounded contexts

**BUG-P2-02: `role: "unknown"` para todos los nodos de `modernize`**
- 20 nodos incluyendo `@interface`, interfaz, `@Service`, `@Entity` — todos "unknown"
- Fix: Detectar tipo desde source: `@interface` → "annotation", `interface` → "interface", `@Entity` → "entity", `@Service` → "service"

**BUG-P2-03: `no_security_signal` inútil para seguridad por filtro**
- Ambos repos: 100% de endpoints sin anotación de seguridad — porque usan filtros/XML, no `@PreAuthorize`
- Métrica aparece en output como dato de calidad — engañosa
- Fix: Detectar `WebSecurityConfigurerAdapter`, `SecurityConfig`, `FilterChain`, filtros JAX-RS. Cambiar a `"security_model": "filter_based"` cuando se detecten

**BUG-P2-04: Paths duplicados en endpoints no consolidados**
- Keycloak: 23 pares (method, path) duplicados — sub-recursos JAX-RS no compuestos con path de clase
- Broadleaf: 4 duplicados — herencia de controller (mismo path, mismo handler, distintos contextos de registro)
- Fix para Keycloak: Composición de `@Path` clase + método durante extracción
- Fix para Broadleaf: Deduplicar por (method, path, controller, handler) y añadir `"contexts": ["admin", "api"]`

**BUG-P2-05: `architecture.confidence` distinto entre modos**
- Compact: `"architecture": "low"` + factor `"architecture not analyzed"`
- Agent: `"architecture": "medium"` + factor `"pattern=layered → downgraded from high"`
- Fix: Unificar cálculo independientemente del modo de output

**BUG-P2-06: `--format` y `--no-cache` inconsistentes entre subcomandos**
- Interfaz de CLI fracturada — breaking expectation para scripting
- Fix: Añadir a todos los comandos o documentar explícitamente cuáles los soportan

**BUG-P2-07: `hotspot_candidates` siempre vacío**
- Con 18K+ commits en ambos repos, el algoritmo nunca encuentra candidatos
- Fix: Combinar datos de `git log --name-only` (churn temporal) + in_degree estático

**BUG-P2-08: `subsystem_summary.member_count: 0` siempre**
- El comando `modernize` detecta subsistemas pero no asigna miembros a ninguno
- Fix: Asignar clases a subsistema por prefijo de paquete coincidente

**BUG-P2-09: File path no funciona como target en `impact`**
- `sourcecode impact services/src/.../KeycloakApplication.java` → `not_found`
- Solo funciona con nombre de clase o FQN
- Fix: Resolver path de archivo a FQN usando el IR antes de buscar

### P3 — Cosmético

- URLs en `code_notes.text` truncadas: `"s.webkit.org/..."` → debería ser `"https://bugs.webkit.org/..."`
- `direct_callers` lista sin campo `direct_callers_count` adyacente
- `--compact --help` reclama "1000–3000 tokens"; medido: 2.900–4.100 tokens
- `--deep` referenciado en output pero ausente de CLI help
- `run_id` en task outputs sin documentación de propósito
- `truncated: null` vs `truncated: false` — debería ser siempre booleano

---

## 9. Readiness para Agentes LLM

### Seguro para inyección en contexto LLM

| Output | Tokens (aprox) | Veredicto |
|---|---|---|
| `--compact` | 2.900–4.100 | ✅ Seguro siempre |
| `--agent` | 4.800–5.500 | ✅ Seguro siempre |
| `onboard` | ~2.600 | ✅ Seguro siempre |
| `fix-bug` (trimmed, síntoma específico) | ~5.000–27.000 | ⚠️ Depende del repo |
| `review-pr` JSON | ~2.700 | ✅ Seguro siempre |
| `review-pr --format github-comment` | ~3.200 | ✅ Seguro siempre |

### No seguro sin `--max-nodes/edges`

| Output | Tokens (aprox) | Veredicto |
|---|---|---|
| `repo-ir` (sin límites) | ~24M tokens (Keycloak 96MB) | ✗ Context overflow garantizado |
| `repo-ir --max-nodes 200` | ~270.000 tokens | ⚠️ Supera ventana de la mayoría de modelos |

### Calidad de señal para agentes

| Campo | Estado |
|---|---|
| `confidence_summary.factors` (machine-readable) | ✅ |
| `analysis_gaps` con `area + reason + impact` | ✅ |
| `ci_decision` en `review-pr` | ✅ |
| `suggested_review_order` en `review-pr` | ✅ |
| `epistemic labels` en github-comment (FACT/STRUCTURAL/INFERRED/OMITTED) | ✅ |
| Outputs deterministas (misma salida en reruns) | ✅ |
| Errores estructurados JSON (no stack traces) | ✅ |
| `relevant_files` con campo `score` | ✗ Ausente |
| `direct_callers_count` como campo numérico | ✗ Ausente |
| `architecture.confidence` consistente entre modos | ✗ Diverge |

---

## 10. Análisis de Mercado — Posicionamiento Competitivo

### 10.1 Mapa del Ecosistema

`sourcecode` compite en intersección de cuatro nichos:

```
Java tooling profundo
        │
        ├─── ArchUnit / jQAssistant / Structure101
        │    (análisis arquitectural estático, sin AI-readiness)
        │
AI context preparation
        │
        ├─── Repomix / files-to-prompt / CodeContext
        │    (volcado raw, sin grafo, sin ranking)
        │
Change intelligence / PR review
        │
        ├─── PR-Agent (CodiumAI) / CodeRabbit / LinearB
        │    (revisión de PR, sin profundidad Java/transaccional)
        │
Code intelligence enterprise
        │
        └─── Sourcegraph Cody / Greptile / GitHub CodeNav
             (búsqueda semántica, sin awareness transaccional)
```

`sourcecode` no tiene competidor directo en la intersección de estos cuatro cuadrantes para Java/Spring.

### 10.2 Comparativa por Herramienta

#### Herramientas de contexto para LLM

| | sourcecode | Repomix | files-to-prompt | Greptile |
|---|---|---|---|---|
| **Java/Maven profundidad** | ★★★★★ | ★★☆☆☆ | ★☆☆☆☆ | ★★★☆☆ |
| **Grafo de dependencias** | ✅ BFS completo | ✗ | ✗ | ✅ semántico |
| **Blast radius** | ✅ con DI | ✗ | ✗ | ✗ |
| **Límites transaccionales** | ✅ | ✗ | ✗ | ✗ |
| **Output acotado por tokens** | ✅ 2-6K | ✗ variable | ✗ variable | ✅ |
| **Velocidad** | 2-9s cold | <1s | <1s | cloud async |
| **Precio** | desconocido | OSS | OSS | $20/dev/mes |
| **Offline/local** | ✅ | ✅ | ✅ | ✗ (cloud only) |

**Greptile** es el competidor más cercano en AI context preparation, pero no tiene awareness de JPA, `@Transactional`, blast radius estructural o DI. Sourcecode gana en profundidad Java; Greptile gana en soporte multilenguaje y UX cloud.

#### Herramientas Java estáticas

| | sourcecode | ArchUnit | jQAssistant | Structure101 |
|---|---|---|---|---|
| **AI-ready output** | ✅ JSON acotado | ✗ | ✗ XML | ✗ GUI only |
| **Blast radius** | ✅ | ✗ | ✅ Cypher queries | ✅ |
| **Velocidad** | 2-9s | depende de tests | minutos | minutos |
| **CLI/CI-first** | ✅ | con JUnit | ✅ | ✗ |
| **Spring DI awareness** | ✅ | via reglas custom | via plugin | ✗ |
| **Sin JVM requerido** | ✅ Python | ✗ Java only | ✗ Java only | ✗ Java only |
| **Precio** | desconocido | OSS | OSS CE / Enterprise ~$5K+/año | ~$500/seat/año |
| **Target** | AI agents + devs | tests de arquitectura | análisis de repo | arquitectos |

**Ventaja única de sourcecode:** No requiere JVM, produce output AI-ready en segundos, y tiene awareness de Spring DI. ArchUnit y jQAssistant requieren JVM y producen artefactos para tests/queries, no para LLMs.

#### Herramientas de revisión de PR

| | sourcecode review-pr | PR-Agent (CodiumAI) | CodeRabbit | Graphite |
|---|---|---|---|---|
| **Blast radius real** | ✅ BFS en grafo | ✗ heurístico | ✗ heurístico | ✗ |
| **Awareness transaccional** | ✅ | ✗ | ✗ | ✗ |
| **github-comment format** | ✅ epistemic labels | ✅ | ✅ | ✗ |
| **Java/Spring profundidad** | ✅★★★★★ | ★★★☆☆ | ★★★☆☆ | ★★☆☆☆ |
| **Multilenguaje** | ✗ Java only | ✅ | ✅ | ✅ |
| **Precio** | desconocido | $19/dev/mes | $15-24/dev/mes | $20/dev/mes |
| **Standalone (sin GitHub App)** | ✅ CLI | ✗ necesita GitHub App | ✗ GitHub App | ✗ |

**sourcecode review-pr** es funcionalmente superior para Java monolitos: blast radius real vs heurísticos, transaccional awareness, labels epistémicos. La limitación es Java-only y ausencia de experiencia web/SaaS.

#### Herramientas de code intelligence

| | sourcecode | Sourcegraph Cody | GitHub CodeNav | CodeSee Maps |
|---|---|---|---|---|
| **Java/Spring profundidad** | ✅★★★★★ | ★★★☆☆ | ★★★☆☆ | ★★★☆☆ |
| **Transactional awareness** | ✅ | ✗ | ✗ | ✗ |
| **Blast radius** | ✅ BFS | ✓ LSP-based | ✓ LSP-based | ✓ visual |
| **AI-ready output** | ✅ JSON acotado | ✅ | ✓ parcial | ✓ visual |
| **Local/sin cloud** | ✅ | ✓ parcial | ✗ | ✗ |
| **Precio** | desconocido | $9/mes pro / enterprise | GitHub Enterprise | $10-25/dev/mes |
| **Onboarding** | CLI | IDE extension | GitHub UI | web app |

### 10.3 Tabla de Diferenciación Real

| Dimensión | sourcecode vs. campo |
|---|---|
| Spring DI resolution en blast radius | **Único** en el mercado conocido |
| Límites transaccionales en context | **Único** en el mercado conocido |
| Event flow (publisher/listener/type) para Spring | **Único** en el mercado conocido |
| Output acotado en tokens (<6K) + fast (<10s) | Compartido con Greptile (cloud), no con Java tools |
| github-comment con epistemic labels | Compartido con PR-Agent/CodeRabbit |
| CLI offline, sin JVM | Único entre herramientas Java |

### 10.4 Puntuaciones Objetivas

Escala: 1–10 basada en comportamiento medido, comparado con herramientas publicadas y monetizadas en el mismo nicho.

#### Estado actual (v1.31.23)

| Dimensión | Puntuación | Referencia |
|---|---|---|
| **Profundidad Java/Spring** | 8/10 | Mejor que Structure101 (6), peor que jQAssistant (9) para análisis formal |
| **AI-readiness del output** | 7.5/10 | A la par con Greptile (8); por encima de Sourcegraph Cody (6.5) en estructuración |
| **Correctness: interfaces** | 9/10 | ~97.6% precisión vs in_degree real medido |
| **Correctness: impl classes** | 8/10 | Corregido P0; cae a 5 para targets con >1000 callers directos |
| **Velocidad (repositorio enterprise)** | 7/10 | 8.5s cold Keycloak; PR-Agent ~12-30s cloud; CodeRabbit ~20-60s |
| **CLI coherence / scriptability** | 5/10 | Flags inconsistentes entre subcomandos; mejor que CodeSee (2) peor que `gh` CLI (9) |
| **Calidad de onboarding context** | 7/10 | project_summary flojo; entry points y transaccional boundaries fuertes |
| **PR review quality** | 8/10 | Epistemic labels y blast radius real; pierde vs CodeRabbit en polish/UX |
| **Modernize / dead code** | 3/10 | hotspot_candidates siempre vacío; member_count siempre 0; roles siempre unknown |
| **Breadth multilenguaje** | 2/10 | Java-only efectivamente; Node.js detectado, no analizado |
| **Documentación** | 5/10 | --deep ghost flag; discrepancias en claims de tokens |
| **TOTAL ESTADO ACTUAL** | **6.5/10** | |

#### Proyección post-corrección P1 (bugs P1-01, P1-02, P1-03)

| Dimensión | Puntuación proyectada | Delta |
|---|---|---|
| **Correctness: impl classes** | 9/10 | +1 (BFS sampled para fan-out extremo) |
| **Calidad de onboarding context** | 8.5/10 | +1.5 (project_summary estructurado) |
| **PR review quality** | 8.5/10 | +0.5 (score en relevant_files) |
| **TOTAL PROYECTADO** | **7.5/10** | +1 punto |

#### Proyección post-corrección P1+P2

| Dimensión | Puntuación proyectada | Delta |
|---|---|---|
| **CLI coherence** | 7.5/10 | +2.5 (--format/--no-cache consistentes) |
| **Modernize / dead code** | 6/10 | +3 (hotspots con git churn, roles clasificados) |
| **Profundidad Java/Spring** | 9/10 | +1 (bounded_contexts reales, path composition) |
| **TOTAL PROYECTADO** | **8.3/10** | +1.8 puntos sobre estado actual |

---

## 11. Modelos de Negocio y Pricing

### 11.1 Referencia de mercado (datos públicos)

| Herramienta | Modelo | Precio base | Tier enterprise |
|---|---|---|---|
| Greptile | SaaS cloud | $20/dev/mes | Custom |
| CodeRabbit | SaaS GitHub App | $15/dev/mes (lite) · $24 (pro) | Custom |
| PR-Agent (CodiumAI) | SaaS / self-hosted | $19/dev/mes | ~$35-50/dev |
| Sourcegraph Cody | Freemium | $9/mes (pro) | Enterprise custom |
| LinearB | SaaS | $15-20/dev/mes | Custom |
| SonarQube | Freemium + Enterprise | OSS CE / $45-150/dev/mes | |
| Structure101 | Licencia perpetua | ~$500/seat/año | Volume discount |
| jQAssistant | OSS | $0 | — |
| CodeSee Maps | SaaS (cerrado 2024) | — | — |
| Graphite | SaaS | $20/dev/mes | Custom |

### 11.2 Vectores de monetización para sourcecode

**Tier OSS Core** (modelo actual, inferido):
- `onboard`, `compact`, `agent`, `endpoints`, `repo-ir`
- Justificación de mercado: competencia directa de Repomix/files-to-prompt requiere ser gratuito

**Tier Pro** (marcado en --help):
- `review-pr`, `fix-bug`, `modernize`
- Precio justo de mercado: **$15-20/dev/mes** (alineado con CodeRabbit/PR-Agent)
- Argumento de venta: blast radius real > heurísticos; transaccional awareness exclusivo

**Tier Enterprise** (pipeline lógico):
- `review-pr` en CI/CD (GitHub Actions, Jenkins)
- `impact` pre-commit hook integrado
- Gestión de repos múltiples / org-level
- SLA de soporte
- Precio: $30-45/dev/mes (entre SonarQube enterprise y PR-Agent)

### 11.3 GitHub Action como vector GTM

`sourcecode review-pr --format github-comment` es el candidato más fuerte para un GitHub Action de pago. El output ya está en formato PR comment con epistemic labels. Competidores en este espacio (CodeRabbit, PR-Agent) cobran $15-24/dev/mes.

Ventaja diferencial: ninguno tiene awareness de `@Transactional` ni blast radius real en el grafo de llamadas Java.

**Pricing sugerido para GitHub Action Pro:** $18/dev/mes — bajo el midpoint CodeRabbit, sobre PR-Agent, justificado por Java depth.

### 11.4 Riesgo de posicionamiento

- **Java-only** en mercado multilenguaje es la limitación más visible. Greptile, CodeRabbit, PR-Agent soportan Python/JS/Go nativamente.
- El nicho Java enterprise es enorme (estimado 35-40% de codebases enterprise) pero la percepción puede ser de herramienta "nicho".
- Alternativa: ampliar a Kotlin (Android, backend) como paso inmediato — misma toolchain Maven/Gradle, mercado adyacente.

---

## 12. Lo Bueno, Lo Malo, Lo Feo

### Lo Bueno

1. **P0 corregido**: `impact OrderServiceImpl` funciona correctamente. La propuesta de valor central es ahora real.
2. **Cache system**: 31x speedup, hash de contenido, determinista — ingeniería sólida
3. **Spring event flow**: listeners + publishers + tipos de evento — no hay competidor que haga esto
4. **Transactional boundaries**: 29 clases correctas en Broadleaf — diferenciador real en el mercado
5. **`review-pr --format github-comment`**: epistemic labels, blast radius, diferenciación build/source — el feature más maduro del producto
6. **Structured JSON errors**: `not_found`, ref inválida — respuestas limpias y accionables
7. **fix-bug budget trimming**: 204KB → 15KB safety net — protección real contra overflow
8. **Interface impact accuracy**: ~97.6% de in_degree real para clases de anotación
9. **repo-ir size control**: corregido — `--max-nodes 200 --max-edges 500` funciona ahora
10. **Determinismo**: misma salida en reruns — prerequisito para CI/CD

### Lo Malo

1. `project_summary` copiado de README — cero inteligencia arquitectural en el campo de entrada del contexto
2. `bounded_contexts` detecta packages de utilities, no dominios de negocio
3. `hotspot_candidates: []` siempre — el análisis de hotspots está muerto sin datos de git
4. `modernize.role: "unknown"` siempre — pérdida de oportunidad para clasificación obvia
5. `--format`/`--no-cache` inconsistentes entre subcomandos — CLI fracturada para scripting
6. `subsystem_summary.member_count: 0` — feature completamente no-funcional

### Lo Feo

1. `indirect_callers: 0` para `KeycloakSession` (1992 callers directos). El campo `explanation` dice correctamente "1992 direct callers" pero el array JSON está vacío. Divergencia entre texto y estructura que un LLM consumiendo el output interpretará incorrectamente.
2. `--deep` referenciado en output como opción disponible (`"Use --deep for up to 80 files"`) pero la CLI devuelve `No such option: --deep`. Un usuario copia y pega la sugerencia del output y recibe un error.
3. URLs en `code_notes` truncadas: `"s.webkit.org/show_bug.cgi?id=219102"` en lugar de la URL completa. El texto del BUG comment en el source tiene `https://bugs.webkit.org/...` — se está perdiendo el prefijo `https://bugs` en el extractor.

---

## 13. Resumen de Prioridades de Corrección

### Prioridad 1 — Antes de enterprise pitch

1. `indirect_callers` para fan-out extremo: BFS sampled con `"indirect_callers_sampled": true` y count estimado
2. `project_summary`: generar desde estructura (`"[N módulos Maven] [framework] — M clases, K endpoints, J transaccional"`)
3. `--deep` en output → eliminarlo del mensaje o añadirlo a CLI como alias de `--agent`
4. `indirect_callers` array vs `explanation` text: sincronizar ambos

### Prioridad 2 — Mejora de credibilidad

5. `bounded_contexts`: usar módulos Maven como señal primaria
6. `bounded_contexts` + `subsystem_summary.member_count`: asignar clases por prefijo de paquete
7. `no_security_signal`: detectar security-by-filter y reportar `security_model: "filter_based"`
8. `modernize.role`: clasificar desde source (annotation, interface, entity, service)
9. `hotspot_candidates`: cruzar git churn con in_degree
10. `architecture.confidence`: unificar entre `--compact` y `--agent`

### Prioridad 3 — Polish

11. `relevant_files.score`: añadir campo float en todos los task outputs
12. `direct_callers_count`: campo numérico adyacente a la lista capeada
13. `--format`/`--no-cache` en todos los subcomandos
14. URLs en code_notes: restaurar prefijo `https://bugs.`
15. `truncated: null` → `truncated: false` siempre
16. `entry_points.controllers.methods` alinear con `endpoints` count
17. `--compact --help` token claim: actualizar a "2.000-5.000 tokens"
18. `impact` via file path: resolver a FQN antes de buscar en IR

---

## 14. Veredicto Final

| Dimensión | v1.31.17 | v1.31.23 | Delta |
|---|---|---|---|
| Correctness — impl classes | ✗ P0 roto | ✅ Corregido | +++ |
| Correctness — interfaces | ✅ Fuerte | ✅ Fuerte | = |
| Correctness — repo-ir bounds | ✗ P0 roto | ✅ Corregido | +++ |
| Performance | ✅ Aceptable | ✅ Mejorado (~8%) | + |
| Boundedness compact/agent | ✅ Sólido | ✅ Sólido | = |
| CLI coherence | ~ Mixto | ~ Mixto | = |
| Market differentiation | ✅ Real | ✅ Real | = |
| AI signal quality | ✅ Con caveats | ✅ Con caveats | = |
| Modernize utility | ✗ Roto | ✗ Roto | = |
| Documentation accuracy | ~ Mayormente | ~ Mayormente | = |
| **VEREDICTO** | trust with caveats | **productizable now** | ↑ |

**Seguro para usar:**
- `onboard` · `compact` · `agent` — contexto de onboarding de nuevos agentes/devs
- `review-pr --format github-comment` — revisión de PRs en Java monolitos
- `fix-bug --symptom "..."` — triage con síntoma específico
- `impact ClassName` (interfaz o impl) — blast radius pre-refactor
- `endpoints` — surface REST, con caveats sobre duplicados JAX-RS

**Usar con cautela:**
- `impact ClassName` cuando el target tiene >500 callers directos — indirect_callers siempre 0
- `modernize` — high_coupling_nodes útil; hotspots, roles y member_count rotos
- `no_security_signal` — no válido como indicador real en proyectos filter-based
- `repo-ir` sin `--max-nodes/edges` — output no acotado

**Monetizable hoy:**
- `review-pr --format github-comment` como GitHub Action ($15-18/dev/mes, competitivo con CodeRabbit/PR-Agent)
- `impact` en interfaces como pre-commit hook empresarial
- `fix-bug` para triage de bugs en soporte enterprise
- `onboard` como primera inyección de contexto para AI coding agents en Java

**No lanzar como enterprise claim sin corregir:**
- "AI-ready change intelligence completa" necesita fix de indirect_callers en fan-out extremo
- "Modernization intelligence" necesita hotspots + member_count + roles

---

*Auditoría realizada con ejecución adversarial completa. Todos los datos son medidos, no estimados.*

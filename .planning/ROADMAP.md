# Roadmap: Sourcecode

## Descripcion General

Sourcecode se construye en dieciseis fases que van desde el scaffold del proyecto hasta una plataforma de analisis semantico e infraestructura para agentes. Las Fases 1 a 10 entregan la herramienta base: CLI instalable, deteccion universal de stacks, dependencias, grafos de modulos, documentacion extraida, calidad de output para LLMs y metricas de codigo. Las Fases 12 a 17 elevan la herramienta hacia comprension semantica real: call graphs, inferencia arquitectonica, flujo de ejecucion, context engine jerarquico para agentes, orquestador interactivo y backend de IR unificado como infraestructura de agentes.

## Fases

- [x] **Fase 1: Fundaciones** - CLI instalable, escaner con respeto a .gitignore, schema JSON versionado, redaccion de secretos y packaging
- [x] **Fase 2: Deteccion Core** - AbstractDetector + detectores para 8 ecosistemas, parseo de manifiestos, deteccion de frameworks y puntos de entrada
- [x] **Fase 3: Clasificacion y Multi-Stack** - TypeClassifier, soporte monorepo/fullstack y niveles de confianza
- [x] **Fase 4: Pulido y Publicacion** - Tests de integracion sobre proyectos reales, documentacion, CI/CD y publicacion en PyPI
- [x] **Fase 5: Scanner Universal** - Ampliar stacks, ecosistemas y senales de deteccion para cubrir mas tipos de proyectos reales
- [x] **Fase 6: Dependencias Inteligentes** - Dependencias exactas, transitivas y resolucion multi-ecosistema con `--dependencies`
- [x] **Fase 7: Grafos de Codigo** - Grafo de modulos, imports, llamadas y jerarquias estructurales con `--graph-modules`
- [ ] **Fase 8: Documentacion Extraida** - Docstrings, comentarios y resúmenes de modulos consumibles por IA con `--docs`
- [ ] **Fase 9: LLM Output Quality** - Optimizar signal/noise del output: file_paths plano, project_summary NL, DocRecord.importance, key_dependencies, compact mejorado
- [x] **Fase 10: Metricas de Calidad** - LOC, complejidad, tests asociados y cobertura con `--full-metrics`
- [x] **Fase 12: Semantica Estatica** - Call graph real, linking cross-file de simbolos, dataflow basico y resolucion de imports avanzada con `--semantics`
- [ ] **Fase 13: Inferencia Arquitectonica** - Clustering por dominios, deteccion de capas, clasificacion arquitectonica y bounded contexts con `--architecture`
- [ ] **Fase 14: Flujo de Ejecucion** - Request flow tracing, pipelines end-to-end, eventos/async flows y critical path extraction con `--execution-flow`
- [ ] **Fase 15: Context Engine para LLM** - Contexto jerarquico, vistas por rol, incremental diff y compresion estructurada para reasoning con `--context-engine`
- [ ] **Fase 16: Orquestador (wizard)** - whoami, pipeline por fases, ejecucion interactiva y seleccion de analisis por tipo de repo
- [ ] **Fase 17: Agent Backend System** - IR unificado (SourceMap), tool chaining, outputs para reasoning loops e integracion con agentes autonomos

## Detalles de Fases

### Fase 1: Fundaciones
**Goal**: El usuario puede instalar `sourcemap-gen` via pip, ejecutarlo en cualquier directorio y obtener un JSON/YAML valido con metadatos del proyecto, arbol de ficheros filtrado y sin secretos expuestos.
**Depends on**: Nothing (primera fase)
**Requirements**: CLI-01, CLI-02, CLI-03, CLI-04, CLI-05, CLI-06, SCAN-01, SCAN-02, SCAN-03, SCAN-04, SCAN-05, SEC-01, SEC-02, SEC-03, DIST-01, DIST-02, DIST-03, DIST-04, OUT-01, OUT-02, OUT-03, OUT-04
**Status**: COMPLETE — verified 2026-04-07 (5/5 success criteria)
**Success Criteria** (what must be TRUE):
  1. El usuario ejecuta `sourcemap-gen` en la raiz de un proyecto y obtiene JSON valido en stdout sin configuracion previa
  2. El usuario ejecuta `sourcemap-gen --format yaml --output mapa.yaml` y el fichero se genera con el mismo schema que el JSON
  3. El arbol de ficheros en el output no contiene entradas de `node_modules/`, `.git/`, `__pycache__/`, `venv/` ni artefactos de build
  4. El usuario ejecuta `sourcemap-gen --compact` y el output tiene como maximo ~500 tokens con tipo de proyecto, stack principal, puntos de entrada y arbol de primer nivel
  5. El output nunca expone valores de ficheros `.env` ni patrones de secreto conocidos (`ghp_`, `sk-`, `AKIA`); el schema incluye `schema_version: "1.0"`
**Plans**: 4 planes

Plans:
- [x] 01-01-PLAN.md — Scaffold Python: pyproject.toml con hatchling, src-layout, CLI Typer con todos los flags (CLI-01..06, DIST-01..04)
- [x] 01-02-PLAN.md — Scanner de ficheros: FileScanner con pathspec/GitIgnoreSpec, exclusiones por defecto, symlinks, profundidad (SCAN-01..05)
- [x] 01-03-PLAN.md — Schema JSON v1.0 y serializer: dataclasses AnalysisMetadata+SourceMap, to_json/to_yaml/compact_view (OUT-01..04)
- [x] 01-04-PLAN.md — Redactor de secretos e integracion CLI: SecretRedactor, conexion scanner+schema+redactor+serializer en main() (SEC-01..03)

### Fase 2: Deteccion Core
**Goal**: La herramienta detecta el stack tecnologico y los frameworks de los 8 ecosistemas principales (Node.js, Python, Go, Rust, Java, PHP, Ruby, Dart) parseando ficheros indicadores y manifiestos, e identifica los puntos de entrada del proyecto.
**Depends on**: Fase 1
**Requirements**: DETECT-01, DETECT-02, DETECT-03, DETECT-04
**Success Criteria** (what must be TRUE):
  1. Ejecutado sobre un proyecto Next.js, el output incluye `stack: nodejs`, `frameworks: [Next.js]`, `package_manager: pnpm/npm/yarn` sin ejecutar ningun codigo del proyecto
  2. Ejecutado sobre un proyecto FastAPI, el output incluye `stack: python`, `frameworks: [FastAPI]` inferido del `pyproject.toml` o `requirements.txt`
  3. Ejecutado sobre un proyecto sin manifiesto formal (solo ficheros `.py` sueltos), el output incluye `stack: python` con `detection_method: heuristic` inferido por extension de fichero
  4. El output incluye `entry_points` con al menos un punto de entrada valido para proyectos Node.js, Python, Go y Rust (inferido de manifiestos o patrones de nombre)
**Plans**: 4 planes

Plans:
- [x] 02-01: `AbstractDetector` + orquestador — clase base con contrato `can_detect`/`detect`, `DetectionResult` con stack/confidence/frameworks/package_manager, orquestador `ProjectDetector` que ejecuta todos los detectores sobre el file index
- [x] 02-02: Detectores Node.js y Python — `NodejsDetector` (package.json, tsconfig, lock files; frameworks via deps: react/next/express/fastapi-equivalentes), `PythonDetector` (pyproject.toml, requirements.txt, setup.py, Pipfile, uv.lock; frameworks: django/flask/fastapi/typer)
- [x] 02-03: Detectores Go, Rust y Java — `GoDetector` (go.mod, cmd/; frameworks: gin/echo/cobra), `RustDetector` (Cargo.toml, src/main.rs vs src/lib.rs; frameworks: axum/actix/clap), `JavaDetector` (pom.xml, build.gradle; frameworks: spring-boot/quarkus/android)
- [x] 02-04: Detectores PHP, Ruby y Dart — `PhpDetector` (composer.json, artisan; frameworks: laravel/symfony), `RubyDetector` (Gemfile, config/routes.rb; frameworks: rails/sinatra), `DartDetector` (pubspec.yaml, lib/main.dart; flutter vs dart puro); deteccion heuristica por extension como fallback universal (DETECT-03)
**UI hint**: no

### Fase 3: Clasificacion y Multi-Stack
**Goal**: El output refleja correctamente proyectos multi-stack y monorepos, clasifica el tipo de proyecto (webapp/api/library/cli/monorepo/fullstack/unknown) e incluye niveles de confianza en cada deteccion.
**Depends on**: Fase 2
**Requirements**: DETECT-05, DETECT-06, DETECT-07, OUT-05
**Success Criteria** (what must be TRUE):
  1. Ejecutado sobre un monorepo con `pnpm-workspace.yaml` o `go.work`, el output tiene `project_type: monorepo` y lista los sub-proyectos con sus propios stacks
  2. Ejecutado sobre un proyecto fullstack (Next.js + FastAPI en el mismo repo), el output lista ambos stacks con `primary: true/false` en lugar de reportar solo uno
  3. Cada stack detectado incluye `confidence: high|medium|low` basado en el numero y peso de los indicadores encontrados
  4. Ejecutado sobre un directorio sin ningun manifiesto conocido, el output es JSON valido con `detection_confidence: low` y `project_type: unknown` en lugar de error
**Plans**: 2 planes

Plans:
- [x] 03-01: `TypeClassifier` — logica de clasificacion webapp/api/library/cli/monorepo/fullstack/unknown basada en senales (directorios pages/, routes/, components/, presencia de bin en package.json, src/lib.rs, etc.); niveles de confianza high/medium/low por numero y peso de indicadores (DETECT-06, DETECT-07)
- [x] 03-02: Soporte monorepo y multi-stack — deteccion de senales de monorepo (pnpm-workspace.yaml, go.work, Cargo.toml [workspace], lerna.json, turbo.json); analisis recursivo limitado a profundidad 3; output estructurado con lista de workspaces y sus stacks (DETECT-05); proyectos sin manifiesto emiten output valido con confidence low (OUT-05)
**UI hint**: no

### Fase 4: Pulido y Publicacion
**Goal**: La herramienta supera tests de integracion sobre proyectos reales de los stacks principales, tiene documentacion de usuario completa y esta publicada en PyPI como `sourcemap-gen`.
**Depends on**: Fase 3
**Requirements**: (ninguno nuevo — esta fase valida y publica lo construido en las fases anteriores)
**Success Criteria** (what must be TRUE):
  1. Los tests de integracion sobre proyectos reales (un repo Next.js, un repo FastAPI, un repo Go, un monorepo) pasan en CI sin fallos
  2. `pip install sourcemap-gen` en Python 3.9, 3.10, 3.11 y 3.12 instala la herramienta correctamente y `sourcemap-gen --version` devuelve la version
  3. El README explica en menos de 5 minutos de lectura como instalar, usar y que contiene el output; la documentacion del schema esta disponible en el repositorio
  4. El repositorio tiene CI/CD con GitHub Actions que ejecuta tests en la matriz Python 3.9-3.12 y publica a PyPI automaticamente en cada tag de release
**Status**: COMPLETE — implemented 2026-04-07; pytest green, docs and workflows added, lint/type debt remains visible in CI
**Plans**: 3 planes

Plans:
- [x] 04-01: Tests de integracion — suite de tests contra proyectos reales como fixtures (Next.js, FastAPI, Go stdlib, monorepo pnpm); verificacion del schema completo y smoke de packaging local
- [x] 04-02: Documentacion de usuario — README con instalacion, uso rapido, descripcion del schema y ejemplos de output; documentacion del schema JSON v1.0 en `docs/schema.md`
- [x] 04-03: CI/CD y publicacion PyPI — GitHub Actions con matriz Python 3.9-3.12 (lint, typecheck, tests); publicacion automatizada via trusted publishing (OIDC) para TestPyPI/PyPI
**UI hint**: no

## Progreso

### Fase 5: Scanner Universal
**Goal**: La herramienta aumenta significativamente su cobertura y se comporta mas cerca de un scanner universal, detectando mas ecosistemas, manifests, build systems y senales heuristicas en proyectos reales que hoy quedan como `unknown` o parcialmente clasificados.
**Depends on**: Fase 4
**Requirements**: DETECT-08, DETECT-09, DETECT-10, OUT-06
**Success Criteria** (what must be TRUE):
  1. Ejecutado sobre proyectos de ecosistemas adicionales o tooling menos comun, la herramienta detecta correctamente el stack principal sin depender de un unico manifiesto convencional
  2. Ejecutado sobre repos con combinaciones de manifests/build files (`Dockerfile`, `Makefile`, `Procfile`, `bun.lockb`, `poetry.lock`, `composer.lock`, `Gemfile.lock`, etc.), el output usa esas senales para elevar confianza o completar clasificacion
  3. Ejecutado sobre proyectos mixtos o legacy con estructura irregular, la herramienta reduce de forma visible los casos `project_type: unknown` y `stacks: []`
  4. El schema conserva compatibilidad hacia atras, pero incorpora senales mas expresivas para justificar detecciones multi-fuente
**Status**: COMPLETE — implemented 2026-04-07; new stacks added, universal signals integrated, full suite green
**Plans**: 4 planes

Plans:
- [x] 05-01: Base universal de senales — manifests alternativos, tooling transversal, scoring multi-fuente y deduplicacion del pipeline
- [x] 05-02: Nuevos ecosistemas managed — detectores para .NET/C#, Elixir/Phoenix y JVM ampliado (Kotlin/Scala)
- [x] 05-03: Ecosistemas infra/systems — Terraform, C/C++, build files y capa de tooling universal
- [x] 05-04: Integracion universal end-to-end — fixtures adicionales, fusion heuristica final y regresion completa de CLI/suite
**UI hint**: no

### Fase 6: Dependencias Inteligentes
**Goal**: La herramienta puede enumerar dependencias externas con versiones exactas y, cuando haya lockfiles o metadata suficiente, resolver tambien dependencias transitivas sin sacrificar la velocidad del comando base.
**Depends on**: Fase 5
**Requirements**: DEPS-01, DEPS-02, DEPS-03, OUT-07
**Success Criteria** (what must be TRUE):
  1. Ejecutado como `sourcecode . --dependencies` sobre proyectos Python, Node.js, PHP, Ruby, Rust, Go y .NET, el output lista dependencias directas con nombre, version exacta o constraint y origen del dato (`manifest`, `lockfile`, `tooling`)
  2. Cuando existe lockfile compatible (`package-lock.json`, `pnpm-lock.yaml`, `poetry.lock`, `uv.lock`, `Gemfile.lock`, `composer.lock`, etc.), el output incluye dependencias transitivas y las relaciona con su dependencia padre o grafo de resolucion
  3. El comando base `sourcecode .` mantiene su latencia habitual y no resuelve arboles transitivos salvo que el usuario active `--dependencies`
  4. El schema expone dependencias de forma uniforme entre ecosistemas para que agentes IA puedan detectar incompatibilidades, duplicados o riesgos de version
**Status**: COMPLETE — implemented 2026-04-08; dependency analysis integrated behind `--dependencies`, full suite green
**Plans**: 4 planes

Plans:
- [x] 06-01: Base de dependencias — schema, flag `--dependencies` y analizador lazy desacoplado
- [x] 06-02: Node.js y Python — manifests + lockfiles con deps directas, resueltas y transitivas
- [x] 06-03: PHP, Ruby, Rust, Go y .NET — cobertura polyglot con limites explicitos donde la transitividad offline sea parcial
- [x] 06-04: Integracion final — workspaces/monorepo, tests end-to-end y documentacion publica del contrato
**UI hint**: no

### Fase 7: Grafos de Codigo
**Goal**: La herramienta construye una vista estructural del codigo con relaciones entre modulos, imports, llamadas y jerarquias basicas para ayudar a agentes y humanos a entender flujo y acoplamiento.
**Depends on**: Fase 6
**Requirements**: GRAPH-01, GRAPH-02, GRAPH-03, OUT-08
**Success Criteria** (what must be TRUE):
  1. Ejecutado como `sourcecode . --graph-modules`, el output incluye un grafo de imports internos entre modulos o paquetes del proyecto en al menos Python, Node.js/TypeScript, Java y Go cuando la informacion estatico-sintactica sea suficiente
  2. El output identifica nodos clave como funciones top-level, clases o entry points y, cuando sea factible con analisis seguro, relaciones de llamada o uso entre ellos
  3. En proyectos grandes o multi-stack, el analisis se degrada con seguridad mediante limites de profundidad/tamano en lugar de bloquearse o intentar parseo total no acotado
  4. El schema deja claro el nivel de confianza y el metodo de construccion del grafo (`ast`, `heuristic`, `unresolved`) para que el consumidor sepa cuanto confiar en cada arista
**Status**: COMPLETE — implemented 2026-04-08; module graph integrated behind `--graph-modules`, full suite green
**Plans**: 4 planes

Plans:
- [x] 07-01: Base del grafo — schema, flag `--graph-modules` y analizador lazy desacoplado
- [x] 07-02: Python y Node.js/TypeScript — imports internos, nodos de modulo y degradacion segura
- [x] 07-03: Relaciones estructurales y soporte polyglot — llamadas simples, jerarquias basicas y soporte inicial Go/JVM
- [x] 07-04: Integracion final — workspaces, limites de analisis, tests end-to-end y documentacion publica del contrato
**UI hint**: no

### Fase 8: Documentacion Extraida
**Goal**: La herramienta extrae documentacion util del codigo y la transforma en un resumen estructurado por modulo, clase o funcion para consumo de agentes IA y onboarding tecnico.
**Depends on**: Fase 7
**Requirements**: DOCS-01, DOCS-02, DOCS-03, OUT-09
**Success Criteria** (what must be TRUE):
  1. Ejecutado como `sourcecode . --docs`, el output incluye docstrings, comentarios de cabecera y descripciones resumidas por modulo para lenguajes soportados donde el parseo sea fiable
  2. El output distingue entre documentacion explicita del autor y resumen inferido por la herramienta para evitar mezclar texto original con interpretaciones
  3. Los modulos principales del proyecto incluyen un resumen compacto con proposito, simbolos destacados y relaciones con otros componentes
  4. El schema sigue siendo consumible por maquinas y evita volcar bloques enormes de texto sin estructura ni procedencia
**Plans**: 4 planes

Plans:
- [x] 08-01-PLAN.md — Schema + CLI base: DocRecord, DocSummary, DocsDepth en schema.py; flags --docs/--docs-depth; scaffold DocAnalyzer; Wave 0 test stubs
- [x] 08-02-PLAN.md — Python + JS/TS extractors: Python AST extractor con docstrings y firmas; JS/TS JSDoc regex extractor con brace-depth; limites y truncacion
- [x] 08-03-PLAN.md — Monorepo + end-to-end: workspace support, merge_summaries funcional, integracion CLI completa, tests de integracion DOCS-ACC-01 a DOCS-ACC-10
- [ ] 08-04-PLAN.md — Polish + verificacion: serializacion verificada, compact exclusion, linting/typing, DocSummary enriquecido, todos los acceptance criteria verdes
**UI hint**: no

### Fase 9: LLM Output Quality
**Goal**: Optimizar el output de la herramienta para maximizar la utilidad por token para LLMs consumidores: reducir ruido, añadir resumen en lenguaje natural, exponer paths planos, priorizar simbolos por importancia y mejorar el modo compacto.
**Depends on**: Fase 8
**Requirements**: LQN-01, LQN-02, LQN-03, LQN-04, LQN-05, LQN-06
**Success Criteria** (what must be TRUE):
  1. `sourcecode .` incluye `file_paths` como lista plana de paths relativos derivada del `file_tree`, sin necesidad de que el LLM reconstruya rutas desde el dict anidado
  2. `sourcecode .` incluye `project_summary` con una descripcion en lenguaje natural de 2-4 lineas generada deterministicamente desde stacks, project_type, entry_points y dependency_summary
  3. `sourcecode . --docs` emite DocRecords con campo `importance: high|medium|low` y no incluye registros con `source="unavailable"` en `docs[]` (solo en `doc_summary.limitations`)
  4. `sourcecode . --dependencies` expone `key_dependencies[]` con las top-15 dependencias directas mas relevantes; `sourcecode . --compact --dependencies` incluye `dependency_summary`
**Plans**: 3 planes

Plans:
- [ ] 09-01-PLAN.md — Schema base + ProjectSummarizer + file_paths: campos nuevos en SourceMap, summarizer.py, wiring en cli.py (LQN-01, LQN-02, LQN-05)
- [ ] 09-02-PLAN.md — DocRecord.importance + filtro unavailable: inferencia estructural, entry_points param, filtro else-block (LQN-03, LQN-04)
- [ ] 09-03-PLAN.md — compact_view + integracion E2E + polish: compact_view actualizado, tests LQN-01..06, ruff+mypy gate (LQN-01..06)
**UI hint**: no

### Fase 10: Metricas de Calidad
**Goal**: La herramienta aporta senales cuantitativas sobre complejidad, tamano, tests y cobertura para priorizar refactors, auditorias y trabajo de agentes automaticos.
**Depends on**: Fase 9
**Requirements**: METRICS-01, METRICS-02, METRICS-03, OUT-10
**Success Criteria** (what must be TRUE):
  1. Ejecutado como `sourcecode . --full-metrics`, el output incluye lineas por archivo o modulo, recuentos de simbolos y una medida de complejidad al menos para los lenguajes donde haya analisis estatico seguro
  2. La herramienta detecta archivos o suites de tests relacionadas y asocia modulos productivos con evidencia de cobertura o ausencia de pruebas
  3. Cuando existe metadata de cobertura (`coverage.xml`, `.coverage`, `lcov.info`, `jacoco.xml`, etc.), el output la incorpora sin ejecutar tests por defecto
  4. El comando distingue claramente entre metricas medidas, inferidas y no disponibles para no transmitir precision falsa
**Status**: COMPLETE — implemented 2026-04-10; --full-metrics flag wired, 4 E2E tests green, ruff+mypy gate passed
**Plans**: 4 planes

Plans:
- [x] 10-01-PLAN.md — Schema + MetricsAnalyzer skeleton + LOC counters: FileMetrics, CoverageRecord, MetricsSummary en schema.py; MetricsAnalyzer con conteo LOC/simbolos por tier de lenguaje (METRICS-01, OUT-10)
- [x] 10-02-PLAN.md — CoverageParser: parsers para Cobertura XML, .coverage SQLite, LCOV y JaCoCo XML con stdlib (METRICS-03, OUT-10)
- [x] 10-03-PLAN.md — Test association + MetricsAnalyzer completo: is_test_file(), infer_production_target(), integracion CoverageParser en analyze(), merge_summaries() (METRICS-01, METRICS-02, METRICS-03, METRICS-04, OUT-10)
- [x] 10-04-PLAN.md — CLI wiring + E2E + quality gate: flag --full-metrics, workspace loop, ruff+mypy gate (METRICS-01, METRICS-02, METRICS-03, METRICS-04, OUT-10)
**UI hint**: no

### Fase 12: Semantica Estatica
**Goal**: La herramienta construye un grafo de llamadas real con linking cross-file de simbolos y resolucion de imports avanzada, permitiendo entender que hace el codigo mas alla de que archivos existen.
**Depends on**: Fase 10
**Requirements**: SEM-01, SEM-02, SEM-03, SEM-04
**Success Criteria** (what must be TRUE):
  1. Ejecutado como `sourcecode . --semantics`, el output incluye un call graph que relaciona funciones/metodos entre archivos del mismo proyecto para Python y JS/TS como minimo
  2. El output resuelve que simbolos (clases, funciones, constantes) son importados en cada archivo y desde que modulo origen provienen, diferenciando imports internos de externos
  3. El analisis degrada con seguridad en proyectos grandes mediante limites de profundidad y tamano, reportando que porcion del proyecto fue analizada
  4. El schema expone el nivel de confianza del call graph (`full`, `partial`, `heuristic`) por lenguaje para que el consumidor entienda las limitaciones
**Plans**: 4 planes
**UI hint**: no

Plans:
- [x] 12-01-PLAN.md — Schema + SemanticAnalyzer skeleton + Python call graph core (SEM-01, SEM-03, SEM-04)
- [x] 12-02-PLAN.md — Python import resolution avanzada + symbol linker (SEM-01, SEM-02)
- [x] 12-03-PLAN.md — JS/TS semantic layer + basic dataflow (SEM-01, SEM-04)
- [x] 12-04-PLAN.md — Polyglot heuristics + CLI wiring + E2E + quality gate (SEM-01, SEM-02, SEM-03, SEM-04)

### Fase 13: Inferencia Arquitectonica
**Goal**: La herramienta agrupa modulos en dominios funcionales e infiere la arquitectura en capas del sistema (controller/service/repo, frontend/backend, etc.) sin configuracion previa.
**Depends on**: Fase 12
**Requirements**: ARCH-01, ARCH-02, ARCH-03, ARCH-04
**Success Criteria** (what must be TRUE):
  1. Ejecutado como `sourcecode . --architecture`, el output agrupa archivos y modulos en dominios funcionales inferidos del analisis del call graph y rutas
  2. La herramienta detecta patrones de capas comunes: MVC, controller/service/repository, hexagonal y capas frontend/backend en proyectos fullstack
  3. El output identifica bounded contexts aproximados en proyectos de dominio rico usando senales del module graph y nomenclatura de simbolos
  4. En proyectos sin arquitectura clara, el output lo indica explicitamente en lugar de generar agrupaciones sin soporte de evidencia
**Plans**: 0 planes
**UI hint**: no

### Fase 14: Flujo de Ejecucion
**Goal**: La herramienta traza como fluye una request o evento desde su entrada hasta su resolucion, exponiendo pipelines end-to-end y rutas criticas del sistema.
**Depends on**: Fase 13
**Requirements**: EXEC-01, EXEC-02, EXEC-03, EXEC-04
**Success Criteria** (what must be TRUE):
  1. Ejecutado como `sourcecode . --execution-flow`, el output traza el flujo de una request HTTP desde el entry point hasta la respuesta en proyectos API (FastAPI, Express, Spring, etc.)
  2. El output identifica pipelines de procesamiento: middleware chains, async event flows, worker queues y jobs programados cuando hay senales suficientes
  3. La herramienta extrae la ruta critica del sistema: el camino de codigo mas largo o mas cargado desde entrada hasta salida
  4. El analisis de flujo es puramente estatico — no ejecuta codigo del proyecto y no requiere un servidor activo
**Plans**: 0 planes
**UI hint**: no

### Fase 15: Context Engine para LLM
**Goal**: La herramienta genera contexto optimizado y jerarquico para agentes IA, con vistas especializadas por rol, diff incremental y compresion estructurada para ventanas de contexto limitadas.
**Depends on**: Fase 14
**Requirements**: CTX-01, CTX-02, CTX-03, CTX-04
**Success Criteria** (what must be TRUE):
  1. La herramienta genera vistas de contexto especializadas por rol de agente: `--context-engine --role architect`, `--role debugger`, `--role onboarding` con distinto nivel de detalle y foco
  2. El output incluye contexto jerarquico colapsable: resumen de alto nivel que expande a detalle por dominio o modulo bajo demanda
  3. La herramienta soporta diff incremental: dado un analisis anterior, emite solo lo que ha cambiado para minimizar tokens consumidos en actualizaciones de contexto
  4. El output de `--compact --context-engine` se mantiene en un presupuesto de tokens configurable sin perder la informacion mas relevante
**Plans**: 0 planes
**UI hint**: no

### Fase 16: Orquestador (wizard)
**Goal**: La herramienta guia al usuario (humano o agente) en la exploracion del repositorio con un pipeline interactivo que selecciona que analisis ejecutar segun el tipo de proyecto.
**Depends on**: Fase 15
**Requirements**: WIZ-01, WIZ-02, WIZ-03
**Success Criteria** (what must be TRUE):
  1. `sourcecode . whoami` emite un resumen en lenguaje natural del proyecto en menos de 500 tokens, combinando project_summary con los analisis disponibles mas relevantes
  2. La herramienta detecta el tipo de repo y propone automaticamente el conjunto optimo de flags: `--dependencies --semantics` para librerias, `--execution-flow --architecture` para backends, etc.
  3. La ejecucion interactiva permite al agente solicitar analisis adicionales incrementalmente sin re-escanear el arbol de ficheros
**Plans**: 0 planes
**UI hint**: no

### Fase 17: Agent Backend System
**Goal**: La herramienta sirve como infraestructura de analisis para agentes autonomos con una representacion intermedia unificada (SourceMap IR), tool chaining y outputs optimizados para reasoning loops.
**Depends on**: Fase 16
**Requirements**: AGENT-01, AGENT-02, AGENT-03
**Success Criteria** (what must be TRUE):
  1. El SourceMap IR unifica todos los analisis en una representacion navegable por agentes: stacks, semantica, arquitectura, flujo y contexto accesibles via un API determinista
  2. La herramienta expone una API de tool chaining donde un agente puede solicitar analisis especificos y combinar sus outputs en un grafo de razonamiento
  3. Los outputs de la herramienta estan optimizados para reasoning loops: cada artefacto incluye metadatos de confianza, cobertura y limitaciones para que el agente sepa cuanto confiar en cada dato
**Plans**: 0 planes
**UI hint**: no

## Progreso

**Orden de ejecucion:**
Las fases se ejecutan en orden numerico: 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10 → 12 → 13 → 14 → 15 → 16 → 17

| Fase | Planes Completos | Estado | Completada |
|------|-----------------|--------|------------|
| 1. Fundaciones | 4/4 | Complete   | 2026-04-07 |
| 2. Deteccion Core | 4/4 | Complete   | 2026-04-07 |
| 3. Clasificacion y Multi-Stack | 2/2 | Complete   | 2026-04-07 |
| 4. Pulido y Publicacion | 3/3 | Complete   | 2026-04-07 |
| 5. Scanner Universal | 4/4 | Complete   | 2026-04-07 |
| 6. Dependencias Inteligentes | 4/4 | Complete | 2026-04-08 |
| 7. Grafos de Codigo | 4/4 | Complete | 2026-04-08 |
| 8. Documentacion Extraida | 4/4 | Complete | 2026-04-09 |
| 9. LLM Output Quality | 3/3 | Complete | 2026-04-10 |
| 10. Metricas de Calidad | 4/4 | Complete | 2026-04-10 |
| 12. Semantica Estatica | 4/4 | Complete | 2026-04-11 |
| 13. Inferencia Arquitectonica | 0/0 | Not planned | - |
| 14. Flujo de Ejecucion | 0/0 | Not planned | - |
| 15. Context Engine para LLM | 0/0 | Not planned | - |
| 16. Orquestador (wizard) | 0/0 | Not planned | - |
| 17. Agent Backend System | 0/0 | Not planned | - |

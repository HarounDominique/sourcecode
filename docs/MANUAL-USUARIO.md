# Manual de usuario — sourcecode

> Versión de la herramienta: **1.64.0**
> Público: personas que van a usar `sourcecode` por primera vez, sin conocimiento previo.

---

## 1. ¿Qué es sourcecode?

`sourcecode` es una herramienta de línea de comandos (CLI) que **lee el código de un proyecto y lo resume en datos estructurados** para que tanto un agente de IA (como Claude, Cursor o Copilot) como una persona puedan entender rápidamente:

- Qué es el proyecto y por dónde se empieza.
- Cómo están conectadas las piezas entre sí.
- Qué se rompe si tocas una clase concreta.
- Qué endpoints REST expone la aplicación.
- Qué riesgos de seguridad, transacciones o migración tiene.

Piensa en ella como un **"mapa de carreteras" del código**: en vez de leerte 300.000 líneas a mano, le preguntas a `sourcecode` y te devuelve el mapa en segundos.

**Importante:** el análisis es **determinista y estático**. No ejecuta el código ni "adivina" con IA: lee los ficheros fuente y extrae hechos comprobables. El mismo proyecto siempre da el mismo resultado.

### ¿Para qué tipo de proyectos?

- **Funciona con cualquier lenguaje** para lo básico (estructura, dependencias, puntos de entrada, hotspots de git).
- **Brilla especialmente con Java y Spring** (Spring Boot, Spring MVC, JAX-RS, Hibernate/JPA, MyBatis). Las funciones avanzadas (impacto, auditoría de transacciones, seguridad, migración Spring Boot 2→3) están pensadas para ese ecosistema.

A lo largo del manual usamos como ejemplos proyectos reales y muy conocidos del mundo Java/Spring:

- **Spring PetClinic** — la app de ejemplo oficial de Spring.
- **Keycloak** — servidor de identidad de Red Hat.
- **Broadleaf Commerce** y **Shopizer** — plataformas e-commerce en Spring.
- **Apache OpenMRS** — sistema médico open source.

---

## 2. Conceptos clave (léelos una vez)

| Concepto | Qué significa en palabras llanas |
|----------|-----------------------------------|
| **Caché (cache)** | La primera vez que analiza un repo tarda unos segundos ("cold scan", 2–10 s). El resultado se guarda, y las siguientes veces responde en milisegundos ("warm cache", 0.3–0.6 s). |
| **RIS** (*Repository Intelligence Snapshot*) | Una "foto" completa del repo ya analizada, guardada en disco. Permite arranques instantáneos sin volver a analizar. |
| **IR** (*Intermediate Representation*) | Una representación a nivel de símbolos (clases, métodos, relaciones) del código Java. Es el "grafo" sobre el que se calcula todo lo demás. |
| **Blast radius** (radio de impacto) | A cuántas cosas afecta un cambio. Si tocas una clase, ¿quién la llama y qué se rompe? |
| **Endpoint** | Una URL de una API REST (por ejemplo `GET /owners/{id}`). |
| **Símbolo (symbol)** | Una unidad de código con nombre: una clase, una interfaz o un método. |

### Formas de salida

Casi todos los comandos comparten estas opciones:

- `--format` / `-f`: formato de salida. Normalmente `json` (por defecto) o `yaml`. Algunos comandos añaden `text` o `github-comment`.
- `--output` / `-o`: guarda el resultado en un fichero en vez de mostrarlo por pantalla.
- `--copy` / `-c`: copia el resultado al portapapeles del sistema al terminar.

`json` es ideal para que lo consuma un agente o un script; `text` y `yaml` son más cómodos para leer a ojo; `github-comment` genera un comentario en Markdown listo para pegar en un Pull Request.

---

## 3. Instalación

Requisito: **Python 3.9 o superior**.

```bash
# Opción recomendada (entorno aislado)
pipx install sourcecode

# Alternativa
pip install sourcecode

# macOS con Homebrew
brew install sourcecode
```

Comprueba que funciona:

```bash
sourcecode --version
sourcecode --help
```

En este manual el comando se llama `sourcecode`. Si lo ejecutas desde el repositorio de desarrollo, el equivalente es `python run_cli.py`.

---

## 4. El comando base y las opciones globales

Si ejecutas `sourcecode` **sin un subcomando**, hace un análisis general del repositorio actual. Es el punto de partida más habitual.

```bash
sourcecode --compact                 # resumen de alta señal
sourcecode . --compact --git-context # añade actividad de git
sourcecode my-project --agent        # JSON completo para un agente
```

El primer argumento opcional es la **ruta** del proyecto (por defecto `.`, el directorio actual).

### Opciones globales, una a una

#### `--compact`
Genera un **resumen de alta señal** (normalmente 1.000–3.000 tokens según el tamaño del repo). Incluye: stacks tecnológicos, puntos de entrada, resumen de dependencias, nivel de confianza y "huecos" (gaps) de información. En proyectos Java añade automáticamente `security_surface`, `mybatis` y `transactional_boundaries` cuando los detecta.
**Para qué sirve:** es el modo perfecto para **darle contexto a un agente de IA sin gastar muchos tokens**. Si solo vas a usar una opción, usa esta.

#### `--agent`
Devuelve **JSON estructurado y sin ruido**, pensado para que lo consuma directamente una IA: identidad del proyecto, puntos de entrada, dependencias, confianza y huecos.
**Diferencia con `--compact`:** `--agent` prioriza máxima señal y estructura limpia; `--compact` prioriza brevedad. Usa `--agent` cuando el agente necesita el máximo detalle.

#### `--full`
Quita los límites de truncado de las listas largas (por ejemplo `transactional_boundaries` o `mybatis.dto_mappers`).
**Para qué sirve:** cuando necesitas el listado **completo** y no la versión recortada. Cuidado: la salida puede ser grande.

#### `--git-context` / `-g`
Añade la **actividad de git**: commits recientes, "hotspots" (ficheros que más cambian) y cambios sin commitear.
**Para qué sirve:** saber *qué se está moviendo* en el proyecto, no solo cómo está estructurado. Muy útil para revisar un repo activo.

#### `--changed-only`
Limita la salida a los ficheros **modificados según git** (en stage, sin stage o sin trackear).
**Para qué sirve:** generar contexto solo de lo que estás tocando ahora mismo, ignorando el resto del repo.

#### `--env-map`
Mapea las **variables de entorno** referenciadas en todo el código.
**Para qué sirve:** entender de qué configuración externa depende la app (por ejemplo `DATABASE_URL`, `SPRING_PROFILES_ACTIVE`). Nota de seguridad: nunca incluye los *valores* reales de las variables del sistema operativo, solo los nombres y dónde se usan.

#### `--depth N`
Profundidad de recorrido del árbol de ficheros (por defecto `4`, rango 1–20).
**Detalle importante:** los proyectos Java/Maven se ajustan automáticamente a un mínimo de **12**; poner un valor menor a 12 no tiene efecto en ellos (porque la estructura de paquetes Java es profunda).

#### `--exclude "patrón1,patrón2"`
Excluye directorios o patrones adicionales, separados por comas.
**Ejemplo:** `--exclude "legacy,generated"` para ignorar código antiguo o autogenerado.

#### `--no-cache`
Ignora la caché y fuerza un análisis nuevo desde cero.
**Para qué sirve:** cuando sospechas que la caché está desactualizada y quieres garantizar datos frescos.

#### `--no-redact`
Por defecto, `sourcecode` **oculta (redacta) secretos** que pudieran aparecer en cadenas de texto de la salida. Esta opción lo desactiva.
**Aviso de seguridad:** úsalo solo si sabes lo que haces. Aun con esta opción, los *valores* de variables de entorno del sistema nunca se incluyen (es una política de seguridad fija).

#### `--format` / `-f`, `--output` / `-o`, `--copy` / `-c`
Ya explicadas en la sección 2 (formato, fichero de salida, portapapeles).

#### `--version` / `-v`
Muestra la versión y sale.

---

## 5. Subcomandos de análisis

Cada subcomando responde a una pregunta concreta. Aquí van en el orden en el que normalmente se usan.

---

### 5.1 `onboard` — "¿Qué es este repo y por dónde empiezo?"

```bash
sourcecode onboard .
sourcecode onboard /ruta/a/keycloak --llm-prompt
sourcecode onboard . --output onboard.json
```

**Qué hace:** construye el contexto completo de un proyecto pensado para alguien (persona o agente) que **nunca lo ha visto**. Te da el resumen de arquitectura, los subsistemas, los puntos de entrada clave, los hotspots y las señales de deuda técnica.

**En lo que es bueno:** es la primera parada cuando heredas un proyecto desconocido. Por ejemplo, abrir Broadleaf Commerce por primera vez y entender en 5 segundos dónde están los controladores, servicios y módulos principales.

**Opciones:**
- `--llm-prompt`: añade al final un **prompt listo para pegar** en un modelo de IA. Muy cómodo: ejecutas, copias y le preguntas a la IA sobre el repo.
- `--output` / `-o`, `--copy` / `-c`: fichero / portapapeles.

---

### 5.2 `prepare-context` — contexto a medida según la tarea

```bash
sourcecode prepare-context explain
sourcecode prepare-context fix-bug --symptom "NullPointerException en OwnerController"
sourcecode prepare-context delta --since main
sourcecode prepare-context review-pr --since origin/main
sourcecode prepare-context onboard --llm-prompt
```

**Qué hace:** es un "todo en uno" que prepara contexto **según el tipo de tarea** que vas a hacer. Eliges una tarea (`TASK`) y una ruta (`PATH`, por defecto `.`).

**Tareas disponibles:**
| Tarea | Para qué |
|-------|----------|
| `explain` | Arquitectura, puntos de entrada, dependencias clave. |
| `fix-bug` | Ficheros ordenados por riesgo, zonas sospechosas, anotaciones relacionadas. |
| `refactor` | Problemas estructurales y oportunidades de mejora. |
| `generate-tests` | Ficheros fuente sin tests y análisis de huecos de cobertura. |
| `onboard` | Contexto completo para nuevos agentes/desarrolladores. |
| `review-pr` | Diff de un PR: rutas de ejecución, impacto en seguridad/transacciones, huecos de tests (necesita un diff de git o `--since`). |
| `delta` | Contexto incremental: solo los ficheros cambiados según git. |

**Opciones:**
- `--since REF`: punto de referencia de git para las tareas `delta` y `review-pr` (por ejemplo `HEAD~3`, `main`, `origin/main`).
- `--symptom TEXTO`: (tarea `fix-bug`) pista de palabra clave del bug. Sube en el ranking los ficheros que coinciden y muestra notas de código relacionadas. Ejemplo: `--symptom "401 en /api/orders"`.
- `--llm-prompt`: añade un prompt listo para usar con una IA.
- `--task-help`: lista las tareas con su descripción y sale.
- `--dry-run`: muestra **qué se analizaría** sin ejecutarlo (útil para comprobar antes de lanzar un análisis grande).
- `--fast`: salta el análisis profundo (búsqueda de contenido, huecos de tests, anotaciones). Usa solo metadatos. Objetivo: menos de 6 segundos.
- `--include-config`: (tarea `generate-tests`) incluye ficheros de configuración de herramientas (`*.conf.js`, `.eslintrc*`, etc.) en los huecos de tests. Por defecto se excluyen.
- `--all`: (tarea `generate-tests`) devuelve la lista completa de huecos de tests sin recortar al top 20.
- `--format`: `json` (por defecto) o `github-comment` (para la tarea `review-pr`).
- `--output` / `-o`, `--copy` / `-c`.

---

### 5.3 `impact` — "¿Qué se rompe si toco esta clase?"

```bash
sourcecode impact UserService
sourcecode impact org.keycloak.services.DefaultKeycloakSession /ruta/a/keycloak
sourcecode impact UserService --depth 6 --output impact.json
```

**Qué hace:** análisis de **radio de impacto**. Construye el grafo del repositorio y propaga el impacto desde la clase objetivo hacia atrás (quién la usa). Devuelve:
- `direct_callers` — clases que llaman o dependen directamente del objetivo.
- `indirect_callers` — los que dependen de forma transitiva (búsqueda en anchura, limitada por `--depth`).
- `endpoints_affected` — endpoints HTTP que dependen transitivamente del objetivo.
- `transactional_boundaries_touched` — clases `@Transactional` que quedan en la cadena de llamadas.
- `risk_score` / `risk_level` — riesgo cuantificado del cambio.

**En lo que es bueno:** evitar romper cosas sin querer. Antes de cambiar `DefaultKeycloakSession` en Keycloak, ves de un vistazo cuántos endpoints y servicios cuelgan de ella.

**Argumentos:**
- `TARGET` (obligatorio): nombre de clase (simple o completo con paquete) o ruta de fichero. Ejemplos: `UserService`, `org.example.UserService`, `UserService.java`.
- `PATH`: raíz del repo (por defecto `.`).

**Opciones:**
- `--depth N`: profundidad de la búsqueda de llamantes indirectos (1–8, por defecto 4). Más profundidad = más alcance pero salida más grande.
- `--include-tests`: incluye los ficheros de test en el análisis (excluidos por defecto).
- `--output` / `-o`, `--format` / `-f`, `--copy` / `-c`.

> Nota: gratis en repos hasta el límite de tamaño; la versión Pro desbloquea monolitos de escala empresarial.

---

### 5.4 `impact-chain` — radio de impacto **con contexto Spring**

```bash
sourcecode impact-chain OrderService .
sourcecode impact-chain com.example.OrderService#placeOrder /ruta/a/repo
sourcecode impact-chain PaymentService . --depth 6 --output impact.json
sourcecode impact-chain OrderPlacedEvent . --type events
```

**Qué hace:** como `impact`, pero **especializado en Spring** y enriquecido con semántica de transacciones y seguridad. Acepta clases **o métodos concretos**. Devuelve los llamantes directos e indirectos, los endpoints afectados, la frontera transaccional del objetivo, las superficies de seguridad por endpoint, los hallazgos de auditoría TX/SEC que tocan la cadena, y un `risk_level`.

**Modo eventos (`--type events`):** en vez de llamadas, analiza la **topología de eventos** de Spring: quién publica un evento, qué listeners lo consumen (con metadatos de fase transaccional), el grafo publicador → evento → consumidor, y riesgos de consumidores `AFTER_COMMIT` / `BEFORE_COMMIT`.

**En lo que es bueno:** entender el impacto real en una app Spring donde mucho ocurre por inyección de dependencias y eventos, no solo por llamadas directas. Solo Java/Spring.

**Argumentos:**
- `SYMBOL` (obligatorio): FQN, nombre de clase, o `Clase#metodo`. Ejemplos: `OrderService`, `com.example.OrderService#placeOrder`.
- `PATH`: raíz del repo.

**Opciones:**
- `--depth N`: profundidad de llamantes indirectos (1–8, por defecto 4).
- `--type` / `-t`: tipo de consulta, `impact` (por defecto) o `events`.
- `--output` / `-o`, `--format` / `-f`, `--copy` / `-c`.

---

### 5.5 `pr-impact` — "¿Qué puede romper este PR?"

```bash
sourcecode pr-impact --files changed_files.txt
sourcecode pr-impact /ruta/a/repo --files diff.txt --format json
sourcecode pr-impact --files changes.txt --output pr_report.txt
```

**Qué hace:** lee una **lista de ficheros Java cambiados** y produce un informe consolidado del radio de impacto del Pull Request: clases modificadas, endpoints REST afectados a través de la cadena de llamadas, llamantes directos de cada clase modificada, publicadores y consumidores de eventos disparados por el cambio, métodos `@Transactional` tocados, y un nivel de riesgo consolidado (CRITICAL / HIGH / MEDIUM / LOW).

**En lo que es bueno:** revisar un PR antes de aprobarlo. Reutiliza el grafo y el análisis de impacto existentes (no parsea de nuevo). Solo Java/Spring.

**Cómo preparar la lista de ficheros:** suele generarse desde git, por ejemplo:
```bash
git diff --name-only origin/main > changed_files.txt
sourcecode pr-impact --files changed_files.txt
```

**Opciones:**
- `--files PATH` (**obligatorio**): fichero con la lista de ficheros cambiados, uno por línea.
- `--format` / `-f`: `text` (por defecto) o `json`.
- `--output` / `-o`, `--copy` / `-c`.

---

### 5.6 `review-pr` — revisión de PR basada en diff

```bash
sourcecode review-pr --since origin/main
sourcecode review-pr . --since main --format github-comment
sourcecode review-pr . --since HEAD~3 --output review.json
```

**Qué hace:** calcula un informe de impacto del cambio a partir del **diff de git**: símbolos cambiados, propagación por el grafo de llamadas, endpoints afectados, fronteras transaccionales, superficie de seguridad y un ranking de riesgo por fichero.

**Diferencia con `pr-impact`:** `review-pr` parte de un **diff de git** (`--since` o un diff presente), mientras que `pr-impact` parte de una **lista de ficheros** que tú le das. Usa `review-pr` cuando trabajas con ramas de git; `pr-impact` cuando ya tienes la lista de cambios.

**En lo que es bueno:** generar un comentario de revisión para pegar en GitHub (`--format github-comment`).

**Opciones:**
- `--since REF`: referencia base de git para el diff (`origin/main`, `HEAD~3`...). Requerido si no hay un diff presente.
- `--format`: `json` (por defecto) o `github-comment`.
- `--output` / `-o`, `--copy` / `-c`.

> Lleva la etiqueta `[Pro*]`, reservada para una futura licencia. Hoy funciona sin autenticación.

---

### 5.7 `fix-bug` — "¿Dónde miro para arreglar este síntoma?"

```bash
sourcecode fix-bug --symptom "NullPointerException in UserService"
sourcecode fix-bug . --symptom "401 on /api/orders"
sourcecode fix-bug . --output bug-context.json
```

**Qué hace:** muestra los ficheros/clases **más probablemente relacionados con un bug**. Los ordena por riesgo, señales de anotaciones (`@Transactional`, anotaciones de seguridad) y acoplamiento estructural. La salida está acotada y lista para una IA.

**En lo que es bueno:** acortar la búsqueda inicial cuando solo tienes un mensaje de error o un síntoma. En vez de leer todo OpenMRS, te apunta a las 5–10 clases candidatas.

**Opciones:**
- `--symptom` / `-s`: pista de palabra clave del bug (sube los ficheros que coinciden y muestra anotaciones relacionadas).
- `--output` / `-o`, `--copy` / `-c`.

> Etiqueta `[Pro*]` (hoy sin autenticación).

---

### 5.8 `modernize` — "¿Por dónde refactorizo primero?"

```bash
sourcecode modernize .
sourcecode modernize /ruta/a/repo --output modernize.json
```

**Qué hace:** analiza el repo buscando candidatos a refactor:
- Módulos con **alto acoplamiento** (muchas conexiones de entrada y salida).
- **Zonas muertas** (símbolos aislados sin llamantes).
- **Hotspots de riesgo** (alto fan-in + anotaciones de seguridad + fronteras transaccionales).
- Marañas de dependencias entre módulos.
- Resumen de subsistemas con conteo de miembros.

**En lo que es bueno:** planificar deuda técnica con datos, no por intuición. Te dice qué es peligroso tocar y qué está seguro.

**Opciones:**
- `--output` / `-o`, `--copy` / `-c`.

> Etiqueta `[Pro*]` (hoy sin autenticación).

---

### 5.9 `explain` — resumen arquitectónico de una clase

```bash
sourcecode explain UserService
sourcecode explain OrderController /ruta/a/repo
sourcecode explain UserService --format json
```

**Qué hace:** genera una **explicación legible** de una clase, derivada por completo del análisis estático: propósito y estereotipo Spring, métodos públicos, quién la llama (llamantes entrantes), qué llama ella (dependencias salientes), eventos publicados y consumidos, fronteras `@Transactional`, restricciones de seguridad (`@PreAuthorize`, `@Secured`...) y endpoints REST relacionados.

**En lo que es bueno:** entender una clase concreta sin abrir el fichero. Por ejemplo `explain OwnerController` en Spring PetClinic te resume su papel en segundos. Solo Java/Spring.

**Argumentos:**
- `CLASS_NAME` (obligatorio): nombre simple de la clase (por ejemplo `UserService`, `OrderController`).
- `PATH`: raíz del repo.

**Opciones:**
- `--format` / `-f`: `text` (por defecto) o `json`.
- `--output` / `-o`, `--copy` / `-c`.

---

### 5.10 `endpoints` — superficie de la API REST

```bash
sourcecode endpoints .
sourcecode endpoints . --output endpoints.json
sourcecode endpoints . --path-prefix /v1/liquidacion
sourcecode endpoints . --controller LiquidacionJornada
sourcecode endpoints . --limit 10
sourcecode endpoints . --by-controller
```

**Qué hace:** extrae **todos los endpoints REST** del código Java. Reconoce anotaciones de Spring MVC (`@GetMapping`, `@PostMapping`, `@PutMapping`, `@DeleteMapping`, `@PatchMapping`, `@RequestMapping`) y de JAX-RS (`@GET`, `@POST`, `@PUT`, `@DELETE`, `@PATCH` con `@Path`). De cada endpoint saca el método HTTP, la ruta, la clase controladora y el método que lo maneja.

**En lo que es bueno:** ver el "contrato" de la API de un vistazo. En Spring PetClinic te lista `GET /owners`, `POST /owners/new`, etc.

**Anotaciones de seguridad personalizadas:** si tu proyecto usa anotaciones de autorización propias, puedes "enseñárselas" creando un `sourcecode.config.json` en la raíz del repo:
```json
{
  "customSecurityAnnotations": [
    {"shortName": "M3FiltroSeguridad",
     "resourceParam": "nombreRecurso", "levelParam": "nivelRequerido"}
  ]
}
```
Los endpoints que coincidan se reportan con política `custom` (con la anotación, el recurso y el nivel requerido). Sin esta configuración, se reportan como `none_detected`.

**Opciones:**
- `--path-prefix` / `-p`: filtra los endpoints cuya ruta empieza por ese prefijo. Ejemplo: `/v1/liquidacion`.
- `--controller`: filtra por clase controladora (coincidencia por substring). Ejemplo: `LiquidacionJornada`.
- `--limit` / `-n`: número máximo de endpoints a devolver.
- `--by-controller`: agrupa los endpoints por clase controladora (superficie de API estructurada, útil para sintetizar diagramas C4/Container).
- `--format` / `-f`, `--output` / `-o`, `--copy` / `-c`.

---

### 5.11 `validation` — validación de los cuerpos de petición

```bash
sourcecode validation .
sourcecode validation . --gaps-only
sourcecode validation . --path-prefix /owners
sourcecode validation . --format yaml
```

**Qué hace:** mapea, **por cada endpoint, qué validación debe cumplir el cuerpo de la petición**. Junta dos fuentes de verdad:
1. **Restricciones declarativas** sobre los DTOs (`@Pattern`, `@Size`, `@NotNull`, min/max, enum), recuperadas incluso desde la especificación OpenAPI cuando los DTOs están autogenerados (en `target/generated-sources`, que no se escanean).
2. **Validadores personalizados** escritos a mano (`@Constraint` + `ConstraintValidator`, por ejemplo un `PetAgeValidator`), enlazados a los campos.

La salida (JSON) incluye, por endpoint, los campos validados con sus reglas y validadores, el catálogo de validadores personalizados encontrados, y el conjunto de endpoints **sin validación declarada** (los "gaps").

**En lo que es bueno:** saber exactamente qué espera una API antes de tocarla, y detectar endpoints que aceptan datos sin validar. En Spring PetClinic recupera la validación de edad de las mascotas desde `PetAgeValidator`.

**Opciones:**
- `--gaps-only`: reporta **solo** los endpoints/campos sin validación declarada (la sección de huecos).
- `--path-prefix` / `-p`: filtra por prefijo de ruta.
- `--format` / `-f`, `--output` / `-o`, `--copy` / `-c`.

---

### 5.12 `spring-audit` — auditoría semántica de Spring

```bash
sourcecode spring-audit .
sourcecode spring-audit . --scope security
sourcecode spring-audit . --min-severity high
sourcecode spring-audit . --ci --min-severity high
sourcecode spring-audit . --ci --format github-comment
```

**Qué hace:** audita el proyecto buscando **anomalías de transacciones y de seguridad** que un compilador no detecta pero que en producción causan bugs sutiles:

| Código | Qué detecta |
|--------|-------------|
| `TX-001` | `@Transactional` en método privado/final (el proxy CGLIB lo ignora → la transacción **no se aplica**). |
| `TX-002` | `REQUIRES_NEW` anidado dentro de una cadena `REQUIRED` (anidamiento inesperado de transacciones). |
| `TX-003` | Frontera `readOnly=true` que se propaga a una operación de escritura. |
| `TX-004` | `NOT_SUPPORTED`/`NEVER` dentro de una cadena transaccional activa. |
| `TX-005` | Excepción "tragada" (swallowing) dentro de un `@Transactional`. |
| `SEC-001` | Endpoint sin proteger en un modelo de seguridad basado en anotaciones. |
| `SEC-002` | CVE-2025-41248: `@PreAuthorize` en un método heredado de un supertipo genérico. |
| `SEC-003` | `@Transactional` en un `@Controller`/`@RestController` (transacción en la capa equivocada). |

**En lo que es bueno:** encontrar bugs de concurrencia y seguridad que solo aparecen en producción. Pensado además para **integrarse en CI/CD** y bloquear merges peligrosos.

**Uso en CI/CD:** con `--ci`, el comando termina con **código de salida 1** si hay hallazgos, lo que hace fallar el pipeline:
```bash
sourcecode spring-audit . --ci                          # falla con cualquier hallazgo
sourcecode spring-audit . --ci --min-severity high      # falla solo con high/critical
sourcecode spring-audit . --ci --format github-comment  # comentario Markdown + falla
```

**Opciones:**
- `--scope` / `-s`: alcance de la auditoría: `all` (por defecto), `tx` (solo transacciones) o `security` (solo seguridad).
- `--min-severity`: severidad mínima a incluir: `critical`, `high`, `medium` o `low` (por defecto).
- `--ci` / `--no-ci`: si está activo, sale con código 1 cuando hay hallazgos al nivel `--min-severity` o superior. Para puertas de CI/CD.
- `--format` / `-f`: `json` (por defecto), `yaml` o `github-comment`.
- `--output` / `-o`, `--copy` / `-c`.

---

### 5.13 `migrate-check` — preparación para Spring Boot 2 → 3

```bash
sourcecode migrate-check .
sourcecode migrate-check /ruta/a/repo --format text
sourcecode migrate-check . --min-severity high
sourcecode migrate-check . --output migration.json
```

**Qué hace:** comprueba si un proyecto está **listo para migrar de Spring Boot 2 a 3**, donde el mayor cambio es el paso de namespace `javax.*` → `jakarta.*`. Detecta los bloqueantes:

| Código | Severidad | Qué detecta |
|--------|-----------|-------------|
| `MIG-001` | CRÍTICO | `import javax.persistence` (JPA no compilará). |
| `MIG-002` | ALTO | `import javax.servlet` (la API Servlet cambió). |
| `MIG-003` | ALTO | `import javax.validation` (Bean Validation cambió). |
| `MIG-004` | ALTO | `import javax.transaction`. |
| `MIG-005` | ALTO | `extends WebSecurityConfigurerAdapter` (eliminada en Spring 6). |
| `MIG-006` | MEDIO | `import javax.annotation`. |
| `MIG-007` | MEDIO | `import javax.inject`. |
| `MIG-008` | MEDIO | `import javax.ws.rs` (JAX-RS cambió). |

**Estratificación Hibernate 5 → 6** (en la sección `hibernate` de la salida): separa la migración en 4 capas independientes (anotaciones JPA / Criteria / HQL / SPI), con una matriz de riesgo por capa y rangos de esfuerzo, un mapa de exposición por módulo, detección de cadenas de llamadas críticas, hotspots de "golden SQL", una puntuación `hibernate_readiness`, y un veredicto **UPGRADE vs REWRITE**. Emite además `rewrite_targets[]` (rangos de líneas en los puntos de llamada → API destino + tipo de migración) para que un agente de migración consuma la salida directamente.

**En lo que es bueno:** estimar el coste real de migrar a Spring Boot 3 antes de empezar, y darle a un agente de IA una lista accionable de qué reescribir.

**Opciones:**
- `--min-severity`: severidad mínima: `critical`, `high`, `medium` o `low` (por defecto).
- `--format` / `-f`: `json` (por defecto) o `text`.
- `--output` / `-o`, `--copy` / `-c`.

---

### 5.14 `export` — vistas estructuradas para otras herramientas

```bash
sourcecode export . --c4
sourcecode export . --by-directory
sourcecode export . --module-graph
sourcecode export . --integrations
sourcecode export . --by-directory --integrations   # se combinan
```

**Qué hace:** exporta **vistas del código en JSON/YAML neutro** que cualquier consumidor puede ingerir: generadores de documentación de arquitectura, renderizadores de diagramas, agentes de búsqueda de código. Las etiquetas se corresponden con el modelo abierto **C4** (notación de arquitectura, no un producto), pero el esquema es independiente de proveedor.

**Las secciones se combinan:** puedes pasar varias para emitir varias secciones en un mismo documento.

**Opciones (secciones):**
- `--by-directory`: un grupo por directorio fuente, con cada símbolo llevando una referencia `ruta:línea`. Es el "mapa de código anclado a fichero:línea" que permite a un consumidor ir directo al fichero en vez de leer directorio por directorio.
- `--module-graph`: grafo de dependencias módulo → módulo (nivel container/component de C4), agregado desde las relaciones a nivel de clase.
- `--integrations`: integraciones salientes (RestTemplate, WebClient, Feign, LDAP, JMS) con evidencia `fichero:línea`. Son las "flechas" hacia sistemas externos.
- `--c4`: documento de arquitectura unificado mapeado al modelo C4 completo (context / containers / components / code) + una superficie de API + un manifiesto de hash de contenido por directorio para consumidores incrementales. Ensambla la exportación completa por sí solo.
- `--format` / `-f`, `--output` / `-o`, `--copy` / `-c`.

---

### 5.15 `repo-ir` — representación de símbolos a bajo nivel

```bash
sourcecode repo-ir
sourcecode repo-ir /ruta/a/repo --since HEAD~1
sourcecode repo-ir --files src/main/java/UserService.java
sourcecode repo-ir --since main --output ir.json
sourcecode repo-ir --max-nodes 200 --max-edges 500
sourcecode repo-ir --output ir.json.gz --gzip
```

**Qué hace:** extrae la **IR determinista a nivel de símbolos** de un repositorio Java: símbolos, relaciones, roles de Spring y (con `--since`) diffs a nivel de símbolo. La salida JSON/YAML contiene `graph{nodes,edges}`, `analysis`, `impact`, `subsystems` y `change_set`.

**En lo que es bueno:** es la materia prima de bajo nivel para alimentar otras herramientas o tu propio tooling. La mayoría de usuarios no la necesita a diario; comandos como `impact` o `explain` ya consumen esta IR por debajo.

**Argumentos:**
- `PATH`: raíz del repo (por defecto `.`).

**Opciones:**
- `--since REF`: diff a nivel de símbolo respecto a una referencia de git (`HEAD~1`, `main`...).
- `--files`: lista de ficheros Java (separados por comas, relativos a `PATH`) a analizar.
- `--include-tests`: incluye los ficheros de test (excluidos por defecto).
- **Control de tamaño** (la salida puede ser enorme):
  - `--summary-only`: omite el grafo completo; mantiene el resumen `analysis`, `impact` y `change_set` (la salida más pequeña, normalmente <300 KB).
  - `--max-nodes N`: conserva solo los N nodos con mayor puntuación de impacto.
  - `--max-edges N`: conserva solo N aristas (prioriza las que están entre nodos conservados).
  - `--gzip`: comprime el fichero de salida (~70–80% más pequeño; **requiere `--output`**).
  - `--force`: emite la salida aunque los tokens estimados superen el límite de 50K (salta el guardarraíl de tamaño).
- `--format` / `-f`, `--output` / `-o`, `--copy` / `-c`.

---

## 6. Herramientas de refactor

### 6.1 `rename-class` — renombrar una clase Java en todo el repo

```bash
sourcecode rename-class . --from ServiceA --to ServiceB
sourcecode rename-class . --from OldName --to NewName --dry-run
sourcecode rename-class . --from OldName --to NewName --output rename-audit.json
```

**Qué hace:** renombra una clase Java de forma **segura y completa** por todo el repositorio. Actualiza: la declaración de clase/interfaz/enum, el nombre del constructor, todos los `import`, todas las referencias de tipo (campos, parámetros, retornos), `extends`/`implements`, genéricos, casts y nombres de `@Qualifier` de Spring. Renombra también el **fichero `.java`** físico y emite un registro de auditoría de cambios.

**En lo que es bueno:** un rename masivo fiable sin un IDE, o como paso automatizable en un script. Solo Java.

**Recomendación:** usa **siempre `--dry-run` primero** para revisar qué cambiaría antes de escribir nada.

**Opciones:**
- `--from` / `-f` (**obligatorio**): nombre actual de la clase (PascalCase, p. ej. `ServiceA`).
- `--to` / `-t` (**obligatorio**): nombre nuevo (PascalCase, p. ej. `ServiceB`).
- `--dry-run`: calcula los cambios pero **no escribe ni renombra** nada en disco.
- `--no-tests`: excluye los ficheros de test del rename (solo `src/main`).
- `--format`: `json` (por defecto) o `yaml`.
- `--output` / `-o`, `--copy` / `-c`.

---

### 6.2 `chunk-file` — partir un fichero Java grande en trozos

```bash
sourcecode chunk-file NominasCalculoService.java
sourcecode chunk-file BigService.java --max-lines 300
sourcecode chunk-file BigService.java --chunk 5
sourcecode chunk-file BigService.java --metadata-only
```

**Qué hace:** parte un fichero Java en **trozos semánticos** respetando las fronteras de método y clase, para que un agente de IA pueda leer ficheros enormes (10.000–25.000+ líneas) por partes, sin timeouts ni análisis fragmentado. Cada trozo incluye su `chunk_id`, líneas de inicio/fin, tipo, nombre del símbolo, una cabecera de contexto (paquete + clase + resumen de imports), el contenido, y un `size_warning` si el trozo supera el máximo (porque no se puede partir a mitad de un método).

**En lo que es bueno:** trabajar con esas clases gigantes de tipo "God object" que un agente no puede tragar de una vez.

**Argumentos:**
- `FILE` (obligatorio): el fichero Java a partir (ruta absoluta o relativa).

**Opciones:**
- `--max-lines` / `-n`: máximo de líneas por trozo (por defecto 500). Los métodos más largos que el máximo emiten `size_warning`.
- `--chunk` / `-c`: devuelve **solo** ese trozo por su ID (empezando en 1). Sin esta opción devuelve todos.
- `--metadata-only`: devuelve solo las fronteras y metadatos de los trozos, sin el contenido (para ver tamaños y límites primero).
- `--format`, `--output` / `-o`, `--copy`.

---

## 7. Velocidad y caché

### 7.1 `cold-start` — arranque instantáneo desde la foto guardada

```bash
sourcecode cold-start .
sourcecode cold-start . --compact
sourcecode cold-start . --output snapshot.json
```

**Qué hace:** devuelve al instante el contexto de arranque desde el **RIS** ya persistido, sin coste de re-análisis. El campo `status` indica el estado: `cold_start_ready` (listo), `cold_start_stale` (la foto está desactualizada) o `no_ris` (no hay foto).

**En lo que es bueno:** arrancar una sesión de agente en milisegundos cuando ya analizaste el repo antes.

**Aviso de tamaño:** la salida completa es grande (~100K–200K tokens en repos medianos).
- `--compact`: emite un subconjunto compacto (~10K tokens): status, `git_head`, stacks, puntos de entrada y dependencias clave. **Seguro para inyectar directamente en una IA.**
- `--output` / `-o`: guarda la foto completa en fichero para herramientas de búsqueda local.

---

### 7.2 `cache` — inspección y gestión de la caché

```bash
sourcecode cache status      # estadísticas de la caché del repo
sourcecode cache warm        # pre-construye la caché antes de una sesión
sourcecode cache clear       # borra las fotos cacheadas del repo
sourcecode cache freshness   # frescura del RIS respecto al HEAD de git actual
```

| Subcomando | Qué hace |
|-----------|----------|
| `status` | Tamaño de la caché, claves de acierto y última vez que se "calentó". |
| `warm` | Pre-puebla la caché ejecutando un análisis fresco. Útil **antes** de una sesión de agente para que todo responda al instante. |
| `clear` | Borra las fotos cacheadas del repositorio. |
| `freshness` | Informa de cuán fresco está el RIS respecto al commit (HEAD) actual de git. |

---

## 8. Integración con agentes de IA (MCP)

**MCP** (*Model Context Protocol*) es el estándar que permite a clientes como **Claude Desktop** o **Cursor** llamar a herramientas externas. `sourcecode` puede exponerse como un servidor MCP para que el agente consulte el código por sí mismo.

```bash
sourcecode mcp init        # configura la integración en Claude Desktop, Cursor, etc.
sourcecode mcp status      # estado: dependencias, ficheros de config, conectividad
sourcecode mcp list-tools  # lista las herramientas MCP que expone el servidor
sourcecode mcp serve       # arranca el servidor MCP por stdio
sourcecode mcp remove      # quita la integración de todos los clientes configurados
```

| Subcomando | Qué hace |
|-----------|----------|
| `init` | Configura la integración MCP para Claude Desktop, Cursor y otros clientes. Es lo primero que ejecutas. |
| `status` | Muestra el estado de la integración: dependencias, ficheros de configuración y conectividad. |
| `list-tools` | Lista todas las herramientas MCP que expone el servidor de sourcecode. |
| `serve` | Arranca el servidor MCP sobre stdio (lo invoca normalmente el cliente, no tú a mano). |
| `remove` | Elimina la integración de sourcecode de todos los clientes configurados, de forma segura. |

**Flujo típico:** `sourcecode mcp init` → reinicias Claude Desktop / Cursor → el agente ya puede preguntarle a sourcecode sobre tu código.

---

## 9. Cuenta, licencia y configuración

### `activate` — activar una licencia Pro

```bash
sourcecode activate SC-XXXX-XXXX-XXXX
```

Valida la clave contra el servidor de licencias y la guarda en `~/.sourcecode/license.json`.
- `LICENSE_KEY` (obligatorio): tu clave Pro.

### `auth` — autenticación

```bash
sourcecode auth status   # muestra el plan actual y el estado de autenticación
sourcecode auth logout   # elimina las credenciales locales
```
`logout` solo borra las credenciales locales; **no cancela tu suscripción**.

### `config` — ver la configuración actual

```bash
sourcecode config
```
Muestra la configuración efectiva actual de la herramienta.

### `version` — versión

```bash
sourcecode version
```
Muestra la versión y sale (equivalente a `sourcecode --version`).

### `telemetry` — telemetría anónima

```bash
sourcecode telemetry status    # ver el ajuste actual
sourcecode telemetry enable    # activar
sourcecode telemetry disable   # desactivar (opt-out)
```
La telemetría anónima está **activada por defecto**; puedes desactivarla cuando quieras. No incluye tu código.

---

## 10. Flujos de trabajo recomendados

**A) Llego a un repo que no conozco (p. ej. Broadleaf Commerce)**
```bash
sourcecode onboard . --llm-prompt        # visión general + prompt para la IA
sourcecode endpoints . --by-controller   # qué API expone
sourcecode modernize .                   # dónde está la deuda técnica
```

**B) Voy a cambiar una clase y no quiero romper nada**
```bash
sourcecode explain PaymentService        # qué hace y quién la usa
sourcecode impact-chain PaymentService . --depth 6   # radio de impacto Spring
```

**C) Estoy revisando un Pull Request**
```bash
sourcecode review-pr . --since origin/main --format github-comment
# o, si tengo la lista de ficheros:
git diff --name-only origin/main > changed.txt
sourcecode pr-impact --files changed.txt
```

**D) Voy a migrar a Spring Boot 3**
```bash
sourcecode migrate-check . --output migration.json
```

**E) Quiero blindar la calidad en CI/CD**
```bash
sourcecode spring-audit . --ci --min-severity high --format github-comment
```

**F) Preparo una sesión de agente de IA sobre el repo**
```bash
sourcecode cache warm                    # caliento la caché
sourcecode mcp init                      # conecto Claude Desktop / Cursor
# o, sin MCP, dándole contexto directo:
sourcecode --compact --copy
```

---

## 11. Tabla de referencia rápida

| Pregunta | Comando |
|----------|---------|
| ¿Qué es este repo? | `sourcecode onboard .` |
| Resumen breve para una IA | `sourcecode --compact` |
| ¿Qué se rompe si toco X? | `sourcecode impact X` / `sourcecode impact-chain X .` |
| ¿Qué hace la clase X? | `sourcecode explain X` |
| ¿Qué API expone? | `sourcecode endpoints .` |
| ¿Qué valida cada endpoint? | `sourcecode validation .` |
| ¿Bugs de transacción/seguridad? | `sourcecode spring-audit .` |
| ¿Listo para Spring Boot 3? | `sourcecode migrate-check .` |
| ¿Qué rompe este PR? | `sourcecode review-pr . --since origin/main` |
| ¿Dónde está este bug? | `sourcecode fix-bug --symptom "..."` |
| ¿Dónde refactorizo? | `sourcecode modernize .` |
| Renombrar una clase | `sourcecode rename-class . --from A --to B --dry-run` |
| Partir un fichero enorme | `sourcecode chunk-file Big.java` |
| Exportar arquitectura (C4) | `sourcecode export . --c4` |
| Conectar con Claude/Cursor | `sourcecode mcp init` |

---

## 12. Consejos finales

- **Empieza siempre por `--compact` o `onboard`** antes de pedir análisis pesados.
- Usa **`--dry-run`** en `rename-class` (y en `prepare-context`) antes de ejecutar de verdad.
- Para repos grandes, usa **`--output fichero.json`** en vez de volcar todo por pantalla, y combínalo con **`--gzip`** donde esté disponible (`repo-ir`).
- Las funciones marcadas como **solo Java/Spring** (`impact-chain`, `explain`, `pr-impact`, auditoría, migración) no darán resultado útil en proyectos de otros lenguajes.
- Si la salida parece desactualizada, añade **`--no-cache`** o ejecuta `sourcecode cache clear`.
- Para CI/CD, recuerda **`--ci`** en `spring-audit`: es lo que hace fallar el pipeline ante un hallazgo.
```

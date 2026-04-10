<!-- generated-by: gsd-doc-writer -->
# Schema de salida

`sourcecode` serializa un objeto `SourceMap` con schema `1.0`.

Los campos **siempre presentes** son: `metadata`, `file_tree`, `file_paths`, `stacks`, `project_type`, `entry_points` y `project_summary`.

Los campos **opcionales** dependen de los flags activos en la invocacion:

| Campo | Requiere |
|---|---|
| `dependencies` | `--dependencies` |
| `dependency_summary` | `--dependencies` |
| `key_dependencies` | `--dependencies` |
| `module_graph` | `--graph-modules` |
| `module_graph_summary` | `--graph-modules` |
| `docs` | `--docs` |
| `doc_summary` | `--docs` |

## Raiz

```json
{
  "metadata": {},
  "file_tree": {},
  "file_paths": [],
  "stacks": [],
  "project_type": "webapp",
  "entry_points": [],
  "project_summary": "Aplicacion web en Nodejs (Next.js). Entry points: app/page.tsx.",
  "dependencies": [],
  "dependency_summary": null,
  "key_dependencies": [],
  "module_graph": null,
  "module_graph_summary": null,
  "docs": [],
  "doc_summary": null
}
```

Campos:

- `metadata`: metadatos del analisis. Siempre presente.
- `file_tree`: arbol del repositorio. Siempre presente.
- `file_paths`: lista plana de todos los paths del repo con separador forward-slash. Siempre presente (Phase 9).
- `stacks`: stacks detectados. Siempre presente (puede ser lista vacia).
- `project_type`: clasificacion general del proyecto. Siempre presente (puede ser `null`).
- `entry_points`: puntos de entrada relevantes. Siempre presente (puede ser lista vacia).
- `project_summary`: descripcion en lenguaje natural del proyecto generada deterministicamente. Presente cuando hay stacks detectados; `null` si no (Phase 9).
- `dependencies`: dependencias detectadas. Solo presente con `--dependencies`; lista vacia por defecto.
- `dependency_summary`: resumen del analisis de dependencias. Solo presente con `--dependencies`; `null` por defecto.
- `key_dependencies`: top-15 dependencias directas relevantes. Solo presente con `--dependencies`; lista vacia por defecto (Phase 9).
- `module_graph`: grafo estructural de codigo. `null` si no se solicito `--graph-modules`.
- `module_graph_summary`: resumen compacto del grafo para consumo rapido por LLMs. `null` si no se solicito `--graph-modules`.
- `docs`: registros de documentacion extraida por simbolo. Solo presente con `--docs`; lista vacia por defecto.
- `doc_summary`: resumen del analisis de documentacion. Solo presente con `--docs`; `null` por defecto.

## metadata

```json
{
  "schema_version": "1.0",
  "generated_at": "2026-04-07T19:41:05.686277+00:00",
  "sourcecode_version": "0.7.0",
  "analyzed_path": "/abs/path/to/project"
}
```

- `schema_version`: version del contrato de salida.
- `generated_at`: timestamp UTC ISO 8601.
- `sourcecode_version`: version de la herramienta que genero el output.
- `analyzed_path`: ruta absoluta analizada.

## file_tree

`file_tree` usa esta convencion:

- `null`: fichero.
- objeto JSON: directorio.

Ejemplo:

```json
{
  "pyproject.toml": null,
  "src": {
    "main.py": null
  }
}
```

## file_paths

`file_paths` es una lista plana de todos los paths del repositorio, derivada de `file_tree` en el momento de la construccion del `SourceMap`. Siempre presente.

```json
[
  "pyproject.toml",
  "src/main.py",
  "src/utils/helpers.py"
]
```

- Los separadores son siempre forward-slash (`/`), independientemente del sistema operativo.
- Los paths son relativos a la raiz analizada.
- La lista respeta la profundidad de escaneo aplicada por `FileScanner` (por defecto `--depth 4`).
- `file_tree` se conserva intacto para compatibilidad retroactiva; `file_paths` es un campo adicional.

Vease tambien: `file_tree_depth1` en la seccion [Modo compacto](#modo-compacto).

## project_summary

`project_summary` es una descripcion en lenguaje natural del proyecto, generada deterministicamente a partir de los campos del `SourceMap` sin llamadas a API. Presente cuando hay stacks detectados; `null` si no.

```json
"Aplicacion web en Nodejs (Next.js, React). Entry points: app/page.tsx. 42 dependencias (nodejs)."
```

### Plantillas de generacion

El valor se construye segun las siguientes plantillas:

**Proyecto con stacks:**
```
{type_label} en {stack_primario} ({frameworks}). Entry points: {paths}. {N} dependencias ({ecosystems}).
```

**Monorepo:**
```
Monorepo con {N} workspaces en {stacks}.
```

**Sin stacks detectados:**
```
Proyecto sin stack detectado.
```

### Reglas de construccion

- `type_label` se obtiene de la siguiente tabla:

  | `project_type` | `type_label` |
  |---|---|
  | `webapp` | `Aplicacion web` |
  | `api` | `API` |
  | `library` | `Libreria` |
  | `cli` | `CLI` |
  | `monorepo` | `Monorepo` |
  | `fullstack` | `Proyecto fullstack` |
  | `unknown` | `Proyecto` |

- Se listan como maximo 3 entry points y 3 frameworks.
- La parte de dependencias solo aparece cuando `dependency_summary` esta disponible y tiene conteo mayor que cero. Si `dependency_summary` es `None`, se omite la parte de dependencias.
- Para monorepos se usa la variante especial: se cuenta el numero de workspaces distintos (`s.workspace`) entre todos los stacks.
- La generacion nunca lanza excepcion; en caso de error interno devuelve `"Proyecto analizado."`.

## stacks

Cada elemento de `stacks` sigue este shape:

```json
{
  "stack": "nodejs",
  "detection_method": "manifest",
  "confidence": "high",
  "frameworks": [
    {
      "name": "Next.js",
      "source": "package.json"
    }
  ],
  "package_manager": "pnpm",
  "manifests": ["package.json"],
  "primary": true,
  "root": ".",
  "workspace": null,
  "signals": [
    "manifest:package.json",
    "framework:Next.js",
    "entry:app/page.tsx"
  ]
}
```

Campos:

- `stack`: ecosistema detectado, por ejemplo `nodejs`, `python` o `go`.
- `detection_method`: `manifest`, `lockfile` o `heuristic`.
- `confidence`: `high`, `medium` o `low`.
- `frameworks`: frameworks asociados al stack.
- `package_manager`: package manager inferido si aplica.
- `manifests`: manifests que dispararon la deteccion.
- `primary`: indica el stack principal del proyecto o workspace.
- `root`: raiz relativa del stack dentro del repo.
- `workspace`: workspace relativa si el proyecto fue analizado como monorepo.
- `signals`: senales que justifican la deteccion y la clasificacion.

## frameworks

Cada framework detectado tiene:

```json
{
  "name": "FastAPI",
  "source": "manifest"
}
```

- `name`: nombre normalizado del framework.
- `source`: origen de la senal, por ejemplo `manifest` o `package.json`.

## project_type

Valores actuales:

- `webapp`
- `api`
- `library`
- `cli`
- `fullstack`
- `monorepo`
- `unknown`

`project_type` se calcula a partir de stacks, entry points y senales agregadas. Puede ser `null` si la clasificacion no pudo completarse.

## entry_points

Cada elemento de `entry_points` sigue este shape:

```json
{
  "path": "app/page.tsx",
  "stack": "nodejs",
  "kind": "web",
  "source": "package.json"
}
```

Campos:

- `path`: ruta relativa al repo analizado.
- `stack`: stack al que pertenece.
- `kind`: tipo de entry point, por ejemplo `web`, `api`, `cli` o `entry`.
- `source`: origen de la deteccion.

## dependencies

Solo presente con `--dependencies`. Cada elemento sigue este shape:

```json
{
  "name": "fastapi",
  "ecosystem": "python",
  "scope": "direct",
  "declared_version": ">=0.115",
  "resolved_version": "0.115.2",
  "source": "lockfile",
  "parent": null,
  "manifest_path": "poetry.lock",
  "workspace": "packages/api"
}
```

Campos:

- `name`: nombre normalizado de la dependencia.
- `ecosystem`: stack o ecosistema al que pertenece, por ejemplo `python`, `nodejs` o `php`.
- `scope`: alcance de la dependencia, por ejemplo `direct`, `dev`, `peer`, `optional` o `transitive`.
- `declared_version`: version o constraint declarada en el manifest.
- `resolved_version`: version exacta resuelta desde un lockfile si esta disponible.
- `source`: origen principal del dato, normalmente `manifest` o `lockfile`.
- `parent`: dependencia padre cuando la relacion transitiva puede resolverse offline.
- `manifest_path`: fichero del que procede la evidencia principal.
- `workspace`: workspace relativa dentro del repo cuando aplica.

## dependency_summary

Solo presente con `--dependencies`. Describe el alcance del analisis:

```json
{
  "requested": true,
  "total_count": 2,
  "direct_count": 1,
  "transitive_count": 1,
  "ecosystems": ["python"],
  "sources": ["lockfile"],
  "limitations": []
}
```

Campos:

- `requested`: `true` si el usuario activo `--dependencies`.
- `total_count`: numero total de registros de dependencia emitidos.
- `direct_count`: numero de dependencias no transitivas.
- `transitive_count`: numero de dependencias transitivas detectadas.
- `ecosystems`: ecosistemas presentes en el bloque de dependencias.
- `sources`: origenes usados para construir la salida.
- `limitations`: limitaciones conocidas del analisis offline, por ejemplo ecosistemas donde no se pudo reconstruir un grafo transitivo fiable.

Vease tambien: `key_dependencies` para el subconjunto de dependencias directas mas relevantes.

## key_dependencies

Solo presente con `--dependencies`. Es un subconjunto de hasta 15 `DependencyRecord` seleccionados de `dependencies` segun los siguientes criterios (Phase 9):

**Criterios de filtrado:**
- `scope != "transitive"` — solo dependencias directas (`direct`, `dev`, `peer`, `optional`).
- `source in {"manifest", "lockfile"}` — excluye registros de tipo `tooling` u otros derivados.

**Ordenacion:**
1. Dependencias del ecosistema del stack primario primero.
2. Luego orden alfabetico por nombre.

**Limite:** maximo 15 registros.

**Default:** lista vacia `[]` cuando `--dependencies` no esta activo.

El shape de cada elemento es identico al de `dependencies`:

```json
{
  "name": "fastapi",
  "ecosystem": "python",
  "scope": "direct",
  "declared_version": ">=0.115",
  "resolved_version": "0.115.2",
  "source": "manifest",
  "parent": null,
  "manifest_path": "pyproject.toml",
  "workspace": null
}
```

`key_dependencies` es la vista prioritaria para consumidores que quieren identificar las dependencias principales sin procesar la lista completa potencialmente larga de `dependencies`.

## module_graph

Solo presente con `--graph-modules`. Expone nodos, aristas y un resumen del analisis:

```json
{
  "nodes": [
    {
      "id": "module:app",
      "kind": "module",
      "language": "python",
      "path": "app",
      "symbol": null,
      "display_name": "app",
      "workspace": null,
      "importance": "high"
    }
  ],
  "edges": [],
  "summary": {
    "requested": true,
    "node_count": 1,
    "edge_count": 0,
    "languages": ["python"],
    "methods": ["ast"],
    "main_flows": [],
    "layers": ["app"],
    "entry_points_count": 1,
    "truncated": false,
    "detail": "high",
    "max_nodes_applied": 80,
    "edge_kinds": ["imports"],
    "limitations": []
  }
}
```

### module_graph.nodes

Cada nodo describe un modulo o simbolo relevante:

- `id`: identificador unico del nodo.
- `kind`: tipo de nodo, por ejemplo `module`, `function` o `class`.
- `language`: lenguaje detectado para ese nodo.
- `path`: ruta relativa al repo analizado.
- `symbol`: simbolo asociado si aplica.
- `display_name`: nombre legible para consumidores humanos.
- `workspace`: workspace relativa cuando el nodo pertenece a un subproyecto.
- `importance`: importancia relativa del nodo (`high`, `medium`, `low`) usada para trimming y tambien expuesta al consumidor.

### module_graph.edges

Cada arista describe una relacion estructural:

- `source`: id del nodo origen.
- `target`: id del nodo destino.
- `kind`: tipo de relacion, por ejemplo `imports`, `contains`, `calls` o `extends`.
- `confidence`: `high`, `medium` o `low`.
- `method`: metodo usado para construir la arista: `ast`, `heuristic` o `unresolved`.

### module_graph.summary

Resume cobertura y limites del analisis:

- `requested`: `true` si el usuario activo `--graph-modules`.
- `node_count`: numero total de nodos emitidos.
- `edge_count`: numero total de aristas emitidas.
- `languages`: lenguajes presentes en el grafo.
- `methods`: metodos usados para construir las aristas.
- `main_flows`: rutas principales inferidas desde entry points siguiendo cadenas de imports/calls de forma best-effort.
- `layers`: capas o clusters inferidos desde la estructura de directorios.
- `entry_points_count`: numero de entry points considerados al construir el resumen.
- `truncated`: `true` si se aplico recorte por `--max-nodes`.
- `detail`: nivel activo del grafo (`high`, `medium`, `full`).
- `max_nodes_applied`: presupuesto de nodos aplicado en `high` o `medium`.
- `edge_kinds`: tipos de arista incluidos finalmente en la salida.
- `limitations`: parse errors, imports no resueltos, archivos omitidos o limites de presupuesto aplicados.

## module_graph_summary

`module_graph_summary` replica el resumen esencial del grafo en un bloque top-level para que consumidores con ventanas de contexto pequeñas no tengan que recorrer `module_graph` completo.

Campos principales:

- `requested`
- `node_count`
- `edge_count`
- `main_flows`
- `layers`
- `entry_points_count`
- `truncated`
- `limitations`

## docs

Solo presente con `--docs`. Es una lista de objetos `DocRecord`, uno por simbolo documentado extraido estaticamente del codigo fuente.

```json
[
  {
    "symbol": "create_user",
    "kind": "function",
    "language": "python",
    "path": "src/api/users.py",
    "doc_text": "Crea un nuevo usuario en la base de datos.",
    "signature": "def create_user(name: str, email: str) -> User",
    "source": "docstring",
    "importance": "medium",
    "workspace": null
  }
]
```

### DocRecord

Cada registro tiene los siguientes campos:

- `symbol` (`str`): nombre del simbolo documentado. Para registros de tipo `module`, el valor es el path relativo del fichero. Para funciones, clases y metodos es el nombre del simbolo (por ejemplo `"create_user"`, `"UserService"`).
- `kind` (`str`): tipo del simbolo. Valores: `"module"`, `"class"`, `"function"`, `"method"`.
- `language` (`str`): lenguaje del fichero fuente. Valores posibles: `"python"`, `"javascript"`, `"typescript"`.
- `path` (`str`): ruta relativa al repo analizado con separador forward-slash.
- `doc_text` (`str | null`): texto del docstring o bloque JSDoc extraido. `null` si no existe documentacion textual.
- `signature` (`str | null`): firma tipada reconstruida desde el AST. Solo se emite cuando hay al menos una anotacion de tipo en Python. `null` en JS/TS y cuando no hay anotaciones.
- `source` (`str`): origen de la documentacion. Valores: `"docstring"` (docstring Python o JSDoc), `"jsdoc"` (bloque JSDoc), `"comment"`, `"signature"` (solo firma, sin texto de doc).
- `importance` (`"high" | "medium" | "low"`): importancia inferida del simbolo. Valor por defecto: `"medium"`. Vease las reglas de inferencia abajo.
- `workspace` (`str | null`): ruta relativa del workspace cuando el proyecto es un monorepo. `null` en proyectos simples.

### DocRecord.importance — reglas de inferencia

La importancia se calcula en `DocAnalyzer._infer_importance()` en orden de prioridad:

1. **`high`**: el `path` del fichero coincide con alguno de los entry points del proyecto, O la profundidad del path es `<= 1` (fichero en raiz: `"main.py"`; o un nivel de directorio: `"src/main.py"`).
2. **`medium`**: la profundidad del path es exactamente `2` (por ejemplo `"src/core/base.py"`), O el `kind` es `"class"` o `"function"`.
3. **`low`**: todo lo demas — metodos y helpers en subdirectorios a profundidad `>= 3`.

La profundidad se calcula como el numero de `/` en el path relativo.

### Lenguajes soportados y limitaciones

El `DocAnalyzer` soporta extraccion activa para:
- **Python** (`.py`): usando `ast` — extrae docstrings de modulos, clases, funciones y metodos; reconstruye firmas tipadas.
- **JavaScript/TypeScript** (`.js`, `.jsx`, `.ts`, `.tsx`, `.mjs`, `.cjs`): usando regex — extrae bloques JSDoc en `depth="symbols"` o `"full"`.

Para lenguajes no soportados (Go, Java, Rust, etc.), **no se emite ningun `DocRecord`**. En su lugar se registra una entrada en `doc_summary.limitations` con el formato `"docs_unavailable:{path}:language={lang}"`. Esto evita emitir registros vacios que aportan ruido sin informacion util.

### Niveles de profundidad (`--docs-depth`)

- `"module"`: solo docstrings de nivel modulo (primer docstring de cada fichero).
- `"symbols"` (por defecto): modulos + clases + funciones a nivel top-level.
- `"full"`: todo lo anterior mas metodos internos de clases.

### Limites del analizador

- Maximo 200 ficheros procesados por invocacion.
- Maximo 50 simbolos emitidos por fichero.
- Los docstrings se truncan a 1000 caracteres; si se truncan, el texto termina en `"...[truncated]"`.
- Ficheros de mas de 200 000 bytes se omiten y se registra `"file_too_large:{path}"` en `limitations`.

## doc_summary

Solo presente con `--docs`. Resume el alcance y las limitaciones del analisis de documentacion.

```json
{
  "requested": true,
  "total_count": 48,
  "symbol_count": 35,
  "languages": ["python", "typescript"],
  "depth": "symbols",
  "truncated": false,
  "limitations": [
    "docs_unavailable:internal/server.go:language=go",
    "file_too_large:vendor/bundle.js"
  ]
}
```

Campos:

- `requested` (`bool`): `true` si el usuario activo `--docs`.
- `total_count` (`int`): numero total de `DocRecord` emitidos en `docs[]`.
- `symbol_count` (`int`): numero de `DocRecord` cuyo `kind` no es `"module"` (funciones, clases y metodos).
- `languages` (`list[str]`): lenguajes presentes en los registros emitidos, ordenados alfabeticamente.
- `depth` (`"module" | "symbols" | "full" | null`): nivel de profundidad activo durante el analisis.
- `truncated` (`bool`): `true` si alguno de los docstrings fue truncado por superar el limite de 1000 caracteres, o si se alcanzo el limite de 200 ficheros.
- `limitations` (`list[str]`): lista de advertencias y limitaciones del analisis. Formatos posibles:
  - `"docs_unavailable:{path}:language={lang}"` — fichero de lenguaje no soportado.
  - `"file_too_large:{path}"` — fichero omitido por superar el limite de tamaño.
  - `"read_error:{path}"` — error de lectura del fichero.
  - `"python_parse_error:{path}"` — error de parseo de AST Python.
  - `"max_files_reached:{actual}>{limit}"` — se alcanzo el limite de ficheros procesables.

## Modo compacto

Con `--compact`, la salida omite `metadata`, el arbol completo, `dependencies`, `docs` y `module_graph`. El resultado es una proyeccion de aproximadamente 500-700 tokens diseñada para consumo rapido por LLMs.

```json
{
  "schema_version": "1.0",
  "project_type": "webapp",
  "project_summary": "Aplicacion web en Nodejs (Next.js, React). Entry points: app/page.tsx.",
  "stacks": [],
  "entry_points": [],
  "file_paths": [
    "package.json",
    "app/page.tsx",
    "app/layout.tsx"
  ],
  "file_tree_depth1": {
    "package.json": null,
    "app": {}
  },
  "dependency_summary": null
}
```

Campos incluidos en el modo compacto:

- `schema_version`: version del schema.
- `project_type`: clasificacion general.
- `project_summary`: descripcion NL del proyecto. Siempre incluido (Phase 9).
- `stacks`: stacks detectados serializados.
- `entry_points`: entry points serializados.
- `file_paths`: lista plana de todos los paths con separador forward-slash. Siempre incluido (Phase 9).
- `file_tree_depth1`: solo el primer nivel del `file_tree`. Se conserva por compatibilidad retroactiva.
- `dependency_summary`: resumen de dependencias cuando `--dependencies` esta activo y `dependency_summary.requested == True`; `null` en cualquier otro caso (Phase 9).

Campos **excluidos** en modo compacto aunque se combinen con otros flags:

- `metadata`
- `file_tree` (sustituido por `file_tree_depth1` y `file_paths`)
- `dependencies`
- `key_dependencies`
- `module_graph`
- `module_graph_summary`
- `docs`
- `doc_summary`

## Ejemplo completo

Ejemplo de salida para un monorepo con web Node.js y API Python con `--dependencies` y `--docs` activos:

```json
{
  "metadata": {
    "schema_version": "1.0",
    "generated_at": "2026-04-07T19:41:05.686277+00:00",
    "sourcecode_version": "0.7.0",
    "analyzed_path": "/abs/path/to/project"
  },
  "file_tree": {
    "pnpm-workspace.yaml": null,
    "packages": {
      "api": {
        "pyproject.toml": null,
        "main.py": null
      }
    },
    "apps": {
      "web": {
        "package.json": null,
        "app": {
          "page.tsx": null
        }
      }
    }
  },
  "file_paths": [
    "pnpm-workspace.yaml",
    "packages/api/pyproject.toml",
    "packages/api/main.py",
    "apps/web/package.json",
    "apps/web/app/page.tsx"
  ],
  "stacks": [
    {
      "stack": "nodejs",
      "detection_method": "manifest",
      "confidence": "high",
      "frameworks": [
        { "name": "Next.js", "source": "package.json" },
        { "name": "React", "source": "package.json" }
      ],
      "package_manager": null,
      "manifests": ["package.json"],
      "primary": true,
      "root": "apps/web",
      "workspace": "apps/web",
      "signals": [
        "manifest:package.json",
        "framework:Next.js",
        "framework:React",
        "entry:apps/web/app/page.tsx"
      ]
    },
    {
      "stack": "python",
      "detection_method": "manifest",
      "confidence": "high",
      "frameworks": [
        { "name": "FastAPI", "source": "manifest" }
      ],
      "package_manager": "pip",
      "manifests": ["pyproject.toml"],
      "primary": false,
      "root": "packages/api",
      "workspace": "packages/api",
      "signals": [
        "manifest:pyproject.toml",
        "framework:FastAPI",
        "package_manager:pip",
        "entry:packages/api/main.py"
      ]
    }
  ],
  "project_type": "monorepo",
  "entry_points": [
    {
      "path": "apps/web/app/page.tsx",
      "stack": "nodejs",
      "kind": "web",
      "source": "package.json"
    },
    {
      "path": "packages/api/main.py",
      "stack": "python",
      "kind": "cli",
      "source": "manifest"
    }
  ],
  "project_summary": "Monorepo con 2 workspaces en Nodejs, Python. Entry points: apps/web/app/page.tsx, packages/api/main.py. 18 dependencias (python, nodejs).",
  "dependencies": [
    {
      "name": "fastapi",
      "ecosystem": "python",
      "scope": "direct",
      "declared_version": ">=0.115",
      "resolved_version": "0.115.2",
      "source": "lockfile",
      "parent": null,
      "manifest_path": "packages/api/poetry.lock",
      "workspace": "packages/api"
    }
  ],
  "dependency_summary": {
    "requested": true,
    "total_count": 18,
    "direct_count": 5,
    "transitive_count": 13,
    "ecosystems": ["python", "nodejs"],
    "sources": ["lockfile", "manifest"],
    "limitations": []
  },
  "key_dependencies": [
    {
      "name": "fastapi",
      "ecosystem": "python",
      "scope": "direct",
      "declared_version": ">=0.115",
      "resolved_version": "0.115.2",
      "source": "lockfile",
      "parent": null,
      "manifest_path": "packages/api/poetry.lock",
      "workspace": "packages/api"
    },
    {
      "name": "next",
      "ecosystem": "nodejs",
      "scope": "direct",
      "declared_version": "^14.0.0",
      "resolved_version": "14.2.1",
      "source": "lockfile",
      "parent": null,
      "manifest_path": "apps/web/package-lock.json",
      "workspace": "apps/web"
    }
  ],
  "module_graph": null,
  "module_graph_summary": null,
  "docs": [
    {
      "symbol": "packages/api/main.py",
      "kind": "module",
      "language": "python",
      "path": "packages/api/main.py",
      "doc_text": "API principal del proyecto.",
      "signature": null,
      "source": "docstring",
      "importance": "high",
      "workspace": "packages/api"
    },
    {
      "symbol": "create_user",
      "kind": "function",
      "language": "python",
      "path": "packages/api/main.py",
      "doc_text": "Crea un nuevo usuario.",
      "signature": "def create_user(name: str, email: str) -> User",
      "source": "docstring",
      "importance": "high",
      "workspace": "packages/api"
    }
  ],
  "doc_summary": {
    "requested": true,
    "total_count": 2,
    "symbol_count": 1,
    "languages": ["python"],
    "depth": "symbols",
    "truncated": false,
    "limitations": [
      "docs_unavailable:apps/web/app/page.tsx:language=typescript"
    ]
  }
}
```

# Schema de salida

`sourcecode` serializa un objeto `SourceMap` con schema `1.0`.

## Raiz

```json
{
  "metadata": {},
  "file_tree": {},
  "stacks": [],
  "project_type": "webapp",
  "entry_points": [],
  "dependencies": [],
  "dependency_summary": null,
  "module_graph": null
}
```

Campos:

- `metadata`: metadatos del analisis.
- `file_tree`: arbol del repositorio.
- `stacks`: stacks detectados.
- `project_type`: clasificacion general del proyecto.
- `entry_points`: puntos de entrada relevantes.
- `dependencies`: dependencias detectadas cuando se solicita `--dependencies`.
- `dependency_summary`: resumen del analisis de dependencias o `null` si no se solicito.
- `module_graph`: grafo estructural de codigo o `null` si no se solicito `--graph-modules`.
- `module_graph_summary`: resumen compacto del grafo pensado para consumo rapido por LLMs.

## metadata

```json
{
  "schema_version": "1.0",
  "generated_at": "2026-04-07T19:41:05.686277+00:00",
  "sourcecode_version": "0.6.0",
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

`project_type` se calcula a partir de stacks, entry points y senales agregadas.

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

Cada elemento de `dependencies` sigue este shape:

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

Cuando se usa `--dependencies`, `dependency_summary` describe el alcance del analisis:

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

## module_graph

Cuando se usa `--graph-modules`, `module_graph` expone nodos, aristas y un resumen del analisis:

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

## Modo compacto

Con `--compact`, la salida no incluye `metadata` ni el arbol completo. Devuelve:

```json
{
  "schema_version": "1.0",
  "project_type": "webapp",
  "stacks": [],
  "entry_points": [],
  "file_tree_depth1": {}
}
```

Campos:

- `schema_version`: version del schema.
- `project_type`: clasificacion general.
- `stacks`: stacks detectados serializados.
- `entry_points`: entry points serializados.
- `file_tree_depth1`: solo el primer nivel de `file_tree`.

`--compact` no incluye `dependencies` ni `dependency_summary`, aunque se combine con `--dependencies`.
Tampoco incluye `module_graph`, aunque se combine con `--graph-modules`.
Tampoco incluye `module_graph_summary`.

## Ejemplo completo

Ejemplo real de salida para un monorepo con web Node.js y API Python:

```json
{
  "metadata": {
    "schema_version": "1.0",
    "generated_at": "2026-04-07T19:41:05.686277+00:00",
    "sourcecode_version": "0.6.0",
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
  "stacks": [
    {
      "stack": "nodejs",
      "detection_method": "manifest",
      "confidence": "high",
      "frameworks": [
        {
          "name": "Next.js",
          "source": "package.json"
        },
        {
          "name": "React",
          "source": "package.json"
        }
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
        "entry:app/page.tsx",
        "entry:apps/web/app/page.tsx"
      ]
    },
    {
      "stack": "python",
      "detection_method": "manifest",
      "confidence": "high",
      "frameworks": [
        {
          "name": "FastAPI",
          "source": "manifest"
        }
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
        "entry:main.py",
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
  ]
}
```

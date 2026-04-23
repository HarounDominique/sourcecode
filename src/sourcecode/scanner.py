from __future__ import annotations

"""Scanner de ficheros para sourcecode.

Construye un arbol JSON anidado del proyecto respetando .gitignore,
exclusiones por defecto y sin seguir symlinks.

Convencion de nodos (D-01, D-02):
  - null (None en Python) = fichero
  - dict = directorio (vacio o con hijos)
"""

import os
from pathlib import Path
from typing import Any, Optional, cast

from pathspec import GitIgnoreSpec

# Directorios excluidos por defecto (SCAN-02)
DEFAULT_EXCLUDES: frozenset[str] = frozenset({
    "node_modules",
    "__pycache__",
    ".git",
    "vendor",
    "venv",
    ".venv",
    "dist",
    "build",
    "target",
})

# Nombres de ficheros de manifiesto conocidos (para find_manifests)
MANIFEST_NAMES: frozenset[str] = frozenset({
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements.txt",
    "Pipfile",
    "uv.lock",
    "package.json",
    "go.mod",
    "Cargo.toml",
    "pom.xml",
    "build.gradle",
    "composer.json",
    "Gemfile",
    "pubspec.yaml",
})


class FileScanner:
    """Escanea un directorio de proyecto y produce un arbol de ficheros filtrado.

    Args:
        root: Directorio raiz del proyecto a analizar.
        max_depth: Profundidad maxima del arbol de ficheros (default: 4). (SCAN-05)
        extra_excludes: Conjunto adicional de nombres de directorio a excluir.
    """

    def __init__(
        self,
        root: Path,
        max_depth: int = 4,
        extra_excludes: Optional[frozenset[str]] = None,
    ) -> None:
        self.root = root.resolve()
        self.max_depth = max_depth
        self._excludes = DEFAULT_EXCLUDES | (extra_excludes or frozenset())
        self._gitignore_spec: Optional[GitIgnoreSpec] = None

    def _load_gitignore_spec(self) -> GitIgnoreSpec:
        """Carga .gitignore del proyecto como GitIgnoreSpec (SCAN-01)."""
        if self._gitignore_spec is None:
            gitignore = self.root / ".gitignore"
            if gitignore.exists():
                lines = gitignore.read_text(encoding="utf-8", errors="replace").splitlines()
            else:
                lines = []
            self._gitignore_spec = GitIgnoreSpec.from_lines(lines)
        return self._gitignore_spec

    def _is_excluded_by_gitignore(self, rel_path: str, is_dir: bool) -> bool:
        """Comprueba si una ruta relativa (a self.root) esta excluida por .gitignore."""
        spec = self._load_gitignore_spec()
        # GitIgnoreSpec espera rutas con / al final para directorios
        path_to_match = rel_path + "/" if is_dir else rel_path
        return spec.match_file(path_to_match)

    def scan_tree(self) -> dict[str, Any]:
        """Construye el arbol JSON anidado del proyecto.

        Retorna:
            dict donde None = fichero (D-02) y dict = directorio (D-01).
        """
        self._load_gitignore_spec()
        # Arbol raiz que se va rellenando
        root_tree: dict[str, Any] = {}

        for dirpath, dirnames, filenames in os.walk(
            self.root, followlinks=False  # SCAN-03: no seguir symlinks de directorio
        ):
            current = Path(dirpath)
            try:
                rel = current.relative_to(self.root)
            except ValueError:
                continue

            depth = len(rel.parts)

            if depth >= self.max_depth:
                # No descender mas alla de max_depth (SCAN-05)
                dirnames.clear()
                continue

            # Filtrar directorios excluidos in-place (CRITICO: slice assignment) (Trampa 1)
            dirnames[:] = [
                d for d in dirnames
                if d not in self._excludes
                and not (current / d).is_symlink()  # SCAN-03: symlinks explicito
                and not self._is_excluded_by_gitignore(
                    str(rel / d) if rel.parts else d,
                    is_dir=True,
                )
            ]

            # Obtener nodo del arbol correspondiente a este directorio
            node = self._get_or_create_node(root_tree, rel.parts)

            # Agregar ficheros al nodo (null = fichero segun D-02)
            for fname in filenames:
                # Skip flag-shaped names (e.g. "-o", "--format") — shell redirect artifacts.
                # No legitimate source file starts with "-".
                if fname.startswith("-"):
                    continue
                fpath = current / fname
                # SCAN-03: no incluir symlinks de fichero
                if fpath.is_symlink():
                    continue
                # Calcular ruta relativa para gitignore (Trampa 2: rutas relativas)
                rel_file = str(rel / fname) if rel.parts else fname
                if self._is_excluded_by_gitignore(rel_file, is_dir=False):
                    continue
                node[fname] = None  # D-02: null = fichero

            # Asegurar que los subdirectorios aceptados existen como dicts en el nodo
            for d in dirnames:
                if d not in node:
                    node[d] = {}

        return root_tree

    def _get_or_create_node(
        self, tree: dict[str, Any], parts: tuple[str, ...]
    ) -> dict[str, Any]:
        """Navega/crea el nodo del arbol para la ruta indicada."""
        node = tree
        for part in parts:
            if part not in node or node[part] is None:
                node[part] = {}
            node = cast(dict[str, Any], node[part])
        return node

    def find_manifests(self) -> list[str]:
        """Encuentra ficheros de manifiesto en profundidad 0-1 (SCAN-04).

        Retorna:
            Lista de paths absolutos de manifiestos encontrados.
        """
        manifests: list[str] = []
        # Profundidad 0: raiz
        for name in MANIFEST_NAMES:
            candidate = self.root / name
            if candidate.exists() and not candidate.is_symlink():
                manifests.append(str(candidate))
        # Profundidad 1: primer nivel
        try:
            for child in self.root.iterdir():
                if child.is_dir() and not child.is_symlink() and child.name not in self._excludes:
                    for name in MANIFEST_NAMES:
                        candidate = child / name
                        if candidate.exists() and not candidate.is_symlink():
                            manifests.append(str(candidate))
        except PermissionError:
            pass
        return manifests

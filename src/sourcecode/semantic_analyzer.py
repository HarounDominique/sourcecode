"""Analisis semantico estatico: call graph, symbol linking e import resolution.

Extiende el analisis estructural de GraphAnalyzer (Fase 7) con:
- Dos pasadas Python: indice de simbolos + resolucion de llamadas cross-file
- SymbolLink para todos los imports internos Python
- Degradacion segura via limitations[] en lugar de excepciones
- Guards: max_files=200, max_file_size=200_000, max_calls=5_000

Plan 12-02 agrega: _build_reexport_map (reexports via __init__.py), _resolve_star_imports
(star import expansion), _link_symbols (SymbolLink consolidation), namespace package support,
y language_coverage["python"] = "full".
"""
from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Optional

from sourcecode.schema import CallRecord, SemanticSummary, SymbolLink, SymbolRecord
from sourcecode.tree_utils import flatten_file_tree

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PY_EXTENSIONS = {".py"}
_MAX_FILES = 200
_MAX_FILE_SIZE = 200_000
_MAX_CALLS = 5_000
_MAX_SYMBOLS = 10_000


class SemanticAnalyzer:
    """Analisis semantico estatico del proyecto — Python call graph en dos pasadas."""

    def __init__(
        self,
        *,
        max_files: int = _MAX_FILES,
        max_file_size: int = _MAX_FILE_SIZE,
        max_calls: int = _MAX_CALLS,
        max_symbols: int = _MAX_SYMBOLS,
    ) -> None:
        self.max_files = max_files
        self.max_file_size = max_file_size
        self.max_calls = max_calls
        self.max_symbols = max_symbols

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def analyze(
        self,
        root: Path,
        file_tree: dict[str, Any],
        *,
        workspace: str | None = None,
    ) -> tuple[list[CallRecord], list[SymbolRecord], list[SymbolLink], SemanticSummary]:
        """Analiza el proyecto y retorna calls, symbols, links y summary.

        Implementa dos pasadas para Python:
        - Pass 1: construye indice de simbolos (FunctionDef, AsyncFunctionDef, ClassDef)
        - Pass 2: resuelve llamadas via ImportBindings + symbol_index
        Plan 12-02: reexport_map, star import expansion, namespace packages, language_coverage=full.
        """
        limitations: list[str] = []

        # 1. Flatten file_tree and filter to Python files
        all_paths = flatten_file_tree(file_tree)
        source_files = [
            p for p in all_paths
            if Path(p).suffix in _PY_EXTENSIONS and (root / p).is_file()
        ]

        # Files referenced in tree but not on disk (read_error)
        for p in all_paths:
            if Path(p).suffix in _PY_EXTENSIONS and not (root / p).is_file():
                norm = Path(p).as_posix()
                limitations.append(f"read_error:{norm}")

        # Guard max_files
        files_skipped = 0
        if len(source_files) > self.max_files:
            n = len(source_files)
            limitations.append(f"max_files_reached:{n}>{self.max_files}")
            files_skipped = n - self.max_files
            source_files = source_files[: self.max_files]

        # 2. Pass 1: Build symbol index
        symbol_index = self._build_symbol_index(root, source_files, limitations=limitations)

        # 3. Build module map (rel_path -> dotted_module_name)
        module_map = self._build_python_module_map(source_files, limitations=limitations)
        # Reverse map: dotted_module_name -> rel_path
        reverse_module_map = {v: k for k, v in module_map.items()}

        # 4. Build reexport map from __init__.py files (Plan 12-02)
        reexport_map = self._build_reexport_map(root, source_files, module_map, limitations)

        # 5. Pass 2: Resolve calls
        calls: list[CallRecord] = []
        links: list[SymbolLink] = []
        truncated = False

        # Cache of per-file bindings (keyed by rel_path) for _link_symbols
        all_bindings: dict[str, dict[str, tuple[str, str]]] = {}

        for rel_path in source_files:
            abs_path = root / rel_path
            norm_path = Path(rel_path).as_posix()

            # File size guard
            try:
                file_size = abs_path.stat().st_size
            except OSError:
                limitations.append(f"read_error:{norm_path}")
                files_skipped += 1
                continue
            if file_size > self.max_file_size:
                limitations.append(f"file_too_large:{norm_path}")
                files_skipped += 1
                continue

            # Read content
            try:
                content = abs_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                limitations.append(f"read_error:{norm_path}")
                files_skipped += 1
                continue

            # Parse
            try:
                tree = ast.parse(content, filename=norm_path)
            except SyntaxError:
                limitations.append(f"syntax_error:{norm_path}")
                files_skipped += 1
                continue

            # Build import bindings for this file (including star import expansion)
            bindings = self._build_import_bindings(
                tree, norm_path, module_map, limitations,
                root=root, symbol_index=symbol_index,
            )
            all_bindings[rel_path] = bindings

            # Resolve calls from top-level functions and methods
            file_symbols = {sr.symbol: sr for sr in symbol_index.get(rel_path, [])}
            rel_path_posix = Path(rel_path).as_posix()

            for top_node in tree.body:
                if not isinstance(top_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue

                caller_symbol = top_node.name
                caller_path = rel_path_posix

                for node in ast.walk(top_node):
                    if not isinstance(node, ast.Call):
                        continue

                    # Check call budget
                    if len(calls) >= self.max_calls:
                        if not truncated:
                            truncated = True
                            limitations.append("call_budget_reached")
                        break

                    func = node.func

                    # Case 1: ast.Name call (e.g., greet())
                    if isinstance(func, ast.Name):
                        callee_name = func.id
                        call_line = node.lineno
                        args, kwargs = self._extract_call_args(node)

                        # Try to resolve via bindings -> reverse_module_map -> symbol_index
                        if callee_name in bindings:
                            source_module, original_symbol = bindings[callee_name]
                            # Try direct resolve first
                            target_path = reverse_module_map.get(source_module)
                            if target_path is not None:
                                target_posix = Path(target_path).as_posix()
                                target_symbols = {sr.symbol: sr for sr in symbol_index.get(target_path, [])}
                                if original_symbol in target_symbols:
                                    calls.append(CallRecord(
                                        caller_path=caller_path,
                                        caller_symbol=caller_symbol,
                                        callee_path=target_posix,
                                        callee_symbol=original_symbol,
                                        call_line=call_line,
                                        confidence="high",
                                        method="ast",
                                        args=args,
                                        kwargs=kwargs,
                                        workspace=workspace,
                                    ))
                            else:
                                # Try via reexport_map: source_module may be a package
                                # that re-exports original_symbol from a sub-module
                                resolved_path = self._resolve_via_reexport(
                                    source_module, original_symbol, reexport_map
                                )
                                if resolved_path is not None:
                                    target_posix = Path(resolved_path).as_posix()
                                    target_symbols = {sr.symbol: sr for sr in symbol_index.get(resolved_path, [])}
                                    if original_symbol in target_symbols:
                                        calls.append(CallRecord(
                                            caller_path=caller_path,
                                            caller_symbol=caller_symbol,
                                            callee_path=target_posix,
                                            callee_symbol=original_symbol,
                                            call_line=call_line,
                                            confidence="medium",
                                            method="ast",
                                            args=args,
                                            kwargs=kwargs,
                                            workspace=workspace,
                                        ))
                        # Same-file call
                        elif callee_name in file_symbols and callee_name != caller_symbol:
                            call_line = node.lineno
                            args, kwargs = self._extract_call_args(node)
                            calls.append(CallRecord(
                                caller_path=caller_path,
                                caller_symbol=caller_symbol,
                                callee_path=rel_path_posix,
                                callee_symbol=callee_name,
                                call_line=call_line,
                                confidence="high",
                                method="ast",
                                args=args,
                                kwargs=kwargs,
                                workspace=workspace,
                            ))
                        # else: unresolved call — silently skip (no limitation per spec)

                    # Case 2: ast.Attribute call (e.g., module.func())
                    elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                        obj_name = func.value.id
                        attr_name = func.attr
                        call_line = node.lineno
                        args, kwargs = self._extract_call_args(node)

                        # Try: obj_name is an import alias for a module
                        if obj_name in bindings:
                            source_module, _orig = bindings[obj_name]
                            # Try source_module.attr_name as a sub-module
                            qualified = f"{source_module}.{attr_name}"
                            target_path = reverse_module_map.get(qualified) or reverse_module_map.get(source_module)
                            if target_path is not None:
                                target_posix = Path(target_path).as_posix()
                                target_symbols = {sr.symbol: sr for sr in symbol_index.get(target_path, [])}
                                if attr_name in target_symbols:
                                    calls.append(CallRecord(
                                        caller_path=caller_path,
                                        caller_symbol=caller_symbol,
                                        callee_path=target_posix,
                                        callee_symbol=attr_name,
                                        call_line=call_line,
                                        confidence="medium",
                                        method="ast",
                                        args=args,
                                        kwargs=kwargs,
                                        workspace=workspace,
                                    ))
                        # else: unresolved attribute call — silently skip

                    # Case 3: dynamic call (not ast.Name or simple ast.Attribute)
                    else:
                        # Check if it looks dynamic (e.g., getattr, subscript, etc.)
                        # Only emit limitation for clearly dynamic patterns
                        if isinstance(func, (ast.Subscript, ast.Call)):
                            limitations.append(f"dynamic_call_skipped:{norm_path}:{node.lineno}")

                # Check budget after each function
                if truncated:
                    break

        # 6. Build consolidated SymbolLink list (Plan 12-02: _link_symbols)
        links = self._link_symbols(
            source_files=source_files,
            root=root,
            module_map=module_map,
            reexport_map=reexport_map,
            symbol_index=symbol_index,
            all_bindings=all_bindings,
            workspace=workspace,
        )

        # Collect all symbols into a flat list
        all_symbols: list[SymbolRecord] = []
        for sym_list in symbol_index.values():
            all_symbols.extend(sym_list)

        # Build summary
        languages = ["python"] if source_files else []
        files_analyzed = len(source_files) - files_skipped
        if files_analyzed < 0:
            files_analyzed = 0

        # Plan 12-02: language_coverage["python"] = "full" when Python files are analyzed
        lang_coverage: dict[str, str] = {}
        if source_files:
            lang_coverage["python"] = "full"

        summary = SemanticSummary(
            requested=True,
            call_count=len(calls),
            symbol_count=len(all_symbols),
            link_count=len(links),
            languages=languages,
            language_coverage=lang_coverage,
            files_analyzed=files_analyzed,
            files_skipped=files_skipped,
            truncated=truncated,
            limitations=limitations,
        )

        return calls, all_symbols, links, summary

    def merge_summaries(self, summaries: Iterable[SemanticSummary]) -> SemanticSummary:
        """Agrega multiples SemanticSummary en uno."""
        result = SemanticSummary(requested=True)
        languages: set[str] = set()
        limitations: list[str] = []
        language_coverage: dict[str, str] = {}

        for summary in summaries:
            result.call_count += summary.call_count
            result.symbol_count += summary.symbol_count
            result.link_count += summary.link_count
            result.files_analyzed += summary.files_analyzed
            result.files_skipped += summary.files_skipped
            languages.update(summary.languages)
            if summary.truncated:
                result.truncated = True
            # Merge language_coverage (last wins on conflict)
            language_coverage.update(summary.language_coverage)
            for limitation in summary.limitations:
                if limitation not in limitations:
                    limitations.append(limitation)

        result.languages = sorted(languages)
        result.language_coverage = language_coverage
        result.limitations = limitations
        return result

    # -----------------------------------------------------------------------
    # Pass 1: Symbol index
    # -----------------------------------------------------------------------

    def _build_symbol_index(
        self,
        root: Path,
        source_files: list[str],
        *,
        limitations: list[str] | None = None,
    ) -> dict[str, list[SymbolRecord]]:
        """Construye un indice de simbolos para cada fichero Python.

        Retorna dict[rel_path -> list[SymbolRecord]] con funciones y clases de nivel superior.
        """
        if limitations is None:
            limitations = []

        index: dict[str, list[SymbolRecord]] = {}
        total_symbols = 0

        for rel_path in source_files:
            abs_path = root / rel_path
            norm_path = Path(rel_path).as_posix()

            # File size check
            try:
                file_size = abs_path.stat().st_size
            except OSError:
                limitations.append(f"read_error:{norm_path}")
                continue
            if file_size > self.max_file_size:
                limitations.append(f"file_too_large:{norm_path}")
                continue

            # Read content
            try:
                content = abs_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                limitations.append(f"read_error:{norm_path}")
                continue

            # Parse
            try:
                tree = ast.parse(content, filename=norm_path)
            except SyntaxError:
                limitations.append(f"syntax_error:{norm_path}")
                continue

            file_symbols: list[SymbolRecord] = []

            for node in tree.body:
                if total_symbols >= self.max_symbols:
                    break

                kind: str | None = None
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    kind = "function"
                elif isinstance(node, ast.ClassDef):
                    kind = "class"

                if kind is not None:
                    exported = not node.name.startswith("_")
                    sr = SymbolRecord(
                        symbol=node.name,
                        kind=kind,
                        language="python",
                        path=norm_path,
                        line=node.lineno,
                        exported=exported,
                        workspace=None,
                    )
                    file_symbols.append(sr)
                    total_symbols += 1

            index[rel_path] = file_symbols

        if total_symbols >= self.max_symbols:
            limitations.append(f"max_symbols_reached:{total_symbols}>={self.max_symbols}")

        return index

    # -----------------------------------------------------------------------
    # Import bindings
    # -----------------------------------------------------------------------

    def _build_import_bindings(
        self,
        tree: ast.Module,
        rel_path: str,
        module_map: dict[str, str],
        limitations: list[str],
        *,
        root: Path | None = None,
        symbol_index: dict | None = None,
    ) -> dict[str, tuple[str, str]]:
        """Construye un mapa local_name -> (source_module_dotted, original_symbol).

        Procesa ast.Import y ast.ImportFrom en el nivel superior del AST.
        Plan 12-02: expande star imports via _resolve_star_imports() cuando
        root y symbol_index estan disponibles.
        """
        bindings: dict[str, tuple[str, str]] = {}

        for node in tree.body:
            if isinstance(node, ast.Import):
                # "import foo.bar as fb" -> bindings["fb"] = ("foo.bar", "foo.bar")
                # "import foo.bar" -> bindings["foo"] = ("foo.bar", "foo.bar")
                for alias in node.names:
                    local_name = alias.asname if alias.asname else alias.name.split(".")[0]
                    bindings[local_name] = (alias.name, alias.name)

            elif isinstance(node, ast.ImportFrom):
                if node.names and node.names[0].name == "*":
                    # Star import — expand via _resolve_star_imports (plan 12-02)
                    if root is not None and symbol_index is not None:
                        # Resolve the source module name
                        if node.level and node.level > 0:
                            star_module = self._resolve_relative_import(
                                rel_path, node.level, node.module or ""
                            )
                        else:
                            star_module = node.module or ""
                        if star_module:
                            expanded_names = self._resolve_star_imports(
                                root, star_module, module_map, symbol_index, limitations
                            )
                            for name in expanded_names:
                                bindings[name] = (star_module, name)
                    continue

                # Resolve the module name
                if node.level and node.level > 0:
                    # Relative import
                    source_module = self._resolve_relative_import(
                        rel_path, node.level, node.module or ""
                    )
                else:
                    source_module = node.module or ""

                if not source_module:
                    continue

                # "from pkg.mod import Foo" -> bindings["Foo"] = ("pkg.mod", "Foo")
                # "from pkg.mod import Foo as F" -> bindings["F"] = ("pkg.mod", "Foo")
                for alias in node.names:
                    local_name = alias.asname if alias.asname else alias.name
                    bindings[local_name] = (source_module, alias.name)

        # Detect name shadowing via ast.Assign at module level
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in bindings:
                        name = target.id
                        limitations.append(f"name_shadowed:{rel_path}:{name}")
                        del bindings[name]
                        # Only remove once — break if dict shrinks during iteration
                        break

        return bindings

    # -----------------------------------------------------------------------
    # Module map
    # -----------------------------------------------------------------------

    def _build_python_module_map(
        self,
        source_files: list[str],
        limitations: list[str] | None = None,
    ) -> dict[str, str]:
        """Convierte rel_path a dotted module name.

        Sigue el patron de GraphAnalyzer._build_python_module_map() pero
        implementado localmente (sin importar GraphAnalyzer).

        Ejemplos:
          "src/foo/bar.py" -> "foo.bar"  (si src/ es la raiz de paquetes)
          "pkg/mod.py"     -> "pkg.mod"
          "pkg/__init__.py" -> "pkg"

        Plan 12-02: soporta namespace packages (directorios sin __init__.py).
        Directories that contain .py files but no __init__.py are treated as
        namespace packages and included in the map with their dotted paths.
        limitations["namespace_package:{dir}"] added for each one found.
        """
        if limitations is None:
            limitations = []

        module_map: dict[str, str] = {}

        # Collect all directories that have __init__.py
        dirs_with_init: set[str] = set()
        for rel_path in source_files:
            path = PurePosixPath(Path(rel_path).as_posix())
            if path.name == "__init__.py" and len(path.parts) > 1:
                dirs_with_init.add(str(path.parent))

        # Collect directories that contain .py files (for namespace package detection)
        dirs_with_py: dict[str, list[str]] = defaultdict(list)
        for rel_path in source_files:
            path = PurePosixPath(Path(rel_path).as_posix())
            if path.suffix in _PY_EXTENSIONS and len(path.parts) > 1:
                dirs_with_py[str(path.parent)].append(rel_path)

        # Report namespace packages (dirs with .py files but no __init__.py)
        reported_ns: set[str] = set()
        for dir_posix, _files in dirs_with_py.items():
            if dir_posix not in dirs_with_init and dir_posix not in reported_ns:
                reported_ns.add(dir_posix)
                limitations.append(f"namespace_package:{dir_posix}")

        for rel_path in source_files:
            path = PurePosixPath(Path(rel_path).as_posix())
            if path.suffix not in _PY_EXTENSIONS:
                continue
            if path.name == "__init__.py":
                module_name = ".".join(path.parts[:-1])
            else:
                module_name = ".".join(path.with_suffix("").parts)
            if module_name:
                module_map[rel_path] = module_name
        return module_map

    # -----------------------------------------------------------------------
    # Relative import resolution
    # -----------------------------------------------------------------------

    def _resolve_relative_import(
        self,
        rel_path: str,
        level: int,
        module_name: str,
    ) -> str:
        """Resuelve un import relativo a su nombre de modulo dotted.

        Replica la logica de GraphAnalyzer._resolve_python_from_import() con PurePosixPath.

        level=1: directorio del fichero actual
        level=2: directorio padre, etc.
        """
        posix_path = PurePosixPath(Path(rel_path).as_posix())
        # Start from the package of the current file
        package_parts = list(posix_path.parent.parts)
        # Navigate up by (level - 1)
        if level > 1:
            package_parts = package_parts[: max(0, len(package_parts) - (level - 1))]
        base = ".".join(package_parts)
        if module_name:
            return f"{base}.{module_name}" if base else module_name
        return base

    # -----------------------------------------------------------------------
    # Call argument extraction
    # -----------------------------------------------------------------------

    @staticmethod
    def _extract_call_args(call_node: ast.Call) -> tuple[list[str], dict[str, str]]:
        """Extrae args y kwargs de un nodo ast.Call.

        Limite de 80 chars por argumento — expresiones largas reemplazadas por '<expr>'.
        """
        _MAX_ARG_LEN = 80

        args: list[str] = []
        for arg in call_node.args:
            try:
                text = ast.unparse(arg)
            except Exception:
                text = "<expr>"
            args.append(text if len(text) <= _MAX_ARG_LEN else "<expr>")

        kwargs: dict[str, str] = {}
        for kw in call_node.keywords:
            if kw.arg is None:
                # **kwargs unpacking — skip
                continue
            try:
                val_text = ast.unparse(kw.value)
            except Exception:
                val_text = "<expr>"
            kwargs[kw.arg] = val_text if len(val_text) <= _MAX_ARG_LEN else "<expr>"

        return args, kwargs

    # -----------------------------------------------------------------------
    # Plan 12-02: Reexport map
    # -----------------------------------------------------------------------

    def _build_reexport_map(
        self,
        root: Path,
        source_files: list[str],
        module_map: dict[str, str],
        limitations: list[str],
    ) -> dict[str, dict[str, str]]:
        """Escanea __init__.py files y construye {module_dotted -> {symbol -> source_path}}.

        Soporta chaining hasta depth=2 con guard contra bucles (visited set).
        Si se alcanza el limite de chain: limitations["reexport_chain_limit:{symbol}"].

        T-12-02-01 mitigation: chain depth limit = 2, visited set per traversal.
        T-12-02-05 mitigation: solo parsea __init__.py que estan en source_files.
        """
        reexport_map: dict[str, dict[str, str]] = {}

        # Reverse map: dotted_module_name -> rel_path
        reverse_module_map: dict[str, str] = {v: k for k, v in module_map.items()}

        # First pass: build direct reexport entries from each __init__.py
        for rel_path in source_files:
            posix = Path(rel_path).as_posix()
            if not posix.endswith("__init__.py"):
                continue

            module_dotted = module_map.get(rel_path)
            if not module_dotted:
                continue

            abs_path = root / rel_path
            try:
                content = abs_path.read_text(encoding="utf-8", errors="replace")
                tree = ast.parse(content, filename=posix)
            except (OSError, SyntaxError):
                continue

            pkg_symbols: dict[str, str] = {}

            for node in tree.body:
                if not isinstance(node, ast.ImportFrom):
                    continue

                # Resolve the module being imported from
                if node.level and node.level > 0:
                    source_mod = self._resolve_relative_import(
                        posix, node.level, node.module or ""
                    )
                else:
                    source_mod = node.module or ""

                if not source_mod:
                    continue

                # Find the rel_path for source_mod
                source_rel = reverse_module_map.get(source_mod)
                if source_rel is None:
                    continue

                source_posix = Path(source_rel).as_posix()

                # Register each imported name
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    local_name = alias.asname if alias.asname else alias.name
                    pkg_symbols[local_name] = source_posix

            if pkg_symbols:
                reexport_map[module_dotted] = pkg_symbols

        # Second pass: follow one level of chaining (depth=2 total)
        # If source_path in reexport_map entry is itself an __init__.py,
        # try to resolve further using the already-built reexport_map.
        for module_dotted, symbols in list(reexport_map.items()):
            for symbol_name, source_posix in list(symbols.items()):
                if not source_posix.endswith("__init__.py"):
                    continue
                # source is itself an __init__.py — find what it re-exports
                source_module = module_map.get(source_posix)
                if source_module is None:
                    # Try reverse lookup by posix path
                    source_module = next(
                        (m for p, m in module_map.items()
                         if Path(p).as_posix() == source_posix),
                        None,
                    )
                if source_module is None:
                    continue

                inner = reexport_map.get(source_module, {})
                if symbol_name in inner:
                    # Resolved one level deeper — update
                    reexport_map[module_dotted][symbol_name] = inner[symbol_name]
                else:
                    # Could not resolve at depth=2 — report chain limit
                    limitations.append(f"reexport_chain_limit:{symbol_name}")

        return reexport_map

    # -----------------------------------------------------------------------
    # Plan 12-02: Resolve via reexport
    # -----------------------------------------------------------------------

    def _resolve_via_reexport(
        self,
        source_module: str,
        original_symbol: str,
        reexport_map: dict[str, dict[str, str]],
    ) -> str | None:
        """Busca original_symbol en reexport_map[source_module].

        Retorna source_path (rel_path posix) o None si no esta en el mapa.
        """
        return reexport_map.get(source_module, {}).get(original_symbol)

    # -----------------------------------------------------------------------
    # Plan 12-02: Star import expansion
    # -----------------------------------------------------------------------

    _MAX_STAR_SYMBOLS = 200

    def _resolve_star_imports(
        self,
        root: Path,
        star_module: str,
        module_map: dict[str, str],
        symbol_index: dict,
        limitations: list[str],
    ) -> list[str]:
        """Expande 'from foo import *' retornando lista de nombres exportados.

        Estrategia:
        1. Si foo es externo (no en module_map): limitacion y retorna []
        2. Leer __all__ del AST del modulo; si existe y es lista de strings: usar esos
        3. Si no hay __all__ o es dinamico: todos los nombres publicos (FunctionDef,
           AsyncFunctionDef, ClassDef a nivel modulo cuyo nombre no empieza con _)
        4. Limitar a 200 simbolos (T-12-02-02 mitigation)

        T-12-02-02 mitigation: limite de 200 simbolos por expansion.
        """
        # Reverse map: dotted_module -> rel_path
        reverse_module_map = {v: k for k, v in module_map.items()}

        module_rel = reverse_module_map.get(star_module)
        if module_rel is None:
            # External module
            limitations.append(f"star_import_external:{star_module}")
            return []

        abs_path = root / module_rel
        try:
            content = abs_path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(content, filename=Path(module_rel).as_posix())
        except (OSError, SyntaxError):
            return []

        # Look for __all__ = [...] at module level
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if not (isinstance(target, ast.Name) and target.id == "__all__"):
                    continue
                # Check if value is a list of string constants
                if isinstance(node.value, ast.List):
                    names: list[str] = []
                    all_strings = True
                    for elt in node.value.elts:
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                            names.append(elt.value)
                        else:
                            all_strings = False
                            break
                    if all_strings:
                        result = names[: self._MAX_STAR_SYMBOLS]
                        if len(names) > self._MAX_STAR_SYMBOLS:
                            limitations.append(
                                f"star_import_too_large:{star_module}"
                            )
                        return result

        # Fallback: collect all public names at module level
        public_names: list[str] = []
        for node in tree.body:
            name: str | None = None
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = node.name
            elif isinstance(node, ast.Assign):
                # Simple module-level assignment: X = ...
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        name = target.id
                        break
            if name and not name.startswith("_"):
                public_names.append(name)

        result = public_names[: self._MAX_STAR_SYMBOLS]
        if len(public_names) > self._MAX_STAR_SYMBOLS:
            limitations.append(f"star_import_too_large:{star_module}")
        return result

    # -----------------------------------------------------------------------
    # Plan 12-02: Symbol link consolidation
    # -----------------------------------------------------------------------

    def _link_symbols(
        self,
        source_files: list[str],
        root: Path,
        module_map: dict[str, str],
        reexport_map: dict[str, dict[str, str]],
        symbol_index: dict[str, list],
        all_bindings: dict[str, dict[str, tuple[str, str]]],
        workspace: str | None = None,
    ) -> list[SymbolLink]:
        """Produce SymbolLink para cada import binding de cada fichero.

        Para imports internos: SymbolLink con source_path y source_line.
        Para imports externos: SymbolLink con is_external=True, source_path=None.
        Imports resueltos via reexport_map: confidence="medium".
        """
        links: list[SymbolLink] = []
        reverse_module_map: dict[str, str] = {v: k for k, v in module_map.items()}

        for rel_path in source_files:
            bindings = all_bindings.get(rel_path)
            if not bindings:
                continue

            importer_posix = Path(rel_path).as_posix()

            for local_name, (source_module, original_symbol) in bindings.items():
                # --- Try via reexport_map first when source is a package ---
                # (from pkg import Symbol where pkg/__init__.py re-exports Symbol)
                resolved_path = self._resolve_via_reexport(
                    source_module, original_symbol, reexport_map
                )
                if resolved_path is not None:
                    # resolved_path is already a posix rel path
                    resolved_rel = next(
                        (p for p in source_files
                         if Path(p).as_posix() == resolved_path),
                        resolved_path,
                    )
                    source_line = self._find_symbol_line(
                        symbol_index, resolved_rel, original_symbol
                    )
                    links.append(SymbolLink(
                        importer_path=importer_posix,
                        symbol=local_name,
                        source_path=resolved_path,
                        source_line=source_line,
                        is_external=False,
                        confidence="medium",
                        method="ast",
                        workspace=workspace,
                    ))
                    continue

                # --- Try direct resolution ---
                target_rel = reverse_module_map.get(source_module)
                if target_rel is not None:
                    target_posix = Path(target_rel).as_posix()
                    # Find source_line from symbol_index
                    source_line = self._find_symbol_line(
                        symbol_index, target_rel, original_symbol
                    )
                    links.append(SymbolLink(
                        importer_path=importer_posix,
                        symbol=local_name,
                        source_path=target_posix,
                        source_line=source_line,
                        is_external=False,
                        confidence="high",
                        method="ast",
                        workspace=workspace,
                    ))
                    continue

                # --- External import ---
                links.append(SymbolLink(
                    importer_path=importer_posix,
                    symbol=local_name,
                    source_path=None,
                    source_line=None,
                    is_external=True,
                    confidence="high",
                    method="ast",
                    workspace=workspace,
                ))

        return links

    @staticmethod
    def _find_symbol_line(
        symbol_index: dict[str, list],
        rel_path: str,
        symbol_name: str,
    ) -> int | None:
        """Busca la linea de definicion de symbol_name en symbol_index[rel_path]."""
        for sr in symbol_index.get(rel_path, []):
            if sr.symbol == symbol_name:
                return sr.line
        return None

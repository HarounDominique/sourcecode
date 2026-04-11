"""Analisis semantico estatico: call graph, symbol linking e import resolution.

Extiende el analisis estructural de GraphAnalyzer (Fase 7) con:
- Dos pasadas Python: indice de simbolos + resolucion de llamadas cross-file
- SymbolLink para todos los imports internos Python
- Degradacion segura via limitations[] en lugar de excepciones
- Guards: max_files=200, max_file_size=200_000, max_calls=5_000

Plan 12-02 agrega: _build_reexport_map (reexports via __init__.py), _resolve_star_imports
(star import expansion), _link_symbols (SymbolLink consolidation), namespace package support,
y language_coverage["python"] = "full".

Plan 12-03 agrega: capa JS/TS con _extract_js_imports, _resolve_js_module_path,
_analyze_js_file, _detect_js_calls, integracion en analyze() para JS/TS files,
y language_coverage["nodejs"] = "heuristic".
"""
from __future__ import annotations

import ast
import re
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

# ---------------------------------------------------------------------------
# JS/TS keyword and builtin exclusions (Plan 12-03)
# ---------------------------------------------------------------------------

_JS_KEYWORD_EXCLUSIONS: frozenset[str] = frozenset({
    # JS reserved words
    "if", "else", "for", "while", "do", "switch", "case", "break", "continue",
    "return", "throw", "try", "catch", "finally", "new", "delete", "typeof",
    "instanceof", "void", "in", "of", "async", "await", "yield", "import",
    "export", "default", "class", "extends", "super", "this", "static",
    "get", "set", "let", "const", "var", "function", "debugger", "with",
    # Common builtins / globals
    "console", "Math", "Object", "Array", "String", "Number", "Boolean",
    "Promise", "Error", "TypeError", "RangeError", "Symbol", "Map", "Set",
    "WeakMap", "WeakSet", "Proxy", "Reflect", "JSON", "RegExp", "Date",
    "setTimeout", "clearTimeout", "setInterval", "clearInterval", "queueMicrotask",
    "require", "module", "exports", "process", "global", "window", "document",
    "navigator", "location", "fetch", "URL", "URLSearchParams", "FormData",
    # TypeScript keywords
    "type", "interface", "namespace", "declare", "abstract", "enum", "as",
    "from", "keyof", "typeof", "infer", "never", "unknown", "any",
})


class SemanticAnalyzer:
    """Analisis semantico estatico del proyecto — Python call graph en dos pasadas."""

    _NODE_EXTENSIONS: frozenset[str] = frozenset({".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"})
    _GO_EXTENSIONS: frozenset[str] = frozenset({".go"})
    _RUST_EXTENSIONS: frozenset[str] = frozenset({".rs"})
    _JVM_EXTENSIONS: frozenset[str] = frozenset({".java", ".kt", ".scala"})

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

        # Collect all Python symbols into a flat list
        all_symbols: list[SymbolRecord] = []
        for sym_list in symbol_index.values():
            all_symbols.extend(sym_list)

        # Build summary bases
        languages = ["python"] if source_files else []
        files_analyzed = len(source_files) - files_skipped
        if files_analyzed < 0:
            files_analyzed = 0

        # Plan 12-02: language_coverage["python"] = "full" when Python files are analyzed
        lang_coverage: dict[str, str] = {}
        if source_files:
            lang_coverage["python"] = "full"

        # -----------------------------------------------------------------------
        # Plan 12-03: JS/TS analysis block
        # -----------------------------------------------------------------------
        js_source_files = [
            p for p in all_paths
            if Path(p).suffix in self._NODE_EXTENSIONS and (root / p).is_file()
        ]
        internal_module_paths: set[str] = set(js_source_files)

        if js_source_files:
            # Build JS symbol index: rel_path -> list[SymbolRecord]
            js_symbol_index: dict[str, list[SymbolRecord]] = {}
            # Build import bindings per file: rel_path -> {local_name -> (specifier, orig)}
            js_all_bindings: dict[str, dict[str, tuple[str, str]]] = {}

            # Pass 1: index symbols for all JS/TS files
            for rel_path in js_source_files:
                abs_path = root / rel_path
                norm_path = Path(rel_path).as_posix()
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
                try:
                    content = abs_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    limitations.append(f"read_error:{norm_path}")
                    files_skipped += 1
                    continue

                js_symbol_index[rel_path] = self._analyze_js_file(content, norm_path)
                js_all_bindings[rel_path] = self._extract_js_imports(content, norm_path)
                files_analyzed += 1

            # Pass 2: detect calls using import bindings
            for rel_path in js_source_files:
                if rel_path not in js_symbol_index:
                    continue  # was skipped above
                abs_path = root / rel_path
                norm_path = Path(rel_path).as_posix()
                try:
                    content = abs_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue

                js_bindings = js_all_bindings.get(rel_path, {})
                file_js_symbols = js_symbol_index.get(rel_path, [])

                # Detect calls for each function symbol as caller context
                for sym in file_js_symbols:
                    if sym.kind not in ("function",):
                        continue
                    if len(calls) >= self.max_calls:
                        if not truncated:
                            truncated = True
                            limitations.append("call_budget_reached")
                        break
                    new_calls = self._detect_js_calls(
                        content=content,
                        rel_path=norm_path,
                        caller_symbol=sym.symbol,
                        js_bindings=js_bindings,
                        js_symbol_index=js_symbol_index,
                        internal_module_paths=internal_module_paths,
                        root=root,
                        workspace=workspace,
                    )
                    # Apply budget
                    remaining = self.max_calls - len(calls)
                    if len(new_calls) > remaining:
                        calls.extend(new_calls[:remaining])
                        if not truncated:
                            truncated = True
                            limitations.append("call_budget_reached")
                    else:
                        calls.extend(new_calls)

                if truncated:
                    break

            # Build SymbolLinks for JS/TS imports
            for rel_path, js_bindings in js_all_bindings.items():
                if rel_path not in js_symbol_index:
                    continue
                norm_path = Path(rel_path).as_posix()
                for local_name, (specifier, _orig) in js_bindings.items():
                    resolved = self._resolve_js_module_path(
                        specifier, norm_path, internal_module_paths, root=root
                    )
                    if resolved is not None:
                        # Internal
                        source_line: int | None = None
                        resolved_syms = js_symbol_index.get(resolved, [])
                        for sr in resolved_syms:
                            if sr.symbol == local_name:
                                source_line = sr.line
                                break
                        links.append(SymbolLink(
                            importer_path=norm_path,
                            symbol=local_name,
                            source_path=resolved,
                            source_line=source_line,
                            is_external=False,
                            confidence="medium",
                            method="heuristic",
                            workspace=workspace,
                        ))
                    else:
                        # External (npm package or unresolvable)
                        links.append(SymbolLink(
                            importer_path=norm_path,
                            symbol=local_name,
                            source_path=None,
                            source_line=None,
                            is_external=True,
                            confidence="medium",
                            method="heuristic",
                            workspace=workspace,
                        ))

            # Collect JS/TS symbols
            for sym_list in js_symbol_index.values():
                all_symbols.extend(sym_list)

            # Update language coverage and languages list
            js_languages: set[str] = set()
            for rel_path in js_source_files:
                if rel_path in js_symbol_index:
                    suffix = Path(rel_path).suffix
                    if suffix in {".ts", ".tsx"}:
                        js_languages.add("typescript")
                    else:
                        js_languages.add("javascript")
            languages.extend(sorted(js_languages))
            lang_coverage["nodejs"] = "heuristic"

        # -----------------------------------------------------------------------
        # Plan 12-04: Go analysis block
        # -----------------------------------------------------------------------
        go_source_files = [
            p for p in all_paths
            if Path(p).suffix in self._GO_EXTENSIONS and (root / p).is_file()
        ]
        if go_source_files:
            for rel_path in go_source_files:
                abs_path = root / rel_path
                norm_path = Path(rel_path).as_posix()
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
                try:
                    content = abs_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    limitations.append(f"read_error:{norm_path}")
                    files_skipped += 1
                    continue
                go_syms, go_calls = self._analyze_go_file(content, norm_path)
                all_symbols.extend(go_syms)
                remaining = self.max_calls - len(calls)
                if len(go_calls) > remaining:
                    calls.extend(go_calls[:remaining])
                    if not truncated:
                        truncated = True
                        limitations.append("call_budget_reached")
                else:
                    calls.extend(go_calls)
                files_analyzed += 1
            languages.append("go")
            lang_coverage["go"] = "heuristic"

        # -----------------------------------------------------------------------
        # Plan 12-04: Rust analysis block
        # -----------------------------------------------------------------------
        rust_source_files = [
            p for p in all_paths
            if Path(p).suffix in self._RUST_EXTENSIONS and (root / p).is_file()
        ]
        if rust_source_files:
            for rel_path in rust_source_files:
                abs_path = root / rel_path
                norm_path = Path(rel_path).as_posix()
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
                try:
                    content = abs_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    limitations.append(f"read_error:{norm_path}")
                    files_skipped += 1
                    continue
                rs_syms, rs_calls = self._analyze_rust_file(content, norm_path)
                all_symbols.extend(rs_syms)
                remaining = self.max_calls - len(calls)
                if len(rs_calls) > remaining:
                    calls.extend(rs_calls[:remaining])
                    if not truncated:
                        truncated = True
                        limitations.append("call_budget_reached")
                else:
                    calls.extend(rs_calls)
                files_analyzed += 1
            languages.append("rust")
            lang_coverage["rust"] = "heuristic"

        # -----------------------------------------------------------------------
        # Plan 12-04: JVM analysis block (Java, Kotlin, Scala)
        # -----------------------------------------------------------------------
        jvm_source_files = [
            p for p in all_paths
            if Path(p).suffix in self._JVM_EXTENSIONS and (root / p).is_file()
        ]
        if jvm_source_files:
            for rel_path in jvm_source_files:
                abs_path = root / rel_path
                norm_path = Path(rel_path).as_posix()
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
                try:
                    content = abs_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    limitations.append(f"read_error:{norm_path}")
                    files_skipped += 1
                    continue
                jvm_syms, jvm_calls = self._analyze_java_file(content, norm_path)
                all_symbols.extend(jvm_syms)
                remaining = self.max_calls - len(calls)
                if len(jvm_calls) > remaining:
                    calls.extend(jvm_calls[:remaining])
                    if not truncated:
                        truncated = True
                        limitations.append("call_budget_reached")
                else:
                    calls.extend(jvm_calls)
                files_analyzed += 1
            languages.append("java")
            lang_coverage["java"] = "heuristic"

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
    # Plan 12-04: Polyglot heuristics
    # -----------------------------------------------------------------------

    def _analyze_go_file(
        self,
        content: str,
        rel_path: str,
    ) -> tuple[list[SymbolRecord], list[CallRecord]]:
        """Heuristic Go: detecta func declarations y call sites locales.

        method="heuristic", confidence="low" para todos los edges Go.
        """
        symbols: list[SymbolRecord] = []
        calls: list[CallRecord] = []

        func_pat = re.compile(
            r"^func\s+(?:\([^)]+\)\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\(",
            re.MULTILINE,
        )
        func_names: set[str] = set()
        for m in func_pat.finditer(content):
            name = m.group(1)
            line = content[: m.start()].count("\n") + 1
            symbols.append(SymbolRecord(
                symbol=name,
                kind="function",
                language="go",
                path=rel_path,
                line=line,
                exported=name[0].isupper() if name else False,
            ))
            func_names.add(name)

        call_pat = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")
        for m in call_pat.finditer(content):
            callee = m.group(1)
            if callee in func_names:
                line = content[: m.start()].count("\n") + 1
                calls.append(CallRecord(
                    caller_path=rel_path,
                    caller_symbol="",
                    callee_path=rel_path,
                    callee_symbol=callee,
                    call_line=line,
                    confidence="low",
                    method="heuristic",
                ))

        return symbols, calls

    def _analyze_rust_file(
        self,
        content: str,
        rel_path: str,
    ) -> tuple[list[SymbolRecord], list[CallRecord]]:
        """Heuristic Rust: detecta fn/struct declarations y call sites.

        method="heuristic", confidence="low" para todos los edges Rust.
        """
        _RUST_KEYWORDS: frozenset[str] = frozenset({
            "if", "for", "while", "match", "loop", "let", "mut", "use", "mod",
            "impl", "pub", "fn", "struct", "enum", "trait", "return", "break",
            "continue", "where", "type", "unsafe", "extern",
        })

        symbols: list[SymbolRecord] = []
        calls: list[CallRecord] = []

        fn_pat = re.compile(
            r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+([a-z_][a-zA-Z0-9_]*)",
            re.MULTILINE,
        )
        fn_names: set[str] = set()
        for m in fn_pat.finditer(content):
            name = m.group(1)
            line = content[: m.start()].count("\n") + 1
            symbols.append(SymbolRecord(
                symbol=name,
                kind="function",
                language="rust",
                path=rel_path,
                line=line,
                exported=False,
            ))
            fn_names.add(name)

        struct_pat = re.compile(
            r"^\s*(?:pub\s+)?struct\s+([A-Za-z_][A-Za-z0-9_]*)",
            re.MULTILINE,
        )
        for m in struct_pat.finditer(content):
            name = m.group(1)
            line = content[: m.start()].count("\n") + 1
            symbols.append(SymbolRecord(
                symbol=name,
                kind="class",
                language="rust",
                path=rel_path,
                line=line,
                exported=False,
            ))

        # Module-qualified calls: foo::bar(
        mod_call_pat = re.compile(
            r"\b([a-z_][a-zA-Z0-9_]*)::([a-z_][a-zA-Z0-9_]*)\s*\("
        )
        for m in mod_call_pat.finditer(content):
            callee = m.group(2)
            line = content[: m.start()].count("\n") + 1
            calls.append(CallRecord(
                caller_path=rel_path,
                caller_symbol="",
                callee_path=rel_path,
                callee_symbol=callee,
                call_line=line,
                confidence="low",
                method="heuristic",
            ))

        # Local calls filtered by known fn names
        local_call_pat = re.compile(r"\b([a-z_][a-zA-Z0-9_]*)\s*\(")
        for m in local_call_pat.finditer(content):
            callee = m.group(1)
            if callee in fn_names and callee not in _RUST_KEYWORDS:
                line = content[: m.start()].count("\n") + 1
                calls.append(CallRecord(
                    caller_path=rel_path,
                    caller_symbol="",
                    callee_path=rel_path,
                    callee_symbol=callee,
                    call_line=line,
                    confidence="low",
                    method="heuristic",
                ))

        return symbols, calls

    def _analyze_java_file(
        self,
        content: str,
        rel_path: str,
    ) -> tuple[list[SymbolRecord], list[CallRecord]]:
        """Heuristic Java/Kotlin: detecta class/method declarations y call sites.

        method="heuristic", confidence="low" para todos los edges Java.
        """
        _JAVA_KEYWORDS: frozenset[str] = frozenset({
            "if", "for", "while", "switch", "catch", "super", "this", "new",
            "return", "break", "continue", "throw", "try", "finally", "instanceof",
        })

        symbols: list[SymbolRecord] = []
        calls: list[CallRecord] = []

        class_pat = re.compile(
            r"(?:class|interface|enum)\s+([A-Z][A-Za-z0-9_]*)"
        )
        for m in class_pat.finditer(content):
            name = m.group(1)
            line = content[: m.start()].count("\n") + 1
            symbols.append(SymbolRecord(
                symbol=name,
                kind="class",
                language="java",
                path=rel_path,
                line=line,
                exported=True,
            ))

        method_pat = re.compile(
            r"(?:public|private|protected|static|\s)+\w[\w<>\[\]]*\s+([a-z][A-Za-z0-9_]*)\s*\("
        )
        method_names: set[str] = set()
        for m in method_pat.finditer(content):
            name = m.group(1)
            if name in _JAVA_KEYWORDS:
                continue
            line = content[: m.start()].count("\n") + 1
            symbols.append(SymbolRecord(
                symbol=name,
                kind="function",
                language="java",
                path=rel_path,
                line=line,
                exported=False,
            ))
            method_names.add(name)

        call_pat = re.compile(r"\b([a-z][A-Za-z0-9_]*)\s*\(")
        for m in call_pat.finditer(content):
            callee = m.group(1)
            if callee in method_names and callee not in _JAVA_KEYWORDS:
                line = content[: m.start()].count("\n") + 1
                calls.append(CallRecord(
                    caller_path=rel_path,
                    caller_symbol="",
                    callee_path=rel_path,
                    callee_symbol=callee,
                    call_line=line,
                    confidence="low",
                    method="heuristic",
                ))

        return symbols, calls

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

    # -----------------------------------------------------------------------
    # Plan 12-03: JS/TS semantic layer
    # -----------------------------------------------------------------------

    def _extract_js_imports(
        self,
        content: str,
        rel_path: str,
    ) -> dict[str, tuple[str, str]]:
        """Extrae import bindings de JS/TS.

        Soporta (en orden de precedencia):
        - import * as ns from './foo'                -> {"ns": ("./foo", "*")}
        - import { Foo, Bar as B } from './foo'      -> {"Foo": ("./foo","Foo"), "B": ("./foo","Bar")}
        - import DefaultName from './foo'             -> {"DefaultName": ("./foo", "default")}
        - import DefaultName, { Named } from './foo' -> combined
        - const { fn } = require('./foo')            -> {"fn": ("./foo", "fn")}
        - const foo = require('./foo')               -> {"foo": ("./foo", "default")}
        """
        bindings: dict[str, tuple[str, str]] = {}

        # Pattern 1: namespace import  — import * as ns from 'specifier'
        _pat_namespace = re.compile(
            r"""import\s+\*\s+as\s+([A-Za-z_$][A-Za-z0-9_$]*)\s+from\s+['"]([^'"]+)['"]""",
            re.MULTILINE,
        )
        for m in _pat_namespace.finditer(content):
            local_name = m.group(1)
            specifier = m.group(2)
            bindings[local_name] = (specifier, "*")

        # Pattern 2: named imports  — import { Foo, Bar as B } from 'specifier'
        # Also handles: import Default, { Named } from 'specifier'
        _pat_named = re.compile(
            r"""import\s+(?:[A-Za-z_$][A-Za-z0-9_$]*\s*,\s*)?\{([^}]+)\}\s+from\s+['"]([^'"]+)['"]""",
            re.MULTILINE,
        )
        for m in _pat_named.finditer(content):
            named_block = m.group(1)
            specifier = m.group(2)
            for item in named_block.split(","):
                item = item.strip()
                if not item:
                    continue
                if " as " in item:
                    orig, alias = item.split(" as ", 1)
                    bindings[alias.strip()] = (specifier, orig.strip())
                else:
                    bindings[item] = (specifier, item)

        # Pattern 3: default import  — import DefaultName from 'specifier'
        # Must NOT match namespace ("* as") or named ("{") imports already handled
        _pat_default = re.compile(
            r"""import\s+([A-Za-z_$][A-Za-z0-9_$]*)\s+from\s+['"]([^'"]+)['"]""",
            re.MULTILINE,
        )
        for m in _pat_default.finditer(content):
            local_name = m.group(1)
            specifier = m.group(2)
            # Skip if this local_name was already set by namespace pattern (import * as ...)
            # or if it looks like a keyword
            if local_name in bindings and bindings[local_name][1] == "*":
                continue
            # Don't override named import bindings for this specifier
            if local_name not in bindings:
                bindings[local_name] = (specifier, "default")

        # Pattern 4: CommonJS destructure  — const { fn, bar } = require('specifier')
        _pat_cjs_destruct = re.compile(
            r"""(?:const|let|var)\s+\{([^}]+)\}\s*=\s*require\s*\(\s*['"]([^'"]+)['"]\s*\)""",
            re.MULTILINE,
        )
        for m in _pat_cjs_destruct.finditer(content):
            named_block = m.group(1)
            specifier = m.group(2)
            for item in named_block.split(","):
                item = item.strip()
                if not item:
                    continue
                if " as " in item:
                    orig, alias = item.split(" as ", 1)
                    bindings[alias.strip()] = (specifier, orig.strip())
                else:
                    bindings[item] = (specifier, item)

        # Pattern 5: CommonJS plain  — const foo = require('specifier')
        _pat_cjs_plain = re.compile(
            r"""(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*require\s*\(\s*['"]([^'"]+)['"]\s*\)""",
            re.MULTILINE,
        )
        for m in _pat_cjs_plain.finditer(content):
            local_name = m.group(1)
            specifier = m.group(2)
            # Only set if not already set by destructure pattern
            if local_name not in bindings:
                bindings[local_name] = (specifier, "default")

        return bindings

    def _resolve_js_module_path(
        self,
        specifier: str,
        caller_path: str,
        internal_module_paths: set[str],
        *,
        root: Path | None = None,
    ) -> str | None:
        """Resuelve un import specifier JS/TS a un rel_path del proyecto.

        - Si specifier no empieza con '.' o '/' -> externo, retornar None
        - Resolver path relativo desde caller_path
        - Probar extensiones en orden: as-is, .js, .ts, .jsx, .tsx, /index.js, /index.ts
        - Verificar que el resultado esta en internal_module_paths
        - Usa Path.resolve() con root para prevenir path traversal (T-12-03-05)
        """
        if not specifier.startswith(".") and not specifier.startswith("/"):
            return None  # external npm package

        caller_dir = PurePosixPath(caller_path).parent

        # Extension probe order
        candidates: list[str] = []
        base = str(caller_dir / specifier) if str(caller_dir) != "." else specifier
        # Normalize PurePosixPath
        base_posix = str(PurePosixPath(base))

        candidates.append(base_posix)
        if not any(base_posix.endswith(ext) for ext in (".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs")):
            candidates.append(base_posix + ".js")
            candidates.append(base_posix + ".ts")
            candidates.append(base_posix + ".jsx")
            candidates.append(base_posix + ".tsx")
            candidates.append(base_posix + "/index.js")
            candidates.append(base_posix + "/index.ts")

        # Path traversal guard: if root is provided, ensure resolved path is under root
        for candidate in candidates:
            # Normalise dots/double-dots
            try:
                if root is not None:
                    resolved_abs = (root / candidate).resolve()
                    root_resolved = root.resolve()
                    # Must be under root
                    resolved_abs.relative_to(root_resolved)
                    # Convert back to rel_path using forward slashes
                    rel = resolved_abs.relative_to(root_resolved)
                    candidate_posix = str(PurePosixPath(rel))
                else:
                    candidate_posix = str(PurePosixPath(candidate))
            except (ValueError, OSError):
                continue

            if candidate_posix in internal_module_paths:
                return candidate_posix

        return None

    def _analyze_js_file(
        self,
        content: str,
        rel_path: str,
    ) -> list[SymbolRecord]:
        """Detecta funciones y clases JS/TS exportadas usando regex con numeros de linea.

        Patrones (re.MULTILINE):
        - function declarations (export optional, async optional)
        - class declarations (export optional)
        - const arrow/function expressions (export optional)

        language: "typescript" for .ts/.tsx, "javascript" for others.
        """
        suffix = Path(rel_path).suffix.lower()
        language = "typescript" if suffix in {".ts", ".tsx"} else "javascript"

        symbols: list[SymbolRecord] = []

        patterns: list[tuple[re.Pattern[str], str]] = [
            (
                re.compile(
                    r"(?:^|\n)\s*(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s+([A-Za-z_$][A-Za-z0-9_$]*)",
                    re.MULTILINE,
                ),
                "function",
            ),
            (
                re.compile(
                    r"(?:^|\n)\s*(?:export\s+(?:default\s+)?)?class\s+([A-Za-z_$][A-Za-z0-9_$]*)",
                    re.MULTILINE,
                ),
                "class",
            ),
            (
                re.compile(
                    r"(?:^|\n)\s*(?:export\s+)?const\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*(?:async\s+)?(?:function|\()",
                    re.MULTILINE,
                ),
                "function",
            ),
        ]

        seen: set[str] = set()
        for pattern, kind in patterns:
            for m in pattern.finditer(content):
                name = m.group(1)
                if name in seen:
                    continue
                seen.add(name)
                line = content[: m.start()].count("\n") + 1
                exported = True  # regex already filters exported/top-level
                symbols.append(SymbolRecord(
                    symbol=name,
                    kind=kind,
                    language=language,
                    path=rel_path,
                    line=line,
                    exported=exported,
                    workspace=None,
                ))

        return symbols

    def _detect_js_calls(
        self,
        content: str,
        rel_path: str,
        caller_symbol: str,
        js_bindings: dict[str, tuple[str, str]],
        js_symbol_index: dict[str, list[SymbolRecord]],
        internal_module_paths: set[str],
        *,
        root: Path | None = None,
        workspace: str | None = None,
    ) -> list[CallRecord]:
        """Detecta call sites en JS/TS usando regex heuristico.

        Solo emite CallRecord si el identificador esta en js_bindings.
        Filtra _JS_KEYWORD_EXCLUSIONS.
        method='heuristic', confidence='medium' para calls en bindings.
        String literals en args -> '<string_literal>' (T-12-03-03).
        max_calls guard compartido (chequeado por el caller en analyze()).
        """
        calls: list[CallRecord] = []

        # Pattern 1: namespace member call — ns.method(
        _pat_member = re.compile(
            r"\b([A-Za-z_$][A-Za-z0-9_$]*)\.([A-Za-z_$][A-Za-z0-9_$]*)\s*\(",
            re.MULTILINE,
        )
        # Pattern 2: direct identifier call — name(
        _pat_ident = re.compile(
            r"\b([A-Za-z_$][A-Za-z0-9_$]*)\s*\(",
            re.MULTILINE,
        )

        emitted: set[tuple[str, str]] = set()

        # --- Pattern 1: namespace member calls (obj.method()) ---
        for m in _pat_member.finditer(content):
            obj_name = m.group(1)
            method_name = m.group(2)

            if obj_name in _JS_KEYWORD_EXCLUSIONS or method_name in _JS_KEYWORD_EXCLUSIONS:
                continue
            if obj_name not in js_bindings:
                continue

            specifier, orig = js_bindings[obj_name]
            if orig != "*":
                continue  # only namespace imports for Pattern 1

            resolved = self._resolve_js_module_path(
                specifier, rel_path, internal_module_paths, root=root
            )
            if resolved is None:
                continue

            # Check the resolved module has this symbol
            resolved_syms = {sr.symbol for sr in js_symbol_index.get(resolved, [])}
            if method_name not in resolved_syms:
                continue

            call_key = (method_name, resolved)
            if call_key in emitted:
                continue
            emitted.add(call_key)

            # Capture args from the call site
            args = self._capture_js_call_args(content, m.end() - 1)

            call_line = content[: m.start()].count("\n") + 1
            calls.append(CallRecord(
                caller_path=rel_path,
                caller_symbol=caller_symbol,
                callee_path=resolved,
                callee_symbol=method_name,
                call_line=call_line,
                confidence="medium",
                method="heuristic",
                args=args,
                kwargs={},
                workspace=workspace,
            ))

        # --- Pattern 2: direct identifier calls (name()) ---
        for m in _pat_ident.finditer(content):
            name = m.group(1)

            if name in _JS_KEYWORD_EXCLUSIONS:
                continue
            if name not in js_bindings:
                continue

            specifier, orig = js_bindings[name]

            resolved = self._resolve_js_module_path(
                specifier, rel_path, internal_module_paths, root=root
            )
            if resolved is None:
                continue

            # Determine callee_symbol
            if orig == "default":
                # For default imports, the local binding name IS the callee symbol
                callee_symbol = name
                # Verify the resolved file has a matching symbol (any function)
                resolved_syms = {sr.symbol for sr in js_symbol_index.get(resolved, [])}
                if callee_symbol not in resolved_syms:
                    # Try first function in the file as fallback
                    fn_syms = [sr for sr in js_symbol_index.get(resolved, []) if sr.kind == "function"]
                    if fn_syms:
                        callee_symbol = fn_syms[0].symbol
                    else:
                        continue
            elif orig == "*":
                # Already handled by Pattern 1
                continue
            else:
                callee_symbol = orig
                resolved_syms = {sr.symbol for sr in js_symbol_index.get(resolved, [])}
                if callee_symbol not in resolved_syms:
                    continue

            call_key = (callee_symbol, resolved)
            if call_key in emitted:
                continue
            emitted.add(call_key)

            args = self._capture_js_call_args(content, m.end() - 1)

            call_line = content[: m.start()].count("\n") + 1
            calls.append(CallRecord(
                caller_path=rel_path,
                caller_symbol=caller_symbol,
                callee_path=resolved,
                callee_symbol=callee_symbol,
                call_line=call_line,
                confidence="medium",
                method="heuristic",
                args=args,
                kwargs={},
                workspace=workspace,
            ))

        return calls

    @staticmethod
    def _capture_js_call_args(content: str, paren_pos: int) -> list[str]:
        """Captura argumentos de una llamada JS/TS desde la posicion del '(' inicial.

        Estrategia: encuentra el cierre de parentesis balanceado, split por ',', hasta 5 args.
        - Identifier simple o literal numerico -> textual
        - String literal ('...', "...", `...`) -> '<string_literal>'
        - Expresion compleja -> '<expr>'

        T-12-03-03 mitigation: string literals no se exponen en el output.
        """
        _MAX_JS_ARGS = 5
        _SIMPLE_IDENT = re.compile(r'^[A-Za-z_$][A-Za-z0-9_$.]*$')
        _NUMERIC_LIT = re.compile(r'^-?\d+(\.\d+)?$')

        # Find matching closing paren (balanced)
        if paren_pos >= len(content) or content[paren_pos] != "(":
            return []

        depth = 0
        end = paren_pos
        for i in range(paren_pos, min(paren_pos + 2000, len(content))):
            ch = content[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    end = i
                    break

        arg_content = content[paren_pos + 1: end]

        # Simple split by comma (up to _MAX_JS_ARGS)
        raw_args: list[str] = []
        current = []
        depth2 = 0
        for ch in arg_content:
            if ch in "([{":
                depth2 += 1
                current.append(ch)
            elif ch in ")]}":
                depth2 -= 1
                current.append(ch)
            elif ch == "," and depth2 == 0:
                raw_args.append("".join(current).strip())
                current = []
                if len(raw_args) >= _MAX_JS_ARGS:
                    break
            else:
                current.append(ch)
        if current and len(raw_args) < _MAX_JS_ARGS:
            raw_args.append("".join(current).strip())

        args: list[str] = []
        for raw in raw_args:
            if not raw:
                continue
            # String literal check (single, double, backtick)
            if (
                (raw.startswith("'") and raw.endswith("'"))
                or (raw.startswith('"') and raw.endswith('"'))
                or (raw.startswith("`") and raw.endswith("`"))
            ):
                args.append("<string_literal>")
            elif _SIMPLE_IDENT.match(raw):
                args.append(raw)
            elif _NUMERIC_LIT.match(raw):
                args.append(raw)
            else:
                args.append("<expr>")

        return args

from __future__ import annotations

"""AST-based semantic contract extractor.

Extracts FileContract from source files using:
  - Python: stdlib ast (always available, method="ast")
  - TS/JS/TSX/JSX: tree-sitter if installed (method="tree_sitter"),
    otherwise enhanced regex (method="heuristic")

Install tree-sitter for best TS/JS results:
  pip install sourcecode[ast]
"""

import ast
import re
import sys
from pathlib import Path
from typing import Any, Iterator, Optional

from sourcecode.contract_model import (
    ExportRecord,
    FileContract,
    FunctionSignature,
    ImportRecord,
    TypeDefinition,
    TypeField,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_FILE_SIZE = 200_000  # bytes — skip files larger than this

# Python stdlib module names — used to filter noise from import lists.
# sys.stdlib_module_names is available in Python 3.10+; fall back to a
# curated set for 3.9 compatibility.
if hasattr(sys, "stdlib_module_names"):
    _PY_STDLIB: frozenset[str] = sys.stdlib_module_names  # type: ignore[attr-defined]
else:
    _PY_STDLIB: frozenset[str] = frozenset({  # type: ignore[no-redef]
        "__future__", "_thread", "abc", "aifc", "argparse", "array", "ast",
        "asynchat", "asyncio", "asyncore", "atexit", "audioop", "base64",
        "bdb", "binascii", "binhex", "bisect", "builtins", "bz2", "calendar",
        "cgi", "cgitb", "chunk", "cmath", "cmd", "code", "codecs", "codeop",
        "collections", "colorsys", "compileall", "concurrent", "configparser",
        "contextlib", "contextvars", "copy", "copyreg", "cProfile", "csv",
        "ctypes", "curses", "dataclasses", "datetime", "dbm", "decimal",
        "difflib", "dis", "doctest", "email", "encodings", "enum", "errno",
        "faulthandler", "fcntl", "filecmp", "fileinput", "fnmatch", "fractions",
        "ftplib", "functools", "gc", "getopt", "getpass", "gettext", "glob",
        "grp", "gzip", "hashlib", "heapq", "hmac", "html", "http", "idlelib",
        "imaplib", "importlib", "inspect", "io", "ipaddress", "itertools",
        "json", "keyword", "lib2to3", "linecache", "locale", "logging", "lzma",
        "mailbox", "marshal", "math", "mimetypes", "mmap", "modulefinder",
        "multiprocessing", "netrc", "nntplib", "numbers", "operator", "optparse",
        "os", "pathlib", "pdb", "pickle", "pickletools", "pipes", "pkgutil",
        "platform", "plistlib", "poplib", "posix", "posixpath", "pprint",
        "profile", "pstats", "pty", "pwd", "py_compile", "pyclbr", "pydoc",
        "queue", "quopri", "random", "re", "readline", "reprlib", "resource",
        "rlcompleter", "runpy", "sched", "secrets", "select", "selectors",
        "shelve", "shlex", "shutil", "signal", "site", "smtpd", "smtplib",
        "sndhdr", "socket", "socketserver", "sqlite3", "ssl", "stat",
        "statistics", "string", "stringprep", "struct", "subprocess", "sunau",
        "symtable", "sys", "sysconfig", "syslog", "tabnanny", "tarfile",
        "tempfile", "termios", "test", "textwrap", "threading", "time",
        "timeit", "tkinter", "token", "tokenize", "tomllib", "trace",
        "traceback", "tracemalloc", "tty", "types", "typing", "unicodedata",
        "unittest", "urllib", "uuid", "venv", "warnings", "wave", "weakref",
        "webbrowser", "wsgiref", "xml", "xmlrpc", "zipapp", "zipfile",
        "zipimport", "zlib", "zoneinfo",
    })

_LANGUAGE_MAP: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "jsx",
    ".mjs": "javascript",
    ".cjs": "javascript",
}

_REACT_HOOKS: frozenset[str] = frozenset({
    "useState", "useEffect", "useContext", "useReducer", "useCallback",
    "useMemo", "useRef", "useImperativeHandle", "useLayoutEffect",
    "useDebugValue", "useId", "useDeferredValue", "useTransition",
    "useSyncExternalStore", "useInsertionEffect",
})

_ENTRYPOINT_STEMS: frozenset[str] = frozenset({
    "main", "cli", "app", "server", "index", "__main__",
    "application", "bootstrap", "entry", "start",
})

# ---------------------------------------------------------------------------
# Tree-sitter lazy initialization
# ---------------------------------------------------------------------------

_TS_AVAILABLE = False
_TS_LANG: Any = None
_TSX_LANG: Any = None
_JS_LANG: Any = None
_TS_INITED = False


def _init_tree_sitter() -> bool:
    global _TS_AVAILABLE, _TS_LANG, _TSX_LANG, _JS_LANG, _TS_INITED
    if _TS_INITED:
        return _TS_AVAILABLE
    _TS_INITED = True

    try:
        from tree_sitter import Language  # noqa: F401
    except ImportError:
        return False

    try:
        import tree_sitter_typescript as tsts  # type: ignore[import]
        _TS_LANG = _make_language(tsts, "language_typescript", "typescript")
        _TSX_LANG = _make_language(tsts, "language_tsx", "tsx")
    except (ImportError, Exception):
        pass

    try:
        import tree_sitter_javascript as tsjs  # type: ignore[import]
        _JS_LANG = _make_language(tsjs, "language_javascript", "javascript") or _make_language(tsjs, "language", "javascript")
    except (ImportError, Exception):
        pass

    if _TS_LANG is not None or _JS_LANG is not None:
        _TS_AVAILABLE = True
    return _TS_AVAILABLE


def _make_language(module: Any, fn_name: str, name: str) -> Any:
    from tree_sitter import Language
    fn = getattr(module, fn_name, None)
    if fn is None:
        return None
    lang_ptr = fn()
    try:
        return Language(lang_ptr)
    except TypeError:
        try:
            return Language(lang_ptr, name)
        except Exception:
            return None


def _get_parser(lang_obj: Any) -> Any:
    from tree_sitter import Parser
    try:
        return Parser(lang_obj)
    except TypeError:
        p = Parser()
        p.set_language(lang_obj)
        return p


# ---------------------------------------------------------------------------
# Tree-sitter helpers
# ---------------------------------------------------------------------------

def _walk(node: Any) -> Iterator[Any]:
    yield node
    for child in node.children:
        yield from _walk(child)


def _find_child(node: Any, *types: str) -> Optional[Any]:
    for child in node.children:
        if child.type in types:
            return child
    return None


def _find_all(node: Any, *types: str) -> list[Any]:
    return [child for child in node.children if child.type in types]


def _text(node: Any, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _named_children(node: Any) -> list[Any]:
    return [c for c in node.children if c.is_named]


# ---------------------------------------------------------------------------
# Tree-sitter TS/JS extraction
# ---------------------------------------------------------------------------

def _ts_imports(root: Any, src: bytes) -> list[ImportRecord]:
    records: list[ImportRecord] = []
    for node in _walk(root):
        if node.type != "import_statement":
            continue
        source = ""
        symbols: list[str] = []
        kind = "side_effect"

        for child in node.children:
            if child.type == "string":
                source = _text(child, src).strip("'\"` \t")
            elif child.type == "import_clause":
                for c in child.children:
                    if c.type == "identifier":
                        symbols.append(_text(c, src))
                        kind = "default"
                    elif c.type == "named_imports":
                        kind = "named"
                        for spec in c.children:
                            if spec.type == "import_specifier":
                                # name: (identifier) or (identifier) "as" (identifier)
                                # we want the original name (first identifier)
                                id_child = _find_child(spec, "identifier")
                                if id_child:
                                    symbols.append(_text(id_child, src))
                    elif c.type == "namespace_import":
                        kind = "namespace"
                        id_child = _find_child(c, "identifier")
                        if id_child:
                            symbols.append(f"* as {_text(id_child, src)}")

        if source:
            records.append(ImportRecord(
                source=source,
                symbols=sorted(set(symbols)),
                kind=kind,
            ))
    return records


def _ts_exports(root: Any, src: bytes) -> list[ExportRecord]:
    records: list[ExportRecord] = []

    for node in _walk(root):
        if node.type != "export_statement":
            continue

        children_types = {c.type for c in node.children}
        is_default = "default" in children_types

        handled = False
        for child in node.children:
            if child.type == "function_declaration":
                name_n = _find_child(child, "identifier")
                name = _text(name_n, src) if name_n else ("default" if is_default else "unknown")
                async_ = any(c.type == "async" for c in child.children)
                records.append(ExportRecord(name=name, kind="function", async_=async_))
                handled = True

            elif child.type == "class_declaration":
                name_n = _find_child(child, "type_identifier", "identifier")
                name = _text(name_n, src) if name_n else ("default" if is_default else "unknown")
                records.append(ExportRecord(name=name, kind="class"))
                handled = True

            elif child.type in ("lexical_declaration", "variable_declaration"):
                for decl in [n for n in _walk(child) if n.type == "variable_declarator"]:
                    name_n = _find_child(decl, "identifier")
                    if not name_n:
                        continue
                    name = _text(name_n, src)
                    val = _find_child(decl, "arrow_function", "function")
                    kind = "function" if val else "const"
                    records.append(ExportRecord(name=name, kind=kind))
                handled = True

            elif child.type == "interface_declaration":
                name_n = _find_child(child, "type_identifier")
                name = _text(name_n, src) if name_n else "unknown"
                records.append(ExportRecord(name=name, kind="interface"))
                handled = True

            elif child.type == "type_alias_declaration":
                name_n = _find_child(child, "type_identifier")
                name = _text(name_n, src) if name_n else "unknown"
                records.append(ExportRecord(name=name, kind="type"))
                handled = True

            elif child.type == "enum_declaration":
                name_n = _find_child(child, "identifier")
                name = _text(name_n, src) if name_n else "unknown"
                records.append(ExportRecord(name=name, kind="enum"))
                handled = True

            elif child.type == "export_clause":
                # export { A, B } or export { A as B }
                for spec in child.children:
                    if spec.type == "export_specifier":
                        id_nodes = [c for c in spec.children if c.type == "identifier"]
                        if id_nodes:
                            records.append(ExportRecord(name=_text(id_nodes[0], src), kind="unknown"))
                handled = True

        if not handled and is_default:
            # export default <expression>
            for child in node.children:
                if child.type not in ("export", "default", ";") and not child.type.startswith("comment"):
                    name_n = _find_child(child, "identifier", "type_identifier")
                    name = _text(name_n, src) if name_n else "default"
                    records.append(ExportRecord(name=name, kind="default"))
                    break

    return records


def _ts_functions(root: Any, src: bytes, exported_names: set[str]) -> list[FunctionSignature]:
    fns: list[FunctionSignature] = []
    seen: set[str] = set()

    for node in _walk(root):
        name = ""
        async_ = False
        params_text = "()"
        ret_text: Optional[str] = None

        if node.type == "function_declaration":
            name_n = _find_child(node, "identifier")
            if not name_n:
                continue
            name = _text(name_n, src)
            async_ = any(c.type == "async" for c in node.children)
            params_n = _find_child(node, "formal_parameters")
            params_text = _text(params_n, src) if params_n else "()"
            ret_n = _find_child(node, "type_annotation")
            ret_text = _text(ret_n, src).lstrip(":").strip() if ret_n else None

        elif node.type == "variable_declarator":
            # const fn = (params): RetType =>
            name_n = _find_child(node, "identifier")
            val_n = _find_child(node, "arrow_function", "function")
            if not name_n or not val_n:
                continue
            name = _text(name_n, src)
            async_ = any(c.type == "async" for c in val_n.children)
            params_n = _find_child(val_n, "formal_parameters")
            params_text = _text(params_n, src) if params_n else "()"
            ret_n = _find_child(val_n, "type_annotation")
            ret_text = _text(ret_n, src).lstrip(":").strip() if ret_n else None

        else:
            continue

        if not name or name in seen:
            continue
        seen.add(name)

        sig = params_text
        if ret_text:
            sig += f": {ret_text}"

        fns.append(FunctionSignature(
            name=name,
            signature=sig,
            async_=async_,
            exported=name in exported_names,
            return_type=ret_text,
        ))

    return fns


def _ts_types(root: Any, src: bytes) -> list[TypeDefinition]:
    types: list[TypeDefinition] = []

    for node in _walk(root):
        if node.type == "interface_declaration":
            name_n = _find_child(node, "type_identifier")
            if not name_n:
                continue
            name = _text(name_n, src)
            fields: list[TypeField] = []
            # "interface_body" in tree-sitter-typescript >= 0.21; "object_type" in older builds
            body_n = _find_child(node, "interface_body", "object_type")
            if body_n:
                for prop in _walk(body_n):
                    if prop.type in ("property_signature", "method_signature"):
                        prop_name_n = _find_child(prop, "property_identifier", "identifier")
                        type_n = _find_child(prop, "type_annotation")
                        if prop_name_n:
                            prop_name = _text(prop_name_n, src)
                            type_text = _text(type_n, src).lstrip(":").strip() if type_n else "unknown"
                            required = not any(c.type == "?" for c in prop.children)
                            fields.append(TypeField(name=prop_name, type=type_text, required=required))
            extends: list[str] = []
            heritage_n = _find_child(node, "extends_type_clause", "extends_clause", "class_heritage")
            if heritage_n:
                for ext_n in _walk(heritage_n):
                    if ext_n.type == "type_identifier":
                        extends.append(_text(ext_n, src))
            types.append(TypeDefinition(name=name, kind="interface", fields=fields, extends=extends))

        elif node.type == "type_alias_declaration":
            name_n = _find_child(node, "type_identifier")
            if not name_n:
                continue
            types.append(TypeDefinition(name=_text(name_n, src), kind="type", fields=[]))

        elif node.type == "enum_declaration":
            name_n = _find_child(node, "identifier")
            if not name_n:
                continue
            fields = []
            body_n = _find_child(node, "enum_body")
            if body_n:
                for member in body_n.children:
                    if member.type == "enum_assignment":
                        mem_name_n = _find_child(member, "property_identifier", "identifier")
                        if mem_name_n:
                            fields.append(TypeField(name=_text(mem_name_n, src), type="enum_member", required=False))
                    elif member.type == "property_identifier":
                        fields.append(TypeField(name=_text(member, src), type="enum_member", required=False))
            types.append(TypeDefinition(name=_text(name_n, src), kind="enum", fields=fields))

    return types


def _ts_hooks(root: Any, src: bytes) -> list[str]:
    used: set[str] = set()
    for node in _walk(root):
        if node.type == "call_expression":
            fn_n = _find_child(node, "identifier", "member_expression")
            if fn_n and fn_n.type == "identifier":
                name = _text(fn_n, src)
                if name in _REACT_HOOKS or (name.startswith("use") and name[3:4].isupper()):
                    used.add(name)
    return sorted(used)


def _merge_imports(imports: list[ImportRecord]) -> list[ImportRecord]:
    """Merge multiple ImportRecords with the same source into one.

    Tree-sitter correctly captures `import { A }` and `import type { B }` from
    the same module as two separate statements.  Merging them produces a compact,
    predictable contract where each source appears exactly once.
    """
    merged: dict[str, ImportRecord] = {}
    for imp in imports:
        if imp.source in merged:
            existing = merged[imp.source]
            combined_symbols = sorted(set(existing.symbols) | set(imp.symbols))
            kind = existing.kind if existing.kind != "side_effect" else imp.kind
            merged[imp.source] = ImportRecord(source=imp.source, symbols=combined_symbols, kind=kind)
        else:
            merged[imp.source] = imp
    return list(merged.values())


def _extract_ts_js_tree_sitter(path: str, source: str, lang_obj: Any, language: str) -> FileContract:
    try:
        parser = _get_parser(lang_obj)
        src_bytes = source.encode("utf-8")
        tree = parser.parse(src_bytes)
        root = tree.root_node

        imports = _merge_imports(_ts_imports(root, src_bytes))
        exports = _ts_exports(root, src_bytes)
        exported_names = {e.name for e in exports}
        functions = _ts_functions(root, src_bytes, exported_names)
        types = _ts_types(root, src_bytes)
        hooks_used = _ts_hooks(root, src_bytes) if language in ("tsx", "jsx") else []

        # Also check TS files for hook usage (custom hooks)
        if language in ("typescript", "javascript"):
            hooks_used = _ts_hooks(root, src_bytes)

        deps = sorted({
            imp.source for imp in imports
            if not imp.source.startswith(".") and not imp.source.startswith("/")
        })

        return FileContract(
            path=path,
            language=language,
            exports=sorted(exports, key=lambda e: e.name),
            imports=sorted(imports, key=lambda i: i.source),
            functions=sorted(functions, key=lambda f: f.name),
            types=sorted(types, key=lambda t: t.name),
            hooks_used=hooks_used,
            dependencies=deps,
            extraction_method="tree_sitter",
        )
    except Exception as exc:
        return FileContract(
            path=path,
            language=language,
            extraction_method="heuristic",
            limitations=[f"tree_sitter_error: {type(exc).__name__}: {exc}"],
        )


# ---------------------------------------------------------------------------
# Enhanced heuristic TS/JS extraction (fallback when tree-sitter unavailable)
# ---------------------------------------------------------------------------

_IMPORT_RE = re.compile(
    r"""import\s+
    (?:
      (?P<default_ns>[A-Za-z_$][A-Za-z0-9_$]*)   # default import
      (?:\s*,\s*)?
    )?
    (?:
      \*\s+as\s+(?P<ns>[A-Za-z_$][A-Za-z0-9_$]*)  # namespace import
      |
      \{(?P<named>[^}]*)\}                          # named imports
    )?
    \s*from\s*['"`](?P<source>[^'"`]+)['"`]
    |
    import\s*['"`](?P<side>[^'"`]+)['"`]            # side-effect import
    """,
    re.VERBOSE | re.MULTILINE,
)

_EXPORT_FN_RE = re.compile(
    r"^export\s+(?P<async>async\s+)?function\s+(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)\s*(?P<params>\([^)]*\))\s*(?::\s*(?P<ret>[^{;]+))?",
    re.MULTILINE,
)
_EXPORT_CONST_RE = re.compile(
    r"^export\s+(?:const|let|var)\s+(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)\s*(?::[^=]+)?=\s*(?P<async>async\s+)?(?P<arrow>\([^)]*\)\s*(?::[^=]+)?=>)?",
    re.MULTILINE,
)
_EXPORT_CLASS_RE = re.compile(
    r"^export\s+(?:abstract\s+)?class\s+(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)",
    re.MULTILINE,
)
_EXPORT_INTERFACE_RE = re.compile(
    r"^export\s+interface\s+(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)",
    re.MULTILINE,
)
_EXPORT_TYPE_RE = re.compile(
    r"^export\s+type\s+(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)",
    re.MULTILINE,
)
_EXPORT_ENUM_RE = re.compile(
    r"^export\s+(?:const\s+)?enum\s+(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)",
    re.MULTILINE,
)
_EXPORT_DEFAULT_RE = re.compile(
    r"^export\s+default\s+(?:async\s+)?(?:function|class)?\s*(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)?",
    re.MULTILINE,
)
_EXPORT_NAMED_RE = re.compile(
    r"^export\s*\{([^}]+)\}",
    re.MULTILINE,
)

_INTERFACE_RE = re.compile(
    r"(?:export\s+)?interface\s+(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)[^{]*\{(?P<body>[^}]*)\}",
    re.DOTALL | re.MULTILINE,
)
_IFACE_FIELD_RE = re.compile(
    r"^\s*(?P<name>[A-Za-z_$][A-Za-z0-9_$?]*)\s*(?P<opt>\?)?\s*:\s*(?P<type>[^;\n]+)",
    re.MULTILINE,
)
_HOOK_CALL_RE = re.compile(r"\b(use[A-Z][A-Za-z0-9]*)\s*\(")


def _heuristic_ts_imports(source: str) -> list[ImportRecord]:
    records: list[ImportRecord] = []
    for m in _IMPORT_RE.finditer(source):
        side = m.group("side")
        if side:
            records.append(ImportRecord(source=side, kind="side_effect"))
            continue
        src = m.group("source")
        if not src:
            continue
        symbols: list[str] = []
        kind = "side_effect"
        if m.group("ns"):
            kind = "namespace"
            symbols.append(f"* as {m.group('ns')}")
        if m.group("named"):
            kind = "named"
            for s in m.group("named").split(","):
                s = s.strip().split(" as ")[0].strip()
                if s:
                    symbols.append(s)
        if m.group("default_ns"):
            if kind == "side_effect":
                kind = "default"
            symbols.insert(0, m.group("default_ns"))
        records.append(ImportRecord(source=src, symbols=sorted(set(symbols)), kind=kind))
    return records


def _heuristic_ts_exports(source: str) -> list[ExportRecord]:
    records: list[ExportRecord] = []
    for m in _EXPORT_FN_RE.finditer(source):
        records.append(ExportRecord(
            name=m.group("name"),
            kind="function",
            async_=bool(m.group("async")),
        ))
    for m in _EXPORT_CONST_RE.finditer(source):
        kind = "function" if m.group("arrow") else "const"
        records.append(ExportRecord(
            name=m.group("name"),
            kind=kind,
            async_=bool(m.group("async")),
        ))
    for m in _EXPORT_CLASS_RE.finditer(source):
        records.append(ExportRecord(name=m.group("name"), kind="class"))
    for m in _EXPORT_INTERFACE_RE.finditer(source):
        records.append(ExportRecord(name=m.group("name"), kind="interface"))
    for m in _EXPORT_TYPE_RE.finditer(source):
        records.append(ExportRecord(name=m.group("name"), kind="type"))
    for m in _EXPORT_ENUM_RE.finditer(source):
        records.append(ExportRecord(name=m.group("name"), kind="enum"))
    for m in _EXPORT_DEFAULT_RE.finditer(source):
        name = m.group("name") or "default"
        records.append(ExportRecord(name=name, kind="default"))
    for m in _EXPORT_NAMED_RE.finditer(source):
        for part in m.group(1).split(","):
            name = part.strip().split(" as ")[0].strip()
            if name:
                records.append(ExportRecord(name=name, kind="unknown"))
    seen: set[str] = set()
    deduped: list[ExportRecord] = []
    for r in records:
        if r.name not in seen:
            seen.add(r.name)
            deduped.append(r)
    return deduped


_FN_DECL_RE = re.compile(
    r"^(?:export\s+)?(?P<async>async\s+)?function\s+(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)\s*(?P<params>\([^)]*\))\s*(?::\s*(?P<ret>[^{;]+?))?[\s{]",
    re.MULTILINE,
)
_ARROW_DECL_RE = re.compile(
    r"^(?:export\s+)?(?:const|let|var)\s+(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)\s*(?::[^=]+)?=\s*(?P<async>async\s+)?(?P<params>\([^)]*\))\s*(?::\s*(?P<ret>[^=>{]+?))?=>",
    re.MULTILINE,
)


def _heuristic_ts_functions(source: str, exported_names: set[str]) -> list[FunctionSignature]:
    fns: list[FunctionSignature] = []
    seen: set[str] = set()
    for m in _FN_DECL_RE.finditer(source):
        name = m.group("name")
        if name in seen:
            continue
        seen.add(name)
        params = m.group("params") or "()"
        ret = (m.group("ret") or "").strip(" \t\n{") or None
        sig = params + (f": {ret}" if ret else "")
        fns.append(FunctionSignature(
            name=name, signature=sig,
            async_=bool(m.group("async")),
            exported=name in exported_names,
            return_type=ret,
        ))
    for m in _ARROW_DECL_RE.finditer(source):
        name = m.group("name")
        if name in seen:
            continue
        seen.add(name)
        params = m.group("params") or "()"
        ret = (m.group("ret") or "").strip(" \t\n") or None
        sig = params + (f": {ret}" if ret else "")
        fns.append(FunctionSignature(
            name=name, signature=sig,
            async_=bool(m.group("async")),
            exported=name in exported_names,
            return_type=ret,
        ))
    return fns


def _heuristic_ts_types(source: str) -> list[TypeDefinition]:
    types: list[TypeDefinition] = []
    seen: set[str] = set()
    for m in _INTERFACE_RE.finditer(source):
        name = m.group("name")
        if name in seen:
            continue
        seen.add(name)
        fields: list[TypeField] = []
        for fm in _IFACE_FIELD_RE.finditer(m.group("body")):
            field_name = fm.group("name").rstrip("?")
            required = not fm.group("opt") and "?" not in fm.group("name")
            fields.append(TypeField(
                name=field_name,
                type=fm.group("type").strip(" ;,"),
                required=required,
            ))
        types.append(TypeDefinition(name=name, kind="interface", fields=fields))
    return types


def _extract_ts_js_heuristic(path: str, source: str, language: str) -> FileContract:
    imports = _heuristic_ts_imports(source)
    exports = _heuristic_ts_exports(source)
    exported_names = {e.name for e in exports}
    functions = _heuristic_ts_functions(source, exported_names)
    types = _heuristic_ts_types(source)

    hooks_used: list[str] = []
    if language in ("tsx", "jsx", "typescript", "javascript"):
        hooks_used = sorted({
            m.group(1) for m in _HOOK_CALL_RE.finditer(source)
            if m.group(1) in _REACT_HOOKS or (m.group(1).startswith("use") and len(m.group(1)) > 3)
        })

    deps = sorted({
        imp.source for imp in imports
        if not imp.source.startswith(".") and not imp.source.startswith("/")
    })

    return FileContract(
        path=path,
        language=language,
        exports=sorted(exports, key=lambda e: e.name),
        imports=sorted(imports, key=lambda i: i.source),
        functions=sorted(functions, key=lambda f: f.name),
        types=sorted(types, key=lambda t: t.name),
        hooks_used=hooks_used,
        dependencies=deps,
        extraction_method="heuristic",
        limitations=["tree_sitter_unavailable: install sourcecode[ast] for full TS/JS extraction"],
    )


# ---------------------------------------------------------------------------
# Python extraction via stdlib ast
# ---------------------------------------------------------------------------

def _py_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    args = node.args
    params: list[str] = []

    defaults_offset = len(args.args) - len(args.defaults)
    for i, arg in enumerate(args.args):
        if arg.arg == "self" or arg.arg == "cls":
            continue
        p = arg.arg
        if arg.annotation:
            try:
                p += f": {ast.unparse(arg.annotation)}"
            except Exception:
                pass
        di = i - defaults_offset
        if di >= 0:
            try:
                p += f" = {ast.unparse(args.defaults[di])}"
            except Exception:
                p += " = ..."
        params.append(p)

    if args.vararg:
        p = f"*{args.vararg.arg}"
        if args.vararg.annotation:
            try:
                p += f": {ast.unparse(args.vararg.annotation)}"
            except Exception:
                pass
        params.append(p)

    for i, kwarg_arg in enumerate(args.kwonlyargs):
        p = kwarg_arg.arg
        if kwarg_arg.annotation:
            try:
                p += f": {ast.unparse(kwarg_arg.annotation)}"
            except Exception:
                pass
        if args.kw_defaults[i] is not None:
            try:
                p += f" = {ast.unparse(args.kw_defaults[i])}"
            except Exception:
                p += " = ..."
        params.append(p)

    if args.kwarg:
        p = f"**{args.kwarg.arg}"
        if args.kwarg.annotation:
            try:
                p += f": {ast.unparse(args.kwarg.annotation)}"
            except Exception:
                pass
        params.append(p)

    sig = f"({', '.join(params)})"
    if node.returns:
        try:
            sig += f" -> {ast.unparse(node.returns)}"
        except Exception:
            pass
    # Keep full signature — serializer applies per-mode compression.
    # Hard cap at 2000 to prevent pathological cases.
    if len(sig) > 2000:
        sig = sig[:1997] + "..."
    return sig


def _py_class_fields(node: ast.ClassDef) -> list[TypeField]:
    fields: list[TypeField] = []
    for item in node.body:
        # Annotated assignments: name: type [= value]
        if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            try:
                type_str = ast.unparse(item.annotation)
            except Exception:
                type_str = "unknown"
            required = item.value is None
            fields.append(TypeField(name=item.target.id, type=type_str, required=required))
        # __init__ parameter annotations as fields
        elif isinstance(item, ast.FunctionDef) and item.name == "__init__":
            for arg in item.args.args:
                if arg.arg in ("self", "cls"):
                    continue
                if arg.annotation:
                    try:
                        type_str = ast.unparse(arg.annotation)
                    except Exception:
                        type_str = "unknown"
                    fields.append(TypeField(name=arg.arg, type=type_str, required=True))
    return fields


def _extract_python(path: str, source: str) -> FileContract:
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        return FileContract(
            path=path,
            language="python",
            extraction_method="ast",
            limitations=[f"syntax_error: {exc}"],
        )

    # Discover __all__ for explicit exports
    all_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    if isinstance(node.value, (ast.List, ast.Tuple)):
                        for elt in node.value.elts:
                            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                all_names.add(elt.value)

    imports: list[ImportRecord] = []
    exports: list[ExportRecord] = []
    functions: list[FunctionSignature] = []
    types: list[TypeDefinition] = []

    for node in ast.iter_child_nodes(tree):
        # Imports
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname or alias.name
                imports.append(ImportRecord(source=alias.name, symbols=[name], kind="named"))

        elif isinstance(node, ast.ImportFrom):
            src = node.module or ""
            if src.startswith("."):
                continue  # skip relative — internal
            symbols = [alias.asname or alias.name for alias in node.names]
            imports.append(ImportRecord(source=src, symbols=sorted(symbols), kind="named"))

        # Functions
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            name = node.name
            if name.startswith("_") and name not in all_names:
                continue
            exported = bool(all_names) and name in all_names or not all_names
            sig = _py_signature(node)
            ret = None
            if node.returns:
                try:
                    ret = ast.unparse(node.returns)
                except Exception:
                    pass
            functions.append(FunctionSignature(
                name=name, signature=sig,
                async_=isinstance(node, ast.AsyncFunctionDef),
                exported=exported,
                return_type=ret,
            ))
            if exported or name in all_names:
                exports.append(ExportRecord(name=name, kind="function", async_=isinstance(node, ast.AsyncFunctionDef)))

        # Classes
        elif isinstance(node, ast.ClassDef):
            name = node.name
            if name.startswith("_") and name not in all_names:
                continue
            bases = []
            for base in node.bases:
                try:
                    bases.append(ast.unparse(base))
                except Exception:
                    pass
            fields = _py_class_fields(node)
            types.append(TypeDefinition(name=name, kind="class", fields=fields, extends=bases))
            exported = bool(all_names) and name in all_names or not all_names
            if exported or name in all_names:
                exports.append(ExportRecord(name=name, kind="class"))

    # Filter stdlib from imports — they add noise without signal for agents
    _stdlib_roots = {m.split(".")[0] for m in _PY_STDLIB}
    imports = [i for i in imports if i.source.split(".")[0] not in _stdlib_roots]

    deps = sorted({
        imp.source.split(".")[0]
        for imp in imports
        if not imp.source.startswith(".")
    })

    return FileContract(
        path=path,
        language="python",
        exports=sorted(exports, key=lambda e: e.name),
        imports=sorted(imports, key=lambda i: i.source),
        functions=sorted(functions, key=lambda f: f.name),
        types=sorted(types, key=lambda t: t.name),
        dependencies=deps,
        extraction_method="ast",
    )


# ---------------------------------------------------------------------------
# Role detection
# ---------------------------------------------------------------------------

def _detect_role(path: str, contract: FileContract) -> str:
    path_lower = path.lower().replace("\\", "/")
    stem = Path(path).stem.lower()
    ext = Path(path).suffix.lower()

    # Entrypoint
    if stem in _ENTRYPOINT_STEMS and not any(
        x in path_lower for x in ["/test", "/spec", "/fixture"]
    ):
        return "entrypoint"

    # Hook (React): filename starts with "use" + uppercase (camelCase convention)
    if stem.startswith("use") and len(stem) > 3 and stem[3:4].upper() == stem[3:4] and stem[3:4].isalpha():
        return "hook"
    # Export names that are hook-style (useXxx pattern, original casing)
    export_names = {e.name for e in contract.exports}
    if any(n.startswith("use") and len(n) > 3 and n[3:4].isupper() for n in export_names):
        return "hook"

    # Route / page
    if any(x in path_lower for x in ["/routes/", "/route.", "/pages/", "/api/", "/handlers/"]):
        return "route"

    # Config
    if any(x in path_lower for x in [".config.", "config/", "constants", "settings", "env."]):
        return "config"

    # Service / repository
    if any(x in path_lower for x in ["service", "repository", "controller", "handler", "usecase"]):
        return "service"

    # Store
    if any(x in path_lower for x in ["store", "slice", "reducer", "actions", "selectors"]):
        return "store"

    # Model / types
    if any(x in path_lower for x in ["model", "entity", "schema", "/types", "/interfaces", "dto"]):
        return "model"

    # Middleware
    if "middleware" in path_lower:
        return "middleware"

    # API client
    if any(x in path_lower for x in ["/api/", "client", "sdk", "http"]):
        return "api"

    # React component: TSX/JSX file with capitalized export
    if ext in (".tsx", ".jsx"):
        capitalized = [e for e in contract.exports if e.name[:1].isupper()]
        if capitalized:
            return "component"

    # Util / helper
    if any(x in path_lower for x in ["util", "helper", "lib/", "common/", "shared/"]):
        return "util"

    return "util"


# ---------------------------------------------------------------------------
# AstExtractor public class
# ---------------------------------------------------------------------------

class AstExtractor:
    """Extract FileContracts from source files using AST parsing."""

    def __init__(self, max_file_size: int = _MAX_FILE_SIZE) -> None:
        self.max_file_size = max_file_size
        self._ts_checked = False
        self._ts_ok = False

    def _ensure_ts(self) -> bool:
        if not self._ts_checked:
            self._ts_ok = _init_tree_sitter()
            self._ts_checked = True
        return self._ts_ok

    def extract(self, path: Path, root: Optional[Path] = None) -> Optional[FileContract]:
        ext = path.suffix.lower()
        language = _LANGUAGE_MAP.get(ext)
        if language is None:
            return None

        try:
            stat = path.stat()
            if stat.st_size > self.max_file_size:
                rel = str(path.relative_to(root)) if root else path.name
                return FileContract(
                    path=rel,
                    language=language,
                    extraction_method="heuristic",
                    limitations=[f"file_too_large: {stat.st_size} bytes > {self.max_file_size}"],
                )
        except OSError:
            return None

        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

        rel_path = str(path.relative_to(root)).replace("\\", "/") if root else path.name

        if language == "python":
            contract = _extract_python(rel_path, source)
        else:
            if self._ensure_ts():
                lang_obj = _get_ts_lang(language)
                if lang_obj is not None:
                    contract = _extract_ts_js_tree_sitter(rel_path, source, lang_obj, language)
                else:
                    contract = _extract_ts_js_heuristic(rel_path, source, language)
            else:
                contract = _extract_ts_js_heuristic(rel_path, source, language)

        contract.role = _detect_role(rel_path, contract)
        return contract

    def has_tree_sitter(self) -> bool:
        return self._ensure_ts()


def _get_ts_lang(language: str) -> Any:
    if language in ("typescript",):
        return _TS_LANG
    if language in ("tsx",):
        return _TSX_LANG or _TS_LANG
    if language in ("javascript", "jsx"):
        return _JS_LANG
    return None

from __future__ import annotations

"""Tests for ast_extractor: Python (ast) and TS/JS (heuristic) extraction."""

from pathlib import Path

import pytest

from sourcecode.ast_extractor import AstExtractor, _extract_python, _extract_ts_js_heuristic


# ---------------------------------------------------------------------------
# Python extraction
# ---------------------------------------------------------------------------

def test_python_extracts_function_signatures(tmp_path: Path) -> None:
    (tmp_path / "svc.py").write_text(
        "from typing import Optional\n"
        "async def fetch(url: str, timeout: int = 30) -> Optional[str]:\n"
        "    pass\n"
    )
    extractor = AstExtractor()
    c = extractor.extract(tmp_path / "svc.py")
    assert c is not None
    assert c.language == "python"
    assert c.extraction_method == "ast"
    fn = next((f for f in c.functions if f.name == "fetch"), None)
    assert fn is not None
    assert fn.async_ is True
    assert "url: str" in fn.signature
    assert "timeout: int = 30" in fn.signature
    assert "-> Optional[str]" in fn.signature


def test_python_extracts_class_as_type(tmp_path: Path) -> None:
    (tmp_path / "model.py").write_text(
        "class User:\n"
        "    name: str\n"
        "    age: int\n"
        "    active: bool = True\n"
    )
    extractor = AstExtractor()
    c = extractor.extract(tmp_path / "model.py")
    assert c is not None
    assert any(t.name == "User" for t in c.types)
    user_type = next(t for t in c.types if t.name == "User")
    assert user_type.kind == "class"
    field_names = [f.name for f in user_type.fields]
    assert "name" in field_names
    assert "age" in field_names


def test_python_respects_all_exports(tmp_path: Path) -> None:
    (tmp_path / "mod.py").write_text(
        '__all__ = ["public_fn"]\n'
        "def public_fn(): pass\n"
        "def _private(): pass\n"
    )
    extractor = AstExtractor()
    c = extractor.extract(tmp_path / "mod.py")
    assert c is not None
    export_names = {e.name for e in c.exports}
    assert "public_fn" in export_names
    assert "_private" not in export_names


def test_python_extracts_imports(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(
        "from pathlib import Path\n"
        "import json\n"
        "from sourcecode.schema import SourceMap, EntryPoint\n"
    )
    extractor = AstExtractor()
    c = extractor.extract(tmp_path / "app.py")
    assert c is not None
    sources = {i.source for i in c.imports}
    # stdlib imports (pathlib, json) are filtered — they add noise without signal
    assert "pathlib" not in sources
    assert "json" not in sources
    # non-stdlib imports are kept
    assert "sourcecode.schema" in sources
    sc_imp = next(i for i in c.imports if i.source == "sourcecode.schema")
    assert "SourceMap" in sc_imp.symbols
    assert "EntryPoint" in sc_imp.symbols


def test_python_syntax_error_returns_limitation(tmp_path: Path) -> None:
    (tmp_path / "broken.py").write_text("def foo(:\n")
    extractor = AstExtractor()
    c = extractor.extract(tmp_path / "broken.py")
    assert c is not None
    assert any("syntax_error" in lim for lim in c.limitations)


def test_python_skips_private_functions_without_all(tmp_path: Path) -> None:
    (tmp_path / "util.py").write_text(
        "def public(): pass\n"
        "def _helper(): pass\n"
        "def __dunder__(): pass\n"
    )
    extractor = AstExtractor()
    c = extractor.extract(tmp_path / "util.py")
    assert c is not None
    fn_names = {f.name for f in c.functions}
    assert "public" in fn_names
    assert "_helper" not in fn_names


def test_python_signature_truncated_for_long_defaults(tmp_path: Path) -> None:
    long_default = "x" * 200
    (tmp_path / "long.py").write_text(
        f'def fn(a: str = "{long_default}"): pass\n'
    )
    extractor = AstExtractor()
    c = extractor.extract(tmp_path / "long.py")
    assert c is not None
    fn = next((f for f in c.functions if f.name == "fn"), None)
    assert fn is not None
    assert len(fn.signature) <= 303  # 300 + "..."


# ---------------------------------------------------------------------------
# Heuristic TS/JS extraction
# ---------------------------------------------------------------------------

def test_heuristic_ts_extracts_named_imports(tmp_path: Path) -> None:
    (tmp_path / "comp.ts").write_text(
        'import { useState, useEffect } from "react";\n'
        'import type { FC } from "react";\n'
        'import axios from "axios";\n'
        'import * as fs from "fs";\n'
    )
    extractor = AstExtractor()
    c = extractor.extract(tmp_path / "comp.ts")
    assert c is not None
    by_source = {i.source: i for i in c.imports}
    assert "react" in by_source
    assert "useState" in by_source["react"].symbols
    assert "useEffect" in by_source["react"].symbols
    assert "axios" in by_source
    assert by_source["axios"].kind == "default"
    assert "fs" in by_source
    assert by_source["fs"].kind == "namespace"


def test_heuristic_ts_extracts_exports(tmp_path: Path) -> None:
    (tmp_path / "svc.ts").write_text(
        "export interface Config { url: string; }\n"
        "export async function fetch(url: string): Promise<string> { return url; }\n"
        "export const PI = 3.14;\n"
        "export default class MyService {}\n"
    )
    extractor = AstExtractor()
    c = extractor.extract(tmp_path / "svc.ts")
    assert c is not None
    export_names = {e.name for e in c.exports}
    assert "Config" in export_names
    assert "fetch" in export_names
    assert "PI" in export_names
    fn = next((f for f in c.functions if f.name == "fetch"), None)
    assert fn is not None
    assert fn.async_ is True


def test_heuristic_ts_extracts_interface_fields(tmp_path: Path) -> None:
    (tmp_path / "types.ts").write_text(
        "export interface ButtonProps {\n"
        "  variant: 'primary' | 'secondary';\n"
        "  onClick?: () => void;\n"
        "  label: string;\n"
        "}\n"
    )
    extractor = AstExtractor()
    c = extractor.extract(tmp_path / "types.ts")
    assert c is not None
    assert any(t.name == "ButtonProps" for t in c.types)
    btn = next(t for t in c.types if t.name == "ButtonProps")
    assert btn.kind == "interface"
    field_names = {f.name for f in btn.fields}
    assert "variant" in field_names
    assert "label" in field_names
    # onClick is optional
    onclick = next((f for f in btn.fields if f.name == "onClick"), None)
    assert onclick is not None
    assert onclick.required is False


def test_heuristic_detects_react_hooks(tmp_path: Path) -> None:
    (tmp_path / "comp.tsx").write_text(
        'import { useState, useCallback } from "react";\n'
        "export function Counter() {\n"
        "  const [count, setCount] = useState(0);\n"
        "  const inc = useCallback(() => setCount(c => c + 1), []);\n"
        "  return count;\n"
        "}\n"
    )
    extractor = AstExtractor()
    c = extractor.extract(tmp_path / "comp.tsx")
    assert c is not None
    assert "useState" in c.hooks_used
    assert "useCallback" in c.hooks_used


def test_heuristic_external_deps_only(tmp_path: Path) -> None:
    (tmp_path / "mod.ts").write_text(
        'import { A } from "./local";\n'
        'import { B } from "../other";\n'
        'import axios from "axios";\n'
        'import { z } from "zod";\n'
    )
    extractor = AstExtractor()
    c = extractor.extract(tmp_path / "mod.ts")
    assert c is not None
    # Only external deps (no ./ or ../)
    assert "axios" in c.dependencies
    assert "zod" in c.dependencies
    assert "./local" not in c.dependencies
    assert "../other" not in c.dependencies


# ---------------------------------------------------------------------------
# Role detection
# ---------------------------------------------------------------------------

def test_role_hook_detected_by_filename(tmp_path: Path) -> None:
    (tmp_path / "useAuth.ts").write_text(
        'import { useState } from "react";\n'
        "export function useAuth() { return useState(null); }\n"
    )
    c = AstExtractor().extract(tmp_path / "useAuth.ts")
    assert c is not None
    assert c.role == "hook"


def test_role_entrypoint_detected(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("def main(): pass\n")
    c = AstExtractor().extract(tmp_path / "main.py")
    assert c is not None
    assert c.role == "entrypoint"


def test_role_service_detected_by_path(tmp_path: Path) -> None:
    svc_dir = tmp_path / "src" / "services"
    svc_dir.mkdir(parents=True)
    (svc_dir / "user.ts").write_text("export class UserService {}\n")
    c = AstExtractor().extract(svc_dir / "user.ts", tmp_path)
    assert c is not None
    assert c.role == "service"


def test_role_component_detected_tsx(tmp_path: Path) -> None:
    (tmp_path / "Button.tsx").write_text(
        "export default function Button({ label }: { label: string }) {\n"
        "  return label;\n"
        "}\n"
    )
    c = AstExtractor().extract(tmp_path / "Button.tsx")
    assert c is not None
    assert c.role == "component"


# ---------------------------------------------------------------------------
# File limits
# ---------------------------------------------------------------------------

def test_large_file_returns_limitation(tmp_path: Path) -> None:
    big = tmp_path / "big.py"
    big.write_bytes(b"x = 1\n" * 50_000)  # ~300KB
    extractor = AstExtractor(max_file_size=100_000)
    c = extractor.extract(big)
    assert c is not None
    assert any("file_too_large" in lim for lim in c.limitations)


def test_unknown_extension_returns_none(tmp_path: Path) -> None:
    (tmp_path / "file.cpp").write_text("int main() { return 0; }\n")
    c = AstExtractor().extract(tmp_path / "file.cpp")
    assert c is None

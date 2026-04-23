from __future__ import annotations

"""Tests for SemanticAnalyzer JS/TS layer — SEM-NODE-01..06.

Wave 0 for plan 12-03: these tests are written in RED before implementing
_extract_js_imports, _detect_js_calls, _analyze_js_file, _resolve_js_module_path.

Tests cover:
  SEM-NODE-01 — Named imports extracted
  SEM-NODE-02 — Default import + call resolution
  SEM-NODE-03 — Namespace import
  SEM-NODE-04 — CommonJS require()
  SEM-NODE-05 — Keyword exclusion (if, for, console -> not in calls)
  SEM-NODE-06 — Basic dataflow: args captured textually
"""

import textwrap
from pathlib import Path

import pytest

from sourcecode.semantic_analyzer import SemanticAnalyzer, _JS_KEYWORD_EXCLUSIONS


# ---------------------------------------------------------------------------
# SEM-NODE-01: Named imports extracted
# ---------------------------------------------------------------------------

def test_named_imports_extracted():
    """SEM-NODE-01: _extract_js_imports extracts named binding from ES6 import.

    Given: import { greet } from './module'
    Expected: bindings == {"greet": ("./module", "greet")}
    """
    analyzer = SemanticAnalyzer()
    consumer_content = "import { greet } from './module';\ngreet();\n"
    bindings = analyzer._extract_js_imports(consumer_content, "consumer.js")

    assert "greet" in bindings, f"Expected 'greet' in bindings, got: {bindings}"
    assert bindings["greet"] == ("./module", "greet"), (
        f"Expected ('.'./module', 'greet'), got: {bindings['greet']}"
    )


def test_multiple_named_imports_extracted():
    """SEM-NODE-01 extended: multiple named bindings from one import statement."""
    analyzer = SemanticAnalyzer()
    content = "import { Foo, Bar } from './module';\n"
    bindings = analyzer._extract_js_imports(content, "consumer.js")

    assert bindings.get("Foo") == ("./module", "Foo")
    assert bindings.get("Bar") == ("./module", "Bar")


# ---------------------------------------------------------------------------
# SEM-NODE-02: Default import + call resolution via analyze()
# ---------------------------------------------------------------------------

def test_default_import(tmp_path: Path):
    """SEM-NODE-02: Default import recognized; analyze() produces CallRecord.

    utils.js:  export default function format() {}
    caller.js: import format from './utils'; ... format(data);
    analyze() must produce a CallRecord with callee_symbol='format',
    callee_path='utils.js', method='heuristic'.
    """
    utils_js = tmp_path / "utils.js"
    utils_js.write_text(
        "export default function format() { return ''; }\n",
        encoding="utf-8",
    )

    caller_js = tmp_path / "caller.js"
    caller_js.write_text(
        textwrap.dedent("""\
            import format from './utils';
            function run(data) {
                format(data);
            }
        """),
        encoding="utf-8",
    )

    file_tree = {"utils.js": None, "caller.js": None}
    analyzer = SemanticAnalyzer()
    calls, symbols, links, summary = analyzer.analyze(tmp_path, file_tree)

    matching = [c for c in calls if c.callee_symbol == "format"]
    assert len(matching) >= 1, (
        f"Expected CallRecord for 'format', got calls={calls}"
    )
    cr = matching[0]
    assert cr.callee_path == "utils.js", f"Expected callee_path='utils.js', got '{cr.callee_path}'"
    assert cr.method == "heuristic"


def test_default_import_binding():
    """SEM-NODE-02: _extract_js_imports returns 'default' marker for default import."""
    analyzer = SemanticAnalyzer()
    content = "import format from './utils';\n"
    bindings = analyzer._extract_js_imports(content, "caller.js")

    assert "format" in bindings
    assert bindings["format"] == ("./utils", "default")


# ---------------------------------------------------------------------------
# SEM-NODE-03: Namespace import
# ---------------------------------------------------------------------------

def test_namespace_import_binding():
    """SEM-NODE-03: import * as ns from './helper' -> binding {"ns": ("./helper", "*")}."""
    analyzer = SemanticAnalyzer()
    content = "import * as h from './helper';\nh.calc();\n"
    bindings = analyzer._extract_js_imports(content, "main.js")

    assert "h" in bindings, f"Expected 'h' in bindings, got: {bindings}"
    assert bindings["h"] == ("./helper", "*")


def test_namespace_call_produces_record(tmp_path: Path):
    """SEM-NODE-03 full: analyze() produces CallRecord for namespace member call h.calc().

    helper.js: export function calc() {}
    main.js:   import * as h from './helper'; ... h.calc();
    """
    helper_js = tmp_path / "helper.js"
    helper_js.write_text("export function calc() { return 0; }\n", encoding="utf-8")

    main_js = tmp_path / "main.js"
    main_js.write_text(
        textwrap.dedent("""\
            import * as h from './helper';
            function run() {
                h.calc();
            }
        """),
        encoding="utf-8",
    )

    file_tree = {"helper.js": None, "main.js": None}
    analyzer = SemanticAnalyzer()
    calls, symbols, links, summary = analyzer.analyze(tmp_path, file_tree)

    matching = [c for c in calls if c.callee_symbol == "calc"]
    assert len(matching) >= 1, (
        f"Expected CallRecord for 'calc' via namespace, got calls={calls}"
    )
    cr = matching[0]
    assert cr.method == "heuristic"
    assert cr.confidence == "medium"


# ---------------------------------------------------------------------------
# SEM-NODE-04: CommonJS require()
# ---------------------------------------------------------------------------

def test_cjs_require_binding():
    """SEM-NODE-04: const { parse } = require('./lib') -> binding {"parse": ("./lib", "parse")}."""
    analyzer = SemanticAnalyzer()
    content = "const { parse } = require('./lib');\nparse(input);\n"
    bindings = analyzer._extract_js_imports(content, "app.js")

    assert "parse" in bindings, f"Expected 'parse' in CJS bindings, got: {bindings}"
    assert bindings["parse"] == ("./lib", "parse")


def test_cjs_require_plain_binding():
    """SEM-NODE-04 extended: const foo = require('./mod') -> binding {"foo": ("./mod", "default")}."""
    analyzer = SemanticAnalyzer()
    content = "const foo = require('./mod');\n"
    bindings = analyzer._extract_js_imports(content, "app.js")

    assert "foo" in bindings
    assert bindings["foo"] == ("./mod", "default")


def test_cjs_require_produces_call(tmp_path: Path):
    """SEM-NODE-04 full: analyze() produces CallRecord when CJS require binding is called.

    lib.js: function parse() {} module.exports = { parse };
    app.js: const { parse } = require('./lib'); ... parse(input);
    """
    lib_js = tmp_path / "lib.js"
    lib_js.write_text(
        "function parse() { return {}; }\nmodule.exports = { parse };\n",
        encoding="utf-8",
    )

    app_js = tmp_path / "app.js"
    app_js.write_text(
        textwrap.dedent("""\
            const { parse } = require('./lib');
            function main(input) {
                parse(input);
            }
        """),
        encoding="utf-8",
    )

    file_tree = {"lib.js": None, "app.js": None}
    analyzer = SemanticAnalyzer()
    calls, symbols, links, summary = analyzer.analyze(tmp_path, file_tree)

    matching = [c for c in calls if c.callee_symbol == "parse"]
    assert len(matching) >= 1, (
        f"Expected CallRecord for 'parse' from CJS require, got calls={calls}"
    )
    cr = matching[0]
    assert cr.callee_path == "lib.js"


# ---------------------------------------------------------------------------
# SEM-NODE-05: Keyword exclusion
# ---------------------------------------------------------------------------

def test_keyword_exclusion_constants():
    """SEM-NODE-05: _JS_KEYWORD_EXCLUSIONS contains JS reserved words and common builtins."""
    assert "if" in _JS_KEYWORD_EXCLUSIONS
    assert "for" in _JS_KEYWORD_EXCLUSIONS
    assert "while" in _JS_KEYWORD_EXCLUSIONS
    assert "switch" in _JS_KEYWORD_EXCLUSIONS
    assert "catch" in _JS_KEYWORD_EXCLUSIONS
    assert "return" in _JS_KEYWORD_EXCLUSIONS
    assert "typeof" in _JS_KEYWORD_EXCLUSIONS
    assert "instanceof" in _JS_KEYWORD_EXCLUSIONS
    assert "new" in _JS_KEYWORD_EXCLUSIONS
    assert "await" in _JS_KEYWORD_EXCLUSIONS
    assert "yield" in _JS_KEYWORD_EXCLUSIONS
    assert "console" in _JS_KEYWORD_EXCLUSIONS
    assert "Promise" in _JS_KEYWORD_EXCLUSIONS
    assert isinstance(_JS_KEYWORD_EXCLUSIONS, frozenset)


def test_keyword_exclusion_no_call_records(tmp_path: Path):
    """SEM-NODE-05: analyze() does NOT produce CallRecord for JS keywords or builtins.

    caller.js with if(, for(, while(, console.log( -> zero call records for those names.
    """
    caller_js = tmp_path / "caller.js"
    caller_js.write_text(
        textwrap.dedent("""\
            function doStuff(condition) {
                if (condition) {
                    for (let i = 0; i < 10; i++) {
                        console.log(i);
                    }
                }
                while (condition) {
                    console.log("loop");
                }
            }
        """),
        encoding="utf-8",
    )

    file_tree = {"caller.js": None}
    analyzer = SemanticAnalyzer()
    calls, symbols, links, summary = analyzer.analyze(tmp_path, file_tree)

    excluded_symbols = {"if", "for", "while", "console"}
    bad_calls = [c for c in calls if c.callee_symbol in excluded_symbols]
    assert len(bad_calls) == 0, (
        f"Expected no CallRecord for keywords/builtins, got: {bad_calls}"
    )


# ---------------------------------------------------------------------------
# SEM-NODE-06: Basic dataflow — args capture
# ---------------------------------------------------------------------------

def test_basic_dataflow_args(tmp_path: Path):
    """SEM-NODE-06: CallRecord.args captures simple identifier arguments textually.

    api.js:    export function processData(input, options) {}
    caller.js: import { processData } from './api'; processData(myData, { timeout: 30 });
    CallRecord.args should contain 'myData' as first element.
    String literals should be replaced with '<string_literal>'.
    """
    api_js = tmp_path / "api.js"
    api_js.write_text(
        "export function processData(input, options) { return null; }\n",
        encoding="utf-8",
    )

    caller_js = tmp_path / "caller.js"
    caller_js.write_text(
        textwrap.dedent("""\
            import { processData } from './api';
            function run(myData) {
                processData(myData, { timeout: 30 });
            }
        """),
        encoding="utf-8",
    )

    file_tree = {"api.js": None, "caller.js": None}
    analyzer = SemanticAnalyzer()
    calls, symbols, links, summary = analyzer.analyze(tmp_path, file_tree)

    matching = [c for c in calls if c.callee_symbol == "processData"]
    assert len(matching) >= 1, (
        f"Expected CallRecord for 'processData', got calls={calls}"
    )
    cr = matching[0]
    assert isinstance(cr.args, list), f"Expected args to be a list, got: {type(cr.args)}"
    # First argument should be the identifier 'myData' (simple identifier captured textually)
    assert len(cr.args) >= 1, f"Expected at least one captured arg, got: {cr.args}"
    assert cr.args[0] == "myData", f"Expected first arg='myData', got: {cr.args[0]}"


def test_string_literal_in_args_redacted(tmp_path: Path):
    """SEM-NODE-06 security: string literals in args replaced with '<string_literal>'.

    caller.js: processData("secret_key") -> args[0] == '<string_literal>' (not the actual string).
    """
    api_js = tmp_path / "api.js"
    api_js.write_text(
        "export function processData(input) { return null; }\n",
        encoding="utf-8",
    )

    caller_js = tmp_path / "caller.js"
    caller_js.write_text(
        textwrap.dedent("""\
            import { processData } from './api';
            function run() {
                processData("secret_key");
            }
        """),
        encoding="utf-8",
    )

    file_tree = {"api.js": None, "caller.js": None}
    analyzer = SemanticAnalyzer()
    calls, symbols, links, summary = analyzer.analyze(tmp_path, file_tree)

    matching = [c for c in calls if c.callee_symbol == "processData"]
    assert len(matching) >= 1, f"Expected CallRecord for 'processData', got calls={calls}"
    cr = matching[0]
    assert len(cr.args) >= 1
    # String literals must NOT leak their content
    assert cr.args[0] == "<string_literal>", (
        f"Expected '<string_literal>' for string arg, got: {cr.args[0]!r}"
    )


# ---------------------------------------------------------------------------
# SEM-NODE: TypeScript class detection
# ---------------------------------------------------------------------------

def test_ts_class_detection(tmp_path: Path):
    """SEM-NODE: TypeScript class recognized as SymbolRecord(kind='class').

    service.ts: export class UserService { constructor(private db: Database) {} }
    analyze() must produce SymbolRecord(symbol='UserService', kind='class', language='typescript').
    """
    service_ts = tmp_path / "service.ts"
    service_ts.write_text(
        textwrap.dedent("""\
            export class UserService {
                constructor(private db: Database) {}

                async getUser(id: string): Promise<User> {
                    return this.db.find(id);
                }
            }
        """),
        encoding="utf-8",
    )

    file_tree = {"service.ts": None}
    analyzer = SemanticAnalyzer()
    calls, symbols, links, summary = analyzer.analyze(tmp_path, file_tree)

    class_symbols = [s for s in symbols if s.kind == "class" and s.symbol == "UserService"]
    assert len(class_symbols) >= 1, (
        f"Expected SymbolRecord for UserService class, got symbols={symbols}"
    )
    sr = class_symbols[0]
    assert sr.language == "typescript"
    assert sr.path == "service.ts"


# ---------------------------------------------------------------------------
# SEM-NODE: language_coverage includes nodejs = heuristic
# ---------------------------------------------------------------------------

def test_js_language_coverage(tmp_path: Path):
    """analyze() sets language_coverage['nodejs'] = 'heuristic' when JS/TS files present."""
    js_file = tmp_path / "index.js"
    js_file.write_text("function hello() { return 'world'; }\n", encoding="utf-8")

    file_tree = {"index.js": None}
    analyzer = SemanticAnalyzer()
    calls, symbols, links, summary = analyzer.analyze(tmp_path, file_tree)

    assert "nodejs" in summary.language_coverage, (
        f"Expected 'nodejs' in language_coverage, got: {summary.language_coverage}"
    )
    assert summary.language_coverage["nodejs"] == "heuristic"


# ---------------------------------------------------------------------------
# SEM-NODE: External imports produce SymbolLink with is_external=True
# ---------------------------------------------------------------------------

def test_external_import_produces_external_link(tmp_path: Path):
    """External npm imports produce SymbolLink(is_external=True)."""
    js_file = tmp_path / "app.js"
    js_file.write_text(
        "import React from 'react';\nimport { useState } from 'react';\n",
        encoding="utf-8",
    )

    file_tree = {"app.js": None}
    analyzer = SemanticAnalyzer()
    calls, symbols, links, summary = analyzer.analyze(tmp_path, file_tree)

    external_links = [lk for lk in links if lk.is_external and lk.importer_path == "app.js"]
    assert len(external_links) >= 1, (
        f"Expected external SymbolLink for 'react' import, got links={links}"
    )


# ---------------------------------------------------------------------------
# SEM-NODE: Internal imports produce SymbolLink with is_external=False
# ---------------------------------------------------------------------------

def test_internal_import_produces_internal_link(tmp_path: Path):
    """Internal relative imports produce SymbolLink(is_external=False)."""
    utils_js = tmp_path / "utils.js"
    utils_js.write_text("export function helper() {}\n", encoding="utf-8")

    app_js = tmp_path / "app.js"
    app_js.write_text(
        "import { helper } from './utils';\nfunction run() { helper(); }\n",
        encoding="utf-8",
    )

    file_tree = {"utils.js": None, "app.js": None}
    analyzer = SemanticAnalyzer()
    calls, symbols, links, summary = analyzer.analyze(tmp_path, file_tree)

    internal_links = [
        lk for lk in links
        if not lk.is_external and lk.importer_path == "app.js" and lk.symbol == "helper"
    ]
    assert len(internal_links) >= 1, (
        f"Expected internal SymbolLink for 'helper', got links={links}"
    )


# ---------------------------------------------------------------------------
# Stub: activated from test_semantic_analyzer_python.py plan 12-03
# ---------------------------------------------------------------------------

def test_js_call_resolution(tmp_path: Path):
    """SEM-JS activated from Python stub: cross-file JS call resolution.

    caller.js imports greet from ./target; greet() is called inside a function.
    analyze() must produce CallRecord(callee_path='target.js', callee_symbol='greet').
    """
    target_js = tmp_path / "target.js"
    target_js.write_text(
        "export function greet() { return 'hello'; }\n",
        encoding="utf-8",
    )

    caller_js = tmp_path / "caller.js"
    caller_js.write_text(
        textwrap.dedent("""\
            import { greet } from './target';
            function main() {
                greet();
            }
        """),
        encoding="utf-8",
    )

    file_tree = {"target.js": None, "caller.js": None}
    analyzer = SemanticAnalyzer()
    calls, symbols, links, summary = analyzer.analyze(tmp_path, file_tree)

    matching = [
        c for c in calls
        if c.callee_symbol == "greet" and c.caller_path == "caller.js"
    ]
    assert len(matching) >= 1, (
        f"Expected CallRecord for 'greet' cross-file, got calls={calls}"
    )
    cr = matching[0]
    assert cr.callee_path == "target.js"
    assert cr.method == "heuristic"

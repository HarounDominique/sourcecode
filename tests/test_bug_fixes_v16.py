"""Regression tests for v1.6.0 bug fixes.

Bug 1 — write_output writes UTF-8 (not locale encoding)
Bug 2 — Java DDD scan depth uses 12, Mapper.xml at depth 10 found
Bug 3 — --changed-only wired to git uncommitted_changes, not empty
Bug 4 — fix-bug ranks by signals (uncommitted/recency/annotations), not filename keywords
"""
from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Bug 1 — UTF-8 round-trip via write_output
# ---------------------------------------------------------------------------

def test_write_output_utf8_roundtrip(tmp_path: Path) -> None:
    """write_output must produce UTF-8 without BOM for non-ASCII content."""
    from sourcecode.serializer import write_output

    content = '{"name": "señor", "value": "données"}'
    out_file = tmp_path / "output.json"
    write_output(content, output=out_file)

    raw = out_file.read_bytes()
    # No UTF-16 BOM (FF FE or FE FF)
    assert not raw.startswith(b"\xff\xfe"), "UTF-16 LE BOM detected — encoding bug not fixed"
    assert not raw.startswith(b"\xfe\xff"), "UTF-16 BE BOM detected"
    # No UTF-8 BOM
    assert not raw.startswith(b"\xef\xbb\xbf"), "UTF-8 BOM should not be present"
    # Correct round-trip
    decoded = out_file.read_text(encoding="utf-8")
    assert "señor" in decoded
    assert "données" in decoded



# ---------------------------------------------------------------------------
# Bug 2 — Java DDD scan reaches depth 10 (Mapper.xml found)
# ---------------------------------------------------------------------------

def _make_java_ddd_tree(root: Path) -> None:
    """Create a fake Java DDD layout with a Mapper.xml at depth 10."""
    # pom.xml at root
    (root / "pom.xml").write_text("<project/>")
    # Deep path: src/main/java/com/example/app/ddd/order/infrastructure/repository/
    deep = (
        root
        / "src" / "main" / "java" / "com" / "example" / "app"
        / "ddd" / "order" / "infrastructure" / "repository"
    )
    deep.mkdir(parents=True)
    (deep / "OrderMapper.xml").write_text("<mapper/>")
    # A shallow Java file to ensure basic detection works
    shallow = root / "src" / "main" / "java" / "com" / "example" / "app"
    (shallow / "Application.java").write_text("public class Application {}")


def test_java_ddd_mapper_xml_at_depth_10(tmp_path: Path) -> None:
    """AdaptiveScanner with base_depth=12 must find Mapper.xml at depth 10."""
    from sourcecode.adaptive_scanner import AdaptiveScanner
    from sourcecode.repo_classifier import RepoClassifier
    from sourcecode.tree_utils import flatten_file_tree

    _make_java_ddd_tree(tmp_path)

    topology = RepoClassifier().classify(tmp_path)
    scanner = AdaptiveScanner(tmp_path, topology=topology, base_depth=12)
    tree = scanner.scan_tree()
    all_paths = [p.replace("\\", "/") for p in flatten_file_tree(tree)]

    mapper_paths = [p for p in all_paths if p.endswith("Mapper.xml")]
    assert mapper_paths, (
        f"OrderMapper.xml not found at depth 10. Depth 12 scan produced {len(all_paths)} files: "
        f"{all_paths[:20]}"
    )


def test_java_ddd_depth8_misses_mapper(tmp_path: Path) -> None:
    """Sanity: base_depth=8 should NOT reach depth-10 Mapper.xml (confirms the bug existed)."""
    from sourcecode.adaptive_scanner import AdaptiveScanner
    from sourcecode.repo_classifier import RepoClassifier
    from sourcecode.tree_utils import flatten_file_tree

    _make_java_ddd_tree(tmp_path)

    topology = RepoClassifier().classify(tmp_path)
    scanner = AdaptiveScanner(tmp_path, topology=topology, base_depth=8)
    tree = scanner.scan_tree()
    all_paths = [p.replace("\\", "/") for p in flatten_file_tree(tree)]

    mapper_paths = [p for p in all_paths if p.endswith("Mapper.xml")]
    # depth 8 from root cannot reach depth-10 file — confirms the fix was needed
    assert not mapper_paths, "depth=8 should not reach depth-10 Mapper.xml"


# ---------------------------------------------------------------------------
# Bug 3 — --changed-only uses allowed_changed_files, not empty
# ---------------------------------------------------------------------------

def test_contract_pipeline_allowed_changed_files(tmp_path: Path) -> None:
    """ContractPipeline.run() with allowed_changed_files returns only those contracts."""
    from sourcecode.contract_pipeline import ContractPipeline

    # Create 3 changed files and 2 unchanged files
    changed = ["src/a.py", "src/b.py", "src/c.py"]
    unchanged = ["src/d.py", "src/e.py"]
    all_files = changed + unchanged

    for f in all_files:
        p = tmp_path / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"def func_{Path(f).stem}(): pass\n")

    cp = ContractPipeline(max_files=100)
    contracts, summary = cp.run(
        tmp_path,
        all_files,
        changed_only=True,
        allowed_changed_files=set(changed),
    )

    contract_paths = {c.path for c in contracts}
    for ch in changed:
        assert ch in contract_paths, f"Changed file {ch} missing from contracts"
    for un in unchanged:
        assert un not in contract_paths, f"Unchanged file {un} should not be in contracts"


def test_contract_pipeline_changed_only_empty_falls_through(tmp_path: Path) -> None:
    """When allowed_changed_files is empty set, contracts should be empty (no silent full-scan)."""
    from sourcecode.contract_pipeline import ContractPipeline

    files = ["src/a.py", "src/b.py"]
    for f in files:
        p = tmp_path / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("def foo(): pass\n")

    cp = ContractPipeline(max_files=100)
    contracts, _ = cp.run(
        tmp_path,
        files,
        changed_only=True,
        allowed_changed_files=set(),  # empty → no files match
    )
    assert contracts == [], "Empty allowed_changed_files should produce no contracts"


# ---------------------------------------------------------------------------
# Bug 4 — fix-bug ranks Java (changed + annotated) above TypeScript (unchanged)
# ---------------------------------------------------------------------------

def _make_note(kind: str, path: str) -> SimpleNamespace:
    return SimpleNamespace(kind=kind, path=path)


def test_fix_bug_signal_ranker_java_above_typescript(tmp_path: Path) -> None:
    """fix-bug: Java files with uncommitted changes rank above unrelated TS files.

    Uses flat paths so RankingEngine depth penalties don't interfere with the
    signal-comparison. Asserts the changed+annotated Java file gets a higher
    score than the unchanged TS 'error-handler' files that the old keyword
    scorer incorrectly elevated.
    """
    from sourcecode.prepare_context import TaskContextBuilder, TASKS

    # Flat paths — avoids deep-path penalties in RankingEngine
    java_files = [
        "OrderService.java",
        "OrderController.java",
        "OrderRepository.java",
        "PaymentService.java",
        "UserService.java",
    ]
    ts_files = [
        "error-interceptor.ts",
        "error-handler.ts",
        "error.service.ts",
    ]
    for f in java_files + ts_files:
        (tmp_path / f).write_text("// placeholder\n")

    # 3 Java files uncommitted (dominant stack = java)
    uncommitted = {java_files[0], java_files[1], java_files[2]}
    # PaymentService has a FIXME annotation
    code_notes = [_make_note("FIXME", java_files[3])]
    git_hotspots = {java_files[0]: 6, java_files[1]: 4, java_files[2]: 2}

    builder = TaskContextBuilder(tmp_path)
    spec = TASKS["fix-bug"]

    relevant = builder._rank_files(
        "fix-bug", spec,
        java_files + ts_files,
        set(), set(),
        git_hotspots=git_hotspots,
        uncommitted_files=uncommitted,
        code_notes=code_notes,
    )

    assert relevant, "fix-bug should return files"

    score_map = {r.path: r.score for r in relevant}

    # Changed Java files must outrank unchanged TS error-handler files
    for j in (java_files[0], java_files[1]):  # changed + committed
        for t in ts_files:
            if j in score_map and t in score_map:
                assert score_map[j] >= score_map[t], (
                    f"Changed Java {j} ({score_map[j]}) should score >= "
                    f"unchanged TS {t} ({score_map[t]}). "
                    f"Full: {score_map}"
                )


def test_fix_bug_why_field_populated(tmp_path: Path) -> None:
    """fix-bug: RelevantFile.why contains signal descriptions, not empty."""
    from sourcecode.prepare_context import TaskContextBuilder, TASKS

    f = "BuggyService.java"
    (tmp_path / f).write_text("// code\n")

    builder = TaskContextBuilder(tmp_path)
    spec = TASKS["fix-bug"]

    relevant = builder._rank_files(
        "fix-bug",
        spec,
        [f],
        set(),
        set(),
        uncommitted_files={f},
        code_notes=[_make_note("FIXME", f)],
        git_hotspots={f: 4},
    )

    assert relevant, "Should have at least one result"
    top = relevant[0]
    assert "uncommitted change" in top.why, f"Expected signal in why, got: {top.why!r}"
    assert "FIXME" in top.why or "annotation" in top.why, (
        f"Expected FIXME signal in why, got: {top.why!r}"
    )


# ---------------------------------------------------------------------------
# Bug 5 — --changed-only always includes security-const files
# ---------------------------------------------------------------------------

def test_contract_pipeline_changed_only_always_includes_const_files(tmp_path: Path) -> None:
    """--changed-only must include *Const.java files regardless of diff status.

    These are read-only reference anchors: included in the analysis to resolve
    constant references but not marked as is_changed (not part of diff output).
    """
    from sourcecode.contract_pipeline import ContractPipeline

    changed = ["src/OrderService.java"]
    const_file = "src/security/SeguridadRecursosConst.java"
    all_files = changed + [const_file]

    for f in all_files:
        p = tmp_path / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("public class Placeholder {}")

    cp = ContractPipeline(max_files=100)
    contracts, _ = cp.run(
        tmp_path,
        all_files,
        changed_only=True,
        allowed_changed_files=set(changed),
    )

    contract_paths = {c.path for c in contracts}
    assert const_file in contract_paths, (
        "Const file must be always-included even when absent from diff"
    )
    # Read-only anchor — must NOT be marked as changed
    const_contract = next(c for c in contracts if c.path == const_file)
    assert not const_contract.is_changed, "Const file is read-only anchor: is_changed must be False"


def test_changed_only_resource_names_unresolved_le5(tmp_path: Path) -> None:
    """With --changed-only active, resource_names_unresolved must be <= 5.

    The always-include logic ensures *Const.java files remain in file_paths for
    constant resolution even when they are absent from the git diff.
    """
    from sourcecode.serializer import _security_surface_from_eps

    # Create the const file on disk (not in the git diff)
    const_dir = tmp_path / "src" / "security"
    const_dir.mkdir(parents=True)
    (const_dir / "SeguridadRecursosConst.java").write_text(
        "public class SeguridadRecursosConst {\n"
        '    public static final String REC_PEDIDOS = "PEDIDOS";\n'
        '    public static final String REC_USUARIOS = "USUARIOS";\n'
        '    public static final String REC_FACTURAS = "FACTURAS";\n'
        "}\n"
    )
    rel_const = "src/security/SeguridadRecursosConst.java"

    class _MockEP:
        def __init__(self, evidence: str) -> None:
            self.evidence = evidence

    eps = [
        _MockEP('nombreRecurso="SeguridadRecursosConst.REC_PEDIDOS"'),
        _MockEP('nombreRecurso="SeguridadRecursosConst.REC_USUARIOS"'),
        _MockEP('nombreRecurso="SeguridadRecursosConst.REC_FACTURAS"'),
    ]

    # Baseline (--changed-only WITHOUT always-include): const file absent → unresolved
    without = _security_surface_from_eps(eps, root=tmp_path, file_paths=[])
    n_without = len((without or {}).get("resource_names_unresolved", []))
    assert n_without >= 1, "Baseline: absent const file must produce unresolved names"

    # After fix (always-include): const file in file_paths → resolved
    with_const = _security_surface_from_eps(eps, root=tmp_path, file_paths=[rel_const])
    n_with = len((with_const or {}).get("resource_names_unresolved", []))
    assert n_with <= 5, (
        f"resource_names_unresolved must be <=5 when const file is always-included, got {n_with}"
    )
    assert n_with == 0, f"All 3 constants must resolve, got {n_with} unresolved"

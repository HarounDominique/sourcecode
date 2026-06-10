"""rename_refactor.py — Safe Java class rename with full reference update.

Performs a deterministic rename of a Java class/interface/enum:
  1. Locates the source file (OldName.java or by scanning class declarations)
  2. Updates class declaration, constructor name, all imports, all references
  3. Renames the physical file on disk
  4. Returns a structured ChangeAudit report (BLOCKER-C)

Covers:
  - Class/interface/enum declaration
  - Constructor declarations
  - Import statements
  - Field type declarations
  - Method parameter and return types
  - Variable declarations and instantiations
  - extends / implements
  - Generic type parameters
  - Spring @Qualifier and @Bean names (camelCase)
  - Test files (optional via include_tests)

Does NOT require compilation. Works on any Java source tree via
regex-based text transformations with word-boundary guards.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Data classes (BLOCKER-C: structured change audit trail)
# ---------------------------------------------------------------------------

@dataclass
class FileChange:
    """A mutation applied to a single file."""
    file: str               # relative path from repo root
    intent: str             # human-readable description of what changed
    diff: str               # unified diff (--- before / +++ after)
    before_lines: list[str] = field(default_factory=list)
    after_lines: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "file": self.file,
            "intent": self.intent,
            "diff": self.diff,
            "before_lines": self.before_lines,
            "after_lines": self.after_lines,
        }


@dataclass
class RenameResult:
    """Full result of a rename-class operation."""
    old_name: str
    new_name: str
    old_file: str           # relative path before rename (empty if not found)
    new_file: str           # relative path after rename (empty if not found)
    changes: list[FileChange] = field(default_factory=list)
    files_scanned: int = 0
    files_modified: int = 0
    dry_run: bool = False
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "old_name": self.old_name,
            "new_name": self.new_name,
            "old_file": self.old_file,
            "new_file": self.new_file,
            "files_scanned": self.files_scanned,
            "files_modified": self.files_modified,
            "dry_run": self.dry_run,
            "errors": self.errors,
            "changes": [c.to_dict() for c in self.changes],
        }


# ---------------------------------------------------------------------------
# Core rename logic
# ---------------------------------------------------------------------------

_VENDOR_DIRS = frozenset({
    "vendor", "node_modules", "dist", "target", "build",
    ".gradle", ".mvn", "generated", "generated-sources",
})


def _to_camel(name: str) -> str:
    """PascalCase → camelCase: ServiceA → serviceA."""
    if not name or len(name) < 2:
        return name.lower() if name else name
    return name[0].lower() + name[1:]


def _collect_java_files(root: Path, *, include_tests: bool = True) -> list[Path]:
    """All .java files under root, excluding vendor/build dirs."""
    results: list[Path] = []
    for p in sorted(root.rglob("*.java")):
        rel = str(p.relative_to(root)).replace("\\", "/")
        parts = rel.split("/")
        if any(part in _VENDOR_DIRS for part in parts[:-1]):
            continue
        if not include_tests:
            if "/test/" in rel or "/tests/" in rel or rel.startswith("test/"):
                continue
        results.append(p)
    return results


def _find_class_file(
    java_files: list[Path],
    class_name: str,
    root: Path,
) -> Optional[Path]:
    """Find the file that declares `class_name` (by filename first, then scan)."""
    # Prefer exact filename match
    candidates = [f for f in java_files if f.stem == class_name]
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        # Multiple files with same stem — pick the one that has the class declaration
        decl_re = re.compile(
            r'\b(?:public\s+)?(?:abstract\s+)?(?:class|interface|enum)\s+' + re.escape(class_name) + r'\b'
        )
        for c in candidates:
            try:
                if decl_re.search(c.read_text(encoding="utf-8", errors="replace")):
                    return c
            except OSError:
                continue
        return candidates[0]

    # Fallback: scan file contents for class declaration
    decl_re = re.compile(
        r'\b(?:public\s+)?(?:abstract\s+)?(?:class|interface|enum)\s+' + re.escape(class_name) + r'\b'
    )
    for f in java_files:
        try:
            if decl_re.search(f.read_text(encoding="utf-8", errors="replace")):
                return f
        except OSError:
            continue
    return None


def _apply_rename(source: str, old_name: str, new_name: str) -> str:
    """Apply word-boundary replacement for class name (PascalCase and camelCase forms)."""
    # PascalCase replacement: all type references, declarations, imports
    result = re.sub(r'\b' + re.escape(old_name) + r'\b', new_name, source)

    # camelCase instance names: serviceA → serviceB (only when different from PascalCase)
    old_camel = _to_camel(old_name)
    new_camel = _to_camel(new_name)
    if old_camel != old_name and old_camel in result:
        result = re.sub(r'\b' + re.escape(old_camel) + r'\b', new_camel, result)

    return result


def _make_diff(old_text: str, new_text: str, rel_path: str) -> str:
    """Produce a unified diff string."""
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    diff_lines = list(difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=f"a/{rel_path}",
        tofile=f"b/{rel_path}",
        lineterm="",
    ))
    return "".join(diff_lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def rename_class(
    root: Path,
    old_name: str,
    new_name: str,
    *,
    dry_run: bool = False,
    include_tests: bool = True,
) -> RenameResult:
    """Rename a Java class throughout the repository.

    Args:
        root:          Absolute repo root directory.
        old_name:      Simple class name to rename (e.g. "ServiceA").
        new_name:      Target simple class name (e.g. "ServiceB").
        dry_run:       If True, compute changes but do not write any files.
        include_tests: If True (default), also rename in test files.

    Returns:
        RenameResult with structured change audit trail (BLOCKER-C format).
    """
    root = root.resolve()

    result = RenameResult(
        old_name=old_name,
        new_name=new_name,
        old_file="",
        new_file="",
        dry_run=dry_run,
    )

    # Validate input
    if not old_name or not old_name[0].isupper():
        result.errors.append(
            f"old_name '{old_name}' must be a Java class name (PascalCase, non-empty)."
        )
        return result
    if not new_name or not new_name[0].isupper():
        result.errors.append(
            f"new_name '{new_name}' must be a Java class name (PascalCase, non-empty)."
        )
        return result
    if old_name == new_name:
        result.errors.append("old_name and new_name are identical — nothing to rename.")
        return result
    if not root.is_dir():
        result.errors.append(f"Root directory '{root}' does not exist.")
        return result

    # Collect files
    java_files = _collect_java_files(root, include_tests=include_tests)
    result.files_scanned = len(java_files)

    # Locate the source file
    source_file = _find_class_file(java_files, old_name, root)
    if source_file is None:
        result.errors.append(
            f"Could not find a file declaring class '{old_name}' under '{root}'."
        )
        return result

    # Determine new file path (same directory, new filename)
    new_file_path = source_file.with_name(new_name + ".java")
    result.old_file = str(source_file.relative_to(root)).replace("\\", "/")
    result.new_file = str(new_file_path.relative_to(root)).replace("\\", "/")

    if new_file_path.exists() and new_file_path != source_file:
        result.errors.append(
            f"Target file '{result.new_file}' already exists — aborting to avoid overwrite."
        )
        return result

    # Apply text replacements to all Java files
    changes: list[FileChange] = []
    for java_file in java_files:
        try:
            old_text = java_file.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            result.errors.append(f"Could not read '{java_file}': {e}")
            continue

        new_text = _apply_rename(old_text, old_name, new_name)
        if new_text == old_text:
            continue

        rel_path = str(java_file.relative_to(root)).replace("\\", "/")
        diff = _make_diff(old_text, new_text, rel_path)

        # Determine intent
        is_source = java_file == source_file
        if is_source:
            intent = f"Renamed class declaration: {old_name} → {new_name}"
        else:
            intent = f"Updated references to {old_name} → {new_name}"

        changes.append(FileChange(
            file=rel_path,
            intent=intent,
            diff=diff,
            before_lines=old_text.splitlines(),
            after_lines=new_text.splitlines(),
        ))

        if not dry_run:
            java_file.write_text(new_text, encoding="utf-8")

    result.changes = changes
    result.files_modified = len(changes)

    # Rename the physical file (BLOCKER-A core fix)
    if not dry_run and source_file.exists():
        source_file.rename(new_file_path)
    elif dry_run:
        # In dry_run mode, add a synthetic change record for the file rename itself
        # if no text change was found in the source file (e.g. only filename changes).
        pass

    return result

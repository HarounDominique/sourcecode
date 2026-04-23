from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Optional

# Schema imports are hoisted here (not lazily inside _scan_source_file) so that
# a stale .pyc / editable-install mismatch is caught at import time rather than
# silently dropping notes mid-scan on the first failed late-import.
from sourcecode.schema import AdrRecord, CodeNote, CodeNotesSummary

_MAX_NOTES = 500
_MAX_NOTES_PER_FILE = 30
_MAX_ADRS = 50
_MAX_FILE_SIZE = 512 * 1024  # 512 KB
_SYMBOL_LOOKBACK = 25  # líneas hacia atrás para encontrar el símbolo envolvente

_SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv",
    ".mypy_cache", "dist", "build", ".tox", ".eggs",
    ".next", ".nuxt", ".output", "vendor", "coverage",
}

_CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs",
    ".go", ".rb", ".java", ".kt", ".php", ".rs", ".sh",
    ".bash", ".cs", ".cpp", ".c", ".h", ".swift",
}

# Directorios canónicos de ADRs
_ADR_DIRS = {
    "docs/decisions", "docs/adr", "adr", "decisions",
    "doc/decisions", "doc/adr", ".adr", "architecture/decisions",
}

# Nombres de fichero típicos de ADR: ADR-0001-*.md, 0001-*.md, DECISION-*.md
_ADR_NAME_RE = re.compile(
    r"^(?:ADR|adr|DECISION|decision)-?\d+.*\.md$|^\d{4}-.*\.md$"
)

# Marcadores de notas reconocidos (case-insensitive en la búsqueda)
_NOTE_KINDS = frozenset(
    ["TODO", "FIXME", "HACK", "NOTE", "DEPRECATED", "WARNING", "XXX", "BUG", "OPTIMIZE"]
)

# Captura marcadores en comentarios de línea (# o //) y bloques (/* ... */)
# group(1) = kind, group(2) = texto de la nota
_NOTE_RE = re.compile(
    r"(?://|#)\s*(TODO|FIXME|HACK|NOTE|DEPRECATED|WARNING|XXX|BUG|OPTIMIZE)"
    r"[\s:!\-–]*(.*?)$"
    r"|/\*+\s*(TODO|FIXME|HACK|NOTE|DEPRECATED|WARNING|XXX|BUG|OPTIMIZE)"
    r"[\s:!\-–]*(.*?)(?:\*/|$)",
    re.IGNORECASE | re.MULTILINE,
)

# Heurística para el símbolo envolvente más cercano
_SYMBOL_PATTERNS: list[re.Pattern] = [
    re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\("),
    re.compile(r"^\s*class\s+([A-Za-z_]\w*)\s*[:({\[]"),
    re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_]\w*)\s*\("),
    re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_]\w*)\s*=\s*(?:async\s*)?\(.*\)\s*=>"),
    re.compile(r"^\s*func\s+(?:\([^)]*\)\s+)?([A-Za-z_]\w*)\s*\("),
    re.compile(r"^\s*(?:pub(?:\(crate\))?\s+)?fn\s+([A-Za-z_]\w*)\s*[(<]"),
    re.compile(r"^\s*(?:public|private|protected|static|abstract|override)"
               r"(?:\s+\w+)+\s+([A-Za-z_]\w*)\s*\("),
    re.compile(r"^\s*sub\s+([A-Za-z_]\w*)\s*\(?"),
]


def _find_nearby_symbol(lines: list[str], line_idx: int) -> Optional[str]:
    start = max(0, line_idx - _SYMBOL_LOOKBACK)
    for i in range(line_idx, start - 1, -1):
        for pat in _SYMBOL_PATTERNS:
            m = pat.match(lines[i])
            if m:
                return m.group(1)
    return None


def _scan_source_file(
    path: Path,
    rel_path: str,
    notes: list,
    total_count: list[int],
) -> None:
    try:
        if path.stat().st_size > _MAX_FILE_SIZE:
            return
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return

    lines = content.splitlines()
    file_count = 0

    for m in _NOTE_RE.finditer(content):
        if total_count[0] >= _MAX_NOTES or file_count >= _MAX_NOTES_PER_FILE:
            return

        # Dos grupos de captura alternativos (// vs /* style)
        kind = (m.group(1) or m.group(3) or "").upper()
        text = (m.group(2) or m.group(4) or "").strip()

        if not kind:
            continue

        line_num = content.count("\n", 0, m.start()) + 1
        symbol = _find_nearby_symbol(lines, line_num - 1)

        notes.append(CodeNote(
            kind=kind,
            path=rel_path,
            line=line_num,
            text=text[:200],
            symbol=symbol,
        ))
        file_count += 1
        total_count[0] += 1


def _parse_adr(path: Path, rel_path: str) -> Optional[object]:
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    title: Optional[str] = None
    status: Optional[str] = None
    summary_lines: list[str] = []
    in_body = False
    summary_done = False

    for line in content.splitlines():
        stripped = line.strip()

        if title is None:
            m = re.match(r"^#+\s+(.+)$", stripped)
            if m:
                title = m.group(1).strip()
                in_body = True
                continue

        if status is None:
            m = re.match(r"\*{0,2}[Ss]tatus\*{0,2}\s*[:\-]\s*(.+)$", stripped)
            if m:
                raw = m.group(1).strip().lower()
                if "accept" in raw:
                    status = "accepted"
                elif "propos" in raw or "draft" in raw:
                    status = "proposed"
                elif "deprecat" in raw or "obsolete" in raw:
                    status = "deprecated"
                elif "supersed" in raw or "replaced" in raw:
                    status = "superseded"
                else:
                    status = raw[:30]
                continue

        if in_body and not summary_done:
            if stripped and not stripped.startswith("#") and not stripped.startswith("|"):
                summary_lines.append(stripped)
                if len(" ".join(summary_lines)) > 250:
                    summary_done = True
            elif summary_lines and not stripped:
                summary_done = True

    summary = " ".join(summary_lines)[:300] if summary_lines else None
    if not title:
        title = path.stem

    return AdrRecord(path=rel_path, title=title, status=status, summary=summary)  # noqa: RET504


class CodeNotesAnalyzer:
    """Extrae notas de código (TODO/FIXME/HACK/etc.) y ADRs del proyecto."""

    def analyze(self, root: Path) -> tuple[list, list, object]:
        notes: list = []
        adrs: list = []
        limitations: list[str] = []
        total_count = [0]

        self._walk(root, root, notes, adrs, total_count, limitations)

        if total_count[0] >= _MAX_NOTES:
            limitations.append(f"notes_truncated_at:{_MAX_NOTES}")

        # Explicit canonical sort guarantees identical output for identical input
        # regardless of filesystem traversal order or OS/platform differences.
        # Without this, output order depended on sorted(Path.iterdir()) which
        # varies across Python versions, APFS vs ext4, and future walk refactors.
        notes.sort(key=lambda n: (n.path, n.line))
        adrs.sort(key=lambda a: a.path)

        kind_counts: Counter = Counter(n.kind for n in notes)
        file_counts: Counter = Counter(n.path for n in notes)
        # Secondary sort by path makes top_files stable when counts are tied.
        top_files = [
            f for f, _ in sorted(file_counts.items(), key=lambda x: (-x[1], x[0]))[:5]
        ]

        summary = CodeNotesSummary(
            requested=True,
            total=len(notes),
            by_kind=dict(kind_counts.most_common()),
            top_files=top_files,
            adr_count=len(adrs),
            limitations=limitations,
        )
        return notes, adrs, summary

    def _walk(
        self,
        root: Path,
        current: Path,
        notes: list,
        adrs: list,
        total_count: list[int],
        limitations: list[str],
    ) -> None:
        try:
            entries = sorted(current.iterdir())
        except OSError:
            # Catches PermissionError, EMFILE (too many open files), and other
            # OS-level errors that previously aborted the entire walk mid-scan,
            # returning an empty or partial list depending on when the error hit.
            return

        for entry in entries:
            name = entry.name
            if entry.is_dir():
                if name in _SKIP_DIRS or (name.startswith(".") and name not in {".adr"}):
                    continue
                self._walk(root, entry, notes, adrs, total_count, limitations)
            elif entry.is_file():
                rel = entry.relative_to(root).as_posix()
                suffix = entry.suffix.lower()

                if suffix == ".md":
                    parent_rel = entry.parent.relative_to(root).as_posix()
                    in_adr_dir = any(
                        parent_rel == d or parent_rel.startswith(d + "/")
                        for d in _ADR_DIRS
                    )
                    if in_adr_dir or _ADR_NAME_RE.match(name):
                        if len(adrs) < _MAX_ADRS:
                            adr = _parse_adr(entry, rel)
                            if adr is not None:
                                adrs.append(adr)
                        continue

                # The quota check lives inside _scan_source_file; the pre-check
                # here was redundant and caused files to be silently skipped when
                # traversal order varied (different files filled the quota first).
                if suffix in _CODE_EXTENSIONS:
                    _scan_source_file(entry, rel, notes, total_count)

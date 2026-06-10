"""file_chunker.py — Semantic chunking of large Java files for AI agent consumption.

Splits a Java source file into context-aware chunks at method/class boundaries.
Each chunk includes a context header so an AI agent can understand it without
reading prior chunks. Handles files of any size without timeout.

Usage:
    chunks = chunk_java_file(path, max_lines=500)
    # Each chunk: ChunkRecord with id, type, symbol, start_line, end_line, content

Design:
  - Primary split at method/constructor boundaries (brace depth == class depth + 1)
  - Secondary: class-level fields grouped together in a preamble chunk
  - Context header: package + class name + imports summary prepended to each chunk
  - Chunks never split mid-method: a method > max_lines is emitted as a single chunk
    with a size warning in metadata
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ChunkRecord:
    """A semantic chunk of a Java source file."""
    chunk_id: int               # 1-based sequential index
    chunk_type: str             # "class_header" | "method" | "constructor" | "field_block" | "class_footer"
    symbol: str                 # e.g. "MyService#processOrder" or "MyService" for class_header
    start_line: int             # 1-based inclusive
    end_line: int               # 1-based inclusive
    content: str                # source lines for this chunk
    context_header: str         # package + class context prepended for AI consumption
    size_warning: bool = False  # True if chunk exceeds max_lines (cannot split further)

    @property
    def total_lines(self) -> int:
        return self.end_line - self.start_line + 1

    def to_dict(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "chunk_type": self.chunk_type,
            "symbol": self.symbol,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "total_lines": self.total_lines,
            "context_header": self.context_header,
            "content": self.content,
            "size_warning": self.size_warning,
        }


@dataclass
class ChunkResult:
    """Result of chunking a Java file."""
    file: str                   # relative or absolute path
    total_lines: int
    total_chunks: int
    class_name: str
    package: str
    chunk_count_by_type: dict[str, int] = field(default_factory=dict)
    chunks: list[ChunkRecord] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "file": self.file,
            "total_lines": self.total_lines,
            "total_chunks": self.total_chunks,
            "class_name": self.class_name,
            "package": self.package,
            "chunk_count_by_type": self.chunk_count_by_type,
            "limitations": self.limitations,
            "chunks": [c.to_dict() for c in self.chunks],
        }


# ---------------------------------------------------------------------------
# Regexes
# ---------------------------------------------------------------------------

_PKG_RE = re.compile(r'^\s*package\s+([\w.]+)\s*;')
_IMPORT_RE = re.compile(r'^\s*import\s+(?:static\s+)?([\w.*]+)\s*;')
_CLASS_RE = re.compile(
    r'^\s*(?:(?:public|protected|private|abstract|final|sealed|non-sealed)\s+)*'
    r'(?:class|interface|enum|@interface)\s+(\w+)'
)
_METHOD_RE = re.compile(
    r'^\s*(?:(?:public|protected|private|static|final|synchronized|abstract|native|default|override)\s+)*'
    r'(?:@\w+(?:\([^)]*\))?\s+)*'      # optional return-type annotations (e.g. @ResponseBody)
    r'(?:<[^>]+>\s+)?'                  # optional generic return type
    r'(?:[\w<>\[\],?\s]+\s+)?'         # return type (optional for constructors)
    r'(\w+)\s*\('                        # method/constructor name + opening paren
)
_FIELD_RE = re.compile(
    r'^\s*(?:(?:public|protected|private|static|final|volatile|transient)\s+)+'
    r'[\w<>\[\].,? ]+\s+\w+\s*(?:=|;)'
)
_ANN_RE = re.compile(r'^\s*@')


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _count_braces(line: str) -> tuple[int, int]:
    """Return (open_count, close_count) for non-string/comment braces in line."""
    open_c = 0
    close_c = 0
    in_str = False
    in_char = False
    escape = False
    i = 0
    while i < len(line):
        ch = line[i]
        if escape:
            escape = False
        elif ch == '\\':
            escape = True
        elif ch == '"' and not in_char:
            in_str = not in_str
        elif ch == "'" and not in_str:
            in_char = not in_char
        elif not in_str and not in_char:
            if ch == '{':
                open_c += 1
            elif ch == '}':
                close_c += 1
            elif ch == '/' and i + 1 < len(line) and line[i+1] == '/':
                break  # rest is line comment
        i += 1
    return open_c, close_c


def _build_context_header(
    package: str,
    class_name: str,
    import_lines: list[str],
    max_imports: int = 10,
) -> str:
    """Build a context header showing package + class + condensed imports."""
    lines = []
    if package:
        lines.append(f"// File context: package {package};")
    lines.append(f"// Enclosing class: {class_name}")
    if import_lines:
        shown = import_lines[:max_imports]
        lines.extend(shown)
        if len(import_lines) > max_imports:
            lines.append(f"// ... ({len(import_lines) - max_imports} more imports omitted)")
    lines.append("")  # blank separator
    return "\n".join(lines)


def _is_method_or_constructor_start(
    stripped: str,
    class_name: str,
    depth: int,
    class_depth: int,
) -> tuple[bool, str, str]:
    """Return (is_method, method_name, chunk_type) if line starts a method/constructor."""
    if depth != class_depth + 1:
        return False, "", ""

    # Skip annotations
    if stripped.startswith("@"):
        return False, "", ""
    # Skip field declarations (end with ; or = before ;)
    if _FIELD_RE.match(stripped) and "{" not in stripped:
        return False, "", ""
    # Skip class/interface/enum declarations
    if _CLASS_RE.match(stripped):
        return False, "", ""

    m = _METHOD_RE.match(stripped)
    if not m:
        return False, "", ""
    name = m.group(1)
    # Distinguish constructor: name matches class name
    chunk_type = "constructor" if name == class_name else "method"
    return True, name, chunk_type


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def chunk_java_file(
    path: Path,
    *,
    max_lines: int = 500,
    include_content: bool = True,
) -> ChunkResult:
    """Split a Java file into semantic chunks at method/class boundaries.

    Args:
        path:            Path to the Java file.
        max_lines:       Target max lines per chunk. Methods exceeding this
                         are emitted as a single chunk with size_warning=True.
        include_content: If False, content field is omitted (metadata-only mode).

    Returns:
        ChunkResult with ordered list of ChunkRecord entries.
    """
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return ChunkResult(
            file=str(path),
            total_lines=0,
            total_chunks=0,
            class_name="",
            package="",
            limitations=[f"Could not read file: {e}"],
        )

    all_lines = source.splitlines()
    total_lines = len(all_lines)

    # ── Pass 1: extract package, class name, imports ──────────────────────
    package = ""
    class_name = ""
    import_lines: list[str] = []
    for raw_line in all_lines:
        stripped = raw_line.strip()
        if not package:
            pm = _PKG_RE.match(raw_line)
            if pm:
                package = pm.group(1)
        im = _IMPORT_RE.match(raw_line)
        if im:
            import_lines.append(raw_line.rstrip())
        if not class_name:
            cm = _CLASS_RE.match(raw_line)
            if cm:
                class_name = cm.group(1)

    context_header = _build_context_header(package, class_name, import_lines)

    # ── Pass 2: track brace depth, identify method/class boundaries ───────
    depth = 0
    class_depth = -1   # depth at which the primary class body starts
    chunks: list[ChunkRecord] = []
    chunk_id = 0
    limitations: list[str] = []

    # "pending" block: lines accumulated for current chunk
    pending_start: int = 1
    pending_lines: list[str] = []
    pending_type: str = "class_header"
    pending_symbol: str = class_name or "unknown"

    # Annotation buffer for upcoming method/constructor
    ann_buffer: list[tuple[int, str]] = []  # (line_no 1-based, raw_line)

    def _flush_chunk(end_line: int) -> None:
        nonlocal chunk_id, pending_start, pending_lines, pending_type, pending_symbol
        if not pending_lines:
            return
        chunk_id += 1
        content = "\n".join(pending_lines) if include_content else ""
        size_warn = len(pending_lines) > max_lines
        if size_warn:
            limitations.append(
                f"Chunk {chunk_id} ({pending_symbol}) has {len(pending_lines)} lines "
                f"(exceeds max_lines={max_lines}) — cannot split mid-method."
            )
        chunks.append(ChunkRecord(
            chunk_id=chunk_id,
            chunk_type=pending_type,
            symbol=pending_symbol,
            start_line=pending_start,
            end_line=end_line,
            content=content,
            context_header=context_header,
            size_warning=size_warn,
        ))
        pending_lines = []
        pending_start = end_line + 1

    in_block_comment = False
    current_method_name = ""
    current_method_type = ""
    method_brace_start_depth = -1
    # State for multi-line method signatures (name detected, waiting for opening '{')
    _pending_method: Optional[tuple[str, str]] = None  # (name, type)

    for line_no_0, raw_line in enumerate(all_lines):
        line_no = line_no_0 + 1  # 1-based
        stripped = raw_line.strip()

        # Block comment tracking
        if in_block_comment:
            pending_lines.append(raw_line)
            if "*/" in stripped:
                in_block_comment = False
            continue
        if "/*" in stripped and "*/" not in stripped:
            in_block_comment = True
            pending_lines.append(raw_line)
            continue

        # Track brace depth
        opens, closes = _count_braces(raw_line)

        # Detect class body start (first '{' after class declaration)
        if class_depth < 0 and class_name and _CLASS_RE.match(raw_line):
            class_depth = depth  # depth BEFORE the '{' on this line
            if opens > 0:
                class_depth = depth  # class body starts after this line

        # Check if this line starts a method/constructor AT class_depth+1
        if class_depth >= 0 and depth == class_depth + 1 and not current_method_name:
            # Multi-line signature: we already detected the method name; this line
            # should contain the opening brace that starts the method body.
            # Guard: opens > closes ensures a net depth increase (not a balanced
            # annotation arg like @RequestMapping({"v1","v2"})).
            if _pending_method and opens > closes:
                mname, mtype = _pending_method
                _pending_method = None
                current_method_name = mname
                current_method_type = mtype
                method_brace_start_depth = depth + opens - 1
                pending_type = mtype
                pending_symbol = f"{class_name}#{mname}" if class_name else mname
                pending_lines.append(raw_line)
                depth += opens - closes
                ann_buffer = []
                continue

            if not _pending_method:
                is_method, mname, mtype = _is_method_or_constructor_start(
                    stripped, class_name, depth, class_depth
                )
                if is_method:
                    # Flush anything accumulated as field_block / class_header
                    # Include annotation lines in the new method chunk
                    if pending_lines:
                        if ann_buffer:
                            ann_start_line = ann_buffer[0][0]
                            pre_ann_lines = pending_lines[:ann_start_line - pending_start]
                            if pre_ann_lines:
                                _flush_chunk(ann_start_line - 1)
                            # Move ann_buffer lines into the new method chunk
                            pending_start = ann_start_line
                            pending_lines = [al for _, al in ann_buffer]
                            ann_buffer = []
                        else:
                            _flush_chunk(line_no - 1)
                            pending_start = line_no
                            pending_lines = []

                    if "{" in raw_line:
                        # Single-line signature: method body opens on same line
                        current_method_name = mname
                        current_method_type = mtype
                        method_brace_start_depth = depth + opens - 1
                        pending_type = mtype
                        pending_symbol = f"{class_name}#{mname}" if class_name else mname
                        pending_lines.append(raw_line)
                        depth += opens - closes
                        ann_buffer = []
                        continue
                    else:
                        # Multi-line signature: record name, wait for '{' on later line
                        _pending_method = (mname, mtype)
                        pending_type = mtype
                        pending_symbol = f"{class_name}#{mname}" if class_name else mname
                        pending_lines.append(raw_line)
                        depth += opens - closes
                        ann_buffer = []
                        continue

        # Update depth
        depth += opens - closes

        # After depth update: check if current method closed
        if current_method_name and depth <= class_depth + 1:
            # Method body closed
            pending_lines.append(raw_line)
            _flush_chunk(line_no)
            current_method_name = ""
            current_method_type = ""
            # Next chunk is field_block until next method
            pending_type = "field_block"
            pending_symbol = class_name or "unknown"
            pending_start = line_no + 1
            ann_buffer = []
            continue

        # Track annotations at class level (buffered to attach to next method)
        if (class_depth >= 0 and depth == class_depth + 1
                and not current_method_name and stripped.startswith("@")):
            ann_buffer.append((line_no, raw_line))
        elif not stripped.startswith("@"):
            # Non-annotation line: clear annotation buffer if we're not entering a method
            if ann_buffer and not (class_depth >= 0 and depth == class_depth + 1):
                ann_buffer = []

        pending_lines.append(raw_line)

    # Flush remaining lines as class_footer
    if pending_lines:
        pending_type = "class_footer" if depth <= (class_depth if class_depth >= 0 else 0) else pending_type
        _flush_chunk(total_lines)

    chunk_count_by_type: dict[str, int] = {}
    for c in chunks:
        chunk_count_by_type[c.chunk_type] = chunk_count_by_type.get(c.chunk_type, 0) + 1

    return ChunkResult(
        file=str(path),
        total_lines=total_lines,
        total_chunks=len(chunks),
        class_name=class_name,
        package=package,
        chunk_count_by_type=chunk_count_by_type,
        chunks=chunks,
        limitations=limitations,
    )

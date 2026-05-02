from __future__ import annotations

"""Semantic contract model for per-file AST extraction.

FileContract is the core unit — one per source file, containing only
high-signal semantic structures: exports, imports, signatures, types.
No implementation bodies, no comments, no formatting.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ImportRecord:
    """Module import declaration."""

    source: str
    symbols: list[str] = field(default_factory=list)
    kind: str = "named"  # named | default | namespace | side_effect | reexport


@dataclass
class ExportRecord:
    """Exported symbol."""

    name: str
    kind: str = "unknown"  # function | class | const | type | default | react_component | enum | interface
    type_ref: Optional[str] = None
    async_: bool = False


@dataclass
class FunctionSignature:
    """Function or method signature — no body."""

    name: str
    signature: str  # compact form: "(param: Type, ...) -> ReturnType"
    async_: bool = False
    exported: bool = False
    return_type: Optional[str] = None


@dataclass
class TypeField:
    """Single field in an interface or struct."""

    name: str
    type: str
    required: bool = True


@dataclass
class TypeDefinition:
    """Interface, type alias, class, or enum definition — fields only, no methods."""

    name: str
    kind: str = "interface"  # interface | type | enum | class
    fields: list[TypeField] = field(default_factory=list)
    extends: list[str] = field(default_factory=list)
    exported: bool = True


@dataclass
class FileContract:
    """Semantic contract for a single source file.

    Contains only the information an LLM coding agent needs to:
    - understand what the file exports
    - understand what it depends on
    - navigate the type system
    - identify callable surfaces

    Never includes implementation bodies unless explicitly requested.
    """

    path: str
    language: str  # python | typescript | tsx | javascript | jsx | go | rust
    role: str = "unknown"  # component | hook | service | model | route | util | config | api | store | middleware | entrypoint

    exports: list[ExportRecord] = field(default_factory=list)
    imports: list[ImportRecord] = field(default_factory=list)
    functions: list[FunctionSignature] = field(default_factory=list)
    types: list[TypeDefinition] = field(default_factory=list)
    hooks_used: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)  # external module names only

    # Ranking signals
    relevance_score: float = 0.0
    fan_in: int = 0   # how many files import this
    fan_out: int = 0  # how many files this imports
    is_entrypoint: bool = False
    is_changed: bool = False

    # Extraction quality
    extraction_method: str = "heuristic"  # ast | tree_sitter | heuristic
    limitations: list[str] = field(default_factory=list)


@dataclass
class ContractSummary:
    """Summary of the contract extraction pipeline."""

    mode: str = "contract"
    total_files: int = 0
    extracted_files: int = 0
    filtered_files: int = 0
    method_breakdown: dict[str, int] = field(default_factory=dict)
    ranked_by: str = "relevance"
    limitations: list[str] = field(default_factory=list)

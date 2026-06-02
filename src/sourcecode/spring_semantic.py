"""spring_semantic.py — Spring transaction boundary model.

Builds a TransactionBoundaryIndex from a CanonicalRepositoryIR.
Parses @Transactional annotation attributes (propagation, isolation,
readOnly, rollbackFor, noRollbackFor, timeout) captured in
SymbolRecord.annotation_values by _extract_symbols.

Entry point:
    tx_index = build_tx_index(cir)

Deterministic: identical CIR → identical index.
Never raises: missing/unparseable attrs → safe defaults with confidence=low.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from sourcecode.canonical_ir import CanonicalRepositoryIR

# ---------------------------------------------------------------------------
# Constants — Spring defaults
# ---------------------------------------------------------------------------

PROPAGATION_DEFAULT = "REQUIRED"
ISOLATION_DEFAULT = "DEFAULT"
TIMEOUT_DEFAULT = -1

_VALID_PROPAGATIONS: frozenset[str] = frozenset({
    "REQUIRED", "REQUIRES_NEW", "SUPPORTS", "NOT_SUPPORTED",
    "MANDATORY", "NEVER", "NESTED",
})

_VALID_ISOLATIONS: frozenset[str] = frozenset({
    "DEFAULT", "READ_UNCOMMITTED", "READ_COMMITTED",
    "REPEATABLE_READ", "SERIALIZABLE",
})

# Strip enum prefix: Propagation.REQUIRES_NEW → REQUIRES_NEW
_ENUM_PREFIX_RE = re.compile(r'(?:Propagation|Isolation|TxType)\.(\w+)')

# readOnly=true / readOnly = true
_READ_ONLY_RE = re.compile(r'readOnly\s*=\s*(true|false)', re.IGNORECASE)

# timeout = 30
_TIMEOUT_RE = re.compile(r'timeout\s*=\s*(-?\d+)')

# propagation = Propagation.REQUIRES_NEW  or  propagation=REQUIRES_NEW
_PROPAGATION_RE = re.compile(r'propagation\s*=\s*(?:Propagation\.)?(\w+)')

# isolation = Isolation.READ_COMMITTED  or  isolation=READ_COMMITTED
_ISOLATION_RE = re.compile(r'isolation\s*=\s*(?:Isolation\.)?(\w+)')

# rollbackFor = {IOException.class, RuntimeException.class}
# or rollbackFor = IOException.class
_ROLLBACK_FOR_RE = re.compile(r'rollbackFor\s*=\s*(\{[^}]*\}|[\w.]+\.class)')

# noRollbackFor = ...
_NO_ROLLBACK_FOR_RE = re.compile(r'noRollbackFor\s*=\s*(\{[^}]*\}|[\w.]+\.class)')

# Extract class names from {Foo.class, Bar.class} or Foo.class
_CLASS_NAME_RE = re.compile(r'(\w+)\.class')


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class TransactionBoundary:
    """Parsed @Transactional semantics for a single symbol (class or method)."""

    symbol: str
    scope: str                          # "class" | "method"
    propagation: str = PROPAGATION_DEFAULT
    isolation: str = ISOLATION_DEFAULT
    read_only: bool = False
    rollback_for: list[str] = field(default_factory=list)
    no_rollback_for: list[str] = field(default_factory=list)
    timeout: int = TIMEOUT_DEFAULT
    source_file: str = ""
    modifiers: list[str] = field(default_factory=list)
    confidence: str = "high"            # "high" | "medium" | "low"
    raw_args: str = ""                  # original annotation args (for debug)

    @property
    def is_read_only(self) -> bool:
        return self.read_only

    @property
    def is_proxy_bypass_risk(self) -> bool:
        """True when proxy cannot intercept (private or final method)."""
        return self.scope == "method" and (
            "private" in self.modifiers or "final" in self.modifiers
        )

    def to_dict(self) -> dict:
        d: dict = {
            "symbol": self.symbol,
            "scope": self.scope,
            "propagation": self.propagation,
            "isolation": self.isolation,
            "read_only": self.read_only,
            "source_file": self.source_file,
            "confidence": self.confidence,
        }
        if self.modifiers:
            d["modifiers"] = self.modifiers
        if self.rollback_for:
            d["rollback_for"] = self.rollback_for
        if self.no_rollback_for:
            d["no_rollback_for"] = self.no_rollback_for
        if self.timeout != TIMEOUT_DEFAULT:
            d["timeout"] = self.timeout
        return d


@dataclass
class TransactionBoundaryIndex:
    """Index of all @Transactional boundaries in a repository."""

    # FQN → boundary (both class-level and method-level)
    by_symbol: dict[str, TransactionBoundary] = field(default_factory=dict)

    # class FQN → list of method-level boundaries declared on that class
    by_class: dict[str, list[TransactionBoundary]] = field(default_factory=dict)

    # class FQN → class-level boundary (inherited by methods that don't override)
    class_level: dict[str, TransactionBoundary] = field(default_factory=dict)

    repo_id: str = ""
    build_time_ms: float = 0.0

    def effective_boundary(self, method_fqn: str) -> Optional[TransactionBoundary]:
        """Return the effective TX boundary for a method symbol.

        Resolution order:
          1. Method-level @Transactional (most specific)
          2. Class-level @Transactional (inherited)
          3. None (no TX boundary)
        """
        if method_fqn in self.by_symbol:
            return self.by_symbol[method_fqn]
        # Derive enclosing class FQN: pkg.Class#method → pkg.Class
        class_fqn = _enclosing_class(method_fqn)
        return self.class_level.get(class_fqn)

    def all_boundaries(self) -> list[TransactionBoundary]:
        return list(self.by_symbol.values())

    def stats(self) -> dict:
        n_class = len(self.class_level)
        n_method = sum(1 for b in self.by_symbol.values() if b.scope == "method")
        propagations: dict[str, int] = {}
        for b in self.by_symbol.values():
            propagations[b.propagation] = propagations.get(b.propagation, 0) + 1
        return {
            "total": len(self.by_symbol),
            "class_level": n_class,
            "method_level": n_method,
            "propagations": propagations,
            "read_only_count": sum(1 for b in self.by_symbol.values() if b.read_only),
        }


# ---------------------------------------------------------------------------
# Annotation arg parser
# ---------------------------------------------------------------------------

def _parse_class_list(raw: str) -> list[str]:
    """Extract class simple names from rollbackFor / noRollbackFor value.

    Handles:
      IOException.class
      {IOException.class, RuntimeException.class}
    """
    return _CLASS_NAME_RE.findall(raw)


def parse_transactional_args(raw_args: str) -> tuple[dict, str]:
    """Parse @Transactional annotation args string into attribute dict.

    Returns (attrs_dict, confidence).
    confidence = "high"  when parse is complete and unambiguous
               = "medium" when some attrs present but others defaulted
               = "low"   when args present but nothing parseable

    Safe: never raises.
    """
    if not raw_args or not raw_args.strip():
        return {}, "high"   # no-args @Transactional is valid — all defaults

    attrs: dict = {}
    parsed_any = False

    try:
        m = _PROPAGATION_RE.search(raw_args)
        if m:
            val = m.group(1).upper()
            attrs["propagation"] = val if val in _VALID_PROPAGATIONS else PROPAGATION_DEFAULT
            parsed_any = True

        m = _ISOLATION_RE.search(raw_args)
        if m:
            val = m.group(1).upper()
            attrs["isolation"] = val if val in _VALID_ISOLATIONS else ISOLATION_DEFAULT
            parsed_any = True

        m = _READ_ONLY_RE.search(raw_args)
        if m:
            attrs["read_only"] = m.group(1).lower() == "true"
            parsed_any = True

        m = _TIMEOUT_RE.search(raw_args)
        if m:
            try:
                attrs["timeout"] = int(m.group(1))
                parsed_any = True
            except ValueError:
                pass

        m = _ROLLBACK_FOR_RE.search(raw_args)
        if m:
            attrs["rollback_for"] = _parse_class_list(m.group(1))
            parsed_any = True

        m = _NO_ROLLBACK_FOR_RE.search(raw_args)
        if m:
            attrs["no_rollback_for"] = _parse_class_list(m.group(1))
            parsed_any = True

    except Exception:
        return {}, "low"

    if not parsed_any and raw_args.strip():
        # Args present but nothing matched — e.g. bare string value like "txManager"
        # That's a transactionManager ref, not an attr we parse. Still valid @Transactional.
        return {}, "medium"

    return attrs, "high"


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

def _enclosing_class(fqn: str) -> str:
    """pkg.Class#method → pkg.Class.  pkg.Class → pkg.Class."""
    if "#" in fqn:
        return fqn.split("#")[0]
    return fqn


def _build_boundary(symbol: str, scope: str, raw_args: str,
                    source_file: str, modifiers: list[str]) -> TransactionBoundary:
    attrs, confidence = parse_transactional_args(raw_args)
    return TransactionBoundary(
        symbol=symbol,
        scope=scope,
        propagation=attrs.get("propagation", PROPAGATION_DEFAULT),
        isolation=attrs.get("isolation", ISOLATION_DEFAULT),
        read_only=attrs.get("read_only", False),
        rollback_for=attrs.get("rollback_for", []),
        no_rollback_for=attrs.get("no_rollback_for", []),
        timeout=attrs.get("timeout", TIMEOUT_DEFAULT),
        source_file=source_file,
        modifiers=list(modifiers),
        confidence=confidence,
        raw_args=raw_args,
    )


def build_tx_index(cir: "CanonicalRepositoryIR") -> TransactionBoundaryIndex:
    """Build TransactionBoundaryIndex from a CanonicalRepositoryIR.

    Consumes cir._raw_ir graph nodes (which carry SymbolRecord fields).
    Never raises — returns empty index on any error.

    Resolution:
      1. Scan all nodes with @Transactional annotation.
      2. Classify as class-level or method-level by symbol_kind.
      3. Method-level boundaries override class-level for the same method.
      4. Class-level boundaries are inherited by methods that lack their own.
    """
    t0 = time.monotonic()
    index = TransactionBoundaryIndex(repo_id=getattr(cir, "cir_hash", "")[:16])

    try:
        raw_ir = getattr(cir, "_raw_ir", {}) or {}
        graph = raw_ir.get("graph") or {}
        nodes = graph.get("nodes") or []

        for node in nodes:
            if not isinstance(node, dict):
                continue

            annotations = node.get("annotations") or []
            if "@Transactional" not in annotations:
                continue

            fqn = node.get("fqn") or node.get("symbol") or ""
            if not fqn:
                continue

            symbol_kind = node.get("symbol_kind") or node.get("type") or ""
            source_file = node.get("source_file") or node.get("declaring_file") or ""
            modifiers = node.get("modifiers") or []

            # annotation_values is stored per-symbol in the graph node
            ann_values = node.get("annotation_values") or {}
            raw_args = ann_values.get("@Transactional", "")

            # Determine scope: class-level or method-level
            if symbol_kind in ("class", "interface", "enum"):
                scope = "class"
            elif symbol_kind in ("method", "constructor"):
                scope = "method"
            elif "#" in fqn:
                scope = "method"
            else:
                scope = "class"

            boundary = _build_boundary(fqn, scope, raw_args, source_file, modifiers)

            index.by_symbol[fqn] = boundary

            if scope == "class":
                index.class_level[fqn] = boundary
            else:
                class_fqn = _enclosing_class(fqn)
                index.by_class.setdefault(class_fqn, []).append(boundary)

    except Exception:
        pass

    index.build_time_ms = round((time.monotonic() - t0) * 1000, 2)
    return index

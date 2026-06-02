"""spring_tx_analyzer.py — Transaction anomaly detection engine.

Patterns implemented (Phase 2):
  TX-001  @Transactional on private or final method — CGLIB proxy bypass
  TX-002  REQUIRES_NEW nested within REQUIRED call chain (depth ≤ 3)
  TX-003  readOnly=true boundary propagating into write-capable callee
  TX-004  NOT_SUPPORTED or NEVER invoked from transactional call chain
  TX-005  Exception swallowing inside @Transactional method (regex)

Self-invocation via this.method() intentionally excluded from Phase 2:
  requires AST-level analysis, regex produces too many false positives.

All patterns are deterministic and never raise.
"""
from __future__ import annotations

import inspect
import re
import time
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Protocol, runtime_checkable

from sourcecode.spring_findings import SpringAuditResult, SpringFinding
from sourcecode.spring_model import SpringSemanticModel
from sourcecode.spring_semantic import (
    PROPAGATION_DEFAULT,
    TransactionBoundary,
    TransactionBoundaryIndex,
    build_tx_index,
)

if TYPE_CHECKING:
    from sourcecode.canonical_ir import CanonicalRepositoryIR

# ---------------------------------------------------------------------------
# BFS config
# ---------------------------------------------------------------------------

_BFS_MAX_DEPTH = 3      # max hops for propagation traversal
_BFS_TIMEOUT_MS = 500   # abort BFS if it takes longer

# Heuristic write-method name patterns for TX-003
_WRITE_METHOD_RE = re.compile(
    r'(?:save|create|insert|update|delete|remove|persist|merge|flush|'
    r'store|write|put|add|modify|patch|upsert|push)\b',
    re.IGNORECASE,
)

# Exception swallowing pattern for TX-005:
# catch block that contains a log/print but no throw/rethrow
_CATCH_SWALLOW_RE = re.compile(
    r'catch\s*\([^)]+\)\s*\{[^}]*'          # catch(...) {
    r'(?:log|logger|LOG|System\.out|e\.print)'  # logging call
    r'[^}]*\}',                               # closing brace (no throw inside)
    re.DOTALL,
)
_RETHROW_IN_CATCH_RE = re.compile(r'\bthrow\b')


# ---------------------------------------------------------------------------
# Pattern protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class TxPattern(Protocol):
    pattern_id: str
    severity: str

    def analyze(
        self,
        cir: "CanonicalRepositoryIR",
        tx_index: TransactionBoundaryIndex,
        root: Optional[Path],
        *,
        model: Optional[SpringSemanticModel] = None,
    ) -> list[SpringFinding]:
        ...


def _call_pattern_analyze(
    pattern: Any,
    cir: "CanonicalRepositoryIR",
    tx_index: TransactionBoundaryIndex,
    root: Optional[Path],
    model: Optional[SpringSemanticModel],
) -> list[SpringFinding]:
    """Dispatch to pattern.analyze(), injecting model if the pattern accepts it.

    Patterns that declare `model` in their signature receive the shared model.
    Patterns without it (e.g. test doubles) are called with the legacy signature.
    """
    try:
        sig = inspect.signature(pattern.analyze)
        if "model" in sig.parameters:
            return pattern.analyze(cir, tx_index, root, model=model)
    except (ValueError, TypeError):
        pass
    return pattern.analyze(cir, tx_index, root)


# ---------------------------------------------------------------------------
# TX-001: @Transactional on private or final method
# ---------------------------------------------------------------------------

class _TX001ProxyBypass:
    pattern_id = "TX-001"
    severity = "high"

    def analyze(
        self,
        cir: "CanonicalRepositoryIR",
        tx_index: TransactionBoundaryIndex,
        root: Optional[Path],
        *,
        model: Optional[SpringSemanticModel] = None,
    ) -> list[SpringFinding]:
        findings: list[SpringFinding] = []

        for boundary in tx_index.all_boundaries():
            if boundary.scope != "method":
                continue
            if not boundary.is_proxy_bypass_risk:
                continue

            problematic_modifier = (
                "private" if "private" in boundary.modifiers else "final"
            )
            reason = (
                "Spring CGLIB proxy cannot intercept private methods"
                if problematic_modifier == "private"
                else "Spring CGLIB proxy cannot override final methods"
            )
            simple_name = boundary.symbol.rsplit(".", 1)[-1].replace("#", ".")

            findings.append(SpringFinding(
                id=SpringFinding.make_id(self.pattern_id, boundary.symbol),
                pattern_id=self.pattern_id,
                category="tx",
                severity=self.severity,
                confidence="high",
                title=f"@Transactional on {problematic_modifier} method — Spring proxy bypass",
                symbol=boundary.symbol,
                source_file=boundary.source_file,
                evidence={
                    "annotation": "@Transactional",
                    "modifier": problematic_modifier,
                    "proxy_mechanism": "CGLIB",
                    "reason": reason,
                },
                explanation=(
                    f"{simple_name} is declared {problematic_modifier} with @Transactional. "
                    f"{reason} — @Transactional is silently ignored. "
                    "The method executes outside any transaction boundary; no rollback occurs on failure."
                ),
                fix_hint=(
                    "Change to package-private or public, or extract the logic to a separate Spring bean."
                    if problematic_modifier == "private"
                    else "Remove the final modifier, or extract to a non-final Spring bean."
                ),
                limitations=[],
                related_symbols=[boundary.symbol.split("#")[0]],
            ))

        return findings


# ---------------------------------------------------------------------------
# BFS helpers (shared by TX-002 and TX-004)
# ---------------------------------------------------------------------------

def _build_forward_adjacency(
    cir: "CanonicalRepositoryIR",
) -> dict[str, list[str]]:
    """Build forward call adjacency: caller → [callees].

    Uses call_graph edges excluding annotated_with / mapped_to / contained_in.
    """
    _skip = frozenset({"annotated_with", "mapped_to", "contained_in"})
    adj: dict[str, list[str]] = {}
    for edge in cir.call_graph:
        if not isinstance(edge, dict):
            continue
        if edge.get("type") in _skip:
            continue
        frm = edge.get("from") or ""
        to = edge.get("to") or ""
        if frm and to:
            adj.setdefault(frm, []).append(to)
    return adj


def _bfs_pairs(
    start: str,
    adjacency: dict[str, list[str]],
    tx_index: TransactionBoundaryIndex,
    max_depth: int,
    deadline_ns: int,
) -> list[tuple[str, str, int]]:
    """BFS from start, yield (caller, callee, depth) pairs where BOTH have TX boundaries.

    Aborts when deadline_ns is reached (monotonic ns).
    """
    pairs: list[tuple[str, str, int]] = []
    visited: set[str] = {start}
    queue: deque[tuple[str, int]] = deque([(start, 0)])

    while queue:
        if time.monotonic_ns() > deadline_ns:
            break
        node, depth = queue.popleft()
        if depth >= max_depth:
            continue
        for callee in adjacency.get(node, []):
            if callee in visited:
                continue
            visited.add(callee)
            caller_b = tx_index.effective_boundary(node)
            callee_b = tx_index.effective_boundary(callee)
            if caller_b and callee_b:
                pairs.append((node, callee, depth + 1))
            queue.append((callee, depth + 1))

    return pairs


# ---------------------------------------------------------------------------
# TX-002: REQUIRES_NEW nested within REQUIRED call chain
# ---------------------------------------------------------------------------

class _TX002RequiresNewNested:
    pattern_id = "TX-002"
    severity = "medium"

    def analyze(
        self,
        cir: "CanonicalRepositoryIR",
        tx_index: TransactionBoundaryIndex,
        root: Optional[Path],
        *,
        model: Optional[SpringSemanticModel] = None,
    ) -> list[SpringFinding]:
        findings: list[SpringFinding] = []
        seen_pairs: set[tuple[str, str]] = set()

        adj = model.call_adj.adjacency if model is not None else _build_forward_adjacency(cir)
        deadline = time.monotonic_ns() + _BFS_TIMEOUT_MS * 1_000_000

        for boundary in tx_index.all_boundaries():
            if boundary.propagation not in ("REQUIRED", "SUPPORTS"):
                continue
            pairs = _bfs_pairs(boundary.symbol, adj, tx_index, _BFS_MAX_DEPTH, deadline)
            for caller, callee, depth in pairs:
                callee_b = tx_index.effective_boundary(callee)
                if not callee_b or callee_b.propagation != "REQUIRES_NEW":
                    continue
                pair = (caller, callee)
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)

                caller_simple = caller.rsplit(".", 1)[-1].replace("#", ".")
                callee_simple = callee.rsplit(".", 1)[-1].replace("#", ".")

                findings.append(SpringFinding(
                    id=SpringFinding.make_id(self.pattern_id, f"{caller}→{callee}"),
                    pattern_id=self.pattern_id,
                    category="tx",
                    severity=self.severity,
                    confidence="medium",
                    title="REQUIRES_NEW nested within REQUIRED transaction",
                    symbol=callee,
                    source_file=callee_b.source_file,
                    evidence={
                        "outer_symbol": caller,
                        "outer_propagation": (
                            tx_index.effective_boundary(caller) or boundary
                        ).propagation,
                        "inner_symbol": callee,
                        "inner_propagation": "REQUIRES_NEW",
                        "call_depth": depth,
                    },
                    explanation=(
                        f"{callee_simple} uses REQUIRES_NEW inside a call chain starting from "
                        f"{caller_simple} (REQUIRED). Spring suspends the outer transaction and opens "
                        "a new one. If the inner transaction commits and the outer rolls back, "
                        "the inner changes are permanently written — silent partial data corruption."
                    ),
                    fix_hint=(
                        "Verify this nested TX is intentional. If not, change inner to REQUIRED "
                        "or extract to a separate service called outside the outer transaction."
                    ),
                    limitations=[
                        "BFS depth limited to 3 hops — deeper chains not analyzed",
                        "Dynamic dispatch (interface calls) may miss actual callee",
                    ],
                    related_symbols=[caller],
                ))

        return findings


# ---------------------------------------------------------------------------
# TX-003: readOnly=true propagating to write-capable callee
# ---------------------------------------------------------------------------

class _TX003ReadOnlyWritePropagation:
    pattern_id = "TX-003"
    severity = "medium"

    def analyze(
        self,
        cir: "CanonicalRepositoryIR",
        tx_index: TransactionBoundaryIndex,
        root: Optional[Path],
        *,
        model: Optional[SpringSemanticModel] = None,
    ) -> list[SpringFinding]:
        findings: list[SpringFinding] = []
        seen_pairs: set[tuple[str, str]] = set()

        adj = model.call_adj.adjacency if model is not None else _build_forward_adjacency(cir)
        deadline = time.monotonic_ns() + _BFS_TIMEOUT_MS * 1_000_000

        for boundary in tx_index.all_boundaries():
            if not boundary.read_only:
                continue
            pairs = _bfs_pairs(boundary.symbol, adj, tx_index, _BFS_MAX_DEPTH, deadline)
            for caller, callee, depth in pairs:
                callee_b = tx_index.effective_boundary(callee)
                if not callee_b or callee_b.read_only:
                    continue
                # Only flag if the callee method name looks like a write
                method_name = callee.split("#")[-1] if "#" in callee else callee.split(".")[-1]
                if not _WRITE_METHOD_RE.search(method_name):
                    continue
                pair = (caller, callee)
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)

                caller_simple = caller.rsplit(".", 1)[-1].replace("#", ".")
                callee_simple = callee.rsplit(".", 1)[-1].replace("#", ".")

                findings.append(SpringFinding(
                    id=SpringFinding.make_id(self.pattern_id, f"{caller}→{callee}"),
                    pattern_id=self.pattern_id,
                    category="tx",
                    severity=self.severity,
                    confidence="medium",
                    title="readOnly=true transaction calling write-capable method",
                    symbol=callee,
                    source_file=callee_b.source_file,
                    evidence={
                        "readonly_boundary": caller,
                        "write_callee": callee,
                        "callee_method": method_name,
                        "call_depth": depth,
                    },
                    explanation=(
                        f"{caller_simple} runs in a readOnly=true transaction but calls "
                        f"{callee_simple} which appears to perform writes. "
                        "The database may reject the write with a TransactionSystemException "
                        "or silently ignore it depending on the JDBC driver and isolation level."
                    ),
                    fix_hint=(
                        "Remove readOnly=true from the outer boundary, or ensure "
                        f"{callee_simple} is called outside the read-only transaction."
                    ),
                    limitations=[
                        "Write detection is heuristic (method name pattern) — may miss non-conventional names",
                        "BFS depth limited to 3 hops",
                    ],
                    related_symbols=[caller],
                ))

        return findings


# ---------------------------------------------------------------------------
# TX-004: NOT_SUPPORTED or NEVER within transactional call chain
# ---------------------------------------------------------------------------

class _TX004TxSuspensionRisk:
    pattern_id = "TX-004"
    severity = "medium"

    # Propagations that cannot run inside an active TX
    _INCOMPATIBLE = frozenset({"NOT_SUPPORTED", "NEVER"})
    # Propagations that represent an active TX
    _ACTIVE_TX = frozenset({"REQUIRED", "REQUIRES_NEW", "MANDATORY", "NESTED"})

    def analyze(
        self,
        cir: "CanonicalRepositoryIR",
        tx_index: TransactionBoundaryIndex,
        root: Optional[Path],
        *,
        model: Optional[SpringSemanticModel] = None,
    ) -> list[SpringFinding]:
        findings: list[SpringFinding] = []
        seen_pairs: set[tuple[str, str]] = set()

        adj = model.call_adj.adjacency if model is not None else _build_forward_adjacency(cir)
        deadline = time.monotonic_ns() + _BFS_TIMEOUT_MS * 1_000_000

        for boundary in tx_index.all_boundaries():
            if boundary.propagation not in self._ACTIVE_TX:
                continue
            pairs = _bfs_pairs(boundary.symbol, adj, tx_index, _BFS_MAX_DEPTH, deadline)
            for caller, callee, depth in pairs:
                callee_b = tx_index.effective_boundary(callee)
                if not callee_b or callee_b.propagation not in self._INCOMPATIBLE:
                    continue
                pair = (caller, callee)
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)

                callee_prop = callee_b.propagation
                caller_simple = caller.rsplit(".", 1)[-1].replace("#", ".")
                callee_simple = callee.rsplit(".", 1)[-1].replace("#", ".")

                if callee_prop == "NEVER":
                    consequence = (
                        "Spring will throw IllegalTransactionStateException at runtime "
                        "because NEVER forbids execution inside an active transaction."
                    )
                    sev = "high"
                else:
                    consequence = (
                        "Spring suspends the active transaction. Any writes inside "
                        f"{callee_simple} run outside the transaction and cannot be rolled back."
                    )
                    sev = "medium"

                findings.append(SpringFinding(
                    id=SpringFinding.make_id(self.pattern_id, f"{caller}→{callee}"),
                    pattern_id=self.pattern_id,
                    category="tx",
                    severity=sev,
                    confidence="medium",
                    title=f"{callee_prop} called within active transaction ({boundary.propagation})",
                    symbol=callee,
                    source_file=callee_b.source_file,
                    evidence={
                        "outer_symbol": caller,
                        "outer_propagation": boundary.propagation,
                        "inner_symbol": callee,
                        "inner_propagation": callee_prop,
                        "call_depth": depth,
                    },
                    explanation=(
                        f"{caller_simple} runs in {boundary.propagation} and calls "
                        f"{callee_simple} which has {callee_prop}. {consequence}"
                    ),
                    fix_hint=(
                        f"Change {callee_simple} propagation to REQUIRED, or ensure it is "
                        "called from a non-transactional context."
                    ),
                    limitations=[
                        "BFS depth limited to 3 hops — deeper chains not analyzed",
                        "Dynamic dispatch may miss actual callee",
                    ],
                    related_symbols=[caller],
                ))

        return findings


# ---------------------------------------------------------------------------
# TX-005: Exception swallowing inside @Transactional (regex, source-based)
# ---------------------------------------------------------------------------

class _TX005ExceptionSwallowing:
    pattern_id = "TX-005"
    severity = "medium"

    def analyze(
        self,
        cir: "CanonicalRepositoryIR",
        tx_index: TransactionBoundaryIndex,
        root: Optional[Path],
        *,
        model: Optional[SpringSemanticModel] = None,
    ) -> list[SpringFinding]:
        if root is None:
            return []

        findings: list[SpringFinding] = []

        for boundary in tx_index.all_boundaries():
            if boundary.scope != "method":
                continue
            if not boundary.source_file:
                continue

            abs_path = root / boundary.source_file
            try:
                if not abs_path.exists() or abs_path.stat().st_size > 200_000:
                    continue
                source = abs_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            # Find the method's body and check for swallowed exceptions
            if not self._has_swallowed_exception(source, boundary.symbol):
                continue

            simple_name = boundary.symbol.rsplit(".", 1)[-1].replace("#", ".")
            findings.append(SpringFinding(
                id=SpringFinding.make_id(self.pattern_id, boundary.symbol),
                pattern_id=self.pattern_id,
                category="tx",
                severity=self.severity,
                confidence="medium",
                title="Exception swallowed inside @Transactional method — rollback suppressed",
                symbol=boundary.symbol,
                source_file=boundary.source_file,
                evidence={
                    "annotation": "@Transactional",
                    "pattern": "catch block with log/print but no rethrow",
                    "method": simple_name,
                },
                explanation=(
                    f"{simple_name} has @Transactional but catches an exception and logs it "
                    "without rethrowing. Spring triggers rollback only when an exception "
                    "propagates out of the method — a swallowed exception means the transaction "
                    "commits even though the operation partially failed."
                ),
                fix_hint=(
                    "Rethrow the exception (or wrap in RuntimeException), or call "
                    "TransactionAspectSupport.currentTransactionStatus().setRollbackOnly()."
                ),
                limitations=[
                    "Detection is regex-based — complex catch blocks or nested methods may be missed",
                    "Only analyzes the immediate method body, not called helpers",
                ],
                related_symbols=[boundary.symbol.split("#")[0]],
            ))

        return findings

    def _has_swallowed_exception(self, source: str, symbol: str) -> bool:
        """Return True if source contains a catch-log-no-rethrow pattern."""
        for match in _CATCH_SWALLOW_RE.finditer(source):
            block = match.group(0)
            if not _RETHROW_IN_CATCH_RE.search(block):
                return True
        return False


# ---------------------------------------------------------------------------
# Default pattern registry
# ---------------------------------------------------------------------------

_DEFAULT_TX_PATTERNS: list[TxPattern] = [
    _TX001ProxyBypass(),       # type: ignore[list-item]
    _TX002RequiresNewNested(),  # type: ignore[list-item]
    _TX003ReadOnlyWritePropagation(),  # type: ignore[list-item]
    _TX004TxSuspensionRisk(),   # type: ignore[list-item]
    _TX005ExceptionSwallowing(),  # type: ignore[list-item]
]


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

def _deduplicate(findings: list[SpringFinding]) -> list[SpringFinding]:
    seen: set[str] = set()
    out: list[SpringFinding] = []
    for f in findings:
        if f.id not in seen:
            seen.add(f.id)
            out.append(f)
    return out


_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


class TxPatternEngine:
    """Runs registered TX patterns against a CIR + TransactionBoundaryIndex.

    Usage:
        engine = TxPatternEngine()
        findings = engine.analyze(cir, tx_index, root=Path("/repo"))
        # or with pre-built model (eliminates duplicate adjacency builds):
        findings = engine.analyze(cir, tx_index, root=Path("/repo"), model=model)

    Never raises. Pattern errors are silently swallowed (finding missed, not crash).
    """

    def __init__(self, patterns: Optional[list[TxPattern]] = None):
        self.patterns: list[TxPattern] = patterns if patterns is not None else _DEFAULT_TX_PATTERNS

    def analyze(
        self,
        cir: "CanonicalRepositoryIR",
        tx_index: TransactionBoundaryIndex,
        root: Optional[Path] = None,
        *,
        model: Optional[SpringSemanticModel] = None,
    ) -> list[SpringFinding]:
        all_findings: list[SpringFinding] = []
        for pattern in self.patterns:
            try:
                found = _call_pattern_analyze(pattern, cir, tx_index, root, model)
                all_findings.extend(found)
            except Exception:
                pass
        deduped = _deduplicate(all_findings)
        return sorted(deduped, key=lambda f: (_SEVERITY_ORDER.get(f.severity, 9), f.symbol))


# ---------------------------------------------------------------------------
# Convenience: run full TX audit from CIR
# ---------------------------------------------------------------------------

def run_tx_audit(
    cir: "CanonicalRepositoryIR",
    *,
    root: Optional[Path] = None,
    scope: str = "all",
    min_severity: str = "low",
    patterns: Optional[list[TxPattern]] = None,
    model: Optional[SpringSemanticModel] = None,
) -> SpringAuditResult:
    """Run TX anomaly detection and return a SpringAuditResult.

    Args:
        cir:          CanonicalRepositoryIR from build_canonical_ir().
        root:         Repo root Path (needed for TX-005 source inspection).
        scope:        "all" | "tx" (reserved — always "tx" for this function).
        min_severity: Filter findings below this severity.
        patterns:     Override default pattern list (for testing).
        model:        Pre-built SpringSemanticModel (avoids duplicate build in CLI).
    """
    _sev_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    min_rank = _sev_rank.get(min_severity, 3)

    t0 = time.monotonic()

    if model is None:
        model = SpringSemanticModel.build(cir)
    tx_index = model.tx_index
    engine = TxPatternEngine(patterns=patterns)
    findings = engine.analyze(cir, tx_index, root=root, model=model)

    # Filter by min_severity
    findings = [f for f in findings if _sev_rank.get(f.severity, 9) <= min_rank]

    elapsed_ms = round((time.monotonic() - t0) * 1000, 1)

    result = SpringAuditResult(
        repo_id=getattr(cir, "cir_hash", "")[:16],
        spring_detected=True,
        scope="tx",
        findings=findings,
        limitations=[
            "Self-invocation via this.method() not detected — requires AST-level analysis",
            "Dynamic dispatch (interface/polymorphic calls) may produce incomplete call chains",
        ],
        metadata={
            "symbols_analyzed": len(getattr(cir, "symbols", [])),
            "tx_boundaries_found": tx_index.stats()["total"],
            "tx_stats": tx_index.stats(),
            "analysis_time_ms": elapsed_ms,
        },
    )
    return result.finalize()

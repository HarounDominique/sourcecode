"""spring_security_audit.py — Spring security surface audit engine.

Patterns implemented (Phase 3):
  SEC-001  Endpoint without security guard in annotation_based security model
  SEC-002  CVE-2025-41248: @PreAuthorize on inherited method from generic supertype
  SEC-003  @Transactional on @Controller/@RestController (TX in wrong layer)

All patterns are deterministic and never raise.
"""
from __future__ import annotations

import inspect
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Protocol, runtime_checkable

from sourcecode.spring_findings import SpringAuditResult, SpringFinding
from sourcecode.spring_model import SpringSemanticModel
from sourcecode.spring_semantic import TransactionBoundaryIndex, build_tx_index

if TYPE_CHECKING:
    from sourcecode.canonical_ir import CanonicalRepositoryIR

# Generic type parameter regex: matches <User>, <T, ID>, <K extends Serializable>, etc.
_GENERIC_PARAM_RE = re.compile(r"<[A-Z][\w,\s<>?]*>")


# ---------------------------------------------------------------------------
# Pattern protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class SecurityPattern(Protocol):
    pattern_id: str
    severity: str

    def analyze(
        self,
        cir: "CanonicalRepositoryIR",
        tx_index: Optional[TransactionBoundaryIndex],
        root: Optional[Path],
        *,
        model: Optional[SpringSemanticModel] = None,
    ) -> list[SpringFinding]:
        ...


def _call_pattern_analyze(
    pattern: Any,
    cir: "CanonicalRepositoryIR",
    tx_index: Optional[TransactionBoundaryIndex],
    root: Optional[Path],
    model: Optional[SpringSemanticModel],
) -> list[SpringFinding]:
    """Dispatch to pattern.analyze(), injecting model if the pattern accepts it."""
    try:
        sig = inspect.signature(pattern.analyze)
        if "model" in sig.parameters:
            return pattern.analyze(cir, tx_index, root, model=model)
    except (ValueError, TypeError):
        pass
    return pattern.analyze(cir, tx_index, root)


# ---------------------------------------------------------------------------
# SEC-001: Endpoint without security guard (annotation_based model)
# ---------------------------------------------------------------------------

class _SEC001UnsecuredEndpoint:
    pattern_id = "SEC-001"
    severity = "high"

    def analyze(
        self,
        cir: "CanonicalRepositoryIR",
        tx_index: Optional[TransactionBoundaryIndex],
        root: Optional[Path],
        *,
        model: Optional[SpringSemanticModel] = None,
    ) -> list[SpringFinding]:
        security_model = cir.metadata.get("security_model", "unknown")
        # filter_based model has centralized config — per-endpoint annotation absence is expected
        if security_model != "annotation_based":
            return []

        findings: list[SpringFinding] = []

        for ep in cir.endpoints:
            if ep.security is not None and ep.security.policy != "none_detected":
                continue

            findings.append(SpringFinding(
                id=SpringFinding.make_id(self.pattern_id, ep.id),
                pattern_id=self.pattern_id,
                category="security",
                severity=self.severity,
                confidence="high",
                title="Endpoint has no security guard — unauthenticated access possible",
                symbol=ep.handler_symbol,
                source_file=_controller_source_file(cir, ep.controller_class),
                evidence={
                    "endpoint_id": ep.id,
                    "method": ep.method,
                    "path": ep.path,
                    "controller_class": ep.controller_class,
                    "security_model": security_model,
                    "security_policy": (
                        ep.security.policy if ep.security else "absent"
                    ),
                },
                explanation=(
                    f"{ep.method} {ep.path} (handler: {ep.handler_symbol.split('.')[-1]}) "
                    "has no @PreAuthorize, @Secured, @RolesAllowed, or equivalent annotation. "
                    "In an annotation_based security model there is no centralized filter — "
                    "any caller can reach this endpoint without authentication."
                ),
                fix_hint=(
                    "Add @PreAuthorize(\"isAuthenticated()\") or a role-specific expression, "
                    "or add @PermitAll if public access is intentional."
                ),
                limitations=[
                    "Only emitted for annotation_based security model — not for filter_based",
                    "Does not detect permit-all at class level (class-level @PermitAll may suppress this finding)",
                ],
                related_symbols=[ep.controller_class],
            ))

        return findings


# ---------------------------------------------------------------------------
# SEC-002: CVE-2025-41248 — @PreAuthorize on inherited method from generic supertype
# ---------------------------------------------------------------------------

class _SEC002PreAuthorizeGenericInheritance:
    pattern_id = "SEC-002"
    severity = "high"

    def analyze(
        self,
        cir: "CanonicalRepositoryIR",
        tx_index: Optional[TransactionBoundaryIndex],
        root: Optional[Path],
        *,
        model: Optional[SpringSemanticModel] = None,
    ) -> list[SpringFinding]:
        # Use shared inheritance graph when available; fall back to local build.
        if model is not None:
            _parent_of = model.inheritance.parent_of
            _generic_parents = model.inheritance.generic_parents
        else:
            _parent_of = _build_extends_map(cir)
            _generic_parents = None  # determined via regex below

        findings: list[SpringFinding] = []
        seen: set[str] = set()

        for ep in cir.endpoints:
            if ep.inheritance_depth <= 0:
                continue
            if ep.security is None:
                continue
            if not ep.security.policy.startswith("spring_pre"):
                continue

            # Resolve parent class signature for this controller
            parent_sig = _parent_of.get(ep.controller_class, "")
            if _generic_parents is not None:
                has_generics = ep.controller_class in _generic_parents
            else:
                has_generics = bool(_GENERIC_PARAM_RE.search(parent_sig))
            confidence = "high" if has_generics else "medium"

            key = f"{ep.controller_class}#{ep.handler_symbol}"
            if key in seen:
                continue
            seen.add(key)

            handler_simple = ep.handler_symbol.split(".")[-1]
            controller_simple = ep.controller_class.split(".")[-1]

            findings.append(SpringFinding(
                id=SpringFinding.make_id(self.pattern_id, ep.id),
                pattern_id=self.pattern_id,
                category="security",
                severity=self.severity,
                confidence=confidence,
                title=(
                    "CVE-2025-41248: @PreAuthorize on inherited method from generic supertype"
                ),
                symbol=ep.handler_symbol,
                source_file=_controller_source_file(cir, ep.controller_class),
                evidence={
                    "endpoint_id": ep.id,
                    "method": ep.method,
                    "path": ep.path,
                    "controller_class": ep.controller_class,
                    "handler_symbol": ep.handler_symbol,
                    "inheritance_depth": ep.inheritance_depth,
                    "security_policy": ep.security.policy,
                    "parent_signature": parent_sig or "unknown",
                    "parent_has_generics": has_generics,
                },
                explanation=(
                    f"{handler_simple} in {controller_simple} is inherited "
                    f"(depth={ep.inheritance_depth}) and secured with @PreAuthorize. "
                    "CVE-2025-41248: Spring Security cannot properly resolve generic type "
                    "parameters when evaluating @PreAuthorize expressions on methods inherited "
                    "from parameterized supertypes — the authorization expression may be silently "
                    "bypassed, granting unauthenticated or unprivileged access."
                ),
                fix_hint=(
                    "Override the method explicitly in the concrete controller class and "
                    "re-declare @PreAuthorize on the override. Apply the Spring Security "
                    "CVE-2025-41248 patch (6.3.x / 6.2.x / 5.8.x)."
                ),
                limitations=[
                    "Generic type detection is regex-based on extends edge signatures",
                    "Inheritance depth > 0 does not guarantee the method is from the parameterized supertype",
                ],
                related_symbols=[ep.controller_class],
            ))

        return findings


# ---------------------------------------------------------------------------
# SEC-003: @Transactional on @Controller/@RestController (TX in wrong layer)
# ---------------------------------------------------------------------------

class _SEC003TransactionalOnController:
    pattern_id = "SEC-003"
    severity = "medium"

    def analyze(
        self,
        cir: "CanonicalRepositoryIR",
        tx_index: Optional[TransactionBoundaryIndex],
        root: Optional[Path],
        *,
        model: Optional[SpringSemanticModel] = None,
    ) -> list[SpringFinding]:
        if tx_index is None:
            return []

        # Collect controller FQNs from endpoints
        controller_fqns: set[str] = {ep.controller_class for ep in cir.endpoints if ep.controller_class}

        if not controller_fqns:
            return []

        findings: list[SpringFinding] = []
        seen: set[str] = set()

        for boundary in tx_index.all_boundaries():
            # Class-level @Transactional on a controller
            if boundary.scope == "class":
                fqn = boundary.symbol
                if fqn not in controller_fqns:
                    continue
                if fqn in seen:
                    continue
                seen.add(fqn)

                simple = fqn.split(".")[-1]
                findings.append(SpringFinding(
                    id=SpringFinding.make_id(self.pattern_id, boundary.symbol),
                    pattern_id=self.pattern_id,
                    category="security",
                    severity=self.severity,
                    confidence="medium",
                    title=f"@Transactional on @Controller class {simple} — TX in wrong layer",
                    symbol=boundary.symbol,
                    source_file=boundary.source_file,
                    evidence={
                        "controller_class": fqn,
                        "tx_scope": "class",
                        "propagation": boundary.propagation,
                        "read_only": boundary.read_only,
                    },
                    explanation=(
                        f"{simple} is annotated with both a Spring MVC controller annotation "
                        "and @Transactional at class level. The controller layer should only "
                        "orchestrate calls — owning a transaction boundary here couples HTTP "
                        "lifecycle to DB transaction scope, makes error handling unpredictable, "
                        "and prevents proper separation of concerns."
                    ),
                    fix_hint=(
                        "Move @Transactional to the service layer. Controllers should delegate "
                        "to @Service beans that own transaction boundaries."
                    ),
                    limitations=[
                        "Detection relies on controller FQNs from cir.endpoints — "
                        "controllers with no mapped endpoints are not detected",
                    ],
                    related_symbols=[],
                ))

            # Method-level @Transactional on a controller method
            elif boundary.scope == "method":
                # symbol is like "com.example.FooController#doSomething"
                class_fqn = boundary.symbol.split("#")[0]
                if class_fqn not in controller_fqns:
                    continue
                if boundary.symbol in seen:
                    continue
                seen.add(boundary.symbol)

                simple_class = class_fqn.split(".")[-1]
                simple_method = boundary.symbol.split("#")[-1]

                findings.append(SpringFinding(
                    id=SpringFinding.make_id(self.pattern_id, boundary.symbol),
                    pattern_id=self.pattern_id,
                    category="security",
                    severity=self.severity,
                    confidence="medium",
                    title=(
                        f"@Transactional on controller method "
                        f"{simple_class}.{simple_method} — TX in wrong layer"
                    ),
                    symbol=boundary.symbol,
                    source_file=boundary.source_file,
                    evidence={
                        "controller_class": class_fqn,
                        "handler_method": simple_method,
                        "tx_scope": "method",
                        "propagation": boundary.propagation,
                        "read_only": boundary.read_only,
                        "modifiers": list(boundary.modifiers),
                    },
                    explanation=(
                        f"{simple_class}.{simple_method} is a controller handler with "
                        "@Transactional. Transaction boundaries in the controller layer couple "
                        "HTTP request lifecycle to database transactions. Exceptions from "
                        "view rendering or response serialization can trigger unexpected "
                        "rollbacks; long transactions hold DB connections across slow HTTP paths."
                    ),
                    fix_hint=(
                        "Extract the transactional logic into a @Service method and call it "
                        f"from {simple_method}. The controller should only handle HTTP concerns."
                    ),
                    limitations=[
                        "Detection relies on controller FQNs from cir.endpoints",
                    ],
                    related_symbols=[class_fqn],
                ))

        return findings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_extends_map(cir: "CanonicalRepositoryIR") -> dict[str, str]:
    """Build child_fqn → parent_signature from `extends` edges in cir.dependencies."""
    result: dict[str, str] = {}
    for edge in cir.dependencies:
        if not isinstance(edge, dict):
            continue
        if edge.get("type") != "extends":
            continue
        child = edge.get("from") or ""
        parent = edge.get("to") or ""
        if child and parent:
            result[child] = parent
    return result


def _controller_source_file(cir: "CanonicalRepositoryIR", controller_fqn: str) -> str:
    """Return the source_file for a controller class from cir.files, or empty string."""
    simple = controller_fqn.split(".")[-1]
    if not simple:
        return ""
    for path in cir.files:
        if isinstance(path, str) and (
            path.endswith(f"{simple}.java") or path.endswith(f"{simple}.kt")
        ):
            return path
    return ""


def _deduplicate(findings: list[SpringFinding]) -> list[SpringFinding]:
    seen: set[str] = set()
    out: list[SpringFinding] = []
    for f in findings:
        if f.id not in seen:
            seen.add(f.id)
            out.append(f)
    return out


_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}

# ---------------------------------------------------------------------------
# Default pattern registry
# ---------------------------------------------------------------------------

_DEFAULT_SECURITY_PATTERNS: list[SecurityPattern] = [
    _SEC001UnsecuredEndpoint(),             # type: ignore[list-item]
    _SEC002PreAuthorizeGenericInheritance(),  # type: ignore[list-item]
    _SEC003TransactionalOnController(),      # type: ignore[list-item]
]


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class SecurityScanner:
    """Runs registered security patterns against a CIR + optional TX index.

    Usage:
        scanner = SecurityScanner()
        findings = scanner.analyze(cir, tx_index=tx_index, root=Path("/repo"))
        # or with pre-built model (shares inheritance graph, avoids rebuilds):
        findings = scanner.analyze(cir, tx_index=tx_index, root=Path("/repo"), model=model)

    Never raises. Pattern errors are silently swallowed (finding missed, not crash).
    """

    def __init__(self, patterns: Optional[list[SecurityPattern]] = None):
        self.patterns: list[SecurityPattern] = (
            patterns if patterns is not None else _DEFAULT_SECURITY_PATTERNS
        )

    def analyze(
        self,
        cir: "CanonicalRepositoryIR",
        tx_index: Optional[TransactionBoundaryIndex] = None,
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
# Convenience: run full security audit from CIR
# ---------------------------------------------------------------------------

def run_security_audit(
    cir: "CanonicalRepositoryIR",
    *,
    root: Optional[Path] = None,
    scope: str = "all",
    min_severity: str = "low",
    patterns: Optional[list[SecurityPattern]] = None,
    tx_index: Optional[TransactionBoundaryIndex] = None,
    model: Optional[SpringSemanticModel] = None,
) -> SpringAuditResult:
    """Run security surface audit and return a SpringAuditResult.

    Args:
        cir:          CanonicalRepositoryIR from build_canonical_ir().
        root:         Repo root Path (for source-based analysis if needed).
        scope:        "all" | "security" (reserved — always "security" for this function).
        min_severity: Filter findings below this severity.
        patterns:     Override default pattern list (for testing).
        tx_index:     Pre-built TransactionBoundaryIndex (built from cir if None).
        model:        Pre-built SpringSemanticModel (avoids duplicate build in CLI).
    """
    _sev_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    min_rank = _sev_rank.get(min_severity, 3)

    t0 = time.monotonic()

    if model is not None:
        tx_index = model.tx_index
    elif tx_index is None:
        tx_index = build_tx_index(cir)

    scanner = SecurityScanner(patterns=patterns)
    findings = scanner.analyze(cir, tx_index=tx_index, root=root, model=model)

    findings = [f for f in findings if _sev_rank.get(f.severity, 9) <= min_rank]

    elapsed_ms = round((time.monotonic() - t0) * 1000, 1)

    result = SpringAuditResult(
        repo_id=getattr(cir, "cir_hash", "")[:16],
        spring_detected=True,
        scope="security",
        findings=findings,
        limitations=[
            "SEC-001: only emitted for annotation_based security model",
            "SEC-002: generic type detection is regex-based on extends edge signatures",
            "SEC-003: only detects controllers visible via cir.endpoints",
        ],
        metadata={
            "endpoints_analyzed": len(cir.endpoints),
            "security_model": cir.metadata.get("security_model", "unknown"),
            "analysis_time_ms": elapsed_ms,
        },
    )
    return result.finalize()

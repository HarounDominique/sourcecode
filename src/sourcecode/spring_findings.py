"""spring_findings.py — Shared finding schema for Spring semantic audit.

SpringFinding is the canonical output unit.
SpringAuditResult is the top-level envelope returned by CLI and MCP.

IDs are deterministic: same symbol + pattern → same ID across runs.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


# ---------------------------------------------------------------------------
# SpringFinding
# ---------------------------------------------------------------------------

@dataclass
class SpringFinding:
    """Single audit finding — one anomaly in one symbol."""

    id: str                         # deterministic: "{pattern_id}-{symbol_hash[:12]}"
    pattern_id: str                 # "TX-001", "SEC-001", ...
    category: str                   # "tx" | "security"
    severity: str                   # "critical" | "high" | "medium" | "low"
    confidence: str                 # "high" | "medium" | "low"
    title: str
    symbol: str                     # FQN of affected symbol
    source_file: str
    evidence: dict                  # pattern-specific structured evidence
    explanation: str                # 2-3 sentences: what + why it matters
    fix_hint: str                   # one actionable sentence
    limitations: list[str] = field(default_factory=list)
    related_symbols: list[str] = field(default_factory=list)

    @staticmethod
    def make_id(pattern_id: str, symbol: str) -> str:
        h = hashlib.sha256(f"{pattern_id}:{symbol}".encode()).hexdigest()[:12]
        return f"{pattern_id}-{h}"

    def to_dict(self) -> dict:
        d: dict = {
            "id": self.id,
            "pattern_id": self.pattern_id,
            "category": self.category,
            "severity": self.severity,
            "confidence": self.confidence,
            "title": self.title,
            "symbol": self.symbol,
            "source_file": self.source_file,
            "evidence": self.evidence,
            "explanation": self.explanation,
            "fix_hint": self.fix_hint,
        }
        if self.limitations:
            d["limitations"] = self.limitations
        if self.related_symbols:
            d["related_symbols"] = self.related_symbols
        return d


# ---------------------------------------------------------------------------
# SpringAuditResult
# ---------------------------------------------------------------------------

@dataclass
class SpringAuditResult:
    """Top-level envelope returned by spring-audit command and MCP tool."""

    schema_version: str = "1.0"
    repo_id: str = ""
    git_head: str = ""
    generated_at: str = ""
    spring_detected: bool = False
    scope: str = "all"              # "all" | "tx" | "security"
    findings: list[SpringFinding] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    # Populated by finalize()
    summary: dict = field(default_factory=dict)

    def finalize(self) -> "SpringAuditResult":
        """Compute summary stats. Call after all findings are added."""
        if not self.generated_at:
            self.generated_at = datetime.now(timezone.utc).isoformat()

        by_severity: dict[str, int] = {
            "critical": 0, "high": 0, "medium": 0, "low": 0
        }
        by_category: dict[str, int] = {}
        for f in self.findings:
            by_severity[f.severity] = by_severity.get(f.severity, 0) + 1
            by_category[f.category] = by_category.get(f.category, 0) + 1

        # Overall confidence: lowest confidence of any high/critical finding
        high_findings = [f for f in self.findings if f.severity in ("high", "critical")]
        if not high_findings:
            conf_level = "high"
        elif all(f.confidence == "high" for f in high_findings):
            conf_level = "high"
        elif any(f.confidence == "low" for f in high_findings):
            conf_level = "low"
        else:
            conf_level = "medium"

        self.summary = {
            "total_findings": len(self.findings),
            "by_severity": by_severity,
            "by_category": by_category,
            "confidence_level": conf_level,
        }
        return self

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "repo_id": self.repo_id,
            "git_head": self.git_head,
            "generated_at": self.generated_at,
            "spring_detected": self.spring_detected,
            "scope": self.scope,
            "summary": self.summary,
            "findings": [f.to_dict() for f in self.findings],
            "limitations": self.limitations,
            "metadata": self.metadata,
        }


# ---------------------------------------------------------------------------
# Shared engine utilities — used by TxPatternEngine and SecurityScanner
# ---------------------------------------------------------------------------

SEVERITY_ORDER: dict[str, int] = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def deduplicate_findings(findings: list[SpringFinding]) -> list[SpringFinding]:
    seen: set[str] = set()
    out: list[SpringFinding] = []
    for f in findings:
        if f.id not in seen:
            seen.add(f.id)
            out.append(f)
    return out

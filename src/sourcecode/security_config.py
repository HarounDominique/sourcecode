"""Custom security annotation configuration (BUG-3).

Enterprise Spring projects routinely guard endpoints with bespoke authorization
annotations (e.g. ``@M3FiltroSeguridad(nombreRecurso=..., nivelRequerido=...)``)
instead of the standard ``@PreAuthorize`` / ``@Secured`` set. Without knowing
those names, the endpoint surface reports ``policy: none_detected`` for every
protected route, which makes ``endpoints`` / ``spring-audit`` blind in exactly
the repos that most need auditing.

This module loads ``sourcecode.config.json`` from a repo root and exposes the
custom annotation specs used by the canonical security extractor.

Best-effort by design: a missing file, malformed JSON, or unexpected shape all
yield an empty list, so repos without a config behave exactly as before.

Config shape::

    {
      "customSecurityAnnotations": [
        {
          "fullyQualifiedName": "com.example.security.M3FiltroSeguridad",
          "shortName": "M3FiltroSeguridad",
          "resourceParam": "nombreRecurso",
          "levelParam": "nivelRequerido",
          "riskLevel": "custom"
        }
      ]
    }
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

CONFIG_FILENAME = "sourcecode.config.json"


@dataclass(frozen=True)
class CustomSecuritySpec:
    """One custom security annotation the analyzer should recognize."""

    short_name: str            # e.g. "M3FiltroSeguridad" (no leading @)
    fqn: str = ""              # fully-qualified name (optional)
    resource_param: str = ""   # annotation attribute naming the protected resource
    level_param: str = ""      # annotation attribute naming the required level
    risk_level: str = "custom"

    @property
    def marker(self) -> str:
        """Annotation token as it appears in source and SymbolRecord.annotations."""
        return f"@{self.short_name}"


def load_custom_security(root: Optional[Path]) -> list[CustomSecuritySpec]:
    """Load custom security specs from ``<root>/sourcecode.config.json``.

    Returns [] for any error or absent config — never raises.
    """
    if root is None:
        return []
    try:
        cfg_path = root / CONFIG_FILENAME
        if not cfg_path.is_file():
            return []
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        return []

    raw = data.get("customSecurityAnnotations") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []

    specs: list[CustomSecuritySpec] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        short = str(item.get("shortName") or "").strip().lstrip("@")
        fqn = str(item.get("fullyQualifiedName") or "").strip()
        if not short and fqn:
            short = fqn.rsplit(".", 1)[-1]
        if not short:
            continue
        specs.append(
            CustomSecuritySpec(
                short_name=short,
                fqn=fqn,
                resource_param=str(item.get("resourceParam") or "").strip(),
                level_param=str(item.get("levelParam") or "").strip(),
                risk_level=str(item.get("riskLevel") or "custom").strip() or "custom",
            )
        )
    return specs


def capture_markers(specs: "list[CustomSecuritySpec]") -> "frozenset[str]":
    """Annotation tokens whose argument lists must be captured during extraction."""
    return frozenset(s.marker for s in specs)

"""pr_comment_renderer.py — Renders review-pr output as a GitHub PR comment.

Mandatory 5-section format:
  1. PR Change Summary       — FACT only
  2. Impacted Execution Flow — STRUCTURAL SIGNAL or FACT only, per-step evidence
  3. Review Priority Order   — ranked by evidence-backed impact
  4. Risk / Impact Signals   — labeled signals, never "risk" as conclusion
  5. Review Guidance         — uncertain items, evidence gaps

Contract:
- Every statement carries an explicit epistemic label.
- No speculation without label.
- No hidden confidence blending.
- Sections omitted when evidence is insufficient.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

_BADGE: dict[str, str] = {
    "FACT":                      "`FACT`",
    "STRUCTURAL SIGNAL":         "`STRUCTURAL SIGNAL`",
    "INFERRED (LOW CONFIDENCE)": "`INFERRED (LOW CONFIDENCE)`",
    "OMITTED":                   "`OMITTED`",
}

_STRUCTURAL_EVIDENCE = frozenset({"direct_injection", "direct_call"})

_MAX_PRIORITY_FILES = 10
_MAX_FLOW_PATHS = 3
_MAX_SIGNALS = 12
_MAX_UNCERTAIN = 6


def _badge(level: str) -> str:
    return _BADGE.get(level, f"`{level}`")


def _short(path: str) -> str:
    parts = Path(path).parts
    return "/".join(parts[-2:]) if len(parts) > 2 else path


def _evidence_to_epistemic(evidence_level: str) -> str:
    if evidence_level in _STRUCTURAL_EVIDENCE:
        return "STRUCTURAL SIGNAL"
    if evidence_level == "heuristic_only":
        return "INFERRED (LOW CONFIDENCE)"
    return "OMITTED"


# ── Section 1: PR Change Summary ──────────────────────────────────────────────

def _section_change_summary(out: dict) -> str:
    runtime: list[dict] = out.get("runtime_changes", [])
    build: dict = out.get("build_changes", {})
    build_files: list[str] = build.get("files", []) if build else []
    base = out.get("base_ref") or out.get("since") or "HEAD~1"

    committed: list[dict] = out.get("committed_changes", [])
    uncommitted: list[dict] = out.get("uncommitted_changes", [])

    lines = [
        "### 1. PR Change Summary",
        "",
        f"**Diff:** `git diff {base}...HEAD` &nbsp;|&nbsp; "
        f"**Committed:** {len(committed)} files {_badge('FACT')} &nbsp;|&nbsp; "
        f"**Unstaged:** {len(uncommitted)} files {_badge('FACT')}",
        "",
    ]

    if uncommitted:
        lines.append(
            f"> **Note:** {len(uncommitted)} file(s) in working tree not committed — "
            "present in analysis, absent from PR diff."
        )
        lines.append("")

    if not runtime and not build_files:
        lines.append("*No runtime files changed.*")
        return "\n".join(lines)

    lines += ["| File | Artifact Type | Role | Source |",
              "|------|--------------|------|--------|"]

    for rc in runtime:
        path = rc.get("path", "")
        atype = rc.get("artifact_type", "")
        effect = rc.get("change_effect", {})
        stmt = effect.get("statement", "") if isinstance(effect, dict) else ""
        diff_src = rc.get("diff_source", "committed")
        src_label = "committed" if "committed" in diff_src else "unstaged"
        lines.append(f"| `{_short(path)}` | `{atype}` | {stmt} | {src_label} |")

    for bf in build_files:
        lines.append(f"| `{_short(bf)}` | `build_manifest` | build / dependency configuration | committed |")

    lines.append("")
    lines.append(f"*All file entries above are {_badge('FACT')} — directly observed in diff.*")
    return "\n".join(lines)


# ── Section 2: Impacted Execution Flow ────────────────────────────────────────

def _section_execution_flow(out: dict) -> str:
    behavioral: list[dict] = out.get("behavioral_impact", [])

    # Only STRUCTURAL SIGNAL or FACT paths — exclude heuristic_only
    strong = [
        bi for bi in behavioral
        if bi.get("evidence_level", "none") in _STRUCTURAL_EVIDENCE
    ]
    weak = [
        bi for bi in behavioral
        if bi.get("evidence_level", "none") == "heuristic_only"
    ]

    lines = ["### 2. Impacted Execution Flow", ""]

    if not strong and not weak:
        lines.append("*No traceable execution flow — insufficient structural evidence.*")
        return "\n".join(lines)

    if strong:
        for bi in strong[:_MAX_FLOW_PATHS]:
            ev = bi.get("evidence_level", "")
            ep = _evidence_to_epistemic(ev)
            entry = bi.get("entry_point", "")
            affected: list[str] = bi.get("affected_path", [])
            end_state = bi.get("end_state", "")
            trace: list[str] = bi.get("trace", [])

            end_state_ep = bi.get("end_state_epistemic_level", "INFERRED (LOW CONFIDENCE)")
            steps = [f"`{entry}`"] + [f"`{s}`" for s in affected]
            if end_state:
                steps.append(f"**{end_state}** {_badge(end_state_ep)}")

            lines.append(f"**Flow:** {' → '.join(steps)}")
            lines.append(f"**Evidence:** {_badge(ep)}")

            if trace:
                for t in trace:
                    lines.append(f"- {t}")
            lines.append("")

    if weak:
        lines.append(f"**Excluded from flow** ({len(weak)} path(s) — evidence insufficient):")
        for bi in weak[:3]:
            entry = bi.get("entry_point", "")
            lines.append(
                f"- `{entry}` path — {_badge('INFERRED (LOW CONFIDENCE)')}: "
                "class reference detected but no injection or call evidence"
            )
        lines.append("")

    return "\n".join(lines).rstrip()


# ── Section 3: Review Priority Order ─────────────────────────────────────────

def _section_priority_order(out: dict) -> str:
    order: list[str] = out.get("suggested_review_order", []) or out.get("review_hotspots", [])
    runtime: list[dict] = out.get("runtime_changes", [])

    cls_map: dict[str, dict] = {rc["path"]: rc for rc in runtime}

    lines = ["### 3. Review Priority Order", ""]

    if not order:
        lines.append("*No priority order available — diff scope too narrow to rank.*")
        return "\n".join(lines)

    for i, path in enumerate(order[:_MAX_PRIORITY_FILES], 1):
        rc = cls_map.get(path, {})
        effect = rc.get("change_effect", {})
        stmt = effect.get("statement", "application source") if isinstance(effect, dict) else "application source"
        ep = effect.get("epistemic_level", "STRUCTURAL SIGNAL") if isinstance(effect, dict) else "STRUCTURAL SIGNAL"
        atype = rc.get("artifact_type", "source")
        lines.append(f"{i}. `{_short(path)}` — {_badge(ep)}: {atype} — {stmt}")

    return "\n".join(lines)


# ── Section 4: Risk / Impact Signals ─────────────────────────────────────────

def _section_signals(out: dict) -> str:
    lines = ["### 4. Risk / Impact Signals", ""]
    signals: list[str] = []

    # Security files in diff — FACT (files are in diff)
    sec = out.get("security_impact", {})
    if sec:
        files: list[str] = sec.get("affected_resources", [])
        ep = sec.get("epistemic_level", "STRUCTURAL SIGNAL")
        basis = sec.get("basis", "")
        if files:
            names = ", ".join(f"`{_short(f)}`" for f in files)
            signals.append(f"{_badge(ep)}: security-classified files changed — {names}")
            if basis:
                signals.append(f"  *Classification basis: {basis}*")
        risk_ep = sec.get("risk_epistemic_level", "INFERRED (LOW CONFIDENCE)")
        signals.append(
            f"{_badge(risk_ep)}: authentication / access-control behavior may be affected — "
            "inspect changed security files to confirm"
        )

    # Transactional boundary — STRUCTURAL SIGNAL if @Transactional detected, else INFERRED
    txn = out.get("transactional_impact", {})
    if txn:
        files_t: list[str] = txn.get("affected_transactions", [])
        ep_t = txn.get("epistemic_level", "STRUCTURAL SIGNAL")
        basis_t = txn.get("basis", "")
        risk_ep_t = txn.get("risk_epistemic_level", "INFERRED (LOW CONFIDENCE)")
        if files_t:
            names_t = ", ".join(f"`{_short(f)}`" for f in files_t)
            signals.append(f"{_badge(ep_t)}: service/business-logic files changed — {names_t}")
            if basis_t:
                signals.append(f"  *Classification basis: {basis_t}*")
        signals.append(
            f"{_badge(risk_ep_t)}: transactional boundary may be affected — "
            "@Transactional scope not confirmed by AST"
        )

    # Configuration files — FACT
    cfg = out.get("configuration_impact", {})
    if cfg:
        cfg_files: list[str] = cfg.get("changed_configs", [])
        if cfg_files:
            names_c = ", ".join(f"`{_short(f)}`" for f in cfg_files)
            signals.append(f"{_badge('FACT')}: configuration files modified — {names_c}")

    # Behavioral impact signals — use each item's epistemic_level
    behavioral: list[dict] = out.get("behavioral_impact", [])
    for bi in behavioral[:_MAX_FLOW_PATHS]:
        for item in bi.get("impact", []):
            stmt = item.get("statement", "")
            ep_i = item.get("epistemic_level", "INFERRED (LOW CONFIDENCE)")
            support = item.get("support", "")
            if stmt:
                signals.append(f"{_badge(ep_i)}: {stmt}")
                if support:
                    signals.append(f"  *{support}*")

    # Runtime notes from execution paths
    exec_paths: list[dict] = out.get("execution_paths", [])
    seen_notes: set[str] = set()
    for ep_dict in exec_paths:
        entry_notes: list = ep_dict.get("entry_point", {}).get("notes", [])
        path_notes: list = [n for item in ep_dict.get("path", []) for n in item.get("notes", [])]
        for note_obj in entry_notes + path_notes:
            if isinstance(note_obj, dict):
                note = note_obj.get("note", "")
                n_ep = note_obj.get("epistemic_level", "STRUCTURAL SIGNAL")
                if note and note not in seen_notes:
                    signals.append(f"{_badge(n_ep)}: {note}")
                    seen_notes.add(note)

    if not signals:
        lines.append("*No impact signals detected — insufficient evidence.*")
        return "\n".join(lines)

    lines.extend(signals[:_MAX_SIGNALS])
    return "\n".join(lines)


# ── Section 5: Review Guidance ────────────────────────────────────────────────

def _section_guidance(out: dict) -> str:
    lines = ["### 5. Review Guidance", ""]

    # Inspect first — top priority files
    order: list[str] = out.get("suggested_review_order", []) or out.get("review_hotspots", [])
    runtime: list[dict] = out.get("runtime_changes", [])
    cls_map: dict[str, dict] = {rc["path"]: rc for rc in runtime}

    if order:
        lines.append("**Inspect first:**")
        for path in order[:3]:
            rc = cls_map.get(path, {})
            effect = rc.get("change_effect", {})
            stmt = effect.get("statement", "") if isinstance(effect, dict) else ""
            ep = effect.get("epistemic_level", "STRUCTURAL SIGNAL") if isinstance(effect, dict) else "STRUCTURAL SIGNAL"
            label = f" — {stmt}" if stmt else ""
            lines.append(f"- `{_short(path)}`{label} {_badge(ep)}")
        lines.append("")

    # Uncertain — heuristic-only behavioral paths
    behavioral: list[dict] = out.get("behavioral_impact", [])
    weak_paths = [
        bi for bi in behavioral
        if bi.get("evidence_level", "none") == "heuristic_only"
    ]
    if weak_paths:
        lines.append("**Uncertain (heuristic-only — verify manually):**")
        for bi in weak_paths[:_MAX_UNCERTAIN]:
            entry = bi.get("entry_point", "")
            affected: list[str] = bi.get("affected_path", [])
            path_str = " → ".join(affected) if affected else "unknown"
            lines.append(
                f"- `{entry}` → {path_str} "
                f"{_badge('INFERRED (LOW CONFIDENCE)')}: class reference only, no injection/call proof"
            )
        lines.append("")

    # Evidence gaps
    gaps: list[str] = out.get("gaps", [])
    cov = out.get("test_coverage_risk", {})
    test_files: list[str] = cov.get("changed_files_without_tests", [])
    test_basis = cov.get("basis", "")
    cov_ep = cov.get("epistemic_level", "INFERRED (LOW CONFIDENCE)")

    gap_items: list[str] = list(gaps)
    if test_files:
        shown = test_files[:5]
        omitted = len(test_files) - len(shown)
        names = ", ".join(f"`{_short(f)}`" for f in shown)
        suffix = f" +{omitted} more" if omitted else ""
        gap_items.append(
            f"changed files without test coverage: {names}{suffix} "
            f"{_badge(cov_ep)}"
            + (f" — {test_basis}" if test_basis else "")
        )

    if gap_items:
        lines.append("**Evidence gaps / missing coverage:**")
        for g in gap_items:
            lines.append(f"- {g}")
    else:
        lines.append("*No evidence gaps detected.*")

    return "\n".join(lines)


# ── Entry point ───────────────────────────────────────────────────────────────

def render_github_comment(out: dict) -> str:
    """Render review-pr TaskOutput dict as a GitHub PR comment (Markdown).

    5 mandatory sections. Every statement carries an explicit epistemic label.
    """
    base = out.get("base_ref") or out.get("since") or "HEAD~1"
    header = (
        "## sourcecode PR Analysis\n\n"
        f"**Base:** `{base}` &nbsp;|&nbsp; "
        f"**Review type:** pull request &nbsp;|&nbsp; "
        f"**Epistemic contract:** FACT · STRUCTURAL SIGNAL · INFERRED (LOW CONFIDENCE) · OMITTED"
    )

    sections = [
        header,
        _section_change_summary(out),
        _section_execution_flow(out),
        _section_priority_order(out),
        _section_signals(out),
        _section_guidance(out),
    ]

    footer = (
        "*Generated by [sourcecode](https://github.com/sourcecode-ai/sourcecode). "
        "Labels: "
        "`FACT` = diff/AST evidence · "
        "`STRUCTURAL SIGNAL` = annotation/import/wiring · "
        "`INFERRED (LOW CONFIDENCE)` = heuristic pattern · "
        "`OMITTED` = insufficient evidence.*"
    )
    sections.append(footer)

    return "\n\n---\n\n".join(s for s in sections if s.strip())

"""output_budget.py — Progressive output trimming for LLM context safety.

Single entry point: trim_to_budget(data, budget_bytes, label="").

Protects against runaway output on large repos by progressively dropping
or truncating lower-priority sections while preserving the highest-value signals.

Trim priority (lowest → highest value, trimmed first):
  1. Inner boost/match lists inside symptom_explain, transactional_boundaries, mybatis
  2. Medium-value lists: code_notes, env_map, related_notes
  3. High-cost sections: relevant_files, suspected_areas, key_dependencies
  4. Last resort: drop non-essential sections entirely

Always preserved: project_type, project_summary, architecture_summary, task,
  goal, confidence, analysis_gaps, error, ci_decision, summary, warnings, hint.
"""

from __future__ import annotations

import json
import sys
from typing import Any


# Sections that must never be removed (structural identity + user-facing signals)
_ALWAYS_KEEP: frozenset[str] = frozenset({
    "task", "goal",
    "project_type", "project_summary", "architecture_summary",
    "schema_version",
    "confidence", "confidence_summary", "analysis_gaps", "gaps",
    "error", "error_code", "message",
    "ci_decision", "summary",
    "warnings", "hint", "_budget_note",
})


# Each step: (top_key, inner_key_or_None, max_items)
# inner_key=None → trim/drop top_key itself
# max_items=0    → drop the section entirely
_TRIM_SCHEDULE: list[tuple[str, str | None, int]] = [
    # Step 1 — trim inner lists in structured sections
    ("symptom_explain",          "boosts",               10),
    ("symptom_explain",          "content_matches",       5),
    ("symptom_explain",          "commit_matches",        5),
    ("symptom_explain",          "synonym_matches",       3),
    ("transactional_boundaries", "classes",               5),
    ("mybatis",                  "dto_mappers",           5),
    # Step 2 — trim medium-value top-level lists
    ("code_notes",               None,                    5),
    ("env_map",                  None,                    5),
    ("related_notes",            None,                    5),
    # Step 3 — trim higher-cost sections
    ("relevant_files",           None,                   15),
    ("suspected_areas",          None,                   10),
    ("key_dependencies",         None,                   10),
    # Step 4 — more aggressive
    ("relevant_files",           None,                    8),
    ("key_dependencies",         None,                    5),
    ("suspected_areas",          None,                    5),
    ("symptom_explain",          "boosts",                0),  # drop inner list
    # Step 5 — drop non-essential sections
    ("symptom_explain",          None,                    0),
    ("code_notes",               None,                    0),
    ("env_map",                  None,                    0),
    ("related_notes",            None,                    0),
    ("angular_analysis",         None,                    0),
    ("spring_profiles",          None,                    0),
    ("execution_paths",          None,                    0),
    ("dependency_graph_summary", None,                    0),
    # Step 6 — last resort
    ("relevant_files",           None,                   10),
    ("suspected_areas",          None,                    0),
    ("key_dependencies",         None,                    0),
]


def _serialized_size(data: Any) -> int:
    """Byte size of JSON-serialized data (UTF-8)."""
    return len(json.dumps(data, ensure_ascii=False).encode("utf-8"))


def trim_to_budget(
    data: dict,
    budget_bytes: int,
    *,
    label: str = "",
    skip: bool = False,
    warn_stderr: bool = False,
) -> dict:
    """Progressively trim *data* to fit within *budget_bytes*.

    Preserves the highest-value sections. Adds ``_budget_note`` when trimming
    occurs so callers can surface it to users.

    Args:
        skip: When True (e.g. writing to a file), skip all trimming and return
              data unchanged with no ``_budget_note``.
        warn_stderr: When True, emit a WARNING line to stderr before returning
                     if trimming was applied. Used for stdout output so users
                     see the warning before the JSON payload.

    Returns data unchanged if already within budget or ``skip=True``.
    """
    if skip or _serialized_size(data) <= budget_bytes:
        return data

    result: dict = dict(data)
    original_size = _serialized_size(result)
    trimmed_sections: list[str] = []
    # Track original counts for total_omitted_items calculation.
    _original_counts: dict[str, int] = {}

    for top_key, inner_key, max_items in _TRIM_SCHEDULE:
        if _serialized_size(result) <= budget_bytes:
            break

        if top_key not in result or result[top_key] is None:
            continue

        section_val = result[top_key]

        if inner_key is None:
            # Trim or drop the top-level key
            if max_items == 0:
                if top_key in _ALWAYS_KEEP:
                    continue
                if isinstance(section_val, list):
                    _original_counts[top_key] = _original_counts.get(top_key, len(section_val))
                del result[top_key]
                trimmed_sections.append(f"{top_key}:dropped")
            elif isinstance(section_val, list) and len(section_val) > max_items:
                _original_counts[top_key] = _original_counts.get(top_key, len(section_val))
                result[top_key] = section_val[:max_items]
                trimmed_sections.append(f"{top_key}≤{max_items}")
        else:
            # Trim or drop an inner list
            if not isinstance(section_val, dict):
                continue
            if inner_key not in section_val:
                continue
            inner_val = section_val[inner_key]
            _inner_key = f"{top_key}.{inner_key}"
            if max_items == 0:
                if isinstance(inner_val, list):
                    _original_counts[_inner_key] = _original_counts.get(_inner_key, len(inner_val))
                new_sec = dict(section_val)
                del new_sec[inner_key]
                result[top_key] = new_sec
                trimmed_sections.append(f"{_inner_key}:dropped")
            elif isinstance(inner_val, list) and len(inner_val) > max_items:
                _original_counts[_inner_key] = _original_counts.get(_inner_key, len(inner_val))
                new_sec = dict(section_val)
                new_sec[inner_key] = inner_val[:max_items]
                result[top_key] = new_sec
                trimmed_sections.append(f"{_inner_key}≤{max_items}")

    final_size = _serialized_size(result)
    if trimmed_sections:
        # Build human-readable section summary for note/warning.
        _section_summary_parts: list[str] = []
        for _sk, _orig in _original_counts.items():
            _cur_key = _sk.replace(".", "/")  # normalize for display
            _section_summary_parts.append(f"{_sk} ({_orig} total)")

        note = (
            f"Output trimmed {original_size // 1024}KB → {final_size // 1024}KB "
            f"(budget {budget_bytes // 1024}KB). "
            f"Trimmed: {', '.join(trimmed_sections)}. "
            "Use --output <file> to capture full output."
        )
        if label:
            note = f"[{label}] {note}"
        result["_budget_note"] = note

        # Compute total omitted items across all truncated lists.
        total_omitted = 0
        for _sk, _orig in _original_counts.items():
            if ":dropped" in "".join(s for s in trimmed_sections if _sk in s):
                total_omitted += _orig
            else:
                # Find the last max_items cap applied to this key.
                _caps = [
                    int(s.split("≤")[1])
                    for s in trimmed_sections
                    if s.startswith(_sk + "≤")
                ]
                _cap = min(_caps) if _caps else 0
                total_omitted += max(0, _orig - _cap)

        result["_truncation_summary"] = {
            "total_omitted_items": total_omitted,
            "original_size_kb": original_size // 1024,
            "final_size_kb": final_size // 1024,
            "budget_kb": budget_bytes // 1024,
        }

        if warn_stderr:
            # Build per-section counts for the warning line.
            _warn_sections: list[str] = []
            for _sk, _orig in _original_counts.items():
                _caps = [
                    int(s.split("≤")[1])
                    for s in trimmed_sections
                    if s.startswith(_sk + "≤")
                ]
                _shown = min(_caps) if _caps else 0
                _warn_sections.append(f"{_sk} ({_shown}/{_orig})")
            _warn_line = (
                f"WARNING: Output will be trimmed "
                f"({original_size // 1024}KB → {final_size // 1024}KB, "
                f"budget {budget_bytes // 1024}KB). "
                f"Affected: {', '.join(_warn_sections)}. "
                "Use --output <file> to capture full output.\n"
            )
            if label:
                _warn_line = f"[{label}] {_warn_line}"
            sys.stderr.write(_warn_line)
            sys.stderr.flush()

    return result


# Budget constants (bytes) — used by CLI callers
BUDGET_COMPACT    = 30_000   # compact/agent main cmd
BUDGET_AGENT      = 40_000   # agent main cmd (slightly more headroom)
BUDGET_FIX_BUG   = 200_000  # fix-bug (with or without --symptom)
BUDGET_REVIEW_PR  = 100_000  # review-pr
BUDGET_ONBOARD    = 30_000   # onboard
BUDGET_EXPLAIN    = 30_000   # explain
BUDGET_REFACTOR   = 50_000   # refactor
BUDGET_DELTA      = 80_000   # delta (change impact context)
BUDGET_IMPACT     = 50_000   # impact blast-radius command

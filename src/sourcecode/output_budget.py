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
    ("relevant_files",           None,                    3),
    ("suspected_areas",          None,                    0),
    ("key_dependencies",         None,                    0),
]


def _serialized_size(data: Any) -> int:
    """Byte size of JSON-serialized data (UTF-8)."""
    return len(json.dumps(data, ensure_ascii=False).encode("utf-8"))


def trim_to_budget(data: dict, budget_bytes: int, *, label: str = "") -> dict:
    """Progressively trim *data* to fit within *budget_bytes*.

    Preserves the highest-value sections. Adds ``_budget_note`` when trimming
    occurs so callers can surface it to users.

    Returns data unchanged if already within budget.
    """
    if _serialized_size(data) <= budget_bytes:
        return data

    result: dict = dict(data)
    original_size = _serialized_size(result)
    trimmed_sections: list[str] = []

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
                del result[top_key]
                trimmed_sections.append(f"{top_key}:dropped")
            elif isinstance(section_val, list) and len(section_val) > max_items:
                result[top_key] = section_val[:max_items]
                trimmed_sections.append(f"{top_key}≤{max_items}")
        else:
            # Trim or drop an inner list
            if not isinstance(section_val, dict):
                continue
            if inner_key not in section_val:
                continue
            inner_val = section_val[inner_key]
            if max_items == 0:
                new_sec = dict(section_val)
                del new_sec[inner_key]
                result[top_key] = new_sec
                trimmed_sections.append(f"{top_key}.{inner_key}:dropped")
            elif isinstance(inner_val, list) and len(inner_val) > max_items:
                new_sec = dict(section_val)
                new_sec[inner_key] = inner_val[:max_items]
                result[top_key] = new_sec
                trimmed_sections.append(f"{top_key}.{inner_key}≤{max_items}")

    final_size = _serialized_size(result)
    if trimmed_sections:
        note = (
            f"Output trimmed {original_size // 1024}KB → {final_size // 1024}KB "
            f"(budget {budget_bytes // 1024}KB). "
            f"Trimmed: {', '.join(trimmed_sections)}. "
            "Use --output <file> to capture full output."
        )
        if label:
            note = f"[{label}] {note}"
        result["_budget_note"] = note

    return result


# Budget constants (bytes) — used by CLI callers
BUDGET_COMPACT    = 30_000   # compact/agent main cmd
BUDGET_AGENT      = 40_000   # agent main cmd (slightly more headroom)
BUDGET_FIX_BUG   = 100_000  # fix-bug (with or without --symptom)
BUDGET_REVIEW_PR  = 100_000  # review-pr
BUDGET_ONBOARD    = 30_000   # onboard
BUDGET_EXPLAIN    = 30_000   # explain
BUDGET_REFACTOR   = 50_000   # refactor
BUDGET_DELTA      = 80_000   # delta (change impact context)
BUDGET_IMPACT     = 50_000   # impact blast-radius command

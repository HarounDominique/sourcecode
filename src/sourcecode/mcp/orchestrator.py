"""Agent Runtime Layer — session state machine, intent detection, workflow orchestrators.

Converts the MCP from a flat tool collection into a guided agent operating system:
- start_session: single entry point that determines state and tool sequence
- analyze_task: intent detection and targeted tool sequence recommendation
- run_pr_review_flow: auto-chains delta + review_pr + blast radius
- run_bug_investigation_flow: auto-chains fix_bug + impact + IR context
- run_feature_flow: auto-chains context + endpoints + delta + structural awareness

TODO — planned high-value Java/Spring flow presets (audit 2026-06-16, repo SAINT):
  These extend the existing orchestrator; they are NOT yet implemented.

  1. TODO: implement later — run_migrate_flow
     Preset wrapping `migrate-check`. Primary high-value entry point for
     Spring Boot 2→3 planning (audit: produces 1,356-file prioritized
     inventory in ~6s — the strongest determinante win). Should surface
     readiness_score, blocking count, per-target breakdown (jakarta /
     spring_security_6 / java_11) and estimated_effort_days.

  2. TODO: implement later — run_security_audit_flow
     Preset wrapping `spring-audit` + `endpoints`. Auto-handle the
     config-less case: when no sourcecode.config.json is present and the
     repo carries custom security annotations, emit a fallback WARNING +
     hint to add sourcecode.config.json (customAnnotations) rather than
     returning a misleading 100% none_detected result.

  3. TODO: implement later — extend R2 orchestration rule (apply_orchestration_rules)
     Inject preset (1)/(2) when detected intent maps to migration or
     security audit, mirroring the existing R2 java_no_endpoints rule.
     Requires new INTENT_MIGRATION / INTENT_SECURITY_AUDIT constants +
     _INTENT_PATTERNS entries + WORKFLOW_SEQUENCES / FLOW_RUNNERS wiring.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Optional

from sourcecode.mcp.runner import run_command

# ---------------------------------------------------------------------------
# Session state constants
# ---------------------------------------------------------------------------

SESSION_INIT = "INIT"                          # no RIS, no context
SESSION_CONTEXT_LOADED = "CONTEXT_LOADED"      # RIS fresh + complete
SESSION_STALE_CONTEXT = "STALE_CONTEXT"        # RIS exists but HEAD changed
SESSION_INCOMPLETE_CONTEXT = "INCOMPLETE_CONTEXT"  # RIS missing critical sections
SESSION_TASK_INTENT_DETECTED = "TASK_INTENT_DETECTED"
SESSION_READY_FOR_REVIEW = "READY_FOR_REVIEW"  # flow complete

# ---------------------------------------------------------------------------
# Intent constants
# ---------------------------------------------------------------------------

INTENT_PR_REVIEW = "pr_review"
INTENT_BUG_INVESTIGATION = "bug_investigation"
INTENT_FEATURE_IMPLEMENTATION = "feature_implementation"
INTENT_REFACTOR = "refactor"
INTENT_TEST_GENERATION = "test_generation"
INTENT_ORIENTATION = "orientation"

# ---------------------------------------------------------------------------
# Workflow sequences: intent → ordered tool names the agent should call
# ---------------------------------------------------------------------------

WORKFLOW_SEQUENCES: dict[str, list[str]] = {
    INTENT_PR_REVIEW: ["get_delta", "review_pr_context", "get_impact_context"],
    INTENT_BUG_INVESTIGATION: ["fix_bug_context", "get_impact_context"],
    INTENT_FEATURE_IMPLEMENTATION: ["get_compact_context", "get_endpoints", "get_delta"],
    INTENT_REFACTOR: ["get_agent_context", "modernize_context", "get_ir_summary"],
    INTENT_TEST_GENERATION: ["generate_tests_context"],
    INTENT_ORIENTATION: ["get_compact_context"],
}

WORKFLOW_DESCRIPTIONS: dict[str, str] = {
    INTENT_PR_REVIEW: "PR review: delta → execution paths → blast radius of changed classes",
    INTENT_BUG_INVESTIGATION: "Bug investigation: risk-ranked files → impact of suspect class",
    INTENT_FEATURE_IMPLEMENTATION: "Feature implementation: context → API surface → recent changes",
    INTENT_REFACTOR: "Refactor: deep context → modernization opportunities → IR coupling",
    INTENT_TEST_GENERATION: "Test generation: untested files ranked by risk",
    INTENT_ORIENTATION: "Orientation: compact context overview",
}

FLOW_RUNNERS: dict[str, str] = {
    INTENT_PR_REVIEW: "run_pr_review_flow",
    INTENT_BUG_INVESTIGATION: "run_bug_investigation_flow",
    INTENT_FEATURE_IMPLEMENTATION: "run_feature_flow",
    INTENT_REFACTOR: "run_feature_flow",
    INTENT_TEST_GENERATION: "generate_tests_context",
    INTENT_ORIENTATION: "get_compact_context",
}

# ---------------------------------------------------------------------------
# Intent detection
# ---------------------------------------------------------------------------

_INTENT_PATTERNS: list[tuple[str, list[str]]] = [
    (INTENT_PR_REVIEW, [
        r"\bpr\b", r"pull request", r"review pr", r"\bdiff\b", r"merge request",
        r"code review", r"changes in branch", r"review.*branch", r"branch.*review",
    ]),
    (INTENT_BUG_INVESTIGATION, [
        r"\bbug\b", r"\berror\b", r"\bexception\b", r"\bcrash\b", r"\bnpe\b",
        r"\bfix\b", r"\bbroken\b", r"\bfail(s|ing)?\b", r"stack.?trace",
        r"null.?pointer", r"wrong.?behav", r"\bincident\b", r"not.?work",
    ]),
    (INTENT_FEATURE_IMPLEMENTATION, [
        r"\bfeature\b", r"\bimplement\b", r"\bdevelop\b", r"new endpoint",
        r"new service", r"add.*(endpoint|service|api|feature)", r"create.*class",
        r"\bbuild\b.*new",
    ]),
    (INTENT_REFACTOR, [
        r"\brefactor\b", r"\bmodernize\b", r"clean.?up", r"technical.?debt",
        r"\bdebt\b", r"\brewrite\b", r"\brestructure\b", r"\bclean.*code\b",
    ]),
    (INTENT_TEST_GENERATION, [
        r"\btest(s|ing)?\b", r"\bcoverage\b", r"unit.?test", r"\bspec\b",
        r"write.?test", r"add.?test",
    ]),
]


def detect_intent(task_description: str) -> tuple[str, float]:
    """Return (intent, confidence). Confidence 1.0 = explicit match, 0.5 = fallback."""
    t = task_description.lower()
    for intent, patterns in _INTENT_PATTERNS:
        for pat in patterns:
            if re.search(pat, t):
                return intent, 1.0
    return INTENT_ORIENTATION, 0.5


def _extract_symptom(task_description: str) -> str:
    """Heuristic: extract error class or quoted string from task description."""
    # Quoted string
    m = re.search(r'"([^"]+)"', task_description)
    if m:
        return m.group(1)
    # Exception/Error class name
    m = re.search(r'\b(\w+(?:Exception|Error|Fault))\b', task_description)
    if m:
        return m.group(1)
    # "in ClassName"
    m = re.search(r'\bin\s+(\w+(?:Service|Controller|Repository|Handler|Manager|Rest))\b', task_description)
    if m:
        return m.group(1)
    return ""


# ---------------------------------------------------------------------------
# Orchestration rules (executable, not docs)
# ---------------------------------------------------------------------------

def apply_orchestration_rules(
    freshness: str,
    is_java: bool,
    api_surface_complete: bool,
    repo_class_count: int,
    intent: str,
    sequence: list[str],
) -> tuple[list[str], list[str]]:
    """Return (adjusted_sequence, rules_applied).

    Rules applied in priority order:
      R1 stale_cache → prepend get_delta (always sync before deep analysis)
      R2 java_no_endpoints → prepend get_endpoints (api_surface must exist first)
      R3 large_repo (>1000) → note RIS path preferred (informational, no seq change)
      R4 no_symptom_bug_flow → quality warning issued by caller (not a seq change)
    """
    seq = list(sequence)
    rules: list[str] = []

    # R1: stale cache + any flow → prepend delta refresh
    if freshness == "stale" and "get_delta" not in seq:
        seq.insert(0, "get_delta")
        rules.append("R1:stale_cache→prepend_delta")

    # R2: Java + no endpoint index → prepend get_endpoints
    if is_java and not api_surface_complete and "get_endpoints" not in seq:
        seq.insert(0, "get_endpoints")
        rules.append("R2:java_no_endpoints→prepend_get_endpoints")

    # R3: large repo informational flag
    if repo_class_count > 1000:
        rules.append("R3:large_repo→RIS_path_preferred")

    return seq, rules


# ---------------------------------------------------------------------------
# Internal execute helper
# ---------------------------------------------------------------------------

def _exec(args: list[str]) -> dict[str, Any]:
    """Call run_command, return result dict. On error returns dict with 'error' key."""
    try:
        result = run_command(args)
        if isinstance(result, dict):
            return result
        return {"raw": result}
    except Exception as exc:
        return {"_exec_error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# RIS helpers
# ---------------------------------------------------------------------------

def _cold_start(repo_path: str) -> dict[str, Any]:
    from sourcecode.ris import get_cold_start_context as _gcs
    return _gcs(Path(repo_path))


def _freshness(ris_status: str) -> str:
    if ris_status == "cold_start_ready":
        return "fresh"
    if ris_status in ("cold_start_stale", "cold_start_incomplete"):
        return "stale"
    return "missing"


def _status_to_session_state(ris_status: str) -> str:
    return {
        "cold_start_ready": SESSION_CONTEXT_LOADED,
        "cold_start_stale": SESSION_STALE_CONTEXT,
        "cold_start_incomplete": SESSION_INCOMPLETE_CONTEXT,
        "no_ris": SESSION_INIT,
    }.get(ris_status, SESSION_INIT)


def _is_java_repo(repo_path: str) -> bool:
    p = Path(repo_path)
    return (p / "pom.xml").exists() or (p / "build.gradle").exists() or (p / "build.gradle.kts").exists()


def _default_sequence_for_state(
    session_state: str, is_java: bool, api_complete: bool,
) -> list[str]:
    if session_state == SESSION_INIT:
        return ["get_compact_context"]
    if session_state == SESSION_STALE_CONTEXT:
        return ["get_delta", "analyze_task"]
    if session_state == SESSION_INCOMPLETE_CONTEXT and is_java:
        return ["get_endpoints", "analyze_task"]
    if session_state == SESSION_CONTEXT_LOADED:
        return ["analyze_task"]
    return ["get_compact_context"]


def _risk_level(freshness: str, stale: bool, class_count: int) -> str:
    if freshness == "missing":
        return "unknown"
    if stale or freshness == "stale":
        return "medium"
    if class_count > 2000:
        return "high"
    if class_count > 500:
        return "medium"
    return "low"


# ---------------------------------------------------------------------------
# start_session
# ---------------------------------------------------------------------------

def start_session_impl(repo_path: str, task_description: str = "") -> dict[str, Any]:
    """Core logic for start_session MCP tool."""
    t0 = time.monotonic()

    cold = _cold_start(repo_path)
    ris_status = cold.get("status", "no_ris")
    freshness = _freshness(ris_status)
    session_state = _status_to_session_state(ris_status)

    is_java = _is_java_repo(repo_path)
    api_surface_complete = cold.get("api_surface_complete", True)
    validation = cold.get("validation", {})
    spring_detected = validation.get("spring_detected", False)
    endpoints_count = validation.get("endpoints_found", len(cold.get("endpoints", [])))

    summary = cold.get("summary", {})
    repo_class_count: int = (
        summary.get("class_count")
        or summary.get("total_classes")
        or 0
    )

    # Intent detection
    intent: Optional[str] = None
    intent_confidence = 0.5
    flow_runner: Optional[str] = None
    if task_description.strip():
        intent, intent_confidence = detect_intent(task_description)
        flow_runner = FLOW_RUNNERS.get(intent)
        base_seq = list(WORKFLOW_SEQUENCES.get(intent, ["get_compact_context"]))
    else:
        base_seq = _default_sequence_for_state(session_state, is_java, api_surface_complete)

    # Apply orchestration rules
    seq, rules_applied = apply_orchestration_rules(
        freshness=freshness,
        is_java=is_java,
        api_surface_complete=api_surface_complete,
        repo_class_count=repo_class_count,
        intent=intent or INTENT_ORIENTATION,
        sequence=base_seq,
    )

    # Recommended next action
    next_tool = seq[0] if seq else "get_compact_context"
    if task_description.strip() and intent and flow_runner:
        next_tool = flow_runner
        next_reason = WORKFLOW_DESCRIPTIONS.get(intent, "")
    elif freshness == "missing":
        next_reason = "No RIS found — build context first (~8s for large repos, instant after)"
    elif freshness == "stale":
        next_reason = "RIS outdated — refresh delta before deeper analysis"
    elif session_state == SESSION_INCOMPLETE_CONTEXT and is_java:
        next_reason = "Java repo with no endpoint index — populate API surface first"
    else:
        next_reason = "RIS fresh — describe your task to get a targeted tool sequence"

    recommended_args: dict[str, Any] = {"repo_path": repo_path}
    if task_description.strip() and intent == INTENT_BUG_INVESTIGATION:
        symptom = _extract_symptom(task_description)
        if symptom:
            recommended_args["symptom"] = symptom

    ttfca_ms = int((time.monotonic() - t0) * 1000)

    effective_state = SESSION_TASK_INTENT_DETECTED if (task_description.strip() and intent) else session_state

    result: dict[str, Any] = {
        "session_state": effective_state,
        "repo_type": "java_spring" if spring_detected else ("java" if is_java else "unknown"),
        "cache_freshness": freshness,
        "recommended_next_action": {
            "tool": next_tool,
            "reason": next_reason,
            "args": recommended_args,
        },
        "required_tools_sequence": seq,
        "risk_level": _risk_level(freshness, cold.get("stale", False), repo_class_count),
        "entrypoint_candidates": cold.get("entrypoints", []),
        "endpoints_count": endpoints_count,
        "affected_modules": [],
        "session_meta": {
            "ttfca_ms": ttfca_ms,
            "tools_suggested": len(seq),
            "agent_decision_reduction": f"{len(seq)}/18 tools exposed",
            "orchestration_rules_applied": rules_applied,
        },
    }

    if intent:
        result["intent"] = intent
        result["intent_confidence"] = intent_confidence
        result["workflow_description"] = WORKFLOW_DESCRIPTIONS.get(intent, "")

    # Include lightweight context when available
    if session_state in (SESSION_CONTEXT_LOADED, SESSION_STALE_CONTEXT, SESSION_INCOMPLETE_CONTEXT):
        result["ris_summary"] = {
            "git_head": cold.get("git_head", ""),
            "last_updated_at": cold.get("last_updated_at", ""),
            "has_uncommitted_changes": cold.get("has_uncommitted_changes", False),
            "hotspots": cold.get("hotspots", [])[:5],
        }

    if is_java and not api_surface_complete:
        result["missing_data_hint"] = (
            "Java repo detected but endpoint index is empty. "
            "Call get_endpoints to populate API surface."
        )

    if freshness == "missing":
        result["bootstrap_hint"] = (
            "No RIS found. Call get_compact_context to bootstrap "
            "(~8s for large repos; subsequent calls instant via RIS)."
        )

    return result


# ---------------------------------------------------------------------------
# analyze_task
# ---------------------------------------------------------------------------

def analyze_task_impl(repo_path: str, task_description: str) -> dict[str, Any]:
    """Core logic for analyze_task MCP tool."""
    t0 = time.monotonic()

    intent, confidence = detect_intent(task_description)
    symptom = _extract_symptom(task_description) if intent == INTENT_BUG_INVESTIGATION else ""

    cold = _cold_start(repo_path)
    freshness = _freshness(cold.get("status", "no_ris"))
    is_java = _is_java_repo(repo_path)
    api_surface_complete = cold.get("api_surface_complete", True)

    base_seq = list(WORKFLOW_SEQUENCES.get(intent, ["get_compact_context"]))
    flow_runner = FLOW_RUNNERS.get(intent)

    seq, rules = apply_orchestration_rules(
        freshness=freshness,
        is_java=is_java,
        api_surface_complete=api_surface_complete,
        repo_class_count=0,
        intent=intent,
        sequence=base_seq,
    )

    extracted_params: dict[str, Any] = {}
    if symptom:
        extracted_params["symptom"] = symptom

    recommended_args: dict[str, Any] = {"repo_path": repo_path}
    if symptom and intent == INTENT_BUG_INVESTIGATION:
        recommended_args["symptom"] = symptom

    ttfca_ms = int((time.monotonic() - t0) * 1000)

    result: dict[str, Any] = {
        "session_state": SESSION_TASK_INTENT_DETECTED,
        "intent": intent,
        "intent_confidence": confidence,
        "workflow_description": WORKFLOW_DESCRIPTIONS.get(intent, ""),
        "required_tools_sequence": seq,
        "recommended_next_action": {
            "tool": flow_runner or (seq[0] if seq else "get_compact_context"),
            "reason": WORKFLOW_DESCRIPTIONS.get(intent, ""),
            "args": recommended_args,
        },
        "extracted_params": extracted_params,
        "session_meta": {
            "ttfca_ms": ttfca_ms,
            "orchestration_rules_applied": rules,
        },
    }

    if intent == INTENT_BUG_INVESTIGATION and not symptom:
        result["quality_warning"] = (
            "No error class or message extracted from task description. "
            "Pass symptom= to fix_bug_context for focused file ranking."
        )

    return result


# ---------------------------------------------------------------------------
# Flow: PR Review
# ---------------------------------------------------------------------------

def run_pr_review_flow_impl(repo_path: str, since: str = "") -> dict[str, Any]:
    """PR Review Flow: delta → execution paths → blast radius of top changed classes.

    Auto-detects merge-base with origin/main or origin/master when since is omitted.
    Runs get_impact_context for up to 3 changed Java classes automatically.
    Returns consolidated output — agent makes zero sequencing decisions.
    """
    t0 = time.monotonic()
    steps: list[str] = []
    quality_warnings: list[str] = []
    output: dict[str, Any] = {}

    # Check freshness
    cold = _cold_start(repo_path)
    if _freshness(cold.get("status", "no_ris")) == "stale":
        quality_warnings.append("RIS_stale—snapshot_may_not_reflect_current_HEAD")

    # Auto-detect since
    if not since:
        import subprocess as _sp
        for base in ("origin/main", "origin/master"):
            try:
                r = _sp.run(
                    ["git", "-C", repo_path, "merge-base", "HEAD", base],
                    capture_output=True, text=True, timeout=5,
                )
                if r.returncode == 0 and r.stdout.strip():
                    since = r.stdout.strip()
                    break
            except Exception:
                pass
        if not since:
            since = "HEAD~1"

    # Step 1: delta
    delta = _exec(["prepare-context", "delta", repo_path, "--since", since])
    steps.append(f"get_delta(since={since[:12]})")
    if "_exec_error" not in delta:
        output["delta_context"] = delta
    else:
        quality_warnings.append(f"delta_failed: {delta['_exec_error']}")

    # Step 2: PR context
    pr_args = ["prepare-context", "review-pr", repo_path, "--since", since]
    pr = _exec(pr_args)
    steps.append("review_pr_context")
    if "_exec_error" not in pr:
        output["pr_context"] = pr
    else:
        quality_warnings.append(f"review_pr_failed: {pr['_exec_error']}")

    # Step 3: impact for top changed classes (up to 3)
    changed_classes = _extract_changed_classes_from_delta(delta)
    impact_results: list[dict[str, Any]] = []
    for cls in changed_classes[:3]:
        imp = _exec(["impact", cls, repo_path, "--depth", "3"])
        steps.append(f"get_impact_context({cls})")
        if "_exec_error" not in imp:
            impact_results.append({"target": cls, "result": imp})
        else:
            quality_warnings.append(f"impact_failed({cls}): {imp['_exec_error']}")
    if impact_results:
        output["impact_analysis"] = impact_results

    ttfca_ms = int((time.monotonic() - t0) * 1000)

    return {
        "session_state": SESSION_READY_FOR_REVIEW,
        "flow": "pr_review",
        "since": since,
        "steps_executed": steps,
        "quality_warnings": quality_warnings,
        "consolidated_output": output,
        "session_meta": {
            "ttfca_ms": ttfca_ms,
            "steps_auto_executed": len(steps),
            "tools_suggested_to_agent": 0,
        },
    }


def _extract_changed_classes_from_delta(delta: dict[str, Any]) -> list[str]:
    if "_exec_error" in delta:
        return []
    changed_files: list[Any] = (
        delta.get("changed_files")
        or delta.get("files")
        or delta.get("data", {}).get("changed_files", [])
        or []
    )
    classes: list[str] = []
    for f in changed_files:
        if isinstance(f, dict):
            path = f.get("path") or f.get("file") or ""
        elif isinstance(f, str):
            path = f
        else:
            continue
        if isinstance(path, str) and path.endswith(".java"):
            name = path.split("/")[-1].replace(".java", "")
            if name and name not in classes:
                classes.append(name)
    return classes


# ---------------------------------------------------------------------------
# Flow: Bug Investigation
# ---------------------------------------------------------------------------

def run_bug_investigation_flow_impl(repo_path: str, symptom: str = "") -> dict[str, Any]:
    """Bug Investigation Flow: risk-ranked files → impact of top suspect → IR context.

    symptom should be an error message, exception class, or affected class name.
    Without symptom, ranking is generic (not focused) — quality_warnings will note this.
    """
    t0 = time.monotonic()
    steps: list[str] = []
    quality_warnings: list[str] = []
    output: dict[str, Any] = {}

    if not symptom:
        quality_warnings.append(
            "no_symptom_provided: file ranking is generic, not focused. "
            "Pass symptom= with error message or class name for targeted analysis."
        )

    # Step 1: fix-bug context
    fix_args = ["prepare-context", "fix-bug", repo_path]
    if symptom:
        fix_args.extend(["--symptom", symptom])
    fix = _exec(fix_args)
    steps.append(f"fix_bug_context(symptom={symptom!r})" if symptom else "fix_bug_context(no_symptom)")
    if "_exec_error" not in fix:
        output["risk_ranked_files"] = fix
    else:
        quality_warnings.append(f"fix_bug_failed: {fix['_exec_error']}")

    # Step 2: impact for top suspect class
    suspect = _extract_class_from_symptom(symptom) or _top_class_from_fix(fix)
    if suspect:
        imp = _exec(["impact", suspect, repo_path, "--depth", "4"])
        steps.append(f"get_impact_context({suspect})")
        if "_exec_error" not in imp:
            output["impact_analysis"] = {"target": suspect, "result": imp}
        else:
            quality_warnings.append(f"impact_failed({suspect}): {imp['_exec_error']}")

    # Step 3: IR summary for Java (dependency context)
    if _is_java_repo(repo_path):
        ir = _exec(["repo-ir", repo_path, "--summary-only"])
        steps.append("get_ir_summary")
        if "_exec_error" not in ir:
            output["ir_summary"] = ir
        else:
            quality_warnings.append(f"ir_summary_failed: {ir['_exec_error']}")

    ttfca_ms = int((time.monotonic() - t0) * 1000)

    return {
        "session_state": SESSION_READY_FOR_REVIEW,
        "flow": "bug_investigation",
        "symptom": symptom,
        "suspect_class": suspect,
        "steps_executed": steps,
        "quality_warnings": quality_warnings,
        "consolidated_output": output,
        "session_meta": {
            "ttfca_ms": ttfca_ms,
            "steps_auto_executed": len(steps),
            "tools_suggested_to_agent": 0,
        },
    }


def _extract_class_from_symptom(symptom: str) -> str:
    if not symptom:
        return ""
    m = re.search(
        r'\b([A-Z][a-zA-Z0-9]+(?:Service|Controller|Repository|Handler|Manager|Util|Helper|DAO|Rest|Api)?)\b',
        symptom,
    )
    return m.group(1) if m else ""


def _top_class_from_fix(fix: dict[str, Any]) -> str:
    if "_exec_error" in fix:
        return ""
    files: list[Any] = (
        fix.get("files")
        or fix.get("ranked_files")
        or fix.get("top_files")
        or fix.get("data", {}).get("files", [])
        or []
    )
    for f in files:
        if isinstance(f, dict):
            path = str(f.get("path") or f.get("file") or "")
        elif isinstance(f, str):
            path = f
        else:
            continue
        if path.endswith(".java"):
            return path.split("/")[-1].replace(".java", "")
    return ""


# ---------------------------------------------------------------------------
# Flow: Feature Implementation
# ---------------------------------------------------------------------------

def run_feature_flow_impl(repo_path: str, feature_description: str = "") -> dict[str, Any]:
    """Feature Implementation Flow: context → API surface → recent changes → structural awareness.

    Provides everything an agent needs to implement a new feature:
    structural context, existing API surface, what changed recently, and coupling hotspots.
    """
    t0 = time.monotonic()
    steps: list[str] = []
    quality_warnings: list[str] = []
    output: dict[str, Any] = {}

    is_java = _is_java_repo(repo_path)

    # Step 1: compact context (use RIS fast path if available)
    cold = _cold_start(repo_path)
    freshness = _freshness(cold.get("status", "no_ris"))
    if freshness == "fresh" and cold.get("summary"):
        output["context_summary"] = cold["summary"]
        steps.append("get_context(RIS_fast_path)")
    else:
        ctx = _exec([repo_path, "--compact"])
        steps.append("get_compact_context")
        if "_exec_error" not in ctx:
            output["context_summary"] = ctx
        else:
            quality_warnings.append(f"compact_context_failed: {ctx['_exec_error']}")

    # Step 2: API surface (Java only)
    if is_java:
        ep = _exec(["endpoints", repo_path])
        steps.append("get_endpoints")
        if "_exec_error" not in ep:
            output["api_surface"] = ep
        else:
            quality_warnings.append(f"endpoints_failed: {ep['_exec_error']}")

    # Step 3: recent delta (last 3 commits for context on active areas)
    delta = _exec(["prepare-context", "delta", repo_path, "--since", "HEAD~3"])
    steps.append("get_delta(HEAD~3)")
    if "_exec_error" not in delta:
        output["recent_changes"] = delta
    else:
        quality_warnings.append(f"delta_failed: {delta['_exec_error']}")

    # Step 4: structural context (coupling, hotspots, refactor opportunities)
    refactor = _exec(["prepare-context", "refactor", repo_path])
    steps.append("refactor_context")
    if "_exec_error" not in refactor:
        output["structural_context"] = refactor
    else:
        quality_warnings.append(f"refactor_context_failed: {refactor['_exec_error']}")

    ttfca_ms = int((time.monotonic() - t0) * 1000)

    return {
        "session_state": SESSION_READY_FOR_REVIEW,
        "flow": "feature_implementation",
        "feature_description": feature_description,
        "steps_executed": steps,
        "quality_warnings": quality_warnings,
        "consolidated_output": output,
        "session_meta": {
            "ttfca_ms": ttfca_ms,
            "steps_auto_executed": len(steps),
            "tools_suggested_to_agent": 0,
        },
    }

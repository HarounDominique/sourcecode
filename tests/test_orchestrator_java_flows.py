"""Tests for the Java/Spring orchestrator flow presets (v1.45 follow-up).

Covers the three pieces delivered for the audit's "predefined flows" value-add:
  1. run_migrate_flow  — wraps migrate-check, lifts a planning headline.
  2. run_security_audit_flow — wraps spring-audit + endpoints, warns on the
     config-less blind spot (no sourcecode.config.json + all none_detected).
  3. detect_intent + R5/R6 orchestration rules route migration / security-audit
     intents to these presets.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sourcecode.mcp import orchestrator as orch
from sourcecode.mcp.orchestrator import (
    INTENT_MIGRATION,
    INTENT_SECURITY_AUDIT,
    apply_orchestration_rules,
    detect_intent,
    run_migrate_flow_impl,
    run_security_audit_flow_impl,
)
from sourcecode.security_config import CONFIG_FILENAME


# ── intent detection ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "migrate to Spring Boot 3",
    "javax to jakarta upgrade",
    "spring boot 2 to 3 readiness",
    "upgrade spring boot",
])
def test_migration_intent_detected(text: str) -> None:
    assert detect_intent(text)[0] == INTENT_MIGRATION


@pytest.mark.parametrize("text", [
    "run a security audit",
    "audit the security surface",
    "who can call this endpoint",
    "check authorization on the endpoints",
])
def test_security_audit_intent_detected(text: str) -> None:
    assert detect_intent(text)[0] == INTENT_SECURITY_AUDIT


# ── R5 / R6 orchestration rules ──────────────────────────────────────────────

def test_r5_prepends_migration_readiness_on_fallback_seq() -> None:
    # Intent is migration but the incoming sequence does not lead with it.
    seq, rules = apply_orchestration_rules(
        freshness="fresh", is_java=True, api_surface_complete=True,
        repo_class_count=10, intent=INTENT_MIGRATION,
        sequence=["get_compact_context"],
    )
    assert seq[0] == "get_migration_readiness"
    assert any(r.startswith("R5") for r in rules)


def test_r6_prepends_endpoints_for_security_audit() -> None:
    seq, rules = apply_orchestration_rules(
        freshness="fresh", is_java=True, api_surface_complete=True,
        repo_class_count=10, intent=INTENT_SECURITY_AUDIT,
        sequence=["get_spring_audit"],
    )
    assert seq[0] == "get_endpoints"
    assert any(r.startswith("R6") for r in rules)


def test_rules_no_op_for_non_java() -> None:
    seq, rules = apply_orchestration_rules(
        freshness="fresh", is_java=False, api_surface_complete=True,
        repo_class_count=10, intent=INTENT_MIGRATION,
        sequence=["get_compact_context"],
    )
    assert "get_migration_readiness" not in seq
    assert not any(r.startswith("R5") for r in rules)


# ── run_migrate_flow ─────────────────────────────────────────────────────────

def test_migrate_flow_lifts_headline(monkeypatch: pytest.MonkeyPatch) -> None:
    report = {
        "readiness_score": 42,
        "blocking_count": 7,
        "estimated_effort_days": 12.5,
        "spring_boot_2_detected": True,
        "summary": {
            "total_findings": 30,
            "affected_files": 18,
            "by_severity": {"critical": 7, "high": 5},
            "by_rule": {"MIG-001": 20},
        },
    }
    monkeypatch.setattr(orch, "_is_java_repo", lambda p: True)
    monkeypatch.setattr(orch, "_exec", lambda args: report)

    result = run_migrate_flow_impl("/fake/repo", min_severity="high")
    assert result["flow"] == "migration"
    assert result["min_severity"] == "high"
    h = result["headline"]
    assert h["readiness_score"] == 42
    assert h["blocking_count"] == 7
    assert h["estimated_effort_days"] == 12.5
    assert h["by_target"] == {"MIG-001": 20}  # falls back to by_rule
    assert result["consolidated_output"]["migration_readiness"] is report
    assert result["quality_warnings"] == []


def test_migrate_flow_warns_on_non_java(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orch, "_is_java_repo", lambda p: False)
    monkeypatch.setattr(orch, "_exec", lambda args: {"readiness_score": 100})
    result = run_migrate_flow_impl("/fake/repo")
    assert any("not_a_java_repo" in w for w in result["quality_warnings"])


# ── run_security_audit_flow ──────────────────────────────────────────────────

def _stub_exec_factory(none_detected: int, total: int):
    def _exec(args):
        if args and args[0] == "spring-audit":
            return {"spring_detected": True, "findings": []}
        if args and args[0] == "endpoints":
            return {"total": total, "no_security_signal": none_detected,
                    "endpoints": []}
        return {}
    return _exec


def test_security_flow_warns_when_config_less_and_all_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()  # no sourcecode.config.json
    monkeypatch.setattr(orch, "_is_java_repo", lambda p: True)
    monkeypatch.setattr(orch, "_exec", _stub_exec_factory(none_detected=17, total=17))

    result = run_security_audit_flow_impl(str(repo))
    assert result["endpoint_security_coverage"] == {
        "total_endpoints": 17, "none_detected": 17, "config_present": False,
    }
    assert "security_config_hint" in result["consolidated_output"]
    assert any("config_less_security_blind_spot" in w for w in result["quality_warnings"])


def test_security_flow_silent_when_config_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / CONFIG_FILENAME).write_text(
        json.dumps({"customSecurityAnnotations": [{"shortName": "Sec"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(orch, "_is_java_repo", lambda p: True)
    monkeypatch.setattr(orch, "_exec", _stub_exec_factory(none_detected=17, total=17))

    result = run_security_audit_flow_impl(str(repo))
    assert result["endpoint_security_coverage"]["config_present"] is True
    assert "security_config_hint" not in result["consolidated_output"]
    assert not any("config_less" in w for w in result["quality_warnings"])


def test_security_flow_silent_when_some_secured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()  # no config, but not ALL none_detected → no false-negative signal
    monkeypatch.setattr(orch, "_is_java_repo", lambda p: True)
    monkeypatch.setattr(orch, "_exec", _stub_exec_factory(none_detected=5, total=17))

    result = run_security_audit_flow_impl(str(repo))
    assert "security_config_hint" not in result["consolidated_output"]


# ── MCP server registration ──────────────────────────────────────────────────

def test_new_flows_registered_as_mcp_tools() -> None:
    from sourcecode.mcp.server import mcp
    names = {t.name for t in mcp._tool_manager.list_tools()}
    assert "run_migrate_flow" in names
    assert "run_security_audit_flow" in names

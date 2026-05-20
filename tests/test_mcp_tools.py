"""Contract tests for sourcecode.mcp.server MCP tools.

All tools must return the canonical output contract:
  {"success": bool, "data": str | None, "error": {"code": str, "message": str} | None}

Requires: pip install sourcecode[mcp]
"""
import json
from unittest.mock import patch

import pytest

mcp_pkg = pytest.importorskip("mcp", reason="mcp extra not installed — skip MCP tool tests")

from sourcecode.mcp import server  # noqa: E402 — after importorskip guard

_RAW_OUTPUT = '{"project": {"primary_stack": "python"}}'
_SUCCESS_KEYS = frozenset({"success", "data", "error"})
_RUNNER_PATH = "sourcecode.mcp.server.run_command"


def _assert_success(result: dict) -> None:
    assert isinstance(result, dict)
    assert set(result.keys()) == _SUCCESS_KEYS, f"unexpected keys: {set(result.keys())}"
    assert result["success"] is True
    assert isinstance(result["data"], str)
    assert result["error"] is None
    json.dumps(result)


def _assert_failure(result: dict, expected_code: str | None = None) -> None:
    assert isinstance(result, dict)
    assert set(result.keys()) == _SUCCESS_KEYS, f"unexpected keys: {set(result.keys())}"
    assert result["success"] is False
    assert result["data"] is None
    assert isinstance(result["error"], dict)
    assert "code" in result["error"]
    assert "message" in result["error"]
    if expected_code:
        assert result["error"]["code"] == expected_code
    json.dumps(result)


# --- compact ---

def test_compact_success_contract():
    with patch(_RUNNER_PATH, return_value=_RAW_OUTPUT):
        result = server.compact("/some/path")
    _assert_success(result)
    assert result["data"] == _RAW_OUTPUT


def test_compact_with_git_context():
    with patch(_RUNNER_PATH, return_value=_RAW_OUTPUT) as mock_rc:
        server.compact("/some/path", git_context=True)
    args = mock_rc.call_args[0][0]
    assert "--git-context" in args
    assert "--compact" in args


def test_compact_without_git_context():
    with patch(_RUNNER_PATH, return_value=_RAW_OUTPUT) as mock_rc:
        server.compact("/some/path", git_context=False)
    args = mock_rc.call_args[0][0]
    assert "--git-context" not in args


def test_compact_failure_contract():
    with patch(_RUNNER_PATH, side_effect=RuntimeError("boom")):
        result = server.compact("/some/path")
    _assert_failure(result, "EXECUTION_FAILED")
    assert "boom" in result["error"]["message"]


# --- agent ---

def test_agent_success_contract():
    with patch(_RUNNER_PATH, return_value=_RAW_OUTPUT):
        result = server.agent("/some/path")
    _assert_success(result)


def test_agent_with_git_context():
    with patch(_RUNNER_PATH, return_value=_RAW_OUTPUT) as mock_rc:
        server.agent("/some/path", git_context=True)
    args = mock_rc.call_args[0][0]
    assert "--git-context" in args
    assert "--agent" in args


def test_agent_without_git_context():
    with patch(_RUNNER_PATH, return_value=_RAW_OUTPUT) as mock_rc:
        server.agent("/some/path", git_context=False)
    args = mock_rc.call_args[0][0]
    assert "--git-context" not in args


def test_agent_failure_contract():
    with patch(_RUNNER_PATH, side_effect=RuntimeError("fail")):
        result = server.agent("/some/path")
    _assert_failure(result, "EXECUTION_FAILED")


# --- prepare_context ---

def test_prepare_context_success_all_tasks():
    all_tasks = ("delta", "review-pr", "fix-bug", "onboard", "explain", "refactor", "generate-tests")
    for task in all_tasks:
        with patch(_RUNNER_PATH, return_value=_RAW_OUTPUT) as mock_rc:
            result = server.prepare_context(task, "/some/path")
        _assert_success(result)
        args = mock_rc.call_args[0][0]
        assert args == ["prepare-context", task, "/some/path"]


def test_prepare_context_invalid_task_rejected():
    result = server.prepare_context("--agent", "/some/path")
    _assert_failure(result, "INVALID_ARGUMENT")


def test_prepare_context_unknown_task_rejected():
    result = server.prepare_context("summarize", "/some/path")
    _assert_failure(result, "INVALID_ARGUMENT")


def test_prepare_context_no_free_form_task():
    result = server.prepare_context("anything I want", "/some/path")
    _assert_failure(result, "INVALID_ARGUMENT")


# --- repo_ir ---

def test_repo_ir_success_contract():
    with patch(_RUNNER_PATH, return_value=_RAW_OUTPUT) as mock_rc:
        result = server.repo_ir("/some/path")
    _assert_success(result)
    args = mock_rc.call_args[0][0]
    assert args == ["repo-ir", "/some/path"]


def test_repo_ir_failure_contract():
    with patch(_RUNNER_PATH, side_effect=RuntimeError("fail")):
        result = server.repo_ir("/some/path")
    _assert_failure(result, "EXECUTION_FAILED")


# --- version ---

def test_version_success_contract():
    with patch(_RUNNER_PATH, return_value="sourcecode 1.2.3") as mock_rc:
        result = server.version()
    _assert_success(result)
    assert mock_rc.call_args[0][0] == ["version"]


def test_version_failure_contract():
    with patch(_RUNNER_PATH, side_effect=RuntimeError("not installed")):
        result = server.version()
    _assert_failure(result, "EXECUTION_FAILED")


# --- config ---

def test_config_success_contract():
    with patch(_RUNNER_PATH, return_value="key=value") as mock_rc:
        result = server.config()
    _assert_success(result)
    assert mock_rc.call_args[0][0] == ["config"]


def test_config_failure_contract():
    with patch(_RUNNER_PATH, side_effect=RuntimeError("fail")):
        result = server.config()
    _assert_failure(result, "EXECUTION_FAILED")


# --- telemetry ---

def test_telemetry_success_all_actions():
    for action in ("status", "enable", "disable"):
        with patch(_RUNNER_PATH, return_value="ok") as mock_rc:
            result = server.telemetry(action)
        _assert_success(result)
        assert mock_rc.call_args[0][0] == ["telemetry", action]


def test_telemetry_invalid_action_rejected():
    result = server.telemetry("--enable")
    _assert_failure(result, "INVALID_ARGUMENT")


def test_telemetry_unknown_action_rejected():
    result = server.telemetry("reset")
    _assert_failure(result, "INVALID_ARGUMENT")


def test_telemetry_failure_contract():
    with patch(_RUNNER_PATH, side_effect=RuntimeError("fail")):
        result = server.telemetry("status")
    _assert_failure(result, "EXECUTION_FAILED")


# --- hallucination resistance ---

def test_no_raw_cli_flags_accepted_as_enum():
    result = server.telemetry("--disable")
    _assert_failure(result, "INVALID_ARGUMENT")

    result = server.prepare_context("--compact", "/path")
    _assert_failure(result, "INVALID_ARGUMENT")


def test_all_tools_json_serializable():
    with patch(_RUNNER_PATH, return_value=_RAW_OUTPUT):
        results = [
            server.compact("/p"),
            server.agent("/p"),
            server.prepare_context("onboard", "/p"),
            server.repo_ir("/p"),
            server.version(),
            server.config(),
            server.telemetry("status"),
        ]
    for r in results:
        json.dumps(r)


def test_no_extra_keys_on_success():
    with patch(_RUNNER_PATH, return_value=_RAW_OUTPUT):
        r = server.compact("/p")
    assert set(r.keys()) == _SUCCESS_KEYS


def test_no_extra_keys_on_failure():
    with patch(_RUNNER_PATH, side_effect=RuntimeError("x")):
        r = server.compact("/p")
    assert set(r.keys()) == _SUCCESS_KEYS


def test_no_extra_keys_on_validation_error():
    r = server.telemetry("bad")
    assert set(r.keys()) == _SUCCESS_KEYS

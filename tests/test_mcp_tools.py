"""Contract tests for sourcecode.mcp.server MCP tools.

Success contract (dict returned, FastMCP wraps with isError=False):
  {"success": bool, "data": dict | str | None, "error": {"code": str, "message": str} | None}

Failure contract (CallToolResult with isError=True per MCP spec §tool-result):
  CallToolResult.isError == True
  CallToolResult.content[0].text == JSON{"success": false, "data": null, "error": {...}}

Requires: pip install sourcecode[mcp]
"""
import json
from typing import Any
from unittest.mock import patch

import pytest

mcp_pkg = pytest.importorskip("mcp", reason="mcp extra not installed — skip MCP tool tests")

from mcp.types import CallToolResult, TextContent  # noqa: E402
from sourcecode.mcp import server  # noqa: E402 — after importorskip guard

_PARSED_OUTPUT = {"project": {"primary_stack": "python"}}
_SUCCESS_KEYS = frozenset({"success", "data", "error"})
_RUNNER_PATH = "sourcecode.mcp.server.run_command"
_CHECK_PATH = "sourcecode.mcp.server._check_repo_path"


@pytest.fixture(autouse=True)
def _bypass_path_check(monkeypatch):
    """Unit tests use fake paths like /some/repo that don't exist on disk.
    Bypass the filesystem check so tests only need to mock run_command.
    H-05 path validation is covered by test_bug_fixes_v13130.py with real paths.
    """
    monkeypatch.setattr("sourcecode.mcp.server._check_repo_path", lambda p: None)


def _assert_success(result: dict) -> None:
    assert isinstance(result, dict)
    assert set(result.keys()) == _SUCCESS_KEYS, f"unexpected keys: {set(result.keys())}"
    assert result["success"] is True
    assert result["data"] is not None
    assert result["error"] is None
    json.dumps(result)


def _assert_failure(result: Any, expected_code: str | None = None) -> None:
    """Assert tool returned CallToolResult with isError=True per MCP spec §tool-result."""
    assert isinstance(result, CallToolResult), (
        f"expected CallToolResult, got {type(result).__name__}"
    )
    assert result.isError is True, "isError must be True for tool failures"
    assert len(result.content) == 1, "failure result must carry exactly one content item"
    text = result.content[0]
    assert isinstance(text, TextContent), f"content[0] must be TextContent, got {type(text).__name__}"
    payload = json.loads(text.text)
    assert payload["success"] is False
    assert payload["data"] is None
    assert isinstance(payload["error"], dict)
    assert "code" in payload["error"]
    assert "message" in payload["error"]
    if expected_code:
        assert payload["error"]["code"] == expected_code


# --- get_compact_context ---

def test_get_compact_context_success():
    with patch(_RUNNER_PATH, return_value=_PARSED_OUTPUT) as mock_rc:
        result = server.get_compact_context("/some/repo")
    _assert_success(result)
    assert result["data"] == _PARSED_OUTPUT
    args = mock_rc.call_args[0][0]
    assert args == ["/some/repo", "--compact"]


def test_get_compact_context_default_path():
    with patch(_RUNNER_PATH, return_value=_PARSED_OUTPUT) as mock_rc:
        result = server.get_compact_context()
    _assert_success(result)
    args = mock_rc.call_args[0][0]
    assert args[0] == "."
    assert "--compact" in args


def test_get_compact_context_with_git_context():
    with patch(_RUNNER_PATH, return_value=_PARSED_OUTPUT) as mock_rc:
        server.get_compact_context("/some/repo", git_context=True)
    args = mock_rc.call_args[0][0]
    assert "--git-context" in args
    assert "--compact" in args


def test_get_compact_context_without_git_context():
    with patch(_RUNNER_PATH, return_value=_PARSED_OUTPUT) as mock_rc:
        server.get_compact_context("/some/repo", git_context=False)
    args = mock_rc.call_args[0][0]
    assert "--git-context" not in args


def test_get_compact_context_failure():
    with patch(_RUNNER_PATH, side_effect=RuntimeError("boom")):
        result = server.get_compact_context("/some/repo")
    _assert_failure(result, "EXECUTION_FAILED")
    payload = json.loads(result.content[0].text)
    assert "boom" in payload["error"]["message"]


# --- get_agent_context ---

def test_get_agent_context_success():
    with patch(_RUNNER_PATH, return_value=_PARSED_OUTPUT) as mock_rc:
        result = server.get_agent_context("/some/repo")
    _assert_success(result)
    args = mock_rc.call_args[0][0]
    assert args == ["/some/repo", "--agent"]


def test_get_agent_context_default_path():
    with patch(_RUNNER_PATH, return_value=_PARSED_OUTPUT) as mock_rc:
        server.get_agent_context()
    args = mock_rc.call_args[0][0]
    assert args[0] == "."
    assert "--agent" in args


def test_get_agent_context_with_git_context():
    with patch(_RUNNER_PATH, return_value=_PARSED_OUTPUT) as mock_rc:
        server.get_agent_context("/some/repo", git_context=True)
    args = mock_rc.call_args[0][0]
    assert "--git-context" in args
    assert "--agent" in args


def test_get_agent_context_without_git_context():
    with patch(_RUNNER_PATH, return_value=_PARSED_OUTPUT) as mock_rc:
        server.get_agent_context("/some/repo", git_context=False)
    args = mock_rc.call_args[0][0]
    assert "--git-context" not in args


def test_get_agent_context_failure():
    with patch(_RUNNER_PATH, side_effect=RuntimeError("fail")):
        result = server.get_agent_context("/some/repo")
    _assert_failure(result, "EXECUTION_FAILED")


# --- get_endpoints ---

def test_get_endpoints_success():
    with patch(_RUNNER_PATH, return_value=_PARSED_OUTPUT) as mock_rc:
        result = server.get_endpoints("/some/repo")
    _assert_success(result)
    args = mock_rc.call_args[0][0]
    assert args == ["endpoints", "/some/repo"]


def test_get_endpoints_calls_runner():
    with patch(_RUNNER_PATH, return_value=_PARSED_OUTPUT) as mock_rc:
        server.get_endpoints("/some/repo")
    mock_rc.assert_called_once()


def test_get_endpoints_failure():
    with patch(_RUNNER_PATH, side_effect=RuntimeError("fail")):
        result = server.get_endpoints("/some/repo")
    _assert_failure(result, "EXECUTION_FAILED")


# --- get_module_context ---

def test_get_module_context_success():
    with patch(_RUNNER_PATH, return_value=_PARSED_OUTPUT) as mock_rc:
        result = server.get_module_context("/some/repo", "src/auth")
    _assert_success(result)
    args = mock_rc.call_args[0][0]
    assert args[0].endswith("src/auth")
    assert "--compact" in args


def test_get_module_context_empty_module_rejected():
    result = server.get_module_context("/some/repo", "")
    _assert_failure(result, "INVALID_ARGUMENT")


def test_get_module_context_whitespace_module_rejected():
    result = server.get_module_context("/some/repo", "   ")
    _assert_failure(result, "INVALID_ARGUMENT")


def test_get_module_context_failure():
    with patch(_RUNNER_PATH, side_effect=RuntimeError("fail")):
        result = server.get_module_context("/some/repo", "api")
    _assert_failure(result, "EXECUTION_FAILED")


# --- get_delta ---

def test_get_delta_success():
    with patch(_RUNNER_PATH, return_value=_PARSED_OUTPUT) as mock_rc:
        result = server.get_delta("/some/repo", "main")
    _assert_success(result)
    args = mock_rc.call_args[0][0]
    assert args == ["prepare-context", "delta", "/some/repo", "--since", "main"]


def test_get_delta_default_since():
    with patch(_RUNNER_PATH, return_value=_PARSED_OUTPUT) as mock_rc:
        server.get_delta("/some/repo")
    args = mock_rc.call_args[0][0]
    assert "--since" in args
    assert args[args.index("--since") + 1] == "HEAD~1"


def test_get_delta_empty_since_uses_auto_detect():
    # Empty since triggers auto-detection (merge-base or HEAD~1 fallback).
    # It is no longer rejected — the tool resolves a ref and proceeds.
    with patch(_RUNNER_PATH, return_value=_PARSED_OUTPUT):
        result = server.get_delta("/some/repo", "")
    assert result["success"] is True


def test_get_delta_failure():
    with patch(_RUNNER_PATH, side_effect=RuntimeError("fail")):
        result = server.get_delta("/some/repo", "HEAD~3")
    _assert_failure(result, "EXECUTION_FAILED")


# --- get_ir_summary ---

def test_get_ir_summary_success():
    with patch(_RUNNER_PATH, return_value=_PARSED_OUTPUT) as mock_rc:
        result = server.get_ir_summary("/some/repo")
    _assert_success(result)
    args = mock_rc.call_args[0][0]
    assert args == ["repo-ir", "/some/repo", "--summary-only"]


def test_get_ir_summary_default_path():
    with patch(_RUNNER_PATH, return_value=_PARSED_OUTPUT) as mock_rc:
        server.get_ir_summary()
    args = mock_rc.call_args[0][0]
    assert args[0] == "repo-ir"
    assert args[1] == "."
    assert "--summary-only" in args


def test_get_ir_summary_failure():
    with patch(_RUNNER_PATH, side_effect=RuntimeError("fail")):
        result = server.get_ir_summary("/some/repo")
    _assert_failure(result, "EXECUTION_FAILED")


# --- onboard_context ---

def test_onboard_context_success():
    with patch(_RUNNER_PATH, return_value=_PARSED_OUTPUT) as mock_rc:
        result = server.onboard_context("/some/repo")
    _assert_success(result)
    args = mock_rc.call_args[0][0]
    assert args == ["prepare-context", "onboard", "/some/repo"]


def test_onboard_context_failure():
    with patch(_RUNNER_PATH, side_effect=RuntimeError("fail")):
        result = server.onboard_context("/some/repo")
    _assert_failure(result, "EXECUTION_FAILED")


# --- version ---

def test_version_success():
    with patch(_RUNNER_PATH, return_value="sourcecode 1.2.3") as mock_rc:
        result = server.version()
    _assert_success(result)
    assert mock_rc.call_args[0][0] == ["version"]


def test_version_failure():
    with patch(_RUNNER_PATH, side_effect=RuntimeError("not installed")):
        result = server.version()
    _assert_failure(result, "EXECUTION_FAILED")


# --- config ---

def test_config_success():
    with patch(_RUNNER_PATH, return_value="key=value") as mock_rc:
        result = server.config()
    _assert_success(result)
    assert mock_rc.call_args[0][0] == ["config"]


def test_config_failure():
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


def test_telemetry_failure():
    with patch(_RUNNER_PATH, side_effect=RuntimeError("fail")):
        result = server.telemetry("status")
    _assert_failure(result, "EXECUTION_FAILED")


# --- envelope invariants ---

def test_all_tools_json_serializable():
    with patch(_RUNNER_PATH, return_value=_PARSED_OUTPUT):
        results = [
            server.get_compact_context("/p"),
            server.get_agent_context("/p"),
            server.get_endpoints("/p"),
            server.get_module_context("/p", "api"),
            server.get_delta("/p", "main"),
            server.get_ir_summary("/p"),
            server.fix_bug_context("/p"),
            server.review_pr_context("/p"),
            server.onboard_context("/p"),
            server.version(),
            server.config(),
            server.telemetry("status"),
        ]
    for r in results:
        json.dumps(r)


def test_no_extra_keys_on_success():
    with patch(_RUNNER_PATH, return_value=_PARSED_OUTPUT):
        r = server.get_compact_context("/p")
    assert set(r.keys()) == _SUCCESS_KEYS


def test_no_extra_keys_on_failure():
    """Runtime failures return CallToolResult with isError=True per MCP spec §tool-result."""
    with patch(_RUNNER_PATH, side_effect=RuntimeError("x")):
        r = server.get_compact_context("/p")
    assert isinstance(r, CallToolResult)
    assert r.isError is True


def test_no_extra_keys_on_validation_error():
    """Validation failures return CallToolResult with isError=True per MCP spec §tool-result."""
    r = server.get_module_context("/p", "")
    assert isinstance(r, CallToolResult)
    assert r.isError is True


def test_get_impact_context_nonexistent_class_returns_is_error():
    """Tool call with nonexistent class returns isError=True per MCP spec §tool-result.

    When the subprocess exits non-zero (class not found), the MCP response must
    carry isError=True so AI agents distinguish errors from successful results.
    """
    with patch(_RUNNER_PATH, side_effect=RuntimeError("class 'NonExistentClass12345' not found")):
        result = server.get_impact_context("/some/repo", target="NonExistentClass12345")
    assert isinstance(result, CallToolResult), (
        f"expected CallToolResult, got {type(result).__name__}"
    )
    assert result.isError is True, "isError must be True when target class does not exist"
    payload = json.loads(result.content[0].text)
    assert payload["success"] is False
    assert "NonExistentClass12345" in payload["error"]["message"]

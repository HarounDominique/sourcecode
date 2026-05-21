"""Contract tests for sourcecode.mcp.server MCP tools.

All tools must return the canonical output contract:
  {"success": bool, "data": dict | str | None, "error": {"code": str, "message": str} | None}

data is the parsed JSON object from the CLI output, not a shell string.

Requires: pip install sourcecode[mcp]
"""
import json
from unittest.mock import patch

import pytest

mcp_pkg = pytest.importorskip("mcp", reason="mcp extra not installed — skip MCP tool tests")

from sourcecode.mcp import server  # noqa: E402 — after importorskip guard

_PARSED_OUTPUT = {"project": {"primary_stack": "python"}}
_SUCCESS_KEYS = frozenset({"success", "data", "error"})
_RUNNER_PATH = "sourcecode.mcp.server.run_command"


def _assert_success(result: dict) -> None:
    assert isinstance(result, dict)
    assert set(result.keys()) == _SUCCESS_KEYS, f"unexpected keys: {set(result.keys())}"
    assert result["success"] is True
    assert result["data"] is not None
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


def test_get_compact_context_failure():
    with patch(_RUNNER_PATH, side_effect=RuntimeError("boom")):
        result = server.get_compact_context("/some/repo")
    _assert_failure(result, "EXECUTION_FAILED")
    assert "boom" in result["error"]["message"]


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


def test_get_delta_empty_since_rejected():
    result = server.get_delta("/some/repo", "")
    _assert_failure(result, "INVALID_ARGUMENT")


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
        ]
    for r in results:
        json.dumps(r)


def test_no_extra_keys_on_success():
    with patch(_RUNNER_PATH, return_value=_PARSED_OUTPUT):
        r = server.get_compact_context("/p")
    assert set(r.keys()) == _SUCCESS_KEYS


def test_no_extra_keys_on_failure():
    with patch(_RUNNER_PATH, side_effect=RuntimeError("x")):
        r = server.get_compact_context("/p")
    assert set(r.keys()) == _SUCCESS_KEYS


def test_no_extra_keys_on_validation_error():
    r = server.get_module_context("/p", "")
    assert set(r.keys()) == _SUCCESS_KEYS

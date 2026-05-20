"""Tests for sourcecode.mcp.runner — in-process CLI runner."""
from unittest.mock import MagicMock, patch

import pytest

from sourcecode.mcp.runner import run_command


def _invoke_result(exit_code: int = 0, output: str = "some output") -> MagicMock:
    m = MagicMock()
    m.exit_code = exit_code
    m.output = output
    return m


def test_run_command_returns_raw_string():
    payload = '{"project": {"primary_stack": "python"}}'
    with patch("sourcecode.mcp.runner._runner.invoke", return_value=_invoke_result(0, payload)):
        result = run_command(["--agent"])
    assert isinstance(result, str)
    assert result == payload


def test_run_command_returns_non_json_string():
    with patch("sourcecode.mcp.runner._runner.invoke", return_value=_invoke_result(0, "plain text")):
        result = run_command(["version"])
    assert result == "plain text"


def test_run_command_strips_whitespace():
    with patch("sourcecode.mcp.runner._runner.invoke", return_value=_invoke_result(0, "  output  \n")):
        result = run_command(["version"])
    assert result == "output"


def test_run_command_nonzero_exit_raises():
    with patch("sourcecode.mcp.runner._runner.invoke", return_value=_invoke_result(1, "something went wrong")):
        with pytest.raises(RuntimeError, match="failed"):
            run_command(["--agent"])


def test_run_command_empty_output_raises():
    with patch("sourcecode.mcp.runner._runner.invoke", return_value=_invoke_result(0, "")):
        with pytest.raises(RuntimeError, match="no output"):
            run_command(["version"])


def test_run_command_whitespace_only_raises():
    with patch("sourcecode.mcp.runner._runner.invoke", return_value=_invoke_result(0, "   \n  ")):
        with pytest.raises(RuntimeError, match="no output"):
            run_command(["version"])


def test_run_command_error_includes_args():
    with patch("sourcecode.mcp.runner._runner.invoke", return_value=_invoke_result(1, "boom")):
        with pytest.raises(RuntimeError, match="--agent"):
            run_command(["--agent"])


def test_run_command_no_subprocess():
    """Runner must not import subprocess — in-process only."""
    import sourcecode.mcp.runner as runner_mod
    assert "subprocess" not in dir(runner_mod)
    assert not hasattr(runner_mod, "subprocess")
    assert not hasattr(runner_mod, "shutil")

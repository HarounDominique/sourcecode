"""Tests for 'sourcecode mcp serve' CLI subcommand."""
import sys
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from sourcecode.cli import _detected_path, _preprocess_args, app

_runner = CliRunner()


def invoke(args: list[str]):
    _detected_path[0] = "."
    processed = _preprocess_args(list(args))
    return _runner.invoke(app, processed)


def test_mcp_is_registered_subcommand():
    result = invoke(["--help"])
    assert result.exit_code == 0
    assert "mcp" in result.output


def test_mcp_serve_help():
    result = invoke(["mcp", "serve", "--help"])
    assert result.exit_code == 0


def test_mcp_serve_calls_mcp_run():
    """When mcp is available, serve calls mcp.run()."""
    mock_server_mod = MagicMock()
    mock_server_mod.mcp = MagicMock()
    mock_server_mod.mcp.run = MagicMock()

    with patch.dict(sys.modules, {"sourcecode.mcp.server": mock_server_mod}):
        result = invoke(["mcp", "serve"])

    mock_server_mod.mcp.run.assert_called_once()
    assert result.exit_code == 0

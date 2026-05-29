"""Bidirectional drift detection for CLI runtime <-> MCP registry."""
from __future__ import annotations

from sourcecode.mcp import registry as mcp_registry
from sourcecode.mcp.server import mcp


def test_registry_validation_passes_cleanly():
    issues = mcp_registry.validate_registry()
    assert issues == [], f"registry drift detected: {issues}"


def test_public_registry_matches_live_mcp_tools():
    public_specs = mcp_registry.build_public_tool_specs()
    registered = {tool.name for tool in mcp._tool_manager.list_tools()}
    spec_names = {spec.name for spec in public_specs}
    assert registered == spec_names, (
        f"registered MCP tools drifted from generated registry:\n"
        f"missing={sorted(spec_names - registered)}\n"
        f"extra={sorted(registered - spec_names)}"
    )


def test_public_cli_commands_have_canonical_mcp_specs():
    runtime = [
        cmd for cmd in mcp_registry.discover_runtime_commands()
        if (cmd.callback is not None or cmd.path == ()) and not cmd.hidden
    ]
    public_specs = {spec.name: spec for spec in mcp_registry.build_public_tool_specs()}

    missing = []
    for command in runtime:
        canonical_name = mcp_registry._tool_name_for_path(command.path)  # noqa: SLF001
        if canonical_name not in public_specs:
            missing.append(" ".join(command.path) or "sourcecode")
            continue
        spec = public_specs[canonical_name]
        assert spec.cli_path == command.path
        assert spec.description
        assert spec.docstring
        runtime_param_names = [param.name for param in command.command.params]
        spec_param_names = [param.name for param in spec.params]
        assert runtime_param_names == spec_param_names
    assert missing == [], f"missing MCP specs for runtime CLI commands: {missing}"


def test_hidden_cli_commands_are_internal_only():
    internal_specs = {spec.name: spec for spec in mcp_registry.build_internal_tool_specs()}
    assert "analyze" in internal_specs
    assert internal_specs["analyze"].internal is True
    assert internal_specs["analyze"].not_exposed_to_cli is True
    assert "analyze" not in {spec.name for spec in mcp_registry.build_public_tool_specs()}


def test_alias_tools_are_generated_from_runtime_commands():
    public_specs = {spec.name: spec for spec in mcp_registry.build_public_tool_specs()}

    assert public_specs["get_compact_context"].build_argv({"repo_path": "/repo"}) == [
        "/repo",
        "--compact",
    ]
    assert public_specs["get_agent_context"].build_argv({"repo_path": "/repo"}) == [
        "/repo",
        "--agent",
    ]
    assert public_specs["get_delta"].build_argv({"repo_path": "/repo", "since": "HEAD~3"}) == [
        "prepare-context",
        "delta",
        "/repo",
        "--since",
        "HEAD~3",
    ]

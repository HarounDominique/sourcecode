"""Runtime-derived MCP registry for sourcecode CLI.

The CLI command tree is the source of truth. This module discovers the real
Typer/click runtime, derives public MCP tool specs from it, and builds the
callables that the MCP server exposes.

Public tools are generated from runtime commands. Legacy convenience aliases
are derived from the same runtime and are kept for backwards compatibility.
Internal helpers remain explicitly marked and are not exported to MCP.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import inspect
from functools import lru_cache
from typing import Any, Callable, Mapping

import click
from mcp.types import CallToolResult, TextContent
from typer.main import get_command

from sourcecode.cli import app as _cli_app
from sourcecode.error_schema import (
    EXECUTION_FAILED_CODE,
    INTERNAL_ERROR_CODE,
    INVALID_INPUT_CODE,
    build_error_object,
)
from sourcecode.mcp.runner import CommandError, run_command as _fallback_run_command


@dataclass(frozen=True)
class ToolParamSpec:
    """Machine-readable input parameter description for an MCP tool."""

    name: str
    kind: str  # argument | option
    annotation: type[Any] = str
    required: bool = False
    default: Any = None
    option_names: tuple[str, ...] = ()
    help: str = ""
    is_flag: bool = False
    is_path: bool = False
    choices: tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolOutputSpec:
    """Machine-readable output description for an MCP tool."""

    envelope: tuple[str, ...] = ("success", "data", "error")
    error_fields: tuple[str, ...] = ("code", "message", "hint", "expected")
    payload_kind: str = "cli_passthrough"
    notes: str = "MCP envelope wraps parsed CLI output; data is passthrough."


@dataclass(frozen=True)
class ToolSpec:
    """Executable MCP tool spec generated from the live CLI runtime."""

    name: str
    description: str
    cli_path: tuple[str, ...]
    params: tuple[ToolParamSpec, ...]
    output: ToolOutputSpec = field(default_factory=ToolOutputSpec)
    supported_targets: tuple[str, ...] = ()
    unsupported_targets: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    internal: bool = False
    not_exposed_to_cli: bool = False
    mcp_hidden: bool = False
    docstring: str = ""
    runtime_command: str = ""
    _argv_builder: Callable[[Mapping[str, Any]], list[str]] | None = field(
        default=None, repr=False, compare=False
    )
    validator: Callable[[Mapping[str, Any]], str | None] | None = field(
        default=None, repr=False, compare=False
    )

    @property
    def public(self) -> bool:
        return not self.internal and not self.not_exposed_to_cli

    @property
    def mcp_visible(self) -> bool:
        return self.public and not self.mcp_hidden

    def build_argv(self, inputs: Mapping[str, Any]) -> list[str]:
        if self._argv_builder is None:
            raise RuntimeError(f"ToolSpec '{self.name}' has no argv builder")
        return self._argv_builder(inputs)


@dataclass(frozen=True)
class RuntimeCommand:
    """Discovered click command in the live Typer runtime."""

    path: tuple[str, ...]
    command: click.Command
    callback: Callable[..., Any] | None
    hidden: bool
    help: str
    docstring: str


def _snake(text: str) -> str:
    return text.replace("-", "_").replace(" ", "_")


def _tool_name_for_path(path: tuple[str, ...]) -> str:
    if not path:
        return "sourcecode_root"
    return "_".join(_snake(part) for part in path)


def _first_doc_line(doc: str) -> str:
    for line in (doc or "").splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def _click_to_param_spec(param: click.Parameter) -> ToolParamSpec:
    annotation: type[Any] = str
    is_path = False
    default = None

    param_type = getattr(param, "type", None)
    param_type_name = type(param_type).__name__
    if param_type_name in {"IntParamType", "IntRange"}:
        annotation = int
    elif param_type_name in {"FloatParamType", "FloatRange"}:
        annotation = float
    elif param_type_name in {"BoolParamType"}:
        annotation = bool
    elif param_type_name in {"Path"}:
        annotation = str
        is_path = True
    else:
        annotation = str

    if getattr(param, "default", inspect._empty) is not inspect._empty:
        default = param.default

    choices: tuple[str, ...] = ()
    if hasattr(param_type, "choices") and getattr(param_type, "choices"):
        choices = tuple(str(choice) for choice in param_type.choices)

    option_names: tuple[str, ...] = ()
    if getattr(param, "opts", None):
        option_names = tuple(str(opt) for opt in param.opts)

    return ToolParamSpec(
        name=param.name,
        kind=getattr(param, "param_type_name", "option"),
        annotation=annotation,
        required=bool(getattr(param, "required", False)),
        default=default,
        option_names=option_names,
        help=getattr(param, "help", "") or "",
        is_flag=bool(getattr(param, "is_flag", False)),
        is_path=is_path,
        choices=choices,
    )


def _discover_commands() -> list[RuntimeCommand]:
    root = get_command(_cli_app)
    discovered: list[RuntimeCommand] = []

    def walk(command: click.Command, path: tuple[str, ...]) -> None:
        callback = getattr(command, "callback", None)
        help_text = getattr(command, "help", "") or ""
        docstring = inspect.getdoc(callback) or help_text or ""
        hidden = bool(getattr(command, "hidden", False))
        discovered.append(
            RuntimeCommand(
                path=path,
                command=command,
                callback=callback,
                hidden=hidden,
                help=help_text,
                docstring=docstring,
            )
        )
        if isinstance(command, click.Group):
            for name, child in command.commands.items():
                walk(child, path + (name,))

    walk(root, ())
    return discovered


@lru_cache(maxsize=1)
def discover_runtime_commands() -> tuple[RuntimeCommand, ...]:
    return tuple(_discover_commands())


def _generic_argv_builder(path: tuple[str, ...], params: tuple[ToolParamSpec, ...]) -> Callable[[Mapping[str, Any]], list[str]]:
    def _builder(inputs: Mapping[str, Any]) -> list[str]:
        argv: list[str] = list(path)
        for param in params:
            if param.name not in inputs:
                continue
            value = inputs[param.name]
            if value is None:
                continue
            if param.kind == "argument":
                argv.append(str(value))
                continue
            if param.is_flag:
                if bool(value):
                    argv.append(param.option_names[0] if param.option_names else f"--{param.name.replace('_', '-')}")
                continue
            if param.default is not None and value == param.default:
                continue
            option_name = param.option_names[0] if param.option_names else f"--{param.name.replace('_', '-')}"
            argv.extend([option_name, str(value)])
        return argv

    return _builder


def _normalize_result(result: Any, default_message: str) -> dict | CallToolResult:
    if isinstance(result, dict) and result.get("success") is False:
        cli_error = result.get("error")
        if isinstance(cli_error, dict):
            error = build_error_object(
                str(cli_error.get("code") or EXECUTION_FAILED_CODE),
                str(cli_error.get("message") or default_message),
                hint=str(cli_error.get("hint") or ""),
                expected=str(cli_error.get("expected") or ""),
            )
        else:
            error = build_error_object(EXECUTION_FAILED_CODE, default_message)
        payload = {"success": False, "data": None, "error": error}
        for key, value in result.items():
            if key not in {"success", "data", "error"}:
                payload[key] = value
        return CallToolResult(
            content=[TextContent(type="text", text=__import__("json").dumps(payload))],
            isError=True,
        )
    return {"success": True, "data": result, "error": None}


def _invalid_call(message: str) -> CallToolResult:
    payload = {
        "success": False,
        "data": None,
        "error": build_error_object(
            INVALID_INPUT_CODE,
            message,
            hint="Pass the supported arguments using the documented tool schema.",
            expected="Valid tool arguments.",
        ),
    }
    return CallToolResult(
        content=[TextContent(type="text", text=__import__("json").dumps(payload))],
        isError=True,
    )


def _current_run_command() -> Callable[[list[str]], Any]:
    try:
        from sourcecode.mcp import server as _server

        candidate = getattr(_server, "run_command", None)
        if callable(candidate):
            return candidate
    except Exception:
        pass
    return _fallback_run_command


def _repo_path_validator(param_name: str = "repo_path") -> Callable[[Mapping[str, Any]], str | None]:
    def _validate(inputs: Mapping[str, Any]) -> str | None:
        raw = inputs.get(param_name, ".")
        if not isinstance(raw, str):
            return f"Parameter '{param_name}' must be a string path for repository tools."
        try:
            from sourcecode.mcp import server as _server

            normalized = _server._normalize_repo_path(raw)
            path_error = _server._check_repo_path(normalized)
        except Exception as exc:
            return f"Unable to validate '{param_name}': {exc}"
        if path_error is None:
            return None
        try:
            import json

            payload = json.loads(path_error.content[0].text)
            message = payload.get("error", {}).get("message")
            if isinstance(message, str) and message.strip():
                return message
        except Exception:
            pass
        return f"Invalid repository path: {raw}"

    return _validate


def _run_argv(argv: list[str]) -> dict | CallToolResult:
    runner = _current_run_command()
    try:
        result = runner(argv)
    except CommandError as exc:
        payload = getattr(exc, "payload", None)
        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            error = payload["error"]
            normalized = {
                "success": False,
                "data": None,
                "error": build_error_object(
                    str(error.get("code") or EXECUTION_FAILED_CODE),
                    str(error.get("message") or f"Command failed: {' '.join(argv)}"),
                    hint=str(error.get("hint") or ""),
                    expected=str(error.get("expected") or ""),
                ),
            }
            for key, value in payload.items():
                if key != "error":
                    normalized[key] = value
            return CallToolResult(
                content=[TextContent(type="text", text=__import__("json").dumps(normalized))],
                isError=True,
            )
        return CallToolResult(
            content=[TextContent(type="text", text=__import__("json").dumps({
                "success": False,
                "data": None,
                "error": build_error_object(
                    EXECUTION_FAILED_CODE,
                    str(exc) or f"Command failed: {' '.join(argv)}",
                    hint="Inspect the CLI stderr for the structured error payload.",
                    expected="Successful CLI execution.",
                ),
            }))],
            isError=True,
        )
    except RuntimeError as exc:
        return CallToolResult(
            content=[TextContent(type="text", text=__import__("json").dumps({
                "success": False,
                "data": None,
                "error": build_error_object(
                    EXECUTION_FAILED_CODE,
                    str(exc) or f"Command failed: {' '.join(argv)}",
                    hint="Inspect the CLI stderr for the structured error payload.",
                    expected="Successful CLI execution.",
                ),
            }))],
            isError=True,
        )
    return _normalize_result(result, f"Command returned success=false: {' '.join(argv)}")


def _make_tool_callable(spec: ToolSpec) -> Callable[..., Any]:
    def _tool(*args: Any, **kwargs: Any) -> dict | CallToolResult:
        if len(args) > len(spec.params):
            return _invalid_call(f"Too many positional arguments for {spec.name}")
        bound: dict[str, Any] = {}
        for param, value in zip(spec.params, args):
            bound[param.name] = value
        for key, value in kwargs.items():
            if key not in {p.name for p in spec.params}:
                return _invalid_call(f"Unknown parameter '{key}' for {spec.name}")
            if key in bound:
                return _invalid_call(f"Duplicate value for parameter '{key}' in {spec.name}")
            bound[key] = value
        if len(bound) < len({p.name for p in spec.params if p.required}):
            missing = [p.name for p in spec.params if p.required and p.name not in bound]
            return _invalid_call(f"Missing required arguments for {spec.name}: {', '.join(missing)}")
        for param in spec.params:
            value = bound.get(param.name, param.default)
            if param.required and isinstance(value, str) and not value.strip():
                return _invalid_call(f"Parameter '{param.name}' must be a non-empty string for {spec.name}")
            if param.choices and value is not None and str(value) not in param.choices:
                return _invalid_call(
                    f"Parameter '{param.name}' for {spec.name} must be one of: {', '.join(param.choices)}"
                )
        if spec.validator is not None:
            message = spec.validator(bound)
            if message:
                return _invalid_call(message)
        return _run_argv(spec.build_argv(bound))

    ordered_params = sorted(spec.params, key=lambda p: (not p.required, p.name))
    params: list[inspect.Parameter] = []
    for p in ordered_params:
        default = inspect._empty if p.required else p.default
        params.append(
            inspect.Parameter(
                p.name,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                default=default,
                annotation=p.annotation,
            )
        )
    _tool.__defaults__ = tuple(p.default for p in ordered_params if not p.required)  # type: ignore[attr-defined]
    _tool.__name__ = spec.name
    _tool.__qualname__ = spec.name
    _tool.__doc__ = spec.docstring
    _tool.__signature__ = inspect.Signature(params, return_annotation=dict)  # type: ignore[attr-defined]
    return _tool


def make_tool_callable(spec: ToolSpec) -> Callable[..., Any]:
    """Public factory for MCP tool callables."""
    return _make_tool_callable(spec)


def _canonical_spec_for_runtime_command(runtime: RuntimeCommand) -> ToolSpec:
    params = tuple(_click_to_param_spec(param) for param in runtime.command.params)
    runtime_command = " ".join(runtime.path) if runtime.path else "sourcecode"
    name = _tool_name_for_path(runtime.path)
    doc = runtime.docstring or runtime.help or ""
    description = _first_doc_line(doc) or f"CLI command: {runtime_command}"
    supported_targets: list[str] = []
    unsupported_targets: list[str] = []
    param_names = {p.name for p in params}
    if "repo_path" in param_names or "path" in param_names:
        supported_targets.append("repo_path")
        unsupported_targets.append("file_path")
    if "module" in param_names:
        supported_targets.append("module_path")
    if "target" in param_names:
        supported_targets.append("class_name")
    if "action" in param_names:
        supported_targets.append("action")

    return ToolSpec(
        name=name,
        description=description,
        cli_path=runtime.path,
        params=params,
        supported_targets=tuple(dict.fromkeys(supported_targets)),
        unsupported_targets=tuple(dict.fromkeys(unsupported_targets)),
        aliases=(),
        internal=runtime.hidden,
        not_exposed_to_cli=runtime.hidden,
        docstring=_build_contract_doc(
            description,
            params,
            runtime.path,
            tuple(dict.fromkeys(supported_targets)),
            tuple(dict.fromkeys(unsupported_targets)),
        ),
        runtime_command=runtime_command,
        _argv_builder=_generic_argv_builder(runtime.path, params),
    )


def _build_contract_doc(
    description: str,
    params: tuple[ToolParamSpec, ...],
    cli_path: tuple[str, ...],
    supported_targets: tuple[str, ...],
    unsupported_targets: tuple[str, ...],
) -> str:
    lines = [description.strip() or "MCP tool generated from CLI runtime.", "", "Contract:"]
    lines.append(f"  cli_path: {' '.join(cli_path) if cli_path else 'sourcecode'}")
    if params:
        lines.append("  inputs:")
        for p in params:
            default = "" if p.required else f" default={p.default!r}"
            choice = f" choices={list(p.choices)!r}" if p.choices else ""
            lines.append(f"    - {p.name}: {p.kind} type={p.annotation.__name__}{default}{choice}")
    else:
        lines.append("  inputs: []")
    lines.append("  outputs:")
    lines.append("    - success: bool")
    lines.append("    - data: CLI passthrough result")
    lines.append("    - error: {code, message, hint, expected}")
    lines.append("  supported_targets: " + (", ".join(supported_targets) if supported_targets else "[]"))
    lines.append("  unsupported_targets: " + (", ".join(unsupported_targets) if unsupported_targets else "[]"))
    return "\n".join(lines)


def _alias_spec(
    name: str,
    description: str,
    cli_path: tuple[str, ...],
    params: tuple[ToolParamSpec, ...],
    argv_builder: Callable[[Mapping[str, Any]], list[str]],
    *,
    supported_targets: tuple[str, ...] = (),
    unsupported_targets: tuple[str, ...] = (),
    aliases: tuple[str, ...] = (),
    internal: bool = False,
    not_exposed_to_cli: bool = False,
    mcp_hidden: bool = False,
    docstring_override: str | None = None,
    validator: Callable[[Mapping[str, Any]], str | None] | None = None,
) -> ToolSpec:
    doc = docstring_override or _build_contract_doc(description, params, cli_path, supported_targets, unsupported_targets)
    return ToolSpec(
        name=name,
        description=description,
        cli_path=cli_path,
        params=params,
        supported_targets=supported_targets,
        unsupported_targets=unsupported_targets,
        aliases=aliases,
        internal=internal,
        not_exposed_to_cli=not_exposed_to_cli,
        mcp_hidden=mcp_hidden,
        docstring=doc,
        runtime_command=" ".join(cli_path) if cli_path else "sourcecode",
        _argv_builder=argv_builder,
        validator=validator,
    )


def _root_aliases() -> list[ToolSpec]:
    validate_repo_path = _repo_path_validator()
    params_compact = (
        ToolParamSpec("repo_path", "argument", str, required=False, default=".", is_path=True),
        ToolParamSpec("git_context", "option", bool, required=False, default=False, option_names=("--git-context",), is_flag=True),
    )
    params_module = (
        ToolParamSpec("repo_path", "argument", str, required=False, default=".", is_path=True),
        ToolParamSpec("module", "argument", str, required=True, default=None, is_path=True),
    )

    def compact_argv(inputs: Mapping[str, Any]) -> list[str]:
        argv: list[str] = [str(inputs.get("repo_path", ".")), "--compact"]
        if bool(inputs.get("git_context")):
            argv.append("--git-context")
        return argv

    def agent_argv(inputs: Mapping[str, Any]) -> list[str]:
        argv: list[str] = [str(inputs.get("repo_path", ".")), "--agent"]
        if bool(inputs.get("git_context")):
            argv.append("--git-context")
        return argv

    def module_argv(inputs: Mapping[str, Any]) -> list[str]:
        repo_path = str(inputs.get("repo_path", ".")).rstrip("/")
        module = str(inputs["module"]).strip("/")
        return [f"{repo_path}/{module}", "--compact"]

    return [
        _alias_spec(
            "get_compact_context",
            "Compact repository summary derived from the root CLI command. "
            "Use get_agent_context for richer detail.",
            ("sourcecode",),
            params_compact,
            compact_argv,
            supported_targets=("repo_path",),
            unsupported_targets=("file_path",),
            validator=validate_repo_path,
        ),
        _alias_spec(
            "get_agent_context",
            "Extended repository context derived from the root CLI command.",
            ("sourcecode",),
            params_compact,
            agent_argv,
            supported_targets=("repo_path",),
            unsupported_targets=("file_path",),
            validator=validate_repo_path,
        ),
        _alias_spec(
            "get_module_context",
            "Compact context for a specific module path derived from the root CLI command.",
            ("sourcecode",),
            params_module,
            module_argv,
            supported_targets=("repo_path", "module_path"),
            unsupported_targets=("file_path",),
            validator=validate_repo_path,
        ),
        _alias_spec(
            "telemetry",
            "Telemetry action helper alias that dispatches to the telemetry CLI group.",
            ("telemetry",),
            (
                ToolParamSpec(
                    "action",
                    "argument",
                    str,
                    required=True,
                    choices=("status", "enable", "disable"),
                ),
            ),
            lambda inputs: ["telemetry", str(inputs["action"])],
            supported_targets=("action",),
        ),
    ]


def _prepare_context_aliases() -> list[ToolSpec]:
    validate_repo_path = _repo_path_validator()

    def task_alias(name: str, task: str, description: str, *, supported_targets: tuple[str, ...]) -> ToolSpec:
        params = (
            ToolParamSpec("repo_path", "argument", str, required=False, default=".", is_path=True),
            ToolParamSpec("since", "argument", str, required=False, default=""),
        )

        def argv_builder(inputs: Mapping[str, Any]) -> list[str]:
            repo_path = str(inputs.get("repo_path", "."))
            argv: list[str] = ["prepare-context", task, repo_path]
            value = str(inputs.get("since", "") or "").strip()
            if not value and name == "get_delta":
                try:
                    from sourcecode.mcp.server import _auto_since as _resolve_since

                    value = _resolve_since(repo_path)
                except Exception:
                    value = "HEAD~1"
            if value:
                argv.extend(["--since", value])
            return argv

        return _alias_spec(
            name,
            description,
            ("prepare-context",),
            params,
            argv_builder,
            supported_targets=supported_targets,
            unsupported_targets=("file_path",),
            validator=validate_repo_path,
        )

    return [
        task_alias("get_delta", "delta", "Incremental delta context from prepare-context.", supported_targets=("repo_path", "git_ref")),
        task_alias("fix_bug_context", "fix-bug", "Bug investigation context from prepare-context.", supported_targets=("repo_path", "symptom")),
        task_alias("review_pr_context", "review-pr", "Pull-request review context from prepare-context.", supported_targets=("repo_path", "git_ref")),
        task_alias("onboard_context", "onboard", "Onboarding context from prepare-context.", supported_targets=("repo_path",)),
        task_alias("explain_context", "explain", "Architecture explanation from prepare-context.", supported_targets=("repo_path",)),
        task_alias("refactor_context", "refactor", "Refactor context from prepare-context.", supported_targets=("repo_path",)),
        _alias_spec(
            "generate_tests_context",
            "Test gap analysis from prepare-context.",
            ("prepare-context",),
            (
                ToolParamSpec("repo_path", "argument", str, required=False, default=".", is_path=True),
                ToolParamSpec("include_all", "argument", bool, required=False, default=False, option_names=("--all",), is_flag=True),
            ),
            lambda inputs: ["prepare-context", "generate-tests", str(inputs.get("repo_path", "."))] + (["--all"] if bool(inputs.get("include_all")) else []),
            supported_targets=("repo_path",),
            unsupported_targets=("file_path",),
            validator=validate_repo_path,
        ),
        _alias_spec(
            "get_ir_summary",
            "Summary-mode Java IR context from repo-ir.",
            ("repo-ir",),
            (
                ToolParamSpec("repo_path", "argument", str, required=False, default=".", is_path=True),
            ),
            lambda inputs: ["repo-ir", str(inputs.get("repo_path", ".")), "--summary-only"],
            supported_targets=("repo_path",),
            unsupported_targets=("file_path",),
            validator=validate_repo_path,
        ),
        _alias_spec(
            "get_impact_context",
            "Blast-radius analysis from impact.",
            ("impact",),
            (
                ToolParamSpec("repo_path", "argument", str, required=False, default=".", is_path=True),
                ToolParamSpec("target", "argument", str, required=True),
                ToolParamSpec("depth", "option", int, required=False, default=4, option_names=("--depth",)),
            ),
            lambda inputs: [
                "impact",
                str(inputs["target"]),
                str(inputs.get("repo_path", ".")),
                "--depth",
                str(inputs.get("depth", 4)),
            ],
            supported_targets=("repo_path", "class_name"),
            unsupported_targets=("file_path",),
            validator=validate_repo_path,
        ),
        _alias_spec(
            "modernize_context",
            "Modernization analysis from modernize.",
            ("modernize",),
            (
                ToolParamSpec("repo_path", "argument", str, required=False, default=".", is_path=True),
            ),
            lambda inputs: ["modernize", str(inputs.get("repo_path", "."))],
            supported_targets=("repo_path",),
            unsupported_targets=("file_path",),
            validator=validate_repo_path,
        ),
        _alias_spec(
            "check_freshness",
            "Cache freshness check derived from cache freshness.",
            ("cache", "freshness"),
            (
                ToolParamSpec("repo_path", "argument", str, required=False, default=".", is_path=True),
            ),
            lambda inputs: ["cache", "freshness", str(inputs.get("repo_path", ".")), "--json"],
            supported_targets=("repo_path",),
            unsupported_targets=("file_path",),
            validator=validate_repo_path,
        ),
        _alias_spec(
            "get_cold_start_context",
            "Cold-start snapshot from the cold-start CLI command.",
            ("cold-start",),
            (
                ToolParamSpec("repo_path", "argument", str, required=False, default=".", is_path=True),
            ),
            lambda inputs: ["cold-start", str(inputs.get("repo_path", "."))],
            supported_targets=("repo_path",),
            unsupported_targets=("file_path",),
            validator=validate_repo_path,
        ),
    ]


def _internal_specs() -> list[ToolSpec]:
    return [
        _alias_spec(
            "analyze",
            "Hidden legacy CLI alias. Not exposed to MCP.",
            ("analyze",),
            (
                ToolParamSpec("path", "argument", str, required=False, default=".", is_path=True),
            ),
            lambda inputs: ["analyze", str(inputs.get("path", "."))],
            internal=True,
            not_exposed_to_cli=True,
        ),
        _alias_spec(
            "start_session",
            "Internal orchestration helper. Not exposed to MCP.",
            ("internal", "start-session"),
            (
                ToolParamSpec("repo_path", "argument", str, required=False, default=".", is_path=True),
                ToolParamSpec("task_description", "argument", str, required=False, default=""),
            ),
            lambda inputs: ["__internal__", "start_session"],
            internal=True,
            not_exposed_to_cli=True,
        ),
        _alias_spec(
            "analyze_task",
            "Internal orchestration helper. Not exposed to MCP.",
            ("internal", "analyze-task"),
            (
                ToolParamSpec("repo_path", "argument", str, required=False, default=".", is_path=True),
                ToolParamSpec("task_description", "argument", str, required=False, default=""),
            ),
            lambda inputs: ["__internal__", "analyze_task"],
            internal=True,
            not_exposed_to_cli=True,
        ),
        _alias_spec(
            "run_pr_review_flow",
            "Internal orchestration helper. Not exposed to MCP.",
            ("internal", "run-pr-review-flow"),
            (
                ToolParamSpec("repo_path", "argument", str, required=False, default=".", is_path=True),
                ToolParamSpec("since", "argument", str, required=False, default=""),
            ),
            lambda inputs: ["__internal__", "run_pr_review_flow"],
            internal=True,
            not_exposed_to_cli=True,
        ),
        _alias_spec(
            "run_bug_investigation_flow",
            "Internal orchestration helper. Not exposed to MCP.",
            ("internal", "run-bug-investigation-flow"),
            (
                ToolParamSpec("repo_path", "argument", str, required=False, default=".", is_path=True),
                ToolParamSpec("symptom", "argument", str, required=False, default=""),
            ),
            lambda inputs: ["__internal__", "run_bug_investigation_flow"],
            internal=True,
            not_exposed_to_cli=True,
        ),
        _alias_spec(
            "run_feature_flow",
            "Internal orchestration helper. Not exposed to MCP.",
            ("internal", "run-feature-flow"),
            (
                ToolParamSpec("repo_path", "argument", str, required=False, default=".", is_path=True),
                ToolParamSpec("feature_description", "argument", str, required=False, default=""),
            ),
            lambda inputs: ["__internal__", "run_feature_flow"],
            internal=True,
            not_exposed_to_cli=True,
        ),
    ]


# Canonical CLI tools that are MCP-noise: raw passthroughs, meta-commands, and duplicates
# superseded by cleaner alias variants. Still tracked by validate_registry(); just not served.
_MCP_HIDDEN_CANONICAL_TOOLS: frozenset[str] = frozenset({
    # Raw CLI passthroughs (clean alias exists)
    "sourcecode_root",    # 40+ flags, no agent guidance; use get_compact_context / get_agent_context
    "prepare_context",    # requires knowing subtask string; use task-specific aliases
    "repo_ir",            # raw IR dump; use get_ir_summary
    "fix_bug",            # raw Pro command; use fix_bug_context for MCP
    "review_pr",          # raw Pro command; use review_pr_context for MCP
    "onboard",            # raw with llm_prompt/copy flags; use onboard_context
    # Duplicates (inferior params — cleaner alias exists)
    "impact",             # path/target order reversed vs get_impact_context; use get_impact_context
    "cold_start",         # duplicate of get_cold_start_context
    "cache_freshness",    # duplicate of check_freshness
    "modernize",          # duplicate of modernize_context
    # Curated overrides — canonical CLI spec replaced by cleaner alias with same name.
    # Listed here so validate_registry() skips CLI param-drift checks on the alias.
    "spring_audit",       # curated: repo_path + scope + min_severity only (strips output_path/format/copy)
    "impact_chain",       # curated: repo_path + symbol + depth + query_type with choices
    # MCP self-management (an agent is not the MCP client admin)
    "mcp_init",
    "mcp_serve",
    "mcp_status",
    "mcp_remove",
    "mcp_list_tools",
    # Telemetry sub-commands (consolidated into telemetry(action=))
    "telemetry_status",
    "telemetry_enable",
    "telemetry_disable",
})


def _java_spring_aliases() -> list[ToolSpec]:
    """Curated MCP overrides for Java/Spring tools.

    These replace the auto-generated canonical specs with cleaner param surfaces:
    - repo_path (not raw `path`) for consistency with all other MCP tools
    - MCP-irrelevant CLI flags (output_path, format, copy) stripped
    - query_type choices documented so agents discover event topology
    - Rich docstrings instead of contract-format stubs
    """
    validate_repo_path = _repo_path_validator()

    _SPRING_AUDIT_DOC = """\
Spring semantic audit: TX anomalies + security surface. JAVA/SPRING ONLY.

Do NOT call on non-Java repositories — returns spring_detected=false with no findings.

Patterns detected:
  TX-001: @Transactional missing rollbackFor for checked exceptions
  TX-002: propagation=NEVER/NOT_SUPPORTED inside @Transactional scope
  TX-003: readOnly=true method calling write operations
  TX-004: REQUIRES_NEW nested inside REQUIRED (TX isolation breach risk)
  TX-005: @Async method called within @Transactional context (TX context lost)
  SEC-001: public endpoint with no security annotation (none_detected policy)
  SEC-002: security annotation on non-endpoint method (misplaced)
  SEC-003: missing auth on admin-pattern operations

Returns: schema_version, spring_detected, scope, summary, findings[], limitations, metadata.
findings fields: id, pattern_id, category, severity, confidence, title, symbol,
  source_file, evidence, explanation, fix_hint.

repo_path: absolute path to the Java repository (default: current working directory).
scope: "all" (default) | "tx" (TX-001..005 only) | "security" (SEC-001..003 only)
min_severity: "low" (default) | "medium" | "high" | "critical"
"""

    _IMPACT_CHAIN_DOC = """\
Spring impact-chain: blast radius of a symbol with TX/SEC semantic enrichment. JAVA/SPRING ONLY.

Do NOT call on non-Java repositories — returns resolution=not_found.

Two query modes via query_type:
  "impact" (default) — BFS call graph: direct_callers, indirect_callers, endpoints_affected,
    transaction_boundary, security_surfaces, impact_findings (TX/SEC patterns in call chain).
  "events" — event topology: publishers, consumers, propagation graph for an event class
    or event publisher. Use when symbol is an event class (e.g. OrderPlacedEvent).

Returns: schema_version, symbol, resolution, direct_callers, indirect_callers,
  endpoints_affected, transaction_boundary, security_surfaces, impact_findings,
  analysis_warnings, risk_level, confidence, metadata.

symbol: FQN, class name, or Class#method.
  Examples: "OrderService", "com.example.OrderService#placeOrder",
            "OrderPlacedEvent" (with query_type="events" for event topology)
repo_path: absolute path to the Java repository (default: current working directory).
depth: BFS traversal depth 1–8 (default 4).
query_type: "impact" (default) | "events"
"""

    spring_audit = _alias_spec(
        "spring_audit",
        "Spring semantic audit: TX anomalies + security surface. JAVA/SPRING ONLY.",
        ("spring-audit",),
        (
            ToolParamSpec("repo_path", "argument", str, required=False, default=".", is_path=True,
                          help="Absolute path to the Java repository."),
            ToolParamSpec("scope", "option", str, required=False, default="all",
                          option_names=("--scope",), choices=("all", "tx", "security"),
                          help="all (default) | tx | security"),
            ToolParamSpec("min_severity", "option", str, required=False, default="low",
                          option_names=("--min-severity",), choices=("low", "medium", "high", "critical"),
                          help="low (default) | medium | high | critical"),
        ),
        lambda inputs: [
            "spring-audit",
            str(inputs.get("repo_path", ".")),
            "--scope", str(inputs.get("scope", "all")),
            "--min-severity", str(inputs.get("min_severity", "low")),
        ],
        supported_targets=("repo_path",),
        unsupported_targets=("file_path",),
        validator=validate_repo_path,
        docstring_override=_SPRING_AUDIT_DOC,
    )

    impact_chain = _alias_spec(
        "impact_chain",
        "Spring impact-chain: blast radius + TX/SEC enrichment. JAVA/SPRING ONLY.",
        ("impact-chain",),
        (
            ToolParamSpec("symbol", "argument", str, required=True, default=None,
                          help="FQN, class name, or Class#method."),
            ToolParamSpec("repo_path", "argument", str, required=False, default=".", is_path=True,
                          help="Absolute path to the Java repository."),
            ToolParamSpec("depth", "option", int, required=False, default=4,
                          option_names=("--depth",), help="BFS depth 1–8 (default 4)."),
            ToolParamSpec("query_type", "option", str, required=False, default="impact",
                          option_names=("--type",), choices=("impact", "events"),
                          help="impact (default) = call-chain blast radius; events = event topology"),
        ),
        lambda inputs: [
            "impact-chain",
            str(inputs["symbol"]),
            str(inputs.get("repo_path", ".")),
            "--depth", str(inputs.get("depth", 4)),
            "--type", str(inputs.get("query_type", "impact")),
        ],
        supported_targets=("repo_path", "class_name"),
        unsupported_targets=("file_path",),
        validator=validate_repo_path,
        docstring_override=_IMPACT_CHAIN_DOC,
    )

    return [spring_audit, impact_chain]


@lru_cache(maxsize=1)
def build_tool_specs() -> tuple[ToolSpec, ...]:
    """Build the full MCP registry from the live CLI runtime."""
    canonical_raw = [
        _canonical_spec_for_runtime_command(runtime)
        for runtime in discover_runtime_commands()
        if (runtime.callback is not None or runtime.path == ())
        and (not runtime.hidden or runtime.path == ("analyze",))
    ]
    # Mark canonical tools that should not be served via MCP (validate_registry still checks them)
    canonical = [
        replace(spec, mcp_hidden=True) if spec.name in _MCP_HIDDEN_CANONICAL_TOOLS else spec
        for spec in canonical_raw
    ]

    aliases = _root_aliases() + _prepare_context_aliases() + _java_spring_aliases()
    internals = _internal_specs()

    merged: dict[str, ToolSpec] = {}
    for spec in [*canonical, *aliases, *internals]:
        merged[spec.name] = spec
    return tuple(merged[name] for name in sorted(merged))


@lru_cache(maxsize=1)
def build_public_tool_specs() -> tuple[ToolSpec, ...]:
    return tuple(spec for spec in build_tool_specs() if spec.public)


@lru_cache(maxsize=1)
def build_internal_tool_specs() -> tuple[ToolSpec, ...]:
    return tuple(spec for spec in build_tool_specs() if not spec.public)


def validate_registry() -> list[str]:
    """Return a list of drift issues between the runtime CLI and MCP registry."""
    issues: list[str] = []
    runtime_by_path = {
        runtime.path: runtime
        for runtime in discover_runtime_commands()
        if (runtime.callback is not None or runtime.path == ()) and not runtime.hidden
    }
    registry_by_path = {
        spec.cli_path: spec
        for spec in build_public_tool_specs()
        if spec.name == _tool_name_for_path(spec.cli_path)
    }

    missing_paths = sorted(runtime_by_path.keys() - registry_by_path.keys())
    if missing_paths:
        issues.append(
            "missing_mcp_tools_for_cli_commands: "
            + str([(" ".join(path) or "sourcecode") for path in missing_paths])
        )

    for path, runtime in runtime_by_path.items():
        spec = registry_by_path.get(path)
        if spec is None:
            continue
        # Skip docstring/param drift for mcp_hidden tools and intentional curated overrides.
        # Curated aliases (e.g. spring_audit, impact_chain) replace canonical CLI specs with
        # cleaner MCP surfaces — their params diverge from the raw CLI by design.
        if spec.name in _MCP_HIDDEN_CANONICAL_TOOLS or spec.mcp_hidden:
            continue
        expected_doc = _first_doc_line(runtime.docstring or runtime.help or "")
        if expected_doc and expected_doc not in spec.description:
            issues.append(f"docstring_mismatch:{spec.name}")
        runtime_param_names = [param.name for param in runtime.command.params]
        spec_param_names = [param.name for param in spec.params]
        if runtime_param_names != spec_param_names:
            issues.append(f"parameter_drift:{spec.name}")

    return issues


@lru_cache(maxsize=1)
def build_mcp_tool_specs() -> tuple[ToolSpec, ...]:
    """Tool specs actually served via MCP: public and not mcp_hidden."""
    return tuple(spec for spec in build_tool_specs() if spec.mcp_visible)


def mcp_tool_specs() -> tuple[ToolSpec, ...]:
    """Tool specs served via MCP (public, not mcp_hidden)."""
    return build_mcp_tool_specs()


def mcp_internal_tool_specs() -> tuple[ToolSpec, ...]:
    return build_internal_tool_specs()

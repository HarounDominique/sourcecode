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

    _GET_COMPACT_DOC = """\
Compact human/LLM summary of a repository (~1000-3000 tokens). USE THIS FIRST.

Best for: quick project orientation, first-time context, token-budget constrained tasks.
Returns: stacks, entry points, dependency summary, architecture summary, confidence, gaps.
Includes security_surface, mybatis, and transactional_boundaries for Java/Spring projects.
For richer machine-oriented detail (deeper signals, more sections), use get_agent_context.

Maps to: sourcecode <repo_path> --compact [--git-context]
repo_path: absolute path to the repository (default: current working directory).
git_context: include git log and branch context in the analysis.
"""

    _GET_AGENT_DOC = """\
Full structured agent context with extended machine-oriented signals (~5000-15000 tokens).

Best for: deep analysis, bug investigation, code review, or when get_compact_context
lacks sufficient detail. Includes all compact fields plus: env_map, code_notes,
architecture layers, security surface, transactional boundaries, module graph summary.
Prefer get_compact_context for quick orientation or token-constrained workflows.

Maps to: sourcecode <repo_path> --agent [--git-context]
repo_path: absolute path to the repository (default: current working directory).
git_context: include git log and branch context in the analysis.
"""

    _GET_MODULE_DOC = """\
Compact analysis of a specific module or subdirectory within a repository.

Maps to: sourcecode <repo_path>/<module> --compact
repo_path: absolute path to the repository root.
module: subdirectory name relative to repo_path (e.g. 'src/auth', 'api', 'core').
Returns: same fields as get_compact_context but scoped to the module subtree.
"""

    _TELEMETRY_DOC = """\
Manage telemetry settings.

Maps to: sourcecode telemetry <action>
action: one of "status" (show current state), "enable" (opt in), "disable" (opt out).
Valid values: "status" | "enable" | "disable"
"""

    return [
        _alias_spec(
            "get_compact_context",
            "Compact human/LLM summary of a repository (~1000-3000 tokens). USE THIS FIRST.",
            ("sourcecode",),
            params_compact,
            compact_argv,
            supported_targets=("repo_path",),
            unsupported_targets=("file_path",),
            validator=validate_repo_path,
            docstring_override=_GET_COMPACT_DOC,
        ),
        _alias_spec(
            "get_agent_context",
            "Full structured agent context with extended machine-oriented signals (~5000-15000 tokens).",
            ("sourcecode",),
            params_compact,
            agent_argv,
            supported_targets=("repo_path",),
            unsupported_targets=("file_path",),
            validator=validate_repo_path,
            docstring_override=_GET_AGENT_DOC,
        ),
        _alias_spec(
            "get_module_context",
            "Compact analysis of a specific module or subdirectory within a repository.",
            ("sourcecode",),
            params_module,
            module_argv,
            supported_targets=("repo_path", "module_path"),
            unsupported_targets=("file_path",),
            validator=validate_repo_path,
            docstring_override=_GET_MODULE_DOC,
        ),
        _alias_spec(
            "telemetry",
            "Manage telemetry settings: status | enable | disable.",
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
            docstring_override=_TELEMETRY_DOC,
        ),
    ]


def _prepare_context_aliases() -> list[ToolSpec]:
    validate_repo_path = _repo_path_validator()

    # Only used for prepare-context tasks that genuinely accept --since (delta, review-pr).
    def _since_task_alias(
        name: str,
        task: str,
        description: str,
        *,
        supported_targets: tuple[str, ...],
        docstring_override: str | None = None,
    ) -> ToolSpec:
        params = (
            ToolParamSpec("repo_path", "argument", str, required=False, default=".", is_path=True),
            ToolParamSpec("since", "option", str, required=False, default="",
                          option_names=("--since",),
                          help="Git ref to diff against (e.g. HEAD~3, origin/main)."),
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
            docstring_override=docstring_override,
        )

    _GET_DELTA_DOC = """\
Incremental context: git-changed files since a reference commit.

Maps to: sourcecode prepare-context delta <repo_path> --since <since>
repo_path: absolute path to the repository (default: current working directory).
since: git ref to diff against (e.g. HEAD~3, main, origin/main).
       If empty or omitted, auto-detects merge-base with origin/main (or
       origin/master). Falls back to HEAD~1 if no remote branch found.
       Pass "HEAD~1" explicitly to force single-commit diff.
"""

    _REVIEW_PR_DOC = """\
Execution paths and risk analysis for changed files in a pull request.

Maps to: sourcecode prepare-context review-pr <repo_path> [--since <since>]
Returns: compact_base + execution_paths (diff-scoped) + hotspots for changed files.
repo_path: absolute path to the repository (default: current working directory).
since: git ref to diff against (e.g. HEAD~3, main, origin/main).
       If omitted, diffs against uncommitted changes or HEAD~1 fallback.
"""

    _FIX_BUG_DOC = """\
Risk-ranked files for bug investigation, optionally focused by symptom.

Maps to: sourcecode prepare-context fix-bug <repo_path> [--symptom <symptom>]
Includes compact_base: security_surface, transactional_boundaries, spring_profiles.
repo_path: absolute path to the repository (default: current working directory).
symptom: optional error message or class name to focus the file ranking
         (e.g. "NullPointerException in UserService.findById").
         Without symptom, ranking is generic (by churn/complexity). With symptom,
         the matching class and its callers are ranked first.
"""

    _ONBOARD_DOC = """\
Onboarding context: structured overview for new contributors.

Maps to: sourcecode prepare-context onboard <repo_path>
Returns: project structure, key entry points, architectural patterns, getting-started guide.
repo_path: absolute path to the repository (default: current working directory).
"""

    _EXPLAIN_DOC = """\
Architecture and entry-point explanation for a repository.

Maps to: sourcecode prepare-context explain <repo_path>
Returns: project summary, architecture overview, entry points, key dependencies.
repo_path: absolute path to the repository (default: current working directory).
"""

    _REFACTOR_DOC = """\
Structural issues and refactor opportunities for a repository.

Maps to: sourcecode prepare-context refactor <repo_path>
Returns: structural issues, coupling hotspots, high-churn files, improvement opportunities.
repo_path: absolute path to the repository (default: current working directory).
"""

    _GENERATE_TESTS_DOC = """\
Untested source files and test gap analysis for a repository.

Maps to: sourcecode prepare-context generate-tests <repo_path> [--all]
Returns: test_gaps list of untested files ranked by risk.
        On large repos (>2000 classes) analysis is bounded by SOURCECODE_TESTS_TIMEOUT_MS
        (default: 15000 ms). If timeout elapses, returns truncated=true with partial results.
repo_path: absolute path to the repository (default: current working directory).
include_all: return full test_gaps list without truncating to top 20.
"""

    _IR_SUMMARY_DOC = """\
Deterministic symbol-level IR summary for Java repositories. Java only.

Maps to: sourcecode repo-ir <repo_path> --summary-only
Returns: reverse_graph, route_surface (top 50 endpoints),
         subsystems (top 15), impact, analysis. Full graph nodes/edges omitted.

reverse_graph: dict[class_FQN → {"contained_in": [method_FQNs], ...}]
  Top 10 most-referenced (highest in-degree) classes in the dependency graph.
  Keys are fully-qualified class names. Iterate with:
    for fqn, data in result["reverse_graph"].items(): ...
route_surface: list of endpoint dicts (method, path, handler, security).
subsystems: list of detected subsystem cluster dicts.
analysis: metadata — total_classes, total_edges, analysis_ms.

Output is bounded to ~100 KB for LLM safety. For full IR (can exceed 10 MB
on large repos), use the CLI: sourcecode repo-ir <path> --output ir.json
Use get_compact_context or get_agent_context for non-Java repos.

repo_path: absolute path to the Java repository (default: current working directory).
"""

    _IMPACT_CONTEXT_DOC = """\
Blast-radius analysis: who calls a class and what breaks if it changes? Java only.

Maps to: sourcecode impact <target> <repo_path> [--depth <depth>]
Returns: direct_callers, indirect_callers, endpoints_affected,
         transactional_boundaries_touched, risk_score, risk_level, stats.

Use this when:
- Planning a refactor: understand the full call chain before changing a class
- PR review: assess blast radius of a changed service or utility class
- Incident triage: find all paths that reach a faulty component

target: class name (simple or FQN) or Java file path. Examples:
        "UserService", "org.example.UserService", "UserService.java"
repo_path: absolute path to the Java repository (default: current working directory).
depth: BFS depth for indirect caller traversal (1–8, default: 4).
"""

    _MODERNIZE_DOC = """\
Analyzes codebase for modernization opportunities: dead zones, hotspot scores, upgrade candidates.

Maps to: sourcecode modernize <repo_path>
Returns: hotspot_candidates (high fan-in + git churn), dead_zone_candidates (isolated classes),
         high_coupling_nodes, subsystem_summary, cross_module_tangles, recommendation.

Best for: refactor planning, identifying where to start, finding safe removal candidates.
Use get_compact_context or get_agent_context first for project orientation.

repo_path: absolute path to the Java repository (default: current working directory).
"""

    _CHECK_FRESHNESS_DOC = """\
Report RIS freshness relative to the current git HEAD.

Answers instantly: is the cached snapshot current? How many commits behind?
Use before deciding whether to call get_compact_context for a refresh.

Returns:
  fresh (bool)             — True when RIS HEAD == current HEAD and no uncommitted changes
  current_git_head (str)   — Current repo HEAD (short SHA)
  ris_git_head (str|null)  — HEAD stored in RIS at last build
  delta_commits (int|null) — Commits between ris_git_head and HEAD (0 = in sync)
  has_uncommitted_changes  — Working tree has staged or unstaged changes
  ris_exists (bool)        — False when no RIS built yet
  ris_last_updated_at (str)— ISO-8601 timestamp of last RIS write

repo_path: absolute path to the repository (default: current working directory).
"""

    _COLD_START_DOC = """\
Instant session bootstrap from persisted Repository Intelligence Snapshot (RIS).

PREFER start_session over this tool — it provides orchestration guidance on top
of the same RIS data. Use get_cold_start_context when you only need the raw
RIS bootstrap object without tool sequencing recommendations.

Returns cached structural context built from prior analysis runs — zero re-analysis cost.

status values:
  "cold_start_ready"  — RIS exists and matches the current git HEAD.
  "cold_start_stale"  — RIS exists but HEAD has changed since last analysis.
                        Data is still useful; run get_compact_context to refresh.
  "no_ris"            — No RIS yet for this repo; run get_compact_context first.

Returns: status, repo_id, git_head, stale (bool), last_updated_at,
         summary (compact snapshot), entrypoints, endpoints, hotspots.

repo_path: absolute path to the repository (default: current working directory).
"""

    _GET_ENDPOINTS_DOC = """\
REST API endpoint surface extraction from Java source files. JAVA ONLY.

Do NOT call this on non-Java repositories — it will return empty results.
Use get_compact_context or get_agent_context for non-Java repos.

Maps to: sourcecode endpoints <repo_path>
Returns: endpoints list with method, path, controller, handler fields;
         security dict always present (policy: roles_allowed|permit_all|deny_all|
         authenticated|...|none_detected); none_detected = no auth annotation found.
         total (int), no_security_signal (int), and security_model (str) fields.
         no_security_signal counts endpoints where security.policy == "none_detected".
         security_model values: "filter_based" (centralized Spring Security config —
         high no_security_signal is expected and does NOT mean endpoints are unprotected),
         "annotation_based" (per-endpoint annotations only), "mixed" (both),
         "unknown" (no security signals detected).
Supports Spring MVC (@GetMapping etc.) and JAX-RS (@GET/@POST etc.).
repo_path: absolute path to the Java repository (default: current working directory).
"""

    _GET_VALIDATION_DOC = """\
Request-body validation surface per endpoint. JAVA/SPRING ONLY.

Do NOT call this on non-Java repositories — it will return empty results.

Combines two sources of bean-validation truth so you know what a request body
must satisfy before generating a payload, a test, or reasoning about a 400:
  * declarative constraints on the DTOs (@Pattern/@Size/@NotNull, minimum/maximum,
    enum) — recovered from the OpenAPI spec even when DTOs are generated under
    target/generated-sources (not scanned);
  * hand-written custom validators (@Constraint + ConstraintValidator, e.g.
    PetAgeValidator), linked to fields via x-field-extra-annotation.

Maps to: sourcecode validation <repo_path>
Returns: endpoints[] (method, path, controller, handler, schema, validatedFields[
  {name, rules[{kind,value}], customValidators[{annotation,validators,message,resolved}]}]),
  custom_validators[] (catalog: annotation, validators, message, validatedTypes, targets),
  gaps[] (POST/PUT/PATCH endpoints with no declared validation),
  summary, openapi_spec.
An unresolved custom annotation (referenced in the spec, no validator in source)
is reported with resolved=false.
repo_path: absolute path to the Java repository (default: current working directory).
gaps_only: when true, return only the gaps section (endpoints lacking validation).
"""

    _CACHE_STATUS_DOC = """\
Report cache metadata for a repository.

Maps to: sourcecode cache status <repo_path> --json
Returns: cache entries with git_head, timestamps, size info.
Use check_freshness for RIS-specific freshness checking (faster, richer).
repo_path: absolute path to the repository (default: current working directory).
"""

    _CACHE_WARM_DOC = """\
Warm the cache for a repository (builds compact and agent analysis snapshots).

Maps to: sourcecode cache warm <repo_path>
Builds or refreshes the Repository Intelligence Snapshot (RIS).
Use before analytical workflows to ensure fast subsequent tool calls (~8s first run,
instant after).
repo_path: absolute path to the repository (default: current working directory).
"""

    _CACHE_CLEAR_DOC = """\
Clear cached analysis for a repository.

Maps to: sourcecode cache clear <repo_path> [--include-ris]
Removes cached context files. After clearing, run get_compact_context or cache_warm to rebuild.
include_ris: also remove the RIS snapshot in addition to analysis cache (default: False).
repo_path: absolute path to the repository (default: current working directory).
"""

    return [
        # --- get_delta / review_pr_context: both genuinely use --since ---
        _since_task_alias(
            "get_delta", "delta",
            "Incremental context: git-changed files since a reference commit.",
            supported_targets=("repo_path", "git_ref"),
            docstring_override=_GET_DELTA_DOC,
        ),
        _since_task_alias(
            "review_pr_context", "review-pr",
            "Execution paths and risk analysis for changed files in a pull request.",
            supported_targets=("repo_path", "git_ref"),
            docstring_override=_REVIEW_PR_DOC,
        ),

        # --- fix_bug_context: --symptom (not --since) ---
        _alias_spec(
            "fix_bug_context",
            "Risk-ranked files for bug investigation, optionally focused by symptom.",
            ("prepare-context",),
            (
                ToolParamSpec("repo_path", "argument", str, required=False, default=".", is_path=True),
                ToolParamSpec("symptom", "option", str, required=False, default=None,
                              option_names=("--symptom",),
                              help="Error message or class name to focus file ranking."),
            ),
            lambda inputs: (
                ["prepare-context", "fix-bug", str(inputs.get("repo_path", "."))]
                + (["--symptom", str(inputs["symptom"])] if inputs.get("symptom") else [])
            ),
            supported_targets=("repo_path", "symptom"),
            unsupported_targets=("file_path",),
            validator=validate_repo_path,
            docstring_override=_FIX_BUG_DOC,
        ),

        # --- onboard / explain / refactor: no --since ---
        _alias_spec(
            "onboard_context",
            "Onboarding context: structured overview for new contributors.",
            ("prepare-context",),
            (
                ToolParamSpec("repo_path", "argument", str, required=False, default=".", is_path=True),
            ),
            lambda inputs: ["prepare-context", "onboard", str(inputs.get("repo_path", "."))],
            supported_targets=("repo_path",),
            unsupported_targets=("file_path",),
            validator=validate_repo_path,
            docstring_override=_ONBOARD_DOC,
        ),
        _alias_spec(
            "explain_context",
            "Architecture and entry-point explanation for a repository.",
            ("prepare-context",),
            (
                ToolParamSpec("repo_path", "argument", str, required=False, default=".", is_path=True),
            ),
            lambda inputs: ["prepare-context", "explain", str(inputs.get("repo_path", "."))],
            supported_targets=("repo_path",),
            unsupported_targets=("file_path",),
            validator=validate_repo_path,
            docstring_override=_EXPLAIN_DOC,
        ),
        _alias_spec(
            "refactor_context",
            "Structural issues and refactor opportunities for a repository.",
            ("prepare-context",),
            (
                ToolParamSpec("repo_path", "argument", str, required=False, default=".", is_path=True),
            ),
            lambda inputs: ["prepare-context", "refactor", str(inputs.get("repo_path", "."))],
            supported_targets=("repo_path",),
            unsupported_targets=("file_path",),
            validator=validate_repo_path,
            docstring_override=_REFACTOR_DOC,
        ),

        # --- other prepare-context aliases ---
        _alias_spec(
            "generate_tests_context",
            "Untested source files and test gap analysis for a repository.",
            ("prepare-context",),
            (
                ToolParamSpec("repo_path", "argument", str, required=False, default=".", is_path=True),
                ToolParamSpec("include_all", "option", bool, required=False, default=False,
                              option_names=("--all",), is_flag=True),
            ),
            lambda inputs: (
                ["prepare-context", "generate-tests", str(inputs.get("repo_path", "."))]
                + (["--all"] if bool(inputs.get("include_all")) else [])
            ),
            supported_targets=("repo_path",),
            unsupported_targets=("file_path",),
            validator=validate_repo_path,
            docstring_override=_GENERATE_TESTS_DOC,
        ),

        # --- Java analysis aliases ---
        _alias_spec(
            "get_ir_summary",
            "Deterministic symbol-level IR summary for Java repositories. Java only.",
            ("repo-ir",),
            (
                ToolParamSpec("repo_path", "argument", str, required=False, default=".", is_path=True),
            ),
            lambda inputs: ["repo-ir", str(inputs.get("repo_path", ".")), "--summary-only"],
            supported_targets=("repo_path",),
            unsupported_targets=("file_path",),
            validator=validate_repo_path,
            docstring_override=_IR_SUMMARY_DOC,
        ),
        _alias_spec(
            "get_impact_context",
            "Blast-radius analysis: who calls a class and what breaks if it changes? Java only.",
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
            docstring_override=_IMPACT_CONTEXT_DOC,
        ),
        _alias_spec(
            "modernize_context",
            "Modernization analysis: dead zones, hotspot scores, upgrade candidates.",
            ("modernize",),
            (
                ToolParamSpec("repo_path", "argument", str, required=False, default=".", is_path=True),
            ),
            lambda inputs: ["modernize", str(inputs.get("repo_path", "."))],
            supported_targets=("repo_path",),
            unsupported_targets=("file_path",),
            validator=validate_repo_path,
            docstring_override=_MODERNIZE_DOC,
        ),

        # --- cache / freshness aliases ---
        _alias_spec(
            "check_freshness",
            "Report RIS freshness relative to the current git HEAD.",
            ("cache", "freshness"),
            (
                ToolParamSpec("repo_path", "argument", str, required=False, default=".", is_path=True),
            ),
            lambda inputs: ["cache", "freshness", str(inputs.get("repo_path", ".")), "--json"],
            supported_targets=("repo_path",),
            unsupported_targets=("file_path",),
            validator=validate_repo_path,
            docstring_override=_CHECK_FRESHNESS_DOC,
        ),
        _alias_spec(
            "get_cold_start_context",
            "Instant session bootstrap from persisted Repository Intelligence Snapshot (RIS).",
            ("cold-start",),
            (
                ToolParamSpec("repo_path", "argument", str, required=False, default=".", is_path=True),
            ),
            lambda inputs: ["cold-start", str(inputs.get("repo_path", "."))],
            supported_targets=("repo_path",),
            unsupported_targets=("file_path",),
            validator=validate_repo_path,
            docstring_override=_COLD_START_DOC,
        ),

        # --- get_endpoints: clean alias replacing raw canonical with 7 CLI params ---
        _alias_spec(
            "get_endpoints",
            "REST API endpoint surface extraction from Java source files. JAVA ONLY.",
            ("endpoints",),
            (
                ToolParamSpec("repo_path", "argument", str, required=False, default=".", is_path=True),
            ),
            lambda inputs: ["endpoints", str(inputs.get("repo_path", "."))],
            supported_targets=("repo_path",),
            unsupported_targets=("file_path",),
            validator=validate_repo_path,
            docstring_override=_GET_ENDPOINTS_DOC,
        ),

        # --- get_validation: clean alias replacing raw canonical (6 CLI params) ---
        _alias_spec(
            "get_validation",
            "Request-body validation surface per endpoint (constraints + custom validators). JAVA/SPRING ONLY.",
            ("validation",),
            (
                ToolParamSpec("repo_path", "argument", str, required=False, default=".", is_path=True),
                ToolParamSpec("gaps_only", "option", bool, required=False, default=False,
                              option_names=("--gaps-only",), is_flag=True,
                              help="Return only endpoints/fields lacking validation."),
            ),
            lambda inputs: (
                ["validation", str(inputs.get("repo_path", "."))]
                + (["--gaps-only"] if bool(inputs.get("gaps_only")) else [])
            ),
            supported_targets=("repo_path",),
            unsupported_targets=("file_path",),
            validator=validate_repo_path,
            docstring_override=_GET_VALIDATION_DOC,
        ),

        # --- cache management: curated aliases stripping CLI noise params ---
        _alias_spec(
            "cache_status",
            "Report cache metadata for a repository.",
            ("cache", "status"),
            (
                ToolParamSpec("repo_path", "argument", str, required=False, default=".", is_path=True),
            ),
            lambda inputs: ["cache", "status", str(inputs.get("repo_path", ".")), "--json"],
            supported_targets=("repo_path",),
            unsupported_targets=("file_path",),
            validator=validate_repo_path,
            docstring_override=_CACHE_STATUS_DOC,
        ),
        _alias_spec(
            "cache_warm",
            "Warm the cache for a repository (builds compact and agent analysis snapshots).",
            ("cache", "warm"),
            (
                ToolParamSpec("repo_path", "argument", str, required=False, default=".", is_path=True),
            ),
            lambda inputs: ["cache", "warm", str(inputs.get("repo_path", "."))],
            supported_targets=("repo_path",),
            unsupported_targets=("file_path",),
            validator=validate_repo_path,
            docstring_override=_CACHE_WARM_DOC,
        ),
        _alias_spec(
            "cache_clear",
            "Clear cached analysis for a repository.",
            ("cache", "clear"),
            (
                ToolParamSpec("repo_path", "argument", str, required=False, default=".", is_path=True),
                ToolParamSpec("include_ris", "option", bool, required=False, default=False,
                              option_names=("--include-ris",), is_flag=True,
                              help="Also remove RIS snapshot (default: False)."),
            ),
            lambda inputs: (
                ["cache", "clear", str(inputs.get("repo_path", ".")), "--yes"]
                + (["--include-ris"] if bool(inputs.get("include_ris")) else [])
            ),
            supported_targets=("repo_path",),
            unsupported_targets=("file_path",),
            validator=validate_repo_path,
            docstring_override=_CACHE_CLEAR_DOC,
        ),
    ]


def _internal_specs() -> list[ToolSpec]:
    return [
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
    # Raw CLI tools with output-format/noise params — clean alias with only repo_path exists
    "endpoints",          # 7 CLI params (output_path/format/copy/etc.); use get_endpoints
    "validation",         # 6 CLI params (output_path/format/copy/path_prefix/gaps_only); use get_validation
    "cache_status",       # path + json_output flag; curated alias strips json_output, renames path→repo_path
    "cache_warm",         # path + compact/agent output flags; curated alias keeps only repo_path
    "cache_clear",        # path + yes/all_ destructive flags; curated alias keeps repo_path + include_ris only
    # Curated overrides — canonical CLI spec replaced by cleaner alias with same name.
    # Listed here so validate_registry() skips CLI param-drift checks on the alias.
    "spring_audit",       # curated: repo_path + scope + min_severity only (strips output_path/format/copy)
    "impact_chain",       # curated: repo_path + symbol + depth + query_type with choices
    "migrate_check",      # curated: repo_path + min_severity only (strips output_path/format/copy/ci)
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
    # Human admin actions — not agent actions
    "activate",    # Pro license key activation; human admin only, not an agent operation
    "config",      # returns plain-text config dump (not JSON); version via `version`, telemetry via `telemetry`
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

    _MIGRATE_CHECK_DOC = """\
Spring Boot 2→3 migration readiness: javax→jakarta namespace blockers. JAVA ONLY.

When to call: when asked about Spring Boot migration readiness, javax vs jakarta imports,
or upgrading from Spring Boot 2.x to 3.x. Use BEFORE get_spring_audit when the goal
is migration planning rather than ongoing Spring semantic audit.
Do NOT call on non-Java repositories — returns readiness_score=100 with no findings.

Rules detected:
  MIG-001 critical — javax.persistence imports (JPA; will not compile after migration)
  MIG-002 high     — javax.servlet imports (Servlet API changed)
  MIG-003 high     — javax.validation imports (Bean Validation changed)
  MIG-004 high     — javax.transaction imports (TX API changed)
  MIG-005 high     — extends WebSecurityConfigurerAdapter (removed in Spring Security 6)
  MIG-006 medium   — javax.annotation imports (CDI annotations)
  MIG-007 medium   — javax.inject imports (DI annotations)
  MIG-008 medium   — javax.ws.rs imports (JAX-RS API)

Returns: schema_version, readiness_score (0–100; 100=ready to migrate),
  jakarta_readiness / boot3_readiness / jdk_modernization (per-dimension 0–100),
  blocking_count, estimated_effort_days, spring_boot_2_detected (true|false|null —
  null=undetermined, never assumed true), spring_boot_version_detected,
  summary (total_findings, affected_files, by_severity, by_rule), findings[],
  limitations, metadata.
findings fields: id, rule_id, severity, title, source_file, first_line,
  imports_found, explanation, fix_hint.

repo_path: absolute path to the Java repository (default: current working directory).
min_severity: "low" (default) | "medium" | "high" | "critical" — filter threshold.
"""

    migrate_check = _alias_spec(
        "migrate_check",
        "Spring Boot 2→3 migration readiness: javax→jakarta blockers. JAVA ONLY.",
        ("migrate-check",),
        (
            ToolParamSpec("repo_path", "argument", str, required=False, default=".", is_path=True,
                          help="Absolute path to the Java repository."),
            ToolParamSpec("min_severity", "option", str, required=False, default="low",
                          option_names=("--min-severity",), choices=("low", "medium", "high", "critical"),
                          help="low (default) | medium | high | critical"),
        ),
        lambda inputs: [
            "migrate-check",
            str(inputs.get("repo_path", ".")),
            "--min-severity", str(inputs.get("min_severity", "low")),
        ],
        supported_targets=("repo_path",),
        unsupported_targets=("file_path",),
        validator=validate_repo_path,
        docstring_override=_MIGRATE_CHECK_DOC,
    )

    return [spring_audit, impact_chain, migrate_check]


@lru_cache(maxsize=1)
def build_tool_specs() -> tuple[ToolSpec, ...]:
    """Build the full MCP registry from the live CLI runtime."""
    canonical_raw = [
        _canonical_spec_for_runtime_command(runtime)
        for runtime in discover_runtime_commands()
        if (runtime.callback is not None or runtime.path == ())
        and not runtime.hidden
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

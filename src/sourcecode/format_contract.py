"""Single source of truth for per-command output-format contracts.

Every CLI command that emits machine-consumable output validates its
``--format`` option through this registry so that:

  * the set of allowed formats for each command lives in exactly one place,
  * ``-f json`` is a strict contract on every command (pure JSON to stdout),
  * invalid-format errors share an identical envelope shape and exit code.

The registry value is an *ordered* tuple; element ``0`` is the command's
default and matches its Typer option default. Defaults are intentionally NOT
changed when centralizing — ``explain`` and ``pr-impact`` keep their
human-facing ``text`` default — to avoid breaking existing scripts. The strict
guarantee is on ``-f json``, which every command supports.

Exit-code policy: an invalid ``--format`` is an argument-validation error and
exits with code ``2`` for every command (matching the documented
``arg validation -> exit 2`` convention used by the root command).
"""

from __future__ import annotations

# Command name (as registered with ``@app.command``, or "main" for the root
# command) -> ordered tuple of allowed formats. Element 0 is the default.
FORMAT_REGISTRY: "dict[str, tuple[str, ...]]" = {
    "main": ("json", "yaml"),
    "repo-ir": ("json", "yaml"),
    "impact": ("json", "yaml"),
    "endpoints": ("json", "yaml"),
    "validation": ("json", "yaml"),
    "impact-chain": ("json", "yaml"),
    "pr-impact": ("text", "json"),
    "migrate-check": ("json", "text"),
    "spring-audit": ("json", "yaml", "github-comment"),
    "explain": ("text", "json"),
    "prepare-context": ("json", "github-comment"),
}

# Invalid --format is an argument-validation error.
FORMAT_ERROR_EXIT_CODE = 2

# The strict machine-readable format every command must support.
STRICT_FORMAT = "json"


def allowed_formats(command: str) -> "tuple[str, ...]":
    """Return the ordered tuple of allowed formats for ``command``.

    Raises ``KeyError`` if the command has no registered contract — a
    programming error, surfaced loudly rather than silently allowing anything.
    """
    try:
        return FORMAT_REGISTRY[command]
    except KeyError as exc:
        raise KeyError(
            f"No format contract registered for command '{command}'. "
            f"Add it to FORMAT_REGISTRY in sourcecode/format_contract.py."
        ) from exc


def default_format(command: str) -> str:
    """Return the default format for ``command`` (registry element 0)."""
    return allowed_formats(command)[0]


def is_valid_format(command: str, fmt: str) -> bool:
    """True iff ``fmt`` is allowed for ``command``."""
    return fmt in FORMAT_REGISTRY.get(command, ())


def format_error_context(command: str, fmt: str) -> "dict[str, object]":
    """Build the homogeneous error-envelope fields for an invalid ``--format``.

    Returns a dict whose ``message`` key is the human message and whose
    remaining keys are passed verbatim as the error-envelope context, so every
    command produces an identically shaped ``--format`` error.
    """
    allowed = list(allowed_formats(command))
    joined = ", ".join(allowed)
    return {
        "message": f"Invalid value '{fmt}' for --format. Valid values: {joined}.",
        "flag": "--format",
        "value": fmt,
        "valid_values": allowed,
        "hint": "Choose one of the supported --format values.",
        "expected": f"One of: {joined}",
    }

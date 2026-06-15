"""Unit tests for the centralized output-format contract (wave 19-01).

Covers the registry invariants, the lookup/validation helpers, the homogeneous
error envelope, and the cli `_enforce_format` integration (valid -> no-op,
invalid -> JSON error on stderr + exit 2).
"""

import json

import pytest

from sourcecode import format_contract as fc


class TestRegistryInvariants:
    def test_every_command_supports_strict_json(self):
        """`-f json` must be a universal strict contract."""
        for command, formats in fc.FORMAT_REGISTRY.items():
            assert fc.STRICT_FORMAT in formats, (
                f"command '{command}' must support '{fc.STRICT_FORMAT}'"
            )

    def test_registry_values_are_nonempty_ordered_tuples(self):
        for command, formats in fc.FORMAT_REGISTRY.items():
            assert isinstance(formats, tuple) and formats, command
            # no duplicates
            assert len(set(formats)) == len(formats), command

    def test_human_facing_defaults_preserved(self):
        """explain and pr-impact keep their text default (decision 2026-06-15)."""
        assert fc.default_format("explain") == "text"
        assert fc.default_format("pr-impact") == "text"

    def test_machine_commands_default_json(self):
        for command in ("main", "repo-ir", "impact", "endpoints", "spring-audit"):
            assert fc.default_format(command) == "json", command


class TestLookups:
    def test_allowed_formats_returns_registry_entry(self):
        assert fc.allowed_formats("spring-audit") == ("json", "yaml", "github-comment")

    def test_allowed_formats_unknown_command_raises(self):
        with pytest.raises(KeyError):
            fc.allowed_formats("does-not-exist")

    def test_default_format_is_first_element(self):
        for command, formats in fc.FORMAT_REGISTRY.items():
            assert fc.default_format(command) == formats[0]

    @pytest.mark.parametrize(
        "command,fmt,valid",
        [
            ("impact", "json", True),
            ("impact", "yaml", True),
            ("impact", "text", False),
            ("explain", "text", True),
            ("explain", "json", True),
            ("explain", "yaml", False),
            ("spring-audit", "github-comment", True),
            ("endpoints", "github-comment", False),
            ("unknown-cmd", "json", False),
        ],
    )
    def test_is_valid_format(self, command, fmt, valid):
        assert fc.is_valid_format(command, fmt) is valid


class TestErrorContext:
    def test_error_context_shape(self):
        ctx = fc.format_error_context("impact", "xml")
        assert ctx["flag"] == "--format"
        assert ctx["value"] == "xml"
        assert ctx["valid_values"] == ["json", "yaml"]
        assert "json, yaml" in ctx["message"]
        assert "xml" in ctx["message"]
        assert ctx["expected"] == "One of: json, yaml"

    def test_error_context_uses_command_specific_allowed(self):
        ctx = fc.format_error_context("spring-audit", "xml")
        assert ctx["valid_values"] == ["json", "yaml", "github-comment"]


class TestEnforceFormatIntegration:
    def test_valid_format_is_noop(self):
        from sourcecode.cli import _enforce_format

        # Must not raise.
        _enforce_format("impact", "json")
        _enforce_format("explain", "text")

    def test_invalid_format_exits_2_with_json_error(self, capsys):
        import typer

        from sourcecode.cli import _enforce_format

        with pytest.raises(typer.Exit) as exc:
            _enforce_format("spring-audit", "xml")
        assert exc.value.exit_code == 2

        captured = capsys.readouterr()
        payload = json.loads(captured.err.strip())
        assert payload["error"]["code"] == "INVALID_INPUT"
        assert payload["flag"] == "--format"
        assert payload["value"] == "xml"
        assert "github-comment" in payload["valid_values"]

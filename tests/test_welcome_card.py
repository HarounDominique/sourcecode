"""Welcome card shown on a bare TTY invocation.

The card is human-only UX: it must never alter the JSON machine contract that
agents and pipes rely on. These tests lock the card content and the tier line.
"""
from __future__ import annotations

import sourcecode.cli as cli
from sourcecode import __version__


def test_welcome_card_renders_brand_and_quickstart(capsys) -> None:
    cli._print_welcome()
    out = capsys.readouterr().out

    # Branding: name + version present
    assert "sourcecode" in out
    assert __version__ in out

    # Quickstart pointers a new user needs
    assert "--compact" in out
    assert "prepare-context onboard" in out
    assert "mcp init" in out
    assert "--help" in out


def test_welcome_card_shows_tier(capsys, monkeypatch) -> None:
    import sourcecode.license as lic

    # Free → activate hint present
    monkeypatch.setattr(lic, "is_pro", False)
    cli._print_welcome()
    free_out = capsys.readouterr().out
    assert "Free" in free_out
    assert "activate" in free_out

    # Pro → no activate hint
    monkeypatch.setattr(lic, "is_pro", True)
    cli._print_welcome()
    pro_out = capsys.readouterr().out
    assert "Pro" in pro_out
    assert "activate" not in pro_out

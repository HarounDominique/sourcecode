"""Regression tests for C3 — UTF-8 double-encoding of accented characters.

Fixture files:
  - tests/fixtures/latin1_sample_iso.java  — Latin-1 encoded (bytes 0xe1, 0xf3, etc.)
  - tests/fixtures/latin1_sample.java      — UTF-8 encoded (multibyte sequences)

Both should decode cleanly to the correct Unicode characters.
The old code read Latin-1 files with errors='replace', producing double-encoded garbage.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from sourcecode.tree_utils import safe_read_text

FIXTURES = Path(__file__).parent / "fixtures"
LATIN1_FILE = FIXTURES / "latin1_sample_iso.java"
UTF8_FILE = FIXTURES / "latin1_sample.java"


class TestSafeReadText:
    """safe_read_text must decode both UTF-8 and Latin-1 files correctly."""

    def test_utf8_file_reads_correctly(self):
        content = safe_read_text(UTF8_FILE)
        # These characters must appear verbatim — not as double-encoded sequences
        assert "días" in content, f"Expected 'días', got: {content[:200]!r}"
        assert "ñ" in content or "función" in content or "ñoño" in content
        assert "Ã" not in content, "Double-encoded: U+00C3 found (Ã). Encoding was mangled."

    def test_latin1_file_reads_correctly(self):
        content = safe_read_text(LATIN1_FILE)
        # Latin-1 0xe1 = 'á' — must decode to the correct character
        assert "á" in content, f"Expected 'á', got: {content!r}"
        assert "ó" in content, f"Expected 'ó', got: {content!r}"
        # No replacement chars
        assert "�" not in content, "Replacement character found — file was not decoded correctly."
        # No double-encoding: 'á' in double-encoded UTF-8-as-Latin-1 would appear as 'Ã¡'
        assert "Ã" not in content, "Double-encoded: U+00C3 found (Ã). Latin-1 file was not decoded correctly."

    def test_nonexistent_file_raises_oserror(self):
        with pytest.raises(OSError):
            safe_read_text(FIXTURES / "does_not_exist.java")


class TestCodeNotesEncoding:
    """CodeNotesAnalyzer must extract notes from Latin-1 files without encoding corruption."""

    def test_latin1_todo_extracted_cleanly(self):
        from sourcecode.code_notes_analyzer import CodeNotesAnalyzer

        # analyze() scans FIXTURES dir — Latin-1 file is there
        analyzer = CodeNotesAnalyzer()
        notes, _, _ = analyzer.analyze(FIXTURES)
        # Should have extracted TODO without replacement chars
        todo_texts = [n.text for n in notes if "latin1_sample_iso" in n.path]
        for text in todo_texts:
            assert "â" not in text, f"Double-encoded byte found in TODO text: {text!r}"
            assert "Ã" not in text, f"Double-encoded char in TODO text: {text!r}"
            assert "�" not in text, f"Replacement char in TODO text: {text!r}"


class TestGetAvailableRefsWindowsEncoding:
    """Regression: _get_available_refs crashed on Windows due to cp1252 encoding and None stdout.

    Bug: subprocess.run(text=True) without encoding= uses the OS default (cp1252 on Windows).
    Git output containing byte 0x8d raises UnicodeDecodeError (not caught). If stdout is None,
    r.stdout.splitlines() raises AttributeError. Both cause unhandled tracebacks.
    Fix: encoding="utf-8", errors="replace", and (r.stdout or "").splitlines().
    """

    def _make_builder(self) -> "TaskContextBuilder":
        from sourcecode.prepare_context import TaskContextBuilder
        return TaskContextBuilder(FIXTURES)

    def test_case_a_stdout_none_does_not_crash(self, monkeypatch):
        """stdout=None must not raise AttributeError on splitlines()."""
        import subprocess
        import types

        fake = types.SimpleNamespace(returncode=0, stdout=None, stderr="")
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: fake)

        builder = self._make_builder()
        refs, suggested = builder._get_available_refs("main")
        assert refs == []
        assert suggested is None

    def test_case_b_non_utf8_bytes_no_unicode_error(self, monkeypatch):
        """Bytes invalid in cp1252 (e.g. 0x8d) must not raise UnicodeDecodeError.

        With encoding='utf-8' + errors='replace', replacement chars appear instead.
        """
        import subprocess
        import types

        # Simulate git returning a branch name that survived errors="replace" decoding
        # (the replacement char � stands in for the bad byte)
        replaced = "develop\nfeature/caf�\n"
        fake = types.SimpleNamespace(returncode=0, stdout=replaced, stderr="")
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: fake)

        builder = self._make_builder()
        refs, _ = builder._get_available_refs("main")
        assert "develop" in refs

    def test_case_c_nonexistent_ref_returns_available_refs(self, monkeypatch):
        """Non-existent --since ref must yield a list of available branches, not a crash."""
        import subprocess
        import types

        fake = types.SimpleNamespace(
            returncode=0,
            stdout="develop\norigin/develop\nrelease/1.2\n",
            stderr="",
        )
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: fake)

        builder = self._make_builder()
        refs, suggested = builder._get_available_refs("main")
        assert "develop" in refs
        # invalid_ref="main", all_refs contains no "master" → no suggestion
        assert suggested is None

    def test_case_d_nonzero_returncode_returns_empty(self, monkeypatch):
        """returncode != 0 must not parse stdout — returns empty list silently."""
        import subprocess
        import types

        fake = types.SimpleNamespace(returncode=128, stdout="fatal: not a git repo\n", stderr="")
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: fake)

        builder = self._make_builder()
        refs, suggested = builder._get_available_refs("main")
        assert refs == []
        assert suggested is None

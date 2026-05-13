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

"""Tests for edit input normalization."""

import os
import tempfile
from pathlib import Path

import pytest

from kiln.edit_normalize import (
    _desanitize,
    _find_actual_string,
    _normalize_quotes,
    _preserve_quote_style,
    _strip_trailing_whitespace,
    _apply_curly_double_quotes,
    _apply_curly_single_quotes,
    normalize_edit_inputs,
)
from kiln.tools import edit_file, FileState


# -----------------------------------------------------------------------
# _normalize_quotes
# -----------------------------------------------------------------------

class TestNormalizeQuotes:
    def test_curly_single_to_straight(self):
        assert _normalize_quotes("\u2018hello\u2019") == "'hello'"

    def test_curly_double_to_straight(self):
        assert _normalize_quotes("\u201chi\u201d") == '"hi"'

    def test_mixed_quotes(self):
        assert _normalize_quotes("\u201cdon\u2019t\u201d") == '"don\'t"'

    def test_no_curly_quotes(self):
        s = "plain 'single' and \"double\""
        assert _normalize_quotes(s) == s

    def test_empty_string(self):
        assert _normalize_quotes("") == ""


# -----------------------------------------------------------------------
# _find_actual_string
# -----------------------------------------------------------------------

class TestFindActualString:
    def test_exact_match(self):
        assert _find_actual_string("hello world", "hello") == "hello"

    def test_curly_quote_fallback(self):
        content = "She said \u201chello\u201d"
        result = _find_actual_string(content, 'She said "hello"')
        assert result == "She said \u201chello\u201d"

    def test_curly_single_quote_fallback(self):
        content = "it\u2019s a test"
        result = _find_actual_string(content, "it's a test")
        assert result == "it\u2019s a test"

    def test_no_match(self):
        assert _find_actual_string("hello world", "goodbye") is None

    def test_prefers_exact_match(self):
        # If both exact and normalized match, exact wins
        content = 'She said "hello"'
        result = _find_actual_string(content, 'She said "hello"')
        assert result == 'She said "hello"'


# -----------------------------------------------------------------------
# _apply_curly_double_quotes / _apply_curly_single_quotes
# -----------------------------------------------------------------------

class TestApplyCurlyQuotes:
    def test_double_opening_at_start(self):
        assert _apply_curly_double_quotes('"hello"') == "\u201chello\u201d"

    def test_double_after_space(self):
        assert _apply_curly_double_quotes('say "hi"') == "say \u201chi\u201d"

    def test_double_after_paren(self):
        assert _apply_curly_double_quotes('("hi")') == "(\u201chi\u201d)"

    def test_single_basic(self):
        assert _apply_curly_single_quotes("'hello'") == "\u2018hello\u2019"

    def test_single_contraction(self):
        # Apostrophe between letters = contraction = right curly
        result = _apply_curly_single_quotes("don't")
        assert result == "don\u2019t"

    def test_single_after_space(self):
        result = _apply_curly_single_quotes("say 'hi'")
        assert result == "say \u2018hi\u2019"


# -----------------------------------------------------------------------
# _preserve_quote_style
# -----------------------------------------------------------------------

class TestPreserveQuoteStyle:
    def test_no_normalization_needed(self):
        # old_string == actual_old → no change
        assert _preserve_quote_style("hello", "hello", "world") == "world"

    def test_double_curly_preserved(self):
        old = 'say "hi"'
        actual = "say \u201chi\u201d"
        result = _preserve_quote_style(old, actual, 'say "bye"')
        assert result == "say \u201cbye\u201d"

    def test_single_curly_preserved(self):
        old = "it's"
        actual = "it\u2019s"
        result = _preserve_quote_style(old, actual, "she's")
        assert result == "she\u2019s"

    def test_no_curly_in_actual(self):
        # actual_old has no curly quotes → return new_string unchanged
        result = _preserve_quote_style("a", "b", "new")
        assert result == "new"


# -----------------------------------------------------------------------
# _desanitize
# -----------------------------------------------------------------------

class TestDesanitize:
    def test_fnr(self):
        result, applied = _desanitize("see <fnr> here")
        assert result == "see <function_results> here"
        assert len(applied) == 1

    def test_multiple_replacements(self):
        result, applied = _desanitize("<n>test</n>")
        assert result == "<name>test</name>"
        assert len(applied) == 2

    def test_no_replacements(self):
        result, applied = _desanitize("plain text")
        assert result == "plain text"
        assert applied == []

    def test_human_assistant(self):
        result, applied = _desanitize("text\n\nH: hello")
        assert result == "text\n\nHuman: hello"
        assert len(applied) == 1

    def test_meta_tokens(self):
        result, applied = _desanitize("< META_START > content < META_END >")
        assert result == "<META_START> content <META_END>"
        assert len(applied) == 2


# -----------------------------------------------------------------------
# _strip_trailing_whitespace
# -----------------------------------------------------------------------

class TestStripTrailingWhitespace:
    def test_strips_spaces(self):
        assert _strip_trailing_whitespace("hello   \nworld  \n") == "hello\nworld\n"

    def test_strips_tabs(self):
        assert _strip_trailing_whitespace("hello\t\nworld") == "hello\nworld"

    def test_preserves_leading_whitespace(self):
        assert _strip_trailing_whitespace("  hello  \n  world  ") == "  hello\n  world"

    def test_preserves_crlf(self):
        assert _strip_trailing_whitespace("hello  \r\nworld  \r\n") == "hello\r\nworld\r\n"

    def test_empty_string(self):
        assert _strip_trailing_whitespace("") == ""

    def test_only_whitespace_lines(self):
        assert _strip_trailing_whitespace("   \n   \n") == "\n\n"


# -----------------------------------------------------------------------
# normalize_edit_inputs (orchestrator)
# -----------------------------------------------------------------------

class TestNormalizeEditInputs:
    def test_exact_match_passthrough(self):
        content = "hello world"
        old, new = normalize_edit_inputs(content, "test.py", "hello", "bye")
        assert old == "hello"
        assert new == "bye"

    def test_trailing_whitespace_stripped_on_python(self):
        content = "hello world"
        old, new = normalize_edit_inputs(content, "test.py", "hello", "bye   ")
        assert new == "bye"

    def test_trailing_whitespace_kept_on_markdown(self):
        content = "hello world"
        old, new = normalize_edit_inputs(content, "README.md", "hello", "bye   ")
        assert new == "bye   "

    def test_trailing_whitespace_kept_on_mdx(self):
        content = "hello world"
        old, new = normalize_edit_inputs(content, "doc.MDX", "hello", "bye   ")
        assert new == "bye   "

    def test_desanitize_fallback(self):
        content = "see <function_results> here"
        old, new = normalize_edit_inputs(
            content, "test.py",
            "see <fnr> here",
            "see <fnr> there",
        )
        assert old == "see <function_results> here"
        assert new == "see <function_results> there"

    def test_curly_quote_fallback(self):
        content = "She said \u201chello\u201d to him"
        old, new = normalize_edit_inputs(
            content, "test.txt",
            'She said "hello" to him',
            'She said "goodbye" to him',
        )
        assert old == "She said \u201chello\u201d to him"
        assert new == "She said \u201cgoodbye\u201d to him"

    def test_combined_desanitize_and_quotes(self):
        content = "\u201c<function_results>\u201d"
        old, new = normalize_edit_inputs(
            content, "test.py",
            '"<fnr>"',
            '"<fnr> done"',
        )
        assert old == "\u201c<function_results>\u201d"
        assert "function_results" in new

    def test_no_match_returns_original(self):
        content = "hello world"
        old, new = normalize_edit_inputs(content, "test.py", "goodbye", "farewell")
        assert old == "goodbye"
        assert new == "farewell"


# -----------------------------------------------------------------------
# Integration: edit_file with normalization
# -----------------------------------------------------------------------

class TestEditFileIntegration:
    def _make_file(self, content: str) -> tuple[str, FileState]:
        """Create a temp file, record it as read, return (path, file_state)."""
        fd, path = tempfile.mkstemp(suffix=".py")
        os.write(fd, content.encode())
        os.close(fd)
        fs = FileState()
        fs.record_read(path)
        return path, fs

    def test_basic_edit(self):
        path, fs = self._make_file("hello world")
        result = edit_file(path, "hello", "goodbye", fs)
        assert "successfully" in result["content"][0]["text"]
        assert Path(path).read_text() == "goodbye world"
        os.unlink(path)

    def test_curly_quote_edit(self):
        path, fs = self._make_file("She said \u201chello\u201d to him")
        result = edit_file(path, 'She said "hello" to him', 'She said "goodbye" to him', fs)
        assert "successfully" in result["content"][0]["text"]
        written = Path(path).read_text()
        assert "\u201cgoodbye\u201d" in written
        os.unlink(path)

    def test_desanitize_edit(self):
        path, fs = self._make_file("see <function_results> here")
        result = edit_file(path, "see <fnr> here", "see <fnr> there", fs)
        assert "successfully" in result["content"][0]["text"]
        written = Path(path).read_text()
        assert written == "see <function_results> there"
        os.unlink(path)

    def test_trailing_whitespace_stripped(self):
        path, fs = self._make_file("hello world")
        result = edit_file(path, "hello", "goodbye   ", fs)
        assert "successfully" in result["content"][0]["text"]
        written = Path(path).read_text()
        assert written == "goodbye world"
        os.unlink(path)

    def test_trailing_whitespace_kept_for_markdown(self):
        fd, path = tempfile.mkstemp(suffix=".md")
        os.write(fd, b"hello world")
        os.close(fd)
        fs = FileState()
        fs.record_read(path)
        result = edit_file(path, "hello", "goodbye   ", fs)
        assert "successfully" in result["content"][0]["text"]
        written = Path(path).read_text()
        # Markdown preserves trailing whitespace (used for hard line breaks)
        assert written == "goodbye    world"
        os.unlink(path)

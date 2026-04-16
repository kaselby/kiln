"""Regression tests for TUI HTML sanitization.

The TUI prints styled text via prompt_toolkit's HTML class, which parses
its input as XML. XML 1.0 forbids most ASCII control characters in content
(only \\t, \\n, \\r are allowed). When tool output contains stray control
chars — NUL from binary files, BEL from shells, bare ESC, etc. — the
expat parser crashes with "not well-formed (invalid token)".

These tests lock in the sanitizer behavior: ANSI escape sequences and
XML-invalid control characters must both be stripped before content
reaches HTML.format().
"""

import pytest

from prompt_toolkit.formatted_text import HTML

from kiln.tui.app import _sanitize_for_html


class TestSanitizer:
    def test_strips_nul(self):
        assert _sanitize_for_html("abc\x00def") == "abcdef"

    def test_strips_bel(self):
        assert _sanitize_for_html("abc\x07def") == "abcdef"

    def test_strips_vt(self):
        assert _sanitize_for_html("abc\x0bdef") == "abcdef"

    def test_strips_ff(self):
        assert _sanitize_for_html("abc\x0cdef") == "abcdef"

    def test_strips_bare_esc(self):
        assert _sanitize_for_html("abc\x1bdef") == "abcdef"

    def test_strips_ansi_csi(self):
        assert _sanitize_for_html("abc\x1b[31mred\x1b[0mdef") == "abcreddef"

    def test_preserves_tab(self):
        assert _sanitize_for_html("col1\tcol2") == "col1\tcol2"

    def test_preserves_lf(self):
        assert _sanitize_for_html("line1\nline2") == "line1\nline2"

    def test_preserves_cr(self):
        assert _sanitize_for_html("line1\rline2") == "line1\rline2"

    def test_strips_all_control_range(self):
        """0x00-0x08, 0x0B, 0x0C, 0x0E-0x1F must all be stripped."""
        forbidden = (
            [chr(c) for c in range(0x00, 0x09)]
            + [chr(0x0B), chr(0x0C)]
            + [chr(c) for c in range(0x0E, 0x20)]
        )
        for ch in forbidden:
            s = f"a{ch}b"
            assert _sanitize_for_html(s) == "ab", f"failed to strip {ch!r}"


class TestHTMLParseAfterSanitize:
    """Sanitized strings must not crash prompt_toolkit's HTML parser."""

    @pytest.mark.parametrize(
        "payload",
        [
            "abc\x00def",         # NUL (binary files)
            "ring\x07bell",        # BEL (shells, tools)
            "a\x0bb",              # VT
            "a\x0cb",              # FF
            "pre\x1bpost",         # bare ESC
            "pre\x1b[31mred\x1b[0mpost",  # full ANSI CSI
            # Realistic multi-line tool output with stray BEL in col 8, line 8
            "\n".join([f"line{i}" for i in range(7)]) + "\ncolabc\x07more",
        ],
    )
    def test_format_after_sanitize_ok(self, payload):
        cleaned = _sanitize_for_html(payload)
        # Should not raise
        HTML("<dim>{}</dim>").format(cleaned)

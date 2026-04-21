"""Regression tests for TUI markdown rendering.

The markdown renderer turns markdown text into prompt_toolkit FormattedText
for the response-commit pass. Historically its architecture was a flat
token walk with a state machine — which produced a string of bugs that
conditional-based fixes could not cleanly resolve:

  - no blank line between top-level blocks (headings ran into paragraphs),
  - nested list items collided (outer marker text ran into inner marker),
  - continuation paragraphs in loose list items failed to indent,
  - loose lists produced doubled newlines at item boundaries.

These tests lock in the rewrite: a tree walk over markdown_it.tree.SyntaxTreeNode
composed with three primitives (_render_node, _join_blocks, _indent_block).
Spacing is a property of the composer; indentation recurses naturally.

Each test focuses on one observable property of the rendered text. We check
rendered text (styles elided) for layout, and a few tests also check the
styled tuples for the formatting (bold / italic / heading) application.
"""

import pytest

from kiln.tui.app import _markdown_to_ft


def render(md: str) -> str:
    """Return rendered text with styles elided (for layout assertions)."""
    ft = _markdown_to_ft(md)
    return "".join(text for _, text in ft)


def tuples(md: str) -> list[tuple[str, str]]:
    """Return the raw styled tuples (for style assertions)."""
    return list(_markdown_to_ft(md))


# ---- Top-level spacing (bug 1: no blank line between blocks) ----


class TestTopLevelSpacing:
    def test_paragraph_paragraph(self):
        assert render("First.\n\nSecond.") == "First.\n\nSecond."

    def test_heading_paragraph(self):
        assert render("# H\n\nBody.") == "H\n\nBody."

    def test_paragraph_list(self):
        assert render("Intro.\n\n- a\n- b") == "Intro.\n\n• a\n• b"

    def test_list_paragraph(self):
        assert render("- a\n- b\n\nOutro.") == "• a\n• b\n\nOutro."

    def test_heading_list_paragraph(self):
        out = render("# H\n\n- a\n- b\n\nBody.")
        assert out == "H\n\n• a\n• b\n\nBody."

    def test_multiple_headings(self):
        assert render("# A\n\n## B\n\n### C") == "A\n\nB\n\nC"

    def test_empty_input(self):
        assert render("") == ""


# ---- Nested lists (bug 2: items collided, no newline between outer+inner) ----


class TestNestedLists:
    def test_two_levels_tight(self):
        md = "- outer a\n  - inner a1\n  - inner a2\n- outer b"
        expected = "• outer a\n  • inner a1\n  • inner a2\n• outer b"
        assert render(md) == expected

    def test_three_levels_tight(self):
        md = "- outer\n  - mid\n    - deep"
        assert render(md) == "• outer\n  • mid\n    • deep"

    def test_ordered_nested(self):
        md = "1. one\n   1. one-a\n   2. one-b\n2. two"
        assert render(md) == "1. one\n   1. one-a\n   2. one-b\n2. two"

    def test_mixed_ordered_bullet(self):
        md = "1. first\n   - sub a\n   - sub b\n2. second"
        assert render(md) == "1. first\n   • sub a\n   • sub b\n2. second"

    def test_outer_and_inner_do_not_collide(self):
        """Regression: the collision bug produced no newline between
        outer item's text and inner list's first marker."""
        out = render("- outer\n  - inner")
        # There must be a newline before the inner marker.
        assert "outer\n" in out
        assert out.count("\n") >= 1


# ---- Loose lists with continuation paragraphs (bug 3: no indent) ----


class TestLooseLists:
    def test_loose_continuation_paragraph_indented(self):
        md = "- item one\n\n  continuation\n\n- item two"
        out = render(md)
        assert out == "• item one\n\n  continuation\n\n• item two"

    def test_loose_has_blank_line_between_items(self):
        md = "- a\n\n- b\n\n- c"
        assert render(md) == "• a\n\n• b\n\n• c"

    def test_loose_nested_list_inside_item(self):
        md = "- outer\n\n  - inner x\n  - inner y\n\n- second outer"
        out = render(md)
        # Outer item contains a paragraph AND a nested list; both aligned
        # under the outer bullet.
        assert "• outer\n\n  • inner x\n  • inner y\n\n• second outer" == out


# ---- Doubled newline regression (bug 4) ----


class TestNoDoubledNewlines:
    def test_tight_list_no_blank_after_nested(self):
        """Tight outer list with a nested list inside one item must not
        produce blank lines in the output."""
        md = "- a\n  - a1\n  - a2\n- b"
        out = render(md)
        assert "\n\n" not in out

    def test_loose_list_exactly_one_blank_between_items(self):
        md = "- a\n\n- b"
        out = render(md)
        # One \n\n between items, no more.
        assert out.count("\n\n") == 1


# ---- Ordered lists ----


class TestOrderedLists:
    def test_basic(self):
        assert render("1. a\n2. b") == "1. a\n2. b"

    def test_double_digit_alignment(self):
        """Items 10+ should align: the single-digit markers get padded so
        body text stays in one column."""
        md = "\n".join(f"{i}. x" for i in range(1, 12))
        out = render(md)
        lines = out.split("\n")
        # "10. x" is 5 chars before the 'x'; "1. x" must pad to match.
        # The key property: all body 'x' characters are in the same column.
        x_columns = [line.index("x") for line in lines]
        assert len(set(x_columns)) == 1, f"body columns misaligned: {x_columns}"

    def test_honors_start_attribute(self):
        # CommonMark: a list starting at 5 renumbers from 5.
        out = render("5. five\n6. six")
        assert out == "5. five\n6. six"


# ---- Inline formatting survives the tree wrapping ----


class TestInlineFormatting:
    def test_bold_text_present(self):
        # The tree form wraps **bold** into a <strong> node; the renderer
        # must still extract the "bold" text content.
        assert "bold" in render("This is **bold** text.")

    def test_italic_text_present(self):
        assert "italic" in render("This is *italic* text.")

    def test_code_text_present(self):
        assert "code" in render("This is `code`.")

    def test_bold_has_bold_style(self):
        toks = tuples("**b**")
        bold_tok = next(t for t in toks if t[1] == "b")
        assert "bold" in bold_tok[0]

    def test_italic_has_italic_style(self):
        toks = tuples("*i*")
        italic_tok = next(t for t in toks if t[1] == "i")
        assert "italic" in italic_tok[0]

    def test_code_inline_has_code_class(self):
        toks = tuples("`c`")
        code_tok = next(t for t in toks if t[1] == "c")
        assert "md-code" in code_tok[0]

    def test_heading_has_heading_class(self):
        toks = tuples("# Title")
        title_tok = next(t for t in toks if t[1] == "Title")
        assert "text-heading" in title_tok[0]

    def test_bold_inside_list_item(self):
        """Regression: the tree-based tree walk initially dropped text inside
        strong/em wrappers inside list items."""
        out = render("- **bold** item")
        assert "bold" in out
        assert "item" in out


# ---- Code blocks and fences ----


class TestCodeBlocks:
    def test_fence_indented_by_two_spaces(self):
        md = "```\nfoo\nbar\n```"
        assert render(md) == "  foo\n  bar"

    def test_fence_with_language(self):
        md = "```python\nx = 1\n```"
        out = render(md)
        assert "  python" in out
        assert "  x = 1" in out

    def test_fence_separated_from_surrounding_paragraphs(self):
        md = "Before.\n\n```\ncode\n```\n\nAfter."
        assert render(md) == "Before.\n\n  code\n\nAfter."


# ---- Blockquotes ----


class TestBlockquotes:
    def test_blockquote_prefixed_with_bar(self):
        md = "> quoted line"
        assert render(md) == "│ quoted line"

    def test_blockquote_separated_from_paragraphs(self):
        md = "Before.\n\n> quoted\n\nAfter."
        assert render(md) == "Before.\n\n│ quoted\n\nAfter."


# ---- Horizontal rule ----


class TestHorizontalRule:
    def test_hr_is_a_line_of_dashes(self):
        out = render("Before.\n\n---\n\nAfter.")
        # 40-char unicode dash line between blank-separated paragraphs.
        assert "Before.\n\n" + ("─" * 40) + "\n\nAfter." == out


# ---- Tables ----


class TestTables:
    def test_basic_table(self):
        md = "| A | B |\n| --- | --- |\n| 1 | 2 |"
        out = render(md)
        # Header, separator, row.
        assert "A" in out and "B" in out
        assert "│" in out  # column sep
        assert "─" in out and "┼" in out  # header sep

    def test_table_cells_with_inline_formatting(self):
        # Regression: _inline_to_plain must pull text from inside strong/em
        # wrappers for correct column width measurement.
        md = "| A | B |\n| --- | --- |\n| **bold** | *it* |"
        out = render(md)
        assert "bold" in out
        assert "it" in out


# ---- Softbreak behavior ----


class TestSoftbreak:
    def test_softbreak_becomes_newline_within_paragraph(self):
        md = "line one\nline two\n\nnew para."
        assert render(md) == "line one\nline two\n\nnew para."


# ---- Smoke test: realistic assistant response ----


class TestRealisticResponse:
    def test_mixed_content_renders_with_consistent_spacing(self):
        md = (
            "Here's the plan:\n\n"
            "## Summary\n\n"
            "The bug is in `_render`.\n\n"
            "1. First cause.\n"
            "2. Second cause.\n"
            "   - sub a\n"
            "   - sub b\n\n"
            "And then:\n\n"
            "```python\ndef f(): pass\n```\n\n"
            "Done."
        )
        out = render(md)
        # Every major block is separated by exactly one blank line.
        # No spurious triple newlines anywhere.
        assert "\n\n\n" not in out
        # Nested list sub-items are indented under their parent.
        assert "   • sub a" in out
        assert "   • sub b" in out
        # Headings and paragraphs are separated.
        assert "Summary\n\nThe bug" in out
        # Code block is separated from surrounding prose.
        assert "And then:\n\n" in out
        assert "\n\nDone." in out

"""Edit input normalization for handling Claude API quirks.

Three normalizations, applied as a fallback chain before edit matching:

1. Trailing whitespace stripping — auto-strip trailing whitespace from
   new_string on non-markdown files. Always applied.

2. Desanitization — Claude's API sanitizes certain token sequences before
   the model sees them. When editing a file containing the originals, the
   model outputs sanitized versions. We reverse them.

3. Curly quote normalization — Claude can't output curly quotes, so
   old_string will use straight quotes even if the file uses curly ones.
   We detect this and preserve the file's quote style in new_string.
"""

import re
import unicodedata


# -----------------------------------------------------------------------
# Desanitization
# -----------------------------------------------------------------------

# Map from sanitized (what the model outputs) to original (what the file has).
_DESANITIZATIONS: dict[str, str] = {
    "<fnr>": "<function_results>",
    "<n>": "<name>",
    "</n>": "</name>",
    "<o>": "<output>",
    "</o>": "</output>",
    "<e>": "<error>",
    "</e>": "</error>",
    "<s>": "<system>",
    "</s>": "</system>",
    "<r>": "<result>",
    "</r>": "</result>",
    "< META_START >": "<META_START>",
    "< META_END >": "<META_END>",
    "< EOT >": "<EOT>",
    "< META >": "<META>",
    "< SOS >": "<SOS>",
    "\n\nH:": "\n\nHuman:",
    "\n\nA:": "\n\nAssistant:",
}


def _desanitize(s: str) -> tuple[str, list[tuple[str, str]]]:
    """Replace sanitized API sequences with their originals.

    Returns (desanitized_string, [(sanitized, original), ...] applied).
    """
    result = s
    applied: list[tuple[str, str]] = []
    for sanitized, original in _DESANITIZATIONS.items():
        before = result
        result = result.replace(sanitized, original)
        if result != before:
            applied.append((sanitized, original))
    return result, applied


# -----------------------------------------------------------------------
# Curly quote normalization
# -----------------------------------------------------------------------

def _normalize_quotes(s: str) -> str:
    """Convert curly quotes to straight quotes."""
    return (s
        .replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"'))


def _is_opening_context(chars: list[str], i: int) -> bool:
    """Heuristic: is the quote at position i an opening quote?"""
    if i == 0:
        return True
    prev = chars[i - 1]
    return prev in (" ", "\t", "\n", "\r", "(", "[", "{", "\u2014", "\u2013")


def _apply_curly_double_quotes(s: str) -> str:
    """Replace straight double quotes with curly ones using open/close heuristic."""
    chars = list(s)
    result = []
    for i, ch in enumerate(chars):
        if ch == '"':
            result.append("\u201c" if _is_opening_context(chars, i) else "\u201d")
        else:
            result.append(ch)
    return "".join(result)


def _apply_curly_single_quotes(s: str) -> str:
    """Replace straight single quotes with curly ones.

    Apostrophes in contractions (letter-quote-letter) get right curly quote.
    """
    chars = list(s)
    result = []
    for i, ch in enumerate(chars):
        if ch == "'":
            prev = chars[i - 1] if i > 0 else None
            nxt = chars[i + 1] if i < len(chars) - 1 else None
            prev_letter = prev is not None and unicodedata.category(prev).startswith("L")
            nxt_letter = nxt is not None and unicodedata.category(nxt).startswith("L")
            if prev_letter and nxt_letter:
                # Contraction apostrophe
                result.append("\u2019")
            else:
                result.append("\u2018" if _is_opening_context(chars, i) else "\u2019")
        else:
            result.append(ch)
    return "".join(result)


def _preserve_quote_style(old_string: str, actual_old: str, new_string: str) -> str:
    """Apply the curly quote style found in actual_old to new_string."""
    if old_string == actual_old:
        return new_string
    has_double = "\u201c" in actual_old or "\u201d" in actual_old
    has_single = "\u2018" in actual_old or "\u2019" in actual_old
    if not has_double and not has_single:
        return new_string
    result = new_string
    if has_double:
        result = _apply_curly_double_quotes(result)
    if has_single:
        result = _apply_curly_single_quotes(result)
    return result


def _find_actual_string(content: str, search: str) -> str | None:
    """Find search in content, falling back to quote-normalized matching.

    Returns the actual substring from content that matched (with its original
    curly quotes), or None if no match.
    """
    if search in content:
        return search
    normalized_search = _normalize_quotes(search)
    normalized_content = _normalize_quotes(content)
    idx = normalized_content.find(normalized_search)
    if idx != -1:
        return content[idx:idx + len(search)]
    return None


# -----------------------------------------------------------------------
# Trailing whitespace
# -----------------------------------------------------------------------

def _strip_trailing_whitespace(s: str) -> str:
    """Strip trailing whitespace from each line, preserving line endings."""
    parts = re.split(r"(\r\n|\n|\r)", s)
    result = []
    for i, part in enumerate(parts):
        if i % 2 == 0:
            result.append(part.rstrip())
        else:
            result.append(part)
    return "".join(result)


# -----------------------------------------------------------------------
# Orchestrator
# -----------------------------------------------------------------------

def normalize_edit_inputs(
    content: str, file_path: str, old_string: str, new_string: str,
) -> tuple[str, str]:
    """Normalize edit inputs to handle Claude API quirks.

    Fallback chain for old_string matching:
      1. Exact match (just strip trailing whitespace on new_string)
      2. Desanitized match (API token sequences)
      3. Quote-normalized match (curly vs straight quotes)
      4. Both combined

    new_string always gets trailing whitespace stripped (except markdown).
    """
    is_markdown = file_path.lower().endswith((".md", ".mdx"))
    if not is_markdown:
        new_string = _strip_trailing_whitespace(new_string)

    # Exact match — nothing else needed
    if old_string in content:
        return old_string, new_string

    # Try desanitization
    desanitized_old, replacements = _desanitize(old_string)
    if replacements and desanitized_old in content:
        desanitized_new = new_string
        for sanitized, original in replacements:
            desanitized_new = desanitized_new.replace(sanitized, original)
        return desanitized_old, desanitized_new

    # Try quote normalization
    actual_old = _find_actual_string(content, old_string)
    if actual_old is not None:
        return actual_old, _preserve_quote_style(old_string, actual_old, new_string)

    # Try both combined
    if replacements:
        actual_old = _find_actual_string(content, desanitized_old)
        if actual_old is not None:
            desanitized_new = new_string
            for sanitized, original in replacements:
                desanitized_new = desanitized_new.replace(sanitized, original)
            return actual_old, _preserve_quote_style(
                desanitized_old, actual_old, desanitized_new
            )

    # No match — return with just trailing whitespace stripped
    return old_string, new_string

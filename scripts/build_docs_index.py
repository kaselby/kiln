"""Regenerate src/kiln/reference/docs/index.md from sibling docs.

Scans every ``<kiln-reference>/docs/*.md`` file (except ``index.md``
itself), extracts the H1 title and a one-line summary, and writes a
deterministic index to ``index.md``.

Summary extraction (in order):

1. YAML frontmatter ``summary:`` if present.
2. First sentence of the first non-heading paragraph after the H1.

Intended entry points:

- ``python scripts/build_docs_index.py`` — rebuild in place.
- Test ``test_docs_index_unchanged`` — CI drift detector; fails if
  the committed ``index.md`` doesn't match what this script would
  regenerate.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = REPO_ROOT / "src" / "kiln" / "reference" / "docs"


H1_RE = re.compile(r"^#\s+(.+?)\s*$")
HEADING_RE = re.compile(r"^#+\s")
SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+")


def extract_metadata(path: Path) -> tuple[str, str]:
    """Return (title, summary) for a doc file.

    - title: text of the first ``# `` heading. Falls back to the filename
      stem (humanized) if no H1 is present.
    - summary: YAML frontmatter ``summary:`` if present, else the first
      sentence of the first non-heading paragraph after the title.
    """
    text = path.read_text()
    lines = text.splitlines()

    title: str | None = None
    summary: str | None = None

    # Optional YAML frontmatter
    i = 0
    if lines and lines[0].strip() == "---":
        for j in range(1, len(lines)):
            if lines[j].strip() == "---":
                front = "\n".join(lines[1:j])
                m = re.search(r"^summary:\s*(.+)$", front, re.MULTILINE)
                if m:
                    summary = m.group(1).strip().strip('"').strip("'")
                i = j + 1
                break

    # H1 title
    for k in range(i, len(lines)):
        m = H1_RE.match(lines[k])
        if m:
            title = m.group(1).strip()
            i = k + 1
            break

    if title is None:
        # Humanize the stem so the index is still usable without an H1.
        title = path.stem.replace("-", " ").replace("_", " ").title()

    if summary is None:
        # First non-heading paragraph after the title
        paragraph: list[str] = []
        for k in range(i, len(lines)):
            line = lines[k].strip()
            if not line:
                if paragraph:
                    break
                continue
            if HEADING_RE.match(line):
                continue
            paragraph.append(line)
        if paragraph:
            joined = " ".join(paragraph)
            first = SENTENCE_END_RE.split(joined, maxsplit=1)[0].rstrip()
            # Strip trailing period for cleaner one-liners.
            if first.endswith("."):
                first = first[:-1]
            summary = first

    return title, summary or ""


def render_index(docs_dir: Path = DOCS_DIR) -> str:
    """Render the full index.md content for the given docs dir."""
    entries: list[tuple[str, str, str]] = []
    for path in sorted(docs_dir.glob("*.md")):
        if path.name == "index.md":
            continue
        title, summary = extract_metadata(path)
        entries.append((path.name, title, summary))

    lines = [
        "# Kiln Reference Docs",
        "",
        "Auto-generated from sibling docs — run `scripts/build_docs_index.py`",
        "to regenerate. The drift test in `test_prompt.py` enforces that the",
        "committed index matches the rendered output.",
        "",
    ]
    for filename, title, summary in entries:
        if summary:
            lines.append(f"- [`{filename}`](./{filename}) — **{title}.** {summary}")
        else:
            lines.append(f"- [`{filename}`](./{filename}) — **{title}.**")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    check = "--check" in argv

    rendered = render_index()
    target = DOCS_DIR / "index.md"

    if check:
        current = target.read_text() if target.exists() else ""
        if current != rendered:
            print(
                f"{target} is out of date. Run `python scripts/build_docs_index.py` to regenerate.",
                file=sys.stderr,
            )
            return 1
        print(f"{target} is up to date.")
        return 0

    target.write_text(rendered)
    print(f"Wrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

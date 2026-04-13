"""MCP tools for the Kiln agent runtime.

Tool implementations are standalone functions that can be imported and
wrapped by agent extensions. The create_mcp_server() factory assembles
them into an MCP server with session-scoped state.

Agent extensions can:
- Import standalone functions (execute_bash, read_file, etc.) and wrap
  them with custom behavior (e.g. worklog capture)
- Import schema constants (BASH_SCHEMA, READ_SCHEMA, etc.) and extend
  them with additional fields
- Build their own MCP server using create_sdk_mcp_server()
"""

import base64
import json
import logging
import mimetypes
import os
import shutil
import subprocess
import tempfile
import uuid as _uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml
from claude_agent_sdk import create_sdk_mcp_server, tool

from .edit_normalize import normalize_edit_inputs
from .shell import PersistentShell, safe_getcwd


# ---------------------------------------------------------------------------
# Shared state classes
# ---------------------------------------------------------------------------

class FileState:
    """Track which files have been read and their state at read time.

    Populated by: MCP Read (directly) and built-in Read PostToolUse hook.
    Consumed by: MCP Edit and MCP Write for "must read first" and
    "modified since read" validations.
    """

    def __init__(self):
        self._state: dict[str, dict] = {}

    def record_read(self, file_path: str, *, partial: bool = False) -> None:
        """Record that a file was read."""
        normalized = str(Path(file_path).resolve())
        try:
            mtime = os.path.getmtime(normalized)
        except OSError:
            mtime = 0.0
        self._state[normalized] = {"timestamp": mtime, "partial": partial}

    def record_write(self, file_path: str) -> None:
        """Update state after a successful write/edit."""
        normalized = str(Path(file_path).resolve())
        try:
            mtime = os.path.getmtime(normalized)
        except OSError:
            mtime = 0.0
        self._state[normalized] = {"timestamp": mtime, "partial": False}

    def get(self, file_path: str) -> dict | None:
        """Return the state entry for a file, or None if not recorded."""
        normalized = str(Path(file_path).resolve())
        return self._state.get(normalized)

    def check(self, file_path: str) -> tuple[bool, str | None]:
        """Validate that a file can be written/edited.

        Returns (ok, error_message).
        """
        normalized = str(Path(file_path).resolve())

        if not os.path.exists(normalized):
            return True, None

        entry = self._state.get(normalized)
        if not entry:
            return False, "File has not been read yet. Read it first before writing to it."

        try:
            current_mtime = os.path.getmtime(normalized)
        except OSError:
            return True, None

        if current_mtime > entry["timestamp"]:
            return False, (
                "File has been modified since read, either by the user or "
                "by a linter. Read it again before attempting to write it."
            )

        return True, None


class SessionControl:
    """Shared state for session lifecycle signals between MCP tools and the TUI."""

    def __init__(self):
        self.quit_requested = False
        self.skip_summary = False
        self.continue_requested = False
        self.handoff_text: str | None = None
        self.context_tokens: int = 0


# Re-export from types (moved there so BackendConfig can reference it
# without circular imports).
from .types import SupplementalContent  # noqa: F401


# ---------------------------------------------------------------------------
# Result helpers
# ---------------------------------------------------------------------------

def _ok(text: str) -> dict:
    """Return a success MCP tool result."""
    return {"content": [{"type": "text", "text": text}]}


def _error(text: str) -> dict:
    """Return an error MCP tool result."""
    return {"content": [{"type": "text", "text": text}], "isError": True}


# ---------------------------------------------------------------------------
# File type constants
# ---------------------------------------------------------------------------

BINARY_EXTENSIONS = frozenset([
    "exe", "dll", "so", "dylib", "app", "msi", "deb", "rpm", "bin",
    "dat", "db", "sqlite", "sqlite3", "mdb", "idx",
    "zip", "rar", "tar", "gz", "bz2", "7z", "xz", "z", "tgz", "iso",
    "doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt", "ods", "odp",
    "ttf", "otf", "woff", "woff2", "eot",
    "psd", "ai", "eps", "sketch", "fig", "xd", "blend", "obj", "3ds",
    "class", "jar", "war", "pyc", "pyo", "rlib", "swf",
    "mp3", "wav", "flac", "ogg", "aac", "m4a", "wma", "aiff", "opus",
    "mp4", "avi", "mov", "wmv", "flv", "mkv", "webm", "m4v", "mpeg", "mpg",
])

IMAGE_EXTENSIONS = frozenset(["png", "jpg", "jpeg", "gif", "webp"])

IMAGE_MIME_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}

# Image size limits. TARGET is what we aim for after resize; HARD is the
# absolute ceiling (bail rather than attempt processing).
IMAGE_TARGET_BASE64 = 5 * 1024 * 1024   # 5MB base64 (~3.75MB raw) — Claude API limit
IMAGE_HARD_LIMIT     = 20 * 1024 * 1024  # 20MB raw — don't even try
IMAGE_MAX_DIM        = 2000               # max width or height in pixels

_tools_log = logging.getLogger("kiln.tools")

MAX_LINES = 2000
MAX_LINE_LEN = 2000


# ---------------------------------------------------------------------------
# Standalone tool implementations (importable, wrappable)
# ---------------------------------------------------------------------------

async def execute_bash(
    shell: PersistentShell, command: str, timeout_ms: int = 120_000
) -> dict:
    """Execute a bash command and return a formatted MCP result."""
    if not command.strip():
        return _error("Error: no command provided.")

    result = await shell.run(command, timeout_ms=timeout_ms)

    parts = []
    if result["output"].strip():
        parts.append(result["output"].rstrip())

    status = []
    if result["timed_out"]:
        status.append(f"TIMED OUT after {result['elapsed_ms']}ms")
    elif result["exit_code"] != 0:
        status.append(f"Exit code: {result['exit_code']}")
    if result["elapsed_ms"] >= 1000:
        status.append(f"{result['elapsed_ms']}ms")
    shell_label = result.get("label", "local")
    if shell_label != "local":
        status.append(f"cwd: {result['cwd']} [{shell_label}]")
    else:
        status.append(f"cwd: {result['cwd']}")

    footer = f"[{result['timestamp']}] {' | '.join(status)}"
    parts.append(footer)

    return _ok("\n".join(parts))


async def execute_bash_background(shell: PersistentShell, command: str) -> dict:
    """Start a background bash command and return job info."""
    result = await shell.run_background(command)
    return _ok(
        f"Background job started.\n"
        f"Job ID: {result['job_id']}\n"
        f"PID: {result['pid']}\n\n"
        f"Use background_job_id={result['job_id']!r} to check status."
    )


async def check_bash_background(shell: PersistentShell, job_id: str) -> dict:
    """Check status of a background bash job."""
    result = await shell.check_background(job_id)
    status = "running" if result["running"] else "finished"
    parts = [f"[Background job {job_id}] Status: {status}"]
    if result["exit_code"] is not None:
        parts.append(f"Exit code: {result['exit_code']}")
    if result["output"]:
        parts.append(result["output"])
    return _ok("\n".join(parts))


async def cleanup_bash_background(shell: PersistentShell, job_id: str) -> dict:
    """Clean up temp files for a background job."""
    await shell.cleanup_background(job_id)
    return _ok(f"Background job {job_id} cleaned up.")


def read_file(
    file_path: str,
    file_state: FileState,
    offset: int | None = None,
    limit: int | None = None,
    pages: str | None = None,
    supplemental: "SupplementalContent | None" = None,
) -> dict:
    """Read a file and return formatted MCP result.

    Handles text (cat -n), images (ImageContent), notebooks (parsed cells),
    and PDFs (via supplemental content injection).
    """
    if not file_path:
        return _error("No file_path provided.")

    normalized = str(Path(file_path).resolve())

    if not os.path.exists(normalized):
        return _error(f"File does not exist: {file_path}")

    if os.path.isdir(normalized):
        return _error(
            f"{file_path} is a directory, not a file. Use Bash with ls "
            "to list directory contents."
        )

    ext = Path(normalized).suffix.lower().lstrip(".")

    if ext in BINARY_EXTENSIONS:
        return _error(
            f"This tool cannot read binary files. The file appears to be "
            f"a binary .{ext} file. Please use appropriate tools for "
            f"binary file analysis."
        )

    # --- Images: return as MCP ImageContent ---
    if ext in IMAGE_EXTENSIONS:
        return _read_image(normalized, file_path, ext, file_state)

    # --- Notebooks: parse and format cells ---
    if ext == "ipynb":
        return _read_notebook(normalized, file_path, file_state)

    # --- PDFs: stash for supplemental content injection ---
    if ext == "pdf":
        return _read_pdf(normalized, file_path, file_state, supplemental, pages)

    # --- Text files ---
    return _read_text(normalized, file_path, file_state, offset, limit)


def _read_image(normalized: str, file_path: str, ext: str, file_state: FileState) -> dict:
    """Read an image file and return as MCP ImageContent.

    Large images are resized to fit within the MCP transport buffer
    (JSON-RPC messages have a finite size limit). Uses sips (macOS)
    or Pillow for resizing; returns an error if neither is available
    and the image is too large.
    """
    try:
        size = os.path.getsize(normalized)
        if size > IMAGE_HARD_LIMIT:
            return _error(
                f"Image too large ({size / 1024 / 1024:.1f} MB). "
                f"Maximum supported size is {IMAGE_HARD_LIMIT // 1024 // 1024} MB."
            )
        data = Path(normalized).read_bytes()
    except OSError as e:
        return _error(f"Failed to read image: {e}")

    mime = IMAGE_MIME_TYPES.get(ext, "image/png")
    b64 = base64.b64encode(data).decode("ascii")

    if len(b64) <= IMAGE_TARGET_BASE64:
        file_state.record_read(normalized, partial=False)
        return {"content": [{"type": "image", "data": b64, "mimeType": mime}]}

    # Image exceeds target — resize it.
    _tools_log.info("Image %s is %dKB base64, resizing (target %dKB)",
                    file_path, len(b64) // 1024, IMAGE_TARGET_BASE64 // 1024)

    resized = _resize_image(normalized, IMAGE_TARGET_BASE64)
    if resized is None:
        return _error(
            f"Image too large for transport ({len(b64) // 1024}KB base64, "
            f"target {IMAGE_TARGET_BASE64 // 1024}KB). "
            f"Install Pillow or use macOS (sips) for automatic resizing."
        )

    data, mime = resized
    b64 = base64.b64encode(data).decode("ascii")
    file_state.record_read(normalized, partial=False)
    return {"content": [{"type": "image", "data": b64, "mimeType": mime}]}


def _resize_image(path: str, max_base64_bytes: int) -> tuple[bytes, str] | None:
    """Resize an image to fit within a base64 byte budget.

    Returns (raw_bytes, mime_type) or None if no resizer is available.
    Tries sips (macOS built-in) first, then Pillow.
    """
    max_raw = int(max_base64_bytes * 3 / 4)

    if shutil.which("sips"):
        result = _resize_with_sips(path, max_raw)
        if result is not None:
            return result

    try:
        from PIL import Image as _PILImage
        return _resize_with_pillow(path, max_raw, _PILImage)
    except ImportError:
        pass

    return None


def _resize_with_sips(path: str, max_raw_bytes: int) -> tuple[bytes, str] | None:
    """Resize using macOS sips. Progressive strategy: JPEG convert, then shrink."""
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            # Try JPEG conversion at original dimensions first (huge win for PNGs)
            out = os.path.join(tmpdir, "out.jpg")
            subprocess.run(
                ["sips", "-s", "format", "jpeg", "-s", "formatOptions", "85",
                 path, "--out", out],
                capture_output=True, timeout=15,
            )
            if os.path.exists(out) and os.path.getsize(out) <= max_raw_bytes:
                return Path(out).read_bytes(), "image/jpeg"

            # Progressive dimension reduction
            for width in [IMAGE_MAX_DIM, 1500, 1024, 768, 512]:
                out = os.path.join(tmpdir, f"out_{width}.jpg")
                subprocess.run(
                    ["sips", "-s", "format", "jpeg", "-s", "formatOptions", "80",
                     "--resampleWidth", str(width), path, "--out", out],
                    capture_output=True, timeout=15,
                )
                if os.path.exists(out) and os.path.getsize(out) <= max_raw_bytes:
                    return Path(out).read_bytes(), "image/jpeg"

            # Last resort: tiny aggressive JPEG
            out = os.path.join(tmpdir, "out_tiny.jpg")
            subprocess.run(
                ["sips", "-s", "format", "jpeg", "-s", "formatOptions", "50",
                 "--resampleWidth", "400", path, "--out", out],
                capture_output=True, timeout=15,
            )
            if os.path.exists(out):
                return Path(out).read_bytes(), "image/jpeg"
    except Exception as e:
        _tools_log.warning("sips resize failed: %s", e)

    return None


def _resize_with_pillow(path: str, max_raw_bytes: int, Image) -> tuple[bytes, str] | None:
    """Resize using Pillow. Progressive strategy matching sips."""
    import io
    try:
        img = Image.open(path)
        img = img.convert("RGB")  # Drop alpha for JPEG

        # Try at original dimensions with JPEG compression
        for quality in [85, 70, 50]:
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality)
            if buf.tell() <= max_raw_bytes:
                return buf.getvalue(), "image/jpeg"

        # Progressive dimension reduction
        for max_dim in [IMAGE_MAX_DIM, 1500, 1024, 768, 512, 400]:
            resized = img.copy()
            resized.thumbnail((max_dim, max_dim))
            buf = io.BytesIO()
            resized.save(buf, format="JPEG", quality=70)
            if buf.tell() <= max_raw_bytes:
                return buf.getvalue(), "image/jpeg"
    except Exception as e:
        _tools_log.warning("Pillow resize failed: %s", e)

    return None


def _read_notebook(normalized: str, file_path: str, file_state: FileState) -> dict:
    """Read a Jupyter notebook and return formatted cell contents."""
    try:
        raw = Path(normalized).read_text(errors="replace")
    except OSError as e:
        return _error(f"Failed to read notebook: {e}")

    try:
        nb = json.loads(raw)
    except json.JSONDecodeError as e:
        return _error(f"Invalid notebook JSON: {e}")

    cells = nb.get("cells", [])
    if not cells:
        file_state.record_read(normalized, partial=False)
        return _ok("Notebook is empty (no cells).")

    language = nb.get("metadata", {}).get("language_info", {}).get("name", "python")
    content_blocks: list[dict] = []
    text_parts: list[str] = []

    for i, cell in enumerate(cells):
        cell_type = cell.get("cell_type", "raw")
        source = cell.get("source", "")
        if isinstance(source, list):
            source = "".join(source)
        cell_id = cell.get("id", f"cell-{i}")

        # Format cell header and source
        if cell_type == "code":
            lang_tag = f" [{language}]" if language != "python" else ""
            text_parts.append(f'<cell id="{cell_id}" type="code"{lang_tag}>\n{source}\n</cell>')
        elif cell_type == "markdown":
            text_parts.append(f'<cell id="{cell_id}" type="markdown">\n{source}\n</cell>')
        else:
            text_parts.append(f'<cell id="{cell_id}" type="{cell_type}">\n{source}\n</cell>')

        # Process outputs for code cells
        if cell_type == "code" and cell.get("outputs"):
            for output in cell["outputs"]:
                output_type = output.get("output_type", "")

                if output_type == "stream":
                    text = output.get("text", "")
                    if isinstance(text, list):
                        text = "".join(text)
                    if text.strip():
                        text_parts.append(f"<output>{text.rstrip()}</output>")

                elif output_type in ("execute_result", "display_data"):
                    data = output.get("data", {})
                    # Check for embedded images
                    for img_mime in ("image/png", "image/jpeg"):
                        if img_mime in data:
                            img_b64 = data[img_mime]
                            if isinstance(img_b64, str):
                                img_b64 = img_b64.replace("\n", "").replace(" ", "")
                                # Flush accumulated text before image
                                if text_parts:
                                    content_blocks.append({"type": "text", "text": "\n\n".join(text_parts)})
                                    text_parts = []
                                content_blocks.append({"type": "image", "data": img_b64, "mimeType": img_mime})
                            break
                    # Text output
                    plain = data.get("text/plain", "")
                    if isinstance(plain, list):
                        plain = "".join(plain)
                    if plain.strip():
                        text_parts.append(f"<output>{plain.rstrip()}</output>")

                elif output_type == "error":
                    ename = output.get("ename", "")
                    evalue = output.get("evalue", "")
                    tb = output.get("traceback", [])
                    err_text = f"{ename}: {evalue}"
                    if tb:
                        err_text += "\n" + "\n".join(tb)
                    text_parts.append(f"<output type=\"error\">{err_text.rstrip()}</output>")

    # Flush remaining text
    if text_parts:
        content_blocks.append({"type": "text", "text": "\n\n".join(text_parts)})

    file_state.record_read(normalized, partial=False)
    return {"content": content_blocks}


def _parse_page_range(pages: str) -> tuple[int, int | None] | str:
    """Parse a page range string like '1-5', '3', or '10-'.

    Returns (first, last) with 1-based page numbers, or an error string.
    last=None means open-ended (to end of document).
    """
    trimmed = pages.strip()
    if not trimmed:
        return "Empty page range."

    # Open-ended: "3-"
    if trimmed.endswith("-"):
        try:
            first = int(trimmed[:-1])
        except ValueError:
            return f'Invalid page range: "{pages}". Use formats like "1-5", "3", or "10-".'
        if first < 1:
            return "Page numbers are 1-indexed."
        return (first, None)

    # Range: "1-5"
    if "-" in trimmed:
        parts = trimmed.split("-", 1)
        try:
            first, last = int(parts[0]), int(parts[1])
        except ValueError:
            return f'Invalid page range: "{pages}". Use formats like "1-5", "3", or "10-".'
        if first < 1 or last < 1:
            return "Page numbers are 1-indexed."
        if last < first:
            return f"Invalid range: last page ({last}) < first page ({first})."
        return (first, last)

    # Single page: "3"
    try:
        page = int(trimmed)
    except ValueError:
        return f'Invalid page range: "{pages}". Use formats like "1-5", "3", or "10-".'
    if page < 1:
        return "Page numbers are 1-indexed."
    return (page, page)


def _read_pdf(
    normalized: str,
    file_path: str,
    file_state: FileState,
    supplemental: "SupplementalContent | None",
    pages: str | None = None,
) -> dict:
    """Read a PDF — stash content for supplemental injection, return summary."""
    if supplemental is None:
        return _error(
            "PDF reading requires supplemental content support, which is not "
            "available in this session. Use an external tool to extract text."
        )

    try:
        data = Path(normalized).read_bytes()
    except OSError as e:
        return _error(f"Failed to read PDF: {e}")

    if not data or not data[:5].startswith(b"%PDF"):
        return _error(
            f"Not a valid PDF file: {file_path}. "
            "The file is missing the PDF header (%PDF magic bytes)."
        )

    size_mb = len(data) / (1024 * 1024)
    if size_mb > 32:
        return _error(
            f"PDF too large ({size_mb:.1f} MB). Maximum supported size is 32 MB."
        )

    # If pages requested, extract subset using pypdf.
    if pages:
        parsed = _parse_page_range(pages)
        if isinstance(parsed, str):
            return _error(parsed)
        first, last = parsed
        try:
            from pypdf import PdfReader, PdfWriter
        except ImportError:
            return _error(
                "Page-range PDF reading requires pypdf. "
                "Install it: pip install pypdf"
            )
        try:
            import io
            reader = PdfReader(io.BytesIO(data))
            total_pages = len(reader.pages)
            if first > total_pages:
                return _error(
                    f"Page {first} out of range — PDF has {total_pages} pages."
                )
            last_idx = min(last, total_pages) if last else total_pages
            writer = PdfWriter()
            for i in range(first - 1, last_idx):
                writer.add_page(reader.pages[i])
            buf = io.BytesIO()
            writer.write(buf)
            data = buf.getvalue()
            page_count = last_idx - first + 1
            page_desc = f"pages {first}-{last_idx}" if page_count > 1 else f"page {first}"
        except Exception as e:
            return _error(f"Failed to extract pages: {e}")

        file_state.record_read(normalized, partial=True)
    else:
        # Estimate page count from the PDF cross-reference table.
        page_count = data.count(b"/Type /Page") - data.count(b"/Type /Pages")
        page_count = max(page_count, 0) or None
        page_desc = None

        file_state.record_read(normalized, partial=False)

    supplemental.add_file(
        data=data,
        mime_type="application/pdf",
        label=os.path.basename(file_path),
    )

    out_size = len(data) / (1024 * 1024)
    size_str = f"{out_size:.1f} MB" if out_size >= 1 else f"{len(data) / 1024:.0f} KB"
    if page_desc:
        return _ok(
            f"PDF detected: {file_path} ({page_desc}, {size_str}). "
            f"Document content will be provided in the next turn for native reading."
        )
    page_str = f", ~{page_count} pages" if page_count else ""
    return _ok(
        f"PDF detected: {file_path} ({size_str}{page_str}). "
        f"Document content will be provided in the next turn for native reading."
    )


def _read_text(
    normalized: str,
    file_path: str,
    file_state: FileState,
    offset: int | None = None,
    limit: int | None = None,
) -> dict:
    """Read a text file in cat -n format."""
    # Dedup: if the file hasn't changed since last read, return a stub
    entry = file_state.get(normalized)
    if entry is not None:
        try:
            current_mtime = os.path.getmtime(normalized)
        except OSError:
            current_mtime = -1
        if current_mtime == entry["timestamp"]:
            file_state.record_read(normalized, partial=False)
            return _ok(f"File {file_path} has not changed since last read.")

    try:
        raw = Path(normalized).read_text(errors="replace")
    except OSError as e:
        return _error(f"Failed to read file: {e}")

    # Strip control characters invalid in XML
    raw = raw.translate({c: None for c in range(32) if c not in (9, 10, 13)})

    lines = raw.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]

    total_lines = len(lines)

    if total_lines == 0:
        file_state.record_read(normalized, partial=False)
        return _ok(
            "<system-reminder>Warning: the file exists but the contents "
            "are empty.</system-reminder>"
        )

    start = max(1, int(offset)) if offset is not None else 1
    max_lines = int(limit) if limit is not None else MAX_LINES
    partial = offset is not None or limit is not None

    if start > total_lines:
        file_state.record_read(normalized, partial=True)
        return _ok(
            f"<system-reminder>Warning: the file exists but is shorter "
            f"than the provided offset ({start}). The file has "
            f"{total_lines} lines.</system-reminder>"
        )

    selected = lines[start - 1 : start - 1 + max_lines]

    output_lines = []
    for i, line in enumerate(selected, start=start):
        if len(line) > MAX_LINE_LEN:
            line = line[:MAX_LINE_LEN]
        output_lines.append(f"{i:>6}\t{line}")

    file_state.record_read(normalized, partial=partial)
    return _ok("\n".join(output_lines))


def edit_file(
    file_path: str,
    old_string: str,
    new_string: str,
    file_state: FileState,
    replace_all: bool = False,
) -> dict:
    """Perform exact string replacement in a file. Returns MCP result."""
    if not file_path:
        return _error("No file_path provided.")

    normalized = str(Path(file_path).resolve())

    if not os.path.exists(normalized):
        return _error(f"File does not exist: {file_path}")

    ok, err = file_state.check(normalized)
    if not ok:
        return _error(err)

    try:
        content = Path(normalized).read_text()
    except OSError as e:
        return _error(f"Failed to read file: {e}")

    # Normalize inputs: desanitize API tokens, fix curly quotes, strip trailing whitespace
    old_string, new_string = normalize_edit_inputs(content, normalized, old_string, new_string)

    match_string = old_string.rstrip("\n")

    if not match_string and not old_string:
        if not new_string:
            return _error("Original and edited file match exactly. Failed to apply edit.")
        new_content = new_string
    else:
        count = content.count(match_string)

        if count == 0:
            return _error("String not found in file. Failed to apply edit.")

        if count > 1 and not replace_all:
            return _error(
                f"{count} matches of the string to replace, but replace_all is "
                f"false. To replace all occurrences, set replace_all to true. "
                f"To replace only one occurrence, please provide more context "
                f"to uniquely identify the instance."
            )

        if replace_all:
            new_content = content.replace(match_string, new_string)
        else:
            new_content = content.replace(match_string, new_string, 1)

    if new_content == content:
        return _error("Original and edited file match exactly. Failed to apply edit.")

    try:
        _write_file_to_disk(normalized, new_content)
    except OSError as e:
        return _error(f"Failed to write file: {e}")

    file_state.record_write(normalized)
    return _ok(f"The file {file_path} has been updated successfully.")


def write_file(file_path: str, content: str, file_state: FileState) -> dict:
    """Write content to a file. Returns MCP result."""
    if not file_path:
        return _error("No file_path provided.")

    normalized = str(Path(file_path).resolve())
    is_new = not os.path.exists(normalized)

    if not is_new:
        ok, err = file_state.check(normalized)
        if not ok:
            return _error(err)

    parent = Path(normalized).parent
    parent.mkdir(parents=True, exist_ok=True)

    try:
        _write_file_to_disk(normalized, content)
    except OSError as e:
        return _error(f"Failed to write file: {e}")

    file_state.record_write(normalized)

    if is_new:
        return _ok(f"File created successfully at: {file_path}")
    return _ok(f"The file {file_path} has been updated successfully.")


def do_activate_skill(name: str, skills_path: Path) -> dict:
    """Activate a skill by name. Returns MCP result."""
    skill_md = skills_path / name / "SKILL.md"

    if not skill_md.exists():
        return _error(f"Error: skill '{name}' not found.")

    content = skill_md.read_text()

    if content.startswith("---"):
        try:
            end = content.index("---", 3)
            content = content[end + 3:].strip()
        except ValueError:
            pass

    return _ok(content)


def do_exit_session(
    session_control: SessionControl,
    skip_summary: bool = False,
    continue_: bool = False,
    handoff: str = "",
) -> dict:
    """Request session exit. Returns MCP result."""
    if session_control is None:
        return _error("No session control available.")

    session_control.quit_requested = True
    if skip_summary:
        session_control.skip_summary = True
    if continue_:
        session_control.continue_requested = True
    if handoff.strip():
        session_control.handoff_text = handoff.strip()

    return _ok(
        "Session exit requested. Stop making tool calls — "
        "the session will end after this turn completes."
    )


def do_update_plan(
    plans_path: Path, agent_id: str, goal: str, tasks: list[dict]
) -> dict:
    """Create or update an agent's plan. Returns MCP result."""
    VALID_STATUSES = {"pending", "in_progress", "done"}

    if not goal:
        return _error("A goal is required.")
    if not tasks:
        return _error("At least one task is required.")

    for t in tasks:
        if t.get("status") not in VALID_STATUSES:
            return _error(
                f"Invalid status '{t.get('status')}' for task "
                f"'{t.get('description', '?')}'. "
                f"Use: pending, in_progress, or done."
            )

    plan_data = {
        "goal": goal,
        "updated": datetime.now(timezone.utc).isoformat(),
        "agent": agent_id,
        "tasks": [
            {"description": t["description"], "status": t["status"]}
            for t in tasks
        ],
    }

    plan_file = plans_path / f"{agent_id}.yml"
    plan_file.parent.mkdir(parents=True, exist_ok=True)
    plan_file.write_text(yaml.dump(plan_data, default_flow_style=False, sort_keys=False))

    return _ok(f"Plan updated.\n\n{format_plan(plan_data)}")


# --- Messaging helpers (importable) ---

_NAMESPACE_REGISTRY_PATH = Path.home() / ".kiln" / "agents.yml"


def _load_namespace_registry() -> dict[str, Path]:
    """Load the namespace → home directory registry from ~/.kiln/agents.yml.

    File format:
        myagent: ~/.myagent
        other: ~/.other

    Returns an empty dict if the file doesn't exist or can't be parsed.
    """
    if not _NAMESPACE_REGISTRY_PATH.exists():
        return {}
    try:
        raw = yaml.safe_load(_NAMESPACE_REGISTRY_PATH.read_text()) or {}
        return {k: Path(os.path.expanduser(str(v))) for k, v in raw.items()}
    except Exception:
        return {}


def _resolve_recipient_inbox(recipient: str, fallback: Path) -> Path:
    """Infer the recipient's inbox path from their agent ID.

    Resolution order:
      1. ~/.kiln/agents.yml registry (explicit namespace → home mapping)
      2. ~/.{prefix}/inbox/ convention (implicit, if the directory exists)
      3. fallback (sender's inbox root)

    Agent IDs follow the pattern <prefix>-<adj>-<noun> (e.g. kiln-cold-grove).
    """
    prefix = recipient.split("-")[0]

    registry = _load_namespace_registry()
    if prefix in registry:
        return registry[prefix] / "inbox"

    candidate_inbox = Path.home() / f".{prefix}" / "inbox"
    if candidate_inbox.is_dir():
        return candidate_inbox

    return fallback


def send_to_inbox(
    inbox_root: Path,
    recipient: str,
    sender: str,
    summary: str,
    body: str,
    priority: str = "normal",
    channel: str | None = None,
) -> Path:
    """Send a single message to a recipient's inbox. Returns the message path."""
    recipient_inbox = inbox_root / recipient
    recipient_inbox.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    msg_id = f"msg-{timestamp}-{_uuid.uuid4().hex[:6]}"
    msg_path = recipient_inbox / f"{msg_id}.md"

    channel_line = f"channel: {channel}\n" if channel else ""
    content = (
        f"---\n"
        f"from: {sender}\n"
        f"summary: \"{summary}\"\n"
        f"priority: {priority}\n"
        f"{channel_line}"
        f"timestamp: {datetime.now(timezone.utc).isoformat()}\n"
        f"---\n\n"
        f"{body}\n"
    )

    msg_path.write_text(content)
    return msg_path


def do_send_message(
    inbox_root: Path,
    agent_id: str,
    summary: str,
    body: str,
    priority: str = "normal",
    to: str | None = None,
    channel: str | None = None,
    channels_path: Path | None = None,
    channels_dir: Path | None = None,
) -> dict:
    """Send a point-to-point or channel broadcast message.

    Standalone function — importable and callable from custom MCP servers
    without duplicating message/channel logic.

    Args:
        inbox_root: Root inbox directory (e.g. <agent_home>/inbox).
        agent_id: Sender's agent ID.
        summary: Brief message summary.
        body: Full message body.
        priority: Message priority (low, normal, high).
        to: Recipient agent ID (for point-to-point).
        channel: Channel name (for broadcast).
        channels_path: Path to channels.json (defaults to inbox_root/../channels.json).
        channels_dir: Path to channels/ directory (defaults to inbox_root/../channels).

    Returns:
        dict with "result" key on success or "error" key on failure.
    """
    if not summary and not body:
        return {"error": "send requires at least a summary or body."}
    if not to and not channel:
        return {"error": "send requires either 'to' (agent ID) or 'channel'."}

    if channels_path is None:
        channels_path = inbox_root.parent / "channels.json"
    if channels_dir is None:
        channels_dir = inbox_root.parent / "channels"

    if to:
        recipient_inbox_root = _resolve_recipient_inbox(to, inbox_root)
        msg_path = send_to_inbox(recipient_inbox_root, to, agent_id, summary, body, priority)
        return {"result": f"Message sent to {to} at {msg_path}"}

    # Channel broadcast
    if not channels_path.exists():
        return {"error": f"Channel '{channel}' has no other subscribers."}
    try:
        ch_data = json.loads(channels_path.read_text())
    except (json.JSONDecodeError, OSError):
        ch_data = {}
    subs = ch_data.get(channel, [])
    recipients = [s for s in subs if s != agent_id]
    if not recipients:
        return {"error": f"Channel '{channel}' has no other subscribers."}
    for recipient in recipients:
        recipient_inbox_root = _resolve_recipient_inbox(recipient, inbox_root)
        send_to_inbox(
            recipient_inbox_root, recipient, agent_id, summary, body,
            priority, channel=channel,
        )
    # Persist to channel history
    history_dir = channels_dir / channel
    history_dir.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "from": agent_id,
        "summary": summary,
        "body": body,
        "priority": priority,
    }
    with open(history_dir / "history.jsonl", "a") as f:
        f.write(json.dumps(entry) + "\n")
    return {
        "result": (
            f"Message broadcast to channel '{channel}' "
            f"({len(recipients)} recipient(s))."
        )
    }


def format_plan(data: dict) -> str:
    """Format a plan dict as readable text."""
    lines = [f"Goal: {data.get('goal', '(none)')}"]
    tasks = data.get("tasks", [])
    for t in tasks:
        status = t.get("status", "pending")
        desc = t.get("description", "")
        lines.append(f"  [{status}] {desc}")
    done = sum(1 for t in tasks if t.get("status") == "done")
    lines.append(f"Progress: {done}/{len(tasks)} done.")
    return "\n".join(lines)


def _write_file_to_disk(path: str, content: str) -> None:
    """Write content to a file, preserving permissions on existing files."""
    p = Path(path)
    existing_mode = None
    if p.exists():
        existing_mode = p.stat().st_mode
    p.write_text(content)
    if existing_mode is not None:
        os.chmod(path, existing_mode)


# ---------------------------------------------------------------------------
# Tool schemas (importable — agent extensions can modify/extend these)
# ---------------------------------------------------------------------------

BASH_DESC = (
    "Executes a bash command in a persistent shell. Environment variables, "
    "working directory, and other state persist between calls."
)
BASH_SCHEMA = {
    "type": "object",
    "properties": {
        "command": {
            "type": "string",
            "description": "The command to execute",
        },
        "description": {
            "type": "string",
            "description": "Brief description of what the command does",
        },
        "timeout": {
            "type": "integer",
            "description": "Timeout in milliseconds (default 120000)",
        },
        "run_in_background": {
            "type": "boolean",
            "description": "Start the command in the background and return immediately. Returns a job_id to check later.",
        },
        "background_job_id": {
            "type": "string",
            "description": "Check status of a background job by its job_id (returned from run_in_background).",
        },
        "cleanup_background_job_id": {
            "type": "string",
            "description": "Clean up temp files for a finished background job by its job_id.",
        },
    },
}

READ_DESC = (
    "Reads a file from the local filesystem. Use this tool by default for "
    "reading all text files.\n\n"
    "Usage:\n"
    "- The file_path parameter must be an absolute path, not a relative path\n"
    "- By default, it reads up to 2000 lines starting from the beginning "
    "of the file\n"
    "- You can optionally specify a line offset and limit (especially handy "
    "for long files), but it's recommended to read the whole file by not "
    "providing these parameters\n"
    "- Any lines longer than 2000 characters will be truncated\n"
    "- Results are returned using cat -n format, with line numbers starting at 1\n"
    "- This tool can read images (PNG, JPG, GIF, WebP). Image content is "
    "presented visually.\n"
    "- This tool can read Jupyter notebooks (.ipynb) and returns all cells "
    "with their outputs, combining code, text, and visualizations.\n"
    "- This tool can read PDF files (.pdf). PDF content is injected as a "
    "native document for full reading. Use the `pages` parameter to read "
    "specific pages (e.g., '1-3', '5', '10-') to save context on large PDFs.\n"
    "- Re-reading an unchanged file returns a short stub instead of the "
    "full contents."
)
READ_SCHEMA = {
    "type": "object",
    "properties": {
        "file_path": {
            "type": "string",
            "description": "The absolute path to the file to read",
        },
        "offset": {
            "type": "number",
            "description": (
                "The line number to start reading from. "
                "Only provide if the file is too large to read at once"
            ),
        },
        "limit": {
            "type": "number",
            "description": (
                "The number of lines to read. "
                "Only provide if the file is too large to read at once."
            ),
        },
        "pages": {
            "type": "string",
            "description": (
                'Page range for PDF files (e.g., "1-5", "3", "10-"). '
                "Only applicable to PDF files."
            ),
        },
    },
    "required": ["file_path"],
}

EDIT_DESC = (
    "Performs exact string replacements in files.\n\n"
    "Usage:\n"
    "- You must use your `Read` tool at least once in the conversation "
    "before editing. This tool will error if you attempt an edit without "
    "reading the file. \n"
    "- When editing text from Read tool output, ensure you preserve the "
    "exact indentation (tabs/spaces) as it appears AFTER the line number "
    "prefix. The line number prefix format is: spaces + line number + tab. "
    "Everything after that tab is the actual file content to match. Never "
    "include any part of the line number prefix in the old_string or "
    "new_string.\n"
    "- ALWAYS prefer editing existing files in the codebase. NEVER write "
    "new files unless explicitly required.\n"
    "- Only use emojis if the user explicitly requests it. Avoid adding "
    "emojis to files unless asked.\n"
    "- The edit will FAIL if `old_string` is not unique in the file. "
    "Either provide a larger string with more surrounding context to make "
    "it unique or use `replace_all` to change every instance of "
    "`old_string`.\n"
    "- Use `replace_all` for replacing and renaming strings across the "
    "file. This parameter is useful if you want to rename a variable for "
    "instance."
)
EDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "file_path": {
            "type": "string",
            "description": "The absolute path to the file to modify",
        },
        "old_string": {
            "type": "string",
            "description": "The text to replace",
        },
        "new_string": {
            "type": "string",
            "description": "The text to replace it with (must be different from old_string)",
        },
        "replace_all": {
            "type": "boolean",
            "description": "Replace all occurrences of old_string (default false)",
            "default": False,
        },
    },
    "required": ["file_path", "old_string", "new_string"],
}

WRITE_DESC = (
    "Write a file to the local filesystem. Overwrites the file if it "
    "already exists.\n\n"
    "Usage:\n"
    "- If the file already exists, you must Read it first. The tool will "
    "fail if you haven't.\n"
    "- Prefer editing existing files over creating new ones.\n"
    "- NEVER create documentation files (*.md) or README files unless "
    "explicitly requested by the User.\n"
    "- Only use emojis if the user explicitly requests it. Avoid writing "
    "emojis to files unless asked."
)
WRITE_SCHEMA = {
    "type": "object",
    "properties": {
        "file_path": {
            "type": "string",
            "description": "The absolute path to the file to write (must be absolute, not relative)",
        },
        "content": {
            "type": "string",
            "description": "The content to write to the file",
        },
    },
    "required": ["file_path", "content"],
}

ACTIVATE_SKILL_DESC = (
    "Activate a skill by name. Loads the skill's instructions as system-level "
    "context for the remainder of the session. Use this when your task calls for "
    "a specific skill listed in your session context."
)
ACTIVATE_SKILL_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
        },
    },
    "required": ["name"],
}

MESSAGE_DESC = (
    "Send messages to agents and manage channel subscriptions.\n\n"
    "Actions:\n"
    "- **send**: Send a message to an agent (via `to`) or broadcast to a "
    "channel (via `channel`). Requires `summary` and `body`.\n"
    "- **subscribe**: Subscribe to a channel to receive all messages sent to it.\n"
    "- **unsubscribe**: Unsubscribe from a channel."
)
MESSAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "description": "The action: send, subscribe, or unsubscribe",
            "enum": ["send", "subscribe", "unsubscribe"],
        },
        "to": {
            "type": "string",
            "description": "Recipient agent ID (for action=send, point-to-point)",
        },
        "channel": {
            "type": "string",
            "description": "Channel name (for subscribe/unsubscribe, or for action=send to broadcast)",
        },
        "summary": {
            "type": "string",
            "description": "Brief summary shown in notifications (for action=send)",
        },
        "body": {
            "type": "string",
            "description": "Full message body (for action=send)",
        },
        "priority": {
            "type": "string",
            "description": "Message priority: low, normal, or high (for action=send, default normal)",
            "enum": ["low", "normal", "high"],
        },
    },
    "required": ["action"],
}

EXIT_SESSION_DESC = (
    "Exit the current session cleanly. The harness will handle session "
    "summaries and memory commits before shutdown.\n\n"
    "Appropriate uses:\n"
    "- Ephemeral agents that have completed their task\n"
    "- Autonomous agents handing off to a continuation\n\n"
    "Set `continue` to true for self-continuation: the harness will run "
    "the normal shutdown (summary, volatile update, commit), then "
    "automatically launch a fresh session. If the current session is "
    "canonical, the new session inherits canonical status.\n\n"
    "Use `handoff` to pass context to the continuation session. The text "
    "will be delivered as an inbox message in the new session — no need "
    "to write handoff.md manually.\n\n"
    "Do NOT use in interactive sessions — let the user decide when to end "
    "the conversation. If you're unsure whether you're running autonomously, "
    "you're not."
)
EXIT_SESSION_SCHEMA = {
    "type": "object",
    "properties": {
        "skip_summary": {
            "type": "boolean",
            "description": (
                "Skip the session summary and memory update protocol. "
                "Rarely needed — most sessions benefit from the cleanup step."
            ),
            "default": False,
        },
        "continue": {
            "type": "boolean",
            "description": (
                "Self-continuation: after clean shutdown, automatically "
                "launch a new session that picks up from the handoff. "
                "The new session inherits canonical status, runs in "
                "yolo mode, and inherits heartbeat settings."
            ),
            "default": False,
        },
        "handoff": {
            "type": "string",
            "description": (
                "Handoff text for the continuation session. Describes "
                "what's currently in flight and what the next session "
                "should pick up. Delivered as an inbox message to the "
                "new session. Only used with continue=true."
            ),
        },
    },
}

PLAN_DESC = (
    "Create or update your working plan. Use this to externalize your task "
    "breakdown before starting complex work. Each call replaces the entire "
    "plan — include all tasks, not just changes.\n\n"
    "Call this tool to:\n"
    "- Break down a complex task before starting\n"
    "- Mark tasks as done as you complete them\n"
    "- Adjust the plan when requirements change\n\n"
    "Your plan is stored on the filesystem and visible to coordinators "
    "and other agents."
)
PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "goal": {
            "type": "string",
            "description": "Brief description of what you're working on",
        },
        "tasks": {
            "type": "array",
            "description": "Ordered list of tasks",
            "items": {
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "What this task involves",
                    },
                    "status": {
                        "type": "string",
                        "description": "pending, in_progress, or done",
                        "enum": ["pending", "in_progress", "done"],
                    },
                },
                "required": ["description", "status"],
            },
        },
    },
    "required": ["goal", "tasks"],
}


# ---------------------------------------------------------------------------
# MCP server factory (assembles tools with session-scoped state)
# ---------------------------------------------------------------------------

def create_mcp_server(
    inbox_root: Path,
    skills_path: Path,
    agent_id: str,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    file_state: FileState | None = None,
    session_control: SessionControl | None = None,
    plans_path: Path | None = None,
    supplemental: SupplementalContent | None = None,
    daemon_client=None,
):
    """Create the Kiln MCP server with standard agent runtime tools.

    Returns (server, cleanup_coro_fn, get_shell_cwd).

    Tools are thin wrappers around the standalone functions above,
    binding session-scoped state (shell, file_state, etc.). Agent
    extensions should import the standalone functions directly rather
    than wrapping this factory.
    """
    if file_state is None:
        file_state = FileState()

    shell: PersistentShell | None = None

    async def cleanup():
        nonlocal shell
        if shell is not None:
            await shell.close()
            shell = None

    def get_shell_cwd() -> str:
        if shell is not None:
            return shell.cwd
        return cwd or safe_getcwd()

    # Resolve plans_path default
    _plans_path = plans_path or inbox_root.parent / "plans"

    # --- Tool implementations (thin wrappers) ---

    @tool("Bash", BASH_DESC, BASH_SCHEMA)
    async def bash_tool(args: dict) -> dict:
        nonlocal shell
        if shell is None:
            shell = PersistentShell(cwd=cwd, env=env)

        cleanup_job_id = args.get("cleanup_background_job_id")
        if cleanup_job_id:
            return await cleanup_bash_background(shell, cleanup_job_id)

        bg_job_id = args.get("background_job_id")
        if bg_job_id:
            return await check_bash_background(shell, bg_job_id)

        command = args.get("command", "")

        if args.get("run_in_background"):
            return await execute_bash_background(shell, command)

        return await execute_bash(
            shell, command, timeout_ms=args.get("timeout", 120_000)
        )

    @tool("Read", READ_DESC, READ_SCHEMA)
    async def read_tool(args: dict) -> dict:
        return read_file(
            args.get("file_path", ""),
            file_state,
            offset=args.get("offset"),
            limit=args.get("limit"),
            pages=args.get("pages"),
            supplemental=supplemental,
        )

    @tool("Edit", EDIT_DESC, EDIT_SCHEMA)
    async def edit_tool(args: dict) -> dict:
        return edit_file(
            args.get("file_path", ""),
            args.get("old_string", ""),
            args.get("new_string", ""),
            file_state,
            replace_all=args.get("replace_all", False),
        )

    @tool("Write", WRITE_DESC, WRITE_SCHEMA)
    async def write_tool(args: dict) -> dict:
        return write_file(
            args.get("file_path", ""),
            args.get("content", ""),
            file_state,
        )

    @tool("activate_skill", ACTIVATE_SKILL_DESC, ACTIVATE_SKILL_SCHEMA)
    async def activate_skill_tool(args: dict) -> dict:
        return do_activate_skill(args["name"], skills_path)

    @tool("message", MESSAGE_DESC, MESSAGE_SCHEMA)
    async def message_tool(args: dict) -> dict:
        action = args.get("action")

        if action == "subscribe":
            channel = args.get("channel")
            if not channel:
                return _error("subscribe requires a channel name.")
            if not daemon_client:
                return _error("Channel operations require the Kiln daemon.")
            count = await daemon_client.subscribe(channel)
            return _ok(f"Subscribed to channel '{channel}'. {count} subscriber(s).")

        elif action == "unsubscribe":
            channel = args.get("channel")
            if not channel:
                return _error("unsubscribe requires a channel name.")
            if not daemon_client:
                return _error("Channel operations require the Kiln daemon.")
            await daemon_client.unsubscribe(channel)
            return _ok(f"Unsubscribed from channel '{channel}'.")

        elif action == "send":
            to = args.get("to")
            channel = args.get("channel")
            summary = args.get("summary", "")
            body = args.get("body", "")
            priority = args.get("priority", "normal")

            if not summary and not body:
                return _error("send requires at least a summary or body.")

            if channel:
                # Channel broadcast — daemon required
                if not daemon_client:
                    return _error("Channel broadcast requires the Kiln daemon.")
                count = await daemon_client.publish(channel, summary, body, priority)
                return _ok(
                    f"Message broadcast to channel '{channel}' "
                    f"({count} recipient(s))."
                )

            elif to:
                # Agent DM — daemon-first, filesystem fallback
                if daemon_client:
                    msg = await daemon_client.send_direct(to, summary, body, priority)
                    return _ok(msg)
                else:
                    result = do_send_message(
                        inbox_root, agent_id,
                        summary=summary, body=body, priority=priority, to=to,
                    )
                    if "error" in result:
                        return _error(result["error"])
                    return _ok(result["result"])

            else:
                return _error("send requires either 'to' (agent ID) or 'channel'.")

        else:
            return _error(f"Unknown action: {action}. Use send, subscribe, or unsubscribe.")

    @tool("exit_session", EXIT_SESSION_DESC, EXIT_SESSION_SCHEMA)
    async def exit_session_tool(args: dict) -> dict:
        return do_exit_session(
            session_control,
            skip_summary=args.get("skip_summary", False),
            continue_=args.get("continue", False),
            handoff=args.get("handoff", ""),
        )

    @tool("plan", PLAN_DESC, PLAN_SCHEMA)
    async def plan_tool(args: dict) -> dict:
        return do_update_plan(
            _plans_path, agent_id,
            args.get("goal", ""),
            args.get("tasks", []),
        )

    mcp_tools = [bash_tool, read_tool, edit_tool, write_tool, activate_skill_tool,
                  message_tool, exit_session_tool, plan_tool]

    server = create_sdk_mcp_server(
        name="kiln",
        version="0.2.0",
        tools=mcp_tools,
    )
    return server, cleanup, get_shell_cwd, mcp_tools

"""Kiln TUI — scrollback-mode terminal interface.

Uses prompt_toolkit's Application (full_screen=False) for persistent keybinding
handling: Escape to interrupt, Ctrl+C to quit, Enter to submit.  Styled output
goes to the terminal's normal scrollback buffer via print_formatted_text so
native text selection and scrolling work naturally.

Responses are not streamed live — tokens accumulate silently and the full
markdown-rendered response prints to scrollback when the turn completes (or
when a tool call begins).  Multi-agent composition is handled by tmux, not
the TUI — this is a single-agent interface.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml
from markdown_it import MarkdownIt

from prompt_toolkit import Application
from prompt_toolkit.buffer import Buffer
# Register Shift+Enter (CSI u: \x1b[13;2u) for terminals that support it.
# Map to an unused function key so we can bind it alongside Alt+Enter.
from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
from prompt_toolkit.keys import Keys
ANSI_SEQUENCES["\x1b[13;2u"] = Keys.F20       # kitty/CSI u protocol
ANSI_SEQUENCES["\x1b[27;2;13~"] = Keys.F20    # xterm modifyOtherKeys format
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import HTML, FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.containers import ConditionalContainer, HSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension as D
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.shortcuts import print_formatted_text
from prompt_toolkit.styles import Style

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk.types import StreamEvent

from ..hooks import parse_message
from ..permissions import (
    PermissionMode,
    PermissionRequest,
    needs_permission,
)

# Max lines of tool result output to show inline
TOOL_RESULT_MAX_LINES = 10


# Semantic style map for the TUI
TUI_STYLE = Style.from_dict({
    "user": "ansicyan bold",
    "assistant": "ansigreen bold",
    "tool": "ansiyellow bold",
    "dim": "#888888",
    "dim-i": "#888888 italic",
    "err": "ansired",
    "err-b": "ansired bold",
    "text": "#cccccc",
    "text-heading": "#e0e0e0 bold",
    "md-code": "#88c0d0",
    "diff-add": "ansigreen",
    "diff-rm": "ansired",
    "diff-hunk": "ansicyan",
    "agent-msg": "ansimagenta",
    "agent-msg-b": "ansimagenta bold",
    "perm-prompt": "ansiyellow bold",
    "perm-key": "ansicyan bold",
    "mode-safe": "ansired bold",
    "mode-default": "ansiyellow bold",
    "mode-yolo": "ansigreen bold",
    "danger": "ansired bold",
})


# ANSI/VT100 escape sequences (e.g. color codes from grep --color).
# \x1b (ESC, 0x1B) is invalid in XML 1.0 and crashes prompt_toolkit's
# HTML/expat parser with "not well-formed (invalid token)".
_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def _tprint(html_str: str, *args, **kwargs) -> None:
    """Print styled text to scrollback above the Application layout.

    Uses prompt_toolkit's print_formatted_text which handles its own
    run_in_terminal coordination. HTML.format() auto-escapes arguments.
    """
    if args or kwargs:
        # Strip ANSI escape sequences from string args before HTML.format()
        # inserts them into the XML template — expat rejects \x1b as invalid.
        args = tuple(_ANSI_ESCAPE_RE.sub("", a) if isinstance(a, str) else a for a in args)
        kwargs = {k: _ANSI_ESCAPE_RE.sub("", v) if isinstance(v, str) else v for k, v in kwargs.items()}
    html = HTML(html_str)
    if args or kwargs:
        html = html.format(*args, **kwargs)
    print_formatted_text(html, style=TUI_STYLE)


# ---- Markdown rendering ----
#
# Uses markdown-it-py to parse complete text into tokens, then converts
# to prompt_toolkit FormattedText. Runs at commit time.

_md = MarkdownIt("commonmark").enable("table")

_StyleTuples = list[tuple[str, str]]


def _markdown_to_ft(text: str) -> FormattedText:
    """Convert markdown text to FormattedText via markdown-it-py."""
    tokens = _md.parse(text)
    result: _StyleTuples = []
    _render_block_tokens(tokens, result)
    # Trim trailing newlines
    while result and result[-1][1] == "\n":
        result.pop()
    return FormattedText(result)


def _render_block_tokens(tokens: list, result: _StyleTuples) -> None:
    """Walk the flat block-level token list and render into styled tuples."""
    i = 0
    style_ctx: list[str] = []  # block-level styles (e.g. heading -> bold)
    list_stack: list[tuple[str, int]] = []  # ("bullet"|"ordered", counter)

    while i < len(tokens):
        tok = tokens[i]

        # --- Headings ---
        if tok.type == "heading_open":
            style_ctx.append("class:text-heading")
        elif tok.type == "heading_close":
            style_ctx.pop()
            result.append(("", "\n"))

        # --- Paragraphs ---
        elif tok.type == "paragraph_open":
            pass
        elif tok.type == "paragraph_close":
            if not tok.hidden:
                result.append(("", "\n"))

        # --- Inline content ---
        elif tok.type == "inline":
            _render_inline(tok.children or [], result, list(style_ctx))

        # --- Fenced code blocks ---
        elif tok.type == "fence":
            lang = tok.info.strip()
            if lang:
                result.append(("class:dim-i", f"  {lang}\n"))
            for line in tok.content.rstrip("\n").split("\n"):
                result.append(("class:md-code", f"  {line}\n"))

        # --- Indented code blocks ---
        elif tok.type == "code_block":
            for line in tok.content.rstrip("\n").split("\n"):
                result.append(("class:md-code", f"  {line}\n"))

        # --- Lists ---
        elif tok.type == "bullet_list_open":
            list_stack.append(("bullet", 0))
        elif tok.type == "bullet_list_close":
            if list_stack:
                list_stack.pop()
        elif tok.type == "ordered_list_open":
            list_stack.append(("ordered", 0))
        elif tok.type == "ordered_list_close":
            if list_stack:
                list_stack.pop()
        elif tok.type == "list_item_open":
            if list_stack:
                kind, count = list_stack[-1]
                count += 1
                list_stack[-1] = (kind, count)
                indent = "  " * len(list_stack)
                if kind == "bullet":
                    result.append(("class:text", f"{indent}\u2022 "))
                else:
                    result.append(("class:text", f"{indent}{count}. "))
        elif tok.type == "list_item_close":
            result.append(("", "\n"))

        # --- Blockquotes ---
        elif tok.type == "blockquote_open":
            style_ctx.append("class:dim")
        elif tok.type == "blockquote_close":
            if "class:dim" in style_ctx:
                style_ctx.remove("class:dim")

        # --- Horizontal rules ---
        elif tok.type == "hr":
            result.append(("class:dim", "\u2500" * 40 + "\n"))

        # --- Tables ---
        elif tok.type == "table_open":
            table_tokens = []
            i += 1
            while i < len(tokens) and tokens[i].type != "table_close":
                table_tokens.append(tokens[i])
                i += 1
            _render_table(table_tokens, result)

        # --- HTML blocks (show raw) ---
        elif tok.type == "html_block":
            result.append(("class:dim", tok.content))

        i += 1


def _render_inline(
    children: list, result: _StyleTuples, style_stack: list[str]
) -> None:
    """Render inline token children with a style stack for nesting."""
    for tok in children:
        if tok.type == "text":
            parts = list(style_stack) if style_stack else []
            # Ensure text color unless an explicit class is already set
            if not any(p.startswith("class:") for p in parts):
                parts.insert(0, "class:text")
            result.append((" ".join(parts), tok.content))
        elif tok.type == "strong_open":
            style_stack.append("bold")
        elif tok.type == "strong_close":
            if "bold" in style_stack:
                style_stack.remove("bold")
        elif tok.type == "em_open":
            style_stack.append("italic")
        elif tok.type == "em_close":
            if "italic" in style_stack:
                style_stack.remove("italic")
        elif tok.type == "code_inline":
            result.append(("class:md-code", tok.content))
        elif tok.type in ("softbreak", "hardbreak"):
            result.append(("", "\n"))
        elif tok.type == "link_open":
            pass  # text shows via child text tokens
        elif tok.type == "link_close":
            pass
        elif tok.type == "image":
            result.append(("class:dim", f"[image: {tok.content}]"))


def _render_table(tokens: list, result: _StyleTuples) -> None:
    """Render table tokens with aligned columns."""
    rows: list[tuple[bool, list[str]]] = []  # (is_header, cells)
    current_row: list[str] = []
    in_header = False

    for tok in tokens:
        if tok.type == "thead_open":
            in_header = True
        elif tok.type == "thead_close":
            in_header = False
        elif tok.type == "tr_open":
            current_row = []
        elif tok.type == "tr_close":
            rows.append((in_header, current_row))
        elif tok.type == "inline":
            current_row.append(_inline_to_plain(tok.children or []))

    if not rows:
        return

    num_cols = max(len(cells) for _, cells in rows)
    col_widths = [0] * num_cols
    for _, cells in rows:
        for j, cell in enumerate(cells):
            col_widths[j] = max(col_widths[j], len(cell))

    for is_hdr, cells in rows:
        padded = [
            (cells[j] if j < len(cells) else "").ljust(col_widths[j])
            for j in range(num_cols)
        ]
        line = "  " + " \u2502 ".join(padded) + "\n"
        if is_hdr:
            result.append(("class:text-heading", line))
            sep = "  " + "\u2500\u253c\u2500".join(
                "\u2500" * w for w in col_widths
            ) + "\n"
            result.append(("class:dim", sep))
        else:
            result.append(("class:text", line))


def _inline_to_plain(children: list) -> str:
    """Extract plain text from inline children (for table cell measurement)."""
    parts = []
    for tok in children:
        if tok.type == "text":
            parts.append(tok.content)
        elif tok.type == "code_inline":
            parts.append(tok.content)
        elif tok.type == "softbreak":
            parts.append(" ")
    return "".join(parts)


def _fmt_tokens(n: int) -> str:
    """Format token count: 1234 -> '1.2k', 12345 -> '12k'."""
    if n < 1000:
        return str(n)
    if n < 10000:
        return f"{n / 1000:.1f}k"
    return f"{n // 1000}k"


def _base_tool_name(name: str) -> str:
    """Strip MCP server prefix: mcp__kiln__Bash -> Bash."""
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 3:
            return parts[2]
    return name


def _display_name(name: str) -> str:
    """Convert internal tool name to a human-friendly display name.

    mcp__kiln__Bash   -> Kiln::Bash
    mcp__aleph__Bash  -> Aleph::Bash
    Read              -> Base::Read
    """
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 3:
            server = parts[1].capitalize()
            return f"{server}::{parts[2]}"
    return f"Base::{name}"


def _format_tool_input(name: str, input: dict) -> str:
    """Format tool input for display, tailored per tool type."""
    base = _base_tool_name(name)
    match base:
        case "Bash":
            cmd = input.get("command", "")
            desc = input.get("description", "")
            # Redact private journal writes from display.
            if "private append" in cmd:
                return "$ private append [private content]"
            lines = cmd.split("\n")
            if len(lines) > 3:
                cmd_display = "\n".join(lines[:3]) + f"\n... ({len(lines) - 3} more lines)"
            else:
                cmd_display = cmd
            if desc:
                return f"{desc}\n$ {cmd_display}"
            return f"$ {cmd_display}"
        case "Read":
            path = input.get("file_path", "")
            parts = [path]
            if "offset" in input:
                parts.append(f"from line {input['offset']}")
            if "limit" in input:
                parts.append(f"({input['limit']} lines)")
            return " ".join(parts)
        case "Write":
            return input.get("file_path", "")
        case "Edit":
            path = input.get("file_path", "")
            old = input.get("old_string", "")
            if old:
                preview = old[:80].replace("\n", "\\n")
                if len(old) > 80:
                    preview += "..."
                return f"{path}  '{preview}'"
            return path
        case "WebSearch":
            return input.get("query", "")
        case "WebFetch":
            return input.get("url", "")
        case _:
            compact = json.dumps(input, separators=(",", ":"))
            if len(compact) > 120:
                return compact[:117] + "..."
            return compact


def _format_tool_result(name: str, content: str | list | None, is_error: bool | None) -> str:
    """Format tool result for display — summary line + truncated output."""
    if content is None:
        return "(no output)"

    # Normalize content to string
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        text = "\n".join(parts)
    else:
        text = str(content)

    if not text.strip():
        return "(empty)"

    # Redact private content from terminal display.
    if "<private>" in text:
        redacted = re.sub(
            r"<private>.*?</private>",
            "[private content]",
            text,
            flags=re.DOTALL,
        )
        text = redacted

    lines = text.split("\n")

    if is_error:
        error_text = text[:500]
        if len(text) > 500:
            error_text += f"\n... ({len(text) - 500} more chars)"
        return f"Error:\n{error_text}"

    base = _base_tool_name(name)
    match base:
        case "Read":
            summary = f"{len(lines)} lines"
        case "Bash":
            summary = "output:"
        case "Write":
            summary = f"wrote {len(text)} bytes"
        case "Edit":
            summary = "applied"
        case _:
            summary = ""

    if len(lines) <= TOOL_RESULT_MAX_LINES:
        output = text
    else:
        output = "\n".join(lines[:TOOL_RESULT_MAX_LINES])
        output += f"\n... ({len(lines) - TOOL_RESULT_MAX_LINES} more lines)"

    if summary:
        return f"{summary}\n{output}"
    return output


class KilnApp:
    """Scrollback-mode terminal interface for agent sessions.

    Uses a prompt_toolkit Application (full_screen=False) for persistent
    keybinding handling. Styled output goes to scrollback via print_formatted_text.
    Responses print in full at commit time (no live streaming).
    """

    _HEARTBEAT_BASE_INTERVAL: float = 60.0  # 1 min starting interval

    def __init__(self, harness) -> None:
        self._harness = harness
        self._agent_label = harness.config.name.capitalize()
        self._stream_chunks: list[str] = []
        self._thinking_buffer = ""
        self._tool_name_queue: list[str] = []
        self._context_tokens = 0  # latest API call's total input ~ current context size
        self._last_call_usage = {}  # per-API-call usage from message_delta events
        self._receiving = False
        self._interrupt_in_flight = False
        self._receive_task: asyncio.Task | None = None
        if harness.config.initial_mode:
            self._perm_mode = PermissionMode(harness.config.initial_mode)
        else:
            self._perm_mode = PermissionMode.DEFAULT
        self._pending_permission: PermissionRequest | None = None
        self._app: Application | None = None

        # Idle message delivery
        self._auto_delivery_enabled = True
        self._last_auto_delivery: float = 0.0
        self._last_turn_source: str = "user"  # "user" or "agent"
        self._watcher_task: asyncio.Task | None = None

        # Heartbeat: nudge agent after idle period, with exponential backoff.
        self._heartbeat_enabled = harness.config.heartbeat
        self._heartbeat_interval: float = harness.config.heartbeat_interval
        self._heartbeat_backoff: float = self._HEARTBEAT_BASE_INTERVAL
        self._heartbeat_task: asyncio.Task | None = None
        self._heartbeat_oneshot: float | None = None  # one-shot override (seconds)

        # Queued messages: user input submitted while receiving.
        # Uses the harness's shared queue so the PostToolUse hook can
        # drain it mid-turn (not just at turn end).
        self._message_queue = harness.user_message_queue

        # Initial send task (startup + --prompt), tracked separately for cancellation
        self._initial_task: asyncio.Task | None = None

        # Channel view state: "agent" or "channel:<name>"
        self._current_view: str = "agent"

        # Plan cache: (mtime, formatted_progress_string)
        self._plan_cache: tuple[float, str | None] = (0.0, None)

        # Build the prompt_toolkit Application
        self._input_buffer = Buffer(multiline=True)
        kb = self._build_keybindings()

        @Condition
        def has_pending_permission():
            return self._pending_permission is not None

        layout = Layout(
            HSplit([
                ConditionalContainer(
                    Window(FormattedTextControl(self._permission_bar), height=1),
                    filter=has_pending_permission,
                ),
                Window(
                    BufferControl(buffer=self._input_buffer),
                    height=D(min=1, max=10),
                    wrap_lines=True,
                    dont_extend_height=True,
                    get_line_prefix=self._input_prefix,
                ),
                Window(FormattedTextControl(self._toolbar), height=1),
            ])
        )

        self._app = Application(
            layout=layout,
            key_bindings=kb,
            full_screen=False,
            style=TUI_STYLE,
        )

    def _input_prefix(self, line_number: int, wrap_count: int) -> list[tuple[str, str]]:
        """Prefix for input lines: '> ' on first line, '# ' in channel view, '  ' on continuations."""
        if line_number == 0 and wrap_count == 0:
            if self._in_channel_view:
                return [("class:agent-msg", "# ")]
            return [("", "> ")]
        return [("", "  ")]

    def _build_keybindings(self) -> KeyBindings:
        """Create keybindings with state-based filters."""
        kb = KeyBindings()
        app_ref = self  # closure reference

        @Condition
        def is_receiving():
            return app_ref._receiving

        @Condition
        def is_idle():
            return not app_ref._receiving

        @Condition
        def is_permission_pending():
            return app_ref._pending_permission is not None

        # --- Permission keybindings ---

        @kb.add("y", filter=is_permission_pending)
        def handle_perm_accept(event):
            req = app_ref._pending_permission
            if req and not req.event.is_set():
                req.decide(True)

        @kb.add("n", filter=is_permission_pending)
        def handle_perm_reject(event):
            req = app_ref._pending_permission
            if req and not req.event.is_set():
                req.decide(False)

        @kb.add("enter", filter=is_permission_pending)
        def handle_enter_permission(event):
            pass

        @kb.add("tab", filter=~is_permission_pending)
        def handle_tab(event):
            app_ref._perm_mode = app_ref._perm_mode.next()
            if app_ref._app:
                app_ref._app.invalidate()

        @kb.add("c-o", filter=is_idle & ~is_permission_pending)
        def handle_view_cycle(event):
            app_ref._cycle_view(+1)

        @kb.add("enter", filter=is_idle & ~is_permission_pending)
        def handle_enter(event):
            text = app_ref._input_buffer.text.strip()
            if not text:
                return

            app_ref._input_buffer.reset()

            if text == "/exit":
                event.app.exit()
                return
            if text == "/restart":
                app_ref._harness.restart_requested = True
                event.app.exit()
                return
            if text == "/fquit":
                sc = app_ref._harness.session_control
                if sc:
                    sc.skip_summary = True
                event.app.exit()
                return
            if text == "/ch":
                app_ref._cycle_view(+1)
                return
            if text == "/plan":
                app_ref._show_plan()
                return
            if text.startswith("/heartbeat"):
                app_ref._toggle_heartbeat(text)
                return

            # Channel view: send directly to the channel
            if app_ref._in_channel_view:
                ch_name = app_ref._current_view.split(":", 1)[1]
                _tprint("<user>You \u2192 #{}</user>: {}", ch_name, text)
                app_ref._send_to_channel(ch_name, text)
                return

            # Lock out further submissions immediately
            app_ref._receiving = True
            if app_ref._app:
                app_ref._app.invalidate()

            _tprint("<user>You:</user> {}", text)

            app_ref._receive_task = asyncio.ensure_future(app_ref._send_and_receive(text))

        @kb.add("enter", filter=is_receiving & ~is_permission_pending)
        def handle_enter_receiving(event):
            text = app_ref._input_buffer.text.strip()
            if not text:
                return
            app_ref._message_queue.append(text)
            app_ref._input_buffer.reset()
            _tprint("<dim>Queued:</dim> {}", text)

        # Newline insertion: Alt+Enter, Shift+Enter, or CSI u Shift+Enter
        @kb.add("escape", "enter")  # Alt+Enter (universal)
        @kb.add("c-j")              # \n from iTerm2 Shift+Enter mapping
        @kb.add(Keys.F20)           # CSI u Shift+Enter (kitty/WezTerm/Ghostty)
        def handle_newline(event):
            event.current_buffer.newline()

        @kb.add("escape", filter=is_permission_pending)
        def handle_escape_permission(event):
            req = app_ref._pending_permission
            if req and not req.event.is_set():
                req.decide(False)

        @kb.add("escape", filter=is_receiving & ~is_permission_pending)
        def handle_escape(event):
            asyncio.ensure_future(app_ref._do_interrupt())

        @kb.add("c-c")
        async def handle_quit(event):
            if app_ref._receiving:
                _tprint("\n<dim-i>--- force killing subprocess ---</dim-i>")
                try:
                    await app_ref._harness.force_stop()
                except Exception:
                    pass
            event.app.exit()

        return kb

    # ---- Channel view helpers ----

    def _subscribed_channels(self) -> list[str]:
        """Return list of channels this agent is subscribed to."""
        channels_path = self._harness.config.home / "channels.json"
        if not channels_path.exists():
            return []
        try:
            channels = json.loads(channels_path.read_text())
        except (json.JSONDecodeError, OSError):
            return []
        return sorted(
            name for name, subs in channels.items()
            if self._harness.agent_id in subs
        )

    def _view_list(self) -> list[str]:
        """Build the ordered list of views: agent + subscribed channels."""
        views = ["agent"]
        for ch in self._subscribed_channels():
            views.append(f"channel:{ch}")
        return views

    def _cycle_view(self, direction: int) -> None:
        """Cycle to the next (+1) or previous (-1) view."""
        views = self._view_list()
        if len(views) <= 1:
            return
        try:
            idx = views.index(self._current_view)
        except ValueError:
            idx = 0
        idx = (idx + direction) % len(views)
        new_view = views[idx]
        if new_view == self._current_view:
            return
        self._current_view = new_view
        self._render_view_switch()
        if self._app:
            self._app.invalidate()

    def _render_view_switch(self) -> None:
        """Print header and content when switching views."""
        if self._current_view == "agent":
            _tprint("\n<dim>\u2500\u2500\u2500 Agent View \u2500\u2500\u2500</dim>\n")
        elif self._current_view.startswith("channel:"):
            ch_name = self._current_view.split(":", 1)[1]
            _tprint("\n<dim>\u2500\u2500\u2500 Channel: </dim><agent-msg-b>{}</agent-msg-b><dim> \u2500\u2500\u2500</dim>", ch_name)
            self._render_channel_history(ch_name)

    def _render_channel_history(self, channel: str, max_lines: int = 30) -> None:
        """Dump recent channel history to scrollback."""
        history_file = self._harness.config.home / "channels" / channel / "history.jsonl"
        if not history_file.exists():
            _tprint("<dim>  (no history yet)</dim>\n")
            return

        lines = []
        try:
            text = history_file.read_text()
            for line in text.strip().splitlines():
                if line.strip():
                    lines.append(json.loads(line))
        except (json.JSONDecodeError, OSError):
            _tprint("<dim>  (error reading history)</dim>\n")
            return

        recent = lines[-max_lines:]
        if len(lines) > max_lines:
            _tprint("<dim>  ... ({} earlier messages)</dim>", len(lines) - max_lines)

        for entry in recent:
            ts_raw = entry.get("ts", "")
            sender = entry.get("from", "?")
            body = entry.get("body", "")
            summary = entry.get("summary", "")
            try:
                dt = datetime.fromisoformat(ts_raw)
                ts_display = dt.astimezone().strftime("%H:%M")
            except (ValueError, TypeError):
                ts_display = "??:??"
            display_text = body if body else summary
            if len(display_text) > 300:
                display_text = display_text[:300] + "..."
            _tprint("<dim>{}</dim> <agent-msg-b>{}</agent-msg-b>: {}", ts_display, sender, display_text)

        _tprint("")

    def _send_to_channel(self, channel: str, text: str) -> None:
        """Send a message from the TUI user directly to a channel."""
        channels_path = self._harness.config.home / "channels.json"
        if not channels_path.exists():
            _tprint("<err>No channels configured.</err>")
            return

        try:
            channels = json.loads(channels_path.read_text())
        except (json.JSONDecodeError, OSError):
            _tprint("<err>Error reading channels.</err>")
            return

        subs = channels.get(channel, [])
        agent_id = self._harness.agent_id
        recipients = [s for s in subs if s != agent_id]

        if not recipients:
            _tprint("<err>No other subscribers on channel '{}'.</err>", channel)
            return

        inbox_root = self._harness.config.inbox_path
        import uuid as _uuid
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        summary = text[:100] if len(text) > 100 else text

        for recipient in recipients:
            recipient_inbox = inbox_root / recipient
            recipient_inbox.mkdir(parents=True, exist_ok=True)
            msg_id = f"msg-{timestamp}-{_uuid.uuid4().hex[:6]}"
            msg_path = recipient_inbox / f"{msg_id}.md"
            content = (
                f"---\n"
                f"from: {agent_id}\n"
                f"summary: \"{summary}\"\n"
                f"priority: normal\n"
                f"channel: {channel}\n"
                f"timestamp: {datetime.now(timezone.utc).isoformat()}\n"
                f"---\n\n"
                f"{text}\n"
            )
            msg_path.write_text(content)

        # Append to channel history
        history_dir = self._harness.config.home / "channels" / channel
        history_dir.mkdir(parents=True, exist_ok=True)
        history_file = history_dir / "history.jsonl"
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "from": agent_id,
            "summary": summary,
            "body": text,
            "priority": "normal",
        }
        with open(history_file, "a") as f:
            f.write(json.dumps(entry) + "\n")

        _tprint("<dim>Sent to {} ({} recipients)</dim>", channel, len(recipients))

    @property
    def _in_channel_view(self) -> bool:
        return self._current_view.startswith("channel:")

    _MODE_STYLE = {
        PermissionMode.SAFE: "mode-safe",
        PermissionMode.DEFAULT: "mode-default",
        PermissionMode.YOLO: "mode-yolo",
    }

    def _toolbar(self) -> HTML:
        """Build the persistent bottom toolbar content."""
        if self._receiving:
            status = "Working..."
        else:
            status = "Ready"

        mode_style = self._MODE_STYLE[self._perm_mode]
        mode_html = f"<{mode_style}>{self._perm_mode.value}</{mode_style}>"

        parts = [status, self._harness.agent_id, mode_html]

        # Current view indicator
        if self._in_channel_view:
            ch_name = self._current_view.split(":", 1)[1]
            parts.append(f"<agent-msg-b>#{ch_name}</agent-msg-b>")
        else:
            channels = self._subscribed_channels()
            if channels:
                parts.append(f"<dim>{len(channels)} ch</dim>")

        # Plan progress
        plan_progress = self._plan_progress()
        if plan_progress:
            parts.append(plan_progress)

        if getattr(self._harness.config, "ephemeral", False):
            parts.append("<err>ephemeral</err>")
        if self._context_tokens:
            parts.append(f"{_fmt_tokens(self._context_tokens)} / 200k")

        # Context budget warning
        if self._context_tokens > 150_000:
            parts.append("<err>\u26a0 auto-delivery paused</err>")

        # Pending message count
        if not self._receiving:
            pending = self._pending_message_count()
            if pending:
                parts.append(f"\U0001f4e8 {pending} pending")

        # Queued user messages waiting for delivery
        if self._message_queue:
            n = len(self._message_queue)
            parts.append(f"<tool>{n} queued</tool>")

        if self._receiving and not self._pending_permission:
            parts.append("Esc to interrupt")

        return HTML(f" {' | '.join(parts)}")

    def _plan_progress(self) -> str | None:
        """Read the agent's plan file and return a compact progress string."""
        plan_path = self._plan_path()
        if not plan_path.exists():
            return None
        try:
            mtime = plan_path.stat().st_mtime
        except OSError:
            return None
        if mtime == self._plan_cache[0]:
            return self._plan_cache[1]
        try:
            data = yaml.safe_load(plan_path.read_text())
        except (yaml.YAMLError, OSError):
            return None
        if not data or not data.get("tasks"):
            result = None
        else:
            tasks = data["tasks"]
            done = sum(1 for t in tasks if t.get("status") == "done")
            result = f"Plan: {done}/{len(tasks)}"
        self._plan_cache = (mtime, result)
        return result

    def _plan_path(self) -> Path:
        return self._harness.config.plans_path / f"{self._harness.agent_id}.yml"

    def _show_plan(self) -> None:
        """Dump the current plan to scrollback."""
        plan_path = self._plan_path()
        if not plan_path.exists():
            _tprint("<dim>No active plan.</dim>")
            return
        try:
            data = yaml.safe_load(plan_path.read_text())
        except (yaml.YAMLError, OSError):
            _tprint("<err>Error reading plan file.</err>")
            return
        if not data or not data.get("tasks"):
            _tprint("<dim>No active plan.</dim>")
            return

        _tprint("\n<text-heading>Plan: {}</text-heading>", data.get("goal", "(no goal)"))
        status_style = {"done": "diff-add", "in_progress": "tool", "pending": "dim"}
        for t in data["tasks"]:
            status = t.get("status", "pending")
            desc = t.get("description", "")
            style = status_style.get(status, "dim")
            icon = {"done": "\u2713", "in_progress": "\u25b6", "pending": "\u25cb"}.get(status, " ")
            _tprint("  <{style}>{icon} [{status}]</{style}> {desc}",
                    style=style, icon=icon, status=status, desc=desc)
        tasks = data["tasks"]
        done = sum(1 for t in tasks if t.get("status") == "done")
        _tprint("<dim>Progress: {}/{} done</dim>\n", done, len(tasks))

    def _permission_bar(self) -> HTML:
        """Build the ephemeral permission prompt that appears above the input."""
        req = self._pending_permission
        if not req:
            return HTML("")
        return HTML(
            " <perm-prompt>Allow {tool}?</perm-prompt>"
            "  <perm-key>[y]</perm-key> accept"
            "  <perm-key>[n]</perm-key> reject".format(tool=_display_name(req.tool_name))
        )

    def run(self) -> None:
        """Run the TUI event loop."""
        asyncio.run(self._main())

    async def _main(self) -> None:
        """Main async loop: connect, then run the Application."""
        _tprint("<dim>Connecting...</dim>")

        # Pass permission callbacks — harness creates the hook in _build_options()
        # where it has access to shell state for path-based exemptions.
        self._harness.set_permission_callbacks(
            get_mode=lambda: self._perm_mode,
            request_permission=self._request_permission,
        )

        try:
            await self._harness.start()
        except Exception as e:
            _tprint("<err-b>Connection error:</err-b> {}", str(e))
            return

        _tprint("<dim>Session started: {}</dim>\n", self._harness.agent_id)

        try:
            with patch_stdout():
                # Start inbox watcher for idle message delivery
                self._watcher_task = asyncio.ensure_future(self._inbox_watcher())

                # Start heartbeat watcher
                self._heartbeat_task = asyncio.ensure_future(self._heartbeat_watcher())

                # Drain any pre-loaded prompts (startup, maintenance, --prompt).
                # The harness populates user_message_queue during start().
                # Also handle direct prompt for ephemeral agents.
                prompt = self._harness.config.prompt
                if prompt and not self._message_queue:
                    # No startup message queued — send prompt directly
                    self._message_queue.append(prompt)

                async def send_initial():
                    await asyncio.sleep(0)  # yield once to let Application start
                    while self._message_queue:
                        msg = self._message_queue.pop(0)
                        _tprint("<dim>...</dim>")
                        await self._send_and_receive(msg)

                if self._message_queue:
                    self._initial_task = asyncio.ensure_future(send_initial())

                await self._app.run_async()
        except (KeyboardInterrupt, EOFError):
            pass
        finally:
            # Cancel background watchers and any pending initial send
            for task in (self._watcher_task, self._heartbeat_task, self._initial_task):
                if task and not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
            # Clean up per-agent heartbeat state file so it doesn't accumulate
            # across sessions. Each session writes its own file if /heartbeat
            # is used; remove it on exit so stale files don't pile up.
            try:
                self._heartbeat_file().unlink(missing_ok=True)
            except OSError:
                pass

            # Bypass permissions for unattended exit tasks (summary, archival)
            self._perm_mode = PermissionMode.YOLO
            sc = self._harness.session_control
            skip_summary = getattr(self._harness.config, "ephemeral", False) or (sc and sc.skip_summary)
            if not skip_summary:
                self._harness.prepare_shutdown()
                if self._message_queue:
                    _tprint("\n<dim>Running session-end protocol...</dim>")
                    try:
                        while self._message_queue:
                            msg = self._message_queue.pop(0)
                            await self._harness.send(msg)
                            async for _ in self._harness.receive():
                                pass
                    except Exception:
                        pass

            # Archive and commit even when skipping summary (but not for ephemeral)
            if not getattr(self._harness.config, "ephemeral", False):
                archive_path = self._harness.archive_conversation()
                if archive_path:
                    _tprint("<dim>Archived conversation to {}</dim>", archive_path)

                commit_result = self._harness.commit_memory()
                if commit_result:
                    _tprint("<dim>Git: {}</dim>", commit_result)

            _tprint("<dim>Disconnecting...</dim>")
            await self._harness.stop()

    async def _send_and_receive(self, text: str, source: str = "user") -> None:
        """Send a message and render the full response."""
        self._stream_chunks = []
        self._thinking_buffer = ""
        self._receiving = True

        try:
            # Inject timestamp so the agent has time awareness
            ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            stamped = f"[{ts}]\n{text}"
            await self._harness.send(stamped)

            async for msg in self._harness.receive():
                if self._interrupt_in_flight:
                    if isinstance(msg, ResultMessage):
                        break
                    continue
                self._handle_sdk_message(msg)

            if not self._interrupt_in_flight:
                self._commit_stream()
                self._commit_thinking()

        except Exception as e:
            _tprint("\n<err-b>Error:</err-b> {}", str(e))
        finally:
            self._receiving = False
            self._interrupt_in_flight = False
            self._last_turn_source = source
            self._last_auto_delivery = time.monotonic()

            # Reset heartbeat backoff on real activity (not heartbeats themselves)
            if source != "heartbeat":
                self._heartbeat_backoff = self._HEARTBEAT_BASE_INTERVAL

            # Check if the agent requested a session exit (exit_session tool)
            sc = self._harness.session_control
            if sc and sc.quit_requested and self._app:
                if sc.continue_requested:
                    self._harness.continue_requested = True
                    self._harness.handoff_text = sc.handoff_text
                self._app.exit()
                return

            if self._app:
                self._app.invalidate()

            # Send queued user message if any
            if self._message_queue:
                queued = self._message_queue.pop(0)
                _tprint("<user>You:</user> {}", queued)
                self._receiving = True
                if self._app:
                    self._app.invalidate()
                self._receive_task = asyncio.ensure_future(self._send_and_receive(queued))

    async def _do_interrupt(self) -> None:
        """Interrupt the current response."""
        if not self._receiving or self._interrupt_in_flight:
            return
        self._interrupt_in_flight = True

        if self._pending_permission and not self._pending_permission.event.is_set():
            self._pending_permission.decide(False)

        self._commit_stream()
        self._commit_thinking()
        _tprint("\n<dim-i>--- interrupted ---</dim-i>")
        try:
            await self._harness.interrupt()
        except Exception:
            pass
        loop = asyncio.get_event_loop()
        loop.call_later(5.0, self._force_cancel_receive)

    def _force_cancel_receive(self) -> None:
        """Safety net: cancel receive task if still running after interrupt timeout."""
        if self._receive_task and not self._receive_task.done():
            self._receive_task.cancel()

    # ---- Idle message delivery ----

    async def _inbox_watcher(self) -> None:
        """Poll the inbox directory for unread messages while the agent is idle."""
        inbox = self._harness.config.agent_inbox(self._harness.agent_id)
        while True:
            await asyncio.sleep(1.0)
            try:
                if not self._should_deliver(inbox):
                    continue
                msg = self._next_unread_message(inbox)
                if msg:
                    await self._deliver_agent_message(msg)
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(5.0)

    def _should_deliver(self, inbox: Path) -> bool:
        """Check whether conditions are met for auto-delivering a message."""
        if not self._auto_delivery_enabled:
            return False
        if self._receiving:
            return False
        if self._pending_permission:
            return False
        if self._input_buffer.text:
            return False
        elapsed = time.monotonic() - self._last_auto_delivery
        min_wait = 2.0 if self._last_turn_source == "user" else 1.0
        if elapsed < min_wait:
            return False
        if self._context_tokens > 150_000:
            return False
        if not inbox.exists():
            return False
        return True

    def _next_unread_message(self, inbox: Path) -> dict | None:
        """Find the next unread message in the inbox, preferring high priority."""
        if not inbox.exists():
            return None

        candidates = []
        for msg_file in sorted(inbox.iterdir()):
            if not msg_file.is_file() or msg_file.suffix != ".md":
                continue
            read_marker = msg_file.with_suffix(".read")
            if read_marker.exists():
                continue
            parsed = parse_message(msg_file)
            if parsed:
                candidates.append(parsed)

        if not candidates:
            return None

        for c in candidates:
            if c["priority"] == "high":
                return c
        return candidates[0]

    async def _deliver_agent_message(self, msg: dict) -> None:
        """Format and inject an agent message as a user turn."""
        msg_path = Path(msg["path"])
        msg_path.with_suffix(".read").touch()

        sender = msg.get("from", "unknown")
        summary = msg.get("summary", "")
        body = msg.get("body", "")

        _tprint("\n<agent-msg-b>\U0001f4e8 {}:</agent-msg-b>", sender)
        if summary:
            _tprint("<agent-msg>{}</agent-msg>", summary)
        if body and body != summary:
            display_body = body if len(body) < 2000 else body[:2000] + "\n... (truncated)"
            _tprint("<agent-msg>{}</agent-msg>", display_body)

        model_text = body or summary
        formatted = f"[Message from {sender}]\n{model_text}"

        if len(model_text) < 200:
            cooldown = 5.0
        else:
            cooldown = 1.0

        self._receiving = True
        if self._app:
            self._app.invalidate()

        await self._send_and_receive(formatted, source="agent")

        self._last_auto_delivery = time.monotonic() + (cooldown - 1.0)

    # ---- Heartbeat ----

    def _heartbeat_file(self) -> Path:
        """Return the per-agent heartbeat state file path."""
        return self._harness.config.home / "state" / f"heartbeat-{self._harness.agent_id}"

    def _check_heartbeat_file(self) -> None:
        """Read heartbeat config from per-agent state file.

        Checks state/heartbeat-{agent_id} first, falls back to the shared
        state/heartbeat for backward compatibility (e.g. manual writes before
        the per-agent fix).
        """
        per_agent = self._heartbeat_file()
        shared = self._harness.config.home / "state" / "heartbeat"
        hb_file = per_agent if per_agent.exists() else shared
        try:
            if not hb_file.exists():
                return
            val = hb_file.read_text().strip().lower()
            if val == "off":
                self._heartbeat_enabled = False
            else:
                minutes = float(val)
                if minutes > 0:
                    self._heartbeat_interval = minutes * 60
                    self._heartbeat_enabled = True
        except (ValueError, OSError):
            pass

    async def _heartbeat_watcher(self) -> None:
        """Nudge the agent after a period of inactivity."""
        while True:
            await asyncio.sleep(10.0)
            self._check_heartbeat_file()
            if not self._heartbeat_enabled:
                continue
            if self._receiving or self._pending_permission:
                continue
            if self._input_buffer.text:
                continue
            if self._context_tokens > 150_000:
                continue
            elapsed = time.monotonic() - self._last_auto_delivery
            interval = self._heartbeat_oneshot or self._heartbeat_backoff
            if elapsed < interval:
                continue

            self._heartbeat_oneshot = None

            ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            msg = f"[{ts}] (heartbeat)"
            minutes = int(elapsed / 60)
            _tprint("\n<dim>\u2764\ufe0f Heartbeat ({} idle)</dim>", f"{minutes}min")

            self._receiving = True
            if self._app:
                self._app.invalidate()
            await self._send_and_receive(msg, source="heartbeat")

            self._heartbeat_backoff = min(
                self._heartbeat_backoff * 2, self._heartbeat_interval
            )

    def _toggle_heartbeat(self, text: str) -> None:
        """Handle /heartbeat command."""
        parts = text.split()
        if len(parts) >= 2:
            try:
                minutes = float(parts[1])
                self._heartbeat_interval = minutes * 60
                self._heartbeat_enabled = True
                _tprint("<dim>Heartbeat: on, interval {}min</dim>", int(minutes))
            except ValueError:
                if parts[1] == "off":
                    self._heartbeat_enabled = False
                    _tprint("<dim>Heartbeat: off</dim>")
                elif parts[1] == "on":
                    self._heartbeat_enabled = True
                    _tprint("<dim>Heartbeat: on, interval {}min</dim>",
                            int(self._heartbeat_interval / 60))
                else:
                    _tprint("<dim>Usage: /heartbeat [on|off|<minutes>]</dim>")
        else:
            self._heartbeat_enabled = not self._heartbeat_enabled
            state = "on" if self._heartbeat_enabled else "off"
            _tprint("<dim>Heartbeat: {} (interval {}min)</dim>",
                    state, int(self._heartbeat_interval / 60))
        # Persist to per-agent state file so the setting survives
        # _check_heartbeat_file polls and doesn't affect other agents.
        self._write_heartbeat_file()

    def _write_heartbeat_file(self) -> None:
        """Write current heartbeat state to the per-agent state file."""
        hb_file = self._heartbeat_file()
        try:
            hb_file.parent.mkdir(parents=True, exist_ok=True)
            if self._heartbeat_enabled:
                hb_file.write_text(str(int(self._heartbeat_interval / 60)))
            else:
                hb_file.write_text("off")
        except OSError:
            pass

    def _pending_message_count(self) -> int:
        """Count unread messages in the inbox."""
        inbox = self._harness.config.agent_inbox(self._harness.agent_id)
        if not inbox.exists():
            return 0
        count = 0
        for msg_file in inbox.iterdir():
            if msg_file.is_file() and msg_file.suffix == ".md":
                if not msg_file.with_suffix(".read").exists():
                    count += 1
        return count

    def _drain_ui_events(self) -> None:
        """Drain the harness UI event queue and render each event."""
        events = self._harness.ui_events
        if not events:
            return
        batch = list(events)
        events.clear()
        for ev in batch:
            etype = ev.get("type", "")
            if etype == "followup_delivered":
                for msg in ev.get("messages", []):
                    _tprint("\n<user>You (followup):</user> {}", msg)
            elif etype == "inbox_message":
                sender = ev.get("from", "unknown")
                summary = ev.get("summary", "")
                channel = ev.get("channel", "")
                if channel:
                    _tprint("\n<dim>  \U0001F4E8 [{}] {}: {}</dim>", channel, sender, summary)
                else:
                    _tprint("\n<dim>  \U0001F4E8 {} \u2192 {}</dim>", sender, summary)
            elif etype == "message_sent":
                to = ev.get("to", "")
                channel = ev.get("channel", "")
                summary = ev.get("summary", "")
                if channel:
                    _tprint("\n<dim>  \u2709 [{}] \u2192 {}</dim>", channel, summary)
                else:
                    _tprint("\n<dim>  \u2709 \u2192 {}: {}</dim>", to, summary)

    def _handle_sdk_message(self, msg: object) -> None:
        """Route an incoming SDK message to the appropriate handler."""
        self._drain_ui_events()

        if isinstance(msg, StreamEvent):
            event = msg.event
            etype = event.get("type", "")
            if etype == "content_block_delta":
                delta = event.get("delta", {})
                delta_type = delta.get("type", "")
                if delta_type == "text_delta":
                    text = delta.get("text", "")
                    if text:
                        if not self._stream_chunks:
                            self._commit_thinking()
                        self._stream_chunks.append(text)
                elif delta_type == "thinking_delta":
                    thinking = delta.get("thinking", "")
                    if thinking:
                        self._on_stream_thinking(thinking)
            elif etype == "message_delta":
                usage = event.get("usage", {})
                if usage:
                    self._last_call_usage = usage
                    self._context_tokens = (
                        usage.get("input_tokens", 0)
                        + usage.get("cache_read_input_tokens", 0)
                        + usage.get("cache_creation_input_tokens", 0)
                    )
                    if self._harness.session_control:
                        self._harness.session_control.context_tokens = self._context_tokens
                    if self._app:
                        self._app.invalidate()

        elif isinstance(msg, AssistantMessage):
            warning = self._harness.check_model(msg.model)
            if warning:
                _tprint("\n<err-b>Warning:</err-b> <err>{}</err>", warning)

            for block in msg.content:
                if isinstance(block, TextBlock) and block.text:
                    if not self._stream_chunks:
                        self._stream_chunks.append(block.text)
                elif isinstance(block, ToolUseBlock):
                    self._tool_name_queue.append(block.name)
                    self._on_tool_call_start(block.name, block.input)

        elif isinstance(msg, UserMessage):
            content = msg.content
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, ToolResultBlock):
                        tool_name = self._tool_name_queue.pop(0) if self._tool_name_queue else ""
                        self._on_tool_call_result(
                            tool_name, block.content, block.is_error
                        )

        elif isinstance(msg, ResultMessage):
            if msg.session_id and not self._harness.session_id:
                self._harness.session_id = msg.session_id
                self._harness.register_session()
            self._on_turn_complete(msg)

        elif isinstance(msg, SystemMessage):
            if msg.subtype not in ("init",):
                _tprint("<dim-i>System: {}</dim-i>", msg.subtype)

    # ---- Permissions ----

    async def _request_permission(self, req: PermissionRequest) -> bool:
        """Display diff/preview and wait for user y/n decision."""
        self._commit_stream()
        self._commit_thinking()

        self._render_permission_prompt(req)

        self._pending_permission = req
        if self._app:
            self._app.invalidate()

        try:
            await asyncio.wait_for(req.event.wait(), timeout=300)
        except asyncio.TimeoutError:
            req.timed_out = True
            req.decide(False)
            _tprint("<dim>Permission request timed out (5 min). Auto-rejected.</dim>")
        finally:
            self._pending_permission = None
            if self._app:
                self._app.invalidate()

        if req.result:
            _tprint("<dim>    accepted</dim>")
        else:
            _tprint("<dim>    rejected</dim>")

        return req.result

    def _render_permission_prompt(self, req: PermissionRequest) -> None:
        """Render diff or command preview for a permission request."""
        display = _display_name(req.tool_name)
        path = req.tool_input.get("file_path", "")
        if path:
            _tprint("\n  <tool>\u2192 {}</tool>  <dim>{}</dim>", display, path)
        else:
            _tprint("\n  <tool>\u2192 {}</tool>", display)

        if req.diff_text:
            _tprint("")
            for line in req.diff_text.splitlines():
                if line.startswith("DANGEROUS:"):
                    _tprint("<danger>    \u26a0 {}</danger>", line)
                elif line.startswith("+++") or line.startswith("---"):
                    _tprint("<dim>    {}</dim>", line)
                elif line.startswith("+"):
                    _tprint("<diff-add>    {}</diff-add>", line)
                elif line.startswith("-"):
                    _tprint("<diff-rm>    {}</diff-rm>", line)
                elif line.startswith("@@"):
                    _tprint("<diff-hunk>    {}</diff-hunk>", line)
                elif line.startswith("new file"):
                    _tprint("<dim-i>    {}</dim-i>", line)
                else:
                    _tprint("<dim>    {}</dim>", line)

    # ---- Rendering ----

    def _on_stream_thinking(self, text: str) -> None:
        """Handle a chunk of streamed thinking text."""
        if not self._thinking_buffer:
            if self._harness.show_thinking:
                _tprint("\n<dim-i>Thinking...</dim-i>")

        self._thinking_buffer += text

    def _on_tool_call_start(self, name: str, input: dict) -> None:
        """Render a tool call with its input details."""
        self._commit_stream()
        self._commit_thinking()

        if needs_permission(self._perm_mode, name):
            return

        details = _format_tool_input(name, input)
        display = _display_name(name)
        if details:
            indented = "\n".join(f"    {line}" for line in details.split("\n"))
            _tprint("\n  <tool>\u2192 {}</tool>\n<dim>{}</dim>", display, indented)
        else:
            _tprint("\n  <tool>\u2192 {}</tool>", display)

    def _on_tool_call_result(
        self, name: str, content: str | list | None, is_error: bool | None
    ) -> None:
        """Render a tool result."""
        formatted = _format_tool_result(name, content, is_error)
        indented = "\n".join(f"    {line}" for line in formatted.split("\n"))
        if is_error:
            _tprint("<err>{}</err>", indented)
        else:
            _tprint("<dim>{}</dim>", indented)

    def _on_turn_complete(self, msg: ResultMessage) -> None:
        """Render turn completion stats."""
        self._commit_stream()
        self._commit_thinking()

        last = self._last_call_usage
        if last:
            self._context_tokens = (
                last.get("input_tokens", 0)
                + last.get("cache_read_input_tokens", 0)
                + last.get("cache_creation_input_tokens", 0)
            )

        parts = [f"{msg.num_turns} turns", f"{msg.duration_ms}ms"]
        summary = "  |  ".join(parts)
        _tprint("\n<dim>--- {} ---</dim>", summary)

        self._last_call_usage = {}

    # ---- Helpers ----

    def _commit_stream(self) -> None:
        """Render accumulated text as markdown and print to scrollback."""
        if self._stream_chunks:
            full_text = "".join(self._stream_chunks)

            # Parse !hb <minutes> suffix — one-shot heartbeat override
            hb_match = re.search(r'!hb\s+(\d+(?:\.\d+)?)\s*$', full_text)
            if hb_match:
                minutes = float(hb_match.group(1))
                self._heartbeat_oneshot = minutes * 60
                full_text = full_text[:hb_match.start()].rstrip()

            _tprint("\n<assistant>{}:</assistant>", self._agent_label)
            print_formatted_text(_markdown_to_ft(full_text), style=TUI_STYLE)
            if hb_match:
                _tprint("<dim>Heartbeat one-shot: {}min</dim>", hb_match.group(1))
            self._stream_chunks = []

    def _commit_thinking(self) -> None:
        """Flush the thinking buffer as dimmed text."""
        if self._thinking_buffer:
            if self._harness.show_thinking:
                _tprint("<dim-i>{}</dim-i>", self._thinking_buffer)
            self._thinking_buffer = ""

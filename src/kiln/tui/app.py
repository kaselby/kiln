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
import subprocess
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

from ..types import (
    ContentBlockDeltaEvent,
    ContentBlockEndEvent,
    ContentBlockStartEvent,
    ErrorEvent,
    Event,
    SystemMessageEvent,
    TextEvent,
    ThinkingEvent,
    ToolCallEvent,
    ToolResultEvent,
    TurnCompleteEvent,
    UsageUpdateEvent,
)

from ..hooks import _resolve_live_trust, format_message_source, parse_message
from ..state import write_presence
from ..permissions import (
    PermissionMode,
    PermissionRequest,
    needs_permission,
)

# Max lines of tool result output to show inline
TOOL_RESULT_MAX_LINES = 10

# Context token threshold for pausing auto-delivery and heartbeats
CONTEXT_PAUSE_THRESHOLD = 180_000


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
    "hook": "ansiblue",
    "hook-block": "ansired bold",
    "agent-msg": "ansimagenta",
    "agent-msg-b": "ansimagenta bold",
    "perm-prompt": "ansiyellow bold",
    "perm-key": "ansicyan bold",
    "mode-safe": "ansired bold",
    "mode-supervised": "ansiyellow bold",
    "mode-yolo": "ansigreen bold",
    "mode-trusted": "ansicyan bold",
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

    mcp__kiln__Bash    -> Kiln::Bash
    mcp__myagent__Bash -> Myagent::Bash
    Bash               -> Kiln::Bash   (CustomBackend short names)
    """
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 3:
            server = parts[1].capitalize()
            return f"{server}::{parts[2]}"
    # CustomBackend uses short names (no MCP prefix)
    return f"Kiln::{name}"


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
        self._current_block_type: str = ""  # tracks ContentBlock type for delta routing
        self._context_tokens = 0  # latest API call's total input ~ current context size
        self._last_call_usage = {}  # per-API-call usage from message_delta events
        self._receiving = False
        self._interrupt_in_flight = False
        self._receive_task: asyncio.Task | None = None
        # Permission mode lives on the harness (session config file); TUI
        # provides a property alias.  Trusted mode is a volatile overlay —
        # it only exists in-memory and can only be set from the TUI.
        self._trusted_override = (harness._initial_mode == PermissionMode.TRUSTED)
        self._last_displayed_mode = harness._initial_mode
        self._pending_permission: PermissionRequest | None = None
        self._app: Application | None = None

        # Idle message delivery
        self._auto_delivery_enabled = True
        self._last_auto_delivery: float = 0.0
        self._last_turn_source: str = "user"  # "user" or "agent"
        self._watcher_task: asyncio.Task | None = None

        # Heartbeat: nudge agent after idle period, with exponential backoff.
        # heartbeat_max = cap for exponential backoff; heartbeat_override = fixed interval (no backoff).
        self._heartbeat_enabled = harness.config.heartbeat
        self._heartbeat_max: float = harness.config.heartbeat_max
        self._heartbeat_override: float = harness.config.heartbeat_override
        self._heartbeat_backoff: float = self._HEARTBEAT_BASE_INTERVAL
        self._heartbeat_task: asyncio.Task | None = None
        self._heartbeat_oneshot: float | None = None  # one-shot override (seconds)

        # Idle nudge: send a message after prolonged inactivity.
        self._idle_nudge_timeout: float = harness.config.idle_nudge_timeout
        self._last_real_activity: float = time.monotonic()
        self._idle_nudge_sent: bool = False

        # Steering queue: user input submitted while receiving.
        # Drained mid-turn by the PostToolUse hook (all at once as additionalContext).
        self._steering_queue = harness.steering_queue
        # Followup queue: scripted messages (thread message_queue, session-end prompts).
        # Drained between turns, one at a time, as proper user prompts.
        self._followup_queue = harness.followup_queue

        # Initial send task (startup + --prompt), tracked separately for cancellation
        self._initial_task: asyncio.Task | None = None

        # Resume indicator: prepended to the first user message when resuming a session.
        # Set to True in _main() after start() if the session is a resume/continue.
        self._resume_indicator_pending: bool = False

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
            if text in ("/fquit", "/fq"):
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
            if text == "/usage":
                app_ref._show_usage()
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
            app_ref._steering_queue.append(text)
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
    def _perm_mode(self) -> PermissionMode:
        # Trusted is a volatile TUI-only overlay — when set, it takes
        # precedence over the session config file.  This ensures trusted
        # mode (which disables ALL guardrails) can only be set by a human
        # sitting at the terminal, not by external config writes.
        if self._trusted_override:
            return PermissionMode.TRUSTED
        return self._harness.permission_mode

    @_perm_mode.setter
    def _perm_mode(self, value: PermissionMode) -> None:
        if value == PermissionMode.TRUSTED:
            self._trusted_override = True
        else:
            self._trusted_override = False
            self._harness.permission_mode = value
        self._last_displayed_mode = value

    @property
    def _in_channel_view(self) -> bool:
        return self._current_view.startswith("channel:")

    _MODE_STYLE = {
        PermissionMode.SAFE: "mode-safe",
        PermissionMode.SUPERVISED: "mode-supervised",
        PermissionMode.YOLO: "mode-yolo",
        PermissionMode.TRUSTED: "mode-trusted",
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
        if self._context_tokens > CONTEXT_PAUSE_THRESHOLD:
            parts.append("<err>\u26a0 auto-delivery paused</err>")

        # Pending message count
        if not self._receiving:
            pending = self._pending_message_count()
            if pending:
                parts.append(f"\U0001f4e8 {pending} pending")

        # Queued messages waiting for delivery
        n_queued = len(self._steering_queue) + len(self._followup_queue)
        if n_queued:
            parts.append(f"<tool>{n_queued} queued</tool>")

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
            _tprint(f"  <{style}>{{}} [{{}}]</{style}> {{}}", icon, status, desc)
        tasks = data["tasks"]
        done = sum(1 for t in tasks if t.get("status") == "done")
        _tprint("<dim>Progress: {}/{} done</dim>\n", done, len(tasks))

    def _show_usage(self) -> None:
        """Show subscription usage for all providers."""
        tool_path = self._harness.config.home / "tools" / "core" / "usage"
        if not tool_path.exists():
            _tprint("<err>Usage tool not found.</err>")
            return

        try:
            result = subprocess.run(
                [str(tool_path), "--json"],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode != 0:
                _tprint("<err>Usage fetch failed.</err>")
                return
            data = json.loads(result.stdout)
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
            _tprint("<err>Usage fetch failed.</err>")
            return

        # Anthropic — nested under "anthropic" or flat at top level (legacy)
        anthropic = data.get("anthropic", data if "five_hour" in data else {})
        if anthropic:
            _tprint("\n<text-heading>Anthropic Max \u2014 Usage</text-heading>")
            self._show_usage_limits(anthropic, [
                ("five_hour", "Session (5h)"),
                ("seven_day", "Weekly \u2014 all models"),
                ("seven_day_sonnet", "Weekly \u2014 Sonnet"),
            ])

        # OpenAI/Codex — nested under "openai"
        openai_data = data.get("openai", {})
        rate_limit = openai_data.get("rate_limit", {})
        if rate_limit:
            plan = openai_data.get("plan_type", "unknown").capitalize()
            _tprint(f"\n<text-heading>OpenAI {plan} \u2014 Usage</text-heading>")
            for window_key, label in [("primary_window", "Primary (5h)"), ("secondary_window", "Secondary (7d)")]:
                window = rate_limit.get(window_key)
                if not window:
                    continue
                used = window.get("used_percent", 0)
                ratio = used / 100.0
                filled = int(ratio * 30)
                bar = "\u2588" * filled + "\u2591" * (30 - filled)
                style = "err" if ratio >= 0.9 else "tool" if ratio >= 0.7 else "diff-add"
                _tprint("  <text-heading>{label}</text-heading>", label=label)
                _tprint(f"  <{style}>{{}}</{style}>  {{}}% used", bar, used)
                reset_at = window.get("reset_at")
                if reset_at:
                    try:
                        total_sec = int(reset_at - datetime.now(timezone.utc).timestamp())
                        if total_sec > 0:
                            hours, rem = divmod(total_sec, 3600)
                            minutes = rem // 60
                            if hours > 24:
                                rt = f"resets in {hours // 24}d {hours % 24}h"
                            elif hours > 0:
                                rt = f"resets in {hours}h {minutes}m"
                            else:
                                rt = f"resets in {minutes}m"
                            _tprint("  <dim>{}</dim>", rt)
                    except (ValueError, TypeError):
                        pass
                _tprint("")

    def _show_usage_limits(self, data: dict, limits: list[tuple[str, str]]) -> None:
        """Render Anthropic-style usage limits with progress bars."""
        for key, label in limits:
            limit = data.get(key)
            if not limit or limit.get("utilization") is None:
                continue

            util = limit["utilization"]
            ratio = util / 100.0
            filled = int(ratio * 30)
            bar = "\u2588" * filled + "\u2591" * (30 - filled)

            if ratio >= 0.9:
                style = "err"
            elif ratio >= 0.7:
                style = "tool"
            else:
                style = "diff-add"

            pct = f"{util:.0f}"
            _tprint("  <text-heading>{label}</text-heading>", label=label)
            _tprint(f"  <{style}>{{}}</{style}>  {{}}% used", bar, pct)

            resets_at = limit.get("resets_at")
            if resets_at:
                try:
                    reset = datetime.fromisoformat(resets_at)
                    total_sec = int((reset - datetime.now(timezone.utc)).total_seconds())
                    if total_sec > 0:
                        hours, rem = divmod(total_sec, 3600)
                        minutes = rem // 60
                        if hours > 24:
                            rt = f"resets in {hours // 24}d {hours % 24}h"
                        elif hours > 0:
                            rt = f"resets in {hours}h {minutes}m"
                        else:
                            rt = f"resets in {minutes}m"
                        _tprint("  <dim>{}</dim>", rt)
                except (ValueError, TypeError):
                    pass
            _tprint("")

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

        # Resumed session: show prior conversation history and arm the indicator.
        if getattr(self._harness, "_resume_uuid", None):
            self._render_prior_conversation()
            self._resume_indicator_pending = True

        try:
            with patch_stdout():
                # Start inbox watcher for idle message delivery
                self._watcher_task = asyncio.ensure_future(self._inbox_watcher())

                # Start heartbeat watcher
                self._heartbeat_task = asyncio.ensure_future(self._heartbeat_watcher())

                # Drain initial startup message (orientation or --prompt).
                # The harness populates followup_queue during start().
                async def send_initial():
                    await asyncio.sleep(0)  # yield once to let Application start
                    msg = None
                    if self._followup_queue:
                        msg = self._followup_queue.pop(0)
                    elif self._steering_queue:
                        msg = self._steering_queue.pop(0)
                    if msg:
                        _tprint("<dim>...</dim>")
                        self._receive_task = asyncio.ensure_future(self._send_and_receive(msg))

                if self._followup_queue or self._steering_queue:
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
            # Bypass permissions for unattended exit tasks (summary, archival)
            self._perm_mode = PermissionMode.YOLO
            sc = self._harness.session_control
            skip_summary = getattr(self._harness.config, "ephemeral", False) or (sc and sc.skip_summary)
            if not skip_summary:
                self._harness.prepare_shutdown()
                if self._followup_queue:
                    _tprint("\n<dim>Running session-end protocol...</dim>")
                    try:
                        while self._followup_queue:
                            msg = self._followup_queue.pop(0)
                            await self._harness.send(msg)
                            async for _ in self._harness.receive():
                                pass
                    except Exception:
                        pass

            # Archive and commit even when skipping summary (but not for ephemeral)
            if not getattr(self._harness.config, "ephemeral", False):
                try:
                    archive_path = self._harness.archive_conversation()
                    if archive_path:
                        _tprint("<dim>Archived conversation to {}</dim>", archive_path)
                except Exception:
                    pass
                try:
                    commit_result = self._harness.commit_memory()
                    if commit_result:
                        _tprint("<dim>Git: {}</dim>", commit_result)
                except Exception:
                    pass

            _tprint("<dim>Disconnecting...</dim>")
            await self._harness.stop()

    def _render_prior_conversation(self) -> None:
        """Render prior conversation history for resumed sessions.

        Reads the conversation JSONL from the prior session and prints the last
        few turns to scrollback so the user has context when resuming.
        Shows at most MAX_TURNS turns (user+assistant pairs); older turns are
        indicated by a count but not shown.
        """
        MAX_TURNS = 5
        get_jsonl = getattr(self._harness, "get_prior_conversation_jsonl", None)
        if not get_jsonl:
            return
        jsonl_path = get_jsonl()
        if not jsonl_path:
            return

        # Parse user/assistant messages from JSONL (skip queue-ops, progress, etc.)
        turns: list[tuple[str, str]] = []  # (role, text)
        try:
            with open(jsonl_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    etype = entry.get("type", "")
                    if etype not in ("user", "assistant"):
                        continue
                    msg = entry.get("message", {})
                    role = msg.get("role", "")
                    content = msg.get("content", "")
                    # content may be a string or a list of blocks
                    if isinstance(content, str):
                        text = content.strip()
                    elif isinstance(content, list):
                        parts = []
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                t = block.get("text", "").strip()
                                if t:
                                    parts.append(t)
                        text = "\n".join(parts).strip()
                    else:
                        continue
                    if text and role in ("user", "assistant"):
                        turns.append((role, text))
        except OSError:
            return

        if not turns:
            return

        total_turns = len(turns)
        shown = turns[-MAX_TURNS * 2:]  # last N pairs (each pair = 2 entries)
        skipped = total_turns - len(shown)

        _tprint("<dim>\u2500\u2500\u2500 Prior conversation \u2500\u2500\u2500</dim>")
        if skipped > 0:
            _tprint("<dim>  ({} earlier messages not shown)</dim>", skipped)

        for role, text in shown:
            # Truncate very long messages
            MAX_CHARS = 500
            display = text if len(text) <= MAX_CHARS else text[:MAX_CHARS] + " \u2026"
            if role == "user":
                _tprint("\n<user>You:</user> {}", display)
            else:
                _tprint("\n<assistant>{}:</assistant> {}", self._agent_label, display)

        _tprint("\n<dim>\u2500\u2500\u2500 Resuming \u2500\u2500\u2500\n</dim>")

    async def _send_and_receive(self, text: str, source: str = "user") -> None:
        """Send a message and render the full response."""
        self._stream_chunks = []
        self._thinking_buffer = ""
        self._receiving = True

        try:
            # Inject timestamp + source header so the agent has time and source awareness
            ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            if source == "user":
                write_presence(
                    self._harness.config.home / "state", "terminal",
                    agent_id=self._harness.agent_id,
                )
            if self._resume_indicator_pending and source == "user":
                self._resume_indicator_pending = False
                stamped = f"[{ts} | TERMINAL MESSAGE | trust: always ✓ | Resumed session]\n{text}"
            elif source == "user":
                stamped = f"[{ts} | TERMINAL MESSAGE | trust: always ✓]\n{text}"
            elif source == "agent":
                # Agent/gateway messages build their own [header] in _deliver_agent_message
                # — just prepend the injection timestamp into the existing bracket
                if text.startswith("["):
                    stamped = f"[{ts} | {text[1:]}"
                else:
                    stamped = f"[{ts}]\n{text}"
            else:
                stamped = f"[{ts}]\n{text}"
            await self._harness.send(stamped)

            async for event in self._harness.receive():
                if self._interrupt_in_flight:
                    if isinstance(event, TurnCompleteEvent):
                        break
                    continue
                self._handle_event(event)

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
            if source not in ("heartbeat", "idle-nudge"):
                self._heartbeat_backoff = self._HEARTBEAT_BASE_INTERVAL
                self._last_real_activity = time.monotonic()
                self._idle_nudge_sent = False

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

            # Send next queued message: steering first (user-typed), then followup
            next_msg = None
            if self._steering_queue:
                next_msg = self._steering_queue.pop(0)
            elif self._followup_queue:
                next_msg = self._followup_queue.pop(0)
            if next_msg:
                _tprint("<user>You:</user> {}", next_msg)
                self._receiving = True
                if self._app:
                    self._app.invalidate()
                self._receive_task = asyncio.ensure_future(self._send_and_receive(next_msg))

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
                self._check_external_mode_change()
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
        if self._context_tokens > CONTEXT_PAUSE_THRESHOLD:
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
        marker = msg_path.with_suffix(".read")
        marker.touch()  # write early to prevent concurrent re-queue; removed on failure

        _resolve_live_trust(msg, self._harness.config.home / "state")
        header = format_message_source(msg)
        summary = msg.get("summary", "")
        body = msg.get("body", "")

        _tprint("\n<agent-msg-b>\U0001f4e8 {}:</agent-msg-b>", header)
        if summary and (not body or summary not in body):
            _tprint("<agent-msg>{}</agent-msg>", summary)
        if body:
            display_body = body if len(body) < 2000 else body[:2000] + "\n... (truncated)"
            _tprint("<agent-msg>{}</agent-msg>", display_body)

        model_text = body or summary
        formatted = f"[{header}]\n{model_text}"

        if len(model_text) < 200:
            cooldown = 5.0
        else:
            cooldown = 1.0

        self._receive_task = asyncio.ensure_future(self._send_and_receive(formatted, source="agent"))
        self._receiving = True
        if self._app:
            self._app.invalidate()

        try:
            await self._receive_task
        except Exception:
            marker.unlink(missing_ok=True)
            raise

        self._last_auto_delivery = time.monotonic() + (cooldown - 1.0)

    # ---- Heartbeat ----

    def _sync_heartbeat_from_config(self) -> None:
        """Read heartbeat settings from the per-session runtime config.

        Called periodically by the heartbeat watcher. Picks up changes
        made by the agent (or user) to the session config file.
        """
        sc = self._harness.session_config
        if sc is None:
            return

        enabled = sc.get("heartbeat_enabled")
        if enabled is not None:
            self._heartbeat_enabled = bool(enabled)

        # Override: fixed interval bypassing backoff (0 = disabled)
        override = sc.get("heartbeat_override")
        if override is not None:
            try:
                self._heartbeat_override = float(override)
            except (ValueError, TypeError):
                pass

        # Max: cap for exponential backoff (fall back to legacy heartbeat_interval)
        hb_max = sc.get("heartbeat_max") or sc.get("heartbeat_interval")
        if hb_max is not None:
            try:
                new_max = float(hb_max)
                if new_max > 0 and new_max != self._heartbeat_max:
                    self._heartbeat_max = new_max
                    self._heartbeat_backoff = min(
                        self._heartbeat_backoff, self._heartbeat_max
                    )
            except (ValueError, TypeError):
                pass

    async def _heartbeat_watcher(self) -> None:
        """Nudge the agent after a period of inactivity."""
        while True:
            await asyncio.sleep(10.0)
            if self._receiving or self._pending_permission:
                continue
            if self._input_buffer.text:
                continue

            # --- Idle nudge check ---
            if self._idle_nudge_timeout > 0:
                idle = time.monotonic() - self._last_real_activity
                if idle >= self._idle_nudge_timeout and not self._idle_nudge_sent:
                    # Send idle nudge (once per idle period)
                    self._idle_nudge_sent = True
                    minutes = int(idle / 60)
                    ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
                    msg = (
                        f"[{ts}]\n"
                        f"<system-reminder>\n"
                        f"(idle-nudge) You've been idle for {minutes} minutes with no real activity.\n"
                        f"</system-reminder>"
                    )
                    _tprint("\n<dim>\u23f0 Idle nudge ({} min inactive)</dim>", minutes)
                    self._receive_task = asyncio.ensure_future(
                        self._send_and_receive(msg, source="idle-nudge"))
                    self._receiving = True
                    if self._app:
                        self._app.invalidate()
                    await self._receive_task
                    continue

            # --- Regular heartbeat ---
            self._sync_heartbeat_from_config()
            if not self._heartbeat_enabled:
                continue
            if self._context_tokens > CONTEXT_PAUSE_THRESHOLD:
                continue
            elapsed = time.monotonic() - self._last_auto_delivery

            # Priority: oneshot > override > backoff
            if self._heartbeat_oneshot:
                interval = self._heartbeat_oneshot
            elif self._heartbeat_override > 0:
                interval = self._heartbeat_override
            else:
                interval = self._heartbeat_backoff

            if elapsed < interval:
                continue

            self._heartbeat_oneshot = None

            ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            msg = f"[{ts}] (heartbeat)"
            minutes = int(elapsed / 60)
            _tprint("\n<dim>\u2764\ufe0f Heartbeat ({} idle)</dim>", f"{minutes}min")

            self._receive_task = asyncio.ensure_future(self._send_and_receive(msg, source="heartbeat"))
            self._receiving = True
            if self._app:
                self._app.invalidate()
            await self._receive_task

            # Only increase backoff when not using override
            if self._heartbeat_override <= 0:
                self._heartbeat_backoff = min(
                    self._heartbeat_backoff * 2, self._heartbeat_max
                )

    def _toggle_heartbeat(self, text: str) -> None:
        """Handle /heartbeat command."""
        parts = text.split()
        if len(parts) >= 2:
            try:
                minutes = float(parts[1])
                self._heartbeat_override = minutes * 60
                self._heartbeat_enabled = True
                _tprint("<dim>Heartbeat: on, fixed {}min (no backoff)</dim>", int(minutes))
            except ValueError:
                if parts[1] == "off":
                    self._heartbeat_enabled = False
                    _tprint("<dim>Heartbeat: off</dim>")
                elif parts[1] == "on":
                    self._heartbeat_enabled = True
                    if self._heartbeat_override > 0:
                        _tprint("<dim>Heartbeat: on, fixed {}min</dim>",
                                int(self._heartbeat_override / 60))
                    else:
                        _tprint("<dim>Heartbeat: on, backoff up to {}min</dim>",
                                int(self._heartbeat_max / 60))
                elif parts[1] == "backoff":
                    # Switch back to backoff mode
                    self._heartbeat_override = 0.0
                    self._heartbeat_backoff = self._HEARTBEAT_BASE_INTERVAL
                    self._heartbeat_enabled = True
                    _tprint("<dim>Heartbeat: on, backoff up to {}min</dim>",
                            int(self._heartbeat_max / 60))
                else:
                    _tprint("<dim>Usage: /heartbeat [on|off|backoff|<minutes>]</dim>")
        else:
            self._heartbeat_enabled = not self._heartbeat_enabled
            state = "on" if self._heartbeat_enabled else "off"
            if self._heartbeat_override > 0:
                _tprint("<dim>Heartbeat: {} (fixed {}min)</dim>",
                        state, int(self._heartbeat_override / 60))
            else:
                _tprint("<dim>Heartbeat: {} (backoff up to {}min)</dim>",
                        state, int(self._heartbeat_max / 60))
        # Persist to session config so changes survive and the agent can see them.
        sc = self._harness.session_config
        if sc is not None:
            sc.update({
                "heartbeat_enabled": self._heartbeat_enabled,
                "heartbeat_max": self._heartbeat_max,
                "heartbeat_override": self._heartbeat_override,
            })

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
            elif etype == "hook_fired":
                hook_name = ev.get("hook", "?")
                decision = ev.get("decision", "")
                ctx_preview = ev.get("context", "")
                reason = ev.get("reason", "")
                updated = ev.get("updated_output", False)
                if decision == "block":
                    msg = f"  \u26a1 hook:{hook_name} \u2192 BLOCKED"
                    if reason:
                        msg += f" ({reason})"
                    _tprint("\n<hook-block>{}</hook-block>", msg)
                elif decision:
                    _tprint("\n<hook>  \u26a1 hook:{} \u2192 {}</hook>", hook_name, decision)
                elif ctx_preview:
                    _tprint("\n<hook>  \u26a1 hook:{} \u2192 {}</hook>", hook_name, ctx_preview)
                elif updated:
                    _tprint("\n<hook>  \u26a1 hook:{} \u2192 (output replaced)</hook>", hook_name)

    def _handle_event(self, event: Event) -> None:
        """Route an incoming Kiln Event to the appropriate handler."""
        self._drain_ui_events()

        if isinstance(event, ContentBlockStartEvent):
            self._current_block_type = event.content_type

        elif isinstance(event, ContentBlockEndEvent):
            self._current_block_type = ""

        elif isinstance(event, ContentBlockDeltaEvent):
            # Suppress text deltas during tool_call blocks — those are argument
            # JSON, not assistant text. The ToolCallEvent has the parsed args.
            if self._current_block_type == "tool_call":
                pass
            elif event.text is not None:
                if not self._stream_chunks:
                    self._commit_thinking()
                self._stream_chunks.append(event.text)
            elif event.thinking is not None:
                self._on_stream_thinking(event.thinking)

        elif isinstance(event, TextEvent):
            # Final complete text — fallback if streaming didn't deliver
            if not self._stream_chunks:
                self._stream_chunks.append(event.text)

        elif isinstance(event, ThinkingEvent):
            # Final complete thinking block (if not streamed via deltas)
            if not self._thinking_buffer:
                self._thinking_buffer = event.text

        elif isinstance(event, ToolCallEvent):
            self._tool_name_queue.append(event.name)
            self._on_tool_call_start(event.name, event.input)

        elif isinstance(event, ToolResultEvent):
            tool_name = self._tool_name_queue.pop(0) if self._tool_name_queue else ""
            self._on_tool_call_result(
                tool_name, event.output, event.is_error,
            )

        elif isinstance(event, TurnCompleteEvent):
            if event.model:
                warning = self._harness.check_model(event.model)
                if warning:
                    _tprint("\n<err-b>Warning:</err-b> <err>{}</err>", warning)
            if event.session_id and not self._harness.session_id:
                self._harness.session_id = event.session_id
                self._harness.register_session()
            if event.usage:
                self._last_call_usage = {
                    "input_tokens": event.usage.input_tokens,
                    "output_tokens": event.usage.output_tokens,
                    "cache_read_input_tokens": event.usage.cache_read_tokens or 0,
                    "cache_creation_input_tokens": event.usage.cache_write_tokens or 0,
                }
                self._context_tokens = (
                    event.usage.input_tokens
                    + (event.usage.cache_read_tokens or 0)
                    + (event.usage.cache_write_tokens or 0)
                )
                if self._harness.session_control:
                    self._harness.session_control.context_tokens = self._context_tokens
            self._on_turn_complete_event(event)

        elif isinstance(event, UsageUpdateEvent):
            if event.usage:
                self._context_tokens = (
                    event.usage.input_tokens
                    + (event.usage.cache_read_tokens or 0)
                    + (event.usage.cache_write_tokens or 0)
                )
                if self._harness.session_control:
                    self._harness.session_control.context_tokens = self._context_tokens
                if self._app:
                    self._app.invalidate()

        elif isinstance(event, ErrorEvent):
            _tprint("\n<err-b>Error:</err-b> <err>{}</err>", event.message)

        elif isinstance(event, SystemMessageEvent):
            if event.subtype not in ("init",):
                _tprint("<dim-i>System: {}</dim-i>", event.subtype)

    # ---- Permissions ----


    def _check_external_mode_change(self) -> None:
        """Detect and announce external permission mode changes.

        Mode is now stored in the session config file, so external tools
        (gateway control channel, scripts) can change it directly.  This
        method detects the change and refreshes the TUI display.
        """
        current = self._perm_mode
        if current != self._last_displayed_mode:
            self._last_displayed_mode = current
            _tprint("<dim>Mode changed to {} (external)</dim>", current.value)
            if self._app:
                self._app.invalidate()

    async def _request_permission(self, req: PermissionRequest) -> bool:
        """Display diff/preview and wait for user y/n decision.

        Polls for external mode overrides every 2s so that a remote mode
        change (e.g. via Discord) can unblock a pending permission request.
        """
        self._commit_stream()
        self._commit_thinking()

        self._render_permission_prompt(req)

        self._pending_permission = req
        if self._app:
            self._app.invalidate()

        try:
            deadline = asyncio.get_event_loop().time() + 300
            while not req.event.is_set():
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    req.timed_out = True
                    req.decide(False)
                    _tprint("<dim>Permission request timed out (5 min). Auto-rejected.</dim>")
                    break
                self._check_external_mode_change()
                if self._perm_mode == PermissionMode.TRUSTED:
                    req.decide(True)
                    _tprint("<dim>    auto-approved (mode switched to trusted)</dim>")
                    break
                if self._perm_mode == PermissionMode.YOLO and not req.is_guardrail:
                    req.decide(True)
                    _tprint("<dim>    auto-approved (mode switched to yolo)</dim>")
                    break
                try:
                    await asyncio.wait_for(req.event.wait(), timeout=min(2.0, remaining))
                except asyncio.TimeoutError:
                    continue
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

    def _on_turn_complete_event(self, event: TurnCompleteEvent) -> None:
        """Render turn completion stats."""
        self._commit_stream()
        self._commit_thinking()

        parts = []
        if event.usage:
            total = event.usage.total_tokens or (
                event.usage.input_tokens + event.usage.output_tokens
            )
            parts.append(f"{_fmt_tokens(total)} tokens")
            cached = event.usage.cache_read_tokens
            if cached:
                parts.append(f"{_fmt_tokens(cached)} cached")
        if event.stop_reason and event.stop_reason != "stop":
            parts.append(f"stop: {event.stop_reason}")
        summary = "  |  ".join(parts) if parts else "done"
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

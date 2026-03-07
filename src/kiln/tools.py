"""In-process MCP tools for the Kiln agent runtime."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import yaml
from claude_agent_sdk import create_sdk_mcp_server, tool

from .shell import PersistentShell, safe_getcwd


# ---------------------------------------------------------------------------
# Shared file state (populated by Read PostToolUse hook, consumed by Edit/Write)
# ---------------------------------------------------------------------------

class FileState:
    """Track which files have been read and their state at read time.

    This replaces Claude Code's internal readFileState for our MCP tools.
    Populated by: MCP Read (directly) and built-in Read PostToolUse hook.
    Consumed by: MCP Edit and MCP Write for "must read first" and
    "modified since read" validations.
    """

    def __init__(self):
        self._state: dict[str, dict] = {}

    def record_read(self, file_path: str, *, partial: bool = False) -> None:
        """Record that a file was read. Call from the Read PostToolUse hook."""
        normalized = str(Path(file_path).resolve())
        try:
            mtime = os.path.getmtime(normalized)
        except OSError:
            mtime = 0.0
        self._state[normalized] = {
            "timestamp": mtime,
            "partial": partial,
        }

    def record_write(self, file_path: str) -> None:
        """Update state after a successful write/edit."""
        normalized = str(Path(file_path).resolve())
        try:
            mtime = os.path.getmtime(normalized)
        except OSError:
            mtime = 0.0
        self._state[normalized] = {
            "timestamp": mtime,
            "partial": False,
        }

    def check(self, file_path: str) -> tuple[bool, str | None]:
        """Validate that a file can be written/edited.

        Returns (ok, error_message). If ok is True, the operation can proceed.
        """
        normalized = str(Path(file_path).resolve())

        if not os.path.exists(normalized):
            # New file — no read required
            return True, None

        entry = self._state.get(normalized)
        if not entry:
            return False, "File has not been read yet. Read it first before writing to it."

        try:
            current_mtime = os.path.getmtime(normalized)
        except OSError:
            return True, None  # Can't check, allow it

        if current_mtime > entry["timestamp"]:
            return False, (
                "File has been modified since read, either by the user or "
                "by a linter. Read it again before attempting to write it."
            )

        return True, None


# ---------------------------------------------------------------------------
# MCP server factory
# ---------------------------------------------------------------------------

class SessionControl:
    """Shared state for session lifecycle signals between MCP tools and the TUI.

    Also carries context usage data (updated by the TUI, read by hooks).
    """

    def __init__(self, *, ephemeral: bool = False):
        self.ephemeral = ephemeral
        self.quit_requested = False
        self.skip_summary = False  # when True, skip session summary on exit
        self.continue_requested = False  # when True, restart as new session after exit
        self.handoff_text: str | None = None  # handoff text for self-continuation
        self.context_tokens: int = 0  # updated by TUI from API usage data


def create_mcp_server(
    inbox_root: Path,
    skills_path: Path,
    agent_id: str,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    file_state: FileState | None = None,
    session_control: SessionControl | None = None,
    plans_path: Path | None = None,
    worklog_path: Path | None = None,
):
    """Create the Kiln MCP server with agent runtime tools.

    Returns:
        Tuple of (server, cleanup_coro_fn, get_shell_cwd) where cleanup_coro_fn
        is an async callable that shuts down the persistent shell, and
        get_shell_cwd returns the shell's current working directory (or the
        initial cwd if the shell hasn't been used yet).

    Args:
        inbox_root: Root inbox directory (e.g. ~/.<agent>/inbox/).
        skills_path: Skills directory (e.g. ~/.<agent>/skills/).
        agent_id: This agent's ID (used for channel subscriptions).
        cwd: Initial working directory for the persistent shell.
        env: Environment variable overrides for the persistent shell.
        file_state: Shared FileState for Read/Edit/Write coordination.
        session_control: Shared session lifecycle state (for exit_session tool).
        plans_path: Directory for agent plan files (e.g. ~/.<agent>/plans/).
        worklog_path: Path to this session's worklog file. If None, derived from agent_id.
    """
    if file_state is None:
        file_state = FileState()

    # Lazily initialized on first Bash call
    shell: PersistentShell | None = None

    async def cleanup():
        nonlocal shell
        if shell is not None:
            await shell.close()
            shell = None

    def get_shell_cwd() -> str:
        """Return the shell's current working directory.

        Before the shell is initialized, returns the initial cwd.
        """
        if shell is not None:
            return shell.cwd
        return cwd or safe_getcwd()

    # ------------------------------------------------------------------
    # Bash tool
    # ------------------------------------------------------------------

    # Worklog for capturing cognitive state during sessions
    if worklog_path is None:
        agent_home = inbox_root.parent
        worklog_path = agent_home / "memory" / "worklogs" / f"worklog-{agent_id}.md"

    def _append_worklog(tag: str, text: str) -> None:
        """Append a timestamped entry to the session worklog."""
        if session_control and session_control.ephemeral:
            return
        ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        worklog_path.parent.mkdir(parents=True, exist_ok=True)
        with open(worklog_path, "a") as f:
            f.write(f"[{ts}] ({tag}) {text}\n")

    _THINKING_DESC = (
        "What you're doing and why — a sentence or two about "
        "your current reasoning. Captured to your session worklog."
    )

    @tool(
        "Bash",
        "Executes a bash command in a persistent shell. Environment variables, "
        "working directory, and other state persist between calls.",
        {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The command to execute",
                },
                "thinking": {
                    "type": "string",
                    "description": _THINKING_DESC,
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
            # command is required for normal execution and run_in_background, but
            # NOT when using background_job_id or cleanup_background_job_id (status
            # checks don't need a new command). Omitting from required so the model
            # can call those paths without providing a dummy command.
        },
    )
    async def bash_tool(args: dict) -> dict:
        nonlocal shell
        if shell is None:
            shell = PersistentShell(cwd=cwd, env=env)

        command = args.get("command", "")
        timeout_ms = args.get("timeout", 120_000)

        # Capture thinking to worklog
        thinking = args.get("thinking")
        if thinking:
            _append_worklog("tool", thinking.strip())

        # Background job cleanup — remove temp files for a finished job
        cleanup_job_id = args.get("cleanup_background_job_id")
        if cleanup_job_id:
            await shell.cleanup_background(cleanup_job_id)
            return _ok(f"Background job {cleanup_job_id} cleaned up.")

        # Background job check — returns status of a previously started job
        bg_job_id = args.get("background_job_id")
        if bg_job_id:
            result = await shell.check_background(bg_job_id)
            status = "running" if result["running"] else "finished"
            parts = [f"[Background job {bg_job_id}] Status: {status}"]
            if result["exit_code"] is not None:
                parts.append(f"Exit code: {result['exit_code']}")
            if result["output"]:
                parts.append(result["output"])
            return _ok("\n".join(parts))

        if not command.strip():
            return {
                "content": [{"type": "text", "text": "Error: no command provided."}],
                "isError": True,
            }

        # Background execution — start and return immediately
        if args.get("run_in_background"):
            result = await shell.run_background(command)
            return _ok(
                f"Background job started.\n"
                f"Job ID: {result['job_id']}\n"
                f"PID: {result['pid']}\n\n"
                f"Use background_job_id={result['job_id']!r} to check status."
            )

        result = await shell.run(command, timeout_ms=timeout_ms)

        # Format output to include metadata
        parts = []
        if result["output"].strip():
            parts.append(result["output"].rstrip())

        # Status line
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

        text = "\n".join(parts)
        return {"content": [{"type": "text", "text": text}]}

    # ------------------------------------------------------------------
    # Read tool (MCP replacement for built-in, text files only)
    # ------------------------------------------------------------------

    MAX_LINES = 2000
    MAX_LINE_LEN = 2000


    @tool(
        "Read",
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
        "- For reading images (PNG, JPG, etc.), PDFs, or Jupyter notebooks, use "
        "the built-in Read tool instead.",
        {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "The absolute path to the file to read",
                },
                "thinking": {
                    "type": "string",
                    "description": _THINKING_DESC,
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
            },
            "required": ["file_path"],
        },
    )
    async def read_tool(args: dict) -> dict:
        thinking = args.get("thinking")
        if thinking:
            _append_worklog("tool", thinking.strip())
        file_path = args.get("file_path", "")
        offset = args.get("offset")
        limit = args.get("limit")

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

        # Reject binary files
        ext = Path(normalized).suffix.lower().lstrip(".")
        if ext in _BINARY_EXTENSIONS:
            return _error(
                f"This tool cannot read binary files. The file appears to be "
                f"a binary .{ext} file. Please use appropriate tools for "
                f"binary file analysis."
            )

        # For images/PDFs/notebooks, tell model to use built-in Read
        if ext in _MEDIA_EXTENSIONS:
            return _error(
                f"This tool handles text files only. For .{ext} files, use "
                f"the built-in Read tool instead."
            )

        # Read the file
        try:
            raw = Path(normalized).read_text(errors="replace")
        except OSError as e:
            return _error(f"Failed to read file: {e}")

        # Strip control characters that are invalid in XML (the Claude Code
        # SDK passes tool results through XML processing). Valid XML chars
        # are: \t \n \r and U+0020+. Form feeds (from pdftotext) and other
        # C0 controls cause "not well-formed (invalid token)" errors that
        # crash the session.
        raw = raw.translate(
            {c: None for c in range(32) if c not in (9, 10, 13)}
        )

        lines = raw.split("\n")
        # Remove trailing empty string from split if file ends with newline
        if lines and lines[-1] == "":
            lines = lines[:-1]

        total_lines = len(lines)

        # Handle empty file
        if total_lines == 0:
            file_state.record_read(normalized, partial=False)
            return _ok(
                "<system-reminder>Warning: the file exists but the contents "
                "are empty.</system-reminder>"
            )

        # Apply offset (1-indexed) and limit
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

        # Slice lines (convert 1-indexed start to 0-indexed)
        selected = lines[start - 1 : start - 1 + max_lines]

        # Format as cat -n output
        output_lines = []
        for i, line in enumerate(selected, start=start):
            # Truncate long lines
            if len(line) > MAX_LINE_LEN:
                line = line[:MAX_LINE_LEN]
            output_lines.append(f"{i:>6}\t{line}")

        file_state.record_read(normalized, partial=partial)
        return _ok("\n".join(output_lines))

    # ------------------------------------------------------------------
    # Edit tool (MCP replacement for built-in)
    # ------------------------------------------------------------------

    @tool(
        "Edit",
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
        "instance.",
        {
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
                    "description": (
                        "The text to replace it with "
                        "(must be different from old_string)"
                    ),
                },
                "thinking": {
                    "type": "string",
                    "description": _THINKING_DESC,
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace all occurrences of old_string (default false)",
                    "default": False,
                },
            },
            "required": ["file_path", "old_string", "new_string"],
        },
    )
    async def edit_tool(args: dict) -> dict:
        thinking = args.get("thinking")
        if thinking:
            _append_worklog("tool", thinking.strip())
        file_path = args.get("file_path", "")
        old_string = args.get("old_string", "")
        new_string = args.get("new_string", "")
        replace_all = args.get("replace_all", False)

        if not file_path:
            return _error("No file_path provided.")

        normalized = str(Path(file_path).resolve())

        # File must exist for Edit
        if not os.path.exists(normalized):
            return _error(f"File does not exist: {file_path}")

        # Must have been read first
        ok, err = file_state.check(normalized)
        if not ok:
            return _error(err)

        # Read current content
        try:
            content = Path(normalized).read_text()
        except OSError as e:
            return _error(f"Failed to read file: {e}")

        # Strip trailing newlines from old_string for matching
        # (matches built-in Edit behavior)
        match_string = old_string.rstrip("\n")

        if not match_string and not old_string:
            # Empty old_string with empty new_string = no-op
            if not new_string:
                return _error(
                    "Original and edited file match exactly. Failed to apply edit."
                )
            # Empty old_string = prepend/insert (built-in behavior for creation)
            new_content = new_string
        else:
            # Count occurrences
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
                # Replace first occurrence only
                new_content = content.replace(match_string, new_string, 1)

        if new_content == content:
            return _error(
                "Original and edited file match exactly. Failed to apply edit."
            )

        # Write the file back
        try:
            _write_file(normalized, new_content)
        except OSError as e:
            return _error(f"Failed to write file: {e}")

        file_state.record_write(normalized)
        return _ok(f"The file {file_path} has been updated successfully.")

    # ------------------------------------------------------------------
    # Write tool (MCP replacement for built-in)
    # ------------------------------------------------------------------

    @tool(
        "Write",
        "Write a file to the local filesystem. Overwrites the file if it "
        "already exists.\n\n"
        "Usage:\n"
        "- If the file already exists, you must Read it first. The tool will "
        "fail if you haven't.\n"
        "- Prefer editing existing files over creating new ones.\n"
        "- NEVER create documentation files (*.md) or README files unless "
        "explicitly requested by the User.\n"
        "- Only use emojis if the user explicitly requests it. Avoid writing "
        "emojis to files unless asked.",
        {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": (
                        "The absolute path to the file to write "
                        "(must be absolute, not relative)"
                    ),
                },
                "content": {
                    "type": "string",
                    "description": "The content to write to the file",
                },
                "thinking": {
                    "type": "string",
                    "description": _THINKING_DESC,
                },
            },
            "required": ["file_path", "content"],
        },
    )
    async def write_tool(args: dict) -> dict:
        thinking = args.get("thinking")
        if thinking:
            _append_worklog("tool", thinking.strip())
        file_path = args.get("file_path", "")
        content = args.get("content", "")

        if not file_path:
            return _error("No file_path provided.")

        normalized = str(Path(file_path).resolve())
        is_new = not os.path.exists(normalized)

        # For existing files, must have been read first
        if not is_new:
            ok, err = file_state.check(normalized)
            if not ok:
                return _error(err)

        # Create parent directories if needed
        parent = Path(normalized).parent
        parent.mkdir(parents=True, exist_ok=True)

        # Write the file
        try:
            _write_file(normalized, content)
        except OSError as e:
            return _error(f"Failed to write file: {e}")

        file_state.record_write(normalized)

        if is_new:
            return _ok(f"File created successfully at: {file_path}")
        return _ok(f"The file {file_path} has been updated successfully.")

    # ------------------------------------------------------------------
    # activate_skill tool
    # ------------------------------------------------------------------

    @tool(
        "activate_skill",
        "Activate a skill by name. Loads the skill's instructions as system-level "
        "context for the remainder of the session. Use this when your task calls for "
        "a specific skill listed in your session context.",
        {"name": str},
    )
    async def activate_skill(args: dict) -> dict:
        name = args["name"]
        skill_md = skills_path / name / "SKILL.md"

        if not skill_md.exists():
            return {
                "content": [{"type": "text", "text": f"Error: skill '{name}' not found."}],
                "isError": True,
            }

        content = skill_md.read_text()

        # Strip YAML frontmatter — the model doesn't need the metadata
        if content.startswith("---"):
            try:
                end = content.index("---", 3)
                content = content[end + 3:].strip()
            except ValueError:
                pass  # malformed frontmatter, return as-is

        return {"content": [{"type": "text", "text": content}]}

    # ------------------------------------------------------------------
    # message tool (point-to-point + channels)
    # ------------------------------------------------------------------

    channels_path = inbox_root.parent / "channels.json"
    channels_dir = inbox_root.parent / "channels"

    def _append_channel_history(channel: str, sender: str, summary: str,
                                body: str, priority: str) -> None:
        """Append a message to the channel's persistent history log."""
        history_dir = channels_dir / channel
        history_dir.mkdir(parents=True, exist_ok=True)
        history_file = history_dir / "history.jsonl"
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "from": sender,
            "summary": summary,
            "body": body,
            "priority": priority,
        }
        with open(history_file, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def _read_channels() -> dict:
        """Read the channel registry. Returns {channel_name: [agent_ids]}."""
        if not channels_path.exists():
            return {}
        try:
            return json.loads(channels_path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}

    def _write_channels(channels: dict) -> None:
        """Write the channel registry with file locking."""
        import fcntl
        channels_path.parent.mkdir(parents=True, exist_ok=True)
        with open(channels_path, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0)
            f.truncate()
            f.write(json.dumps(channels, indent=2) + "\n")

    def _send_one(recipient: str, sender: str, summary: str, body: str,
                  priority: str, channel: str | None = None) -> Path:
        """Send a single message to a recipient's inbox. Returns the message path."""
        import uuid as _uuid
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

    @tool(
        "message",
        "Send messages to agents and manage channel subscriptions.\n\n"
        "Actions:\n"
        "- **send**: Send a message to an agent (via `to`) or broadcast to a "
        "channel (via `channel`). Requires `summary` and `body`.\n"
        "- **subscribe**: Subscribe to a channel to receive all messages sent to it.\n"
        "- **unsubscribe**: Unsubscribe from a channel.",
        {
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
        },
    )
    async def message_tool(args: dict) -> dict:
        action = args.get("action")

        if action == "subscribe":
            channel = args.get("channel")
            if not channel:
                return _error("subscribe requires a channel name.")

            import fcntl
            channels_path.parent.mkdir(parents=True, exist_ok=True)
            with open(channels_path, "a+") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                f.seek(0)
                try:
                    channels = json.loads(f.read() or "{}")
                except json.JSONDecodeError:
                    channels = {}

                subs = channels.get(channel, [])
                if agent_id in subs:
                    return _ok(f"Already subscribed to channel '{channel}'.")
                subs.append(agent_id)
                channels[channel] = subs

                f.seek(0)
                f.truncate()
                f.write(json.dumps(channels, indent=2) + "\n")

            return _ok(f"Subscribed to channel '{channel}'. {len(subs)} subscriber(s).")

        elif action == "unsubscribe":
            channel = args.get("channel")
            if not channel:
                return _error("unsubscribe requires a channel name.")

            import fcntl
            channels_path.parent.mkdir(parents=True, exist_ok=True)
            with open(channels_path, "a+") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                f.seek(0)
                try:
                    channels = json.loads(f.read() or "{}")
                except json.JSONDecodeError:
                    channels = {}

                subs = channels.get(channel, [])
                if agent_id not in subs:
                    return _ok(f"Not subscribed to channel '{channel}'.")
                subs.remove(agent_id)
                if subs:
                    channels[channel] = subs
                else:
                    del channels[channel]

                f.seek(0)
                f.truncate()
                f.write(json.dumps(channels, indent=2) + "\n")

            return _ok(f"Unsubscribed from channel '{channel}'.")

        elif action == "send":
            summary = args.get("summary", "")
            body = args.get("body", "")
            priority = args.get("priority", "normal")

            if not summary and not body:
                return _error("send requires at least a summary or body.")

            to = args.get("to")
            channel = args.get("channel")

            if not to and not channel:
                return _error("send requires either 'to' (agent ID) or 'channel'.")

            # Point-to-point
            if to:
                msg_path = _send_one(to, agent_id, summary, body, priority)
                return _ok(f"Message sent to {to} at {msg_path}")

            # Channel broadcast
            channels = _read_channels()
            subs = channels.get(channel, [])
            recipients = [s for s in subs if s != agent_id]

            if not recipients:
                return _error(f"Channel '{channel}' has no other subscribers.")

            for recipient in recipients:
                _send_one(recipient, agent_id, summary, body, priority, channel=channel)

            # Persist to channel history
            _append_channel_history(channel, agent_id, summary, body, priority)

            return _ok(
                f"Message broadcast to channel '{channel}' "
                f"({len(recipients)} recipient(s))."
            )

        else:
            return _error(f"Unknown action: {action}. Use send, subscribe, or unsubscribe.")

    # ------------------------------------------------------------------
    # exit_session tool (ephemeral agents only)
    # ------------------------------------------------------------------

    @tool(
        "exit_session",
        "Exit the current session cleanly. The harness will handle session "
        "summaries and memory commits before shutdown.\n\n"
        "Appropriate uses:\n"
        "- Ephemeral agents that have completed their task\n"
        "- Autonomous agents handing off to a continuation\n\n"
        "Set `skip_summary` to true when doing autonomous self-continuation — "
        "the summary protocol is redundant overhead.\n\n"
        "Set `continue` to true for self-continuation: the harness will run "
        "the normal shutdown (summary, volatile update, commit), then "
        "automatically launch a fresh session. If the current session is "
        "canonical, the new session inherits canonical status.\n\n"
        "Use `handoff` to pass context to the continuation session. The text "
        "will be delivered as an inbox message in the new session — no need "
        "to write handoff.md manually.\n\n"
        "Do NOT use in interactive sessions — let the user decide when to end "
        "the conversation. If you're unsure whether you're running autonomously, "
        "you're not.",
        {
            "type": "object",
            "properties": {
                "skip_summary": {
                    "type": "boolean",
                    "description": (
                        "Skip the session summary and memory update protocol. "
                        "Use when handing off to a continuation session."
                    ),
                    "default": False,
                },
                "continue": {
                    "type": "boolean",
                    "description": (
                        "Self-continuation: after clean shutdown, automatically "
                        "launch a new session that picks up from the handoff. "
                        "The new session inherits canonical status and runs in "
                        "yolo mode with heartbeat enabled."
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
        },
    )
    async def exit_session_tool(args: dict) -> dict:
        if session_control is None:
            return _error("No session control available.")

        session_control.quit_requested = True
        if args.get("skip_summary", False):
            session_control.skip_summary = True
        if args.get("continue", False):
            session_control.continue_requested = True
        handoff = args.get("handoff", "").strip()
        if handoff:
            session_control.handoff_text = handoff

        return _ok(
            "Session exit requested. Stop making tool calls — "
            "the session will end after this turn completes."
        )

    # ------------------------------------------------------------------
    # plan tool — externalized task planning
    # ------------------------------------------------------------------

    _VALID_STATUSES = {"pending", "in_progress", "done"}

    def _plan_file() -> Path:
        """Path to this agent's plan file."""
        root = plans_path or inbox_root.parent / "plans"
        return root / f"{agent_id}.yml"

    def _format_plan(data: dict) -> str:
        """Format a plan dict as readable text for injection into context."""
        lines = [f"Goal: {data.get('goal', '(none)')}"]
        tasks = data.get("tasks", [])
        for t in tasks:
            status = t.get("status", "pending")
            desc = t.get("description", "")
            lines.append(f"  [{status}] {desc}")
        done = sum(1 for t in tasks if t.get("status") == "done")
        lines.append(f"Progress: {done}/{len(tasks)} done.")
        return "\n".join(lines)

    @tool(
        "plan",
        "Create or update your working plan. Use this to externalize your task "
        "breakdown before starting complex work. Each call replaces the entire "
        "plan — include all tasks, not just changes.\n\n"
        "Call this tool to:\n"
        "- Break down a complex task before starting\n"
        "- Mark tasks as done as you complete them\n"
        "- Adjust the plan when requirements change\n\n"
        "Your plan is stored on the filesystem and visible to coordinators "
        "and other agents.",
        {
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
        },
    )
    async def plan_tool(args: dict) -> dict:
        goal = args.get("goal", "")
        tasks = args.get("tasks", [])

        if not goal:
            return _error("A goal is required.")
        if not tasks:
            return _error("At least one task is required.")

        # Validate statuses
        for t in tasks:
            if t.get("status") not in _VALID_STATUSES:
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

        plan_path = _plan_file()
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text(yaml.dump(plan_data, default_flow_style=False, sort_keys=False))

        formatted = _format_plan(plan_data)
        return _ok(f"Plan updated.\n\n{formatted}")

    server = create_sdk_mcp_server(
        name="kiln",
        version="0.2.0",
        tools=[bash_tool, read_tool, edit_tool, write_tool, activate_skill,
               message_tool, exit_session_tool, plan_tool],
    )
    return server, cleanup, get_shell_cwd


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ok(text: str) -> dict:
    """Return a success MCP tool result."""
    return {"content": [{"type": "text", "text": text}]}


def _error(text: str) -> dict:
    """Return an error MCP tool result."""
    return {
        "content": [{"type": "text", "text": text}],
        "isError": True,
    }


# Extensions that should be rejected outright (binary files)
_BINARY_EXTENSIONS = frozenset([
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

# Extensions that our MCP Read can't handle — redirect to built-in Read
_MEDIA_EXTENSIONS = frozenset([
    "png", "jpg", "jpeg", "gif", "webp",  # images
    "pdf",                                  # PDFs
    "ipynb",                                # Jupyter notebooks
])


def _write_file(path: str, content: str) -> None:
    """Write content to a file, preserving permissions on existing files.

    Detects encoding of existing files and preserves it.
    """
    p = Path(path)

    # Preserve permissions of existing files
    existing_mode = None
    if p.exists():
        existing_mode = p.stat().st_mode

    p.write_text(content)

    if existing_mode is not None:
        os.chmod(path, existing_mode)

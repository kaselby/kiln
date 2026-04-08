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

import json
import os
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

MEDIA_EXTENSIONS = frozenset([
    "png", "jpg", "jpeg", "gif", "webp",
    "pdf",
    "ipynb",
])

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
) -> dict:
    """Read a text file and return formatted MCP result (cat -n format)."""
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

    if ext in MEDIA_EXTENSIONS:
        return _error(
            f"This tool handles text files only. For .{ext} files, use "
            f"the built-in Read tool instead."
        )

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
    "- For reading images (PNG, JPG, etc.), PDFs, or Jupyter notebooks, use "
    "the built-in Read tool instead."
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

    channels_path = inbox_root.parent / "channels.json"
    channels_dir = inbox_root.parent / "channels"

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
        import fcntl
        action = args.get("action")

        if action == "subscribe":
            channel = args.get("channel")
            if not channel:
                return _error("subscribe requires a channel name.")
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
            result = do_send_message(
                inbox_root, agent_id,
                summary=args.get("summary", ""),
                body=args.get("body", ""),
                priority=args.get("priority", "normal"),
                to=args.get("to"),
                channel=args.get("channel"),
                channels_path=channels_path,
                channels_dir=channels_dir,
            )
            if "error" in result:
                return _error(result["error"])
            return _ok(result["result"])

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

    server = create_sdk_mcp_server(
        name="kiln",
        version="0.2.0",
        tools=[bash_tool, read_tool, edit_tool, write_tool, activate_skill_tool,
               message_tool, exit_session_tool, plan_tool],
    )
    return server, cleanup, get_shell_cwd

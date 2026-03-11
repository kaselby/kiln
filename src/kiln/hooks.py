"""Infrastructure hook callbacks — message delivery, read tracking, context warnings, and usage logging."""

import json
import os
import re
import subprocess
from datetime import date, datetime
from pathlib import Path

import yaml

from claude_agent_sdk import (
    HookContext,
    HookInput,
    HookJSONOutput,
)


def create_inbox_check_hook(inbox_path: Path, ui_events: list[dict] | None = None):
    """Create a PostToolUse hook that checks for unread messages after every tool call.

    Returns summaries of unread messages as additionalContext (agent-facing).
    Also pushes inbox_message UI events for TUI rendering (user-facing).

    Writes .read markers when notifying, so the watcher doesn't re-deliver
    messages the agent has already been told about. This is safe because the
    hook and watcher operate in non-overlapping time windows: the hook fires
    mid-turn (PostToolUse) when the watcher is blocked (_receiving=True);
    the watcher fires between turns when no tool calls are happening.
    Messages arriving while the agent is idle bypass the hook entirely and
    are delivered by the watcher as proper user turns.
    """
    # Track messages already injected this session (in-memory dedup for the
    # rare case where .read write succeeds but the set check runs before the
    # filesystem catches up on the next iteration).
    _injected: set[str] = set()

    async def inbox_check_hook(
        input_data: HookInput, tool_use_id: str | None, context: HookContext
    ) -> HookJSONOutput:
        if not inbox_path.exists():
            return {}

        summaries = []
        for msg_file in sorted(inbox_path.iterdir()):
            if not msg_file.is_file() or msg_file.suffix != ".md":
                continue

            # Skip messages already delivered by the watcher
            read_marker = msg_file.with_suffix(".read")
            if read_marker.exists():
                continue

            # Skip messages already injected this session (in-memory dedup)
            path_str = str(msg_file)
            if path_str in _injected:
                continue

            # Extract sender and channel from frontmatter
            parsed = parse_message(msg_file)
            if parsed:
                sender = parsed["from"] or "unknown"
                if parsed["channel"]:
                    ping = f"[Notification] Message from {sender} in {parsed['channel']} — {msg_file}"
                else:
                    ping = f"[Notification] Message from {sender} — {msg_file}"
                summaries.append(ping)
                _injected.add(path_str)
                read_marker.touch()  # prevent watcher re-delivery

                # Push UI event for TUI rendering
                if ui_events is not None:
                    ui_events.append({
                        "type": "inbox_message",
                        "from": parsed.get("from", ""),
                        "summary": parsed["summary"],
                        "channel": parsed.get("channel", ""),
                        "path": path_str,
                    })

        if not summaries:
            return {}

        return {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": "\n".join(summaries),
            }
        }

    return inbox_check_hook


def create_skill_context_hook(skills_path: Path):
    """Create a PostToolUse hook (matcher="mcp__kiln__activate_skill") that injects
    skill content as system-level context.

    When the activate_skill MCP tool runs, this hook replaces its output with a short
    confirmation (via updatedMCPToolOutput) and injects the full skill content as
    additionalContext so it appears as a system message rather than a tool result.
    """

    async def skill_context_hook(
        input_data: HookInput, tool_use_id: str | None, context: HookContext
    ) -> HookJSONOutput:
        tool_input = input_data.get("tool_input", {})
        name = tool_input.get("name", "")
        if not name:
            return {}

        skill_md = skills_path / name / "SKILL.md"
        if not skill_md.exists():
            return {}

        content = skill_md.read_text()

        # Strip YAML frontmatter
        if content.startswith("---"):
            try:
                end = content.index("---", 3)
                content = content[end + 3:].strip()
            except ValueError:
                pass

        return {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "updatedMCPToolOutput": f"Skill '{name}' activated.",
                "additionalContext": f"[Skill: {name}]\n\n{content}",
            }
        }

    return skill_context_hook


def create_read_tracking_hook(inbox_path: Path, file_state=None):
    """Create a PostToolUse hook (matcher="Read") that:

    1. Marks inbox messages as read (creates .read marker files).
    2. Records file reads in the shared FileState so MCP Edit/Write can
       enforce the "must read first" and "modified since read" validations.

    Args:
        inbox_path: The agent's inbox directory.
        file_state: A FileState instance shared with the MCP Edit/Write tools.
    """

    async def read_tracking_hook(
        input_data: HookInput, tool_use_id: str | None, context: HookContext
    ) -> HookJSONOutput:
        # input_data is a TypedDict — get the tool_input
        tool_input = input_data.get("tool_input", {})
        file_path_str = tool_input.get("file_path", "")
        if not file_path_str:
            return {}

        file_path = Path(file_path_str)

        # Record in shared file state (for MCP Edit/Write validation)
        if file_state is not None:
            has_offset = tool_input.get("offset") is not None
            has_limit = tool_input.get("limit") is not None
            file_state.record_read(
                str(file_path), partial=(has_offset or has_limit)
            )

        # Check if this file is inside the inbox directory
        try:
            file_path.relative_to(inbox_path)
        except ValueError:
            return {}

        # It's an inbox file — mark it as read
        if file_path.suffix == ".md" and file_path.exists():
            read_marker = file_path.with_suffix(".read")
            read_marker.touch()

        return {}

    return read_tracking_hook




def create_context_warning_hook(session_control, max_tokens: int = 200_000):
    """Create a PostToolUse hook that warns when context usage crosses thresholds.

    Fires at 50% (100k), then every 10% after that. Each threshold fires
    only once. The warning includes the current usage and a suggestion to
    consider handoffs at higher levels.

    Args:
        session_control: SessionControl instance with context_tokens field.
        max_tokens: Maximum context window size (default 200k).
    """
    # Thresholds as fractions: 0.50, 0.60, 0.70, 0.80, 0.90
    thresholds = [0.5, 0.6, 0.7, 0.8, 0.9]
    fired: set[float] = set()

    async def context_warning_hook(
        input_data: HookInput, tool_use_id: str | None, context: HookContext
    ) -> HookJSONOutput:
        if session_control is None:
            return {}

        tokens = session_control.context_tokens
        if tokens <= 0:
            return {}

        fraction = tokens / max_tokens

        # Find all thresholds crossed that haven't fired yet.
        # Mark all as fired in one call — prevents stale lower-threshold
        # messages firing on subsequent calls when context jumps multiple
        # levels at once.
        newly_crossed = [t for t in thresholds if fraction >= t and t not in fired]
        if not newly_crossed:
            return {}

        fired.update(newly_crossed)
        crossed = max(newly_crossed)
        pct = int(crossed * 100)
        token_k = f"{tokens // 1000}k"

        if crossed >= 0.8:
            urgency = (
                "Context is getting tight. If you're working autonomously, "
                "prepare to self-continue: update volatile and call "
                "exit_session(continue=true, handoff='...')."
            )
        elif crossed >= 0.6:
            urgency = (
                "If this is a long task, start thinking about wrapping up "
                "and self-continuing."
            )
        else:
            urgency = "No action needed yet — this is informational."

        return {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": (
                    f"[Context: {pct}%] Using ~{token_k} of {max_tokens // 1000}k tokens. "
                    f"{urgency}"
                ),
            }
        }

    return context_warning_hook





def create_active_agents_hook(
    interval: int = 15,
    channels_path: Path | None = None,
    session_prefix: str = "kiln-",
):
    """Create a PostToolUse hook that periodically shows running agent sessions and channels.

    Fires every `interval` tool calls. Skips if there's only one session
    (just the current agent — no useful info to show) and no active channels.

    Args:
        interval: Number of tool calls between checks.
        channels_path: Path to channels.json (for listing active channels).
        session_prefix: Tmux session name prefix for agent sessions.
    """
    call_count = 0
    ephemeral_prefix = f"_{session_prefix}"

    async def active_agents_hook(
        input_data: HookInput, tool_use_id: str | None, context: HookContext
    ) -> HookJSONOutput:
        nonlocal call_count
        call_count += 1

        if call_count % interval != 0:
            return {}

        # Get running agent sessions
        try:
            result = subprocess.run(
                ["tmux", "list-sessions", "-F", "#{session_name}"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0:
                return {}
        except (OSError, subprocess.TimeoutExpired):
            return {}

        sessions = [
            s for s in result.stdout.strip().splitlines()
            if s.startswith(session_prefix) or s.startswith(ephemeral_prefix)
        ]

        # Get active channels
        channels = []
        if channels_path and channels_path.exists():
            try:
                data = json.loads(channels_path.read_text())
                channels = [name for name, subs in data.items() if subs]
            except (json.JSONDecodeError, OSError):
                pass

        if len(sessions) <= 1 and not channels:
            return {}

        parts = []
        if len(sessions) > 1:
            parts.append(f"Agents: {', '.join(sessions)} ({len(sessions)} total)")
        if channels:
            parts.append(f"Channels: {', '.join(channels)}")

        return {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": f"[Active agents] {' | '.join(parts)}",
            }
        }

    return active_agents_hook


# ---------------------------------------------------------------------------
# Agent-behavioral hooks (worklog, memory reminders) were removed from
# kiln in the library-not-framework redesign (2026-03-07). They belong
# to the agent's own codebase.
# ---------------------------------------------------------------------------


def create_plan_nudge_hook(plan_path: Path, interval: int = 20):
    """Create a PostToolUse hook that periodically injects the agent's current plan.

    Reads the agent's plan file from disk and injects it as additionalContext.
    Nudges happen every `interval` tool calls, but only when a plan file exists.
    If there's no plan, stays silent — plan creation is prompted by the
    agent's identity document, not nagging hooks.

    Args:
        plan_path: Path to this agent's plan YAML file.
        interval: Number of tool calls between plan injections.
    """
    call_count = 0

    async def plan_nudge_hook(
        input_data: HookInput, tool_use_id: str | None, context: HookContext
    ) -> HookJSONOutput:
        nonlocal call_count
        call_count += 1

        if call_count % interval != 0:
            return {}

        if not plan_path.exists():
            return {}

        try:
            data = yaml.safe_load(plan_path.read_text())
        except (yaml.YAMLError, OSError):
            return {}

        if not data or not data.get("tasks"):
            return {}

        # Format the plan
        lines = [f"Goal: {data.get('goal', '(none)')}"]
        tasks = data.get("tasks", [])
        for t in tasks:
            status = t.get("status", "pending")
            desc = t.get("description", "")
            lines.append(f"  [{status}] {desc}")
        done = sum(1 for t in tasks if t.get("status") == "done")
        total = len(tasks)
        lines.append(f"Progress: {done}/{total} done.")

        # Don't nudge if all tasks are done
        if done == total:
            return {}

        plan_text = "\n".join(lines)
        return {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": (
                    f"[Plan check] Your current plan:\n{plan_text}\n"
                    f"Update with the `plan` tool if this is stale or if you've completed tasks."
                ),
            }
        }

    return plan_nudge_hook


def _get_session_timestamp(path: Path) -> datetime:
    """Extract timestamp from session file frontmatter, falling back to file mtime."""
    try:
        text = path.read_text()
        if text.startswith("---"):
            end = text.index("---", 3)
            frontmatter = yaml.safe_load(text[3:end])
            if frontmatter and "timestamp" in frontmatter:
                ts = frontmatter["timestamp"]
                if isinstance(ts, datetime):
                    return ts
                return datetime.fromisoformat(str(ts))
    except (ValueError, yaml.YAMLError):
        pass
    return datetime.fromtimestamp(path.stat().st_mtime)


def _build_session_recap(sessions_path: Path) -> str:
    """Summarize today's recent sessions using Haiku.

    Reads up to 5 most recent session files from today, calls Haiku to
    produce a concise recap. Returns empty string on failure or if no
    sessions exist.
    """
    if not sessions_path.exists():
        return ""

    today_prefix = date.today().strftime("%Y-%m-%d")
    today_files = sorted(
        [
            f
            for f in sessions_path.iterdir()
            if f.name.startswith(today_prefix) and f.suffix == ".md"
        ],
        key=_get_session_timestamp,
        reverse=True,
    )[:5]

    if not today_files:
        return ""

    content_parts = []
    for f in today_files:
        content_parts.append(f"### {f.stem}\nFile: {f}\n\n{f.read_text()}")
    combined = "\n\n---\n\n".join(content_parts)

    prompt = (
        "Below are session summaries from today for a persistent AI agent, "
        "ordered from MOST RECENT to oldest. Each session header includes the file path.\n\n"
        "Produce a recap covering: what was worked on, key decisions, current state, "
        "and anything unfinished. Structure the recap in chronological order (most recent "
        "session FIRST, clearly labeled). For each session mentioned, include its file path "
        "so the agent can read the full summary if needed.\n\n"
        "Write in second person ('you did X'). Be specific — names, paths, "
        "details matter more than vague summaries. Keep it concise but don't sacrifice "
        "clarity for brevity.\n\n"
        f"{combined}"
    )

    try:
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        result = subprocess.run(
            ["claude", "-p", "--model", "haiku", "--no-session-persistence", "--effort", "low"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return ""
    except Exception:
        return ""


def create_usage_log_hook(logs_path: Path, agent_id: str, tools_bin: Path | None = None):
    """Create a PostToolUse hook that logs custom tool and skill usage to a JSONL file.

    Logs two categories:
    - Custom tools: Bash calls to tools/bin/* (e.g. exa, tavily)
    - Skill activations: mcp__kiln__activate_skill calls
    Built-in tools (Read, Write, Bash, etc.) are skipped.
    """
    log_file = logs_path / "tool-usage.jsonl"
    bin_prefix = str(tools_bin) + "/" if tools_bin else None

    def _append(entry: dict) -> None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with open(log_file, "a") as f:
            f.write(json.dumps(entry) + "\n")

    async def usage_log_hook(
        input_data: HookInput, tool_use_id: str | None, context: HookContext
    ) -> HookJSONOutput:
        tool_name = input_data.get("tool_name", "")
        tool_input = input_data.get("tool_input", {})
        tool_output = input_data.get("tool_output", {})
        is_error = False
        if isinstance(tool_output, dict):
            is_error = bool(tool_output.get("is_error"))

        # Skill activations
        if tool_name == "mcp__kiln__activate_skill" or tool_name.endswith("__activate_skill"):
            skill = tool_input.get("name", "unknown")
            _append({
                "ts": datetime.now().isoformat(),
                "agent": agent_id,
                "type": "skill",
                "name": skill,
            })
            return {}

        # Custom tools (Bash calls to tools/bin/)
        if (tool_name == "Bash" or tool_name.endswith("__Bash")) and bin_prefix:
            command = tool_input.get("command", "")
            if bin_prefix in command:
                # Use regex to extract the tool name immediately after bin_prefix.
                # Simple split() could grab a shell operator (&&, |) if bin_prefix
                # appears in a non-invocation context earlier in the command string.
                m = re.search(re.escape(bin_prefix) + r"([\w][\w.-]*)", command)
                custom_tool = m.group(1) if m else "unknown"
                _append({
                    "ts": datetime.now().isoformat(),
                    "agent": agent_id,
                    "type": "tool",
                    "name": custom_tool,
                    "error": is_error,
                })

        return {}

    return usage_log_hook


def parse_message(msg_file: Path) -> dict | None:
    """Parse a message file into its components.

    Returns a dict with keys: from, summary, priority, body, path.
    Returns None if the file can't be read.
    """
    try:
        text = msg_file.read_text()
    except OSError:
        return None

    result = {
        "from": "",
        "summary": "",
        "priority": "normal",
        "channel": "",
        "body": "",
        "path": str(msg_file),
    }

    if not text.startswith("---"):
        # No frontmatter — treat entire content as body, first line as summary
        first_line = text.strip().split("\n")[0]
        result["summary"] = first_line[:200] if first_line else ""
        result["body"] = text.strip()
        return result

    # Parse YAML frontmatter
    lines = text.split("\n")
    fm_end = None
    for i, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            fm_end = i
            break

    if fm_end is None:
        result["body"] = text.strip()
        return result

    for line in lines[1:fm_end]:
        if line.startswith("from:"):
            result["from"] = line[len("from:"):].strip().strip('"').strip("'")
        elif line.startswith("summary:"):
            result["summary"] = line[len("summary:"):].strip().strip('"').strip("'")
        elif line.startswith("priority:"):
            result["priority"] = line[len("priority:"):].strip().strip('"').strip("'")
        elif line.startswith("channel:"):
            result["channel"] = line[len("channel:"):].strip().strip('"').strip("'")

    result["body"] = "\n".join(lines[fm_end + 1:]).strip()
    return result


def create_queued_message_hook(queue: list[str], ui_events: list[dict]):
    """Create a PostToolUse hook that delivers queued user messages mid-turn.

    When the user types while the agent is receiving, messages are appended
    to the shared queue. This hook drains the queue after each tool call and
    injects the messages as additionalContext so the agent sees them before
    its next action. Also pushes followup_delivered UI events for TUI rendering.
    """

    async def queued_message_hook(
        input_data: HookInput, tool_use_id: str | None, context: HookContext
    ) -> HookJSONOutput:
        if not queue:
            return {}

        messages = list(queue)
        queue.clear()
        ui_events.append({"type": "followup_delivered", "messages": messages})

        parts = [f"[User followup]: {msg}" for msg in messages]
        return {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": "\n".join(parts),
            }
        }

    return queued_message_hook


def create_message_sent_hook(ui_events: list[dict]):
    """Create a PostToolUse hook (matcher="mcp__kiln__message") that pushes
    a UI event when the agent sends a message.

    Purely cosmetic — lets the TUI show outbound messages to the user.
    Only fires for action=send; subscribe/unsubscribe are not interesting.
    """

    async def message_sent_hook(
        input_data: HookInput, tool_use_id: str | None, context: HookContext
    ) -> HookJSONOutput:
        tool_input = input_data.get("tool_input", {})
        if tool_input.get("action") != "send":
            return {}

        # Check the tool output for errors — don't show UI event for failed sends
        tool_output = input_data.get("tool_output", {})
        if isinstance(tool_output, dict) and tool_output.get("is_error"):
            return {}

        ui_events.append({
            "type": "message_sent",
            "to": tool_input.get("to", ""),
            "channel": tool_input.get("channel", ""),
            "summary": tool_input.get("summary", ""),
        })
        return {}

    return message_sent_hook


def _extract_summary(msg_file: Path) -> str | None:
    """Extract the summary field from a message file's YAML frontmatter."""
    parsed = parse_message(msg_file)
    if parsed is None:
        return None
    return parsed["summary"] or None

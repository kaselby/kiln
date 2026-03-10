"""Permission system — mode management, tool classification, diff generation, and PreToolUse hook.

Includes guardrails for dangerous commands that fire regardless of permission mode.
"""

import asyncio
import difflib
import os
import re
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable

from claude_agent_sdk import HookContext, HookInput, HookJSONOutput


# ---------------------------------------------------------------------------
# Guardrails — dangerous command detection
# ---------------------------------------------------------------------------
#
# IMPORTANT: these guardrails are regex-based and operate on the command
# string as submitted.  They catch direct invocations but are trivially
# bypassable by an agent that writes a script to a file and then executes
# it, uses variable indirection, pipes to an interpreter, encodes the
# command, or uses shell aliases/functions.  This is by design — the
# guardrails are a speed bump for the common case (catching accidental
# dangerous commands in normal task flow), NOT a security boundary.
#
# Known bypass categories that are NOT caught:
#   - Write-to-file + execute:  Write("rm -rf /") → bash script.sh
#   - Variable indirection:     CMD='git push'; $CMD
#   - Pipe to interpreter:      echo 'git push' | bash
#   - Encoding:                 echo <base64> | base64 -d | bash
#   - Alias/function def:       alias gp='git push'; gp
#   - curl | bash:              curl URL | bash
#
# The real safeguard is the user reviewing the agent's intent, not
# these regex patterns.  See also: the permission mode system below.

# Each entry: (compiled regex, tier, human description)
# "block"   = always denied, no override from the agent
# "confirm" = requires explicit user approval (even in YOLO mode)
#
# Block patterns are checked first and take priority over confirm patterns.

# Commands whose string arguments are themselves executed (and therefore
# still dangerous).  Patterns are checked against the command word that
# "owns" a quoted string — if it matches, the string content is NOT masked.
_EXEC_WRAPPERS = re.compile(
    r"\b(?:bash|sh|zsh|fish|dash|ksh|csh|tcsh"
    r"|eval|exec|source|\."
    r"|ssh|sudo|su|doas|env|nohup|xargs|watch"
    r"|python[23]?|ruby|perl|node|php"
    r"|screen|tmux\s+(?:send-keys|run-shell))\b"
)


def _mask_quoted_strings(command: str) -> str:
    """Replace content inside quoted strings with placeholder text.

    Preserves string content when the surrounding command is an execution
    wrapper (bash -c, ssh, eval, etc.) so that dangerous patterns inside
    those strings are still caught.

    Falls back to the original command (unmasked) on any parse ambiguity,
    erring on the side of false positives over missed detections.
    """
    # Mask heredoc bodies first — they're almost always data, not commands.
    # Matches: << EOF ... EOF, << 'EOF' ... EOF, << "EOF" ... EOF
    # Also handles <<- (strip tabs) variant.
    command = re.sub(
        r"<<-?\s*['\"]?(\w+)['\"]?.*?\n.*?\n\1\b",
        lambda m: m.group(0).split("\n", 1)[0] + "\n__HEREDOC_MASKED__\n" + m.group(1),
        command,
        flags=re.DOTALL,
    )

    # Split on unescaped single and double quotes using a simple state machine.
    # Single-quoted strings: always literal (no escapes).
    # Double-quoted strings: we mask content unless it contains $( or `
    #   (command substitution makes the content potentially executable).
    result = []
    i = 0
    n = len(command)

    while i < n:
        ch = command[i]

        if ch == "'" :
            # Single-quoted string — find closing quote
            end = command.find("'", i + 1)
            if end == -1:
                # Unclosed quote — fall back to original
                return command
            inner = command[i + 1:end]
            # Check if the command owning this string is an exec wrapper
            prefix = command[:i].rstrip()
            if _EXEC_WRAPPERS.search(prefix):
                result.append(command[i:end + 1])  # preserve
            else:
                result.append("'__MASKED__'")
            i = end + 1

        elif ch == '"':
            # Double-quoted string — find closing unescaped quote
            j = i + 1
            while j < n:
                if command[j] == '\\':
                    j += 2  # skip escaped char
                elif command[j] == '"':
                    break
                else:
                    j += 1
            if j >= n:
                # Unclosed quote — fall back to original
                return command
            inner = command[i + 1:j]
            # If inner contains command substitution, don't mask (content is executable)
            if '$(' in inner or '`' in inner:
                result.append(command[i:j + 1])
            else:
                prefix = command[:i].rstrip()
                if _EXEC_WRAPPERS.search(prefix):
                    result.append(command[i:j + 1])  # preserve
                else:
                    result.append('"__MASKED__"')
            i = j + 1

        elif ch == '\\' and i + 1 < n:
            result.append(command[i:i + 2])
            i += 2

        else:
            result.append(ch)
            i += 1

    return "".join(result)


_GUARDRAIL_PATTERNS: list[tuple[re.Pattern, str, str]] = []


def _compile_guardrails():
    raw = [
        # --- Block: catastrophic, almost never intentional from an agent ---
        (r"\brm\s+-\S*r\S*\s+/\s*$", "block", "recursive delete from filesystem root"),
        (r"\brm\s+-\S*r\S*\s+/\*", "block", "recursive delete from filesystem root"),
        (r"\brm\s+-\S*r\S*\s+~/?\s*$", "block", "recursive delete of home directory"),
        (r"\bmkfs\b", "block", "format filesystem"),
        (r"\bdd\b.*\bof\s*=\s*/dev/", "block", "write directly to raw device"),

        # --- Confirm: destructive but sometimes legitimate ---
        (r"\bgit\s+push\b", "confirm", "git push"),
        (r"\bgit\s+filter-branch\b", "confirm", "git filter-branch (rewrites history)"),
        (r"\bgit\s+filter-repo\b", "confirm", "git filter-repo (rewrites history)"),
        (r"\bgit\s+reset\s+--hard\b", "confirm", "git reset --hard (discards changes)"),
        (r"\bgit\s+clean\b.*-\w*f", "confirm", "git clean (deletes untracked files)"),
        (r"\btmux\s+kill-(session|server)\b", "confirm", "kill tmux session/server"),
        (r"\bkillall\s", "confirm", "kill processes by name (killall)"),
        (r"\bpkill\s", "confirm", "kill processes by pattern (pkill)"),
    ]
    for pattern_str, tier, desc in raw:
        _GUARDRAIL_PATTERNS.append((re.compile(pattern_str), tier, desc))


_compile_guardrails()


def register_guardrail(pattern: str, tier: str, description: str) -> None:
    """Register an additional guardrail pattern.

    Args:
        pattern: Regex pattern string to match against bash commands.
        tier: "block" (always denied) or "confirm" (requires user approval).
        description: Human-readable description shown in the prompt/notification.
    """
    if tier not in ("block", "confirm"):
        raise ValueError(f"tier must be 'block' or 'confirm', got {tier!r}")
    _GUARDRAIL_PATTERNS.append((re.compile(pattern), tier, description))


def _is_tool(tool_name: str, base: str) -> bool:
    """Check if tool_name matches a base tool (e.g. 'Bash').

    Matches the bare name and any MCP-prefixed variant:
    'Bash', 'mcp__kiln__Bash', 'mcp__myagent__Bash', etc.
    """
    return tool_name == base or tool_name.endswith(f"__{base}")


def _has_rm_rf(command: str) -> bool:
    """Check if a command contains rm with both -r and -f flags in any form."""
    # Quick exit
    if not re.search(r"\brm\s", command):
        return False
    # Single flag group: rm -rf, rm -fr, rm -rfi, etc.
    if re.search(r"\brm\s+.*-\w*(?:r\w*f|f\w*r)", command):
        return True
    # Separate flags: rm -r -f, rm -r somepath -f, etc.
    # Collect all short flags after rm
    match = re.search(r"\brm\s(.*)", command)
    if match:
        after_rm = match.group(1)
        flags = re.findall(r"-(\w+)", after_rm)
        all_flags = "".join(flags)
        if "r" in all_flags and "f" in all_flags:
            return True
    return False


def _is_exempt(reason: str, cwd: str, agent_home: str, command: str = "") -> bool:
    """Check if a confirm-tier guardrail should be exempted based on CWD or command.

    Exempts git push when the shell is inside the agent's home directory,
    or when the command explicitly targets it (e.g. `cd ~/.<agent> && git push`
    or `git -C ~/.<agent> push`).
    """
    if reason == "git push":
        try:
            real_cwd = os.path.realpath(cwd)
            if real_cwd == agent_home or real_cwd.startswith(agent_home + os.sep):
                return True
        except (OSError, ValueError):
            pass
        # Check if the command itself targets the agent's home repo
        if command:
            home = os.path.expanduser("~")
            home_patterns = [
                re.escape(agent_home),
                re.escape(agent_home.replace(home, "~")),
                r"\$KILN_AGENT_HOME",
                r"\$\{KILN_AGENT_HOME\}",
            ]
            for pat in home_patterns:
                # cd <agent-home> && git push  /  cd <agent-home>; git push
                if re.search(rf"\bcd\s+[\"']?{pat}[\"']?\s*[;&]", command):
                    return True
                # git -C <agent-home> push
                if re.search(rf"\bgit\s+-C\s+[\"']?{pat}[\"']?\s+push\b", command):
                    return True
    return False


def classify_danger(command: str) -> tuple[str, str] | None:
    """Classify a bash command's danger level.

    Returns (tier, description) where tier is "block" or "confirm",
    or None if the command is not flagged as dangerous.

    Quoted string content is masked before matching to avoid false
    positives on commit messages, echo statements, etc.  Content is
    preserved (not masked) when the quoting command is an execution
    wrapper (bash -c, ssh, eval, …) or contains command substitution.
    """
    masked = _mask_quoted_strings(command)

    # Block patterns first
    for pattern, tier, desc in _GUARDRAIL_PATTERNS:
        if tier == "block" and pattern.search(masked):
            return ("block", desc)

    # rm -rf detection (confirm tier) — uses helper for flag combinations
    if _has_rm_rf(masked):
        return ("confirm", "recursive force delete (rm -rf)")

    # Other confirm patterns
    for pattern, tier, desc in _GUARDRAIL_PATTERNS:
        if tier == "confirm" and pattern.search(masked):
            return ("confirm", desc)

    return None


class PermissionMode(Enum):
    SAFE = "safe"
    SUPERVISED = "supervised"
    YOLO = "yolo"
    TRUSTED = "trusted"

    def next(self) -> "PermissionMode":
        cycle = [PermissionMode.SAFE, PermissionMode.SUPERVISED, PermissionMode.YOLO, PermissionMode.TRUSTED]
        idx = cycle.index(self)
        return cycle[(idx + 1) % len(cycle)]


def needs_permission(mode: PermissionMode, tool_name: str) -> bool:
    """Whether this tool requires user permission in the given mode."""
    if mode in (PermissionMode.YOLO, PermissionMode.TRUSTED):
        return False
    if _is_tool(tool_name, "Edit") or _is_tool(tool_name, "Write"):
        return True  # Edit/Write gated in both safe and supervised
    if _is_tool(tool_name, "Bash") and mode == PermissionMode.SAFE:
        return True
    return False


@dataclass
class PermissionRequest:
    """Data passed to the TUI when permission is needed."""

    tool_name: str
    tool_input: dict[str, Any]
    diff_text: str
    is_guardrail: bool = False
    result: bool = False
    timed_out: bool = False
    event: asyncio.Event = field(default_factory=asyncio.Event)

    def decide(self, allowed: bool) -> None:
        self.result = allowed
        self.event.set()


def generate_diff(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Generate a human-readable diff or preview for a tool call."""
    if _is_tool(tool_name, "Edit"):
        return _diff_edit(tool_input)
    elif _is_tool(tool_name, "Write"):
        return _diff_write(tool_input)
    elif _is_tool(tool_name, "Bash"):
        return _preview_bash(tool_input)
    return ""


def _diff_edit(tool_input: dict[str, Any]) -> str:
    path = tool_input.get("file_path", "unknown")
    old = tool_input.get("old_string", "")
    new = tool_input.get("new_string", "")

    old_lines = old.splitlines()
    new_lines = new.splitlines()

    diff_lines = list(difflib.unified_diff(
        old_lines, new_lines,
        fromfile=path, tofile=path,
        lineterm="",
    ))
    return "\n".join(diff_lines)


def _diff_write(tool_input: dict[str, Any]) -> str:
    path_str = tool_input.get("file_path", "unknown")
    content = tool_input.get("content", "")
    path = Path(path_str)

    if path.exists():
        try:
            existing = path.read_text()
            old_lines = existing.splitlines()
            new_lines = content.splitlines()
            diff_lines = list(difflib.unified_diff(
                old_lines, new_lines,
                fromfile=path_str, tofile=path_str,
                lineterm="",
            ))
            return "\n".join(diff_lines)
        except (OSError, UnicodeDecodeError):
            pass

    # New file — show preview
    lines = content.splitlines()
    n = len(lines)
    preview = lines[:15]
    parts = [f"new file ({n} lines)"]
    for line in preview:
        parts.append(f"+{line}")
    if n > 15:
        parts.append(f"... ({n - 15} more lines)")
    return "\n".join(parts)


def _preview_bash(tool_input: dict[str, Any]) -> str:
    cmd = tool_input.get("command", "")
    desc = tool_input.get("description", "")
    parts = []
    if desc:
        parts.append(desc)
    parts.append(f"$ {cmd}")
    return "\n".join(parts)


# Type alias for the permission handler callback the TUI registers
PermissionHandler = Callable[["PermissionRequest"], Awaitable[bool]]


def _notify(title: str, message: str) -> None:
    """Send a desktop notification. Best-effort, never raises.

    macOS: tries terminal-notifier first (reliable from tmux), falls back to osascript.
    Linux: uses notify-send (libnotify).
    """
    try:
        subprocess.Popen(
            ["terminal-notifier", "-title", title, "-message", message,
             "-sound", "default"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return
    except FileNotFoundError:
        pass
    try:
        subprocess.Popen(
            ["osascript", "-e",
             f'display notification "{message}" with title "{title}"'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return
    except (FileNotFoundError, OSError):
        pass
    try:
        subprocess.Popen(
            ["notify-send", title, message],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except (FileNotFoundError, OSError):
        pass


def create_permission_hook(
    get_mode: Callable[[], PermissionMode],
    request_permission: PermissionHandler,
    get_cwd: Callable[[], str] | None = None,
    agent_id: str = "agent",
    agent_home: str | None = None,
):
    """Factory: create a PreToolUse hook that checks permissions.

    Guardrails for dangerous commands fire regardless of permission mode.

    Args:
        get_mode: Returns the current permission mode (reads TUI state).
        request_permission: Async callback that shows the diff/prompt in the TUI
                          and returns True (allow) or False (deny).
        get_cwd: Returns the shell's current working directory. Used for
                path-based guardrail exemptions (e.g. allowing git push
                from the agent's home directory).
        agent_id: The agent's session ID (used in notifications).
        agent_home: The agent's home directory (used for git push exemptions).
                   Defaults to ~ if not provided.
    """
    if agent_home is None:
        agent_home = os.path.expanduser("~")
    agent_home = os.path.realpath(agent_home)

    async def permission_hook(
        input_data: HookInput, tool_use_id: str | None, context: HookContext
    ) -> HookJSONOutput:
        tool_name = input_data.get("tool_name", "")
        tool_input = input_data.get("tool_input", {})
        mode = get_mode()

        # --- Guardrails: check dangerous commands BEFORE mode check ---
        if _is_tool(tool_name, "Bash"):
            command = tool_input.get("command", "")
            danger = classify_danger(command)

            if danger:
                tier, reason = danger

                if tier == "block":
                    _notify(f"Kiln: {agent_id}", f"BLOCKED: {reason}")
                    return {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "deny",
                            "permissionDecisionReason":
                                f"Blocked by guardrail: {reason}. "
                                f"Slow down — guardrails exist for a reason. "
                                f"Make sure you aren't trying to do anything "
                                f"that requires human supervision, and please "
                                f"do not attempt to work around this by using "
                                f"any form of indirection or alternate execution "
                                f"paths."
                        }
                    }

                # Confirm tier — skip in TRUSTED mode (user is present and watching)
                if mode == PermissionMode.TRUSTED:
                    return {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "allow",
                        }
                    }

                # Check path-based exemptions before prompting
                if get_cwd and _is_exempt(reason, get_cwd(), agent_home, command):
                    return {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "allow",
                        }
                    }

                # No exemption — always prompt, even in YOLO mode
                _notify(
                    f"Kiln: {agent_id}",
                    f"Dangerous command needs approval: {reason}",
                )
                diff_text = f"DANGEROUS: {reason}\n\n$ {command}"
                req = PermissionRequest(
                    tool_name=tool_name,
                    tool_input=tool_input,
                    diff_text=diff_text,
                    is_guardrail=True,
                )
                allowed = await request_permission(req)
                decision = "allow" if allowed else "deny"
                result: HookJSONOutput = {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": decision,
                    }
                }
                if allowed:
                    result["hookSpecificOutput"]["permissionDecisionReason"] = (
                        f"User approved guardrail: {reason}."
                    )
                if not allowed:
                    result["hookSpecificOutput"]["permissionDecisionReason"] = (
                        f"User rejected dangerous command: {reason}. "
                        f"Slow down — guardrails exist for a reason. "
                        f"Make sure you aren't trying to do anything "
                        f"that requires human supervision, and please "
                        f"do not attempt to work around this by using "
                        f"any form of indirection or alternate execution "
                        f"paths."
                    )
                return result

        # --- Normal permission check (respects mode) ---
        if not needs_permission(mode, tool_name):
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                }
            }

        diff_text = generate_diff(tool_name, tool_input)
        req = PermissionRequest(
            tool_name=tool_name,
            tool_input=tool_input,
            diff_text=diff_text,
        )

        allowed = await request_permission(req)

        if allowed:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                }
            }
        else:
            if req.timed_out:
                reason = (
                    "Permission request timed out (no human responded "
                    "within 5 minutes). This is likely not a rejection — "
                    "the user may be away. Consider retrying later or "
                    "switching to a different task."
                )
            else:
                reason = "User rejected tool call"
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }

    return permission_hook

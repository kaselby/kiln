"""Permission system — mode management, approval routing, and PreToolUse hook.

Provides PermissionHandler, which manages tool permissions including
mode-based access control, guardrail enforcement, and unified approval
routing across terminal and daemon sources.
"""

import asyncio
import difflib
import logging
import os
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable

from claude_agent_sdk import HookContext, HookInput, HookJSONOutput

from .guardrails import classify_danger, is_exempt

_log = logging.getLogger("kiln.permissions")

_GUARDRAIL_WARNING = (
    "Slow down \u2014 guardrails exist for a reason. "
    "Make sure you aren't trying to do anything "
    "that requires human supervision, and please "
    "do not attempt to work around this by using "
    "any form of indirection or alternate execution "
    "paths."
)


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _is_tool(tool_name: str, base: str) -> bool:
    """Check if tool_name matches a base tool (e.g. 'Bash').

    Matches the bare name and any MCP-prefixed variant:
    'Bash', 'mcp__kiln__Bash', 'mcp__myagent__Bash', etc.
    """
    return tool_name == base or tool_name.endswith(f"__{base}")


# ---------------------------------------------------------------------------
# Permission modes
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Permission request
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Diff generation
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Desktop notifications
# ---------------------------------------------------------------------------

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


# Type alias for the terminal approval callback
ApprovalCallback = Callable[["PermissionRequest"], Awaitable[bool]]


# ---------------------------------------------------------------------------
# PermissionHandler
# ---------------------------------------------------------------------------

class PermissionHandler:
    """Manages tool permissions: mode checks, guardrails, and approval routing.

    Provides a PreToolUse hook that checks guardrails and mode-based
    permissions, routing all approval requests through available sources
    (terminal TUI, daemon/Discord) with first-response-wins semantics.
    """

    def __init__(
        self,
        get_mode: Callable[[], PermissionMode],
        terminal_handler: ApprovalCallback | None = None,
        get_cwd: Callable[[], str] | None = None,
        agent_id: str = "agent",
        agent_home: str | None = None,
    ):
        if agent_home is None:
            agent_home = os.path.expanduser("~")
        self._agent_home = os.path.realpath(agent_home)
        self._agent_id = agent_id
        self._get_mode = get_mode
        self._terminal_handler = terminal_handler
        self._get_cwd = get_cwd

    # -- Public interface ---------------------------------------------------

    async def hook(
        self, input_data: HookInput, tool_use_id: str | None, context: HookContext
    ) -> HookJSONOutput:
        """PreToolUse hook: check guardrails, then mode-based permissions."""
        tool_name = input_data.get("tool_name", "")
        tool_input = input_data.get("tool_input", {})
        mode = self._get_mode()

        # --- Guardrails: dangerous command detection (fires before mode check) ---
        if _is_tool(tool_name, "Bash"):
            command = tool_input.get("command", "")
            danger = classify_danger(command)

            if danger:
                tier, reason = danger

                if tier == "block":
                    _notify(f"Kiln: {self._agent_id}", f"BLOCKED: {reason}")
                    return self._deny(f"Blocked by guardrail: {reason}. {_GUARDRAIL_WARNING}")

                # Confirm tier — skip in TRUSTED mode (user is present and watching)
                if mode == PermissionMode.TRUSTED:
                    return self._allow()

                # Check path-based exemptions before prompting
                if self._get_cwd and is_exempt(reason, self._get_cwd(), self._agent_home, command):
                    return self._allow()

                # No exemption — always prompt, even in YOLO mode
                _notify(f"Kiln: {self._agent_id}", f"Dangerous command needs approval: {reason}")
                req = PermissionRequest(
                    tool_name=tool_name,
                    tool_input=tool_input,
                    diff_text=f"DANGEROUS: {reason}\n\n$ {command}",
                    is_guardrail=True,
                )
                allowed, context = await self._request_approval(
                    req,
                    title=f"\u26a0\ufe0f Dangerous: {reason}",
                    preview=f"```\n$ {command[:500]}\n```",
                    detail=command if len(command) > 500 else None,
                    severity="warn",
                )
                if allowed:
                    return self._allow(f"User approved guardrail: {reason}.", context=context)
                return self._deny(f"User rejected dangerous command: {reason}. {_GUARDRAIL_WARNING}", context=context)

        # --- Normal permission check (respects mode) ---
        if not needs_permission(mode, tool_name):
            return self._allow()

        diff_text = generate_diff(tool_name, tool_input)
        req = PermissionRequest(
            tool_name=tool_name,
            tool_input=tool_input,
            diff_text=diff_text,
        )

        # Build fields for daemon display
        base_name = self._base_tool_name(tool_name)
        preview = self._build_preview(tool_name, tool_input)
        allowed, context = await self._request_approval(
            req,
            title=f"{base_name} requires approval ({mode.value} mode)",
            preview=preview,
            detail=diff_text,
            severity="info",
        )
        if allowed:
            return self._allow(context=context)
        if req.timed_out:
            return self._deny(
                "Permission request timed out (no human responded "
                "within 5 minutes). This is likely not a rejection \u2014 "
                "the user may be away. Consider retrying later or "
                "switching to a different task.",
                context=context,
            )
        return self._deny("User rejected tool call", context=context)

    # -- Unified approval ---------------------------------------------------

    async def _request_approval(
        self, req: PermissionRequest, *,
        title: str, preview: str,
        detail: str | None = None, severity: str = "info",
    ) -> tuple[bool, str]:
        """Route an approval request to all available sources, first response wins.

        Races the terminal handler (TUI or None) against the daemon
        (if online). Handles cross-source cleanup: if daemon wins,
        dismisses the TUI prompt; if terminal wins, updates the Discord
        message.

        Returns (allowed, context) where context is a short string
        describing what triggered and the outcome, suitable for
        additionalContext injection so the agent knows a prompt occurred.
        """
        has_terminal = self._terminal_handler is not None
        daemon_available = self._is_daemon_available()

        tasks: dict[asyncio.Task, str] = {}

        if has_terminal:
            tasks[asyncio.create_task(self._terminal_handler(req))] = "terminal"

        if daemon_available:
            async def _daemon():
                result = await self._daemon_request_approval(
                    title=title, preview=preview,
                    detail=detail, severity=severity,
                )
                if "error" in result:
                    return None  # daemon call failed, not a decision
                return result.get("approved", False)
            tasks[asyncio.create_task(_daemon())] = "daemon"

        if not tasks:
            _log.info("No approval sources available \u2014 denying")
            return False, f"Permission request: {title} \u2014 denied (no approval sources)"

        # Race all sources — first definitive response wins
        while tasks:
            done, _ = await asyncio.wait(tasks.keys(), return_when=asyncio.FIRST_COMPLETED)

            for completed in done:
                source = tasks.pop(completed)
                try:
                    result = completed.result()
                except Exception:
                    _log.exception("Approval source '%s' raised", source)
                    continue

                if result is None:
                    # Source failed (e.g. daemon unavailable) — keep waiting
                    continue

                allowed = bool(result)
                _log.info("Resolved by %s: %s", source, "approved" if allowed else "denied")

                # Cross-source cleanup
                if source == "daemon" and not req.event.is_set():
                    req.decide(allowed)

                if source == "terminal" and daemon_available:
                    status = "approved" if allowed else "rejected"
                    asyncio.create_task(self._resolve_daemon_approval(status))

                # Cancel remaining sources
                for t in tasks:
                    t.cancel()

                if allowed:
                    outcome = "approved"
                elif req.timed_out:
                    outcome = "timed out"
                else:
                    outcome = "denied"
                return allowed, f"Permission request: {title} \u2014 {outcome}"

        _log.warning("All approval sources failed \u2014 denying")
        return False, f"Permission request: {title} \u2014 denied (all sources failed)"

    # -- Daemon communication -----------------------------------------------

    def _get_daemon_client(self):
        """Lazily create a DaemonClient for approval routing."""
        if not hasattr(self, "_daemon_client"):
            try:
                from .daemon.client import DaemonClient
                agent = self._agent_id.split("-")[0]
                self._daemon_client = DaemonClient(
                    agent=agent, session=self._agent_id, auto_start=False,
                )
            except ImportError:
                _log.debug("DaemonClient not available")
                self._daemon_client = None
        return self._daemon_client

    def _is_daemon_available(self) -> bool:
        """Check if the daemon socket exists and a client can be created."""
        from .daemon.config import SOCKET_PATH, PID_FILE
        if not SOCKET_PATH.exists():
            return False
        if PID_FILE.exists():
            try:
                pid = int(PID_FILE.read_text().strip())
                os.kill(pid, 0)
            except (ValueError, ProcessLookupError, PermissionError, OSError):
                return False
        return self._get_daemon_client() is not None

    async def _daemon_request_approval(
        self, *,
        title: str, preview: str,
        detail: str | None = None, severity: str = "info",
        timeout: float = 300,
    ) -> dict:
        """Request approval via the daemon's management API."""
        client = self._get_daemon_client()
        if not client:
            return {"error": "no daemon client"}

        args = {
            "title": title,
            "preview": preview,
            "severity": severity,
            "timeout": timeout,
        }
        if detail:
            args["detail"] = detail

        try:
            return await client.mgmt(
                "request_approval", args, timeout=timeout + 15,
            )
        except Exception as e:
            _log.warning("Daemon approval request failed: %s", e)
            return {"error": str(e)}

    async def _resolve_daemon_approval(self, status: str) -> None:
        """Tell the daemon to resolve a pending approval. Best-effort."""
        client = self._get_daemon_client()
        if not client:
            return
        try:
            await client.mgmt("resolve_approval", {"status": status}, timeout=5)
        except Exception:
            pass

    # -- Hook helpers -------------------------------------------------------

    @staticmethod
    def _build_preview(tool_name: str, tool_input: dict) -> str:
        """Build a short preview string for the daemon embed body."""
        if _is_tool(tool_name, "Edit"):
            path = tool_input.get("file_path", "unknown")
            old = tool_input.get("old_string", "")
            snippet = old[:80] + "..." if len(old) > 80 else old
            return f"`{path}`\n```\n{snippet}\n```" if snippet else f"`{path}`"
        if _is_tool(tool_name, "Write"):
            path = tool_input.get("file_path", "unknown")
            return f"`{path}`"
        if _is_tool(tool_name, "Bash"):
            cmd = tool_input.get("command", "")
            desc = tool_input.get("description", "")
            preview = f"**{desc}**\n" if desc else ""
            return f"{preview}```\n$ {cmd[:300]}\n```"
        return ""

    @staticmethod
    def _base_tool_name(tool_name: str) -> str:
        """Extract the base tool name from an MCP-prefixed name."""
        if "__" in tool_name:
            return tool_name.rsplit("__", 1)[-1]
        return tool_name

    @staticmethod
    def _allow(reason: str | None = None, context: str | None = None) -> HookJSONOutput:
        result: HookJSONOutput = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
            }
        }
        if reason:
            result["hookSpecificOutput"]["permissionDecisionReason"] = reason
        if context:
            result["hookSpecificOutput"]["additionalContext"] = context
        return result

    @staticmethod
    def _deny(reason: str | None = None, context: str | None = None) -> HookJSONOutput:
        result: HookJSONOutput = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
            }
        }
        if reason:
            result["hookSpecificOutput"]["permissionDecisionReason"] = reason
        if context:
            result["hookSpecificOutput"]["additionalContext"] = context
        return result

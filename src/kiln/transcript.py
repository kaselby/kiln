"""Claude-compatible transcript writer and loader for the custom backend.

Writes a live JSONL transcript during custom-backend sessions in the same
record format that Claude Code produces.  On resume, the loader reconstructs
internal history from the transcript so the session can continue.

The writer is event-driven — called at lifecycle seam points by CustomBackend,
not by serializing _history.  Each write appends one JSON line and flushes,
so the transcript is crash-safe at record granularity.
"""

from __future__ import annotations

import json
import logging
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .types import ThinkingEvent, ToolCallEvent, Usage

log = logging.getLogger("kiln.transcript")

# Version string for transcript records — identifies the writer.
_VERSION = "kiln-0.1"
_ENTRYPOINT = "sdk-py"
_USER_TYPE = "external"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _git_branch(cwd: str) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd, capture_output=True, text=True, timeout=3,
        )
        return result.stdout.strip() if result.returncode == 0 else "HEAD"
    except Exception:
        return "HEAD"


class TranscriptWriter:
    """Appends Claude-compatible JSONL records for a custom-backend session.

    Instantiated by CustomBackend.start() when a transcript_path is provided.
    Called at lifecycle seam points — never reads or serializes _history.

    Record chaining: every record gets a unique uuid. parentUuid points to
    the immediately preceding record.  Tool-result records also carry
    sourceToolAssistantUUID linking back to the assistant that made the call.
    """

    def __init__(self, path: Path, *, cwd: str, model: str, session_id: str | None = None):
        self._path = path
        self._cwd = cwd
        self._model = model
        self._session_id = session_id or _new_uuid()
        self._git_branch = _git_branch(cwd)

        # Chain state
        self._last_uuid: str | None = None
        self._last_assistant_uuid: str | None = None

        # On resume (session_id provided + file exists), recover chain state
        # from the existing transcript so new records chain correctly.
        if session_id and path.exists():
            self._recover_chain_state(path)

        # Ensure directory exists
        path.parent.mkdir(parents=True, exist_ok=True)

        # Open append-only file handle
        self._fh = open(path, "a", encoding="utf-8")

    def _recover_chain_state(self, path: Path) -> None:
        """Read the last records from an existing transcript to restore chain state."""
        last_uuid = None
        last_assistant_uuid = None
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            uid = record.get("uuid")
            if uid:
                last_uuid = uid
            if record.get("type") == "assistant" and uid:
                last_assistant_uuid = uid
        self._last_uuid = last_uuid
        self._last_assistant_uuid = last_assistant_uuid

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def path(self) -> Path:
        return self._path

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None

    # -- Shared metadata envelope ------------------------------------------

    def _envelope(self, record_type: str, **extra) -> dict[str, Any]:
        uid = _new_uuid()
        rec = {
            "parentUuid": self._last_uuid,
            "isSidechain": False,
            "type": record_type,
            "uuid": uid,
            "timestamp": _now_iso(),
            "userType": _USER_TYPE,
            "entrypoint": _ENTRYPOINT,
            "cwd": self._cwd,
            "sessionId": self._session_id,
            "version": _VERSION,
            "gitBranch": self._git_branch,
        }
        rec.update(extra)
        self._last_uuid = uid
        return rec

    def _write(self, record: dict) -> None:
        self._fh.write(json.dumps(record, separators=(",", ":")) + "\n")
        self._fh.flush()

    # -- User records ------------------------------------------------------

    def write_user(
        self,
        content: str | list[dict],
        *,
        prompt_id: str | None = None,
    ) -> str:
        """Emit a user record.  Returns the record uuid.

        content: plain string or list of Claude-style content blocks for
        rich input (images, documents, etc.).
        """
        rec = self._envelope("user")
        if prompt_id:
            rec["promptId"] = prompt_id
        rec["message"] = {
            "role": "user",
            "content": content,
        }
        self._write(rec)
        return rec["uuid"]

    # -- Assistant records -------------------------------------------------

    def write_assistant(
        self,
        *,
        text: str = "",
        thinking_events: list[ThinkingEvent] | None = None,
        tool_calls: list[ToolCallEvent] | None = None,
        stop_reason: str | None = None,
        usage: Usage | None = None,
        model: str | None = None,
        request_id: str | None = None,
    ) -> str:
        """Emit a single assistant record for one provider call.

        Called after the provider stream completes with the buffered turn data.
        Content blocks use stable grouped ordering: thinking, then text, then
        tool_use.  This is a v1 simplification — exact provider event order
        is not preserved.  Returns the record uuid.
        """
        content: list[dict] = []

        # Thinking blocks
        if thinking_events:
            for te in thinking_events:
                block: dict[str, Any] = {
                    "type": "thinking",
                    "thinking": te.text,
                }
                if te.signature:
                    block["signature"] = te.signature
                content.append(block)

        # Text block
        if text:
            content.append({"type": "text", "text": text})

        # Tool use blocks
        if tool_calls:
            for tc in tool_calls:
                content.append({
                    "type": "tool_use",
                    "id": tc.id,
                    "name": tc.name,
                    "input": tc.input,
                })

        # Determine stop reason
        if stop_reason is None:
            stop_reason = "tool_use" if tool_calls else "end_turn"

        rec = self._envelope("assistant")
        if request_id:
            rec["requestId"] = request_id

        msg_id = _new_uuid()
        rec["message"] = {
            "id": f"msg_{msg_id.replace('-', '')[:24]}",
            "type": "message",
            "role": "assistant",
            "model": model or self._model,
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "stop_details": None,
            "usage": self._format_usage(usage) if usage else {},
        }

        self._write(rec)
        self._last_assistant_uuid = rec["uuid"]
        return rec["uuid"]

    # -- Tool result records -----------------------------------------------

    def write_tool_result(
        self,
        call_id: str,
        output: str,
        *,
        is_error: bool = False,
        rich_content: list[dict] | None = None,
    ) -> str:
        """Emit a user record containing a tool_result block.

        When rich_content is present (MCP blocks with images/files), the
        tool_result.content is a block list instead of a plain string.
        This preserves block structure per the spec's "do not over-normalize"
        rule.  When rich_content is None, content is the plain text output.

        Returns the record uuid.
        """
        # Build content: block list if rich, plain string otherwise
        if rich_content:
            content: str | list[dict] = self._map_rich_tool_content(rich_content)
        else:
            content = output

        result_block: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": call_id,
            "content": content,
        }
        if is_error:
            result_block["is_error"] = True

        rec = self._envelope("user")
        rec["message"] = {
            "role": "user",
            "content": [result_block],
        }

        # Convenience mirror field (matches Claude) — always text
        prefix = "Error: " if is_error else ""
        rec["toolUseResult"] = prefix + (output[:500] if len(output) > 500 else output)
        if self._last_assistant_uuid:
            rec["sourceToolAssistantUUID"] = self._last_assistant_uuid

        self._write(rec)
        return rec["uuid"]

    @staticmethod
    def _map_rich_tool_content(mcp_blocks: list[dict]) -> list[dict]:
        """Map MCP tool result content blocks to Claude-compatible transcript blocks."""
        import base64 as _b64
        result = []
        for block in mcp_blocks:
            btype = block.get("type", "")
            if btype == "text":
                result.append({"type": "text", "text": block.get("text", "")})
            elif btype == "image":
                data = block.get("data", "")
                mime = block.get("mimeType", "image/png")
                result.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": mime,
                        "data": data,
                    },
                })
            else:
                # Unknown block type — preserve as text representation
                result.append({"type": "text", "text": f"[{btype}]"})
        return result or [{"type": "text", "text": "(no output)"}]

    # -- Attachment records ------------------------------------------------

    def write_hook_additional_context(
        self,
        content: str,
        *,
        hook_name: str = "",
        tool_use_id: str = "",
    ) -> str:
        """Emit an attachment record for hook-injected context."""
        rec = self._envelope("attachment")
        rec["attachment"] = {
            "type": "hook_additional_context",
            "content": [content],
            "hookName": hook_name,
            "toolUseID": tool_use_id,
            "hookEvent": "PostToolUse",
        }
        self._write(rec)
        return rec["uuid"]

    def write_hook_stopped_continuation(
        self,
        *,
        hook_name: str = "",
        tool_use_id: str = "",
        message: str = "Execution stopped by PostToolUse hook",
    ) -> str:
        """Emit an attachment record for hook-stopped continuation."""
        rec = self._envelope("attachment")
        rec["attachment"] = {
            "type": "hook_stopped_continuation",
            "message": message,
            "hookName": hook_name,
            "toolUseID": tool_use_id,
            "hookEvent": "PostToolUse",
        }
        self._write(rec)
        return rec["uuid"]

    # -- System records ----------------------------------------------------

    def write_system_error(
        self,
        error_message: str,
        *,
        status: int | None = None,
        is_retryable: bool = False,
    ) -> str:
        """Emit a system record for provider/API errors."""
        rec = self._envelope("system")
        rec["subtype"] = "api_error"
        rec["level"] = "error"
        rec["error"] = {"message": error_message}
        if status:
            rec["error"]["status"] = status
        if is_retryable:
            rec["retryAttempt"] = 1
        self._write(rec)
        return rec["uuid"]

    # -- Last-prompt record ------------------------------------------------

    def write_last_prompt(self, prompt_text: str) -> None:
        """Emit a last-prompt record (not part of the chain)."""
        # last-prompt doesn't participate in uuid chaining
        rec = {
            "type": "last-prompt",
            "lastPrompt": prompt_text[:500],
            "sessionId": self._session_id,
        }
        self._fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
        self._fh.flush()

    # -- Helpers -----------------------------------------------------------

    @staticmethod
    def _format_usage(usage: Usage) -> dict:
        result: dict[str, Any] = {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
        }
        if usage.cache_read_tokens is not None:
            result["cache_read_input_tokens"] = usage.cache_read_tokens
        if usage.cache_write_tokens is not None:
            result["cache_creation_input_tokens"] = usage.cache_write_tokens
        return result


# ---------------------------------------------------------------------------
# Transcript Loader — reconstruct history from JSONL for resume
# ---------------------------------------------------------------------------

# Deferred import to avoid circular dependency at module level.
# The Turn types live in backends.custom which imports from types.
def _load_turn_types():
    from .backends.custom import (
        AssistantTurn,
        ContextInjection,
        ToolResultTurn,
        UserTurn,
    )
    return UserTurn, AssistantTurn, ToolResultTurn, ContextInjection


def load_transcript(path: Path) -> list:
    """Parse a Claude-compatible JSONL transcript into a list of Turn objects.

    Reconstructs the internal history that CustomBackend uses for
    _build_input().  Handles:
    - user records → UserTurn
    - assistant records → AssistantTurn (no raw_output_items — uses fallback path)
    - tool_result user records → ToolResultTurn
    - hook_additional_context attachments → ContextInjection
    - other record types (system, progress, last-prompt) → skipped

    Returns:
        List of Turn objects suitable for assigning to CustomBackend._history.
    """
    UserTurn, AssistantTurn, ToolResultTurn, ContextInjection = _load_turn_types()
    history: list = []

    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue

        rtype = record.get("type", "")
        msg = record.get("message", {})

        if rtype == "user":
            content = msg.get("content", "")

            # Tool result records have content as a list with tool_result blocks
            if isinstance(content, list):
                tool_results = [
                    b for b in content
                    if isinstance(b, dict) and b.get("type") == "tool_result"
                ]
                if tool_results:
                    for tr in tool_results:
                        raw_content = tr.get("content", "")
                        # Rich tool results store content as a block list;
                        # extract text since images/docs can't round-trip
                        # through resume.
                        if isinstance(raw_content, list):
                            text_parts = []
                            for block in raw_content:
                                if isinstance(block, dict):
                                    if block.get("type") == "text":
                                        text_parts.append(block.get("text", ""))
                                    elif block.get("type") in ("image", "document"):
                                        text_parts.append(f"[{block['type']}]")
                                    else:
                                        text_parts.append(str(block))
                            output_text = "\n".join(text_parts) if text_parts else ""
                        else:
                            output_text = str(raw_content)
                        history.append(ToolResultTurn(
                            call_id=tr.get("tool_use_id", ""),
                            output=output_text,
                            is_error=tr.get("is_error", False),
                        ))
                    continue

                # Rich user input (text/image/document blocks)
                # For now, reconstruct as text (images/docs won't round-trip
                # through resume — they're in the transcript for auditability)
                text_parts = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                        elif block.get("type") in ("image", "document"):
                            text_parts.append(f"[{block['type']}]")
                history.append(UserTurn(text="\n".join(text_parts)))
            else:
                history.append(UserTurn(text=str(content)))

        elif rtype == "assistant":
            content = msg.get("content", [])
            turn = AssistantTurn()

            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type", "")
                    if btype == "text":
                        turn.text += block.get("text", "")
                    elif btype == "thinking":
                        turn.thinking_text += block.get("thinking", "")
                    elif btype == "tool_use":
                        turn.tool_calls.append(ToolCallEvent(
                            id=block.get("id", ""),
                            name=block.get("name", ""),
                            input=block.get("input", {}),
                        ))

            history.append(turn)

        elif rtype == "attachment":
            attachment = record.get("attachment", {})
            atype = attachment.get("type", "")
            if atype == "hook_additional_context":
                text_content = attachment.get("content", [])
                text = text_content[0] if text_content else ""
                history.append(ContextInjection(text=str(text)))
            # hook_stopped_continuation: no history entry needed — it's a
            # flow-control marker, not conversation content

        # system, progress, last-prompt, queue-operation: skip

    return history


def transcript_session_id(path: Path) -> str | None:
    """Extract the sessionId from the first record of a transcript."""
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                sid = record.get("sessionId")
                if sid:
                    return sid
    except (OSError, json.JSONDecodeError):
        pass
    return None

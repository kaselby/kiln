"""ClaudeBackend — wraps the Claude Agent SDK / CC CLI.

Thin wrapper: the SDK owns the agentic loop, tool dispatch, and hook
execution.  This backend maps SDK message types to Kiln Events so the
harness and TUI never see SDK internals.
"""

import asyncio
import base64 as _b64
import logging
from collections.abc import AsyncIterator

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from ..types import (
    BackendConfig,
    ContentBlockDeltaEvent,
    ContentBlockEndEvent,
    ContentBlockStartEvent,
    ContentBlock,
    DocumentContent,
    ErrorEvent,
    Event,
    SystemMessageEvent,
    TextContent,
    TextEvent,
    ThinkingEvent,
    ToolCallEvent,
    ToolResultEvent,
    TurnCompleteEvent,
    Usage,
    UsageUpdateEvent,
)

log = logging.getLogger("kiln.backends.claude")


class ClaudeBackend:
    """Backend wrapping the Claude Agent SDK.

    CC owns the agentic loop — it handles tool dispatch, hooks, and
    permissions internally.  Kiln is a supervisor: it sends messages,
    receives SDK messages, and maps them to Kiln Events.
    """

    def __init__(self) -> None:
        self._client: ClaudeSDKClient | None = None
        self._config: BackendConfig | None = None
        # Accumulated state across stream events within a turn.
        self._block_index: int = 0
        self._pending_usage: dict | None = None
        self._last_model: str | None = None
        self._current_block_type: str = ""

    async def start(self, config: BackendConfig) -> None:
        options = self._build_sdk_options(config)
        self._client = ClaudeSDKClient(options)
        self._config = config
        await self._client.connect()

    def _build_sdk_options(self, config: BackendConfig) -> ClaudeAgentOptions:
        opts = dict(
            system_prompt=config.system_prompt,
            tools=config.base_tools,
            allowed_tools=[],
            hooks=config.hooks,
            mcp_servers=config.mcp_servers,
            model=config.model,
            cwd=config.cwd,
            env=config.env,
            permission_mode=config.permission_mode,
            include_partial_messages=True,
            continue_conversation=False,
            resume=config.resume_conversation_id,
            stderr=config.stderr_callback,
            extra_args=config.extra_args,
            max_buffer_size=10 * 1024 * 1024,  # 10MB — images as base64 exceed default 1MB
        )
        if config.effort:
            opts["effort"] = config.effort
        return ClaudeAgentOptions(**opts)

    async def send(self, message: str | list[ContentBlock]) -> None:
        if not self._client:
            raise RuntimeError("ClaudeBackend not started")
        if isinstance(message, str):
            await self._client.query(message)
        else:
            await self._client.query(self._build_rich_message(message))

    async def receive(self) -> AsyncIterator[Event]:
        if not self._client:
            raise RuntimeError("ClaudeBackend not started")

        self._block_index = 0
        self._pending_usage = None
        timeout = self._config.stream_timeout if self._config else None

        if not timeout:
            async for msg in self._client.receive_response():
                for event in self._map_sdk_message(msg):
                    yield event
            return

        # Stream timeout: interrupt on stall, drain, signal error.
        ait = self._client.receive_response().__aiter__()
        while True:
            try:
                msg = await asyncio.wait_for(anext(ait), timeout=timeout)
                for event in self._map_sdk_message(msg):
                    yield event
            except StopAsyncIteration:
                return
            except asyncio.TimeoutError:
                log.warning(
                    "Stream stall: no data for %ds. Interrupting.", int(timeout)
                )
                try:
                    await asyncio.wait_for(self._client.interrupt(), timeout=10)
                except Exception:
                    pass
                # Drain remaining messages — original iterator is dead
                # (asyncio.wait_for cancelled it). Fresh one.
                try:
                    drain = self._client.receive_response().__aiter__()
                    while True:
                        msg = await asyncio.wait_for(anext(drain), timeout=15)
                        for event in self._map_sdk_message(msg):
                            yield event
                except (StopAsyncIteration, asyncio.TimeoutError, Exception):
                    pass
                yield ErrorEvent(
                    message=(
                        "Stream stalled — no data for %d seconds. "
                        "The stalled turn was interrupted." % int(timeout)
                    ),
                    is_retryable=True,
                )
                return

    async def interrupt(self) -> None:
        if self._client:
            await self._client.interrupt()

    async def stop(self) -> None:
        if self._client:
            await self._client.disconnect()
            self._client = None

    # ------------------------------------------------------------------
    # SDK message → Kiln Event mapping
    # ------------------------------------------------------------------

    def _map_sdk_message(self, msg: object) -> list[Event]:
        if isinstance(msg, StreamEvent):
            return self._map_stream_event(msg)
        elif isinstance(msg, AssistantMessage):
            return self._map_assistant_message(msg)
        elif isinstance(msg, UserMessage):
            return self._map_user_message(msg)
        elif isinstance(msg, ResultMessage):
            return self._map_result_message(msg)
        elif isinstance(msg, SystemMessage):
            return self._map_system_message(msg)
        return []

    def _map_stream_event(self, msg: StreamEvent) -> list[Event]:
        event = msg.event
        etype = event.get("type", "")
        events: list[Event] = []

        if etype == "content_block_start":
            cb = event.get("content_block", {})
            cb_type = cb.get("type", "text")
            kiln_type = {
                "text": "text",
                "thinking": "thinking",
                "tool_use": "tool_call",
            }.get(cb_type, "text")
            self._current_block_type = kiln_type
            events.append(ContentBlockStartEvent(
                content_index=self._block_index,
                content_type=kiln_type,
            ))

        elif etype == "content_block_delta":
            delta = event.get("delta", {})
            delta_type = delta.get("type", "")
            if delta_type == "text_delta":
                text = delta.get("text", "")
                if text:
                    events.append(ContentBlockDeltaEvent(
                        content_index=self._block_index,
                        text=text,
                    ))
            elif delta_type == "thinking_delta":
                thinking = delta.get("thinking", "")
                if thinking:
                    events.append(ContentBlockDeltaEvent(
                        content_index=self._block_index,
                        thinking=thinking,
                    ))
            elif delta_type == "signature_delta":
                # Signature deltas are internal — stored on ThinkingBlock,
                # surfaced when we map the AssistantMessage.
                pass

        elif etype == "content_block_stop":
            events.append(ContentBlockEndEvent(
                content_index=self._block_index,
                content_type=self._current_block_type,
            ))
            self._block_index += 1
            self._current_block_type = ""

        elif etype == "message_delta":
            usage = event.get("usage")
            if usage:
                self._pending_usage = usage
                parsed = self._extract_usage(usage)
                if parsed:
                    events.append(UsageUpdateEvent(usage=parsed))

        return events

    def _map_assistant_message(self, msg: AssistantMessage) -> list[Event]:
        """Map AssistantMessage to content events.

        NOT a turn completion — this arrives mid-turn (before tool results).
        TurnCompleteEvent is only emitted from ResultMessage.
        """
        events: list[Event] = []

        # Stash model and usage for the TurnCompleteEvent from ResultMessage.
        self._last_model = msg.model
        if msg.usage:
            self._pending_usage = msg.usage

        for block in msg.content:
            if isinstance(block, TextBlock) and block.text:
                events.append(TextEvent(text=block.text))
            elif isinstance(block, ThinkingBlock):
                events.append(ThinkingEvent(
                    text=getattr(block, "thinking", "") or "",
                    signature=getattr(block, "signature", None),
                    redacted=getattr(block, "type", "") == "redacted_thinking",
                ))
            elif isinstance(block, ToolUseBlock):
                events.append(ToolCallEvent(
                    id=block.id,
                    name=block.name,
                    input=block.input,
                ))

        return events

    def _map_user_message(self, msg: UserMessage) -> list[Event]:
        events: list[Event] = []
        content = msg.content
        if isinstance(content, list):
            for block in content:
                if isinstance(block, ToolResultBlock):
                    # Extract text from content (may be str or list of blocks)
                    output = self._extract_tool_result_text(block.content)
                    events.append(ToolResultEvent(
                        tool_call_id=block.tool_use_id,
                        output=output,
                        is_error=block.is_error,
                    ))
        return events

    def _map_result_message(self, msg: ResultMessage) -> list[Event]:
        # Prefer streaming/AssistantMessage usage (per-call, reflects actual
        # context window size) over ResultMessage usage (cumulative across
        # the SDK's agentic loop — inflates context display).
        usage = self._extract_usage(self._pending_usage) if self._pending_usage else None
        if not usage and msg.usage:
            usage = self._extract_usage(msg.usage)

        model = self._last_model
        self._last_model = None
        self._pending_usage = None

        return [TurnCompleteEvent(
            stop_reason=msg.stop_reason,
            session_id=msg.session_id,
            usage=usage,
            model=model,
        )]

    def _map_system_message(self, msg: SystemMessage) -> list[Event]:
        return [SystemMessageEvent(
            subtype=msg.subtype,
            text=getattr(msg, "text", None),
        )]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _extract_usage(self, raw: dict | None) -> Usage | None:
        if not raw:
            return None
        cache_read = raw.get("cache_read_input_tokens")
        cache_write = raw.get("cache_creation_input_tokens")
        input_t = raw.get("input_tokens", 0) or 0
        output_t = raw.get("output_tokens", 0) or 0
        return Usage(
            input_tokens=input_t,
            output_tokens=output_t,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            total_tokens=input_t + output_t + (cache_read or 0) + (cache_write or 0),
        )

    @staticmethod
    def _extract_tool_result_text(content) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif isinstance(block, str):
                    parts.append(block)
            return "\n".join(parts)
        return str(content)

    @staticmethod
    def _build_rich_message(blocks: list[ContentBlock]):
        """Convert Kiln ContentBlocks to an async iter for client.query()."""
        content_parts = []
        for block in blocks:
            if isinstance(block, TextContent):
                content_parts.append({"type": "text", "text": block.text})
            elif isinstance(block, DocumentContent):
                content_parts.append({
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": block.mime_type,
                        "data": _b64.b64encode(block.data).decode("ascii"),
                    },
                })

        async def _iter():
            yield {
                "type": "user",
                "message": {"role": "user", "content": content_parts},
                "parent_tool_use_id": None,
            }

        return _iter()

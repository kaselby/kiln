"""Custom agentic loop backend — Kiln IS the loop.

Uses a Provider for LLM API calls and manages the full tool-use cycle:
send → stream → tool dispatch → hooks → repeat. This is the backend for
any non-Claude model (OpenAI, Google, local, etc.).
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import base64 as _b64

from ..types import (
    BackendConfig,
    ContentBlock,
    DocumentContent,
    ErrorEvent,
    Event,
    HookDispatcher,
    TextContent,
    TextEvent,
    ThinkingEvent,
    ToolCallEvent,
    ToolDef,
    ToolResultEvent,
    TurnCompleteEvent,
    Usage,
    Provider,
)

log = logging.getLogger("kiln.backends.custom")


# ---------------------------------------------------------------------------
# Conversation history types — internal to CustomBackend
# ---------------------------------------------------------------------------

@dataclass
class UserTurn:
    text: str = ""
    content_blocks: list[ContentBlock] | None = None

@dataclass
class AssistantTurn:
    raw_output_items: list[Any] = field(default_factory=list)
    tool_calls: list[ToolCallEvent] = field(default_factory=list)
    text: str = ""
    thinking_text: str = ""

@dataclass
class ToolResultTurn:
    call_id: str
    output: str
    is_error: bool = False
    rich_content: list[dict] | None = None  # Raw MCP content blocks (images, etc.)

@dataclass
class ContextInjection:
    text: str

Turn = UserTurn | AssistantTurn | ToolResultTurn | ContextInjection


# ---------------------------------------------------------------------------
# CustomBackend
# ---------------------------------------------------------------------------

class CustomBackend:
    """Hand-rolled agentic loop. Kiln IS the loop.

    The harness drives this via start/send/receive/stop. Internally,
    receive() loops: call provider → process streaming events → execute
    tool calls → inject hook results → call provider again — until the
    model produces a final text response with no tool calls.
    """

    def __init__(self, provider: Provider):
        self._provider = provider
        self._config: BackendConfig | None = None
        self._history: list[Turn] = []
        self._tool_registry: dict[str, ToolDef] = {}
        self._hook_dispatcher: HookDispatcher | None = None
        self._has_pending_turn: bool = False
        self._turn_start_time: float = 0
        self._turn_count: int = 0
        self._interrupted: bool = False

    async def start(self, config: BackendConfig) -> None:
        self._config = config
        self._tool_registry = {t.name: t for t in config.tool_defs}
        self._hook_dispatcher = config.hook_dispatcher
        self._supplemental = config.supplemental

    async def send(self, message: str | list[ContentBlock]) -> None:
        if isinstance(message, str):
            self._history.append(UserTurn(text=message))
        elif isinstance(message, list):
            self._history.append(UserTurn(content_blocks=message))
        else:
            self._history.append(UserTurn(text=str(message)))
        self._has_pending_turn = True
        self._turn_start_time = time.monotonic()
        self._turn_count = 0
        self._interrupted = False

    async def receive(self) -> AsyncIterator[Event]:
        if not self._config or not self._provider:
            raise RuntimeError("Backend not started.")

        # Accumulate usage across agentic sub-turns so the final
        # TurnCompleteEvent reports total usage for the whole turn.
        cumulative_usage = Usage()
        last_turn_complete: TurnCompleteEvent | None = None

        while self._has_pending_turn and not self._interrupted:
            self._has_pending_turn = False
            self._turn_count += 1

            # Build provider input
            input_items = self._build_input()

            # Apply message transforms before sending to provider
            input_items = self._apply_transforms(input_items)

            # Build generation params
            gen_params = self._generation_params()

            # Stream from provider
            tool_calls: list[ToolCallEvent] = []
            assistant_turn = AssistantTurn()
            raw_output_collector: list[dict] = []

            try:
                stream = self._provider.stream(
                    model=self._config.model,
                    instructions=self._config.system_prompt,
                    input=input_items,
                    tools=self._provider.build_tool_schemas(self._config.tool_defs),
                    raw_output_collector=raw_output_collector,
                    **gen_params,
                )

                async for event in stream:
                    if self._interrupted:
                        break

                    # Capture data for history before yielding
                    if isinstance(event, ToolCallEvent):
                        tool_calls.append(event)
                        assistant_turn.tool_calls.append(event)
                    elif isinstance(event, TextEvent):
                        assistant_turn.text += event.text
                    elif isinstance(event, ThinkingEvent):
                        assistant_turn.thinking_text += event.text
                    elif isinstance(event, TurnCompleteEvent):
                        # Don't yield intermediate TurnCompleteEvents — the
                        # TUI expects exactly one per user turn. Accumulate
                        # usage and hold the event for emission at loop exit.
                        if event.usage:
                            cumulative_usage = _accumulate_usage(
                                cumulative_usage, event.usage,
                            )
                        last_turn_complete = event
                        continue

                    yield event

            except Exception as e:
                log.error("Provider stream error: %s", e)
                yield ErrorEvent(
                    message=f"Provider error: {e}",
                    is_retryable=_is_retryable_error(e),
                )
                return

            # Capture raw output items from the provider for multi-turn replay
            if raw_output_collector:
                assistant_turn.raw_output_items = raw_output_collector

            # Store assistant turn in history
            self._history.append(assistant_turn)

            if self._interrupted or not tool_calls:
                break

            # Process tool calls
            hook_stopped = False
            for tc in tool_calls:
                if self._interrupted:
                    break

                # Pre-tool hook (permission check)
                if self._hook_dispatcher:
                    hook_result = await self._hook_dispatcher.pre_tool(
                        tc.name, tc.input,
                    )
                    if hook_result.denied:
                        reason = hook_result.reason or "Permission denied"
                        self._history.append(ToolResultTurn(
                            call_id=tc.id, output=reason, is_error=True,
                        ))
                        yield ToolResultEvent(
                            tool_call_id=tc.id, output=reason, is_error=True,
                        )
                        continue

                # Execute tool
                result_text, result_error, rich_content = await self._execute_tool(tc)

                # If the provider can't deliver rich content natively,
                # stash it for supplemental injection by the harness.
                stashed_for_injection = False
                if rich_content and self._supplemental:
                    if self._provider.build_rich_tool_result(rich_content) is None:
                        self._stash_rich_content(rich_content)
                        rich_content = None
                        stashed_for_injection = True

                yield ToolResultEvent(
                    tool_call_id=tc.id,
                    output=result_text,
                    is_error=result_error,
                )

                # Append tool result BEFORE context injection — APIs expect
                # function_call_output immediately after the function_call.
                tool_result_turn = ToolResultTurn(
                    call_id=tc.id, output=result_text, is_error=result_error,
                    rich_content=rich_content,
                )
                self._history.append(tool_result_turn)

                # Post-tool hook (context injection + flow control)
                if self._hook_dispatcher:
                    post_result = await self._hook_dispatcher.post_tool(
                        tc.name, tc.input, result_text,
                    )
                    if post_result.updated_tool_output is not None:
                        tool_result_turn.output = post_result.updated_tool_output
                    if post_result.additional_context:
                        self._history.append(
                            ContextInjection(text=post_result.additional_context),
                        )
                    if not post_result.continue_:
                        hook_stopped = True
                        break
                if stashed_for_injection:
                    hook_stopped = True
                    break

            # Tool calls processed — continue the agentic loop unless
            # a hook or stash requested a stop.
            if not self._interrupted and not hook_stopped:
                self._has_pending_turn = True

        # Emit a single TurnCompleteEvent with accumulated usage
        if last_turn_complete is not None:
            yield TurnCompleteEvent(
                stop_reason=last_turn_complete.stop_reason,
                usage=cumulative_usage,
                session_id=last_turn_complete.session_id,
                model=last_turn_complete.model,
            )

    async def interrupt(self) -> None:
        self._interrupted = True
        self._has_pending_turn = False

    async def stop(self) -> None:
        if self._provider:
            try:
                await self._provider.close()
            except Exception:
                log.debug("Provider close failed", exc_info=True)
        self._history.clear()
        self._tool_registry.clear()
        self._hook_dispatcher = None
        self._config = None

    # -------------------------------------------------------------------
    # Input assembly
    # -------------------------------------------------------------------

    def _build_input(self) -> list[dict[str, Any]]:
        """Build provider input from conversation history.

        Returns a list of items in a generic format — the provider's
        stream() method receives these and maps to its specific API format.
        """
        items: list[dict[str, Any]] = []

        for turn in self._history:
            if isinstance(turn, UserTurn):
                if turn.content_blocks:
                    items.append(self._build_rich_user_message(turn))
                else:
                    items.append({
                        "role": "user",
                        "content": turn.text,
                    })

            elif isinstance(turn, AssistantTurn):
                if turn.raw_output_items:
                    # Round-trip raw provider items for multi-turn continuity.
                    # Strip output-only fields that APIs reject as input.
                    for raw in turn.raw_output_items:
                        items.append(_strip_output_only_fields(raw))
                else:
                    # Fallback: reconstruct from our parsed data
                    parts = []
                    if turn.text:
                        parts.append({"type": "text", "text": turn.text})
                    for tc in turn.tool_calls:
                        parts.append({
                            "type": "tool_call",
                            "id": tc.id,
                            "name": tc.name,
                            "arguments": json.dumps(tc.input),
                        })
                    if parts:
                        items.append({
                            "role": "assistant",
                            "content": parts,
                        })

            elif isinstance(turn, ToolResultTurn):
                output: str | list[dict] = turn.output
                if turn.rich_content:
                    rich = self._provider.build_rich_tool_result(turn.rich_content)
                    if rich is not None:
                        output = rich
                items.append({
                    "type": "function_call_output",
                    "call_id": turn.call_id,
                    "output": output,
                })

            elif isinstance(turn, ContextInjection):
                items.append({
                    "role": self._provider.context_injection_role,
                    "content": turn.text,
                })

        return items

    def _build_rich_user_message(self, turn: UserTurn) -> dict[str, Any]:
        """Convert ContentBlock list to a user message.

        Delegates format-specific encoding to the provider — each provider
        knows its own API format for images and documents.
        """
        parts = []
        for block in turn.content_blocks or []:
            if isinstance(block, TextContent):
                parts.append({"type": "input_text", "text": block.text})
            elif isinstance(block, DocumentContent):
                if block.mime_type.startswith("image/"):
                    parts.append(
                        self._provider.build_image_content(
                            block.data, block.mime_type,
                        )
                    )
                else:
                    # Ensure filename has extension for type detection
                    filename = block.label
                    if "." not in filename:
                        ext = block.mime_type.rsplit("/", 1)[-1]
                        filename = f"{filename}.{ext}"
                    parts.append(
                        self._provider.build_document_content(
                            block.data, block.mime_type, filename,
                        )
                    )
        if not parts:
            parts.append({"type": "input_text", "text": ""})
        return {"role": "user", "content": parts}

    # -------------------------------------------------------------------
    # Message transforms (spec §7)
    # -------------------------------------------------------------------

    def _apply_transforms(self, items: list[dict]) -> list[dict]:
        """Apply the message transform pipeline before each provider call.

        For v1 (single-provider sessions), most transforms are no-ops.
        The critical one is orphaned tool call handling for interrupted streams.
        """
        items = self._filter_errored_turns(items)
        items = self._insert_orphaned_results(items)
        return items

    def _filter_errored_turns(self, items: list[dict]) -> list[dict]:
        """Drop assistant turns with error/aborted stop reasons."""
        return [
            item for item in items
            if not (
                isinstance(item, dict)
                and item.get("role") == "assistant"
                and item.get("stop_reason") in ("error", "aborted")
            )
        ]

    def _insert_orphaned_results(self, items: list[dict]) -> list[dict]:
        """Insert synthetic error results for tool calls without matching results."""
        result = []
        pending_call_ids: set[str] = set()

        for item in items:
            # Flush pending orphans before a new assistant turn or user message
            if item.get("role") in ("assistant", "user") and pending_call_ids:
                for call_id in pending_call_ids:
                    result.append({
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": "Tool execution was interrupted — no result available.",
                    })
                pending_call_ids = set()

            # Track tool calls from assistant turns
            if item.get("role") == "assistant":
                content = item.get("content", [])
                if isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "tool_call":
                            pending_call_ids.add(part.get("id", ""))

            # Track function call items (OpenAI Responses format)
            if item.get("type") == "function_call":
                pending_call_ids.add(item.get("call_id", ""))

            # Remove matched tool results
            if item.get("type") == "function_call_output":
                pending_call_ids.discard(item.get("call_id", ""))

            result.append(item)

        # Flush remaining orphans at end
        for call_id in pending_call_ids:
            result.append({
                "type": "function_call_output",
                "call_id": call_id,
                "output": "Tool execution was interrupted — no result available.",
            })

        return result

    # -------------------------------------------------------------------
    # Tool execution
    # -------------------------------------------------------------------

    def _stash_rich_content(self, content_blocks: list[dict]) -> None:
        """Stash rich content blocks in supplemental for harness injection.

        Called when the provider can't deliver rich content natively in
        tool results. Converts MCP content blocks to raw bytes for the
        harness's backend-agnostic supplemental injection pipeline.
        """
        for block in content_blocks:
            btype = block.get("type", "")
            if btype == "image":
                data = _b64.b64decode(block.get("data", ""))
                mime = block.get("mimeType", "image/png")
                self._supplemental.add_file(data, mime, "tool-image")

    async def _execute_tool(
        self, tc: ToolCallEvent,
    ) -> tuple[str, bool, list[dict] | None]:
        """Execute a tool call. Returns (text_output, is_error, rich_content).

        MCP results can contain text, images, and other block types. The text
        representation is always produced (for ToolResultEvent / TUI display).
        When non-text content is present, the raw MCP content blocks are
        preserved in rich_content so providers can include them natively in
        tool results.
        """
        tool = self._tool_registry.get(tc.name)
        if not tool:
            return f"Unknown tool: {tc.name}", True, None

        try:
            result = await tool.handler(tc.input)
            is_error = result.get("isError", False)
            content = result.get("content", [])

            if not content or not isinstance(content, list):
                return str(result), is_error, None

            text_parts = []
            has_rich = False
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type", "")
                if btype == "text" and block.get("text"):
                    text_parts.append(block["text"])
                elif btype == "image":
                    mime = block.get("mimeType", "image/png")
                    text_parts.append(f"[Image: {mime}]")
                    has_rich = True
                else:
                    # Any non-text block type — preserve for provider
                    has_rich = True

            text = "\n".join(text_parts) if text_parts else "(no output)"
            return text, is_error, content if has_rich else None
        except Exception as e:
            log.error("Tool %s raised: %s", tc.name, e)
            return f"Tool execution error: {e}", True, None

    # -------------------------------------------------------------------
    # Generation params
    # -------------------------------------------------------------------

    def _generation_params(self) -> dict[str, Any]:
        """Build provider-agnostic generation parameters from BackendConfig."""
        params: dict[str, Any] = {}
        if self._config.temperature is not None:
            params["temperature"] = self._config.temperature
        if self._config.max_output_tokens is not None:
            params["max_output_tokens"] = self._config.max_output_tokens
        if self._config.effort:
            params["reasoning"] = _map_thinking_level(self._config.effort)
        return params


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Fields that appear in API responses but are rejected as input parameters.
_OUTPUT_ONLY_FIELDS = {"status", "namespace"}


def _strip_output_only_fields(item: dict) -> dict:
    """Remove output-only fields from a raw API item for input replay.

    The Responses API includes metadata fields (e.g. status, namespace) on
    output items that it rejects when sent back as input.
    """
    if not any(k in item for k in _OUTPUT_ONLY_FIELDS):
        return item
    return {k: v for k, v in item.items() if k not in _OUTPUT_ONLY_FIELDS}


def _accumulate_usage(total: Usage, turn: Usage) -> Usage:
    """Merge usage across agentic sub-turns.

    Output tokens and total are summed (cumulative generation).
    Input and cache tokens take the latest sub-turn's values — they
    reflect the current context window size, not billable volume.
    """
    return Usage(
        input_tokens=turn.input_tokens,
        output_tokens=total.output_tokens + turn.output_tokens,
        cache_read_tokens=turn.cache_read_tokens,
        cache_write_tokens=turn.cache_write_tokens,
        total_tokens=total.total_tokens + turn.total_tokens,
    )


def _map_thinking_level(effort: str) -> dict[str, Any] | None:
    """Map Kiln effort level to OpenAI reasoning config."""
    mapping = {
        "low": {"effort": "low", "summary": "auto"},
        "medium": {"effort": "medium", "summary": "auto"},
        "high": {"effort": "high", "summary": "auto"},
        "max": {"effort": "xhigh", "summary": "auto"},
    }
    return mapping.get(effort)


def _is_retryable_error(e: Exception) -> bool:
    """Classify whether an exception is retryable."""
    msg = str(e).lower()
    if "rate limit" in msg or "429" in msg:
        return True
    if "500" in msg or "502" in msg or "503" in msg or "overloaded" in msg:
        return True
    return False

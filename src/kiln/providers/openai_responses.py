"""OpenAI Responses API provider.

Handles streaming communication with OpenAI's Responses API, mapping
SSE events to Kiln's event types. Used by CustomBackend for GPT-5.x
and other OpenAI reasoning models.

Stateless mode: we manage conversation context ourselves (no store,
no previous_response_id). Each call gets the full input array.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from openai import (
    APIConnectionError,
    APIStatusError,
    AsyncOpenAI,
    RateLimitError,
)
from openai.types.responses import (
    Response,
    ResponseCompletedEvent,
    ResponseContentPartAddedEvent,
    ResponseContentPartDoneEvent,
    ResponseCreatedEvent,
    ResponseErrorEvent,
    ResponseFailedEvent,
    ResponseFunctionCallArgumentsDeltaEvent,
    ResponseFunctionCallArgumentsDoneEvent,
    ResponseFunctionToolCall,
    ResponseInProgressEvent,
    ResponseIncompleteEvent,
    ResponseOutputItemAddedEvent,
    ResponseOutputItemDoneEvent,
    ResponseOutputMessage,
    ResponseReasoningItem,
    ResponseReasoningSummaryPartAddedEvent,
    ResponseReasoningSummaryPartDoneEvent,
    ResponseReasoningSummaryTextDeltaEvent,
    ResponseReasoningSummaryTextDoneEvent,
    ResponseRefusalDeltaEvent,
    ResponseRefusalDoneEvent,
    ResponseTextDeltaEvent,
    ResponseTextDoneEvent,
)

from ..types import (
    ContentBlockDeltaEvent,
    ContentBlockEndEvent,
    ContentBlockStartEvent,
    ErrorEvent,
    Event,
    TextEvent,
    ThinkingEvent,
    ToolCallEvent,
    ToolDef,
    TurnCompleteEvent,
    Usage,
)

log = logging.getLogger("kiln.providers.openai")


class _RetryableStreamError(Exception):
    """Raised for stream-level errors that should trigger retry."""
    pass


# Retry config
_MAX_RETRIES = 3
_BASE_DELAY = 1.0  # seconds
_MAX_DELAY = 10.0

# ThinkingLevel -> OpenAI reasoning effort
_EFFORT_MAP: dict[str, str] = {
    "off": "minimal",  # GPT-5+ always reasons — fall back to minimal
    "minimal": "minimal",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "max": "high",
}


@dataclass
class _StreamState:
    """Accumulator for a single streaming response."""
    block_index: int = 0
    current_item_type: str | None = None
    partial_thinking: str = ""
    partial_text: str = ""
    partial_args: str = ""
    current_call_id: str | None = None
    current_item_id: str | None = None
    current_tool_name: str | None = None
    def reset_block(self):
        self.current_item_type = None
        self.partial_thinking = ""
        self.partial_text = ""
        self.partial_args = ""
        self.current_call_id = None
        self.current_item_id = None
        self.current_tool_name = None
        self.block_index += 1


class OpenAIResponsesProvider:
    """OpenAI Responses API provider.

    Makes streaming API calls to OpenAI's Responses endpoint and yields
    Kiln Events. Handles retry with exponential backoff for transient errors.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        session_id: str | None = None,
    ):
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self._session_id = session_id

    async def stream(
        self,
        *,
        model: str,
        instructions: str,
        input: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        reasoning: dict[str, Any] | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        extra_params: dict[str, Any] | None = None,
        raw_output_collector: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[Event]:
        """Stream a model response, yielding Kiln Events.

        If raw_output_collector is provided, raw API output items from
        output_item.done events are appended to it. The CustomBackend
        uses this to store conversation history for multi-turn replay.

        Retries on transient errors (5xx, rate limits, connection issues)
        with exponential backoff.
        """
        params: dict[str, Any] = {
            "model": model,
            "input": input,
            "instructions": instructions,
            "stream": True,
            "store": False,
            "tool_choice": "auto",
            "parallel_tool_calls": True,
        }

        if tools:
            params["tools"] = tools

        if reasoning:
            params["reasoning"] = reasoning
            params["include"] = ["reasoning.encrypted_content"]

        if self._session_id:
            params["prompt_cache_key"] = self._session_id

        if temperature is not None:
            params["temperature"] = temperature

        if max_output_tokens is not None:
            params["max_output_tokens"] = max_output_tokens

        if extra_params:
            params.update(extra_params)

        # Track whether any events have been yielded to the consumer.
        # Once events are yielded, mid-stream retry would produce duplicates,
        # so we fall through to an ErrorEvent instead.
        has_yielded = [False]

        last_error: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            if attempt > 0:
                # Don't retry if events have already been yielded — the
                # consumer has seen partial content and retry would duplicate.
                if has_yielded[0]:
                    yield ErrorEvent(
                        message=f"Stream failed after partial delivery: {last_error}",
                        is_retryable=True,
                    )
                    return

                delay = min(_BASE_DELAY * (2 ** (attempt - 1)), _MAX_DELAY)
                if isinstance(last_error, APIStatusError):
                    retry_after = last_error.response.headers.get("retry-after")
                    if retry_after:
                        try:
                            delay = max(delay, float(retry_after))
                        except ValueError:
                            pass
                log.warning(
                    "Retry %d/%d after %.1fs (error: %s)",
                    attempt, _MAX_RETRIES, delay, last_error,
                )
                await asyncio.sleep(delay)

            try:
                stream = await self._client.responses.create(**params)
                state = _StreamState()
                async for event in self._process_stream(stream, state, raw_output_collector):
                    has_yielded[0] = True
                    yield event
                return  # Success — done
            except RateLimitError as e:
                last_error = e
                if attempt == _MAX_RETRIES:
                    yield ErrorEvent(
                        message=f"Rate limited after {_MAX_RETRIES} retries: {e}",
                        is_retryable=False,
                    )
                    return
            except (APIConnectionError, _RetryableStreamError) as e:
                last_error = e
                if attempt == _MAX_RETRIES:
                    yield ErrorEvent(
                        message=f"Connection error after {_MAX_RETRIES} retries: {e}",
                        is_retryable=False,
                    )
                    return
            except APIStatusError as e:
                if e.status_code >= 500:
                    last_error = e
                    if attempt == _MAX_RETRIES:
                        yield ErrorEvent(
                            message=f"Server error after {_MAX_RETRIES} retries: {e}",
                            is_retryable=False,
                        )
                        return
                else:
                    # 4xx — not retryable
                    yield ErrorEvent(
                        message=f"API error ({e.status_code}): {e.message}",
                        is_retryable=False,
                    )
                    return
            except Exception as e:
                yield ErrorEvent(
                    message=f"Unexpected error: {e}",
                    is_retryable=False,
                )
                return

    async def _process_stream(
        self,
        stream,
        state: _StreamState,
        raw_output_collector: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[Event]:
        """Map OpenAI Responses API stream events to Kiln Events."""
        has_tool_calls = False

        async for event in stream:
            # -- Output item lifecycle --
            if isinstance(event, ResponseOutputItemAddedEvent):
                item = event.item
                if isinstance(item, ResponseReasoningItem):
                    state.current_item_type = "thinking"
                    state.partial_thinking = ""
                    yield ContentBlockStartEvent(
                        content_index=state.block_index,
                        content_type="thinking",
                    )
                elif isinstance(item, ResponseOutputMessage):
                    state.current_item_type = "text"
                    state.partial_text = ""
                    yield ContentBlockStartEvent(
                        content_index=state.block_index,
                        content_type="text",
                    )
                elif isinstance(item, ResponseFunctionToolCall):
                    state.current_item_type = "tool_call"
                    state.partial_args = item.arguments or ""
                    state.current_call_id = item.call_id
                    state.current_item_id = item.id
                    state.current_tool_name = item.name
                    has_tool_calls = True
                    yield ContentBlockStartEvent(
                        content_index=state.block_index,
                        content_type="tool_call",
                    )

            # -- Reasoning summary deltas --
            elif isinstance(event, ResponseReasoningSummaryTextDeltaEvent):
                if state.current_item_type == "thinking":
                    state.partial_thinking += event.delta
                    yield ContentBlockDeltaEvent(
                        content_index=state.block_index,
                        thinking=event.delta,
                    )

            elif isinstance(event, ResponseReasoningSummaryPartDoneEvent):
                if state.current_item_type == "thinking":
                    state.partial_thinking += "\n\n"
                    yield ContentBlockDeltaEvent(
                        content_index=state.block_index,
                        thinking="\n\n",
                    )

            # -- Text deltas --
            elif isinstance(event, ResponseTextDeltaEvent):
                if state.current_item_type == "text":
                    state.partial_text += event.delta
                    yield ContentBlockDeltaEvent(
                        content_index=state.block_index,
                        text=event.delta,
                    )

            elif isinstance(event, ResponseRefusalDeltaEvent):
                if state.current_item_type == "text":
                    state.partial_text += event.delta
                    yield ContentBlockDeltaEvent(
                        content_index=state.block_index,
                        text=event.delta,
                    )

            # -- Function call argument deltas --
            elif isinstance(event, ResponseFunctionCallArgumentsDeltaEvent):
                if state.current_item_type == "tool_call":
                    state.partial_args += event.delta
                    yield ContentBlockDeltaEvent(
                        content_index=state.block_index,
                        text=event.delta,
                    )

            elif isinstance(event, ResponseFunctionCallArgumentsDoneEvent):
                if state.current_item_type == "tool_call":
                    state.partial_args = event.arguments

            # -- Item done (finalization) --
            elif isinstance(event, ResponseOutputItemDoneEvent):
                item = event.item

                # Capture raw item for conversation replay
                if raw_output_collector is not None:
                    try:
                        raw_output_collector.append(item.model_dump())
                    except Exception:
                        log.warning("Failed to serialize output item for replay")

                if isinstance(item, ResponseReasoningItem):
                    # Build thinking text from summary parts
                    thinking_text = state.partial_thinking.rstrip("\n")
                    if not thinking_text and item.summary:
                        thinking_text = "\n\n".join(
                            p.text for p in item.summary if hasattr(p, "text")
                        )

                    yield ThinkingEvent(
                        text=thinking_text,
                        # Round-trip the whole reasoning item as opaque JSON
                        signature=item.model_dump_json(),
                        is_summary=True,
                    )
                    yield ContentBlockEndEvent(
                        content_index=state.block_index,
                        content_type="thinking",
                    )
                    state.reset_block()

                elif isinstance(item, ResponseOutputMessage):
                    # Build final text from content parts
                    final_text = state.partial_text
                    if not final_text and item.content:
                        parts = []
                        for part in item.content:
                            if hasattr(part, "text"):
                                parts.append(part.text)
                            elif hasattr(part, "refusal"):
                                parts.append(part.refusal)
                        final_text = "".join(parts)

                    yield TextEvent(text=final_text)
                    yield ContentBlockEndEvent(
                        content_index=state.block_index,
                        content_type="text",
                    )
                    state.reset_block()

                elif isinstance(item, ResponseFunctionToolCall):
                    try:
                        args = json.loads(state.partial_args or item.arguments)
                    except json.JSONDecodeError:
                        args = {}
                        log.warning(
                            "Malformed tool call JSON for %s: %s",
                            item.name, state.partial_args,
                        )

                    yield ToolCallEvent(
                        id=item.call_id,
                        name=item.name,
                        input=args,
                        item_id=item.id,
                    )
                    yield ContentBlockEndEvent(
                        content_index=state.block_index,
                        content_type="tool_call",
                    )
                    state.reset_block()

            # -- Response complete --
            elif isinstance(event, ResponseCompletedEvent):
                resp = event.response
                usage = _extract_usage(resp) if resp else None
                stop_reason = _map_status(resp.status if resp else None)
                if stop_reason == "stop" and has_tool_calls:
                    stop_reason = "tool_use"

                yield TurnCompleteEvent(
                    stop_reason=stop_reason,
                    usage=usage,
                    session_id=resp.id if resp else None,
                    model=resp.model if resp else None,
                )

            elif isinstance(event, ResponseIncompleteEvent):
                resp = event.response
                usage = _extract_usage(resp) if resp else None
                yield TurnCompleteEvent(
                    stop_reason="length",
                    usage=usage,
                    session_id=resp.id if resp else None,
                    model=resp.model if resp else None,
                )

            # -- Errors --
            elif isinstance(event, ResponseErrorEvent):
                # Stream-level error. Server errors get retried.
                is_server = event.code and event.code.startswith("server_")
                if is_server:
                    raise _RetryableStreamError(f"Stream error: {event.message}")
                yield ErrorEvent(
                    message=f"Stream error ({event.code}): {event.message}",
                    is_retryable=False,
                )

            elif isinstance(event, ResponseFailedEvent):
                resp = event.response
                error_msg = "Response failed"
                if resp and resp.error:
                    error_msg = f"Response failed: {resp.error.message}"
                yield ErrorEvent(message=error_msg, is_retryable=False)

            # Ignore: ResponseCreatedEvent, ResponseInProgressEvent,
            # ResponseContentPart{Added,Done}Event, ResponseText/RefusalDoneEvent,
            # ResponseReasoningSummary{PartAdded,TextDone}Event — informational only

    def build_tool_schemas(self, tools: list[ToolDef]) -> list[dict[str, Any]]:
        """Convert ToolDefs to Responses API format.

        Responses API uses a flat structure — no "function" wrapper key.
        """
        return [
            {
                "type": "function",
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.input_schema,
                "strict": False,
            }
            for tool in tools
        ]

    def build_image_content(self, data: bytes, mime_type: str) -> dict[str, Any]:
        """Encode image as data URI for the Responses API."""
        import base64
        b64 = base64.b64encode(data).decode("ascii")
        return {
            "type": "input_image",
            "image_url": f"data:{mime_type};base64,{b64}",
        }

    def build_document_content(
        self, data: bytes, mime_type: str, filename: str,
    ) -> dict[str, Any]:
        """Encode document as input_file for the Responses API.

        The API extracts text and images from PDFs, text from docx/txt/etc,
        and runs spreadsheet-specific augmentation for xlsx/csv.
        """
        import base64
        b64 = base64.b64encode(data).decode("ascii")
        return {
            "type": "input_file",
            "filename": filename,
            "file_data": b64,
        }

    def build_rich_tool_result(
        self, content_blocks: list[dict[str, Any]],
    ) -> list[dict[str, Any]] | None:
        """Convert MCP content blocks to Responses API rich output format.

        The Responses API accepts function_call_output.output as either a
        string or a list of input_text / input_image / input_file items.

        Fail-closed: if any block can't be converted, returns None so the
        caller falls back to the plain text output. This prevents silent
        data loss for unsupported content types.
        """
        import base64 as _b64

        parts: list[dict[str, Any]] = []
        for block in content_blocks:
            btype = block.get("type", "")
            if btype == "text" and block.get("text"):
                parts.append({"type": "input_text", "text": block["text"]})
            elif btype == "image":
                data = _b64.b64decode(block.get("data", ""))
                mime = block.get("mimeType", "image/png")
                parts.append(self.build_image_content(data, mime))
            elif btype:
                # Unknown non-text block — can't convert, fail closed
                return None
        return parts if parts else None

    def build_assistant_input(
        self,
        *,
        text: str,
        tool_calls: list[ToolCallEvent],
    ) -> list[dict[str, Any]] | dict[str, Any] | None:
        """Build Responses-API-compatible assistant replay items.

        OpenAI input accepts assistant text as an output message item, but tool
        calls replay as top-level `function_call` input items rather than nested
        inside assistant message content.
        """
        items: list[dict[str, Any]] = []
        if text:
            items.append({
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{
                    "type": "output_text",
                    "text": text,
                    "annotations": [],
                }],
            })
        for tc in tool_calls:
            items.append({
                "type": "function_call",
                "call_id": tc.id,
                "name": tc.name,
                "arguments": json.dumps(tc.input),
                "status": "completed",
            })
        return items


    @property
    def context_injection_role(self) -> str:

        return "developer"

    async def close(self):
        """Clean up the HTTP client."""
        await self._client.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_usage(resp: Response) -> Usage | None:
    if not resp.usage:
        return None
    u = resp.usage
    cached = 0
    if hasattr(u, "input_tokens_details") and u.input_tokens_details:
        cached = getattr(u.input_tokens_details, "cached_tokens", 0) or 0
    return Usage(
        input_tokens=u.input_tokens - cached,
        output_tokens=u.output_tokens,
        cache_read_tokens=cached if cached else None,
        cache_write_tokens=None,  # OpenAI doesn't report this
        total_tokens=u.total_tokens,
    )


def _map_status(status: str | None) -> str:
    match status:
        case "completed" | None:
            return "stop"
        case "incomplete":
            return "length"
        case "failed" | "cancelled":
            return "error"
        case _:
            return "stop"

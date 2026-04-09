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
                result_output, result_error = await self._execute_tool(tc)
                yield ToolResultEvent(
                    tool_call_id=tc.id,
                    output=result_output,
                    is_error=result_error,
                )

                # Post-tool hook (context injection)
                if self._hook_dispatcher:
                    injection = await self._hook_dispatcher.post_tool(
                        tc.name, tc.input, result_output,
                    )
                    if injection:
                        self._history.append(ContextInjection(text=injection))

                self._history.append(ToolResultTurn(
                    call_id=tc.id, output=result_output, is_error=result_error,
                ))

            # Tool calls processed — loop continues
            if not self._interrupted:
                self._has_pending_turn = True

    async def interrupt(self) -> None:
        self._interrupted = True
        self._has_pending_turn = False

    async def stop(self) -> None:
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
                    # Round-trip raw provider items for multi-turn continuity
                    items.extend(turn.raw_output_items)
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
                items.append({
                    "type": "function_call_output",
                    "call_id": turn.call_id,
                    "output": turn.output,
                })

            elif isinstance(turn, ContextInjection):
                items.append({
                    "role": self._provider.context_injection_role,
                    "content": turn.text,
                })

        return items

    def _build_rich_user_message(self, turn: UserTurn) -> dict[str, Any]:
        """Convert ContentBlock list to a user message."""
        import base64

        parts = []
        for block in turn.content_blocks or []:
            if isinstance(block, TextContent):
                parts.append({"type": "input_text", "text": block.text})
            elif isinstance(block, DocumentContent):
                if block.mime_type.startswith("image/"):
                    b64 = base64.b64encode(block.data).decode("ascii")
                    parts.append({
                        "type": "input_image",
                        "image_url": f"data:{block.mime_type};base64,{b64}",
                    })
                else:
                    # Generic document — text description fallback
                    parts.append({
                        "type": "input_text",
                        "text": f"[Document: {block.label} ({block.mime_type})]",
                    })
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

    async def _execute_tool(self, tc: ToolCallEvent) -> tuple[str, bool]:
        """Execute a tool call. Returns (output, is_error)."""
        tool = self._tool_registry.get(tc.name)
        if not tool:
            return f"Unknown tool: {tc.name}", True

        try:
            result = await tool.handler(tc.input)
            # SdkMcpTool handler returns {"content": [{"type": "text", "text": "..."}]}
            content = result.get("content", [])
            if content and isinstance(content, list):
                output = content[0].get("text", "")
            else:
                output = str(result)
            is_error = result.get("isError", False)
            return output, is_error
        except Exception as e:
            log.error("Tool %s raised: %s", tc.name, e)
            return f"Tool execution error: {e}", True

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

def _map_thinking_level(effort: str) -> dict[str, Any] | None:
    """Map Kiln effort level to OpenAI reasoning config."""
    mapping = {
        "low": {"effort": "low", "summary": "auto"},
        "medium": {"effort": "medium", "summary": "auto"},
        "high": {"effort": "high", "summary": "auto"},
        "max": {"effort": "high", "summary": "auto"},
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

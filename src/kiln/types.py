"""Kiln-native types — events, protocols, and configuration.

These types decouple Kiln's session management from any specific LLM SDK.
Backends produce Event streams; the harness and TUI consume them. Providers
handle the actual API communication for the custom backend.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol


# ---------------------------------------------------------------------------
# Event types — the universal language between backends and consumers
# ---------------------------------------------------------------------------

@dataclass
class TextEvent:
    """Complete text block from the assistant."""
    text: str


@dataclass
class ThinkingEvent:
    """Thinking/reasoning block."""
    text: str
    signature: str | None = None     # Opaque — round-trip for multi-turn continuity
    redacted: bool = False           # Safety-filtered (Anthropic)
    is_summary: bool = False         # OpenAI reasoning summaries (not raw thinking)


@dataclass
class ToolCallEvent:
    """Model requests a tool call."""
    id: str                          # call_id — for matching with ToolResultEvent
    name: str
    input: dict[str, Any]
    item_id: str | None = None       # OpenAI Responses API item ID (fc_...) for replay


@dataclass
class ToolResultEvent:
    """Result of tool execution."""
    tool_call_id: str
    output: str
    is_error: bool = False


@dataclass
class TurnCompleteEvent:
    """Model's turn is complete."""
    stop_reason: str | None = None   # "stop", "tool_use", "length", "error"
    usage: Usage | None = None
    session_id: str | None = None    # CC conversation UUID / OpenAI response ID
    model: str | None = None         # Actual model used (may differ from requested)


@dataclass
class ContentBlockStartEvent:
    """Start of a new content block (for streaming TUI)."""
    content_index: int
    content_type: str                # "text", "thinking", "tool_call"


@dataclass
class ContentBlockDeltaEvent:
    """Streaming delta within a content block."""
    content_index: int
    text: str | None = None
    thinking: str | None = None


@dataclass
class ContentBlockEndEvent:
    """End of a content block."""
    content_index: int
    content_type: str
    redacted: bool = False           # For redacted thinking blocks


@dataclass
class UsageUpdateEvent:
    """Mid-turn usage snapshot for live context tracking.

    Emitted from streaming deltas so the TUI can update context size
    during a turn, not just at turn end.
    """
    usage: Usage


@dataclass
class ErrorEvent:
    """An error occurred during generation."""
    message: str
    is_retryable: bool = False


@dataclass
class SystemMessageEvent:
    """System-level message (init, status, etc)."""
    subtype: str
    text: str | None = None


# Union of all event types for type hints.
Event = (
    TextEvent | ThinkingEvent | ToolCallEvent | ToolResultEvent
    | TurnCompleteEvent | UsageUpdateEvent | ContentBlockStartEvent
    | ContentBlockDeltaEvent | ContentBlockEndEvent | ErrorEvent
    | SystemMessageEvent
)


# ---------------------------------------------------------------------------
# Usage — cache-aware token accounting
# ---------------------------------------------------------------------------

@dataclass
class Usage:
    """Token usage for a single turn.

    None vs 0 semantics: 0 means the provider reported zero. None means
    the provider doesn't report that metric (local models, some hosts).
    """
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    total_tokens: int = 0


# ---------------------------------------------------------------------------
# Tool definitions — backend-agnostic tool representation
# ---------------------------------------------------------------------------

@dataclass
class ToolDef:
    """A tool's schema and handler, independent of MCP transport.

    ClaudeBackend ignores these (it uses MCP servers). CustomBackend
    calls handler() directly for tool dispatch.
    """
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


# ---------------------------------------------------------------------------
# Hook dispatch — direct hook execution for CustomBackend
# ---------------------------------------------------------------------------

# Hook callable signature — matches the existing hook convention.
# (input_data, tool_use_id, context) -> output_dict
HookCallable = Callable[[dict, str | None, dict], Awaitable[dict]]


@dataclass
class HookResult:
    denied: bool = False
    reason: str | None = None


@dataclass
class PostToolResult:
    """Result of post-tool hook processing."""
    additional_context: str | None = None
    updated_tool_output: str | None = None  # Replace tool output text in history
    continue_: bool = True  # False = stop the agentic loop, return to harness


@dataclass
class HookRule:
    """A pattern-matched hook binding."""
    pattern: str | None              # None = match all, "Read" = exact match
    hook: HookCallable

    def matches(self, tool_name: str) -> bool:
        if self.pattern is None:
            return True
        return tool_name == self.pattern or tool_name.endswith(f"__{self.pattern}")


class HookDispatcher:
    """Direct hook execution for the custom agentic loop.

    Wraps the same hook callables used by the CC SDK's HookMatcher system,
    but invokes them directly rather than through the SDK's hook machinery.
    """

    def __init__(
        self,
        pre_tool_hooks: list[HookRule] | None = None,
        post_tool_hooks: list[HookRule] | None = None,
    ):
        self._pre = pre_tool_hooks or []
        self._post = post_tool_hooks or []

    @staticmethod
    def _unwrap(result: dict) -> dict:
        """Unwrap CC SDK hook output envelope if present.

        CC SDK hooks return {"hookSpecificOutput": {actual output}}.
        Non-SDK hooks may return flat dicts. Handle both.
        """
        inner = result.get("hookSpecificOutput")
        return inner if inner and isinstance(inner, dict) else result

    async def pre_tool(self, tool_name: str, tool_input: dict) -> HookResult:
        for rule in self._pre:
            if rule.matches(tool_name):
                result = await rule.hook(
                    {"tool_name": tool_name, "tool_input": tool_input},
                    None,
                    {"signal": None},
                )
                output = self._unwrap(result)
                # CC SDK uses "permissionDecision"; flat format uses "decision"
                decision = output.get("permissionDecision") or output.get("decision")
                if decision == "deny":
                    reason = output.get("permissionDecisionReason") or output.get("reason")
                    return HookResult(denied=True, reason=reason)
        return HookResult(denied=False)

    async def post_tool(
        self, tool_name: str, tool_input: dict, tool_response: str,
    ) -> PostToolResult:
        additional_context = []
        updated_output: str | None = None
        should_continue = True
        for rule in self._post:
            if rule.matches(tool_name):
                result = await rule.hook(
                    {"tool_name": tool_name, "tool_input": tool_input,
                     "tool_response": tool_response},
                    None,
                    {"signal": None},
                )
                unwrapped = self._unwrap(result)
                ctx = unwrapped.get("additionalContext")
                if ctx:
                    additional_context.append(ctx)
                updated = unwrapped.get("updatedMCPToolOutput")
                if updated is not None:
                    updated_output = updated
                # continue_ is top-level (not inside hookSpecificOutput)
                if result.get("continue_") is False:
                    should_continue = False
        return PostToolResult(
            additional_context="\n".join(additional_context) if additional_context else None,
            updated_tool_output=updated_output,
            continue_=should_continue,
        )


# ---------------------------------------------------------------------------
# Content blocks — backend-agnostic rich content for send()
# ---------------------------------------------------------------------------

@dataclass
class TextContent:
    """Plain text content block."""
    text: str


@dataclass
class DocumentContent:
    """Binary document (PDF, image) for supplemental injection."""
    data: bytes
    mime_type: str
    label: str = "file"


ContentBlock = TextContent | DocumentContent


class SupplementalContent:
    """Collects content that needs injection as user messages.

    Some content types (e.g. PDF document blocks, images on providers without
    native rich tool results) can't be expressed in tool results — they need
    to go in user-level messages. MCP tools and the CustomBackend stash raw
    file data here; the harness drains it and injects it between turns.
    """

    def __init__(self):
        self._pending: list[dict] = []

    def add_file(self, data: bytes, mime_type: str, label: str = "") -> None:
        """Stash a file for user-message injection."""
        self._pending.append({
            "data": data,
            "mime_type": mime_type,
            "label": label,
        })

    def drain(self) -> list[dict]:
        """Return and clear all pending items."""
        items, self._pending = self._pending, []
        return items

    @property
    def has_pending(self) -> bool:
        return bool(self._pending)


# ---------------------------------------------------------------------------
# Provider protocol — API communication for CustomBackend
# ---------------------------------------------------------------------------

class Provider(Protocol):
    """LLM API provider. Handles the actual HTTP/SDK communication.

    Backends use providers; the harness never touches them directly.
    """

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

        If raw_output_collector is provided, raw API output items are
        appended to it for conversation replay (multi-turn continuity).
        """
        ...

    def build_tool_schemas(self, tools: list[ToolDef]) -> list[dict[str, Any]]:
        """Convert ToolDefs to provider-specific tool format."""
        ...

    def build_image_content(self, data: bytes, mime_type: str) -> dict[str, Any]:
        """Convert raw image bytes to provider-specific image input format."""
        ...

    def build_document_content(
        self, data: bytes, mime_type: str, filename: str,
    ) -> dict[str, Any]:
        """Convert raw document bytes to provider-specific file input format.

        Handles PDFs, docx, txt, spreadsheets, etc. Each provider has its
        own format for file uploads (OpenAI uses input_file, Anthropic uses
        document blocks, etc).
        """
        ...

    def build_rich_tool_result(
        self, content_blocks: list[dict[str, Any]],
    ) -> list[dict[str, Any]] | None:
        """Convert MCP tool result content blocks to provider-specific format.

        Called when a tool returns rich content (images, files) that the
        provider might be able to include natively in function_call_output.
        Returns a list suitable for the output field, or None if the
        provider doesn't support rich tool results (caller falls back to text).
        """
        ...

    def build_assistant_input(
        self,
        *,
        text: str,
        tool_calls: list[ToolCallEvent],
    ) -> list[dict[str, Any]]:
        """Build provider-compatible input items for an assistant turn replay.

        Used when resuming from a transcript or any history source that lacks
        raw provider output items.  Returns a list of top-level input items
        (e.g. a message item for text, separate function_call items for tool
        calls) matching the provider's expected replay format.  Returns an
        empty list if there is nothing to replay.
        """
        ...

    @property
    def context_injection_role(self) -> str:

        """Role for hook context injection messages.

        "developer" for OpenAI (reasoning models ignore "system"),
        "system" for Anthropic, "user" with [SYSTEM] prefix as fallback.
        """
        ...

    async def close(self) -> None:
        """Clean up resources (HTTP clients, connections, etc)."""
        ...


# ---------------------------------------------------------------------------
# Backend protocol and config
# ---------------------------------------------------------------------------

@dataclass
class BackendConfig:
    """Everything a backend needs to initialize and run.

    Assembled by the harness, consumed by the backend. The harness does all
    the complex assembly (prompt, hooks, tools, env); the backend just
    translates to its specific API.
    """
    # Prompt
    system_prompt: str
    model: str

    # Tools — both formats, backend picks what it needs
    mcp_servers: dict[str, Any]              # For ClaudeBackend (SDK needs MCP configs)
    tool_defs: list[ToolDef]                 # For CustomBackend (direct tool dispatch)

    # Hooks — both formats, backend picks
    hooks: dict[str, list]                   # For ClaudeBackend (SDK HookMatcher format)
    hook_dispatcher: HookDispatcher | None = None  # For CustomBackend

    # Environment
    cwd: str = "."
    env: dict[str, str] = field(default_factory=dict)

    # Generation
    effort: str | None = None                # Maps to thinking level per-provider
    temperature: float | None = None
    max_output_tokens: int | None = None

    # Session identity
    session_id: str | None = None            # Stable ID (OpenAI prompt_cache_key, logging)
    resume_conversation_id: str | None = None  # CC-specific: conversation UUID to resume

    # Transcript — durable JSONL for custom-backend resume
    transcript_path: str | None = None       # Live transcript file path (custom backend only)

    # Stream behavior
    stream_timeout: float | None = None

    # Stderr
    stderr_callback: Callable[[str], None] | None = None

    # Supplemental content — shared with CustomBackend so it can stash
    # rich content (images, files) that the provider can't deliver natively
    # in tool results. The harness drains and injects as a user turn.
    supplemental: SupplementalContent | None = None

    # Permission mode (ClaudeBackend-specific, but harmless to carry generically)
    permission_mode: str = "bypassPermissions"

    # Base tools (ClaudeBackend-specific — CC built-in tools passed to SDK).
    # [] means no CC built-ins. The harness always sets this explicitly from
    # the resolved agent config; the default here is a safety net, not a
    # source of truth. Do NOT add tool defaults here — put them in
    # DEFAULT_TOOLS in config.py.
    base_tools: list[str] = field(default_factory=list)

    # Extra SDK args (ClaudeBackend-specific passthrough)
    extra_args: dict[str, Any] = field(default_factory=dict)


class Backend(Protocol):
    """LLM backend interface.

    Implementations own the model interaction. The harness drives
    the backend; the TUI consumes its events. Two implementations:

    - ClaudeBackend: wraps CC SDK/CLI. CC owns the agentic loop.
    - CustomBackend: hand-rolled loop. Kiln IS the loop. Uses a Provider.
    """

    async def start(self, config: BackendConfig) -> None:
        """Initialize and connect to the model."""
        ...

    async def send(self, message: str | list[ContentBlock]) -> None:
        """Send a user message. String for plain text, list for rich content."""
        ...

    async def receive(self) -> AsyncIterator[Event]:
        """Yield events until the current turn completes."""
        ...

    async def interrupt(self) -> None:
        """Interrupt the current generation."""
        ...

    async def stop(self) -> None:
        """Disconnect and clean up."""
        ...

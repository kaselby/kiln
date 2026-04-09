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
    | TurnCompleteEvent | ContentBlockStartEvent | ContentBlockDeltaEvent
    | ContentBlockEndEvent | ErrorEvent | SystemMessageEvent
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

    async def pre_tool(self, tool_name: str, tool_input: dict) -> HookResult:
        for rule in self._pre:
            if rule.matches(tool_name):
                result = await rule.hook(
                    {"tool_name": tool_name, "tool_input": tool_input},
                    None,
                    {"signal": None},
                )
                if result.get("decision") == "deny":
                    return HookResult(denied=True, reason=result.get("reason"))
        return HookResult(denied=False)

    async def post_tool(
        self, tool_name: str, tool_input: dict, tool_response: str,
    ) -> str | None:
        additional_context = []
        for rule in self._post:
            if rule.matches(tool_name):
                result = await rule.hook(
                    {"tool_name": tool_name, "tool_input": tool_input,
                     "tool_response": tool_response},
                    None,
                    {"signal": None},
                )
                ctx = result.get("additionalContext")
                if ctx:
                    additional_context.append(ctx)
        return "\n".join(additional_context) if additional_context else None


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

    @property
    def context_injection_role(self) -> str:
        """Role for hook context injection messages.

        "developer" for OpenAI (reasoning models ignore "system"),
        "system" for Anthropic, "user" with [SYSTEM] prefix as fallback.
        """
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

    # Stream behavior
    stream_timeout: float | None = None

    # Stderr
    stderr_callback: Callable[[str], None] | None = None

    # Permission mode (ClaudeBackend-specific, but harmless to carry generically)
    permission_mode: str = "bypassPermissions"

    # Base tools (ClaudeBackend-specific — e.g. ["WebSearch"])
    base_tools: list[str] = field(default_factory=lambda: ["WebSearch"])

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

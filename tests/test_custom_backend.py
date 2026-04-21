"""Tests for CustomBackend hook semantics and rich tool results.

Covers the three PostToolUse hook features that CustomBackend must implement
with the same semantics as the Claude SDK:
- additionalContext: inject system-level context after a tool call
- continue_=False: stop the agentic loop, return control to the harness
- updatedMCPToolOutput: replace tool output text in conversation history

Also covers rich tool result handling (images in function_call_output)
and the interaction between continue_=False and parallel tool calls
(orphaned tool call cleanup).
"""

import asyncio
import base64

import pytest

from kiln.types import (
    BackendConfig,
    HookDispatcher,
    HookRule,
    PostToolResult,
    SupplementalContent,
    ToolCallEvent,
    ToolDef,
    ToolResultEvent,
    TurnCompleteEvent,
)
from kiln.backends.custom import (
    AssistantTurn,
    ContextInjection,
    CustomBackend,
    ToolResultTurn,
    UserTurn,
)
from kiln.providers.openai_responses import OpenAIResponsesProvider


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

class StubProvider:
    """Minimal provider for tests that don't need real API calls."""

    def build_rich_tool_result(self, content_blocks):
        """Delegates to OpenAI's implementation for realistic behavior."""
        return OpenAIResponsesProvider.build_rich_tool_result(self, content_blocks)

    def build_assistant_input(self, *, text, tool_calls):
        """Delegates to OpenAI assistant replay formatting for realism."""
        return OpenAIResponsesProvider.build_assistant_input(
            self, text=text, tool_calls=tool_calls,
        )


    def build_image_content(self, data, mime_type):
        b64 = base64.b64encode(data).decode("ascii")
        return {"type": "input_image", "image_url": f"data:{mime_type};base64,{b64}"}

    @property
    def context_injection_role(self):
        return "developer"



class NoRichProvider(StubProvider):
    """Provider that doesn't support rich tool results."""

    def build_rich_tool_result(self, content_blocks):
        return None  # Always fail — no native rich support


IMG_B64 = base64.b64encode(b"fake-png-data").decode()


def make_image_tool():
    """Tool handler that returns MCP image content."""
    async def handler(params):
        return {"content": [
            {"type": "text", "text": f"File: {params.get('file_path', '/img.png')}"},
            {"type": "image", "data": IMG_B64, "mimeType": "image/png"},
        ]}
    return ToolDef(name="Read", description="", input_schema={}, handler=handler)


def make_text_tool():
    """Tool handler that returns text-only content."""
    async def handler(params):
        return {"content": [{"type": "text", "text": "hello world"}]}
    return ToolDef(name="Bash", description="", input_schema={}, handler=handler)


def make_backend(
    tools=None,
    post_hooks=None,
    provider=None,
    supplemental=None,
):
    """Create a CustomBackend with optional hooks and tools."""
    prov = provider or StubProvider()
    backend = CustomBackend(prov)
    config = BackendConfig(
        system_prompt="test",
        model="gpt-test",
        mcp_servers={},
        tool_defs=tools or [],
        hooks={},
        hook_dispatcher=HookDispatcher(
            post_tool_hooks=post_hooks or [],
        ) if post_hooks else None,
        supplemental=supplemental,
    )
    asyncio.get_event_loop().run_until_complete(backend.start(config))
    return backend


# ---------------------------------------------------------------------------
# HookDispatcher tests
# ---------------------------------------------------------------------------

class TestHookDispatcher:

    def test_post_tool_returns_post_tool_result(self):
        """post_tool() returns PostToolResult, not str|None."""
        dispatcher = HookDispatcher()
        result = asyncio.get_event_loop().run_until_complete(
            dispatcher.post_tool("Read", {}, "output")
        )
        assert isinstance(result, PostToolResult)
        assert result.additional_context is None
        assert result.updated_tool_output is None
        assert result.continue_ is True

    def test_additional_context_merged(self):
        """Multiple hooks' additionalContext values are joined."""
        async def hook_a(inp, tid, ctx):
            return {"hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": "from hook A",
            }}

        async def hook_b(inp, tid, ctx):
            return {"hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": "from hook B",
            }}

        dispatcher = HookDispatcher(post_tool_hooks=[
            HookRule(pattern=None, hook=hook_a),
            HookRule(pattern=None, hook=hook_b),
        ])
        result = asyncio.get_event_loop().run_until_complete(
            dispatcher.post_tool("Read", {}, "output")
        )
        assert "from hook A" in result.additional_context
        assert "from hook B" in result.additional_context

    def test_continue_false_propagated(self):
        """continue_=False at top level stops the loop."""
        async def stop_hook(inp, tid, ctx):
            return {
                "continue_": False,
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": "stopping",
                },
            }

        dispatcher = HookDispatcher(post_tool_hooks=[
            HookRule(pattern="Read", hook=stop_hook),
        ])

        result = asyncio.get_event_loop().run_until_complete(
            dispatcher.post_tool("Read", {}, "output")
        )
        assert result.continue_ is False
        assert result.additional_context == "stopping"

    def test_continue_false_only_for_matching_tool(self):
        """continue_=False hook only fires for matched tools."""
        async def stop_hook(inp, tid, ctx):
            return {"continue_": False, "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
            }}

        dispatcher = HookDispatcher(post_tool_hooks=[
            HookRule(pattern="Read", hook=stop_hook),
        ])

        result = asyncio.get_event_loop().run_until_complete(
            dispatcher.post_tool("Bash", {}, "output")
        )
        assert result.continue_ is True

    def test_updated_tool_output_extracted(self):
        """updatedMCPToolOutput from hookSpecificOutput is returned."""
        async def replace_hook(inp, tid, ctx):
            return {"hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "updatedMCPToolOutput": "replaced output",
                "additionalContext": "context here",
            }}

        dispatcher = HookDispatcher(post_tool_hooks=[
            HookRule(pattern=None, hook=replace_hook),
        ])
        result = asyncio.get_event_loop().run_until_complete(
            dispatcher.post_tool("ActivateSkill", {"name": "test"}, "original"),
        )
        assert result.updated_tool_output == "replaced output"
        assert result.additional_context == "context here"

    def test_updated_tool_output_last_writer_wins(self):
        """Multiple hooks setting updatedMCPToolOutput: last one wins."""
        async def hook_first(inp, tid, ctx):
            return {"hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "updatedMCPToolOutput": "first",
            }}

        async def hook_second(inp, tid, ctx):
            return {"hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "updatedMCPToolOutput": "second",
            }}

        dispatcher = HookDispatcher(post_tool_hooks=[
            HookRule(pattern=None, hook=hook_first),
            HookRule(pattern=None, hook=hook_second),
        ])
        result = asyncio.get_event_loop().run_until_complete(
            dispatcher.post_tool("tool", {}, "orig"),
        )
        assert result.updated_tool_output == "second"


# ---------------------------------------------------------------------------
# _execute_tool tests
# ---------------------------------------------------------------------------

class TestExecuteTool:

    def test_image_preserves_rich_content(self):
        """Image tool results preserve raw MCP content blocks."""
        backend = make_backend(tools=[make_image_tool()])
        tc = ToolCallEvent(id="c1", name="Read", input={"file_path": "/img.png"})
        text, is_err, rich = asyncio.get_event_loop().run_until_complete(
            backend._execute_tool(tc)
        )
        assert not is_err
        assert "[Image: image/png]" in text
        assert rich is not None
        assert any(b["type"] == "image" for b in rich)

    def test_text_only_no_rich_content(self):
        """Text-only tool results have no rich_content."""
        backend = make_backend(tools=[make_text_tool()])
        tc = ToolCallEvent(id="c1", name="Bash", input={})
        text, is_err, rich = asyncio.get_event_loop().run_until_complete(
            backend._execute_tool(tc)
        )
        assert text == "hello world"
        assert rich is None

    def test_unknown_tool_returns_error(self):
        """Unknown tool name returns error with no rich content."""
        backend = make_backend()
        tc = ToolCallEvent(id="c1", name="NoSuchTool", input={})
        text, is_err, rich = asyncio.get_event_loop().run_until_complete(
            backend._execute_tool(tc)
        )
        assert is_err
        assert "Unknown tool" in text
        assert rich is None

    def test_unknown_block_type_triggers_rich(self):
        """Non-text, non-image block types still set has_rich."""
        async def handler(params):
            return {"content": [
                {"type": "text", "text": "header"},
                {"type": "resource", "uri": "file:///x"},
            ]}

        backend = make_backend(tools=[
            ToolDef(name="T", description="", input_schema={}, handler=handler),
        ])
        tc = ToolCallEvent(id="c1", name="T", input={})
        text, is_err, rich = asyncio.get_event_loop().run_until_complete(
            backend._execute_tool(tc)
        )
        assert rich is not None  # has_rich=True for unknown block type
        assert text == "header"  # unknown block doesn't add text


# ---------------------------------------------------------------------------
# build_rich_tool_result tests (OpenAI provider)
# ---------------------------------------------------------------------------

class TestBuildRichToolResult:

    def setup_method(self):
        self.provider = StubProvider()

    def test_image_blocks_converted(self):
        """Image blocks become input_image items."""
        blocks = [
            {"type": "text", "text": "caption"},
            {"type": "image", "data": IMG_B64, "mimeType": "image/png"},
        ]
        result = self.provider.build_rich_tool_result(blocks)
        assert result is not None
        assert len(result) == 2
        assert result[0] == {"type": "input_text", "text": "caption"}
        assert result[1]["type"] == "input_image"
        assert "data:image/png;base64," in result[1]["image_url"]

    def test_empty_blocks_returns_none(self):
        assert self.provider.build_rich_tool_result([]) is None

    def test_unknown_block_fails_closed(self):
        """Unknown block types cause fail-closed (return None)."""
        blocks = [
            {"type": "text", "text": "ok"},
            {"type": "resource", "uri": "file:///x"},
        ]
        result = self.provider.build_rich_tool_result(blocks)
        assert result is None, "Should fail closed on unknown block type"

    def test_image_only(self):
        """Image-only content (no text) works."""
        blocks = [{"type": "image", "data": IMG_B64, "mimeType": "image/jpeg"}]
        result = self.provider.build_rich_tool_result(blocks)
        assert result is not None
        assert len(result) == 1
        assert result[0]["type"] == "input_image"


# ---------------------------------------------------------------------------
# _build_input + _apply_transforms: rich output and orphan handling
# ---------------------------------------------------------------------------

class TestBuildInput:

    def test_rich_tool_result_in_function_call_output(self):
        """ToolResultTurn with rich_content produces a list output."""
        backend = make_backend()
        backend._history = [
            ToolResultTurn(
                call_id="c1",
                output="[Image: image/png]",
                rich_content=[
                    {"type": "text", "text": "File: /img.png"},
                    {"type": "image", "data": IMG_B64, "mimeType": "image/png"},
                ],
            ),
        ]
        items = backend._build_input()
        assert len(items) == 1
        assert items[0]["type"] == "function_call_output"
        assert isinstance(items[0]["output"], list)
        assert items[0]["output"][0]["type"] == "input_text"
        assert items[0]["output"][1]["type"] == "input_image"

    def test_text_only_tool_result_is_string(self):
        """ToolResultTurn without rich_content produces a string output."""
        backend = make_backend()
        backend._history = [
            ToolResultTurn(call_id="c1", output="hello"),
        ]
        items = backend._build_input()
        assert items[0]["output"] == "hello"

    def test_unsupported_rich_falls_back_to_text(self):
        """When provider can't convert rich content, falls back to text."""
        backend = make_backend()
        backend._history = [
            ToolResultTurn(
                call_id="c1",
                output="header",
                rich_content=[
                    {"type": "text", "text": "header"},
                    {"type": "resource", "uri": "file:///x"},  # unknown
                ],
            ),
        ]
        items = backend._build_input()
        # Provider fails closed on unknown block → string fallback
        assert items[0]["output"] == "header"

    def test_assistant_fallback_uses_provider_native_replay_shape(self):
        """Transcript-loaded assistant turns must replay in provider-native format."""
        backend = make_backend()
        backend._history = [
            AssistantTurn(
                text="I can help.",
                tool_calls=[
                    ToolCallEvent(id="c1", name="Read", input={"file_path": "/tmp/x"}),
                ],
            ),
        ]

        items = backend._build_input()
        assert len(items) == 2
        assert items[0]["type"] == "message"
        assert items[0]["role"] == "assistant"
        assert items[0]["content"][0]["type"] == "output_text"
        assert items[0]["content"][0]["text"] == "I can help."
        assert items[1]["type"] == "function_call"
        assert items[1]["call_id"] == "c1"


    def test_parallel_tool_calls_orphan_handling(self):
        """When continue_=False stops after tool 1 of 2, tool 2 gets
        a synthetic orphan result via _apply_transforms.

        Full scenario:
        - Assistant emits 2 parallel tool calls
        - Tool 1 returns rich content, post-hook says continue_=False
        - Tool 2 never executes
        - Harness injects supplemental user turn
        - _insert_orphaned_results fills in tool 2's missing result
        """
        backend = make_backend()
        backend._history = [
            UserTurn(text="show me the image and list files"),
            AssistantTurn(
                raw_output_items=[
                    {"type": "function_call", "call_id": "c1",
                     "name": "Read", "arguments": "{}", "id": "fc_1"},
                    {"type": "function_call", "call_id": "c2",
                     "name": "Bash", "arguments": "{}", "id": "fc_2"},
                ],
                tool_calls=[
                    ToolCallEvent(id="c1", name="Read", input={}, item_id="fc_1"),
                    ToolCallEvent(id="c2", name="Bash", input={}, item_id="fc_2"),
                ],
            ),
            # Only tool 1 got a result
            ToolResultTurn(
                call_id="c1", output="[Image: image/png]",
                rich_content=[
                    {"type": "image", "data": IMG_B64, "mimeType": "image/png"},
                ],
            ),
            ContextInjection(text="[Supplemental content pending]"),
            # Tool 2 never executed — no ToolResultTurn
            # Harness injected supplemental content
            UserTurn(text="[Document content follows]"),
        ]

        raw_items = backend._build_input()
        items = backend._apply_transforms(raw_items)

        # Find the outputs for each call
        c1_outputs = [i for i in items
                      if i.get("type") == "function_call_output"
                      and i.get("call_id") == "c1"]
        c2_outputs = [i for i in items
                      if i.get("type") == "function_call_output"
                      and i.get("call_id") == "c2"]

        # Tool 1: rich output (image)
        assert len(c1_outputs) == 1
        assert isinstance(c1_outputs[0]["output"], list)

        # Tool 2: synthetic orphan result
        assert len(c2_outputs) == 1
        assert isinstance(c2_outputs[0]["output"], str)
        assert "interrupted" in c2_outputs[0]["output"].lower()

        # Ordering: c1 result < c2 orphan < user turn
        c1_idx = items.index(c1_outputs[0])
        c2_idx = items.index(c2_outputs[0])
        user_idx = next(
            i for i, item in enumerate(items)
            if item.get("role") == "user"
            and "[Document" in str(item.get("content", ""))
        )
        assert c1_idx < c2_idx < user_idx



# ---------------------------------------------------------------------------
# updatedMCPToolOutput in CustomBackend
# ---------------------------------------------------------------------------

class TestUpdatedToolOutput:

    def test_hook_replaces_history_output(self):
        """updatedMCPToolOutput modifies the ToolResultTurn in history."""
        async def replace_hook(inp, tid, ctx):
            return {"hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "updatedMCPToolOutput": "Skill 'test' activated.",
            }}

        backend = make_backend(
            tools=[make_text_tool()],
            post_hooks=[HookRule(pattern=None, hook=replace_hook)],
        )

        # Manually simulate what receive() does for one tool call
        tc = ToolCallEvent(id="c1", name="Bash", input={})
        result_text, result_error, rich = asyncio.get_event_loop().run_until_complete(
            backend._execute_tool(tc)
        )

        tool_result_turn = ToolResultTurn(
            call_id=tc.id, output=result_text,
            is_error=result_error, rich_content=rich,
        )
        backend._history.append(tool_result_turn)

        post_result = asyncio.get_event_loop().run_until_complete(
            backend._hook_dispatcher.post_tool(tc.name, tc.input, result_text)
        )
        if post_result.updated_tool_output is not None:
            tool_result_turn.output = post_result.updated_tool_output

        # History should have the replaced output
        assert backend._history[-1].output == "Skill 'test' activated."
        # The turn object is the same reference
        assert tool_result_turn.output == "Skill 'test' activated."

    def test_updated_output_does_not_clear_rich_content(self):
        """updatedMCPToolOutput only changes .output, not .rich_content."""
        async def replace_hook(inp, tid, ctx):
            return {"hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "updatedMCPToolOutput": "replaced",
            }}

        backend = make_backend(
            tools=[make_image_tool()],
            post_hooks=[HookRule(pattern=None, hook=replace_hook)],
        )

        tc = ToolCallEvent(id="c1", name="Read", input={"file_path": "/img.png"})
        _, _, rich = asyncio.get_event_loop().run_until_complete(
            backend._execute_tool(tc)
        )

        turn = ToolResultTurn(
            call_id="c1", output="original", rich_content=rich,
        )
        backend._history.append(turn)

        post_result = asyncio.get_event_loop().run_until_complete(
            backend._hook_dispatcher.post_tool("Read", {}, "original")
        )
        if post_result.updated_tool_output is not None:
            turn.output = post_result.updated_tool_output

        assert turn.output == "replaced"
        assert turn.rich_content is not None  # Preserved
        assert any(b["type"] == "image" for b in turn.rich_content)


# ---------------------------------------------------------------------------
# Supplemental fallback: provider can't handle rich → stash for injection
# ---------------------------------------------------------------------------

class TestSupplementalFallback:

    def test_rich_content_stashed_when_provider_cant_handle(self):
        """When provider returns None for build_rich_tool_result, content
        is stashed in supplemental for harness injection."""
        supplemental = SupplementalContent()
        backend = make_backend(
            tools=[make_image_tool()],
            provider=NoRichProvider(),
            supplemental=supplemental,
        )

        # Simulate what receive() does: execute tool, check provider, stash
        tc = ToolCallEvent(id="c1", name="Read", input={"file_path": "/img.png"})
        _, _, rich = asyncio.get_event_loop().run_until_complete(
            backend._execute_tool(tc)
        )
        assert rich is not None  # Tool returned rich content

        # Provider can't handle it
        assert backend._provider.build_rich_tool_result(rich) is None

        # Stash it
        backend._stash_rich_content(rich)
        assert supplemental.has_pending
        items = supplemental.drain()
        assert len(items) == 1
        assert items[0]["mime_type"] == "image/png"

    def test_stashed_content_clears_rich_on_tool_result(self):
        """When content is stashed, the ToolResultTurn gets no rich_content,
        so _build_input falls back to the text string."""
        supplemental = SupplementalContent()
        backend = make_backend(
            tools=[make_image_tool()],
            provider=NoRichProvider(),
            supplemental=supplemental,
        )

        tc = ToolCallEvent(id="c1", name="Read", input={"file_path": "/img.png"})
        text, is_err, rich = asyncio.get_event_loop().run_until_complete(
            backend._execute_tool(tc)
        )

        # Simulate the receive() logic
        if rich and backend._provider.build_rich_tool_result(rich) is None:
            backend._stash_rich_content(rich)
            rich = None  # Cleared

        # ToolResultTurn with no rich_content
        backend._history.append(ToolResultTurn(
            call_id="c1", output=text, rich_content=rich,
        ))

        items = backend._build_input()
        # Should be a plain string, not a rich list
        assert isinstance(items[0]["output"], str)
        assert "[Image:" in items[0]["output"]
        # And supplemental has the image for harness injection
        assert supplemental.has_pending

"""Tests for stream stall timeout recovery in harness.receive().

The bug: asyncio.wait_for(anext(ait)) cancels the inner task on timeout,
which throws CancelledError into the receive_response() generator chain and
closes it.  The drain loop then calls anext() on the dead generator, gets
immediate StopAsyncIteration, and drains nothing.  The stale ResultMessage
from the interrupt stays in the SDK's message buffer and poisons the
recovery turn — receive_response() consumes it immediately, terminating
with zero new content, and the agent is stuck at Ready.

Fix: drain with a fresh receive_response() iterator instead of the dead one.
"""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock


# ---------------------------------------------------------------------------
# Minimal mock that reproduces the SDK's shared-buffer architecture
# ---------------------------------------------------------------------------

class MockClient:
    """Simulates ClaudeSDKClient with a shared message buffer.

    Messages flow through an asyncio.Queue (analogous to the SDK's
    anyio MemoryObjectReceiveStream).  receive_response() creates a
    fresh async generator each time, reading from the same queue.
    """

    def __init__(self):
        self._buffer: asyncio.Queue = asyncio.Queue()
        self.interrupt_called = False
        self.queries_sent: list[str] = []

    async def query(self, message: str) -> None:
        self.queries_sent.append(message)

    async def interrupt(self) -> None:
        self.interrupt_called = True

    async def receive_response(self) -> AsyncIterator:
        """Yield messages from the shared buffer until ResultMessage."""
        while True:
            msg = await self._buffer.get()
            yield msg
            if isinstance(msg, ResultMessage):
                return

    # Test helpers
    def inject(self, msg):
        """Put a message into the buffer (simulates CC subprocess output)."""
        self._buffer.put_nowait(msg)

    def inject_interrupt_response(self):
        """Simulate what CC emits after receiving an interrupt."""
        self.inject(AssistantMessage(
            content=[TextBlock(text="[interrupted partial output]")],
            model="claude-opus-4-20250514",
        ))
        self.inject(ResultMessage(
            subtype="result",
            duration_ms=1000,
            duration_api_ms=500,
            is_error=True,
            num_turns=1,
            session_id="test",
            stop_reason="interrupted",
        ))


# ---------------------------------------------------------------------------
# Harness receive() — extracted and simplified for testing
# ---------------------------------------------------------------------------

async def receive_with_timeout(client, timeout, followup_queue):
    """Reproduce the harness receive() logic (fixed version)."""
    ait = client.receive_response().__aiter__()
    while True:
        try:
            msg = await asyncio.wait_for(anext(ait), timeout=timeout)
            yield msg
        except StopAsyncIteration:
            return
        except asyncio.TimeoutError:
            # Interrupt
            try:
                await asyncio.wait_for(client.interrupt(), timeout=10)
            except Exception:
                pass
            # Drain with FRESH iterator (the fix)
            try:
                drain = client.receive_response().__aiter__()
                while True:
                    msg = await asyncio.wait_for(anext(drain), timeout=5)
                    yield msg
            except (StopAsyncIteration, asyncio.TimeoutError, Exception):
                pass
            # Queue recovery
            followup_queue.append("[SYSTEM] Recovery message")
            return


async def receive_with_timeout_BROKEN(client, timeout, followup_queue):
    """The original buggy version — drain reuses the dead iterator."""
    ait = client.receive_response().__aiter__()
    while True:
        try:
            msg = await asyncio.wait_for(anext(ait), timeout=timeout)
            yield msg
        except StopAsyncIteration:
            return
        except asyncio.TimeoutError:
            # Interrupt
            try:
                await asyncio.wait_for(client.interrupt(), timeout=10)
            except Exception:
                pass
            # Drain with DEAD iterator (the bug)
            try:
                while True:
                    msg = await asyncio.wait_for(anext(ait), timeout=5)
                    yield msg
            except (StopAsyncIteration, asyncio.TimeoutError, Exception):
                pass
            # Queue recovery
            followup_queue.append("[SYSTEM] Recovery message")
            return


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestStreamStallRecovery:
    """Verify stream stall timeout → interrupt → drain → recovery."""

    @pytest.mark.asyncio
    async def test_normal_flow_no_stall(self):
        """Normal turn completes without timeout — no recovery needed."""
        client = MockClient()
        followup = []

        # Inject a normal response
        client.inject(AssistantMessage(
            content=[TextBlock(text="Hello!")],
            model="claude-opus-4-20250514",
        ))
        client.inject(ResultMessage(
            subtype="result", duration_ms=100, duration_api_ms=50,
            is_error=False, num_turns=1, session_id="test",
        ))

        msgs = []
        async for msg in receive_with_timeout(client, timeout=5, followup_queue=followup):
            msgs.append(msg)

        assert len(msgs) == 2
        assert isinstance(msgs[0], AssistantMessage)
        assert isinstance(msgs[1], ResultMessage)
        assert followup == []  # No recovery needed

    @pytest.mark.asyncio
    async def test_stall_triggers_interrupt_and_drain(self):
        """Timeout fires, interrupt sent, stale messages drained, recovery queued."""
        client = MockClient()
        followup = []

        # Don't inject anything up front — simulate a stall.
        # After interrupt is called, inject the interrupt response.
        original_interrupt = client.interrupt

        async def interrupt_with_response():
            await original_interrupt()
            # CC responds to interrupt with partial output + ResultMessage
            client.inject_interrupt_response()

        client.interrupt = interrupt_with_response

        msgs = []
        async for msg in receive_with_timeout(client, timeout=0.2, followup_queue=followup):
            msgs.append(msg)

        # Drain should have consumed the interrupt's messages
        assert client.interrupt_called
        assert len(msgs) == 2  # AssistantMessage + ResultMessage from interrupt
        assert isinstance(msgs[0], AssistantMessage)
        assert isinstance(msgs[1], ResultMessage)
        assert msgs[1].is_error is True
        # Recovery message queued
        assert len(followup) == 1
        assert "Recovery" in followup[0]

    @pytest.mark.asyncio
    async def test_stall_recovery_message_buffer_is_clean(self):
        """After drain, the message buffer should be empty for the recovery turn."""
        client = MockClient()
        followup = []

        async def interrupt_with_response():
            client.interrupt_called = True
            client.inject_interrupt_response()

        client.interrupt = interrupt_with_response

        # Consume the stall turn
        async for _ in receive_with_timeout(client, timeout=0.2, followup_queue=followup):
            pass

        assert len(followup) == 1

        # Now simulate the recovery turn: inject CC's response to the recovery msg
        client.inject(AssistantMessage(
            content=[TextBlock(text="Continuing where I left off...")],
            model="claude-opus-4-20250514",
        ))
        client.inject(ResultMessage(
            subtype="result", duration_ms=200, duration_api_ms=100,
            is_error=False, num_turns=2, session_id="test",
        ))

        # The recovery turn should get FRESH messages, not stale ones
        recovery_msgs = []
        async for msg in client.receive_response():
            recovery_msgs.append(msg)

        assert len(recovery_msgs) == 2
        assert isinstance(recovery_msgs[0], AssistantMessage)
        assert recovery_msgs[0].content[0].text == "Continuing where I left off..."
        assert isinstance(recovery_msgs[1], ResultMessage)
        assert recovery_msgs[1].is_error is False  # Fresh result, not the stale error

    @pytest.mark.asyncio
    async def test_BROKEN_version_leaves_stale_result(self):
        """Demonstrate the bug: dead iterator drain leaves stale ResultMessage."""
        client = MockClient()
        followup = []

        async def interrupt_with_response():
            client.interrupt_called = True
            client.inject_interrupt_response()

        client.interrupt = interrupt_with_response

        # Run the BROKEN version
        msgs = []
        async for msg in receive_with_timeout_BROKEN(client, timeout=0.2, followup_queue=followup):
            msgs.append(msg)

        # Broken drain consumed nothing — msgs is empty
        assert len(msgs) == 0
        # Recovery IS queued (that part works)
        assert len(followup) == 1

        # Now the stale messages are still in the buffer — this is the bug.
        # The recovery turn's receive_response() will consume them instead of
        # waiting for fresh output from the recovery prompt.
        client.inject(AssistantMessage(
            content=[TextBlock(text="Fresh recovery output")],
            model="claude-opus-4-20250514",
        ))
        client.inject(ResultMessage(
            subtype="result", duration_ms=200, duration_api_ms=100,
            is_error=False, num_turns=2, session_id="test",
        ))

        recovery_msgs = []
        async for msg in client.receive_response():
            recovery_msgs.append(msg)

        # BUG: recovery turn got the STALE messages, not the fresh ones
        assert isinstance(recovery_msgs[-1], ResultMessage)
        assert recovery_msgs[-1].is_error is True  # Stale error result!
        assert recovery_msgs[-1].stop_reason == "interrupted"  # From the interrupt, not recovery

    @pytest.mark.asyncio
    async def test_stall_with_no_interrupt_response(self):
        """CC is completely stuck — interrupt gets no response. Drain times out."""
        client = MockClient()
        followup = []

        # Interrupt doesn't produce any messages (CC is totally stuck)
        async def stuck_interrupt():
            client.interrupt_called = True
            # Don't inject anything

        client.interrupt = stuck_interrupt

        msgs = []
        async for msg in receive_with_timeout(client, timeout=0.2, followup_queue=followup):
            msgs.append(msg)

        # No messages drained (timeout)
        assert len(msgs) == 0
        # But recovery is still queued
        assert len(followup) == 1
        assert client.interrupt_called

    @pytest.mark.asyncio
    async def test_partial_messages_before_stall_are_preserved(self):
        """Messages received before the stall should still be yielded."""
        client = MockClient()
        followup = []

        # Inject one message, then stall
        client.inject(AssistantMessage(
            content=[TextBlock(text="Partial output before stall")],
            model="claude-opus-4-20250514",
        ))

        got_first = asyncio.Event()

        async def interrupt_with_response():
            client.interrupt_called = True
            client.inject_interrupt_response()

        client.interrupt = interrupt_with_response

        msgs = []
        async for msg in receive_with_timeout(client, timeout=0.5, followup_queue=followup):
            msgs.append(msg)

        # Should have: pre-stall message + drain messages (interrupt response)
        assert len(msgs) >= 1
        assert isinstance(msgs[0], AssistantMessage)
        assert msgs[0].content[0].text == "Partial output before stall"
        # Recovery queued
        assert len(followup) == 1

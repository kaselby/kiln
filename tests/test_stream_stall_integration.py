"""Integration test: verify CC sends partial messages as a real stream.

Spawns a real Claude Code subprocess via the SDK, asks for a long response,
and verifies that:
  1. Partial AssistantMessages arrive incrementally (not batched at the end)
  2. Text content grows over time across partials
  3. No gap between consecutive messages is anywhere near the 120s timeout

This test hits the actual API — it costs real tokens and takes 30-60s.
Run explicitly:  pytest tests/test_stream_stall_integration.py -v -s
"""

import time

import pytest

from claude_agent_sdk.types import (
    AssistantMessage,
    ClaudeAgentOptions,
    Message,
    ResultMessage,
    StreamEvent,
)


LONG_GENERATION_PROMPT = (
    "Write a 1500-word short story about a lighthouse keeper who discovers "
    "a message in a bottle. Be descriptive and literary. Do not use any tools. "
    "Just write the story directly as your response."
)

# If any gap between consecutive messages exceeds this, something is wrong.
# Normal streaming should produce messages every ~100-500ms.
MAX_ACCEPTABLE_GAP_S = 30.0


def _get_text_length(msg: Message) -> int:
    """Extract total text length from an AssistantMessage."""
    if not isinstance(msg, AssistantMessage):
        return 0
    total = 0
    for block in msg.content:
        if hasattr(block, "text"):
            total += len(block.text)
    return total


async def _run_streaming_test(model: str) -> dict:
    """Run a streaming generation and return detailed stats."""

    options = ClaudeAgentOptions(
        model=model,
        max_turns=1,
        include_partial_messages=True,
        system_prompt="You are a helpful assistant. Respond directly with no tool use.",
    )

    from claude_agent_sdk._internal.client import InternalClient
    client = InternalClient()

    timestamps: list[float] = []
    messages: list[Message] = []
    text_lengths: list[int] = []  # text length at each partial

    async for msg in client.process_query(LONG_GENERATION_PROMPT, options):
        now = time.monotonic()
        timestamps.append(now)
        messages.append(msg)
        text_lengths.append(_get_text_length(msg))

    # Partition messages by type
    stream_events = [
        (i, msg) for i, msg in enumerate(messages)
        if isinstance(msg, StreamEvent)
    ]
    assistant_partials = [
        (i, msg) for i, msg in enumerate(messages)
        if isinstance(msg, AssistantMessage) and msg.stop_reason is None
    ]
    assistant_finals = [
        (i, msg) for i, msg in enumerate(messages)
        if isinstance(msg, AssistantMessage) and msg.stop_reason is not None
    ]

    gaps = [
        timestamps[i + 1] - timestamps[i] for i in range(len(timestamps) - 1)
    ]

    # Text lengths at each AssistantMessage partial
    partial_text_lengths = [text_lengths[i] for i, _ in assistant_partials]

    return {
        "messages": messages,
        "timestamps": timestamps,
        "gaps": gaps,
        "stream_events": stream_events,
        "assistant_partials": assistant_partials,
        "assistant_finals": assistant_finals,
        "text_lengths": text_lengths,
        "partial_text_lengths": partial_text_lengths,
        "max_gap": max(gaps) if gaps else 0,
        "avg_gap": sum(gaps) / len(gaps) if gaps else 0,
        "total_duration": timestamps[-1] - timestamps[0] if len(timestamps) > 1 else 0,
    }


def _print_stats(label: str, stats: dict) -> None:
    """Print streaming statistics."""
    print(f"\n--- {label} ---")
    print(f"Total messages:       {len(stats['messages'])}")
    print(f"  StreamEvents:       {len(stats['stream_events'])}")
    print(f"  Assistant partials: {len(stats['assistant_partials'])}")
    print(f"  Assistant finals:   {len(stats['assistant_finals'])}")
    print(f"Total duration:       {stats['total_duration']:.1f}s")
    print(f"Avg gap:              {stats['avg_gap']:.3f}s")
    print(f"Max gap:              {stats['max_gap']:.3f}s")
    if stats['gaps']:
        print(f"Min gap:              {min(stats['gaps']):.3f}s")
    if stats['partial_text_lengths']:
        print(f"Text growth:          {stats['partial_text_lengths'][0]} -> "
              f"{stats['partial_text_lengths'][-1]} chars "
              f"over {len(stats['partial_text_lengths'])} assistant partials")
    # Show message type breakdown
    type_counts: dict[str, int] = {}
    for msg in stats['messages']:
        name = type(msg).__name__
        type_counts[name] = type_counts.get(name, 0) + 1
    print(f"  Type breakdown:     {type_counts}")


def _assert_streaming(stats: dict, label: str) -> None:
    """Assert that messages arrived as a genuine stream."""

    # 1. We got many messages total — proves the stream is flowing
    assert len(stats["messages"]) >= 10, (
        f"[{label}] Only {len(stats['messages'])} total messages — "
        "expected many streaming updates."
    )

    # 2. Messages arrived spread across the duration, not bunched at the end.
    #    Check that the first 25% of messages arrived in the first 50% of time.
    if len(stats["timestamps"]) >= 4 and stats["total_duration"] > 2.0:
        t0 = stats["timestamps"][0]
        quarter_idx = len(stats["timestamps"]) // 4
        quarter_time = stats["timestamps"][quarter_idx] - t0
        assert quarter_time < stats["total_duration"] * 0.5, (
            f"[{label}] First 25% of messages arrived at {quarter_time:.1f}s "
            f"of {stats['total_duration']:.1f}s — messages may be buffered."
        )

    # 3. Text content grew across assistant partials (if we got multiple)
    ptl = stats["partial_text_lengths"]
    if len(ptl) >= 3:
        early = ptl[len(ptl) // 4]
        late = ptl[3 * len(ptl) // 4]
        assert late > early, (
            f"[{label}] Text not growing: 25th%={early}, 75th%={late}. "
            "Partials may not contain incremental content."
        )

    # 4. No gap between consecutive messages is anywhere near the stall timeout
    assert stats["max_gap"] < MAX_ACCEPTABLE_GAP_S, (
        f"[{label}] Max gap was {stats['max_gap']:.1f}s — "
        f"exceeds {MAX_ACCEPTABLE_GAP_S}s threshold."
    )

    # 5. Sanity: got a proper result and substantial text
    assert isinstance(stats["messages"][-1], ResultMessage), (
        f"[{label}] Last message should be ResultMessage."
    )
    final_text = max(stats["text_lengths"]) if stats["text_lengths"] else 0
    assert final_text > 500, (
        f"[{label}] Only {final_text} chars — expected a long response."
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sonnet_streams_partial_messages():
    """Sonnet: partial messages arrive incrementally during long generation."""
    stats = await _run_streaming_test("claude-sonnet-4-6")
    _print_stats("Sonnet streaming", stats)
    _assert_streaming(stats, "Sonnet")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_opus_streams_partial_messages():
    """Opus 4.6: partial messages arrive incrementally during long generation.

    This is the model that was stalling in swift-field's session.
    """
    stats = await _run_streaming_test("claude-opus-4-6")
    _print_stats("Opus 4.6 streaming", stats)
    _assert_streaming(stats, "Opus 4.6")

"""Tests for steering message injection (Pi-style semantics).

Steering messages are user input typed while the agent is receiving.
They're injected as real ``role: user`` turns by aborting the current
turn at a tool boundary (PostToolUse hook returns ``continue_=False``)
and letting the harness re-send the queued text as a proper user message.
"""

import pytest

from kiln.hooks import create_steering_hook


@pytest.mark.asyncio
async def test_steering_hook_empty_queue_returns_passthrough():
    """Empty queue → hook is a no-op."""
    queue: list[str] = []
    hook = create_steering_hook(queue)
    result = await hook({}, None, None)
    assert result == {}


@pytest.mark.asyncio
async def test_steering_hook_nonempty_aborts_turn():
    """Non-empty queue → hook aborts the turn so harness can inject.

    The hook does NOT drain the queue itself — that's the harness's job
    in ``receive()``. It just signals that the current turn should stop.
    """
    queue = ["reconsider the approach", "and check the tests"]
    hook = create_steering_hook(queue)
    result = await hook({}, None, None)

    assert result.get("continue_") is False
    ctx = result.get("hookSpecificOutput", {}).get("additionalContext", "")
    assert "steering" in ctx.lower()
    # Hook must not mutate the queue — the harness reads it.
    assert queue == ["reconsider the approach", "and check the tests"]


@pytest.mark.asyncio
async def test_steering_hook_is_idempotent():
    """Calling the hook multiple times with non-empty queue returns the
    same signal each time. No mutation, no accumulation."""
    queue = ["one"]
    hook = create_steering_hook(queue)
    r1 = await hook({}, None, None)
    r2 = await hook({}, None, None)
    assert r1 == r2
    assert queue == ["one"]


def test_steering_delivery_config_default():
    """Default delivery mode is 'all' — the config knob exists and parses."""
    from kiln.config import AgentConfig

    cfg = AgentConfig(name="test", home="/tmp")
    assert cfg.steering_delivery == "all"


def test_steering_delivery_config_one_at_a_time():
    """'one-at-a-time' is a valid alternative."""
    from kiln.config import AgentConfig, _apply_raw_fields

    cfg = AgentConfig(name="test", home="/tmp")
    _apply_raw_fields(cfg, {"steering_delivery": "one-at-a-time"})
    assert cfg.steering_delivery == "one-at-a-time"


def test_steering_delivery_config_dash_alias():
    """kebab-case spelling works too."""
    from kiln.config import AgentConfig, _apply_raw_fields

    cfg = AgentConfig(name="test", home="/tmp")
    _apply_raw_fields(cfg, {"steering-delivery": "one-at-a-time"})
    assert cfg.steering_delivery == "one-at-a-time"


def test_steering_delivery_config_invalid_ignored():
    """Invalid values are silently ignored (stick with default)."""
    from kiln.config import AgentConfig, _apply_raw_fields

    cfg = AgentConfig(name="test", home="/tmp")
    _apply_raw_fields(cfg, {"steering_delivery": "every-other-tuesday"})
    assert cfg.steering_delivery == "all"
